# 第 4 章 · Tools Runtime（工具运行时）

> 官方原文：`官方文档-Developer-Guide/tools-runtime.md`（250 行）
> 对照源码：`tools/registry.py`（977 行）、`model_tools.py`（987 行）、`toolsets.py`（485 行）、
> `tools/approval.py`（1336 行）+ 7 个 `approval_*.py` 兄弟、`tools/environments/*`、
> `agent/inline_tool_executors.py`（241 行）
>
> 📌 **本文里 mini 版的数字（行数 / 工具数 / 测试条数）锚的是标签 `ch04-tools-runtime`**
> —— 也就是第 4 章末的状态。行号会随章节漂移，标签不会：
> `git show ch04-tools-runtime:mini-hermes/tools/registry.py`。

[TOC]

---

## 0. 这一章的一句话

**工具不是被"调用"的，是被"发现"的。**

前两章里，模型和工具的接口是"名字 → 一个函数"。这一章回答：
**那个映射表是怎么长出来的？谁往里放东西？放进去的东西为什么会在某些时候消失？**

原文的一句话概括：

> Hermes tools are self-registering functions grouped into toolsets and executed through a
> central registry/dispatch system.
>
> （Hermes 的工具是**自注册**的函数，按 toolset 分组，通过一个中心化的注册表/分发系统执行。）

三个词就是全章骨架：**自注册**（§1-§2）→ **分组**（§3-§4）→ **分发**（§5-§8）。

---

## 1. 工具注册模型（`registry.register()`）

### 原文

每个工具模块在 **import 时**调用 `registry.register(...)` 声明自己。
`model_tools.py` 负责 import/发现这些模块，并构建给模型看的 schema 列表。

原文给的签名：

```python
registry.register(
    name="terminal",               # 唯一工具名（用在 API schema 里）
    toolset="terminal",            # 这个工具属于哪个 toolset
    schema={...},                  # 面向模型的 schema（描述 + 参数）
    handler=handle_terminal,       # 模型调用时执行的函数
    check_fn=check_terminal,       # 可选：返回 True/False 表示可用性
    requires_env=["SOME_VAR"],     # 可选：依赖的环境变量（给 UI 显示用）
    is_async=False,                # handler 是不是 async 协程
    description="Run commands",    # 可选：ToolEntry 的注册表元数据
    emoji="💻",                    # 转圈/进度显示用的 emoji
)
```

每次调用创建一个 `ToolEntry`，存进单例 `ToolRegistry._tools` 这个以工具名为 key 的 dict 里。

**覆盖语义**（原文）：

> A registration that would shadow an existing tool from a **different** toolset is rejected
> (with an error log) unless the caller passes `override=True`; plugin overrides of built-in
> tools additionally require the operator opt-in
> `plugins.entries.<plugin_id>.allow_tool_override: true` in `config.yaml`.

（想覆盖**另一个 toolset** 里同名的工具会被拒绝并记日志，除非显式传 `override=True`；
插件要覆盖内置工具还额外需要运维显式开启配置项。）

**关于 `description=` 和 `schema["description"]`**（原文专门讲了一段）：

> `schema["description"]` is the authoritative model-facing description. The separate
> `description=` argument populates `ToolEntry.description`; when it is omitted, the registry
> metadata falls back to the schema description. `get_definitions()` builds the OpenAI function
> definition from `entry.schema` and does not copy `entry.description` into a schema that lacks
> `description`. Therefore, `description=` alone does not describe the tool to the model, and
> when both values differ the model sees the schema value.

（**`schema["description"]` 才是模型看到的那份**。`description=` 参数只填 `ToolEntry.description`
这个注册表元数据；省略时元数据回退到 schema 的值。`get_definitions()` 是从 `entry.schema`
构建 OpenAI 定义的——**它不会把 `entry.description` 塞进缺 `description` 的 schema**。
所以：光写 `description=` 不会描述给模型；两边不一样时，模型看到的是 schema 那份。）

原文的结论：**除非有注册表消费者确实需要不同的元数据，否则描述只在 schema 里写一次。**

### 为什么这么设计 / 砍掉会怎样

**为什么"自注册"而不是"一张中央表列所有工具"？**

想想一张中央表会长什么样：

```python
TOOLS = {
    "terminal": handle_terminal,
    "read_file": handle_read_file,
    # ... 59 个
}
```

它的问题不是难看，是**耦合方向反了**：这张表必须 import 每个工具的实现，
于是"加工具"变成"改中央表"——而改中央表的那个文件会变成**所有人的冲突点**。
更糟的是：**表里有一个名字写错，只在运行时暴露**。

自注册把耦合方向倒过来：**工具知道注册表，注册表不认识任何具体工具**。
`tools/registry.py` 是个 **零依赖**（`tools/AGENTS.md`：*has no deps and is imported by
every tool file*）——它 import 的东西只有标准库。

**砍掉会怎样**：你会得到一张手工维护的表 + 一个永远在涨的 import 列表。
在第 3 章我们已经在别的地方见过这个模式的代价（`if/elif` 链条漂移分家）。
这里多一个具体后果：**工具文件之间开始互相 import**（因为把它们串起来的那个文件得认识所有人）。

---

## 2. 发现机制（`discover_builtin_tools()`）

### 原文

> When `model_tools.py` is imported, it calls `discover_builtin_tools()` from `tools/registry.py`.
> This function scans every `tools/*.py` file using AST parsing to find modules that contain
> top-level `registry.register()` calls, then imports them.

（`model_tools.py` 被 import 时调用 `discover_builtin_tools()`，它用 **AST 解析**扫每个
`tools/*.py`，找出含顶层 `registry.register()` 调用的模块，然后 import 它们。）

