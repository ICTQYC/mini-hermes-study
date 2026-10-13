# 中文精译 · 第 14 章：上下文引擎插件（Context Engine Plugins）

> 上游：`官方文档-Developer-Guide/context-engine-plugin.md`（原文 **272 行** · 1 个 H1 + 11 个 H2 + 5 个 H3）· 锚点 commit `f5d1926110`（v0.21.3）

[TOC]

---

## 0. 这一章在回答什么

> 📌 译注（补充，2026-10-13）

上下文引擎插件回答的是"**当上下文逼近 token 上限时，用什么策略来管理它**"—— 内置的 `ContextCompressor` 走的是有损摘要，而插件引擎可以换成另一套策略（例如构建知识 DAG、做无损上下文管理的 LCM 引擎）。本章给出这套插件的完整接口契约：必须实现的 `ContextEngine` 抽象基类（ABC）方法（`name` / `update_from_response` / `should_compress` / `compress`）、必须自行维护的若干 token 状态类属性、一组默认空操作的**可选钩子**（含两个用于每回合"选择 / 观测"的 `select_context()` 与 `on_turn_complete()`），以及引擎如何暴露自己的工具。随后讲清组织面：插件住在 `plugins/context_engine/<name>/`、如何经由目录发现或通用插件系统注册（**同一时刻只允许一个引擎活动**）、生命周期各阶段的调用次序、如何用 `config.yaml` 的 `context.engine` 选中它。最后两节收尾在工程细节上：如何写 ABC 契约测试，以及压缩在**池化守护线程**上运行所要求的线程安全前提。

---

## 1. How it works —— 工作原理

上下文引擎插件用另一种管理对话上下文的策略，**替换**内置的 `ContextCompressor`。例如一个无损上下文管理（Lossless Context Management，LCM）引擎，它构建知识 DAG，而不是做有损摘要。

agent 的上下文管理建立在一个 `ContextEngine` 抽象基类（ABC）之上（`agent/context_engine.py`）。内置的 `ContextCompressor` 是默认实现。插件引擎必须实现同一套接口。

同一时刻**只能有一个**上下文引擎处于活动状态。选择由配置驱动：

```yaml
# config.yaml
context:
  engine: "compressor"    # default built-in
  engine: "lcm"           # activates a plugin engine named "lcm"
```

插件引擎**从不自动激活** —— 用户必须显式地把 `context.engine` 设成该插件的名字。

---

## 2. Directory structure —— 目录结构

每个上下文引擎住在 `plugins/context_engine/<name>/` 里：

```
plugins/context_engine/lcm/
├── __init__.py      # exports the ContextEngine subclass
├── plugin.yaml      # metadata (name, description, version)
└── ...              # any other modules your engine needs
```

---

## 3. The ContextEngine ABC —— ContextEngine 抽象基类

你的引擎必须实现这些**必需**方法：

```python
from agent.context_engine import ContextEngine

class LCMEngine(ContextEngine):

    @property
    def name(self) -> str:
        """Short identifier, e.g. 'lcm'. Must match config.yaml value."""
        return "lcm"

    def update_from_response(self, usage: dict) -> None:
        """Called after every LLM call with the usage dict.

        Update self.last_prompt_tokens, self.last_completion_tokens,
        self.last_total_tokens from the response.
        """

    def should_compress(self, prompt_tokens: int = None) -> bool:
        """Return True if compaction should fire this turn."""

    def compress(self, messages: list, current_tokens: int = None,
                 focus_topic: str = None) -> list:
        """Compact the message list and return a new (possibly shorter) list.

        The returned list must be a valid OpenAI-format message sequence.

        ``focus_topic`` is an optional topic string from manual
        ``/compress <focus>``; engines that support guided compression should
        prioritise preserving information related to it, others may ignore it.
        """
```

### 引擎必须维护的类属性（Class attributes your engine must maintain）

agent 直接读取它们用于展示和日志：

```python
last_prompt_tokens: int = 0
last_completion_tokens: int = 0
last_total_tokens: int = 0
threshold_tokens: int = 0        # when compression triggers
context_length: int = 0          # model's full context window
compression_count: int = 0       # how many times compress() has run
```

### 可选方法（Optional methods）

