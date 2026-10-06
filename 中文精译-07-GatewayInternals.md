# 中文精译 · 第 7 章：网关内部（Gateway Internals）

> **原文**：`website/docs/developer-guide/gateway-internals.md`（297 行）
> **本篇定位**：翻译为主，目标是**看懂原理**。差异对比、源码深挖、实现日志都不在这里 ——
> 那些属于后面两个阶段（mini 实现 / 源码讲解）。
> 看不懂的地方或想深挖的点，随时问，我再往这里补。
>
> 📌 行号锚在上游 commit **`f5d1926110`**（`version = "0.21.3"`）；上游每合并一个 PR 行号就会漂。
> 译文里凡是**与源码不符**的地方，用 `⚠️ 上游文档出入` 标出来（本章已发现 1 处，见附录 C）。

[TOC]

---

## 0. 这一章在回答什么

前面六章讲的都是**一个 agent 在自己的进程里**怎么跑。这一章讲的是**外面那层**：
消息从哪来、谁有资格进来、进来之后归哪个会话、以及它凭什么能一直活着。

一句话概括：**网关是"唯一常驻的那个进程"，也是"在 agent 之前说'不'的那道门"。**

```
外部平台（20+ 个：Telegram / Discord / Slack / 飞书 / 企业微信 …）
      │  ①收
      ▼
  适配器（每个平台一个）→ 归一化成 MessageEvent
      │  ②守
      ▼
  网关守卫（在 agent 之前！）授权 / 重复投递 / 正在跑
      │  ③路
      ▼
  会话键 → 会话行 → AIAgent（第 2~6 章那一套全在这里面）
      │  ④交
      ▼
  回程：交付路径（直答 / 主频道 / 指定目标 / 跨平台）
```

**这一章要解决四个问题**：

| # | 问题 | 对应节 |
|---|---|---|
| 1 | **谁能进来**（准入） | §4 授权 |
| 2 | **这条消息归哪个会话**（路由） | §3 消息流程 + 会话键格式 |
| 3 | **它怎么活着**（常驻 + 多进程/多 profile） | §12 进程管理、§13 多路复用 |
| 4 | **东西怎么发回去**（交付） | §8 交付路径 |

另外还有四件"围着上面四条转"的事：斜杠命令分发（§5）、配置来源（§6）、
平台适配器（§7）、钩子与记忆集成（§9、§10）、后台维护（§11）。

**与前面章节的接线**（读的时候对着看）：

- 第 5 章的**网关侧会话维护**（进 agent 之前那道兜底压缩）就是在这里被触发的；
- 第 6 章的**`state.db`** 就是这里的 `SessionStore` 落的地方；
- 第 6 章挂到本章的三条：跨进程压缩锁、多 profile 多库、输入的"归属"仲裁。

---

## 1. 关键文件

| 文件 | 干什么 |
|---|---|
| `gateway/run.py` | `GatewayRunner` **门面** —— 把 `gateway/run_*.py` 那一堆兄弟模块（startup / adapters / inbound / turn / busy / goals / notifications / shutdown / …）和 `gateway/slash_commands_*.py` 的处理器拼起来 |
| `gateway/session.py` | `SessionStore` —— 会话持久化 + **会话键构造** |
| `gateway/delivery.py` | 出站消息投递到目标平台/频道 |
| `gateway/pairing.py` | 私聊配对流程（用户授权） |
| `gateway/channel_directory.py` | 把 chat ID 映射成人能读的名字（给 cron 投递用） |
| `gateway/hooks.py` | 钩子的发现、加载、生命周期事件分发 |
| `gateway/mirror.py` | `send_message` 的跨会话消息镜像 |
| `gateway/status.py` | **token 锁**管理（profile 作用域的网关实例） |
| `gateway/builtin_hooks/` | "永远注册的内置钩子"的扩展点（**出厂时是空的**） |
| `gateway/platform_registry.py` | 适配器注册表、工厂、以及内置平台插件的**延迟（懒）加载器** |
| `plugins/platforms/<名字>/` | 内置的消息适配器（大多数平台是 `adapter.py` + `plugin.yaml`） |
| `gateway/platforms/` | 共享的 `base.py` + 遗留/直连适配器（Signal、API server、webhook…） |

📌 **读法提醒（第 6 章学到的同一件事）**：`gateway/run.py` 是**门面**，
真正的实现散在 `gateway/run_*.py` 兄弟模块里（实测有 **21 个**）。
按主题找代码：`grep -rn "def <名字>" gateway/run_*.py`，别死磕门面。

---

## 2. 架构总览

