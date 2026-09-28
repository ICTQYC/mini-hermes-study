"""会话存储门面（对应 Hermes 的 `hermes_state.py`）。

第 6 章做的事：**把 JSON 换成 SQLite**。四个变化，每个都有可测量的收益：

| | 旧的 JSON 版 | 现在 |
|---|---|---|
| 落盘 | 整份重写（每次 O(整个会话)） | **逐条追加**（每次 O(1)） |
| 检索 | 一堆文件里 `grep` | FTS5 一条 SQL（跨会话） |
| 并发写 | 后写者覆盖先写者 | WAL + 显式事务 + 抖动重试 |
| 血缘 | 无（旧 docstring 只是许诺过 "lineage"） | `parent_session_id` 链（压缩轮转真会产生） |

★ **结构抄的是真实版的切法**：门面 + 兄弟模块（mixin 拼装）。
  真实版 `hermes_state.py:441` 一口气继承 13 个 mixin；mini 留 4 个：

      class SessionDB(SessionSessionsMixin, SessionMessagesMixin,
                      SessionSearchMixin, SessionSchemaMixin):

  门面自己只管**连接、事务、生命周期**（"怎么和 SQLite 说话"），
  业务按主题分在兄弟里 —— 一个主题一个文件，谁都不许长成上帝文件。

★ **写入礼节**（`_execute_write`）以真实版 `hermes_state.py:920-960` 为准：
  `BEGIN IMMEDIATE` + 两档抖动重试 + 每 50 次写一次 PASSIVE checkpoint。
"""
from __future__ import annotations

import random
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from hermes_constants import state_db_path
from hermes_state_common import (
    CHECKPOINT_EVERY_N_WRITES,
    SQLITE_TIMEOUT_S,
    WRITE_PATIENCE_S,
    WRITE_RETRY_MAX_S,
    WRITE_RETRY_MIN_S,
    WRITE_RETRY_SLOW_AFTER_S,
    WRITE_RETRY_SLOW_MAX_S,
    WRITE_RETRY_SLOW_MIN_S,
)
from hermes_state_messages import SessionMessagesMixin
from hermes_state_schema import SessionSchemaMixin
from hermes_state_search import SessionSearchMixin
from hermes_state_sessions import SessionSessionsMixin

T = TypeVar("T")

# SQLite 的"锁被人占着"：5 = SQLITE_BUSY，6 = SQLITE_LOCKED
# ⚠️ 错误码和文本两条路都要认：`sqlite_errorcode` 是 Python 3.11 才有的属性，
#    而 mini 要同时支持 3.9（`/usr/bin/python3`）与 3.13。
_BUSY_CODES = (5, 6)
_BUSY_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
)


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    if getattr(exc, "sqlite_errorcode", None) in _BUSY_CODES:
        return True
    text = str(exc).lower()
    return any(m in text for m in _BUSY_MARKERS)