这些方法在 ABC 里都有合理的默认实现。按需覆盖：

| 方法 | 默认 | 何时覆盖 |
|--------|---------|--------------|
| `on_session_start(session_id, **kwargs)` | 空操作 | 你需要加载持久化状态（DAG、DB） |
| `on_session_end(session_id, messages)` | 空操作 | 你需要刷写状态、关闭连接 |
| `on_session_reset()` | 重置 token 计数器 | 你有需要清理的每会话状态 |
| `update_model(model, context_length, ...)` | 更新 context_length + threshold | 你需要在模型切换时重算预算 |
| `get_tool_schemas()` | 返回 `[]` | 你的引擎提供 agent 可调用的工具（例如 `lcm_grep`） |
| `handle_tool_call(name, args, **kwargs)` | 返回错误 JSON | 你实现工具处理器 |
| `should_compress_preflight(messages)` | 返回 `False` | 你能做一次廉价的、API 调用前的估算 |
| `get_status()` | 标准 token/threshold 字典 | 你有要暴露的自定义指标 |
| `select_context(request_messages, *, conversation_messages, incoming_message, budget_tokens)` | 返回 `None`（空操作） | 你选择/路由哪段上下文进入**本次**请求（检索、主题路由）—— 见下 |
| `on_turn_complete(messages, usage=None, **kwargs)` | 空操作 | 你摄取/索引/观测已结束的回合 —— 见下 |

---

## 4. Per-turn context selection and observation —— 每回合的上下文选择与观测

`compress()` 回答的是"上下文太长 → 把它变短"。两个可选、默认空操作的钩子覆盖了与之正交的**选择 / 观测**轴，因此引擎不再需要把 `should_compress()` 强置为 `True`、并把 `compress()` 当作每回合回调来滥用：

```python
def select_context(self, request_messages, *, conversation_messages=None,
                   incoming_message=None, budget_tokens=0):
    """Choose/replace the context for THIS request, before dispatch.

    Return a new message list to use for this one provider call (retrieval,
    topic routing, role/branch switching), or None to leave it unchanged.
    Request-only: the persisted conversation history is never mutated.
    """

def on_turn_complete(self, messages, usage=None, **kwargs):
    """Observe a finished turn after the assistant/tool loop completes.

    Receives a shallow copy of the finalized transcript plus the turn's
    canonical usage dict (or None if no provider response was reached), so the
    engine can ingest/index/summarize for the next select_context(). The return
    value is ignored.
    """
```

契约：

- **默认空操作、失败放行（fail-open）。** 两者默认都 `return None`。缺一个钩子、抛一个异常、或返回一个无效值，都会让请求保持原样 —— 所以一个出故障的引擎**绝不**比不装引擎更糟。宿主还会对继承自 ABC 的默认实现做身份检查并完全跳过它，因此未实现该钩子的引擎（包括内置压缩器）在每次请求上**零**额外开销。
- **`select_context()` 仅限请求（request-only）。** 返回的列表只替换**单次** provider 调用的消息；持久化的历史**永不**被写入。返回 `None`、`[]`、非列表、或含非字典元素的列表，都会失败放行为未修改的请求。
- **次序 / 缓存稳定。** 该钩子在提示词缓存控制（prompt cache-control）**之前**、以及在每一个请求清洗器（sanitizer）之前运行，于是 (a) 一份替换仍要经过与任何请求相同的校验，(b) 默认空操作让请求保持逐字节一致 —— 对未实现的引擎来说，提示词缓存行为不变。一个替换了列表的引擎只改变自己的缓存前缀。按每次 provider 请求求值（重试时会重跑）。
- **`on_turn_complete()`** 只做回合后观测；把 `messages` 当作只读。**覆盖度是尽力而为的：** 它从标准的回合收尾接缝（finalization seam）触发。回环里某些异常的提前返回路径（例如一次内容策略拦截、或一次 provider 的终态失败）会持久化并返回，而不经过收尾，因此它们当前**不会**发出这个钩子 —— 请把它当作对已完成回合的尽力而为观测，而不是对每一次提前退出的保证回调。把所有终态路径统一到一个收尾接缝之下，是一项单独的后续工作。

### 何时用这些钩子、何时不要用（When to use these hooks — and when NOT to）

