# 中文精译 · 第 8 章：Provider 运行时解析（Provider Runtime Resolution）

> **原文**：`website/docs/developer-guide/provider-runtime.md`（**222 行**，官方侧边栏 `developer-guide` 第 15 位；
> 同时是 `architecture.md`「Recommended Reading Order」的**第 4 篇** —— 两份官方推荐口径**唯一共同点名**、我们此前一直没做的一篇）。
> **本篇定位**：精译为主（看懂"这一层在干什么、为什么必须有"）。差异与源码深挖留给《源码讲解-08》。
> **📌 行号锚**：真实源码 `$HOME/.hermes/hermes-agent/`，锚上游 commit **`f5d1926110`**（`version = "0.21.3"`）。
> 本文所有 `文件:行号` 都对这个 commit（本篇只用到文件级引用，行号在源码讲解里给）。
> **本章出入处数**：轻量核对 3 处（见附录 C）—— 按 2026-10-10 定的新口径，只核**架构性声明**，
> 细节不逐条对（学习用，不为细节付成本）。

[TOC]

---

## 0. 这一章在回答什么

一句话：**"用哪个供应商、哪个模型、哪把钥匙、发到哪个地址" 这件事，Hermes 只解析一次，五个入口共用。**

五个入口是：CLI（`hermes chat`）、网关（消息平台）、cron 任务（全新会话里跑）、ACP 编辑器会话、
以及**辅助模型调用**（下面 §10 会讲它是什么）。

为什么值得单独有一层：这几个入口的**触发时机、进程模型、有没有人坐在前面**都不一样，
但"用哪个模型"这件事的答案**必须一致** —— 不能出现"CLI 用的是 A 端点、网关用的是 B 端点、
cron 又悄悄回落到 C"这种局面。所以解析逻辑被抽成一层共享运行时，而不是各入口各写一遍。

主要实现（原文列表，逐项照抄，见附录 B 的规模）：

| 文件 | 负责什么 |
|---|---|
| `hermes_cli/runtime_provider.py` | **凭据解析**、自定义端点的运行时解析 |
| `hermes_cli/auth.py` | **供应商注册表** + `resolve_provider()` |
| `hermes_cli/model_switch.py` | 共享的 `/model` 切换流水线（CLI 与网关共用） |
| `agent/auxiliary_client.py` | **辅助模型**的路由 |
| `providers/` | ABC + 注册表入口：`ProviderProfile` / `register_provider` / `get_provider_profile` / `list_providers` |
| `plugins/model-providers/<名字>/` | 每个供应商一个插件，声明 `api_mode` / `base_url` / `env_vars` / `fallback_models`，**首次访问时注册进注册表**；用户插件（`$HERMES_HOME/plugins/model-providers/<名字>/`）**覆盖同名内建件** |

📌 **这一层最关键的设计决定**：`runtime_provider.py` **不自己存供应商数据**。
它在解析时调用 `providers/` 的 `get_provider_profile(provider_id)`，拿到规范的 `base_url`、
`env_vars` 优先级列表、`api_mode`、`fallback_models`：

> 一句话理由（原文逐字）：*"without needing to duplicate that data in multiple files"* ——
> 数据只存一份，解析器只管**用**。所以**加一个新供应商不需要改解析器**，只要放一个插件调 `register_provider()`。

---

## 1. Chat-completions 的 reasoning 形状 —— 字段不统一的代价

（原文节名：*Chat-completions reasoning shapes*）

**推理内容该用哪个字段名，业界没统一，导致用 chat-completions 接口的开发者不得不为每家厂商单独适配，很烦。**

任何"OpenAI 兼容"的中转都可能把 `reasoning` / `reasoning_content` 返回成三种完全不同的形状：

- **字符串**；
- **文本块字典**（text-part dict）；
- **文本块与字符串片段混在一个列表里**。

Hermes 在**做任何字符串操作之前**先把它拍平（flatten），而且是**五处都拍**：
主流式路径、Relay 录制、同步与异步**辅助流**、以及完整响应的 reasoning 抽取。

两条必须守住的细节（原文逐字要求的）：

- **片段保留自己的空白** —— 归一化时**不插**额外的分隔符（否则模型写的字会被粘起来）；
- **主流式与 Relay 录制保留完整粗体小标题之间的段落分隔** —— 也就是不能把本来分段的内容压成一行。

最后一条是产品级的边界：**reasoning 始终与"给用户看的答案"分开**（推理内容不进答案）。

