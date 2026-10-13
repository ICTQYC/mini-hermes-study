# 中文精译 · 第 15 章：观察者钩子（Observer Hooks）

> 上游：`官方文档-Developer-Guide/observer-hooks.md`（上游路径 `website/docs/developer-guide/observer-hooks.md` · 原文 **331 行** · 1 个 H1 + 7 个 H2 + 6 个 H3）
> 定位：Hermes 观察者钩子（Observer Hooks）是插件**只读**重建 agent 执行轨迹的遥测契约 —— 事件族、关联 id（correlation id）、脱敏载荷（payload）、时序与状态。
> 📌 行号锚：上游 commit `f5d1926110`（version = "0.21.3"）
> 本章出入：附录 C 实测 **7 处**文档↔源码不符（另附 5 处说明，不计入）。

[TOC]

---

## 0. 这一章在回答什么

> 📌 译注（补充，2026-10-13）

观察者钩子回答的是"**插件想在不改动运行时行为的前提下重放/导出 agent 到底做了什么，该接在哪里**"。这里的「观察者（observer）」指一种**只上报、不干预**的消费者：它订阅 Hermes 的生命周期边界，拿到的是稳定的事件、关联 id（correlation id）、脱敏过的载荷（payload）、时序、状态与错误字段 —— 但换不掉任何一个执行环节。这套契约刻意做成**与后端无关**，于是 trace、指标、审计、重放、导出各方向可以各接各的，官方点名的是 Langfuse、OpenTelemetry 风格的采集器，以及 NeMo Relay。它**不**替代 Hermes 的规划器、模型 provider、记忆、工具注册表、审批 UX、CLI、网关行为或执行语义；会**改变行为**的请求/执行包装器不在这份契约内，那是中间件（middleware）的地盘。钩子回调统一是**失败放开（fail-open）**的：回调抛异常只会记一条警告，主循环照跑 —— 观察者的崩溃不该变成用户会话的崩溃。Hermes 另有一条第一方 NeMo Relay 共享指标路径，直接复用这些生命周期边界，不需要启用任何可观测性插件。本章按"契约 → 关联 id → 六大事件族 → 载荷安全 → 性能 → 怎么写 → 谁在用"的顺序展开。

---

## 1. Contract —— 契约

插件从 `register(ctx)` 里注册观察者回调：

```python
def register(ctx):
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
```

每个钩子回调都以关键字参数（keyword arguments）的形式接收载荷。插件应当接受 `**kwargs`，这样后续**增量新增**的字段仍保持向后兼容：

```python
def on_post_tool_call(**kwargs):
    tool_name = kwargs.get("tool_name")
    status = kwargs.get("status")
    result = kwargs.get("result")
```

插件管理器把这个字段注入到每一个钩子载荷里：

```text
telemetry_schema_version = "hermes.observer.v1"
```

钩子回调是**失败放开（fail-open）**的：Hermes 会捕获回调异常、记一条警告，并让 agent 循环继续跑下去。换句话说，回调抛出的异常只影响这次上报，不影响这一轮对话。

大多数观察者钩子的返回值会被忽略。例外是几个更早的、**会改变行为**的钩子：

| 钩子 | 返回行为 |
| --- | --- |
| `pre_llm_call` | 可返回一个字符串或 `{"context": "..."}`，把临时上下文注入当前用户消息。 |
| `pre_tool_call` | 可返回 `{"action": "block", "message": "..."}` 在执行前阻断一个工具，或返回 `{"action": "modify", "args": {...}}` 变换工具的输入参数。 |
| `transform_tool_result` | 可在 `post_tool_call` 之后返回一个替换用的工具结果字符串。 |
| `transform_llm_output` | 可返回一个替换用的最终助手文本字符串。 |

遥测类插件应当把这些**会改变行为**的返回当作可选的历史兼容特性，而不是可观测性的必要条件。

