# 源码讲解 · 第 6 章：会话存储（Session Storage）

> 维护说明：章节交付时的统计与实测输出保留原口径；当前发布前修复、套件数字与验证收据见《发布前修复记录.md》。mini 代码引用按维护后的实现核对。

> 配套《中文精译-06-SessionStorage.md》。那篇讲**原理**（原文说了什么），
> 这篇讲**差异**：mini 版和真实版差在哪、每处差价值不值得改，外加本章实现时抓出的真问题。
>
> **阅读顺序**：最前面是「**导言 · 机制总览：Hermes 具体怎么做的**」—— 先把 Hermes 一侧讲清
> （只有机制与源码，不含 mini），再看 §〇 差异一览表，最后逐项看差距与真问题。
>
> 体例同《源码讲解-05》：导言（机制总览）→ 差异一览表 → 逐项四步
> （**Hermes 怎么做 → 读源码 → mini 怎么做 → 为什么没做成这样**）→ 真问题 → 差距清单 → 附录。
>
> 📌 **两边的锚不是同一个东西，别混**：
> - **mini 侧**：本文描述**第 6 章末**状态，对应标签 `ch06-session-storage`。
>   核对：`git show ch06-session-storage:mini-hermes/hermes_state.py`
> - **真实 Hermes 侧**：行号是**某一刻的快照**，锚在上游 commit
>   **`f5d1926110`**（`pyproject.toml:5` → `version = "0.21.3"`，取证于 2026-09-23）。
>   上游每合并一个 PR 行号就会漂 —— 引用时请连同这个 commit 一起引。

[TOC]

---

## 两份代码的路径与规模

```text
mini 版（本仓库，第 6 章末）                      真实版
  $HOME/Desktop/study/hermes学习/mini-hermes/    ~/.hermes/hermes-agent/
    hermes_state.py          223 行  ←  hermes_state.py（门面）           1,600+ 行
    hermes_state_common.py   185 行  ←  hermes_state_common.py             880+ 行
    hermes_state_schema.py   178 行  ←  hermes_state_schema.py           1,200+ 行
    hermes_state_sessions.py 223 行  ←  hermes_state_sessions.py         1,649 行
    hermes_state_messages.py 223 行  ←  hermes_state_messages.py         2,000+ 行
    hermes_state_search.py   248 行  ←  hermes_state_search.py           1,100+ 行
    tests/test_session_storage.py 511 行（14 组 / 107 条断言）
    ── 换掉的是：旧的 hermes_state.py（46 行 JSON 版）
```

**先记住这条**：本章的规模差**不在"机制"上，而在"一边是学习项目、一边是多人多平台的共享存储"**。
真实版的 21 个兄弟模块里，一半以上是在处理 mini 根本没有的东西：多 profile、跨进程压缩锁、
桌面端展示排序、计费归属、网关异常路径的消息归属、损坏恢复、导入导出限额。
**能抄的机制都抄了**（13 条，见差距清单），剩下的每一条都能说清"为什么 mini 用不上"。

---

## 导言 · 机制总览：Hermes 具体怎么做的（先读这一节）

> **这一节只有 Hermes 一侧**：不讲 mini 怎么实现、也不讲差异（那些从 §〇 开始）。
> 读完它应该能独立回答：**这一章里 Hermes 到底做了什么？**

### 0.1 谁在管这件事：一个门面 + 一堆兄弟

| 角色 | 是谁 | 干什么 |
|---|---|---|
| **门面** | `class SessionDB(...)` `hermes_state.py:441` | 连接、事务、生命周期 —— 对外的唯一入口 |
| **兄弟** | `hermes_state_*.py`，**21 个** | 一人管一个主题（schema / messages / search / compression / portability / gateway …） |
| **拼装方式** | `class SessionDB(SessionSessionsMixin, SessionSearchMixin, SessionSchemaMixin, SessionMessagesMixin, SessionTitlesMixin, …)` | 门面把 13 个 mixin 一次继承起来 |

真实版的门面**不掺业务**：业务在兄弟里，一个主题一个文件。这是它躲开"上帝文件"的办法
（对应顶层 `AGENTS.md` 那条：文件过 ~2,000 行 / 函数过 ~300 行就要拆）。

### 0.2 库长什么样：一条会话链 + 世代化的消息

```
~/.hermes/state.db（SQLite，WAL 模式）
├── sessions            —— 会话行：元数据 + 用量计数 + 血缘（parent_session_id）+ 标题
├── messages            —— 消息行：role/content/tool_calls/… + **active / compacted**（世代）
├── system_prompts      —— 系统提示词去重表（hash → 正文）
├── messages_fts        —— FTS5 虚拟表（外部内容模式）
├── messages_fts_trigram / messages_fts_cjk   —— CJK 与子串检索
├── session_model_usage —— 按模型/任务的用量归属
├── compression_locks   —— 跨进程压缩锁
└── schema_version      —— 单行表，记录迁移状态
```

两条最关键的设计（后面每一项都围着它们转）：

1. **会话是一条链**：压缩触发**会话轮转**时，新会话把老会话设成父亲（`parent_session_id`），
   于是"这个会话的历史"是**一条链上的历史**（`include_ancestors` 就是为它准备的）。
2. **消息是世代化的**：`active = 1` 是"现在活着的那一代"，`active = 0, compacted = 1` 是
   被原地压缩**归档**的旧世代。**同一条消息同时出现在两个世代里是正常现象** ——
   真实版明文警告：别把这些归档行当重复数据删掉。

### 0.3 什么时候写、怎么写：写入礼节

`_execute_write()`（`hermes_state.py:920-960`）是全局唯一的写入通道，docstring 逐字：

> `Run *fn(conn)* inside BEGIN IMMEDIATE with jittered lock retry; commit is handled here
> (callers must not commit). Returns *fn*'s result. BEGIN IMMEDIATE takes the WAL write lock up
> front so contention surfaces immediately; on locked/busy the Python lock is released, a jitter
> slept, and the WHOLE callback retried — *fn* must stay idempotent under retry.`

配三个常量（`:476-480`）：

```python
_WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S = 0.020, 0.150   # 快档（前 _SLOW_AFTER_S 秒）
_WRITE_RETRY_SLOW_AFTER_S = 2.0
_WRITE_RETRY_SLOW_MIN_S, _WRITE_RETRY_SLOW_MAX_S = 0.250, 1.000   # 慢档
_CHECKPOINT_EVERY_N_WRITES = 50
```

连接参数也**都不是默认值**（`:731`）：`timeout=1.0`（不是 30）、`isolation_level=None`
（自己显式事务）、`check_same_thread=False`（工具是并发线程跑的）。
而且设完 WAL 会**回头读一次磁盘上的真实模式**（`_on_disk_journal_mode`，`:740`）——
因为 WAL 在某些文件系统上会静默退回 delete 模式，而"以为自己开了 WAL"会让并发行为完全不是你想的那样。

### 0.4 表结构怎么演进：改一处就够

`SCHEMA_SQL`（`hermes_state_common.py:324`）是**唯一事实来源**。三类变更各有归属：

| 变更 | 谁处理 | 要写迁移吗 |
|---|---|---|
| 建库 | `executescript(SCHEMA_SQL)` | 不用 |
| **加一列** | `_reconcile_columns()`（`PRAGMA table_info` 差分 + `ALTER TABLE ADD COLUMN`） | **不用**（改 SCHEMA_SQL 即可） |
| 动数据 / 动索引 | `schema_version` 版本门控链 | 要 |

真实版 `_reconcile_columns` 的 docstring 逐字：

> `ADD every SCHEMA_SQL column missing from the live tables (SCHEMA_SQL is the single source of
> truth; column additions need no version-gated migration).`