---

## 2. 解析优先级 —— 四层顺序，以及顺序为什么重要

（原文节名：*Resolution precedence*）

从上到下，先命中的赢：

1. **显式的 CLI / 运行时请求**（这一次调用就指定了供应商或模型）；
2. **`config.yaml` 里的 model / provider 配置**；
3. **环境变量**；
4. **供应商专属默认值，或自动解析**（auto）。

★ 顺序为什么重要（原文给的判据）：**"保存下来的 model/provider 选择，才是正常运行的唯一事实来源"**
（*the saved model/provider choice as the source of truth for normal runs*）。

反面例子就一句话：**别让一个"上一次 shell 里导出过的旧环境变量"悄悄盖掉用户刚刚在 `hermes model` 里选的端点。**
—— 这正是把 config 排在 env **上面**的原因。

**配置优先级的设计，不能让环境变量盖过配置文件。**

---

## 3. 供应商清单

（原文节名：*Providers*）

原文列出的供应商家族（完整内建集合以 `plugins/model-providers/` 为准）：

AI Gateway (Vercel) · OpenRouter · Nous Portal · OpenAI Codex · Copilot / Copilot ACP ·
Anthropic（原生）· Google / Gemini · Alibaba / DashScope（`alibaba`、`alibaba-coding-plan`）·
DeepSeek · Z.AI · Kimi / Moonshot（`kimi-coding`、`kimi-coding-cn`）·
MiniMax（`minimax`、`minimax-cn`、`minimax-oauth`）· Kilo Code · Hugging Face ·
OpenCode Zen / OpenCode Go · AWS Bedrock · Azure Foundry · NVIDIA NIM · xAI (Grok) · Arcee ·
GMI Cloud · StepFun · Qwen OAuth · Xiaomi · Ollama Cloud · LM Studio · Tencent TokenHub。

**两条"自己接"的路**：

- **`provider: custom`** —— 任何 OpenAI 兼容端点的一等供应商；
- **具名自定义供应商** —— `config.yaml` 里的 `providers:` 字典（**旧的 `custom_providers` 列表仍会被读**，
  为了向后兼容）。

📌 观察：这里能看出 Hermes 的产品姿态 —— **供应商是"边缘"上的东西**（第 7 章的根 `AGENTS.md`：
*core is a narrow waist; capability lives at the edges*），加供应商是加插件，不是改核心。

---

## 4. 解析结果长什么样

（原文节名：*Output of runtime resolution*）

运行时解析器返回的数据：

| 字段 | 含义 |
|---|---|
| `provider` | 供应商 id（`deepseek` / `anthropic` / `custom` …） |
| `api_mode` | **走哪种线上协议**：`chat_completions` / `anthropic_messages` / `codex_responses`（见 §8、§9） |
| `base_url` | 实际发请求的地址 |
| `api_key` | 这一家该用的钥匙 |
| `source` | **这个结果从哪来**（显式 / config / env / auto） |
| 供应商专属元数据 | 例如凭据的过期与刷新信息 |

📌 注意 `source` 这一栏：它让"为什么用了这个端点"变成**可回答的**（而不是靠猜）。
这和我们在第 5~7 章反复见到的同一种设计取向一致：**把裁决理由落成数据，别只留在日志里。**

---

## 5. 为什么要有这一层

（原文节名：*Why this matters*）

原文的说法很直白：这个解析器是 Hermes 能把**认证与运行时逻辑**共享给下面这些入口的**主要原因**：

- `hermes chat`（交互式 CLI）；
- **网关**的消息处理；
- **cron 任务**（在全新会话里跑）；
- **ACP** 编辑器会话；
- **辅助模型任务**。

也就是说：**入口不同，但"用哪个模型"的答案只有一个来源。**

---

## 6. AI Gateway（Vercel）

（原文节名：*AI Gateway*）

用法：把 `AI_GATEWAY_API_KEY` 写进 `~/.hermes/.env`，然后以 `--provider ai-gateway` 运行。

行为：Hermes 会从该网关的 `/models` 端点**拉取可用模型**，并且**只筛出支持工具调用的语言模型**
（过滤掉不支持 tool-use 的、非语言模型）。

---

## 7. 三把 key 各管自己的域名 —— 别把钥匙发给错的端点

（原文节名：*OpenRouter, AI Gateway, and custom OpenAI-compatible base URLs*）