这套契约的取舍很直白：观察者要能"看见"已经够难了，一旦让"能看见的人"也能改行为，观测插件就成了事实上的中间件，二者必须分开。因此条文只给了两条规矩 —— **只增不改**（`**kwargs` 保证新增字段不破坏老回调）和**拿不到就别造**（返回值默认忽略，只有上表四个老钩子例外）。反过来说，砍掉 `**kwargs` 会让每次加字段都变成破坏性变更；砍掉"返回值默认忽略"，观察者与执行者就会混成一团，任何观测插件的 bug 都会变成行为变更。

---

## 2. Correlation IDs —— 关联 id

观察者载荷使用稳定的 id，让插件不必**只靠回调顺序**就能把事件拼起来。

| 字段 | 含义 |
| --- | --- |
| `session_id` | 会话 / session 身份。 |
| `task_id` | 任务身份，对子代理与隔离执行尤其有用。 |
| `turn_id` | 用户回合身份，一个回合里的各次 API 尝试与各次工具调用共享它。 |
| `api_request_id` | 不透明的 provider 尝试身份。**不要解析它的字符串格式。** |
| `api_call_count` | agent 循环内的数字型 API 尝试计数。 |
| `tool_call_id` | provider 提供的工具调用 id（如可用）。 |
| `parent_session_id` / `child_session_id` | 被委派的子代理的会话链接。 |
| `parent_subagent_id` / `child_subagent_id` | 子代理链接（如可用）。 |
| `parent_turn_id` | 派生出被委派工作的那个父回合。 |

消费者应当优先使用显式字段，而不是去解析复合 id。尤其是 `api_request_id`，它是一个**不透明**的关联值。

把 id 做成显式字段（而不是让消费者去拆一个复合字符串），是为了让 id 的字形可以随便演进。`api_request_id` 被单独点名"不透明"，就是这条规矩的样板：它是 provider 尝试的句柄，不是能拆出时间戳或序号的结构 —— 任何按格式切分它的代码都会在实现演进时碎掉。

---

## 3. Event Families —— 事件族

观察者钩子按生命周期边界分成六族：**会话**（会话的开始、结束、定稿与重置）、**回合级 LLM**（一个用户回合的进入与收尾）、**请求级 API**（agent 循环内部每一次 provider 尝试）、**工具**（单次工具调用的一生）、**审批**（危险命令审批提示的前后）、**子代理**（被委派的子 agent 的创建与回收）。下面逐族给出钩子名、触发时机与字段。

### Session Lifecycle —— 会话生命周期

会话钩子描述会话的边界与重置：

| 钩子 | 何时触发 |
| --- | --- |
| `on_session_start` | 一个**全新**会话在系统提示词构建完成之后开始。 |
| `on_session_end` | 一次 `run_conversation` 调用结束，包括被中断或未完成的回合。 |
| `on_session_finalize` | CLI 或网关拆掉一个活跃的会话身份。 |
| `on_session_reset` | CLI 或网关从一个旧会话身份迁移到一个新会话身份。 |

常见字段包括 `session_id`、`completed`、`interrupted`、`reason`，以及（如可用）`old_session_id` 与 `new_session_id`。

`on_session_end` 是**回合/单次运行**级别的。它不一定是一个聊天身份的最终生命周期边界。凡是要"每个会话身份只做一次"的生命周期清理，请用 `on_session_finalize` 与 `on_session_reset`。

这一节的关键取舍是**别把 `on_session_end` 当会话终点**：`run_conversation` 是每条消息跑一次，所以它每回复一次就响一次；而真正的会话身份清理（落盘、关连接、释放租约）只应在 CLI 退出、`/new`、`/reset` 或网关拆会话时做一次。把两者塞进同一个钩子，要么让清理过度频繁，要么在真实边界上被漏掉 —— 契约因此把它们拆成两个动词。

### Turn-Scoped LLM Hooks —— 回合级 LLM 钩子

这些钩子框住的是**用户回合**，而不是单次 provider API 尝试：

| 钩子 | 何时触发 |
| --- | --- |
| `pre_llm_call` | 在某个用户回合的工具循环开始之前。 |
| `post_llm_call` | 在该回合以最终助手输出结束之后。 |

`pre_llm_call` 的常见字段包括 `session_id`、`turn_id`、`user_message`、`conversation_history`、`is_first_turn`、`model`、`platform` 与 `sender_id`。

