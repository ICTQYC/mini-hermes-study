# 源码讲解 · 第 9 章：程序化集成（Programmatic Integration）

> **一句话**：要把 Hermes 当引擎塞进别人的程序里（IDE 插件、自定义 UI、CI 流水线、内嵌子智能体），
> 官方给了**三条协议通道**，按你的传输方式和调用方挑一条。
> —— 《中文精译-09-ProgrammaticIntegration》§0

> All three drive the same `AIAgent` core. They differ only in wire format and which set of features they expose.
> —— 官方原文 `programmatic-integration.md:21`

> **原文**：`website/docs/developer-guide/programmatic-integration.md`（官方 216 行）
> **源码锚**：`~/.hermes/hermes-agent/`，commit `f5d1926110`（`version = "0.21.3"`）。
> 本文所有 `文件:行号` 都对齐这一个 commit；**没实际读到的行号一律不写**（只写文件路径）。

**口径说明**

- 每节固定四块，顺序不变：**① Hermes 怎么做的**（散文把机制讲透）→ **② 读 Hermes 的代码**（只引一段 ≤15 行，优先引注释）→ **③ mini 的实现（现状）** → **④ 与真实版的差别**。
- **mini 栏口径**：`已对齐` / `✅ 已抄（<commit>）` / `⬜ 没做（量级）` / `⏭ 不抄（理由）` / **`⏭ 本章不实现（用户 2026-10-12）`**。
- **本章的特殊性**：用户 2026-10-12 决定 mini 不实现本章（「第九章好像确实不需要怎么实现，实现可以留后面」）。所以**③ 块多为「mini 没有对外接口」**，**④ 块多为「⏭ 本章不实现（用户 2026-10-12）」**——决定与理由集中写进 **§十一 差距清单**（含 A1~A11 / B1~B9 的形状）。
- 三条落点里只有第三条（OpenAI 兼容 API 服务）**自包含**：另两条要第三方客户端（ACP 的 IDE、网关的宿主），第三条只要 `curl` 即可真跑。**本章的实现候选就选它**。
- 行数一律 `wc -l` 口径；中文；术语首次出现给定义。

**规模（锚 commit `f5d1926110` 实测）**

| 侧 | 路径 / 量 | 规模 | 角色 |
|---|---|---|---|
| 真实 | `acp_adapter/` | **14 文件 / 4,163 行** | 路线 ① ACP 适配器包（`server.py` 1,037 + `tools.py` 840 占 45%） |
| 真实 | `tui_gateway/`（顶层） | **72 文件 / 28,517 行** | 路线 ② TUI 网关 JSON-RPC 主服务 |
| 真实 | `tui_gateway/contracts/` | **17 文件 / 6,898 行** | 类型化契约 / 入参校验（**另计**，顶层 72 文件不含它） |
| 真实 | `tui_gateway/` 注册方法 | **约 124 个**（`@method` / `@_session_method`，22 个 `methods_*.py`） | 一张 `dispatch()` 撑起的全部 RPC 面 |
| 真实 | `gateway/platforms/api_server.py` | **4,056** | 路线 ③ OpenAI 兼容 HTTP 主适配器（`APIServerAdapter`） |
| 真实 | `gateway/platforms/api_server_openai_routes.py` | **1,051** | `OpenAICompatRoutesMixin`：`/v1/chat/completions` · `/v1/responses` |
| 真实 | `gateway/platforms/api_server_runs.py` | **948** | `/v1/runs` 一族（含 steer / 幂等键） |
| 真实 | `website/docs/developer-guide/programmatic-integration.md` | **216** | 对应原文档 |
| 真实 | `gateway/platforms/` 下 `web_server.py` 一族 | 另算 | 仪表盘 HTTP 面（与本章无关） |
| mini | `gateway/`（`adapters/` 只有 `console.py`） | **0 行对外接口**（`grep ThreadingHTTPServer\|http.server\|jsonrpc` 零命中） | 只有消息入站路径，无 HTTP / stdio 协议面 |

**节 ↔ 精译对照**

| 本章节 | 《中文精译-09》对应 |
|---|---|
| 导言 · 机制总览 | §0 这一章在回答什么 |
| §〇 差异表 | 附录 C · 本篇与上游源码的出入 |
| §一 三条通道的全景（同一内核、三种线格式） | §0 三条通道总览表 · §4 该选哪一个 |
| §二 ACP：薄翻译壳与接缝清单 | §1 ACP (Agent Client Protocol) |
| §三 ACP 的审批与编辑守卫（fail-closed + 线程局部） | §1 ACP（权限请求 · `Diff` / `ToolCall` 内容块） |
| §四 一个 dispatch + 多种传输 | §2 TUI Gateway（开头 · 方法目录） |
| §五 一个回合的链路（ack 后线程跑 + 事件流） | §2 事件回推 |
| §六 history 回写 = rewind | §2 在 `prompt.submit` 上回写历史（rewind） |
| §七 重连三元组 | §2 重连时重建进行中的回合 · 服务端→客户端请求 |
| §八 OpenAI 兼容服务（路由表 / 两个目录面 / SSE） | §3 OpenAI-Compatible API Server（含终端 run 状态） |
| §九 模型热切换 | §5 Model hot-swapping |
| §十 真问题与收据 | （精译无对应正节 —— 取证纪事） |
| §十一 差距清单 | 附录 B · 附录 C |
| 附录 A 引用总表 | （精译无对应） |
| 附录 B 三方对照矩阵 | 附录 B · 本篇提到的实现文件 |
| 附录 C 上游出入 | 附录 C · 本篇与上游源码的出入 |
| （本章无对应） | §6 A note on `--mode rpc` · 附录 A 术语表 |

[TOC]

---

## 导言 · 机制总览

这一章要回答的问题只有一件：**「一个外部程序想驱动 Hermes，该从哪扇门进去、那扇门长什么样」**。三条通道——ACP（编辑器协议）、TUI 网关 JSON-RPC（细粒度宿主协议）、OpenAI 兼容 API（HTTP 出口）——服务三种完全不同的消费者，触发时机、进程模型、线格式、暴露的能力集都不一样。但它们**没有一个自带大脑**：三条线全部把请求翻译成同一种东西后，交给**同一个 `AIAgent` 内核**去跑，自己只负责「接线 + 翻译 + 把内核吐出来的东西按各自的线格式发出去」。这条「同一内核、三种线格式」的分工，就是整章要守住的那条接缝。

下面七小节按「谁 / 何时 / 做什么 / 什么不能破 / 出错怎么办 / 谁调它 / 顺序」把这三条通道拆开；每条都落到真实源码的 `文件:行号` 与**逐字注释**（锚 `f5d1926110`）。

### 0.1 谁在管（三条通道各自的入口）

三条通道各有**独立的进程入口**，互不复用：

| 通道 | 入口文件 | 命令 / 脚本 | 传输 |
|---|---|---|---|
| ACP | `acp_adapter/entry.py` | `hermes acp`（`hermes_cli/subcommands/acp.py:13`）· `hermes-acp`（`pyproject.toml:415`） | stdio（唯一） |
| TUI 网关 | `tui_gateway/entry.py` · `tui_gateway/ws.py:266 handle_ws` | `python -m tui_gateway.entry` / WebSocket `/api/ws` | stdio 或 WebSocket |
| OpenAI 兼容 | `gateway/platforms/api_server.py` | `hermes gateway`（`.env` 里 `API_SERVER_ENABLED=true`） | HTTP / SSE |

逐字引（`acp_adapter/entry.py:3-4`，模块 docstring）：

> Loads ``~/.hermes/.env``, routes logging to stderr (stdout is reserved for ACP JSON-RPC), and starts the ACP agent server.

逐字引（`tui_gateway/ws.py:1-2`，模块 docstring）：

> WebSocket transport for the tui_gateway JSON-RPC server: reuses :func:`tui_gateway.server.dispatch` verbatim so every RPC, slash command, approval flow and agent event takes the same handlers as Ink over stdio.

逐字引（`hermes_cli/config_defaults.py:2880-2881`，`API_SERVER_KEY` 说明）：

> Bearer token for API server authentication. Required whenever the API server is enabled; server refuses to start without it.

**要害**：三条通道**都在内核之外**——它们不新增模型目录、不新增凭证库、不新增会话存储（见 §0.6）。谁是「管事的」，不是某一个类，而是「某一条把内核包起来的入口 + 一张把内核事件翻成线格式的表」。

### 0.2 什么时候（谁在什么时候启进程 / 连端口）

三种生命周期，正好对应三种拓扑：

- **ACP：编辑器是父进程，为一次编辑会话 spawn 一个子进程**。一个 `hermes acp` 进程只持**一个**客户端连接（`acp_adapter/server.py:251`），但进程内可并发多会话——`_executor` 是 4 线程池。逐字引（`server.py:45`）：
  > `# Runs the synchronous AIAgent off the event loop.`
  （同步内核跑在 worker 线程，asyncio 事件循环留在主线程。）
- **TUI 网关：宿主启动时 spawn 一个长驻后端**。Ink TUI（`hermes --tui`）走 stdio；Desktop / 网页看板连 WebSocket `/api/ws`（`hermes_cli/web_routers/chat_ws.py:573`）。逐字引（`chat_ws.py:568-571` 注释）：
  > both transports bind to the same session id, so agent emits fan out to both sinks
- **OpenAI 兼容：网关进程长驻，客户端按 HTTP 请求连一次**。端口默认 `127.0.0.1:8642`，配置项 `API_SERVER_PORT`（`config_defaults.py:2883-2884`）。

**要害**：ACP / 网关是「**连一条长命通道**」（stdio 或 WS），API 是「**每次请求连一次**」。这条差别决定了前者要处理断线重连（§七），后者不用。

### 0.3 做什么（三条通道各把内核暴露成什么形状）

三道接口对**同一内核**做三种不同的投影：

