# 源码讲解 · 第 14 章：上下文引擎插件（Context Engine Plugins）

> 维护说明：章节交付时的统计与实测输出保留原口径；当前发布前修复、套件数字与验证收据见《发布前修复记录.md》。mini 代码引用按维护后的实现核对。

> 上游：`官方文档-Developer-Guide/context-engine-plugin.md`（272 行）· 中文精译见 `中文精译-14-ContextEnginePlugin.md`（416 行）。
> 真实侧行号锚 `f5d1926110`（v0.21.3）· mini 侧行号锚当前 HEAD（实现 commit `a6ed019`；本章是**插槽化已有部件**，不是新压缩机制）。
> ★ **引用归属约定**：mini 侧引用一律写成 `mini-hermes/<路径>:<行号>`；**未带 `mini-hermes/` 前缀的裸 `文件:行号` 一律指真实侧**。

[TOC]

---

> 配套《中文精译-14-ContextEnginePlugin.md》。那篇讲**原文说了什么**，这篇讲**Hermes 与 mini 差在哪、为什么这么切**。
> **阅读顺序**：先读「导言 · 机制总览」（只有 Hermes 一侧），再看 §〇 差距总表，最后逐节看四块对照。
> 📌 **两边的锚，别混**：**真实 Hermes 侧**行号锚在上游 commit **`f5d1926110`**（`version = "0.21.3"`），
> 裸写 `文件:行号` 一律指**真实侧**；**mini 侧**行号锚**当前 HEAD**（本章实现提交 `a6ed019`，其后回改文档的 `7b0b18b`），
> mini 侧引用**一律带前缀** `mini-hermes/<路径>:<行号>`。

---

## 导言 · 机制总览：把已有的压缩器插进插槽（先读这一节）

本章**不重述压缩机制**。阈值怎么来、保护头尾怎么划、摘要怎么拼、缓存为什么只让压缩破一次、
微压缩怎么每回合分期付款 —— 这些全在《源码讲解-05-ContextCompressionAndCaching》（§〇 三角色表 / §二~§五 /
§十一）与《源码讲解-10-MicroCompaction》里，此处**只回指，不重复**。

本章只讲**三件新东西**：

1. **契约**（§一 · §三）：抽象面与默认面**各是什么、为什么这么切**；
2. **选择**（§二 · §四）：`config.context.engine` + 目录发现 + 注册 + 回落；
3. **接缝**（§一 · §六）：**唯一构造点**改成查表、零配置逐字节一致。

**★ 先记住这条：第 5 章到今天，同一个部件变了形状。**

> **第 5 章时** mini 的 `ContextEngine` 只有**两个**抽象方法（`should_compress` / `compress`），
> **引擎也只有一种**—— 构造点 `run_agent.py::_compressor()` 里把 `ContextCompressor(...)` **写死**。
> **第 14 章**把**同一个部件**补成**可替换插槽**：抽象面加 `name`、加 `context.engine` 与目录发现、
> 把那个唯一构造点改成**查表**。**压缩逻辑一行没动**——这是判断本章读法对不对的试纸：
> 如果本文读起来像「又一套压缩机制」，那就是读错了。

「**插槽**」= 一个可替换的部件位（宿主要的是「一个能回答*该不该压*、*怎么压*的对象」，不关心它是谁）；
「**接缝**」= 宿主与插槽之间**唯一的**那处连接（这里就是构造点）—— 两者在 §一 展开。

### 0.1 谁在管：一个 4 抽象成员的 ABC，加一个有 15 个默认实现的基类

**接口**是 `class ContextEngine(ABC)`（`agent/context_engine.py:47`，全文件 **242 行**）。
**实现**是内置的 `class ContextCompressor(SummaryDispatchMixin, MicroCompactionMixin, ContextEngine)`
（`agent/context_compressor.py:1782`，全文件 **5,061 行**）。

关键是**契约切得多薄**：整个基类里**只有 4 个抽象成员**是「子类必须实现」——

| # | 抽象成员 | 行 | 逐字 docstring |
|---|---|---|---|
| 1 | `name`（`@property` + `@abstractmethod`） | `:50-53` | *"Short identifier (e.g. 'compressor', 'lcm')."* |
| 2 | `update_from_response(self, usage)` | `:74-81` | *"Update tracked token usage after every LLM call."* |
| 3 | `should_compress(self, prompt_tokens=None) -> bool` | `:83-85` | *"Return True if compaction should fire this turn."* |
| 4 | `compress(self, messages, current_tokens=None, focus_topic=None, force=False, memory_context="")` | `:96-107` | *"…Older engines may omit optional parameters — the host filters them by signature."* |

**其余 15 个**成员（`should_compress_info` `:87-94`、`prune_tool_results_only` `:109-118`、
`select_context` `:120-140`、`on_turn_complete` `:142-153`、`should_compress_preflight` `:155-157`、
`should_defer_preflight_to_real_usage` `:159-162`、`get_automatic_compaction_status_message` `:164-173`、
`has_content_to_compress` `:175-178`、`on_session_start` `:180-181`、`on_session_end` `:183-184`、
`on_session_reset` `:186-201`、`get_tool_schemas` `:203-205`、`handle_tool_call` `:207-210`、
`get_status` `:212-223`、`update_model` `:225-242`）**全部带默认实现**，子类按能力覆盖。

> ★ **抽象面越小，替换成本越低**。这是本章最值钱的一句设计判断：一个只想换压缩策略的人，
> 只要写 4 个东西；`select_context` / 引擎工具 / 生命周期钩子这些正交能力**不实现也合法**，
> 因为基类已经给了 no-op 默认。

**另一个极端形态**：仓库**不内置任何独立引擎**。`plugins/context_engine/` 在 `f5d1926110` 下
**只有加载器 `__init__.py`（93 行）**，零引擎子目录（`git ls-tree -r f5d1926110 -- plugins/context_engine/`
只回一行）。唯一的生产级实现就是内置 `ContextCompressor` 自己 —— **契约与实现分居两处**。

### 0.2 什么时候选：**零配置 = 不走发现**

选择发生在 agent 初始化期，函数是 `agent/agent_init.py:1761 _select_context_engine(_agent_cfg)`。
它的 docstring 就是整条解析链：

> *"Config-driven context engine: ``context.engine`` → plugins/context_engine/<name>/ → general plugin system → None (built-in ContextCompressor)."* — `agent/agent_init.py:1762-1763`

★ **最省事也最重要的一步在 `:1768-1769`**：读到 `"compressor"`（配置默认值）
**直接 `return None`** —— 逐字是

> `if _engine_name == "compressor": return None  # built-in; don't auto-activate plugins`

`return None` 的语义是「用内置，别去碰插件系统」。于是**零配置的一次运行**根本**不列目录、不 import 任何候选**，
返回 `None` 后由 `_build_context_engine`（`agent/agent_init.py:1829-1832`）走内置构造。

配置默认值本身也把这条理由写在注释里（`hermes_cli/config_defaults.py:1204-1208`）：

> `# Context engine — how the context window is managed near the token limit. "compressor" =`
> `# built-in lossy summarization; or a plugin name (e.g. "lcm") installed in`
> `# plugins/context_engine/<name>/ or ~/.hermes/plugins/.`

补一句最容易误解的：`context.engine` 指向的**不是**一条「优先级链」，而是**两个互斥的岔路**——
`"compressor"` 走内置（不发现），别的名字才走发现。**引擎永不自动激活**（原文 §1 明说），
用户不显式写名字，插件就一辈子不会被载入。

### 0.3 做什么：发现 → 加载 → 选择，成败都**有声**

目录加载器只有 93 行（`plugins/context_engine/__init__.py`）：

- 根目录常量 `:15`：`_CONTEXT_ENGINE_PLUGINS_DIR = Path(__file__).parent`；
- `discover_context_engines()` `:18-22`：对每个子目录给出 `(name, description, is_available)`；
- `load_context_engine(name)` `:25-33`：`engine_dir = _CONTEXT_ENGINE_PLUGINS_DIR / name`，不存在回 `None`；
- `_load_engine_from_dir(engine_dir)` `:36-45`：把子目录 `__init__.py` 载入为 `plugins.context_engine.<name>`，
  再按 `register(ctx)` 优先、`base_cls` 子类次之的顺序提取出**一个**实例。

**注册入口**（当引擎走通用插件系统而不是目录时）是
`hermes_cli/plugins.py:702-721::PluginContext.register_context_engine`，docstring 逐字：

> *"Register the (single) ``agent.context_engine.ContextEngine`` replacing the built-in ContextCompressor; a second registration is rejected with a warning."*

「**the (single)**」是硬约束：**同一时刻只能有一个引擎活动**，第二次注册直接**拒绝并警告**。
选择结果最终落在 agent 的一个槽位上（`agent/agent_init.py:1832` `agent.context_compressor = _selected_engine`）。

### 0.4 不变式：三条不能破的规矩

**① 不实现可选钩子的引擎，每回合**零成本**。** 光靠 ABC 的 no-op 默认**不够**——默认方法也是真方法，
调它就要付一次函数调用。所以宿主侧还有一道**身份比较**跳过（`agent/conversation_loop.py:1166-1178`），
docstring 逐字：

> *"Non-implementing engines must pay nothing per turn; ``hasattr`` is not enough because the ABC defines a no-op default. Lazy import avoids a cycle with agent.context_engine."*

实现是 `:1176`：`return getattr(hook, "__func__", None) is not getattr(_CE, name)` ——
**比较函数对象与 ABC 默认是不是同一个**。子类真覆盖了才调，没覆盖直接跳过（§四 展开）。

**② 选择钩子排在缓存与清洗之后之前的位置是**载重**的。** `select_context` 由
`agent/turn_request_assembly.py:142` 调用，**先于**同文件的 `_sanitize_api_messages`（`:148`）
与 `evict_stale_outbound_tool_images`（`:155`）。ABC 的 docstring 把理由写死（`agent/context_engine.py:131-135`）：

> *"The host runs it before prompt cache-control and every request sanitizer, so a malformed replacement never reaches the provider and **the default no-op keeps the request byte-identical**…"*

于是「不实现 = 请求字节不变」是**结构上**成立的，不是靠自觉。

**③ 回落必须**有声**。** 见 0.5。

### 0.5 出错：三条失败路径，全部**回落到内置 + 可读消息**

| 失败 | 处置 | 出处 |
|---|---|---|
| 名字不在目录里 / 目录加载不出来 | 回落内置 + `warning` 日志里带**被请求的名字** | `agent/agent_init.py:1804-1807`，逐字 *"Context engine '%s' not found — falling back to built-in compressor"* |
| 引擎在通用插件系统里注册了、但**无法深拷贝**（持有锁/DB 连接） | 回落内置 + **准确**消息 | `agent/agent_init.py:1785-1802`，逐字 *"…could not be safely copied for this agent (%s) — falling back to built-in compressor. Plugin engines that hold uncopyable state (locks, DB connections) should implement `__deepcopy__`…"*（**#42449**：宁可说清「拷不动」，也不冒充「没找到」） |
| 目录加载抛异常 | 只记 `debug` 日志，继续走插件系统分支 | `agent/agent_init.py:1775-1776` |

**要害**：三条里**没有一条是「拒绝启动」**。一个名字写错不该让 agent 起不来 —— 但**每一条都留了可读痕迹**。
「静默降级」才是这一章最坏的一种：用户以为换了引擎、其实还在用内置。

### 0.6 跨层：它嵌在四条既有线上

- **压缩机制本身**：**本节不重述** —— 阈值三步算术、保护头尾、摘要生成、缓存不变式见
  《源码讲解-05》§〇 / §二~§五 / §十一；微压缩见《源码讲解-10》。本章只保证
  「换一个引擎」不会绕过它们。
- **生命周期**：宿主用一个 seam 驱动引擎的会话迁移，`run_agent.py:362-368`，docstring 逐字
  *"Drive the context engine's session transition: on_session_end → on_session_reset → on_session_start → carry_over_new_session_context. **Each hook is optional** (the built-in compressor only resets)."*（§七 展开）
- **提示词缓存**：选择钩子对缓存的处置见 0.4②，机制本身回指《源码讲解-05》§十。
- **并发**：引擎可能跑在宿主池化线程上，契约逐字在 `agent/conversation_compression.py:1-9`
  （*"…context engines and memory providers included, runs on a pooled daemon thread… shared engines must be thread-safe."*），§十 展开。

### 0.7 时间线：一次启动到一轮请求

```text
[agent 初始化]
   agent_init._select_context_engine(_agent_cfg)          agent/agent_init.py:1761
        ├─ context.engine 缺失 / == "compressor" → return None（★ 不走发现）  :1768-1769
        └─ 否则 → plugins/context_engine/<name>/ 发现 → 通用插件系统 → 都没命中 → 警告 + None  :1772-1807
   agent_init._build_context_engine(...)                  agent/agent_init.py:1829
        └─ 绑定到 agent.context_compressor                     :1832
   ▼
[每一轮请求]
   should_compress(...)                                   ← 多态调用，引擎是谁都一样
   compress(...)                                          ← 契约 §三；机制见《源码讲解-05》
   （可选）select_context / on_turn_complete              ← 默认 no-op + 身份比较跳过（零成本）  conversation_loop.py:1166
   ▼
[会话迁移 / 换模型]
   on_session_end → on_session_reset → on_session_start   run_agent.py:362-368
   update_model(model, context_length, …)                 agent/context_engine.py:225-242
```

### 验收四问

1. **一个引擎最少要实现几个成员？** 4 个：`name`（`agent/context_engine.py:50-53`）、`update_from_response`（`:74`）、
   `should_compress`（`:83`）、`compress`（`:96`）。其余 15 个都有默认实现，不写也合法。（§0.1 · §三）
2. **不写 `context.engine` 会怎样？** 走 `"compressor"` 默认值，`_select_context_engine` **直接 `return None`**，
   **一步发现都不做**，用内置压缩器 —— 这就是「零配置 = 与改造前逐字节一致」的载体。（§0.2 · §一）
3. **仓库里到底有没有内置的独立引擎？** 没有。`plugins/context_engine/` 只有加载器 `__init__.py`（93 行）、
   **零引擎目录**；唯一生产实现是内置 `ContextCompressor`（5,061 行）自己。（§0.1 · §0.3 · §二）
4. **不实现可选钩子的引擎，每回合付多少代价？** 零。宿主用 `hook.__func__ is not ABC.method` **身份比较**跳过
   （`agent/conversation_loop.py:1166-1178`）—— 不是靠 `hasattr`，因为 ABC 给的 no-op 默认也是真方法。（§0.4）

---

## §〇 与真实版的差距总表

**一句话点题**：本章 = 把 mini **已有的**压缩器**插槽化**（补 `name`、加选择键、加目录发现、把唯一构造点改查表），
**不是新增一套压缩机制**。压缩本身（阈值 / 保护头尾 / 摘要 / 缓存不变式 / 微压缩）**一行未改**，
详见《源码讲解-05》与《源码讲解-10》——本表只列**插槽层**的差。

**再放一次那句口径**（与导言那条同义）：第 5 章时 mini 的 `ContextEngine` 只有**两个**抽象方法、引擎只**一种**、
构造点**写死**；第 14 章把**同一个部件**补成可替换插槽 —— 抽象面加 `name`、加 `context.engine` 与目录发现、
唯一构造点改成查表。

