# 中文精译 · 第 8 章：网关会话生命周期（Gateway Session Lifecycle）

> **原文**：`website/docs/developer-guide/gateway-session-lifecycle.md`（621 行，官方 Developer Guide **第 10 位**，
> 紧跟第 7 章的 `gateway-internals`）。
> **本篇定位**：翻译为主，目标是**看懂原理**。差异对比、源码深挖、实现日志都不在这里 ——
> 那些属于后面两个阶段（mini 实现 / 源码讲解）。
> 看不懂的地方或想深挖的点，随时问，我再往这里补。
>
> 📌 行号锚在上游 commit **`f5d1926110`**（`version = "0.21.3"`）；上游每合并一个 PR 行号就会漂。
> 译文里凡是**与源码不符**的地方，用 `⚠️ 上游文档出入` 标出来（本章已发现 **2 处**，见附录 C）。

[TOC]

---

## 0. 这一章在回答什么

第 7 章讲的是"消息怎么进来、谁说了算、怎么活着"。但有一件事第 7 章只点到没说透：

> **两轮之间，这个会话是怎么活下来的？**

这一章就是那件事的完整答案。它把"会话"当成一个有**生命周期**的东西来管：

```text
        一条消息进来
             │
             ▼
   ┌───────────────────────┐
   │ get_or_create_session │ ← 这一章的主角：它决定"用哪个会话"
   │  （决定用哪个 session_id）│
   └───────────┬───────────┘
               │
   ┌───────────┴───────────┬────────────────────┬──────────────────┐
   ▼                       ▼                    ▼                  ▼
 正常复用              显式边界            软恢复/硬擦除        空闲清理
（同一份转写本）      `/new` `/reset`      resume_pending /    （只清资源，
                     → 新 session_id      suspended           **不结束对话**）
```

**它要回答的四个问题**（这一章的五脏）：

| # | 问题 | 对应节 |
|---|---|---|
| 1 | 一条消息从哪来？这个"来源"里有哪些字段，谁在读它们？ | §1 `SessionSource` |
| 2 | "当前会话"这个状态存在哪？有哪些标志位，它们的**优先级**是什么？ | §2 `SessionEntry` + §3 `SessionStore` |
| 3 | 会话键怎么算？群聊里到底是"共享一个会话"还是"每人一个"？ | §4 会话键 + §5 多用户隔离 |
| 4 | 网关崩了/重启了，正在进行中的对话会怎样？ | §7 重启恢复 + §10 后台维护 |

另外还有三件围着它们转的事：**消息排队**（§8）、**会话上下文注入**（§9，含 PII 脱敏）、
**Agent 缓存**（§11 —— 这节解释了"为什么网关要缓存 agent"，也解释了它带来的内存风险）。

**为什么这一章重要（和第 5/6/7 章的关系）**：

| 章 | 那里留下了什么 | 这一章怎么接 |
|---|---|---|
| 第 5 章 | 压缩（compression）的四阶段；"网关侧会话维护"只提了接口 | §6 明确：**不活动和墙钟时间永远不轮转会话**；压缩继续负责长历史 |
| 第 6 章 | `state.db` 是权威转写本；`sessions`/`messages` 表 | §3 的存储布局说明它和 `sessions.json` 的关系（⚠️ 见附录 C ①） |
| 第 7 章 | 两级守卫、忙时策略、交付；挂着"投递债/会话清理"两笔债 | §8 排队（守卫三件套的完整形状）、§10 后台维护（清理但不结束对话） |

---

## 1. SessionSource —— 消息来源描述符

`SessionSource` 是一条"**这条消息从哪来**"的冻结记录（frozen record）。它挂在每个入站
`MessageEvent` 上，被三件事读取：**路由**（归哪个会话）、**隔离**（谁能看见谁）、
**上下文注入**（写进系统提示词）。

### 字段

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `platform` | `Platform` | *必填* | 平台枚举（telegram、discord、slack、signal、whatsapp、matrix、local 等） |
| `chat_id` | `str` | *必填* | 平台层的聊天/群/频道标识。会先过适配器的 `chat_id_key` 变换 |
| `chat_name` | `Optional[str]` | `None` | 可读的聊天/群名字 |
| `chat_type` | `str` | `"dm"` | `"dm"` / `"group"` / `"channel"` / `"thread"` 之一。**它决定会话键怎么算、怎么隔离** |
| `user_id` | `Optional[str]` | `None` | 平台内的用户标识。用于**授权**与"每人一个会话"的隔离 |
| `user_name` | `Optional[str]` | `None` | 发言人的显示名。会被注入系统提示词 |
| `thread_id` | `Optional[str]` | `None` | 论坛话题 / Discord 线程 / Slack thread 的标识。用来区分同一聊天里的不同线程 |
| `chat_topic` | `Optional[str]` | `None` | 频道主题/描述（Discord channel topic、Slack channel purpose） |
| `user_id_alt` | `Optional[str]` | `None` | 平台侧**稳定**的备用 id（Signal UUID、飞书 union_id）。**当 `user_id` 是易变值时用它** |
| `chat_id_alt` | `Optional[str]` | `None` | Signal 群的内部 id —— 把 Signal 群 V2 标识映射到规范形态 |
| `is_bot` | `bool` | `False` | 发言人是 bot 或 webhook（Discord bots）时为真 |
| `guild_id` | `Optional[str]` | `None` | Discord guild / Slack workspace / Matrix server 的作用域标识 |
| `parent_chat_id` | `Optional[str]` | `None` | 当 `chat_id` 指的是一个线程时，它所属的父频道 |
| `message_id` | `Optional[str]` | `None` | 触发这条消息的原始 id。用于置顶/回复/加表情，以及 Discord 的 id 注入 |
| `role_authorized` | `bool` | `False` | 适配器**按平台角色**（而不是按具体用户 id）授权时为真 |

📌 **读这张表的方法**：不必背。要看的是它体现的三条设计取向：

1. **一个来源要装下所有平台的怪癖**（Signal 的 UUID、飞书的 union_id、Discord 的 guild）；
2. **"易变的 id"和"稳定的 id"分开存**（`user_id` vs `user_id_alt`）——
   因为会话键要靠 id 稳定，而平台有时会换 id；
3. **"谁授权了它"也要记**（`role_authorized`）—— 授权来源不同，后面的策略可能不同。

### 关键方法

- **`description`**（属性，`str`）：给人看的摘要，例如
  `"DM with Alice"`、`"group: My Group, thread: 12345"`；