| 通道 | 消息 | 事件 | 权限 | 流式 |
|---|---|---|---|---|
| ACP | `agent_message_chunk`（按 `messageId` 归组） | `session_update` 通知 | `request_permission` + `PermissionOption` | 分片经 `AssistantMessageIdAllocator` 归组 |
| TUI 网关 | `prompt.submit` / `session.*` 等 RPC | JSON-RPC **notification** `method:"event"` | **反向 server→client 请求**（`approval` / `clarify` / `sudo`…） | `message.delta` → `message.complete` |
| OpenAI 兼容 | `POST /v1/chat/completions` | `/v1/runs/{id}/events` 的 SSE | `POST /v1/runs/{id}/approval` | SSE `data: …` 行 |

逐字引（`acp_adapter/events.py:130-131`，`AssistantMessageIdAllocator` docstring）：

> Allocates stable per-message ids for streamed assistant chunks.
> ACP clients group streamed ``agent_message_chunk`` / ``agent_thought_chunk`` deltas into one assistant reply by ``messageId`` …

逐字引（`gateway/platforms/api_server_openai_routes.py:412`）：

> POST /v1/chat/completions — OpenAI Chat Completions format.

**要害**：三条线都**没有自己的「回合模型」**——ACP 的 `prompt` 参数、网关的 `prompt.submit`、API 的 `messages[]` 最后都被折成一次 `agent.run_conversation(...)`。差别只在「请求怎么进来、事件怎么出去」。

### 0.4 不变式（三条通道都必须守的四条线）

四条线，破哪条都会让「内核的正确性」在边缘漏掉：

1. **角色交替**。持久历史里若出现 `user;user`，恢复时会被修复。逐字引（`acp_adapter/session.py:352-353`）：
   > `# repair_alternation: this list becomes the resumed agent's LIVE conversation; a durable`
   > `# ``user;user`` violation in state.db would otherwise re-fire the pre-request repair every request.`
2. **审批 fail-closed**。任何拿不准的审批结果都落回 `deny`。逐字引（`acp_adapter/permissions.py:15-17`）：
   > `# ACP permission option id -> Hermes approval result. Ids are stable across the`
   > `# ``allow_permanent=True`` and ``False`` paths even though the option list differs.`
3. **会话归属**。transcript 由**服务器/库**拥有；客户端发来的历史只作提示上下文，**不原样落库**。破坏性重写必须有**独立 consent 位**。逐字引（`tui_gateway/methods_prompt.py:655-659`）：
   > `# An ordinal/id alone is not consent. A client that carries a leftover ordinal into an ORDINARY`
   > `# submit sends a request that is indistinguishable, field by field, from a real rewind …`
   > `# the cut it asks for is a destructive replace_messages() the user never requested`
4. **UI 通知失败不能杀死回合**。发通知是 fire-and-forget，失败只记日志。逐字引（`acp_adapter/server.py:260`）：
   > ``session_update`` that logs instead of raising; False on failure.

（相关约束在前面的章节已讨论：**提示词字节稳定**是第 5 章的既有约束——边缘通道不得每请求重建系统提示词；**角色交替**是第 6 章的约束——mini 严格校验、真实版宽容修复。）

**要害**：前两条是「**向内不破坏内核**」，后两条是「**向外不让边缘故障拖垮内核**」。四条线合起来一句话：**持久状态由服务端按契约管理；边缘可以经显式授权的会话操作修改它，不能把任意客户端历史直接当成权威记录。UI 通知失败应隔离，但用户明确取消仍应中断回合。**

### 0.5 出错（协议错误 / 审批未知 / 客户端发送失败怎么收场）

三种收场，**没有一种靠静默兜底**：

- **审批未知或超时 → 一律拒绝**。未知 `option_id` 落 `deny`。逐字引（`acp_adapter/permissions.py:71-73`）：
  > `logger.warning("Permission request returned unknown option_id: %s", outcome.option_id)`
  > `return "deny"`
  超时与显式拒绝**分开**表达。逐字引（`permissions.py:115-117`）：
  > `# Distinct from an explicit deny: tools.approval reports "timed out`
  > `# without user response" instead of a user denial.`
  > `return "timeout"`
  调度失败 / 异常（`response is None`）→ `deny`（`permissions.py:119-120`）。
- **客户端 UI 发送失败 → 非致命**。`_send` 只 log 不 raise（`server.py:260`）；流式回调走 `safe_schedule_threadsafe`，失败仅 `logger.debug`（`events.py:51-52`，逐字「Fire-and-forget an ACP session update from a worker thread.」）。
- **协议错误码**分工明确：
  - `-32601`：未知方法。逐字引（`tui_gateway/rpc_dispatch.py:22-23`）：
    > `return _err(rid, -32601, f"unknown method: {method} — the client and the Hermes backend are out of sync "`
    > `"（different versions）; run `hermes update` and restart both"`（message 原文为英文，此处按源码逐字）
    ACP 侧的良性探针（`ping` / `health` / `healthcheck`）也回 `-32601`，由 `entry.py:35` 的 `_BenignProbeMethodFilter` 只压掉它的 traceback 噪声。
  - `-32700`：JSON 解析失败（`tui_gateway/entry.py:310`）。
  - `4000`：入参契约校验失败（`rpc_dispatch.py:29-31`）。
  - `4029`：破坏性截断缺 `confirm_truncate`（`methods_prompt.py:267`）。
  - `409 run_not_accepting_steer`：run 非 `running` 时拒绝 steer（API 侧）。

**要害**：错误都**在写状态之前**冒出来，且带「下一步」提示——不把问题推到「状态已经被改坏」之后。

### 0.6 跨层（它复用了内核哪些东西）

三条通道**几乎全是接线**：内核已有的东西一条不重造，通道只接回调、翻译、发通知。ACP 侧复用点最完整（另两条同形状）：

| 复用物 | 通道侧引用点 |
|---|---|
| **核心 `AIAgent`** | `session.py:380 from run_agent import AIAgent` · `server.py:773 agent.run_conversation(...)` |
| **进程内共享 `state.db`**（`~/.hermes/state.db`） | `session.py:276 acquire(...)`（拿进程内同一写者）· `session.py:304 db.create_session(source="acp")` |
| **工具注册表 + toolset** | `server.py:432 get_tool_definitions(enabled_toolsets=...)` · `session.py:397 enabled_toolsets=["hermes-acp"]`（`toolsets.py:187` = 编码工具去掉 `clarify`） |
| **terminal 审批回调** | `server.py:743-748`：`terminal_tool._get_approval_callback()` / `set_approval_callback(approval_cb)` |
| **provider 认证** | `auth.py:11 detect_provider()` → `hermes_cli.runtime_provider.resolve_runtime_provider` · `session.py:402` |
| **上下文压缩 / 模型目录** | `server.py:39 ContextCompressor` · `model_catalog.py` |
| **MCP** | `server.py:410 _register_session_mcp_servers` · `entry.py:193` 后台发现 |

**要害**：接入一条新通道的成本 = 「装两个可注入回调（事件 + 审批）+ 写一张翻译表 + 一个发送路径」，**不动内核**。这就是整章的架构接缝。

### 0.7 时间线（一条 `prompt.submit` 从收到到终态）

以 TUI 网关的 `prompt.submit` 为主线（内核对三条通道一致）：

1. **同步校验** —— 入口 `methods_prompt.py:560`；sanitize 文本、取会话、活跃会话额度检查；
2. **占位** —— `_lock_in_submit_turn`（`methods_prompt.py:525`）持 `history_lock`：先按需做截断（见 §六），再 `session["running"]=True`、`_start_inflight_turn`；逐字引（`methods_prompt.py:526-527` docstring）：
   > Under ``history_lock``: refuse watch-child races / malformed truncation, apply the cut, mark the turn running + in flight.
3. **快速 ack** —— `methods_prompt.py:678` `return _ok(rid, {"status": "streaming", **survivor_fields})`：客户端立刻拿回执，**不阻塞**；
4. **回合线程** —— `_run_after_agent_ready` → `_run_prompt_submit`（`prompt_turn.py:843`）：`_emit("message.start")`、逐 token 发 `message.delta`、最后 `_emit("message.complete", sid, payload)`（`prompt_turn.py:904`）；
5. **事件出口** —— 每一帧由 `_event_frame`（`server.py:631`）定型成 `{"jsonrpc":"2.0","method":"event","params":{...}}`，由 `write_json`（`server.py:611`）按会话 `session_id` 路由到 owner transport。

**终态**：回合以 `message.complete` 收尾；API 侧的等价物是 `run.<status>`（`completed` / `cancelled` / `failed`），终态事件名恒等于状态。

**要害**：**ack 与结果分离**——客户端在「已受理」时就拿到回执，真正的流式与终态走事件通道。这条设计让「慢回合」不会把请求-响应这条线堵死。

### 验收四问

1. **三条通道是不是只驱动同一个内核、差别只在「线格式 + 暴露哪批特性」？** 是。ACP 走 `session.py:427 AIAgent(**kwargs)`；网关走同一 dispatch 家族；API 走 `_run_agent`——三者都把请求折成一次 `run_conversation(...)`。（§0.1 / §0.3）
2. **协议与传输是不是分开的（一份逻辑、多传输）？** 是。`dispatch(req, transport)`（`rpc_dispatch.py:45`）用一根 ContextVar 绑定「本次请求写哪」；stdio 与 WebSocket **逐字同一协议**（`ws.py:1-2`）。（§0.2）
3. **审批是不是 fail-closed？** 是。未知 `option_id` → `deny`（`permissions.py:71-73`）、超时 → `"timeout"`、无 response → `deny`（`permissions.py:115-120`）。（§0.5）
4. **破坏性写是不是必须有独立 consent 位、且默认 fail-closed？** 是。rewind 缺 `confirm_truncate` → `4029`，**什么都没写**；有截断参数却无确认一律拒（`methods_prompt.py:263-267`）。（§0.4）

---

## §〇 差异表