```text
┌─────────────────────────────────────────────────┐
│                  GatewayRunner                  │
│                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │ Telegram │  │ Discord  │  │  Slack   │       │
│  │  适配器  │  │  适配器  │  │  适配器  │       │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘       │
│       │             │             │             │
│       └─────────────┼─────────────┘             │
│                     ▼                           │
│              _handle_message()                  │
│                     │                           │
│         ┌───────────┼───────────┐               │
│         ▼           ▼           ▼               │
│   斜杠命令分发    AIAgent      排队/后台         │
│                  （构造）       会话             │
│                     │                           │
│                     ▼                           │
│                 SessionStore                    │
│              （SQLite 持久化）                   │
└───────┴─────────────┴─────────────┴─────────────┘
```

**这张图要看出三件事**：

1. **适配器是"平"的**：每个平台一个，互不知道对方存在，只对下层的 `_handle_message()` 负责。
2. **`_handle_message()` 是唯一的咽喉**：所有平台的入站都从这里过 → 所以守卫、授权、命令分发都挂在这。
3. **会话存储在最底下**：也就是说"网关不持有对话状态，它只是让 status 落到持久化层"，
   下一轮要用的历史得从 `SessionStore` 读回来（第 6 章那个库）。

---

## 3. 消息流程

一条消息从任何平台进来，都走这四步：

1. **平台适配器**收到原始事件，归一化成 `MessageEvent`。
2. **基础适配器**检查"会话是否活跃"守卫：
   - 如果这个会话**正在跑 agent** → 把消息**排队**（`_pending_messages`），并设置中断事件；
   - 如果是 `/approve`、`/deny`、`/stop` → **绕过守卫**（内联分发）。
3. **`GatewayRunner._handle_message()`** 收到事件：
   - 用 `_session_key_for_source()` 解析**会话键**
     （格式 `agent:{命名空间}:{平台}:{聊天类型}:{聊天 id}`；默认 profile 的命名空间是 `main`，
     多路复用时是 `<profile>` —— 见 §13）；
   - 检查**授权**（见 §4）；
   - 检查是不是**斜杠命令** → 分发给对应处理器；
   - 检查 agent 是否**已经在跑** → 拦截 `/stop`、`/status` 这类命令；
   - 都不是 → **构造 `AIAgent` 实例**并跑这一轮。
4. **响应**经平台适配器发回去。

### 会话键格式

会话键编码了**完整的路由上下文**：

```
agent:{命名空间}:{平台}:{聊天类型}:{聊天 id}
```

例如默认 profile 下是 `agent:main:telegram:private:123456789`；
当多路复用器把那个聊天路由给 profile `work` 时，就是
`agent:work:telegram:private:123456789`
（`gateway/session.py::_session_key_namespace`；**一个字面就叫 `main` 的 profile 会被标成 `main~`**）。

⚠️ **永远不要手工拼会话键** —— 一律走 `gateway/session.py` 的 `build_session_key()`。

**为什么 `main~` 这个"波浪号"值得单独说**：源码注释写得很直白（`gateway/session.py:626-631`）——

> 默认/None profile → `agent:main`（**与历史所有键逐字节一致**）；具名 profile → `agent:<名字>`，
> 这样两个 profile 服务同一个聊天时永远不会撞。而**一个真叫 `main` 的 profile** 本来会产出默认的
> 命名空间，于是与默认 profile **共享一切**（路由索引、agent 缓存、存储）——
> 所以它被标成 `main~`：`~` **不在 profile id 的字母表里**，所以这个标记形态**不可能**是别的 profile 的 id。

这是"用一个不可能作为合法值的字符"来做区分的经典手法：**不靠约定，靠字母表**。

### 两级消息守卫

当 agent 正在跑，进来的消息要**连着过两道**守卫：

| 级别 | 在哪 | 检查什么 | 干什么 |
|---|---|---|---|
| **第 1 级** | **基础适配器**（`gateway/platforms/base.py`） | `_active_sessions` | 会话活跃 → 消息进 `_pending_messages` 并设置中断事件。**在消息到达网关 runner 之前**就拦下 |
| **第 2 级** | **网关 runner**（`gateway/run_inbound.py`） | `_running_agents` | 拦截特定命令（`/stop`、`/new`、`/queue`、`/status`、`/approve`、`/deny`）并按各自方式路由；其余一律触发 `running_agent.interrupt()` |

**为什么两级**：第 1 级在**更靠外**的位置挡（适配器就知道"这个会话忙"），
第 2 级在**更靠里**的位置**区分命令语义**（`/stop` 要能穿过忙碌状态）。

有些命令**必须在 agent 被挡住的时候也能到 runner**（比如 `/approve`）—— 它们是
**内联分发**的（`await self._message_handler(event)`），**绕过后台任务系统**以避免竞态。