**编排顺序是有讲究的**（`hermes_state_schema.py:919-941`）：建表 → 补列 → **然后才**建那些
"引用补出来的列"的索引。原话：

> `Indexes referencing reconciler-added columns must be created AFTER _reconcile_columns
> (in SCHEMA_SQL the executescript would fail on legacy DBs).`

顺序错的后果是**老用户直接起不来**（建索引时报 `no such column`）。这一条 mini 也踩了一次
（见 §三）。

### 0.5 消息怎么进出：一个签名 + 两种读法

**写**：`append_message()`（`hermes_state_messages.py:279-291`）有 22 个关键字参数，而它同时定义了
三个契约 —— 形参名 = 表列名 = 消息 dict 的键名。原话：

> `msg = dict(locals())  # every keyword above is a message-dict field of the same name`

**读**：两个方法，用途严格分开：

| 方法 | 给谁用 | 约束 |
|---|---|---|
| `get_messages()` | 排查 / 导出 / 看世代 | 原始行，带全部元数据 |
| `get_messages_as_conversation()` | **喂给模型 / 重放** | docstring 逐字：`the model-fed restore must not regrow what compaction summarized away` |

后者默认只看 `active = 1`，并且能用 `include_ancestors=True` **顺着父亲链把祖先的消息一起取回来**
（压缩轮转会让历史散在一条链上）。它还有一个 `repair_alternation`：durable 的 `user;user` 相邻
对会在每次重放时被修一次，而**存储本身不动**（`the stored transcript is never mutated`）。

**原地压缩落库**只有一行 SQL（`:44`）：

```sql
UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1
```

老世代整体退休 → 保留的上下文作为**新行**插入。这就是"多个世代并存"的来源。

### 0.6 怎么找回来：索引 + 血缘 + 标题

- **检索**：FTS5 **外部内容**表（`content='messages'`，不复制正文，只建倒排索引）+ 三个触发器同步
  （INSERT/DELETE/UPDATE 各一个）。CJK 与子串另有 trigram / cjk 两张表；
  还要一条 `_search_messages_like_fallback` 兜底（`_LIKE_ANY_COLUMN_SQL` 扫三列 + `ESCAPE`）。
- **血缘**：递归查询（官方文档的两段 CTE：找祖先 / 找后代）。
- **标题**：非 NULL 之间唯一的**部分索引**（`WHERE title IS NOT NULL`）+ 血缘里自动编号
  （`get_next_title_in_lineage("X")` → `"X #2"`）。
- **隔离**：库路径来自 `get_hermes_home() / "state.db"`（默认 profile）或
  `profiles/<名>/state.db`（具名 profile）—— **一个 profile 一个库**，
  行里还有 `profile_name` 当第二道闸门（跨命名空间回填有专门的闸门，`#88381`）。

### 0.7 串起来：一条消息落库的完整路径

```text
agent 回合里产出消息
   │
   ├─ append_message()：一个签名 → 一行 INSERT（形参名=列名）
   │      └─ _execute_write()：BEGIN IMMEDIATE
   │            ├─ 撞锁 → 抖动重试（整块幂等重跑，最多 WRITE_PATIENCE_S）
   │            └─ 成功 → 每 50 次写做一次 PASSIVE checkpoint（PASSIVE 是因为它不阻塞正常读写）
   │      └─ 触发器同步 FTS 索引（外部内容表）
   │      └─ 会话计数 +1（message_count / tool_call_count）
   ▼
压缩发生时（原地压缩）
   ├─ 老世代：UPDATE active=0, compacted=1
   └─ 新世代：保留的上下文作为新行插入
          → 于是"同一条消息出现在两个世代里"是可预期的
   ▼
读回来（重放）
   ├─ get_messages_as_conversation(active=1)   ← 不许复活被压缩掉的世代
   └─ include_ancestors=True → 顺着 parent_session_id 把祖先也带上
```

**读完这一节的验收**：不看后面的差异表，能说清"库里有几张表、各自管什么""为什么加列不用写迁移"
"构建索引为什么要排在补列之后""active/compacted 是什么""为什么抓取历史要顺着血缘链"。

---

## 〇、差异一览表（先看这张）

列序：**真实 Hermes | mini 版 | 性质 | 值得改**。
「mini 版」这一列**一律指当前代码**（要看某章交付时的样子，用标签 `git show`）。

| # | 主题 | 真实 Hermes | mini 版 | 性质 | 值得改 |
|---|---|---|---|---|---|
| 1 | 结构 | 门面 + **21** 个兄弟模块，门面继承 13 个 mixin | 门面 + **5** 个兄弟，继承 4 个 mixin | 规模（切法一致） | ❌ 数量差没意义 |
| 2 | `SCHEMA_SQL` | 唯一事实来源，含全部列 | 同 | **已对齐** | — |
| 3 | 加列 | 声明式（差分 + ADD COLUMN） | 同 | **已对齐** | — |
| 4 | 建库顺序 | 建表 → 补列 → 建索引（`:929-930`） | 同（踩过同一条） | **已对齐** | — |
| 5 | 版本门控 | 只给数据/索引迁移 | 同 + **拒绝打开更新的库**（mini 加的） | **已对齐** + mini 增强 | ✅ 已在 mini 里 |
| 6 | 连接参数 | `timeout=1.0` / `isolation_level=None` / `check_same_thread=False` | 同 | **已对齐** | — |
| 7 | WAL | 设完**读回核对**磁盘模式 | 同 | **已对齐** | — |
| 8 | 写入礼节 | `BEGIN IMMEDIATE` + 两档抖动 + 每 50 次 checkpoint + 幂等回调 | 同 | **已对齐** | — |
| 9 | 会话行 | upsert 只补空缺（COALESCE 保留先写者） | 同 | **已对齐** | — |
| 10 | 提示词 | `system_prompts` 哈希去重 + 会话行存 hash | 同（不再留旧文本列） | **已对齐**（少两列迁移） | — |
| 11 | `append_message` | 22 个 kwargs，三契约同名 | 13 个 kwargs，三契约同名 | 规模（机制一致） | ❌ 参数数量是需求差 |
| 12 | 读路径 | 两个方法；`repair_alternation` / `include_row_ids` / `api_content` 字节保真 | 两个方法；**不做**交替修复、无字节保真字段 | 机制差（消费者不存在） | ❌ 见 #8 差距 |
| 13 | 世代 | `active` / `compacted` + 展示去重（`_dedupe_display_generations`） | `active` / `compacted`，展示去重没做 | **已对齐**（去重列进差距清单） | ⬜ 等桌面端消费者 |
| 14 | 血缘 | 递归 CTE + `include_ancestors` | 同（`session_lineage` + `include_ancestors`） | **已对齐** | — |
| 15 | 检索 | FTS5 词索引 + **trigram + cjk 两张表** + LIKE 兜底 + 路由链 | FTS5 词索引 + **LIKE 兜底**（按 CJK 路由） | 机制差（见 §十一 实测） | ⬜ 有规模需求再补 |
| 16 | 检索清洗 | 10+ 类"不插标记/不扫"的判据、多路由 | 4 条清洗规则 + 2 条路由 | 规模 | ❌ |
| 17 | 标题 | 非 NULL 唯一部分索引 + 血缘编号 + `title_source` | 同（少 `title_source`） | **已对齐** | — |
| 18 | 隔离 | 多 profile 多库 + `profile_name` 回填闸门 + IDOR 范围 | **单库**（路径唯一来源） | 机制缺（消费者不存在） | ❌ 见 #1 差距 |
| 19 | 跨进程 | `compression_locks` / `session_turn_leases` / DB 版冷却 | 无（mini 单进程） | 机制缺 | ❌ 第 7 章 |
| 20 | 计费 | 全套计费列 + `session_model_usage` 三表 | 只有 input/output token 计数 | 规模 | ❌ 见 #5 差距 |
| 21 | 网关 | 异常路径输入归属、`observed` / `platform_message_id` / `effect_disposition` | 无 | 机制缺（无网关无平台） | ❌ 第 7 章 |
| 22 | 恢复工具 | `hermes sessions recover` / 导入限额 / `optimize-storage` | 无 | 规模 + CLI 族 | ❌ 见 #7 差距 |
| 23 | 测试防护 | 实盘 guard + `HERMES_STATE_DB_GUARD_BYPASS` + pytest 标记 | 无（测试全走临时 `MINI_HOME`） | 机制缺（消费者不存在） | ❌ 见 #8 差距 |
| 24 | 迁移 | JSONL → SQLite 无迁移器（上游也只说"replaces"） | 同（旧 JSON 不读） | 一致 | — |