只列**架构性**的差别为主，外加少数「已对齐」与「量级」行。性质取一：`架构性` / `已对齐` / `量级` / `不抄`。

| # | 面 | 真实 Hermes | mini 现状 | 性质 |
|---|---|---|---|---|
| 1 | 对外接口总数 | 三条协议：ACP · TUI 网关 JSON-RPC · OpenAI 兼容 HTTP | **0 条**（`gateway/adapters/` 只有 `console.py`） | 架构性 |
| 2 | ACP 通道 | `acp_adapter/` 14 文件 / 4,163 行；stdio JSON-RPC；需 `agent-client-protocol==0.9.0` | 无 | 不抄 |
| 3 | TUI 网关通道 | `tui_gateway/` 顶层 72 文件 / 28,517 行 + `contracts/` 17 文件；~124 方法；stdio + WebSocket | 无 | 不抄 |
| 4 | OpenAI 兼容通道 | `api_server.py` 4,056 + `_openai_routes` 1,051 + `_runs` 948 行；`aiohttp`；HTTP + SSE | 无（`gateway` 只有消息入站路径） | 架构性 |
| 5 | 传输与协议分离 | 一个 `dispatch()` 同时挂 stdio（`entry.py`）与 WebSocket（`ws.py`），协议逐字相同 | 无协议层可分离 | 架构性 |
| 6 | 同步核心 vs 异步 transport | 同步 `AIAgent` 跑 worker 线程 / executor（`server.py:46`），事件循环留主线程 | 无（进程内直调，无 loop） | 架构性 |
| 7 | 会话语义注入方式 | ContextVar / 线程局部（`edit_approval.py:37`、`server.py:721-727`），**绝不用全局 env** | 无审批注入点 | 架构性 |
| 8 | 核心对边缘的感知 | 唯一改动 = `model_tools.py:787` 一行钩子（未绑定即空操作，CLI/gateway 直接绕过） | 无此钩子 | 架构性 |
| 9 | 审批语义 | fail-closed：未知 → `deny`、超时 → `"timeout"`、无 response → `deny` | 无审批回调面 | 架构性 |
| 10 | UI 通知失败 | non-fatal（`server.py:260` 只 log 不 raise；`events.py:52` fire-and-forget） | 无对外通知 | 架构性 |
| 11 | 会话归属 | 服务器拥有 transcript；客户端历史只作提示；ACP 持久化 `source="acp"`（`session.py:304`） | 同源（transcript 由库拥有，但**无外部写入路径**） | 已对齐 |
| 12 | 终态语义 | 终态事件名恒等于状态（`run.<status>`）；绝不 `completed` 且 `completed:false` | 无 run 概念 | 架构性 |
| 13 | 重连 / 增量回放 | seq 事件环（`event_replay.py:50 _stamp_event`）+ `inflight` 快照 + `open_requests` | 无长连客户端 | 不抄 |
| 14 | 破坏性重写门禁 | rewind 要 `confirm_truncate`（否则 `4029`）+ `survivor_user_row_ids` 重绑 | 无 rewind 协议面 | 不抄 |
| 15 | 反向 server→client 请求 | `srq-<12hex>` id 的 JSON-RPC 请求（`approval` / `clarify` / `sudo` / `vault.*`…） | 无 | 不抄 |
| 16 | 模型热切换 | `switch_model()` 原地换运行时 + 清 `_cached_system_prompt`（`agent_runtime_helpers.py:2199`）；transcript 不动 | 无 `/model` 命令（第 8 章已定） | 不抄 |
| 17 | 协议错误码 | ACP `-32601`（探针良性）；TUI `-32700` / `-32601` / `4000` / `4029` / `5035`；API `409 run_not_accepting_steer` | 无 | 不抄 |
| 18 | 外部依赖面 | ACP 需 SDK + 会说 ACP 的 IDE；网关需宿主（Ink TUI / Electron）；**只有 API 服务器只要 HTTP 客户端** | 零依赖 | 架构性 |

---

## §一 三条通道的全景

**① Hermes 怎么做的**：

三条路（ACP / TUI 网关 / OpenAI 兼容 API）**驱动的是同一个 `AIAgent` 内核**，差别只在两处：**线格式**（wire format，对外说哪种报文）与**暴露哪一组功能**。

**为什么不需要三套内核**：内核才是这台机器上最贵、最不该分叉的资产——prompt 装配、供应商解析与凭据门控、工具注册表、审批、会话库 schema、上下文压缩、提示词缓存，全都住在核心。任何一条线若自备一套，同一件事立刻出现两个版本，行为随接口漂移（同一句话在 CLI 与 IDE 里装配出不同系统提示；同一把 key 在两处走不同门控）。所以三条线都被压成**薄层**：核心提供回调，适配器去接。

三条的差别落到两个正交维度：

| 通道 | 线格式 | 暴露哪组功能 |
|---|---|---|
| ACP | 编辑器协议（JSON-RPC over stdio） | 编辑器要的那一小组：会话生命周期、流式分片、工具卡片、**原生 diff 与审批对话框**、plan 面板 |
| TUI 网关 | JSON-RPC（stdio 或 WebSocket） | **细粒度全量**：会话 / 斜杠命令 / 审批 / 澄清 / 事件流 / 子智能体 |
| API 服务器 | HTTP + SSE，OpenAI 形状 | OpenAI 兼容子集（`/v1/chat/completions`、`/v1/models`…） |

三条的规模**极不对称**：`acp_adapter/` **4,163 行** · `tui_gateway/` **28,517 行** · API 服务器 **6,055 行**（4,056 + 1,051 + 948，`wc -l` 口径）。这个不对称量的是**暴露面**，不是内核能力：ACP 是翻译壳（协议方言 + 渲染）；TUI 网关要暴露「宿主能做的一切」（22 个 `methods_*.py`、约 124 个方法 + 契约）；API 服务器要铺一张 HTTP 路由表与 runs 一族。也正因如此，**只有第三条自包含**——另两条的价值在「与特定客户端对接」（需要会说 ACP 的 IDE、或宿主），第三条的价值在接口形状本身，一个 HTTP 客户端（curl）就能跑。

**② 读 Hermes 的代码**：

`acp_adapter/session.py:380-382`（锚 `f5d1926110`）：

```python
        from run_agent import AIAgent
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider
```

要害：适配器**不自己造 agent、不自己解析供应商**——它伸手去 import 同一个 `run_agent.AIAgent` 与同一个 `hermes_cli.runtime_provider`，自己只负责翻译。同一对 import 也出现在 `tui_gateway/server.py` 与 `gateway/platforms/api_server.py`，这就是「三条驱动同一内核」在代码里的落点。

**③ mini 的实现（现状）**：

**⏭ 本章不实现（用户 2026-10-12）**。mini 一个对外接口都没有：`gateway/` 只有**消息路径**（`adapters/`、`run_turn`、`session_state`…），没有 stdio/HTTP 协议面；无 `acp_adapter/`、无 `http.server`、无 `agent-client-protocol` 依赖（grep 零命中）。不做的理由与三条路各自的前提一致——ACP 与 TUI 网关都要一个**会说那套协议的第三方宿主**，mini 零依赖、没有那个客户端，写了没有消费者；唯一自包含的第三条的取舍在 §八 单独交代。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 三条通道 | ACP 4,163 / TUI 网关 28,517 / API 6,055 行 | ⏭ 本章不实现（无任何对外接口） |
| 驱动内核 | `run_agent.AIAgent`（三条共用） | 同一内核已存在（`run_agent.py`），但只被本进程 CLI / 网关直接调用 |
| 线格式 | 三种（编辑器协议 / JSON-RPC / HTTP+SSE） | 无 |
| 自包含性 | 仅 API 服务器 curl 可跑 | — |

---

## §二 ACP：薄翻译壳与接缝清单

**① Hermes 怎么做的**：

**进程与角色**。编辑器是**父进程**，它 spawn 一个子进程 `hermes acp`（也注册为 console script `hermes-acp`，或 `python -m acp_adapter`）；两者用 **stdin/stdout 上的 JSON-RPC over stdio** 通信，**stderr 给日志**。

**stdout 是协议生命线**：stdio 上 stdout **只能**承载 JSON-RPC 帧，任何人往 stdout 打一行人类可读文本，都会把协议流撕碎。所以入口把 logging 整体导向 stderr（`entry.py:58 _setup_logging`），还额外把 `AIAgent` 自己的输出也改指 stderr（`session.py:85 _acp_stderr_print`，在 `:429` 挂到 `agent._print_fn`）。ACP 与 TUI 网关的分野也在这：ACP 全目录没有 socket/listen，**stdio 是唯一 transport**（TUI 网关另可选 WebSocket）。

**一个连接、多会话并发**。一个 agent 进程只持**一个** `acp.Client` 连接（`server.py:251 self._conn`，在 `on_connect` `:255` 里存入）；但同一进程内可以有**多个并发会话**——`SessionManager._sessions`（`session.py:160`）里每个 session 有自己独立的 `AIAgent`。并发靠一个**全局线程池** `_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="acp-agent")`（`server.py:46`）：同步的 `AIAgent` 回合跑在 worker 线程，asyncio 事件循环留在主线程。不这么做，一个会话的长回合会把事件循环冻住，别的会话连不上。

**分层**（自上而下 6 层）：`acp.run_agent`(SDK 传输：分帧 + 方法派发) → `HermesACPAgent`(RPC 方法) → `prompt()`(一轮编排) → `SessionManager`/`SessionState`(会话状态) → 同步核心 `run_conversation` → **事件桥**(`events.py`/`tools.py`，把核心的同步回调翻成 ACP `session_update`) → 出口 `conn.session_update`。