这一节解决一个具体的安全问题：同时存在多把钥匙时（`OPENROUTER_API_KEY`、`AI_GATEWAY_API_KEY`、
`OPENAI_API_KEY`），**不要把错的钥匙发给自定义端点**。

规则（每把钥匙只发给自己那条域名）：

- `OPENROUTER_API_KEY` **只**发给 `openrouter.ai` 的端点；
- `AI_GATEWAY_API_KEY` **只**发给 `ai-gateway.vercel.sh` 的端点；
- `OPENAI_API_KEY` 用于自定义端点，**并作为兜底**。

Hermes 还会区分两件长得很像的事：

- **用户真的选了一个自定义端点**；
- **没有配任何自定义端点时走的那条 OpenRouter 兜底路径**。

为什么这个区分很重要（原文列了四条）：

- 本地模型服务器（`localhost` 上的推理服务）；
- **非 OpenRouter / 非 AI Gateway** 的 OpenAI 兼容 API；
- 切换供应商时**不必重跑 setup**；
- **配置里存过的自定义端点应当继续可用**，即使当前 shell 没有导出 `OPENAI_BASE_URL`。

---

## 8. Anthropic 原生路径

（原文节名：*Native Anthropic path*）

Anthropic 不再是"只能走 OpenRouter"。当解析选中 `anthropic` 时：

- `api_mode = anthropic_messages`；
- 走 **原生 Anthropic Messages API**；
- 由 `agent/anthropic_adapter.py` 做**协议翻译**。

**凭据解析的优先级**（这一条是本节的重点）：两者都在时，**可刷新的 Claude Code 凭据优先于复制来的环境变量 token**。
具体表现为四条：

- Claude Code 的凭据文件**只要包含可刷新认证，就被当作首选来源**；
- 手写的 `ANTHROPIC_TOKEN` / `CLAUDE_CODE_OAUTH_TOKEN` **仍然可用**，作为显式覆盖；
- Hermes 在调原生 Messages API **之前会预检（preflight）Anthropic 凭据刷新**；
- 仍然保留一条兜底：**401 之后重建 Anthropic 客户端再重试一次**。

📌 这一节的通用价值：**"谁优先"必须写下来**。同一份凭据可能有多个来源（凭据文件 / 环境变量 / 覆盖值），
不写优先级就会出现"我明明换了 token 它还用旧的"这类只能靠翻代码才能解释的现象。

---

## 9. OpenAI Codex 路径

（原文节名：*OpenAI Codex path*）

Codex 走**另一条协议**：

- `api_mode = codex_responses`（Responses API）；
- 有**专属的凭据解析**与 auth store 支持。

---

## 10. 辅助模型的独立路由

（原文节名：*Auxiliary model routing*）

**辅助任务**指这些"不是主对话、但也要调模型"的活：

- 视觉（看图）；
- 网页抓取的摘要；
- **上下文压缩的摘要**（第 5 章那段摘要就是它）；
- 技能中心（skills hub）操作；
- MCP 辅助操作；
- 记忆刷写（memory flush）。

它们**可以用自己的供应商 / 模型**，与主对话模型分开。

当一个辅助任务被配置成 provider = `main` 时，Hermes 会**走与普通聊天同一条共享运行时路径**。实际含义三条：

- 环境变量驱动的自定义端点仍然有效；
- 通过 `hermes model` / `config.yaml` 保存的自定义端点也有效；
- 辅助路由**能分辨**"真的保存了一个自定义端点"与"OpenRouter 兜底路径"。

---

## 11. 回退模型链

（原文节名：*Fallback models*）

Hermes 支持配置一条**回退供应商链**：一串 `(provider, model)` 条目，主模型出错时**按顺序**尝试。
旧的单对形式 `fallback_model` 字典仍被接受（向后兼容，**首次写入时迁移**）。

### 11.1 内部怎么工作

1. **存储**：`AIAgent.__init__` 存下 `fallback_model` 字典，并把 `_fallback_activated = False`。
2. **触发点**：`_try_activate_fallback()`（转发到 `agent/chat_completion_helpers.py::try_activate_fallback()`）
   在回合阶段有**三处**调用点（`agent/turn_api_error.py`、`agent/turn_response_check.py`、`agent/turn_recovery.py`）：
   - **无效 API 响应**（`choices` 为 None、缺内容）重试到上限之后；
   - **不可重试的客户端错误**（HTTP 401 / 403 / 404）；
   - **瞬时错误**（HTTP 429 / 500 / 502 / 503）重试到上限之后。

   📌 注意这与第 2 章讲过的重试纪律**同一套判据**：401/403/429/5xx 可回退，400/422 不回退。