📌 这是本章第一个"看起来可以简化、其实不能"的地方：**内联 vs 后台任务的区别不是性能，是顺序保证。**

---

## 4. 授权

网关用**多层授权检查**，按顺序求值：

| 顺序 | 检查 | 说明 |
|---|---|---|
| 1 | **平台级"全放行"开关** | 例如 `TELEGRAM_ALLOW_ALL_USERS`；设了 → 该平台所有用户都被授权 |
| 2 | **平台白名单** | 例如 `TELEGRAM_ALLOWED_USERS`（逗号分隔的用户 id） |
| 3 | **私聊配对** | 已授权的用户可以给新用户**发配对码** |
| 4 | **全局"全放行"** | `GATEWAY_ALLOW_ALL_USERS`，或 `config.yaml` 里的 `gateway.allow_all_users`（由 `gateway/config_loader.py::bridge_core_env_settings` 桥接成环境变量）|
| 5 | **默认：拒绝** | 未授权的用户被拒 |

**第 5 条是这一节的重点**：默认不是"放行后配置"，而是**拒绝**（fail-closed）。
换句话说，新装一个网关，**它默认什么都不接受**。

### 私聊配对流程

```text
管理员：/pair
网关：  "配对码：ABC123。把它给那个用户。"
新用户：ABC123
网关：  "配对成功！你现在被授权了。"
```

配对状态持久化在 `gateway/pairing.py` 里，**重启不丢**。

---

## 5. 斜杠命令分发

网关里所有斜杠命令走**同一条解析管线**：

1. `hermes_cli/commands.py` 的 `resolve_command()` 把输入映射成**规范名**（处理别名、前缀匹配）；
2. 规范名对照 `GATEWAY_KNOWN_COMMANDS` 检查；
3. `_handle_message()`（`gateway/run_inbound.py`）**按名字查处理器** ——
   即 `gateway/slash_commands_*.py` 那几个 mixin 上的 `_handle_<名字>_command` ——
   查表走 `gateway/run_busy.py` 里的 `_command_handler_table()`（覆盖 `_IDLE_COMMANDS` / `_PLAIN_COMMANDS`）；
   **没有 `if 规范名 == ...` 那种链**；
4. 有些命令还受配置门控（`CommandDef` 上的 `gateway_config_gate`）。

📌 **"没有 if 链"这条值得记住**：上游的代码形状规则里明确写了
"**键在名字/种类上的 `if/elif` 阶梯 ≥ 4 个分支要改成 字典/表 → 处理器**"。
斜杠命令是这条规则最典型的受益者：加一个命令 = 加一行表项，而不是在一条长链里插分支。

### 运行中守卫

**绝对不能在 agent 处理消息时执行**的命令会被提前拒掉：

当 `_quick_key in self._running_agents` 时，`gateway/run_busy.py` 的
`_dispatch_busy_slash_command()` 按每个命令的 `CommandDef.busy_policy` / `busy_handler` 路由：

| 情况 | 结果 |
|---|---|
| 有"运行中专用"变体（`_busy_<键>_command`） | 走那个变体 |
| `busy_policy` 允许 | 走普通处理器 |
| 都不行 | 回一条拒绝消息（"⏳ Agent is running —— `/model` 不能在回合中间跑…"） |

**旁路命令**（`/stop`、`/new`、`/approve`、`/deny`、`/queue`、`/status`）都有运行中处理器，内联分发。

---

## 6. 配置来源

网关从**多个来源**读配置：

| 来源 | 提供什么 |
|---|---|
| `~/.hermes/.env` | API 密钥、bot token、平台凭据 |
| `~/.hermes/config.yaml` | 模型设置、工具配置、显示选项 |
| 环境变量 | 覆盖上面任何一个 |

⚠️ **和 CLI 的一个真实差异**（原文点名的坑）：CLI 用的是 `load_cli_config()`（**带一份硬编码默认值**），
而网关**直接用 YAML 加载器读 `config.yaml`**。后果是：

> **只在 CLI 默认值字典里、却没写进用户配置文件里的键，在 CLI 和网关里行为可能不同。**

这条的价值在于它是"**两处实现、一份事实**"的典型反例 —— 同一个配置项在两条路径上解释不同，
而且**不会报错**，只会表现不一致。

---

## 7. 平台适配器

大多数平台以**插件适配器**的形式发布在 `plugins/platforms/<名字>/adapter.py`；
少数遗留适配器还直接住在 `gateway/platforms/`。它们全都继承
`gateway/platforms/base.py` 的 `BasePlatformAdapter`：