「mini 现状」列四档：`✅ 已抄（a6ed019）` / `已对齐` / `⬜ 没做（量级）` / `⏭ 不抄（理由）`。

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| **抽象面（必须实现）** | **4** 个：`name` / `update_from_response` / `should_compress` / `compress`（`agent/context_engine.py:50-53,74-81,83-85,96-107`） | ✅ 已抄（`a6ed019`）：**3** 个（`name` / `should_compress` / `compress`，`mini-hermes/agent/context_engine.py:47-49,68-69,76-77`，其中 `name` 是第 14 章新补）；记账降为可选 |
| **默认面（可选钩子）** | **15** 个带默认实现（`agent/context_engine.py:87-242`） | 已对齐**口径**、⬜ 没做（量级）：mini ABC 只有 4 个可选（`update_usage` / `tools` / `describe` / `update_model`，`mini-hermes/agent/context_engine.py:85-111`），其中 **2 个**与真实侧默认钩子对得上（`describe`↔`get_status`、`update_model`↔`update_model`）；`tools()` 只是**占位钩子**（真实侧的 `get_tool_schemas`/`handle_tool_call` 需要注入路径，mini 没建 ⇒ 归入「没做」）；另 **13 个**（生命周期 3 / 选择观测 2 / 引擎工具 2 / 预检 2 / 其余 4）在 mini 无消费者 |
| **类属性即状态契约** | 6 个 token 字段 + 4 个参数（`agent/context_engine.py:55-72`，注释逐字 *"engines MUST maintain these; run_agent.py reads them directly."*） | ⬜ 没做（形态不同）：mini 不把这些放 ABC；实例提供时展示 token，未提供时调用 `describe()`（`mini-hermes/run_agent.py:1735-1738`） |
| **`name` 是必需项** | `@property` + `@abstractmethod`（`agent/context_engine.py:50-53`） | ✅ 已抄（`a6ed019`）：同为 `@property` + `@abstractmethod`（`mini-hermes/agent/context_engine.py:47-49`）；内置实现返回 `"compressor"`（`mini-hermes/agent/context_compressor.py:624-633`） |
| **选择键** | `context.engine`，默认 `"compressor"`（`hermes_cli/config_defaults.py:1207-1208`） | ✅ 已抄（`a6ed019`）：同名同默认（`mini-hermes/hermes_cli/config.py:83-84,93`）+ 读取函数 `get_context_engine_name()`（`:340-353`） |
| **零配置 = 不走发现** | `if _engine_name == "compressor": return None`（`agent/agent_init.py:1768-1769`） | ✅ 已抄（`a6ed019`）：`select_engine` 见默认名**直接 `default_factory()`**、不进扫描（`mini-hermes/agent/engine_registry.py:315-318`） |
| **目录发现** | 有独立加载器 `plugins/context_engine/__init__.py`（93 行）：`discover` / `load` / `_load_engine_from_dir` | ✅ 已抄（`a6ed019`）：同形三件套 `discover_engines` / `read_engine` / `engines_dir`（`mini-hermes/agent/engine_registry.py:148-271`） |
| **发现根的位置** | 仓库相对 `plugins/context_engine/<name>/`（`plugins/context_engine/__init__.py:15`） | 已对齐形状、**位置本地化**：`<MINI_HOME>/engines/<name>/`（`mini-hermes/agent/engine_registry.py:148-155`）—— 与 `skills_dir()` 同构、单根、每 profile 一套 |
| **注册入口** | `register_context_engine`（`hermes_cli/plugins.py:702-721`），「the (single)」+ 第二次注册拒绝 | ⏭ 不抄（理由）：mini 没有通用插件系统 / `ctx` / manager，抄了是没有消费者的死代码（清单 B1）；mini 只有「目录」这一条注册路径 |
| **备用发现根** | `~/.hermes/plugins/`（注释逐字 `config_defaults.py:1206`） | ⏭ 不抄（理由）：第二个根会让「引擎装哪儿」有两个答案，且测试要钉两处；mini 硬约定「路径唯一来源」（`mini-hermes/hermes_cli/config.py:110-112` 逐字 *"别处不许硬编码"*） |
| **唯一构造点 = 接缝** | `_build_context_engine`（`agent/agent_init.py:1829-1832`）绑定选中的实例 | ✅ 已抄（`a6ed019`）：`mini-hermes/run_agent.py::_compressor():1590-1627` 改**查表**；旧构造**逐字**搬进 `_build_default_compressor()`（`:1570-1588`），其余调用点**一行未改** |
| **回落语义** | 未知名 → 内置 + warning 带名字（`agent/agent_init.py:1804-1807`） | ✅ 已抄（`a6ed019`）：`EngineSelection.warning` 非空、经 `_emit_engine_warning` 打印（`mini-hermes/run_agent.py:1620-1624`；警告文案 `mini-hermes/agent/engine_registry.py:329-333`） |
| **深拷贝单例防护** | `copy.deepcopy(_candidate)` 防子 agent 改父预算，拷不动 → 内置 + 准确消息（`agent/agent_init.py:1785-1802`，**#42449**） | ⏭ 不抄（理由）：mini 每次发现**真读盘、每次新实例**（`mini-hermes/agent/engine_registry.py:245-271`），没有共享单例可防 |
| **每回合选择 / 观测钩子** | `select_context`（`agent/turn_request_assembly.py:142`）+ `on_turn_complete`（`agent/turn_finalizer.py:541`） | ⏭ 不抄（理由）：mini 的 agent loop **没有**「每请求替换上下文」「回合后摄入」的消费者（只超阈值压缩 + 每回合微压缩）；留到真有检索/主题路由时再加 |
| **零成本钩子的手法** | `hook.__func__ is not ABC.method` **身份比较**（`agent/conversation_loop.py:1166-1178`） | ⏭ 不抄（**机制**在文中讲，见 §0.4 / §四）：mini 没有「钩子挂在热路径、非实现者必须零成本」这条性能约束的对象 |
| **引擎工具注入** | `get_tool_schemas` / `handle_tool_call` + `_inject_context_engine_tools` 按 `enabled_toolsets` 门控、去重（`agent/agent_init.py:1948-1990`） | 已对齐**钩子形状**、⬜ 没做（注入路径）：mini ABC 有 `tools()` 占位（`mini-hermes/agent/context_engine.py:95-97`，默认 `[]`），但不建注入（无引擎带工具可测） |
| **线程安全契约** | 池化 daemon 线程 / 深快照 / 只在 commit 发布 / 每会话一 pass（`agent/conversation_compression.py:1-9`） | ⏭ 不抄（理由）：mini 的压缩**同步、内联**跑在 `_preflight_compression`（`mini-hermes/run_agent.py:1724-1763`），无线程池；写这种契约会变成「没人验证的注释」（与 `mini-hermes/agent/context_compressor.py` 自记的「注释里点名不存在的开关」同一个病） |
| **仓库是否内置独立引擎** | **否**：`plugins/context_engine/` 只有 `__init__.py`（93 行）、零子目录 | 已对齐（结构同）：`<MINI_HOME>/engines/` 由用户放目录；内置压缩器住 `mini-hermes/agent/context_compressor.py`，**不经**发现路径 |
| **每模型阈值表** | `update_model` 重算 + 每模型阈值（最长子串匹配）+ 配置 percent 只快照一次（`agent/context_engine.py:225-242`） | ⏭ 不抄（理由）：mini 只有一个模型（`DEFAULT_CONTEXT_LENGTH` + 环境变量口子），逐模型覆盖表没有第二个键可覆盖 |

> **读法**：这张表里**只有「抽象面 / 默认面 / 选择键 / 零配置不走发现 / 目录发现 / 唯一构造点 / 回落有声」**
> 是本章真正动过的东西；其余 `⏭` 行是**结构性不抄**（没有消费者），`⬜` 行是**量级**（真实侧有 15 个钩子，
> mini 只有 4 个，差的都是 mini 不用的能力）。

---

## §一 插槽的工作原理（How it works）

**① Hermes 怎么做的**：上下文管理是**有损**的 —— 压缩一定丢东西。而「什么该留」**强依赖场景**：
代码仓库要留文件路径和命令，长对话要留用户原话，法律文档要留条款编号。Hermes 没法替所有人决定，
于是把这**决策**抽成一个可替换接口：agent 只需要「一个能回答*该不该压*、*怎么压*的对象」，
不需要知道它是谁。**不做这件事会坏在哪**：换策略 = 改宿主代码 —— 每换一种压缩思路，
就要动一遍 agent 初始化、构造参数、阈值来源；一旦有多处构造点，还会出现「改了 A 忘改 B」的静默不一致。
把可替换位收敛成**一个接口 + 一处接缝**，就把「换策略」的成本压到「放一个目录」。

三条关键判据（都写在原文 §1，源码也照此实现）：

| 判据 | 含义 | 别踩的坑 |
|---|---|---|
| **同一时刻只能一个引擎活动** | 选择结果落在**单个**槽位（`agent/agent_init.py:1832`） | 不是「优先级链」；第二次注册被**拒绝 + 警告**（`hermes_cli/plugins.py:703-704`） |
| **引擎永不自动激活** | 用户必须显式写 `context.engine` 的名字 | 名字的**默认值 `"compressor"` 恒指内置**，不等于「某个插件」 |
| **零配置 = 逐字节一致** | 不写 `context` 段 ⇒ 与改造前完全同路径 | 若默认路径也走发现，改造就把「确定性」交了出去 |

**② 读 Hermes 的代码** —— `agent/agent_init.py:1761-1769`（选中/不选中的**全部**判据就在这 9 行里）：

```
def _select_context_engine(_agent_cfg):
    """Config-driven context engine: ``context.engine`` → plugins/context_engine/<name>/ →
    general plugin system → None (built-in ContextCompressor)."""
    _engine_name = "compressor"
    with suppress(Exception):
        _ctx_cfg = _agent_cfg.get("context", {}) if isinstance(_agent_cfg, dict) else {}
        _engine_name = _ctx_cfg.get("engine", "compressor") or "compressor"
    if _engine_name == "compressor":
        return None  # built-in; don't auto-activate plugins
```

**要害**：整条链的**第一道分支**不是「找插件」，而是「**别找**」。`return None` 就是「内置」这个答案本身 ——
它把「零配置」从一条文档承诺变成一行**提前返回**；只有名字真不是 `"compressor"` 时，后面那 40 行发现逻辑才会跑。
读这一章所有「默认」相关断言（I4 逐字节一致、默认路径安静）时，根都在这一行。

**③ mini 的实现（现状）**：✅ 已抄（`a6ed019`）。选择逻辑住在新建的
`mini-hermes/agent/engine_registry.py`（335 行），只保留**一条**发现路径（`<MINI_HOME>/engines/<name>/`）
+ 一个选择函数 `select_engine()`（`:291-335`）；宿主那处**唯一构造点**改成查表，
`mini-hermes/run_agent.py:1590-1627`：

```python
def _compressor(self) -> "Any":
    c = getattr(self, "_compressor_obj", None)
    if c is None:
        from agent.engine_registry import select_engine
        from hermes_cli.config import get_context_engine_name
        selection = select_engine(get_context_engine_name(),
                                  default_factory=self._build_default_compressor)
        if selection.warning:                       # ★ 回落必须有声
            self._emit_status(selection.warning)
        c = selection.engine
        self._compressor_obj = c
    return c
```

旧构造**逐字**搬进 `_build_default_compressor()`（`mini-hermes/run_agent.py:1570-1588`，参数与顺序一个没动）——
这就是「零配置 = 与改造前逐字节一致」（不变量 I4）的载体；`default_factory` 是**注入**的必填关键字，
避免注册表反过来 `import ContextCompressor` 造成**第二个构造点**。其余调用点
（`should_compress` / `compress` / `micro_compact`）**一行未改** —— 它们本来就是多态调用。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 选择入口 | `agent/agent_init.py:1761 _select_context_engine` | 同形，落在 `mini-hermes/run_agent.py:1590 _compressor()` + `mini-hermes/agent/engine_registry.py::select_engine():291` |
| 解析链 | `context.engine` → 目录 → 通用插件系统 → 内置（4 段） | 只有 **2 段**：`context.engine` → 目录 → 内置（mini 无插件系统） |
| 返回形态 | `None` 表示内置、否则返回实例 | 结构化 `EngineSelection(engine, requested, used, fell_back, warning)`（`mini-hermes/agent/engine_registry.py:124-144`）—— 让「回落」能被调用方**读出来并打印** |
| 接缝数 | 构造点 + 生命周期 seam + 每回合钩子 | **1 处**（`_compressor()`）；生命周期/每回合钩子 mini 未接 |

---

## §二 目录结构（Directory structure）

**① Hermes 怎么做的**：每个引擎**住在一个目录里**（原文 §2）：

```
plugins/context_engine/lcm/
├── __init__.py      # exports the ContextEngine subclass
├── plugin.yaml      # metadata (name, description, version)
└── ...              # any other modules your engine needs
```

**为什么是目录而不是配置文件登记**：目录结构本身就是注册表 ——
**新增一种引擎 = 放一个目录，宿主一行不改**。目录名就是引擎名，`__init__.py` 负责导出 ABC 子类，
`plugin.yaml` 只放元数据（不参与选择）。**不做这件事会坏在哪**：如果引擎必须显式登记在宿主某处，
«插件» 就又变回了 «改宿主代码»，插槽的意义归零。目录式还顺手给了两条免费性质：
**隔离**（每个引擎自带模块空间）与**可发现**（列出根目录就知道装了哪些）。

**② 读 Hermes 的代码** —— `plugins/context_engine/__init__.py:25-33`（加载语义全在这 9 行）：

```
def load_context_engine(name: str) -> Optional["ContextEngine"]:  # noqa: F821
    """Load a ContextEngine instance by name; None if not found or it fails to load."""
    engine_dir = _CONTEXT_ENGINE_PLUGINS_DIR / name
    if not engine_dir.is_dir():
        logger.debug("Context engine '%s' not found in %s", name, _CONTEXT_ENGINE_PLUGINS_DIR)
        return None
    return _loader.load_named(
        name, engine_dir, _load_engine_from_dir, kind="Context engine", noun="engine", logger=logger
    )
```

**要害**：加载器是**薄**的 —— 它只做「拼路径 → 判目录在不在 → 交给通用 loader」。
真正决定「一个目录里的哪样东西算引擎」的是 `_load_engine_from_dir`（`:36-45`）：import 后再按
`register(ctx)` 优先、`base_cls` 子类次之的顺序**提取一个**实例。**「目录名 == 引擎名」不是约定，是地址**：
`_CONTEXT_ENGINE_PLUGINS_DIR / name` 就是这个引擎的**唯一**定位方式。

**③ mini 的实现（现状）**：✅ 已抄（`a6ed019`）。形状对齐、**位置本地化**为
`<MINI_HOME>/engines/<name>/__init__.py`（`mini-hermes/agent/engine_registry.py:148-155`）：

```python
def engines_dir() -> Path:
    """引擎库根目录：``<MINI_HOME>/engines``（**唯一来源**）。"""
    return get_mini_home() / "engines"
```

选 `get_mini_home()/…`（而不是仓库相对路径）有三条理由，注释里逐条照抄第 13 章 `skills_dir()` 那条：
① profile 就是 home，一套引擎天然隔离；② 测试/E2E 只靠 `MINI_HOME` **一条路**重定向，不会去读用户真实目录；
③ mini 没有仓库级安装布局，照抄 `plugins/` 会产生「装了但无处发现」的路径。
**契约校验**（`read_engine():212-242`）比上游严格：软链目录跳过、**目录名不得是保留名 `compressor`**、
`__init__.py` 里必须**本目录定义**（不接受转发导入）ContextEngine 子类、必须能**无参实例化**、
实例 `name` 必须**等于目录名** —— 任意一条不满足 ⇒ **跳过并计数**，带可读原因，**永不抛**。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 根目录 | `plugins/context_engine/<name>/`（仓库相对，`__init__.py:15`） | `<MINI_HOME>/engines/<name>/`（HOME 锚定，`mini-hermes/agent/engine_registry.py:148-155`） |
| 目录内容 | `__init__.py` + `plugin.yaml`（元数据）+ 任意模块 | 只认 `__init__.py`（单文件约定，`mini-hermes/agent/engine_registry.py:29-30,183-209`）；无 `plugin.yaml` |
| 保留名 | 无显式保留名（内置压缩器**不在**这个目录里，所以不冲突） | 显式保留 `"compressor"`：占住它的目录被跳过（`mini-hermes/agent/engine_registry.py:225-228`），避免 `select_engine("compressor")` 有两个答案 |
| 坏目录处置 | `logger.debug` 一行；不计数 | 跳过 + **计数 + 可读原因**（`SkippedEngine`，`mini-hermes/agent/engine_registry.py:108-121`），与 `discover_skills` 同口径 |

---

## §三 ContextEngine 抽象基类（The ContextEngine ABC）

ABC 是**契约**那一面：它规定「一个引擎**最少**要提供什么」，其余全是**可选能力**。
整节只有一条设计主线：**把必须实现的压到最小，把能省的都变成有默认的 no-op** ——
因为插槽的价值取决于「实现一个插件有多便宜」。

### 必需方法（抽象面 4）

**① Hermes 怎么做的**：整个基类只有 **4 个**成员带 `@abstractmethod`，**其余 15 个都有默认实现**。
这 4 个不是随便挑的：它们正好覆盖「**自报家门**（`name`）+ **记账**（`update_from_response`）+
**判据**（`should_compress`）+ **动作**（`compress`）」—— 少任何一个都构不成一个完整引擎；
多一个都会抬高替换成本。`name` 被做成 `@property` + `@abstractmethod`（而不是普通类属性），
理由是「**选择键不能靠派生**」：名字是靠 `context.engine` 与目录**寻址**用的键，靠类名派生意味着改一次类名就
**静默改掉**用户的选择。

**② 读 Hermes 的代码** —— `agent/context_engine.py:50-53` + `:96-100`（签名两端）：

```
    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (e.g. 'compressor', 'lcm')."""
...
    @abstractmethod
    def compress(
        self, messages: List[Dict[str, Any]], current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None, force: bool = False, memory_context: str = "",
    ) -> List[Dict[str, Any]]:
```

**要害**：`compress` 的**可选参数**（`focus_topic` / `force` / `memory_context`）不是给插件出难题的，
docstring 明说 *"Older engines may omit optional parameters — the host filters them by signature."* ——
**宿主按签名过滤**：引擎只签它认得的，多传的关键字宿主自己滤掉。于是「契约窄」与「宿主想加能力」不冲突：
窄签名是**最小义务**，可选参数是**加演**。（mini 的对应口径见 §四。）