3. **激活流程**（`_try_activate_fallback` 逐步）：
   - 已经激活过、或压根没配 → **立刻返回 False**；
   - 调 `auxiliary_client.py::resolve_provider_client()` **建一个新客户端**（带正确的认证）；
   - 决定 `api_mode`：`openai-codex` → `codex_responses`；`anthropic` → `anthropic_messages`；其余 → `chat_completions`；
   - **原地替换**：`self.model` / `self.provider` / `self.base_url` / `self.api_mode` / `self.client` / `self._client_kwargs`；
   - Anthropic 回退时**建原生 Anthropic 客户端**，不是 OpenAI 兼容客户端；
   - **重新评估提示词缓存**（OpenRouter 上的 Claude 模型会开启）；
   - 置 `_fallback_activated = True` —— **防止再次触发**；
   - **重试计数归零**，继续回合循环。
4. **配置从哪来**：
   - CLI：`hermes_cli/fallback_config.get_fallback_chain()` → 传给 `AIAgent(fallback_model=...)`；
   - 网关：`gateway/run_config_loaders.py` 的 `_load_fallback_model()` 读 `config.yaml` → 传给 `AIAgent`；
   - **校验**：`provider` 与 `model` 两个键都非空，否则回退**直接禁用**。

### 11.2 哪些地方**不**支持回退

- **子代理委派**（`tools/delegate_tool.py`）：子代理继承父级的供应商，但**不继承回退配置**；
- **辅助任务**：走它们自己那套独立的供应商自动探测链（见 §10）。

**cron 任务支持回退**：`run_job()` 从 `config.yaml` 读 `fallback_providers`（或旧的 `fallback_model`）
并传给 `AIAgent(fallback_model=...)`，与网关的 `_load_fallback_model()` 同一套路。

### 11.3 测试覆盖（原文列出的三处）

- `tests/agent/test_fallback_credential_isolation.py` —— 主/回退**凭据隔离**；
- `tests/hermes_cli/test_fallback_cmd.py` —— `/fallback` 这个 CLI 命令；
- `tests/gateway/test_fallback_eviction.py` —— 网关**淘汰失败供应商**。

---

## 12. 相关文档

原文指向三篇：`agent-loop`（第 2 章，我们做过）、`acp-internals`、`context-compression-and-caching`（第 5 章，我们做过）。

---

## 附录 A · 术语表

| 术语 | 原文 | 一句话意思 |
|---|---|---|
| 供应商 id | provider id | 如 `deepseek`、`anthropic`、`custom` —— 解析器与注册表用的键 |
| 线上协议 | **`api_mode`** | 这一家**用哪种 API 形状说话**：`chat_completions`（OpenAI 兼容，最常见）/ `anthropic_messages`（Anthropic 原生）/ `codex_responses`（OpenAI Responses） |
| 端点 | `base_url` | 请求实际发往的地址；同一个供应商可能因区域/网关而有多个 |
| 解析 | resolution | 从四层来源（显式 → config → env → 默认/auto）定下"这次用哪家、哪个模型、哪把钥匙、发哪里" |
| **辅助模型** | **auxiliary model** | **不干主对话的活但也要调模型**的那些调用（看图、摘要、记忆刷写…）。它可以配自己的供应商/模型，默认可用 `main` 表示"跟主对话同路" |
| 辅助流 | auxiliary stream | 辅助任务的流式/非流式响应通道（§1 里要一起拍平 reasoning 的五处之一） |
| 回退链 | **fallback chain** | 主模型失败时按顺序尝试的 `(供应商, 模型)` 列表；**只触发一次**（`_fallback_activated`） |
| ACP | Agent Client Protocol | 编辑器（VS Code / Zed / JetBrains）接进来的那套协议（官方有 `acp-internals` 一篇，我们还没做） |
| 中转 / 网关（此处） | relay | 第三方"OpenAI 兼容"转发服务（OpenRouter、AI Gateway 这类）；它返回的字段形状不保证与官方一致 → §1 |

---

## 附录 B · 本篇提到的实现文件（锚 `f5d1926110`，实测规模）

