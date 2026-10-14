"""建库、声明式加列、版本门控（对应 Hermes 的 `hermes_state_schema.py`）。

三条分工，抄的是这个**切法**：

| 情况 | 谁处理 | 要写迁移吗 |
|---|---|---|
| 库还不存在 | `_ensure_schema()` 跑一遍 `SCHEMA_SQL` | 不用 |
| 加一列 | `_reconcile_columns()` 差分 + `ALTER TABLE ADD COLUMN` | **不用**（改 SCHEMA_SQL 即可） |
| 动数据 / 动索引 | `_run_migrations()` 版本门控链 | 要 |

真实版 docstring 逐字（`hermes_state_schema.py:738-739`）：
`SCHEMA_SQL is the single source of truth; column additions need no version-gated migration.`
"""
from __future__ import annotations

import re
import sqlite3
from typing import Dict, List

from hermes_state_common import POST_RECONCILE_SQL, SCHEMA_SQL, SCHEMA_VERSION


class SchemaTooNewError(RuntimeError):
    """库的版本比代码新 —— 拒绝打开。

    ★ **这是 mini 加的**：真实版没有对"新库"的显式拒绝（它只在版本更旧时推进）。
      加它的理由很实际：旧代码打开新库会**静默**按旧理解读写，
      这类破坏不会有任何报错，只会在以后变成"数据莫名丢了"。
      一句话拒绝，比事后排查便宜得多。
    """


# 表级约束（不是列）：差分时跳过
_CONSTRAINT_PREFIXES = ("primary key", "foreign key", "unique", "check", "constraint")

_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)\s*;",
    re.S | re.I,
)


def parse_schema_columns(sql: str) -> Dict[str, Dict[str, str]]:
    """从 `SCHEMA_SQL` 里解出 ``{表名: {列名: 列定义片段}}``。

    片段是**列名之后的一整段**（类型 + 约束），例如 ``INTEGER NOT NULL DEFAULT 1`` ——
    不是只取类型名。这个区别是踩出来的：真实版 `#51646` 记着一个线上事故 ——
    reconcile 补列时**丢掉了 `NOT NULL DEFAULT 1`**，于是那些行 `active` 写成 NULL，
    而所有读路径都是 `WHERE active = 1` → **整段历史被藏起来**（不报错）。
    所以补列必须带上完整定义。

    只看 `CREATE TABLE IF NOT EXISTS`（虚拟表、索引、触发器天然不匹配），
    跳过表级约束与注释行。真实版同名私有方法（`_parse_schema_columns`）。
    """
    tables: Dict[str, Dict[str, str]] = {}
    for table, body in _TABLE_RE.findall(sql):
        cols: Dict[str, str] = {}
        for raw_line in body.split("\n"):
            line = raw_line.split("--")[0].strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            if line.lower().startswith(_CONSTRAINT_PREFIXES):
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            cols[parts[0]] = " ".join(parts[1].split())
        tables[table.lower()] = cols
    return tables


def _add_column_safe_definition(definition: str) -> str:
    """把 SCHEMA_SQL 里的列定义改成 SQLite `ADD COLUMN` **允许**的形状。

    SQLite 对 `ALTER TABLE ADD COLUMN` 有硬限制：

    | 不许带 | 为什么 |
    |---|---|
    | `PRIMARY KEY` / `UNIQUE` / `AUTOINCREMENT` | 加列不能顺便当主键/唯一键 |
    | 光有 `NOT NULL` 没有 `DEFAULT` | 已有行填不出值，SQLite 直接拒绝 |

    其余（类型、`REFERENCES`、`DEFAULT`）都保留 —— **保留 `DEFAULT` 正是重点**
    （真实版 `#51646`：丢了默认值的那次补列，把历史整段藏了起来）。
    """
    ddl = definition
    for clause in ("PRIMARY KEY", "UNIQUE", "AUTOINCREMENT"):
        idx = ddl.upper().find(clause)
        if idx != -1:
            ddl = ddl[:idx]
    ddl = ddl.strip()
    if "NOT NULL" in ddl.upper() and "DEFAULT" not in ddl.upper():
        upper = ddl.upper().replace("NOT NULL", "")
        ddl = upper.strip()
    return ddl