**③ mini 的实现（现状）**：✅ 已抄（`a6ed019`）。mini 的 ABC 现在是 **3 个抽象成员**（1 个抽象属性 + 2 个抽象方法），
`mini-hermes/agent/context_engine.py:42-81`：

```python
class ContextEngine(ABC):
    @property                                    # ★ 第 14 章补
    @abstractmethod
    def name(self) -> str: ...
    @abstractmethod
    def should_compress(self, messages) -> bool: ...
    @abstractmethod
    def compress(self, messages) -> List[Dict[str, Any]]: ...
```

**注意签名**：mini 的 `compress` **刻意窄**——只签 `(self, messages)`（不带 `focus_topic` /
`force` / `memory_context`）。宿主用 `inspect.signature` 过滤 `call_llm` / `call_llm_meta`：
只把引擎声明能接的关键字传过去；接受 `**kwargs` 的引擎可以收到两项。这是「最小义务 + 宿主过滤」：
窄签名是必须的，宿主多传的是可选能力。未实现抽象方法 ⇒ **实例化即 `TypeError`**（不变量 I3，守它的是
`tests/test_context_engine.py`，`mini-hermes/tests/test_context_engine.py:205-250` 一带）。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 抽象成员数 | **4**（`agent/context_engine.py:50,74,83,96`） | ✅ 已抄：**3**（`name` @ `mini-hermes/agent/context_engine.py:47-49` · `should_compress` @ `:68-69` · `compress` @ `:76-77`）—— 记账 `update_usage` 降为**可选**（真实侧是必需的那个 `update_from_response`） |
| `name` 形态 | `@property` + `@abstractmethod`（必需） | ✅ 已抄：同（`mini-hermes/agent/context_engine.py:47-49`） |
| `compress` 签名 | `(messages, current_tokens, focus_topic, force, memory_context)` | 已对齐口径、**更窄**：`(self, messages)`（`mini-hermes/agent/context_engine.py:76-77`）；差异在「宿主过滤」保留 |
| 记账方法名 | `update_from_response(usage)`（**抽象**） | 名字不同：`update_usage(usage, messages)`（**默认空操作**，`mini-hermes/agent/context_engine.py:85-91`）——mini 可选而非必需 |

### 引擎必须维护的类属性

**① Hermes 怎么做的**：ABC 不只规定**方法**，还规定**类属性** —— 因为宿主**直接读**它们，不经方法。
逐字注释把这条讲得最清楚：*"Token state: engines MUST maintain these; run_agent.py reads them directly."*
也就是说，这些字段是**接口**，不是内部状态。分两组：

| 组 | 字段 | 默认 | 出处 |
|---|---|---|---|
| **token 状态**（6 个，宿主展示/日志直接读） | `last_prompt_tokens` · `last_completion_tokens` · `last_total_tokens` · `threshold_tokens` · `context_length` · `compression_count` | 全 `= 0` | `agent/context_engine.py:55-61` |
| **压缩参数**（4 个，宿主预检读） | `threshold_percent` · `protect_first_n` · `protect_last_n` · `emit_automatic_compaction_status` | `0.75` · `3` · `6` · `True` | `:62-72` |

★ 注意这 4 个参数**文档没提**（原文 §3 只列了 6 个 token 字段），但它们是**必修类属性**：
`protect_first_n` 的逐字注释解释了「头几位」的口径（*"…non-system head messages kept verbatim IN ADDITION to
the always-protected system prompt (3 keeps the historical head shape)."*），
`emit_automatic_compaction_status` 解释了「静音」只静音**成功**的后台维护（*"False keeps successful automatic
compaction passes silent (routine background maintenance); warnings, errors and manual /compress still surface."*）。
另外，ABC 上的 `threshold_percent=0.75` / `protect_last_n=6` 与内置实现 `__init__` 的默认（`0.50` / `20`）
**不一样** —— ABC 的值是「任何引擎都得有个合理的预检默认」，不是内置实现的取值（详见《源码讲解-05》§一）。

**② 读 Hermes 的代码** —— `agent/context_engine.py:55-72`（含两条逐字注释）：

```
    # Token state: engines MUST maintain these; run_agent.py reads them directly.
    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    last_total_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 0
    compression_count: int = 0
    # Compaction parameters (read by run_agent.py for preflight). protect_first_n counts
    # non-system head messages kept verbatim IN ADDITION to the always-protected system
    # prompt (3 keeps the historical head shape).
    # ...
    threshold_percent: float = 0.75
    protect_first_n: int = 3
    protect_last_n: int = 6
    # False keeps successful automatic compaction passes silent (routine background
    # maintenance); warnings, errors and manual /compress still surface.
    emit_automatic_compaction_status: bool = True
```

**要害**：「宿主直接读」这五个字决定了一件事 —— **换引擎后宿主代码不用改**。因为宿主读的是**属性**，
不管引擎内部把这状态算在哪；接口稳定，实现随便换。

**③ mini 的实现（现状）**：mini 的 ABC **不声明**这些类属性，状态展示是可选能力：
`should_compress` 返回 `False` 时，宿主先确认实例有对应能力，再读两个字段，`mini-hermes/run_agent.py:1735-1738`：

```python
if not c.should_compress(messages):
    if callable(getattr(c, "current_tokens", None)) and hasattr(c, "threshold_tokens"):
        used, src = c.current_tokens(messages)
        self._emit_status(f"上下文 {used} token（{src}），未到阈值 {c.threshold_tokens}，不压缩")
    else:
        self._emit_status(c.describe())
```

`threshold_tokens` 在内置实现里是个 **property**（三步算术，`mini-hermes/agent/context_compressor.py:526-546`），
`name` 也是 property（`:624-633`）—— **能力在、只是不在 ABC 上声明**。这条差异的代价要说清：
mini 不要求所有引擎都有这些展示字段；缺少它们时使用 ABC 自带的 `describe()`。
只实现 `name` / `should_compress` / `compress` 的最小引擎，也能通过不压缩和触发压缩两条普通路径。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| token 状态字段 | 6 个**类属性**，注释「engines MUST maintain these; run_agent.py reads them directly」（`agent/context_engine.py:55-61`） | ⬜ 没做（量级）：实例提供（`threshold_tokens` property，`mini-hermes/agent/context_compressor.py:526-546`），ABC 不声明 |
| 压缩参数默认 | `threshold_percent=0.75` / `protect_first_n=3` / `protect_last_n=6` / `emit_…=True`（`:62-72`） | ⬜ 没做：mini 的阈值/保护参数从 `compression.*` 配置构造注入（《源码讲解-05》§三），ABC 无一字 |
| 宿主读取方式 | 直接读引擎属性 | 宿主读实例（`mini-hermes/run_agent.py:1735-1738`）—— token 展示可选，最小引擎不需要额外属性 |
| 一处「文档漏了」 | 文档 §3 只列 6 个 token 字段，**漏了 4 个压缩参数**（附录 C 记一条） | —（mini 两处都没有，不适用） |

### 可选方法（默认面 15）

**① Hermes 怎么做的**：`compress` + `should_compress` 只覆盖「**太长 → 变短**」这条轴。
真实侧另有 **15 个**带默认实现的成员，覆盖**正交**能力，不实现就白拿 no-op：

| 能力 | 方法（默认） | 行 |
|---|---|---|
| 带原因的判据 | `should_compress_info`（`(bool, None)`） | `:87-94` |
| 低成本确定性剪枝 | `prune_tool_results_only`（`(messages, 0)`） | `:109-118` |
| **请求级替换**上下文 | `select_context`（`None`） | `:120-140` |
| **回合后观测** | `on_turn_complete`（`None`） | `:142-153` |
| 预检（API 前估算） | `should_compress_preflight`（`False`） | `:155-157` |
| 粗估是否等真实读数 | `should_defer_preflight_to_real_usage`（`False`） | `:159-162` |
| 自动压缩状态文案 | `get_automatic_compaction_status_message` | `:164-173` |
| 有无可压内容 | `has_content_to_compress`（`True`） | `:175-178` |
| 会话开始 / 结束 / 复位 | `on_session_start` / `on_session_end` / `on_session_reset` | `:180-201` |
| **引擎自带工具** | `get_tool_schemas`（`[]`）/ `handle_tool_call`（错误 JSON） | `:203-210` |
| 状态诊断 | `get_status`（标准 token/threshold dict） | `:212-223` |
| 模型切换重算 | `update_model`（重算 `threshold_tokens`） | `:225-242` |

**为什么要给默认**：这些能力的**消费方各不相同**（宿主预检、会话生命周期、请求组装、工具注入）——
让每个引擎都必须实现它们，等于强制一个「只换压缩策略」的人去写一堆 `return None`。
默认实现让**未实现 = 合法**。配合 0.4 的**身份比较**跳过，未实现的钩子在热路径上**零开销**：
这正是原文 `When to use these hooks — and when NOT to` 一节的源码依据（在 §四 展开）。

**② 读 Hermes 的代码** —— `agent/context_engine.py:116-140`（`select_context` 的默认，两端）：

```
    def select_context(
        self, request_messages: List[Dict[str, Any]], *, conversation_messages: List[Dict[str, Any]] = None,
        incoming_message: Dict[str, Any] = None, budget_tokens: int = 0,
    ) -> List[Dict[str, Any]]:
        """Optionally *select* (replace) the context for THIS request, pre-generation.
        ...
        The returned list is request-only — it MUST NOT be treated as persisted transcript state
        (session DB history is untouched)…
        """
        return None
```

**要害**：默认实现就是**一行 `return None`** —— 但语义很重：**请求级替换**（不是持久历史改写），
且默认 no-op 保证**请求字节不变**（配合 0.4② 的顺序）。这解释了为什么它敢挂在一个「每请求都跑」的位置上：
不实现 = 不改一个字节；实现了才动。**能力与代价成正比 —— 而代价的开关握在引擎作者手里（实现才付、不实现不付），不在宿主手里。**

**③ mini 的实现（现状）**：已对齐**口径**、⬜ 没做（量级）。mini 的 ABC 只有 **4 个**默认实现：

| mini 可选方法 | 行 | 默认 | 与真实侧对应 |
|---|---|---|---|
| `update_usage(usage, messages)` | `mini-hermes/agent/context_engine.py:85-91` | 空操作 | ↔ `update_from_response`（真实侧是**必需**） |
| `tools()` | `:95-97` | `[]` | ↔ `get_tool_schemas` |
| `describe()` | `:101-103` | `class.__name__` | ↔ `get_status`（形态不同：一行人话 vs 结构化 dict） |
| `update_model(model, provider="")` | `:107-111` | 空操作 | ↔ `update_model` |

差的 **13 个**（真实 15 个默认实现 − 对得上的 2 个）**在 mini 里都没有消费者**，逐一点名：
**生命周期三项**（`on_session_start` / `on_session_end` / `on_session_reset`）、
**选择观测两项**（`select_context` / `on_turn_complete`）、**引擎工具两项**（`get_tool_schemas` / `handle_tool_call` 及其注入路径）、
**预检两项**（`should_compress_preflight` / `should_defer_preflight_to_real_usage`）、
**其余四项**（`should_compress_info` / `prune_tool_results_only` / `get_automatic_compaction_status_message` / `has_content_to_compress`）（mini 的 agent loop 不做请求级替换、不做回合后摄入、没有插件宿主）——
按「不做投机性基础设施」的口径，**没有实现就没有钩子**，而不是先摆一堆空壳。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 默认面规模 | **15** 个默认实现（`agent/context_engine.py:87-242`） | ⬜ 没做（量级）：**4** 个（`mini-hermes/agent/context_engine.py:85-111`） |
| 记账方法归属 | `update_from_response` 是**必需**（`:74-81`） | 名字不同、**可选**：`update_usage`（`mini-hermes/agent/context_engine.py:85-91`） |
| 请求级替换 / 回合观测 | `select_context` / `on_turn_complete` 默认 no-op（`:120-153`） | ⏭ 不抄（理由）：mini 无消费者（见 §〇 同名列） |
| 引擎工具 | `get_tool_schemas` / `handle_tool_call` + 注入 | 已对齐**钩子**：`tools()`（`mini-hermes/agent/context_engine.py:95-97`）；⬜ 没做**注入路径** |
| 诊断形态 | `get_status()` 返回标准 token/threshold dict（`:212-223`） | 形态不同：`describe()` 返回一行人话（`mini-hermes/agent/context_engine.py:101-103`） |

## §四 每回合的上下文选择与观察（Per-turn context selection and observation）

`compress()` 回答的是「上下文太长 → 把它变短」（机制见《源码讲解-05》§〇~§五、《源码讲解-10》）。本节讲的是与它**正交**的另外两根轴：**每请求的选择（`select_context`）** 与 **回合后的观测（`on_turn_complete`）**。两个钩子都在抽象基类(ABC)上、都默认空操作，所以引擎不必再把 `should_compress()` 强置为 `True`、把 `compress()` 当每回合回调来滥用。

**① Hermes 怎么做的**

两个钩子各自回答一个动作，分工不重叠：

| 钩子 | 干什么 | 真实调用点 |
|---|---|---|
| `select_context(request_messages, *, conversation_messages=None, incoming_message=None, budget_tokens=0)` | **生成前**替换**这一次请求**的上下文：检索增强的选择、主题 / 分支路由、角色切换。返回新列表即换掉本次请求，返回 `None` 即原样不动 | `agent/turn_request_assembly.py:142` |
| `on_turn_complete(messages, usage=None, **kwargs)` | **回合结束后**观测 / 摄取这一回合（索引、更新路由状态），供下一次 `select_context` 用；返回值被忽略 | `agent/turn_finalizer.py:541` |

契约有四条硬边界，缺一条就会比"不装引擎"更糟：

| 面 | 规则 | 不做会坏在哪 |
|---|---|---|
| **失败放行（fail-open）** | 两者默认 `return None`；缺钩子 / 抛异常 / 返回值无效 ⇒ 请求保持原样 | 一个出故障的引擎**绝不**比不装引擎更糟 |
| **仅限请求（request-only）** | `select_context` 返回的列表只替换**单次** provider 调用的消息；持久化转写本**永不**被写（宿主给的是结构克隆） | 引擎一次误写就污染了会话历史 |
| **次序 / 缓存稳定** | 选择钩子在提示词缓存控制**之前**、在每个请求清洗器(sanitizer) **之前**运行；默认空操作让请求**逐字节一致** | 换前缀 = 提示词缓存全部失效；放 sanitizer 之后 = 替换绕过校验 |
| **覆盖度尽力而为** | `on_turn_complete` 从标准回合收尾接缝(finalization seam)触发；回环里某些异常提前返回（内容策略拦截、provider 终态失败）会持久化并返回而不经收尾，因此**不**发出该钩子 | 把它当"每次退出的保证回调"就会漏观测 |

★ **零成本钩子的手法（本节的源码依据）**：宿主规定**非实现引擎每回合必须付零成本**。这个判定**不能靠 `hasattr`** —— ABC 给这些钩子定义了 no-op 默认，`hasattr(engine, "select_context")` 恒为 `True`。正确的判据是**身份比较**：只有当引擎上的属性与其 ABC 默认**是同一个函数对象**时才算"它继承了默认"，此时宿主直接跳过、连参数克隆都不做：

```python
# agent/conversation_loop.py:1174-1178
return getattr(hook, "__func__", None) is not getattr(_CE, name)
```

**小表：两条轴为什么必须分开成两个钩子**

| 需求 | 用哪个动词 | 为什么不是另一个 |
|---|---|---|
| 换掉"哪些消息进入本次请求" | `select_context` | `pre_llm_call` 插件钩子按成文设计是**只注入**：它把片段追加进 user message，**没有任何重写消息列表的路径**（`agent/turn_context.py:666-720` 只 join 出 str） |
| 回合后索引 / 摄取 | `on_turn_complete` | 它是观测侧的镜像，配合已经做过路由的引擎；纯观测应交给 memory provider |

### 何时用这些钩子、何时不要用（When to use these hooks — and when NOT to）

- **只有当你的引擎必须*替换*每请求上下文时才实现 `select_context()`** —— 检索增强的选择、主题 / 分支路由、角色切换。它是唯一能换掉"哪些消息进入一次请求"的动词；`pre_llm_call` 按成文设计是**只注入**的（追加到 user message，绝不重写列表，以保住提示词缓存前缀）。不需要替换，就不要实现它。
- **如果只需要回合后的观测 / 摄取**（索引、内存同步、分析），就该实现一个**内存提供者(memory provider)**，而不是上下文引擎：上下文引擎取得会话压缩策略的**所有权**，内存提供者观测回合却什么都不拥有。`on_turn_complete()` 作为观测侧镜像存在，是给那些*已经*需要 `select_context()` 的引擎用的 —— 让同一个组件能从它刚路由过的那一回合里学习 —— 而**不是**一个通用的回合回调。
- **一次真实的 `select_context()` 会改变提示词缓存前缀**：非空操作的选择，在它改变选择的那几个回合里，那次请求的前缀不再匹配 provider 已缓存的前缀，于是那几个回合**重写**缓存而非**读取**缓存。引擎应当在**什么都没变时返回稳定的选择**（同一对象或一个等值列表），只在路由决策确实不同时才重塑上下文 —— 一个每回合都重洗的选择会静默地放弃每一次缓存复用。
- **性能上被"奖励"不实现**：主机侧的 `_engine_overrides_hook` 用身份比较跳过从 ABC 继承的默认实现，所以不实现 `select_context` / `on_turn_complete` 的引擎在每请求 / 每回合路径上拿到的是**零额外开销** —— "不需要就别实现"不只是建议，是被代码奖励的。

