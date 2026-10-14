# 中文精译 · 第 12 章：小篇合卷 —— 代码归属图与完成回执投递（Codebase Ownership Map & Background Completion Backlogs）

> **本篇定位**：精译为主（看懂"改动该落在哪个子系统、找哪份文档入门"，以及"多个后台进程几乎同时完成时，界面怎么把它们并成一个通知回合送进来"）。它是一章两篇的**合卷**（收尾路线里的"小篇合卷"）：上篇《代码归属图》是一张"子系统 ↔ 源码目录 ↔ 文档入口"的导航图；下篇《后台完成回执》是这条投递链路的规格说明。差异对比、源码深挖与实现日志留给《源码讲解-12》。
> **来源**：`官方文档-Developer-Guide/codebase-ownership.md`（**34 行**，H1 + 无 H2；`title` 逐字：*Codebase Ownership Map*）与 `官方文档-Developer-Guide/completion-backlog-delivery.md`（**52 行**，H1 + 2 个 H2；`sidebar_label` 逐字：*Completion backlogs*）。两篇在官方侧边栏里相邻，`codebase-ownership` 在前。
> **上游**：commit `f5d1926110` · `version = "0.21.3"` · 原文合计 **86 行**

[TOC]

---

## 0. 这一章在回答什么

这一章把两篇小文并成一步走完的**收尾**：

- **上篇**回答**改动落在哪里**。Hermes 是一个大仓库，而绝大多数贡献**恰好只碰一个子系统**。那张表给每个子系统配好三样东西：它的**源码目录**、动它之前该先读的**文档入口**、以及它的**镜像测试目录**。照着它，你能找到正确的起步文档、正确的落点、和正确的测试。
- **下篇**回答**一批后台完成回执怎么送进来**。交互式界面会把**同一个会话里已经就绪的**连续后台进程完成事件，聚合成**一个通知回合**；它讲清谁拥有这次聚合、哪些东西**不**进批、以及本地那个探针**验证到了哪一步、又验证不到哪一步**。

两篇的共同底色是同一个词：**归属（ownership）** —— 上篇是"改动归属哪个子系统"，下篇是"事件归属哪个会话"。合在一章，是因为它们分别在**代码层**与**运行时层**回答"这件事该由谁负责"。

---

## 第一部分 · Codebase Ownership Map —— 代码归属图

Hermes 是一个大仓库，而绝大多数贡献**恰好只碰一个子系统**。本页把每个子系统映射到它的**源码目录**、以及动它之前你应当先读的**文档入口**。用它来找到正确的起步文档、正确的改动落点、以及正确的测试目录（**测试镜像源码**：`tools/` 里的代码在 `tests/tools/` 里被测，插件在 `tests/plugins/<type>/` 里被测，依此类推）。