**接缝清单：ACP 不新造任何东西**。它**复用**核心的：`AIAgent`、共享 `SessionDB`（`source="acp"`）、工具注册表 + toolset（`hermes-acp`）、危险命令审批回调、模型目录与切换、auth（= runtime provider，**不自建凭证库**）、上下文压缩、工具预览。**ACP 专属的只有 schema 映射与渲染**：`content.py`（内容块编解码）、`tools.py`（工具名 → `ToolKind` + 卡片渲染）、`events.py`（回调 → 通知）、`permissions.py`、`edit_approval.py`、`auth.py`、`commands.py`、`model_catalog.py`、`provenance.py`。一句话：**ACP = 「装回调 + 翻译 + 发通知」，没有第八层**。

**② 读 Hermes 的代码**：

`acp_adapter/entry.py:3-4`（模块 docstring）：

```
Loads ``~/.hermes/.env``, routes logging to stderr (stdout is reserved for ACP
JSON-RPC), and starts the ACP agent server.
```

要害：`(stdout is reserved for ACP JSON-RPC)` 是写进入口契约的一条**硬约束**，不是风格偏好——它把「日志往哪走」提升成协议正确性的一部分。

**③ mini 的实现（现状）**：

**⏭ 本章不实现（用户 2026-10-12）**。mini 无对外接口：没有 `acp_adapter/`、没有 stdio 服务器、没有 `agent-client-protocol` 依赖，`gateway/` 是消息路径而非协议面。不做的理由：这 4,163 行里真正**架构性**的只有三件事（同步核心 ↔ 异步 transport 的桥、会话语义靠 ContextVar / 线程局部注入、传输层 UI 发送永远 non-fatal），其余全是**协议方言**（schema 映射、`ToolKind`、diff 渲染、模型目录编码、auth 广告）——只有写了真实 ACP 客户端才有意义，而 mini 零依赖、没有那个客户端。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 进程模型 | 编辑器父进程 spawn `hermes acp` 子进程；JSON-RPC over stdio | ⏭ 本章不实现（无对外接口） |
| stdout 纪律 | 只跑协议，日志与 agent 打印全走 stderr | 无协议面，无此约束 |
| 会话并发 | 一连接多会话，共享 4-worker 线程池 | — |
| 复用核心 | 不新造 session / tool / approval / streaming；专属仅 schema 映射与渲染 | 核心在内（`run_agent.AIAgent`），无人来装回调 |

---

## §三 ACP 的审批与编辑守卫

**① Hermes 怎么做的**：

ACP 有**两张审批面**，对象与时点都不同：

- **危险命令**（terminal）：`permissions.py` 把 Hermes 的审批语义 ⇄ ACP `request_permission` 双向映射；
- **文件编辑**（write_file / patch）：`edit_approval.py` 在**文件被写之前**拦截。

**为什么 `edit_approval.py` 单独存在**，三条理由：

1. **预执行**。它是**文件守卫**：在 `model_tools.py` 派发工具**之前**调用 `maybe_require_edit_approval()`，被拒的 patch **绝不落到磁盘**（拒绝是返回一个 JSON 错误串，而不是抛异常）。`permissions.py` 拦的是危险**命令**（terminal 执行时）——时机 / 对象都不同：一个判「这个 diff 要不要落盘」，一个判「这条 shell 要不要跑」。V4A patch 模式也覆盖。
2. **要出原生 diff**。它用 `acp.tool_diff_content(path=…, old_text=…, new_text=…)` 让编辑器弹**真正的 diff 卡片**，而不是一句笼统的「是否允许」；这是 ACP 专属渲染。
3. **只对 ACP 生效**。靠 **ContextVar**（`edit_approval.py:37 _EDIT_APPROVAL_REQUESTER`）注入 requester：CLI / gateway 不绑定 → requester 为 `None` → **直接绕过**这道守卫。

**三条不变式**：

| # | 不变式 | 判据 |
|---|---|---|
| 1 | 失败一律 **fail-closed** | 未知 `option_id` → `deny`；超时（默认 60s）→ `"timeout"`（与显式 deny 区分）；调度失败 `response is None` → `deny`；构造提案失败 / requester 抛异常 → **默认拒绝** |
| 2 | UI 发送失败**永不杀回合** | `session_update` 失败只 log 不 raise（`server.py:260 _send`）；流式回调 fire-and-forget（`safe_schedule_threadsafe(..., timeout=5)`，失败仅 `logger.debug`，`events.py:52`） |
| 3 | 审批路由必须绑**线程局部** | 在 worker 线程绑定交互式审批；**绝不用**全局 `HERMES_INTERACTIVE` env（GHSA-96vc-wcxf-jjff） |

第 3 条要展开：并发会话共享一个线程池，如果审批走**全局 env flag**，一个 worker 把它设成 interactive，就会 **race** 到别的 worker 的 non-interactive 自动放行路径上——于是本该拦的命令被静默放行。所以交互路由做成 `tools.approval` 的 **ContextVar**，每回合先 `contextvars.copy_context()`（`server.py:830`）再丢进 executor，worker 之间互不串味。**核心对 ACP 的唯一感知**正是这条思路的收口：`model_tools.py:787-788` 的一行可注入钩子——未绑 requester 时是**空操作**，CLI / gateway 路径完全不受影响。

**② 读 Hermes 的代码**：

`acp_adapter/server.py:721-727`（`_run_agent_turn` docstring）：

```
        Approval routing is thread-local, so it MUST be bound here, not on the loop thread.
        Interactive routing is a ``tools.approval`` contextvar, not ``HERMES_INTERACTIVE`` in
        os.environ, so concurrent workers can't race a global flag onto the non-interactive
        auto-approve path (GHSA-96vc-wcxf-jjff).
```

要害：这段注释把「**为什么**」写死了——两个「必须 / 不是」：绑定**在 worker 线程**（不是 loop 线程）、用 **contextvar**（不是 `os.environ`），并附上安全公告号。审批路由是**会话级状态**，任何全局开关都会在并发下串味。

**③ mini 的实现（现状）**：

**⏭ 本章不实现（用户 2026-10-12）**。mini 没有对外接口，所以没有 ACP 审批桥、没有 `edit_approval.py`、没有编辑预执行守卫。但有两处**诚实的差异**要记下：

- mini **已有**「审批回调可注入」这一形状：`AIAgent(approval_callback=…)`（`run_agent.py:167/223`，消费点 `:939`），CLI（`cli.py:126`）与网关（`gateway/run_turn.py:100`）各自注入。注入方式是**构造参数**，不是 ACP 那套「ContextVar + 全局 setter」。
- mini 的危险命令审批**也是 fail-closed**：`if not (self.approval_callback and self.approval_callback(shown)): "[denied] …"`（`run_agent.py:939`）——回调缺失或返回假值即拒绝，与不变式 1 同向。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 命令审批 | `permissions.py` ⇄ ACP `request_permission`（fail-closed + 超时语义） | fail-closed 的 `approval_callback`（构造参数注入），无协议映射 |
| 编辑预执行守卫 | `edit_approval.py`（写前拦截 + 原生 diff + ContextVar） | ⏭ 本章不实现（无守卫，无出口） |
| 审批路由作用域 | 线程局部 ContextVar（绑 worker 线程，GHSA-96vc-wcxf-jjff） | 构造参数，无全局 flag 竞态面 |
| 核心感知 | 一行可注入钩子（`model_tools.py:787`，未绑定即空操作） | 无（核心不经任何钩子感知外部出口） |

---

## §四 一个 dispatch + 多种传输

**① Hermes 怎么做的**：TUI 网关要同时服务两类宿主 —— Ink TUI（`hermes --tui`）在自己的进程外 `spawn` 一个 Python 子进程，用 stdin/stdout 当通道；Desktop（Electron）、网页看板、dashboard 的 Chat 页则连 WebSocket `/api/ws`。两类宿主的传输完全不同（管道 vs 套接字、同步读写 vs asyncio），但**依赖的功能完全相同**：会话、斜杠命令、审批、流式事件、约 124 个方法。

给它们各写一套服务，就会把整张方法表复制一遍：同一个 bug 要修两处，TUI 上通了 Desktop 上未必通，审批/中断这些关键路径会各自漂移。Hermes 的做法是**只留一份 `dispatch`，把"往哪写"抽成传输接口**：

- **线协议只有一种**：换行分隔的 JSON-RPC 2.0，双向（`tui_gateway/ws.py:1-4` 逐字："Wire protocol is identical to stdio"）。传输换了，报文一个字都不变。
- **传输接口被压到最小**：`tui_gateway/transport.py:37-45` 的 `Transport` 协议只有两个方法 —— `write(obj)->bool`（发一帧，返回 `False` 表示对端没了）与 `close()`。stdio 与 WebSocket 各写一份实现，别处不再关心底下是管道还是 socket。
- **"本次请求该写回哪"用 ContextVar 传**：`transport.py:48-50` 定义 `_current_transport`，`dispatch` 进来先 `bind_transport(t)`（`tui_gateway/rpc_dispatch.py:51`）。这样**慢 handler 被扔进线程池后仍能写回正确的对端**（池化线程沿用同一份 context 副本，见 `rpc_dispatch.py:70`）。
- **入口签名留默认值**：`dispatch(req, transport=None)`（`rpc_dispatch.py:45`），省略 transport 就回落到模块级 stdio transport —— 正是 `tui_gateway/entry.py`（Ink TUI 路径）的行为。

两条配套小规矩是"一份逻辑"能成立的前提：**真 stdout 只留给协议** —— `tui_gateway/server.py:191-192` 把 `sys.stdout` 重定向到 stderr，库里的 `print()` 只会落到 `gateway.stderr`，不会污染 JSON 流；**快 handler 就地跑、慢 handler 进池** —— 白名单 `_LONG_HANDLERS`（`server.py:160`，逐条注明为何慢）、线程池 `server.py:178`，`rpc_dispatch.py:62` 判定"不在池里就 inline 跑"，于是 `approval.respond` / `session.interrupt` 不会被一个跑十分钟的 `prompt.submit` 堵在读循环后面。