`post_llm_call` 的常见字段包括 `session_id`、`turn_id`、`user_message`、`assistant_response`、`conversation_history`、`model` 与 `platform`。

LLM 跨度（span）遥测请用请求级的 API 钩子。`pre_llm_call` 与 `post_llm_call` 则用于回合级上下文、兼容性与最终回合摘要。

把"回合"与"每次 provider 尝试"分开，是因为一次用户回合里常常有多次 API 调用（工具循环、重试、fallback）。要按请求计价、按请求建跨度，就用请求级 API 钩子；要的是"这一回合用户问什么、最终答什么"，就用回合级钩子。合成一个钩子，只会让两边的消费者各自再切一刀。

### Request-Scoped API Hooks —— 请求级 API 钩子

API 钩子描述 agent 循环内部的 provider 尝试：

| 钩子 | 何时触发 |
| --- | --- |
| `pre_api_request` | 紧邻一次 provider API 请求之前。 |
| `post_api_request` | 一次成功的 provider 响应之后。 |
| `api_request_error` | 一次失败的 provider 请求或可重试的错误路径之后。 |

`pre_api_request` 包含：

- 身份：`session_id`、`task_id`、`turn_id`、`api_request_id`
- 运行时：`platform`、`model`、`provider`、`base_url`、`api_mode`
- 尝试元数据：`api_call_count`、`message_count`、`tool_count`、`approx_input_tokens`、`request_char_count`、`max_tokens`
- 时序：`started_at`
- 脱敏后的请求载荷：`request`

`post_api_request` 包含同样的身份 / 运行时字段，外加：

- `api_duration`、`started_at`、`ended_at`
- `finish_reason`、`message_count`、`response_model`
- `usage`
- `assistant_content_chars`、`assistant_tool_call_count`
- 脱敏后的响应载荷：`response`
- 兼容对象：`assistant_message`

`api_request_error` 包含同样的身份 / 运行时字段，外加：

- `api_duration`、`started_at`、`ended_at`
- `status_code`、`retry_count`、`max_retries`、`retryable`、`reason`
- 结构化的 `error = {"type": ..., "message": ...}`
- 脱敏后的失败请求载荷：`request`

脱敏后的 `request`、`response` 与 `error` 字段是给新消费者用的**规范**观察者输入。

三个钩子覆盖了一次尝试的一生：请求前、成功后、失败后。把 `api_request_error` 单独拉出来，是为了让失败也有一条**规范**的上报路径，而不是让消费者从 `post_api_request` 的"没出现"里反推失败 —— 那样既丢掉了 `status_code` / `retryable` / `reason`，也会把"根本没发出去"和"发出去了但挂了"混成一类。

### Tool Lifecycle —— 工具生命周期

工具钩子描述单个工具调用：

| 钩子 | 何时触发 |
| --- | --- |
| `pre_tool_call` | 在通过护栏（guardrail）批准的工具分发之前。 |
| `post_tool_call` | 在工具分发之后 —— 完成、取消、阻断或出错都算。 |
| `transform_tool_result` | 在 `post_tool_call` 之后、结果被追加进模型上下文之前。 |

`pre_tool_call` 包含 `tool_name`、`args`、`task_id`、`session_id`、`tool_call_id`、`turn_id` 与 `api_request_id`。

`post_tool_call` 包含同样的身份字段，外加 `result`、`duration_ms`、`status`、`error_type` 与 `error_message`。

`status` 是**观察者级**的生命周期结局。常见取值包括：

| 状态 | 含义 |
| --- | --- |
| `ok` | 工具正常完成。 |
| `error` | 工具跑了，并返回或抛出了一个错误结局。 |
| `blocked` | 某个 `pre_tool_call` 钩子阻断了执行。 |
| `cancelled` | 执行在正常完成之前被取消。 |

`post_tool_call` 在 blocked 与 cancelled 路径上**也会**发出，这样遥测插件才能干净地闭合跨度（span）。