**② 读 Hermes 的代码**

逐字引（`agent/conversation_loop.py:1166-1170`，`_engine_overrides_hook` docstring）：

```text
    """True when ``engine`` implements ContextEngine hook ``name`` itself.

    Non-implementing engines must pay nothing per turn; ``hasattr`` is not enough because
    the ABC defines a no-op default. Lazy import avoids a cycle with agent.context_engine."""
```

**要害**：这段注释把两条判据都写死了——"非实现引擎每回合必须付零成本"是**要求**，"`hasattr` 不够"点名的正是错误做法。落到实现（`:1174-1178`）就是那句 `getattr(hook, "__func__", None) is not getattr(_CE, name)` 的**身份比较**：只有当引擎上的函数对象与 ABC 默认不是同一个时，才认为"它真的实现了这个钩子"。`hasattr` 之所以会被 ABC 的 no-op 默认"骗到"，正因为默认实现让这个属性始终存在。

**③ mini 的实现（现状）**

mini **没有这两个钩子的消费者**，所以 **⏭ 不抄**。两条理由：

1. **无消费者**：mini 的 agent loop 里只有两种上下文动作 —— 超阈值**压缩**与每回合**微压缩**（机制见《源码讲解-05》§〇、《源码讲解-10》），**没有**"每请求替换上下文"的需求（无检索、无主题路由、无分支切换），也**没有** memory provider 去接回合后观测。抄进来就是一圈**空接缝** —— 没有第二个消费者会去实现它。
2. **无该性能约束**：真实侧之所以要发明 identity 跳过，是因为一个 no-op 钩子坐落在**每请求 / 每回合**的热路径上，即使什么都不做也要为参数克隆和调用买单。mini 现在**没有这条热路径**，也就没有"非实现引擎必须零成本"这条约束的落点 —— 抄了只会写成一条**没有任何代码去验证**的注释。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| `select_context` | ABC 默认 no-op + 宿主 identity 跳过；真实调用点 `agent/turn_request_assembly.py:142`（**先于** sanitizer，缓存不变） | ⏭ 不抄（无每请求替换上下文的消费者） |
| `on_turn_complete` | ABC 默认 no-op + 宿主 identity 跳过；真实调用点 `agent/turn_finalizer.py:541` | ⏭ 不抄（无 memory provider / 回合观测消费者） |
| 零成本语义 | `_engine_overrides_hook`：`hook.__func__ is not ABC.method` 的身份比较（`agent/conversation_loop.py:1166-1178`） | ⏭ 不抄（mini 没有该热路径约束；这条机制已写进①） |
| fail-open 兜底 | `None` / `[]` / 非 list / 含非 dict ⇒ 一律回未修改的请求 | ⏭ 不抄（随两个钩子一起不引入） |

---

## §五 引擎工具（Engine tools）

**① Hermes 怎么做的**

上下文引擎可以暴露 **agent 直接调用的工具**。契约是两个方法（都在 ABC 上、都有默认实现）：

| 方法 | 契约 | ABC 默认 |
|---|---|---|
| `get_tool_schemas()` | 返回本引擎的工具 schema 列表 | `return []`（`agent/context_engine.py:203-205`） |
| `handle_tool_call(name, args, **kwargs)` | 处理一次调用，**必须返回 JSON 字符串**；`kwargs` 可含 `messages`（实时内存列表） | `json.dumps({"error": f"Unknown context engine tool: {name}"})`（`:207-210`） |

引擎工具在**启动时被注入 agent 的工具列表**，并**自动分发** —— 无需注册表注册。注入不是"把 schema 直接 append 进去"那么随意，它有三条判据，每条都对应一个**具体的故障**：

| 判据 | 规则 | 不做会坏在哪 |
|---|---|---|
| **门控** | `enabled_toolsets is None or "context_engine" in enabled_toolsets` | 与 memory provider 工具同一道闸；缺了它，`platform_toolsets: telegram: []` 仍会把 `lcm_*` 工具漏进工具面，给小模型加本地延迟 |
| **去重** | 与已有工具名比对，重名跳过 | 插件可能经 `ctx.register_tool()` 注册同一个 schema；重名会让 provider 侧 `400` |
| **无名跳过** | `normalize_tool_schema` 归一化后取不到名字 ⇒ 跳过 | 一个无名工具会让严格 provider `400`，并**废掉整个 toolset**（#47707） |

**分发**：工具执行器按"引擎工具名集合"命中，于是走一条专用分发 —— 调用 `handle_tool_call(function_name, args, messages=…)`，并把实时内存的 `messages` 传下去（`agent/tool_executor.py:1559`）。

★ **关键对照：内置压缩器零引擎工具**。`ContextCompressor`（唯一的生产级实现）**不实现** `get_tool_schemas` / `handle_tool_call`，所以注入函数从它身上拿到的是 ABC 默认的 `[]` —— **什么也不注入**。也就是说"引擎工具"这条路径在**默认配置下完全是死的**：它的头等消费者是像 `lcm` 那样自带检索工具的外部引擎。别被文档里 `lcm_grep` 的示例误导成"内置压缩器也暴露工具"。

**② 读 Hermes 的代码**

逐字引（`agent/agent_init.py:1949-1951`，`_inject_context_engine_tools` 的注释）：

```text
    # Context engine tool schemas (lcm_*), deduped against existing names (plugins may
    # register the same schemas; duplicates 400 provider-side) and gated on enabled_toolsets
    # so `platform_toolsets: telegram: []` can't leak them.
```

**要害**：三行注释同时点出**去重**与**门控**两条判据，且每条都绑定一个具体故障（provider `400` / 工具面泄漏）。注入是一段**防御性的**代码 —— 它把"插件可能重复注册同一 schema""平台配置可能把工具面清空"两种现实都算了进去，而不是单纯地 `agent.tools.append(...)`。

**③ mini 的实现（现状）**

⬜ **不抄**。mini 的 ABC **已经有 `tools()` 占位钩子**（`mini-hermes/agent/context_engine.py:95-97`，默认 `[]`）—— **形状抄了**（"引擎可暴露工具"这件事在契约上留了口子），但"启动时把引擎工具注入工具列表并自动分发"这条路**没建**。理由：

- 要接 `mini-hermes/tools/registry.py` + toolset 闸门（第 4 章）才谈得上注入与门控；
- 而 mini **没有任何引擎带工具**可测 —— 建了也是**没有消费者的死路径**。

所以 mini 只保留 `tools()` 这一个钩子（形状对齐），**不建注入路径**（`handle_tool_call` 也先不抄）。等真有带工具的引擎时再补注入 + 门控 + 去重。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 契约 | `get_tool_schemas()` / `handle_tool_call()`（默认 `[]` / error JSON） | ⬜ 没做（只有占位钩子 `tools()`，`mini-hermes/agent/context_engine.py:95-97` 默认 `[]`） |
| 注入 | `agent/agent_init.py:1948`：启动注入，按 `enabled_toolsets` 门控 + 去重 + 无名跳过 | ⬜ 没做（无注入路径） |
| 分发 | `agent/tool_executor.py:1559`：按引擎工具名集合命中 ⇒ `handle_tool_call(..., messages=…)` | ⬜ 没做（无分发路径） |
| 内置引擎的工具面 | `ContextCompressor` **不实现** ⇒ 注入函数拿到 `[]`，零引擎工具 | 已对齐：默认引擎同样不暴露工具（`tools()` 默认 `[]`） |

---

## §六 注册（Registration）

**① Hermes 怎么做的**

注册有**两条路径**：

### 经由目录（推荐）（Via directory (recommended)）

把引擎放进 `plugins/context_engine/<name>/`，该目录的 `__init__.py` **必须导出一个 `ContextEngine` 子类**。发现系统会自动找到并实例化它 —— 这是推荐路径，多数场景够用。

### 经由通用插件系统（Via general plugin system）

一个通用插件也可以注册一个上下文引擎，入口是 `hermes_cli/plugins.py:702 register_context_engine`：

```python
def register(ctx):
    engine = LCMEngine(context_length=200000)
    ctx.register_context_engine(engine)
```

**只能注册一个引擎**：槽位是单一的，第二个试图注册的插件会被**当场拒绝并给出一个警告**（而不是常规的覆盖）。

★ **关键事实（契约与实现分离的极端形态）**：仓库里 `plugins/context_engine/` 这个目录下**只有加载器 `__init__.py`（93 行），零个引擎子目录** —— `git ls-tree -r f5d1926110 -- plugins/context_engine/` 只返回一个文件。也就是说"目录注册"这条路径在仓库里**没有任何内置引擎可被发现**；唯一生产级的实现是内置 `ContextCompressor`，它住在 `agent/context_compressor.py`，**根本不走**这条发现路径（`context.engine == "compressor"` 时选择函数直接返回 `None`）。注册器的头等消费者是**外部引擎**（文档里的 `lcm`），仓库自己**不占这个位置** —— 契约（一个加载器 + 一个注册入口）与实现（一个 5,061 行的内置压缩器，落在别处）被彻底分开。

**② 读 Hermes 的代码**

逐字引（`hermes_cli/plugins.py:702-704`）：

```text
    def register_context_engine(self, engine) -> Optional[PluginRegistration]:
        """Register the (single) ``agent.context_engine.ContextEngine`` replacing the built-in
        ContextCompressor; a second registration is rejected with a warning."""
```

**要害**：docstring 里 `the (single)` 明写了"单一"，于是"只能有一个引擎"是**进代码的约束**，不是一句建议 —— 第二个注册者拿到的是 warning 而非常规覆盖（`:705-709` 先判 `_context_engine is not None` 就 return）。契约的"唯一性"和实现的"单一槽位"是对齐的。

**③ mini 的实现（现状）**

✅ **已抄**（`a6ed019`）。mini 只落**一条注册路径：目录发现**，实现在 `mini-hermes/agent/engine_registry.py`（**335 行**，锚 mini HEAD `7b0b18b`），与真实侧的核心动作一一对应：

| 真实动作 | mini 对应 | 行号 |
|---|---|---|
| 目录常量 | `engines_dir()` = `<MINI_HOME>/engines`（唯一来源） | `mini-hermes/agent/engine_registry.py:148` |
| 列候选目录 | `iter_engine_dirs()`：排序 + 跳隐藏 / 排除目录 | `:158` |
| 读一个引擎 | `read_engine()`：坏目录**跳过并计数**、永不抛；五项契约校验（软链 / 保留名 / 可导入且定义了子类 / 可无参实例化 / `name == 目录名`） | `:212` |
| 扫一遍 | `discover_engines()` → `EngineScan(engines, skipped)` | `:245` |
| 按名解析 | `resolve_engine(name)`：**查已发现索引**（名字只做相等比较），不拼路径 | `:274` |
| 选择（接缝） | `select_engine()` → `EngineSelection` | `:291` |

```python
def engines_dir() -> Path:                       # mini-hermes/agent/engine_registry.py:148
    """引擎库根目录：``<MINI_HOME>/engines``（**唯一来源**）。"""
    return get_mini_home() / "engines"
```

★ **与第 13 章技能发现同构**：mini 的这条发现路径**不是新造的形状**，而是**照第 13 章抄的** —— `engines_dir()` ↔ `mini-hermes/agent/skill_utils.py:144 skills_dir()`、`iter_engine_dirs()` ↔ `:133 iter_skill_dirs()`、`read_engine()` 的"宽容 + 计数、永不抛" ↔ `:271 read_skill()`、`discover_engines()` 的 `EngineScan` ↔ `:315 discover_skills()` 的 `SkillScan`、`resolve_engine()` 的"查索引不拼路径" ↔ `:347 resolve_skill()`。三条纪律（**零第三方依赖**、**每次选择真读盘（无进程内缓存）**、**宽容 + 计数**）也一字不差地沿用第 13 章。

★ **回落必须"有声"**：`select_engine()` 返回的 `EngineSelection` 带一个 `warning` 字段 —— 未知名 / 该目录被跳过时，回落内置默认引擎并**返回一段可读警告**（`warning` 非空），由宿主经 `_emit_engine_warning()` 打到用户眼前 —— 有 `status_callback`（CLI）走它；**没有**（网关 `gateway/run_turn.py` 传 `None`、`doctor.py` 不传）**只走一条 `print(..., file=sys.stderr)`**。★ 这两条都是**审计改出来的**：先前直接复用 `_emit_status` ⇒ 这些入口**静默丢警告**（用户以为换了引擎）；改成 `logger` + `print` 双写 ⇒ 零配置时 `lastResort` 与 `print` 叠加，终端**重复两遍**。故最终**只留一条出口**。**静默回落是这一章最坏的一种**：用户以为换了引擎、其实没换。

至于真实侧的**第二条入口（通用插件系统 `register_context_engine`）**，mini **⏭ 不抄**：mini 根本没有插件宿主（没有 `plugins/`、没有 manager、没有 `ctx`），搬过来就是**没有第二个消费者的死代码**，还会牵进一整套生命周期 / 管理器。mini 只保留"目录发现"这一条 —— 它正是真实侧**推荐**、且多数场景够用的那条。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 目录注册 | `plugins/context_engine/<name>/` + 加载器 `plugins/context_engine/__init__.py`（93 行）；★ 仓库该目录下**零引擎子目录** | ✅ 已抄（a6ed019）：`<MINI_HOME>/engines/<name>/`，`mini-hermes/agent/engine_registry.py`（335 行） |
| 发现根 | 仓库相对 `plugins/context_engine/`（另有 `~/.hermes/plugins/` **备用根**） | 已对齐（本地化）：`get_mini_home()/"engines"` —— **单根**，与 `skills_dir()` 同构 |
| 发现失败的处置 | `load_context_engine` 返回 `None` ⇒ 回落；不可 `deepcopy` 也回落并带 warning | ✅ 已抄（a6ed019）：坏目录**跳过 + 计数**；未知名回落默认 + `EngineSelection.warning`（有声） |
| 通用插件系统 | `hermes_cli/plugins.py:702 register_context_engine`，单一槽位，第二个被拒并警告 | ⏭ 不抄（mini 无插件宿主 / 无 `ctx` / 无 manager） |
| 备用发现根 `~/.hermes/plugins/` | 第二个根，两条发现路径 | ⏭ 不抄（无消费者；mini 的硬约定是"路径唯一来源"，两条根会让"引擎装哪儿"有两个答案） |

## §七 生命周期（Lifecycle）

**① Hermes 怎么做的**

一个 `AIAgent` 对象可以**跨多个会话**活着：CLI 里 `/new` 会换会话，gateway 里一条过期连接会换会话，子 agent 会从父 agent 派生。而上下文引擎持有**跨调用**的状态——token 计数（`last_prompt_tokens` / `last_completion_tokens` / `compression_count`）、微压缩游标、以及"上一个模型证明过什么"的校准位。会话边界如果不通知引擎，这些账会**带着旧会话的数字**跑进新会话：阈值判断基于过期用量，摘要游标指向一条已经不属于当前历史的消息。这就是宿主必须驱动一次"会话过渡"的原因。

过渡由一个 seam 统一驱动：`run_agent.py:362 _transition_context_engine_session(...)`，它按固定顺序依次调用四个钩子：

> `on_session_end` → `on_session_reset` → `on_session_start` → `carry_over_new_session_context`

这个顺序不是随意的，三段各解决一个不同的问题：

- **`on_session_end(session_id, messages)` 必须最先**：它要把**旧会话的消息**交回引擎（引擎可能想落盘、索引或做收尾统计）。逐字注释（`hermes_cli/cli_session_mixin.py:461-469`）说明了为什么它排在最前、而且只做"便宜"的那一半：*"The context-engine `on_session_end` is delivered synchronously here: cheap (no LLM) and ordering-sensitive — it must land before `reset_session_state()` rebinds the engine. The memory-provider half (LLM-bound, seconds) is NOT run here…"* —— 它必须在 `reset_session_state()` **重新绑引擎之前**落地（ordering-sensitive），否则引擎看到的"旧会话"已经被新会话的 id 覆盖了。
- **`on_session_reset()` 居中**：清掉**上一个模型**期间捕获的跨调用校准态。为什么非清不可，ABC 的逐字注释（`agent/context_engine.py:188-197`）把失败模式说透了：这些字段编码"'provider 证明过这个 prompt 放得下'"、"'preflight 可以延后'"，而这类判断**只对产生它的那个模型有效**；带着它们切到一个**更小上下文**的模型，会让 `should_defer_preflight_to_real_usage()` 压掉新模型**真正需要**的那次 preflight 压缩——正是 #23767 的"切换后超发"故障。注释还点明一个刻意的哨兵选择：`last_prompt_tokens` 设成 **`0`（不是 `-1`）**是刻意的，`0` = "还没有真实用量 → 用粗略估算"，`-1` 是另一个哨兵（#36718，"压缩刚跑完，等真实用量"），不能在这里设。
- **`on_session_start(session_id, **ctx)` 最后**：把**新会话**的身份与环境告诉新引擎，让它重建自己的状态。宿主传的不只是 id —— `run_agent.py:377-387` 组装了 `old_session_id` / `carry_over_context` / `platform` / `model` / `context_length` / `conversation_id`，并**滤掉空值**后才透传。
- **`carry_over_new_session_context`**（`run_agent.py:388-389`，仅当 `carry_over_context` 为真且新旧 id 都在时）是第四段：它把"要不要继承上一会话的上下文"这个决定交给引擎。

