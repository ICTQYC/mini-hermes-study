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

★ Python sqlite3 默认 timeout 为 5 秒；本项目缩短等待：多进程抢锁时，长等会把"谁先拿到写锁"从毫秒级
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


# ── 第 11 章：FTS 同步触发器名单 + 降级标记 ──────────────────────────────
# （真实版 `hermes_state_common.py:321-322`（名单）/ `:878-880`（标记））
_FTS_TRIGGERS = (
    "messages_fts_insert",
    "messages_fts_delete",
    "messages_fts_update",
)
"""三个同步触发器的名字（真实版是 **6 个**：base 3 + trigram 3，`:321-322`；mini 只有 base 一套）。

★ 为什么名单必须是常量：fail-open 要"把这一整套**摘掉**"，`rebuild_fts()` 要
  "把这一整套**装回来**" —— 两处必须看**同一份**名单。名单要是散在那两句 SQL 里，
  迟早会漏掉一个；漏一个的后果是"以为摘干净了，其实还有一条路径在往坏索引里写"，
  而那正是 FTS5 external-content 的经典损坏点（真实版 `hermes_state_fts.py:28-31`）。
"""

FTS_STALE_KEY = "fts_stale"
"""`state_meta` 里的降级标记键（真实版 `hermes_state_common.py:878-880`，逐字）：

    # Set when a base/trigram FTS index was detached after runtime corruption; startup must rebuild the complete
    # index before reinstalling sync triggers (rows written while they were absent leave an unknown gap).
    FTS_STALE_KEY = "fts_stale"

★ 它的存在理由就是**跨重启可见**：触发器一摘，索引就有了一段"范围未知的空窗"
  （触发器缺席期间写进来的行没进索引）。在内存里记一个标志，进程一重启就忘了，
  重开库会以为索引是好的 → 把触发器装回去 → 空窗永远补不上、越补越错。
  落在 `state_meta` 里，"降级中"这件事就成了**持久事实**，而不是某个进程的私事。
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
    session_key        TEXT,                   -- 网关的路由键（build_session_key 产出；cli 会话为 NULL）
                                               --   ↑ 真实版 `hermes_state_common.py:338` 同列，
                                               --     第 7 章加：网关重启后要能按"同一个聊天"找回会话
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
    api_call_count     INTEGER DEFAULT 0,      -- 模型逻辑请求次数（含回退和辅助摘要，不等于迭代次数）
    input_tokens       INTEGER DEFAULT 0,
    output_tokens      INTEGER DEFAULT 0,
    -- ══ 第 8 章：会话生命周期状态（对应真实版 SessionEntry 的那些标志位）══
    --   真实版把它们放在 `sessions.json` 镜像 + SQLite 两处（`gateway/session.py:756/787`）；
    --   mini 只有库这一个读者 → **只落库**（差异与代价写在第 8 章差距清单 B4）。
    updated_at         REAL,                   -- 最后活动时间：资源巡检靠它（真实版 SessionEntry.updated_at）
    suspended          INTEGER DEFAULT 0,      -- 硬轮转：异常重复等原因挂起；/stop 只中断当前回合
    resume_pending     INTEGER DEFAULT 0,      -- 软恢复：崩溃/drain 超时 → 下次访问**接着**旧会话
    resume_reason      TEXT,                   -- restart_timeout / shutdown_timeout / restart_interrupted
    last_resume_marked_at REAL,
    is_fresh_reset     INTEGER DEFAULT 0       -- /new 之后的第一条消息（要重注入话题/技能）
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

-- ══════════════════════════════════════════════════════════════════════
-- 第 11 章：键值元数据表（真实版 `hermes_state_common.py:448-451`，逐字同 DDL）
--
--   CREATE TABLE IF NOT EXISTS state_meta (
--       key TEXT PRIMARY KEY,
--       value TEXT
--   );
--
-- ★ **纯加表，不写迁移**：SCHEMA_SQL 是唯一事实来源，加一张 `IF NOT EXISTS` 的表
--   跟加一列一样，改这里就够了（老库打开时自动补建）。
-- ★ 它存在的唯一理由：装**跨重启可见**的持久标记 —— `fts_stale`（见下面常量）。
--   在内存里记一个"我降级了"的标志，进程一退出就没了；重开库会以为索引是好的。
-- ══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS state_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""
# ↑ 分界线：**上面是四张实体表的建表语句**（第 11 章加了 `state_meta`），
#   下面是"引用具体列"的 DDL（索引 / FTS 虚拟表 / 触发器 / 版本表）。

_POST_RECONCILE_SQL = """
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
-- ★ 第 7 章：按"路由键 + 时间倒序"找回一个聊天的当前会话
--   （真实版 `hermes_state_common.py:659` 是同形状的索引）
CREATE INDEX IF NOT EXISTS idx_sessions_session_key ON sessions(session_key, started_at DESC);
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

"""

# ── 第 11 章：触发器从上面那份 DDL 里**拆出来**，成为独立可执行的清单 ──────
# ★ 抄的是真实版的切法（`hermes_state_fts.py:28-31` 逐字）：
#
#     # Split DDL: the table/view is safe to ensure any time; triggers are created ONLY
#     # while the index is complete-or-marker-gated. A stale index must keep its
#     # triggers DROPPED — an external-content 'delete' for a rowid the index never
#     # held is the canonical FTS5 corruption hazard.
#
#   理由在 mini 里同样成立：触发器要能被 fail-open **整套摘掉**（`DROP TRIGGER`）、
#   被 `rebuild_fts()` **整套装回来**，所以它必须是"一份可以逐条执行的清单"，
#   而不是埋在一大段 DDL 尾巴里的一串文本。
#   `PRE_RECONCILE` 的语义：建表/建索引/建虚拟表随时可以 ensure（幂等），
#   只有触发器要等"索引是完整的"才能装。
FTS_TRIGGER_STATEMENTS = (
    # INSERT：新行进 messages → 同一份内容进 FTS（真实版 `:703`）
    """CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END""",
    # DELETE：外部内容表的删除是一条**特殊的 delete 命令**（不是 DELETE 语句）——
    # 要把"当时索引进去的那几个值"再喂一遍，FTS5 才知道倒排表里该摘掉什么（真实版 `:718`）
    """CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
END""",
    # UPDATE = 先 delete 旧值、再 insert 新值（真实版 `:737`）
    """CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
    INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END""",
)

# 拼回一整份（`_ensure_schema` 用 `executescript` 一次跑完，行为不变）。
# ★ 用 ";\\n" 连接、并补一个收尾分号：`executescript` 是按分号切语句的，
#   而触发器的**语句体**里本来就有分号（`BEGIN … END` 之内），所以清单里的每条
#   都是"不带收尾分号的完整触发器定义"，只在拼成整份时才补分号。
FTS_TRIGGER_SQL = ";\n".join(FTS_TRIGGER_STATEMENTS) + ";\n"

POST_RECONCILE_SQL = _POST_RECONCILE_SQL + FTS_TRIGGER_SQL
"""建索引 / FTS 虚拟表 / 三个触发器（**顺序不能换**：触发器在最后，见上面的注释）。"""