`status` 被刻意做成一个**封闭枚举**而不是自由文本：观察者的仪表盘要按状态分组，就必须有一个有限集合。同时，`blocked` 与 `cancelled` 既被纳入枚举、又要在这些路径上补发 `post_tool_call` —— 正是为了让"开始了却没正常结束"的调用也能被闭合，否则跨度会一直悬着。

### Approval Lifecycle —— 审批生命周期

审批钩子描述危险命令的审批提示：

| 钩子 | 何时触发 |
| --- | --- |
| `pre_approval_request` | 在审批请求被展示或发送之前。 |
| `post_approval_response` | 在用户作答之后，或该请求超时之后。 |

常见字段包括 `command`、`description`、`pattern_key`、`pattern_keys`、`session_key` 与 `surface`。

`post_approval_response` 还包含 `choice`，取值例如 `once`、`session`、`always`、`deny`、`timeout` 与 `cancelled`（没人作答：提示被撤下 —— 回合被中断或结束 —— 或者在 CLI 上根本没送达用户）。

审批钩子是**纯观察**的。插件无法从这些钩子预先作答或否决审批。要让一个工具压根到不了审批这一步，请用 `pre_tool_call` 阻断。

审批处于安全关键路径上，所以这组钩子被明确限定为**只读观察**：插件不能投票、不能预先作答、不能否决。真要拦一个工具，用 `pre_tool_call`。这条边界让审批 UX 可以独立演进，也堵掉了"一个观测插件静默放行了一条危险命令"这类失效。

### Subagent Lifecycle —— 子代理生命周期

子代理钩子描述被委派的子 agent 工作：

| 钩子 | 何时触发 |
| --- | --- |
| `subagent_start` | 一个被委派的子 agent 被创建。 |
| `subagent_stop` | 一个被委派的子 agent 返回或失败。 |

`subagent_start` 的字段包括 `parent_session_id`、`parent_turn_id`、`parent_subagent_id`、`child_session_id`、`child_subagent_id`、`child_role` 与 `child_goal`。

`subagent_stop` 的字段包括父 / 子会话 id、角色 / 状态字段、`child_summary`、`duration_ms`，以及一份**只含元数据**的 `tool_call_history`。历史里每一条包含：工具名、参数**名**、有界的副作用目标、输入 / 输出字节数，以及结局。URL 的 query 串与 fragment 会被移除；原始参数、提示词、命令、内容、请求头与结果被**刻意排除**。

观察者可以用这些钩子建模嵌套轨迹，同时让子 agent 的执行**始终挂在派生出它的那个父回合上**。

子代理钩子要同时满足两个目标：让观察者能建模**嵌套**轨迹，又不把子 agent 的原始工作内容（提示词、参数、命令、结果）泄进遥测。`tool_call_history` 因此只保留"工具名 + 参数名 + 有界的副作用目标 + 字节数 + 结局"这种元数据，并把 URL 的 query 与 fragment 剥掉 —— 足以重建调用图与代价，不足以重建内容。

---

## 4. Payload Safety —— 载荷安全

观察者载荷是为遥测消费者设计的，不是用来直接摸原始对象的。新消费者应当使用脱敏后的 API 载荷：

- `pre_api_request.request`
- `post_api_request.response`
- `api_request_error.request`
- `api_request_error.error`

脱敏会把 provider 对象转成 JSON 兼容结构、给大载荷**设上限**、对敏感键做**脱敏（redact）**，并避免在脱敏字段里暴露原始响应对象。

像 `request_messages`、`conversation_history` 与 `assistant_message` 这类历史兼容字段，对已有插件可能**仍然存在**。新的可观测性消费者应当优先使用脱敏载荷。

这些脱敏字段之所以存在，是因为观察者迟早要处理 provider SDK 的原生对象：它们带 client、callback、file handle 这类不可 JSON 的成员，还可能夹着 `Authorization` 头。与其让每个消费者各自重写一遍"能不能序列化 + 要不要脱敏 + 太大怎么办"，契约端一次性把它做掉：转 JSON、设上限、脱敏敏感键。代价是消费者拿到的是**派生**结构而非原始对象 —— 想要原始对象的老插件只能继续依赖 `request_messages` / `conversation_history` / `assistant_message` 这类历史字段，而那些字段恰恰是新消费者被劝退不要用的。

