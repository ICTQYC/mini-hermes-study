# 中文精译 · 第 5 章：上下文压缩与缓存

> **原文**：`website/docs/developer-guide/context-compression-and-caching.md`（645 行）
> **本篇定位**：翻译为主，目标是**看懂原理**。差异对比、源码深挖、实现日志都不在这里 ——
> 那些属于后面两个阶段（mini 实现 / 源码讲解）。
> 看不懂的地方或想深挖的点，随时问，我再往这里补。

---

## 0. 这一章在回答什么

上下文窗口是**有限**的，而对话是**无限**长的。Hermes 用两件事来对付这个矛盾：

1. **压缩**（compression）—— 窗口快满了就把历史**摘要**掉，让对话能一直继续下去。
2. **缓存**（caching）—— 每一轮都重发全部历史，所以要让**没变的那部分字节完全一致**，
   让 provider 命中前缀缓存，否则每轮都按全量重新计费。

一句话记住两者的关系：**压缩是"不得不变"，缓存是"能不变就不变"**。
所以整章的核心张力是：**压缩会打断缓存（它改的就是历史），而缓存要求你尽量少改历史。**

原文点名的源码位置（照抄，便于核对）：

```
agent/context_engine.py        —— ContextEngine 抽象基类（ABC）
agent/context_compressor.py    —— 默认引擎 ContextCompressor
agent/prompt_caching.py        —— 提示词缓存标记
gateway/run_turn.py            —— 网关侧的会话维护（session hygiene；搜 "Session hygiene"）
agent/compression_facade.py    —— 搜 _compress_context
```

---

## 1. 可插拔的上下文引擎（Pluggable Context Engine）

上下文管理建立在一个抽象基类 `ContextEngine`（`agent/context_engine.py`）之上。
内置的 `ContextCompressor` 是默认实现，但**插件可以替换它**（例如某个提供
"无损上下文管理"的插件）。

```yaml
context:
  engine: "compressor"    # 默认 —— 内置的有损摘要
  engine: "lcm"           # 示例 —— 提供无损上下文的插件
```

引擎负责四件事：

- 决定**什么时候该压缩**（`should_compress()`）
- **执行**压缩（`compress()`）
- 可选：**暴露工具**给 agent 调用（例如 `lcm_grep`）
- 从 API 响应里**记账 token 用量**

选择由配置驱动，读的是 `config.yaml` 里的 `context.engine`。解析顺序：

1. 先看 `plugins/context_engine/<名字>/` 目录
2. 再看通用插件系统（`register_context_engine()`）
3. 都没有 → 回退到内置的 `ContextCompressor`

⚠️ **插件引擎永远不会被自动激活** —— 用户必须显式把 `context.engine` 设成插件的名字。
默认值 `"compressor"` 永远指向内置实现。

配置方式：`hermes plugins` → Provider Plugins → Context Engine，或直接改 `config.yaml`。

> **为什么这么设计**：把"上下文管理"做成 ABC + 可替换实现，而不是写死一套摘要逻辑。
> 因为**摘要是有损的、且策略强依赖场景**（代码仓库 vs 长对话 vs 法律文档，"什么该留"完全不同），
> Hermes 没法替所有人决定。砍掉可插拔性会怎样：想换策略就得上游改核心，
> 而这一层的改动风险极高（它直接改历史）。**这是"窄腰"原则在上下文层的应用**：
> 保留一个稳定的接口（`should_compress` / `compress`），把策略交给边缘。

---

## 2. 双压缩系统（Dual Compression System）

Hermes 有**两个各自独立工作的压缩层**：

```
                      ┌──────────────────────────┐
   收到的消息          │  网关侧的会话维护        │  在上下文 85% 时触发
   ─────────────────► │  （进 agent 之前，粗估）  │  大会话的安全网
                      └─────────────┬────────────┘
                                    │
                                    ▼
                      ┌──────────────────────────┐
                      │   Agent ContextCompressor │  在上下文 50% 时触发（默认）
                      │  （循环内，真实 token 数） │  常规上下文管理
                      └──────────────────────────┘
```

### 2.1 网关侧的会话维护（session hygiene，85% 阈值）

（原文 `### 1. Gateway Session Hygiene (85% threshold)`）

> **译名说明**：hygiene 在这里**不是「清洁」**，而是「**回合之外的例行体检 + 兜底**」。
> 本仓库统一译作**「网关侧的会话维护（session hygiene）」**；讲机制时也叫
> **「进 agent 之前的那道兜底压缩」**。

位置：`gateway/run_turn.py`（搜 `Session hygiene`）。它是一张**安全网**，
在 agent 处理消息**之前**运行，防止会话在两轮之间胀得太大导致 API 直接报错
（典型场景：Telegram/Discord 里隔夜堆积的消息）。

- **阈值**：固定为模型上下文长度的 **85%**
- **token 从哪来**：优先用**上一轮 API 真实报出的** token 数；其次是会话行上持久化的
  "用量锚点"（真实计数 + 此后追加内容的增量估算；**网关重启也能活下来**）；
  最后才用按字符数的粗估（`estimate_messages_tokens_rough`）
- **触发条件**：`len(history) >= 4` **且**压缩功能开启
- **目的**：兜住那些**逃过了 agent 自己的压缩器**的会话

> 网关的阈值**故意**比 agent 的压缩器高。如果也设成 50%（和 agent 一样），
> 长网关会话会**每一轮都被提前压缩**。
>
> **为什么这么设计**：两层不是重复，是**两个不同位置的最优解**。
> agent 循环里有**真实的 API token 数**，所以它能精确、也能早一点动手（50%）；
> 网关在 agent 之前跑，**拿不到本轮的真实用量**（只能靠上一轮 + 估算），
> 估算会不准 → 那就只让它做**最后一道保险**（85%），宁可晚也不能误触发。
> **同一个策略（压缩）放在两个位置，判据就应该是"这里的信息质量"**。
> 砍掉网关这层会怎样：在 Telegram 里挂一晚上、第二天早上发一句话，
> 请求会带着一夜堆积的历史直接超窗被 provider 拒绝 —— agent 还没机会开口就失败了。

### 2.2 Agent 的 ContextCompressor（50% 阈值，可配置）

（原文 `### 2. Agent ContextCompressor (50% threshold, configurable)`）

位置：`agent/context_compressor.py`。这是**主压缩系统**，跑在 agent 的工具循环里，
能拿到准确、由 API 报出的 token 数。

#### 2.2.1 token 记账：先问 provider 的真实账单，退一步才用估算