- **只有当你的引擎必须*替换*每请求上下文时才实现 `select_context()`** —— 检索增强的选择、主题/分支路由、角色切换。它是唯一能换掉"哪些消息进入一次请求"的动词：`pre_llm_call` 插件钩子按**成文设计**是**只注入（inject-only）**的（它追加到用户消息上，绝不重写列表，以保住提示词缓存前缀）。如果你不需要替换，就不要实现它。
- **如果你的插件只需要回合后观测 / 摄取**（索引、内存同步、分析），就实现一个**内存提供者（memory provider）**（`sync_turn()` —— 见[内存提供者插件](./memory-provider-plugin.md)），而不是上下文引擎。上下文引擎取得会话压缩策略的**所有权**；内存提供者观测回合却什么都不拥有。`on_turn_complete()` 作为观测侧镜像存在，是为了那些*已经*需要 `select_context()` 的引擎 —— 让同一个组件能从它刚刚路由过的那一回合中学习 —— 而不是一个通用的回合回调。
- **一次真实的 `select_context()` 对提示词缓存的影响。** 一个非空操作的选择，在它改变选择的那几个回合里，自然会改变提示词缓存前缀 —— 那次请求的前缀不再匹配 provider 已缓存的前缀，于是那几个回合会**重写**缓存而非**读取**缓存。引擎应当在**什么都没变时返回稳定的选择**（同一对象或一个等值列表），只在路由决策确实不同时才重塑上下文；一个每回合都重洗的选择会静默地放弃每一次缓存复用。

---

## 5. Engine tools —— 引擎工具

上下文引擎可以暴露 agent 直接调用的工具。从 `get_tool_schemas()` 返回 schema，并在 `handle_tool_call()` 里处理调用：

```python
def get_tool_schemas(self):
    return [{
        "name": "lcm_grep",
        "description": "Search the context knowledge graph",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"],
        },
    }]

def handle_tool_call(self, name, args, **kwargs):
    if name == "lcm_grep":
        results = self._search_dag(args["query"])
        return json.dumps({"results": results})
    return json.dumps({"error": f"Unknown tool: {name}"})
```

引擎工具在启动时被注入 agent 的工具列表，并自动分发 —— 无需注册表注册。

---

## 6. Registration —— 注册

### 经由目录（推荐）（Via directory (recommended)）

把你的引擎放进 `plugins/context_engine/<name>/`。`__init__.py` 必须导出一个 `ContextEngine` 子类。发现系统会自动找到并实例化它。

### 经由通用插件系统（Via general plugin system）

一个通用插件也可以注册一个上下文引擎：

```python
def register(ctx):
    engine = LCMEngine(context_length=200000)
    ctx.register_context_engine(engine)
```

只能注册一个引擎。第二个试图注册的插件会被拒绝，并给出一个警告。

---

## 7. Lifecycle —— 生命周期

```
1. Engine instantiated (plugin load or directory discovery)
2. on_session_start() — conversation begins
3. update_from_response() — after each API call
4. should_compress() — checked each turn
5. compress() — called when should_compress() returns True
6. on_session_end() — session boundary (CLI exit, /reset, gateway shutdown)
```

`on_session_reset()` 在 `/new` 或 `/reset` 时被调用，用来在不做完整关闭的情况下清理每会话状态。

---

## 8. Configuration —— 配置

用户经由 `hermes plugins` → Provider Plugins → Context Engine 选择你的引擎，或通过编辑 `config.yaml`：

```yaml
context:
  engine: "lcm"   # must match your engine's name property
```

`compression` 配置块（`compression.threshold`、`compression.protect_last_n` 等）是内置 `ContextCompressor` 专属的，有一个明确的例外：`compression.model_thresholds`（逐模型阈值覆盖）属于上下文引擎契约的一部分。宿主在首次 `update_model()` 调用**之前**把解析出来的映射赋给 `engine.model_thresholds`，基类的 `update_model()` 会应用它（最长子串匹配，回退到引擎配置的阈值）。覆盖了 `update_model()` 的引擎拥有自己的压缩策略，可以采纳或忽略这个映射 —— 用 `from agent.context_compressor import resolve_model_threshold` 复用同一套解析逻辑。除这以外，你的引擎如有需要应定义自己的配置格式，在初始化时从 `config.yaml` 里读取。

