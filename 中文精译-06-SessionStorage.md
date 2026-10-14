# 中文精译 · 第 6 章：会话存储（Session Storage）

> **原文**：`website/docs/developer-guide/session-storage.md`（524 行）
> **本篇定位**：翻译为主，目标是**看懂原理**。差异对比、源码深挖、实现日志都不在这里 ——
> 那些属于后面两个阶段（mini 实现 / 源码讲解）。
> 看不懂的地方或想深挖的点，随时问，我再往这里补。
>
> 📌 行号锚在上游 commit **`f5d1926110`**（`version = "0.21.3"`）；上游每合并一个 PR 行号就会漂。
> 译文里凡是**与源码不符**的地方，用 `⚠️ 上游文档出入` 标出来（本章已发现 3 处，见附录 C）。

> mini 当前维护说明见对应《源码讲解》的“发布前维护”节；原文翻译与历史实测不代替当前实现契约。

[TOC]

---

## 0. 这一章在回答什么

前五章讲的是"消息在内存里怎么流动"。这一章讲的是**落地**：那些消息**最后存在哪**。

```
一次对话的生命周期
  内存里：messages 列表（第 2~5 章）
     │
     ▼  每追加一条就写一次
  磁盘上：~/.hermes/state.db（SQLite，本章）
     │
     ├─ 会话元数据（模型、token 计数、计费、标题…）
     ├─ 完整消息历史（含 reasoning、tool_calls 这些旁挂字段）
     └─ 全文索引（FTS5）→ 给 session_search 用
```

**这一章要解决三个问题**：

1. **存在哪** —— 路径不是一个常量，而是**解析出来的**（profile 隔离 → §1）。
2. **长什么样** —— 表结构、索引、版本迁移（→ §5、§6）。
3. **多个进程同时写怎么办** —— 网关 + CLI + worktree agent 共用一个库（→ §7）。

**先说两件容易搞混的事**（后面反复出现）：

- **`compaction` ≠ 第 5 章的 `compression`**。第 5 章那个是"把旧轮次摘要成一段"；
  本章说的 **compaction 是行级的**：把旧行打上 `active=0`（归档）、把保留的上下文
  作为 `active=1`（活行）重新插入。摘要生成先改消息列表，持久化层再将结果落库；它们不是两套各自直接写库的算法（→ §1.7）。
- **每个 profile 有一个独立的 `state.db`，一个进程可以持有多个库**。每个具名 profile 有**自己的** `state.db`；
  一个 `hermes serve` 进程可能同时服务好几个 profile，所以"现在该用哪个库"是**跟着会话走的**，
  不是跟着进程走的（→ §1.2、§1.6）。

本章出现的源码文件（原文照抄，便于核对）：

```
hermes_state.py         —— 门面（facade）：对外入口
hermes_state_*.py       —— 兄弟模块（schema / fts / search / compression / portability / gateway …）
hermes_state_common.py  —— SCHEMA_SQL 与 SCHEMA_VERSION 在这里
hermes_state_schema.py  —— 建表与迁移的执行者
gateway/delivery_ledger.py —— 发件箱表（按需创建）
```

---

## 1. Hermes home 与 profile 隔离

### 1.1 路径是**解析**出来的，不是一个常量

`get_hermes_home()` 是状态与配置的**权威**文件系统解析器。解析顺序（**从高到低**）：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | **context-local 覆盖**（上下文局部） | 代码里显式设的那个，优先级最高 |
| 2 | **`HERMES_HOME` 环境变量** | 子进程 / 多 profile 场景靠它传递 |
| 3 | **平台默认** | macOS / Linux：`~/.hermes`；Windows：`%LOCALAPPDATA%/hermes` |

推论（原文强调）：**默认数据库永远是 `get_hermes_home() / "state.db"`**，
而不是让调用方硬编码 `~/.hermes/state.db`。硬编码会在 profile 场景下静默写错库。

### 1.2 具名 profile 是**互相隔离的孤岛**

一个叫 `coder` 的 profile 用的是 `<默认 Hermes 根>/profiles/coder/`，
因此它有**自己的** `state.db`、配置、日志、以及其它 profile 作用域的状态。

规则：**任何代表某个 profile 去建库、读配置、起子进程的代码，都必须持有或显式传入那个
profile 的 `HERMES_HOME`**。一旦回落到默认根，就会把**别的 profile 的状态混进这次操作** ——
这是本节最重要的一条，也是踩过的坑。

### 1.3 引导（bootstrap）顺序，与"别用 `HOME` 选 profile"

CLI 引导在 import 其它 Hermes 代码**之前**先调 `_apply_profile_override()`。细节：