（原文 `#### Token accounting: provider anchors and explicit heuristic fallbacks`）

**这一节回答一个问题**：「要不要压缩」这个判断，用的 token 数**是从哪来的**？
答案分两层：**有 provider 报过的真实账单就用真实的；没有才用本地粗估** ——
而且粗估的误差方向是**被刻意设计过的**（见本节最后的"为什么"）。

先统一术语（后面全章都用这套）：

| 术语 | 原文 | 一句话意思 |
|---|---|---|
| **用量锚点** | usage anchor | provider **上次亲口报的** prompt/completion token 数 **+ 只对"那之后追加的消息"**做的粗估 |
| **闸门** | gate | 检查"要不要压缩"的**时机点**。本章共 4 处：回合开始的预检、空闲压缩、发请求前的检查、工具调用之后 |
| **门闩** | latch | 压缩后设下的**一次性标记**：等下一次响应回来才解除（**即使那次响应没带 usage 也算解除**） |
| **保命兜底** | liveness fallback | 拿不到真实计数时用的估算。看着"不精确"，但**关掉它系统会卡死**，所以保留 |
| **被拒后补救** | reactive recovery | provider 明确报"上下文超长"**之后**才做的压缩 |
| **夹到区间** | clamp | 把一个值限制在 [下限, 上限] 之间 |

**① 账怎么记（用量锚点）**

**每一个压缩闸门**都**先问"用量锚点"**（`agent/usage_anchor.py`）。
锚点 = provider 上次报的 prompt/completion token 数 **+ 只对"自那次响应之后追加的消息"做粗略估算**。

锚点用**内容指纹**来认它计价的那份 transcript，所以：

- 网关每轮都从数据库**重新读一遍历史**也不影响它（内容一样 → 指纹一样）
- 它被**持久化在会话行上**，所以新进程（`--resume`、桌面端每轮起一个 `serve`）
  能恢复它 —— 只要那份持久化 transcript 仍然对得上

**会被清掉锚点的三件事**：压缩、会话重置、codex 原生压缩。

**② 没有锚点时：宁可多发一轮请求，也不先压**

**"等一个请求"具体是什么**：没有锚点时（第一次请求、用户回退/编辑后重发），
**粗估**可能已经超阈值 —— 但系统**不立刻压缩**，而是照常把这一轮发出去，
用 provider 报回的**真实** token 数再判断（`should_defer_preflight_to_real_usage`）。

连"粗估已经超过整个窗口"也不例外。理由是**代价不对称**：
多发一轮只花一次请求；误压一次要丢信息 **+ 把提示词缓存整段作废**。
而"粗估"本身就可能是错的 —— **估得大，不能证明这个请求一定会被拒**。

换模型同理：旧的用量会被清掉，第一个请求同样交给新 provider 裁决 ——
代价是一个**确实过大**的请求可能**先被拒一次**，然后才走"被拒后补救"。

⚠️ **"等"不等于"关掉"**：一旦某个响应**没带 usage**，估算兜底立刻回来接管。
真实的用量已经超阈值、以及 provider **确实证实**了溢出，都照样允许压缩。
压缩后那个**门闩**只等一次响应，而且**即使那次响应没带 usage 也会被消费掉**。
整个过程仍然受压缩尝试预算和"无进展守卫"约束，**不会变成无限重发循环**。

**③ 哪些角落还留着估算（保命兜底）**

下面这些**不**跟着上面那条"等一个请求"的规则走 —— 它们是**故意保留**的估算：

- 锚点里的**追加增量**是粗估的（第一条追加的 assistant 消息已被 completion 用量覆盖）。
  所以**一条很大的新工具结果**仍可能靠估算的增量越过阈值。
  另外注意指纹的**范围**：它**不**对整个前缀、模型、工具、系统提示词做指纹。
- 可选的**空闲压缩**有自己的下限/冷却，可以作用于**没有锚点**的会话；
  它**不共享**上面那条"等一个请求"的规则。
- **进 agent 之前的那道兜底压缩（session hygiene）**保留自己的粗估回退和"硬消息数安全阀"。
  ⚠️ 回放测试框架里的 `gateway` 形态只是**重载 transcript 字典**，
  **并不演练**那条独立的维护策略（别拿它当"网关那条路也测过了"的证据）。
- **工具调用之后**的"无 usage 回退"、微压缩、摘要/尾部尺寸计算、剪枝和溢出进展检查
  仍然使用本地估算。原生压缩保留它 provider 专属的归属权和检查点门闩。

**④ 边角料（知道有这回事就够）**

- **"直接向 provider 问精确计数"的接口，目前还没做**（原文：provider count endpoints
  remain deferred）。要消掉剩下的估算，需要一个**显式的策略决定**：要么接受这些记录在案的
  保命兜底，要么用 provider 的证据替换它们 —— 同时还要为"永远不返回 usage 的 provider"
  定义行为。**简单地把"所有无锚点的维护动作"关掉并不等价**（那会让系统卡死）。
- `evals/token_accounting/replay_gates.py` 覆盖这些场景：窗口内/超窗口的**估高**、
  真实超阈值的**对照组**、重载/恢复锚点、以及本地 HTTP 上的溢出/无 usage 恢复
  （用真实压缩、但摘要文本固定）。**这些是脚本化的控制流检查，不是厂商分词器或计费证据。**
- **不透明的 provider blob**（Codex 推理/压缩项上的 `encrypted_content`）
  在**所有本地估算里都按 0 计**；只有真实 usage 才会给它计价 —— 数不出来就承认数不出来。
- **图片**按"**从 provider 的 usage 里反推出来的**每图单价"计价（`agent/image_token_cost.py`），
  **不是**按厂商公式：某个响应相对上一个锚点的增量里引入了 N 张图时，
  真实 `prompt_tokens` 与"纯文本投影"之间的**残差**就是 N × 那个单价。
  它按 `model@host` 存进 `~/.hermes/cache/image_token_costs.json`，
  并在**每轮内绑定一次**，让触发估算器、尾部预算扫描和网关侧的会话维护**用的是同一个数字**。
  第一次视觉回合之前用一个固定默认值 **1,500**。

> 出处（维护者视角）：本节不改变 #104462 那条"绝不估算"字面要求的适用范围；
> #100661 是"冷却把会话卡死"的那一次。