- **`to_dict()` / `from_dict()`**：序列化往返 —— 用于把来源持久化进 `sessions.json`
  （它是 **SQLite 的镜像**，不是权威；见 §3 的层次表与附录 C ①）。

---

## 2. SessionEntry —— 活动会话记录

`SessionEntry` 是**每个会话一条**的元数据记录：内存里有一份，并持久化到
`{sessions_dir}/sessions.json`（**SQLite 的镜像**，由 `write_sessions_json` 控制，默认开）。每条记录做的事情只有一件：
把 `session_key` 映射到"它**当前**的 `session_id`"。

> ★ 先把两个词区分开，后面就不绕了：
> **会话键（session_key）** = "哪条车道"（一个聊天/一个线程），长期稳定；
> **会话 id（session_id）** = "这条车道上的第几趟车"（一次具体的对话实例），会被重置换新。

### 字段

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `session_key` | `str` | *必填* | 决定性的键，标识"这条对话车道"（见 §4） |
| `session_id` | `str` | *必填* | 这次对话实例的唯一 id。格式 `YYYYMMDD_HHMMSS_<hex>`（⚠️ 见附录 C ②） |
| `created_at` | `datetime` | *必填* | 这个实例是什么时候建的 |
| `updated_at` | `datetime` | *必填* | 最后活动时间 —— **资源清理靠它** |
| `origin` | `Optional[SessionSource]` | `None` | 创建这个会话的来源。**投递时按它找路** |
| `display_name` | `Optional[str]` | `None` | 聊天显示名（来自 `SessionSource.chat_name`） |
| `platform` | `Optional[Platform]` | `None` | 持久化的平台枚举 —— **重启之后还要能路由** |
| `chat_type` | `str` | `"dm"` | 也持久化，因为策略查询要看它 |
| `input_tokens` | `int` | `0` | 累计输入（prompt）token |
| `output_tokens` | `int` | `0` | 累计输出（completion）token |
| `cache_read_tokens` | `int` | `0` | 累计提示词缓存**读** token |
| `cache_write_tokens` | `int` | `0` | 累计提示词缓存**写** token |
| `total_tokens` | `int` | `0` | 全部轮次合计 |
| `estimated_cost_usd` | `float` | `0.0` | 估算的累计美元成本 |
| `cost_status` | `str` | `"unknown"` | 成本跟踪的状态标签 |
| `last_prompt_tokens` | `int` | `0` | 最后一次 API 报的 prompt token 数 —— **压缩前的预检用它**（比估算准） |

📌 注意最后那行：`last_prompt_tokens` 是"**真实锚点**"（第 5 章讲过 usage 锚点的价值）——
它让"这一轮要不要压"这个判断基于**上一次 API 的真实数字**，而不是字符估算。

### 布尔标志（一台简单的状态机）

这些标志的作用是：**回答"下一次访问这个会话时该怎么办"**。

| 标志 | 默认 | 说明 |
|---|---|---|
| `was_auto_reset` | `False` | 显式挂起导致替换会话时置位。也保留在历史记录里 |
| `auto_reset_reason` | `None` | 显式挂起时是 `"suspended"`；更老的记录可能留着历史上的重置原因 |
| `reset_had_activity` | `False` | 被替换掉的那个会话**之前有没有活动** |
| `is_fresh_reset` | `False` | 由显式 `/new` 或 `/reset` 置位。**第一条消息时要重新注入话题/频道技能**。它和 `was_auto_reset` 分开，是为了不发出误导性的"会话已过期"提示 |
| `expiry_finalized` | `False` | 历史遗留的"终结栅栏"，只为恢复而保留；**没有任何定时器会写它** |
| `suspended` | `False` | **硬擦除**信号。由 `/stop` 或卡死循环升级（连续重启失败 ≥3 次）置位。下次 `get_or_create_session()` **无条件换新 `session_id`**，不管 `resume_pending` 是什么 |
| `resume_pending` | `False` | **软恢复**标记。由 `suspend_recently_active()`（崩溃恢复）或 drain 超时置位。下次访问**保留原 `session_id`** —— 用户接着同一份转写本继续。下一轮成功完成后才清除 |
| `resume_reason` | `None` | 为什么标记要恢复：`"restart_timeout"`、`"shutdown_timeout"`、`"restart_interrupted"` |
| `last_resume_marked_at` | `None` | 最近一次标记的时间戳 |

**一句话记住硬/软的区别**：

| | `suspended`（硬） | `resume_pending`（软） |
|---|---|---|
| 触发 | `/stop`、卡死循环 | 崩溃恢复、drain 超时 |
| 下次访问 | **换新会话**（旧对话丢掉） | **接着旧对话**（转写本不动） |
| 语义 | "这个会话坏了/别要了" | "它只是被打断了" |

### 状态转移逻辑（`get_or_create_session`）

```text
                    ┌──────────┐
                    │  消息进来  │
                    └────┬─────┘
                         ▼
              ┌──────────────────────┐
              │  session_key 存在？    │──── 否 ──► 新建一条 SessionEntry
              │  且 !force_new        │
              └──────────┬───────────┘
                         │ 是
                         ▼
              ┌──────────────────────┐
              │ entry.suspended?     │──── 是 ──► 自动重置：换新 session_id
              └──────────┬───────────┘            （reason="suspended"）
                         │ 否
                         ▼
              ┌──────────────────────┐
              │ entry.resume_pending?│──── 是 ──► 返回原记录（保留 session_id）
              └──────────┬───────────┘            下一轮成功后清标志
                         │ 否
                         ▼
              ┌──────────────────────┐
              │  策略说要重置吗？      │──── 是 ──► 自动重置：换新 session_id
              └──────────┬───────────┘            （reason="idle"/"daily"）
                         │ 否
                         ▼
              ┌──────────────────────┐
              │ 返回原记录，           │
              │ 刷新 updated_at        │
              └──────────────────────┘
```

**`get_or_create_session()` 里的优先级顺序**（这个顺序就是契约）：

1. `suspended=True` → **永远强制重置**（硬擦除）；
2. `resume_pending=True` → **保留 session_id**（软恢复）；
3. 没有触发条件 → 返回原记录（刷新 `updated_at`）。

📌 **顺序为什么不能换**：第 1 条必须在第 2 条之前 —— 否则"既被 `/stop` 过、又被标记要恢复"
的会话会被复活，而用户的意图明明是**把这条对话扔掉**。

📌 另外注意转移图里那格 **"策略说要重置吗？（reason=`idle`/`daily`）"** ——
它是**历史遗留路径**。第 6 节会明确说：现在**不活动和墙钟时间永远不轮转会话**。
读到这里先记住有这个格子，到 §6 再回来看它为什么是死的。