```text
plugins/platforms/                  # 插件化适配器（一个平台一个目录）
├── telegram/adapter.py     # Telegram Bot API（长轮询或 webhook）
├── discord/adapter.py      # Discord bot（discord.py）
├── slack/adapter.py        # Slack Socket Mode
├── whatsapp/adapter.py     # WhatsApp Business Cloud API
├── matrix/adapter.py       # Matrix（mautrix，可选端到端加密）
├── mattermost/adapter.py   # Mattermost WebSocket API
├── email/adapter.py        # 邮件（IMAP/SMTP）
├── sms/adapter.py          # 短信（Twilio）
├── dingtalk/adapter.py     # 钉钉 WebSocket
├── feishu/adapter.py       # 飞书/Lark（WebSocket 或 webhook）
├── wecom/adapter.py        # 企业微信回调
├── line/adapter.py         # LINE Messaging API
├── teams/adapter.py        # Microsoft Teams
├── irc/adapter.py          # IRC（作用域锁的经典示例）
├── homeassistant/adapter.py # Home Assistant 对话集成
└── …                       # google_chat、ntfy、photon、raft、simplex …

gateway/platforms/                  # 核心 base + 遗留直连适配器
├── base.py              # BasePlatformAdapter —— 所有平台共享的逻辑
├── signal.py            # Signal（signal-cli REST API）
├── weixin.py            # 微信个人号（iLink Bot API）
├── bluebubbles.py       # Apple iMessage（BlueBubbles macOS 服务）
├── qqbot/               # QQ 机器人（官方 API v2，子包）
├── yuanbao.py           # 元宝（腾讯）私聊/群
├── msgraph_webhook.py   # Microsoft Graph 变更通知 webhook（Teams、Outlook…）
├── webhook.py           # 入站/出站 webhook 适配器
└── api_server.py        # REST API 服务适配器
```

（实测：`plugins/platforms/` 下正好 **22 个**平台目录，对得上原文那句"20+ 个平台"。）

**延迟加载（deferred loading）**：内置的 `kind: platform` 插件会在
`gateway/platform_registry.py` 里注册**廉价的** `register_deferred` 加载器（经 `hermes_cli/plugins.py`），
这样平台 SDK 只在**网关启动、投递、或跑 setup/status** 时才被 import ——
**普通的 `hermes chat` 不会拉它们**。查找时只加载**一个**适配器；只有需要"枚举全部平台"的路径才会跑完所有待加载项。

📌 这条的动机是**启动开销**：20+ 个平台的 SDK（有的还很重）不该在你只是想聊两句时被 import。

**实验性的 connector 平台**走 `gateway/relay/` 里的**通用 relay 适配器**，而不是直接的平台模块。
当配置了 `GATEWAY_RELAY_URL` 或 `gateway.relay_url` 时，网关注册 `relay` 平台，
用**出站 WebSocket** 拨通 connector，并在同一条 socket 上收 `descriptor`、`inbound`、`interrupt_inbound` 帧。
connector 侧公布一个 `CapabilityDescriptor`；Hermes 可以往回发普通出站回复、
**无需 token 的 `follow_up` 操作**、以及中断帧。线协议契约见
《Relay ↔ Connector 契约》（`relay-connector-contract.md`）。

适配器实现的**通用接口**：

- `connect()` / `disconnect()` —— 生命周期；
- `send()` —— 出站投递；
- 入站事件被归一化成 `MessageEvent`，经 `handle_message()` 转发。

### 内部推送唤醒的"回执"语义

内部推送唤醒走 `gateway.wake.admit_internal_event`：公开的 `handle_message()` **仍然返回 `None`**，
但事件上那个**进程内的** `_gateway_accepted` 回执，**只在真的完成调度或入队之后**才被设置。

原文把几种"不算被接受"的情形点得很清楚：

- **没有处理器**；
- **显式给定的会话键对不上**；
- **队列满了被丢掉**。

自定义适配器如果改写了入口逻辑，应当把内部事件**委托**给
`BasePlatformAdapter.handle_message()`（或者自己显式记录真实准入），
**不要**把"回调被消费/丢弃"当成"被接受了"。

这个回执**独立于心跳执行记账**，而且**不绕过**授权、紧急停止、以及后续的回合准备门。

📌 一句话：**"我调用了" ≠ "它被接受了"** —— 所以要有一个显式的回执位。

### token 锁

用**唯一凭据**连接的适配器，要在 `connect()` 里调 `acquire_scoped_lock()`、
在 `disconnect()` 里调 `release_scoped_lock()`。目的：**防止两个 profile 同时用同一个 bot token**。

锁冲突会以 `{scope}_lock` 的形式抛出，并带 `retryable=True` ——
于是**运行中途**的重连可以在另一个持有者退出后自动恢复。