> **为什么这么设计**：这是全章最"工程"的一节，但理由只有一句 ——
> **本地估算不可靠，而误判的代价是不对称的。**
>
> 估算偏大 → 你**白白压缩**（损失信息 + 打断缓存）；估算偏小 → 请求被 provider 拒绝
> （但这是**可补救**的：走"被拒后补救"）。所以策略是"**有真实数据就用真实数据，
> 并且愿意为拿到真实数据多等一轮**"。
>
> 三个具体体现：
> ① **锚点用内容指纹而不是行数** —— 因为网关每轮重读历史，行号/对象身份都不稳定；
> ② **图片单价从 usage 反推而不是查公式** —— 各家的图片计价方式不同且会变，
> "从真实账单反推"是唯一不会过期的做法；
> ③ **不透明 blob 按 0 计** —— 你数不出来它，那就承认数不出来，
> 只让真实 usage 给它定价（假装估一个数只会让估算更错）。

#### 2.2.2 失败冷却：什么时候退避，以及三种"证据变了"的例外

（原文 `#### Failure cooldown and provider-proven overflow`）

一次**失败或卡住**的摘要尝试会为这个会话装上**失败冷却**：

- 阶梯式递增：**60s → 300s → 900s**
- **永远不会短于** `compression.context_timeout_seconds`
- 持久化在 `state.db` 里（进程重启也还在）

冷却装着的期间，普通的"到阈值就压缩"会被**推迟** ——
这样**一个坏掉的摘要后端不会每轮都被重试**。

但有三条路径**绕过冷却、真的去试一次**（三条都是"**证据变了**"）：

1. **手动 `/compress`**（`force=True`）—— 清掉冷却并重试。
   证据 = **用户明确要求**（最强的信号）。
2. **同回合的 `fallback_chain` 重试**（主路由卡住之后的备用路由）——
   被取消的主路由自己装的那个"卡顿冷却"**不能顺带压住这条重试**（`bypass_cooldown`）。
   证据 = **换了条路走**，不是同一条路在重试。
3. **provider 证实的溢出** —— provider 自己用"上下文超长"错误拒绝了请求时，
   恢复流程**无视冷却**做一次**有界**尝试（`max_compression_attempts`），
   而且**不清掉**冷却。证据 = **provider 主动告诉你现在就是溢出** ——
   这是最强的客观证据（"不压缩就一定失败"）。

第 3 条的理由很直白：**在这里推迟会把会话卡死** ——
每一轮都会被 provider 顶回来，而下一次失败又会把冷却阶梯往上抬。
如果那次尝试也失败，冷却照常记录。

> **为什么这么设计**：冷却存在的目的是"**别在每轮都重试一个已经坏了的东西**"，
> 而它必须能被"**证据变了**"这个事件打断。三条绕过的路径恰好是三种"证据变了"。
>
> 📌 **可迁移的判据**：给某种失败加"退避"时，要同时给出**它的解除条件**。
> 只有"什么时候别做"，没有"什么情况下必须做"，那个退避就会在真正的紧急状态下
> 变成故障本身（这里就是把会话永久卡住）。

---

<!-- 下一段：§Configuration 及其子节 → §Computed Values -->

## 3. 配置（Configuration）

所有压缩设置都从 `config.yaml` 的 `compression` 键下读：

```yaml
compression:
  enabled: true              # 开关压缩（默认 true）
  threshold: 0.50            # 占上下文窗口的比例（默认 0.50 = 50%）
  # model_thresholds:        # 按模型覆盖阈值（子串匹配，最长的键胜出）
  #   "glm-5.2": 0.40        # 见下面「按模型覆盖阈值」一节
  #   "claude-sonnet": 0.35
  target_ratio: 0.20         # 阈值里留多少当作尾部预算（默认 0.20）
  tail_mode: lean            # 尾部保留策略：lean | legacy（默认 lean）
  protect_last_n: 20         # 至少保护的最近消息数（默认 20）
  min_tail_user_messages: 1  # 尾部保证保留的「真实用户消息」数（默认 1）
  codex_gpt55_autoraise: true        # Codex OAuth 上的 gpt-5.5：触发点抬到 85%（默认 true）
  codex_gpt55_autoraise_notice: true # 显示那一次性提示（默认 true）
  codex_app_server_auto: native      # Codex app-server 线程压缩：native|hermes|off
  codex_responses_native: false      # gpt-5.6 直连 OpenAI/Codex 时用服务端压缩（可选）
  codex_responses_compact_threshold: null  # 服务端自动压缩的触发点
  in_place: true             # 在同一个会话 id 上压缩，不做轮转（默认 true）

# 摘要用的模型/供应商配在 auxiliary 下：
auxiliary:
  compression:
    model: null              # 覆盖摘要用的模型（默认自动探测）
    provider: auto           # 供应商："auto" / "openrouter" / "nous" / "main" 等
    base_url: null           # 自定义 OpenAI 兼容端点
```

### 3.1 参数详解