---

## 一、门面 + 兄弟模块：抄的是**切法**

### 真实 Hermes 怎么做的

`hermes_state.py:441`：

```python
class SessionDB(
    SessionSessionsMixin, SessionFtsSetupMixin, SessionSearchMixin, SessionSchemaMixin,
    SessionPortabilityMixin, SessionTelegramTopicsMixin, SessionCompressionMixin,
    SessionGatewayMixin, SessionMaintenanceMixin, SessionUsageMixin, SessionTitlesMixin,
    SessionMessagesMixin, SessionRewindMixin,
):
```

13 个 mixin，每个来自一个兄弟文件（`hermes_state_sessions.py:248` 是 `SessionSessionsMixin`、
`hermes_state_messages.py:100` 是 `SessionMessagesMixin`、`hermes_state_search.py:168` 是
`SessionSearchMixin`、`hermes_state_titles.py:23`、`hermes_state_schema.py:174`…）。

**门面自己只管"怎么和 SQLite 说话"**（连接、事务、checkpoint、生命周期），一句话业务都不写。

### mini 怎么做

```python
class SessionDB(SessionSessionsMixin, SessionMessagesMixin,
                SessionSearchMixin, SessionSchemaMixin):
```

4 个 mixin / 5 个文件（`hermes_state.py` 223 行 + 4 个兄弟 185/178/223/248 行）。

### 为什么没做成 13 个

| 真实的 mixin | mini 有吗 | 没有的原因 |
|---|---|---|
| Sessions / Messages / Search / Schema / Titles | ✅ | 本就有主题 |
| Compression | ❌ | mini 的压缩世代落库只有 `record_compaction()` 一个方法，不值得单开文件 |
| Portability / Maintenance | ❌ | 导出/恢复/整理是 CLI 子命令族（差距清单 #7） |
| Telegram / Gateway / Rewind / Usage / FtsSetup | ❌ | 对应能力 mini 都没有（平台、网关、回退、计费、多索引布局） |

**判据**：拆文件的理由是"这个主题有足够多的内容"，不是"上游有 21 个"。mini 装 5 个刚好。

---

## 二、`SCHEMA_SQL` 是唯一事实来源：加列不用写迁移

### 真实 Hermes 怎么做的

- DDL 全在一个常量 `SCHEMA_SQL`（`hermes_state_common.py:324`）。
- 启动时 `executescript(SCHEMA_SQL)` → 全是 `IF NOT EXISTS`，所以**幂等**。
- 然后 `_reconcile_columns()`：用 `PRAGMA table_info` 拿活库的列，和 `SCHEMA_SQL` 解析出来的
  期望列做**差分**，缺的就 `ALTER TABLE ADD COLUMN`（包在 try/except 里，幂等）。
- `schema_version` 只留给"表达不了的变更"：数据迁移、索引/FTS 重建。

### mini 怎么做

`hermes_state_common.py`：`SCHEMA_SQL`（4 张实体表）+ `POST_RECONCILE_SQL`（索引 / FTS / 触发器）。
`hermes_state_schema.py`：

| 函数 | 职责 |
|---|---|
| `parse_schema_columns()` | 从 SQL 文本解析 `{表: {列: 列定义片段}}` |
| `_reconcile_columns()` | 差分 + `ALTER TABLE ADD COLUMN`，返回"补了哪些" |
| `_add_column_safe_definition()` | 把定义改成 `ADD COLUMN` 允许的形状（剥 PK/UNIQUE，NOT NULL 必须有默认值） |
| `_read_schema_version()` / `_run_migrations()` | 版本门控（现在还是空的 v1） |
| `schema_boot_report()` | 打开库时"做了什么"（可观察，给 doctor / 测试） |

### 一条从上游事故里抄来的细节

`_add_column_safe_definition()` 存在的理由是 `#51646`：reconcile 补列时**丢了
`NOT NULL DEFAULT 1`**，于是那批行的 `active` 被写成 NULL，而所有读路径都是 `WHERE active = 1`
→ **整段历史被藏起来，且不报错**。

所以 mini 的解析器返回的是**完整定义片段**不是类型名，测试里专门有一条守着它：

```python
check("列定义片段带约束（不是只有类型名）——#51646 的学费",
      "DEFAULT 1" in declared["messages"]["active"], ...)
```

---

## 三、建库顺序：本章抓到的第一个真 bug（上游有同一条教训）

### 真实 Hermes 怎么做的

`hermes_state_schema.py:919-941` 的顺序写得很明确：

```python
cursor.executescript(SCHEMA_SQL)          # ① 建表
self._reconcile_columns(cursor)           # ② 补列
...
# Indexes referencing reconciler-added columns must be created AFTER _reconcile_columns
# (in SCHEMA_SQL the executescript would fail on legacy DBs).          # ③ 建索引
self._execute_ddl_skipping_settled_triggers(cursor, DEFERRED_INDEX_SQL)
```

### mini 怎么做（以及我踩的那一脚）

**第一版把索引和 FTS 一起塞在 `SCHEMA_SQL` 里**，于是自测第 4 组（"老库缺列自动补齐"）直接炸：

```
sqlite3.OperationalError: no such column: parent_session_id
```

原因是那个测试造的"老库"只有 `id/source` 两列，而 `executescript(SCHEMA_SQL)` 里有
`CREATE INDEX ... ON sessions(parent_session_id)` → 建索引时列还不存在。

**修法**：把 DDL 拆成两段，按真实版的顺序执行 ——

```
⓪ 只读版本检查                      已有库先于 WAL 切换和任何 DDL
① executescript(SCHEMA_SQL)          建表（4 张实体表）
② _reconcile_columns()               补列
③ executescript(POST_RECONCILE_SQL)  索引 / FTS 虚拟表 / 触发器
④ 版本门控
```

拒绝过新库的只读探针在 `SessionDB.__init__` 中先执行；`_ensure_schema()` 再防守一次。后续建表、补列、建索引的顺序仍写进 docstring，并用**测试**守住（反向验证时把顺序改回去，
那一条会干净地变红并报出 `no such column: parent_session_id`）。

---

## 四、连接与 pragma：三个非默认值 + 一次"不信自己"

### 真实 Hermes 怎么做的

`hermes_state.py:731`：

```python
sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=1.0, isolation_level=None)
```

| 参数 | 值 | 理由 |
|---|---|---|
| `timeout` | 1.0（默认 30） | 长等会把"谁先拿锁"拖成秒级；真正解决争用靠应用层重试 |
| `isolation_level` | None | 关掉 Python 的隐式事务，改成显式 `BEGIN IMMEDIATE` |
| `check_same_thread` | False | 工具是并发线程执行的 |

然后是 `_wal_active`（`:740`）：设完 `PRAGMA journal_mode=WAL` **回头读一次磁盘上的真实模式**。

### mini 怎么做

同三条参数（`hermes_state.py:_connect()`），WAL 也一样读回核对：