但**启动时**不一样：一个活着的**外部持有者**属于**配置冲突**。
`gateway/restart.py::is_global_startup_conflict()` 认得 `*_lock` / `lock_conflict` 这两个错误码族，
启动路由器会把该平台**按 fatal 停放**（而不是丢进重试队列）。
如果除此之外别无所连，网关会**以退出码 78 退出**（`EX_CONFIG`，`gateway_state=startup_failed`），
好让**监督者别再重启它**：

| 监督者 | 怎么"停放" |
|---|---|
| systemd | `RestartPreventExitStatus=78` |
| s6 | finish → 125 |
| launchd | stderr 包装器把 78 映射成 0 之后，`KeepAlive.SuccessfulExit=false` |

**而如果同时还有一个真正的瞬时故障**，网关**继续活着**，只让那个同伴重试。

📌 这一节讲的是"**进程退出码也是接口**"：78 不是随手选的数，它是 `sysexits.h` 里的
`EX_CONFIG`，而且是**给监督者看的**信号 —— 它决定"该不该再把我拉起来"。

---

## 8. 交付路径

出站投递（`gateway/delivery.py`）处理四种：

| 类型 | 说明 |
|---|---|
| **直接回复** | 把响应发回**发起它的那个聊天** |
| **主频道投递** | 把 cron 任务输出与后台结果送到配置好的**主频道** |
| **指定目标投递** | 发送引擎显式指定 `telegram:-1001234567890`；通过 [`hermes send` CLI](/guides/pipe-script-output) 暴露给 shell 脚本，也可由 cron 的 `deliver:` 目标使用 |
| **跨平台投递** | 投到**与来源不同**的平台 |

⚠️ **cron 任务的投递不会镜像进网关会话历史** —— 它们只活在**自己的 cron 会话**里。
这是一个**有意的设计选择**：为了避免**消息角色交替违规**（第 2 章那条"两条同角色消息不能相邻"）。）

---

## 9. 钩子

网关钩子是**响应生命周期事件的 Python 模块**。

### 钩子事件表

| 事件 | 什么时候触发 |
|---|---|
| `gateway:startup` | 网关进程启动 |
| `session:start` | 新的对话会话开始 |
| `session:end` | 会话完成或超时 |
| `session:reset` | 用户用 `/new` 重置会话 |
| `agent:start` | agent 开始处理一条消息 |
| `agent:step` | agent 完成一次工具调用迭代 |
| `agent:end` | agent 结束并返回响应 |
| `command:*` | 任何斜杠命令被执行（通配） |

**钩子从两处发现**：

1. `gateway/builtin_hooks/` —— 扩展点，**出厂发行版里是空的**
   （实测该目录只有一个 `__init__.py`；`_register_builtin_hooks()` 是个空壳桩）；
2. `<profile home>/hooks/` —— 用户安装的（默认 profile 是 `~/.hermes/hooks/`；
   多路复用时**每个被服务的 profile 各一个目录** —— 路径**在调用时解析，绝不在 import 时**）。

每个钩子是一个目录，里面有 `HOOK.yaml` 清单和一个 `handler.py`。

📌 "**路径在调用时解析，绝不在 import 时**"这半句是第 6 章那条纪律的同一条：
import 期拿到的 profile home 是**启动 profile 的**，服务第二个 profile 时它就已经错了。

---

## 10. 记忆 provider 集成

当某个记忆 provider 插件（例如 Honcho）启用时：

1. 网关**为每条消息**构造一个带会话 ID 的 `AIAgent`；
2. `MemoryManager` 用会话上下文初始化 provider；
3. provider 的工具（例如 `honcho_profile`、`viking_search`）经这条路被路由：

```text
AIAgent._invoke_tool()
  → self._memory_manager.handle_tool_call(name, args)
    → provider.handle_tool_call(name, args)
```

4. 会话结束/重置时触发 `on_session_end()`，做清理与**最后一次数据提交**。

### 记忆落盘（flush）的生命周期

| 情况 | 会不会"终结"这个会话 |
|---|---|
| **显式对话边界**（`/new`、`/reset`、`/resume`） | ✅ **会**：把即将离开的会话落盘并**终结** |
| **空闲时间、每日边界** | ❌ **不会** |
| **资源型淘汰**（TTL、LRU、内存压力） | ❌ 不会：先把缓存的转写本**提交给配置的记忆 provider**，然后才释放 agent 的客户端；**不关闭持久对话** —— 下一轮会重新加载**同一份**转写本与身份 |

📌 这条区分很关键：**"把东西提交给记忆"和"结束这段对话"是两件事**。
资源压力下前者必须做（否则丢数据），后者绝不能做（否则对话就断了）。