（原文 `### Parameter Details`）

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `threshold` | `0.50` | 0.0-1.0 | 当 prompt token ≥ `threshold × context_length` 时触发压缩 |
| `model_thresholds` | `{}` | 映射 | 按模型覆盖 `threshold`。键对**模型名做子串匹配**（最长匹配胜出）；`"<供应商>:<子串>"` 形式的键**只在该供应商上生效**。小上下文的**下限仍然叠加在上**（见下） |
| `target_ratio` | `0.20` | 0.10-0.80 | 控制尾部保护预算：`threshold_tokens × target_ratio`（**只在 legacy 模式用** —— `lean` 有自己的**夹到区间**规则（clamp）） |
| `tail_mode` | `lean` | `lean`/`legacy` | 尾部保留策略。`legacy` 保留 `target_ratio` 大小的**逐字**尾部（大窗口模型上 100K+ token）。`lean` 只保留**夹到区间后**（clamp：限制在上下限之间）的 `2.5% × 上下文窗口`（下限 10K、上限 25K），把连续性改由**摘要**承担：一份保留标识符的详细会话日志（**同一次摘要请求顺带产出** —— lean 每次尝试只发一次辅助 LLM 调用）、一份机制化抽取的锚点索引（PR 号、SHA、路径、错误串 —— **用正则抽，绝不转述**）、**每条真实用户消息逐字引用**（按"最新的优先"分配预算），以及一个 `session_search` 找回指针，让 agent 能重新拿到被摘掉的内容。超长的区域会被**均匀采样**进摘要输入（带显式的省略标记），而不是触发额外调用。在真实的 500K token 会话上：保留约 **49K** 而非 162K，配合找回指针后**召回率更高**（见 `evals/compaction/results/`）。lean 尾部里的旧工具结果会被降级成**一行短文 + 找回指针** |
| `protect_last_n` | `20` | ≥1 | 永远保留的最近消息数（下限） |
| `min_tail_user_messages` | `1` | ≥1 | 保证在**未压缩尾部**里存活的**真实**（可行动）用户消息数。`1` = 现有的"最后一条用户消息锚点"（保持原行为的默认值）。调到 `3` 就表示：即使尾部 token 预算被笨重的工具输出占满，**最后 3 轮真实用户发言**也要逐字留下。平台回声、压缩交接行、合成的续写行**都不计入** N。这条保证**优先于尾部 token 预算** —— 锚点把切点往回拉时，尾部可以超预算 |
| `protect_first_n` | `3` | （写死） | 系统提示词 + 第一轮对话永远保留 |
| `idle_compact_after_seconds` | `0` | ≥0 秒 | 可选：会话空闲这么多秒后恢复时，**提前**压缩（0 = 关闭）。上下文 ≤ `threshold × target_ratio` 时跳过；遵守冷却/防抖/加锁守卫 |
| `codex_gpt55_autoraise` | `true` | 布尔 | 在 ChatGPT Codex OAuth 路由上，把 gpt-5.4/5.5/5.6 与 gpt-6 Astra 的触发点抬到 **85%**（见下）。设 `false` 保持全局 `threshold` |
| `codex_gpt55_autoraise_notice` | `true` | 布尔 | 显示那个一次性的 Codex gpt-5.5 抬升提示。设 `false` 则保留 85% 抬升但不再提示 |
| `codex_app_server_auto` | `native` | `native`/`hermes`/`off` | Codex app-server 会话的线程压缩模式（见下） |
| `codex_responses_native` | `false` | 布尔 | 在 Responses API 上选择 OpenAI 的**服务端**压缩。只对**直连 OpenAI API 或 ChatGPT Codex 订阅**上的 gpt-5.6 家族生效（见下） |
| `codex_responses_compact_threshold` | `null` | `null` 或正整数 | `null` 表示跟随本地解析出的压缩触发点，再留 **8,192 token** 的安全余量。正整数则是绝对值，只在必要时刻**向下夹**。非法值走自动行为。自动模式在没有可用本地触发点时回退到 `200000` |
| `in_place` | `true` | 布尔 | 在同一个会话 id 上压缩，而不是轮转出新会话（见下） |

### 3.2 原地压缩（单一稳定的会话 id）

（原文 `### In-place compaction (single stable session id)`）

`compression.in_place: true`（默认）时，一次压缩**在同一个会话 id 上重写实时消息列表**：
重建系统提示词、把摘要后的中间段换进去、把压缩前的回合**软归档**在同一个 id 下
（会话存储里 `active=0, compacted=1`）—— 仍然可以被 `session_search` 搜到、仍然可恢复，
**永远不删除**。这里没有 `parent_session_id` 链，也没有 `name #N` 重新编号：
**一段对话终生只用一个持久 id**。

这一项**消掉了一整簇"会话轮转"类 bug**：丢失的 `/goal` 状态、孤儿会话、
跨越边界的搜索断档。

消费方**观察这个模式**，而不是去 diff 会话 id：

- `session:compress` 事件带上 `in_place: true/false` 和 `old_session_id`
  （原地模式下是空串，因为**没有**旧 id）。
- 网关用 agent 那个与轮转无关的 `_last_compaction_in_place` 标志来重新设定 transcript
  的基线，而不是靠"id 变了没有"来判断。

设 `in_place: false` 恢复旧的轮转路径：每次压缩提交一个新会话 id，
用 `parent_session_id` 指向前一个。

> **为什么这么设计**：这一节是全章最值得迁移的一条经验 ——
> **"轮转出新会话"把一个局部动作（压缩）变成了全局身份变化**，
> 于是所有按 id 做的功能都得知道这件事，也就都得各修一遍（`/goal`、搜索、孤儿清理）。
> 改成原地重写后，**id 不再是"这段对话的身份证明"里会变的那部分**，
> 那一整簇 bug 就变成结构上不可能发生。
>
> 注意它的代价被明确给出了：压缩前的回合**没有删除**，而是软归档（可搜可恢复）。
> 也就是说"原地"省下的不是存储，而是**身份稳定性**。
>
> 📌 可迁移判据：**如果你发现某个功能的实现要靠"两个 id 之间的关系"来表达，
> 先问"能不能不动 id"** —— 能的话，问题通常直接消失。

### 3.3 摘要可行性（这份摘要产得出来吗）与尾部保留

（原文 `### Auxiliary feasibility and tail retention`）

一个更小的**辅助压缩模型**可以降低实时压缩触发点，而**不改动**所选的尾部策略。

- `lean` 模式下，**选择预算仍然按主模型的上下文窗口**算：2.5%，夹到 10K–25K token。
  例：主模型 1M、辅助模型 512K 时，**选择预算仍保留 25K**，
  哪怕可行性检查把触发点从 850K 降到了 512K。
- 显式 `legacy` 模式则会**重算** `threshold_tokens × target_ratio`
  （512K × 0.20 = 102,400 token）。

这些都是**尾部选择的预算**，不是对整段压缩后上下文的硬上限：
被保护的消息、边界对齐、摘要、锚点都可能再加 token。

### 3.4 按模型覆盖阈值

（原文 `### Per-model threshold overrides`）

`compression.model_thresholds` 让你**按当前模型**决定在哪个点触发压缩 ——
当你会在上下文窗口差异极大的模型之间切换时很有用
（例：1M 上下文的模型可以晚点压，128K 的该早点压）：

```yaml
compression:
  threshold: 0.50
  model_thresholds:
    "glm-5.2": 0.40
    "glm-5.2-1M": 0.25
    "claude-sonnet": 0.35
    "openai-codex:astra": 0.85   # 只在 Codex OAuth 路由上（272K 上限）
```

解析规则：

- 键对模型名做**子串匹配**，**最长的匹配键胜出**
  （模型 `glm-5.2-1M` 上，`glm-5.2-1M` 胜过 `glm-5.2`）。
- 键可以带**供应商作用域**，写成 `"<供应商>:<子串>"`（如 `"openai-codex:astra": 0.85`）。
  带作用域的键**当且仅当**本会话的供应商就是那条路由时才匹配 ——
  所以同一个 slug 在别处（OpenRouter、Nous、直连 OpenAI）服务时，
  **仍然用全局 `threshold`**。
  排序**只看模型子串**，所以 `"astra-900k"` 对 900K 那个变体仍然胜过 `"openai-codex:astra"`；
  而子串完全相同时，**带作用域的键胜过裸键**。
