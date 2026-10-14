# 源码讲解 · 第 15 章：观察者钩子（Observer Hooks）

> 维护说明：章节交付时的统计与实测输出保留原口径；当前发布前修复、套件数字与验证收据见《发布前修复记录.md》。维护节说明当前契约；正文与附录仍含各阶段源码片段，按段内提交读取。

> 上游：`官方文档-Developer-Guide/observer-hooks.md`（331 行）· 中文精译见 `中文精译-15-ObserverHooks.md`（528 行）。
> 真实侧行号锚 `f5d1926110`（v0.21.3）· mini 侧历史规模锚 `1ccaa50`，维护契约以文首说明为准（实现 commit `1ccaa50`；本章是**新增一个只读遥测面**，不改任何既有行为）。
> ★ **引用归属约定**：mini 侧引用一律写成 `mini-hermes/<路径>:<行号>`；**未带 `mini-hermes/` 前缀的裸 `文件:行号` 一律指真实侧**。

[TOC]

## 发布前维护 · 逻辑请求计数

逻辑请求计数在观察者开关之外更新，未注册观察者时 `api_calls` 仍准确。回退和摘要各是新的逻辑请求；传输层重试仍属于原逻辑请求。`iterations_used` 仅为本轮预算消费增量，不能替代请求计数；CLI 与网关持久化使用 `api_calls`，不能把累计预算重复加进会话用量。

这些是当前实现的行为；下文的章节提交、规模表和实测输出保留各自历史时点。新增回归在 `mini-hermes/tests/test_publish_regressions.py`，完整收据见[发布前修复记录](发布前修复记录.md)。



---

> 配套《中文精译-15-ObserverHooks.md》。那篇讲**原文说了什么**，这篇讲**Hermes 与 mini 差在哪、为什么这么切**。
> **阅读顺序**：先读「导言 · 机制总览」（只有 Hermes 一侧），再看 §〇 差异总表，最后逐节看四块对照。
> 📌 **两边的锚，别混**：**真实 Hermes 侧**行号锚在上游 commit **`f5d1926110`**（`version = "0.21.3"`），
> 裸写 `文件:行号` 一律指**真实侧**；**mini 侧**行号按**当前维护后的实现**核对（本章历史实现提交 `1ccaa50`），
> mini 侧引用**一律带前缀** `mini-hermes/<路径>:<行号>`。
> **两边规模**（`wc -l`）：真实侧 `hermes_cli/plugins.py` **2147** · `hermes_cli/plugins_dispatch.py` **530** ·
> `agent/api_request_hooks.py` **196** · `model_tools.py` **987** · `agent/plugin_stream_hooks.py` **165** ·
> `hermes_cli/middleware.py` **239** · `plugins/observability/langfuse/__init__.py` **1026**；
> mini 侧 `mini-hermes/agent/observer.py` **461** · `mini-hermes/run_agent.py` **1704** ·
> `mini-hermes/tests/test_observers.py` **994** · `mini-hermes/demos/ch15_real_run.py` **368**。

---

## 节 ↔ 精译 对照（本篇只讲差异与现状，原文机制见精译-15）

| 本篇 | 精译-15 对应节 | 讲什么 |
|---|---|---|
| 导言 · 机制总览 | §0 | 机制总览（**只有 Hermes 一侧**） |
| §〇 差异一览表 | — | 20 个面的现状对照（一眼看全） |
| §一 契约 | §1 契约 | 注册形状 · 宽容 · 不去重保序 · 返回值两类 · 版本字段 |
| §二 关联 id | §2 关联 id | 5 个身份字段与生成点 · `None → ""` 线协议 |
| §三 事件族 | §3 事件族 | 6 族 39 名 ↔ mini 只落 7 个 |
| §四 载荷安全 | §4 载荷安全 | 键级 + 值级 + 三级截断 + 总量上限 |
| §五 性能 | §5 性能 | 零成本快路径（fire-site 的 gate-before-invoke） |
| §六 写一个观察者插件 | §6 写一个观察者插件 | 插件宿主 ↔ 进程级注册表 |
| §七 现有消费者 | §7 现有消费者 | Langfuse / Relay ↔ mini 的 demo 与夹具 |
| §八 实现期真问题 | — | 三件真炸过的事（现象 → 根因 → 判据） |
| §九 收据 | — | 套件 · 反验 7 条 · 真 API 收据 |
| §十 与真实版的差距清单 | — | B 批 10 条 + D1~D3 |
| 附录 A · B · C | 附录 A · B · C | 引用汇编 / 两边规模 / 上游出入（搬自精译-15） |

---

## 导言 · 机制总览：Hermes 具体怎么做的（先读这一节）

「观察者钩子」在 Hermes 里**不是一个新模块**，而是一条早就存在的派发通道被追认成契约：39 个钩子名
（`hermes_cli/plugins.py:108-199`）由插件宿主统一登记，`invoke_hook` 在既有的生命周期边界上把它们逐个调起来。
观察者只是**其中一种消费者** —— 只读、返回值默认忽略、拿到的是脱敏过的 JSON 副本。

本节只讲三件事：**谁在管**（注册表 / 派发核 / 快路径）· **什么时候发生**（7 个钩子的时机与关联 id）·
**坏掉会怎样**（失败放开 / 超时隔离 / 只读语义）。本节只讲 Hermes 一侧（真实侧行号锚 `f5d1926110`）；
mini 的对照从 §〇 开始，逐节四块对照见后续各节。

### 0.1 谁在管这件事：一张 dict、一个派发核、一次 dict 探针

| 角色 | 是什么 | 落点 | 要害 |
|---|---|---|---|
| **注册表** | 一张 `事件名 → [回调]` 的表：`self._hooks: Dict[str, List[Callable]] = {}` | `hermes_cli/plugins.py:1152`（归 `PluginManager` 持有，`hermes_cli/plugins.py:1134`） | 插件侧入口 `PluginContext.register_hook`（`hermes_cli/plugins.py:916-918`），真正 append 在 `_track_callback`（`:927-938`） |
| **派发核** | `invoke_hook(hook_name, **kwargs) -> List[Any]` | `hermes_cli/plugins_dispatch.py:188-220`（共 33 行，全部逻辑都在里面） | 注入版本字段 → 逐个回调 → 只收非 `None` 返回值 |
| **快路径** | `has_hook()` —— 一次 dict 探针 | `hermes_cli/plugins.py:1738-1743`（模块级，先 lazy discover）· `hermes_cli/plugins_dispatch.py:445-447`（manager 方法本体） | 「要不要发」只有这一个事实源；fire-site 拿它短路 |

三个角色的分工压成一句：**注册表是唯一的状态，派发核是唯一的调用点，`has_hook()` 是唯一的门。**
真实侧在插件宿主之上还有一层门面：`hermes_cli/lifecycle.py:26-29` 的 `invoke_hook` **先**通知第一方观察者
（`hermes_cli/observability/` 那条 NeMo Relay 共享指标路径），**再**走插件钩子；配套的
`has_hook`（`hermes_cli/lifecycle.py:32-44`）也是两处都问。所以「有没有人消费这个钩子」这一问，
在真实侧有**两个**来源 —— 这是它和 mini 单表结构最大的形态差。

### 0.2 什么时候发生：7 个钩子的时机

`VALID_HOOKS`（`hermes_cli/plugins.py:108-199`，实测 **39** 个名字）覆盖会话、回合、请求、工具、审批、子代理、
流式、网关、看板九类边界。本章的对照只围绕**一条 agent 轮次主路径上的 7 个**：它们成对地框住「进来 / 出去」，
也是 mini 侧同名接线的 7 个（`mini-hermes/agent/observer.py:165-173`）。

| 钩子 | 何时触发 | 真实侧触发点 |
|---|---|---|
| `pre_llm_call` | 某个用户回合的工具循环开始**之前** | `agent/turn_context.py:665-688`（调用在 `:677`） |
| `post_llm_call` | 该回合以最终助手输出结束**之后**（工具循环跑完才发，**一次/回合**） | `agent/turn_finalizer.py:411-441`（调用在 `:433`） |
| `pre_api_request` | 紧邻一次 provider API 请求**之前** | `agent/turn_api_request.py:50-82` |
| `post_api_request` | 一次**成功的** provider 响应之后 | `agent/turn_response_intake.py:61-87` |
| `api_request_error` | 一次失败的 provider 请求或可重试的错误路径之后 | `agent/api_request_hooks.py:161-196` |
| `pre_tool_call` | 在通过护栏（guardrail）批准的工具分发**之前** | `agent/tool_executor.py:637-650` → `hermes_cli/plugins.py:1797-1840` |
| `post_tool_call` | 工具分发**之后** —— 完成 / 取消 / 阻断 / 出错都算 | `model_tools.py:685-698`（门 `:687`，发出 `:691`） |

两处口径要留意：`post_llm_call` 是**回合级收尾**（工具循环跑完之后才发一次），`post_tool_call` 是**每次工具分发**
都发 —— 两者不在同一个粒度上，所以「一轮对话里它们各响几次」没有统一答案。另外 `pre_tool_call` 是这 7 个里
**唯一带否决权**的（还能返回 `block` / `approve` / `modify`，`hermes_cli/plugins.py:1810-1840`），
其余 6 个的返回值要么被忽略、要么只在收尾处被采纳（§0.4）。

### 0.3 具体做什么：五步

**① 注册。** `ctx.register_hook(name, cb)` → `_track_callback`：名字不在 `VALID_HOOKS` 里只记一条 warning，
**仍然写入**（`hermes_cli/plugins.py:916-918` 的 docstring 逐字 *unknown names warn but are still stored*；
append 在 `:935`）；返回值是一个 host-owned 的可撤销句柄 `PluginRegistration`，`unload()` 时反向解绑。

**② 注入版本。** `invoke_hook` 的第一件事是
`kwargs.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)`（`hermes_cli/plugins_dispatch.py:200-201`），
常量值 `"hermes.observer.v1"`（`hermes_cli/middleware.py:16`）。**唯一例外**是 `gateway_platform_event` ——
注释逐字解释：网关平台事件自成事件本地信封，总线级版本会把无关的适配器载荷拧成一份大一统的兼容契约
（`hermes_cli/plugins_dispatch.py:198-200`）。流式钩子走另一条注入路径（`agent/plugin_stream_hooks.py:66`）。

**③ 截断与脱敏。** 载荷安全在 **fire-site 侧**做，不在派发核里：`agent/api_request_hooks.py:117-132` 的
`_sanitize_hook_payload` 把请求 / 响应 / 错误载荷依次过「键级敏感键 → JSON 化（深度 / 长度 / 序列上限）→
总量字符上限」，超限再降级（§四 展开）。

**④ 逐个调。** `for cb in self._hooks.get(hook_name, [])`（`hermes_cli/plugins_dispatch.py:206`）——
**直接读 dict**：无缓存、无去重、不改顺序。每个回调按**窄签名**收参：`inspect.signature` 只把回调**声明过的**
kwarg 传进去，`**kwargs` 回调拿全量，无法 introspect 则全量（`hermes_cli/plugins_dispatch.py:167-186`）。

**⑤ 收返回值。** 只 append `ret is not None` 的结果，**保持注册顺序**，不做去重或合并
（`hermes_cli/plugins_dispatch.py:215-217`）；怎么用由**调用方**按钩子语义决定 —— `block` 取首个有效指令、
transform 系取首个非空字符串、`pre_verify` 取首个非空 message。

### 0.4 三条不变式：只读、JSON-safe 副本、零成本

**① 只读。** 返回值默认**显式忽略**，源码注释把这件事写死在三处：审批观察者
（`hermes_cli/plugins.py:138-139` 逐字 *returns ignored — plugins cannot veto or pre-answer (use pre_tool_call)*）、
`pre_command`（`:195-197` 逐字 *Return values are IGNORED in v1*）、流式观察者
（`:112-114` 逐字 *payloads are immutable … cannot transform the stream*）。会被采纳的只有 4 个更早的钩子
（`pre_llm_call` / `pre_tool_call` / `transform_tool_result` / `transform_llm_output`）—— 契约的取舍是：
**观测与执行必须分开**，否则任何一个观测插件的 bug 都自动变成行为变更。

**② 规范 API 载荷是 JSON-safe 副本。** 插件拿到的不是原始对象，而是 `_sanitize_hook_payload` 的产物
（`agent/api_request_hooks.py:117-132`）：provider SDK 对象被转成 JSON 兼容结构、大值被设上限、敏感键被
redact。于是「观察者改不动 agent 状态」是**结构上**成立的，不是靠自觉；想要原始对象的老插件只能退回去吃
`request_messages` / `conversation_history` / `assistant_message` 这类历史兼容字段 —— 而那正是新消费者被劝退
不要用的（也正因此，原文 §5 才要提醒作者「别对已经脱敏过的载荷再做深拷贝」）。

**③ 零成本快路径。** 门开在 **fire-site**，不开在 `invoke_hook` 内部 —— 派发核即便无回调也仍会解析一次超时配置
再遍历空列表。真正的成本判据在 `hermes_cli/kanban_db_dispatch.py:1197-1201` 的注释里（逐字节选）：

```text
Cost rule: every call site short-circuits on has_hook(), so when nothing subscribes
no payload is built and the hot paths (each dispatcher tick, each task write) pay one
dict probe.
```

代价**在构造载荷**（一次工具结果可能几万字符），不在探针；所以门必须开在**构造之前**。真实侧整条派发链上
遍布这道门：`model_tools.py:687`、`agent/turn_api_request.py:52`、`agent/turn_response_intake.py:63`、
`agent/api_request_hooks.py:171`、`hermes_cli/kanban_db.py:180-188` 等。

### 0.5 出错了怎么办：失败放开有边界

| 情形 | 行为 | 落点 |
|---|---|---|
| 回调**抛异常**（任意钩子） | 捕获 → 记一条 WARNING → **continue**（该回调不产生结果，其余照跑，不 re-raise） | `hermes_cli/plugins_dispatch.py:218-219`、处置 `:222-244` |
| 超时（12 个**有界**钩子） | worker 被**抛弃不 join**，本次返回「跳过」 | `hermes_cli/plugins_dispatch.py:41-45`、理由 `:25-40`、写回 `:306-318` |
| 超时（`pre_tool_call`） | **fail-closed**：append 阻断指令 `{"action": "block", "message": …}` | `hermes_cli/plugins_dispatch.py:48`、`:211-212`、常量 `:53` |
| 仍在跑 / 刚被抛弃 / 抑制窗口内（60s） | 同一回调的后续调用被跳过 | `hermes_cli/plugins_dispatch.py:255-268`、`：52` |
| 不在有界白名单里的钩子 | **同步跑到底**，没有超时 | `hermes_cli/plugins_dispatch.py:159-163`、`:31-32` |
| `BaseException`（`KeyboardInterrupt` / `SystemExit`） | **不吞**，原样穿出去 | `hermes_cli/plugins_dispatch.py:218` 只捕 `except Exception` |

**要害**：「失败放开」是**异常面**的性质，不是「我的钩子绝不影响执行」的全面保证。超时面上，
`pre_tool_call` 是 fail-closed（阻断工具），其余有界钩子只是这一次不上报；一个观察者插件不能据此假设
自己绝不会影响执行（这是原文 §1 那句话最容易被读宽的地方）。

另一处工程细节值得单看：**告警是去重的**。`_report_hook_failure`（`hermes_cli/plugins_dispatch.py:222-244`）
按 `(hook, callback, error)` 只报**一次** WARNING，重复降到 DEBUG，理由是「一个签名写错的回调会每小时刷
~1700 行 WARNING，把真信号埋掉」（`#111922`）；key 用 `__module__` / `__qualname__` 而不是 `id()`
（CPython 会回收 `id`，插件重载后指错对象）。

### 0.6 跨层的那一半：网关目录式钩子 vs agent 层观察者

真实侧**并存两套**钩子宿主，同名不同源，这是读这份契约最容易踩的坑：

| 层 | 机制 | 谁配 | 事件面 |
|---|---|---|---|
| **网关目录式钩子** | 用户目录：`<hermes home>/hooks/<名>/` 里放 `HOOK.yaml` + `handler.py`（`def handle(event_type, context)`） | 用户（写目录） | `gateway:startup` · `session:start` / `end` / `reset` · `agent:start` / `step` / `end` · `command:*`（`gateway/hooks.py:1-11`，全文件 181 行） |
| **agent 层观察者（本章）** | 39 名 `VALID_HOOKS` 经 `invoke_hook` 派发，载荷是 kwargs | 插件（`register(ctx)`） | 会话 / 回合 LLM / 请求 API / 工具 / 审批 / 子代理 / 流式 / 看板 / 网关平台事件 |

关键在于**会话级事件在真实侧属于观察者契约**：`on_session_start`（`agent/conversation_loop.py:770-776`）、
`on_session_end`（`agent/turn_finalizer.py:658-671`）、`on_session_finalize` / `on_session_reset`
（`hermes_cli/cli_session_mixin.py:427-436`、`gateway/slash_commands_session.py:210-212`）。语义上它们是
**会话身份**边界，不是「一轮对话」边界 —— `on_session_end` 每条消息都响一次，真正的身份清理只在
CLI 退出、`/new`、`/reset` 或网关拆会话时发生一次（§一 的会话族会讲这条取舍）。

还有一条只有跨面才看得见的规矩：`gateway_platform_event` 是总线派发里**唯一不注入**总线级版本字段的钩子，
因为它的事件本地信封由各平台适配器自己定（`hermes_cli/plugins.py:173-190` 注释 +
`hermes_cli/plugins_dispatch.py:198-200`）。

### 0.7 一轮的完整时间线

把 7 个钩子按**实际触发顺序**画出来（含关联 id 的生成点）：

```text
[一次 run_conversation：用户消息进历史]
   task_id 生成                     agent/turn_context.py:473
   turn_id 生成                     agent/turn_context.py:476-478   ← 同一轮所有事件共用它
   on_session_start（新会话首次构建后） agent/conversation_loop.py:773
   ▼
   pre_llm_call                     agent/turn_context.py:677
   ▼
   api_request_id 生成              agent/conversation_loop.py:1536  ← 每次真发请求一个
   pre_api_request                  agent/turn_api_request.py:62
        ├─ 成功 → post_api_request      agent/turn_response_intake.py:64
        └─ 失败 → api_request_error     agent/api_request_hooks.py:174
   ▼（模型要求调工具）
   pre_tool_call                    agent/tool_executor.py:643 → hermes_cli/plugins.py:1810
   tool_call_id 来自 provider        agent/agent_runtime_helpers.py:267
   post_tool_call                   model_tools.py:691（blocked / error 路径也发）
   ▼ 回到 API 请求阶段（工具循环再来一圈；不重发 pre_llm_call）
   ▼（模型给出最终回答）
   post_llm_call                    agent/turn_finalizer.py:433
   on_session_end                   agent/turn_finalizer.py:659-660
```

注意这里有一处**顺序陷阱**：若按「事件对的进入 / 退出」读，
`pre_llm_call → pre_api_request → post_api_request → post_llm_call → pre_tool_call → post_tool_call`
看起来像一条直线，但实际触发顺序里 `post_tool_call` 在 `post_llm_call` **之前** —— 前者的口径是「每次 provider
尝试 / 每次工具分发」，后者是「整个回合收尾」。id 也一样分层：`turn_id` 一轮一个、`api_request_id` 每个上游请求尝试一个；mini 的底层传输重试不拆分，`tool_call_id` 由 provider 给。

### 验收四问

1. **一个观察者插件最少要写什么？** 两行：一个 `def cb(**kwargs)` + 一次 `ctx.register_hook("post_tool_call", cb)`
   （`hermes_cli/plugins.py:916-918`）。`**kwargs` 是「载荷加法式演进」的载体，但**窄签名也合法** ——
   `inspect.signature` 会把回调没声明的字段扣下（`hermes_cli/plugins_dispatch.py:167-186`）。（§0.3）
2. **回调抛异常、或者干脆挂死，会怎样？** 抛异常：捕获 + 一条 WARNING + 继续，不 re-raise
   （`hermes_cli/plugins_dispatch.py:218-219`、`:222-244`）；挂死：只有 12 个有界钩子
   （`:41-45`）走超时 worker，被抛弃不 join，其中 `pre_tool_call` 是唯一 fail-closed 的
   （`:48`、`:211-212`）；`BaseException` 不被吞（`:218`）。（§0.5）
3. **一个插件都没装时，这套机制要付什么代价？** 一次 dict 探针。门在 fire-site（`has_hook()`，本体
   `hermes_cli/plugins_dispatch.py:445-447`），因为真正的成本是构造脱敏载荷，所以每条调用点都要在
   **构造之前**短路（`model_tools.py:687`、`agent/turn_api_request.py:52`、`agent/turn_response_intake.py:63`、
   `agent/api_request_hooks.py:171`）。（§0.4）
4. **插件拿到的是原始对象吗？能不能靠返回值改行为？** 规范 API 字段不是原始对象：`_sanitize_hook_payload`
   给的是脱敏截断后的 JSON-safe 派生结构（`agent/api_request_hooks.py:117-132`）；返回值默认忽略，只有
   4 个更早的钩子被采纳（`hermes_cli/plugins.py:138-139`、`:195-197`）。（§0.3 · §0.4）

---

## §〇 差异一览表

**一句话点题**：本章在 mini 侧落的是**同一条契约的最小可运行内核** —— 一张事件表、一次 `has()` 探针、
逐回调隔离、一套载荷安全三件套、5 个关联 id；真实侧那套**插件宿主 + 超时隔离线程 + 中间件 + 导出器 +
运维面**整体没有对应的基座，逐条写清「为什么这里没有它」。