---

## 3. SessionStore —— 存储与操作

`SessionStore` 是主存储层：一个内存字典（`_entries`）+ 一份持久化，外加 SQLite
（`SessionDB`）作为**会话元数据与消息转写本的权威存储**。

### 构造函数

```python
SessionStore(sessions_dir: Path, config: GatewayConfig, has_active_processes_fn=None)
```

- `sessions_dir` —— `sessions.json` 所在的目录；
- `config` —— `GatewayConfig` 实例（路由与维护设置）；
- `has_active_processes_fn` —— 可选回调，按 `session_key` 检查"有没有还在跑的后台进程"。
  **有活动进程的会话，它的路由条目不会被剪枝。**

### 操作（方法）

| 方法 | 干什么 |
|---|---|
| `get_or_create_session(source, force_new=False)` | **核心入口**。返回已有或新建 `SessionEntry`；评估显式挂起与重启恢复状态；建/结束 SQLite 记录 |
| `update_session(session_key, last_prompt_tokens=None)` | 一次交互之后的轻量更新：刷新 `updated_at`，可选记下 `last_prompt_tokens` |
| `reset_session(session_key, display_name=None)` | **显式重置**（来自 `/new` 或 `/reset`）：换新 `session_id`、置 `is_fresh_reset=True`；结束旧 SQLite 会话、建新的 |
| `switch_session(session_key, target_session_id)` | 切到另一个已存在的会话 id（来自 `/resume`）：结束当前 SQLite 会话、重开目标 |
| `suspend_session(session_key)` | 标记 `suspended=True`（来自 `/stop`）→ 下次访问强制重置 |
| `mark_resume_pending(session_key, reason)` | 标记 `resume_pending=True`（来自 drain 超时）→ 下次访问保留 session_id。**不会覆盖 `suspended=True`** |
| `clear_resume_pending(session_key)` | 恢复成功的一轮之后清掉 `resume_pending`。由网关在 `run_conversation()` 返回后调用 |
| `suspend_recently_active(max_age_seconds=120)` | **崩溃恢复**：把最近活跃的会话标成 `resume_pending=True`。跳过已经是 pending / 已经 suspended 的记录。在**非正常退出**后的启动时调用 |
| `prune_old_entries(max_age_days)` | 丢掉超过 N 天（按 `updated_at`）的条目。**跳过 `suspended` 的、以及有活动进程的会话** |
| `list_sessions(active_minutes=None)` | 列出所有会话，可按"最近活动"过滤，按 `updated_at` 倒序 |
| `lookup_by_session_id(session_id)` | 按持久化的 session id 反查当前 `SessionEntry` |
| `has_any_sessions()` | 历史上有没有建过会话（**看 SQLite 的历史**，不只是内存字典） |
| `append_to_transcript(session_id, message, skip_db=False)` | 往 SQLite 转写本追加一条。`skip_db=True` 用于避免重复写（agent 自己已经落过盘时） |
| `rewrite_transcript(session_id, messages)` | **整体替换**转写本（`/retry`、`/undo`、`/compress` 用） |
| `load_transcript(session_id)` | 从 SQLite 读回一个会话的全部消息 |
| `rewind_session(session_id, n=1)` | 回退 `n` 个用户轮次（**软删除，保留审计痕迹**）。它只是 `SessionDB.rewind_user_turn`（`hermes_state_rewind.py`）的薄包装 —— 那份回退逻辑是 **CLI 的 `/undo` `/retry`、TUI 与网关共用的同一份**。返回 `{rewound_count, turns_undone, target_text}` |

📌 **方法表里最值得记的两条**：

1. **`mark_resume_pending` 不会覆盖 `suspended`** —— 硬擦除永远赢（和 §2 的优先级同一条规则，
   在两处都写了）；
2. **`rewind_session` 是"薄包装"** —— 回退这件事只有一份实现，三个界面（CLI/TUI/网关）共用。
   这是"一份实现、多处调用"的好例子：行为不会因为你在哪个界面按 `/undo` 而不同。

### 内部助手

- `_ensure_loaded()` / `_ensure_loaded_locked()` —— 把 `sessions.json` 读进 `_entries`；
- `_save()` —— **原子写** `sessions.json`（临时文件 + `atomic_replace`，避免写一半崩掉）；
- `_generate_session_key(source)` —— 委托给 `build_session_key()`，带上配置参数。

### 存储布局

```text
{sessions_dir}/
  sessions.json          # 内存里 _entries 的持久化，JSON
                           # 映射 session_key → SessionEntry（只有元数据）
  {session_id}.jsonl     # （遗留，spec 002 已移除）
```

**权威的转写本在 SQLite**（`hermes_state` 的 `SessionDB`）。
`sessions.json` 持久化的是 `session_key → session_id` 的映射与条目元数据（标志位、时间戳、
token 计数）。**如果 SQLite 不可用**，存储会退回到 JSONL —— 但那是**降级路径**。

📌 这一节有一个**必须记住的层次关系**：

| 存什么 | 存在哪 | 权威吗 |
|---|---|---|
| 消息转写本（真正的对话内容） | SQLite `state.db` | ✅ 权威 |
| `session_key → session_id` 映射 + 会话元数据/标志位 | `sessions.json`（JSON） | 网关自己的路由表 |
| （降级）转写本 | JSONL | ⚠️ 只在 SQLite 不可用时 |

📌 **核实过的当前状态**（我一开始以为这段过时了，核到底发现**文档是对的**）：

- `sessions.json` **确实还在写** —— `gateway/session.py:787`
  `self._write_sessions_json = bool(getattr(config, "write_sessions_json", True))`，
  默认开；源码里多处称它是 "sessions.json **mirror**"（`:959`、`:971`）；
- 但**权威**是 SQLite：同一个类的 docstring 逐字（`:756`）
  *"Session routing index + transcripts: SQLite (SessionDB), legacy JSONL fallback."*；
- 另有**一个消费方**已经不再读它了：`gateway/channel_directory.py:320-324` 列"已知频道/联系人"时
  是 *"state.db first, sessions.json fallback (pre-migration)"* —— **我最初就是被这句误导的**
  （它是那个消费方的回退路径，不代表存储层不写）。
- 顺带一条安全约束（源码注释逐字，`:455`）：*"`api_key`/`api_mode` must NEVER reach sessions.json"*。
  **镜像里不许出现凭据** —— 因为它是给人看/给别的进程读的 JSON，不是加密存储。

---

## 4. 会话键的生成规则