| 宿主 | 传输 | 入口 |
|---|---|---|
| Ink TUI（`hermes --tui`） | stdio：父进程 `spawn` 子进程，stdin/stdout | 读循环 `entry.py:294` |
| Desktop / 网页看板 / dashboard Chat | WebSocket `/api/ws` | `ws.py:266 handle_ws` |
| 两者共用 | 同一 `dispatch`（`rpc_dispatch.py:45`） | 同一方法表 + 同一契约校验 |

**② 读 Hermes 的代码**：

```python
def dispatch(req: dict, transport: Optional[Transport] = None) -> dict | None:
    """Route inbound RPCs — long handlers to the pool (returns None; the worker writes its own
    response via the bound transport), everything else inline (returns the response dict).
    *transport* pins every write of this request — events included — to that transport;
    omitted → the module stdio transport (``tui_gateway.entry`` behaviour)."""
    t = transport or _stdio_transport
```

（`rpc_dispatch.py:45-50`）要害在 `transport` 是个**可省的参数**：WebSocket 侧显式传自己的 transport，stdio 侧根本不传、落到模块默认 —— 一条 `dispatch` 同时是两条传输的入口，差别只在有没有传那一参数。

**③ mini 的实现（现状）**：mini 没有对外接口。`mini-hermes/gateway/` 只有消息路径（哪条通道怎么回话），没有任何 stdio / HTTP / WebSocket 协议面，"传输"这一层根本不存在 —— 会话入口是进程内的一次 CLI 调用，没有第二个客户端会来连。要长出"传输可换"的接缝，前提是**至少有两种不同传输的宿主**（一个管道客户端 + 一个长连客户端），而 mini 只有一个调用方。故 **⏭ 本章不实现（用户 2026-10-12）**：本章的规模（`tui_gateway/` **72 文件 / 28,517 行**）与前提（多客户端后端）都在"零依赖学架构"的射程之外。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 传输抽象 | `Transport` 协议（write/close）+ ContextVar 绑定（`transport.py:37-50`） | ⏭ 不抄（无第二宿主，接缝无处可接） |
| 线协议 | 换行分隔 JSON-RPC 2.0，双向，传输无关 | ⏭ 不抄（无对外协议面） |
| stdout 纪律 | 真 stdout 只留协议，`print` 导去 stderr（`server.py:191-192`） | ⏭ 不抄（无协议流可守） |
| 快慢分流 | 快 handler 就地、慢 handler 进池（`server.py:160/178`） | ⏭ 不抄（进程内同步调用，无读循环可堵） |

---

## §五 一个回合的链路

**① Hermes 怎么做的**：一个回合的入口是 `prompt.submit`（`tui_gateway/methods_prompt.py:560`）。它的形状是**先校验、再占位、立刻 ack、回合另开线程跑**：

1. **同步校验**（进锁前）：typed stop phrase（`methods_prompt.py:569-570`）、取会话（`575`）、hosted-room 证明与活跃会话额度。
2. **重绑传输**（`615-619`）：在读锁下把会话当前 transport 重绑到 `current_transport()` —— 注释原话"streaming must stay on the active websocket even if a disconnect/fallback moved the session to stdio"。这一步正是 §四 那根 ContextVar 的用途。
3. **占位**（`_lock_in_submit_turn`，`methods_prompt.py:525`）：持 `history_lock`，按需先做截断（见 §六），再 `session["running"]=True`，并 `_start_inflight_turn`（`tui_gateway/session_history.py:267`）记下"在途回合"。
4. **立刻 ack**：`methods_prompt.py:678` `return _ok(rid, {"status": "streaming", **survivor_fields})` —— 客户端马上拿到回执，不等模型。
5. **回合线程**：`methods_prompt.py:671-677` 起 `threading.Thread(target=_run_after_agent_ready, daemon=True)`，线程里（迟到构建时先等 agent ready）调 `_run_prompt_submit`（`tui_gateway/prompt_turn.py:843`）。

**回推给客户端的东西不是 RPC 响应，而是 JSON-RPC notification**：`method:"event"`，按 `session_id` 定位。形状由 `_event_frame`（`server.py:631`）定型：

```json
{"jsonrpc":"2.0","method":"event","params":{"type":"message.delta","session_id":"…","payload":{…}}}
```

回合内容依次是 `message.start`（`prompt_turn.py:876`）→ 逐 token 的 `message.delta` → `message.complete`（`prompt_turn.py:904`），工具走 `tool.start` / `tool.generating` / `tool.complete`。这些帧的路由由 `write_json`（`server.py:611-628`）决定：**帧里带 `session_id` → 该会话的 transport**（注释：异步事件从没有 contextvar 绑定的线程也能命中 owner），否则用 contextvar 绑定的 transport，再否则模块 stdio。所以回合线程不必自己认识"对面是谁"。

**为什么先 ack 再跑**：读循环是单线程的（`entry.py:294`）。如果一个回合在循环里就地跑完再返回，模型要想几秒到几分钟，这段时间客户端发来的 `approval.respond`、`session.interrupt` 全被排在后面 —— 用户按"停止"没反应，一个待审批能卡住整个网关。把"接了"与"跑完"拆开：ack 只是一张**回执**（"已受理，正在流"），真正结果走事件流陆续回来；读循环腾出手继续收帧，慢 handler 进池（§四）。于是**打断、审批、steer 这些"回合进行中才能发生的事"才有活路**。

**② 读 Hermes 的代码**：

```python
    run_thread = threading.Thread(
        target=lambda: _run_after_agent_ready(
            rid, sid, session, text, display_kind, hosted_terminal_callback, turn_author),
        daemon=True)
    # Handle lets session.interrupt tell a live turn from a stuck `running` flag.
    session["_run_thread"] = run_thread
    run_thread.start()
    return _ok(rid, {"status": "streaming", **survivor_fields})
```

（`methods_prompt.py:671-678`）要害在最后一行：`start()` 一返回就 ack `streaming`，**ack 早于模型产出**；`session["_run_thread"]` 留句柄，正是让 `session.interrupt` 能区分"真在跑"与"卡住的 running 标志"。

**③ mini 的实现（现状）**：mini 没有对外接口。mini 的回合在调用者线程上同步跑完才返回 —— 没有 ack、没有独立回合线程、没有 `method:"event"` 事件流；因为没有第二个进程会来打断、审批或订阅。把 ack 与执行拆开的价值（读循环不被占、回合可被打断）只对"有长连客户端"的形态成立。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 回合返回 | 立刻 ack `{"status":"streaming"}`，结果走事件流 | ⏭ 不抄（进程内同步返回，无 ack 可分） |
| 执行线程 | 回合在独立 daemon 线程跑（`methods_prompt.py:671`） | ⏭ 不抄（调用者线程即回合线程） |
| 进度回推 | `method:"event"` notification，按 `session_id` 路由（`server.py:611-638`） | ⏭ 不抄（无协议面，进度直接打印到终端） |
| 回合中可打断 | `session.interrupt` 靠 `_run_thread` 句柄判活 | ⏭ 不抄（Ctrl-C 直接杀进程） |

---

## §六 history 回写 = rewind

**① Hermes 怎么做的**：一次 rewind / 编辑 / 重新生成，**在协议上就是一次 `prompt.submit`**，只是额外带了截断参数（`methods_prompt.py:241 _parse_truncation_params`）。服务端把它当作对 session **持久行的破坏性重写**，因此要求客户端**显式声明意图**：

| 参数 | 作用 | 缺了会怎样 |
|---|---|---|
| `truncate_before_user_ordinal` | 从第几个用户回合切开（从 0 起） | 仅序号且会话是持久的 → `4004`（要求带 row_id） |
| `truncate_before_row_id` | 目标用户回合的整数 SQLite 行 id（**首选地址**） | 找不到 → `4018`，**绝不回退到序号** |
| `truncate_before_message_id` | 同上，另一种 id 形状 | 找不到 → `4018` |
| `confirm_truncate` | **必需**的意图位 | 带了截断参数却没它 → `4029` |
| `confirm_empty_truncate` | 截断会把记录抹空（序号 0）时**额外**必需 | → `4028` |

门禁的顺序本身就是设计：`confirm_truncate` 的检查**在目标解析之前**（`_parse_truncation_params`，`methods_prompt.py:242-246` 注释："Malformed (4004) -> unconfirmed (4029; checked BEFORE target resolution …)"）—— 一个"状态里残留了 ordinal 的普通提交"先撞 `4029` 被拒，**不付**读持久转录、也不改写活会话 dict 的代价。ordinal 与 row_id 同时给出必须一致，否则 `4030`（`315-326`）。

**落盘顺序是 fail-closed**：全程持 `history_lock`，**先写 state.db 再改内存**（`methods_prompt.py:379-381` 注释："a failed write after the in-memory rewrite would stack the new exchange on the 'undone' turns"）。落盘走 `db.replace_messages(key, truncated, active_only=True, archive_dropped=True, reject_active_turn_lease=True)`（`412`）：`archive_dropped=True` 把被丢的行软归档（`active=0`，仍在 FTS 里），**误切可恢复** —— 注释点名三起事故（#70516 / #80763 / #82756）。改完 `session["history"] = truncated`、`history_version += 1`（`436-437`）。

**回执要带新 id**：`survivor_user_row_ids`（`methods_prompt.py:425`）列出存活用户回合的**新**行 id —— 因为这次重写把保留前缀**作为新行重新插入**，客户端缓存过的每个 row_id 事后都作废，必须用它重绑；否则下一次针对某个更早幸存回合的 rewind 会被 `4018` 拒掉（某条为 `null` 表示那轮没有持久 id，客户端就丢掉缓存那一个）。

**为何这类破坏性操作必须显式确认**：`methods_prompt.py:655-662` 的注释把理由说透了 —— 普通提交上残留的 ordinal，与一次真正的 rewind **逐字段无法区分**（同方法、同形状、目标还在范围内），而它要的 cut 是一次不可逆的 `replace_messages()`（#80763：296 → 52 条消息，244 行持久记录没了）。**只有客户端知道这是不是 rewind**，所以必须由客户端声明；服务端负责"声明了才动 + 在锁内原子重写 + 回执给出新 id"，默认 fail-closed。