口径：「真实 Hermes」列给 `文件:行号`（锚 `f5d1926110`）；「mini 版」列一律写**现状**，
四档 `✅ 已抄（1ccaa50）` / `已对齐` / `⬜ 没做（量级）` / `⏭ 不抄（理由）`；「性质」列是它的短形式。
前 10 行是本章真接线的（A 批），后 10 行是没做 / 不抄的（B 批）。

| 面 | 真实 Hermes | mini 版 | 性质 |
|---|---|---|---|
| **契约宽容 · 未知事件名** | 不在 `VALID_HOOKS` 里：`logger.warning` 后**仍然 append**（`hermes_cli/plugins.py:932-935`；docstring 逐字 *unknown names warn but are still stored*，`:917`） | ✅ 已抄（1ccaa50）：同形同语义，警告里带**全部**合法名（`mini-hermes/agent/observer.py:357-369`；名字表 `:165-176`） | 已抄(1ccaa50) |
| **契约宽容 · 不去重 + 保序** | 无条件 `mapping.setdefault(key, []).append(callback)`（`hermes_cli/plugins.py:935`；遍历保序 `hermes_cli/plugins_dispatch.py:206`） | ✅ 已抄（1ccaa50）：内部用 `list` 而非 `set`，同一回调登记两次就被调两次（`mini-hermes/agent/observer.py:352-354`、`:369`、`:400-402`） | 已抄(1ccaa50) |
| **零成本快路径** | 门在 fire-site：`has_hook()` = 一次 dict 探针（`hermes_cli/plugins_dispatch.py:445-447`；模块级 `hermes_cli/plugins.py:1738-1743`）；`invoke_hook` 内部**没有** early-return | ✅ 已抄（1ccaa50）：调用点先 `if self._observers.has(...)` 再构造载荷（`mini-hermes/run_agent.py:601-603`、`:1057-1069`、`:1227-1229`、`:1297-1302`；`emit` 内还做一次防御性再探 `mini-hermes/agent/observer.py:424-426`） | 已抄(1ccaa50) |
| **失败放开 · 逐回调隔离** | `for cb …: try … except Exception → _report_hook_failure`，不 re-raise、其余回调照跑（`hermes_cli/plugins_dispatch.py:207-219`） | ✅ 已抄（1ccaa50）：逐个回调独立 `try/except Exception` + 一条 warning（`mini-hermes/agent/observer.py:435-442`）；**告警去重**（`(hook, callback, error)` 只报一次）是 ⬜ 没做（量级）—— mini 每次失败都记一条 | 已抄(1ccaa50) |
| **载荷安全 · 键级脱敏** | 5 个敏感键 + `*_api_key` 后缀，命中替换成 `"<redacted>"`（`agent/api_request_hooks.py:15`、`:59-64`） | ✅ 已抄（1ccaa50）：同键集、同判定、同标记（`mini-hermes/agent/observer.py:185`、`:188`、`:213-222`、`:272`），**另加一层值级文本脱敏**（`:248-249`，复用第 5 章的 7 条模式） | 已抄(1ccaa50) |
| **载荷安全 · 截断、总量上限、副本** | 深度 8 / 字符串 8000 / 序列 200（`agent/api_request_hooks.py:66-69`）+ 总量 50000（env 覆盖、下限 1000，`:51-57`）+ 两级降级（`:117-132`）；给插件的是**副本**，所以原文 §5 才提醒别二次深拷贝 | ✅ 已抄（1ccaa50）：三档常量逐字同值（`mini-hermes/agent/observer.py:190-197`）、`hook_jsonable` 同形**含 dict `break` 与 list 切片两种不同的溢出语义**（`:265-279`）、`sanitize_payload` 两级降级（`:296-317`）；env 名改成本地的 `MINI_OBSERVER_PAYLOAD_MAX_CHARS`（`:194-195`） | 已抄(1ccaa50) |
| **关联 id · 5 字段** | 冻结 dataclass `_CallIds`（task / session / tool_call / turn / api_request），`hook_kwargs()` 一律 None → `""`（`model_tools.py:642-653`，逐字 *hook/middleware wire contract*） | ✅ 已抄（1ccaa50）：同 5 字段、同形状、同归一（`mini-hermes/agent/observer.py:323-340`）；装配点 `mini-hermes/run_agent.py:1315-1331`；三个 id 的生成点见 `mini-hermes/run_agent.py:406-410`、`:1063`、`:1223` | 已抄(1ccaa50) |
| **版本字段** | `kwargs.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)`（`hermes_cli/plugins_dispatch.py:200-201`），值 `"hermes.observer.v1"`（`hermes_cli/middleware.py:16`）；例外只有 `gateway_platform_event` | ✅ 已抄（1ccaa50）：在**唯一装配出口** `emit()` 注入，值 `"mini.observer.v1"`（`mini-hermes/agent/observer.py:178-179`、`:429`）；调用点一律不手写（`mini-hermes/run_agent.py:1311-1312`） | 已抄(1ccaa50) |
| **事件名表 · 7 vs 39** | `VALID_HOOKS` **39** 名（`hermes_cli/plugins.py:108-199`），兼作 shell-hook 白名单（`:201-203`） | ✅ 已抄（1ccaa50）：只落**真接线**的 **7** 名（`mini-hermes/agent/observer.py:165-173`）；其余 32 名是 ⬜ 没做（量级）—— 表里没有触发点的名字等于给读者一个「能用」的假承诺 | 已抄(1ccaa50) |
| **只读语义 · 返回值忽略** | 只有 4 个更早的钩子被采纳（`pre_llm_call` / `pre_tool_call` / `transform_tool_result` / `transform_llm_output`）；其余显式忽略并在注释里写死（`hermes_cli/plugins.py:138-139`、`:195-197`、`:112-114`） | ✅ 已抄（1ccaa50）：`emit()` 直接丢弃返回值、不交给任何人（`mini-hermes/agent/observer.py:418-419`、`:439`）；想阻断走 `approval_callback`（`mini-hermes/run_agent.py:1250-1297`）；**那 4 个改行为钩子**是 ⏭ 不抄（理由）：第二套控制流 | 已抄(1ccaa50) |
| **超时隔离线程** | 12 个有界钩子跑在超时 worker 上，超时**抛弃不 join**（`hermes_cli/plugins_dispatch.py:41-45`；理由逐字 *abandon without join — joining reintroduced a shutdown hang*，`:25-40`）；默认 30s / 上限 600s（`:138-139`） | ⏭ 不抄（理由）：mini 的观察者是**同步**调用，没有「钩子跑在工作线程里」这个基座；没有宿主超时包装；慢回调仍会阻塞调用线程（mini 侧自记见 `mini-hermes/agent/observer.py:128-130`） | 不抄(理由) |
| **`pre_tool_call` 超时 fail-closed** | 唯一的 fail-closed 集（`hermes_cli/plugins_dispatch.py:48`）：超时 ⇒ append `{"action": "block", "message": _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE}`（`:211-212`、常量 `:53`） | ⏭ 不抄（理由）：依赖上一行的超时基座；没有超时机制时这条分支永不触发 | 不抄(理由) |
| **抑制窗口 / gate key** | 60s 抑制窗口（`hermes_cli/plugins_dispatch.py:52`、跳过判据 `:255-268`）；gate key = `(hook, id(cb), 调用身份)`，身份只吃 `tool_call_id` / `turn_id`（`:143-156`），超时后按身份判再标 abandoned（`:306-318`） | ⏭ 不抄（理由）：整族依赖超时基座；mini 没有超时回调线程堆积；并行工具仍可能并发调用观察者，回调应自行保证线程安全 | 不抄(理由) |
| **插件宿主** | `register(ctx)` + `PluginContext`（`hermes_cli/plugins.py:225-231`、`:916-918`）+ 通用插件发现 / 加载 / 卸载 + 可撤销句柄 `PluginRegistration` | ⏭ 不抄（理由）：mini 没有通用插件系统与热重载；同样是「订阅」，宿主换成**进程级注册表 + `observer=` 构造参数**（`mini-hermes/agent/observer.py:445-447`；`mini-hermes/run_agent.py:249-252`、`:326`） | 不抄(理由) |
| **中间件层** | 独立一层：`MIDDLEWARE_SCHEMA_VERSION = "hermes.middleware.v1"`，中间件载荷多注入一个字段（`hermes_cli/middleware.py:17`、`:44-47`）；改行为的请求 / 执行包装器属它、不属观察者契约 | ⏭ 不抄（理由）：mini 没有中间件层（全仓 `middleware` 零命中）；观察者只读，没有「改行为的包装器」要与之分家 | 不抄(理由) |
| **专用族与事件总线** | 流式观察者走另一条注入路径（`agent/plugin_stream_hooks.py:66`）；事件总线带递归深度 cap 8 与 pending budget 64（`hermes_cli/plugins_dispatch.py:87-93`）；另有系统提示词 section 注册与渲染（`:453-470`） | ⏭ 不抄（理由）：mini 没有 token 热路径 / 流式输出（`mini-hermes/run_agent.py:19-20` 自记未实现），没有总线订阅者，也没有 prompt section 注入需求 | 不抄(理由) |
| **导出器与现有消费者** | Langfuse 插件登记 **11** 个钩子（`plugins/observability/langfuse/__init__.py:1014-1026`）；NeMo Relay 走第一方路径映射 **13** 个钩子（`hermes_cli/observability/relay_shared_metrics.py:935-950`）；`hermes_cli/lifecycle.py:26-44` 把两条路并成一个门面 | ⏭ 不抄（理由）：mini 没有插件宿主、没有网络导出；它的消费者 = demo 与测试夹具（`mini-hermes/demos/ch15_real_run.py`、`mini-hermes/tests/test_observers.py`），只为「契约可测」服务 | 不抄(理由) |
| **签名筛 kwargs** | `inspect.signature` 只把回调**声明过的** kwarg 传进去，`**kwargs` 回调拿全量，无法 introspect 则全量（`hermes_cli/plugins_dispatch.py:167-186`） | ⏭ 不抄（理由）：mini 载荷本来就是 kwargs 字典、回调统一 `**kwargs` 接收；且同一个回调可以同时听多个事件，所以 mini 还要往里塞一个 `event=`（`mini-hermes/agent/observer.py:437-439`）—— 多一层 inspect 只增加出错面 | 不抄(理由) |
| **运维面与注册 ledger** | `hermes hooks list / test / revoke / doctor` CLI（`hermes_cli/hooks.py:1`，文件 335 行），把合成载荷喂给 `run_once()`；与观察者通道的唯一耦合是复用 `VALID_HOOKS` 当事件白名单（`:167-173`）；注册面另有 ledger 反向解绑 | ⏭ 不抄（理由）：那是 config-owned shell hooks 的运维面 + 测试夹具，不含派发逻辑；mini 不卸载插件，可撤销句柄也无收益 | 不抄(理由) |
| **会话级事件归属** | 会话钩子**属于**观察者契约：`on_session_start`（`agent/conversation_loop.py:770-776`）、`on_session_end`（`agent/turn_finalizer.py:658-671`）、`on_session_finalize` / `on_session_reset`（`hermes_cli/cli_session_mixin.py:427-436`、`gateway/slash_commands_session.py:210-212`） | ⏭ 不抄（理由）：mini 的会话 / 进程边界归 `gateway/hooks.py` 的**目录式钩子**（`mini-hermes/gateway/hooks.py:28-37`），agent 层观察者只覆盖 turn / API / 工具三个 **agent 内**边界 —— 划成两层、各有所有者，而不是同一个表里混两种消费者 | 不抄(理由) |

**读法**：前 10 行里真正**新写**的是「事件表 7 名 + `ObserverRegistry` + 载荷装配出口 + 零成本门」
四件事，其余是形状照抄（同键集、同阈值、同 `None → ""`）；后 10 行全部是**结构性没有基座**，
不是「可以再补一点」—— 真实侧那五套东西（超时线程、中间件、总线 / 流式、插件宿主、运维面）
在 mini 里各自缺一整个子系统，缺任何一个，对应的钩子分支就永远是死代码。

---

## §一 契约

**① Hermes 怎么做的**：观察者契约管的是**注册的形状**，不是钩子名（名字那一半在 §三）。插件从 `register(ctx)` 里调 `ctx.register_hook(name, callback)`，宿主把回调塞进**一张 `事件名 → [回调]` 的表**（`PluginManager._hooks`，`hermes_cli/plugins.py:1152`）。这张表的三条规矩合起来定义了"什么算一个观察者"：

| 规矩 | 怎么做 | 不这样做会坏在哪 |
|---|---|---|
| **未知名字只警告仍存** | 名字不在 `VALID_HOOKS` 里 ⇒ 记一条带**全部**合法名的 warning，**然后照存** | `raise` 会让一个错别字炸掉整个插件装载（乃至启动）；静默丢弃则用户永远不知道"我听的钩子根本没人发" |
| **不去重** | `mapping.setdefault(key, []).append(callback)` —— 无条件 append，用 `list` 不用 `set` | 去重要先比回调身份，而"同一回调听两个事件"与"同一回调把同一事件听两遍"就再也分不出来；观察者是**订阅**语义，"登记两次 = 调两次"是订阅者自己的选择 |
| **保序** | 派发时按登记顺序遍历（`hermes_cli/plugins_dispatch.py:206` 直接 `for cb in self._hooks.get(...)`） | 消费者若想依赖"我比另一个观察者先看到"，顺序就必须由**注册顺序**决定，而不是哈希顺序 |

另一条主脉是**收参 / 返回值的纪律**。回调应当接受 `**kwargs`（或只签它要用的字段），于是载荷能**加法式演进** —— 加字段不破坏老回调。返回值只有两类：**绝大多数是纯观察者，返回值被忽略**；只有 4 个**更早的、会改变行为**的钩子其返回值被采纳（`pre_llm_call` / `pre_tool_call` / `transform_tool_result` / `transform_llm_output`），而遥测类插件应当把这 4 条当作**可选的历史兼容特性**，不是可观测性的必要条件。砍掉 `**kwargs`，每次加字段都变成破坏性变更；砍掉"返回值默认忽略"，观测插件就长成了事实上的中间件 —— 这两条正是"只增不改"与"拿不到就别造"。

最后一条是**版本字段**：派发核在**唯一出口**给每一份载荷注入 `telemetry_schema_version`，插件不必自己带、也不该自己带。

**② 读 Hermes 的代码** —— `hermes_cli/plugins.py:916-918` + `:927-935`（注册入口 + 真正的登记逻辑）：

```python
    def register_hook(self, hook_name: str, callback: Callable) -> PluginRegistration:
        """Register a lifecycle hook callback (unknown names warn but are still stored)."""
        return self._track_callback("hook", hook_name, callback, self._manager._hooks, VALID_HOOKS)
...
        """Append ``callback`` under ``key`` (warning on unknown ``key``) and lease its removal."""
        if key not in valid:
            logger.warning("Plugin '%s' registered unknown %s '%s' (valid: %s)", self.manifest.name, kind,
                           key, ", ".join(sorted(valid)))
        mapping.setdefault(key, []).append(callback)
```

**要害**：三件事都落在同一段里 —— **警告**（`if key not in valid:`）、**照存**（`mapping.setdefault(...).append(...)` 待在 `if` **之外**）、**不去重**（`append` 无条件）。`:917` 那句 docstring 就是这三条的名字："unknown names warn but are still stored"。另注意 `register_hook` 与 `register_middleware`（`:920-926`）**共用**这个 `_track_callback`，区别只在传进去的 `valid` 集合与目标 `mapping` —— **观察者与中间件在注册面同形，分岔点在返回值的采纳上**。

**③ mini 的实现（现状）**：✅ 已抄（`1ccaa50`）。mini 没有插件宿主，也就**没有 `ctx`** —— 订阅者直接调注册表 `ObserverRegistry.add()`（`mini-hermes/agent/observer.py:357`）。三条规矩逐条对上：

```python
def add(self, event: str, callback: Callable[..., Any]) -> None:
    if event not in VALID_EVENTS:
        logger.warning(
            "registered unknown observer event '%s' (valid: %s)",
            event, ", ".join(sorted(VALID_EVENTS)))
    self._callbacks.setdefault(event, []).append(callback)
```

（`mini-hermes/agent/observer.py:363-369`；`VALID_EVENTS = frozenset(OBSERVER_EVENTS)` 在 `:176`，即 mini 版的 `VALID_HOOKS`。）

底层的容器就用 `Dict[str, List[Callable]]`（`mini-hermes/agent/observer.py:353-354`），注释逐字点明"保序、允许重复（**不去重**）—— 用 list 而不是 set 就是为了这两条"。**返回值的两类在 mini 塌成一类**：`emit()` 一律丢弃回调返回值（`mini-hermes/agent/observer.py:435-442`），模块 docstring 把替代路径也写明 —— 想在 mini 里阻断/改写一条工具调用请走 `approval_callback`，"观察者**不是**第二套控制流"。**版本字段**注入在唯一出口 `emit()` 里（`mini-hermes/agent/observer.py:429`），常量 `TELEMETRY_SCHEMA_VERSION = "mini.observer.v1"`（`:178`）—— **若同名，两边的收据就分不清谁是谁了**。mini 也不返回句柄：真实侧 `add` 的对应物另给一个可回滚的 `PluginRegistration`，mini 不卸载插件所以没有句柄（B8）。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 注册入口 | `ctx.register_hook(name, cb)`，经插件宿主 + manager（`hermes_cli/plugins.py:916-918`） | ✅ 已抄（1ccaa50）：直接 `ObserverRegistry.add()`（`mini-hermes/agent/observer.py:357`）——mini 无 `ctx`，订阅者就是进程内代码 |
| 未知名字 | warn-but-store，警告带全部合法名（`:932-935`） | ✅ 已抄：同形（`mini-hermes/agent/observer.py:363-369`） |
| 不去重 / 保序 | `setdefault(...).append(...)`，`list` 保序（`:935`） | ✅ 已抄：同一形状（`mini-hermes/agent/observer.py:369`） |
| 返回值纪律 | 4 个改行为的钩子采纳，其余忽略（`hermes_cli/plugins.py:138-142`、`:195-197`） | ⏭ 不抄（理由）：mini 观察者**一律忽略**返回值 ⇒ 纯只读；那 4 个是**行为钩子**，不属观察面 |
| 版本字段 | `kwargs.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)`，值 `hermes.observer.v1`（`hermes_cli/plugins_dispatch.py:200-201`；`hermes_cli/middleware.py:16`） | ✅ 已抄：唯一出口注入（`mini-hermes/agent/observer.py:429`），值 `mini.observer.v1`（`:178`） |
| 登记句柄 / ledger | `PluginRegistration`，unload 时反向解绑 | ⏭ 不抄（理由）：mini 不卸载插件，句柄无收益（B8） |

---

## §二 关联 id

**① Hermes 怎么做的**：观察者要能**不靠回调顺序**就把事件拼起来 —— 靠的是稳定 id，而且是**显式字段**，不是让消费者去拆一个复合字符串。文档把 `api_request_id` 单独点名"不透明"（**不要解析它的字符串格式**），就是这条规矩的样板：任何按格式切分它的代码都会在实现演进时碎掉。

契约的**线协议面**固定 **5 个字段**（真实侧就是 `_CallIds` 那 5 个），用一个冻结 dataclass 承载，并配一个把 `None` 归一成 `""` 的线协议函数；文档另列了 `api_call_count` 与一组子代理链接字段（见 ④ 末行）：

| 字段 | 含义 | 生成点（真实侧） |
|---|---|---|
| `task_id` | 任务身份，对子代理与隔离执行尤其有用 | `agent/turn_context.py:473` `effective_task_id = task_id or str(uuid.uuid4())` |
| `turn_id` | 用户回合身份，一个回合里的各次 API 尝试与各次工具调用**共享**它 | `agent/turn_context.py:476-478` `f"{agent.session_id or 'session'}:{effective_task_id}:{uuid.uuid4().hex[:8]}"` |
| `session_id` | 会话身份 | `agent.session_id`（如 `agent/turn_context.py:679` 传给 `pre_llm_call`） |
| `api_request_id` | **一次 provider 尝试**的身份，不透明 | `agent/conversation_loop.py:1536` `f"{s.turn_id}:api:{s.api_call_count}"` |
| `tool_call_id` | provider 给出的工具调用 id（如可用） | `agent/agent_runtime_helpers.py:267`（从 provider 的 tool_call 取） |

★ **`None → ""` 是线协议，不是可选优化。** 两条理由：① 在只读遥测里，"没有这个 id"与"id 是空串"**是同一件事**，让 N 个消费者各写一遍 `or ""` 只是把归一成本摊给 N 方；② 空串是**可判定的"无身份"** —— 真实侧的超时门逐字写着 *"an empty id is treated as 'no identity' by the hook gate"*（`tools/terminal_tool_result.py:143`）。归一放在**一处**（`hook_kwargs()`），于是"空"这个语义在所有消费者眼里只有一个字形。

★ **为什么必须"每请求一个 id"**：一次模型调用可能包含回退请求或底层传输重试；观察者边界是否拆分要看具体实现，不能用 HTTP 次数推算。`turn_id` 描述的是"用户这一问"，`api_request_id` 描述的是"这一发"。只有 `turn_id` 的话，两次请求在观察者眼里塌成一条：计价重复、跨度（span）合并、失败被成功那一发掩盖。真实侧把 `api_request_id` 定义成 `{turn_id}:api:{api_call_count}`，恰好让"同一回合的多发"**既可分辨又可聚合**（同前缀即同回合）。工具级同理：同一回合里并发调用同一个工具必须**按调用**分开，不能塌成同一个门 key（`tools/terminal_tool_result.py:142-143` 逐字交代了这条）。

**② 读 Hermes 的代码** —— `model_tools.py:642-653`（5 字段 + 线协议，全在这 12 行）：