会话键是决定性的字符串，用来标识"**一条对话车道**"。由
`build_session_key(source, group_sessions_per_user, thread_sessions_per_user)` 生成。

### 键的格式

```text
agent:main:{platform}:{chat_type}[:{chat_id}][:{thread_id}][:{participant_id}]
```

（第 7 章已经讲过 `agent:main` 这个前缀的来历与 `main~` 那招，这里不再重复。）

### DM 规则

| 情形 | 键 |
|---|---|
| 有 chat_id 的 DM | `agent:main:telegram:dm:12345` |
| 有 chat_id + 线程 | `agent:main:telegram:dm:12345:thread_678` |
| 没有 chat_id、有 participant_id | `agent:main:signal:dm:user_abc` |
| 既没有 chat_id 也没有 participant_id | `agent:main:telegram:dm` |
| WhatsApp DM（已规范化） | `agent:main:whatsapp:dm:{规范化号码}` |

- DM **只要**有 `chat_id` 就一定带上它 —— 于是每条私聊各自隔离；
- `thread_id` 进一步区分同一个 DM 聊天里的不同线程；
- 没有 `chat_id` 时，退到 `user_id_alt` 或 `user_id` 当 participant_id；
- **什么标识都没有时，那个平台上所有 DM 塌成一个共享会话**（这就是为什么"退路"要写清楚）。

### 群 / 频道规则

| 情形 | 键 |
|---|---|
| 群聊 | `agent:main:telegram:group:-10012345` |
| 群聊，按人隔离 | `agent:main:telegram:group:-10012345:user_abc` |
| 群里的线程（共享） | `agent:main:discord:group:12345:thread_678` |
| 群里的线程（按人） | `agent:main:discord:group:12345:thread_678:user_abc` |
| 频道 | `agent:main:slack:channel:C12345` |
| WhatsApp 群（已规范化） | `agent:main:whatsapp:group:{规范化id}:{参与者}` |

- `chat_id` 标识父群/父频道；
- `thread_id` 区分父级下面的线程；
- **"按人隔离"**（追加 `participant_id`）由两个开关控制：
  - `group_sessions_per_user`（默认 `True`）—— 群/频道**每人是独立会话**；
  - `thread_sessions_per_user`（默认 `False`）—— 线程**默认共享**
    （Telegram 论坛话题、Discord 线程、Slack thread 都是"一个线程一个共享会话"）；
- `participant_id` = `user_id_alt` 或 `user_id`（按这个优先级）；
- WhatsApp 的标识会做**规范化**，以对付 JID/LID 别名来回翻转。

### 特例：WhatsApp

WhatsApp 电话号码会过 `canonical_whatsapp_identifier()`：去掉 `@s.whatsapp.net` 后缀、
规范成 E.164 格式。目的是**防止会话碎片化** —— 桥接层有时对同一个号码返回不同别名形态，
不规范化就会"同一个人变成两条会话"。

📌 **这一节的精华是"隔离的默认值"**，用一句话概括：

```text
私聊：永远一人一间            （不可配）
群聊：默认每人一间            （group_sessions_per_user = True）
线程：默认大家一间            （thread_sessions_per_user = False）← 唯一"默认共享"的
```

"线程默认共享"是个**有意的选择**：一个话题下面的讨论本来就是一群人一起看的，
按人切开反而会让人觉得"群里怎么没人理我"。而群聊按人切开，是因为群里可能有陌生人。

---

## 5. 多用户隔离策略

多用户隔离决定：**同一个聊天里的多个用户，是共享一段对话，还是各有一条私有会话**。

### 判定逻辑（`is_shared_multi_user_session`）

```python
def is_shared_multi_user_session(source, *, group_sessions_per_user, thread_sessions_per_user):
    if source.chat_type == "dm":
        return False  # 私聊永远私有
    if source.thread_id:
        return not thread_sessions_per_user  # 线程：除非按人，否则共享
    return not group_sessions_per_user       # 群：除非共享，否则按人隔离
```

📌 注意这三行是**会话键那套规则的"另一面"**：`build_session_key` 决定"键长什么样"，
这个函数决定"键里没有 participant_id 时，意味着什么"（= 共享）。
两处必须一致，否则会出现"键共享了但提示词说这是私聊"这种矛盾。

### 汇总

| 聊天类型 | 默认 | 配置开关 |
|---|---|---|
| DM | 私有（永不共享） | 无 |
| 群 / 频道 | 每人隔离 | `group_sessions_per_user`（默认 `True`） |
| 线程（论坛、discord） | **共享**（所有参与者看到同一份上下文） | `thread_sessions_per_user`（默认 `False`） |

### 对系统提示词的影响

当 `shared_multi_user_session=True` 时，系统提示词**不写固定的用户名**，而是说明：

> *"Multi-user {thread|session} —— 消息会带上 [发送者名字] 前缀。可能有多个用户参与。"*

**每个用户消息的发送者名字，是网关在运行时**加前缀的 —— 这样做的原因是**保住提示词缓存**：
系统提示词**不随轮次变化**（第 5 章那条"缓存神圣"的规矩在这里的具体体现）。
如果改成"把参与者名字写进系统提示词"，每换一个人说话缓存就失效一次。

---

## 6. 显式对话边界

这一节是整章最"反直觉"的一节，但规则很硬：

> **不活动和墙钟时间永远不轮转对话。**

- `/new` 和 `/reset` 创建**显式边界**；长历史继续由**上下文压缩**管理；
- **遗留的定时器配置会被忽略**。`SessionResetPolicy` 这个数据类型是**惰性的兼容数据，
  不是运行时策略**（也就是说：它还在代码里，但没人按它做事）；
- 显式挂起（suspension）**仍然会在下一次入站轮次时创建边界**；
- 恢复流程**尊重**显式与历史已终结的边界，不会把它们重新打开；
- **纯资源淘汰**（resource-only eviction）与 WebSocket 孤儿回收，都让对话**保持可恢复**。

📌 **这一节和 §2 那张状态图的关系**：状态图里那格"策略说要重置吗？（`idle`/`daily`）"
是**历史遗留路径**。现在的答案就是这一节：**没有任何东西会按时间重置**。
理解这一点之后，下面的推论都要成立：

| 推论 | 为什么 |
|---|---|
| 用户回来时对话一定还在 | 时间流逝不是结束会话的理由 |
| 想重开必须显式 `/new` | 边界只能由用户（或显式挂起）创建 |
| 内存压力只能"放掉资源"，不能"结束对话" | 二者被**刻意分开**（§10、§11 会反复出现这条） |

---

## 7. 重启恢复流程