```python
conn.execute("PRAGMA journal_mode=WAL")
mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
self._wal_active = str(mode).lower() == "wal"
```

测试里有一条守它：`check("WAL 真的生效（读回来核对过）", db.wal_active is True)`。
**注意这条测的是"属性是真值"，不是"代码调过 PRAGMA"** —— 后者是摆设。

### 为什么值得抄

一句话：**"我以为我开了 WAL"和"磁盘上现在是 WAL"是两件事**。网络盘上 SQLite 会静默退回
delete 模式；那时并发行为完全不同（读者会挡住写者），而你的代码看起来一切正常。

---

## 五、写入礼节：`BEGIN IMMEDIATE` + 两档抖动 + 每 50 次 checkpoint

### 真实 Hermes 怎么做的

见导言 0.3（docstring 逐字引过）。三个要点：**先拿锁**（争用在事务开始暴露）、
**整块重试**（回调必须幂等）、**提交在框架里**（调用方不许 commit）。
再加 `_CHECKPOINT_EVERY_N_WRITES = 50` 的 PASSIVE checkpoint。

### mini 怎么做

`SessionDB._execute_write()`（`hermes_state.py`）逐条对齐：

```python
with self._lock:                       # 同进程多线程串行化
    self._conn.execute("BEGIN IMMEDIATE")
    try:
        result = fn(self._conn)
        self._conn.execute("COMMIT")
    except BaseException:
        self._conn.execute("ROLLBACK")
        raise
```

失败分类要**认两种**信号（3.11+ 的 `sqlite_errorcode` 与文本），因为 mini 要同时跑 3.9：

```python
_BUSY_CODES = (5, 6)                   # SQLITE_BUSY / SQLITE_LOCKED
_BUSY_MARKERS = ("database is locked", "database table is locked", "database schema is locked")
```

### 值不值得抄：**必须有**

判据是"mini 有没有真实争用"。有的 —— **第 2 章的工具是并发线程执行的**
（`ThreadPoolExecutor`），agent 又会在回合里逐条落盘，所以"同一进程内两个线程同时写库"
是**每天都会发生**的事，不是假想。

测试也是真争用（不是 mock）：甲连接 `BEGIN IMMEDIATE` 持锁 250ms，乙连接此刻写 →
必须重试后成功，且"重试不算多次写入"（`b.write_count == 1`）。

---

## 六、表结构：抄机制，砍规模

### 真实 Hermes 怎么做的

| 表 | 列数（量级） | mini 要不要 |
|---|---|---|
| `sessions` | 40+ 列（含计费 11 列、网关路由 8 列、工作区 3 列、压缩失败字段…） | 抄 15 列，砍计费/路由/工作区 |
| `messages` | 27 列（含 `api_content` / `observed` / `display_*` / codex 三条） | 抄 13 列 |
| `system_prompts` | 2 列 | ✅ 全抄 |
| `session_model_usage` / `compression_locks` / `session_turn_leases` / `gateway_routing` | — | ❌ 不抄（消费者不存在） |

### mini 怎么做

```sql
sessions(id, source, model, model_config, system_prompt_hash, parent_session_id,
         started_at, ended_at, end_reason, title, message_count, tool_call_count,
         api_call_count, input_tokens, output_tokens)

messages(id, session_id, role, content, tool_call_id, tool_calls, tool_name,
         timestamp, token_count, finish_reason, reasoning, reasoning_content,
         active, compacted)

system_prompts(hash, prompt)        -- 去重
schema_version(version)             -- 单行
```

索引也照抄了关键几条：`idx_sessions_started DESC`、`idx_messages_session_id(session_id, id)`、
`idx_messages_session_active`、以及**标题的非 NULL 唯一部分索引**。

### 为什么砍这些

| 砍掉的 | 原因 |
|---|---|
| 计费 11 列 + `session_model_usage` | 需要价格表版本管理、按模型/任务归属；mini 只记 token 数 |
| 网关路由 8 列（`session_key` / `chat_id` / …） | 消费者是网关（第 7 章） |
| `api_content`（字节保真） | 消费者是"临时层注入过、要按字节重放"的场景；mini 的临时层不进历史 |
| `display_*` 5 列 + 6 个 display 触发器 | 消费者是桌面端展示排序 |
| `observed` / `platform_message_id` / `effect_disposition` | 消费者是网关异常路径与平台重投 |

---

## 七、`append_message`：一个签名定义三个契约

### 真实 Hermes 怎么做的

`hermes_state_messages.py:279-291`：22 个关键字参数，然后

```python
msg = dict(locals())  # every keyword above is a message-dict field of the same name
```

### mini 怎么做

13 个关键字参数，同一个把戏，但**列清单从 `SCHEMA_SQL` 推出来**：

```python
_MESSAGE_COLUMNS = tuple(parse_schema_columns(SCHEMA_SQL)["messages"])
...
row = {k: v for k, v in locals().items() if k in _MESSAGE_COLUMNS}
```

这一步比我原来想的更值：**"列清单"和"插入语句"都从同一份 DDL 长出来**，所以加了列忘了改
插入代码这件事**不可能发生**（要么自动带上，要么根本不在列里）。

### 一处刻意的不同：`tool_name` 只进库、不进消息 dict

第一次写的时候我把 `tool_name` 塞进了工具结果的**消息 dict**，然后意识到：
那份 dict 会被**原样发给模型**（`_build_api_messages` 只是拷一层），严格 provider 会 400。

修法：`_persist_message(msg, tool_name=...)` 让库拿到它，而消息 dict 保持线上形状。
读路径的 `_WIRE_FIELDS` 也把它排除在外（`role` / `content` / `tool_calls` / `tool_call_id`），
并留了一条注释说明"为什么它在这里缺席"。

**这是"同一个字段有两个舞台"的典型例子**：库里它要用来检索（LIKE 兜底扫三列），线上它一秒都不该出现。


---

## 八、两种读法：库里什么样 vs 喂模型什么样

### 真实 Hermes 怎么做的

| 方法 | 给谁 | 关键参数 |
|---|---|---|
| `get_messages()` | 排查 / 导出 / 看世代 | 原始行 |
| `get_messages_as_conversation()` | **喂模型 / 重放** | `include_ancestors` / `include_inactive` / `include_compacted` / `repair_alternation` / `include_row_ids` |

docstring 里那句约束是这一节的灵魂（`hermes_state_messages.py:951`）：

> `the model-fed restore must not regrow what compaction summarized away`

即：**喂给模型的那份默认只看活世代**。压缩掉的归档世代只给"展示"用
（`include_compacted` 是 "deduped display history"），**不许顺着重放复活**。

### mini 怎么做

```python
db.get_messages(sid)                                    # 原始行（tool_calls 是 JSON 字符串）
db.get_messages_as_conversation(sid)                    # 线上形状（tool_calls 是对象）
db.get_messages_as_conversation(sid, include_ancestors=True)   # 带上祖先
db.get_messages(sid, include_compacted=True)            # 两个世代都看
```

三个档位的取行语义也照抄（`_fetch_rows`）：

| 参数组合 | 取哪些行 | 谁用 |
|---|---|---|
| 默认 | `active = 1` | **喂模型** |
| `include_compacted=True` | `active = 1 OR compacted = 1` | 展示/排查（多世代） |
| `include_inactive=True` | 全部 | 审计（含被回退的行） |

### mini 没抄的三样（都写进差距清单）

| 真实有 | mini | 为什么 |
|---|---|---|
| `repair_alternation` | ❌ | mini 的交替校验**很严**（`_validate_alternation` 直接抛错），历史上不该出现 `user;user`；真出现时应该**炸给开发者看**而不是悄悄修 |
| `api_content` 字节保真 | ❌ | 需要"临时层注入也要按字节重放"的消费者；mini 的临时层不进历史 |
| `include_row_ids` | ❌ | 桌面端展示要行 id；mini 没有桌面端 |