- 显式给了 `--profile` / `-p` → 解析该 profile，并把**解析出的目录**写进 `HERMES_HOME`。
- 没给显式选择器 → 若 `HERMES_HOME` 本身已是"某个 profile 的 home"，**保留它**；
  否则引导可以用默认根里的"当前活动 profile"选择。
- **`HOME` 只决定"没有任何覆盖、也没有 `HERMES_HOME` 时"的平台默认路径**。
  **改 `HOME` 不是选 profile 的安全做法** —— 尤其是子进程一旦丢掉 `HERMES_HOME`，
  即使另一个 profile 是活动的，它也会回落成默认 profile。所以**起子进程的地方必须显式传 `HERMES_HOME`**。

### 1.4 `display_hermes_home()` 只管显示

只用于**给人看的文本**：它把解析出的 home 尽量表示成相对于用户家目录的形式
（例如 `~/.hermes/profiles/coder`）。它**不提供**另一套解析规则 —— 别拿它当路径来源。

### 1.5 测试隔离保护（Test isolation guard）

测试**必须**用临时的 `HERMES_HOME`，或显式指定临时数据库路径。

**实盘保护（live-system guard）**：当一个进程处于"测试上下文"时，如果它试图在**真实的默认
Hermes 根**或**真实的具名 profile** 下打开生产 `state.db`，保护会**提前抛错**——
目的是不让 fixture 数据或 SQLite 副作用碰到活着的安装。

两个**逃生门（escape hatch）**，都只给测试用：

| 逃生门 | 形态 | 用途 |
|---|---|---|
| `HERMES_STATE_DB_GUARD_BYPASS=1` | 环境变量 | 给**真的需要**访问实盘库的子进程 |
| `@pytest.mark.live_system_guard_bypass` | 标记 | 同上的**进程内**版本 |

⚠️ **绝不要在正常的 Hermes 命令、开发 shell 或应用配置里设这两个**：它们关掉的是一个硬
`RuntimeError` 保护，被保护的是**活着的会话历史**；而且一旦某个 shell 导出了它，
**之后每一次 pytest 都会继承这个 bypass**。

### 1.6 桌面端的 profile 隔离，与"压缩世代（compaction generations）"

每个具名 profile 把转写本存在**自己的** `$HERMES_HOME/state.db` ——
**即使一个 `hermes serve` 进程同时在服务多个 profile**，这条也成立。由此有几条硬要求：

| 场景 | 要求 |
|---|---|
| **会话内重建 agent**（Bot Chat 能力刷新、`tools.configure`） | 必须**保留该会话的数据库句柄**，并在构造期间绑定好它的 profile home |
| **释放旧 agent** | **不能**顺手关掉"已经被新 agent 继承"的那个句柄 |
| **`tools.configure` 解析配置** | 从**活动会话的 `profile_home`** 解析 —— 即使客户端只给了 `session_id` |
| **重建的顺序** | 先把新 agent 的模型配置准备好 → 再分配替换品 → 再一次性"安装 agent + 移交所有权"；**准备失败**时，拆卸责任仍留在旧 agent 手上 |
| **解析不出的显式 profile / 目录已消失** | 在访问启动配置或历史**之前**就失败 |
| **过期的 `tools.configure` 会话 ID** | 返回 `session not found`，**且不改任何配置**；不过"省略 session id"的全局设置操作仍然可用 |

### 1.7 原地压缩为什么会有"看起来重复"的行

**原地压缩（in-place compaction）**：把旧行**归档**成 `active=0`，再把保留下来的上下文
**作为 `active=1` 的新行插入**。

于是：**一条受保护的消息完全可能同时出现在两个"世代"里**，内容与时间戳一模一样。

- ⚠️ **别把这些归档行当重复数据删掉**（这是原文明确点名的坑）。
- 排查"重复的**活**写入"时，看 `active=1`。
- 排查"历史像是被回退了"时，**除了 session id，还要检查数据库属于哪个 profile**。

---

## 2. Codex app-server 的输入归属（input ownership）

> 这一节和下一节原文是**写给维护者的变更说明体**，直接读会比较糊。先给白话三句：
>
> 1. 用户这句话**由 agent 先写进库**；
> 2. 但 Codex 后端会把同一句话**回显**成一条 `userMessage` 通知 —— 如果不管，库里就会出现重复；
> 3. 拼接时的规则是：**只剔除"与发给 `turn/start` 的那段文本完全一致的那条首项"**，
>    其它一律保留（后面才出现的、不一致的、以及**另一轮**里被单独接受的同样文本，都留）。

细节（逐条）：

- agent 在**启动 Codex 回合之前**，先把已接受的用户输入持久化。
- Codex 随后把这条输入**投影**成一条打头的 `userMessage` 通知。
- 在运行时的**拼接边界（splice boundary）**上，Hermes **只排除那条打头的项**，
  且条件是**它与序列化进 `turn/start` 的文本完全一致**（**包含富输入被强制转换后的形态**）。
