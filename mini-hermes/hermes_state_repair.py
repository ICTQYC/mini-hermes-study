"""state.db 的显式修复（对应 Hermes 的 `hermes_state_repair.py`）。

第 11 章 §3：**一个"真坏掉"的库怎么自己修回来**。这条链在真实版是一条长梯子，
mini 只保留其中**足够复演 §3 外观**的那几级。逐条列清楚"真实版有、mini 没有、为什么"：

| 真实版机制 | 真实实现（`f5d1926110`） | mini | 为什么 |
|---|---|---|---|
| 跨进程准入锁 | `_cross_process_repair_lock`（`hermes_state_repair.py:164`，flock） | **不做**（B1） | mini 单进程，没有第二个 repairer 可竞争 |
| 外部持有者 fail-closed | `_live_writer_holds_db`（`:827`）+ `_exclusive_repair_db_guard`（`:641`） | **不做**（B2） | Linux `/proc/<pid>/fd` 侦查 + 多进程处境；mini 里"另一个进程持有我的库"不存在 |
| `state-snapshots/` 快照目录 | `hermes_cli/backup.py:37/1212/1255` | **不做**（B6） | 运维设施，与"恢复机制的形状"无关 |
| 内部在线备份 API 快照 | `_copy_database_snapshot`（`:664`）+ `source.backup(pages=256)`（`:684`） | **不做**（B5） | mini 用 raw-copy 备份就够；`pages=`/进度/截止是给长库修复用的 |
| 受守卫的 snapshot-and-promotion | `_repair_state_db_schema_locked`（`:966`）：scratch 副本 → 跑策略 → **事务提升**（禁 `os.replace`） | **简化掉** | mini 的修复是"重建 FTS 索引 + 装回触发器 + 清标记"，**不动 canonical 数据** ⇒ 不需要 scratch 副本、也不需要把结果提升回主文件 |
| 修复策略阶梯 | `_REPAIR_STRATEGIES`（`:1120`：`rebuild_fts` / `reindex_btree` / `dedup_schema` / `drop_fts_rebuild`+VACUUM） | **只留 `rebuild_fts`** | 其余策略是给"B 树 / schema 损坏"用的，而那类损坏 mini 明确**不自动修**（见下） |
| 修复次数账本 / 备份去重 / 保留上限 | `.repair-attempts.json` + `_MAX_MALFORMED_BACKUPS = 3`（`:37-47`） | **不做** | 纯运维防御（一次真实事故攒了 89GB 雷同备份），mini 不需要 |

★ mini 的两条判定（照真实版）：

1. **`_db_opens_cleanly` 问的是"能不能打开、schema 能不能解析"**（真实版 `:748`）——
   它**不**跑全量 `PRAGMA integrity_check` 的 B 树体检：FTS 影子表坏掉**不会**让 schema
   解析失败（虚拟表的 DDL 还在 `sqlite_master` 里），而"schema 能读"恰恰是"允许走 FTS
   重建"的前提。反过来，**文件级**损坏（第一页坏）会让**第一条语句**都抛
   `file is not a database` ⇒ 被这里拦下。
2. **结构性损坏（B 树 / schema 本身坏）mini 不自动修**。真实 `doctor` 的判定逐字
   （`hermes_cli/doctor_state.py:207-212`）：

       state.db structural corruption (canonical tables/indexes damaged, not the FTS index) —
       an FTS rebuild cannot repair it. ... Do NOT restore a .malformed-backup copy beside
       state.db: it is a snapshot of the same corrupt file.

   ⇒ `repair_state_db_schema` 这种情况**直接返回失败、不装作能修**。
   （这也是 raw-copy 备份叫 `.malformed-backup-*` 的原因：它是**取证镜像**，不是"修好的库"。）

★ 文档 §3 的**三段验证查询**（照现实抄，逐字判据）：
  ① 标记查询应**零行**（`fts_stale` 行没了）·
  ② 预期触发器**在场** ·
  ③ 权威行数**不得下降**。
  三段全过才叫"修好了"。
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from hermes_state_common import FTS_STALE_KEY, SQLITE_TIMEOUT_S, _FTS_TRIGGERS

# 库的 sidecar 后缀（真实版 `hermes_state_repair.py:50` 逐字：`_DB_SIDECAR_SUFFIXES`）。
# ★ raw-copy 必须把这三件**一起**拷 —— 它们是**同一份 SQLite 在线镜像**：
#   分开拷 = "新主文件 + 外来旧 WAL" = 文档 §3 说的 `malformed`，或凭空复活旧行。
_DB_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _read_only_connection(db_path) -> sqlite3.Connection:
    """开一条**只读**连接（体检 / 验证查询都在它上面跑，绝不改文件）。"""
    return sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=SQLITE_TIMEOUT_S)


# ── ① 健康探针 ────────────────────────────────────────────────────────────
def _db_opens_cleanly(db_path) -> Optional[str]:
    """健康探针（真实版 `hermes_state_repair.py:748` 的 mini 版）。

    ``None`` = "能打开、schema 能解析"；否则返回人类可读的原因字符串。

    ★ 为什么**不**跑全量 `PRAGMA integrity_check`：见模块 docstring 的判定 1 ——
      "schema 能读"恰好是允许走 FTS 重建的前提；把 FTS 影子表损坏也算成"打不开"，
      就会把唯一能修的情形挡在门外。真正的**结构性**损坏会让第一条语句都抛。
    """
    path = Path(db_path)
    if not path.exists():
        return "库文件不存在：%s" % path
    try:
        conn = _read_only_connection(path)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()   # schema 能不能解析
    except sqlite3.Error as exc:
        return "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            conn.close()
        except Exception:                                  # noqa: BLE001
            pass
    return None


# ── ② 只读体检 ────────────────────────────────────────────────────────────
def inspect_state_db(db_path) -> Dict[str, Any]:
    """`--check-only` 用的**只读**体检：健康探针 + 标记 + 触发器集合 + 权威行数。

    判定"健康"的口径（三条必须同时成立）：
      · 健康探针通过（能打开、schema 能解析）；
      · `state_meta` 里**没有** `fts_stale` 标记（标记在 ⇒ FTS 处于摘除/降级态）；
      · 三个同步触发器**全在场**（缺一个 ⇒ 有写入路径没在同步索引）。
    "不健康"分两种，病因不同、出路也不同：
      · 打不开 ⇒ **结构性损坏**，FTS 重建修不了（走离线恢复/从备份恢复）；
      · 能开但标记在/触发器缺 ⇒ **FTS 降级**，`repair` 能修（重建索引）。
    ★ 第 11 章修：**逐项隔离 + 旧库要认得出来**（见下面那段注释里的真实事故）。
    """
    path = Path(db_path)
    result: Dict[str, Any] = {
        "path": str(path),
        "opens_cleanly": False,
        "reason": None,
        "marker": None,
        "triggers": [],
        "expected_triggers": sorted(_FTS_TRIGGERS),
        "missing_triggers": sorted(_FTS_TRIGGERS),
        "marker_readable": False,        # 旧库（第 11 章之前建的）没有 state_meta 表 ⇒ 读不到标记
        "reason_marker": None,
        "canonical_rows": None,
        "healthy": False,
    }
    reason = _db_opens_cleanly(path)
    result["reason"] = reason
    if reason is not None:
        return result
    result["opens_cleanly"] = True
    conn = _read_only_connection(path)
    try:
        # ★ 逐项隔离：**一句读不了不能把整份体检带崩**。
        #   真实事故（第 11 章真跑时在真实 home 上撞出来的）：旧库没有 `state_meta` 表
        #   （第 11 章之前建的），原来一条 `OperationalError: no such table: state_meta`
        #   直接 early-return ⇒ 明明在场的三个触发器被报成"缺 3 个"、行数报 None。
        #   **误诊比不诊更坏**（会把人骗去跑修复、或以为索引坏了）。
        try:
            row = conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (FTS_STALE_KEY,)).fetchone()
            result["marker"] = None if row is None else row[0]
            result["marker_readable"] = True
        except sqlite3.Error as exc:
            result["reason_marker"] = "%s: %s" % (type(exc).__name__, exc)
        try:
            present = sorted(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'messages_fts%'"))
            result["triggers"] = present
            result["missing_triggers"] = [t for t in sorted(_FTS_TRIGGERS) if t not in present]
        except sqlite3.Error as exc:
            result["reason_triggers"] = "%s: %s" % (type(exc).__name__, exc)
        try:
            result["canonical_rows"] = int(
                conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        except sqlite3.Error as exc:
            result["reason_rows"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        conn.close()
    # 健康 = 读得到标记 · 标记不在场 · 触发器齐 · 行数读得出来（旧库缺 state_meta ⇒ 不健康：待 repair 升级）
    result["healthy"] = (result["marker_readable"] and result["marker"] is None
                         and not result["missing_triggers"] and result["canonical_rows"] is not None)
    return result


# ── ③ raw-copy 备份（默认先做）────────────────────────────────────────────
def _backup_db_file(db_path) -> Tuple[Optional[Path], Optional[str]]:
    """raw-copy 一个（可能打不开的）库 + 三件 sidecar 到时间戳备份。

    真实版 docstring 逐字（`hermes_state_repair.py:480-481`）：

        Raw bytes on purpose: the DB won't open cleanly, so preserve them exactly for forensics.

    ★ 为什么是"原样拷字节"而不是 SQLite 的在线备份 API：库**可能已经打不开**，
      那唯一还能保存它的方式就是把字节原封不动拷出来（给取证 / 人工修复）。
      它**故意不保证 SQLite 一致性** —— 它要的不是"一个能用的库"，是"一份没被动过的现场"。
    ★ 产物名形如 `state.db.malformed-backup-<stamp>`（真实版 `:505`），同秒再避让一个序号。
      返回 ``(backup_path, None)`` 或 ``(None, reason)``。
    """
    path = Path(db_path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name("%s.malformed-backup-%s" % (path.name, stamp))
    seq = 0
    while backup_path.exists():
        seq += 1
        backup_path = path.with_name("%s.malformed-backup-%s_%d" % (path.name, stamp, seq))
    try:
        shutil.copy2(path, backup_path)
        for suffix in _DB_SIDECAR_SUFFIXES:
            side = path.with_name(path.name + suffix)
            if side.exists():
                shutil.copy2(side, backup_path.with_name(backup_path.name + suffix))
    except OSError as exc:
        return None, "raw-copy 备份失败：%s" % exc
    return backup_path, None


# ── ④ 三段验证查询（文档 §3）──────────────────────────────────────────────
def _count_canonical_rows(db_path) -> int:
    """权威行数（`messages` 表的行数）—— 修复前后各取一次，用来证明"没少行"。"""
    conn = _read_only_connection(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    finally:
        conn.close()


def _verify_repair(db_path, before_rows: int) -> Dict[str, Any]:
    """三段验证查询（文档 §3）。跑在**新开的只读连接**上 —— 看的是磁盘事实，不是内存。

    判据逐字（文档 §3）：
      ① 标记查询 = **零行**（`fts_stale` 行没了）；
      ② 预期触发器**在场**（三个都在）；
      ③ 权威行数**不得下降**（`rows_after >= rows_before`）。
    """
    path = Path(db_path)
    conn = _read_only_connection(path)
    try:
        marker_rows = int(conn.execute(
            "SELECT COUNT(*) FROM state_meta WHERE key = ?", (FTS_STALE_KEY,)).fetchone()[0])
        present = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'messages_fts%'"))
        rows_after = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    finally:
        conn.close()
    expected = sorted(_FTS_TRIGGERS)
    return {
        "marker_rows": marker_rows,                       # ① 应为 0
        "marker_clear": marker_rows == 0,
        "triggers_present": present == expected,          # ② 三个都在
        "triggers": present,
        "expected_triggers": expected,
        "rows_before": before_rows,
        "rows_after": rows_after,
        "rows_not_decreased": rows_after >= before_rows,  # ③ 不得下降
    }


# ── ⑤ 修复入口 ────────────────────────────────────────────────────────────
def repair_state_db_schema(db_path, *, backup: bool = True) -> Dict[str, Any]:
    """修一个坏掉的 state.db（真实版 `hermes_state_repair.py:849` 的 mini 版）。

    真实版返回 ``{repaired, strategy, backup_path, error}``；mini 多一个 ``verification``
    （三段验证查询的结果）。流程**顺序不能换**：

      0. **健康探针**：打不开（结构性损坏）⇒ 直接失败，不装作能修
         （真实 `doctor` 逐字：`an FTS rebuild cannot repair it`）。
      1. **raw-copy 备份**（默认；`--no-backup` 可关）—— 真实版把"备份被拒"当 HARD STOP：
         全策略失败时，这份取证镜像就是唯一的恢复源（`hermes_state_repair.py:993`）。
      2. **修复**：重建 FTS 索引 + 装回触发器 + 清 `fts_stale` 标记
         （= `SessionDB.rebuild_fts()`；这是唯一允许跑 `FTS5('rebuild')` 的地方）。
      3. **三段验证查询**。
    """
    # 延迟 import：避免与 `hermes_state` 形成模块级环（repair 只被 CLI 引用，不在热路径上）。
    from hermes_state import SessionDB

    result: Dict[str, Any] = {
        "repaired": False,
        "strategy": None,
        "backup_path": None,
        "error": None,
        "verification": None,
        "db_path": str(db_path),
    }
    path = Path(db_path)

    # 0. 健康探针：打不开就别猜能修
    reason = _db_opens_cleanly(path)
    if reason is not None:
        result["error"] = "库打不开（结构性损坏），FTS 重建修不了它：%s" % reason
        return result

    # 1. raw-copy 备份（默认；这是"取证镜像"，不是修好的库）
    if backup:
        backup_path, err = _backup_db_file(path)
        if err is not None:
            # 真实版把"备份被拒"当 HARD STOP：见 docstring 的 1。
            result["error"] = "拒绝继续：raw-copy 备份失败（HARD STOP）—— %s" % err
            return result
        result["backup_path"] = str(backup_path)

    # 2. 修复：重建 FTS（mini 的唯一策略）
    before_rows = _count_canonical_rows(path)
    result["strategy"] = "rebuild_fts"
    db = SessionDB(path)
    try:
        # 开库时读到标记 ⇒ 保持摘除；rebuild_fts 重建索引 + 装回触发器 + 清标记，整段一个写事务。
        db.rebuild_fts()
    except Exception as exc:                              # noqa: BLE001
        result["error"] = "重建 FTS 失败：%s: %s" % (type(exc).__name__, exc)
        return result
    finally:
        db.close()

    # 3. 三段验证查询
    verification = _verify_repair(path, before_rows)
    result["verification"] = verification
    result["repaired"] = bool(
        verification["marker_clear"]
        and verification["triggers_present"]
        and verification["rows_not_decreased"]
    )
    if not result["repaired"]:
        result["error"] = "修复后三段验证未全部通过"
    return result