---

## 9. Testing —— 测试

```python
from agent.context_engine import ContextEngine

def test_engine_satisfies_abc():
    engine = YourEngine(context_length=200000)
    assert isinstance(engine, ContextEngine)
    assert engine.name == "your-name"

def test_compress_returns_valid_messages():
    engine = YourEngine(context_length=200000)
    msgs = [{"role": "user", "content": "hello"}]
    result = engine.compress(msgs)
    assert isinstance(result, list)
    assert all("role" in m for m in result)
```

完整的 ABC 契约测试套件见 `tests/agent/test_context_engine.py`。

---

## 10. Thread safety —— 线程安全

当 `compression.context_timeout_seconds > 0`（默认值）时，Hermes 会把整趟压缩过程 —— 包括你的引擎的 `compress()` 与边界回调，以及任何内存提供者的 `on_pre_compress` / `on_session_switch` —— 放在一个带宿主侧超时的池化守护线程上运行。因此你的引擎必须假定：

- 调用可能到达任意一个池化线程。不要依赖线程亲和性（thread affinity），也不要依赖与对话线程共享的 `threading.local` 状态。
- 你收到的消息列表是一份私有的深度快照；就地修改它是允许的（历史遗留契约），但只有在这趟压缩提交（commit）之后，修改才变得可见。宿主超时之后，你仍在跑的活儿会被丢弃 —— 绝不要在提交之外发布到外部/持久化状态。
- *不同*会话的压缩趟可以在池中的兄弟线程上并发运行；一个跨会话共享的单一引擎/provider 实例必须是线程安全的。

---

## 11. See also —— 另见

- [上下文压缩与缓存（Context Compression and Caching）](/developer-guide/context-compression-and-caching) —— 内置压缩器是怎么工作的
- [内存提供者插件（Memory Provider Plugins）](/developer-guide/memory-provider-plugin) —— 与之类似的、单选（single-select）的内存插件系统
- [插件（Plugins）](/user-guide/features/plugins) —— 通用插件系统总览

---

## 附录 A · 译名对照

| 术语 | 译名 | 首次出现节 |
|---|---|---|
| context engine | 上下文引擎 | §0 |
| plugin engine | 插件引擎 | §1 |
| engine | 引擎 | §1 |
| `ContextCompressor` | `ContextCompressor`（原样，不译） | §1 |
| `ContextEngine` | `ContextEngine`（原样，不译） | §1 |
| ABC (abstract base class) | 抽象基类（ABC） | §1 |
| Lossless Context Management (LCM) | 无损上下文管理（Lossless Context Management，LCM） | §1 |
| knowledge DAG | 知识 DAG | §1 |
| lossy summarization | 有损摘要 | §1 |
| config-driven | 配置驱动 | §1 |
| auto-activated | 自动激活 | §1 |
| directory discovery | 目录发现 | §2 |
| discovery system | 发现系统 | §6.1 |
| `plugin.yaml` | `plugin.yaml`（原样，不译） | §2 |
| required method | 必需方法 | §3 |
| class attribute | 类属性 | §3.1 |
| optional method | 可选方法 | §3.2 |
| default implementation | 默认实现 | §1 |
| no-op | 空操作 | §3.2 |
| per-turn | 每回合 | §4 |
| hook | 钩子 | §4 |
| selection / observation axis | 选择 / 观测轴 | §4 |
| fail-open | 失败放行（fail-open） | §4 |
| identity-check | 身份检查 | §4 |
| request-only | 仅限请求（request-only） | §4 |
| prompt cache-control / prompt cache | 提示词缓存控制 / 提示词缓存 | §4 |
| sanitizer | 清洗器（sanitizer） | §4 |
| cache prefix / cache reuse | 缓存前缀 / 缓存复用 | §4 |
| retrieval | 检索 | §3.2 |
| topic routing | 主题路由 | §3.2 |
| branch switching | 分支切换 | §4 |
| guided compression | 引导式压缩 | §3 |
| inject-only | 只注入（inject-only） | §4 |
| `pre_llm_call` | `pre_llm_call`（原样，不译） | §4 |
| memory provider | 内存提供者 | §4 |
| `sync_turn()` | `sync_turn()`（原样，不译） | §4 |
| finalization seam | 收尾接缝（finalization seam） | §4 |
| best-effort | 尽力而为 | §4 |
| content-policy block | 内容策略拦截 | §4 |
| provider terminal failure | provider 终态失败 | §4 |
| tool schema | 工具 schema | §5 |
| registry registration | 注册表注册 | §5 |
| registration | 注册 | §6 |
| warning | 警告 | §6.2 |
| lifecycle | 生命周期 | §7 |
| session boundary | 会话边界 | §7 |
| gateway shutdown | 网关关闭 | §7 |
| `threshold` | 阈值（threshold） | §8 |
| `model_thresholds` | `model_thresholds`（原样，不译） | §8 |
| per-model threshold overrides | 逐模型阈值覆盖 | §8 |
| longest substring match | 最长子串匹配 | §8 |
| compaction policy | 压缩策略 | §8 |
| thread safety | 线程安全 | §10 |
| pooled daemon thread | 池化守护线程 | §10 |
| host-side timeout | 宿主侧超时 | §10 |
| deep snapshot | 深度快照 | §10 |
| thread affinity | 线程亲和性 | §10 |
| legacy contract | 历史遗留契约 | §10 |
| commit | 提交（commit） | §10 |
| gateway | 网关 | §7 |
| token | token | §4 |