重启恢复系统保证"**正在进行中的会话**在网关重启、崩溃、drain 超时之后还能保住"。
它是 issue #7536 的解法。

### 启动恢复序列

```text
网关启动
       │
       ▼
┌───────────────────────────────┐
│ 有 .clean_shutdown 标记吗？      │── 有 ──► 跳过挂起（上次是干净退出）
└───────────────────────────────┘
       │ 没有
       ▼
┌───────────────────────────────┐
│ session_store                 │── 把最近 120 秒内更新过的
│ .suspend_recently_active()    │   会话标成 resume_pending
└───────────────────────────────┘
       │
       ▼
┌───────────────────────────────┐
│ _suspend_stuck_loop_sessions()│── 把"跨 3+ 次重启都还活跃"的
│                               │   会话挂起
└───────────────────────────────┘
       │
       ▼
┌───────────────────────────────┐
│ 启动恢复期间，入站消息先排队      │
│ (_startup_restore_in_progress)│
└───────────────────────────────┘
       │
       ▼
┌───────────────────────────────┐
│ 对每个适配器，找出 resume_pending │
│ 的会话 → 合造一个 MessageEvent    │
│ → 跑 _handle_message，让 agent   │
│ 自己继续下去                     │
└───────────────────────────────┘
```

📌 最后一步值得单独看：**"继续"这个动作不是特殊代码路径**，而是**造一条合成的消息**，
走**和用户发消息一模一样的那条路**（`_handle_message`）。
这样"恢复"就不需要一套自己的逻辑 —— 它复用正常的回合流程。

### `suspend_recently_active(max_age_seconds=120)`

在没有 `.clean_shutdown` 标记（= 崩溃或非正常退出）时的启动阶段调用。
对最近 120 秒内更新过的每个会话：

- 置 `resume_pending=True`、`resume_reason="restart_interrupted"`、`last_resume_marked_at=now`；
- **跳过**已经是 `resume_pending=True` 的（不重复标记）；
- **跳过**显式 `suspended=True` 的（硬擦除应当保持）。

📌 那个 120 秒窗口的意思是：**只有"刚刚在跑"的会话才值得恢复**。
三天没动过的会话不需要"续跑" —— 它下次被访问时本来就是普通复用。

### 卡死循环检测（`_suspend_stuck_loop_sessions`）

用一个 JSON 文件（`{HERMES_HOME}/restart_counts.json`，⚠️ 见附录 C ③）
统计**连续重启次数**。如果某个会话跨 **3+ 次连续重启**都还活跃，
它会被**自动挂起**，让用户得到一个干净的开始。

📌 这是"**恢复机制必须有自己的退出条件**"的范例：没有它，一个每次启动都崩的会话
会让网关陷入"崩溃 → 恢复 → 再崩溃"的死循环。

### Drain 超时标记

在优雅关闭/重启时，drain 系统会对"drain 超时触发时还在轮次中间"的会话调用
`mark_resume_pending()`。三个原因：

- `"restart_timeout"` —— 重启 drain 期间被杀；
- `"shutdown_timeout"` —— 关闭 drain 期间被杀；
- `"restart_interrupted"` —— 崩溃恢复（来自 `suspend_recently_active`）。

三个原因都在 `_AUTO_RESUME_REASONS` 里，都有资格在启动时自动续跑。

### 下次访问时的自动续跑

当 `get_or_create_session()` 遇到 `resume_pending=True`：

1. 返回原记录，**不**创建新 `session_id`；
2. 已有的转写本**原样加载**；
3. 标记**不在这里清除** —— 它会一直留着，直到**下一轮成功完成**
   （`clear_resume_pending()` 由网关在 `run_conversation()` 返回真实响应之后调用）；
4. 如果恢复的这轮**又被打断**，`resume_pending` 保持置位，下次重启会**再试**。
   终结性的升级交给卡死循环计数器（3 次 → 挂起）。

📌 第 3 条是"**标记的清除时机**"这个常见坑的标准答案：
**在成功之后清，而不是在读取时清**。否则"标记 → 读 → 立即清 → 又崩"会永远恢复不上。

### 干净关闭标记（`.clean_shutdown`）

在优雅关闭的**末尾**写入。下次启动时：

- **如果在**：完全跳过 `suspend_recently_active()`。活跃 agent 已经被 drain 干净了，
  没有会话卡住；
- 然后**删掉**这个标记。

这样就能避免 `hermes update`、`hermes gateway restart`、`/restart` 之后**不必要的自动重置**。

📌 这里的手法值得记：**用一个"上次是不是干净退出"的标记，来决定"要不要做脏退出的恢复"**。
比"每次启动都做恢复"更精确 —— 后者的代价是"干净重启的会话也被当成被打断的"。

---

## 8. 消息排队流程

消息排队系统处理两种场景：

1. **打断式跟进**（interrupt follow-ups）—— 用户在 agent 处理期间连发多条消息时，
   后续消息进入**单槽待发位**（single-slot pending）；
2. **`/queue` 的 FIFO** —— 显式的 `/queue` 命令：每一条都必须**各自产生一整轮** agent
   回合，按顺序，**不合并**。

### 数据结构

```text
adapter._pending_messages: Dict[session_key, MessageEvent]
    └── 每个会话**一个**"下一条"槽位。重复发送会**覆盖**（突发合并）
        与"照片连发"的跟进共用

self._queued_events: Dict[session_key, List[MessageEvent]]
    └── 溢出缓冲。槽位被占时，每次 /queue 调用往这里追加。
        每轮 drain 之后**逐条提拔**上来
```

📌 两种结构对应两种**意图**，这是本节的核心：

| | `_pending_messages`（单槽，覆盖） | `_queued_events`（FIFO，不合并） |
|---|---|---|
| 用户意图 | "我改主意了"（第 7 章讲过） | "这几件事**都要做**，按顺序" |
| 行为 | 新的覆盖旧的 | 一条一条来，一条不少 |
| 来源 | 普通消息、照片连发 | 显式 `/queue` |

### 入队（`_enqueue_fifo`）

```text
_enqueue_fifo(session_key, event, adapter)
       │
       ▼
┌───────────────────────────────────────┐
│ 槽位空吗？                              │
│ (session_key 不在 _pending_messages 里) │── 空 ──► 放进槽位
└───────────────────────────────────────┘
       │ 不空
       ▼
追加到 _queued_events[session_key]（溢出队列尾）
```

### 出队 / 提拔（`_promote_queued_event`）

在"槽位被消费掉"的 drain 点调用。如果溢出队列里有东西：