```python
@dataclass(frozen=True)
class _CallIds:
    """Identity fields of one tool call, threaded through hooks and middleware."""
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    turn_id: Optional[str] = None
    api_request_id: Optional[str] = None

    def hook_kwargs(self) -> Dict[str, str]:
        """Same fields with None -> "" (hook/middleware wire contract)."""
        return {k: v or "" for k, v in asdict(self).items()}
```

**要害**：`frozen=True` 让它不可变（传进回调的那份没人能改），`hook_kwargs()` 一行把 `None` 归一成 `""`，docstring 直接把 `(hook/middleware wire contract)` 写在里面 —— **观察者和中间件共用同一份 id 线协议**，所以注入点也同形（典型如 `model_tools.py:693` 的 `**_CallIds(...).hook_kwargs()`）。id 的**生成**与 id 的**出站形状**被拆开：生成各在各的边界，出站只有一个函数。

**③ mini 的实现（现状）**：✅ 已抄（`1ccaa50`）。mini 的 `CallIds`（`mini-hermes/agent/observer.py:324-340`）逐字照搬这个形状（同 5 字段、同 `frozen=True`、同 `hook_kwargs()` 的 `{k: v or "" ...}`），并补了一句"为什么空是 `""` 而不是 `None`"的本地理由。取值只有**一个**入口 `_observer_ids()`（`mini-hermes/run_agent.py:1315-1331`）：

```python
def _observer_ids(self, *, tool_call_id=None, api_request_id=None):
    return CallIds(
        task_id=self._current_task_id,
        session_id=self.session_id,
        tool_call_id=tool_call_id,
        turn_id=self._current_turn_id,
        api_request_id=api_request_id,
    ).hook_kwargs()
```

（`mini-hermes/run_agent.py:1325-1331`，逐字去掉了签名行的换行。）

三个生成点在 mini 的落法：**`turn_id`** 在 `run_conversation` 入口**每轮生成一个**（`mini-hermes/run_agent.py:406-408`），形状照 `agent/turn_context.py:476-478`；同一轮内所有事件（LLM / API / 工具）共用同一个，跨轮必不同 —— 注释解释了为什么**不用自增计数器**：网关会缓存复用 agent 对象（第 8 章），计数器会跨会话撞号，uuid 后缀不会。**`api_request_id`** 在 `_one_api_request` 里**每个模型逻辑请求生成一个**（`:966`），序号 `_api_call_count` **每轮归零**（`:371`）—— 于是回退重发是**新的一发**，有独立 id、`attempt` 递增（`mini-hermes/run_agent.py:1047-1050` 交代了这一层为什么单抽出来）。**`tool_call_id`** 优先用 provider 给的（`:1223`，`msg["tool_calls"][i]["id"]`），没给才落空串。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 5 字段载体 | `_CallIds`（frozen dataclass）+ `hook_kwargs()`（`model_tools.py:643-653`） | ✅ 已抄：同形同名（`mini-hermes/agent/observer.py:324-340`） |
| `None → ""` | 线协议，`{k: v or "" ...}`（`model_tools.py:652`） | ✅ 已抄：同（`mini-hermes/agent/observer.py:340`） |
| `turn_id` 生成 | `{session_id}:{task_id}:{uuid4().hex[:8]}`，且优先 relay 挂起的 id（`agent/turn_context.py:476-478`） | 已对齐形状、**略去 relay 分支**（mini 无 Relay）：`mini-hermes/run_agent.py:406-408` |
| `api_request_id` 生成 | `{turn_id}:api:{api_call_count}`（`agent/conversation_loop.py:1536`） | ✅ 已抄：同形（`mini-hermes/run_agent.py:1063`）——只在模型逻辑请求边界生成，回退重发算新一发 |
| `tool_call_id` 来源 | provider 的 tool_call（`agent/agent_runtime_helpers.py:267`） | ✅ 已抄：优先 `msg["tool_calls"][i]["id"]`（`mini-hermes/run_agent.py:1223`），没给才落空串 |
| 子代理链接 id | `parent_session_id` / `child_subagent_id` / `parent_turn_id` / `api_call_count` 等（文档 §2 另列） | ⏭ 不抄（理由）：mini 无子代理实现 ⇒ 这些字段**没有生成点**（见 §三 子代理族） |

---

## §三 事件族

**① Hermes 怎么做的**：文档把观察者钩子按生命周期边界分成 **6 族** —— **会话**（开始 / 结束 / 定稿 / 重置）、**回合级 LLM**（用户回合的进入与收尾）、**请求级 API**（agent 循环里每一次 provider 尝试）、**工具**（单次工具调用的一生）、**审批**（危险命令审批提示的前后）、**子代理**（被委派子 agent 的创建与回收）。真实侧的名字总表 `VALID_HOOKS` 实测 **39** 个（`hermes_cli/plugins.py:108-199`），这 6 族占其中 16 个；其余是**改行为类**（`transform_*` / `pre_verify` / `pre_gateway_dispatch` / `pre_transcription` / `transform_api_error_classification`）、**旁支观察面**（流式 `on_stream_*`、`gateway_platform_event`、`on_room_member_activity`、kanban 六名、`agent_loop_stopped`、`pre_command`、`on_skill_lifecycle`）。

**判据只有一条：每个事件名都必须有真实触发点。** 名字进表不等于会响 —— 所以逐族对照时，要问的是"这一族在**我们这条执行链**上真的会走到吗"：

| 族 | 真实侧的钩子 | mini 落了哪几个 | 为什么 |
|---|---|---|---|
| **会话** | `on_session_start` / `on_session_end` / `on_session_finalize` / `on_session_reset`（4） | **0** | ★ 会话级那族归**网关目录式钩子**（拍板 P4）——mini 的 agent 层未发出会话观察者事件，见下 |
| **回合级 LLM** | `pre_llm_call` / `post_llm_call`（2） | **2** | ✅ 回合是 agent 层的主边界，两端都接得上（`post_llm_call` 取"每次模型调用"口径，与真实侧"每回合一次"有差异） |
| **请求级 API** | `pre_api_request` / `post_api_request` / `api_request_error`（3） | **3** | ✅ 与回退链天然对齐：一次 LLM 调用可能多次 HTTP，失败有独立上报路径 |
| **工具** | `pre_tool_call` / `post_tool_call` / `transform_tool_result`（3） | **2** | `transform_tool_result` 是**改行为**的（替换结果字符串），不属只读观察面 |
| **审批** | `pre_approval_request` / `post_approval_response`（2） | **0** | ⏭ 不抄：mini 的审批是**控制流**（`approval_callback`），只读审批观察面没有第二个消费者 |
| **子代理** | `subagent_start` / `subagent_stop`（2） | **0** | ⏭ 不抄：mini **没有**子代理 / 委派实现 ⇒ 事件名没有触发点 |

★ **会话级那一族为什么归网关（P4）**：真实侧的会话钩子发在 agent 侧的会话边界上（`on_session_start` 在 `agent/conversation_loop.py:774`；`on_session_end` 在 `agent/turn_finalizer.py:660`；`on_session_finalize` / `on_session_reset` 在 `hermes_cli/cli_session_mixin.py:427` 与 `gateway/slash_commands_session.py:210`）。而 mini 的 CLI 与网关都有会话边界，但观察者未订阅会话事件：`session:start` / `session:end` / `session:reset` 由目录式钩子承担（`mini-hermes/gateway/hooks.py:28-37` 的 `HOOK_EVENTS`，用户写 `HOOK.yaml` + `handler.py` 订阅）。这两层的**所有者与语义都不同** —— 网关钩子是"用户可替换的**外部**扩展点"，agent 内观察者是"进程内代码订阅的**只读**遥测"；把会话事件塞进 `OBSERVER_EVENTS`，会立刻出现"两套会话钩子、谁先谁后"的歧义。所以 mini 的观察者只覆盖 **turn / API / 工具**三个 **agent 内**边界，会话级留给目录式钩子。

两条"不落"的纪律也在这里落地：**B10**（`VALID_HOOKS` 全 39 名不抄）= 表里没有触发点的名字等于给读者一个"能用"的假承诺，所以 mini 只落**真接线**的 7 个；**B5**（事件总线 emit/subscribe、递归深度 cap、pending budget）= 那不是 observer hooks 的核心能力，纯规模，mini 不引。

**② 读 Hermes 的代码** —— `hermes_cli/plugins.py:134-142`（总表的**注释本身就是契约**；这里取审批族那一段）：

```python
    # agent_loop_stopped: an agent turn was interrupted mid-run (/stop, or the running-agent
    # fast-path of /new; see gateway/run.py::_interrupt_and_clear_session). Kwargs: session_key,
    # platform, reason, invalidation_reason. Return values are ignored.
    "agent_loop_stopped",
    # Approval observers (tools/approval.py); returns ignored — plugins cannot veto or pre-answer
    # (use pre_tool_call). Kwargs: command, description, pattern_key, pattern_keys, session_key,
    # surface: "cli"|"gateway"|"smart"; post_approval_response adds choice ("once"|"session"|
    # "always"|"deny"|"timeout"|"smart_approve"|"smart_deny") and decided_by.
    "pre_approval_request", "post_approval_response",
```

**要害**：名字旁边那几行注释**就是这一族的事件契约** —— 触发点（"an agent turn was interrupted mid-run"）、返回纪律（"Returns ignored"）、字段清单与值域（`surface` 三值、`choice` 八值加 `decided_by`）。它同时示范了上一条判据：每个名字都在注释里交代了**它在哪响**；写不出这句注释的名字，就是没有触发点的名字。它也点明了 mini 为什么不落审批族 —— 这一族的定义是"**只读、不能否决、不能预先作答**"，而 mini 的审批（`approval_callback`，`mini-hermes/run_agent.py:1252`）恰恰是**唯一能改控制流**的那一处，引一套只读观察面过来只是重复。

**③ mini 的实现（现状）**：✅ 已抄（`1ccaa50`）**3 族共 7 个**事件名 = 2 个回合级 LLM + 3 个请求级 API + 2 个工具。事件表逐字在 `mini-hermes/agent/observer.py:165-173`：

```python
OBSERVER_EVENTS: Tuple[str, ...] = (
    "pre_llm_call",
    "post_llm_call",
    "pre_api_request",
    "post_api_request",
    "api_request_error",
    "pre_tool_call",
    "post_tool_call",
)
```

表的**上方注释**写着 mini 版的表为什么要这么短："7 个；★ 每个名字都有真实触发点"（`mini-hermes/agent/observer.py:17`），而紧挨着每个名字的**触发点**在宿主侧各有落点：`pre_llm_call` / `post_llm_call` 在 `_run_conversation_turn` 的模型调用前后（`mini-hermes/run_agent.py:601-628`）、`pre_api_request` / `post_api_request` / `api_request_error` 在 `_one_api_request` 的 HTTP 前后与异常分支（`:1057-1087`）、`pre_tool_call` / `post_tool_call` 在 `_run_one_tool` 的执行前后（`:1227-1229`、`:1245-1248`）。**7 个名字与 7 个触发点一一对应**，不多不少 —— 没有"表里有、代码里没有"的名字。

**三族不落的原因，逐条对着判据说**：
- **会话族 → 网关目录式钩子（P4）**。mini 的 `HOOK_EVENTS` 已含 `session:start` / `session:end` / `session:reset`（`mini-hermes/gateway/hooks.py:28-37`），由用户放目录订阅；agent 有 `reset_session()`，但没有会话事件发出点，强行另立只会得到"两套会话钩子"。
- **审批族 ⏭ 不抄**：mini 的审批已经是控制流（`approval_callback`），而契约定义审批钩子为**纯观察**（不能投票 / 预先作答 / 否决）—— 把只读面架在已有的控制流旁边，没有第二个消费者，还会引出"谁能拦住工具"的歧义。
- **子代理族 ⏭ 不抄**：mini 没有子代理 / 委派实现（全仓 `grep` 只命中注释），`subagent_start` / `subagent_stop` **没有触发点**，落进表里就是 B10 点名的"假承诺"。
- 另有一个**同族但不同类**的名字被排除：`transform_tool_result` / `transform_llm_output` 属"会改变行为"的那一类（中间件地盘），不进只读观察面。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 事件名总数 | `VALID_HOOKS` **39** 个（`hermes_cli/plugins.py:108-199`） | ⬜ 没做（量级）：只落 **7** 个（`mini-hermes/agent/observer.py:165-173`）——B10 的判据是"表里没有触发点的名字 = 假承诺" |
| 六族覆盖面 | 会话 4 / 回合 LLM 2 / API 3 / 工具 3 / 审批 2 / 子代理 2 | 落了 **3 族共 7 个**：回合 LLM 2 / API 3 / 工具 2；会话、审批、子代理三族均为 **0** |
| 会话级事件归属 | agent 层直接发 `on_session_*`（`agent/conversation_loop.py:774`、`agent/turn_finalizer.py:660`、`hermes_cli/cli_session_mixin.py:427`、`gateway/slash_commands_session.py:210`） | 已对齐**分工**、归**网关目录式钩子**（拍板 P4）：`mini-hermes/gateway/hooks.py:28-37` 的 `session:start/end/reset` |
| 审批族 | `pre_approval_request` / `post_approval_response`，只读、returns ignored（`hermes_cli/plugins.py:138-142`） | ⏭ 不抄（理由）：mini 的审批走控制流 `approval_callback`（`mini-hermes/run_agent.py:1252`） |
| 子代理族 | `subagent_start`（`tools/delegate_tool.py:289`）/ `subagent_stop`（`tools/delegate_tool_results.py:366`） | ⏭ 不抄（理由）：mini 无子代理实现 ⇒ 无触发点 |
| 事件总线 | emit/subscribe + 递归深度 cap / pending budget（`hermes_cli/plugins_dispatch.py:87-93`、`:323-443`） | ⏭ 不抄（理由）：不是 observer hooks 的核心能力（B5），纯规模 |
| 改行为类同族名 | `transform_tool_result`（`model_tools.py:852` 门控）/ `transform_llm_output`（`agent/turn_finalizer.py:420`） | ⏭ 不抄（理由）：改行为 ⇒ 自己的地盘是中间件，不进只读观察面 |

---

## §四 载荷安全（Payload Safety）

**① Hermes 怎么做的**

观察者拿到的**不是**原始对象。契约端在派发之前一次性做掉三件事：把 provider 对象转成 JSON 兼容结构、给大载荷**设上限**、对敏感键做**脱敏（redact）**。之所以必须由宿主做，是因为钩子的入参迟早会是 provider SDK 的原生对象 —— 它们带 client、callback、file handle 这类不可 JSON 的成员，还可能夹着 `Authorization` 头。省掉这一层，每个消费者都要自己重写一遍「能不能序列化 + 要不要脱敏 + 太大怎么办」；做了，代价是消费者拿到的是**派生**结构而非原始对象（想要原始对象的老插件只能继续依赖 `request_messages` / `conversation_history` / `assistant_message` 这类历史字段 —— 而那些字段恰是新消费者被劝退不要用的那批）。

三层，各挡一类失效：

| 层 | 挡的是什么 | 机制（锚 `f5d1926110`） |
|---|---|---|
| **① 键级** | 字段名本身就是凭证：`{"api_key": "sk-…"}` | `_is_sensitive_hook_key`（`agent/api_request_hooks.py:59-64`）：先 `lower()` 再把 `-` 换成 `_`（于是 `API-Key` / `Proxy-Authorization` 也命中），判定 = 命中固定键集 `_SENSITIVE_HOOK_KEYS`（`:15`：`api_key` / `authorization` / `proxy_authorization` / `cookie` / `set_cookie`）**或**后缀 `*_api_key`。命中 ⇒ 值整块换成 `"<redacted>"`（`:95`）；**命中就不再递归**（原始字符串一个字都不剩）。不命中一律照原样 —— 脱敏是**定点**的，不是整份糊掉 |
| **② 值级文本** | 值里夹着凭证，键名完全正常：`{"note": "token=ghp_…"}`、工具结果、`args` 里的 `headers.Authorization` 那一行 | `_hook_jsonable` **自己不做**这一层；真实侧的文本级脱敏住在另一个模块 `agent/redact.py`（**1,315 行**，实测） |
| **③ 三级截断 + 总量上限** | 超深 / 超长的单个值 / 超宽的序列 / 总量爆掉 | 结构阈值三个默认参（`:68-69`）：`max_depth=8`、`max_string=8000`、`max_sequence=200`；越界分别留 `<dict depth limit>`（`:71-72`）、`…[truncated N chars]`（`:76-77`）、`_truncated_items`（`:91-93`、`:99-101`）。总量上限默认 **50000**、env `HERMES_PLUGIN_PAYLOAD_MAX_CHARS` 可覆盖、**下限 1000**（`:51-57` 的 `max(1000, int(raw))`，坏值回落 50000）⇒ 超限走**两级降级** |

三条判据决定了这一层「做对」的样子：

| 判据 | 规则 | 不做会坏在哪 |
|---|---|---|
| 超限的处置是**降级 + 预览**，不是丢弃 | 全量 → `max_string=1000 / max_sequence=50` 再来一次 → 还超就整份换成 `{"_truncated": True, "original_type": …, "preview": 前缀[:limit]}`（`:117-132`） | 丢弃 ⇒ 观察者看到的是「没有数据」，与「压根没观测」分不开；仪表盘上两者长得一样 |
| **两种容器的溢出语义故意不同** | dict 越界 `break`（第 201 项被取出用于判界，之后的项不再访问），标记以**普通键** `_truncated_items` 塞回同一个 dict；list/tuple/set 用 `seq[:200]` **保留前 N 个**，再**追加**一条 `{"_truncated_items": 剩余数}` | 想「统一成一种」的人会改坏两边：dict 用键值形态承载内容与截断标记，标记只能落在键空间里；列表的标记只能是一个元素。改统一 = 观察者侧的键判断与长度判断同时失效 |
| 上限来自 env 但有**下限** | `max(1000, int(raw))` | 有人把上限写成 `5`，等于每次观测只剩 5 个字符，还查不出原因 |

**边界**：不是每一发钩子都过这一层。`transform_api_error_classification` 的输入被逐字标注为**可能未脱敏**（`hermes_cli/plugins.py:124-125`：*Privacy: error_message/error_body may be unredacted.*），它的输出另外只留 `message.strip()[:500]`；`tools/*_tool_result` 那类**输出替换** seam 也不走这套 sanitizer，各走各的。也就是说「脱敏后的 `request` / `response` / `error` 是给新消费者的**规范**输入」这句话的适用范围，恰好是**观察者**那批钩子。

**② 读 Hermes 的代码** —— `agent/api_request_hooks.py:88-101`。两个容器的溢出分支就挨在这儿，差异一眼可见：

```text
        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for idx, (key, item) in enumerate(value.items()):
                if idx >= max_sequence:
                    out["_truncated_items"] = len(value) - max_sequence
                    break
                str_key = str(key)
                out[str_key] = "<redacted>" if cls._is_sensitive_hook_key(str_key) else recurse(item)
            return out
        if isinstance(value, (list, tuple, set)):
            seq = list(value)
            out = [recurse(item) for item in seq[:max_sequence]]
            if len(seq) > max_sequence:
                out.append({"_truncated_items": len(seq) - max_sequence})
```

**要害**：dict 那条是**边遍历边判 + `break`**，所以第 201 个键往后的**根本没有被访问**（不是「先全量构造再切片」）；list 那条是**先切片**再追加标记 —— 一个「提前停」，一个「取前 N」。同一份 sanitizer 的降级纪律写在它的 docstring 里（`:119`），逐字：*"JSON-able payload under the size cap: full → reduced caps → truncated preview."* ——「**preview**」这个词是关键：最后一级不是拒绝，是给一份能读的前缀。

**③ mini 的实现（现状）**　✅ 已抄（`1ccaa50`）。

三件套落在新建的 `mini-hermes/agent/observer.py` 这一个文件里（461 行）：键集与标记 `:185`/`:188`、判定 `:213-222`（`lower().replace("-", "_")` + 后缀规则，与真实侧同形）、结构阈值 `:190-192`、递归 JSON 化 `:225-293`、总量上限与两级降级 `:200-210`/`:296-317`。两处**照抄不改**的地方正是真实侧最容易抄错的：

- **dict 溢出 `break`（后面的键全丢，只留计数）vs list/tuple/set 保留前 N 再追加标记** —— 语义差异原样保留，并且**被断言钉住**：`mini-hermes/tests/test_observers.py:556-557` 用一个 `items()` 会计数的 dict 子类证明「只访问了 **201** 个键」（改成 `continue` ⇒ 反向验证 RV-6 **1 红**；改成「全量遍历再切」计数会从 201 跳到 300）。照抄的理由很直接：真实侧就是这样，而「统一成一种」会让 mini 的载荷与真实侧的载荷对不上，读者的迁移直觉也就错了。
- **上限的降级顺序**：全量 → `max_string=1000 / max_sequence=50` → `{"_truncated": True, "original_type", "preview"}`（`mini-hermes/agent/observer.py:309-317`）。env 名换成 mini 自己的 `MINI_OBSERVER_PAYLOAD_MAX_CHARS`（`:194-195`，同名会与真实 Hermes 的 `HERMES_PLUGIN_PAYLOAD_MAX_CHARS` 打架），`max(1000, …)` 下限与坏值回落逐字照抄（`:206-210`）。