**② 读 Hermes 的代码**：

```python
    """Coerce + admit the truncation params; ``(target_row_id, client_ordinal, err)``.
    Malformed (4004) -> unconfirmed (4029; checked BEFORE target resolution so a
    leaked-state request never pays the durable read or heal-stamps live dicts).  An
    ordinal/id alone is not consent: a leftover ordinal on an ORDINARY submit is
    indistinguishable from a real rewind, and the cut is a destructive replace."""
```

（`methods_prompt.py:242-246`）要害在末句那对区分：`ordinal/id alone is not consent` —— 光有目标不等于同意，`confirm_truncate` 才是那个独立的同意位；而且它**在解析目标之前**就判，让被拒的请求不产生任何读写副作用。

**③ mini 的实现（现状）**：mini 没有对外接口。mini 的"重来"只是 `/resume` 边界（从某处继续），不存在"客户端发指令让服务端就地重写历史"这条路径；破坏性重写的门禁矩阵（`4004/4018/4029/4030` + survivor 重绑 + 软归档）是长在"多客户端 + 长连会话"上的，mini 无此前提。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 回写入口 | 就是 `prompt.submit`，额外带截断参数（`methods_prompt.py:241`） | ⏭ 不抄（无客户端发指令的协议面） |
| 破坏性同意 | 必需 `confirm_truncate`，抹空再要 `confirm_empty_truncate` | ⏭ 不抄（无破坏性重写路径） |
| 落盘顺序 | 持 `history_lock`，先 DB 后内存，fail-closed（`379-381`） | ⬜ 没做（mini 无并发读者，锁内单写者即可） |
| 回执 | `survivor_user_row_ids` 让客户端重绑失效 id（`425`） | ⏭ 不抄（无缓存 id 的客户端） |
| 可恢复性 | `archive_dropped=True` 软归档，误切可救（`412`） | ⏭ 不抄（无软归档表结构） |

---

## §七 重连三元组

**① Hermes 怎么做的**：客户端断了、进程还活着，重连后要把"界面上正在发生什么"补回来。这件事被拆成**三件互不替代的东西**：

1. **per-session 的 seq 事件环**：每个发出去的 `event` 帧在 `write_json` 里被 `_stamp_event`（`tui_gateway/event_replay.py:50`）盖一个 **per-session 单调 `seq`**，并进一个有界环。客户端重连发 `session.events.since {last_seen}`（`tui_gateway/methods_session.py:2209`），拿回 `events` + `latest_seq` + `truncated` + `epoch`（`event_replay.py:100 events_since` / `111 is_truncated`）。环挤爆了 → `truncated=true`，客户端必须改为拉 history，**不能信任有洞的回放**。
2. **inflight 快照**：回合存活期间记在 `session["inflight_turn"]`（`session_history.py:267` 起始、`283` 追 delta、`294` 记中途 corrections、`314` 失败保留）。`_inflight_snapshot`（`tui_gateway/session_auto_continue.py:341`）把它转成线格式：`user` / `assistant`（已流部分）/ `streaming` / `corrections`+`correction_offsets` / `display_kind`+`display_metadata` / 失败字段。随 `session.resume` / `session.activate` 的结果下发（组装处 `server.py:2740`）。
3. **open_requests**：还没被回答的 server→client 请求（审批、澄清、sudo…）。`open_requests(sid)`（`tui_gateway/server_requests.py:229`）→ `server.py:696 _open_requests`，被塞进 `session.resume` / `session.activate` / `session.events.since` 的结果里，重连的客户端把每一帧当作"刚收到"重新呈现。

配套一根引线：`gateway.ready` 带 **`replay_epoch`**（`entry.py:277-282`、`ws.py:299-304`）。进程重启会让 seq 归零，客户端靠这个不透明 epoch 侦测"后端换了"，重置自己的水位线（`event_replay.py:45 replay_epoch`）。

**三件各自解决什么，合起来为何 ≠"重发一次历史"**：

| 件 | 回答的问题 | 为什么 history 顶替不了 |
|---|---|---|
| seq 事件环 | "自 N 之后我漏了哪些事件"（增量、便宜） | history 是**已落地**的转录，事件流里还有大量非转录帧（status / 控制 / 工具进度） |
| inflight 快照 | "此刻正在跑、但还没落库的那一轮长什么样" | 在途回合**故意还没进 history**，重发历史只会给它一个空档 |
| open_requests | "进程正停在哪一步等人回答我" | 通知环扛不住"一个还在等人回答的问题"（`server_requests.py:15-18` 注释），历史更不会记它 |

一句话：history 给的是**已结算的过去**，这三件给的是**活着的现在** —— 增量事件（漏了什么）、在途回合（正在跑什么）、未答请求（在等我什么）。三者缺一，重连画面就有洞。

配套的生命周期语义：断线只把该 transport 从会话上 `_detach_session_transport`（`tui_gateway/session_transports.py:85`），会话本身留在 `_sessions` 里；多客户端共看一个会话由 `FanoutTransport`（`transport.py:143`）提供 —— 每 peer 一个有界邮箱 + 一个守护写线程，慢 socket 丢掉自己的订阅（历史/回放是恢复路径），不拖死别人。

**② 读 Hermes 的代码**：

```python
def is_truncated(sid: str, last_seen: int) -> bool:
    """True when events between *last_seen* and the ring's oldest retained seq were
    evicted — the client must refetch history instead of trusting the replay."""
    with _replay_lock:
        return last_seen < _replay_evicted_through.get(sid or "", 0)
```

（`event_replay.py:111-115`）要害在注释那句"refetch history **instead of trusting the replay**"：`seq` 的价值只在"没洞"时成立，一旦越界就诚实报 `truncated`，把判断权交给客户端去拉 history —— 环是增量通道，不是事实来源。

**③ mini 的实现（现状）**：mini 没有对外接口。mini 没有长连客户端、没有可断的 transport、没有 seq 水位线；它的"重连"就是重新读 `state.db`。三元组长在"进程活着 + 客户端断了"这个状态上，而 mini 的会话每次都是一次性进程内读取。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 增量回放 | per-session `seq` 事件环 + `session.events.since`（`event_replay.py:50/100`） | ⏭ 不抄（无长连客户端，无水位线） |
| 越界诚实 | `truncated=true`，客户端改拉 history（`111`） | ⏭ 不抄（无环可越界） |
| 在途回合 | `inflight` 快照随 resume/activate 下发（`session_auto_continue.py:341`） | ⏭ 不抄（回合跑完才落库，无在途态） |
| 未答请求 | `open_requests` 随三处结果下发（`server_requests.py:229`） | ⏭ 不抄（无 server→client 请求） |
| 重启侦测 | `gateway.ready` 带 `replay_epoch`（`entry.py:277-282`） | ⏭ 不抄（每会话一次性进程内读，无跨重启水位） |

## §八 OpenAI 兼容服务

**① Hermes 怎么做的**：这条路的性质，一句话——**把 `AIAgent` 内核包成一个网关平台适配器，对外说 OpenAI 的线格式**。

先说清后端是**谁**，因为文档一读就容易认错门：真实实现是 `gateway/platforms/api_server.py`，它是**一个网关平台适配器**（`Platform.API_SERVER`，`gateway/config.py:191`），和 telegram / discord 那些适配器**同层**。它**不是** `hermes serve`（`serve` 是 headless dashboard 的后端，走 `hermes_cli/web_server.py`，`hermes_cli/main.py` 的 `cmd_dashboard` 起），也不是 `hermes_cli/web_server.py`。启动方式 = **起网关**：`.env` 里 `API_SERVER_ENABLED=true` + `API_SERVER_KEY`（≥16 字符），然后 `hermes gateway`；默认监听 `127.0.0.1:8642`（`hermes_cli/config_defaults.py:2875` 是 `API_SERVER_ENABLED`，`:2882` 是端口默认值）。

**形状一：它被拆成 mixin 家族，不是一个文件。** 主适配器 `APIServerAdapter`（`api_server.py:1119`）继承 `OpenAICompatRoutesMixin`（第 122 行 import）：

| 文件 | 行数 | 装什么 |
|---|---:|---|
| `gateway/platforms/api_server.py` | **4,056** | 主适配器 + 会话/能力/健康/浏览器控制路由 |
| `gateway/platforms/api_server_openai_routes.py` | **1,051** | `OpenAICompatRoutesMixin`（class 在 377）：`/v1/chat/completions`（409）、`/v1/responses`（764）、`GET/DELETE /v1/responses/{id}`（934/946）、SSE writer |
| `gateway/platforms/api_server_runs.py` | **948** | `/v1/runs` 那一族（异步 run 范式） |
| `gateway/platforms/api_server_room_grants.py` | **228** | 房间授权路由 |

它依赖 `aiohttp`（第 111-116 行的 `try import` 用 `AIOHTTP_AVAILABLE` 门控）——**真实版不是零依赖**。把这条记住，它是后面"mini 为什么用 stdlib"的对照。

**形状二：路由表是唯一事实源。** `_http_route_table()`（`api_server.py:1533`）返回一串 `(method, path, handler)` 三元组；`connect()` 在 `:3939` 逐条 `router.add_route`，并且**每条都自动挂一份 `/p/{profile}` 镜像**（`:3941`）。多 profile 靠这个前缀分家。这条设计的判据是：**加一条路由 = 改表一行**，镜像与鉴权中间件都是循环里附带的，**不可能漏挂、不可能漂移**；反过来，如果路由散在 `if/elif` 里，profile 镜像和中间件就会各写各的。

**形状三：两个"目录面"故意分工。** 这不是重复，是刻意切成两个读者群：