---

## 11. 后台维护

网关在收消息的同时跑周期性维护：

| 维护 | 干什么 |
|---|---|
| **Cron 滴答** | 检查任务计划，触发到点的任务 |
| **会话清理** | 回收缓存资源，**但不结束转写本** |
| **记忆落盘** | 在软缓存淘汰**之前**把记忆提交掉 |
| **缓存刷新** | 刷新模型列表与 provider 状态 |

---

## 12. 进程管理

网关是**长驻进程**，管理方式：

- `hermes gateway start` / `hermes gateway stop` —— 手动控制；
- `systemctl`（Linux）或 `launchctl`（macOS）—— 服务管理；
- PID 文件在 `~/.hermes/gateway.pid` —— **profile 作用域**的进程跟踪。

**profile 作用域 vs 全局**（这一节最容易搞混，逐条列清）：

| 场景 | 行为 |
|---|---|
| **独立部署**（一个 profile 一个网关） | `hermes -p x gateway stop` **只停那个 profile 的**网关 |
| **多路复用**（只有一个网关进程，归默认 profile 所有） | 在默认 profile 上 `hermes gateway stop` → **把所有被服务的 profile 一起停掉**；而对一个被服务的次级 profile 执行 `hermes -p x gateway stop` → **以退出码 78 拒绝**（它没有自己的网关） |
| `hermes gateway stop --all` | 用全局 `ps aux` 扫描杀掉所有网关进程（**更新时用**） |

**存活判据**：由 `gateway.status.live_gateway_pid_for_home` 判定 ——
它比对的是 **PID + 启动时间指纹**，**绝不只是"PID 存在"**。

📌 最后半句是踩出来的：PID 会被复用，只看"这个号有进程吗"会**把别人的进程当成自己的网关**。

---

## 13. 多路复用 profile（`gateway.multiplex_profiles: true`）

开着这个开关时，**一个进程**服务默认 profile **加上** `profiles/` 下每一个活着的目录
（`hermes_cli/profiles.py::profiles_to_serve(multiplex=True)`）。

难点在于：**`os.environ` 和模块全局量里装的是【启动】那个 profile 的值**。
所以每一个"替次级 profile 干活"的动作都必须**显式绑定作用域** ——
记住：**一个 profile = home + 密钥作用域 + 终端作用域，三者一体**。

| 动作 | 绑定点 |
|---|---|
| 被路由的回合 | `gateway/run.py::_profile_runtime_scope(home)`，经 `run_turn.py::_profile_scope_for_source` |
| agent 释放 / 淘汰（TTL、LRU、内存压力） | `gateway/run_agent_cache.py::_run_release_in_profile_scope` |
| 关闭 | `gateway/run_shutdown.py::_finalize_session` ⚠️（见附录 C） |
| 回合后媒体投递 | `gateway/platforms/base.py::_media_delivery_scope` |
| Cron 滴答 | `cron/scheduler_provider.py::_profile_cron_scope(home)`（一个 ticker，顺序跑各 profile） |
| 子进程（`hermes -p X` worker、relay 回合、浏览器驱动） | `tools/environments/local.py::served_profile_child_env` |
| 后台线程 | `agent/memory_provider.py::spawn_context_thread` |

几条**硬规矩**：

- **密钥读取会在多路复用下 fail closed**：只有 `set_multiplex_active(True)` 之后，
  `agent.secret_scope.get_secret` 才会抛 `UnscopedSecretError`；而这个开关由网关、cron、
  `gateway migrate` 以及桌面/仪表盘的 `serve` 后端设置。
- **适配器的 YAML 绝不进 `os.environ`**：`gateway/platforms/_shared.py::apply_yaml_bridge`
  在次级 profile 的作用域下只往 `PlatformConfig.extra` 里塞值、**跳过对环境变量的写入**；
  各种门（gate）通过 `platform_gate_env` 读取。
- **共享入口的平台（WhatsApp bridge、Relay）只在默认 profile 上跑**：
  次级 profile 如果启用了它，会被**记一条日志**并写进运行时状态
  （`run_adapters.py::_note_unserved_secondary_platform`）。

用户视角的逐项隔离清单见《多 profile 网关 · 每个 profile 隔离什么》
（`/user-guide/multi-profile-gateways#what-is-isolated-per-profile`）。

📌 这一节是第 6 章那套"隔离"的**运行期版本**：第 6 章讲"存在哪个库里"，
这一节讲"**这段代码在替谁干活**"。

---

## 相关文档（原文列出）

- 会话存储（Session Storage）
- Cron 内部（Cron Internals）
- ACP 内部（ACP Internals）
- Agent 回合循环（Agent Loop Internals）
- 消息网关（用户指南）