---

## 附录 B · 本篇提到的文件与实测行数

行数一律 `git -C $HOME/.hermes/hermes-agent show f5d1926110:<路径> | wc -l` 实测，路径先经 `git ls-tree -r f5d1926110 --name-only` 确认存在（目录按下挂文件数计）。**实测锚 `f5d1926110`。**

**B-1 · 原文按路径点名的文件与目录：**

| 路径 | 实测 | 角色 |
|---|---|---|
| `agent/context_engine.py` | **242 行** | §1 点名的 `ContextEngine` ABC 所在文件（抽象方法 `:52/:75/:84/:97`） |
| `plugins/context_engine/<name>/` | —（目录约定） | §2 引擎目录约定；锚点下 `plugins/context_engine/` 只有 **1 个文件**（加载器 `__init__.py`），**无**任何示例引擎实例目录 |
| `plugins/context_engine/lcm/` | **不在树中** | §2 示例树的占位示例引擎（`lcm/` 未随仓发出） |
| `config.yaml`（`~/.hermes/config.yaml`） | — | §1/§8 的 `context.engine` 选择 |
| `tests/agent/test_context_engine.py` | **347 行** | §9 "完整的 ABC 契约测试套件" |
| `agent/context_compressor.py`（`agent.context_compressor`） | **5061 行** | §8 `resolve_model_threshold` 所在模块；内置 `ContextCompressor` 的实现 |

**B-2 · 与本章各节符号对应的宿主源码文件（原文按符号/命令点名，落到锚点上的所在）：**

| 文件 | 实测 | 角色 |
|---|---|---|
| `plugins/context_engine/__init__.py` | **93 行** | §2 目录发现：`discover_context_engines()` / `load_context_engine()`；`:3` 逐字 *"only one is active (``context.engine`` in config.yaml; default ``\"compressor\"``…)"* |
| `hermes_cli/plugins.py` | **2147 行** | §6.2 注册入口 `register_context_engine`（`:702`）；"第二个注册被拒绝并警告"（`:705-709`） |
| `agent/agent_init.py` | **2356 行** | §6.1/§7 的配置驱动选择 `_select_context_engine`（`:1761`）、§5 启动时注入工具的 `_inject_context_engine_tools`（`:1948`）；§8 "宿主在首次 `update_model()` 之前赋 `model_thresholds`"（`:1845-1852`） |
| `hermes_cli/plugins_cmd.py` | **2213 行** | §8 `hermes plugins` → "Provider Plugins"（`:1699`）→ "Context Engine"（`:1520`） |
| `hermes_cli/config_defaults.py` | **2922 行** | §1/§8 `context.engine` 默认 `"compressor"`（`:1208`）；§8 `compression.threshold`（`:539`）、`compression.protect_last_n`（`:550`）、`compression.model_thresholds`（`:636`）；§10 `compression.context_timeout_seconds`（`:596`，默认 `120`） |