- 没有键匹配（或映射为空）时用全局 `threshold`。
- **每次 `/model` 切换都会重新解析**；切到没有匹配键的模型 → 回退到全局 `threshold`。
- **小上下文下限仍然叠加在覆盖之上**（只能抬高）：
  上下文窗口低于 512K 的模型下限是 `0.75` ——
  低于下限的覆盖会被抬到 `0.75`，**高于**下限的覆盖（如 `0.80`）照常生效。

插件上下文引擎可以复用同一套解析逻辑：

```python
from agent.context_compressor import resolve_model_threshold
```

自己覆盖了 `update_model()` 的引擎**拥有自己的压缩策略**，可以忽略这张映射表。

### 3.5 Codex gpt-5.x / Astra 的阈值自动抬高

（原文 `### Codex gpt-5.x / Astra threshold autoraise`）

ChatGPT Codex OAuth 后端把 gpt-5.4/5.5/5.6 和 gpt-6 Astra **硬性限制在 272K** 上下文
（同一个 slug 在 OpenAI 直连 API 和 OpenRouter 上是 1.05M，在 GitHub Copilot 上是 400K）。

在默认 50% 触发点下，压缩会在 **~136K** 就开火 —— 只用了模型实际可用窗口的一半。
所以当**当前路由是 Codex OAuth**（`provider: openai-codex`）**且**模型属于这些家族时
（Astra 匹配任何含 `astra` 的 slug；可选的 `-900k` 变体**排除在外**，
因为它们已经解锁了更宽的窗口），Hermes 把触发点抬到 **85%**（约 231K），
并显示一条带"如何关掉"命令的提示。

这条提示**每个 profile 只显示一次** —— `$HERMES_HOME` 下一个标记
（`.codex_gpt55_autoraise_notice`）记录它已经跑过，
所以反复初始化 agent/会话（例如网关每收到一条消息）**不会重复弹**；
如果抬高后的阈值后来变了，会再提示一次。

只有这一条路由受影响；**同样的模型在任何别的供应商上仍然用你的全局 `threshold`**。

```bash
# 退回全局阈值
hermes config set compression.codex_gpt55_autoraise false
# 保留 85% 抬升，只隐藏那次提示
hermes config set compression.codex_gpt55_autoraise_notice false
```

### 3.6 Codex 大上下文 `-900k` 变体（需手动开启）

（原文 `### Codex large-context `-900k` picker variants (opt-in)`）

ChatGPT Codex 后端对 gpt-5.4 和 gpt-5.6（Sol/Terra/Luna）家族**宣称** 272K 窗口，
但对 ChatGPT 订阅账号**实际上接受约 911K 输入 token**（2026-08 实测验证）。

Hermes 把**宣称的 272K 当作基础 slug 的默认值** ——
窗口更大意味着每次请求更多 token、订阅额度消耗快得多，所以大窗口是**严格选择加入**的。

要用大窗口，在 `/model` 里挑显式的 `-900k` 变体
（如 `gpt-5.6-sol-900k`、`gpt-5.6-terra-900k`、`gpt-5.6-luna-900k`、`gpt-5.4-900k`）。
这些是 **Hermes 侧的别名**：后缀会在模型 id 发给后端**之前被剥掉**，
计价/用量统计也按基础模型算。真正强制 272K 的 slug（gpt-5.5、gpt-5.4-mini）**没有** `-900k` 变体。

压缩阈值跟着窗口走：基础 slug（272K）享受上面的 **85% 自动抬高**，
而 `-900k` 变体保持你的全局 `compression.threshold`（默认 50%，约 450K）——
**抬高是为了不浪费小窗口，900K 的窗口不需要它。**

### 3.7 Codex app-server 的线程压缩

（原文 `### Codex app-server thread compaction`）

Codex app-server 会话（`api_mode: codex_app_server` —— codex CLI/agent 运行时）
和其它所有路由**都不一样**：**底层线程的上下文归 codex agent 所有**，
所以 Hermes 的辅助摘要器**压不动它** —— 只重写本地的 transcript 镜像，
真实线程会一直涨到硬性上下文重置为止。

这个运行时的压缩走 app-server 自己的机制：

- **手动**压缩（`/compress`）请 app-server 压缩线程（`thread/compact/start`），
  然后等那个压缩回合跑完。
- **自动**压缩由 `compression.codex_app_server_auto` 控制：
  默认 `native` 让 app-server 自己决定何时压缩，Hermes 只记录产生的压缩事件
  （压缩计数器、会话事件）。设成 `hermes` 则由 Hermes 的压缩阈值发起 app-server 压缩；
  `off` 则完全关闭 Hermes 发起的自动压缩（codex 仍可能自己原生压缩）。

在这个运行时上，**Hermes 的本地 transcript 永远不会被重写** ——
`state.db` 记录压缩边界，而可见的 transcript 保持完整。
其它所有路由（**包括 Codex OAuth 的聊天会话**）继续用 Hermes 的摘要压缩器。

> **为什么这么设计**：压缩的前提是"**我持有那份上下文，所以我能改它**"。
> 这一节是这条前提失效时的处理：**上下文的主人在别人手里**，
> 那么"重写本地副本"不是在压缩，只是在**制造一份和真实状态不一致的镜像** ——
> 真实线程照样涨，而你以为压过了（最坏的一类 bug：看起来做了、其实没做）。
>
> 所以正确做法是**把动作转交给持有者**（app-server 的 `thread/compact/start`），
> 本地只记录"边界在哪"。
>
> 📌 可迁移判据：**动手改一份数据之前，先确认自己是不是它的所有者。**
> 不是所有者时，方案应该是"请求所有者去做"，而不是"改我手里的复印件"。

### 3.8 原生 Responses 压缩（直连 OpenAI / Codex 订阅上的 gpt-5.6）

（原文 `### Native Responses compaction (gpt-5.6 on direct OpenAI / Codex subscription)`）

OpenAI 的 Responses API 支持**服务端压缩**：当请求里带上
`context_management: [{type: "compaction", compact_threshold: N}]`
且渲染后的输入超过 N token 时，服务端会把更老的上下文**剪枝**成一个
**不透明的加密 `compaction` 输出项**。Hermes 把这一项捕获进 assistant 消息已有的
replay sidecar，并在后续回合**原样发回去**，用它顶替被剪掉的历史 ——
长程召回**不需要客户端做一次摘要**，而且对 ZDR 友好（`store: false`、不用 `previous_response_id`）。

