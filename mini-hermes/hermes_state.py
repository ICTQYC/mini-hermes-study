"""会话存储门面（对应 Hermes 的 `hermes_state.py`）。

第 6 章做的事：**把 JSON 换成 SQLite**。四个变化，每个都有可测量的收益：

| | 旧的 JSON 版 | 现在 |
|---|---|---|
| 落盘 | 整份重写（每次 O(整个会话)） | **逐条追加**（每次 O(1)） |
| 检索 | 一堆文件里 `grep` | FTS5 一条 SQL（跨会话） |
| 并发写 | 后写者覆盖先写者 | WAL + 显式事务 + 抖动重试 |
| 血缘 | 无（旧 docstring 只是许诺过 "lineage"） | `parent_session_id` 链（压缩轮转真会产生） |

★ **结构抄的是真实版的切法**：门面 + 兄弟模块（mixin 拼装）。
  真实版 `hermes_state.py:441` 一口气继承 13 个 mixin；mini 留 5 个：

      class SessionDB(SessionSessionsMixin, SessionMessagesMixin, SessionFtsMixin,
                      SessionSearchMixin, SessionSchemaMixin):

  门面自己只管**连接、事务、生命周期**（"怎么和 SQLite 说话"），
  业务按主题分在兄弟里 —— 一个主题一个文件，谁都不许长成上帝文件。
  （第 11 章加的就是 `SessionFtsMixin`：`hermes_state_fts.py` —— "派生索引怎么降级、
   怎么回来"是一个完整主题，不该挤进门面。）

★ **写入礼节**（`_execute_write`）以真实版 `hermes_state.py:920-960` 为准：
  `BEGIN IMMEDIATE` + 两档抖动重试 + 每 50 次写一次 PASSIVE checkpoint。
  第 11 章在这里加了**第二条重试理由**：FTS 派生索引损坏 ⇒ 摘掉派生索引后
  **重跑同一个 `fn`**（真实版 `hermes_state.py:1020-1035`）。见 `_degrade_or_raise_on_corruption`。

★ 第 11 章的 §2（**库文件本身损坏**）做的是**隔离那个句柄**（quarantine）——"停写就是保护"：

  | 隔离动作 | mini 落点 | 真实版 |
  |---|---|---|
  | ① 置**粘性**标志 + 记原因 | `_halt_db_corrupt` | `hermes_state.py:1249-1267` |
  | ② **写前守卫**（之后任何写入立即失败、不碰文件） | `_raise_if_db_corrupt`（调用点：`_execute_write` 每轮 while 顶部） | `:1340-1343`，调用 `:942` |
  | ③ `close()` 后**拒绝重开** | `_reopen_after_close` | `_reopen_after_close_locked` `:888/900-904` |
  | ④ `close()` **跳过**显式 WAL checkpoint | `close()` | `:1449-1457` |

  为什么"停写"排第一、跳 checkpoint 只算第二道防线：真实现场记录（`StateDbCorruptError`
  的 docstring）——第一次报结构损坏后网关又重试写了约 50 分钟，SIGTERM 时那颗关停 checkpoint
  把 15 页写到错误页号，把"还能读"的文件写成了 `file is not a database`。

  ⚠️ 真实版还用 `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`（Python 3.12+ `Connection.setconfig`）
  把 **SQLite 自己那次内部关停 checkpoint** 也关掉（真实版 `:405-408/1267/1275-1281`）；
  **mini 未实现它（B8）**——没有显式 checkpoint 不代表 SQLite 不会在最后连接关闭时
  自动 checkpoint；关闭内部检查点仍是保护差距，见 `close()` 的 docstring。
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
from hermes_state_errors import (
    _STATE_DB_CORRUPT_MSG,
    StateDbCorruptError,
    is_structural_corruption_error,
)
from hermes_state_fts import SessionFtsMixin
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


class SessionDB(SessionSessionsMixin, SessionMessagesMixin, SessionFtsMixin,
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
        # ── 第 11 章：FTS 降级状态（两个"标志"分工不同，别混）──────────────
        #   `_fts_enabled`  —— 本进程此刻**还愿不愿意**用 FTS 同步（fail-open 后为 False）
        #   `_fts_stale`    —— 权威表里**有没有**那段"范围未知的空窗"（持久事实）
        # 真实版同名两个属性（`hermes_state.py` / `hermes_state_search.py:1085`）。
        self._fts_enabled = True
        self._fts_stale = False
        # ── 第 11 章 A7：结构损坏的**粘性隔离**（sticky quarantine）────────────
        #   `_db_corrupt`        —— 一旦置真，**整个句柄生命期内保持真**（粘性）
        #   `_db_corrupt_reason` —— 记下第一次为了什么被隔离（给 close / 诊断用）
        #   真实版同位置逐字（`hermes_state.py:574`）：
        #     self._db_corrupt, self._db_corrupt_reason = False, ""  # sticky quarantine (StateDbCorruptError)
        self._db_corrupt, self._db_corrupt_reason = False, ""
        # `close()` 之后拒绝重开（真实版 `_reopen_after_close_locked` `:888/900`）
        self._closed = False
        if not read_only and self.db_path.is_file():
            # 写连接会切换 WAL，故版本门控先通过独立只读连接完成。
            probe = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True,
                                    timeout=SQLITE_TIMEOUT_S)
            try:
                self._check_schema_version(probe)
            finally:
                probe.close()
        if not read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()
        try:
            if not read_only:
                self._ensure_schema()
        except BaseException:
            self._conn.close()
            raise
        # ★ 开库时**读一次**降级标记 ⇒ 置内存标志（A2）。
        #   读的是 `state_meta` 里的持久标记，不是"上次进程的内存" —— 所以跨重启可见。
        self._fts_stale = self._read_fts_stale_marker()
        if self._fts_stale:
            self._fts_enabled = False
            # ★ 保持摘除：`_ensure_schema()` 里的 `CREATE TRIGGER IF NOT EXISTS` 会
            #   把触发器**复活**（SQL 是幂等的，它不管"上次是谁摘的"）。
            #   若不在开库时再摘一次，那个"触发器缺席期间写进来的行补不进索引"的空窗
            #   就永远补不上了 —— 这条对应真实版 `hermes_state_schema.py:958-963`
            #   （`if self._fts_stale: self._drop_all_fts_triggers(cursor)`）。
            #   ⚠️ 真实的"开库受守卫重建"（准入锁 + 外部持有者守卫）留给下一轮；
            #      这里只做"标记还在 ⇒ 保持摘除"这一半，语义上已经是**更保守**的那侧。
            if not read_only:
                self.drop_all_fts_triggers()

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
                self.db_path.resolve().as_uri() + "?mode=ro", uri=True,
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

        ★ 第 11 章加进来的第二条重试理由（A4）：**FTS 派生索引损坏**。
          一条 canonical 写入会被 `messages` 上那三个同步触发器拖进坏影子表而整条失败 ——
          这时要做的不是"把错误交给调用方"，而是
          **摘掉派生索引 → 重跑同一个 `fn`**（此时写不再触达坏影子表，就写成了）。
          真实版就是在这里接的线（`hermes_state.py:1020-1035`），
          判据逐字：`detach the derived indexes atomically and retry (never rebuild here).`
        """
        patience = WRITE_PATIENCE_S if patience_s is None else float(patience_s)
        started = time.monotonic()
        deadline = started + patience
        while True:
            try:
                with self._lock:
                    # ★ 第 11 章 A7 写前守卫（真实版 `hermes_state.py:942` 每轮 while 顶部）：
                    #   已隔离 ⇒ 立即失败，**碰都不碰文件**（"停写就是保护"）。
                    self._raise_if_db_corrupt()
                    # ★ A7：close() 之后的重开（真实版 `_reopen_after_close_locked`）。
                    #   被隔离的句柄在上面那句就已经抛了；这里只处理**健康**句柄的懒重开。
                    if self._closed:
                        self._reopen_after_close("write")
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
                    # ★ 不是锁 —— 但**不能直接抛**：`OperationalError` 也是 `DatabaseError`
                    #   的子类，损坏有可能以这个类型出现（except 子句自上而下匹配，
                    #   下面那个 DatabaseError 分支就再也轮不到它）。所以先走一次损坏分岔口。
                    if self._degrade_or_raise_on_corruption(exc):
                        continue
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(self._jitter_delay(time.monotonic() - started))
            except sqlite3.DatabaseError as exc:
                # ── 第 11 章的分岔口（真实版 `hermes_state.py:1020-1035`）────────
                #   `IntegrityError`（唯一约束等）也是 DatabaseError 的子类：
                #   它既不是 FTS 归因、也不在"结构性损坏"桶里 ⇒ 原样抛出，行为不变。
                if self._degrade_or_raise_on_corruption(exc):
                    continue
                raise

    def _degrade_or_raise_on_corruption(self, exc: BaseException) -> bool:
        """坏的是**哪一层**，决定"接着写"还是"停下"（A4 的核心判断，真实版 `:1020-1035`）。

        返回值语义：

        | 返回 | 意思 |
        |---|---|
        | `True` | 已 fail-open（摘掉了派生索引）⇒ 调用方 **`continue` 重跑同一个 `fn`** |
        | `False` | 这次不是损坏 ⇒ 调用方**原样抛**（`IntegrityError`、语法错等走这里） |
        | 抛 `StateDbCorruptError` | 结构性损坏 ⇒ **不重试**，立即失败 |

        ★ 两桶是**互斥**的，不是程度差异：
          `matches_fts`（派生索引）⇒ fail-open；`bare CORRUPT/NOTADB`（文件本身）⇒ 停写。
          顺序错了就整条走错路（比如把"FTS 坏了"当"文件坏了"去停写 —— 一个其实健康的库被隔离）。
        """
        # ① FTS 派生索引损坏：权威行**没坏**，写不进去只是被同步触发器拖累
        #    ⇒ 同一事务里落标记 + 摘触发器（`enter_fts_fail_open` 自己开那个事务，
        #      不是这次失败的那个写入事务 —— 那个已经被 ROLLBACK 掉了）
        if self.enter_fts_fail_open(exc):
            return True
        # ② 剩下的损坏 = 结构化：此时"接着写"只会扩散损坏；**停写才是保护**。
        #    ★ 第 11 章 A7：先 `_halt_db_corrupt(exc)` 置**粘性隔离标志**、再抛
        #      （真实版 `:1033-1034` 逐字：`if self._is_structural_corruption_error(exc):
        #      self._halt_db_corrupt(exc)`）。置标志这一步是这一轮补上的 ——
        #      此后每一次写入都会在写前守卫（`_raise_if_db_corrupt`）上立即失败，
        #      而且**不碰文件**（不像"只抛一次、下次接着写"那样把损坏扩散出去）。
        #      `StateDbCorruptError` 不在两条重试分支里 ⇒ 失败写入**不重试**，立即向上抛。
        if is_structural_corruption_error(exc):
            self._halt_db_corrupt(exc)
        return False

    # ── 第 11 章 A7：结构损坏隔离（写前守卫 / 粘性标志 / 拒重开）────────────
    def _corrupt_error(self, prefix: str = "") -> StateDbCorruptError:
        """构造隔离异常（真实版 `hermes_state.py:1245` `_corrupt_error`）。"""
        return StateDbCorruptError(prefix + _STATE_DB_CORRUPT_MSG)

    def _quarantine_reason(self) -> Optional[str]:
        """隔离原因（真实版 `hermes_state.py:1362-1381`）：隔离中返回
        `structural corruption (<原因>)`，否则 ``None``。给 close() / 诊断用。"""
        if self._db_corrupt:
            return "structural corruption (%s)" % self._db_corrupt_reason
        return None

    def _halt_db_corrupt(self, exc: BaseException) -> None:
        """隔离这个句柄，然后抛（真实版 `hermes_state.py:1249-1267`）。

        真实版 docstring 逐字：`Quarantine this handle and raise; never run in-file repair here.`

        ★ **粘性（sticky）**：标志一旦置真就**永不复位**。整个句柄生命期内，之后每一次写入
          都会在 `_raise_if_db_corrupt`（写前守卫）上立即失败 —— 这就是"停写就是保护"。
        ★ 这里**绝不**做任何"就地修复"（真实版同句 `never run in-file repair here`）：
          一个活着的句柄没有能力安全地改自己的 B 树页；修复只能走**离线**路径
          （`hermes_state_repair`：raw-copy 备份 + FTS 重建）。
        """
        self._db_corrupt = True
        self._db_corrupt_reason = str(exc)
        raise self._corrupt_error() from exc

    def _raise_if_db_corrupt(self) -> None:
        """写前守卫（真实版 `hermes_state.py:1340-1343`，调用点 `:942`）。

        ★ 已隔离 ⇒ 立即抛 `StateDbCorruptError`，**不碰文件**。
          "不碰文件"才是重点：真实事故里第一次报结构损坏后，网关又重试写了约 50 分钟，
          把"还能读"的文件写成了 `file is not a database`。
        """
        if self._db_corrupt:
            raise self._corrupt_error()

    def _reopen_after_close(self, context: str = "write") -> sqlite3.Connection:
        """`close()` 之后重开连接的守卫（真实版 `_reopen_after_close_locked` `:888-904`）。

        真实版注释逐字（`:899`）：

            A reopen resolves the PATH again: a replaced file would be written through stale
            WAL/shm assumptions; a quarantined handle must never hand a fresh connection to a
            damaged file.

        （重开会把路径**再解析一遍**：一个被替换过的文件会顺着过期的 WAL/shm 假设被写；
        而被隔离的句柄，**绝不能**把一条新连接交到一个受损文件上。）

        mini 的连接在 `__init__` 只建一次；这里把"重开"这个动作**显式化**成一个方法：
        健康句柄照常重连（真实版是懒开），被隔离的句柄**拒绝**（抛 `StateDbCorruptError`）。
        """
        if self._db_corrupt:
            raise self._corrupt_error(
                "state.db 连接（%s）在 close() 之后已被隔离（结构性损坏）；"
                "拒绝为一次 %s 重开。" % (self.db_path, context)
            )
        self._closed = False
        self._conn = self._connect()
        return self._conn

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
        """关闭连接。★ 这条路径里不发显式 WAL checkpoint —— 上游 §2 的**第二道防线**。

        真实版 `close()`（`hermes_state.py:1449-1457`）在隔离中会**跳过**
        `PRAGMA wal_checkpoint(PASSIVE)`。现场记录（见 `StateDbCorruptError` 的 docstring）：
        第一次报结构性损坏后，网关仍重试写入约 50 分钟；SIGTERM 时那颗关停 checkpoint
        把 **15 页**写到错误的页号（第 1 页收到一个 `messages_fts_trigram_data` 叶子），
        把一个"还能读"的文件写成了 `file is not a database`。

        ⇒ 顺序是：**先"停写"（隔离），再"跳检查点"（本方法）**。mini 的 `close()` 从来就
        只做裸关、**不发显式 checkpoint** ⇒ 第二道防线满足；本方法把它**显式化**，
        而不是依赖"碰巧谁都没在关停时写过 checkpoint"。
        （真实版还要关掉 SQLite 自己那次内部关停 checkpoint：
          `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`，Python 3.12+；**mini 不实现 —— B8**，
          理由见模块 docstring 与 `hermes_state_errors.StateDbCorruptError`。）

        ★ 另一条不变量（真实版 `_reopen_after_close_locked` `:888/900`）：`close()` 之后，
          被隔离的句柄**拒绝重开**（见 `_reopen_after_close`）。
        """
        # 显式声明：这里不发显式 checkpoint，SQLite 内部自动 checkpoint 仍可能发生（对齐真实版 `:1449-1457` 的"跳过"分支）。
        self._closed = True
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