| 文件 | 实测 | 角色 |
|---|---|---|
| `hermes_cli/runtime_provider.py` | 58 KB | 凭据解析 + 自定义端点运行时解析（本篇主角） |
| `hermes_cli/auth.py` | 114 KB | 供应商注册表 + `resolve_provider()` |
| `hermes_cli/model_switch.py` | 84 KB | 共享 `/model` 切换流水线 |
| `agent/auxiliary_client.py` | 401 KB | 辅助模型路由 + `resolve_provider_client()` |
| `providers/` | `README.md` / `base.py` / `__init__.py` | ABC + 注册表入口 |
| `plugins/model-providers/` | **39 个供应商目录**（文档列 29 个家族） | 每家的声明式插件（`api_mode` / `base_url` / `env_vars` / `fallback_models`） |

---

## 附录 C · 本篇与上游源码的出入（轻量核对，2026-10-10）

★ **三处"文档说的与源码不符"**（读源码取证时发现，正文按原文照译，这里给事实）：

1. **§11.2「子代理不支持回退」—— 与源码相反**。文档说子代理"继承父级供应商但不继承回退配置"；
   实测 `tools/delegate_tool.py`：子代理**默认继承父级的回退链**，只有显式 pinned 或
   `fallback_providers: []` 才会断。另外**辅助任务也不是"没有回退"**，而是不走主机制
   （`_try_activate_fallback`），它走**解析期**自己的链（见 §10）。
2. **§11.1「三处触发点」—— 实际远多于三处（6 个模块 / 11 处调用点）**。文档列了 `turn_api_error.py` / `turn_response_check.py` / `turn_recovery.py` 三处；实测锚点上 `_try_activate_fallback()` 还被 `agent/turn_api_call.py:258`、`agent/turn_empty_response.py:271`、`agent/turn_truncation.py:213` 与 `:600` 调用（`turn_api_error.py` 自身也有 `:330` 与 `:359` 两处）。上游自己的测试就把这件事写成了断言：`tests/agent/test_prompt_cache_ttl_propagation.py:256` —— `assert seen >= 5, "expected the retry-loop _try_activate_fallback sites"`。文档列的是**按原因归纳的三类**，不是调用点清单。

3. **§7「`OPENAI_API_KEY` 用于自定义端点，并作为兜底」—— 源码比这句话严格**。
   实测 `runtime_provider.py:317-326 _host_gated_env_key_candidates`：三把 key（OLLAMA / OPENAI / OPENROUTER）
   **都只挂自己的权威域名**，其余一律给空串；自定义端点走 `_host_derived_api_key()`（从 host 反推
   `<VENDOR>_API_KEY`）。docstring 附了安全理由（逐字）：*"Sending OPENAI/OPENROUTER/OLLAMA keys to an
   unrelated endpoint leaks credentials (GHSA-76xc-57q6-vm5m); match on HOST, not substring."*
   —— 即"兜底"是**按域名推导出的那把 key**，不是"把 OpenAI 的 key 发给任意端点"。

（下面三处是"清单/形态类"出入，不算错，列出便于对照。）

按用户 2026-10-10 定的新口径（**架构优先、不为细节付成本**），本篇只核**架构性声明**，
不逐条对函数级细节（那些留给《源码讲解-08》）。核了三处：

1. **供应商清单不全**：文档列 29 个家族，实测 `plugins/model-providers/` 下有 **39 个供应商目录**
   （含 `actual` / `azure-foundry` / `kilo-code` 等未在文中点名的）。→ 文档自己也写了
   *"see `plugins/model-providers/` for the complete bundled set"*，属"清单永远落后于现实"，不算错。
2. **`providers/` 的规模比文档暗示的小**：文档把它说成"ABC + 注册表入口"，实测就是一个很薄的小包
   （`base.py` + `__init__.py` + `README.md`）—— 与文档描述一致，但**别以为供应商数据住在这里**：
   真正的数据在 `plugins/model-providers/<名字>/`，`providers/` 只是取用口。
3. **文档没写的两件（对我们后面做 mini 有用）**：
   - `api_mode` 的默认判定是**按 URL 探测**（`runtime_provider.py` 里那句 *"URL detection (host-mandated wire shapes)"*），
     不是硬编码表 —— 所以"自定义端点该走哪种协议"这件事有确定规则可抄；
   - 有一层 `*_explicit_api_mode` 的支持位（哪些供应商**允许**被显式指定 `api_mode`）——
     文档完全没提，但它是"用户手选协议"这类功能的闸门。
