"""会话存储的共享常量与表结构（对应 Hermes 的 `hermes_state_common.py`）。

★ 抄的是这个**决策**：`SCHEMA_SQL` 是**唯一事实来源**（single source of truth）。
  建库、加列、列名差分全都从这一份 SQL 推出来，所以"加一列"这件事
  **不需要写迁移** —— 改这里就够了。

真实版 `hermes_state_schema.py:738-739` 的 docstring 逐字：

    ADD every SCHEMA_SQL column missing from the live tables (SCHEMA_SQL is the single
    source of truth; column additions need no version-gated migration).
"""
from __future__ import annotations

# ── 版本 ────────────────────────────────────────────────────────────────
SCHEMA_VERSION = 1
"""当前 schema 版本（真实版是 30，`hermes_state_common.py:239`）。

⚠️ 与真实版同一条纪律：**纯加列不走版本号**（`_reconcile_columns()` 就够），
   版本门控只留给"动数据或动索引"的迁移。
"""

# ── 连接与写入礼节（对应 Hermes `hermes_state.py:476-480, 731`）──────────
SQLITE_TIMEOUT_S = 1.0
"""连接级 busy timeout。

★ 为什么不用默认的 30 秒：多进程抢锁时，长等会把"谁先拿到写锁"从毫秒级
  拖成秒级（真实版注释的理由就是这个）；真正解决争用的是下面那套**应用层重试**。
"""

WRITE_RETRY_MIN_S, WRITE_RETRY_MAX_S = 0.020, 0.150
WRITE_RETRY_SLOW_AFTER_S = 2.0
WRITE_RETRY_SLOW_MIN_S, WRITE_RETRY_SLOW_MAX_S = 0.250, 1.000
"""两档抖动（真实版 `hermes_state.py:476-478`）：前 2 秒用快档，之后换慢档。

★ 为什么要**随机**抖动：SQLite 内部的退避是**确定性**的 —— 所有竞争的写者会在
  同一个间隔上一起重试、一起失败、再一起重试（这叫**车队效应**，convoy effect）。
  把间隔随机化，就是把这支"车队"打散。
"""

WRITE_PATIENCE_S = 10.0
"""单次写入的总耐心：超过就放弃并抛错（真实版 `_WRITE_PATIENCE_S`）。

★ 为什么必须有上限：没有它，"一直重试"会把一次卡住的写入变成永久挂起。
  宁可让调用方拿到一个明确失败，也不要静默吊死整个回合。
"""

CHECKPOINT_EVERY_N_WRITES = 50
"""每 50 次成功写入做一次 `PRAGMA wal_checkpoint(PASSIVE)`，防止 WAL 无限长大。

★ 为什么是 PASSIVE：它只是"尽量做"，不会因为别人正在读而阻塞写者 ——
  在写入路径上做任何**可能长时间阻塞**的事，都会把争用放大。
"""