原文给的（简化）实现：

```python
# tools/registry.py (simplified)
def discover_builtin_tools(tools_dir=None):
    tools_path = Path(tools_dir) if tools_dir else Path(__file__).parent
    for path in sorted(tools_path.glob("*.py")):
        if path.name in {"__init__.py", "registry.py", "mcp_tool.py"}:
            continue
        if _module_registers_tools(path):  # AST check for top-level registry.register()
            importlib.import_module(f"tools.{path.stem}")
```

原文的三条要点：

1. **自动发现意味着新工具文件会被自动捡起——没有手工名单要维护。**
2. AST 检查**只匹配顶层** `registry.register()`（不匹配函数内部的调用），
   所以 `tools/` 里的辅助模块不会被 import。
3. 可选工具（比如图像生成缺 `fal_client`）的 import 错误被**捕获并记日志**，
   不会阻止其他工具加载。

然后原文列出了核心发现之后的**两批额外发现**：

1. **MCP 工具** —— `tools.mcp_tool_discovery.discover_mcp_tools()` 读 MCP 服务器配置，
   注册来自外部服务器的工具。
2. **插件工具** —— `hermes_cli.plugins.discover_plugins()` 加载用户/项目/pip 插件。

### 为什么这么设计 / 砍掉会怎样

**为什么用 AST 检查，而不是"让每个文件自己登记，注册表扫一遍"？**

因为**那还是需要一份名单**。任何方案只要需要"知道有哪些文件"，就要么读目录、要么读名单。
读目录之后的问题是：**读到的哪些文件是"工具"？**

- 靠命名约定 → 约定会被破坏
- 靠 try/except import 全试一遍 → **import 有副作用**，试错的代价是执行了不该执行的代码
- **靠"这个文件的模块体里有没有那句调用"** → 精确、无副作用（`ast.parse` 不执行代码）

**AST 检查的另一个好处是它是"静态"的**：不需要运行任何工具代码就能知道
"这个文件是不是工具模块"。所以它可以被缓存（§2④），而缓存的前提正是"判断不依赖运行时状态"。

**砍掉会怎样**：回到手工名单。在 258 个文件的规模下，一份手工名单会长期处于
"部分过期"的状态——而且**过期是静默的**。

---

## 3. 工具可用性检查（`check_fn`）

### 原文

每个工具可以可选地提供 `check_fn`——一个返回 `True`（可用）/ `False`（不可用）的可调用。
典型检查：

- **API key 在不在** —— 比如 web search 的 `lambda: bool(os.environ.get("SERP_API_KEY"))`
- **服务在不在跑** —— 比如 Honcho 服务器配好了没
- **二进制装了没** —— 比如浏览器工具要的 `playwright` 在不在

`registry.get_definitions()` 构建 schema 列表时会跑每个工具的 `check_fn()`：

```python
# Simplified from registry.py
if entry.check_fn:
    try:
        available = bool(entry.check_fn())
    except Exception:
        available = False   # Exceptions = unavailable
    if not available:
        continue            # Skip this tool entirely
```

原文的三条关键行为：

- 检查结果**按调用缓存**（per-call）——如果多个工具共用同一个 `check_fn`，它只跑一次
- `check_fn()` 里的异常被当作"不可用"（**fail-safe**）
- `is_toolset_available()` 检查某个 toolset 的 `check_fn` 过不过，用于 UI 显示和 toolset 解析

### 【源码】真身 —— 这一节是本章**差异最大**的地方

原文说"**cached per-call**"（按调用缓存）。真实实现是一套**按 profile 分键的进程级 TTL 缓存**：

```python
# tools/registry.py:222
_CHECK_FN_TTL_SECONDS = 30.0
_CHECK_FN_FAILURE_GRACE_SECONDS = 60.0
_CHECK_FN_CACHE_MAX = 512
CHECK_FN_CACHE_BYPASS = ""          # 哨兵：强制不走缓存
```

| 常量 | 值 | 含义 |
|---|---|---|
| `_CHECK_FN_TTL_SECONDS` | **30 秒** | 成功结果存活 30s |
| `_CHECK_FN_FAILURE_GRACE_SECONDS` | **60 秒** | **抖动保护窗口**（**不是**"失败也缓存 60 秒"；详见 §9③ 与《源码讲解-04》附录 B） |
| `_CHECK_FN_CACHE_MAX` | **512** | 缓存条目上限 |
| `CHECK_FN_CACHE_BYPASS` | `""` | 一个哨兵值，用它可以**绕过缓存** |

### 为什么这么设计 / 砍掉会怎样

**`check_fn` 存在的根本原因**是 `narrow waist` 那条铁律（`AGENTS.md`）：

> Every model tool is sent on every API call, so the bar for a new *core* tool is high.

（**每个模型工具都会在每次 API 调用里被发出去**，所以新加核心工具的门槛很高。）

这句话反过来读就是 `check_fn` 的用途：**既然发出去的成本那么高，那就想清楚再发**。

- 没配 `SERP_API_KEY` → `web_search` 的 schema 就不该出现在请求里
- 没装 `playwright` → 浏览器工具那一堆 schema 全是浪费

**砍掉会怎样**：模型会看到一堆它**永远调不通**的工具。
后果不是报错（调用时才知道不行），是**模型把预算花在试错上**——
它会去调、拿到错误、再换一个。每一次都是真金白银的 token。

> 📌 深层理由：**schema 本身也是提示词的一部分。**
> 发 59 个工具 vs 发 20 个可用工具，对模型的注意力分配是两回事。
> 这是第 3 章"提示词装配"在第 4 章的延续——**schema 是工具层的提示词**。

---

## 4. Toolset 解析

### 原文