用 `compression.codex_responses_native: true` 开启。这个闸门**故意做得很窄**，
并且**每个请求都重新检查**：

- **模型**：只限 gpt-5.6 家族。别的模型在带上这个字段时会**服务端失败**
  （gpt-5.1/5.2 返回 HTTP 500 或让流卡住 —— **没有结构化的拒绝可供降级**，2026-08 实测）。
- **路由**：只限 `api.openai.com`（OpenAI API key）或 ChatGPT Codex 后端（Codex 订阅 OAuth）。
  xAI、GitHub/Copilot、OpenRouter、relay、本地服务器**永远看不到这个字段**。

压缩的其它一切都照旧：**本地压缩器仍然保持待命，作为兜底的所有者**
（原生阈值被夹到比本地触发点低约 8K token，让服务端先压）；
如果 provider 对这个字段给出**结构化拒绝**，本会话的原生压缩会被关闭，
请求在不带该字段的情况下重试。把会话切到不合格的模型或路由，
这个字段就**不再发送** —— 已捕获的检查点会在端点变化时被现有的
"跨发行方守卫"从 replay 里丢掉。

默认 `compression.codex_responses_compact_threshold: null` 会从解析出的本地触发点
**推导**原生阈值。例：本地触发点 765,000 会选出 **756,808**。
设正整数则保留一个绝对值（如 200,000）。非法值走自动行为。
没有可用的本地触发点时，自动模式用 200,000。
provider 的下限是 **1,024 token**，所以小于等于这个下限的异常本地触发点
**无法保持"原生严格先发生"的顺序**。

### 3.9 计算值（200K 上下文的模型 + 全部默认值）

（原文 `### Computed Values (for a 200K context model at defaults)`）

```
context_length       = 200,000
threshold_tokens     = 200,000 × 0.50 = 100,000
tail_token_budget    = 100,000 × 0.20 = 20,000
max_summary_tokens   = min(200,000 × 0.05, 12,000) = 10,000
```

> **⚠️ 阈值取自「主模型」的上下文窗口**
>
> `threshold_tokens` 永远是 `threshold × context_length`，而这个 `context_length`
> 是**主 agent 模型**的窗口，**永远不是**辅助/摘要模型的。
>
> 一个 262,144 token 的模型在默认 `0.50` 下，阈值是 `262,144 × 0.50 = 131,072`。
> 这个数字接近常见的"128K 上下文"**纯属百分比的巧合**，
> **不**说明"辅助模型的窗口是触发点"。
>
> 辅助模型的上下文窗口是**另一回事** —— 它影响的是"**摘要能不能产出来**"，
> 不影响"**压缩什么时候触发**"（见下面「摘要模型的上下文长度」那条警告）。

> **为什么这么设计**：把这两件事分开说清，是因为它们**极易被混为一个数**。
> 触发点决定"什么时候开始压缩"（成本/信息损失），
> 辅助窗口决定"这个摘要任务能不能完成"（可行性）——
> 前者按**主**模型的窗口算，因为你要回答的是"主模型还能装多少"。
>
> 📌 这也是一条数值常识：**两个不相关的量算出接近的数字时，先假设是巧合**，
> 不要倒推因果关系。

## 4. 压缩算法（Compression Algorithm）

`ContextCompressor.compress()` 走一个**四阶段**算法。

### 4.1 阶段一：剪掉旧工具结果（便宜，不调 LLM）

（原文 `### Phase 1: Prune Old Tool Results`）

**受保护尾部之外**的旧工具结果（**超过 200 字符**的）被替换成：

```
[Old tool output cleared to save context space]
```

这是一个便宜的**预处理**，从啰嗦的工具输出（文件内容、终端输出、搜索结果）里省下大量 token。

### 4.2 阶段二：确定边界

（原文 `### Phase 2: Determine Boundaries`）

```
┌─────────────────────────────────────────────────────────────┐
│  消息列表                                                    │
│                                                             │
│  [0..2]   ← protect_first_n（系统提示词 + 第一轮对话）        │
│  [3..N]   ← 中间回合 → 被摘要                                  │
│  [N..end] ← 尾部（按 token 预算 或 protect_last_n）           │
└─────────────────────────────────────────────────────────────┘
```

尾部保护是**按 token 预算**做的：从末尾往前扫，累加 token 直到预算用完。
如果按预算保护下来的消息**比**固定的 `protect_last_n` 还少，就**回退**到那个固定条数。

边界会**对齐**，避免把 `tool_call` / `tool_result` 组切开：
`_align_boundary_backward()` 会**越过连续的工具结果**找到它们的父 assistant 消息，
让整组保持完整。

### 4.3 阶段三：生成结构化摘要

（原文 `### Phase 3: Generate Structured Summary`）

> **⚠️ 摘要模型的上下文长度**
>
> 摘要模型的上下文窗口必须**至少和主 agent 模型一样大**。
> 整个中间段是在**一次** `call_llm(task="compression")` 调用里发给摘要模型的。
> 如果摘要模型的上下文更小，API 会返回"上下文超长"错误 ——
> `_generate_summary()` **捕获它、记一条警告、返回 `None`**。
> 于是压缩器**在没有摘要的情况下**直接丢掉中间回合，**静默地**失去对话上下文。
> **这是压缩质量变差最常见的原因。**

中间回合用辅助 LLM 按一个**结构化模板**生成摘要：

```
## Goal                        ## 目标
## Constraints & Preferences   ## 约束与偏好
## Progress                    ## 进度
### Done                       ### 已完成
### In Progress                ### 进行中
### Blocked                    ### 受阻
## Key Decisions               ## 关键决策
## Relevant Files              ## 相关文件
## Next Steps                  ## 下一步
## Critical Context            ## 关键上下文
```

摘要预算随被压缩内容的多少而缩放：

- 公式：`content_tokens × 0.20`（`_SUMMARY_RATIO` 常量）
- 下限：2,000 token
- 上限：`min(context_length × 0.05, 12,000)` token

### 4.4 阶段四：组装压缩后的消息

（原文 `### Phase 4: Assemble Compressed Messages`）

压缩后的消息列表是：

1. **头部消息**（第一次压缩时，系统提示词上会**追加一句说明**）
2. **摘要消息**（角色会挑一个**不会造成连续同角色**的）
3. **尾部消息**（**原样不动**）

**孤立的** tool_call/tool_result 配对由 `_sanitize_tool_pairs()` 清理：