- 更晚出现的、或不匹配的用户事件**保持原样**；另一轮里被单独接受的**相同**文本同样保留。
- 这条规则**也适用于合成输入 / 无 key 的输入**（不依赖平台消息 ID）。
- **已经存在的历史重复行不会被改写**。
- 当 agent 报告"这次持久化由我负责"时，**网关会跳过自己的转写本写入**。

---

## 3. 网关异常路径的输入归属

> 同样先白话：如果**在 agent 还没构造出来**、或者**输入已经落库了**的时候网关抛了异常，
> 那么"这条消息到底算谁写的"就成了问题。做法是：**网关在 `display_metadata` 这个随行字段里
> 打一个"归属标记"**，然后让它走 agent 的正常持久化路径；异常写入方**只认这个标记**，
> 认到了才补写。

细节：

- 异常可能发生在①**agent 构造之前**，或②**输入已经进 SQLite 之后**。
- 网关给已接受的输入打上**归属标记（owner marker）**，放在既有的 `display_metadata`
  **随行字段（sidecar）**里，并经 agent 的正常持久化路径带过去。
- **发给 provider 的消息里永远不含这段元数据**。
- 平台标记把"入站消息 ID"按**平台 / profile / 作用域 / 聊天 / 话题**做命名空间划分；
  原来的 `platform_message_id` **保持不变**（引用/回复解析还要用它）。
- **无 key 的回合也会拿到一个新的标记** —— 即使文本与时间戳都与别处相同。

**异常写入方（exception writer）**的查找规则：

- 它**只**探测那个标记；然后按已发布的**重路由**和**canonical 的活压缩后继**往下找，
  再找**压缩祖先（compression ancestors）**。
- **计入**：活行（active）与压缩归档行。
- **不计入**：被撤销（undone）的行、被观测到的输入（observed input）、**无关的写入方**。
- 因此：**另一个进程恰好也在写同一个会话，并不会把这一回合压掉**。
- 不需要"整段历史作基线"，也不需要为归档消息体分配内存。
- **归属读取失败 ≠ 可以投机追加**；普通的历史读取失败，仍然维持原来的
  "历史不可用"响应。

**边界（原文明确划线）**：

- 正常的、由 agent 负责的持久化**完全没变**。
- 这是**失败路径下的写入方仲裁**，**不是**通用的"恰好一次投递"、**不是**内容去重、
  **也不是**表结构迁移。
- 历史行**不会被改写**；**没有标记的历史输入**无法为一条重投事件确立归属。

---

## 4. 架构总览

```
~/.hermes/state.db (SQLite，WAL 模式)
├── sessions              —— 会话元数据、token 计数、计费
├── messages              —— 每个会话的完整消息历史
├── session_model_usage   —— 按"模型 / 任务"维度的用量归属行
├── messages_fts          —— FTS5 虚拟表（content + tool_name + tool_calls）
├── messages_fts_trigram  —— FTS5 虚拟表，trigram 分词器（CJK / 子串搜索）
├── messages_fts_cjk      —— FTS5 虚拟表，cjk_unicode61 分词器
├── state_meta            —— 键值元数据表
├── gateway_routing       —— 网关路由元数据
├── compression_locks     —— 跨进程压缩锁
├── async_delegations     —— 异步委派记账
├── delivery_obligations  —— 网关发件箱（欠下的回复）；由 gateway/delivery_ledger.py 按需创建
└── schema_version        —— 单行表，记录迁移状态
```

`hermes sessions recover` 会把上面这些**带行的表**复制进恢复出的库
（FTS 索引与 `schema_version` 会**重新生成**），包括"按需创建的 `delivery_obligations`"
（如果源库里存在）—— 它的行数会和 `sessions` / `messages` 一样被校验。

**五个关键设计决策**（原文列举）：

| 决策 | 为什么 |
|---|---|
| **WAL 模式** | 允许读写并行，每一时刻仍只有一个写者（网关多平台场景） |
| **FTS5 虚拟表** | 跨全部会话消息的快速文本搜索 |
| **会话血缘**（`parent_session_id` 链） | 上下文压缩触发的会话分裂 |
| **来源标记**（`cli` / `telegram` / `discord` …） | 按平台过滤 |
| **批量运行器与 RL 轨迹** | **不**存在这里（它们是独立的系统） |


---

## 5. SQLite 表结构

### 5.1 `sessions` 表

原文给的是**节选**：完整列清单看 `hermes_state_common.py` 里的 `SCHEMA_SQL`
（由 `hermes_state_schema.py` 执行）。原文点名"节选之外还有"的列包括：

- **网关路由元数据**：`session_key`、`chat_id`、`chat_type`、`thread_id`、
  `display_name`、`origin_json`、`expiry_finalized`