---

## 附录 A · 术语表（本章新增）

| 原文 | 译法 | 说明 |
|---|---|---|
| gateway | 网关（gateway） | 连接 Hermes 与外部消息平台的**长驻进程** |
| adapter | 适配器 | 一个平台一个；把平台事件归一化成 `MessageEvent` |
| MessageEvent | 消息事件 | 归一化之后的入站消息统一形状 |
| session key | 会话键 | `agent:{命名空间}:{平台}:{聊天类型}:{聊天 id}` —— **路由上下文的编码** |
| namespace | 命名空间 | 会话键里那一段；默认 profile 是 `main`，具名 profile 是它的名字（叫 `main` 的被标成 `main~`） |
| message guard | 消息守卫 | "agent 正在跑"时挡住/排队新消息的两道闸（适配器级 + runner 级） |
| authorization | 授权 | 五层检查，**默认拒绝** |
| allowlist / allow-all | 白名单 / 全放行 | 平台级与全局两级开关 |
| DM pairing | 私聊配对 | 已授权用户给新用户发配对码 |
| slash command | 斜杠命令 | `/stop` `/new` `/model` … |
| busy policy | 忙时策略 | `CommandDef.busy_policy` / `busy_handler`：agent 跑着时这个命令怎么办 |
| inline dispatch | 内联分发 | 绕过后台任务、直接 await —— 为了顺序保证 |
| scoped lock / token lock | 作用域锁 / token 锁 | 防两个 profile 用同一个 bot token |
| fail closed | 失败即关闭 | 不确定时选择"拒绝"，不是"放行" |
| home channel | 主频道 | cron 输出与后台结果的默认落点 |
| mirroring | 镜像 | 把消息抄到另一个会话（`send_message`） |
| hook | 钩子 | 响应生命周期事件的 Python 模块（`HOOK.yaml` + `handler.py`） |
| memory provider | 记忆 provider | 外部记忆后端插件（如 Honcho） |
| flush | 落盘 / 收尾提交 | 把缓存的转写本提交给记忆 provider —— **不等于**结束对话 |
| deferred loading | 延迟加载 | 平台 SDK 只在真需要时才 import |
| relay | 中继（relay） | 连接器型平台的通用适配器（出站 WebSocket） |
| receipt | 回执 | `_gateway_accepted`：**真的被接受**才算数，不是"回调被调用过" |
| EX_CONFIG / 退出码 78 | 配置错误退出 | 告诉监督者"别再重启我"的信号 |
| multiplexing | 多路复用 | 一个进程服务多个 profile |
| scoped binding | 作用域绑定 | 每个替次级 profile 干的活都要显式绑定它的 home/密钥/终端 |

---

## 附录 B · 这一章在整套文档里的位置

```text
第 2 章  回合循环            —— 一个 agent 在**自己的进程里**怎么跑
第 3 章  提示词装配          —— 每轮发给模型的那包怎么拼
第 4 章  工具运行时          —— 工具怎么注册 / 被选中 / 什么形状
第 5 章  上下文压缩与缓存     —— 窗口满了怎么办（含**网关侧**那道兜底）
第 6 章  会话存储            —— 上面这些**落在哪**（SQLite）
第 7 章  网关内部 ★本章      —— **外面那层**：谁能进、归哪个会话、怎么活着、怎么发回去
（第 8 章之后：插件 / 技能 / 工具群 —— 仍是占位，未定）
```

**本章收的三笔债**（前面章节挂过来的）：

| 债 | 来自 | 本章对应 |
|---|---|---|
| 跨进程压缩锁 | 第 5/6 章 | §13 的多 profile 绑定 + 第 6 章 `compression_locks` |
| 多 profile 多库 | 第 6 章差距清单 #1 | §12 进程管理 + §13 多路复用 |
| 网关的输入归属（`observed` / 平台消息 id / 异常写入方仲裁） | 第 6 章差距清单 #6 | §3 消息流程（两级守卫）+ §4 授权 |

**谁会用到这一章**：

| 场景 | 用到的节 |
|---|---|
| 想知道"为什么它能在两轮之间干活" | §11 后台维护 + 第 5 章的网关侧会话维护 |
| 加一个新平台 | §7 平台适配器（上游另有 `ADDING_A_PLATFORM.md`） |
| 两个身份/两个 bot 同时在线 | §12 + §13（多 profile 与多路复用） |
| 排查"消息发不出去/发到别处" | §8 交付路径 + §4 授权 + §7 token 锁 |
| 排查"重启循环" | §7 的退出码 78 + §12 的存活判据 |

---

## 附录 C · 上游文档出入（本次核实 1 处）