| 面 | 路由 | 回什么 | 给谁 |
|---|---|---|---|
| 兼容面（**最小**） | `GET /v1/models`（`_handle_models`，`:2236`） | 主模型名 + 配置里的 `model_routes` 别名；每行只有 `id/object/created/owned_by/root/parent`。**绝不回凭据** | OpenAI 客户端 / basic 前端"要个模型名回填" |
| Hermes 感知面（**全量**） | `GET /api/model/options`（`_handle_model_options`，`:2253`） | 整套 provider 行 + 每模型定价 + 能力提示（转手 `hermes_cli.inventory.build_model_options_payload`） | Hermes 感知的模型选择器 |

两个面共用**同一份 payload builder**，有三个入口：API 服务器 REST（bearer key）· dashboard REST（`X-Hermes-Session-Token`）· TUI 网关 RPC `model.options`。`/api/model/options` 带 `refresh=1` 时会**打穿 provider-model 缓存**并探测所有已存自定义 provider；普通打开只探当前那一个，免得离线的已存端点把选择器卡住。判据一句话：**`/v1/models` 是给 OpenAI 客户端用的兼容端点，不是 Hermes 的模型目录**。

**形状四：`/v1/chat/completions` 收两种范式。** 非流式：一次性回 `chat.completion`。流式（SSE）：`Content-Type: text/event-stream`，逐块写 `data: {chunk}\n\n`，以 `data: [DONE]\n\n` 收尾；出错时给 OpenAI 形状的 `{"error": {"message", "type", "code"}}`。**兼容面的纪律就在这里**：这些字段名不是 Hermes 定的，是**别人定的契约**，选这条路等于被迫看清"哪些字段必须收、哪些必须吐、`model` 字段为什么可以被忽略"。

**形状五：终态语义——"完成"是一个有定义的终态，不是一个布尔猜测。** 一次 run 的终态从"agent 那一轮**实际怎么结束的**"推出来，且**终态事件名恒等于状态**（`run.<status>`）：

| 这轮怎么结束 | status | 终态事件 | 标志 |
|---|---|---|---|
| 产出了最终答复 | `completed` | `run.completed` | `completed: true` |
| 被中断（`/stop` 或内部 interrupt） | `cancelled` | `run.cancelled` | `completed:false`、`interrupted:true`、`turn_exit_reason` 指认是谁打断的 |
| provider / agent 失败 | `failed` | `run.failed` | `completed:false`、`error` |
| 没干完就结束（迭代预算用尽 / 被截断） | `failed` | `run.failed` | `completed:false`、适用时带 `partial`、`turn_exit_reason`（如 `max_iterations_reached(60/60)`） |

立章原则：**一个 run 绝不能在同一个 payload 里既报 `completed` 又带 `completed:false` 或 `partial:true`**。于是"是不是完成了"永远只有一个来源，客户端不用猜。

**② 读 Hermes 的代码**（`gateway/platforms/api_server.py:3940`，这是 `connect()` 里注册路由的那一段）：

```python
            # Native routes + multiplex /p/<profile>/ mirrors (the prefix middleware validates and
            # scopes config/credentials when multiplexing is on).
            for method, path, handler in self._http_route_table():
                self._app.router.add_route(method, path, handler)
                self._app.router.add_route(method, f"/p/{{profile}}{path}", handler)
```

要害：**注册是数据驱动的**——路由只是表里的一行，`/p/{profile}` 镜像在循环里**附带生成**。加路由的人不需要记得"还要挂一份镜像"，因为根本没有"还要"这一步。

**③ mini 的实现（现状）**：**mini 没有对外接口。** `mini-hermes/gateway/` 只有第 7 章的消息路径（各平台适配器 + 忙时策略），没有任何 HTTP / stdio 协议面。落点与理由记在 §十一：

```
mini-hermes/
  gateway/            ← 只有消息路径适配器；无 HTTP 监听、无协议面
  （对齐真实版的落点形状应是 gateway/platforms/api_server.py + CLI 子命令 `mini serve`）
```

★ **mini 栏 = `⏭` 本章不实现（用户 2026-10-12）**。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 后端实体 | 网关平台适配器（`Platform.API_SERVER`），`hermes gateway` 起，`127.0.0.1:8642` | 无 |
| 依赖 | `aiohttp`（门控） | 无（若要落，用 stdlib `http.server`，零依赖） |
| 路由表 | `_http_route_table()` 唯一事实源 + `/p/{profile}` 自动镜像 | 无 |
| 目录面 | `/v1/models`（最小）↔ `/api/model/options`（全量）分工 | 无 |
| SSE / 终态语义 | 非流式 + SSE；`completed/cancelled/failed`，事件名恒等于状态 | 无 |

---

## §九 模型热切换

**① Hermes 怎么做的**：**每个界面都能会话中途换模型，底层就是一条 `/model` 斜杠命令。** 各面各有入口：

| 面 | 入口 |
|---|---|
| CLI / TUI | `/model claude-sonnet-4`、`/model openrouter:anthropic/claude-sonnet-4.6` |
| TUI 网关 RPC | `command.dispatch`，参数 `{"command": "/model …"}` |
| ACP | IDE 把斜杠命令当提示词发来，agent 自己派发 |
| API 服务器 | 请求体里的 `model` 字段（**per-request**） |

**真正的"原地换"发生在 agent 上，不在任何一个界面里。** 实现在 `agent/agent_runtime_helpers.py::switch_model`（def 在 `:2150`，`run_agent.py:475` 把它 forward 出去）。它的 docstring **自述**了这一节的形状：*Mirrors `_try_activate_fallback()` but also updates `_primary_runtime` so the change persists across turns.*

**接缝 = 第 8 章的两段式解析 + `run_agent._apply_runtime`。** 这不是新机制：热切换在切换前先把目标 provider 解析成 `(api_mode, base_url, 凭据)`（`_resolve_switch_destination`，`:2167`），用的**正是 provider-runtime 那条链**；解析出来的东西落到 agent 上那一刻，就是"原地换运行时"。所以——

> **热切换 = 用户发起的、持久化的 fallback 激活。**

两者的分界只有一件事：fallback 是一次性 latch（按轮复位），**热切换更新 `_primary_runtime` 让它跨轮持续**（`:2208`）。同一个"原地换"机器，一个给自动回退用，一个给用户主动选。

**代价 = 换完之后要做四件收尾**（回答"会不会断缓存 / 断流"）：

1. **重建 LLM client**（`_swap_switch_runtime`，`:1972`）；
2. **重算 prompt-caching 标志**（`_anthropic_prompt_cache_policy`，`:2183`）；
3. **换 compressor**——窗口是模型的属性，换了模型就得换压缩阈值；
4. **清 `_cached_system_prompt`**（`:2200`，下一轮重建）。

四件事合起来的意思是：**代价 = 提示词缓存前缀那一块重算 / 重计费**；但**对话历史（transcript）不动**。所以准确说法不是"断缓存"，而是"**提示词缓存那一层重来**"。

两条边界也要记住：**失败即 no-op**——`_swap_switch_runtime` 抛异常就整体回滚（`_restore_switch_snapshot`，`:2176`）再 re-raise，调用方（`tui_gateway/model_switch.py:224 _commit_agent_switch`）看到异常就 abort 整个提交；**不写进程全局 env**——桌面端一个进程里塞着多个同 profile 会话，写 `os.environ` 会泄给所有会话，所以走 **per-session `model_override`**（`tui_gateway/model_switch.py:290` 注释点明）。

**② 读 Hermes 的代码**（`agent/agent_runtime_helpers.py:2150`，`switch_model` 的 docstring）：

```python
def switch_model(
    agent, new_model, new_provider, api_key='', base_url='', api_mode='', capabilities=None
):
    """Switch the model/provider in-place for a live agent (rebuild clients, caching flags,
    compressor). Mirrors ``_try_activate_fallback()`` but also updates ``_primary_runtime`` so
    the change persists across turns. A failed swap/rebuild rolls back to the pre-switch
    snapshot and re-raises (callers catch)."""
```

要害：**一段 docstring 点明三件事**——原地换（rebuild，不是新建 agent）、与 fallback 同源（镜像那台机器）、**成功即跨轮持久、失败即回滚复发**。这三条就是热切换的全部语义。

**③ mini 的实现（现状）**：mini **没有 `/model` 命令**（第 8 章已定），所以没有用户可见的热切换；但**接缝已经在了**——第 8 章建的两段式解析（`hermes_cli/runtime_provider.py:255/344/417`）与原地换（`run_agent.py:706 _apply_runtime`，docstring 明说"回退的唯一写入口"）正是这一层。缺的只是"用户选择的 destination + 持久化"这一小段：

```
run_agent.py:706   _apply_runtime(...)        ← 原地换（换 provider/model/base_url/api_mode/api_key + context_length）
run_agent.py:748   _try_activate_fallback(...) ← 唯一的回退触发点（一次性）
→ 热切换 = 在这两者之上接「用户选的目标 + 写 _primary_runtime 式的持久化」；核心不用动
```

★ **mini 栏 = `已对齐`（接缝）+ `⏭` 本章不实现（用户 2026-10-12）**。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 用户入口 | `/model` 每个界面都有；API 请求体 `model` per-request | 无 `/model`（第 8 章已定） |
| 原地换 | `agent_runtime_helpers.py:2150 switch_model` | 接缝已在：`run_agent.py:706 _apply_runtime` 已对齐 |
| 该目标从哪来 | 两段式解析（`_resolve_switch_destination`） | 两段式解析已对齐（`hermes_cli/runtime_provider.py`） |
| 跨轮持久 | 更新 `_primary_runtime` | 无（`_primary_runtime` 尚未建） |
| 收尾代价 | 重建 client / 重算缓存标志 / 清 `_cached_system_prompt`；transcript 不动 | 未实现 |

---

## §十 真问题与收据

### 四条上游出入（核对细节见附录 C）