- **工作区字段**：`cwd` / `git_branch` / `git_repo_root`
- **交接与压缩失败字段**（handoff / compression-failure）
- `profile_name`、`rewind_count`、`archived`、`pinned`

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    -- ... 还有网关 / 工作区 / 交接 / 压缩相关的列 ...
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique
    ON sessions(title) WHERE title IS NOT NULL;
```

看这几行 DDL 能读出三件事：

1. 会话汇总计费信息在会话行上，按模型/任务的归属另存于 `session_model_usage`（`estimated_cost_usd` / `actual_cost_usd` / `pricing_version` …），
   也就是"价格表变了"这件事是靠 `pricing_version` 记录当时用的版本。
2. **`title` 是"非 NULL 之间唯一"**（`WHERE title IS NOT NULL` 的部分索引）——
   允许大量没有标题的会话，但一旦起了标题就不能重名。
3. **血缘就是自引用外键** `parent_session_id → sessions(id)`：压缩触发分裂时，
   新会话把老会话设成父亲（→ §10）。

### 5.2 `messages` 表

同样**节选**；完整表结构还有 `effect_disposition`、`platform_message_id`、`observed`、
`active`、`compacted`、`api_content`、`display_kind`、`display_metadata`：

```sql
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT
    -- ... 还有展示 / 压缩相关的列 ...
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, id);
```

原文给的**七条注意**（逐条译）：

1. **`tool_calls` 存成 JSON 字符串**（工具调用对象的列表被序列化）。
2. **`reasoning_details`、`codex_reasoning_items`、`codex_message_items` 也是 JSON 字符串。**
3. **桌面端历史补全**在 REST 与 JSON-RPC 两套投影里都保留 assistant 的随行数据
   （`session.resume`、`session.activate`、`session.history`），包括带 reasoning 与工具调用的行。
   **REST 可能返回 SQLite 的 JSON 字符串，而 RPC 返回解码后的对象；桌面端两者都接受。**
   有一种情况是**最后的 Responses 回复只存在于 `codex_message_items` 里，而 `content` 是空的**；
   此时**规范内容（canonical content）仍然优先**，analysis / commentary 类条目**不会被提升**成回复正文。
4. **`reasoning` 存的是原始推理文本**（对暴露它的 provider 而言）。
5. **"只有推理的干净结束"**（`content` 为空、`finish_reason=stop`、有 reasoning）——
   **回答时用那段推理文本**，但**绝不**把它写进 `content`：
   `content` 保持空，文本留在 `reasoning` / `reasoning_content`，
   由 `api_content` 带着它，好让下一次请求**逐字节**重放那个回答。
   所以**历史展示面会把它显示成"推理"，而不是"回复"**。
6. **`api_content` 是"字节保真"随行字段**：当"实际发给 API 的那个内容字符串"与 `content`
   不同时（临时记忆（ephemeral memory）/ 插件注入 / 持久化覆盖），就在这里存**线上字节**，
   为的是**前缀缓存稳定的重放**。存的是"原样发出的那份"，**唯一例外是孤立代理项
   （lone surrogate）**—— sqlite3 绑不上它，而对话循环本来也会把每个外发载荷里的它清掉。
   `NULL` 表示"`content` 就是原样发出去的那份"。
7. 时间戳是 Unix epoch 浮点数（`time.time()`）。

第 5、6 条值得多想一秒：**"你要我发的" 与 "我实际发出的" 可能是两个字符串**，
而缓存命中的判据是后者 —— 所以它必须单独存一份。

### 5.3 FTS5 全文索引

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    tool_name,
    tool_calls,
    content='messages',
    content_rowid='id'
);
```

FTS5 表**靠三个触发器**与 `messages` 表保持同步（INSERT / UPDATE / DELETE 各一个）。
现在的触发器**受 `state_meta` 里 `fts_rebuild_high_water` / `fts_rebuild_progress`
两个标记控制** —— 这样"后台重建 FTS"才能在不重复索引的前提下进行 —— 并且覆盖**全部三个**
被索引的列（确切 SQL 见 `hermes_state_common.py` 的 `SCHEMA_SQL`）。

⚠️ **上游文档出入**：本节与 §6 的版本表把 FTS 布局变更算进 `schema_version`，
但源码里 FTS 的**布局版本是独立计数**的（`fts_storage_version`）—— 详见附录 C 第 ③ 条。

---

## 6. schema 版本与迁移

原文写"当前 schema 版本：**23**"。⚠️ **这条过期了** —— 见附录 C 第 ① 条（源码是 30）。

`schema_version` 表只存**一个整数**。两类变更：