---

## 附录 C · 上游文档与源码的出入

核对锚：上游 commit `f5d1926110`（v0.21.3）。核对键位：`ContextEngine` ABC 的类属性/方法名、`config.yaml` 的键名字、目录约定、注册入口、`.py` 文件名。绝大多数条目**逐条命中**，另有 **4 处出入**逐条给出，末尾附一处说明。

**C-1 · 已逐条命中的（结论：一致）**

- §1 "`context.engine` 选择插件、同一时刻仅一个活动" → `plugins/context_engine/__init__.py:3` 逐字 *"only one is active (``context.engine`` in config.yaml; default ``\"compressor\"``, the built-in ContextCompressor)"*；`agent/context_engine.py:5` 逐字 *"``context.engine`` selects a plugin (``plugins/context_engine/<name>/``); one is active."*
- §3 必需方法集 `name` / `update_from_response` / `should_compress` / `compress` → `agent/context_engine.py` 的四个 `@abstractmethod`：`name`（`:52`）、`update_from_response`（`:75`）、`should_compress`（`:84`）、`compress`（`:97`）。**方法名逐字一致。**
- §3 必需方法的签名 `name -> str`、`update_from_response(usage)`、`should_compress(prompt_tokens=None)`、`compress(messages, current_tokens=None, …)` → `agent/context_engine.py:52/75/84/97` 逐字对应。
- §7 生命周期六步次序 → `agent/context_engine.py:6-8` docstring 逐字 *"Lifecycle: on_session_start() -> per API response update_from_response() -> per turn should_compress() / compress() -> on_session_end() at real session boundaries only (CLI exit, /reset, gateway expiry), never per-turn."*
- §5 "引擎工具启动时注入工具列表、自动分发、无需注册表注册" → `agent/agent_init.py:1948 def _inject_context_engine_tools(agent):`，同处注释逐字 *"Context engine tool schemas (lcm_*), deduped against existing names"*。
- §6.2 "只能注册一个引擎，第二个被拒绝并警告" → `hermes_cli/plugins.py:703-704` docstring 逐字 *"Register the (single) ``agent.context_engine.ContextEngine`` replacing the built-in ContextCompressor; a second registration is rejected with a warning."*；实现 `:705-708` 逐字 `if self._manager._context_engine is not None:` / `logger.warning("Plugin '%s' tried to register a context engine, but one is already registered. Only one context engine plugin is allowed.", …)`。
- §8 `compression.threshold` / `compression.protect_last_n` → `hermes_cli/config_defaults.py:539` 逐字 `"threshold": 0.50,`、`:550` 逐字 `"protect_last_n": 20,`。
- §8 `compression.model_thresholds` + 宿主在首次 `update_model()` 之前赋给 `engine.model_thresholds` + 基类 `update_model()` 应用它（最长子串匹配）→ `hermes_cli/config_defaults.py:636` 逐字 `"model_thresholds": {},`；`agent/agent_init.py:1845` 逐字注释 *"Per-model overrides BEFORE the initial update_model() so the first threshold resolution already sees them."* 与 `:1847 if cs.model_thresholds:`；`agent/context_engine.py:239-240` 逐字 `self._base_threshold_percent = resolve_model_threshold(` / `model, getattr(self, "model_thresholds", {}), self._config_threshold_percent, provider)`。
- §8 `from agent.context_compressor import resolve_model_threshold` → `agent/context_compressor.py:1674 def resolve_model_threshold(`，docstring `:1677` 逐字 *"Per-model threshold: longest matching ``model_thresholds`` key wins, else ``default``."*（**`.py` 文件名与导入路径逐字一致**）。
- §8 `hermes plugins` → Provider Plugins → Context Engine → `hermes_cli/plugins_cmd.py:1699` 逐字 `"  Provider Plugins"`、`:1520` 逐字 `("Context Engine", "compressor", "compressor", lambda: _get_current_context_engine(),`。
- §9 `tests/agent/test_context_engine.py` 是完整 ABC 契约测试 → 文件在场（**347 行**）。
- §10 "`compression.context_timeout_seconds > 0`（默认）时整趟压缩跑在池化守护线程上" → `hermes_cli/config_defaults.py:596` 逐字 `"context_timeout_seconds": 120,`；`agent/conversation_compression.py:3` docstring 逐字 *"Thread-safety contract for extension points: with ``compression.context_timeout_seconds > 0`` (default) the"*。