**注意第 8 行那个取舍是"反向"的**：真实版对脏数据**宽容**（修一修继续用），mini **严厉**
（炸）。这不是谁对谁错 —— 真实版面对的是线上用户的历史，mini 面对的是学习项目的开发期。
判据写在这里，以后要改就知道为什么改。

---

## 九、世代：`active` / `compacted` 与原地压缩落库

### 真实 Hermes 怎么做的

一行 SQL（`hermes_state_messages.py:44`）：

```sql
UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1
```

**为什么这行 SQL 会用两个列而不是一个**（理解这个才能理解后面的"重复行"）：

| 组合 | 含义 |
|---|---|
| `active=1, compacted=0` | 活行（喂模型） |
| `active=0, compacted=1` | **被压缩归档**的旧世代（展示用，不许喂模型） |
| `active=0, compacted=0` | 被**回退/撤销**的行（谁都不看，除非显式审计） |

真实版明文警告：受保护的消息会在两个世代里各出现一次、内容与时间戳完全相同 ——
**这是设计，不是重复数据**。

### mini 怎么做

`archive_active_rows()`（归档）+ `record_compaction(session_id, compressed_messages)`
（归档 + 新世代插入），并且在**第 5 章的压缩路径上真的接了一刀**：

```python
before = [dict(m) for m in messages]
messages[:] = c.compress(messages, call_llm=self._summary_llm)
if self.session_db is not None and before != messages:
    self.session_db.record_compaction(self.session_id,
                                      [m for m in messages if m.get("role") != "system"])
```

**为什么必须接这一刀**：不接的话 `active` / `compacted` 两列就是**摆设**
（抄了机制却没有消费者 = 假承诺，第 4 章那条纪律）。

### 值不值得抄：**值得，但要知道代价**

- 收益：数据库里留下"压缩历史"—— 出问题时能看到"这一轮之前长什么样"，而不是只有压缩后的结果。
- 代价：**同一份内容存两遍**。所以真实版要处理"展示时去重"（`_dedupe_display_generations`），
  而 mini 现在不做（差距清单 ⬜）—— mini 的 `include_compacted=True` 会**原样返回两个世代**，
  这件事在 docstring 里写明了，测试里也守着。

---

## 十、血缘：压缩轮转的落地形式

### 真实 Hermes 怎么做的

- `parent_session_id` 自引用外键 + `idx_sessions_parent`。
- 官方文档给了两段递归 CTE（找祖先 / 找后代）。
- `get_messages_as_conversation(include_ancestors=True)` 会走 `_resume_lineage_ids()`
  把整条链的消息取回来，并且**对祖先的重复行做去重**
  （`_dedupe_replayed_user`：轮转会把并发尾巴整段复制到子会话，所以副本可能不相邻 ——
  先用 `(timestamp, canonical content)` 精确比，再用相邻启发式）。

### mini 怎么做

```python
db.session_lineage("leaf")        # → ["root", "mid", "leaf"]（最早的祖先在前）
db.get_messages_as_conversation("leaf", include_ancestors=True)
```

`session_lineage` 用一段递归 CTE（`WITH RECURSIVE lineage AS (…)`），顺序按 `started_at` 排。

### 为什么这一章才有血缘

第 5 章之前，mini 的对话**不会分裂**（压缩是原地替换消息列表），所以"父会话"是个没有来源的概念
（旧 `hermes_state.py` 的 docstring 里只是**许诺**了 lineage）。
第 5 章接上压缩之后，**网关侧的会话轮转**才会真的产生"新会话 + 旧会话"的结构
—— 消费者出现了，机制才值得做。

### mini 没抄的

| 真实有 | 为什么没抄 |
|---|---|
| 祖先重放去重（`_dedupe_replayed_user`） | 消费者是"网关轮转把并发尾巴复制进子会话"这个具体场景；mini 的轮转还没做 |
| 末端重放修复（`repair_alternation`） | 见 §八 |

---

## 十一、检索：为什么是"两条路"（这一节是实测出来的）

### 真实 Hermes 怎么做的

四条路由（FTS5 词索引 → trigram → cjk_unicode61 → LIKE 兜底），外加"未索引区间"的补充扫描：

| 索引 | 解决什么 |
|---|---|
| `messages_fts`（unicode61） | 英文/数字的**按词**检索，带 BM25 排序 |
| `messages_fts_trigram` | **子串**检索（≥3 字符）与 CJK |
| `messages_fts_cjk`（cjk_unicode61 扩展） | CJK 按字切分 |
| `_search_messages_like_fallback` | 上面都不适用时（FTS 过期、工具行、被排除的 source）扫基表 |

### mini 的实测：为什么最终只留两条

我先只抄了 `messages_fts`，然后拿真输入试：

```
默认分词器（unicode61）：'部署' → 0 命中    '部署文件' → 1 命中
                        （一整串中文被当成一个"词" → 只能整串匹配）
```

于是去看 trigram 能不能救，**实测不能**（trigram 要求查询串 ≥3 字符）：

```
加 trigram 表：'部署' → 0 命中    '部署文' → 1 命中    '中文' → 0 命中
```

而 `cjk_unicode61` 需要**编译扩展**（真实版 `load_fts5_cjk_extension`），零依赖的 mini 用不了。

**结论**：mini 留两条 —— 英文走 FTS5（快、能排序），含 CJK 的查询走 **LIKE 兜底**
（真实版同名机制 `_search_messages_like_fallback`，扫三列 + `ESCAPE` + `instr` 定位片段）。
路由规则写得很直白：

```python
if has_cjk(query):
    return self._search_like(...)
return self._search_fts(...)
```

结果里每条都带 `route`（`"fts"` / `"like"`），让"是哪条路服务的"**可观察**（不是靠猜）。

### 查询清洗：抄了四条规则

`sanitize_fts5_query()` 对应真实版 `_sanitize_fts5_query`（`hermes_state_search.py:789-792`）的
docstring 描述：

| 输入 | 问题 | 洗成 |
|---|---|---|
| `chat-send` | `-` 被当语法 | `"chat-send"` |
| `my-app.config.ts` | 点号也是 | `"my-app.config.ts"` |
| `"未配对` | 引号不配对 → 语法错 | `未配对` |
| `hello AND` | 悬空布尔操作符 → 语法错 | `hello` |
| `p2p:8080` | `:` 会被当"列过滤" | `p2p 8080` |
| `deploy*` | 前缀查询**要保留** | `deploy*` |

### 值不值得补 trigram / cjk

判据不是"上游有"，而是"mini 有没有这个需求 + 代价"：

- **需求**：有（中文用户搜中文历史）。但 **LIKE 兜底已经解决了**（mini 的库规模下性能足够）。
- **代价**：trigram 会给每张索引表加 ~2.6 倍影子存储（上游 v30 的注释原话），
  还要一套"老库重建 + optimize-storage"的机制 —— 那是 mini 用不上的规模。
- **结论**：⬜ 有规模需求再补（差距清单 #4）。

---

## 十二、标题：非 NULL 唯一 + 血缘编号

### 真实 Hermes 怎么做的

- `idx_sessions_title_unique`：**部分索引** `ON sessions(title) WHERE title IS NOT NULL`。
  于是"没有标题"可以有无数个，而"起了名字"的之间必须唯一。
- `get_next_title_in_lineage(base)`：`"X"` 被占用 → `"X #2"` → `"X #3"`。
- 还有 `title_source`（记录标题是谁起的：用户还是模型）。

### mini 怎么做