| 类型 | 谁处理 | 举例 |
|---|---|---|
| **纯加列（声明式）** | `_reconcile_columns()` —— 把活库的列与 `SCHEMA_SQL` 做 diff，缺哪列就 `ADD` 哪列 | 绝大多数版本号 |
| **数据迁移 / 索引与 FTS 变更（表达不了的）** | 版本门控链（version-gated chain） | 见下表 |

版本表（原文照译，**节选**；未列出的版本号都是"只加列"）：

| 版本 | 变更 |
|---|---|
| 1 | 初始表结构（sessions、messages、FTS5） |
| 2 | messages 加 `finish_reason` |
| 3 | sessions 加 `title` |
| 4 | `title` 加唯一索引（NULL 允许；非 NULL 必须唯一） |
| 5 | 加计费列：`cache_read_tokens`、`cache_write_tokens`、`reasoning_tokens`、`billing_provider`、`billing_base_url`、`billing_mode`、`estimated_cost_usd`、`actual_cost_usd`、`cost_status`、`cost_source`、`pricing_version` |
| 6 | messages 加推理列：`reasoning`、`reasoning_details`、`codex_reasoning_items` |
| 7 | messages 加 `reasoning_content` |
| 8 | sessions 加 `api_call_count` |
| 9 | messages 加 `codex_message_items`（为了 Codex Responses 的消息 id / phase 重放） |
| 10 | 加 `messages_fts_trigram` 虚拟表（trigram 分词器，供 CJK / 子串搜索）并回填既有行 |
| 11 | 重建 `messages_fts` 与 `messages_fts_trigram` 的索引以覆盖 `tool_name` + `tool_calls`，并从"外部内容模式"改为"内联模式"；删旧触发器、回填每一行 |
| 16 | 给 delegate 子代理的行在 `model_config` 里打标（`$._delegate_from`），好让父会话被删后遗留的孤儿不出现在会话选择器里 |
| 18 | 网关元数据合并 —— 从 `sessions.json` 回填 `display_name` / `origin_json` / `expiry_finalized` |
| 20 | 按模型的用量归属 —— 用历史的"按会话汇总值"播种 `session_model_usage` 行 |
| 22 | 按任务的用量归属 —— 重建 `session_model_usage`，让 `task` 列参与主键 |
| 23 | FTS 存储重新设计 —— 用外部内容 FTS 表替代 v11 的内联模式副本（对既有库是**选择性加入**的迁移） |
| 29 | Cron 会话退出 trigram（子串/CJK）索引：`messages_fts_trigram_src` 视图 + 触发器按 `sessions.source` 过滤，一次性重建会清掉历史行 |
| 30 | 委派子会话（subagent）也退出 trigram 索引 —— 判据是 `source='subagent'` 或 `$._delegate_from` 标记（`FTS_TRIGRAM_SESSION_SQL`）。行**仍在** `messages` 与标准 `messages_fts` 词索引里，所以 `session_search` 照样搜得到；缩小的只是**约 2.6 倍**的 trigram 影子表。与 v29 共用同一次重建 |

**声明式加列**的做法：`ALTER TABLE ADD COLUMN` 包在 `try/except` 里以处理"列已存在"的情形
（幂等）；**每个迁移块成功之后**才推进版本号。

读这张表能看出上游的**取舍倾向**：能用"加一列 + 幂等"解决的就不写迁移；
只有**动了数据或索引**（回填、重建、改主键）才肯写一段有版本号的迁移。

---

## 7. 写入争用（Write Contention）怎么处理

背景：**多个 Hermes 进程共用一个 `state.db`**（网关 + 各 CLI 会话 + worktree 里的 agent）。
`SessionDB` 有四招：

| 手段 | 具体值 | 为什么 |
|---|---|---|
| **短 SQLite 超时** | **1 秒**（原文对比 30s；Python `sqlite3.connect` 的默认值实际为 5s） | 长等会把"谁先拿到锁"拖成秒级 |
| **应用层重试 + 随机抖动** | 20–150ms，最多 15 次 | 见下面的"车队效应" |
| **`BEGIN IMMEDIATE` 事务** | 在事务**开始**时就显式取写锁 | 让争用在事务起点暴露，而不是在中间某次写时炸 |
| **周期性 WAL checkpoint** | 每 **50** 次成功写入一次（`PASSIVE`） | 防止 WAL 无限长大 |

**要解决的问题叫"车队效应（convoy effect）"**：SQLite 内部的退避是**确定性**的，
于是所有竞争的写者会在**同一个间隔**上一起重试、一起失败、一起再重试 ——
加**随机抖动**就是把这个同步打散。

原文给的常量块：

```
_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.020   # 20ms
_WRITE_RETRY_MAX_S = 0.150   # 150ms
_CHECKPOINT_EVERY_N_WRITES = 50
```