**一处加法**：②（值级文本脱敏）在真实侧住在 `agent/redact.py`，mini 把它**接进了** `hook_jsonable` 的字符串分支 —— `mini-hermes/agent/observer.py:249` 对每个字符串值先过 `redact_sensitive_text`（复用第 5 章那 7 条模式，`mini-hermes/agent/context_compressor.py:126-136` 模式表 / `:139` 函数），再判 `max_string`。代价是这条正则在**截断之前**跑在整串上（O(原长)）；换来的是「键名正常、值里有凭证」这一路也不出门。为什么必须加：mini 没有真实侧那条独立的文本级管道（同文件 `:144-145` 自述：mini 没有「所有工具输出统一过一遍脱敏」那条管道），键级一层挡不住 `token=ghp_…` 这种。顺带一处口径：真实版 `agent/redact.py` 在锚点上实测 **1,315 行**，mini 注释里写的「850+」是数量级口径。

```python
 # （mini-hermes/agent/observer.py:265-279，两个分支压缩示意）
if isinstance(value, dict):
    for idx, (key, item) in enumerate(value.items()):
        if idx >= max_sequence:
            out["_truncated_items"] = len(value) - max_sequence
            break                                   # ← 提前停：后面的键不被访问
seq = list(value)
out_list = [recurse(item) for item in seq[:max_sequence]]
if len(seq) > max_sequence:
    out_list.append({"_truncated_items": len(seq) - max_sequence})   # ← 保留前 N + 追加标记
```

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 键级脱敏 | `_is_sensitive_hook_key` + `_SENSITIVE_HOOK_KEYS`（`agent/api_request_hooks.py:15,59-64`），命中换 `"<redacted>"` | ✅ 已抄（`1ccaa50`）：同键集、同判定、同标记（`mini-hermes/agent/observer.py:185,188,213-222`） |
| 值级文本脱敏 | `_hook_jsonable` **不做**；文本级脱敏在 `agent/redact.py`（1,315 行），不在这一发的路径上 | ✅ 已抄 + **一处加法**：字符串分支先过 `redact_sensitive_text`（`mini-hermes/agent/observer.py:249`；模式表 `mini-hermes/agent/context_compressor.py:126-153`） |
| 结构截断 | `max_depth=8 / max_string=8000 / max_sequence=200`（`agent/api_request_hooks.py:68-69`），dict `break` vs list 取前 N | ✅ 已抄（`1ccaa50`）：三个默认参同值（`mini-hermes/agent/observer.py:190-192`），差异语义照抄且有断言钉「只访问 201 个键」 |
| 总量上限与降级 | 50000 / env / 下限 1000 / 两级降级 + 预览（`agent/api_request_hooks.py:51-57,117-132`） | ✅ 已抄（`1ccaa50`）：`max(1000, …)` + 降级 + 预览（`mini-hermes/agent/observer.py:200-210,296-317`）；env 名本地化 |

---

## §五 性能（Performance）

**① Hermes 怎么做的**

默认的、未被插桩的路径必须保持廉价 —— 这条不是口号，是一行代码：**要不要构造脱敏载荷，只由一个 `has_hook(名字)` 决定**（原文 §5）。门**不能**开在派发核里面：`invoke_hook` 内部**没有** early-return（即使零回调，它也会先 `_resolve_hook_callback_timeout()` 读一次配置 —— `hermes_cli/plugins_dispatch.py:203` —— 再遍历一个空列表）。真正的短路在**调用点**，形状是

```text
if has_hook("post_tool_call"):     # 一次 dict 探针
```

这样「没订阅」的路径只付探针的钱。**成本在构造载荷，不在探针** —— 这是整节的重心：

| 谁在花钱 | 花在哪 | 量级 |
|---|---|---|
| 探针 | `bool(self._hooks.get(hook_name))`（`hermes_cli/plugins_dispatch.py:447`）：一次 dict 查，无缓存、无身份比较、无标志位 | 常数 |
| 构造脱敏载荷（真实侧） | `pre_api_request` / `post_api_request` 的 `request` / `response`：要**遍历整个请求体**（含全部消息与工具 schema）过一次 `_hook_jsonable` + `json.dumps`，再判总量，超了还要再降级重跑一次 | 每次请求 O(请求体) |
| 构造载荷（mini 侧的重活） | `post_tool_call` 的 `result`：工具输出可能是几万字符（真跑收据里 9000+ 字符的工具输出 ⇒ 载荷 8025 字符且带 `truncated` 标记） | 每次工具调用 O(结果) |

所以「先问 `has_hook` 再构造」是性能契约的核心：一个观察者插件都没装的用户，**不该**为脱敏载荷付钱。门开在构造**之后**（先造好再问有没有人要）不算零成本 —— 那是把最贵的一步照付一遍。真实侧这道门遍布每个钩子发点（`model_tools.py:687` 的 `post_tool_call`、`agent/turn_api_request.py:52`、`agent/turn_response_intake.py:63`、`agent/api_request_hooks.py:171` 的 `api_request_error` …… 每个调用点 1 行）。原文给插件作者的四条纪律也服务于同一个目标：只注册**真的会消费**的钩子、别对已脱敏的载荷做深拷贝或二次脱敏、回调保持快且失败放开、网络导出或批量写盘挪到旁路。

**② 读 Hermes 的代码** —— `hermes_cli/plugins.py:1738-1743`（模块级门面，逐字）：

```text
def has_hook(hook_name: str) -> bool:
    """True when a loaded plugin handles a hook (lazy-discovers first, like :func:`has_middleware`).

    Lazy-discovers first — same gate-before-invoke rationale as :func:`has_middleware` (tracking #64178).
    """
    return _delivery_manager().has_hook(hook_name)
```

**要害**：注释里的 *gate-before-invoke* 就是这条性能契约的名字 —— **在调用之前设门**，而不是调用之后再判断要不要结果。它落到 manager 方法上只剩一行：`return bool(self._hooks.get(hook_name))`（`hermes_cli/plugins_dispatch.py:445-447`，逐字 docstring *"Return True when at least one callback is registered for a hook."*）。真正的「成本账」写在 Kanban 那批观察者的注释里，逐字（`hermes_cli/kanban_db_dispatch.py:1199-1201`）：*"Cost rule: every call site short-circuits on has_hook(), so when nothing subscribes no payload is built and the hot paths (each dispatcher tick, each task write) pay one dict probe."* ——「**no payload is built**」正是判据：不是「构造完再丢掉」。同一形状的辅助函数也留了失败的判据（`hermes_cli/kanban_db.py:180-188`，逐字：*"Inspection failure counts as unconsumed (dropping an observer is always safe)."*）。

**③ mini 的实现（现状）**　✅ 已抄（`1ccaa50`）。

形状与真实侧同构：**调用点先 `has()`，再构造载荷**。五个 fire-site 各一行门（`mini-hermes/run_agent.py:601-603` 回合、`:1057-1069` API 请求、`:1227-1229` 工具前、`:1297-1298` 工具后；`:599-600` 把两个回合事件的门一次问完，因为 `post_llm_call` 要在 `finally` 里发）。`has()` 本体刻意是「一次 dict 探针」（`mini-hermes/agent/observer.py:372-379`，逐字 `return bool(self._callbacks.get(event))`）—— **不做**身份比较、不加缓存，与真实侧同形。`emit()` 内部**也**再探一次（`:424-426`）：那是防御「有人边跑边清空注册表」，代价是没人听时连 `sanitize_payload` 都不跑（有测试盯着，`mini-hermes/tests/test_observers.py:372-392`）。

「成本在构造载荷」这件事在 mini 有一处最直白的注释（`mini-hermes/run_agent.py:1294-1295`，逐字）：*"★ 又是零成本快路径：**先问 `has()`，没人听就直接返回** —— `args`/`result` 可能是几万字符的工具输出，把它塞进载荷再丢掉是最蠢的一种开销。"*

```python
 # （mini-hermes/run_agent.py:601-604 回合；:1297-1298 工具后）
_pre_llm = self._observers.has("pre_llm_call")          # ← 一次 dict 探针
if _pre_llm:
    self._observers.emit("pre_llm_call", **self._payload_pre_llm_call(...))   # ← 门之后才构造
...
if not self._observers.has("post_tool_call"):
    return                                             # ← 几万字符的 result 根本不进载荷
```

度量方式是把 **7 个载荷构造器**包一层计数（`mini-hermes/run_agent.py:1333-1463` 是它们的唯一产地，前缀 `_payload_`）。两条实测收据：**0 观察者的真轮次 ⇒ 7 个构造器调用 0 次**（`mini-hermes/tests/test_observers.py:323-347`，且同一用例断言「轮次里真的发生了 API 与工具活动」，排除「什么都没干所以 0 次」）；只订阅一个事件时**它的载荷恰好 1 次、其余 6 个仍是 0**（`:350-362` —— 快路径是**按事件**开的，不是全局开关）。反向验证 RV-1 删掉调用点那道门 ⇒ **2 红**。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 门的形状 | fire-site `if has_hook("…"):`（`model_tools.py:687`、`agent/turn_api_request.py:52`、`agent/turn_response_intake.py:63`、`agent/api_request_hooks.py:171` …）；`invoke_hook` 内部**无** early-return（`hermes_cli/plugins_dispatch.py:203` 先读超时配置） | ✅ 已抄（`1ccaa50`）：5 个 fire-site 各一道 `self._observers.has(...)`（`mini-hermes/run_agent.py:601-603,1057-1069,1227-1229,1297-1298`），构造点全在门之后 |
| 探针本体 | `bool(self._hooks.get(hook_name))`，无缓存 / 无身份比较（`hermes_cli/plugins_dispatch.py:445-447`） | ✅ 已抄（`1ccaa50`）：同形一行（`mini-hermes/agent/observer.py:372-379`）；`emit` 内部再探一次纯属防御（`:424-426`） |
| 花在构造上的钱 | 脱敏载荷要遍历整个请求体（全部消息 + 工具 schema），超限还要降级重跑（`agent/api_request_hooks.py:117-132`） | 已对齐（成本落在别处）：mini 的重活是工具结果的字符串（真跑收据：9000+ 字符结果 ⇒ 载荷 8025 字符带 `truncated`；最大载荷 8434 < 50000） |
| 「零成本」的证明方式 | 注释即契约（`hermes_cli/kanban_db_dispatch.py:1199-1201` pay one dict probe）；插件作者四条纪律 | ✅ 已抄（`1ccaa50`）：把「载荷构造 0 次」变成断言（`mini-hermes/tests/test_observers.py:323-362`）+ 真跑两遍对照（`mini-hermes/demos/ch15_real_run.py:219-248`） |

---

## §六 写一个观察者插件（Writing An Observer Plugin）

**① Hermes 怎么做的**

真实侧「写一个观察者」= 写一个**插件**。产物是一个插件包（`plugin.yaml` + `__init__.py`），入口是模块级 `register(ctx)`，订阅动作只有一句 `ctx.register_hook(名字, 回调)`（`hermes_cli/plugins.py:916-918`）。宿主是插件管理器：钩子名总表 `VALID_HOOKS`（`hermes_cli/plugins.py:108-199`，**39** 名）、一张 `事件名 -> [回调]` 的 dict（`hermes_cli/plugins.py:1152`）、可撤销的注册句柄（ledger）、以及 `hermes plugins enable` 这类装载面。所以「观察者」在这套契约里与「插件」同义：**订阅面相同，宿主是插件系统**。

写的时候有三条现成的分工，插件作者不必自己实现：

| 谁做 | 做什么 | 插件作者的义务 |
|---|---|---|
| 宿主 | 注入 `telemetry_schema_version`（`hermes_cli/plugins_dispatch.py:200-201`）、脱敏与截断（§四）、逐回调隔离（§三的失败放开） | 别自己再拼一份版本字段、别再二次脱敏 |
| 契约 | 返回值默认忽略 —— 只有四个「更早的、会改变行为」的钩子（`pre_llm_call` / `pre_tool_call` / `transform_tool_result` / `transform_llm_output`）会被采纳 | 观察者**不该**依赖任何返回值；要改行为那是中间件的地盘，不在这份契约内 |
| 性能契约 | 昂贵载荷被 `has_hook(...)` 挡在门后（§五） | 只注册**真的会消费**的钩子；回调保持快且失败放开 |

回调统一收**关键字参数**，并且应当写成 `**kwargs` —— 这样后续增量新增的字段不会破坏老回调。最小形态就是四个钩子配成两对跨度：两个请求级 API 钩子用 `api_request_id` 配对，两个工具钩子用 `tool_call_id` 配对；需要嵌套 agent 工作或安全生命周期事件时，再补子代理与审批钩子。

**② 读 Hermes 的代码** —— `plugins/observability/langfuse/__init__.py:1014-1026`（内置消费者就是一个插件，这个函数是它的全部注册动作）：

```text
def register(ctx) -> None:
    # Both hook-name variants so the plugin works across Hermes versions:
    # *_api_request fire per API call (preferred); *_llm_call once per turn.
    hooks = (
        ("pre_api_request", on_pre_llm_request), ("post_api_request", on_post_llm_call),
        ("api_request_error", on_api_request_error), ("pre_llm_call", on_pre_llm_call),
        ("post_llm_call", on_post_llm_call), ("pre_tool_call", on_pre_tool_call),
        ("post_tool_call", on_post_tool_call), ("on_session_finalize", on_session_finalize),
        ("on_session_end", on_session_finalize), ("subagent_start", on_subagent_start),
        ("subagent_stop", on_subagent_stop),
    )
    for name, fn in hooks:
        ctx.register_hook(name, fn)
```

**要害**：一个 `register` 就是一张 `(钩子名, 回调)` 表 + 一个循环 —— 没有任何自建基础设施，因为脱敏、版本注入、隔离、门控全在宿主侧。注释那两行还说明了**订阅表本身是兼容层**：`*_api_request` 与 `*_llm_call` 两套名字都登记，跨版本的 Hermes 都能接得上；同一个回调 `on_session_finalize` 还被挂到两个事件名上，因为这批钩子的语义在两个版本里被拆过。

**③ mini 的实现（现状）**　✅ 已抄（`1ccaa50`）—— **同一件事，换宿主**。

mini 没有通用插件系统（无 `ctx`、无 manifest、无 ledger、无卸载），所以「同样是订阅」被拆成两半：一份**进程级注册表** `OBSERVERS`（`mini-hermes/agent/observer.py:445-447`，真实侧对应物就是 `PluginManager._hooks` 那张全局表），外加一个**构造参数** `observer=`（`mini-hermes/run_agent.py:249-252` 声明、`:326` 落成 `self._observers`）—— 不传就用进程级那份，传了就能做测试隔离 / 多租户 / 网关按会话分表。订阅动作与真实侧对齐：`registry.add(事件名, 回调)`，未知事件名只警告仍存、不去重、保序（`mini-hermes/agent/observer.py:357-369`）。回调收 `**kwargs`，其中多一个 `event` 键告诉它「自己是哪个事件被叫起来的」（`:435-439`）—— 真实侧靠「按签名筛 kwargs」（`hermes_cli/plugins_dispatch.py:167-186`）省掉的那个字段，mini 既然不抄签名筛，就显式给。

```python
 # 进程级默认注册表（真实侧 = PluginManager._hooks 那张全局表）
OBSERVERS = ObserverRegistry()                       # mini-hermes/agent/observer.py:447
 # AIAgent 的构造参数：不传 = 用进程级那份（测试隔离 / 多租户）
observer: Optional[ObserverRegistry] = None,          # mini-hermes/run_agent.py:252
self._observers = observer if observer is not None else OBSERVERS   # :287
 # 订阅（同一个动作，宿主不同）：只读查询与门都是它
reg.add("post_tool_call", on_post_tool_call)          # mini-hermes/agent/observer.py:357
```

为什么**不**做插件宿主：mini 只有技能和上下文引擎等专用目录发现；providers 包提供已知路由解析，并非 model-provider 插件目录，没有通用 plugin host / 中间件层（全仓 `middleware` 0 命中）。抄一套 `ctx` + manifest + ledger 出来，没有任何东西会去装它 —— 那就是一条**没有消费者的死路径**。进程内代码（CLI / 网关 / 测试）就是 mini 的「宿主」，它们用构造参数把注册表递给 agent。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 宿主 | 插件管理器 + 插件包（`register(ctx)`，`hermes_cli/plugins.py:916-918`）；钩子表 `PluginManager._hooks`（`:1152`） | ✅ 已抄（`1ccaa50`）：**进程级注册表** `OBSERVERS`（`mini-hermes/agent/observer.py:447`）+ `observer=` 构造参数（`mini-hermes/run_agent.py:252,326`） |
| 订阅动作 | `ctx.register_hook(名字, 回调)`；未知名 warn-but-store、不去重、保序 | ✅ 已抄（`1ccaa50`）：`registry.add(事件, 回调)`，三条契约同形（`mini-hermes/agent/observer.py:357-369`） |
| 回调接口 | 收 kwargs，`**kwargs` 保增量兼容；宿主按签名只传回调声明过的字段（`hermes_cli/plugins_dispatch.py:167-186`） | 已对齐口径、⏭ 不抄（签名筛）：回调收全量 kwargs，另给一个 `event` 键（`mini-hermes/agent/observer.py:435-439`） |
| 装载 / 卸载 | `hermes plugins enable`、可撤销句柄（ledger）、懒发现（`hermes_cli/plugins.py:1738-1743`） | ⏭ 不抄（理由）：mini 不卸载插件 ⇒ 句柄与懒发现都没有收益；注册表随进程走 |

---

## §七 现有消费者（Existing Consumers）

**① Hermes 怎么做的**

随包内置两个消费者，**两种接法**：

| 消费者 | 接法 | 覆盖 | 要不要启用 |
|---|---|---|---|
| Langfuse 插件 | 走**通用钩子面**：`register(ctx)` 里登记 **11** 个钩子（`plugins/observability/langfuse/__init__.py:1014-1026`；`plugins/observability/langfuse/plugin.yaml:9-19` 同步列出同一批 11 项） | 2 个回合级 + 3 个请求级 + 2 个工具 + 2 个会话 + 2 个子代理 | 要：`hermes plugins enable observability/langfuse` |
| NeMo Relay | 走**第一方共享指标路径**：不经插件面，`_HOOK_HANDLERS` 把 **13** 个钩子映射到 Relay 的 session / task / model-call / tool-call / approval 记录 | `hermes_cli/observability/relay_shared_metrics.py:935-950`（也消费 `on_skill_lifecycle`、`post_approval_response`；这些名字同样属于插件钩子表） | **不要**：由 `hermes_cli/lifecycle.py:13-15` 转发（门面 `hermes_cli/observability/__init__.py:11` 的 `observe_lifecycle` + `:21` 的 `handles_hook`，后者在 `hermes_cli/observability/relay_shared_metrics.py:867` 用 `hook_name in HANDLED_HOOKS and enabled()` 判定） |

Langfuse 是**纯 observer** 的样板：它只登记观察者名（连 `pre_tool_call` 也只做观测、不返回指令），用关联 id 把事件拼成 trace —— `_trace_key`（`plugins/observability/langfuse/__init__.py:291-298`）以 `turn_id` 优先、`task:`/`session:`/`thread:` 兜底，`session_id` 进 trace_context 做 Langfuse 的会话分组（`:559-560` 逐字注释 *"session_id must be in trace_context for Langfuse session grouping."*）；它有状态上限 `_MAX_TRACE_STATE = 256`（`:50-53` 逐字：*"turns that never reach _finish_trace would leak forever, so over the cap the least-recently-updated are evicted. Bounds the leak, not concurrency."*）；可选依赖缺失时整块 fail-open（`:24-28`）。

两个消费者叠在一起正好说明契约为什么强调「与后端无关」：**同一组生命周期边界**，既要能喂第三方 SaaS（走通用钩子面，因此能演示整条契约），也要能喂厂商自家的指标（走第一方路径，连插件都不用开）。

**② 读 Hermes 的代码** —— `hermes_cli/observability/relay_shared_metrics.py:935-942` + `:950`（第一方路径的映射表，正是「不经插件面也能消费」的载体）：

```text
_HOOK_HANDLERS: dict[str, Callable[[_Runtime, dict[str, Any]], Any]] = {
    "on_session_start": lambda rt, kw: rt.record_client_active(kw),
    "pre_llm_call": lambda rt, kw: rt.start_task(kw),
    "pre_api_request": lambda rt, kw: rt.start_model_call(kw),
    "pre_tool_call": lambda rt, kw: rt.start_tool_call(_with_runtime_toolset(kw)),
    "post_tool_call": lambda rt, kw: rt.record_tool_call(_with_runtime_toolset(kw)),
    "post_approval_response": lambda rt, kw: rt.record_approval(kw),
    "on_skill_lifecycle": lambda rt, kw: rt.record_skill_lifecycle(kw),
    …（同形另有 6 项：post_api_request / api_request_error / on_session_end / subagent_stop /
      on_session_finalize / on_session_reset ⇒ 共 13 个）
HANDLED_HOOKS = frozenset(_HOOK_HANDLERS)
```

**要害**：这张表里**没有** Langfuse 的影子 —— 它是一张「钩子名 → Relay 记录动作」的映射，`start_*` / `record_*` / `finish_*` 成对，靠的就是同一批钩子与同一批关联 id。`HANDLED_HOOKS` 再把它翻成一句布尔判定，让宿主能在**发之前**问「有没有第一方消费者」（`hermes_cli/observability/relay_shared_metrics.py:867`）—— 与 §五 的 `has_hook` 门同一个形状：两条消费路径共用同一道门。

**③ mini 的实现（现状）**

mini 的消费者**不是插件，而是两个进程内订阅者**：一个真跑脚本与一个测试文件。