Toolset 是**具名的工具捆**。Hermes 通过以下途径解析：

- 显式的启用/禁用 toolset 列表
- **平台预设**（`hermes-cli`、`hermes-telegram` 等）
- 动态 MCP toolset
- 定制用途的集合（如 `hermes-acp`）

#### `get_tool_definitions()` 的过滤流程（原文）

主入口是 `model_tools.get_tool_definitions(enabled_toolsets, disabled_toolsets, quiet_mode)`：

1. **给了 `enabled_toolsets`** —— 只包含这些 toolset 里的工具。每个名字经 `resolve_toolset()`
   展开成单个工具名（复合 toolset 会被展开）。
2. **给了 `disabled_toolsets`** —— 从**全部** toolset 开始，减掉禁用的。
3. **都没给** —— 包含全部已知 toolset。
4. **注册表过滤** —— 解析出的工具名集合传给 `registry.get_definitions()`，
   它施加 `check_fn` 过滤并返回 OpenAI 格式的 schema。
5. **动态 schema 修补** —— 过滤之后，`execute_code` 和 `browser_navigate` 的 schema 会被
   **动态调整**，只引用**真的通过了过滤的**工具（防止模型幻觉出不可用的工具）。

#### 旧名字（原文）

带 `_tools` 后缀的旧 toolset 名（如 `web_tools`、`terminal_tools`）通过
`_LEGACY_TOOLSET_MAP` 映射到现代工具名，用于向后兼容。

### 为什么这么设计 / 砍掉会怎样

**为什么要有 toolset 这一层，而不是直接给"工具名白名单"？**

因为"按名字列举"和"按用途分组"回答的是不同的问题：

| 需求 | 用名字列表 | 用 toolset |
|---|---|---|
| "Telegram 上不要浏览器工具" | 要列 18 个名字，且**新增浏览器工具会漏** | 一句 `browser` |
| 平台预设 | 每个平台维护一份长名单 | 继承 `_HERMES_CORE_TOOLS` + 加减 |
| 别名兼容 | 做不到 | `_LEGACY_TOOLSET_MAP` |

**关键差别还是"会不会漏"**：按名字列举的名单会**随着工具增加而过期**，
而过期是静默的（第 2 章第 3 次出现的同一形状）。Toolset 是**间接一层**，
所以新增工具**默认落在它所属的 toolset 里**，不需要改任何名单。

**砍掉会怎样**：每个平台都要维护自己的完整工具名清单。59 个核心工具 × 多个平台，
每加一个工具就要改 N 个地方——这是"手工名单"问题的放大版。

---

## 5. 分发（Dispatch）

> 📌 **和第三章的接缝**：工具清单**不是**提示词的一部分——它是 API 请求 payload 的
> **另一个顶层字段**（和 `messages` 平级）。但它**和三层提示词共享同一个缓存前缀**，
> 而且"换工具集"与"重建提示词"被绑成**同一个失效事件**（提示词里埋了"能力指纹"，
> 对不上就故意重建一次）。所以它不属于 volatile 层，却和 volatile 层受同一个约束管。
> 展开见《源码讲解-04-ToolsRuntime.md》§五第一小节。

### 原文

> At runtime, tools are dispatched through the central registry, with agent-loop exceptions
> for some agent-level tools such as memory/todo/session-search handling.

（运行时工具经中心注册表分发，少数 agent 级工具（memory/todo/session-search 这类）走
agent loop 例外。）

#### 原文给的流程

原文这里有个小标题和一句引子（这些精译里之前漏了）：

> **Dispatch flow: model tool_call → handler execution**
> When the model returns a `tool_call`, the flow is:

（**分发流程：模型的 tool_call → 真正执行 handler**。当模型返回一个 `tool_call` 时，流程如下：）

```
Model response with tool_call
    ↓
agent loop (`agent/conversation_loop.py`, via `run_agent.py`'s `AIAgent` facade)
    ↓
model_tools.handle_function_call(name, args, task_id, user_task)
    ↓
[Agent-loop tools?] → handled directly by agent loop (todo, memory, session_search, delegate_task)
    ↓
[Plugin pre-hook] → invoke_hook("pre_tool_call", ...)
    ↓
registry.dispatch(name, args, **kwargs)
    ↓
Look up ToolEntry by name
    ↓
[Async handler?] → bridge via _run_async()
[Sync handler?]  → call directly
    ↓
Return result string (or JSON error)
    ↓
[Plugin post-hook] → invoke_hook("post_tool_call", ...)
```

每个节点的中文意思（**方括号 = 判断/分支**，不是顺序执行的一步）：