---

## 5. Performance —— 性能

默认的、未被插桩的路径应当保持廉价。昂贵的请求 / 响应载荷构造被 `has_hook(...)` 挡在门后 —— 只有至少一个插件注册了相关钩子时，Hermes 才会去构造脱敏后的 API 遥测载荷。

插件作者应当保持这个性质：

- 只注册插件**真的会消费**的那些钩子。
- 避免对已经脱敏过的载荷做深拷贝或二次脱敏。
- 让钩子回调保持快、且失败放开。
- 在可行时把网络导出或批量写盘**挪到旁路**。

"先问 `has_hook` 再构造"是这条性能契约的核心：脱敏载荷的构造要遍历整个请求体（含全部消息与工具 schema），而它在一轮对话里会被调用很多次，所以必须是**按需**的。砍掉这层门控，等于让"一个观察者插件都没装"的用户也付构造脱敏载荷的钱。

---

## 6. Writing An Observer Plugin —— 写一个观察者插件

最小的观察者插件：

```python
def register(ctx):
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)


def on_pre_api_request(**kwargs):
    start_llm_span(
        request_id=kwargs.get("api_request_id"),
        turn_id=kwargs.get("turn_id"),
        request=kwargs.get("request"),
        model=kwargs.get("model"),
    )


def on_post_api_request(**kwargs):
    finish_llm_span(
        request_id=kwargs.get("api_request_id"),
        response=kwargs.get("response"),
        usage=kwargs.get("usage"),
        duration=kwargs.get("api_duration"),
    )


def on_pre_tool_call(**kwargs):
    start_tool_span(
        call_id=kwargs.get("tool_call_id"),
        name=kwargs.get("tool_name"),
        args=kwargs.get("args"),
    )


def on_post_tool_call(**kwargs):
    finish_tool_span(
        call_id=kwargs.get("tool_call_id"),
        result=kwargs.get("result"),
        status=kwargs.get("status"),
        duration_ms=kwargs.get("duration_ms"),
    )
```

用 `session_id`、`turn_id`、`api_request_id` 与 `tool_call_id` 做跨度关联。当导出格式支持嵌套 agent 工作或安全生命周期事件时，用子代理钩子与审批钩子。

这个最小插件的形状说明了契约的分工：注册四个钩子（两个 API、两个工具），用 `api_request_id` / `tool_call_id` 把"开始"和"结束"配成一对跨度。只看回合与计价用 API 钩子就够；需要嵌套 agent 工作或安全生命周期，再补子代理与审批钩子。

---

## 7. Existing Consumers —— 现有消费者

随包内置的 Langfuse 插件演示了如何用钩子直接观察回合、provider 请求与工具调用。