- `pending_event is None`（槽位空）时：把溢出队列头当作新事件返回；
- `pending_event` 存在时：把溢出队列头**暂存进槽位**，留给下一轮递归；
- **如果没有可用的适配器**：把它**推回** `_queued_events`（**不许静默丢弃**）。

### 队列深度

`_queue_depth(session_key, adapter)` 返回 `len(溢出) + (槽位被占则 1，否则 0)`。

### 清空

一个会话的排队事件在 `/new` 与 `/reset` 时清掉（经 `_handle_reset_command`）。

📌 这一条与"边界"语义一致：`/new` 说"这条对话重开"，那么**排在里面的旧意图也不该再执行**。

### FIFO 不变量

**每次 `/queue` 调用恰好产生一整轮 agent 回合，按 FIFO 顺序，不合并。**
"单槽 `_pending_messages` + 溢出 `_queued_events`"这个设计保证：
活跃轮次期间的重复发送**不会**导致乱序处理。

---

## 9. 会话上下文注入

`SessionContext` 由 `SessionSource` + `GatewayConfig` 构建，注入 agent 的系统提示词。
它告诉 agent 四件事：

- 当前这条消息**从哪来**；
- **哪些平台**连着；
- 定时任务的产出**可以投到哪**；
- 这是不是一个**多人共享**的会话。

### 构造（`build_session_context`）

```python
def build_session_context(source, config, session_entry=None) -> SessionContext
```

1. 从配置里收集已连接平台；
2. 收集每个平台的**主频道**（home channel）；
3. 用 `is_shared_multi_user_session()` 判断是否共享会话；
4. 如果给了 `session_entry`，附上会话元数据（键、id、时间戳）。

### PII 脱敏（`build_session_context_prompt`）

那段动态系统提示词（`## Current Session Context`）可以选择在发给 LLM 之前
**脱敏个人可识别信息**：

- 用户 id → `user_<12hex>`（SHA-256 前缀）；
- 聊天 id → `<platform>:<12hex>` 或直接 `<12hex>`；
- **排除脱敏的平台**：Discord（`@mention` 需要原始 id），
  以及任何**没有标 `pii_safe`** 的插件注册平台。

**脱敏只作用于系统提示词文本。** 路由、会话键、适配器操作**永远**用原始值。

📌 "脱敏只作用于提示词文本"这条边界非常值得记：
如果为了脱敏去改会话键，那"同一个人的会话"就会因为脱敏前后不一致而**裂成两个**。
**身份标识用于路由，必须保持原样；用于给模型看的，才可以脱敏。**

---

## 10. 后台维护

`_session_housekeeping_watcher` 周期性做三件事：**清扫空闲的缓存 agent**、
**内存压力下淘汰缓存条目**、**每小时剪枝一次旧路由条目**。

> **它永远不会因为"不活动"或"到了一天的某个时刻"而结束一个转写本。**

- TTL、LRU 与压力淘汰都会**先把活着的转写本提交给记忆 provider**，再软释放客户端；
- 正在跑轮次的会话**受保护**；终端、浏览器、后台进程的资源**在软释放后仍然存活**；
- **路由条目剪枝会保留权威的 SQLite 转写本**；有活进程的会话**保护**自己的路由条目不被剪枝；
- 历史遗留的 `expiry_finalized` 标志仍然作为恢复栅栏存在，但**不再有任何定时器 watcher 去写它**。

📌 **这一节一句话**：维护可以"放掉内存里的东西"，但**不能"结束对话"** ——
两件事被刻意分开（§6 已经说过一遍，这里说第二遍，因为它太容易搞混）。

---

## 11. Agent 缓存

网关维护一个 `AIAgent` 实例的 **LRU 缓存**，按 `session_key` 索引 ——
目的是**跨轮次保住提示词缓存**。

### 缓存属性

- **最大条数**：128（`agent.agent_cache.max_size`，默认常量 `_AGENT_CACHE_MAX_SIZE`）；
- **淘汰策略**：LRU（用 `OrderedDict`）；
- **空闲 TTL**：3600 秒（1 小时）—— `agent.agent_cache.idle_ttl_secs`，
  由 `_session_housekeeping_watcher` 执行；
- **内存预算**：`agent.agent_cache.memory_high_mb`（默认 `auto`）—— 见下；
- **锁**：`_agent_cache_lock`（线程锁）。

### 内存压力淘汰

一个缓存的 agent **钉住** `_session_messages` —— 那是**活的完整转写本，含工具输出**；
在一个有 100+ 次工具调用的会话上，这是**几十 MB**。而"条数上限"和"空闲 TTL"两把尺子
**都对它视而不见**：一个服务很多聊天的网关会让**每一份热转写本都常驻**
（TTL 内跑过轮次的 agent 永远不会被"空闲清扫"扫到），于是 RSS 一路爬升，
直到 cgroup 开始限流，而 SIGTERM 已经没有时间在 systemd 的 stop 超时内冲干净（**#80764**）。

`_sweep_agent_cache_under_pressure()` 就是那个泄压阀。每个 watcher tick 它比较
**匿名内存**与 `memory_high_mb`：

- 网关跑在 cgroup 限额下时，用 cgroup 自己的 `memory.stat` 里的 `anon`
  （**计费口径就是它** —— 所以同 unit 的子进程比如 `execute_code` 的内核也算在内，**#110549**）；
- 否则用进程自己的匿名 RSS；
- **超预算**就按 LRU 通过**和条数上限执行时同一条软路径**（`_commit_then_release_soft`）淘汰，
  然后跑 `malloc_trim`，让释放出来的 arena **真正还给操作系统**。

被淘汰的会话，**下一轮从落盘的会话重建转写本**。

**三类会话永远不会被放掉**：

1. **正在轮次中间的**（它们的客户端与沙箱正在被使用）；
2. `protect_recent` 个**最近使用**的会话（它们的提示词缓存最值钱）；
3. **活转写本还没完全落盘**的 ——
   `transcript_persistence_caught_up()` 拿 `_last_flushed_db_idx` 与
   `len(_session_messages)` 比较；**这正是 FTS 写坏防护所盯的那处偏差**
   （当它选择"保住活历史而不是落后的转写本"时，盯的是同一个差值）。

`memory_high_mb: auto` 会从网关所在的 cgroup 限额推导预算
（先 `memory.high`，再 `memory.max`，再 cgroup v1），**没有限额时退到总内存**。
写一个数字就钉死它，写 `0`/`off` 就整趟关掉。这些助手都在
`gateway/agent_cache_pressure.py`。