| 方法 | 行为 |
|---|---|
| `set_session_title()` | 撞唯一约束时**返回 False 而不是抛**（调用方是 UI，不该因为它崩） |
| `resolve_session_by_title()` | 按标题找，血缘里取**最近**的 |
| `get_next_title_in_lineage()` | 没人用基础名 → 就用它；被占用 → `#2` → `#3` |

`title_source` 没抄（mini 没有"模型自动起标题"这条路径，消费者不存在）。

---

## 十三、隔离：为什么它必须从"库路径"讲起

### 隔离要防的是什么（先看"不隔离"会长什么样）

| 不隔离的后果 | 具体会发生什么 |
|---|---|
| **历史串台** | `--resume` 把别的身份/项目的对话恢复出来 → 上下文被污染，甚至**把 A 项目的内容发到 B 项目的平台上** |
| **密钥串台** | 用 A 的 bot token 去发 B 的消息 → 消息出现在**错误的账号**下，而且是别人可见的 |
| **权限串台** | 默认 profile 开了 `GATEWAY_ALLOW_ALL_USERS` → 第二个 bot 跟着也开了 → **安全边界静默失效** |

### 真实 Hermes 怎么做的：**三层**，一层比一层软

| 层 | 机制 | 一句话说清 | 锚点 |
|---|---|---|---|
| **① 文件层**（最硬） | 一个 profile 一个 home，一个 `state.db` | 隔离是**分家**，不是过滤 —— 压根打开的是**另一个文件** | `get_hermes_home() / "state.db"`；具名 profile 在 `profiles/<名>/` |
| **② 行层** | `sessions.profile_name` + 回填闸门 | 库万一被搬走/恢复/合并，行上还盖着"我属于谁"的章；**NULL 读作"无主"，不是"默认"** | `hermes_state_sessions.py:289-290`、`#88381` |
| **③ 入口层** | `chat_id` / `thread_id` 给 `/resume` 划范围 | **知道 session_id ≠ 有权读它**（IDOR） | 同一段注释 `:288-289` |

**① 已经分家了，为什么还要 ②③？** 因为"分家之后"还有这些事会发生：

- 库被**备份 / 恢复 / 导入**到另一个 profile（`hermes sessions recover`、导出再导入）；
- gateway 轮转时把**并发尾巴整段复制**进子会话（于是同一条消息出现在两个会话里）；
- 一个进程里握着**好几个库的句柄**（multiplexing），哪次操作该用哪个是**会话属性、不是进程属性**。

### 为什么它难：多对多 + 静默

**多对多**：一个 `hermes serve` 进程可以同时服务多个 profile → "现在该用哪个库"**不能靠进程状态推断**，
必须显式绑定。

**静默**：不绑定**不报错**。顶层 `AGENTS.md` 的原话：

> `an unbound read is a silent default-profile leak, never an error`
> （未绑定的读取是**静默的默认 profile 泄漏**，永远不会报错）

两个真实例子：

**例 1 · 子进程丢了 `HERMES_HOME`**

```text
主进程（profile=coder）启动子进程
   └─ 忘了传 HERMES_HOME
        └─ 子进程回落到默认 profile 的 home
             └─ 它在【默认库】里建了会话、写了消息
结果：不报错。只是"coder 的历史丢了、默认 profile 里多了点奇怪的东西"。
```

**例 2 · `#88381`（行层那道闸门存在的理由）**

session key 里**编码了 profile 命名空间**：`agent:<ns>:…`（默认 profile 是 `agent:main:…`）。
子会话从父亲回填 `profile_name` 时，如果**不校验命名空间**：

```text
默认 profile 的子会话 fork 自兄弟 profile 的行
   └─ 回填了兄弟的 profile_name
        └─ 这条会话被【永久】误标成兄弟 profile 的（durably mislabelled）
             └─ 之后的统计 / 展示 / 权限全跟着错，而且改不回来
```

所以真实版的闸门是：**无 key 的行自由继承；两个带 key 的行必须命名空间一致**。

### 判据（带走这三问）

> ① 这个操作**打开了哪个文件**？（来源必须是**解析器**，不是写死的常量）
> ② 这个值**换一个 profile 会变吗**？（会 → 必须绑定；不会 → 它本来就该共享）
> ③ 这个操作**失败时是报错，还是悄悄用了默认值**？（悄悄 → 危险）

第 ③ 问最要命：**多 profile 的 bug 几乎从不表现为崩溃，只表现为"数据莫名其妙跑到别处"。**

### mini 怎么做

单 profile，所以只抄了**最核心的那一条**：**路径只有一个来源**。

```python
def state_db_path() -> Path:              # hermes_constants.py
    return get_mini_home() / "state.db"   # ← 全项目搜不到第二个 "state.db" 字面量
```

它的价值不在"现在只有一个 profile"，而在于**多 profile 的改动面被压缩到这一个函数**：
以后要支持多 profile，改的是 `get_mini_home()`（按名字解析）+ 构造时传入，
而不是满仓库找哪个文件写死了路径。

**实测（两个 home = 两个"profile"）**：

```text
A 家：库 = /tmp/iso-a/state.db    写了一条「项目代号 猎户座」→ 自己搜到 1 条
B 家：库 = /tmp/iso-b/state.db    搜「猎户座」 → 0 条
                                  会话列表     → 空
                                  拿 A 的会话 id 直接读 → []
                                  --resume     → None
两个目录里各有一个 state.db（86,016 字节），互不相通
```

这份实测也是测试守着的东西：`check("换一个 MINI_HOME → 换一个库（不会串库）", …)`。

### 为什么多 profile 那一整套不抄

| 真实的机制 | mini | 原因 / 什么时候必须补 |
|---|---|---|
| 一个 profile 一个库 | ❌ | mini 单 profile；**要第二个身份/第二个平台 bot 时必须补** |
| `profile_name` 列 + 回填闸门 | ❌ | 有"跨 profile 分叉 / 导入"时补 —— 否则 `#88381` 会重演（而且是**不可逆**的误标） |
| IDOR 范围（`chat_id/thread_id`） | ❌ | 有网关时补（别人能给你传 session_id） |
| 实盘 guard（测试别碰生产库） | ❌ | mini 的测试全走临时 `MINI_HOME`；代价：**别人给 mini 写测试时可能碰到真实库** |

**补法的顺序是死的**（真要做时照这个来）：

```text
① home 解析器支持多 profile（按名字解析，不是写死）
② SessionDB 构造时绑定自己的 home（不许从全局读）
③ 行上盖 profile_name + 回填闸门（校验命名空间）
④ 入口加范围（谁能 resume 谁）
⑤ 测试用【两个 home 跑 A→B→A】
   （上游 AGENTS.md 的要求原话："Prove live with two homes (A→B→A) under multiplex,
     not one temp HERMES_HOME"）
```

### 收束

**隔离的本质不是"加个过滤条件"，而是"让'这个操作属于谁'这件事必须被显式说出来"。**

文件层负责**分家**，行层负责**搬动之后还认得出自己**，入口层负责**知道 id 也不等于有权读**。
而这一章之所以必须从"库路径"讲起 —— 存储是隔离的落点，`state.db` 是最大的那个文件；
硬编码路径 = 拆掉隔离，而且**不报错**。

## 本章实现时抓出的真问题