⚠️ **上游文档出入**：源码里 `_WRITE_MAX_RETRIES` **这个名字不存在**，而且抖动是**两档**的
—— 见附录 C 第 ② 条。

---

## 8. 常用操作

### 初始化

```python
from pathlib import Path
from hermes_state import SessionDB

db = SessionDB()                              # 默认：~/.hermes/state.db
db.close()
db = SessionDB(db_path=Path("/tmp/test.db"))  # 或用自定义路径；用完也应关闭
```

### 建 / 管会话

```python
# 新建会话
db.create_session(
    session_id="sess_abc123",
    source="cli",
    model="anthropic/claude-sonnet-4.6",
    user_id="user_1",
    parent_session_id=None,  # 或上一个会话 ID（血缘）
)

# 结束会话
db.end_session("sess_abc123", end_reason="user_exit")

# 重开会话（清掉 ended_at / end_reason）
db.reopen_session("sess_abc123")
```

### 存消息

```python
msg_id = db.append_message(
    session_id="sess_abc123",
    role="assistant",
    content="Here's the answer...",
    tool_calls=[{"id": "call_1", "function": {"name": "terminal", "arguments": "{}"}}],
    token_count=150,
    finish_reason="stop",
    reasoning="Let me think about this...",
)
```

### 取消息

```python
# 原始消息（带全部元数据）
messages = db.get_messages("sess_abc123")

# OpenAI 会话格式（给 API 重放用）
conversation = db.get_messages_as_conversation("sess_abc123")
# 返回：[{"role": "user", "content": "..."}, {"role": "assistant", ...}]
```

**这两个方法的区别值得记**：前者是"库里的样子"（带 id、时间戳、token_count、reasoning…），
后者是"要发给模型的样子"（角色、内容及工具调用等重放字段）。**别混用**。

### 会话标题

```python
# 设标题（非 NULL 标题之间必须唯一）
db.set_session_title("sess_abc123", "Fix Docker Build")

# 按标题解析（返回血缘里最新的那个）
session_id = db.resolve_session_by_title("Fix Docker Build")

# 自动生成血缘里的下一个标题
next_title = db.get_next_title_in_lineage("Fix Docker Build")
# 返回："Fix Docker Build #2"
```

---

## 9. 全文搜索

`search_messages()` 支持 FTS5 查询语法，并会**自动清洗**用户输入。

### 基本用法

```python
results = db.search_messages("docker deployment")
```

### FTS5 查询语法

| 语法 | 例子 | 含义 |
|---|---|---|
| 关键词 | `docker deployment` | 两个词都要（隐含 AND） |
| 引号短语 | `"exact phrase"` | 精确短语 |
| 布尔 OR | `docker OR kubernetes` | 任一 |
| 布尔 NOT | `python NOT java` | 排除 |
| 前缀 | `deploy*` | 前缀匹配 |

### 带过滤的搜索

```python
# 只搜 CLI 会话
results = db.search_messages("error", source_filter=["cli"])

# 排除网关会话
results = db.search_messages("bug", exclude_sources=["telegram", "discord"])

# 只搜用户消息
results = db.search_messages("help", role_filter=["user"])
```

### 结果格式

每条结果包含：

- `id`、`session_id`、`role`、`timestamp`
- **`snippet`** —— FTS5 生成的片段，命中处用 `>>>match<<<` 标出
- **`context`** —— 命中前后各一条消息（内容截断到 200 字符）
- `source`、`model`、`session_started` —— 来自父会话

### 输入清洗

`_sanitize_fts5_query()` 处理这些边角：

- 剥掉**不配对的引号**与特殊字符；
- 把**带连字符的词**用引号包起来（`chat-send` → `"chat-send"`）；
- 去掉**悬空的布尔操作符**（`hello AND` → `hello`）。

---

## 10. 会话血缘（Session Lineage）

会话之间可以靠 `parent_session_id` 串成链。**什么时候会产生链**：
旧流程或显式轮转可产生父子会话；当前默认压缩原地归档消息，并不必然分裂会话。

### 查：找血缘

```sql
-- 找某个会话的全部祖先
WITH RECURSIVE lineage AS (
    SELECT * FROM sessions WHERE id = ?
    UNION ALL
    SELECT s.* FROM sessions s
    JOIN lineage l ON s.id = l.parent_session_id
)
SELECT id, title, started_at, parent_session_id FROM lineage;

-- 找某个会话的全部后代
WITH RECURSIVE descendants AS (
    SELECT * FROM sessions WHERE id = ?
    UNION ALL
    SELECT s.* FROM sessions s
    JOIN descendants d ON s.parent_session_id = d.id
)
SELECT id, title, started_at FROM descendants;
```

### 查：最近会话 + 预览