📌 第 3 类最值得记：**"没落盘的东西不许淘汰"** —— 否则内存压力会**吃掉还没存下来的对话**。
它和第 6 章的"FTS 索引落后于正文"防的是同一类偏差（正文与派生索引不同步）。

### 缓存生命周期

```text
消息到达
    │
    ▼
get_or_create_session()  →  拿到 session_key
    │
    ▼
查 _agent_cache[session_key]
    │
    ├── 命中 → move_to_end()，复用 AIAgent（**保住提示词缓存**）
    │
    └── 未命中 → 新建 AIAgent，放进缓存
                  （到容量上限就 popitem(last=False) 淘汰 LRU）
    │
    ▼
run_conversation()  →  agent 处理消息
    │
    ▼
后台维护**软释放**空闲 agent，但**不结束转写本**
```

### 清理流程

资源淘汰会**先提交记忆**、再软释放客户端。完整的
`_cleanup_agent_resources(agent)` 拆除**只留给真正的对话边界与关闭**。

---

## 附录：关键配置

| 配置键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `group_sessions_per_user` | `bool` | `true` | 群/频道会话按人隔离 |
| `thread_sessions_per_user` | `bool` | `false` | 线程会话按人隔离 |
| `session_store_max_age_days` | `int` | `0` | 剪掉 N 天前的会话（0=禁用） |
| `agent.gateway_auto_continue_freshness` | `int` | `3600` | "续跑"的新鲜度窗口（秒） |
| `agent.gateway_timeout` | `int` | `1800` | 一轮 agent 的超时（默认 30 分钟） |
| `agent.agent_cache.max_size` | `int` | `128` | 缓存 AIAgent 的 LRU 条数上限 |
| `agent.agent_cache.idle_ttl_secs` | `int` | `3600` | 空闲这么久就淘汰 agent |
| `agent.agent_cache.memory_high_mb` | `int`/`str` | `auto` | 超过这个匿名 RSS 预算就淘汰 LRU 转写本 |
| `agent.agent_cache.max_evictions_per_pass` | `int` | `16` | 每趟压力淘汰最多放掉几个会话 |
| `agent.agent_cache.protect_recent` | `int` | `8` | 压力淘汰**永不触碰**的最近使用会话数 |

---

## 状态库与 FTS 恢复

权威转写本住在 `sessions` 与 `messages` 两张表里。**FTS5 表及其同步触发器是派生索引** ——
它们可以**被拆掉再重建，而不会删掉权威消息**。有界的实时故障模式与显式修复流程见
《状态库恢复》（`state-db-recovery.md`）。

### 对话的生命周期（收口）

**不支持任何"空闲重置"或"每日重置"设置。** 显式 `/new` 与 `/reset`、压缩、
挂起与崩溃恢复**各自保留自己独立的生命周期角色**。

---

## 附录 A · 术语表（本章新增）

| 原文 | 译法 | 说明 |
|---|---|---|
| session key | 会话键 | "哪条对话车道"（长期稳定），`agent:main:...` |
| session id | 会话 id | "这条车道上的第几趟车"（会被重置换新） |
| SessionSource | 消息来源描述符 | 一条消息从哪来的冻结记录（平台/聊天/用户/线程…） |
| SessionEntry | 活动会话记录 | 每个会话一条的元数据 + 状态标志 |
| SessionStore | 会话存储层 | 内存字典 + `sessions.json` + SQLite 的入口 |
| SessionContext | 会话上下文 | 注入系统提示词的那段"当前会话"信息 |
| conversation lane | 对话车道 | 会话键所标识的那条线（一个聊天/一个线程） |
| suspension | 挂起 | 硬擦除信号（`suspended`）：下次访问换新会话 |
| resume pending | 待恢复 | 软恢复标记：下次访问**接着**原会话 |
| explicit boundary | 显式边界 | `/new` `/reset` 造出的对话分界（唯一的分界来源） |
| resource-only eviction | 纯资源淘汰 | 只放内存、**不结束对话**的清理 |
| drain (timeout) | 排空（超时） | 关闭/重启时等正在跑的轮次收尾 |
| stuck loop | 卡死循环 | 连续重启都还活跃的会话（3 次 → 挂起） |
| single-slot pending | 单槽待发位 | 每个会话一个"下一条"，重复发送会覆盖 |
| overflow buffer | 溢出缓冲 | `/queue` 的 FIFO 队列（不合并、逐条提拔） |
| promotion | 提拔 | 从溢出缓冲把一条挪进槽位 |
| queue depth | 队列深度 | 溢出条数 + 槽位是否被占 |
| housekeeping watcher | 后台维护巡检 | 周期清扫空闲 agent / 压力淘汰 / 每小时剪枝 |
| soft release | 软释放 | 放掉客户端资源，但**保留对话** |
| LRU eviction | LRU 淘汰 | 最久未使用先走 |
| memory pressure | 内存压力 | 匿名 RSS 超预算 → 触发淘汰 |
| anon RSS | 匿名 RSS | 进程自己分配的、没有文件背书的常驻内存 |
| cgroup memory.high / memory.max | cgroup 内存上限 | 容器/服务的内存限额层次 |
| prompt caching | 提示词缓存 | 跨轮次复用前缀（缓存 agent 的动机） |
| PII redaction | PII 脱敏 | 发给模型前把 id 换成哈希（路由仍用原值） |
| home channel | 主频道 | 定时任务产出/后台结果的默认投递目标 |
| growth/decay of tokens | 累计 token | 每会话的 in/out/cache 读/写/cost 记账 |
| fresh reset | 新重置 | `/new` 之后的第一条消息要重注入话题技能 |

---

## 附录 B · 这一章在整套文档里的位置

```text
官方侧边栏（Developer Guide）        我们的进度
 5  agent-loop                       ✅ 第 2 章
 6  prompt-assembly                  ✅ 第 3 章
 7  context-compression-and-caching   ✅ 第 5 章
 8  micro-compaction                 ⬜ **被跳过**（第 5 章只讲主线压缩）
 9  gateway-internals                ✅ 第 7 章
10  gateway-session-lifecycle  ★本章  ← 就是你手上这篇
11  multiplexing-gateway             ⬜（第 7 章侧线做了它的一半：多 profile 入口层）
12  completion-backlog-delivery      ⬜ 52 行（"投递债"—— 第 7 章差距清单里挂着）
13  session-storage                  ✅ 第 6 章
14  state-db-recovery                ⬜ 107 行（与第 6 章配对）
15  provider-runtime                 ⬜
36  tools-runtime                    ✅ 第 4 章
```

**这一章与已讲章节的接线**：