- 引用了已被移除调用的工具结果 → **删掉**
- 结果已被移除的工具调用 → **注入一条占位结果**

### 4.5 迭代式再压缩

（原文 `### Iterative Re-compression`）

后续每次压缩时，**上一次的摘要会被一起发给 LLM**，指令是**更新它**，
而不是从头再摘一遍。这样信息能跨多次压缩保留下来 ——
条目从"进行中"移到"已完成"、新增进展、删掉过时信息。

压缩器实例上的 `_previous_summary` 字段就存着上次的摘要文本。

> **为什么这么设计**：这一节有一个很值钱的性质 ——
> **"再压缩"不是"重新开始"，而是"编辑上一版"**。
>
> 如果每次都从零摘要，那**第一次压缩就已经丢掉的信息永远不会回来**，
> 而且每次压缩都会独立地再丢一次（损失**累积**）。
> 改成"更新上一版"之后，摘要变成一份**活的文档**，
> 后一次压缩可以**修正**前一次的错误（把误判的"进行中"标成"已完成"）。
>
> 📌 可迁移判据：**有损操作重复发生时，让它作用于上一次的结果而不是原始输入。**
>
> 另外，阶段三那条警告值得单独记住：它的失败模式是**静默降级**
> —— 摘要失败不报错、不中止，而是**直接丢掉中间回合**。
> 所以"压缩后模型变笨了"的第一嫌疑人是**摘要模型的上下文窗口**，而不是压缩逻辑。

---

## 5. 前后对比示例（Before/After Example）

### 5.1 压缩前（45 条消息，约 95K token）

（原文 `### Before Compression (45 messages, ~95K tokens)`）

```
[0] system:    "You are a helpful assistant..."（系统提示词）
[1] user:      "Help me set up a FastAPI project"
[2] assistant: <tool_call> terminal: mkdir project </tool_call>
[3] tool:      "directory created"
[4] assistant: <tool_call> write_file: main.py </tool_call>
[5] tool:      "file written (2.3KB)"
    ... 还有 30 多轮改文件、跑测试、修 bug ...
[38] assistant: <tool_call> terminal: pytest </tool_call>
[39] tool:      "8 passed, 2 failed\n..."  (5KB 输出)
[40] user:      "Fix the failing tests"
[41] assistant: <tool_call> read_file: tests/test_api.py </tool_call>
[42] tool:      "import pytest\n..."  (3KB)
[43] assistant: "I see the issue with the test fixtures..."
[44] user:      "Great, also add error handling"
```

### 5.2 压缩后（25 条消息，约 45K token）

（原文 `### After Compression (25 messages, ~45K tokens)`）

```
[0] system:    "You are a helpful assistant...
               [Note: Some earlier conversation turns have been compacted...]"
[1] user:      "Help me set up a FastAPI project"          ← 头部保留
[2] assistant: "[CONTEXT COMPACTION] 早先的回合已被压缩...

               ## Goal
               搭一个带测试和错误处理的 FastAPI 项目

               ## Progress
               ### Done
               - 建好项目结构：main.py、tests/、requirements.txt
               - main.py 里实现了 5 个 API 端点
               - tests/test_api.py 里写了 10 个用例
               - 10 个里 8 个通过

               ### In Progress
               - 修 2 个失败的用例（test_create_user、test_delete_user）

               ## Relevant Files
               - main.py —— 5 个端点的 FastAPI 应用
               - tests/test_api.py —— 10 个用例
               - requirements.txt —— fastapi、pytest、httpx

               ## Next Steps
               - 修测试 fixture
               - 加错误处理"

[3] user:      "Fix the failing tests"                     ─┐
[4] assistant: <tool_call> read_file: tests/test_api.py </tool_call>  ├ 尾部原样保留
[5] tool:      "import pytest\n..."                        │
[6] assistant: "I see the issue with the test fixtures..."  │
[7] user:      "Great, also add error handling"            ─┘
```

**读这个例子的三个要点**：

1. **头部（system + 第一轮）和尾部（最近几轮）是逐字保留的**，只有中间被摘要
   —— 这正是"阶段二"那张图的落点。
2. 摘要**替代了 32 条消息**（[2]~[39]），而它自己只占 1 条；
   省下的量级（95K → 45K）主要来自这里 + 阶段一剪掉的旧工具输出。
3. **最后一条真人是用户消息**（[7]），摘要里也写着"下一步" ——
   模型接手时既有"大局"（摘要）也有"当前这一手"（尾部）。

---

## 6. 提示词缓存（Anthropic）

（原文 `## Prompt Caching (Anthropic)`）

源码：`agent/prompt_caching.py`

通过缓存**对话前缀**，在多轮对话里把**输入 token 成本降低约 75%**。
用的是 Anthropic 的 `cache_control` 断点。

### 6.1 策略：system_and_3

Anthropic 允许每个请求**最多 4 个** `cache_control` 断点。Hermes 用"system_and_3"策略：

```
断点 1：系统提示词                    （所有回合都稳定）
断点 2：倒数第 3 条非 system 消息  ─┐
断点 3：倒数第 2 条非 system 消息   ├─ 滚动窗口
断点 4：最后一条非 system 消息     ─┘
```

### 6.2 它是怎么做的

`apply_anthropic_cache_control()` 会**深拷贝**消息并注入 `cache_control` 标记：

```python
# 缓存标记的格式
marker = {"type": "ephemeral"}
# 或者要 1 小时 TTL：
marker = {"type": "ephemeral", "ttl": "1h"}
```

标记**放哪里**取决于内容类型：

| 内容类型 | 标记放在哪 |
|---|---|
| 字符串内容 | 转成 `[{"type": "text", "text": ..., "cache_control": ...}]` |
| 列表内容 | 加到**最后一个元素**的 dict 上 |
| `None` / 空 | 作为 `msg["cache_control"]` |
| 工具消息 | 作为 `msg["cache_control"]`（**仅原生 Anthropic**） |

### 6.3 面向缓存的设计模式

（原文 `### Cache-Aware Design Patterns`）

1. **系统提示词要稳定**：它是断点 1，跨所有回合被缓存。
   避免在对话中途改动它（**压缩只在第一次压缩时**追加一句说明）。
2. **消息顺序很重要**：缓存命中要求**前缀匹配**。
   在**中间**增删消息会让它之后的全部缓存失效。
3. **压缩与缓存的相互作用**：压缩之后，**被压缩区域的缓存失效**，
   但**系统提示词那部分缓存活下来**。滚动的那 3 条消息窗口会在 **1~2 轮内**重新建立缓存。
