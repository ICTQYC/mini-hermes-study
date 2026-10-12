# 中文精译 · 第 9 章：程序化集成（Programmatic Integration）

> **原文**：`website/docs/developer-guide/programmatic-integration.md`（**216 行**，官方侧边栏 `developer-guide`，`sidebar_position: 8`；
> 副标题逐字：*Three protocols for driving hermes-agent from external programs: ACP, the TUI gateway JSON-RPC, and the OpenAI-compatible HTTP API*）。
> **本篇定位**：精译为主（看懂"要从外部程序驱动 Hermes，官方给的三条协议各是什么、该挑哪条"）。差异与源码深挖留给《源码讲解-09》。
> **📌 行号锚**：真实源码 `$HOME/.hermes/hermes-agent/`，锚上游 commit **`f5d1926110`**（`version = "0.21.3"`）。
> 本篇只用到文件级引用（行号留给源码讲解）。
> **本章出入**：轻量核对 3 处（见附录 C）—— 只核**协议性声明**（方法名 / 端点 / 字段 / 错误码在锚点源码里能否指认），
> 细节不逐条对（学习用，不为细节付成本）。

[TOC]

---

## 0. 这一章在回答什么

一句话：**要把 Hermes 当引擎塞进别人的程序里（IDE 插件、自定义 UI、CI 流水线、内嵌子智能体），官方给了三条协议通道，按你的传输方式和调用方挑一条。**

三条通道（原文总览表，逐字照抄）：