| 已讲过的概念 | 在本章哪里被用到 |
|---|---|
| 第 5 章：压缩 / 缓存 / usage 锚点 | §6（时间不轮转，压缩管长历史）、§2（`last_prompt_tokens` 预检） |
| 第 6 章：`state.db` / 表 / 世代 / FTS | §3（存储布局）、§11（`_last_flushed_db_idx` 与 FTS 防坏的同一处偏差）、尾节（FTS 可拆可重建） |
| 第 7 章：会话键与 `main~` / 两级守卫 / 忙时策略 / 交付 | §4（键格式的完整版）、§8（队列 = 守卫三件套的完整形状）、§1（`origin` 与投递路由） |

**读这一章要能答出的四个问题**：

1. `suspended` 和 `resume_pending` 同时为真时，`get_or_create_session()` 会怎么做？为什么？
2. "不活动"和"内存压力"分别会不会让一个对话结束？各自的处置是什么？
3. 群聊、线程、私聊三种情况下的**默认隔离**分别是什么？（哪个是"默认共享"的？）
4. 网关重启之后，一个"刚在跑"的会话怎么被续上？（提示：它走的路径和用户发新消息是不是同一条？）

---

## 附录 C · 上游文档出入（本次核实 2 处）

核对的锚：上游 commit **`f5d1926110`**（v0.21.3）。本次逐条核了原文点名的
**文件规模、兄弟模块名、常量、默认值、函数名**，结论如下。

### ① 不是出入 —— 我自己误判了一条，记录在此

一开始我以为文档过时了（"`sessions.json` 早就不写了"），理由是
`gateway/channel_directory.py:320-324` 那句
*"Known channels/contacts from session origins: state.db first, sessions.json fallback (pre-migration)"*。
**核到底发现文档是对的**：那是 `channel_directory` **这个消费方**的回退路径；
存储层本身还在写 `sessions.json` ——

- `gateway/session.py:787`：`self._write_sessions_json = bool(getattr(config, "write_sessions_json", True))`（默认开）；
- `:959` / `:971` 源码注释里称它为 "sessions.json **mirror**"；
- `:756` 的 docstring 把层次写得很清楚：*"Session routing index + transcripts: SQLite (SessionDB),
  legacy JSONL fallback."* —— **SQLite 权威、JSON 镜像、JSONL 只在降级时**。

**留这条的目的**：它是"**看一句注释就下结论**"的典型陷阱 —— 那句注释描述的是一条回退路径，
而它读起来像"这个文件已经废弃了"。判据是：**要断言"某物不存在"，必须去看它的写方，而不是看某个读方**。

### ② 会话 id 不是 8 位十六进制，而是 6 位

- 文档 §2 写：格式 `YYYYMMDD_HHMMSS_<8hex>`；
- 源码：`hermes_state_ids.py:21` → `DEFAULT_HEX_LEN = 6`
  （`:24-28` 是 `stamp = ...strftime("%Y%m%d_%H%M%S")` + `uuid.uuid4().hex[:hex_len]`）。
- 影响：小。只是"8 → 6"，看 id 形态时不会误判。

### ③ 卡死循环的计数文件名与文档写的不一致

- 文档 §7 写：用 `{HERMES_HOME}/restart_counts.json` 统计连续重启次数；
- 源码：真实文件名是 **`.restart_failure_counts`** —— `gateway/run.py:3969`
  `_STUCK_LOOP_FILE = ".restart_failure_counts"`，由 `gateway/run_shutdown.py:1217`
  `_stuck_loop_counts_path()`（= `_hermes_home / self._STUCK_LOOP_FILE`）拼出来，
  `:1237` 的 `_suspend_stuck_loop_sessions()` 读它。
  **源码里搜不到 `restart_counts.json` 这个字面量**（名字对不上，而且是**点文件**、不是 `.json`）。
- 影响：小。按 `_STUCK_LOOP_FILE` / `_stuck_loop_counts_path()` 找，不要按文档给的文件名找。

### 已核实**正确**的声明（别再重查）

| 文档说 | 实测 |
|---|---|
| `gateway/session.py` ~1200 行、`gateway/run.py` ~5500 行 | ✅ **1,284** 行 / **5,632** 行 |
| 兄弟模块 `run_watchers.py` / `run_agent_cache.py` / `session_recovery.py` / `run_busy.py` | ✅ 四个都在（另有 `gateway/session_*.py` **8** 个、`gateway/run_*.py` **21** 个） |
| `_AUTO_RESUME_REASONS` = 三个 reason | ✅ `gateway/run.py:3973` 逐字一致 |
| `.clean_shutdown` 标记（关闭时写、下次启动删） | ✅ `gateway/run_shutdown.py:1889` |
| `is_shared_multi_user_session` 的三分支逻辑 | ✅ `gateway/session.py:614-622`（源码写成**等价的两分支**：`return not (thread_sessions_per_user if source.thread_id else group_sessions_per_user)`） |
| PII：`user_<12hex>`、Discord 与未标 `pii_safe` 的平台不脱敏 | ✅ `gateway/session.py:33`（`_hash_user_id`）、`:185-199`（`_should_redact_pii` + `entry.pii_safe`） |
| 缓存默认：128 / TTL 3600 / 每趟 16 / 保护 8 | ✅ `gateway/run.py:46`（`_AGENT_CACHE_MAX_SIZE = 128`）、`gateway/agent_cache_pressure.py:24`（16）、`:27`（8） |
| 配置默认：`gateway_timeout` 1800、`idle_ttl_secs` 3600、`max_size` 128、`gateway_auto_continue_freshness` 3600 | ✅ `hermes_cli/config_defaults.py:62/70/71/218` |
| 后台维护：每小时剪枝一次 | ✅ `gateway/run_watchers.py:26`（`_SESSION_STORE_PRUNE_INTERVAL = 3600.0`）、`:40`（watcher 默认 interval **300s**） |
| `sessions.json` 是映射/元数据的持久化（SQLite 权威） | ✅ `gateway/session.py:756/787/959/971`（**镜像** + `write_sessions_json` 开关默认开） |
| 镜像里不许出现凭据 | ✅ `gateway/session.py:455`：`api_key`/`api_mode` must NEVER reach sessions.json |
| §7/§8/§11 点名的一批函数 | ✅ `_startup_restore_in_progress`、`_enqueue_fifo`、`_promote_queued_event`、`_queue_depth`、`_queued_events`、`_commit_then_release_soft`、`transcript_persistence_caught_up`、`_last_flushed_db_idx`、`_sweep_agent_cache_under_pressure` —— 全部能在 `gateway/` 里找到 |