四个钩子**全部可选**。宿主不靠"引擎实现了没有"来分支，而是用一个统一的兜底 `run_agent.py:164 _call_engine_hook(engine, hook, *args, **kwargs)` —— 它 `hasattr` 不到就直接返回，调用抛异常只 `logger.debug` 一条、**永不抛出**（docstring 逐字：*"Invoke an optional context-engine lifecycle hook; failures are logged, never raised."*）。于是哪怕最简的实现什么都不覆盖，过渡流程照样跑完，只是全是空操作。

各阶段在真实源码里的调用点：

| 阶段 | 位置 |
|---|---|
| 构造 | `agent_init.py:1829-1870`（插件引擎或内置） |
| `update_model`（模型切换/回退时重算阈值） | `agent_init.py:1849-1852`、`turn_context_compaction.py:89-96` |
| `on_session_start` | `agent_init.py:1988-1995`（`on_session_start` 注入点） |
| `on_session_end` | `hermes_cli/cli_session_mixin.py:479-483`（`/new` 边界）；`run_agent.py:373`（过渡） |
| `on_session_reset` | `run_agent.py:375`（过渡）；内置覆盖见 `context_compressor.py:1790-1795` |

**② 读 Hermes 的代码**

```python
def _transition_context_engine_session(
    self, *, old_session_id=None, new_session_id=None, previous_messages=None,
    carry_over_context: bool = False, reset_engine: bool = True, **extra_context,
) -> None:
    """Drive the context engine's session transition: on_session_end → on_session_reset
    → on_session_start → carry_over_new_session_context. Each hook is optional
    (the built-in compressor only resets)."""
```
（`run_agent.py:362-368`）

要害：docstring 用一句话把**两个契约**都写死了——**顺序**（`→` 连起来的四段）和**可选性**（"Each hook is optional"）。末句 `the built-in compressor only resets` 补上第三层意思：哪怕默认实现只覆盖中间那一段，宿主也照跑四段——**顺序由宿主定，引擎不参与编排**。

**③ mini 的实现（现状）**

⬜ **没做**。mini 的引擎只有"**一个 agent 实例内解析一次**"这一种生命周期：`mini-hermes/run_agent.py:1590-1627` 的 `_compressor()` 第一次被调用时跑一次 `select_engine(...)`，把结果缓存进 `self._compressor_obj`，此后不再重选、不再经过任何 seam。

没有会话切换 seam：mini 的 `AIAgent` 没有 `reset_session_state`、没有 `_transition_context_engine_session`；`on_session_end` / `on_session_reset` / `on_session_start` 这三个钩子在 mini 的 ABC（`mini-hermes/agent/context_engine.py`，112 行）里**根本不存在**——它只有 `name` / `should_compress` / `compress` 三个抽象成员加四个默认实现（`update_usage` / `tools` / `describe` / `update_model`）。

为什么可以没有：mini 里"**一个 agent 实例 = 一个会话**"，压缩器的状态全挂在宿主实例上（第 10 章的微压缩游标就是 `ContextCompressor` 的实例字段）。既然不存在"同一个 agent 对象跨会话复用"的路径，也就不存在"把旧会话的账带进新会话"那个窗口——三段过渡要防的坏状态在 mini 里结构上不可能出现。

要抄的量级是**三样一起上**：宿主侧的过渡 seam（约 25 行）、ABC 三个可选钩子（约 20 行）、以及**真正的会话切换调用点**（mini 没有 `/new` 时重绑引擎的路径）。只抄 ABC 那三个空钩子 = 一组没有调用者的死钩子，所以按 mini 的纪律（"不做投机性基础设施"）标记为 ⬜ 没做。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 会话切换 seam | `run_agent.py:362 _transition_context_engine_session`（四段，宿主编排） | ⬜ 没有 |
| ABC 生命周期钩子 | `on_session_start` / `on_session_end` / `on_session_reset`（默认空体或重置） | ⬜ 无这三个钩子 |
| 引擎实例生命周期 | 可跨多次会话切换被 `reset` 复用 | 一个 agent 实例内解析一次并缓存（`_compressor_obj`） |
| 跨调用校准态复位 | `on_session_reset()` 由宿主在换会话时驱动 | 无（无换会话路径，无需复位） |

> **未能完全确认的一条**：`carry_over_new_session_context` 在 `run_agent.py:388-389` 被 `_call_engine_hook` 调用，但 `agent/context_engine.py`（242 行）里**没有**这个方法的定义——它不在 ABC 契约内。`_call_engine_hook` 自带 `hasattr` 兜底，所以内置压缩器（不定义它）走这里只是空操作。该钩子的语义/契约来源**未在 ABC 内确认**（可能是插件侧的约定，也可能是历史遗留），这里如实标注，不猜。

---

## §八 配置（Configuration）

**① Hermes 怎么做的**

配置面上只有一个键：`context.engine`，默认值 `"compressor"`。真值住在 `hermes_cli/config_defaults.py:1204-1208`（注释 + `"context": {"engine": "compressor"}`），逐字注释把它当成**一份自解释的选择词表**：

> `# Context engine — how the context window is managed near the token limit. "compressor" = built-in lossy summarization; or a plugin name (e.g. "lcm") installed in plugins/context_engine/<name>/ or ~/.hermes/plugins/.`

★ 关键的一条：**零配置 = 不走发现**。选择函数 `agent/agent_init.py:1761 _select_context_engine(_agent_cfg)` 读到 `"compressor"` 时**直接 `return None`**（`:1768-1769` 逐字：`if _engine_name == "compressor": return None  # built-in; don't auto-activate plugins`），调用方 `_build_context_engine` 拿到 `None` 就构造内置 `ContextCompressor`。含义很硬：**零配置的人一点开销都不付**——不 import 插件 loader、不列目录、不跑 `deepcopy`。整条发现路径（`plugins/context_engine/<name>/` → 通用插件系统 `get_plugin_context_engine()` → `None` 内置）只为"明确写了别的名字"的人打开。

只有当名字不是 `"compressor"` 时，才会（a）先试目录加载器 `load_context_engine(name)`，（b）退回通用插件系统里注册过的引擎并对名字，（c）候选要能 `copy.deepcopy`（防子 agent 改父预算的共享单例），拷贝不了则每条 warning 后回退内置。

**★ 设计取舍：为什么默认值用魔术名 `compressor`，而不是空值？**

把这四层理由叠起来看，答案是"空值会让系统多出第四条分支，而魔术名让它归零"：

1. **取值空间是一组"名字"，默认值必须是词表成员。** `context.engine` 的值要么是内置实现的名字、要么是某个插件的名字——而内置实现**自己也有名字**（`ContextCompressor.name == "compressor"`）。若默认值写成空串/缺键/`None`，"没有值"这个状态就落在名字词表**之外**，每个读取点都要先判一次"到底有没有值"，再进名字分支。
2. **"插件永不自动激活"能压成一条等式。** `if _engine_name == "compressor": return None` —— 显式写 `compressor` 和完全不写，在这里**合流成同一条路径**。若默认是空值，就得写"空 ⇒ 内置" + "非空 ⇒ 发现"两套判据，而"用户显式写 compressor"又会变成第三态。魔术名把三态压成一态，这也正是 mini 不变量 I4（"零配置 = 与改造前逐字节一致"）能成立的前提。
3. **自解释、可 grep。** 用户在 config 里看到的那一行本身就是一句可读的话（注释与文档里 `"compressor" = built-in lossy summarization`）；一个空默认值在配置文件里读不出"那默认到底用的是什么"。
4. **给"保留名"留了抓手。** 既然默认值是一个具体名字，目录发现就能把它当**保留名**防守（mini 的 I6：占用 `<MINI_HOME>/engines/compressor/` 的目录被跳过并计数）。否则会出现"用户以为换掉了、其实没换"的状态——用户放了一个叫 `compressor` 的目录，而宿主还在用内置的那个。真实侧对同一风险的处理更直接：名字是 `"compressor"` 时**根本不进发现路径**。

**② 读 Hermes 的代码**

```python
def _select_context_engine(_agent_cfg):
    """Config-driven context engine: ``context.engine`` → plugins/context_engine/<name>/ →
    general plugin system → None (built-in ContextCompressor)."""
    _engine_name = "compressor"
    with suppress(Exception):
        _ctx_cfg = _agent_cfg.get("context", {}) if isinstance(_agent_cfg, dict) else {}
        _engine_name = _ctx_cfg.get("engine", "compressor") or "compressor"
    if _engine_name == "compressor":
        return None  # built-in; don't auto-activate plugins
```
（`agent/agent_init.py:1761-1770`）

要害：docstring 把整条解析链压成一行（`… → … → None (built-in)`）；而 `return None` **不是"没选到"**，是"**选到了内置**"——这里用 `None` 当内置的哨兵，于是"零配置"与"显式写 compressor"在同一个等式上合流，下游 `_build_context_engine` 分不出、也不需要分出二者。

**③ mini 的实现（现状）**　✅ 已抄（`a6ed019`）

- ★ **发现的已知副作用**（审计所得，已写进模块 docstring）：发现会 `exec_module` **每个**引擎目录的 `__init__.py` 顶层代码（**含未被选中的那些**），且不注册 `sys.modules` ⇒ **每次 `discover_engines()` 都会重执行** —— 设计使然（要靠它读 `name`），所以引擎 `__init__.py` 顶层不得放重活。
- **键与默认**：`mini-hermes/hermes_cli/config.py:83-93` —— `KEY_CONTEXT = "context"` / `KEY_CONTEXT_ENGINE = "engine"` / `DEFAULT_CONTEXT_ENGINE = "compressor"`，逐字对齐真实版 `config_defaults.py:1206`。同段注释写明"默认 `\"compressor\"` 永远指向**内置**实现……用户写别的名字才会走目录发现"。
- **读值**：`mini-hermes/hermes_cli/config.py:340-353`：

  ```python
  def get_context_engine_name(config=None) -> str:
      value = _section(config, KEY_CONTEXT).get(KEY_CONTEXT_ENGINE)
      if isinstance(value, str) and value.strip():
          return value.strip()
      return DEFAULT_CONTEXT_ENGINE
  ```

  三段式（`_section` 拿段 → 取值 → 非串/空白落默认），**绝不抛**；与 `get_configured_provider` / `get_compression_config` 同口径。docstring 明说这个默认值是"零配置 = 与改造前逐字节一致"（I4）的载体。
- **挂接**：`mini-hermes/run_agent.py:1590-1627` 的 `_compressor()` 首次用到时调 `select_engine(get_context_engine_name(), default_factory=self._build_default_compressor)`；`_build_default_compressor`（`:1570-1588`）**逐字保留改造前的构造参数与顺序**。`__all__` 补了 `KEY_CONTEXT` / `KEY_CONTEXT_ENGINE` / `DEFAULT_CONTEXT_ENGINE` / `get_context_engine_name`（`mini-hermes/hermes_cli/config.py:361、365、369`）。
- **"零配置不走发现"落在哪里**：`mini-hermes/agent/engine_registry.py:315-318` 的 `select_engine` 在 `requested == DEFAULT_ENGINE_NAME` 时**直接返回**默认工厂的结果、**不调 `discover_engines()`** —— 与真实侧 `if _engine_name == "compressor": return None` 完全同构（该文件 `:299` 的注释点明了它对应 `agent_init.py:1761-1808`）。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 键 / 默认 | `context.engine = "compressor"`（`config_defaults.py:1206`） | ✅ 同名同默认（`mini-hermes/hermes_cli/config.py:83-93`） |
| 读值方式 | 内联在 `_select_context_engine` 里（`agent_init.py:1764-1767`） | 独立纯函数 `get_context_engine_name()`（`:340`），**绝不抛** |
| 零配置短路 | `== "compressor"` ⇒ `return None`（不走发现） | ✅ `== DEFAULT_ENGINE_NAME` ⇒ 直接返回默认工厂（**不扫目录**） |
| 发现根 | 两条（仓库 `plugins/context_engine/` + `~/.hermes/plugins/`）+ 通用插件系统 | 一条（`<MINI_HOME>/engines/`），无插件系统 |

---

## §九 测试（Testing）

**① Hermes 怎么做的**

真实侧的契约测试是 `tests/agent/test_context_engine.py`（**347 行**），一份**完整的 ABC 契约测试套件**。它的骨架是一个 `StubEngine(ContextEngine)`（`:15`）——"Minimal engine that satisfies the ABC without doing real work"，用最少的实现把抽象面填满，然后分组验证每一类承诺：

| 测试类 | 测的承诺 |
|---|---|
| `TestContextEngineABC`（`:69`） | ABC 真的强制接口：漏方法的子类**不能实例化**；stub 满足 ABC 且 `name` 正确 |
| `TestDefaults`（`:93`） | ABC 的默认实现：`get_status()` 的标准 dict、`on_session_reset()` 清状态 |
| `TestStubEngine`（`:122`） | 引擎工具契约：`get_tool_schemas()` / `handle_tool_call()` |
| `TestCompressorSessionReset`（`:145`） | 内置压缩器覆盖后的 `on_session_reset` 真的清干净 |
| `TestPluginContextEngineSlot`（`:171`） | 插件槽：注册一个引擎、`get_plugin_context_engine()` 取回；第二个注册被拒 |
| `TestPluginContextEngineDeepCopy`（`:208`） | `deepcopy` 保状态（防子 agent 改父预算的共享单例） |
| `TestInitAgentDoesNotMutatePluginSingleton`（`:236`） | 子 agent 初始化**不污染**父 agent 的插件单例；不可拷贝的引擎优雅回退；`agent_init` 源码里确实是 deepcopy 而非 alias |

★ 另有宿主契约套件 `tests/agent/test_context_engine_host_contract.py`（**193 行**），第 1 条断言就是"`_transition_context_engine_session` 驱动完整生命周期"——这正面印证了**生命周期是宿主侧契约**（§七）：它的顺序与可选性由宿主保证，不是每个引擎各自实现出来的。

**② 读 Hermes 的代码**

```python
class TestContextEngineABC:
    """Verify the ABC enforces the required interface."""

    def test_missing_methods_raises(self):
        """A subclass missing required methods cannot be instantiated."""
        class Incomplete(ContextEngine):
            @property
            def name(self):
                return "incomplete"
        with pytest.raises(TypeError):
            Incomplete()
```
（`tests/agent/test_context_engine.py:69-79`）

要害：这条断言**不测行为，测契约**——`Incomplete` 只实现了 `name`，漏了 `should_compress` / `compress`，于是实例化就该抛 `TypeError`。它守的是 `abc.ABC` 免费给的那条保证：谁把某个 `@abstractmethod` 删了，这条立刻变红。

**③ mini 的实现（现状）**　✅ 已抄（`a6ed019`）

`mini-hermes/tests/test_context_engine.py`（**555 行 / 7 组 / 45 条**）。与真实侧不同的是组织形态：独立脚本（非 pytest），自带 `check(name, cond, detail)`（`:58`），末尾打印"`N 通过 / M 失败`"并 `sys.exit`（`:442-446`）；`MINI_HOME` 在 **import 任何 mini 模块之前**指到临时目录（`:43-44`），每条用例开跑先 `reset_home()` 清空引擎库与配置（`:84-90`），所以用例之间**顺序无关**。

**七条不变量**，逐条守住一句承诺：