核对的锚：上游 commit **`f5d1926110`**（v0.21.3）。本次逐条核了原文点名的
**文件归属、函数名、事件表、平台数量、退出码、锁函数、钩子桩**，结论如下。

### ① `_finalize_session` 这个名字在该文件里对不上

- 文档写：关闭时绑定 `gateway/run_shutdown.py::_finalize_session`
- 源码：`gateway/run_shutdown.py:1117` 的实际名字是 **`_finalize_session_off_loop`**
  （同一文件里 :1066 在 await 它）

**影响**：小。按文档去 grep `_finalize_session` 会先撞到别的模块里的同名方法，
但连 `_off_loop` 一起看就能找到。

### 已核实**正确**的声明（别再重查）

| 文档说 | 实测 |
|---|---|
| `gateway/run.py` 是门面，拆出 `run_*.py` 兄弟 | ✅ 实测 **21 个** `gateway/run_*.py` |
| "20+ 个消息平台" | ✅ `plugins/platforms/` 下正好 **22 个**目录 |
| `_session_key_namespace` + "叫 `main` 的 profile 标成 `main~`" | ✅ `gateway/session.py:625-634`，`~` 不在 profile id 字母表的理由写在 docstring 里 |
| `build_session_key()` | ✅ `gateway/session.py:654` |
| 两级守卫（`_active_sessions` / `_pending_messages` / `_running_agents`） | ✅ `platforms/base.py:1884-1885`、`run_inbound.py` |
| `GATEWAY_KNOWN_COMMANDS` / `_command_handler_table` / `_IDLE_COMMANDS` / `_PLAIN_COMMANDS` | ✅ `hermes_cli/commands.py`、`gateway/run_busy.py:758-797` |
| `resolve_command()` 在 `hermes_cli/commands.py` | ✅ `:351` |
| `acquire_scoped_lock()` / `release_scoped_lock()` | ✅ `gateway/status.py:1180` / `:1228` |
| 退出码 78 = `EX_CONFIG` + `is_global_startup_conflict()` | ✅ `gateway/restart.py:14` / `:61` |
| `gateway.wake.admit_internal_event` + `_gateway_accepted` 回执 | ✅ `gateway/wake.py:42` / `:48-50` |
| `_register_builtin_hooks()` 是空壳桩、`builtin_hooks/` 出厂为空 | ✅ `gateway/hooks.py:86-87`；目录里只有 `__init__.py` |
| 钩子事件表（8 个） | ✅ `gateway/hooks.py:5-6` 与表逐项一致 |
| `AIAgent._invoke_tool()` → `_memory_manager.handle_tool_call` | ✅ `agent/inline_tool_executors.py:238`、`agent/tool_executor.py:1569` |
| `gateway.pid` | ✅ `gateway/status.py:158` |
| `live_gateway_pid_for_home`（PID + 启动时间指纹） | ✅ `gateway/status.py:1121` |
| §13 那张绑定表里的 6 个函数 | ✅ 逐个核过定义位置（`run.py:1751`、`run_turn.py:2166`、`run_agent_cache.py:698`、`platforms/base.py:3312`、`cron/scheduler_provider.py:95`、`tools/environments/local.py:345`、`agent/memory_provider.py:29`） |
| `set_multiplex_active` / `UnscopedSecretError` 在 `agent/secret_scope.py` | ✅ `:27` / `:52` |
| `apply_yaml_bridge` / `platform_gate_env` 在 `gateway/platforms/_shared.py` | ✅ `:189` / `:69` |
| `profiles_to_serve(multiplex=True)` 在 `hermes_cli/profiles.py` | ✅ `:802` |
| `register_deferred`（延迟加载器） | ✅ `gateway/platform_registry.py:158` |
| `bridge_core_env_settings` 在 `gateway/config_loader.py` | ✅ |
| `_note_unserved_secondary_platform` 在 `gateway/run_adapters.py` | ✅ |

### 文档没写、但值得知道的两条（实测补上）

1. **`gateway/run.py` 门面拆出的是 21 个兄弟**（原文只列了 8 个 + 省略号）——
   其中 `run_turn*` 自己就占 5 个（`run_turn`、`run_turn_followup_ack`、`run_turn_runner`、
   `run_turn_runner_approval_settle`），说明"一整轮"这件事在真实版里被切得很细。
2. **平台的插件目录不止 `adapter.py` + `plugin.yaml`**：例如 `plugins/platforms/telegram/`
   还有 `inline_picker.py`、`telegram_context.py`、`telegram_entities.py`、`telegram_ids.py`、
   `telegram_network.py` —— 平台相关的**领域知识**（实体解析、id 规则、网络策略）也各自成文件。