**C-2 · 出入（4 条，逐条给 `文件:行号` + 逐字引文）**

**① §1 的 YAML 示例在同一个 `context:` 映射里写了两个 `engine:` 键。**

- 原文第 19–21 行逐字：`  engine: "compressor"    # default built-in` / `  engine: "lcm"           # activates a plugin engine named "lcm"`。
- YAML 语义里同一映射的重复键是**后键覆盖前键**，一个配置文件不可能同时设成 `compressor` 与 `lcm`。
- 源码 `context` 块只有**一个** `engine` 键：`hermes_cli/config_defaults.py:1208` 逐字 `"engine": "compressor",`；读取处 `agent/agent_init.py:1767` 逐字 `_engine_name = _ctx_cfg.get("engine", "compressor") or "compressor"`。
- **结论**：示例应读作"两种取值二选一"，逐字照抄会得到一份歧义（无效）的配置。

**② §3 的抽象方法 `compress()` 只列到 `focus_topic`，源码还多两个参数。**

- 原文第 61–62 行逐字：`def compress(self, messages: list, current_tokens: int = None,` / `focus_topic: str = None) -> list:`。
- 源码 `agent/context_engine.py:97-99` 逐字：`def compress(` / `self, messages: List[Dict[str, Any]], current_tokens: Optional[int] = None,` / `focus_topic: Optional[str] = None, force: bool = False, memory_context: str = "",`。
- 源码 docstring `:105` 逐字 *"Older engines may omit optional parameters — the host filters them by signature."* —— 故此为**口径省略**，非矛盾。

**③ §3.1 "类属性"只列 6 个 token 状态字段，源码另有 4 个压缩参数属性未列。**

- 原文第 78–83 行列 6 个：`last_prompt_tokens` / `last_completion_tokens` / `last_total_tokens` / `threshold_tokens` / `context_length` / `compression_count`。
- 源码 `agent/context_engine.py:56-61` 确有这 6 个，但 `:67-72` 另有 `threshold_percent: float = 0.75`、`protect_first_n: int = 3`、`protect_last_n: int = 6`、`emit_automatic_compaction_status: bool = True`（`:62` 注释逐字 *"Compaction parameters (read by run_agent.py for preflight). protect_first_n counts"*）。
- **结论**：**漏列** 4 个（文档以"agent 直接读取用于展示/日志"为口径，属取舍，但清单不完整）。

**④ §3.2 "可选方法"表是 ABC 默认方法的一个子集，源码还有 5 个默认（非抽象）方法未列。**

- 原文表列 10 项（`on_session_start` / `on_session_end` / `on_session_reset` / `update_model` / `get_tool_schemas` / `handle_tool_call` / `should_compress_preflight` / `get_status` / `select_context` / `on_turn_complete`）。
- 源码 `agent/context_engine.py` 另有：`should_compress_info`（`:87`）、`prune_tool_results_only`（`:109`）、`should_defer_preflight_to_real_usage`（`:159`）、`get_automatic_compaction_status_message`（`:164`）、`has_content_to_compress`（`:175`）—— 均为默认实现、可覆盖。
- **结论**：**漏列** 5 个。

**C-3 · 一处说明（不算出入）**

- §2 目录约定 `plugins/context_engine/<name>/`：锚点下 `plugins/context_engine/` 只随仓发出**一个**文件 —— 加载器 `plugins/context_engine/__init__.py`（93 行），**没有**任何示例引擎实例目录（示例里的 `lcm/` 不在树中；内置 `compressor` 也不在此目录，而在 `agent/context_compressor.py`）。另，`hermes_cli/config_defaults.py:1205-1206` 逐字注释点出引擎也可装在 *"…installed in"* / *"# plugins/context_engine/<name>/ or ~/.hermes/plugins/."*（`~/.hermes/plugins/` 为通用插件目录），而 §2 只写了仓库内约定 —— 属**表述范围**差异，不构成冲突。