- `mini-hermes/demos/ch15_real_run.py`（368 行 / 28 条，双解释器 28/28，不联网）：把 7 个事件全订上、把载荷流水按真发顺序打出来（`subscribe_all` `:82-87`），然后在**真实载荷**上断言字段（5 个关联 id、`api_request_error` 的 `status_code`/`retryable`、`attempt` 递增、`tool_call_id` 来自 provider），并跑一遍「0 观察者 vs 7 观察者」的成本对照（`:219-248`）。
- `mini-hermes/tests/test_observers.py`（994 行 / 6 组 / 124 条，双解释器一致）：两个夹具就是「消费方的替身」—— `record_events` 收载荷、`count_payload_builds` 数构造次数（`:136-140`）；第 6 组用假 provider 驱动真 agent，把 7 个事件按序发出来变成断言。

**没有内置生产消费者订阅**：`mini-hermes/cli.py` 与 `mini-hermes/gateway/run_turn.py` 都不传 `observer=`，模块级门面 `has_observers` / `emit`（`mini-hermes/agent/observer.py:450-461`）就是给这类「agent 之外」的调用点留的口子 —— 目前只有测试与 demo 用它。所以 mini 的消费者**证明了契约可消费**，但**没有一个是产品**：既没有可装载的第三方导出器（无插件宿主），也没有第一方指标路径（无 Relay 基座、无 exporter）。

```python
 # （mini-hermes/demos/ch15_real_run.py:82-87 —— mini 的「消费方」长这样）
def subscribe_all(registry: ObserverRegistry) -> list:
    stream: list = []
    for ev in OBSERVER_EVENTS:
        registry.add(ev, (lambda e: (lambda **kw: stream.append(kw)))(ev))
    return stream
```

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 插件式消费者 | Langfuse 插件登记 **11** 个钩子（`plugins/observability/langfuse/__init__.py:1014-1026`；`plugins/observability/langfuse/plugin.yaml:9-19`），纯只读、用关联 id 拼 trace、有 256 条状态上限、缺依赖 fail-open | ⏭ 不抄（无插件宿主 / 无导出器基座）：mini 没有能装载第三方导出器的宿主，「插件式消费者」这条路径无处落地 |
| 第一方共享指标 | NeMo Relay 走 `_HOOK_HANDLERS`（13 个钩子）+ `HANDLED_HOOKS` 布尔门（`hermes_cli/observability/relay_shared_metrics.py:935-950`），不开任何插件 | ⏭ 不抄（无 Relay 基座）：mini 没有 `observability` 这一层，也就没有「宿主自带的第二消费方」 |
| 消费方的替身 | 无（两个都是产品级消费者） | ✅ 已抄（`1ccaa50`）：`mini-hermes/demos/ch15_real_run.py`（368 行 / 28 条）与 `mini-hermes/tests/test_observers.py`（994 行 / 6 组 / 124 条）—— 证明契约可消费，但都不是产品 |
| 覆盖面 | Langfuse 11 + Relay 13，跨 6 个事件族（含会话、审批、子代理、技能生命周期） | 已对齐口径、⬜ 没做（量级）：mini 只有 7 个事件名，消费者只覆盖 3 个边界（回合 / 请求 / 工具），会话级边界归网关的目录式钩子 |

---

## §八 实现期真问题

> 只记**本轮真的发生过**的三件事。每条给「现象 → 根因 → 判据」；**判据那一栏写的是"下次怎么避免"**，不是感想。三条的共同形状是同一句话：**观察到的失败，先怀疑观测方式，再怀疑被观测的东西。**

**真问题 1 · 子代理 600s 超时，但成果已经落盘；真正不确定的是"工作树停在哪一版"。**

**现象**：写实现的子代理在**真跑那一步**（真 DeepSeek 调用）撞上 600s 时间盒，无回报返回。它超时前已经把四件东西写进树里：`mini-hermes/agent/observer.py`（461 行）、`mini-hermes/run_agent.py` 的 7 处接线（1341 → 1704）、`mini-hermes/tests/test_observers.py`（994 行）、`mini-hermes/demos/ch15_real_run.py`（368 行）——但**没有一句结论**：套件没跑、反向验证没做、真跑没有收据。

**根因**：时间盒压在了链路上最长、且**不由子代理控制**的那一段上。真跑要真凭据（一次真模型调用 + 一次 9000 字符量级的工具输出），单次时延不可预测；而实现本体（新模块 + 接线 + 契约测试）本来是可以先落盘的。更要紧的一点：**子代理超时不会回滚任何已写文件**，于是"工作树"可能停在三态之一 —— ① 实现完成态；② **反向验证改到一半的中间态**（本章的 RV 就是"把代码故意改坏、看测试红不红"，任何一步被打断都会留下一棵**故意改坏**的树）；③ RV 已还原态。对主代理来说，这三态**从文件内容上肉眼不可分**。

**判据**：**先取证，别先假设。** 本轮把"工作树干不干净"变成一个可执行的判定：RV 开始**之前**先把两个将被改动的文件原样拷出（`/tmp/ch15_observer.bak` / `/tmp/ch15_run_agent.bak` ＝ RV 前基线），全部还原之后用**逐字节比对**收口 —— `cmp` 两条都 `IDENTICAL`，SHA-256 也逐条相同（`agent/observer.py` = `769d92c3…4aeeb0`；`run_agent.py` = `65e80764…cfd0da`）。"我改回来了"是主观判断；"字节相同"才是判据。（另附一条独立的干净证据：`git status --porcelain` 在 `1ccaa50` 之后**空输出**。）

**下次怎么避免**：① **分工固定** —— 子代理交**代码 + 契约测试**；**真跑 / 反向验证 / 提交**由主代理做（第 10 章已复盘出同一条规则，本轮再次成立：超时的原因恰恰是真跑）。② 任何"改坏再还原"的操作**必须先留字节基线**（`.bak` 或哈希清单），收口时逐条 `cmp`；不要用一个 `git checkout` 掩盖"我到底动过哪些文件"。③ 超时回来的子代理产物**一律按"待验证输入"处理**，不是完成品：先复跑套件，再自己补 RV 与真跑。

**真问题 2 · 收据脚本自己的 logging handler 有 bug，被误判成"实现的失败放开坏了"。**

**现象**：真 API 收据跑到"失败放开"那一条时，**警告列表是空的** —— 看上去像观察者把回调异常静默吞了；同时终端上冒出一段 Python 自己打的 `--- Logging error ---` traceback。第一反应很自然：`ObserverRegistry.emit` 的 `except` 分支没记警告，实现有缺陷。

**根因**：**坏的是收据脚本的 handler，不是被测代码。** 原写法在 handler 里做 `record.getMessage() % record.args`；而 `getMessage()` **已经**把 `args` 代进去了，再 `% record.args` 等于对一条已格式化的字符串做**二次格式化** —— 参数个数对不上就抛 `TypeError`，而 logging 的既定契约是"handler 自己的异常不许穿出去"，于是它把这条错误以 `--- Logging error ---` 打到 stderr，**那条真正的警告也就没进列表**。修法留在最终脚本里：`/tmp/ch15_real_api_receipt.py:37-40` 的 `Collect.emit` 只有一句 `warnings.append(record.getMessage())`，旁边逐字一行注释 `# ★ 修正：它已经代入过参数，别再 % args`。对照物是测试夹具 —— 它从一开始就是对的写法：`mini-hermes/tests/test_observers.py:203-204` 存的是 `record` **对象本体**，`:423` 才 `r.getMessage()`。

**判据**：**"日志里没有我要的那条警告"先怀疑捕获方式，再怀疑实现。** 三条可操作的分离信号：① 见到 `--- Logging error ---` ⇒ 捕获链自己坏了，与生产代码无关；② handler 里**只许**调 `getMessage()`，不许再套 `%` / `str.format`（`record.args` 只在 `getMessage()` 内部消费一次）；③ 判"失败放开有没有留痕"看两个**独立**信号 —— **有 traceback** = 捕获坏了；**既无 traceback 又无警告** = 才是实现真的没记。

**下次怎么避免**：收据脚本**复用测试的同一套夹具**（`mini-hermes/tests/test_observers.py:207-221` 的 `capture_logs`），不要在收据里另写一份 handler；凡"断言异常路径被记下来了"的收据，先跑一条**已知一定会记警告**的对照，确认捕获链自己活着再解读结果。

**真问题 3 · 真跑里模型把命令改写了 ⇒ "截断"根本没被触发。**

**现象**：真 API 收据里 `post_tool_call` 的 `result` 找不到 `truncated` 标记 —— 第一眼看像 `hook_jsonable` 的 `max_string=8000` 没生效、截断实现坏了。实际情况是：那条**刻意用来造大输出**的命令被模型优化掉了（改成只打印/测量长度），工具只吐了几十个字符 ⇒ 载荷根本没到 8000 那条线，**没有被触发的机会**。

**根因**：收据的**触发条件寄托在模型的服从性上**。"让模型执行 `python3 -c "print('x' * 9000)"`"在这个模型手里是**可改写的一步** —— 它完全可以用 `wc -c` / `len()` 达成"告诉我它多长"这个表面目标，而这与收据要证的代码路径**正交**。收据要证的是**代码路径**，路径只在输入真的够大时才走到。

**判据**：**收据要自己造出触发条件，不能假设模型照抄。** 本轮把这条收据改成三重保险：① 提示词把"原样照抄、不要改写、不要加 `len()`/`wc`"写死，并把它变成一个可核对的任务（回报前 20 个字符 + 总字符数）——该文案逐字留在 `/tmp/ch15_real_api_receipt.py:30-32`；② 收据**先印实际观测值再判**（脚本 `:68-70` / `:83-84` 把 `result` 长度与 `truncated` 标记一起打出来），于是"没触发"不会伪装成"实现坏了"；③ 拿到的真收据是 **8025 字符且带 `truncated` 标记**（= 8000 截断 + `...[truncated 1000 chars]`），另有一条**完全不依赖模型**的证法在 demo 里（`mini-hermes/demos/ch15_real_run.py:259-289` 直接把 9000 字符塞进工具参数、并在 `:291-309` 用 env 把总额压到 1000 逼出降级预览）。

**下次怎么避免**：凡"这条断言依赖模型 / 外部行为"的收据，先问一句**"触发条件是谁造的"** —— 能由脚本自己造的（塞参数、造假 provider、直接构造载荷）就不交给模型；只能由模型造的，就把**实际观测量先打印**再断言，否则一条失败的收据无法区分"没触发"和"实现坏"。

**三条真问题的共同形状**（一句话各自带走）：

| # | 现象（看起来像什么） | 根因一句话 | 判据（下次怎么避免） |
|---|---|---|---|
| 1 | 子代理超时 ⇒ 不知道树停在哪一版 | 时间盒压在真跑；超时不回滚已写文件，而 RV 会**故意改坏**代码 | RV 前留字节基线，收口用 `cmp`/哈希；分工固定"子代理交码，真跑与 RV 归主代理" |
| 2 | 失败放开"没记警告" + 日志里有 traceback | **收据脚本**的 handler 把已格式化的消息又 `% record.args` | 见 `--- Logging error ---` 就先怀疑捕获；handler 只调 `getMessage()`；复用测试夹具 |
| 3 | 载荷里没有 `truncated` 标记 | 模型改写命令 ⇒ 输入没到截断线 | 收据自己造触发条件；先把实际观测量打印再断言 |

---

## §九 收据

> 数字口径：**全套件 / 新测试文件 / demo / 反向验证 / 真 API** 五组。反向验证与真 API 的跑法是主代理的（真凭据 + 会改工作树），本节的**套件与 demo 由本轮写文档时实测复跑**（清 `__pycache__` 后两次独立运行），故它们在下面逐行给数字。

**① 全套件（双解释器；本轮实测复跑）**

| 测试文件 | `/usr/local/bin/python3` 3.13.14 | `/usr/bin/python3` 3.9.6 |
|---|---|---|
| `tests/test_compression.py` | 157 通过 / 0 失败（9 组） | 157 / 0（9 组） |
| `tests/test_context_engine.py` | 45 / 0（7 组） | 45 / 0（7 组） |
| `tests/test_fallback_and_aux.py` | 113 / 0（4 组） | 113 / 0（4 组） |
| `tests/test_gateway.py` | 266 / 0（21 组） | 266 / 0（21 组） |
| `tests/test_loop_mechanisms.py` | 57 / 0 | 57 / 0 |
| `tests/test_micro_compaction.py` | 119 / 0（10 组） | 119 / 0（10 组） |
| **`tests/test_observers.py`（本章新增）** | **124 / 0（6 组）** | **124 / 0（6 组）** |
| `tests/test_packaging.py` | 18 / 0 | 18 / 0 |
| `tests/test_prompt_assembly.py` | 155 / 0（13 组） | 155 / 0（13 组） |
| `tests/test_provider_runtime.py` | 106 / 0（6 组） | 106 / 0（6 组） |
| `tests/test_session_storage.py` | 108 / 0（14 组） | 108 / 0（14 组） |
| `tests/test_skills.py` | 103 / 0（7 组） | 103 / 0（7 组） |
| `tests/test_state_db_recovery.py` | 121 / 0（12 组） | **117 / 0（12 组）** |
| `tests/test_tools_runtime.py` | 89 / 0（10 组） | 89 / 0（10 组） |
| **合计（14 个测试文件）** | **1,581 通过 / 0 失败** | **1,577 通过 / 0 失败** |

- 基线（第 15 章之前）：**1,457 / 1,453** ⇒ **+124 = 新测试文件**（两边都恰好 +124，数目自洽；`test_observers.py` 就是那 124 条）。
- 两解释器**唯一**的差别仍是 `test_state_db_recovery.py` 的 4 条（121 vs 117）—— 与第 12 章收据记的同一个分支（3.13 允许 `DROP messages_fts_data`，3.9/SQLite 3.51 直接拒）一致。**本章的新文件不受解释器影响**（两边都是 124 / 0）。

**② 新测试文件 `mini-hermes/tests/test_observers.py`：994 行 / 6 组 / 21 个用例函数 / 124 条 `check()`**

| 组 | 承诺（组标题原文） | 用例 | `check()` |
|---|---|---|---|
| 1. 注册契约 | 未知名**只警告仍存**（警告带全部合法名）· **不去重** · **保序** · 只读查询 · `has()` 是唯一事实源 | 3 | 17 |
| 2. **零成本快路径 ★** | **0 观察者 ⇒ 载荷构造 0 次**（不是构造完再丢掉）· 有观察者 ⇒ 恰好 1 次 · `emit` 无订阅时连 sanitize 都不跑 | 3 | 11 |
| 3. 失败放开 | 坏回调只丢自己 · 轮次与工具执行不受影响 · 有警告 · **返回值一律忽略** | 2 | 9 |
| 4. 载荷安全 | 敏感键 `<redacted>` · 值里的凭证也被文本级脱敏 · 超长串带 `truncated N chars` · 深度 8 / 序列 200 · **dict 与 list 溢出语义故意不同** · 总量上限两级降级 + env | 6 | 34 |
| 5. 关联 id | 5 字段齐 · **None → `""`** · 同轮 `turn_id` 相同、跨轮不同 · 每请求一个 `api_request_id`（含回退重发）· `tool_call_id` 来自 provider | 4 | 23 |
| 6. 事件触发点 ★（假 provider 驱动真 agent） | 一轮 + 一次工具调用 ⇒ 7 个事件**按序**发出（10 发）· 失败也发 `post_llm_call` · 工具三结局用 `status` 区分 | 3 | 30 |
| **合计** | | **21** | **124** |

两条结构性纪律也钉在文件里：① **未注册自检** —— 定义了 `test_` 却没进 `GROUPS` ⇒ `raise SystemExit(2)`（`mini-hermes/tests/test_observers.py:966-971`，"写了但不会跑"这类静默失败直接变成硬失败）；② 每条用例**独立接住异常**（`:977-980`），一条红不会吞掉后面的组。隔离照 `tests/test_skills.py` 的三件套（`MINI_HOME` 在 import 之前设 + `env_var()` 摘/还 + 子进程传干净 env，`:185`）。

**③ 真跑 demo `mini-hermes/demos/ch15_real_run.py`：368 行 / 28 条**

- 双解释器 **28 / 28 通过**（3.13.14 与 3.9.6 各一次，`0 失败`），**不联网** —— 它用**假 provider** 驱动**真 agent**（`_http_post` 换成罐头响应，`:121-131`），所以它不是"测函数"，是"走真宿主路径 + 在真载荷上断言"。
- 五段覆盖：① 一轮 10 发事件按序 + 字段（`:150-182`）· ② 429 → 回退换 provider → 重发（一次 LLM 调用两个 `api_request_id`、`attempt` 递增、`retryable=True`，`:187-217`）· ③ **零成本快路径**：同一轮对话跑两遍，0 观察者时 7 个载荷构造器总计 **0 次**（`:222-248`）· ④ 载荷安全：仿真凭据 / 9000 字符串 / 深嵌套 / 260 键大 dict / 总额压到 1000（`:253-309`）· ⑤ 失败放开：三个回调、中间那个抛异常，前后两个照跑、轮次照常收尾、有警告（`:314-352`）。
- 一条**口径订正**的留痕：demo 第一次跑是 **27 / 1**（证据：早于提交的那次输出里，第 ③ 组那条"按事件各构造若干次"红了，计数是 `_payload_pre_llm_call: 4` —— 因为 `post_llm_call` 的载荷会复用 `pre_llm_call` 的公共那一层）。修的是**断言口径**（把"pre_\* 只构造 2 次"改成"流水条数一一对应"的口径，并把这层复用写进 `:234-235` 的打印说明），不是实现；改完两个解释器都是 28 / 28。

**④ 反向验证：7 条改法**全部**干净变红**（"干净"＝ 进程正常退出、`失败项` 里出现预期断言名，**不是**崩溃）