class SessionSchemaMixin:
    """给 `SessionDB` 用的建库 / 迁移部分。"""

    # ── 入口：连接建好后调一次 ──────────────────────────────────────────
    def _check_schema_version(self, conn: sqlite3.Connection) -> None:
        """只读检查已有版本；老库没有版本表时仍允许后续协调 schema。"""
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
        current = self._read_schema_version(conn.cursor()) if exists else None
        if current is not None and current > SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"库的 schema 版本是 {current}，而代码只认到 {SCHEMA_VERSION} —— "
                "这通常意味着你用**旧代码**打开了**新库**。拒绝继续。")

    def _ensure_schema(self) -> None:
        """建库 / 补列 / 版本，**顺序不能换**（真实版 `hermes_state_schema.py:919-941`）：

        ```
        ⓪ 只读版本检查                   已有库先于 WAL 切换；这里再次先于 DDL 检查
        ① executescript(SCHEMA_SQL)        建表（全是 IF NOT EXISTS → 幂等）
        ② _reconcile_columns()             补列（加列不用写迁移）
        ③ executescript(POST_RECONCILE_SQL) 建索引 / FTS / 触发器
        ④ 版本门控                         数据与索引迁移
        ```

        ★ ①②③ 的先后是**踩出来的**：索引里有 `ON sessions(parent_session_id)` 这种
          "引用某个列"的语句。老库的 sessions 表还没这列，先建索引就会炸
          （`no such column: parent_session_id`）—— 老用户直接起不来。
          真实版把这类 DDL 单独放一份、注释里写明原因（`:929-930`）。
        """
        self._check_schema_version(self._conn)          # 必须先于任何 DDL
        cur = self._conn.cursor()
        # executescript 会先隐式提交，然后按顺序执行整份 DDL（全部 IF NOT EXISTS → 幂等）
        self._conn.executescript(SCHEMA_SQL)
        added = self._reconcile_columns(cur)
        # ③ 引用列的 DDL 必须等列补齐之后（见 docstring）
        self._conn.executescript(POST_RECONCILE_SQL)
        current = self._read_schema_version(cur)
        if current is None:
            cur.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        elif current > SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"库的 schema 版本是 {current}，而代码只认到 {SCHEMA_VERSION} —— "
                f"这通常意味着你用**旧代码**打开了**新库**。拒绝继续。"
            )
        elif current < SCHEMA_VERSION:
            self._run_migrations(cur, current)
            cur.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        self._conn.commit()
        self._schema_boot_report = {"added_columns": added, "version": max(current or 0, SCHEMA_VERSION)}

    def schema_boot_report(self) -> Dict[str, object]:
        """本次打开库时"做了什么"（给 doctor / 测试观察用，不参与业务）。"""
        return dict(getattr(self, "_schema_boot_report", {}) or {})

    # ── 声明式加列 ─────────────────────────────────────────────────────
    def _reconcile_columns(self, cursor: sqlite3.Cursor) -> List[str]:
        """把活库缺的列补上，返回补了哪些（``表.列``）。

        ★ 抄的是这条：**加列这件事不需要写迁移**。`SCHEMA_SQL` 说有哪些列，
          这里就保证活库有那些列 —— 一句 `ALTER TABLE ADD COLUMN` 就够，
          包在 `try/except` 里让"列已存在"变成无害（幂等）。
        """
        added: List[str] = []
        for table, declared in parse_schema_columns(SCHEMA_SQL).items():
            try:
                rows = cursor.execute(f'PRAGMA table_info("{table}")').fetchall()
            except sqlite3.OperationalError:
                continue  # 表不存在（正常不会发生：executescript 刚跑过）
            live = {row[1] for row in rows}
            for col, definition in declared.items():
                if col in live:
                    continue
                ddl = _add_column_safe_definition(definition)
                if not ddl:
                    continue
                try:
                    cursor.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {ddl}')
                    added.append(f"{table}.{col}")
                except sqlite3.OperationalError:
                    pass  # 竞态下别人刚加过：同样幂等
        return added

    # ── 版本门控 ───────────────────────────────────────────────────────
    def _read_schema_version(self, cursor: sqlite3.Cursor):
        row = cursor.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return None if row is None else int(row[0])

    def _run_migrations(self, cursor: sqlite3.Cursor, from_version: int) -> None:
        """版本门控链：**只放"动数据 / 动索引"的迁移**。

        现在没有（v1 就是初始版本）。留这个形状是为了让"下一次真的要动数据"
        有一个明确该写在哪里的地方 —— 而不是临时在某个函数里塞一段 UPDATE。
        """
        return None