| # | 问题 | 症状 | 修法 |
|---|---|---|---|
| 1 | **建库顺序错**（索引跟着建表一起跑） | 老库打开直接 `no such column: parent_session_id` | 拆成 `SCHEMA_SQL` + `POST_RECONCILE_SQL`，按 建表→补列→建索引 执行（上游同一条教训，`:929-930`） |
| 2 | **`sqlite3.Row` 没有 `.get()`** | 读路径 `AttributeError` | 老实 `dict(row)` 再取；注释写清这个坑 |
| 3 | **`int(cur.lastrowid)` 可能是 None** | 静态检查报 `int \| None` | `int(cur.lastrowid or 0)` |
| 4 | **`tool_name` 混进消息 dict** | 会被原样发给模型（严格 provider 400） | `_persist_message(msg, tool_name=...)` 分离两个舞台 |
| 5 | **`include_compacted` 被 `include_inactive` 挡住** | 想看两个世代却只拿到 2 行 | `_fetch_rows` 改成三档语义（照抄真实版） |
| 6 | **中文搜不到**（不是 bug，是设计缺口） | `/search 部署` 命中 0 | 实测三种索引后决定：CJK → LIKE 兜底 |
| 7 | **反向验证时套件被带崩** | 建库顺序改回去 → 整个套件 Traceback | 用例自己接住异常 + harness 把异常记成"可诊断的红" |
| 8 | **测试用真争用会 `BEGIN` 套 `BEGIN`** | `cannot start a transaction within a transaction` | 测试里用裸 SQL 拿锁（不走 `_execute_write`） |

第 7 条值得单独说：反向验证的要求是"**干净地**变红"。第一轮做的时候第 1 条改回去直接把
整个测试文件崩了（后面的组一个都没跑）—— 那不算可诊断。所以给用例加了 try/except
（把异常变成一行 `✗ … → OperationalError: no such column: …`），并给 harness 加了同样的兜底：
**任何用例抛异常都记成一条失败并继续**。改完之后 8 项反向验证全部干净变红。

---

## 测试覆盖：107 条 / 14 组在守什么

| 组 | 守的承诺 |
|---|---|
| 1. 建库 | 表/索引/FTS5 都在 · 版本正确 · **WAL 真生效**（读磁盘核对） |
| 2. 幂等打开 | 反复打开不重建、不报错、版本不动 |
| 3. `SCHEMA_SQL` 唯一来源 | 声明里的列活库全有 · 列定义**带约束**（#51646） |
| 4. 声明式加列 | 老库缺列自动补齐且有报告 · **顺序对**（先补列再建索引） |
| 5. 版本门控 | 库比代码新 → 明确拒绝并报出版本 |
| 6. upsert | 先写者赢 · 只补空缺 · `started_at` 不移动 · 提示词去重 |
| 7. 追加与计数 | 形参名=列名 · rowid 递增 · 计数跟着涨 · `record_usage` 累加 |
| 8. 两种读法 | 库里那份带元数据 / 喂模型那份不带 · `tool_name` 只留在库里 |
| 9. 归档世代 | 归档后默认读不到 · `include_compacted` 两个世代并存 · 压缩不改 `message_count` |
| 10. 血缘 | 链的顺序 · `include_ancestors` 取回祖先 |
| 11. 检索两条路 + 清洗 | 英文 fts / 中文 like · 子串命中 · 上下文 · 过滤 · 六种清洗 |
| 12. 写入礼节 | **真争用**下重试成功 · 两档抖动区间 · 每 50 次写一次 checkpoint |
| 13. 标题 | 非 NULL 唯一（重名返回 False）· 血缘编号 |
| 14. 隔离 | 库路径唯一来源（跟随 `MINI_HOME`） |

**总账**：`test_session_storage.py` **511 行 / 14 组 / 107 条断言**；
全仓 **575 通过 / 0 失败**（双解释器 3.13.14 与 3.9.6 完全一致）。

### 反向验证（8 项，全部干净变红）

| 改回旧写法 | 变红的那一条 |
|---|---|
| ① 建库顺序：先建索引再补列 | 老库能直接打开（顺序：建表 → 补列 → 建索引）→ `OperationalError: no such column` |
| ② reconcile 只取类型名 | 列定义片段带约束 —— #51646 的学费 |
| ③ 去掉"拒绝更新的库" | 库比代码新 → 明确拒绝打开 |
| ④ upsert 改成覆盖 | 后写不覆盖先写的 model / title |
| ⑤ 追加不更新计数 | message_count 跟着涨 / tool_call_count 只数工具调用 |
| ⑥ 读路径不过滤 active | 归档后默认读不到任何行 |
| ⑦ 去掉 CJK 路由 | 中文走 LIKE 兜底 / 中文子串能搜到 |
| ⑧ 抖动只剩快档 | 慢档落在 250–1000ms |

### 真跑收据（真 DeepSeek，`MINI_HOME=/tmp/ch06-real-home`）

```
第 1 轮：用工具列 /tmp 下 ch06* 的文件 → 读 /tmp/ch06-checklist.md → 总结
        3 次 API、1 个工具轮（terminal + read_file）
        /sessions → 库：/tmp/ch06-real-home/state.db，1 个会话，消息 6，预览正确
        /search ch06 → 命中 6 条，走的是 fts 那条路，片段带 >>>ch06<<< 标记
        /exit → 「会话已写入 /tmp/ch06-real-home/state.db（6 条消息 · mini --resume 可继续）」

第 2 轮：--resume → 「已恢复会话 20260928_124327_24b701（6 条消息，来自 …/state.db）」
        问"刚才我让你做了什么" → 模型答对了（说明历史真的恢复成了线上形状）
        → 8 条消息（6 + 2），**同一个会话行**（resume 不新建行，只追加）

库内实况（sqlite3 直查）：
  sessions: message_count=8  tool_call_count=2  api_call_count=4
            input_tokens=18700  output_tokens=503  system_prompt_hash=03005014070f…
  messages: 8 行，role 序列 user/assistant/tool/assistant/tool/assistant/user/assistant
            tool 行带 tool_name，assistant 行带 tool_calls JSON
  messages=8  messages_fts=8        ← 外部内容表：索引行数跟着主表（触发器真的在工作）
  system_prompts: 1 行 / 3671 字符  ← 提示词去重（8 条消息 + 1 个会话只存一份）
  schema_version: 1
  目录里还有 state.db-wal / state.db-shm（WAL 生效）

提示词漂移告警（换工作目录再 resume）：
  「注意：这个会话的**系统提示词**和存盘时不一样了（存盘 3,671 字符 → 现在 873 字符）…」
```

最后一条是第 3 章欠的账：`_ensure_system_message` 的注释里写着
"mini 还没做这个校验——写进第 6 章的清单了"。现在做了，而且是**说出来的**（不是静默换人格）。

---

## 十四、差距清单（每条标注值不值得改）

### A 批 · 已抄（13 条，全部落地）

| # | 条目 | mini 落地 |
|---|---|---|
| 1 | 门面 + 兄弟模块（mixin 拼装） | `hermes_state.py` + 4 个兄弟（5 个文件而不是 21 个） |
| 2 | `SCHEMA_SQL` 是唯一事实来源 | `hermes_state_common.py`；列清单也从它推（`_MESSAGE_COLUMNS`） |
| 3 | 声明式加列 | `_reconcile_columns()` + `schema_boot_report()` |
| 4 | 建库顺序（表→补列→索引） | `SCHEMA_SQL` / `POST_RECONCILE_SQL` 两段 |
| 5 | 版本门控 | `schema_version` + `_run_migrations()` 形状（+ **拒绝更新的库**） |
| 6 | 表结构 + 关键索引 | 15 列 / 13 列 / 部分唯一索引 |
| 7 | `system_prompts` 哈希去重 | `_store_system_prompt()` + `get_stored_system_prompt()` |
| 8 | 连接与 pragma + WAL 读回核对 | `_connect()` + `wal_active` |
| 9 | 写入礼节（`BEGIN IMMEDIATE` + 两档抖动 + checkpoint） | `_execute_write()` / `_jitter_delay()` / `_try_wal_checkpoint()` |
| 10 | upsert 保留先写者 | `create_session()` 的 `COALESCE` 清单 |
| 11 | `append_message` 三契约同名 | 13 kwargs + `locals()` 过滤 |
| 12 | 两种读法 + 世代（`active`/`compacted`） | `get_messages*` + `archive_active_rows()` + `record_compaction()`（**接在压缩路径上**） |
| 13 | FTS5 外部内容表 + 触发器 + 查询清洗 + 标题/血缘 | `hermes_state_search.py` / `_titles` 部分 / `session_lineage()` |