| 变异 | 改回什么 | 干净变红 | 该由哪条断言接住（本轮读测试得到的对应） |
|---|---|---|---|
| RV-1 | 删掉调用点那道门（`if self._observers.has(...)`） | **2 红** | 第 ② 组 `test_zero_cost_fast_path_builds_no_payload` 里"★ 0 观察者 ⇒ 载荷构造 0 次"与"7 个构造器每一个都是 0"两条 —— 用例 docstring 本就写着"删掉任意一个调用点的门，本条立刻变红"（`:323-343`） |
| RV-2 | 取消**失败放开**（去掉逐回调的 `try/except`） | **5 红** | 第 ③ 组 `test_bad_callback_cannot_break_the_turn` 的 5 条（前后回调照跑 / 轮次照常 / 工具结果照常回填 / 有警告点名事件与异常 / 警告带异常摘要，`:417-427`） |
| RV-3 | 去掉 `None → `""`（线协议） | **2 红** | 第 ⑤ 组 `test_call_ids_wire_contract` 的两条：全 None ⇒ 全 `""`、部分有值时缺的仍是空串（`:631-637`） |
| RV-4 | 改阈值常量（depth 8 / string 8000 / sequence 200） | **1 红** | 第 ④ 组 `test_string_and_sequence_limits` 第一条"阈值就是真实侧那三个默认"（`:504-505`） |
| RV-4b | 关掉**截断行为**（超长串不截） | **1 红** | 同组"★ 超长串截到 8000 并注明丢了几个字符"（`:510-511`） |
| RV-5 | `add()` 改成**去重** | **4 红** | 第 ① 组 `test_no_dedup_and_registration_order` 的全部 4 条（该用例恰好 4 条断言） |
| RV-6 | dict 溢出由 `break` 改成 `continue` | **1 红** | 第 ④ 组 `test_container_overflow_semantics_differ_on_purpose` 里"★★ 那个 `break` 是**真的提前停止**：只访问了 201 个键"（`:556-557`）—— 与 demo `:287-289` 那条"200 个键 + 一条 `_truncated_items`（60）"同源 |

"红数"逐条照主代理的回执；"该由哪条断言接住"是**本轮读测试**得到的对应关系（不是跑出来的），写在这里是为了让下次的 RV 有可核对的预期值：**红数对不上，就说明改动的落点与预期不同**。

**⑤ 真 API 收据（真 DeepSeek、临时 `MINI_HOME`、脚本 `/tmp/ch15_real_api_receipt.py`）**

- **一轮 2 次 LLM 调用 = 10 条事件**，顺序逐条为 `pre_llm → pre_api → post_api → post_llm → pre_tool → post_tool → pre_llm → pre_api → post_api → post_llm`。
- **1 个 `turn_id`** 串起全部 10 发；**2 个 `api_request_id`**（`{turn_id}:api:1` / `…:api:2`）；工具那两发带 **provider 真的给出的 `tool_call_id`**（`call_00_…`，不是 mini 生成的）。
- 真工具输出 9000+ 字符 ⇒ `post_tool_call` 载荷里的 `result` 是 **8025 字符且带 `truncated` 标记**（8000 截断 + `...[truncated 1000 chars]` 这 25 个字符）；本轮**整份载荷最大 8434 字符 < 50000 上限** —— 也就是说这一轮里"总上限降级"那条分支**没被触发**（它是被 demo 的 env=1000 逼出来的，见 ③ ④）。
- **失败放开**：一个故意抛异常的回调 ⇒ **轮次照常收尾**（最终回答非空）+ **1 条点名警告**（落盘 `logs/errors.log` 里也能找到）。
- **零成本快路径**：**0 观察者**的真轮次 ⇒ 7 个载荷构造器**总计 0 次**（逐个都是 0，不是"和为零"）。

**⑥ 实现规模**（`wc -l`）

`mini-hermes/agent/observer.py` **461** · `mini-hermes/run_agent.py` **1704**（1341 → 1704）· `mini-hermes/tests/test_observers.py` **994** · `mini-hermes/demos/ch15_real_run.py` **368**；合计新增/改动见附录 B 的 diff stat。

---

## §十 与真实版的差距清单

**口径**：A 批（10 条"决定架构形状"的）**已抄**，落点与对照见 §〇 差异一览表与 §一~§七 各节的 ④ 表；本节只列**没做 / 不抄**的部分，逐条给 `差距 | 性质 | 代价`（真实侧锚 `f5d1926110`，mini 侧锚 HEAD `1ccaa50`）。性质用三类理由 + 两档标记：**① 基座** ＝ mini 没有那条前提 · **② 规模** ＝ 纯细节或规模差距 · **③ 用不上的复杂度**；**⏭ 不抄（理由）** / **⬜ 没做（量级）**。

| # | 差距（真实侧有什么） | 性质 | 代价 |
|---|---|---|---|
| **B1** | **超时隔离线程**：`_run_hook_callback_bounded` + 三张表（有界集 / fail-closed 集 / 调用线程集）+ 60s 抑制窗口（`hermes_cli/plugins_dispatch.py:25-53` / `:246-321`） | ⏭ 不抄（**① 基座**）：mini 的观察者是**同步**调用，没有"钩子跑在工作线程里"这个前提 | 抄过来是 ~75 行**永远走不到**的代码（没有线程就没有超时）；今天的行为 = 回调在调用方线程跑到底 ⇒ 一个慢观察者会拖慢这一轮（mini 无长命观察者，如实标注不粉饰） |
| **B2** | `pre_tool_call` 的 **`block` / `approve` 改行为指令**（`hermes_cli/plugins.py:1797-1805` / `:1830-1840`）+ 超时 fail-closed 直接给阻断指令（`hermes_cli/plugins_dispatch.py:48` / `:53`） | ⏭ 不抄（**③ 用不上的复杂度**）：mini 的审批只有 `approval_callback` 一处控制流 | 引第二套会立刻产生"两处都能否掉工具、谁先谁后"的歧义；代价是 mini 的观察者**确实不能**否决工具（要否决走 `mini-hermes/run_agent.py:1252` 的审批回调或网关守卫） |
| **B3** | **抑制窗口 / `gate_key` / `_hook_abandoned` / `_hook_running_callbacks`** 一族共享状态与一把锁（`hermes_cli/plugins_dispatch.py:143-163` / `:306-321`） | ⏭ 不抄（**① 基座**）：整族依赖 B1 | 纯规模（~40 行 + 锁）；无线程时"这个回调刚被抛弃、先别叫它"这件事本身不存在 |
| **B4** | **流式观察者**（`_STOP` 哨兵 + 有界队列 drop-oldest + 惰性停线程，`agent/plugin_stream_hooks.py` 全 165 行） | ⏭ 不抄（**① 基座**）：mini 没有流式输出（`mini-hermes/run_agent.py:22-23` 的"没实现的"清单里还挂着它） | 165 行并发代码换不到任何可观测面：mini 只有"一次性 HTTP 响应"一种形态 |
| **B5** | **事件总线** emit/subscribe（保留命名空间 `hermes:` + 递归深度 cap 8 + pending budget 64 + 单 worker，`hermes_cli/plugins_dispatch.py:87-93` / `:323-334`） | ⏭ 不抄（**③ 用不上的复杂度**）：不是 observer hooks 的核心能力 | 另建一套发布订阅 + 队列 + 停止协议；mini 的观察者已经是直接函数调用，加总线只多一层间接 |
| **B6** | **系统提示词 section 注册与渲染**（位置集合 / 4 000 字符 / 32 节 / 8 000 总量，`hermes_cli/plugins_dispatch.py:55-64` / `:449-470`） | ⏭ 不抄（**③ 用不上的复杂度**）：另一个特性（往提示词里注字节），与派发无耦合 | 会牵动第 3 章的提示词装配与缓存不变式，收益与本机制无关 |
| **B7** | **`hermes hooks` 运维面**（`list/test/revoke/doctor` + 每个钩子一份合成载荷，`hermes_cli/hooks.py:1` / `:165-175`） | ⏭ 不抄（**② 规模**）：它是 config-owned shell hooks 的运维面 + 夹具，不属观察者通道（只复用 `VALID_HOOKS` 当白名单） | 335 行 CLI + 一批合成载荷；mini 没有 shell hooks 可管 |
| **B8** | **注册 ledger / 可撤销句柄**（`PluginRegistration` + force reload 反向 unwind，`hermes_cli/plugins.py:927-938`） | ⏭ 不抄（**① 基座**）：mini 不热重载、不卸载观察者 | 句柄拿到手也没人 `release`；代价是 mini 只能"加"不能"摘"（换注册表 = 构造时给 `AIAgent(observer=...)`） |
| **B9** | **载荷深拷贝容错**（`_safe_copy`：deepcopy 失败退化为原样，`hermes_cli/middleware.py:50-57`）+ 事件派发前 `copy.deepcopy(item.payload)`（`hermes_cli/plugins_dispatch.py:399`） | ⏭ 不抄（**③ 用不上的复杂度**）：mini 的载荷在装配出口已被 `sanitize_payload` 换成**新的** JSON-safe 副本（`mini-hermes/agent/observer.py:296-317`） | mini 没有"把活对象交给回调"的路径 ⇒ 没有深拷贝要防；如实记为差异，不假装等价 |
| **B10** | **`VALID_HOOKS` 全 39 名**（含 8 个 `on_kanban_*`、3 个 `on_stream_*`、`on_skill_lifecycle`、`pre_transcription`、`pre_verify`、`pre_command`…，`hermes_cli/plugins.py:108-199`） | ⏭ 不抄（**② 规模**）：表里没有触发点的名字等于给读者一个"能用"的假承诺 | 只落**有真触发点**的 7 个（`mini-hermes/agent/observer.py:165-173`）；代价是拼错的事件名只在 7 名的尺度上被发现，不在 39 名的尺度上 |
| **D1** | **事件名覆盖：39 vs 7** | ⬜ 没做（量级） | 缺的 32 个各有归属：会话级归**网关目录式钩子**（拍板 P4，`mini-hermes/agent/observer.py:44-54` 写清三层分工）；审批 / 子代理 / 流式 / Kanban 在 mini 无基座或无用例。如实标为**覆盖差**，不说"等价" |
| **D2** | **真实消费方**：内置 Langfuse 登记 **11** 个钩子（`plugins/observability/langfuse/__init__.py:1014-1026`）+ 内置 NeMo Relay 映射 **13** 个钩子（`hermes_cli/observability/relay_shared_metrics.py:935-950`） | ⬜ 没做（量级）：mini 没有通用插件宿主，也就没有第三方消费方 | "观察者真的有人听"这件事在 mini 只能由 `mini-hermes/demos/ch15_real_run.py` 与测试夹具证明 —— 没有"另一个进程 / 第三方库"在消费它，这是本章最实的一条差距 |
| **D3** | **`post_llm_call` 的口径差**：真实侧**每回合一次**（工具循环跑完才发，`agent/turn_finalizer.py:411-433`）；mini 取**每次模型调用**一次（与 `pre_llm_call` 成对，`mini-hermes/run_agent.py:628-631` 在 `finally` 里发） | ⬜ 没做（量级）：mini 没有 `finalize_turn` 这个收尾层 | 消费方若按"一回合一条 span"去聚合，mini 的条数会偏多（一次工具调用 ⇒ 2 条）；这条差异由实现自己在 `mini-hermes/agent/observer.py:38-41` 记下，这里保留不美化 |

**三条"代价"里最该记住的一条**：mini 的观察者**只能看、不能改，也几乎不能被"挂错"惩罚** —— 超时隔离（B1/B3）、改行为（B2）、深拷贝（B9）三项不抄，换来的是"读代码的人要理解的机制更少"，代价集中在**D2**：没有任何真实第三方在听，所以"这份遥测面在设计上够不够用"这个问题，mini 里**无法用生产流量回答**，只能用 demo 与契约测试回答。

---

## 附录 A · 从正文挪过来的引用

> 正文每节的「② 读 Hermes 的代码」只准引一段（≤15 行）且优先引注释，撑不住的行号按文件归档到这里。每条给「该段首 ~20 字」+「它证明什么」；括号里是"它在说什么"，不当逐字引文用。
>
> - **真实侧**锚 `f5d1926110`（取值 `git -C ~/.hermes/hermes-agent show f5d1926110:<path>`），**裸 `文件:行号` 一律指真实侧**
> - **mini 侧**锚 HEAD `1ccaa50`（本章实现），**一律写 `mini-hermes/<路径>:<行号>`**
> - 下列行号**本轮逐条实测取过非空行**；与各节 ② 的逐字引文若有重叠，由装配方按需去重（② 每节只留一段，本表是全量归档）

### 真实侧（锚 `f5d1926110`）

**`hermes_cli/plugins.py`（2147 行）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:108` / `:199` | `VALID_HOOKS: Set[str] = {` … `"pre_command",` / `}` | 钩子名总表全集：本轮用 `ast` 剥注释后解析，实测 **39** 个（本章 mini 只落 7 个，见 §十 D1） |
| `:136-142` | `# platform, reason, invalidation_reason. Return values are ignored.` | 观察者族的"返回值被忽略"是**逐族写在总表注释里**的（`agent_loop_stopped` / 审批观察者：*returns ignored — plugins cannot veto or pre-answer*）；`:140-141` 还自我描述 `surface` 与 `choice` 的值域 |
| `:153-157` | `# Kanban task observers (hermes_cli.kanban_db), fired AFTER the DB commit …` | 观察者与**提交次序**的关系："慢插件不许攥着 SQLite 写锁"；`claimed` 在派发器发、`completed/blocked` 在 worker 发 |
| `:173-190` | `# gateway_platform_event: normalized envelopes only, never raw SDK objects …` | 为什么这个钩子是**版本注入的例外**（事件本地载荷契约）；`:190` 附近还有 `on_kanban_dispatch_tick` 必须在派发锁**释放之后**发的那段来历 |
| `:195-197` | `# Slash-command dispatch observer (#64204, observer-first …) Return values are` | `pre_command` 是**观察者优先**的又一例："v1 里返回被忽略，返回 directive 形状的 dict 只记一条 debug" |
| `:916-918` | `def register_hook(self, hook_name: str, callback: Callable) -> PluginRegistration:` | 注册入口真身 + docstring 逐字 *unknown names warn but are still stored*（§一 的核心契约） |
| `:927-938` | `def _track_callback(` | `if key not in valid: logger.warning(...)` 然后**无条件** `mapping.setdefault(key, []).append(callback)` —— 宽容注册与不去重是同一个函数里两行 |
| `:1152` | `self._hooks: Dict[str, List[Callable]] = {}` | 整章唯一的事实源就这一个 dict（`has_hook` 探它、`invoke_hook` 遍历它） |
| `:1257-1261` | `# config.yaml shell hooks / outbound webhooks live in ``_hooks`` but are` | shell hooks / outbound webhooks **也住同一个 dict**，但它们是 **config-owned** ⇒ force reload 后要重新登记。这条把 `hermes hooks` 与我们这条通道的边界说明了 |
| `:1738-1743` | `def has_hook(hook_name: str) -> bool:` | 模块级门面；docstring 逐字 *gate-before-invoke rationale* —— 零成本快路径的官方理由书 |
| `:1797-1805` | `def _get_pre_tool_call_directive_details(` | 改行为型返回值的**唯一**解析处：`block`（message 变成工具结果）与 `approve`（把任意工具升级到人工审批闸门，`rule_key` 决定 always 白名单粒度） |
| `:1830-1840` | `if action not in ("block", "approve"):` | 白名单式收值：不认识的动作 `continue`；`block` 必须有 message（它要变成工具结果） |
| `:1932-1941` | `def get_plugin_error_classification(` | run-all-then-pick-first 的分类器通道（`Mini` 无 `FailoverReason` 基座 ⇒ 见 §十 B2 同族的不抄理由） |

**`hermes_cli/plugins_dispatch.py`（530 行）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:25-53` | `# Allowlist of agent-turn hot-path hooks bounded by plugins.hook_callback_timeout (fail-open: …` | 三张表 + 一条抑制窗口常量的**完整来由**：有界集（含故意不设界的四类）、fail-closed 集（只有 `pre_tool_call`）、调用线程集（只有 `subagent_stop`）、60s 抑制；注释逐字点出两个事故号（`#76821` 挂死、`#6622` shutdown hang） |
| `:143-163` | `def _hook_call_identity(kwargs: Dict[str, Any]) -> Optional[str]:` | **为什么 gate 的粒度是"调用"而不是"回调"**：docstring 逐字 *Deliberately not ``api_request_id`` — one API request carries many tool calls* |
| `:167-186` | `def _invoke_hook_callback(callback: Callable, payload: Dict[str, Any]) -> Any:` | 载荷**加法式演进**的实现：`inspect.signature` 只传回调声明过的 kwarg；`**kwargs` 回调拿全量；`async def` 的 coroutine 要解析（否则 *the plugin's body never runs*，`#12449`） |
| `:188-205` | `def invoke_hook(self, hook_name: str, **kwargs: Any) -> List[Any]:` | 派发核的契约一句打尽：*Payloads evolve additively … Each callback is isolated. Bounded hooks and ``pre_tool_call`` run under ``plugins.hook_callback_timeout`` (worker abandoned, never joined); ``pre_tool_call`` fails closed …* |
| `:206-220` | `for cb in self._hooks.get(hook_name, []):` | 逐回调 `try/except` + `continue`（失败放开的**异常面**），以及 `_HOOK_SKIPPED` 的两条分支（fail-closed 才 append 阻断指令） |
| `:222-244` | `def _report_hook_failure(` | 告警去重的完整理由：`(hook, callback, error)` 一条 WARNING、重复降 DEBUG；key 用 `__module__/__qualname__`（不用 `id()`，CPython 会回收）；错误串截 200 防集合膨胀（`#111922`） |
| `:246-262` | `def _run_hook_callback_bounded(` | 有界执行的返回约定：被抑制 / 仍在跑 / 已超时 / worker 起不来 ⇒ 一律 `_HOOK_SKIPPED`；**异常照常抛出**（异常面与超时面分开处置） |
| `:306-321` | `if not done.wait(timeout=timeout):  # do not join — that would reintroduce the hang` | abandon-without-join 的落地：写抑制窗口 + 只在"token 还是我的"时才标 abandoned（中间那段注释解释了为什么不然会误封回调） |
| `:87-93` | `# Reserved event namespace prefix — only core may publish ``hermes:<event>``.` | 事件总线的三条边界：保留命名空间、递归深度 cap 8、pending budget 64（§十 B5 的规模证据） |
| `:323-334` | `def _subscribe_event(self, owner: str, event: str, callback: Callable) -> None:` | 订阅要带 `owner`（谁登记的），并且**只接受 callable**；注册顺序即投递顺序 |
| `:399` | `resolve_plugin_command_result(callback(**copy.deepcopy(item.payload)))` | 事件派发前**深拷贝**载荷（§十 B9 的真实侧出处） |
| `:449-451` | `def iter_hook_callbacks(self, hook_name: str) -> tuple[Callable, ...]:` | *stable snapshot* —— mini 的 `callbacks_for()` 照它写（回调里增删不改变当前这轮遍历） |
| `:55-64` / `:449-470` | `# System-prompt sections are tightly bounded …` / `def render_system_prompt_sections(` | 系统提示 section 的硬预算（位置集合、4 000 字符、32 节、8 000 总量）与渲染入口（§十 B6） |