class SessionDB(SessionSessionsMixin, SessionMessagesMixin,
                SessionSearchMixin, SessionSchemaMixin):
    """会话数据库（门面）。

    用法::

        db = SessionDB()                  # $MINI_HOME/state.db
        db.create_session(sid, "cli", model="deepseek-chat")
        db.append_message(sid, "user", "你好")
        db.search_messages("你好")
        db.close()
    """

    def __init__(self, db_path: Optional[Any] = None, read_only: bool = False):
        self.db_path = Path(db_path) if db_path is not None else state_db_path()
        self.read_only = read_only
        self._write_count = 0
        self._wal_active = False
        # 同进程多线程（第 2 章的并发工具执行）会同时碰这个连接 → 自己串行化写入。
        # 真实版对应的是 `check_same_thread=False` + `_execute_write` 里的锁。
        self._lock = threading.RLock()
        if not read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()
        if not read_only:
            self._ensure_schema()

    # ── 连接 ───────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        """建连接 + 设 pragma。

        三个参数都不是默认值，各有理由（真实版 `hermes_state.py:731`）：

        | 参数 | 值 | 为什么 |
        |---|---|---|
        | `timeout` | **1.0**（默认 30） | 长等会把"谁先拿锁"拖成秒级；真正解决争用靠应用层重试 |
        | `isolation_level` | **None** | 关掉 Python 的隐式事务，改成**自己显式** `BEGIN IMMEDIATE` |
        | `check_same_thread` | **False** | 工具是并发线程执行的；串行化由 `self._lock` 负责 |
        """
        if self.read_only:
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True,
                check_same_thread=False, timeout=SQLITE_TIMEOUT_S, isolation_level=None,
            )
        else:
            conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False,
                timeout=SQLITE_TIMEOUT_S, isolation_level=None,
            )
        conn.row_factory = sqlite3.Row
        if not self.read_only:
            # ★ 不信 PRAGMA 的"我设成功了"—— 回头**读一次**磁盘上的真实模式。
            #   真实版为此专门有 `_on_disk_journal_mode()` 检查（`hermes_state.py:740`）：
            #   WAL 在某些文件系统（网络盘）上会静默退回 delete 模式，
            #   而"以为自己是 WAL"会让并发行为完全不是你以为的那样。
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                self._wal_active = str(mode).lower() == "wal"
            except sqlite3.DatabaseError:
                self._wal_active = False
        return conn

    @property
    def wal_active(self) -> bool:
        """WAL 是不是**真的**生效了（不是"我们设过"，而是"磁盘上现在是"）。"""
        return self._wal_active

    # ── 写入：BEGIN IMMEDIATE + 抖动重试 ───────────────────────────────
    def _execute_write(self, fn: Callable[[sqlite3.Connection], T],
                       patience_s: Optional[float] = None) -> T:
        """在 `BEGIN IMMEDIATE` 里跑 `fn`，撞锁就抖动着重试（**整块**重试）。

        ★ 三条纪律（真实版 docstring 逐字，`hermes_state.py:925-931`）：

          `BEGIN IMMEDIATE takes the WAL write lock up front so contention surfaces
          immediately; on locked/busy the Python lock is released, a jitter slept,
          and the WHOLE callback retried — *fn* must stay idempotent under retry.`

        1. **`BEGIN IMMEDIATE` 先拿写锁**：争用在**事务开始**就暴露，
           而不是在中间某条语句上炸（那时你已经改了一半）。
        2. **整块重试**，不是"重试那一条语句" → 所以 `fn` 必须**幂等**
           （它可能被完整地跑第二遍）。
        3. **提交/回滚在这里**：调用方**不许**自己 commit（不然事务边界就散了）。
        """
        patience = WRITE_PATIENCE_S if patience_s is None else float(patience_s)
        started = time.monotonic()
        deadline = started + patience
        while True:
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.execute("COMMIT")
                    except BaseException:
                        self._conn.execute("ROLLBACK")
                        raise
                self._write_count += 1
                if self._write_count % CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                if not _is_busy_error(exc):
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(self._jitter_delay(time.monotonic() - started))

    @staticmethod
    def _jitter_delay(elapsed: float) -> float:
        """两档随机抖动：前 `WRITE_RETRY_SLOW_AFTER_S` 秒用快档，之后换慢档。

        ★ 为什么必须**随机**：SQLite 内部退避是确定性的，所有竞争的写者会在同一间隔上
          一起重试、一起失败 —— 越重试越同步（车队效应）。随机化就是把这支车队打散。
        """
        if elapsed < WRITE_RETRY_SLOW_AFTER_S:
            return random.uniform(WRITE_RETRY_MIN_S, WRITE_RETRY_MAX_S)
        return random.uniform(WRITE_RETRY_SLOW_MIN_S, WRITE_RETRY_SLOW_MAX_S)

    def _try_wal_checkpoint(self) -> None:
        """每 50 次成功写入做一次 PASSIVE checkpoint（best-effort，失败不影响写入）。

        PASSIVE = "能收就收，不因为别人在读而阻塞"。在写入路径上做可能长时间阻塞的事，
        等于拉长持锁时间、放大争用。
        """
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.DatabaseError:
            pass

    @property
    def write_count(self) -> int:
        """成功写入次数（测试用它验证"第 50 次触发了 checkpoint"）。"""
        return self._write_count

    # ── 生命周期 ───────────────────────────────────────────────────────
    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "SessionDB":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def new_session_id() -> str:
    """新会话 id：`20260926_153012_a1b2c3`（时间戳 + 随机尾巴，可排序又可读）。"""
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