- **I1 选中的引擎真的被用**（`:151-172`）。不只断言类型：`not isinstance(c, ContextCompressor)` + `name == 目录名`，然后**走宿主真路径** `agent._preflight_compression(msgs)`，断言那个引擎 `compress()` 产出的**标记文本真的进了最终历史**（`marker_in(msgs)`）、且 `compress_calls == 1`。判据是"**行为可观测**"，不是"返回的名字对"。
- **I2 未知名 ⇒ 回落 + 有声**（`:175-202`）。`engine="nope"` ⇒ 仍是 `ContextCompressor`（不拒绝启动），且 `status_callback` 收到的串里**读得到被请求的名字 `nope`**、以及"可用引擎"清单。★ 这条还分**两种上下文**测：**网关形状**（调用方配了 logging handler）与**裸入口**（没配）—— 因为「退回 `logger` 单出口」只在有 handler 时显形（stderr 会空），「`logger`+`print` 双写」只在没 handler 时显形（stderr 两条）。配一条**反向对照** `test_silent_when_engine_is_default`（`:190-202`）：名字就是 `"compressor"` 或**连 `context` 段都没有**时，**不许产生任何警告**——否则"有声"会退化成满屏噪声。
- **I3 漏实现抽象方法 ⇒ 实例化即 `TypeError`**（`:205-250`）。全漏 / 只漏 `compress` 都在实例化时报错、信息里点名缺失成员；再补一个**强对照**：补齐三个抽象成员 ⇒ 正常实例化（守住"契约不是拦好引擎"）。
- **I4 默认引擎行为与改造前逐字节一致**（`:253-289`）。零配置 ⇒ `ContextCompressor`；与**直接构造**的 9 个关键字段 + `threshold_tokens` + `describe()` 逐个相等；同一输入 + 同一假 LLM ⇒ `compress` 输出**逐条 `==`**；并且断言**传进去的列表逐字节没被改**（引擎不碰 agent 状态）。
- **I5 好引擎入库、坏目录进 `skipped` 并计数**（`:292-337`）。五类坏目录（无 `__init__.py` / 语法错 / 没导出子类 / `name` 与目录名不符 / 漏抽象方法）全部**跳过并计数**、原因**可读**、跳过清单**确定序**、一个都没进候选；引擎库不存在 ⇒ 空扫描、不抛。
- **I6 名字唯一（保留名 + 确定性）**（`:340-363`）。占用内置**保留名** `compressor` 的目录被跳过并计数；`select_engine("compressor")` **永远命中内置**（不被目录顶替）；两次发现给出**完全一致**的顺序与名字（选择不靠 FS 顺序）。
- **I7 契约与目录名一致**（`:366-390`）。默认与插件引擎都 `isinstance(ContextEngine)` 且 `should_compress` / `compress` 可调用；插件引擎 `name == 目录名 == context.engine 值`**三处一致**；`engines_dir()` 锚定 `MINI_HOME`（测试重定向只有这一条路）。

夹具与"防漏"：`make_agent()` 用假 provider（**不联网**，`:70-81`）；`write_engine()` 能故意让 `name` 与目录名**不同**来测契约校验（`:126-133`）；末尾一条**未注册自检**（`:424-430`）——定义了 `test_` 却没登记进 `GROUPS` ⇒ 退出码 2（"写了但从来没跑"是最经典的测试静默失败）。

**反向验证**（改坏就干净变红，与断言一一对应）：

- **RV-1 掐多态**：把 `run_agent.py:1114` 的 `select_engine(...)` 改回写死 `ContextCompressor(...)`（恢复本章改造前的构造）⇒ **测试 7 红 / 真跑 7 红**（选了别的引擎却没被用，直接打在 I1 上）。
- **RV-2 静默降级**：删掉回落时的 `warning`（`select_engine` 不再产警告 / 宿主不再 `_emit_status`）⇒ **测试 2 红 / 真跑 1 红**（行为可能仍是内置压缩器，但**读不出**"名字写错了"——这条专抓"静默"）。

**真跑**：`mini-hermes/demos/ch14_real_run.py`（**233 行 / 12 条**）。隔离三件套（临时 `MINI_HOME` 在 import 前设，`:28-31`；`finally: shutil.rmtree` 保证不留垃圾，`:224-227`）。它真写一个最小引擎到 `<MINI_HOME>/engines/under_lossy/__init__.py`、真写配置 `{"context":{"engine":"under_lossy"}}`、建真 `AIAgent` + **真系统提示词**（`system_prompt.build_system_prompt`）+ 真工具群历史 + 真会话库（`hermes_state.SessionDB`），然后走 `agent._preflight_compression(msgs)` 这条**宿主真路径**。13 条断言里最硬的三条是：**引擎的 `compress()` 真被执行过**（计数 ≥1）、**标记文本真进了最终历史**、**标记也进了会话库 active 行**（压缩作为新世代落库）；另有一条明确选择"引擎**不碰系统提示词**"的断言（首条 `system` 逐字还是真提示词）。第 ⑤ 段是 I2 的真跑版（未知名 ⇒ 回落 + 有声），第 ⑥ 段是 I4 的真跑版（零配置的 `compress()` 输出与**直接构造**逐条相等）。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 测试文件 | `tests/agent/test_context_engine.py`（347 行）+ `test_context_engine_host_contract.py`（193 行） | ✅ `mini-hermes/tests/test_context_engine.py`（450 行 / 7 组 / 39 条） |
| 框架 | pytest（`class Test…` + `monkeypatch` 夹具） | 独立脚本 + `check()` + `sys.exit`（与 `mini-hermes/tests/test_compression.py` 同款） |
| 覆盖面 | ABC 契约 + 默认实现 + 引擎工具 + **插件槽位注册** + deepcopy 单例 + **宿主生命周期** | ABC 契约 + 默认等价 + 发现容错/保留名；**无**插件槽位/deepcopy/生命周期（mini 无这些基座） |
| 失败粒度 | pytest 报告 | 一条失败 = 一行可诊断的红；用例抛异常被逐条接住（`:436-439`），不吞掉后面的组 |
| 反向验证 | —（未随套件提供） | RV-1 / RV-2 两条"改坏就变红"（测试 7/2 红 · 真跑 7/1 红） |

## §十 线程安全（Thread safety）

**① Hermes 怎么做的**：压缩这一趟要调摘要用的 LLM，秒级到十几秒不等；如果它跑在对话线程上，宿主就没有"取消"这一手——线程杀不掉，只能干等或卡死。Hermes 的处置是**把整趟压缩挪到线程池上，由宿主在对话线程上等一个超时**：超时到了就丢弃这一趟的产物，对话继续。于是"压缩"从一个不可打断的动作，变成一个**可放弃**的动作，代价是引擎必须接受四条新约束。这跟"压缩策略"本身无关——阈值、保护头几位/保护尾几条、滚动摘要、缓存不变式都是《源码讲解-05》的内容，本节只讲**换到池上跑之后，插槽对实现者新增的义务**。

开关是 `compression.context_timeout_seconds`（`hermes_cli/config_defaults.py:596`，默认 `120`）：**大于 0 就启用**，也就是默认启用。读它在 `agent/conversation_compression.py:707-731`（`resolve_context_compression_timeouts`，逐字 `if cfg.get("context_timeout_seconds") is not None:` / `idle = float(cfg["context_timeout_seconds"])`），返回 `(idle, ceiling)` 两档超时；`idle <= 0` 才关掉进度感知包装。

由此引擎必须假定四件事（每一条都对应一个"不做就会坏"的场景）：

| 约束 | 不遵守会坏在哪 |
|---|---|
| 调用可能到达**任意池化线程**——不依赖线程亲和性、不依赖与对话线程共享的 `threading.local` | 引擎把预算状态挂在 `threading.local` / 线程名判断上 ⇒ 在下一次压缩时读到**空**的或**别的会话**的状态 |
| 收到的消息列表是一份**私有深度快照**；就地改它对活会话不可见 | 引擎以为"我改了就是改了" ⇒ 超时丢弃后，对话里残留半改状态；或反过来，引擎不敢就地改而多复制一份 |
| 状态**只在被接纳的 commit 上发布**（`CompressionCommitFence`） | 超时后仍在跑的活儿还在往 DB / 外部写 ⇒ 持久化里出现一段**从未被采纳**的摘要 |
| **每会话同一时刻一趟**（durable lock），但**会话之间并发** ⇒ 跨会话共享的引擎实例必须线程安全 | 引擎用可变的实例属性存"当前这趟"的中间量 ⇒ 两个会话的压缩互相污染 |

池本身是一条硬边界：`_COMPRESS_EXECUTOR_MAX_WORKERS = 4`（`agent/conversation_compression.py:668`），池化执行器在 `_get_compress_timeout_executor()`（`:691-704`）建为 `DaemonThreadPoolExecutor(max_workers=4, thread_name_prefix="compress-ctx-timeout")`，`:698` 的注释逐字交代了规模的来由——*"Small pool sized for live compress + cancelled workers winding down, not asyncio's min(32, cpu+4)."*。而池**满了不排队**：`run_compress_context_with_progress_timeout`（`:1022`）在 `:1060` 逐字给出拒绝文案 *"Context compression pool saturated (%d workers busy) — refusing new compression this cycle and continuing without compression."*，并把失败类别标成 `failure_class="pool_saturated"`（`:1070`）——这是**失败放行（fail-open）**：放弃本周期压缩，继续对话，而不是让请求堵在队列里。

**② 读 Hermes 的代码**：`agent/conversation_compression.py:1-9`（模块 docstring，逐字）——这就是上面那张表的一句总纲：

```text
1: """Context compression: feasibility probe, warning replay, compress, image fix.
2: 
3: Thread-safety contract for extension points: with ``compression.context_timeout_seconds > 0`` (default) the
4: whole pass, context engines and memory providers included, runs on a pooled daemon thread. Calls may arrive
5: on any pooled thread (never rely on thread-affinity/locals); the message list is a private deep snapshot
6: (in-place mutation is invisible to the live conversation unless the pass commits); state is published ONLY
7: on an admitted :class:`CompressionCommitFence` commit (work still running after a host timeout is discarded);
8: one pass per session at a time (durable lock) but sessions run concurrently, so shared engines must be thread-safe.
9: """
```

要害：这份契约**不是写给引擎选的**，而是"宿主已经把压缩搬上池了"这个既成事实的**推论**——注意 `context engines and memory providers included` 与 `state is published ONLY on an admitted … commit` 两处：前者说明这条契约同时约束两类扩展点（引擎与内存提供者），后者说明**"提交"是唯一的状态发布口径**，而不是"调完 `compress()` 就算数"。第 6 行的 `in-place mutation is invisible … unless the pass commits` 是这段里最容易读反的一句：它**允许**就地改（历史遗留契约，见《精译-14》§10），但就地改的可见性被绑在 commit 上，不是绑在"函数返回"上。

**③ mini 的实现（现状）**：⏭ **不抄（无线程池；写了没人验）**。

mini 的压缩是**同步、内联**跑的：`run_agent.py::_preflight_compression`（`mini-hermes/run_agent.py:1724`）在**调用线程上**直接调引擎并原地改写消息列表——

```python
# _preflight_compression 的关键调用；压缩仍同步执行。
c = self._compressor()
if not c.should_compress(messages):
    ...  # 展示可选状态，然后返回
parameters = inspect.signature(c.compress).parameters
...  # 过滤引擎未声明的辅助调用参数
messages[:] = c.compress(messages, **optional)
```

没有线程池、没有宿主超时、没有 commit fence：grep 全仓 `context_timeout` / `timeout_seconds` **零命中**（`compression.context_timeout_seconds` 这个键在 mini 里根本不存在）。mini 里确实有线程（`mini-hermes/run_agent.py:898` 的 `threading.Thread(..., name="mini-api-call")`、`:1199` 的 `ThreadPoolExecutor` 跑并发工具调用），但**都不经过引擎**。

所以不抄的理由不是"太重"，而是**没有可验证对象**：给 `ContextEngine` 加"调用可能到达任意池化线程 / 你拿到的是一份私有深快照 / 状态只在 commit 上发布"这类契约，等于写下一段**没有任何代码去执行它**的注释——mini 没有池、没有超时丢弃、没有 commit，四条约束在 mini 里既不可能被违反也不可能被验证。这正是 `mini-hermes/agent/engine_registry.py:55-59` 那句"照抄就是没有消费者的死代码"的同一个判据。**如实标注为差异，不引依赖。**

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 压缩在哪跑 | 池化守护线程（`compression.context_timeout_seconds` 默认 `120` ⇒ 开）(`agent/conversation_compression.py:3`/`:707-731`) | 同步、内联在调用线程（`mini-hermes/run_agent.py:1724`，`c.compress(...)` 在 `:1752-1752`） |
| 超时 / 取消 | idle + total ceiling 双超时；超时后仍在跑的活儿**丢弃**（`:6-7`） | ⏭ 不抄（无超时；压缩跑完为止） |
| 消息列表 | 私有**深度快照**，就地改对活会话不可见；只在 commit 时可见 | 直接**原地**改写调用方列表（`messages[:] = …`）；无快照、无 commit 概念 |
| 并发 | 每会话一趟（durable lock），会话间并发 ⇒ 共享引擎必须线程安全（`:8`） | ⬜ 没有这个并发面：`_preflight_compression` 每 agent 一处；另一处并发（`:1096`）跑的是工具调用，不碰引擎 |
| 池规模 / 满则拒绝 | 4 工人池（`:668`/`:692-704`），池满**拒绝**新压缩并继续（`:1060`） | ⏭ 不抄（无池可满） |
| 契约的表达方式 | 模块 docstring 一句总纲 + `CompressionCommitFence` 是代码实体 | ⬜ 没做（无对应实体可写） |

---

## §十一 另见（See also）

- **上游原文**：`官方文档-Developer-Guide/context-engine-plugin.md`（272 行）；本系列译文《中文精译-14-ContextEnginePlugin.md》。本章与它们的**逐条出入**（含上游文档内部口径不一致的一处）集中在 **附录 C**。
- 《源码讲解-05-ContextCompressionAndCaching》——**压缩机制本体**：阈值触发、保护头几位 / 保护尾几条、滚动摘要、摘要调用参数、缓存不变式。本章凡涉及这些一律回指，不重述。第 5 章时 mini 的 `ContextEngine` 只有两个抽象方法、引擎只有一种；本章把它补成可替换插槽（抽象面加 `name`、加 `context.engine` 与目录发现、唯一构造点改查表）。
- 《源码讲解-10-MicroCompaction》——**微压缩**（每回合分期付款）。内置压缩器把微压缩作为 **mixin** 混入自己（`agent/context_compressor.py:1782` 逐字 `class ContextCompressor(SummaryDispatchMixin, MicroCompactionMixin, ContextEngine):`），所以"引擎"与"微压缩"是两个正交的可替换面。
- 《源码讲解-13-CreatingSkills》——**目录发现 + 宽容计数的样板**。mini 的 `mini-hermes/agent/engine_registry.py` 照 `mini-hermes/agent/skill_utils.py` 的 `skills_dir()` / `iter_skill_dirs()` / `discover_skills() -> SkillScan(skills, skipped)` 形状写：单一根 + 排序 + 坏目录跳过并计数。
- 《源码讲解-04-ToolsRuntime》——**工具 schema 注册与 toolset 闸门**。引擎工具（`get_tool_schemas` / `handle_tool_call`）的注入（`agent/agent_init.py:1948`）与分发（`agent/tool_executor.py:1559`）接在这里。
- **上游文档三处外链**（《精译-14》§11 同款）：上下文压缩与缓存（`/developer-guide/context-compression-and-caching`）· 内存提供者插件（`/developer-guide/memory-provider-plugin`，与之类似的单选插件系统）· 通用插件系统（`/user-guide/features/plugins`）。

---

## 附录 A · 引用汇编（正文没引、值得留痕的）

> 正文每节的「② 读 Hermes 的代码」只准引一段（≤15 行），撑不住的行号在这里按文件归档。**正文 ② 已逐字引过的那段不在本表重复**（`agent/conversation_compression.py:1-9` 归 §十 ②；四条硬料所引的 `agent/context_engine.py:47-107`、`agent/conversation_loop.py:1166-1170`、`agent/agent_init.py:1761-1769`、`plugins/context_engine/` 零子目录 归正文）。
>
> - **真实侧**锚 `f5d1926110`：`git -C ~/.hermes/hermes-agent show f5d1926110:<path>`
> - **mini 侧**锚 HEAD `7b0b18b`（第 14 章 A1~A6 实现落在 `a6ed019`）：`sed -n 'Np' mini-hermes/<path>`
> - 每条给「该段首 ~20 字」+「它证明什么」；括号里的说明是"它在说什么"，不当逐字引文用。

### 真实侧（锚 `f5d1926110`）

**`agent/context_engine.py`（242 行）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:18-21` | `MEMORY_CONTEXT_MAX_CHARS = 6_000` | `memory_context` 出站的硬预算：6000 字符 = 头 4000 + 截断标记 + 尾 1500（`:29` 的切片正是这三个常量） |
| `:24-29` | `def sanitize_memory_context(memory_context: str) -> str:` | 引擎收到的 `memory_context` 在跨 egress 边界前先过 `redact_sensitive_text(force=True, redact_url_credentials=True)` 再截断 |
| `:32-44` | `def automatic_compaction_status_message(engine, *, phase, default_message, **context):` | 宿主统一门面：`emit_automatic_compaction_status=False` ⇒ 静默；或转调引擎的 `get_automatic_compaction_status_message(...)` 定制；`None` = 什么都不发 |
| `:109-118` | `def prune_tool_results_only(self, messages, current_tokens=None):` | ABC 的默认是 `return messages, 0`——低成本确定性修剪的**默认就是空操作**，不是"只有内置压缩器才有" |
| `:188-197` | `# Reset cross-call calibration state captured under the PREVIOUS model.` | `on_session_reset` 的逐字设计说明：为什么清 `last_prompt_tokens` 要用 `0`（不是 `-1`）、跨模型切换不清理会重放 #23767 那个"切换后超大发送" |
| `:229-234` | `# Model switch / fallback: recompute threshold_tokens (override for more).` | `update_model` 的阈值重算口径：逐模型覆盖（最长子串匹配）+ 配置 percent **只快照一次** |