### B 批 · 不抄（10 条，每条带原因与代价）

| # | 条目 | 为什么不抄 | 代价 |
|---|---|---|---|
| 1 | 多 profile（多库 + `profile_name` + 回填闸门 + IDOR 范围） | mini 单 profile，消费者不存在 | 支持多 profile 时必须补，否则**静默串库** |
| 2 | `compression_locks` / `session_turn_leases` / `_COMPRESSION_BUSY_WAIT_S` | 跨进程锁；mini 单进程 | 多进程压缩会互相踩（第 7 章） |
| 3 | trigram / cjk 索引 + `fts_storage_version` 独立计数 + 后台重建 + `optimize-storage` | 实测 **trigram 救不了 2 字中文词**；cjk 需要编译扩展 | CJK 检索靠 LIKE 扫描；库大了会慢 |
| 4 | 展示层（`display_*` 5 列 + 6 触发器 + 世代去重） | 消费者是桌面端 | 桌面端要重排历史时得补；`include_compacted` 会返回重复内容 |
| 5 | 计费全套（11 列 + `session_model_usage` 三表） | 规模型 | 不能按模型/任务出账 |
| 6 | 网关字段（`observed` / `platform_message_id` / `effect_disposition` / `api_content`）+ 输入归属仲裁 | 消费者是网关异常路径与平台重投 | 重复写消息时没有"谁负责"的仲裁（第 7 章） |
| 7 | CLI 族（`sessions recover` / `export_all` / `prune_sessions` / 导入限额） | 规模型 + 子命令族 | 库损坏时没有恢复工具 |
| 8 | 实盘 guard（`HERMES_STATE_DB_GUARD_BYPASS` + pytest 标记） | mini 测试全走临时 `MINI_HOME` | 别人写测试时可能碰真实库 |
| 9 | `repair_alternation` / `include_row_ids` / 祖先重放去重 | 消费者是"桌面端 + 网关轮转复制尾巴" | 历史被污染时 mini 会**炸**而不是悄悄修（有意选择，见 §八） |
| 10 | `system_prompt` 旧文本列迁移 CASE / JSONL→SQLite 迁移器 | 没有历史库要迁移（上游也只说 "replaces"） | 第 6 章之前的 JSON 会话读不出来（git 里有旧版本） |

### mini 侧新增（真实版没有，标出来）

| # | 条目 | 为什么加 |
|---|---|---|
| M1 | **拒绝打开比代码更新的库**（`SchemaTooNewError`） | 挡住"用旧代码写新库"这类静默破坏；真实版只在更旧时推进 |
| M2 | `/sessions` 与 `/search` 两个 REPL 命令 | 给 `list_sessions()` 与 FTS5 一个**真实消费者**（否则抄了没人用 = 假承诺） |
| M3 | 提示词漂移告警（`_check_resumed_prompt`） | 第 3 章欠的账；真实版靠 `_stored_prompt_matches_runtime()` 校验，mini 用"存盘 vs 现在"的字节比较 + 说出来 |

---

## 附录 A · 术语表（本章新增）

| 原文 | 译法 | 说明 |
|---|---|---|
| facade / sibling | 门面 / 兄弟模块 | 门面只做连接与生命周期，业务按主题分在兄弟里 |
| declarative column add | 声明式加列 | `SCHEMA_SQL` 说有哪些列，活库差分后 `ADD COLUMN`；**不用写迁移** |
| reconcile | 差分补齐 | `_reconcile_columns()` 的动词 |
| generation（compaction） | 世代 | 同一会话的多套消息行：活世代 vs 归档世代 |
| active / compacted | 活行标记 / 归档标记 | 两个布尔组合出三种状态（活 / 归档 / 被回退） |
| external-content FTS | 外部内容 FTS | FTS5 只存倒排索引，正文仍在主表（靠 rowid 指） |
| tokenizer | 分词器 | unicode61（默认，不切 CJK）/ trigram（≥3 字符）/ cjk_unicode61（需扩展） |
| fallback（LIKE） | 兜底扫描 | FTS 不适用时扫基表三列 |
| write contention | 写入争用 | 多进程/多线程抢同一个 SQLite 写锁 |
| convoy effect | 车队效应 | 确定性退避让所有写者同步重试 |
| WAL / checkpoint | WAL / 检查点 | PASSIVE checkpoint = 能收就收，不阻塞读者 |
| lineage | 血缘 | `parent_session_id` 链，压缩轮转时产生 |
| IDOR | 越权直接对象引用 | 光知道 id 就能读别人的东西（网关 `/resume` 的范围闸门防它） |

---

## 附录 B · 这一章在整套文档里的位置

```
第 2 章  回合循环          —— 消息在内存里怎么流动
第 3 章  提示词装配        —— 每一轮发给模型的那包怎么拼
第 4 章  工具运行时        —— 工具怎么注册、怎么被选中、结果什么形状
第 5 章  上下文压缩与缓存   —— 窗口满了怎么办、缓存怎么不被打断
第 6 章  会话存储 ★本章    —— 上面这些**落在哪**（SQLite）+ 压缩的世代痕迹
第 7 章  网关 / 平台       —— 消息从外部怎么进来（跨进程锁、多 profile、输入归属都在那一章）
```

**本章与第 5 章的接口**（两条，都接上了）：

| 接口 | 落地 |
|---|---|
| 压缩要**改库里的行** | `record_compaction()` 接在 `_preflight_compression()` 之后（老世代归档 + 新世代插入） |
| 压缩要**跨进程锁** | `compression_locks` 表在真实版里；mini 没有 → 第 7 章（差距清单 #2） |

**谁会读这个库**（本章之后会反复遇到）：

| 读者 | 读什么 | mini 现状 |
|---|---|---|
| agent 回合循环 | 启动时读历史（`get_messages_as_conversation`），每追加一条写一次 | ✅ 已接 |
| `/resume` | 最近的有消息会话 + 血缘链 | ✅ 已接 |
| 会话列表 / 检索 | `list_sessions()` / FTS5 | ✅ 已接（`/sessions` `/search`） |
| `session_search` 工具 | 同上的 FTS5 | ⬜ 第 10 章（工具群） |
| 计费 | 会话行上的 token 与成本 | ⬜ 不抄（只留 token 计数） |

---

## 附录 C · 上游文档出入（3 处）与两条实测补充

见《中文精译-06-SessionStorage.md》附录 C 的三条（本章不再重复）：

1. 文档标题写"当前 schema 版本 23"，它自己的版本表列到 **30**，源码是 `SCHEMA_VERSION = 30`
   （`hermes_state_common.py:239`）。
2. 写入重试常量 `_WRITE_MAX_RETRIES` 在源码里**搜不到**；且抖动是**两档**
   （前 2 秒 20–150ms，之后 250–1000ms，`hermes_state.py:476-480`）。
3. **FTS 布局版本独立计数**（`fts_storage_version` / `FTS_STORAGE_VERSION = 2`），
   不在 `schema_version` 里；老库停在 layout 0，直到 `hermes sessions optimize-storage`。

**本章新增的两条实测结论**（不是文档错，是文档没写的）：

4. 默认分词器（unicode61）**不切 CJK**：中文只能整串匹配（`部署文件` 命中、`部署` 不命中）。
5. trigram 也救不了 2 字中文词（要求查询串 ≥3 字符）—— 这是 mini 只留 LIKE 兜底的理由。