原生的 NeMo Relay SDK 集成把 Hermes 的会话、回合、LLM 与工具生命周期映射到 Relay。显式的 Relay 插件配置可以添加 [ATOF、ATIF 或 OTEL](https://docs.nvidia.com/nemo/relay/configure-plugins/observability/about) 导出器与执行中间件；见 [Relay 共享指标](relay-shared-metrics.md)。

两个现有消费者代表了两种接法：Langfuse 插件走通用钩子面（因此能演示整条契约），NeMo Relay 走第一方共享指标路径（因此连插件都不用开）。这也正解释了契约为什么强调"与后端无关" —— 同一组生命周期边界，既要能喂第三方 SaaS，也要能喂厂商自家的指标。

---

## 附录 A · 译名对照

| 术语 | 译名 | 首次出现节 |
|---|---|---|
| observer hook | 观察者钩子 | 标题 |
| observer | 观察者 | §0 |
| telemetry contract | 遥测契约 | §0 |
| read-only | 只读 | §0 |
| backend-neutral | 与后端无关 | §0 |
| hook | 钩子 | §0 |
| callback | 回调 | §1 |
| keyword arguments | 关键字参数 | §1 |
| additive fields | 增量新增的字段 | §1 |
| fail-open | 失败放开（fail-open） | §1 |
| behavior-affecting | 会改变行为 | §1 |
| ephemeral context | 临时上下文 | §1 |
| guardrail | 护栏（guardrail） | §3.4 |
| correlation id | 关联 id | §0 |
| opaque | 不透明 | §2 |
| turn | 回合 | §2 |
| subagent | 子代理 | §2 |
| delegated | 被委派的 | §2 |
| payload | 载荷 | §0 |
| redact | 脱敏（redact） | §4 |
| span | 跨度（span） | §3.2 |
| truncate | 截断 | §4（"设上限"处对应 `bounds`） |
| bounds | 给…设上限 | §4 |
| sanitized | 脱敏后的 | §3.3 |
| canonical | 规范（canonical） | §3.3 |
| compatibility object | 兼容对象 | §3.3 |
| attempt | 尝试（provider 尝试） | §3.3 |
| retryable | 可重试的 | §3.3 |
| guardrail-approved | 通过护栏批准 | §3.4 |
| dispatch | 分发 | §3.4 |
| status | 状态 | §3.4 |
| lifecycle outcome | 生命周期结局 | §3.4 |
| approval | 审批 | §3.5 |
| pattern_key / pattern_keys | `pattern_key` / `pattern_keys`（原样，不译） | §3.5 |
| surface | `surface`（原样，不译） | §3.5 |
| veto | 否决 | §3.5 |
| pre-answer | 预先作答 | §3.5 |
| metadata-only | 只含元数据 | §3.6 |
| bounded side-effect targets | 有界的副作用目标 | §3.6 |
| query string / fragment | query 串 / fragment | §3.6 |
| nested trajectories | 嵌套轨迹 | §3.6 |
| uninstrumented path | 未被插桩的路径 | §5 |
| offload | 挪到旁路 | §5 |
| deep-copy | 深拷贝 | §5 |
| middleware | 中间件 | §0 |
| existing consumer | 现有消费者 | §7 |
| Relay shared metrics | Relay 共享指标 | §7 |
| exporter | 导出器 | §7 |

---

## 附录 B · 本篇提到的文件与实测行数

行数一律 `git -C $HOME/.hermes/hermes-agent show f5d1926110:<路径> | wc -l` 实测；文档侧行号取自原文（`wc -l` = **331 行**）。**实测锚 `f5d1926110`。**

**B-1 · 原文结构实测（段 · 行号 · 行数 ↔ 本篇节，行号均为实测）**

| 段 | 原文行号 | 行数 | 本篇节 | 备注 |
|---|---|---|---|---|
| YAML frontmatter（`title` / `description`） | `1–4` | 4 | —（不进正文） | 信息并入引文块与 §0 |
| H1 | `6` | 1 | 标题 | |
| 前言（4 段） | `8–24` | 17 | §0 导语 | 前言四段并入导语，不单列标题 |
| `## Contract` | `26–68` | 43 | §1 契约 | 3 个代码块 + 1 张返回行为表 |
| `## Correlation IDs` | `70–88` | 19 | §2 关联 id | 1 张字段表 |
| `## Event Families` | `90–237` | 148 | §3 事件族 | 下辖 6 个 H3 |
| ├ `### Session Lifecycle` | `92–108` | 17 | §3.1 会话生命周期 | |
| ├ `### Turn-Scoped LLM Hooks` | `110–128` | 19 | §3.2 回合级 LLM 钩子 | |
| ├ `### Request-Scoped API Hooks` | `130–166` | 37 | §3.3 请求级 API 钩子 | |
| ├ `### Tool Lifecycle` | `168–194` | 27 | §3.4 工具生命周期 | 1 张状态表 |
| ├ `### Approval Lifecycle` | `196–214` | 19 | §3.5 审批生命周期 | |
| └ `### Subagent Lifecycle` | `216–237` | 22 | §3.6 子代理生命周期 | |
| `## Payload Safety` | `239–255` | 17 | §4 载荷安全 | |
| `## Performance` | `257–269` | 13 | §5 性能 | |
| `## Writing An Observer Plugin` | `271–320` | 50 | §6 写一个观察者插件 | 1 个代码块 |
| `## Existing Consumers` | `322–331` | 10 | §7 现有消费者 | |

**B-2 · 与本章各节符号对应的宿主源码文件（原文按符号 / 命令点名，落到锚点上的所在）**

原文**没有按路径点名任何源码文件**（只点名钩子名、`ctx.register_hook`、`has_hook(...)`、`telemetry_schema_version` 与两个文档链接）。下表是各符号在锚点上的**真实落点**：
| 落点文件 | 实测 | 角色 |
|---|---|---|
| `hermes_cli/plugins.py` | **2147 行** | 钩子名总表 `VALID_HOOKS`（`:108–199`，**39 个**钩子名）；§1 注册入口 `def register_hook`（`:916`） |
| `hermes_cli/plugins_dispatch.py` | **530 行** | 派发核 `def invoke_hook`（`:188`）；`telemetry_schema_version` 注入（`:201`）；失败放开处置 `_report_hook_failure`（`:222`）与 `logger.warning`（`:242`）；超时白名单（`:41`）、fail-closed 集（`:48`）、调用线程集（`:50`）、抑制窗口 `60.0`（`:52`） |
| `hermes_cli/middleware.py` | **239 行** | `OBSERVER_SCHEMA_VERSION = "hermes.observer.v1"`（`:16`）；`MIDDLEWARE_SCHEMA_VERSION`（`:17`）；观察者 / 中间件载荷注入（`:40` / `:45`） |
| `hermes_cli/lifecycle.py` | **63 行** | 生命周期门面：`invoke_hook`（`:27`）、`has_hook`（`:32`）、`finalize_session`（`:46`） |
| `hermes_cli/hooks.py` | **335 行** | shell-hooks 的 CLI（`list/test/revoke/doctor`）；**不是**钩子名总表所在（见附录 C-3） |
| `hermes_cli/observability/__init__.py` | **25 行** | `observe_lifecycle`（`:11`）/ `handles_hook`（`:21`） |
| `hermes_cli/observability/relay_shared_metrics.py` | **1091 行** | §7 NeMo Relay 共享指标：`_HOOK_HANDLERS`（`:935`）、`HANDLED_HOOKS`（`:950`） |
| `hermes_cli/cli_session_mixin.py` | **1171 行** | §3.1 会话边界派发 `_notify_session_boundary`（`:429`）与其载荷（`:434–437`） |
| `gateway/slash_commands_session.py` | **1045 行** | gateway 侧 `on_session_reset`，多传 `old_session_id` / `new_session_id`（`:210–213`） |
| `agent/plugin_stream_hooks.py` | **165 行** | 流式观察者路径的 `telemetry_schema_version` 注入（`:66`） |
| `agent/api_request_hooks.py` | **196 行** | §3.3 / §4 载荷脱敏与 `api_request_error` 派发：`_SENSITIVE_HOOK_KEYS`（`:15`）、`_hook_payload_max_chars`（`:52`）、`_hook_jsonable`（`:67`）、`_sanitize_hook_payload`（`:118`）、`_invoke_api_request_error_hook`（`:161`） |
| `agent/turn_api_request.py` | **181 行** | §3.3 `pre_api_request` 发出点（`:52` 门控 / `:60–80` 载荷） |
| `agent/turn_response_intake.py` | **183 行** | §3.3 `post_api_request` 发出点（`:63` 门控 / `:64–87` 载荷） |
| `agent/turn_context.py` | **1182 行** | §3.2 `pre_llm_call` 发出点（`:666` / `:678`） |
| `agent/turn_finalizer.py` | **674 行** | §3.2 `transform_llm_output`（`:420`）与 `post_llm_call`（`:433`）；§3.1 `on_session_end`（`:660`） |
| `agent/conversation_loop.py` | **1683 行** | §3.1 `on_session_start`（`:774`） |
| `model_tools.py` | **987 行** | §3.4 `post_tool_call` 与 `transform_tool_result`：状态推导 `_tool_result_observer_fields`（`:657`）、门控（`:687` / `:852`）、`status="blocked"`（`:938`）、`status="error"`（`:960`） |
| `agent/tool_executor.py` | **1806 行** | §3.4 各路径状态：`blocked`（`:633`）、`cancelled`（`:292` / `:327` / `:912` / `:1401`）、`timeout`（`:920` / `:1397`） |
| `tools/approval.py` | **1336 行** | §3.5 审批钩子发出点（`:897` / `:900`）、`surface="cli"`（`:896`）、`cancelled` 分支（`:908`） |
| `tools/approval_context.py` | **331 行** | `_fire_approval_hook`（`:54`） |
| `tools/approval_prompt.py` | **336 行** | 插件审批传输：`surface=f"transport:{name}"`（`:238`）、两钩子（`:241` / `:249`） |
| `tools/approval_smart.py` | **145 行** | `surface="smart"`（`:135`）、`choice=f"smart_{verdict}"` + `decided_by`（`:144`） |
| `tools/approval_gateway_wait.py` | **206 行** | `choice="cancelled"`（`:94`）、`choice="notify_failed"`（`:192`） |
| `tools/delegate_tool.py` | **763 行** | §3.6 `subagent_start`（`:287–295`） |
| `tools/delegate_tool_results.py` | **410 行** | §3.6 `subagent_stop`（`:365–372`）、`_subagent_stop_tool_call_history`（`:134`）、`_sanitize_tool_target`（`:81`） |
| `plugins/observability/langfuse/__init__.py` | **1026 行** | §7 内置 Langfuse 插件（`def register(ctx)` `:1014`；11 个钩子 `:1017–1025`） |
| `plugins/observability/langfuse/plugin.yaml` | **19 行** | `hooks:` 列表（11 项） |
| `website/docs/developer-guide/observer-hooks.md` | **331 行** | 本章原文 |
| `website/docs/developer-guide/relay-shared-metrics.md` | **487 行** | 前言 / §7 点名的姊妹文档（在场） |

**B-3 · 搬走 / 未删节**

- **frontmatter（`1–4`，4 行）**：不进译文正文；其 `title` / `description` 的信息并入引文块的"定位"一句与 §0。
- **H1 前言四段（`8–24`，17 行）**：并入 §0 导语（**搬走**，不单列标题）。原文前言讲四件事 —— 只读遥测契约的用途与受众（`8–11`）、刻意与后端无关 / 不替代什么（`13–16`）、行为变更型包装器不属于本契约（`18–20`）、第一方 NeMo Relay 路径（`22–24`）。
- **`## Contract` 第一个代码块（`30–36`，7 行）与 §6 最小插件代码块（`275–316`，42 行）**：两者的 `register(ctx)` 四行**逐字重复**（`31–35` 对 `276–280`）。原文如此，译文**两处都保留**（未合并、未删）。
- **代码块总计 4 个（共 55 行）**：`30–36`（7 行）、`41–46`（6 行）、`50–52`（3 行）、`275–316`（42 行）→ **4/4 逐字保留**（英文原文与缩进一字未改，仅周围散文为中文）。
- **外链（`24`、`329`、`331`）**：`relay-shared-metrics.md`（×2）与 NVIDIA Relay observability 文档页（×1）逐字保留为链接，**未展开**。
- **未删节**：原文**没有任何一段被砍掉**；原文各节末尾本就没有小标题的段落，译文同样保持无标题（不新增 `### 源码要点` / `### 为什么这么设计` 之类标题）。

---

## 附录 C · 上游文档与源码的出入

核对锚：上游 commit `f5d1926110`（version = "0.21.3"）。核对键位：钩子名、返回动作、状态值、审批 `choice` 值域、载荷字段名、schema 版本常量、截断阈值、"失败放开"的处置位置。绝大多数条目**逐条命中**，另有 **7 处出入**逐条给出，末尾附 5 处说明（不计入出入）。

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
- **结论**：并集口径可读但**易误用** —— `old_session_id` / `new_session_id` 只在 **gateway** 的 `on_session_reset` 上出现（CLI 路径不传），`reason` 不在 `on_session_end` 上。按字面在 `on_session_end` 里读 `kwargs["reason"]` 会拿到 `None`。

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