SCHEMA_SQL = """
-- ══════════════════════════════════════════════════════════════════════
-- 系统提示词去重表：会话行只存 sha256 哈希，正文在这里存一份
-- （真实版 `hermes_state_common.py:329`）
-- ══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS system_prompts (
    hash   TEXT PRIMARY KEY,
    prompt TEXT NOT NULL
);

-- ══════════════════════════════════════════════════════════════════════
-- 会话行：元数据 + 用量计数 + 血缘 + 标题
-- （真实版 `:334`；这里保留了机制，砍掉了计费与网关路由那些列）
-- ══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS sessions (
    id                 TEXT PRIMARY KEY,
    source             TEXT NOT NULL,          -- cli / tui …（平台来源标记）
    model              TEXT,
    model_config       TEXT,                   -- JSON 字符串
    system_prompt_hash TEXT REFERENCES system_prompts(hash),
    parent_session_id  TEXT REFERENCES sessions(id),
                                               -- ↑ 血缘：压缩轮转 / 分支时指向上一个会话
    started_at         REAL NOT NULL,
    ended_at           REAL,
    end_reason         TEXT,
    title              TEXT,
    message_count      INTEGER DEFAULT 0,
    tool_call_count    INTEGER DEFAULT 0,
    api_call_count     INTEGER DEFAULT 0,      -- 这个会话调了几次模型（≈ 迭代次数）
    input_tokens       INTEGER DEFAULT 0,
    output_tokens      INTEGER DEFAULT 0
);


-- ══════════════════════════════════════════════════════════════════════
-- 消息行（真实版 `:397`）
--
-- ★ 两条关键列：
--   active    —— 1 = 活行；0 = 已被归档（原地压缩把旧世代标掉）
--   compacted —— 1 = 这一行属于**归档世代**（展示时仍要看它，喂模型时绝不带它）
--   合起来表达"同一个会话存在多个世代"这件事（真实版 `_messages.py:44`）。
-- ══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT NOT NULL REFERENCES sessions(id),
    role              TEXT NOT NULL,
    content           TEXT,
    tool_call_id      TEXT,
    tool_calls        TEXT,                    -- JSON 字符串（工具调用列表）
    tool_name         TEXT,
    timestamp         REAL NOT NULL,           -- Unix epoch 浮点（time.time()）
    token_count       INTEGER,
    finish_reason     TEXT,
    reasoning         TEXT,
    reasoning_content TEXT,                    -- 出网前复制的那份（第 2 章）
    active            INTEGER NOT NULL DEFAULT 1,
    compacted         INTEGER NOT NULL DEFAULT 0
);

-- ══════════════════════════════════════════════════════════════════════
-- 迁移状态：**单行**表（真实版 `schema_version`）
-- ══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""
# ↑ 分界线：**上面是三张实体表的建表语句**，
#   下面是"引用具体列"的 DDL（索引 / FTS 虚拟表 / 触发器 / 版本表）。

POST_RECONCILE_SQL = """
-- ══════════════════════════════════════════════════════════════════════
-- 下面这段必须在 `_reconcile_columns()` **之后**再执行。
--
-- ★ 为什么（真实版 `hermes_state_schema.py:929-930` 逐字）：
--     Indexes referencing reconciler-added columns must be created AFTER
--     _reconcile_columns (in SCHEMA_SQL the executescript would fail on legacy DBs).
--
--   实测过：一个只有 `id/source` 两列的**老库**，跑 SCHEMA_SQL 会在
--   `ON sessions(parent_session_id)` 这一句上炸（no such column）——
--   因为表已经是老的、索引却按新列建。顺序错了 = 老用户直接起不来。
-- ══════════════════════════════════════════════════════════════════════
CREATE INDEX IF NOT EXISTS idx_sessions_source  ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent  ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);

-- 标题唯一：**只约束"非 NULL 之间"** —— 允许大量没起标题的会话。
-- （真实版 `:559` 一带；部分索引是 SQLite 的用法）
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique
    ON sessions(title) WHERE title IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_messages_session        ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_session_id     ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_session_active ON messages(session_id, active);

-- ══════════════════════════════════════════════════════════════════════
-- 全文检索：FTS5 **外部内容**表（`content='messages'` = 不复制正文，
-- 只建索引，正文还在 messages 里）。真实版 `:695`。
--
-- ★ 为什么用外部内容模式：普通 FTS5 会把每段文本**再存一份**进索引表；
--   外部内容模式只存倒排索引 + rowid 指向 messages，库小一大截。
--   代价：必须自己维护同步 —— 就是下面三个触发器。
-- ══════════════════════════════════════════════════════════════════════
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    tool_name,
    tool_calls,
    content='messages',
    content_rowid='id'
);

-- 三个触发器：INSERT / DELETE / UPDATE 各一个（真实版 `:703/:718/:737`）
CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;

-- 外部内容表的删除是一条**特殊的 delete 命令**（不是 DELETE 语句）：
-- 要把"当时索引进去的那几个值"再喂一遍，FTS5 才知道倒排表里该摘掉什么。
CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
    INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;

"""