```sql
SELECT s.*,
    COALESCE(
        (SELECT SUBSTR(m.content, 1, 63)
         FROM messages m
         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
         ORDER BY m.timestamp, m.id LIMIT 1),
        ''
    ) AS preview,
    COALESCE(
        (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
        s.started_at
    ) AS last_active
FROM sessions s
ORDER BY s.started_at DESC
LIMIT 20;
```

**预览取的是"这个会话的第一条用户消息前 63 个字符"**；如果没有消息，退回 `started_at`
当"最后活动时间"。

### 查：token 用量统计

```sql
-- 按模型汇总 token
SELECT model,
       COUNT(*) as session_count,
       SUM(input_tokens) as total_input,
       SUM(output_tokens) as total_output,
       SUM(estimated_cost_usd) as total_cost
FROM sessions
WHERE model IS NOT NULL
GROUP BY model
ORDER BY total_cost DESC;

-- token 用量最高的会话
SELECT id, title, model, input_tokens + output_tokens AS total_tokens,
       estimated_cost_usd
FROM sessions
ORDER BY total_tokens DESC
LIMIT 10;
```

---

## 11. 导出与清理

```python
# 导出单个会话（含消息）
data = db.export_session("sess_abc123")

# 导出全部会话（含消息）为 dict 列表
all_data = db.export_all(source="cli")

# 删旧会话（只删已结束的）
deleted_count = db.prune_sessions(older_than_days=90)
deleted_count = db.prune_sessions(older_than_days=30, source="telegram")

# 清消息但保留会话记录
db.clear_messages("sess_abc123")

# 删会话及其全部消息
db.delete_session("sess_abc123")
```

注意 `prune_sessions` 只动**已结束**的会话 —— 活着的对话删不掉。

---

## 12. 数据库位置

默认路径：`get_hermes_home() / "state.db"`

| 场景 | 路径 |
|---|---|
| 默认 profile | `~/.hermes/state.db` |
| 具名 profile | `~/.hermes/profiles/<name>/state.db` |
| 其它 | `HERMES_HOME` 指到哪就是哪（→ §1） |

**数据库文件、WAL 文件（`state.db-wal`）、共享内存文件（`state.db-shm`）都在同一个目录里。**
（这也是一条运维常识：备份时三个文件要一起考虑，或者用 SQLite 的备份接口。）

---

## 附录 A · 术语表（本章新增）

| 原文 | 译法 | 说明 |
|---|---|---|
| session storage | 会话存储 | 本章主题：消息与元数据落到 SQLite |
| profile | profile（配置档案） | 保留英文；**一个 profile = 一套 home + 密钥作用域 + 终端作用域** |
| facade / sibling | 门面 / 兄弟模块 | `hermes_state.py` 是门面，`hermes_state_*.py` 是兄弟模块（按主题切分） |
| schema | 表结构（schema） | 本章指 SQLite 的表 / 索引 / 虚拟表定义 |
| migration | 迁移 | 分两类：声明式加列、版本门控的数据/索引变更 |
| write contention | 写入争用 | 多进程抢同一个 SQLite 写锁 |
| convoy effect | 车队效应 | 确定性退避让所有写者同步重试的恶性同步 |
| jitter | 抖动 | 随机化的重试间隔 |
| WAL | WAL（写前日志） | SQLite 的一种日志模式：多读者 + 一写者 |
| FTS5 | FTS5 | SQLite 内置的全文检索扩展 |
| tokenizer | 分词器 | FTS5 决定"怎么切词"的组件（trigram / cjk_unicode61） |
| lineage | 血缘（lineage） | `parent_session_id` 链，压缩分裂时产生 |
| input ownership | 输入归属 | "这条消息算谁写进库的"（防重复写） |
| sidecar | 随行字段（sidecar） | 挂在主字段旁边的附加数据（如 `display_metadata`、`api_content`） |
| in-place compaction | 原地压缩 | 行级归档：旧行 `active=0`、保留内容 `active=1` **重新插入** |
| compaction generation | 压缩世代 | 同一会话内容可能同时存在于多个世代（归档 vs 活行） |
| escape hatch | 逃生门 | 专门用来关掉某项保护的开关（本章是测试专用） |
| live-system guard | 实盘保护 | 防止测试碰到生产 `state.db` 的硬保护 |
| byte-fidelity | 字节保真 | 存的是**实际发出去的字节**，用于缓存稳定重放 |
| lone surrogate | 孤立代理项 | 未与另一代理码位配对的 Unicode 代理码位，不能按普通 UTF-8 编码交给 sqlite3 |

---

## 附录 B · 这一章在整套文档里的位置

```
第 2 章  回合循环        —— 消息在内存里怎么流动
第 3 章  提示词装配      —— 每一轮发给模型的那包怎么拼
第 4 章  工具运行时      —— 工具怎么注册、怎么被选中、结果什么形状
第 5 章  上下文压缩与缓存 —— 窗口满了怎么办、缓存怎么不被打断
第 6 章  会话存储 ★本章  —— 上面这些**最后落在哪**（SQLite）
第 7 章  （下一章）      —— 网关 / 平台适配（消息从外部怎么进来）
```