**`agent/context_compressor.py`（5061 行）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:1674` | `def resolve_model_threshold(` | §八 点名要复用的解析函数（docstring `:1677` 逐字 *"longest matching ``model_thresholds`` key wins, else ``default``"*） |
| `:1782` | `class ContextCompressor(SummaryDispatchMixin, MicroCompactionMixin, ContextEngine):` | 内置实现 = **两个 mixin + ABC**：微压缩不是副本而是混入（回指《源码讲解-10》） |
| `:1786-1788` | `@property` / `def name(self) -> str:` / `return "compressor"` | 内置引擎的 `name`；这个串同时是配置默认值与 mini 的保留名（`mini-hermes/agent/engine_registry.py:90`） |
| `:2502` | `def update_from_response(self, usage: Dict[str, Any]):` | 每次 API 调用后的记账点 |
| `:2579-2596` | `def should_defer_preflight_to_real_usage(self, rough_tokens: int) -> bool:` | 粗糙估算何时该"再等一次真实用量"；含 #2153 的边界（永不 defer ⇒ 压缩可能永不触发） |
| `:2623-2625` | `def _tripped(self) -> bool:` | 反抖动断路器：连续两次无效压缩或连续两次回退摘要 ⇒ 停下（注释逐字给出定义） |
| `:2971-2989` | `def prune_tool_results_only(` | ABC 默认的**重写**：确定性、无 LLM；但"commit 会打破提示词缓存" ⇒ 需 `proactive_prune_min_reclaim_tokens` + 充足回涨跑道，否则原样返回 `(messages, 0)` |
| `:4389` | `def has_content_to_compress(self, messages) -> bool:` | ABC 默认 `True` 的**重写**；网关 `/compress` 预检（无内容可压时免一次 LLM 调用） |
| `:4738` | `def compress(` | 内置实现的 `compress`：签名比 ABC **多** `bypass_cooldown: bool = False`（ABC 里没有这个参数） |
| `:2292` | `def update_model(` | 比 ABC 多一个 `max_tokens` 参数 |

**`agent/conversation_compression.py`**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:668` | `_COMPRESS_EXECUTOR_MAX_WORKERS = 4` | 压缩专用小池的规模（§十） |
| `:691-704` | `def _get_compress_timeout_executor():` | 进程级 `DaemonThreadPoolExecutor(max_workers=4, thread_name_prefix="compress-ctx-timeout")`；`:700` 注释逐字说明为何不是 asyncio 的 `min(32, cpu+4)` |
| `:707-731` | `def resolve_context_compression_timeouts(compression_cfg=None):` | 读 `compression.context_timeout_seconds`（默认 120）；`idle <= 0` 关闭进度感知包装；ceiling 至少夹到一个 idle 窗口 |
| `:1519-1539` | `def _supported_compression_kwargs(` | 向后兼容策略：`inspect.signature` 只传引擎**接受**的 kwargs；不可 introspect 时退化为 `{"current_tokens": current_tokens}`。docstring 逐字解释为何不能"先调再捕 `TypeError`"——有状态压缩器会跑两遍 |

**`agent/agent_init.py`（2356 行）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:1829-1874` | `_build_context_engine` 构造段 | 插件引擎 vs 内置两分支；`:1871-1874` 用 `getattr` + `suppress` 调 `bind_session_state`（插件不实现也无妨） |
| `:1845-1852` | `# Per-model overrides BEFORE the initial update_model() …` | 宿主在首次 `update_model()` **之前**把 `model_thresholds` 赋给引擎（§八 的源码依据） |
| `:1948-1992` | `def _inject_context_engine_tools(agent):` | 引擎工具注入：`:1961-1964` 按 `enabled_toolsets` 门控、`:1966-1968` 现有名字去重、`:1972-1980` 无名 schema 跳过（防 #47707 请求污染）；末尾 `:1988-1992` 顺带发 `on_session_start` |

**`hermes_cli/plugins.py`（2147 行）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:702-721` | `def register_context_engine(self, engine) -> Optional[PluginRegistration]:` | 注册入口**真身**在这一段；已注册 ⇒ `:705-709` 警告并 `return`；`:714` 落槽；`:715-719` 登记可回滚的替换句柄（见附录 C N-⑥） |
| `:1146` | `self._context_engine = None  # Set by a plugin via register_context_engine()` | 单槽位——"同一时刻只有一个引擎"的落地 |
| `:1995-1997` | `def get_plugin_context_engine():` | 读槽 getter |

**`plugins/context_engine/__init__.py`（93 行）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:25-33` | `def load_context_engine(name: str) -> Optional["ContextEngine"]:` | 目录不存在 ⇒ `None`（不抛）；否则走 `load_named` |
| `:36-45` | `def _load_engine_from_dir(engine_dir: Path) -> Optional["ContextEngine"]:` | import 子目录 `__init__.py` → `instance_from_module(..., collected_attr="engine", base_cls=ContextEngine, ...)` |
| `:48-84` | `class _EngineCollector(_loader.NoopPluginContext):` | 捕获 `register_context_engine`；并把 `register_command` 转发到全局插件命令表（带内置命令 / 已注册命令的冲突检测）——文档 §6 没提这条命令路径 |

**`plugins/plugin_loader.py`**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:40-53` | `def iter_plugin_dirs(root: Path) -> List[Path]:` | 发现只要求子目录里**有 `__init__.py`**；跳过 `_`/`.` 开头；排序返回（见附录 C N-①） |
| `:56-64` | `def read_plugin_description(plugin_dir: Path) -> str:` | 只从 `plugin.yaml` 取 `description`；读不到 ⇒ 空串 |
| `:144-160` | `def instance_from_module(mod, *, collector, collected_attr, base_cls, name, logger):` | 提取顺序：`register(ctx)` 优先，其次遍历模块属性找 `base_cls` 子类并**无参** `attr()`（见附录 C N-②） |

**`hermes_cli/plugins_cmd.py`（2213 行）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:1487-1505` | `def _discover_context_engines() -> list[tuple[str, str]]:` | CLI 菜单的引擎清单 = 仓库目录发现 + 插件注册（名冲突时 repo 描述优先） |
| `:1510-1512` | `_get_current_context_engine = functools.partial(_config_str, "context", "engine", default="compressor")` | `context.engine` 的读与写（写为 `_save_context_engine`） |
| `:1520-1521` | `("Context Engine", "compressor", "compressor", lambda: _get_current_context_engine(),` | Provider Plugins 菜单里的类别规格 |

**`hermes_cli/config_defaults.py`（2922 行）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:1204-1206` | `# Context engine — how the context window is managed near the token limit. "compressor" =` | 默认值与注释：引擎可装在 `plugins/context_engine/<name>/` **或** `~/.hermes/plugins/`（第二个发现根，见附录 C-⑤） |
| `:539` / `:550` | `"threshold": 0.50,` / `"protect_last_n": 20,   # minimum recent messages kept uncompressed` | `compression` 块里内置压缩器专属的两项（§八 点名） |
| `:596` | `"context_timeout_seconds": 120,` | §十 的开关：`>0`（默认）⇒ 整趟压缩上池 |
| `:636` | `"model_thresholds": {},` | §八 那个"例外"（属于引擎契约的部分）的默认值 |

**宿主接缝与钩子（`agent/` 其余文件）**

| 文件:行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `agent/turn_request_assembly.py:136-146` | `# Per-turn context selection hook: an engine may select/replace context for THIS` | 次序是**承重**的：`_apply_context_engine_selection`（`:142`）先于 `_sanitize_api_messages`（`:148`）与 `evict_stale_outbound_tool_images`（`:155`） |
| `agent/conversation_loop.py:1181-1225` | `_apply_context_engine_selection` | fail-open 校验：要求**非空** dict 列表（`:1221-1224` 注释逐字点出 `all([])` 为真这个坑） |
| `agent/conversation_loop.py:1227-1244` | `_notify_context_engine_turn_complete` | 观察钩子的宿主侧：给引擎一份**副本**（不能改持久转写本） |
| `agent/turn_finalizer.py:537-546` | `# Context engine observation hook: the turn finished with the finalized transcript.` | `on_turn_complete` 仅从标准收尾接缝触发；`usage` 在没到 provider 响应时按契约给 `None` |
| `agent/tool_executor.py:1559-1565` | `if agent._context_engine_tool_names and function_name in agent._context_engine_tool_names:` | 引擎工具分发：`handle_tool_call(..., messages=messages)`（`messages` = 实时内存列表）；失败走可读 JSON |
| `agent/turn_usage.py:91` / `:125` | `compressor.update_from_response({})` / `compressor.update_from_response(usage_dict)` | `update_from_response` 的 host 调用点（`:120-121` 注释说明先取 boundary latch 再调） |
| `hermes_cli/cli_session_mixin.py:461-483` | `def _launch_session_boundary_memory_flush(` | `/new` 边界：引擎的 `on_session_end` **同步**投递（便宜、顺序敏感，必须在 `reset_session_state()` 重绑引擎之前），memory provider 那一半异步 |

### mini 侧（锚 HEAD `7b0b18b`）

**`mini-hermes/agent/context_engine.py`（112 行；82 → 112）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:1-35` | `"""ContextEngine —— 上下文管理的抽象基类（第 5 章 §1）。` | 模块 docstring 三段：三个约束 / mini 与 Hermes 的差异（`:23-27` 明写"mini 只保留一条发现路径"）/ `name`（第 14 章） |
| `:47-64` | `@property` / `@abstractmethod` / `def name(self) -> str:` | 第 14 章补的抽象属性；docstring 逐字交代"为什么必须是必需项、为什么做成属性"（选择键不能靠类名派生） |

**`mini-hermes/agent/engine_registry.py`（335 行，★ 第 14 章新建）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:1-61` | `"""引擎发现 + 注册 + 选择（第 14 章 A4/A6）。` | 模块 docstring：目录契约（`name == 目录名 == config 值` 三处一致）、宿主调用约定、三条纪律、与上游的差异（`:55-59` 逐条列出不抄的那套：通用插件系统 / 两个发现根 / `deepcopy` 单例 / 线程安全契约） |
| `:83-90` | `ENGINE_INIT_FILENAME = "__init__.py"` | 目录契约常量 + 保留名 `DEFAULT_ENGINE_NAME = "compressor"`（`:90`） |
| `:94-144` | `@dataclass(frozen=True) class EngineEntry:` | 四个数据形状：`EngineEntry` / `SkippedEngine`（名字 + **可读原因**）/ `EngineScan`（能用 + 跳过，两份都确定序）/ `EngineSelection`（含 `fell_back` 与 `warning`；`:136-137` 注释点出 `warning` **不是日志**、必须经 `_emit_status` 打到用户眼前） |
| `:148` / `:158` | `def engines_dir() -> Path:` / `def iter_engine_dirs(root=None) -> List[Path]:` | 根 = `get_mini_home()/"engines"`（唯一来源，与 `skills_dir()` 同构）；排序 + 跳隐藏 / 排除目录 |
| `:177-211` | `def _load_engine_class(engine_dir: Path) -> Tuple[Optional[type], str]:` | `importlib.util` **只在真发现时**载入；只认本模块定义的 `ContextEngine` 子类（单文件约定） |
| `:212-242` | `def read_engine(engine_dir: Path) -> Tuple[Optional[EngineEntry], str]:` | 契约校验五条（软链 / 保留名 / 导入失败 / 无子类 / 无参实例化失败 / `name` 与目录名不符）；不满足 ⇒ 跳过 + 可读原因，**永不抛** |
| `:291-335` | `def select_engine(name=None, *, default_factory, root=None) -> EngineSelection:` | 三分流：默认名 / 空 ⇒ `default_factory()`（静默）；命中 ⇒ 那个引擎；未知名 / 被跳过 ⇒ 回落 + **有声 `warning`**（含被请求名 + 可用清单 + 注册提示）。`:307-310` 注释交代 `default_factory` 为何是**注入**的（不新增第二个构造形状 ⇒ 守住 I4） |

**`mini-hermes/run_agent.py`（1305 行；1271 → 1305）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:1465-1484` | `def _build_default_compressor(self):` | 内置默认构造：**逐字保留改造前的构造参数与顺序**（I4 的载体）；现在是**注**给 `select_engine()` 的工厂，不是被写死调用 |
| `:1485-1523` | `def _compressor(self) -> "Any":` | ★ 唯一构造点：`select_engine(get_context_engine_name(), default_factory=self._build_default_compressor)`；`:1118-1120` 把回落警告经 `_emit_status` 打出来；`_compressor_obj` 缓存语义不变 |

**`mini-hermes/hermes_cli/config.py`（370 行；340 → 370）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:83-93` | `KEY_CONTEXT = "context"` / `DEFAULT_CONTEXT_ENGINE = "compressor"` | `context.engine` 的键名与默认值（与真实侧同名同默认，`:80-82` 注释引 `config_defaults.py:1206`） |
| `:340-353` | `def get_context_engine_name(config=None) -> str:` | 三段式读值，缺省 / 非串 / 空白 ⇒ `"compressor"`，**绝不抛**；docstring 说明默认值为何定在配置层（I4 的载体） |

**`mini-hermes/agent/context_compressor.py`（1046 行；1035 → 1046）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:624-633` | `@property` / `def name(self) -> str:` / `return "compressor"` | 唯一生产实现补上 `name`（照真实侧 `agent/context_compressor.py:1786-1788`）；这个串同时是注册表里的保留名 |

**`mini-hermes/tests/test_context_engine.py`（450 行）/ `mini-hermes/demos/ch14_real_run.py`（228 行）**

| 文件:行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `mini-hermes/tests/test_context_engine.py:5-15` | `## 测的是什么（7 组 = 7 条不变量）` | 七组不变量表（I1 行为可观测 / I2 回落有声 / I3 漏抽象即 `TypeError` / I4 零行为漂移 / I5 宽容计数 / I6 名字唯一 / I7 契约与三处一致） |
| `mini-hermes/tests/test_context_engine.py:17-28` | `## 隔离` / `## 组织方式` | `MINI_HOME` 必须在 import **之前**设；"定义了 `test_` 却没收进 `GROUPS` ⇒ 退出码 2"的自检（与 `tests/test_skills.py` 同款） |
| `mini-hermes/tests/test_context_engine.py:394` | `GROUPS = [` | 七组注册表 |
| `mini-hermes/demos/ch14_real_run.py:2-19` | `"""第 14 章 · 真跑：**引擎真的接管了压缩**（发现 → 选中 → 生效 → 回落），端到端。` | 真跑的承诺口径：不联网、真历史、在**最终历史**与会话库 active 行里找引擎独有的标记文本；六件事逐条列出 |
| `mini-hermes/demos/ch14_real_run.py:28-31` | `# ── 隔离三件套：MINI_HOME 必须在 **import mini 的模块之前** 设好 ─` | `_HOME = Path(tempfile.mkdtemp(prefix="mini_ch14_real_"))` + `os.environ["MINI_HOME"] = str(_HOME)` |
| `mini-hermes/demos/ch14_real_run.py:213` | `record("⑥ ★ 零配置的 `compress()` 输出与**直接构造**逐条相等（零行为漂移）",` | I4 的真跑版 |

**`mini-hermes/agent/skill_utils.py`（436 行，未变）**

| 行号 | 该段首 ~20 字 | 它证明什么 |
|---|---|---|
| `:121-138` | `def skills_dir() -> Path:` / `def iter_skill_dirs(root=None)` | 本轮发现代码的**样板**：`get_mini_home()/"skills"` + 排序 + 跳隐藏 / 排除。`engine_registry.engines_dir()` / `iter_engine_dirs()` 照它写（`mini-hermes/agent/engine_registry.py:85` 注释逐字引 `agent/skill_utils.py:79` 的 `EXCLUDED_SKILL_DIRS`） |

---

## 附录 B · 实测行数

行数一律实测：**真实侧** `git -C ~/.hermes/hermes-agent show f5d1926110:<路径> | wc -l`；**mini 侧** `wc -l mini-hermes/<路径>`。锚：真实侧 `f5d1926110`、mini 侧 HEAD `7b0b18b`（mini 源码最后一次改动是第 14 章实现 `a6ed019`）。

**B-1 · 真实侧（锚 `f5d1926110`）**

| 文件 | 实测行数 | 它是什么 |
|---|---|---|
| `agent/context_engine.py` | **242** | §三 的 `ContextEngine` ABC：4 个抽象成员（`:50-51` 属性 / `:74` / `:83` / `:96-97`）+ 15 个带默认实现的方法 |
| `agent/context_compressor.py` | **5061** | 内置实现 `ContextCompressor`（`:1782`）——契约的**唯一**生产级实现 |
| `tests/agent/test_context_engine.py` | **347** | §九 点名的 "完整的 ABC 契约测试套件" |
| `plugins/context_engine/__init__.py` | **93** | §二/§六 的目录发现加载器；`plugins/context_engine/` 目录下**只有这一个文件**（`git ls-tree -r f5d1926110 -- plugins/context_engine/` 只返回它） |
| `agent/agent_init.py` | **2356** | §六/§七 的配置驱动选择（`_select_context_engine:1761`）+ §五 的工具注入（`_inject_context_engine_tools:1948`） |
| `hermes_cli/plugins.py` | **2147** | §六.2 的注册入口（`register_context_engine:702-721`）；**2147 是文件总行数，不是该方法的行号**（见附录 C N-⑥） |
| `hermes_cli/config_defaults.py` | **2922** | §一/§八 的 `context.engine` 默认（`:1207-1208`）+ §十 的 `compression.context_timeout_seconds`（`:596`，默认 `120`） |
| `agent/conversation_loop.py` | **1683** | §四 两个每回合钩子的宿主实现（`_engine_overrides_hook:1166` / `_apply_context_engine_selection:1181` / `_notify_context_engine_turn_complete:1227`） |
| `agent/turn_request_assembly.py` | **261** | §四 的次序落点（`:136-146`） |
| `agent/turn_finalizer.py` | **674** | §四 观察钩子的触发点（`:537-546`） |
| `agent/tool_executor.py` | **1806** | §五 引擎工具的分发（`:1559-1565`） |

**B-2 · mini 侧（锚 HEAD `7b0b18b`；第 14 章实现 `a6ed019`）**

| 文件 | 实测行数 | 变化（`a6ed019^` → 今） | 它是什么 |
|---|---|---|---|
| `mini-hermes/agent/context_engine.py` | **112** | 82 → 112（**+30**） | 抽象基类；第 14 章补 `name` 抽象属性（`:47-64`） |
| `mini-hermes/agent/engine_registry.py` | **348** | 新建 | 发现 + 注册 + 选择（本章唯一的新概念文件） |
| `mini-hermes/run_agent.py` | **1341** | 1271 → 1341（+34 插槽化，+36 审计修复） | 唯一构造点改查表（`_compressor:1158`）+ 注入式默认工厂（`_build_default_compressor:1570`） |
| `mini-hermes/hermes_cli/config.py` | **370** | 340 → 370（**+30**） | `context.engine` 读取（`get_context_engine_name:340`） |
| `mini-hermes/agent/context_compressor.py` | **1046** | 1035 → 1046（**+11**） | 唯一生产实现补 `name`（`:624-633`） |
| `mini-hermes/tests/test_context_engine.py` | **555** | 新建 | 七组不变量（I1~I7），独立脚本 + `check()` + `GROUPS` |
| `mini-hermes/demos/ch14_real_run.py` | **233** | 新建 | 真跑（发现 → 选中 → 生效 → 回落），不联网、临时 `MINI_HOME` |
| `mini-hermes/agent/skill_utils.py` | **436** | 未变 | 发现样板的出处（`skills_dir:121` / `iter_skill_dirs:133`），`engine_registry` 照它的形状写 |

---

## 附录 C · 上游文档与源码的出入

> 核对锚：上游 commit `f5d1926110`（v0.21.3），与《中文精译-14》同一锚。**C-①~C-④ 与 C-⑤ 是《精译-14》附录 C 已列出的条目（4 处出入 + 1 处说明），本轮逐条回源码复验**（行号与逐字均本轮重新打印核对）；**N-①~N-⑥ 是本轮新发现的**。每条给「文档怎么说 → 源码实际怎么（`文件:行号` + 逐字）→ 影响」。

### C-① · 复验：§1 的 YAML 示例在同一个 `context:` 映射里写了两个 `engine:` 键

- **文档怎么说**：上游 §1 逐字 —— `  engine: "compressor"    # default built-in` 紧接着 `  engine: "lcm"           # activates a plugin engine named "lcm"`。
- **源码实际怎么**：`context` 块只有**一个** `engine` 键 —— `hermes_cli/config_defaults.py:1207-1208` 逐字 `"context": {` / `"engine": "compressor",`；读取处 `agent/agent_init.py:1767` 逐字 `_engine_name = _ctx_cfg.get("engine", "compressor") or "compressor"`（单键 `get`）。YAML 语义里同一映射的重复键是**后键覆盖前键**，一份配置不可能同时是 `compressor` 与 `lcm`。
- **影响**：**复验成立**。示例应读作"两种取值二选一"；逐字照抄会得到一份歧义（无效）的配置。

### C-② · 复验：§3 的抽象方法 `compress()` 只列到 `focus_topic`，源码还多两个参数

- **文档怎么说**：上游 §3 逐字 —— `def compress(self, messages: list, current_tokens: int = None,` / `focus_topic: str = None) -> list:`。
- **源码实际怎么**：`agent/context_engine.py:96-99` 逐字 ——

  ```
  96:     @abstractmethod
  97:     def compress(
  98:         self, messages: List[Dict[str, Any]], current_tokens: Optional[int] = None,
  99:         focus_topic: Optional[str] = None, force: bool = False, memory_context: str = "",
  ```

  且 docstring `:104-105` 逐字 *"…``force`` asks to bypass an engine-owned cooldown; ``memory_context`` is provider text for the handoff prompt. Older engines may omit optional parameters — the host filters them by signature."*
- **影响**：**复验成立**，但性质是**有意的向后兼容**（宿主按签名过滤），不是矛盾。补一处本轮的口径修正：《精译-14》C-2 ② 说"源码还多两个参数"——严格说 **ABC** 比文档多 `force` 与 `memory_context` 两个；而**内置实现**还再多一个 `bypass_cooldown: bool = False`（`agent/context_compressor.py:4738`），这个参数**不在 ABC 里**，属于内置压缩器自加的口子。

### C-③ · 复验：§3.1 "类属性"只列 6 个 token 状态字段，源码另有 4 个未列

- **文档怎么说**：上游 §3.1 列 6 个 —— `last_prompt_tokens` / `last_completion_tokens` / `last_total_tokens` / `threshold_tokens` / `context_length` / `compression_count`。
- **源码实际怎么**：`agent/context_engine.py:55-61` 确有这 6 个（`:55` 注释逐字 `# Token state: engines MUST maintain these; run_agent.py reads them directly.`）；`:62-72` 另有 4 个 —— `:62` 注释逐字 `# Compaction parameters (read by run_agent.py for preflight). protect_first_n counts`、`:67` `threshold_percent: float = 0.75`、`:68` `protect_first_n: int = 3`、`:69` `protect_last_n: int = 6`、`:72` `emit_automatic_compaction_status: bool = True`。
- **影响**：**复验成立**（`漏列` 4 个）。这 4 个同样是"宿主直接读"的契约：`protect_first_n`/`protect_last_n` 决定预检压缩的保护头几位 / 保护尾几条，`emit_automatic_compaction_status` 决定自动压缩的状态消息是否发出。

### C-④ · 复验：§3.2 "可选方法"表是 ABC 默认方法的一个子集，源码还有 5 个默认方法未列

- **文档怎么说**：上游 §3.2 表列 10 项（`on_session_start` / `on_session_end` / `on_session_reset` / `update_model` / `get_tool_schemas` / `handle_tool_call` / `should_compress_preflight` / `get_status` / `select_context` / `on_turn_complete`）。
- **源码实际怎么**：`agent/context_engine.py` 另有 5 个默认（非抽象）方法未列 —— `should_compress_info`（`:87`）、`prune_tool_results_only`（`:109`）、`should_defer_preflight_to_real_usage`（`:159`）、`get_automatic_compaction_status_message`（`:164`）、`has_content_to_compress`（`:175`）。
- **影响**：**复验成立**（`漏列` 5 个）。其中至少两个不只是"多一个可覆盖点"：`should_compress_info` 是**带阻塞原因**的触发判据（默认 `return self.should_compress(prompt_tokens), None`），`prune_tool_results_only` 是**不等 `should_compress` 的低成本修剪**入口——想接这两条能力只能去源码里找。

### C-⑤ · 复验（说明，不算出入）：§2 只写仓库内目录约定，源码注释还给了第二个发现根

- **文档怎么说**：上游 §2 只给 `plugins/context_engine/<name>/` 这一个约定。
- **源码实际怎么**：`hermes_cli/config_defaults.py:1204-1206` 逐字注释 —— `# Context engine — how the context window is managed near the token limit. "compressor" =` / `# built-in lossy summarization; or a plugin name (e.g. "lcm") installed in` / `# plugins/context_engine/<name>/ or ~/.hermes/plugins/.`（`~/.hermes/plugins/` 是**通用插件目录**）。
- **影响**：**复验成立**，属**表述范围**差异（不是冲突）：引擎也可以由通用插件注册（`hermes_cli/plugins.py:702` 的 `register_context_engine`），而不只是走仓库内目录。

### N-① · 新发现：§2 把 `plugin.yaml` 写成目录结构的固定成员并给它 `name`/`version` 两个字段；在上下文引擎这条路径上只有 `description` 被读

- **文档怎么说**：上游 §2 逐字目录树 ——

  ```
  plugins/context_engine/lcm/
  ├── __init__.py      # exports the ContextEngine subclass
  ├── plugin.yaml      # metadata (name, description, version)
  └── ...              # any other modules your engine needs
  ```
- **源码实际怎么**：发现只认 `__init__.py` —— `plugins/plugin_loader.py:41` docstring 逐字 *"Sorted child dirs of *root* that have an ``__init__.py`` (skips ``_``/``.`` names)."*，`:49` 逐字 `if child.is_dir() and (child / "__init__.py").exists():`。`plugin.yaml` 在这条路径上只被 `read_plugin_description`（`:56-64`）打开一次，逐字 `return meta.get("description", "")`——`name`/`version` **没有读取点**。（`plugin.yaml` 的其它读取点都在**通用插件系统**：`hermes_cli/plugin_validate.py:476`、`hermes_cli/memory_setup.py:99`、`hermes_cli/config_migrations.py:276`，与上下文引擎加载无关。）而引擎名来自**目录名**：`plugins/context_engine/__init__.py:39` 逐字 `name = engine_dir.name`。
- **影响**：`plugin.yaml` 在上下文引擎路径上是**可选的**，只影响 CLI 菜单里显示的描述串；缺它不影响发现与实例化，写进去的 `name`/`version` 也不会被引擎系统用到（名字的权威来源是**目录名**与引擎自己的 `name` 属性）。那行注释会让人误以为 `plugin.yaml` 是必需清单。

### N-② · 新发现：§6.1 说"`__init__.py` 必须导出一个 `ContextEngine` 子类"；源码的提取有**两条**路径，且两条的构造约束不同

- **文档怎么说**：上游 §6.1 逐字 —— *"把你的引擎放进 `plugins/context_engine/<name>/`。`__init__.py` 必须导出一个 `ContextEngine` 子类。发现系统会自动找到并实例化它。"*
- **源码实际怎么**：`plugins/plugin_loader.py:146` docstring 逐字 *"Extract the provider instance: ``register(ctx)`` first, then any ``base_cls`` subclass."*；`:147-152` 先 `mod.register(collector)` 再取 `collector.engine`；`:155-159` 才遍历模块属性找子类并 `attr()` **无参**实例化。也就是说引擎模块也可走 `register(ctx)` + `ctx.register_context_engine(engine)` 那条，而这时**可以带参构造**——文档 §6.2 的例子正是 `LCMEngine(context_length=200000)`；只有"导出子类"那条路被强制无参。捕获器就是 `plugins/context_engine/__init__.py:48-57` 的 `_EngineCollector`（`:56-57` 逐字 `def register_context_engine(self, engine):` / `self.engine = engine`）。
- **影响**：两条路的**构造参数约束不同**。照 §6.1 只导出子类的引擎必须能无参构造（否则 `attr()` 抛异常、`instance_from_module` 静默返回 `None`，表现为"装了但没发现"）；想带参就得自己提供 `register(ctx)`。文档没交代这条区别。

### N-③ · 新发现：§5 说引擎工具"启动时被注入工具列表、自动分发、无需注册表注册"；源码这条注入有 `enabled_toolsets` 门控 + 去重 + 无名跳过三道处理

- **文档怎么说**：上游 §5 逐字 —— *"引擎工具在启动时被注入 agent 的工具列表，并自动分发 —— 无需注册表注册。"*
- **源码实际怎么**：`agent/agent_init.py:1961-1964` 逐字 ——

  ```
  1961:     if (
  1962:         agent.context_compressor
  1963:         and agent.tools is not None
  1964:         and (agent.enabled_toolsets is None or "context_engine" in agent.enabled_toolsets)
  ```

  `:1949-1951` 注释逐字 *"…gated on enabled_toolsets so ``platform_toolsets: telegram: []`` can't leak them."*；`:1982-1983` 与已有工具名去重（逐字 `if _tname in _existing_tool_names:` / `continue  # already registered via plugin/cache path`）；`:1972-1980` 无名 schema 跳过（逐字 *"Context engine returned a tool schema with no resolvable name; skipping to avoid poisoning the request (%r)"*，见 #47707）。
- **影响**：**"注入"不是无条件的**——`enabled_toolsets` 里没有 `context_engine` 时引擎工具**一个都不进**工具列表。文档省略了这道闸门，于是"我的 `lcm_grep` 怎么没出现"这个问题只看 §5 是找不到原因的（要去看 `enabled_toolsets` / `platform_toolsets`）。

### N-④ · 新发现：§8 说 `compression` 块是"内置专属（只有一个例外）"，§10 又依赖 `compression.context_timeout_seconds`——它管的正是插件引擎那一趟

- **文档怎么说**：§8 逐字 —— *"`compression` 配置块（`compression.threshold`、`compression.protect_last_n` 等）是内置 `ContextCompressor` 专属的，有一个明确的例外：`compression.model_thresholds`…"*；§10 逐字 —— *"当 `compression.context_timeout_seconds > 0`（默认值）时…包括你的引擎的 `compress()`…"*。
- **源码实际怎么**：`compression.context_timeout_seconds` 由**宿主**读，读点在 `agent/conversation_compression.py:707-727`（`resolve_context_compression_timeouts`，逐字 `if cfg.get("context_timeout_seconds") is not None:` / `idle = float(cfg["context_timeout_seconds"])`），它决定的是 `run_compress_context_with_progress_timeout`（`:1022`）这条**与引擎实现无关的外壳**；默认值 `hermes_cli/config_defaults.py:596` 逐字 `"context_timeout_seconds": 120,`。
- **影响**：`compression` 块**不是**"内置专属"——至少还有第二个跨引擎的键（超时），且它开关的正是 §10 那条线程安全前提。把它读成"插件引擎不用管 `compression` 块"会漏掉这条；文档两节的口径在这里是不一致的。

### N-⑤ · 新发现（未能确认）：§7 生命周期第 6 步把 `on_session_end()` 的时机写成含 "gateway shutdown"；gateway 目录里找不到调用点

- **文档怎么说**：上游 §7 逐字 —— `6. on_session_end() — session boundary (CLI exit, /reset, gateway shutdown)`。另 `agent/context_engine.py:8` 的 ABC docstring 逐字 *"…at real session boundaries only (CLI exit, /reset, gateway expiry), never per-turn."*（一处写 `shutdown`、一处写 `expiry`）。
- **源码实际怎么**：明确的两处是 `hermes_cli/cli_session_mixin.py:479-481`（`/new` 边界，逐字 `if engine is not None and hasattr(engine, "on_session_end"):` / `engine.on_session_end(session_id or "", history_snapshot)`）与 `run_agent.py:372`（`_transition_context_engine_session`）。本轮在 `gateway/*.py` 里**未能找到**对引擎 `on_session_end` 的调用点——**可能**经 `run_agent` 的 transition 间接触发，但未逐行坐实。
- **影响**：**未能确认**，不作断言。写自定义引擎时不要依赖"gateway 过期一定会收到 `on_session_end`"这条假设；反过来，`on_session_end` 里做的事如果在 gateway 场景下真的依赖它，需自行核验。

### N-⑥ · 新发现（口径钉死）：`register_context_engine` 的"真身行号" vs 常被混用的文件总行数

- **说明**：`hermes_cli/plugins.py` 的**文件总行数**是 **2147**（本轮 `git show f5d1926110:hermes_cli/plugins.py | wc -l` 实测），它**不是**注册入口的行号。入口真身是 `hermes_cli/plugins.py:702-721` —— `:702` 逐字 `def register_context_engine(self, engine) -> Optional[PluginRegistration]:`（被 `:701` 的 `@_serialized_replacement` 装饰），`:705-708` 拒绝第二次注册并逐字警告 `"Plugin '%s' tried to register a context engine, but one is already registered. Only one context engine plugin is allowed."`，`:714` 逐字 `self._manager._context_engine = engine`，`:715-719` 登记可回滚句柄。单槽位初始化在 `:1146`（逐字 `self._context_engine = None  # Set by a plugin via register_context_engine()`），getter 在 `:1995-1997`。
- **影响**：把文件总行数当方法行号会指到文件末尾的无关代码。同理，`hermes_cli/plugins_cmd.py`（**2213** 行）里的引擎相关代码在 `:1487-1521`，不是 2213。
