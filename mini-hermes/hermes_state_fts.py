"""FTS 派生索引的 fail-open 与显式重建（对应 Hermes 的 `hermes_state_fts.py`）。

第 11 章最核心的一条：**"派生索引坏了" ≠ "写不进去"**。

```
一条 canonical 消息写入（gateway/flush 路径）
        │
        ▼
messages 上的 AFTER INSERT 触发器把同一份内容送进 messages_fts
        │
        ▼
messages_fts* 影子表坏了 ⇒ **整条语句**抛 sqlite3.DatabaseError
        │
        ├─ canonical 行本身**没坏**（它在 messages 里，好好的）
        └─ 但现在**每一条写入**都会被这个触发器拖死 —— 因为触发器在，
           `messages` 的任何 INSERT/DELETE/UPDATE 都要往坏影子表里写
```

所以处置只有一个方向：**把派生索引摘掉，让权威写入继续**。这就是 fail-open。

★ **"同一个事务"到底指哪一个？**（这条最容易读错）

指的是 `enter_fts_fail_open()` **自己开的那个** `BEGIN IMMEDIATE`
（真实版 `hermes_state_fts.py:363`），它把三件事圈在一个事务里提交：

    ① 写 `state_meta(key='fts_stale', value='1')`  ← 面包屑
    ② DROP 三个同步触发器                        ← 摘除
    ③ COMMIT                                     ← 原子生效

**不是**那条失败的 canonical 写入事务 —— 那条**已经被 SQLite 回滚/放弃了**
（`_execute_write` 的 `except` 里已经 `ROLLBACK`）。fail-open 是**另起一个事务**去收拾残局，
收拾完由外层重试环 `continue` 去**重跑同一个写入**。

为什么两者必须同事务：崩在①②之间会留下"摘了触发器但没留面包屑"的状态 ——
下次开库会以为索引是完整的，把触发器装回去，于是"触发器缺席期间写进来的行"
永远补不进索引（空窗）。同事务 ⇒ 要么都发生、要么都没发生，**崩也只会更保守**。

★ **A6 不变量：活路径绝不允许跑 `FTS5('rebuild')`。** 真实侧三处逐字引文：

1. 写路径 `hermes_state.py:1028-1029`：
   `... detach the derived indexes atomically and retry (never rebuild here).`
2. 搜索 `hermes_state_search.py:918-920`：
   `... a live search never runs the unbounded rebuild.`
3. 本文件 `hermes_state_fts.py:354-356`：
   `once triggers are absent the index has a gap of unknown extent, so nobody may
   reinstall them without a full rebuild.`

理由（真实版 `hermes_state_fts.py:405-407` 的现场数据）：一次阻塞式 rebuild 在 25GB 的库上
**持写锁约 16 分钟**。把这种操作塞进"一次用户消息写入"或"一次搜索"，等于把一次毫秒级动作
变成十几分钟挂起，还和前台并发写互相锁死。
所以 `rebuild` 只属于**显式的恢复路径** —— 就是本文件的 `rebuild_fts()`：
它不在任何活写/活搜的路径上，只能被显式调用。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from hermes_state_common import (
    FTS_STALE_KEY,
    FTS_TRIGGER_STATEMENTS,
    _FTS_TRIGGERS,
)
from hermes_state_errors import StateDbCorruptError, is_fts_scoped_corruption_error

# `state_meta` 的 UPSERT 写法（真实版 `hermes_state_schema.py:129-131` 逐字）：
#
#     _STALE_KEY_UPSERT_SQL = (
#         "INSERT INTO state_meta (key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value = excluded.value"
#     )
#
# ★ 为什么用 UPSERT 而不是先 SELECT 再 INSERT/DELETE：
#   标记是"设置位"，重复设置必须无害（可能连续两次 fail-open）。
#   一条语句搞定 ⇒ 没有"查到了 → 别人删了 → 我插进去"的中间窗口。
_STALE_KEY_UPSERT_SQL = (
    "INSERT INTO state_meta (key, value) VALUES (?, '1') "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
)


class SessionFtsMixin:
    """给 `SessionDB` 用的 FTS fail-open / 标记 / 重建部分。

    ★ 抄的是真实版的**切法**：`hermes_state_fts.py` 单独一个文件 ——
      "派生索引怎么降级、怎么回来"是一个完整主题，不该挤进 `hermes_state.py` 那个门面里
      （真实版的门面一口气继承 13 个 mixin，mini 现在有 5 个）。
    """

    _conn: sqlite3.Connection

    # ── `state_meta` 上那一个标记：读 / 写 / 清 ──────────────────────────
    def _read_fts_stale_marker(self) -> bool:
        """读降级标记（真实版 `hermes_state_schema.py:959-960`：`stale_row is not None`）。

        ★ 读它在**开库时**做一次：标记是"跨重启可见"的载体，读它就等于问
          "上一次那个进程，把 FTS 摘了吗？"。老库（还没有 `state_meta` 表）读不到 ⇒ False。
        """
        try:
            row = self._conn.execute(
                "SELECT 1 FROM state_meta WHERE key = ? LIMIT 1", (FTS_STALE_KEY,)
            ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    def _write_fts_stale_marker(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """落标记：`state_meta(key='fts_stale', value='1')`（真实版写入点 `hermes_state_fts.py:365-369`）。

        `conn` 可传，是为了让调用方把它放进**自己那个事务**里（fail-open 就要求这样）。
        """
        target = self._conn if conn is None else conn
        target.execute(_STALE_KEY_UPSERT_SQL, (FTS_STALE_KEY,))

    def _clear_fts_stale_marker(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """清标记 = **删行**（真实版删除点唯一：成功重建时，`hermes_state_schema.py:650`）。

        ★ 反向纪律：**defer / 重建失败时一律保留标记**。所以"标记还在"这个事实
          恒等于"FTS 仍是摘除状态"—— 别的读者（搜索路径）就靠它切到 LIKE。
        """
        target = self._conn if conn is None else conn
        target.execute("DELETE FROM state_meta WHERE key = ?", (FTS_STALE_KEY,))

    # ── 摘触发器 ────────────────────────────────────────────────────────
    @staticmethod
    def _drop_fts_triggers(cursor: sqlite3.Cursor) -> None:
        """逐个 `DROP TRIGGER IF EXISTS`（真实版 `hermes_state_fts.py:319-325`）。

        ★ `IF EXISTS` 是必须的：**重复摘除无害**是被依赖的性质（可能连续两次 fail-open）。
        """
        for trigger in _FTS_TRIGGERS:
            try:
                cursor.execute("DROP TRIGGER IF EXISTS %s" % trigger)
            except sqlite3.OperationalError:
                pass

    def drop_all_fts_triggers(self) -> None:
        """把**整套**同步触发器摘掉（真实版 `hermes_state_schema.py:212-216`）。

        真实版的"整套"= base 3 + trigram 3 + CJK 3（三套都摘）；
        mini 只有 base 一套，所以这里就是那三个 —— 但名字照抄（`drop_all` 的语义是
        "别只摘一半"，将来 mini 加第二套索引时，摘除点不用改）。
        """
        self._drop_fts_triggers(self._conn.cursor())

    # ── ★ 本章核心：fail-open ───────────────────────────────────────────
    def enter_fts_fail_open(self, exc: BaseException) -> bool:
        """摘掉损坏的 FTS 索引，让 canonical 写入继续（真实版 `hermes_state_fts.py:353-403`）。

        真实版 docstring 逐字（`:354-356`）：

            Detach corrupt FTS indexes so canonical writes can continue. Breadcrumb +
            trigger drop commit atomically: once triggers are absent the index has a
            gap of unknown extent, so nobody may reinstall them without a full rebuild.

        返回 ``True`` = "已经降级完成，**重跑同一个写入就行**"；
        ``False`` = "这次不是 FTS 派生损坏（或本来就已经摘了），不归我管"。

        ★ 门禁（真实版 `:357-358`）：**只对 FTS 归因的损坏动手**。
          分不清就不要乱摘 —— 摘错了就把一个健康的索引整成了空窗。
        ★ 事务形状（真实版 `:362-386`）：`BEGIN IMMEDIATE` → 落标记 → 摘触发器 → commit。
          失败则 rollback 后原样抛（调用方看到的是"没降级成功"，于是写路径照旧失败）。
        ★ 这里**绝不**跑 `FTS5('rebuild')`（见模块 docstring 的 A6）。
        """
        if not self._fts_enabled or not is_fts_scoped_corruption_error(exc):
            return False
        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    # ① 面包屑（与摘除**同事务**，这是本机制的全部要点）
                    self._write_fts_stale_marker(self._conn)
                    # ② 摘掉整套同步触发器：此后 canonical 写入不再触达坏影子表
                    self._drop_fts_triggers(self._conn.cursor())
                    # ③ 原子生效
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
        except sqlite3.Error:
            return False
        # 内存标记（跨重启那份在 state_meta 里；这份只是本进程的当下状态）
        self._fts_stale = True
        self._fts_enabled = False
        return True

    # ── 显式恢复路径 ────────────────────────────────────────────────────
    def rebuild_fts(self) -> None:
        """全量重建 FTS 索引 → 装回触发器 → 清标记。**这是唯一允许 rebuild 的地方。**

        ★ 顺序不能换，每一步都有理由：

            ① INSERT INTO messages_fts(messages_fts) VALUES('rebuild')   全量重扫 messages
            ② 装回三个触发器（此刻索引才"完整"）                          —— 见 FTS_TRIGGER_STATEMENTS
            ③ DELETE state_meta 里的 fts_stale 标记                      —— 最后一步

        为什么 ② 在 ① 之后：触发器一装上，新写入就会同步；索引还没重建完就装触发器，
        等于把"空窗"钉死在索引里。
        为什么 ③ 在最后：若崩在 ② 与 ③ 之间，标记还在 ⇒ 下次开库仍然认为"摘除中" ⇒
        只会更保守（重跑一次重建），不会把有空洞的索引当成完整的。**塌向安全的那一边**。

        ★ 整段跑在一个 `_execute_write` 事务里：重建要么整体成立、要么整体回滚
          （真实版同样把它包在一个 `BEGIN IMMEDIATE` 写事务里，`hermes_state_schema.py:649`）。
        """
        def _do(conn: sqlite3.Connection) -> None:
            # FTS5 的内建维护命令：按外部内容表（content='messages'）全量重扫重建索引。
            # 真实版 `hermes_state_schema.py:644` 逐字同一句：
            #   rebuild_sql += "INSERT INTO messages_fts(messages_fts) VALUES('rebuild');"
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            for statement in FTS_TRIGGER_STATEMENTS:
                conn.execute(statement)
            conn.execute("DELETE FROM state_meta WHERE key = ?", (FTS_STALE_KEY,))

        self._execute_write(_do)
        # 内存状态跟上：索引完整 + 标记没了
        self._fts_stale = False
        self._fts_enabled = True


__all__ = ["SessionFtsMixin", "StateDbCorruptError"]