4. **TTL 选择**：默认 `5m`（5 分钟）。如果用户会在回合之间离开一会儿的长会话，用 `1h`。
5. **模型身份是缓存键的一部分**：provider 侧的缓存**按模型（以及账号/API key）作用域**。
   任何**对话中途的模型切换** —— 显式 `/model`、主模型回退、
   或者凭证池轮换到另一个账号 —— 都意味着**下一次请求零命中**，
   并以**未打折的输入价**重读整段对话。
   这是 provider 缓存的工作方式决定的，**不是 Hermes 能避免的**；
   `/model`、回退供应商、凭证池的**面向用户的文档因此都带成本警告**。
   **不要添加"静默切换模型或凭证"的功能。**

### 6.4 开启提示词缓存

满足以下条件时**自动开启**：

- 模型是 Anthropic Claude（按模型名识别）
- 供应商支持 `cache_control`（原生 Anthropic API 或 OpenRouter）

```yaml
# config.yaml —— TTL 可配（必须是 "5m" 或 "1h"）
prompt_caching:
  cache_ttl: "5m"
```

CLI 启动时会显示缓存状态：

```
💾 Prompt caching: ENABLED (Claude via OpenRouter, 5m TTL)
```

> **为什么这么设计**：这一节把前面所有内容串起来了 ——
> **缓存的单位是"从第一个字节开始的最长公共前缀"**，
> 所以：
> ① 越靠前的内容越值钱（系统提示词被所有回合共享 → 断点 1）；
> ② 改**中间**比改**尾部**贵得多（尾部之后没有东西可失效）；
> ③ 切模型/换凭证 = 换了一把缓存钥匙 → 全量重读。
>
> 三条设计模式都是这一条的推论。"滚动 3 条窗口"的存在理由也在这：
> 断点必须**跟着对话末尾走**，否则新增内容永远不会被缓存。

---

## 7. 上下文压力告警已被移除

（原文 `## Context Pressure Warnings`）

中间的**上下文压力告警已被移除** —— 见 `agent/turn_iteration_prep.py` 里
迭代预算那一段的注释：

> *No intermediate pressure warnings — they caused models to 'give up' prematurely
> on complex tasks*
> （不要中间的压力告警 —— 它们会让模型在复杂任务上**过早放弃**）

现在：prompt token 达到配置的 `compression.threshold`（默认 50%）时**直接压缩**，
**中间没有任何告警步骤**；网关侧的会话维护（session hygiene）在模型上下文窗口的 **85%** 处作为第二道安全网触发。

> **为什么这么设计**：这是一个**"好心提示反而有害"**的实例，值得记住 ——
> 告诉模型"你快没空间了"，本意是让它节省、收敛，
> 实际效果是它**提前放弃**复杂任务（"空间不多了，那我不如先总结一下现状"）。
> 所以正确的做法是**让这件事对模型不可见**：空间不够时**系统自己处理**（压缩），
> 而不是**通知模型去处理**。
>
> 📌 可迁移判据：**给模型增加"环境信息"之前，先问它会不会改变模型的目标。**
> 这条提示对"任务是否继续"有影响，那它就**不只是信息**，而是任务约束。

---

## 附录 A · 结构对照表

| 原文小节 | 本文件 |
|---|---|
| （开篇导语） | §0 |
| `## Pluggable Context Engine` | §1 |
| `## Dual Compression System` | §2（含 2.1 / 2.2.1 / 2.2.2） |
| `## Configuration` + `### Parameter Details` | §3 / §3.1 |
| `### In-place compaction` | §3.2 |
| `### Auxiliary feasibility and tail retention` | §3.3 |
| `### Per-model threshold overrides` | §3.4 |
| `### Codex gpt-5.x / Astra threshold autoraise` | §3.5 |
| `### Codex large-context -900k picker variants` | §3.6 |
| `### Codex app-server thread compaction` | §3.7 |
| `### Native Responses compaction` | §3.8 |
| `### Computed Values` + 那条 note | §3.9 |
| `## Compression Algorithm`（4 个 Phase + 迭代） | §4.1 ~ §4.5 |
| `## Before/After Example` | §5 |
| `## Prompt Caching (Anthropic)` | §6 |
| `## Context Pressure Warnings` | §7 |

## 附录 B · 这篇精译里**故意没有**的东西

按「精译 = 翻译为主、看懂原理即可」的边界，以下内容**不在这篇里**（它们属于实现/讲解阶段）：

- **mini 版的现状与动手清单** —— 那类记录**会在实现完成后变成错的**
  （第 4 章的教训：`【动手】` 块里写着"mini 还没有 check_fn"，而那时已经实现了）。
- **源码深挖**（成千行级的 `文件:行号` 逐段对照）—— 放《源码讲解-05》附录。
- **"文档与源码不一致"核对表** —— 需要真去读 `agent/context_compressor.py` /
  `agent/prompt_caching.py` 等源码才能写，属于讲解阶段。**这篇里因此不含任何
  未经核对的源码断言。**
- **本项目的规模数字**（行数、测试条数）—— 会腐烂，而且放错地方会误导。

## 附录 C · 待补（本章还没做完的部分）

第 5 章的五件交付物里，本篇只是第 1 件：

| # | 交付物 | 状态 |
|---|---|---|
| 1 | **中文精译**（本文件） | ✅ 初稿（645 行原文 → 本文件） |
| 2 | mini-hermes 实现 | ✅ 四个模块（`context_engine` / `usage_anchor` / `context_compressor` / `prompt_caching`） |
| 3 | 机制自测（不联网、假响应驱动） | ✅ `tests/test_compression.py` 114 条 / 9 组 |
| 4 | 真实跑通 | ✅ 提交 `e117b8c`（42 条 / 27,502 token → 24 条 / 15,990）；写《源码讲解-05》时又**重跑了一遍**（34 条 / 52,942 → 25 条 / 33,825，真 DeepSeek 摘要 1,160 字符），收据在《源码讲解-05》§12.5 |
| 5 | 源码讲解-05（差异表 + 真问题 + 差距清单） | ✅《源码讲解-05-ContextCompressionAndCaching.md》（§〇 差异一览表 24 条 / §十二 真问题 / §十四 差距清单 18 条） |
| — | 「文档与源码不一致」核对（第 2、3、4 章都有发现） | ✅ 本章 **6 处**，见《源码讲解-05》附录 C（最重的一处：文档说"默认 50% 触发"，源码对 <512K 窗口的模型抬到 75%） |