**本章与第 5 章的接口**（两条，后面实现时都会碰到）：

1. **压缩会改库里的行**：原地压缩写 `active=0` 的归档行 + `active=1` 的活行；
   网关侧的会话维护还可以**轮转会话 id** → 于是有了 `parent_session_id` 血缘。
2. **压缩需要跨进程锁**：`compression_locks` 表就在这个库里 ——
   网关进程与 CLI 进程可能同时对同一个会话动压缩，锁必须落在**共享的存储**上，
   而不能只放在各自的内存里。

**谁会读这个库**（本章之后会反复遇到）：

| 读者 | 读什么 |
|---|---|
| agent 的回合循环 | 启动时把历史读出来（`get_messages_as_conversation`），每追加一条写一次 |
| `session_search` 工具 | FTS5 索引（跨会话搜索） |
| 网关 | `gateway_routing`、转写本、`delivery_obligations`（欠下的回复） |
| 桌面端 / TUI | `session.resume` / `session.activate` / `session.history` 投影 |
| 计费 | 会话行上的 token 计数与成本字段 |

---

## 附录 C · 上游文档出入（3 处，已对源码核实）

核对的锚：上游 commit **`f5d1926110`**（v0.21.3）。

### ① 版本号：文档标题写 23，它自己的表列出 30，源码是 30

- 文档 §Schema Version and Migrations 开头：`Current schema version: **23**`
- 文档自己的版本表最低到 **v30**（v29 / v30 是 trigram 索引的两次收窄）
- 源码：`hermes_state_common.py:239` → `SCHEMA_VERSION = 30`

**结论**：**标题那行过期了**（版本表才是新的）。这个数被别处引用过就会误导
（例如"某个迁移有没有跑过"的判断）。

### ② 写入重试常量：`_WRITE_MAX_RETRIES` 这个名字在源码里不存在，且抖动是两档的

文档给的常量块（§Write Contention Handling）：

```
_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.020   # 20ms
_WRITE_RETRY_MAX_S = 0.150   # 150ms
_CHECKPOINT_EVERY_N_WRITES = 50
```

源码 `hermes_state.py:476-480` 实际是：

```python
_WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S = 0.020, 0.150  # fast jitter for the first _SLOW_AFTER_S
_WRITE_RETRY_SLOW_AFTER_S = 2.0
_WRITE_RETRY_SLOW_MIN_S, _WRITE_RETRY_SLOW_MAX_S = 0.250, 1.000
...
_CHECKPOINT_EVERY_N_WRITES = 50
```

- **`_WRITE_MAX_RETRIES` 搜不到**（约束更像"按截止时间/耐心值"，见 `:1350` 附近）。
- **重试抖动分两档**：前 2 秒用 20–150ms 的快档，之后换 **250–1000ms 的慢档** ——
  文档只写了快档，读者会以为重试一直是密集的。

### ③ FTS 布局版本**独立计数**，不在 `schema_version` 里

文档把 FTS 的布局变更写成 `schema_version` 的一个个版本（v10 / v11 / v23 / v29 / v30）。
源码里另有一个**独立**计数器，注释原话（`hermes_state_common.py:247-254`）：

> `# FTS storage-layout version, tracked INDEPENDENTLY of SCHEMA_VERSION in the`
> `# state_meta key ``fts_storage_version``. The main schema version advances`
> `# freely on open (so future migrations always land); the FTS *layout* only`
> `# reaches the current version when a DB is either born fresh or explicitly`
> `# optimized via ``hermes sessions optimize-storage``. A legacy DB sits at`
> `# layout 0 (marker absent) with a working inline index until the user opts in.`

```python
FTS_STORAGE_VERSION = 2      # 1 = v23 外部内容布局 + trigram 排除工具行
                             # 2 = trigram 还排除结构化的 tool_calls JSON
```

**差别很实际**：**主 schema 版本号在打开库时就会自由推进**（保证以后的迁移总能落地），
而 **FTS 布局版本只有"库是新建的"或"用户显式跑了 `hermes sessions optimize-storage`"
才会到当前值** —— 老库会**停在 layout 0**（标记不存在）并继续用能工作的内联索引，
直到用户选择加入。所以"schema_version = 30"**并不代表**FTS 布局也是最新的。

---

**读到这里你应该能回答**：① 为什么路径不能硬编码；② `compaction` 与 `compression` 的区别；
③ 多个进程同时写是怎么不打架的；④ 为什么会在库里看到"内容一样的重复行"；⑤ 搜历史走哪条路径。