**`agent/api_request_hooks.py`（196 行）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:15` | `_SENSITIVE_HOOK_KEYS = {"api_key", "authorization", …` | 键级脱敏的固定键集（mini 逐字同值，`mini-hermes/agent/observer.py:185`） |
| `:51-57` | `def _hook_payload_max_chars() -> int:` | 总额上限：env `HERMES_PLUGIN_PAYLOAD_MAX_CHARS`、默认 50000、**下限 1000**、坏值回落 |
| `:59-70` | `def _is_sensitive_hook_key(key: Any) -> bool:` | 判定先 `lower().replace("-", "_")`，再"固定键集 **或** 后缀 `*_api_key`"两路命中；同时这里就是 `max_depth=8 / max_string=8000 / max_sequence=200` 三个默认参的所在（`:68-69`） |
| `:89-101` | `out: Dict[str, Any] = {}` | **dict 溢出 `break`**（后面的键根本没被访问）+ 一条 `_truncated_items`；list 那侧是切片保留前 N 再追加标记 —— 两个容器语义故意不同（本章 RV-6 就钉这条） |
| `:116-132` | `def _sanitize_hook_payload(cls, value: Any) -> Any:` | 两级降级：docstring 逐字 *full → reduced caps → truncated preview*；最后一级换成 `{"_truncated": True, "original_type": …, "preview": …}` |
| `:161-170` | `def _invoke_api_request_error_hook(` | `api_request_error` 的发出签名（含 `retryable` / `status_code` / `reason` 这些"解释为什么换 provider"的字段） |

**`model_tools.py`（987 行）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:643-653` | `class _CallIds:` | 关联 id 的**线协议**：5 字段冻结 dataclass + `hook_kwargs()` 的 *None -> ""*（mini 的 `CallIds` 逐字照此，`mini-hermes/agent/observer.py:323-340`） |
| `:685-698` | `if not has_hook("post_tool_call"):` … `return` | 工具观测的 fire-site 门：**先探再构造**，且 status 缺省时用 `_tool_result_observer_fields` 从结果反推 |
| `:852-860` | `if has_hook("transform_tool_result"):` | 改行为型钩子的采纳口径：*first string return wins*（`next((r for r in hook_results if isinstance(r, str)), result)`） |

**`hermes_cli/middleware.py`（239 行） / `hermes_cli/lifecycle.py`（63 行） / `hermes_cli/hooks.py`（335 行）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `hermes_cli/middleware.py:16-17` | `OBSERVER_SCHEMA_VERSION = "hermes.observer.v1"` | 版本常量真身；下一行是**另一个** schema（`hermes.middleware.v1`）——"telemetry"这个名字容易让人以为还有第二个 telemetry schema |
| `hermes_cli/middleware.py:38-46` | `def observer_payload(**kwargs: Any) -> Dict[str, Any]:` | 版本注入的第二种载体（`setdefault`），与 `hermes_cli/plugins_dispatch.py:201` 同形 |
| `hermes_cli/middleware.py:50-57` | `def _safe_copy(payload: Any) -> Any:` | **容错深拷贝**：请求体里可能有 client / 回调 / 文件句柄，硬 deepcopy 会把整条中间件链带崩 ⇒ 失败就退化为原样（§十 B9） |
| `hermes_cli/lifecycle.py:27-44` | `"""Notify first-party observers, then invoke compatibility plugin hooks."""` | 门面层的两段式：先 first-party 观察者、再兼容插件钩子；`has_hook` 在同文件另立一个（既查观察者也查插件） |
| `hermes_cli/hooks.py:1` | `"""hermes hooks — inspect and manage shell-script hooks."""` | 澄清落点：它是 shell-hooks 的 CLI，**不是**钩子名总表（总表在 `hermes_cli/plugins.py:108-199`） |
| `hermes_cli/hooks.py:165-175` | `def _cmd_test(args) -> None:` | 它只是**引用** `VALID_HOOKS` 当事件白名单（`Unknown event: …` / `Valid events: …`）；`_DEFAULT_PAYLOADS` 的合成载荷是给 `hooks test` / `doctor` 用的夹具 |

**观测 fire-site 与专用族（其余文件）**

| 文件:行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `agent/turn_context.py:473-478` | `effective_task_id = task_id or str(uuid.uuid4())` | `task_id` 与 `turn_id` 的生成点（`turn_id = f"{session_id or 'session'}:{task_id}:{uuid4().hex[:8]}"`；前面还有一条 relay 待定 id 的短路） |
| `agent/turn_context.py:666-672` | `def _collect_pre_llm_call_context(` | `pre_llm_call` 的采纳口径：注入**用户消息**（永不注入 system prompt）；`Oversized per-hook context is spilled to disk` |
| `agent/turn_context.py:678-688` | `"pre_llm_call",` | 该钩子的实参（`session_id` / `task_id` / `turn_id` / `user_message` …）——mini 的 `_payload_pre_llm_call` 取同名子集 |
| `agent/conversation_loop.py:774` | `"on_session_start", session_id=agent.session_id, model=agent.model,` | 会话开始事件的发出点 |
| `agent/conversation_loop.py:1536` | `s.api_request_id = agent._current_api_request_id = f"{s.turn_id}:api:{s.api_call_count}"` | 每请求一个 id 的**生成点**：形状 `{turn_id}:api:{n}` 逐字与 mini 同名同形（`mini-hermes/run_agent.py:1063`） |
| `agent/turn_api_request.py:52` | `if has_hook("pre_api_request"):` | 请求级事件的 fire-site 门 |
| `agent/turn_response_intake.py:63` | `if has_hook("post_api_request"):` | 响应侧的门（与上一行成对） |
| `agent/turn_finalizer.py:411-433` | `def _apply_output_hooks(` | `transform_llm_output` 与 `post_llm_call` 的次序与"每回合一次"口径（§十 D3）；`:418` 逐字 *First hook to return a string wins* |
| `agent/turn_finalizer.py:658-671` | `if not getattr(agent, "_persist_disabled", False):` | `on_session_end` 的实参（`session_id` / `task_id` / `turn_id` / `completed` / `failed` / `interrupted` / `turn_exit_reason` …）——**没有 `reason`**（精译 C-2 ⑥ 的落点） |
| `hermes_cli/cli_session_mixin.py:429-437` | `Non-blocking; errors swallowed. Safe from shutdown, /new, /reset.` | 会话边界派发的载荷（`session_id` / `platform` / `reason = "new_session" 或 "session_boundary"`） |
| `gateway/slash_commands_session.py:210-213` | `_invoke_hook("on_session_reset", session_id=_new_sid, reason="new_session",` | 网关侧 `on_session_reset` **多传** `old_session_id` / `new_session_id`（CLI 路径不传） |
| `agent/plugin_stream_hooks.py:66` | `payload.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)` | 流式钩子的版本注入走**另一条**路径（不在 `invoke_hook` 里） |
| `hermes_cli/kanban_db.py:180-188` | `def _kanban_observer_consumed(event: str) -> bool:` | 热路径短路的第二例；docstring 逐字 *Inspection failure counts as unconsumed (dropping an observer is always safe)* |
| `hermes_cli/kanban_db_dispatch.py:1197-1201` | `# the re-port of PR #56066). All five are observers only: …` | 零成本快路径的**权威注释**（逐字 *every call site short-circuits on has_hook(), so when nothing subscribes no payload is built … pay one dict probe*）——mini 的 `has()` 门就是这个形状 |
| `hermes_cli/observability/__init__.py:11-25` | `def observe_lifecycle(hook_name: str, **kwargs: Any) -> None:` | first-party 观察者的门面 + 失败放开（`logger.warning` 后继续），`handles_hook` 与之配对 |
| `hermes_cli/observability/relay_shared_metrics.py:935-950` | `_HOOK_HANDLERS: dict[str, Callable[[_Runtime, dict[str, Any]], Any]] = {` | 内置 Relay 消费方：实测 **13** 个钩子映射到 session / task / model-call / tool-call / approval 记录（§十 D2） |
| `tools/approval.py:895-905` | `hook_kwargs = dict(command=prompt_command, description=prompt_description, pattern_key=pattern_key,` | 审批钩子的实参 + `surface="cli"`；`:899-900` 两个钩子一前一后夹着真提示；`:903-905` 逐字交代 `cancelled` 的语义（**fail closed，但不把拒绝算在用户头上**） |
| `tools/approval_context.py:54-58` | `"""Invoke a plugin lifecycle hook (pre_approval_request / post_approval_response).` | *Never raises: approval flow is safety-critical, plugin observability is not.* —— 与本章失败放开同一条哲学 |
| `tools/delegate_tool.py:287-295` | `_invoke_hook("subagent_start", parent_session_id=parent_sid,` | 子代理族的字段实参（父/子 session 与 subagent id、`child_role` / `child_goal`） |
| `tools/delegate_tool_results.py:365-372` | `invoke_hook("subagent_stop", parent_session_id=…` | 子代理收尾的发出点（与 `agent/plugin_stream_hooks.py` 的流式族、Kanban 族同属"mini 无基座"的专用族，§十 B4/B10） |
| `plugins/observability/langfuse/__init__.py:1014-1026` | `def register(ctx) -> None:` | 真实侧**消费方样板**：`register(ctx)` 里一次性登记 **11** 个钩子（`*_api_request` 与 `*_llm_call` 两套名字都登记，跨版本可用） |
| `plugins/observability/langfuse/plugin.yaml:9-19` | `hooks:` | 同一批 11 个钩子写在清单里（清单与代码两处对齐） |

### mini 侧（锚 HEAD `1ccaa50`）

**`mini-hermes/agent/observer.py`（461 行，第 15 章新建）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:1-8` | `"""观察者（observer hooks）—— **只读遥测**在这里的落点（第 15 章）。` | 模块定位：只落四件核心（A1 注册表 / A2 零成本 / A3 失败放开 / A4 载荷安全），其余进"为什么不做" |
| `:10-15` | `\| mini \| 是什么 \| 真实侧锚 \|`（四件核心对照表的表头） | 四件核心各自对应的**真实侧锚点**（一张自查表） |
| `:17-32` | `## 事件表（7 个；★ 每个名字都有真实触发点）` | 7 个事件 × mini 触发点 × 载荷字段；`:29-32` 写清"字段名照真实侧同名、mini 没有的字段一律不编" |
| `:38-41` | `· 真实侧 ``post_llm_call`` 只发 **一次/回合**` | **D3 口径差的自述**（真实侧每回合一次 vs mini 每次模型调用——mini 未统一成每回合一次的观察者收尾） |
| `:44-54` | `## 分工（拍板 P4）：**不做会话级事件**` | 三层分工表（会话/进程 ⇒ 网关目录式钩子；agent 内三边界 ⇒ 本模块；改行为/阻断 ⇒ `approval_callback`） |
| `:56-62` | `## 只读语义（A8）` | 返回值一律忽略 + "要阻断走 `approval_callback`"——观察者不是第二套控制流 |
| `:64-80` | `## 零成本快路径（A2 · 本章★核心）` | 为什么门必须开在**构造之前**；并逐字引 `hermes_cli/kanban_db_dispatch.py:1197-1201`；`★ 这里**不做**身份比较` 一段解释为何照 `has()` 这一处事实源 |
| `:82-89` | `## 注册契约（A1）` | 四条：未知名只警告仍存 / 不去重 / 保序 / 只读查询 |
| `:91-108` | `## 载荷安全三件套（A4 · 与 agent/api_request_hooks.py:52-132 逐字对齐）` | ① 敏感键 + **文本级**两层 ② JSON 化截断（dict `break` ↔ list 保留前 N 的**差异语义**）③ 总额上限两级降级 |
| `:110-124` | `## 关联 id（A5）` / `## 版本字段（A6）` | 三档 id 的生成口径（每轮 / 每请求 / 取 provider）；版本字段**只在唯一装配出口注入**（调用点不许手写） |
| `:126-143` | `## 为什么不做（B 批 · 逐条对着真实侧说"为什么这里没有它"）` | §十 B1~B10 的**逐条理由**（本节与它一一对应，此处不重复抄） |
| `:158` | `from agent.context_compressor import redact_sensitive_text` | 文本级脱敏**复用第 5 章那一份**（7 条模式），并注明无 import 环 |
| `:165-173` | `OBSERVER_EVENTS: Tuple[str, ...] = (` | mini 的 `VALID_HOOKS`：只有**真接线**的 7 个 |
| `:176` / `:178-179` | `VALID_EVENTS = frozenset(OBSERVER_EVENTS)` | 注册校验用的集合；版本常量 `mini.observer.v1` 与键名 |
| `:185` / `:188` | `_SENSITIVE_HOOK_KEYS = {"api_key", "authorization", …` | 敏感键集与替换标记（逐字同真实侧 `:15` / `:95`） |
| `:190-197` | `JSONABLE_MAX_DEPTH = 8` | 三个阈值常量 + env 名（`MINI_OBSERVER_PAYLOAD_MAX_CHARS`，**故意不与真实侧同 env**，免得打架）+ 50000/1000 |
| `:200-210` | `def payload_max_chars() -> int:` | env 覆盖、下限 1000、坏值回落 |
| `:213-222` | `def is_sensitive_key(key: Any) -> bool:` | 先归一化再双路判定（固定键集 + 后缀 `*_api_key`） |
| `:225-243` | `def hook_jsonable(` | 函数契约与"两处容易抄错的地方"的自述；`:241-242` 注明 mini 的**唯一加法**（字符串先过文本级脱敏）及其代价（跑在截断之前的整串上） |
| `:249-251` | `value = redact_sensitive_text(value)` | 文本级脱敏的调用点 + 串截断 `...[truncated N chars]` |
| `:265-273` / `:274-279` | `if isinstance(value, dict):` / `if isinstance(value, (list, tuple, set)):` | 两个容器的溢出语义（dict `break` 只访问到第 201 个键；list 切片保留前 200 + 追加标记）——RV-6 断言的就是这里 |
| `:296-317` | `def sanitize_payload(value: Any) -> Any:` | 全量 → 降级 caps → 预览三级；`:304-305` 写明"超限是降级不是丢弃：观察者永远能读到东西" |
| `:323-340` | `class CallIds:` | 5 字段冻结 dataclass + `hook_kwargs()` 的 None→`""`（线协议）及其理由 |
| `:346-354` | `class ObserverRegistry:` | "一张 `事件名 -> [回调]` 表" + 三个动词（`add` / `has` / `emit`）；list 而非 set 正是为了保序与不去重 |
| `:357-369` | `def add(self, event: str, callback: Callable[..., Any]) -> None:` | 未知名 `logger.warning` + 无条件 `append`（宽容注册与不去重） |
| `:372-379` | `def has(self, event: str) -> bool:` | 唯一事实源：一次 dict 探针、无缓存、无身份比较、无标志位 |
| `:381-387` | `def has_observers(self, event: str) -> bool:` | 别名**不是**第二套判定（"可见性同源"） |
| `:389-407` | `def registered_events(self) -> Tuple[str, ...]:` | 四个只读查询（`registered_events` / `callbacks_for` 的稳定快照 / `callback_count` / `count`） |
| `:410-442` | `def emit(self, event: str, **fields: Any) -> None:` | 装配唯一出口的三件事（防御性再探 → 版本注入 + `sanitize_payload` → 逐回调独立 `try/except`、返回值丢弃）；`:421-422` 明确**不吃 `BaseException`**（`KeyboardInterrupt` / `SystemExit` 不归观察者管） |
| `:445-447` / `:450-456` / `:459-461` | `OBSERVERS = ObserverRegistry()` | 进程级默认表 + 两个模块级门面（`has_observers` / `emit`），给"agent 之外"的调用点用，判定源仍然只有 `has()` |

**`mini-hermes/run_agent.py`（1704 行；1341 → 1704）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:49-51` | `# ★ 第 15 章：只读遥测观察者（observer hooks）。` | 为什么 `OBSERVERS` 是进程级、构造时可换一份（测试隔离 / 多租户 / 网关按会话分表） |
| `:165` | `def _tool_result_observer_fields(result: Any) -> tuple:` | 工具结局从结果反推（`status` / `error_type` / 异常类名）——与 `model_tools.py:687` 同名同责 |
| `:210-213` | `# ---- 第 15 章：只读遥测观察者（observer hooks）----` … `observer: Optional[ObserverRegistry] = None,` | 第 8 个回调面：**事件名注册表**（与上面 7 个"固定形参"回调并列而不混同） |
| `:279-291` | `# 第 15 章：只读遥测观察者（observer hooks）` … `self._current_turn_id: Optional[str] = None` | 三条挂载理由（三个边界只在本文件 / 只有 `AIAgent` 同时握着 id 与用量 / `has()` 是唯一事实源）+ 两个计数器在轮次入口复位 |
| `:361-371` | `# 1b. ★ 第 15 章：**每轮一个 turn_id**` | `turn_id` 的形状照 `agent/turn_context.py:476-478`；`:364-366` 解释为何不用自增计数器（跨进程/跨库要对得上，agent 对象会被网关缓存复用 ⇒ 计数器会撞号） |
| `:502-511` | `# 第 15 章接缝 ①：pre_llm_call / post_llm_call` … `_pre_llm = self._observers.has("pre_llm_call")` | 回合级两个事件的门与发（`★ 零成本快路径` 注释就在这段） |
| `:537-542` | `self._observers.emit("post_llm_call", …status=_llm_status…` | 在 `finally` 里发 ⇒ **成功 / 失败 / 被中断都发**、用 `status` 区分（这个 shape 就是 D3 的落点） |
| `:951-986` | `"""**一次真发 HTTP** 的边界（第 15 章接缝 ②③④：pre/post/_error 三个事件）。` | 为何单独抽这一层（回退重发是一次**新请求** ⇒ 每请求一个 `api_request_id`、`attempt` 递增）；并明确**传输层重试不算新请求**；`:961-968` 三个门 + `api_request_id` 的生成（`{turn_id}:api:{n}`）；`:979-985` 错误发 `api_request_error`、成功发 `post_api_request` |
| `:1126-1129` | `if self._observers.has("pre_tool_call"):` | 工具级两个事件的门（agent 级工具 / 审批拒绝 / 参数解析失败三路都发） |
| `:1176` / `:1192-1196` | `_tool_status, _tool_error_type, _tool_error_message = _tool_result_observer_fields(content)` | 工具结局的判定与 `post_tool_call` 的**唯一出口** |
| `:1203-1207` | `# 规矩（改这里之前先读 agent/observer.py 的模块 docstring）：` | 三条载荷构造纪律：一个方法一个事件 · 调用点先问门 · 这里**只取值**（截断/脱敏/版本注入全在 `emit`） |
| `:1210-1226` | `def _observer_ids(self, *, tool_call_id: Optional[str] = None,` | 三档 id 的合并：`session_id` / `task_id` / `turn_id` 取 agent 当前值，`tool_call_id` / `api_request_id` 按事件传入 |
| `:1228-1241` | `def _payload_pre_llm_call(` | 回合级载荷（`iteration` / `message_count` + 关联 id） |
| `:1242-1258` | `def _payload_post_llm_call(` | 复用 pre 的公共层 + `status` / `tool_call_count` / `duration_ms`（**demo ③ 那条计数口径的来源**） |
| `:1259-1274` | `def _payload_api_request_common(` | 请求级公共层（`api_mode` / `provider` / `model` / `attempt` / `message_count`） |
| `:1275-1284` | `def _payload_pre_api_request(` | ★ 不带真实侧的 `request=`（脱敏后的请求体），理由在 `agent/observer.py` docstring（`request=` 会稳定挤掉最有用的字段） |
| `:1285-1303` / `:1304-1323` | `def _payload_post_api_request(` / `def _payload_api_request_error(` | 响应侧追加 `usage` / `status` / `duration_ms`；错误侧追加 `error` / `retryable` / `status_code` |
| `:1324-1337` | `def _payload_pre_tool_call(` | 工具级载荷（`tool_call_id` / `tool_name` / `args`） |
| `:1338-1355` | `def _payload_post_tool_call(` | 工具结局载荷 + `:1348` 逐条写明 `status` 的取值与判定依据 |

**`mini-hermes/tests/test_observers.py`（994 行，第 15 章新建）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:80` / `:113` / `:120` / `:128` / `:135` / `:167` | `def check(name: str, cond: bool, detail: str = "") -> None:` … | 夹具形状（与 `tests/test_skills.py` 同款）：`check` / `clear_config` / `make_agent` / `record_events` / `count_payload_builds`（零成本快路径的度量工具）/ `drive_one_tool_turn` |
| `:185` / `:198-221` | `def env_var(key: str, value):` / `class _LogCapture(logging.Handler):` | 第三条隔离（env 摘/还）与**日志夹具**（存 `record` 本体、`:423` 才 `getMessage()`）——真问题 2 的对照物 |
| `:240` / `:270` / `:296` | `def test_unknown_event_warns_but_is_still_stored()` … | 第 ① 组三条（宽容注册 / 不去重保序 / 只读查询） |
| `:323-343` | `★★ 没有观察者时，**载荷一次都不许构造**` | 第 ② 组核心断言；docstring 逐字写着"删掉任意一个调用点的门，本条立刻变红"（RV-1 的预期） |
| `:398-427` | `★ 一个坏回调**只能丢自己**` | 第 ③ 组 5 条（RV-2 的预期）；`:401-402` 也写明"删掉那个 `try/except`"会红哪两条 |
| `:504-511` / `:556-557` | `阈值就是真实侧那三个默认：depth 8 / string 8000 / sequence 200` / `★★ 那个 break 是**真的提前停止**（只访问了 201 个键）` | RV-4 / RV-4b / RV-6 的预期落点 |
| `:631-637` | `★ 全 None ⇒ 全是 ""` | RV-3 的预期落点（两条） |
| `:918-958` | `GROUPS = [` | 六组注册表（组标题 + 承诺 + 用例函数）——§九 ② 那张表的原文出处 |
| `:961-985` | `def main() -> None:` | `:966-971` 未注册自检（写了 `test_` 却没进 `GROUPS` ⇒ `SystemExit(2)`）；`:977-980` 每条用例独立接住异常 |

**`mini-hermes/demos/ch15_real_run.py`（368 行，第 15 章新建）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:1-19` | `"""第 15 章 · 真跑：**观察者真的在旁边看着，而且什么都没改**` | 真跑的承诺口径（五段逐条列出；假 provider 驱动真 agent、不联网） |
| `:28-31` | `# ── 隔离三件套：MINI_HOME 必须在 **import mini 的模块之前** 设好 ─` | 临时 `MINI_HOME` + `sys.path` 注入（照第 13/14 章骨架） |
| `:47-50` | `PAYLOAD_BUILDERS = ("_payload_pre_llm_call", …` | 7 个构造器的名单（demo 自己数它们被调了几次） |
| `:82-87` | `def subscribe_all(registry: ObserverRegistry) -> list:` | 把 7 个事件全订上、按真发顺序收进流水 |
| `:106-118` | `def count_payload_builds(agent: AIAgent) -> dict:` | 给载荷构造器**计数包裹**（这就是"0 次"可被断言的手段） |
| `:121-131` | `def drive_turn(agent, tool_name, args) -> dict:` | 假 provider：第 1 次响应要工具、第 2 次给最终回答（一次工具调用 ⇒ 2 次 LLM 调用 / 2 次请求） |
| `:141-182` | `echo = "_test_ch15_echo"` … 第 ① 段 8 条断言 | 7 个事件名的真流水（10 发）+ 关联 id 断言（1 个 `turn_id` / 2 个 `api_request_id` / provider 的 `call_prov_1`）+ "观察者没改结果" |
| `:187-217` | `reg2 = ObserverRegistry()`（第 ② 段） | `api_request_error` 那一路：`error.type` / `status_code=429` / **`retryable=True`** / `attempt` 1→2 / 载荷里能看到换了谁 / 只有成功那发有 `post_api_request` |
| `:222-248` | `cold = make_agent(ObserverRegistry())`（第 ③ 段） | **0 观察者 ⇒ 总计 0 次**；有观察者 ⇒ 计数跟流水条数走；`:234-235` 印出"pre_\* 的 2 次直发 + 被 post 复用"这层口径 |
| `:253-289` | `reg4 = ObserverRegistry()`（第 ④ 段：仿真凭据 / 9000 字符串 / 深嵌套 / 260 键大 dict） | 键级 `<redacted>`、文本级抹掉 `ghp_…`、串截到 8000 并注明丢了多少、`<dict depth limit>`、大 dict **只留 200 个键 + `_truncated_items`** |
| `:291-309` | `os.environ[PAYLOAD_MAX_CHARS_ENV] = "1000"` | 把总额压到下限逼出"降级 → 预览"；并证明 env 覆盖与"跑完还回去（默认 50000）" |
| `:314-352` | `warnings: list = []`（第 ⑤ 段：三个回调、中间那个抛异常） | 坏回调只丢自己（`hits == ["before", "after"]`）、轮次照常收尾、有警告点名、返回值一律忽略 |
| `:356-359` | `n_ok = sum(1 for _, c, _ in RESULTS if c)` | 收据行（`N 通过 / M 失败（共 K 条）`，`exit code` 由它决定） |

---

## 附录 B · 两边的规模与实测行数

行数一律 `wc -l` 实测：**真实侧** `git -C ~/.hermes/hermes-agent show f5d1926110:<路径> | wc -l`；**mini 侧** `wc -l mini-hermes/<路径>`。锚：真实侧 `f5d1926110`、mini 侧 HEAD `1ccaa50`。**本节不写"文件共 N 行"以外的推断**；所有数字本轮逐条重取过一次。

**B-1 · 真实侧（锚 `f5d1926110`，10 个文件）**

| 文件 | 实测行数 | 它是什么（本章用它的哪一面） |
|---|---|---|
| `hermes_cli/plugins.py` | **2147** | 注册面：钩子名总表 `VALID_HOOKS`（`:108-199`，**39 名**）、`register_hook`（`:916`）、`_track_callback`（`:927`）、`has_hook`（`:1738`）、改行为指令解析（`:1797` / `:1830`） |
| `hermes_cli/plugins_dispatch.py` | **530** | 派发核：`invoke_hook`（`:188`）、版本注入（`:200-201`）、逐回调隔离（`:206-220`）、失败告警去重（`:222-244`）、三张超时表 + 抑制窗口（`:25-53`）、bounded worker（`:246-321`）、事件总线（`:87-93` / `:323`）、系统提示 section（`:449-470`） |
| `agent/api_request_hooks.py` | **196** | 载荷安全：敏感键（`:15` / `:59`）、总额上限（`:51-57`）、JSON 化三阈值（`:68-69`）、溢出语义（`:89-101`）、两级降级（`:117-132`）、`api_request_error` 发出（`:161`） |
| `model_tools.py` | **987** | 关联 id 线协议 `_CallIds`（`:643-653`）；工具观察 fire-site 门（`:687`）与 `status` 推导（`:657`）；`transform_tool_result` 采纳（`:852-860`） |
| `agent/plugin_stream_hooks.py` | **165** | 流式观察者（§十 B4 的规模出处）；版本注入的第二条路径（`:66`） |
| `hermes_cli/middleware.py` | **239** | `OBSERVER_SCHEMA_VERSION`（`:16`）、`observer_payload`（`:38`）、容错深拷贝 `_safe_copy`（`:50-57`） |
| `hermes_cli/hooks.py` | **335** | `hermes hooks` 运维面（shell hooks，§十 B7）；它**不是**钩子名总表（总表在 `plugins.py`） |
| `plugins/observability/langfuse/__init__.py` | **1026** | 真实消费方样板：`register(ctx)`（`:1014`）+ **11** 个钩子（`:1017-1025`）；清单 `plugins/observability/langfuse/plugin.yaml:9-19` 同步 |
| `agent/turn_context.py` | **1182** | `task_id` / `turn_id` 生成（`:473-478`）；`pre_llm_call` 的采纳与实参（`:666-688`） |
| `agent/conversation_loop.py` | **1683** | `on_session_start`（`:774`）；每请求一个 `api_request_id` 的生成点（`:1536`） |

**B-2 · mini 侧（锚 HEAD `1ccaa50`，4 个文件）+ 本次 diff stat**

| 文件 | 实测行数 | 变化 | 它是什么 |
|---|---|---|---|
| `mini-hermes/agent/observer.py` | **461** | 新建 | 观察者注册表 + `CallIds` + 载荷安全三件套 + 进程级 `OBSERVERS`（本章唯一的新模块） |
| `mini-hermes/run_agent.py` | **1704** | 1341 → 1704（`--numstat` **379 增 / 16 删**） | 7 处触发点接线 + 7 个载荷构造器 + `turn_id` / `api_request_id` 生成（唯一构造点是 `AIAgent(observer=…)`） |
| `mini-hermes/tests/test_observers.py` | **994** | 新建 | 6 组 / 21 用例 / 124 条 |
| `mini-hermes/demos/ch15_real_run.py` | **368** | 新建 | 真跑（假 provider 驱动真 agent）：28 条、双解释器 28 / 28 |

本次 diff stat（`git diff 977cc85 1ccaa50`，逐字）：

```text
mini-hermes/agent/observer.py       | 461 +++++++++++++++++
mini-hermes/demos/ch15_real_run.py  | 368 +++++++++++++
mini-hermes/run_agent.py            | 395 +++++++++++++-
mini-hermes/tests/test_observers.py | 994 ++++++++++++++
4 files changed, 2202 insertions(+), 16 deletions(-)
```

```text
461	0	mini-hermes/agent/observer.py
368	0	mini-hermes/demos/ch15_real_run.py
379	16	mini-hermes/run_agent.py
994	0	mini-hermes/tests/test_observers.py
```

两处计数口径不同，别混用：`--numstat` 的 `run_agent.py` 是 **379 增 / 16 删**（净 +363 = 1341 → 1704）；`--stat` 给同一文件的条长 **395** 是"变更行数"（379 + 16）。四个文件里三个是新建，`run_agent.py` 是唯一被改的既有文件。

---

## 附录 C · 上游文档与源码的出入

> 核对锚：上游 commit `f5d1926110`（version `0.21.3`）。核对键位：钩子名、返回动作、状态值、审批 `choice` 值域、载荷字段名、schema 版本常量、截断阈值、"失败放开"的处置位置。绝大多数条目**逐条命中**，另有 **7 处出入**逐条给出，末尾附 5 处说明（不计入出入）。以下**搬自《中文精译-15》附录 C**（C-2 的 7 条 + C-3 的 5 处），`文件:行号` 逐条保留、结论不改写。

**C-1 · 已逐条命中的（结论：一致）**

- §0/§1 的点名钩子**全部**在钩子名总表里：文档点名 17 个钩子名（`pre_api_request`、`post_api_request`、`api_request_error`、`pre_tool_call`、`post_tool_call`、`transform_tool_result`、`transform_llm_output`、`pre_llm_call`、`post_llm_call`、`on_session_start`、`on_session_end`、`on_session_finalize`、`on_session_reset`、`subagent_start`、`subagent_stop`、`pre_approval_request`、`post_approval_response`）逐个进 `hermes_cli/plugins.py:108-199` 的 `VALID_HOOKS` 里 `grep -c` → **17/17 命中**（各恰好 1 次；该集实测共 39 个钩子名）。
- §1 `ctx.register_hook("...", ...)` → `hermes_cli/plugins.py:916` 逐字 `def register_hook(self, hook_name: str, callback: Callable) -> PluginRegistration:`（`:917` 逐字 *"Register a lifecycle hook callback (unknown names warn but are still stored)."*）。
- §1 `telemetry_schema_version = "hermes.observer.v1"` → `hermes_cli/middleware.py:16` 逐字 `OBSERVER_SCHEMA_VERSION = "hermes.observer.v1"`；注入点 `hermes_cli/plugins_dispatch.py:201` 逐字 `kwargs.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)`、`hermes_cli/middleware.py:40` / `:45`、`agent/plugin_stream_hooks.py:66` 逐字 `payload.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)`。**常量值逐字一致。**
- §1 "回调失败放开：Hermes 捕获异常、记警告、循环继续" → `hermes_cli/plugins_dispatch.py:188 def invoke_hook`，逐回调 `try/except` → `:222 def _report_hook_failure`，`:242` 一条 `logger.warning(...)`（同一 `(hook, callback, error)` 只报一次，重复降到 DEBUG）；`:191` docstring 逐字 *"Each callback is isolated."*。
- §1 `pre_llm_call` 可返回字符串或 `{"context": "..."}` 注入当前用户消息 → `agent/turn_context.py:702-705` 逐字 `if isinstance(r, dict) and r.get("context"):` / `_piece = str(r["context"])` / `elif isinstance(r, str) and r.strip():`；`:670` docstring 逐字 *"their context is injected into the user message (never the system prompt)."*
- §1 `transform_llm_output` 可返回替换最终助手文本 → `agent/turn_finalizer.py:418-427`，`:418` 逐字 `# First hook to return a string wins; None/empty leaves the text unchanged.`
- §5 "昂贵载荷构造被 `has_hook(...)` 门控" → `agent/turn_api_request.py:52 if has_hook("pre_api_request"):`；`agent/turn_response_intake.py:63 if has_hook("post_api_request"):`；`agent/api_request_hooks.py:171 if not _lifecycle.has_hook("api_request_error"):`；`model_tools.py:687 if not has_hook("post_tool_call"):`；`hermes_cli/plugins_dispatch.py:445 def has_hook` 逐字 *"Return True when at least one callback is registered for a hook."*
- §3.3 `pre_api_request` 的 17 个字段（含 `request`）、`post_api_request` 的增量 11 个字段、`api_request_error` 的增量 10 个字段（含 `error = {"type": ..., "message": ...}`）→ 分别在 `agent/turn_api_request.py:60-80`、`agent/turn_response_intake.py:64-87`、`agent/api_request_hooks.py:161-195` 逐字命中（详情见 C-2 ⑦ 的"漏项"部分，命中项不含其中）。
- §3.4 "`post_tool_call` 在 blocked 与 cancelled 路径也会发出" → `model_tools.py:938 return _emit(result, status="blocked", error_type=error_type, error_message=error_message)`；`agent/tool_executor.py:633` 逐字 `ref.emit_post(agent, result, status="blocked", error_type=error_type, error_message=error_message)`；`agent/tool_executor.py:292` 逐字 `status="cancelled", error_type="keyboard_interrupt", error_message=message`、`:912`、`:1401`。
- §3.4 `transform_tool_result` 在 `post_tool_call` 之后、结果入模型上下文之前 → `model_tools.py:953` 先 `_emit(result, duration_ms=duration_ms)`，`:954` 再 `return _apply_transform_tool_result_hook(function_name, function_args, result, duration_ms, ids)`；`:852 if has_hook("transform_tool_result"):`。
- §3.5 审批钩子是 observer-only、不能预先作答或否决 → `hermes_cli/plugins.py:138-139` 逐字 `# Approval observers (tools/approval.py); returns ignored — plugins cannot veto or pre-answer` / `# (use pre_tool_call).`；派发处 `tools/approval_context.py:54 def _fire_approval_hook` 逐字 *"Never raises: approval flow is safety-critical, plugin observability is not."*
- §3.5 `pre_/post_approval_*` 的字段 `command` / `description` / `pattern_key` / `pattern_keys` / `session_key` / `surface` → `tools/approval.py:895-896` 逐字 `hook_kwargs = dict(command=prompt_command, description=prompt_description, pattern_key=pattern_key,` / `pattern_keys=list(pattern_keys), session_key=session_key, surface="cli")`；`surface` 的三个文档值实测都在：`cli`（`:896`）、`gateway`（`:821`）、`smart`（`tools/approval_smart.py:135`）。
- §3.5 `cancelled` 的语义（没人作答、提示被撤下）→ `tools/approval.py:903-905` 逐字 `if choice == "cancelled":` / `# The prompt never reached a human (callback raised, no callback under prompt_toolkit, interrupted` / `# read): fail closed, but do not attribute a refusal to the user (#22992).`
- §3.6 `subagent_start` 的七个字段 → `tools/delegate_tool.py:287-295` 逐字 `"subagent_start", parent_session_id=parent_sid,` / `parent_turn_id=getattr(parent_agent, "_current_turn_id", "") or "", parent_subagent_id=parent_subagent_id,` / `child_session_id=getattr(child, "session_id", None), child_subagent_id=subagent_id,` / `child_role=effective_role, child_goal=goal,`。**字段名与文档逐字一致。**
- §3.6 `tool_call_history` 只含元数据、URL 的 query / fragment 被移除 → `tools/delegate_tool_results.py:134 def _subagent_stop_tool_call_history`，`:150-155` 逐字条目 `{"tool_name": str(item.get("tool") or "unknown")[:256], "tool_input": {argument_keys, targets}, "input_bytes", "output_bytes", "status"}`；`tools/delegate_tool_results.py:81 def _sanitize_tool_target` 逐字 *"Keep bounded side-effect targets while dropping URL secrets."* 与 `:88 bounded = value[:1024]`、`:99 return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))`（query 与 fragment 置空；`_TOOL_INPUT_URL_KEYS` = `{"endpoint", "url", "urls"}`，`:79`）。
- §7 内置 Langfuse 插件 → `plugins/observability/langfuse/__init__.py:1014 def register(ctx) -> None:`，`:1017-1025` 注册 11 个钩子（`pre_api_request` / `post_api_request` / `api_request_error` / `pre_llm_call` / `post_llm_call` / `pre_tool_call` / `post_tool_call` / `on_session_finalize` / `on_session_end` / `subagent_start` / `subagent_stop`）；`plugins/observability/langfuse/plugin.yaml:10-19` 的 `hooks:` 同步列出同一批。
- §0/§7 第一方 NeMo Relay 路径 → `hermes_cli/observability/relay_shared_metrics.py:935 _HOOK_HANDLERS`、`:950 HANDLED_HOOKS = frozenset(_HOOK_HANDLERS)`（13 个钩子映射到 Relay 的 session / task / model-call / tool-call / approval 记录）。
- §0/§7 点名的姊妹文档 `relay-shared-metrics.md` → 在场（`website/docs/developer-guide/relay-shared-metrics.md`，487 行）。

**C-2 · 出入（7 条，逐条给 `文件:行号` + 逐字引文）**

**① §1 的 `pre_tool_call` 返回动作少列一个：源码还支持 `{"action": "approve", ...}`。**

- 原文第 63 行逐字（表格该行）：``| `pre_tool_call` | May return `{"action": "block", "message": "..."}` to block a tool before execution, or `{"action": "modify", "args": {...}}` to transform the tool's input arguments. |``
- 源码 `hermes_cli/plugins.py:1797 def _get_pre_tool_call_directive_details`，`:1802-1805` 逐字：*"Check ``pre_tool_call`` hooks for ``{"action": "block", "message"}`` (veto; message becomes the tool result) or ``{"action": "approve", "message", "rule_key"?}`` (escalate ANY tool to the human-approval gate; ``rule_key`` picks the ``[a]lways`` allowlist grain). First valid directive wins; irrelevant returns are ignored."*
- 实现分支 `:1830 if action not in ("block", "approve"):`、`:1835 if action == "block" and not message:`、`:1837 rule_key = result.get("rule_key") if action == "approve" else None`。
- **结论**：**漏列** `approve`（把任意工具升级到人工审批闸门的第三个动作），非矛盾。

**② §3.4 的 `status` 表少列 `timeout`。**

- 原文第 186–191 行的表只列 4 值：`ok` / `error` / `blocked` / `cancelled`。
- 源码还发出 `timeout`：`agent/tool_executor.py:920` 逐字 `result_cls, outcome = _ToolTimeoutResult, dict(` / `duration_ms=int(timeout_s * 1000), status="timeout", error_type="tool_timeout", error_message=message,`；`:1397` 逐字 `outcome = dict(duration_ms=int((timeout_s or 0.0) * 1000), status="timeout", error_type="tool_timeout", error_message=function_result)`。
- 另，子代理的 `tool_call_history` 里还有第 5 个值 `unknown`：`tools/delegate_tool_results.py:154` 逐字 `"status": status if status in {"ok", "error"} else "unknown",`。
- **结论**：**漏列** `timeout`（与 `unknown`）—— 文档说 "Common values include:"，但缺的这两个恰是超时/异常路径的观察者最关心的终态。

**③ §3.5 的 `choice` 清单不全，且未提 `decided_by`。**

- 原文第 208–210 行逐字：*"`post_approval_response` also includes `choice`, with values such as `once`, `session`, `always`, `deny`, `timeout`, and `cancelled` (nobody answered: …)"*。
- 源码另有：`tools/approval_smart.py:144` 逐字 `_ctx._fire_approval_hook("post_approval_response", **payload, choice=f"smart_{verdict}", decided_by="aux_llm")`（→ `smart_approve` / `smart_deny` + 字段 `decided_by`）；`tools/approval_gateway_wait.py:192` 逐字 `_ctx._fire_approval_hook("post_approval_response", **payload, choice="notify_failed")`；`tools/approval_prompt.py:248` 逐字 `hook_choice = result.choice if result.failure is None else f"transport_{result.failure}"`。
- 源码侧对这条值域的自我描述见 `hermes_cli/plugins.py:140-141` 逐字 `# surface: "cli"|"gateway"|"smart"; post_approval_response adds choice ("once"|"session"|` / `# "always"|"deny"|"timeout"|"smart_approve"|"smart_deny") and decided_by.`
- **结论**：**漏列** 4 类取值（`smart_approve` / `smart_deny` / `notify_failed` / `transport_<failure>`）与字段 `decided_by`。注意：文档列的 6 个值**全部真实存在**（`cancelled` 见 `tools/approval.py:903`、`tools/approval_gateway_wait.py:94`），所以这是清单不全而非笔误。

**④ §3.5 的 `surface` 值域比文档宽。**

- 原文把 `surface` 与其余字段并列，未给值域；源码自我描述为 `"cli"|"gateway"|"smart"`（`hermes_cli/plugins.py:140`）。
- 实测值域还含 `transport:<name>`：`tools/approval_prompt.py:238` 逐字 `pattern_keys=list(pattern_keys), session_key=session_key, surface=f"transport:{name}",`。
- **结论**：**漏列**。文档没写给值域，属"口径省略"；但若读者按 `hermes_cli/plugins.py:140` 的三值去枚举，会漏掉插件审批传输这一路。

**⑤ §1 "钩子回调是失败放开"有一个明确反例：超时路径下的 `pre_tool_call` 是 fail-closed。**

- 原文第 54–55 行逐字：*"Hook callbacks are fail-open. Hermes catches callback exceptions, logs a warning, and keeps the agent loop running."*
- 异常路径确实失败放开（见 C-1 第 4 条）。但**超时/挂死**路径不同：`hermes_cli/plugins_dispatch.py:48` 逐字 `_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS: Set[str] = {"pre_tool_call"}`；`:212` 逐字 `results.append({"action": "block", "message": _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE})`；`:53` 逐字 `_PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE = "pre_tool_call plugin callback timed out or is still running"`；`:191-193` docstring 逐字 *"Bounded hooks and ``pre_tool_call`` run under ``plugins.hook_callback_timeout`` (worker abandoned, never joined); ``pre_tool_call`` fails closed with a block directive, others skip."*
- **结论**：文档的 "fail-open" 只对**回调抛异常**成立；对一个**超时或仍在运行**的回调，`pre_tool_call` 会返回阻断指令（fail-closed），其余有界钩子则被跳过（既非放开也非阻断，而是"这次不上报"）。观察者插件不能把"失败放开"当成"我的钩子绝不影响执行"的全面保证。

**⑥ §3.1 的会话字段清单是跨钩子的并集，且 `on_session_end` 的字段名与实测不符。**

- 原文第 103–104 行逐字：*"Common fields include `session_id`, `completed`, `interrupted`, `reason`, `old_session_id`, and `new_session_id` where available."*
- 实测 `on_session_end` 的载荷（`agent/turn_finalizer.py:660-671`）逐字为 `session_id` / `task_id` / `turn_id` / `completed` / `failed` / `interrupted` / `turn_exit_reason` / `model` / `platform` —— **没有 `reason`**，且有文档未列的 `failed` 与 `turn_exit_reason`。
- `reason` 与 `old_session_id` / `new_session_id` 分别属于另外两个钩子：`hermes_cli/cli_session_mixin.py:434-437` 逐字 `context = {` / `"session_id": self.agent.session_id if self.agent else None,` / `"platform": getattr(self, "platform", None) or "cli",` / `"reason": "new_session" if event_type == "on_session_reset" else "session_boundary"}`；`gateway/slash_commands_session.py:210-213` 逐字 `_invoke_hook("on_session_reset", session_id=_new_sid, reason="new_session",` / `platform=source.platform.value if source.platform else "",` / `old_session_id=_old_sid, new_session_id=_new_sid)`。
- **结论**：并集口径可读但**易误用** —— `old_session_id` / `new_session_id` 只在 **gateway** 的 `on_session_reset` 上出现（CLI 路径不传），`reason` 不在 `on_session_end` 上。按字面在 `on_session_end` 里读 `kwargs["reason"]` 会抛 `KeyError`；`kwargs.get("reason")` 才会返回 `None`。

**⑦ §3.2 / §3.3 的字段清单均为**子集**（原文用 "Common … include" / "includes"）。**

- §3.2 `pre_llm_call`：原文第 119–121 行列 8 项；实测 `agent/turn_context.py:678-688` 另含 `task_id` 与 `parent_session_id`（逐字 `parent_session_id=getattr(agent, "_parent_session_id", None) or "",`）。
- §3.3 `pre_api_request`：原文第 142–147 行列 13 项；实测 `agent/turn_api_request.py:63-81` 另含 `user_message`、`conversation_history`、`retry_count`、`request_messages`、`system_prompt`、`middleware_trace`。
- §3.3 `post_api_request`：原文第 151–156 行列 11 项；实测 `agent/turn_response_intake.py:71-87` 另含 `first_chunk_at`（`:79-81` 逐字 `# First stream chunk time (epoch s); None if not streamed / no chunk.` / `# TTFB = first_chunk_at - started_at.`）与 `moa_references`（`:87` 逐字 `moa_references=_moa_reference_metrics_for_hook(agent),`）。
- **结论**：**漏列**。文档措辞（"Common … include" / "includes"）使这些属**子集口径**而非矛盾，但三处合计漏 10 个字段；其中 `first_chunk_at` 与 `moa_references` 对"首字节延迟"和"MoA 扇出计价"是唯一的观察通道。

**C-3 · 说明（5 处，不算出入）**

- **钩子名总表的真身在 `hermes_cli/plugins.py:108`，不在 `hermes_cli/hooks.py`。** `hermes_cli/hooks.py:1` 逐字 `"""hermes hooks — inspect and manage shell-script hooks."""`，是 `list` / `test` / `revoke` / `doctor` 四个子命令的 CLI（335 行），它只是**引用** `from hermes_cli.plugins import VALID_HOOKS`（`:169`）来做事件名校验。文档点名的 17 个钩子应去 `hermes_cli/plugins.py:108-199` 核。属**落点澄清**，不是矛盾。
- **`telemetry_schema_version` 的字段名与常量名不同源。** 字段叫 `telemetry_*`，值取自 `OBSERVER_SCHEMA_VERSION`（`hermes_cli/middleware.py:16`）；同一文件 `:17` 另有 `MIDDLEWARE_SCHEMA_VERSION = "hermes.middleware.v1"`，只加在中间件载荷上（`:45`）。文档给的是观察者那一个，值逐字正确 —— 只是"telemetry"这个名字容易让人以为还有第二个 telemetry schema。属**命名口径**差异。
- **"注入到每一个钩子载荷"有一个显式例外。** `hermes_cli/plugins_dispatch.py:198-200` 逐字 `# Gateway platform events define event-local envelopes; a bus-wide version here would turn` / `# unrelated adapter payloads into one monolithic compatibility contract.` / `if hook_name != "gateway_platform_event":` / `kwargs.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)`。另，流式钩子（`on_stream_*`、`on_interim_message`、`on_room_member_activity`）走 `agent/plugin_stream_hooks.py:66` 的另一条注入路径。文档的"every hook payload"应读作"经 `invoke_hook` 派发的绝大多数钩子"。
- **"bounds large payloads"没有给数字，实测阈值如下（属口径省略）。** `agent/api_request_hooks.py:53-57` 逐字 `raw = os.getenv("HERMES_PLUGIN_PAYLOAD_MAX_CHARS", "50000")` / `return max(1000, int(raw))` / `return 50000`；`:68` 逐字 `cls, value: Any, *, depth: int = 0, max_depth: int = 8, max_string: int = 8000,`（`max_sequence: int = 200` 在 `:69`）；`:118` docstring 逐字 *"JSON-able payload under the size cap: full → reduced caps → truncated preview."*；`:122` 降级档逐字 `for caps in ({}, {"max_string": 1000, "max_sequence": 50}):`；`:129-132` 仍超限则整块退化为逐字 `{"_truncated": True, "original_type": type(value).__name__, "preview": encoded[:limit]}`。敏感键表 `:15` 逐字 `_SENSITIVE_HOOK_KEYS = {"api_key", "authorization", "proxy_authorization", "cookie", "set_cookie"}`。
- **`pre_llm_call` 的返回还多一层"超长即落盘"的处置。** `agent/turn_context.py:670-672` docstring 逐字 *"Run ``pre_llm_call`` plugins; their context is injected into the user message (never the system prompt). Oversized per-hook context is spilled to disk so a runaway plugin can't inflate every subsequent turn's prompt."*（`:693-707` 走 `tools.hook_output_spill.spill_if_oversized`）。文档只说"注入当前用户消息"，未提这层上限 —— 属**表述范围**差异，不构成冲突。