| 子系统 | 源码目录 | 文档入口 |
|-----------|-------------------|------------------|
| Agent core（循环、传输、压缩） | `agent/`, `run_agent.py` | [Agent Loop](https://hermes-agent.nousresearch.com/docs/developer-guide/agent-loop)（代理循环）, [Context Compression & Caching](https://hermes-agent.nousresearch.com/docs/developer-guide/context-compression-and-caching)（上下文压缩与缓存） |
| Prompt assembly（提示词组装） | `agent/prompt_builder.py`, `agent/system_prompt.py` | [Prompt Assembly](https://hermes-agent.nousresearch.com/docs/developer-guide/prompt-assembly)（提示词组装） |
| Model providers & transports（模型提供方与传输） | `agent/transports/`, `plugins/model-providers/`, `hermes_cli/models.py` | [Adding Providers](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-providers)（添加提供方）, [Model Provider Plugins](https://hermes-agent.nousresearch.com/docs/developer-guide/model-provider-plugin)（模型提供方插件）, [Provider Runtime](https://hermes-agent.nousresearch.com/docs/developer-guide/provider-runtime)（提供方运行时） |
| Built-in tools（内置工具） | `tools/` | [Adding Tools](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-tools)（添加工具）, [Tools Runtime](https://hermes-agent.nousresearch.com/docs/developer-guide/tools-runtime)（工具运行时） |
| Messaging gateway（消息网关） | `gateway/`, `plugins/platforms/` | [Gateway Internals](https://hermes-agent.nousresearch.com/docs/developer-guide/gateway-internals)（网关内部）, [Adding Platform Adapters](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters)（添加平台适配器） |
| CLI（命令行） | `hermes_cli/` | [Extending the CLI](https://hermes-agent.nousresearch.com/docs/developer-guide/extending-the-cli)（扩展 CLI） |
| Plugins system（插件系统） | `plugins/` | [Build a Hermes Plugin](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)（构建一个 Hermes 插件） |
| Skills（bundled & optional，随包内置与可选） | `skills/`, `optional-skills/` | [Creating Skills](https://hermes-agent.nousresearch.com/docs/developer-guide/creating-skills)（创建技能） |
| Cron / scheduled jobs（定时任务） | `cron/` | [Cron Internals](https://hermes-agent.nousresearch.com/docs/developer-guide/cron-internals)（Cron 内部） |
| Session storage（会话存储） | `hermes_state.py`, `hermes_state_*.py` | [Session Storage](https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage)（会话存储） |
| Browser stack（浏览器栈） | `tools/browser_tool.py`, `tools/browser_supervisor.py`, `tools/browser_cdp_tool.py` | [Browser Supervisor](https://hermes-agent.nousresearch.com/docs/developer-guide/browser-supervisor)（浏览器监督器） |
| Egress firewall（出向防火墙） | `agent/proxy_sources/iron_proxy.py` | [Egress Internals](https://hermes-agent.nousresearch.com/docs/developer-guide/egress-internals)（出向内部） |
| ACP (IDE integration)（IDE 集成） | `acp_adapter/` | [ACP Internals](https://hermes-agent.nousresearch.com/docs/developer-guide/acp-internals)（ACP 内部） |
| Desktop app（桌面应用） | `apps/desktop/` | [Desktop Plugin SDK](https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk)（桌面插件 SDK）, [Worktree UI Development](https://hermes-agent.nousresearch.com/docs/developer-guide/worktree-ui-dev)（Worktree UI 开发） |
| TUI（终端界面） | `ui-tui/`, `tui_gateway/` | [Worktree UI Development](https://hermes-agent.nousresearch.com/docs/developer-guide/worktree-ui-dev)（Worktree UI 开发） |
| Docs site（文档站） | `website/` | [Contributing](https://hermes-agent.nousresearch.com/docs/developer-guide/contributing)（贡献指南） |
| Tests（测试） | `tests/`, `tests-js/` | [Contributing → Before Submitting](https://hermes-agent.nousresearch.com/docs/developer-guide/contributing#before-submitting)（贡献指南 → 提交之前） |

由这张图自然落出的几条约定：

- **改动应当留在它自己的子系统内。** 一个需要去改核心文件的插件是**设计坏味道** —— 应当去拓宽通用的插件面（见仓库 `AGENTS.md` 里的**贡献评审准则**）。
- **你碰过的每个源码目录，都跑它的镜像测试目录。** 对 `plugins/platforms/telegram/` 的一次改动，需要 `tests/plugins/platforms/` **变绿**，而不只是你碰巧想到的那个测试文件。
- **当牵涉两个子系统时，由更窄的那个拥有这次改动。** 宁可把修复做在适配器或插件里，也不要在代理核心里加一个分支；核心是一道**窄腰**，那里每加一样东西，都要在**每一次 API 调用**上付账。

---

## 第二部分 · Background completion backlogs —— 后台完成回执的批量投递

交互式界面会把**同一个会话里已经就绪的**连续后台进程完成事件，聚合成**一个通知回合**。这**不**引入延迟，也**不**承诺把不同时刻结束的任务合并起来。失败与成功的输出都**留在这一批里**；若只有一个完成，则**保留它原本的文本**。

进程身份会**一直保留到投递那一刻**。因此，一次显式的 `process_manage` wait/log/kill 消费，**即使该 CLI 完成事件已经离开进程注册表、进入了输入队列**，仍能把它**抑制掉**。一个**被完全消费掉的批**不会开启任何回合。监视输出（watching output）与异步委派的结果**仍是各自独立**的通知，保持**它们原本的顺序**；它们**不会**被折进完成批里。

### Consumers and ownership —— 消费者与归属

- **经典 CLI：** `hermes_cli/cli_process_notifications.py` 拥有空闲/回合后（idle/post-turn）的**排出**、**感知压缩的归属**检查、以及最后一步的输入解包。
- **TUI 与桌面端：** `tui_gateway/session_notifications.py` 在检查归属**之后**，对**轮询器**的那份**就绪快照**做分组。每个进程**仍然各自**发出自己的 UI 状态。忙碌的会话**重新入队的是结构化事件**，而不是渲染好的批字符串。
- **回合后的 TUI 安全网：** `tui_gateway/prompt_turn.py` 走的是**同一条**路由与渲染路径。桌面端与 dashboard 聊天客户端共用这个后端。
- **消息网关：** `gateway/run_notifications.py` 早已使用它自己的、**按路由键**（route-keyed）的**短窗口**批处理。这次"交互式完成回执"的改动**不替换**那套机制，也**不改动**适配器的发送。
- **非交互/无头消费者：** 这次改动**不会**为一次 API 请求、ACP 客户端、或一次性（one-shot）CLI 造出一个新的**自主通知循环**。

**共享的渲染器**是 `tools/process_registry_notifications.py::ProcessNotificationBatch`。**无论是一个批、还是它的投递状态，都不会被持久化进系统提示词**。**被指名的事件仍然要求有一个可证明的归属者**；另一个活着的会话**不能**把它们认领过去。委派的投递继续走它既有的、**持久的认领/完成账本**（claim/complete ledger），频率是**每个委派一次**，而不是每个进程批一次。

### Local validation and its limits —— 本地验证及其边界

`evals/completion_backlog_probe.py REPO OUTPUT.json` 会在**临时目录**里**真启动**本地 shell 子进程，读它们**真实的**完成事件，并驱动**生产**的 CLI、TUI 轮询器与回合后通知路由。一个 **loopback HTTP 回合汇点（turn sink）**替换掉 `chat` / `_run_prompt_submit`；它会记录**真实的投递**，但**不**演练模型推理、原生渲染器交互、或任何托管的平台。合成的 watch 与委派**信封**是**带标签的测试样本（fixture）**；委派的认领用的是**真实的临时 SQLite 账本**。backlog 这个用例里**包含一个非零退出码**。

探针会检查：一个**就绪的积压**、**单个精确负载**、被**显式消费**的结果、**外部归属**、以及**与完成事件交错的** watch/委派。它度量的是**通知边界上的回合准入**，而不是模型层面省下的 token。

---

## 附录 A · 译名对照

| 术语 | 译名 | 首次出现位置 |
|---|---|---|
| codebase ownership（ownership map） | 代码归属（归属图） | §0 |
| subsystem | 子系统 | 第一部分（引言段） |
| source directories | 源码目录 | 第一部分（引言段） |
| docs entry point | 文档入口 | 第一部分（引言段） |
| mirror（tests mirror source） | 镜像（测试镜像源码） | 第一部分（引言段） |
| contribution rubric | 贡献评审准则 | 第一部分（约定 1） |
| design smell | 设计坏味道 | 第一部分（约定 1） |
| narrow waist | 窄腰 | 第一部分（约定 3） |
| completion backlog | 完成回执 | §0 |
| consumers | 消费者 | 第二部分（`Consumers and ownership`） |
| ownership | 归属 | 第二部分（`Consumers and ownership`） |
| notification turn | 通知回合 | 第二部分（引子） |
| poller | 轮询器 | 第二部分（`Consumers and ownership`） |
| headless | 无头 | 第二部分（`Consumers and ownership`） |
| drain | 排出 | 第二部分（`Consumers and ownership`） |
| ready snapshot | 就绪快照 | 第二部分（`Consumers and ownership`） |
| structured event | 结构化事件 | 第二部分（`Consumers and ownership`） |
| route-keyed short-window batching | 按路由键的短窗口批处理 | 第二部分（`Consumers and ownership`） |
| claim/complete ledger | 认领/完成账本 | 第二部分（`Consumers and ownership`） |
| envelope | 信封（envelope） | 第二部分（`Local validation and its limits`） |
| fixture | 固定装置（fixture） | 第二部分（`Local validation and its limits`） |
| ledger | 账本 | 第二部分（`Consumers and ownership`） |
| turn sink（loopback HTTP turn sink） | 回合汇点（loopback HTTP 回合汇点） | 第二部分（`Local validation and its limits`） |
| probe | 探针 | 第二部分（`Local validation and its limits`） |
| foreign ownership | 外部归属 | 第二部分（`Local validation and its limits`） |
| turn admission | 回合准入 | 第二部分（`Local validation and its limits`） |
| addressed event | 指名事件（addressed event） | 第二部分（`Consumers and ownership`） |

---

## 附录 B · 本篇提到的文件与实测行数

行数一律 `git show f5d1926110:<路径> | wc -l` 实测，路径先经 `git ls-tree` 确认存在（目录按下挂文件数计）。**实测锚 `f5d1926110`。**

**B-1 · 下篇点名的**文件（两个小篇里，只有下篇按**源码路径**点名文件）**：**

| 文件 | 实测 | 角色 |
|---|---|---|
| `hermes_cli/cli_process_notifications.py` | **64 行** | 经典 CLI 的拥有者：`_owns_process_notification`（`:5`）、`_drain_process_notifications`（`:22`）、`_tui_unwrap_input`（`:45`） |
| `tui_gateway/session_notifications.py` | **773 行** | TUI/桌面端的轮询器与分组：`_notif_dispatch_completions`（`:502`，构造批）、`_notif_handle_ready`（`:533`，"One ready snapshot"） |
| `tui_gateway/prompt_turn.py` | **967 行** | 回合后的 TUI 安全网：`_run_post_turn_followups`（`:416`）复用同一条路由/渲染路径 |
| `gateway/run_notifications.py` | **1887 行** | 消息网关自有的 route-keyed 短窗口批：`_event_route_key`（`:1416`）、`_flush_process_completion_batch`（`:1456`） |
| `tools/process_registry_notifications.py` | **411 行** | 共享渲染器 `ProcessNotificationBatch`（`:15`）、`group_process_notifications`（`:39`）、逐事件 `format_process_notification` |
| `evals/completion_backlog_probe.py` | **195 行** | 本地 I/O 探针本体（`REPO OUTPUT.json` 入口 `:3`；三个 surface `:188`） |

**B-2 · 与下篇有直接关系的宿主文件（原文按**符号**点名，落到锚点上的所在）：**

| 文件 | 实测 | 角色 |
|---|---|---|
| `tools/process_registry.py` | **2376 行** | `process_manage` wait/log/kill 的消费标记：`is_completion_consumed`（`:1434`）、`_completion_consumed.add`（`:1764` read_log / `:1798` wait / `:1872`·`:1887` kill_process） |
| `tools/async_delegation.py` | **1038 行** | 委派的**持久账本**（`state.db / async_delegations`，`:80`）：`claim_event_delivery`（`:344`）、`complete_event_delivery`（`:409`） |
| `cli.py` | **4764 行** | CLI 侧两个排出调用点：`"cli-idle"`（`:3467`）与 `"cli-post-turn"`（`:3603`） |
| `tests/hermes_cli/test_completion_backlog.py` | **24 行** | 该链路的测试入口（`from evals.completion_backlog_probe import probe`） |

**B-3 · 上篇（代码归属图）表内点名的源码路径与目录：**

| 路径 | 实测 | 说明 |
|---|---|---|
| `agent/` | **288 个文件**（目录） | Agent core |
| `run_agent.py` | **1581 行** | Agent core |
| `agent/prompt_builder.py` | **1745 行** | Prompt assembly |
| `agent/system_prompt.py` | **817 行** | Prompt assembly |
| `agent/transports/` | **11 个文件**（目录） | Model providers & transports |
| `plugins/model-providers/` | **79 个文件**（目录） | Model providers & transports |
| `hermes_cli/models.py` | **2788 行** | Model providers & transports |
| `tools/` | **318 个文件**（目录） | Built-in tools |
| `gateway/` | **164 个文件**（目录） | Messaging gateway |
| `plugins/platforms/` | **108 个文件**（目录） | Messaging gateway（`plugins/platforms/telegram/` = **8 个文件**） |
| `hermes_cli/` | **516 个文件**（目录） | CLI |
| `plugins/` | **388 个文件**（目录） | Plugins system |
| `skills/` | **331 个文件**（目录） | Skills（bundled） |
| `optional-skills/` | **788 个文件**（目录） | Skills（optional） |
| `cron/` | **29 个文件**（目录） | Cron / scheduled jobs |
| `hermes_state.py` | **1674 行** | Session storage（另 `hermes_state_*.py` 共 **26 个**同级文件） |
| `tools/browser_tool.py` | **1388 行** | Browser stack |
| `tools/browser_supervisor.py` | **564 行** | Browser stack |
| `tools/browser_cdp_tool.py` | **410 行** | Browser stack |
| `agent/proxy_sources/iron_proxy.py` | **1044 行** | Egress firewall |
| `acp_adapter/` | **14 个文件**（目录） | ACP（IDE 集成） |
| `apps/desktop/` | **2773 个文件**（目录） | Desktop app |
| `ui-tui/` | **491 个文件**（目录） | TUI |
| `tui_gateway/` | **90 个文件**（目录） | TUI |
| `website/` | **848 个文件**（目录） | Docs site |
| `tests/` | **4527 个文件**（目录） | Tests（`tests/tools/` = 665；`tests/plugins/platforms/` = 28） |
| `tests-js/` | **16 个文件**（目录） | Tests |
| `AGENTS.md` | **446 行** | 约定 1 引用的**贡献评审准则**（`:31 ## Contribution Rubric`）；约定 2 的镜像目录规则（`:346`） |
| `website/docs/developer-guide/` | **48 个文件**（目录） | 表内 22 个文档链接的目标目录，逐个实测在场（`contributing.md:223 ### Before Submitting` 即 `#before-submitting` 锚） |

---

## 附录 C · 上游文档与源码的出入

核对锚：上游 commit `f5d1926110`（v0.21.3）。**结论：未发现出入。** 两篇原文点名的每一样东西都在源码里命中，逐条如下：

**下篇（completion-backlog-delivery.md）：**

- `ProcessNotificationBatch` → `tools/process_registry_notifications.py:15` `class ProcessNotificationBatch:`（docstring 逐字：*"Keep completion identity until the owning surface starts its turn."*）。全文引用它的只有两个文件：`hermes_cli/cli_process_notifications.py:26`、`:40`、`:50`、`:51`，以及 `tui_gateway/session_notifications.py:503`、`:516`。
- "**经典 CLI** 拥有 idle/post-turn 排出" → 调用点 `cli.py:3467`（`"cli-idle"`）与 `cli.py:3603`（`"cli-post-turn"`）**两处都在**，落的都是 `hermes_cli/cli_process_notifications.py:22 _drain_process_notifications(self, consumer)`。
- "**感知压缩的归属**" → `hermes_cli/cli_process_notifications.py:5 _owns_process_notification` 的 docstring 逐字：*"Whether this session owns a delegation event (pre-compression keys resolve to their continuation; fail closed)."*
- "TUI/桌面端 **检查归属之后**分组那份**就绪快照**" + "每个进程仍然各自发出自己的 UI 状态" → `tui_gateway/session_notifications.py:533 def _notif_handle_ready(...)`，docstring 逐字：*"One ready snapshot: ownership and UI emission per event, one turn per completion run."*；逐事件 `_emit("status.update", sid, {"kind": "process", ...})` 在 `:487`，成批构造在 `:516`。
- "忙碌会话**重新入队结构化事件**" → `tui_gateway/session_notifications.py:461` 与 `:493` 都是 `queue.put(evt)`（evt 为事件 dict）、`:510` 的 `deferred.append` 同为 dict；注释逐字（`:480`）：*"a re-queued completion would otherwise re-emit every 0.5s while the session is busy"*。
- "回合后 TUI 安全网**同一条路径**" → `tui_gateway/prompt_turn.py:416 _run_post_turn_followups` 内 `:445` 直接调用 `_notif_handle_ready(...)`，并在 `:441` 传入 `owns_event=lambda e: _session_owns_notification_event(sid, session, e)`（与轮询器同一道 fail-closed 闸门）。
- "消息网关**自有的 route-keyed 短窗口批**" → `gateway/run_notifications.py:1416 _event_route_key`、`:1456 _flush_process_completion_batch`、`:1462 await asyncio.sleep(self._completion_notification_batch_window)`。
- "wait/log/kill 消费可抑制回执" → `tools/process_registry.py:1434 def is_completion_consumed`；三处消费标记实测都在：`read_log`（`:1764`）、`wait`（`:1798`）、`kill_process`（`:1872`、`:1887`，由 `consume_output` 门控）。
- "委派走**持久认领/完成账本**、每委派一次" → `tools/async_delegation.py:80` 逐字 `# ── Durable ledger (state.db / async_delegations) ───`；`claim_event_delivery`（`:344`）与 `complete_event_delivery`（`:409`）均以 `delegation_id` 为键。
- "`evals/completion_backlog_probe.py REPO OUTPUT.json`" → `evals/completion_backlog_probe.py:3` 逐字：*"Run with the repo's Python: evals/completion_backlog_probe.py REPO OUTPUT.json."*
- "loopback HTTP 回合汇点替换 `chat` / `_run_prompt_submit`，记录投递但不演练模型推理/原生渲染器/托管平台" → 探针文件头 `:5-6` 逐字：*"The sink replaces chat/_run_prompt_submit, NOT ownership, consumption, queue draining, batching, or formatting."*；`:1` 逐字：*"Local I/O probe for completion batching, not a hosted-model or native UI test."*；实现见 `:46`（`ThreadingHTTPServer`）与 `:83`（`patch.object(server, '_run_prompt_submit', tui_submit)`）。
- "合成的 watch/委派信封是**带标签的测试样本**" → `evals/completion_backlog_probe.py:106` 委派信封逐字带 `'goal': 'Fixture delegation'`。
- "委派认领用**真实的临时 SQLite 账本**" → 同一个探针在 `tempfile.TemporaryDirectory()` 里设 `HERMES_HOME`，再走 `ad._persist_dispatch` / `ad._persist_completion` 与 `ad._transaction()` 读 `async_delegations`。
- "backlog 用例**含一个非零退出码**" → `evals/completion_backlog_probe.py:90` 逐字：`f'print("BACKLOG_{index}"); sys.exit({7 if index == count - 1 else 0})'`（backlog = 12 个子进程，最后一个退出码 7）。
- "探针检查**就绪积压 / 单个精确负载 / 显式消费 / 外部归属 / 交错**" → `evals/completion_backlog_probe.py:188` 逐字：`for surface in ('cli', 'poller', 'post-turn')`，`:189` 逐字：`for scenario in ('backlog', 'single', 'consumed', 'foreign', 'mixed')`。

**上篇（codebase-ownership.md）：**

- 表内 **22 个文档链接的目标文件**在上游锚点全部实测在场（`website/docs/developer-guide/` 下 48 个文件）；本仓发布链接已改为官网绝对地址，含 `plugins/index.md`（上游链接原写作 `plugins/index.md`）与 `contributing.md#before-submitting`（锚 = `contributing.md:223 ### Before Submitting`）。
- 表内**源码目录/文件**全部实测在场（见附录 B-3），含 `hermes_state_*.py`（26 个同级文件）与 `tests-js/`。
- "插件需要改核心文件是设计坏味道（见 `AGENTS.md` 的贡献评审准则）" → `AGENTS.md:31` 逐字 `## Contribution Rubric — What We Want / What We Don't`。
- "测试镜像源码" → `AGENTS.md:346` 逐字：*"**Placement mirrors the source tree.** A test lives in `tests/<top-level source dir>/`"*；`tests/tools/`（665 个文件）与 `tests/plugins/<type>/`（如 `tests/plugins/platforms/`，28 个文件）均在。
- 约定 2 的例子 `plugins/platforms/telegram/` → 实测 `plugins/platforms/telegram/` 有 8 个文件、镜像目录 `tests/plugins/platforms/` 在场。

**补充（非出入，一句澄清）**：文中那句"**共享的渲染器**是 `…::ProcessNotificationBatch`"，其"共享"的**范围**严格说是 **CLI + TUI/桌面端** —— 消息网关另有一份自己的合成文本渲染 `_format_coalesced_process_completions`（`gateway/run_notifications.py:1420`）。这不是矛盾：原文那条 gateway bullet 已经明说网关"早已使用它自己的…机制、这次改动不替换它"，故与"共享"一句**互相界定**、并不冲突。

**两处无法从源码证伪的散文断言**（如实记下，不算出入）：① 下篇"这次改动**不会**为非交互/无头消费者造出新的自主通知循环"—— 这是一条**否定性**断言，源码里没有对应的反例可查；② "**无论是一个批、还是它的投递状态，都不会被持久化进系统提示词**"—— 指向的是"系统提示词不被写入"这一约束，源码侧无单点符号可指认。两条按原文如实译出。