1. **`--mode rpc` 不存在，而它的替代品是 28.5k 行。** 文档结尾一节**自己否定**了它——原文一行："Hermes does not have a `--mode rpc` flag."。全仓 `--mode` 只有 pet render 与 uninstall 两个**别的**用途，`'rpc'` 字面量只出现在 `tui_gateway/compute_host.py` / `host_supervisor.py`（那是内部帧类型名，不是 CLI 旗标）。这一节实际是**一块"这里没有门，请走那三扇门"的牌子**；而它指向的 stdio-JSON-RPC 那扇门（TUI 网关）是 **28,517 行**。

2. **终端 run 状态表里四个看门狗 issuer 名字，有两个在锚点源码里零字面命中。** 文档写的是 "e.g."，实测：`cron_inactivity_watchdog` 有命中；`turn_liveness_watchdog` **只出现在测试夹具**里；`gateway_inactivity_watchdog` 与 `session_turn_lease_lost` **一个字面命中都没有**。让 issuer 真正有值的不是常量表，而是运行期函数 `interrupt_issuer()`（`agent/interrupt_control.py:27-32`）：它读 `agent._tool_interrupt_reason`，没有原因（= 人停）就返回 `None`，否则把原因串的空格换成下划线。→ 要找"谁打断了这一轮"，**别搜常量表，去追 `_tool_interrupt_reason` 的赋值点**。

3. **`acp-internals.md` 说 reasoning 走 `step_callback`，代码走 `agent.reasoning_callback`。** 代码在 `acp_adapter/server.py:881`，以代码为准（同处 `agent.thinking_callback = None` 与文档一致，出入只在 reasoning 这一条）。

4. **安全一条（值得单列）：审批路由必须绑线程局部，不能放全局 env。** `acp_adapter/server.py:721-727` 的注释逐字点名 GHSA-96vc-wcxf-jjff——交互式路由是 `tools.approval` 的 contextvar，**不是** `os.environ` 里的 `HERMES_INTERACTIVE`，因为并发 worker 会把一个全局 flag 竞态到"非交互自动批准"那条路上。**API 服务器同理**：并发请求各自带自己的会话，审批不能串味。

### "文档三条路"与"实现规模"完全不对称

文档给读者三扇平等的门，实际三扇门的体积差一个数量级：

| 路 | 规模（实测） | 对外的依赖 |
|---|---|---|
| ACP | `acp_adapter/` **14 文件 / 4,163 行** | 需 `agent-client-protocol` SDK + 会说 ACP 的 IDE |
| TUI 网关 | `tui_gateway/` **72 文件 / 28,517 行**（22 个 `methods_*.py`，约 124 方法） | 需宿主（Ink TUI / Electron），两条传输 |
| OpenAI 兼容 | **4,056 + 1,051 + 948 = 6,055 行** | **只要 HTTP 客户端**（curl 即可） |

文档正文只有 **216 行**。"三扇门"在文档里等宽，在代码里是 **4.1k / 28.5k / 6.1k** —— 这不是文档写错，是**文档的粒度（能力清单）与实现的粒度（生产级基座）本来就不同轴**；读者必须自己意识到"能列出来"≠"能抄下来"。

### 本章的收据

"收据"= **翻译与勘查跑出来的实测数字**（可复核），不是判断：

- **规模**（`wc -l`，锚 `f5d1926110`）：`acp_adapter/` **14 文件 / 4,163 行** · `tui_gateway/` **72 文件 / 28,517 行** · `api_server.py` **4,056** + `api_server_openai_routes.py` **1,051** + `api_server_runs.py` **948** · 文档 **216 行**。
- **看门狗名字命中数**：文档四个 issuer 例名里，`gateway_inactivity_watchdog` 与 `session_turn_lease_lost` 在锚点源码里**零字面命中**（另两个一个真命中、一个只在测试夹具）。
- **mini 侧对外接口**：**0 行**。

---

## §十一 差距清单

★ **用户决定（2026-10-12）：`本章 mini 不实现（用户 2026-10-12：“第九章好像确实不需要怎么实现 实现可以留后面”）`。**

一句话理由：**三条路里只有 API 服务器自包含**，能用 stdlib `http.server` 零依赖落地、并可用 curl 真跑出收据；另两条（ACP / TUI 网关）的价值在"与特定客户端对接"，**需要第三方客户端**，抄进来只是一份自洽的私有协议。**决定与理由登记在此，A1~A11 的形状一并记下，将来要做时不必重新勘查。**

### A 批 · 要抄的"形状"（A1~A11，记形状不记规模）

**A1~A9 是从真实版抄的"形状"，mini 落点与大致规模如下**（★ 全部 mini 栏 = `⏭` 本章不实现（用户 2026-10-12）；一旦落地则改口径）：

| # | 形状 | 真实来源 | mini 落点 | 规模 |
|---|---|---|---|---|
| A1 | **路由表是唯一事实源**（加路由只改表，不散在 if/elif） | `api_server.py:1533 _http_route_table()` | 同形状的表 + 一个分发器 | ~30 |
| A2 | **两个目录面故意分工**：最小面不回凭据 / 全量面才给 provider 行与能力 | `GET /v1/models` ↔ `/api/model/options` | `/v1/models`（只主模型 + 别名）+ `/api/model/options`（全量，复用第 8 章注册表） | ~50 |
| A3 | `POST /v1/chat/completions`：**非流式 + 流式（SSE）**；错误形状对齐 OpenAI | `api_server_openai_routes.py` | `data: {...}` 行 + `[DONE]`；`{"error": {message,type,code}}` | ~120 |
| A4 | ★ **会话归属**：服务器拥有 transcript，客户端 messages **只作提示、不原样落库**；会话由请求头定 | `X-Hermes-Session-Id` / `-Key` | `X-Mini-Session-Id`（缺省由服务器分配并回报）；落库走第 6 章会话库 | ~40 |
| A5 | 提示词由**服务器**装配（复用第 3 章），客户端只提供 user 文本 | 核心装配 | 直接调 `prompt_assembly` | ~15 |
| A6 | ★ **核心只暴露两个注入点：事件回调 + 审批回调**；transport 只做"装回调 + 翻译 + 发通知" | `model_tools.py:787 maybe_require_edit_approval`（核心对 ACP 的唯一感知） | 同一形状：API 服务器注册 callback；**审批默认 deny（fail-closed）**，请求超时/未知一律拒 | ~40 |
| A7 | `model` **per-request**，不改会话默认 | `/model` + `switch_model`（`agent_runtime_helpers.py:2150`） | 复用第 8 章两段式解析 + `_apply_runtime` | ~20 |
| A8 | **终态语义**：`completed` / `cancelled` / `failed`，终态事件名恒等于状态（绝不 completed + `completed:false` 同时出现） | 终端 run 状态表 | 响应体 + 日志同一来源 | ~10 |
| A9 | 忙时沿用网关的忙时处置（不新造一套） | 第 7 章 `busy_policy` | 复用 | ~15 |

**合计 ≈ 340 行代码 + 契约测试（~120 条）+ 真跑收据**（curl 非流式/流式各一次 + 去文件系统核会话库）。

**三条不变量必须显式守住**（这是本章最值钱的一句判据）：① 角色交替 ② 提示词字节稳定（**不许**每请求重建系统提示词）③ 会话归属（**不原样落客户端 messages**）。守住这三条 = A4/A5 的必要性；**"偷懒把客户端 messages 原样写库"会让三条全破**。

**另外两条形状要抄，但不进代码、进文档**：

- **A10 · ACP 的形状**：整条线 = "**装回调 + 翻译 + 发通知**"；核心对它的感知只有**一行可注入钩子**（`model_tools.py:787`）。→ 写进 §二/§三 的 ① 块（"边缘能力怎么接进内核"的范本）。
- **A11 · TUI 网关的形状**：**一个 `dispatch` + 多种传输**（stdio / WebSocket 共用，`rpc_dispatch.py:45`）；客户端靠 **seq 事件环**增量补事件（`event_replay.py:50`，越界给 `truncated`）+ `inflight` 快照 + `open_requests` 三元组重连。→ 写进文档，**不实现**（mini 无长连客户端）。

### B 批 · 不抄（B1~B9，逐条理由）

理由分三类：**① 基座**（要有第三方客户端才有意义）/ **② 规模差距**（生产级体量，超出"零依赖学架构"）/ **③ 用不上的复杂度**（消费者还没到位）。

| # | 不抄什么 | 规模 | 理由 |
|---|---|---|---|
| B1 | ACP 整条 | 4,163 行 | ① 需第三方 SDK + IDE 客户端；mini 零依赖 |
| B2 | TUI 网关整条 | 28,517 行 / ~124 方法 | ①② 基座 + 规模；只留形状（A11） |
| B3 | 反向 server 请求台账（`srq-<12hex>` + `open_requests`） | — | ① mini 本轮只有审批回调（A6 已覆盖）；台账留给"多客户端"场景 |
| B4 | 重连三元组（seq 环 + inflight 快照 + open_requests） | — | ① 基座（无长连客户端）；文档里说清"为什么需要 seq"即够 |
| B5 | rewind 校验矩阵（`truncate_before_row_id` + 必需 `confirm_truncate` + `survivor_user_row_ids`） | — | ② mini 已有 `/resume` 边界；不引破坏性重写 |
| B6 | Pi-style RPC 映射 | 0 行代码 | 纯文档层（全仓只命中两份 `.md`） |
| B7 | `api_server_runs.py` 的 runs 族（含 steer / 幂等键） | 948 行 | ① mini 无 run 概念（7.2 的 steer 是本进程内的） |
| B8 | `/p/{profile}` 路由镜像 / room dispatch / grants / 幂等键 | ~500 行 | ③ 多租户 HTTP 面，mini 用不上 |
| B9 | dashboard 的 `web_server.py` 一族 | 1,876 + 兄弟 | ③ 与本章无关（那是仪表盘的 HTTP 面） |

**另外不做运行中热切换**（第 8 章已定：mini 无 `/model` 命令）→ 本章只在 §九 的 ① 块里记"真实版原地换的代价：重建 client、重算缓存标志、**清 `_cached_system_prompt`**，transcript 不动"。