| 协议 | 传输 | 最适合 | 由谁定义 |
|---|---|---|---|
| **ACP** | stdio 上的 JSON-RPC | 已经会说 [Agent Client Protocol](https://github.com/zed-industries/agent-client-protocol) 的 IDE 客户端（VS Code、Zed、JetBrains） | `acp_adapter/` |
| **TUI 网关** | stdio 上的 JSON-RPC（或 WebSocket） | 想要**细粒度**控制会话、斜杠命令、审批与流式事件的自定义宿主 | `tui_gateway/server.py` |
| **API 服务器** | HTTP + Server-Sent Events | OpenAI 兼容前端（Open WebUI、LobeChat、LibreChat…）与语言无关的 Web 客户端 | `gateway/platforms/api_server.py` |

三条通道驱动的都是**同一个 `AIAgent` 内核**。它们的差别只在**线格式**与各自**暴露哪一组功能**。

三个术语，本篇第一次出现就在这里定下（原文没给定义，下列是照原文能指认的机制补的一句）：

- 「ACP」= Agent Client Protocol：Zed Industries 牵头的一套编辑器↔智能体协议，让 IDE 把 Hermes 当成"会干活的后端"接进来 —— 会话创建、提示词、工具调用、权限请求都是它的报文类型，由 `acp_adapter/` 实现。
- 「TUI 网关」= `tui_gateway/server.py` 定义的那套 JSON-RPC 协议；Hermes 自己的 Ink TUI（`hermes --tui`）和桌面仪表盘的 PTY 桥讲的正是它。
- 「API 服务器」= 把 Hermes 包成 OpenAI 形状的 HTTP 服务（`/v1/chat/completions` 等），任何"已经会说 OpenAI 格式"的客户端都能直接用。
- 「线格式」= wire format：同一内核对外说哪种报文格式。

---

## 1. ACP (Agent Client Protocol) —— 编辑器接入协议

`hermes acp` 启动一个说 ACP 的 stdio JSON-RPC 服务端。生产环境里用的是 VS Code（Zed Industries 的 ACP 扩展）、Zed，以及任何装了 ACP 插件的 JetBrains IDE。

暴露的能力：会话创建、提示词提交、流式的智能体消息分片、工具调用事件、权限请求、会话分叉（fork）、取消，以及认证。工具输出会被渲染成 IDE 认识的 ACP `Diff` / `ToolCall` 内容块。

完整生命周期、事件桥与审批流见：[ACP Internals](./acp-internals)。

```bash
hermes acp                  # 在 stdio 上提供 ACP 服务
hermes acp --check          # 校验 ACP 依赖与 adapter 导入
hermes acp --setup          # 为 ACP 的终端认证跑交互式 provider/model 配置
```

---

## 2. TUI Gateway JSON-RPC —— 细粒度宿主协议

`tui_gateway/server.py` 就是 Ink TUI（`hermes --tui`）与内嵌仪表盘 PTY 桥所讲的那套协议。任何外部宿主都可以在 stdio 上讲同一套协议（或经 `tui_gateway/ws.py` 走 WebSocket）。

### 方法目录（节选）

```
prompt.submit           prompt.background       session.steer
session.create          session.list            session.active_list
session.activate        session.close           session.interrupt
session.history         session.compress        session.branch
session.title           session.usage           session.status
clarify.lock            config.set / config.get commands.catalog
command.resolve         command.dispatch        cli.exec
reload.mcp              reload.env              process.stop
delegation.status       subagent.interrupt      subagent.steer
spawn_tree.save / list / load
terminal.resize         clipboard.paste         image.attach
```

`session.active_list`、`session.activate`、`session.close` 是 TUI 的会话切换器用的**进程内活动会话**控制。要发现**已保存的**对话记录，用 `session.list` / `/resume`；活动会话这几个方法只对**当前在这个 TUI 网关进程里开着**的会话有效。

在**同一个已认证的网关**里，恢复或激活一个活动会话是**再挂一个事件订阅者**，而不是替换掉先前那条连接。流式事件与终端事件会发给**所有**已挂上的客户端；断开一个客户端**不会**结束另一个客户端正在看的会话。既有的提交独占性与已配置的忙碌输入策略继续有效。已挂上的客户端可以导引该会话的子智能体；浏览器控制器的结果仍然要求是**注册了那个控制器的那条连接**。这**不**意味着不同网关进程可以写同一个会话，也**不**意味着 owner 重启期间提示词准入是持久的。

### 在 `prompt.submit` 上回写历史（rewind）

一次 rewind / 编辑 / 重新生成，本质就是一次 `prompt.submit`：它在跑新的一轮之前**丢掉已存记录的一部分**。因为这是一次**对会话持久行的破坏性重写**，网关只在客户端**明说意图**时才照做：

| 参数 | 含义 |
|---|---|
| `truncate_before_user_ordinal` | 从哪一轮用户回合切开（**从 0 开始**的序号）。从该回合起往后全部丢弃。纯展示用的时间线行（`display_kind`）不计数。必须是真正的整数 —— JSON 布尔值会被拒，错误码 `4004`。 |
| `truncate_before_row_id` | 目标用户回合的整数 SQLite 行 ID（`messages.id` / `row_id`），从它切开。**首选的持久地址**。序号与行 ID 同时给出时，网关会校验两者一致（不一致返回 `4030`）。未知 / 过期的行 ID 会被拒（`4018`）—— **不会**回退到序号。 |
| `confirm_truncate` | 只要发了序号、消息 ID 或行 ID 就**必须**带。它声明这次提交确实是一次 rewind，而不是一次恰好带了残留参数的普通发送。只发它、不带目标 → 拒，错误码 `4004`。 |
| `confirm_empty_truncate` | 当这次截断会把记录清空（序号 `0`）时**额外必须**带。 |

带了截断参数却没有 `confirm_truncate` → 拒绝，错误码 `4004` 或 `4029`，且**什么都没写**。实现 rewind 的宿主必须在用户提出请求的**那一刻**设好该标志，并且**绝不能**让截断参数跨普通提交残留在状态里。优先用 `truncate_before_row_id`（取自 resume 的 `row_id` / `_row_id`）；只有在还没有持久 id 可用时，才把序号当作**向后兼容 / 乐观行**路径保留。

在一次针对持久会话成功的截断提交上，`prompt.submit` 的结果会额外带上 `survivor_user_row_ids` —— 即重写之后**幸存用户回合**的新行 ID，按可见用户序号排列。因为这次重写是把保留的前缀**作为新行重新插入**，所以宿主在 rewind 之前缓存的每个行 ID 事后都**过期**了；请用这个列表重新绑定缓存 ID（某条目为 `null` 表示该回合没有持久 ID —— 就丢掉缓存的那一个），否则下一次针对某个更早的幸存回合的 rewind 会被 `4018` 拒掉。

### 事件回推

`message.delta`、`message.complete`、`tool.start`、`tool.generating`、`tool.complete`、`gateway.ready`、`request.cancel`，外加会话生命周期事件与错误事件。

### 服务端→客户端请求（智能体来问你的那些）

审批、澄清问题、sudo / 密钥提示、保险库解锁、MCP 配置，以及桌面端的读 / 操作桥，都是**网关发给客户端的 JSON-RPC 请求**，不是事件。帧里带字符串 id，客户端用带同一 id 的普通 JSON-RPC 响应作答：

```
← {"jsonrpc":"2.0","id":"srq-7","method":"approval","params":{"session_id":"…","request_id":"…","command":"rm -rf build","description":"…"}}
→ {"jsonrpc":"2.0","id":"srq-7","result":{"choice":"once"}}
```

方法：`approval` → `{choice}`；`clarify` → `{answer}`（单问）或 `{answers}` / `{}` 取消（批量，可用 `clarify.lock` 提前锁住一个答案）；`sudo`、`secret`、`vault.code`、`vault.unlock_prompt` → `{value}`；`connection` → `{settled_by, targets}`（即 `manage_connections` 卡片：每个目标一个结果）；`terminal.read`、`window.read`、`preview.act`、`tour` → `{value}`（JSON 文本）。你的宿主没实现某个方法，就用一条 JSON-RPC 错误（`-32601`）回应，让智能体**快速失败**，别耗到超时。

当网关撤回一个问题（超时、被打断、或在别的界面被答了），它会发出 `request.cancel` `{ id, method, reason }`；只清掉**匹配**的那个提示。`session.resume` / `session.activate` 的结果与 `session.events.since` 会带上 `open_requests` —— 即那些**还开着的**帧 —— 让重连的客户端能把它们重新渲染出来（并且仍然可以作答）。

### 重连时重建进行中的回合

`session.resume` / `session.activate` 的结果会带上 `inflight` —— 那个**还在跑**（或保留下来的失败）而历史里还没有的回合：`user`、迄今已流出的 `assistant`、`streaming`、回合中途的 `corrections`，以及错误字段。当这个回合是**网关**发起、而不是人打出来的（后台进程完成、异步委派结果、隐藏的脚手架提示），`inflight` 还会带上即将落到持久化 `messages` 行上的同一份 `display_kind` / `display_metadata`，这样客户端渲染"实时提示"的样子与回合落地后渲染历史的样子**完全一致** —— `process_complete` 对应一个带 `display_metadata.display_text` 的时间线标记，`hidden` 则什么都不显示。这两个字段在**真实用户输入**时都不存在；**永远不要**从提示文本去推断来源（用户在引用一个标记字符串，那仍然是用户）。

### Pi 风格 RPC 映射

Pi-mono RPC 规范里的每条命令（[issue #360](https://github.com/NousResearch/hermes-agent/issues/360)）都有对应的 TUI 网关等价物：

| Pi 命令 | Hermes 等价物 |
|---|---|
| `prompt` | `prompt.submit`（或 ACP 的 `session/prompt`） |
| `steer` | `session.steer` |
| `follow_up` | 排在当前回合之后的 `prompt.submit` |
| `abort` | `session.interrupt` |
| `set_model` | `command.dispatch` 执行 `/model <provider:model>`（会话中途、持久生效） |
| `compact` | `session.compress` |
| `get_state` | `session.status` |
| `get_messages` | `session.history` |
| `switch_session` | `session.resume` |
| `fork` | `session.branch` |
| `ui_request` / `ui_response` | 服务端→客户端请求 `clarify` / `sudo` / `secret` / `approval`，用 JSON-RPC 响应帧作答 |

---

## 3. OpenAI-Compatible API Server —— OpenAI 兼容的 HTTP 出口

`gateway/platforms/api_server.py` 把 Hermes 以 HTTP 暴露出去，给任何已经会说 OpenAI 格式的客户端用。当你想要一个 Web 前端、一个 curl 驱动的 CI 跑手，或者一个非 Python 的消费方时，它就有用。

端点：

```
POST /v1/chat/completions        OpenAI Chat Completions（经 SSE 流式）
POST /v1/responses               OpenAI Responses API（有状态）
POST /v1/runs                    启动一次 run，返回 run_id（202）
GET  /v1/runs/{id}               run 状态
GET  /v1/runs/{id}/events        生命周期事件的 SSE 流
POST /v1/runs/{id}/approval      解决一个待审批
POST /v1/runs/{id}/steer         在下一个工具边界注入运行中途的指引
POST /v1/runs/{id}/stop          打断这次 run
GET  /v1/capabilities            机器可读的功能开关
POST /v1/browser-control/register 注册一个浏览器控制器
GET  /v1/browser-control/ws      浏览器控制器 WebSocket
GET  /v1/models                  列出 hermes-agent
GET  /api/model/options          供应商感知的选择器清单
GET  /health, /health/detailed
```

配置、请求头（`X-Hermes-Session-Id`、`X-Hermes-Session-Key`）与前端接线见：[API Server](../user-guide/features/api-server)。

浏览器扩展可以选择加入这个**默认关闭**的控制器协议，去驱动**当初打开这条 Hermes 对话的那个浏览器会话**本身。API 与仪表盘两条传输共享同一个**主体绑定的 broker**和一份显式的**能力白名单**；见 [Browser-extension control](../user-guide/features/api-server#browser-extension-control)。

### 模型目录的多个出口

OpenAI 兼容 API **刻意**把 `GET /v1/models` 保持得极小：它是前端所期待的**兼容性端点**，不是 Hermes 完整供应商 / 模型选择器的目录。

如果某个外部控制面需要 Hermes 精挑的供应商行、逐模型定价或能力提示，就用下面这些**已认证**的选择器出口之一：

- API 服务器 REST：`GET /api/model/options`，带 API 服务器的 bearer key
- 仪表盘后端 REST：`GET /api/model/options`，带 `X-Hermes-Session-Token`
- TUI 网关 RPC：`model.options`

这些出口共享同一套 payload 构造器，以及同一条**自定义供应商探测策略**：

- 普通打开：只探测当前的**那一个**自定义供应商，这样离线的已保存端点不会把选择器卡住。
- 显式刷新（`refresh=1` 或 `refresh: true`）：清掉供应商-模型缓存，并探测**所有**已保存的自定义供应商，让实时目录完整重填。

要 OpenAI 客户端兼容，用 `/v1/models`。要构建 Hermes 感知的模型选择器，用 `/api/model/options` 或 `model.options`。

`POST /v1/runs/{id}/steer` 是 Hermes `/steer` 的 HTTP 等价物：它**不**新建用户回合，也**不**立刻改写已经飞在路上的助手输出。它是把文本追加到这次 run 上，在**下一个工具边界**之后才对智能体可见，于是它能纠偏而不丢掉当前这轮工具调用循环。

`/v1/runs/{id}/steer` 只在 run 状态为 `running` 时被接受。排队中、审批暂停、正在停止、已取消、已失败、已完成的 run 都返回 `409 run_not_accepting_steer`，即使服务器在协作式关停期间还留着一份内部智能体引用。

一个 `200`（以及 `run.steered` 事件）只意味着文本被**排队**了，不意味着智能体已经消费了它。如果一次 steer 落在智能体最终响应**之后** —— 后面再没有工具边界能把它递出去 —— 未递达的文本会作为 `pending_steer` 出现在终止事件（`run.completed`、`run.failed` 或 `run.cancelled`）与 run 状态上，客户端可以把它当成下一个用户回合重放，而不是丢掉。

#### 终端 run 状态

一次 run 的终止状态**取决于智能体那一回合实际是怎么结束的**，且终止事件名始终与之匹配（`run.<status>`）：

| 回合结果 | 状态 | 终止事件 | 事件 / 状态上的标志 |
|---|---|---|---|
| 产出了最终答复 | `completed` | `run.completed` | `completed: true` |
| 被打断（`/stop`，或智能体内部的打断） | `cancelled` | `run.cancelled` | `completed: false`、`interrupted: true`、`turn_exit_reason` 指明发起者 —— 人按停止是 `interrupted_by_user`；看门狗（例如 `cron_inactivity_watchdog`、`turn_liveness_watchdog`、`gateway_inactivity_watchdog`、`session_turn_lease_lost`）结束回合时是 `interrupted_by_system(<issuer>)` / `interrupted_during_api_call(<issuer>)` |
| 供应商 / 智能体失败 | `failed` | `run.failed` | `completed: false`、`error` |
| 没干完就结束（迭代预算用尽、回复被截断或不完整） | `failed` | `run.failed` | `completed: false`、适用时带 `partial`、`turn_exit_reason`（例如 `max_iterations_reached(60/60)`）、带任何兜底文本的 `output` |

一次 run **绝不会**在同一个 payload 里被报成 `completed` 又带 `completed: false`，或带 `partial: true`。同一条规则适用于 `/api/sessions/{id}/chat/stream`：它的 `assistant.completed` payload 带真实的 `completed` / `partial` / `interrupted` 标志，而它的终止事件是 `run.completed`、`run.failed` 或 `run.cancelled`。

---

## 4. Which one should I use? —— 该选哪一个

- **你在写 IDE 插件，且这个 IDE 已经会说 ACP** → ACP。IDE 侧零协议工作量。
- **你在写自定义的桌面 / Web / TUI 宿主，想要 Hermes 的每一项功能**（斜杠命令、审批、澄清、多智能体、会话分叉） → TUI 网关 JSON-RPC。
- **你想要任何 OpenAI 兼容前端、语言无关的 HTTP 客户端，或 curl 驱动的自动化** → API 服务器。
- **你想要不启子进程的 Python 进程内嵌入** → 直接 `import run_agent.AIAgent`。见 [Agent Loop](./agent-loop)。

---

## 5. Model hot-swapping —— 会话中途换模型

会话中途换模型在**每个界面**上都能用 —— 底层就是 `/model` 斜杠命令。

- **CLI / TUI：** `/model claude-sonnet-4` 或 `/model openrouter:anthropic/claude-sonnet-4.6`
- **TUI 网关 RPC：** `command.dispatch`，参数 `{"command": "/model claude-sonnet-4"}`
- **ACP：** IDE 把斜杠命令当提示词发过去；智能体自己派发它
- **API 服务器：** 在请求体里带上 `model` 字段

**供应商感知的解析**（同一个模型名，会按你当前在哪家供应商上选对格式）是内建的。见 `hermes_cli/model_switch.py`。

---

## 6. A note on `--mode rpc` —— 关于 `--mode rpc` 的说明

Hermes **没有** `--mode rpc` 这个开关。上面三条协议已经覆盖了这些用例 —— IDE 协议客户端用 ACP，stdio JSON-RPC 宿主用 TUI 网关，HTTP 用 API 服务器。如果你确实找到一个它们都填不上的真实缺口，就带着你正在构建的**具体消费方**去开一个 issue。

---

## 附录 A · 术语表

| 术语 | 原文 | 一句话意思 |
|---|---|---|
| 线格式 | wire format | 同一个内核对外说哪种报文格式（ACP / TUI 网关 JSON-RPC / OpenAI HTTP） |
| ACP | Agent Client Protocol | 编辑器↔智能体协议（Zed Industries 牵头）；IDE 侧已有实现可直接接，Hermes 侧由 `acp_adapter/` 实现 |
| TUI 网关 | TUI gateway | `tui_gateway/server.py` 定义的那套 JSON-RPC 协议；Hermes 自家 TUI 与仪表盘 PTY 桥用它 |
| 斜杠命令 | slash command | 以 `/` 开头的命令（如 `/model`、`/steer`）；网关侧用 `command.dispatch` 派发 |
| 序数 | ordinal | 用户回合的编号；**可见用户序号**只数真正的用户回合，纯展示的时间线行不计入 |
| 行 ID | row ID | SQLite `messages` 表里的持久行号，rewind 的**首选**地址 |
| rewind | rewind / edit / regenerate | 丢掉一部分已存记录后重跑一轮；因为是**破坏性重写**，必须显式确认 |
| inflight | in-flight turn | 还在跑、历史里还没有的那个回合；重连时靠它把界面补回来 |
| 主体绑定的 broker | principal-bound broker | 先认"是谁"再放行的中间层；浏览器控制器与仪表盘共享它 |
| 导引 | steer | 不新建用户回合，把文本挂到正在跑的 run 上，下一个工具边界才生效 |
| 终端认证 | terminal auth | 编辑器侧通过 `hermes acp --setup` 这类**终端命令**完成的供应商 / 模型配置流程 |
| `ToolCall` / `Diff` 内容块 | ACP `Diff` / `ToolCall` content blocks | ACP 里工具输出的两种渲染载体，IDE 认识它们 |
| Pi-mono RPC 规范 | Pi-mono RPC spec | 一份外部 RPC 命令清单；本篇给的是它与 TUI 网关方法的逐条对应表 |

---

## 附录 B · 本篇提到的实现文件（锚 `f5d1926110`，实测规模）

行数一律 `git show f5d1926110:<路径> | wc -l` 实测。

| 文件 | 实测 | 角色 |
|---|---|---|
| `acp_adapter/` | **14 个文件 / 4163 行** | ACP 适配器包（`entry.py` 里就是 `--check` / `--setup` 两个开关） |
| `tui_gateway/server.py` | 3243 行 | TUI 网关 JSON-RPC 主服务（§2 的主角） |
| `tui_gateway/ws.py` | 407 行 | 同一协议的 WebSocket 传输（§2 提到的那条"或 WebSocket"） |
| `gateway/platforms/api_server.py` | 4056 行 | OpenAI 兼容 HTTP 服务器（§3 的主角） |
| `gateway/platforms/api_server_runs.py` | 948 行 | `/v1/runs` 一族路由（含 steer 的 `409 run_not_accepting_steer`），由 `api_server.py` 运行期转发 |
| `hermes_cli/model_switch.py` | 1679 行 | 共享 `/model` 切换流水线（§5 指到这里） |
| `run_agent.py` | 1581 行 | §4 最后一条的进程内嵌入入口（`from run_agent import AIAgent`） |

---

## 附录 C · 本篇与上游源码的出入（轻量核对，2026-10-12）

本篇的**协议性声明**在锚点 `f5d1926110` 上基本都能逐条指认（方法目录、`4004`/`4018`/`4029`/`4030` 错误码、`survivor_user_row_ids`、`pending_steer`、`inflight`、`open_requests`、`X-Hermes-Session-Id` / `-Key` 请求头、`run_not_accepting_steer`、`model.options`、`spawn_tree.*` 全部命中）。
三处值得记下的：

1. **§3 的端点清单是"节选"，不是全集** —— 文档没声称穷尽（`GET /v1/capabilities` 本身就是"机器可读的功能开关"，会回吐完整路由表）。
   实测锚点 `APIServerAdapter._http_route_table` 里还有文档未列的 `/v1/skills`、`/api/sessions`、`/api/jobs` 等。**不算错，只是别把它当全集。**

2. **`gateway/platforms/api_server.py` 这个"文件"比文档暗示的大** —— 文档把 API 服务器整体挂在它名下（文件级指认**没错**），
   但 `/v1/runs` 一族（含 §3 讲的 steer 与 `409 run_not_accepting_steer`）实测已拆到兄弟模块 `gateway/platforms/api_server_runs.py`（948 行），
   由 `api_server.py` 通过 `_run_route_delegate` 运行期转发。**读源码时别只开一个文件。**

3. **`#### 终端 run 状态` 表里那四个看门狗名字 —— 已核实（结论：那是示例名，不是常量）** ——
   文档写的是 "e.g."，实测锚点上：`cron_inactivity_watchdog` 有命中（2 个文件）、`turn_liveness_watchdog` **只出现在测试夹具里**
   （`tests/agent/test_turn_iteration_prep.py:87`），而 `gateway_inactivity_watchdog` 与 `session_turn_lease_lost`
   **一个字面命中都没有**。让 issuer 真正有值的是 `agent/interrupt_control.py:27-32 interrupt_issuer()`：
   它读 `agent._tool_interrupt_reason`，没有原因（= 人停）就返回 `None`，否则把原因串的**空格换成下划线**
   （docstring 逐字：*"Slug of the system producer behind the pending interrupt, or `None` for a human stop."*）；
   消费点在 `agent/turn_iteration_prep.py:333-334` 与 `:442-444`：`interrupted_by_system(<issuer>)` /
   `interrupted_during_api_call(<issuer>)`。
   → **issuer 是运行期由"系统生产者"提供的值，不是每个看门狗一个常量** —— 所以要找"谁打断了这一轮"，别去搜常量表，去追
   `_tool_interrupt_reason` 的**赋值点**。