| 原文节点 | 中文 |
|---|---|
| Model response with tool_call | 模型返回了带 `tool_call` 的响应 |
| agent loop (`agent/conversation_loop.py`, via `run_agent.py`'s `AIAgent` facade) | 进入 agent loop（真身在 `agent/conversation_loop.py`，经 `run_agent.py` 的 `AIAgent` 门面进去） |
| `model_tools.handle_function_call(name, args, task_id, user_task)` | 调 `handle_function_call`，参数工具名、参数、任务 id、用户任务 |
| `[Agent-loop tools?]` → handled directly by agent loop (todo, memory, session_search, delegate_task) | 【是不是 **agent 级工具**？】→ 是则**由 agent loop 直接处理**，不往下走。原文点名四个：`todo` / `memory` / `session_search` / `delegate_task` |
| `[Plugin pre-hook]` → `invoke_hook("pre_tool_call", ...)` | 【**插件前置钩子**】→ 触发 `pre_tool_call` |
| `registry.dispatch(name, args, **kwargs)` | 交给注册表分发 |
| Look up ToolEntry by name | 按名字查出那条 `ToolEntry` |
| `[Async handler?]` → bridge via `_run_async()` | 【handler 是 **async** 的？】→ 经 `_run_async()` **桥接**到同步路径 |
| `[Sync handler?]` → call directly | 【是**同步**的？】→ 直接调用 |
| Return result string (or JSON error) | 返回结果字符串（**或 JSON 错误**） |
| `[Plugin post-hook]` → `invoke_hook("post_tool_call", ...)` | 【**插件后置钩子**】→ 触发 `post_tool_call` |

> 📌 这张图是**原文的**，所以有两处要和真实源码分开看（细节见 §9 不一致清单）：
> ① 第 4 个节点写"四个 agent 级工具被拦截"——实际是**两层集合**，
> `INLINE_TOOL_EXECUTORS` **14 个** ⊇ `_AGENT_LOOP_TOOLS` **4 个**；
> ② `handle_function_call` 的真实顺序是 **11 步**，不是这张图里的 6 步。

#### 错误包装（原文）

所有工具执行都**两层**包了错误处理：

1. **`registry.dispatch()`** —— 捕获 handler 抛的任何异常，返回
   `{"error": "Tool execution failed: ExceptionType: message"}` 这个 JSON。
2. **`handle_function_call()`** —— 把整个 dispatch 再套一层 try/except，返回
   `{"error": "Error executing tool_name: message"}`。

> This ensures the model always receives a well-formed JSON string, never an unhandled exception.
> （这保证模型**永远能拿到格式良好的 JSON 字符串**，而不会是未捕获的异常。）

#### agent 级工具（原文）

**四个**工具在注册表分发**之前**被拦下来，因为它们需要 agent 级状态（TodoStore、MemoryStore 等）：

- `todo` —— 规划/任务跟踪
- `memory` —— 持久记忆写入
- `session_search` —— 跨会话回忆
- `delegate_task` —— 派生子 agent 会话

这些工具的 schema **仍然注册在注册表里**（给 `get_tool_definitions` 用），
但它们的 handler 在分发真的到达时返回一个**stub 错误**。

#### async 桥接（原文）

handler 是 async 时，`_run_async()` 把它桥到同步分发路径上：

- **CLI 路径（没有跑着的 loop）** —— 用一个**常驻事件循环**，好让缓存住的 async client 活着
- **网关路径（有跑着的 loop）** —— 起一个**一次性线程**跑 `asyncio.run()`
- **worker 线程（并发工具）** —— 用存在线程局部存储里的**每线程常驻 loop**

### 为什么这么设计 / 砍掉会怎样

**为什么错误包装要"两层甚至三层"？** 原文给了直接理由：
*This ensures the model always receives a well-formed JSON string.*

但更根本的理由是**工具是这个系统里唯一"用户代码也能写"的部分**（第 5 章会讲插件）：

- 核心工具的异常，开发时就会发现
- **插件工具的异常，只在那个人用的时候才发现**

所以"工具调用**不可能**把异常漏到 agent loop 里"必须是一条**结构性保证**，
而不是"每个工具都记得 try/except"。对照 `agent/inline_tool_executors.py` 的模块 docstring
（第 2 章读过）：它取代了 `invoke_tool` 和 `execute_tool_calls_sequential` **两条已经漂移分家**的
if/elif 链——同一个问题：**"要求每个地方都做对"会被漂移打败，"结构上不可能做错"不会。**

**砍掉会怎样**：一个工具抛异常 → 异常冒到 agent loop →
要么整个 turn 崩（用户看到 traceback），要么被外层的某个宽泛 except 吞掉（**静默失败**）。
两种都糟，而且第二种更糟。

---

## 6. 危险命令审批（`DANGEROUS_PATTERNS`）

### 原文

terminal 工具集成了定义在 `tools/approval.py` 里的危险命令审批系统：

**1. 模式检测** —— `DANGEROUS_PATTERNS` 是一个 `(regex, description)` 元组的列表，
覆盖破坏性操作：

- 递归删除（`rm -rf`）
- 文件系统格式化（`mkfs`、`dd`）
- SQL 破坏性操作（无 `WHERE` 的 `DROP TABLE`、`DELETE FROM`）
- 系统配置覆写（`> /etc/`）
- 服务操作（`systemctl stop`）
- 远程代码执行（`curl | sh`）
- fork 炸弹、进程 kill 等

**2. 检测** —— 执行任何终端命令前，`detect_dangerous_command(command)` 对着所有模式检查。

**3. 审批提示** —— 命中时：

- **CLI 模式** —— 交互式提示，让用户批准、拒绝、或永久允许
- **网关模式** —— 一个 async 审批回调把请求发到消息平台
- **Smart 审批** —— 可选地，一个**辅助 LLM** 可以自动批准命中模式但低风险的命令
  （例如 `rm -rf node_modules/` 是安全的，但它命中了"递归删除"）

**4. 会话状态** —— 审批**按会话**跟踪。一旦你为某个会话批准了"递归删除"，
后续的 `rm -rf` 不再重复提示。

**5. 永久允许清单** —— "永久允许"选项把模式写进 `config.yaml` 的 `command_allowlist`，
跨会话持久。

### 为什么这么设计 / 砍掉会怎样

**为什么"审批"这件事必须做成独立的一层，而不是工具内部的一个 if？**

因为审批要处理的**四种"谁来回答"**里，有三种根本不在工具进程里：

```
CLI            → 人在同一个终端 → 同步问
网关           → 人在 Telegram → 跨进程、异步、可能要等几分钟
Smart 审批     → 辅助 LLM → 一次额外 API 调用
永久允许清单   → 上次已经决定过了 → 读配置
```

工具不该（也不能）知道这些。所以分界线是：

- **工具说**："这个调用危险"（`danger_check`）
- **审批层说**："那么接下来问谁、等多久、记不记得"

**砍掉会怎样**：每个工具自己弹窗、自己等、自己决定记不记得。
后果是**审批策略无法统一**——用户没法说"以后所有 rm -rf 都问我"，
因为每个工具的"记住"是独立的。

---

## 7. 终端 / 运行时环境

### 原文

终端系统支持**多个后端**：

- local
- docker
- ssh
- singularity
- modal
- daytona
- vercel_sandbox

它还支持：

- 每任务的 cwd 覆盖
- 后台进程管理
- PTY 模式
- 危险命令的审批回调

原文接下来用一整段讲了 `tools/process_registry_checkpoint.py` 和
`tools/process_registry_results.py` 的分工（这段是文档里**最"密"**的一段）：

> `tools/process_registry_checkpoint.py` owns running-process checkpoints and PID-safe
> adoption. Completed output is separate: `tools/process_registry_results.py` writes one
> atomic, redacted receipt per process under the profile's `logs/process-results/`.
> Producers cannot overwrite another parent's results by rewriting the shared PID checkpoint.
> The registry persists the receipt before releasing its completion event; one-shot linger
> waits on that event. The existing process query methods load retained snapshots without
> adopting PIDs or enqueuing notifications. Reads require the commissioning durable session
> or its compression continuation; knowing a handle alone does not authorize a retained
> result read. The registry captures that owner before starting any output reader, including
> on CLI and non-notifying processes, and preserves the producer's profile context in reader
> threads. Receipt redaction is forced independently of live-output opt-out; retention is
> bounded by age and count.

这一段逐句翻译见 §7.2 ——它值得单拆，因为**每一句都在防一个具体的竞态或越权**。

### 为什么这么设计 / 砍掉会怎样

**为什么要多后端？** 最简单的答案是"有人要用 docker 隔离"。
但更准确的是**三个互相独立的需求**：

| 需求 | 要的后端 |
|---|---|
| 沙箱（别把我机器搞坏） | docker / singularity / vercel_sandbox |
| 算力（跑不动的活扔出去） | modal / daytona |
| 环境（我的代码在服务器上） | ssh |

**这三个需求要求的"环境语义"完全不同**：

- 沙箱 → **默认 cwd 必须是容器里的**（宿主机的路径在容器里可能不存在）
- 算力 → **文件要先同步过去**（`file_sync.py`）
- 环境 → **默认 cwd 是远程的家**（`"ssh": "~"`）

所以"多后端"不是一个功能，是**一组语义各不相同的环境**。
**砍掉会怎样**：所有工具都假设"我在用户机器上"——
于是任何隔离/远程需求都变成"你自己去装个 VM"。

---

## 8. 并发（原文只有一句话）

### 原文

> Tool calls may execute sequentially or concurrently depending on the tool mix and
> interaction requirements.
>
> （工具调用可能串行、也可能并发执行，取决于工具的组合和交互要求。）

**这一节原文就这一句。** 而这一句话背后是第 2 章花了一整节讲的东西。

### 【源码】真身

**并发决策不在这一章，在第 2 章。** 回顾一下第 2 章的结论：

```
_plan_tool_batch_segments  → 把批次拆成有序的 ("parallel"|"sequential", calls) 段
    三层判据：
      ① 屏障 —— _NEVER_PARALLEL_TOOLS / 参数不可解析 / 非并行安全
      ② 路径冲突 —— 路径重叠且至少一个写者 → 关闭并行段（读↔读仍然并行）
      ③ 降级与合并 —— 一段 <2 个调用 → 降级串行；相邻串行段合并
_should_parallelize_tool_batch → "planner 是否给出整批单一并行段"
```

代码位置：`agent/tool_batch_planner.py`、`agent/tool_dispatch_helpers.py`。

而这一章该补的是**另一个角度**：那些判据里的"非并行安全"是**哪来的**？

答案就在本章 §1：**它是注册时声明的**。对照 mini 版：

```python
# mini-hermes/tools/registry.py 的 register_tool 参数
interactive: bool = False,   # interactive=True 的工具会强制整批工具串行执行
```

**`interactive` 是"这个工具不能和别人并发"的唯一声明处。**

> 📌 所以"并发"这一节其实是**跨两章的**：
> - **声明**在工具层（本章）：`interactive` / 并行安全性
> - **决策**在 loop 层（第 2 章）：分段 planner
>
> 开发者文档把"并发"放在 tools-runtime 里只给一句话，**从分章角度是合理的**
> （声明确实属于工具层），但它**没有指回去**告诉读者"决策在哪"。
> 读者按这一章找并发实现会一无所获。

### 为什么这么设计 / 砍掉会怎样

一句话版：**并发安全性是工具的属性，并发决策是 loop 的职责。**
把两者混在一起（让工具自己决定"我能不能并发"）就会出现"每个工具都在猜别人在干什么"。

---

## 9. ⚠️ 文档与源码不一致（本章发现 **9 处**）

按"改错了会不会静默出错"排序（第 3 章那条判据）：

| # | 文档说 | 源码是 | 严重度 |
|---|---|---|---|
| ① | **"四个工具**被拦截"（`todo`/`memory`/`session_search`/`delegate_task`） | **两层集合：`INLINE_TOOL_EXECUTORS` 14 个 ⊇ `_AGENT_LOOP_TOOLS` 4 个**。文档只说了外层，而且把 `todo_list` 写成了旧名 `todo` | ★★★ 按文档实现会漏 10 个工具 |
| ② | 注册 ≠ 生效：文档**没提** toolset 是必经的暴露闸门 | *discovery registers the schema, but a tool is **only exposed if a toolset names it*** —— 加工具必须改 `toolsets.py` | ★★★ 按文档做工具 → 工具存在但模型永远看不见 |
| ③ | `check_fn` 结果 "**cached per-call**" | 进程级 **TTL 缓存**：30s / **60s 抖动保护窗口** / 512 上限 / **按 `hermes_home_key()` 分 profile** / 可 bypass；还有 `_check_fn_last_good` 和分级日志 | ★★★ 抖动保护是"能力不会忽然消失"的保证，漏了会做出相反的行为 |
| ④ | `DANGEROUS_PATTERNS` 定义在 `tools/approval.py` | **`tools/approval_detection.py:203`**（107 条，且有预编译版本 `DANGEROUS_PATTERNS_COMPILED`） | ★★ 按文档找代码会白读 1336 行 |
| ⑤ | 动态 schema 修补只有"`execute_code` 和 `browser_navigate`" | **`_DYNAMIC_SCHEMA_REWRITERS` 10 项** | ★ 漏掉的 8 项不需要知道细节，但"只有 2 个"这个印象会让人以为这是特例 |
| ⑥ | `register()` 签名 9 个参数 | **13 个**（多 `max_result_size_chars` / `dynamic_schema_overrides` / `override` / `scope`） | ★ `dynamic_schema_overrides` 是"schema 的一部分随运行时变"的机制，有教学价值 |
| ⑦ | `discover_builtin_tools` 的简化实现 | 真实还多：**包入口 `tools/<pkg>/tool.py`**、**缺 `__init__.py` 要跳过+警告**、**磁盘 AST 结论缓存（按 mtime+size）**、**~145ms** 的成本说明、文本预筛、**模块级 `for` 里的注册也算** | ★ 每条都对应一个真实失效模式 |
| ⑧ | §Concurrency 一句话 | 机制在 `agent/tool_batch_planner.py`（第 2 章），**本章不指回去** | ★ 读者会找错地方 |
| ⑨ | MCP 发现写成"核心发现之后的第二批发现"（**像是同一流程的一部分**） | 真实代码里它**被故意从模块级移走**了——理由是阻塞 120s 会冻结网关心跳（#16856）。现在由**每个入口自己**在启动时跑 | ★★ 按文档理解会以为它在 import 时就完成 |

**没有出入的地方**（也值得记，说明文档不是普遍不准）：

- ✅ 七个终端后端 —— 和 `_BUILTIN_BACKENDS` **完全一致**
- ✅ `_AGENT_LOOP_TOOLS` 的**四个成员**（只是名字该写 `todo_list`）
- ✅ agent 级工具的 schema 仍在注册表里、handler 返回 stub 错误 —— 真身即
  `return tool_error(f"{function_name} must be handled by the agent loop")`
- ✅ 错误包装的**两层**（真实有三层，但文档说的两层确实存在）
- ✅ `_run_async` 的三条路径
- ✅ `_LEGACY_TOOLSET_MAP` 的存在与用途

> 📌 **本章不一致的"形状"和第 3 章不同。**
> 第 3 章是**顺序**错（同一份文档内部自相矛盾）；
> 这一章主要是**深度不够 + 重构后路径没跟**（文档停留在简化版，真实实现后来长出了缓存、
> 包支持、预编译、多 profile 分键）。
>
> 一个观察：**"简化版示例"和"真实实现"之间的差距，只要没人去对，就会一直扩大。**
> 而它扩大的方式是**静默**的——文档不会报错。

---

## 10. 给 mini 版的改造清单（本章动手做什么）

**这一节是动手前的「计划」，右列是做完之后的实际结果。**
做完之后才知道的三件事（两个真 bug + 一处测试设计错误）在《源码讲解-04-ToolsRuntime.md》**§十**。

按"值不值得改"排：

| # | 改什么 | 为什么值得 | 计划 | 实际 |
|---|---|---|---|---|
| ① | **手写名单 → AST 自动发现** | 本章核心。手写名单的失效模式是**静默少一个工具** | 中 | ✅ 做了 |
| ② | **加 `check_fn` + 发 schema 前过滤** | 让"没配好就不发给模型"这件事在 mini 里也能演示 | 小 | ✅ 做了（含抖动保护） |
| ③ | **加 toolset 解析（含"注册≠暴露"）** | mini 已经有 `toolset` 字段但没人用——把语义补上 | 中 | ✅ 做了 |
| ④ | **错误返回改 JSON 字符串 + 截断** | 契约问题。纯文本错误模型解析不了 | 小 | ✅ 做了（**成功**结果也一起改了） |
| ⑤ | **加 `_AGENT_LOOP_TOOLS` 守卫** | 让"这 4 个不该被独立调用"变成结构保证 | 小 | ✅ 做了（mini 只有 1 个） |
| ⑥ | **加 `_LEGACY_TOOL_ALIASES`** | 演示"对外收窄、对内兼容" | 极小 | ✅ 位置留了，空表 |
| ⑦ | **危险检测挪到一处** | mini 现在是"工具自己带检测"，没有单一事实来源 | 小 | ✅ 做了 |
| ⑧ | 只留 local 后端，但**把后端表的位置留出来** | 见 §7 的"动手" | 小 | ✅ 做了 |
| ⑨ | **（计划外）补上 `search_files`** | 提示词一直在宣传它，但它**不存在**（详见《源码讲解-04》§十） | — | ✅ 做了 |
| ❌ | async 桥接 | mini 没有 async 工具 —— **记录"为什么可以不做"** | — | 不做 |
| ❌ | 107 条危险模式 | 把检测挪到一处就够，**抄数量没有教学价值** | — | 只留 10 条 |
| ❌ | process_registry 那一整套 | 需要"多父 + 持久化 + 重启"的真实场景 | — | 不做 |

> 📌 **计划外发现的两个 bug + 一处测试设计错误，已移到 《源码讲解-04-ToolsRuntime.md》**§十****。
> 那三节是**实现日志**（做完之后才知道的），不属于"翻译"。

## 附录 A · mini 版代码结构对照（本章相关）

**本节记录的是第 4 章**做完之后**的状态。**

| mini 版文件 | 状态 | 对应 Hermes |
|---|---|---|
| `tools/registry.py` | ✅ 362 行（加了 `check_fn` + TTL 缓存 + JSON 契约） | `tools/registry.py`（977 行） |
| `model_tools.py` | ✅ 293 行（AST 发现 + toolset 解析 + 分发） | `model_tools.py`（987 行） |
| `toolsets.py` | ✅ **新建** 144 行（4 个 toolset） | `toolsets.py`（485 行，60 个 toolset / 59 个核心工具） |
| `tools/approval.py` | ✅ **新建** 84 行（危险检测的唯一事实来源） | `tools/approval.py` + 7 个兄弟 = 8 文件 |
| `tools/terminal.py` | ✅ 115 行（只有 local；后端表的位置留着） | `tools/terminal_tool.py` + `terminal_tool_backends.py` |
| `tools/file_tools.py` | ✅ 147 行（结果改 JSON） | `tools/file_tools*.py` |
| `tools/search_files.py` | ✅ **新建** 130 行（★ 计划外，详见《源码讲解-04》§十） | `search_files`（走 ripgrep） |
| `tools/todo.py` | ✅ 267 行（**未动**，含 `AGENT_LEVEL_TOOLS`） | `tools/todo_tool.py` + `agent/inline_tool_executors.py` |
| `tests/test_tools_runtime.py` | ✅ **新建** 635 行 / 63 条 / 7 组 | — |
| `run_agent.py` 的 `INLINE_TOOL_EXECUTORS` | ✅ 第 2 章就有（1 个 key） | `agent/inline_tool_executors.py`（14 个 key） |

规模对比（**第 3 章末 → 第 4 章末**）：

```text
                          第 3 章末      第 4 章末
tools/registry.py            105    →      362 行
model_tools.py                66    →      293 行
toolsets.py                 （无）   →      144 行
tools/approval.py           （无）   →       84 行
tools/terminal.py             78    →      115 行
tools/file_tools.py           92    →      147 行
tools/search_files.py       （无）   →      130 行
tools/todo.py                267    →      267 行（未动）
测试断言                     221    →      284 条（154 + 54 + 13 + 63）
测试代码行数                1980    →     2771 行
源码（不含测试与 demo）     3668    →     4710 行
含测试与 demo 总行数        5924    →     7636 行
```

**对照 Hermes 的规模**：`tools/` 下 258 个 `.py`，其中 43 个是工具模块。
mini 现在有 **4 个**工具模块（terminal / file_tools / search_files / todo）。
**这个比例（0.1 倍不到）正是"手写名单还能活"的原因**——
而这一章要做的恰是"在还小的时候就把该有的结构立起来"。

## 附录 B · 本章砍掉的东西（与何时补）

| 不做 | 代价 | 何时补 |
|---|---|---|
| 第二个终端后端（docker 等） | 只有本地执行 | **大概永远不需要**（真正的复杂度在多父+持久化，不在后端数量） |
| async 桥接 | async 工具会卡住 loop | 等 mini 出现 async 工具时 |
| `process_registry` 的两半 | 没有后台进程的持久化/接管 | 等有"重启后恢复进程"的需求 |
| Smart 审批（辅助 LLM 自动批准） | 每条命中都要人答一次 | 第 5 章之后（需要 LLM 调用能力） |
| 永久允许清单落盘 | 每次新会话都要重新批准 | 等有 `config.json` 的读写层 |
| MCP / 插件两批发现 | 没有外部工具来源 | 第 5 章（插件）/ 更后面（MCP） |
| 107 条危险模式 | 只拦最常见的几类 | 抄"挪到一处"的结构就够，数量不抄 |

> **核心取舍一句话**：这一章的价值 70% 在**"发现机制 + 注册≠暴露 + 两层 agent 级工具集合"**
> 这三件**结构**上的事，30% 在 `check_fn` / JSON 契约 / 审批分层这些**局部**改进。
> 后端数量、模式条数、async 桥接都是"等有真实需求再补"。

## 附录 C · 实测输出

### C.1 注册表的真实规模（在 Hermes 源码树里跑）

```console
$ ls tools/*.py | wc -l
258
$ grep -l "registry.register(" tools/*.py | wc -l
43
$ # 也就是说：215 个 .py 是库/后端/兄弟文件，不参与工具发现

$ TOOLSETS 的键数
60
$ len(_HERMES_CORE_TOOLS)
59

$ DANGEROUS_PATTERNS 的条数（tools/approval_detection.py:203）
107
```

### C.2 两层 agent 级工具集合（本章最重要的一张表）

```text
_AGENT_LOOP_TOOLS      (_model_tools.py:607)   4 个
  delegate_task, memory, session_search, todo_list

INLINE_TOOL_EXECUTORS  (agent/inline_tool_executors.py)   14 个
  todo_list, message_agent, session_search, memory, clarify,
  read_terminal, desktop_preview, drive_preview, annotate_preview,
  read_window_below, gui_tour, manage_connections, setup_mcp, delegate_task

前者 ⊆ 后者：True
只在 INLINE 里的（10 个）：
  annotate_preview, clarify, desktop_preview, drive_preview, gui_tour,
  manage_connections, message_agent, read_terminal, read_window_below, setup_mcp
```

### C.3 mini 版现状（本章的起点）

```console
$ wc -l tools/*.py model_tools.py
       3 tools/__init__.py
      92 tools/file_tools.py
     105 tools/registry.py
      78 tools/terminal.py
     267 tools/todo.py
      66 model_tools.py
     611 total

$ # mini 的"发现"还是一份手写名单：
$ sed -n '19,22p' model_tools.py
_BUILTIN_MODULES = [
    "tools.terminal",
    "tools.file_tools",
]
```

### C.4 本章涉及的 Hermes 文件规模

```console
$ wc -l tools/registry.py model_tools.py toolsets.py tools/approval.py
  977 tools/registry.py
  987 model_tools.py
  485 toolsets.py
 1336 tools/approval.py
$ ls tools/approval*.py | wc -l
8
$ ls tools/environments/*.py | wc -l
（见 §7② 的文件清单）
```

### C.5 本章核对用到的关键常量

| 常量 | 值 | 位置 |
|---|---|---|
| `_CHECK_FN_TTL_SECONDS` | 30.0 | `tools/registry.py:222` |
| `_CHECK_FN_FAILURE_GRACE_SECONDS` | 60.0 | `:225` |
| `_CHECK_FN_CACHE_MAX` | 512 | `:226` |
| `_MAX_TOOL_ERROR_CHARS` | 2048 | `:25` |
| `_MAX_LOGGED_ERROR_CHARS` | 8192 | `:28` |
| `_TOOL_DEFS_CACHE_MAX` | 8 | `model_tools.py:204` |
| `_BUILTIN_BACKENDS` | 7 个（local/docker/singularity/modal/daytona/vercel_sandbox/ssh） | `tools/terminal_tool_backends.py:54` |
| `_CONTAINER_BACKENDS` | 5 个 | `tools/terminal_tool_config.py:64` |

### C.6 第 4 章末的 mini：实测输出

**① AST 自动发现（`_BUILTIN_MODULES` 手写名单已删）**

```console
$ python3 -c "import model_tools; print(model_tools.discover_builtin_tools())"
AST 发现的模块： ['tools.file_tools', 'tools.search_files', 'tools.terminal']
注册的工具：     ['list_dir', 'read_file', 'search_files', 'terminal', 'write_file']
默认暴露：       ['list_dir','read_file','search_files','terminal','todo_list','write_file']
孤儿工具：       []
```

注意它**自己跳过**了 `registry.py` / `approval.py` / `__init__.py`（基础设施），
也跳过了 `todo.py`（它不用 `register_tool`，走 `AGENT_LEVEL_TOOLS`）——
而上面那张手写名单**永远做不到这件事**，因为它连"什么是工具模块"都不知道。

**② `/tools`（把静默的闸门变成可见的）**

```console
你 › /tools
发给模型的工具（6 个）：
  ✓ list_dir: 列出一个目录下的文件和子目录。
  ✓ read_file: 读一个文本文件。返回 JSON：{path, total_lines, shown, content, ...
  ✓ search_files: 在目录下递归搜索文件**内容**（正则）。返回 JSON：{matches:[...
  ✓ terminal: 在本机 shell 里执行一条命令。返回 JSON：{exit_code, stdout, stderr
  ✓ todo_list: 维护多步任务（3 步以上）的任务清单。…
  ✓ write_file: 把内容完整写入文件（会覆盖已有内容），父目录不存在会自动创建。
toolset：['file', 'search', 'terminal', 'todo']　探测缓存：{'cached': 3, 'last_good': 3, 'no_cache': 0}
```

最后那行 `探测缓存` 是**活的 check_fn 缓存**：3 条 = 三个不同的探测函数
（终端 / 搜索 / 文件工具；文件那三个工具共用同一个 → 被 memo 层去重成 1 次探测）。

**③ 真跑一轮（DeepSeek，抓的是会话记录里每一次真实请求/结果）**

任务一（**空结果**，验"模型不会把失败说成结果"）：

```text
→ search_files({"pattern": "def build_system_prompt", "path": "agent/prompt_builder.py"})
← {"matches": [], "total_shown": 0, "truncated": false}
→ todo_list({"todos": [{"id":"1","content":"验证第4章实现","status":"pending"}]})
← {"todos": [{"id":"1","content":"验证第4章实现","status":"pending"}], "revision": 1}
[agent] 没搜到——agent/prompt_builder.py 里不存在 'def build_system_prompt'（可能文件不存在或函数名不同）
```

`build_system_prompt` 确实在 `agent/system_prompt.py:291` 而不是 `prompt_builder.py`——
**agent 的"没搜到"是诚实的**，它没有把空结果编成内容。

任务二（**命中路径**，三个工具串起来）：

```text
[agent] search_files 在 agent/system_prompt.py 命中两处（237 行 build_system_prompt_parts、
        291 行 build_system_prompt）；read_file 显示 291 行是函数定义，292-297 行是 docstring；
        terminal 显示该文件共 404 行。
```

三个数字（237 / 291 / 404）事后都单独核对过，全对。
这也证明**JSON 外壳没有妨碍模型读内容**——`{"exit_code":0,"stdout":"404 ..."}` 它照样读得懂。

**④ 全量测试（Python 3.13.14 与 3.9.6，两个解释器都过）**

```console
$ /usr/local/bin/python3    # 3.13.14
  test_loop_mechanisms.py    54 通过 / 0 失败
  test_prompt_assembly.py   154 通过 / 0 失败　（13 组）
  test_packaging.py          13 通过 / 0 失败
  test_tools_runtime.py      54 通过 / 0 失败　（6 组）
$ /usr/bin/python3          # 3.9.6 —— 同上，全过
```

**合计 284 条。**
