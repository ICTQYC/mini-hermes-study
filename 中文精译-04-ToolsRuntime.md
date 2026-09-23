# 第 4 章 · Tools Runtime（工具运行时）

> 官方原文：`官方文档-Developer-Guide/tools-runtime.md`（250 行）
> 对照源码：`tools/registry.py`（977 行）、`model_tools.py`（987 行）、`toolsets.py`（485 行）、
> `tools/approval.py`（1336 行）+ 7 个 `approval_*.py` 兄弟、`tools/environments/*`、
> `agent/inline_tool_executors.py`（241 行）
>
> 读法同前两章：**段落式翻译 + `【源码】` 块接真实实现 + `【动手】` 块接 mini 版 +
> 每节末尾「为什么这么设计 / 砍掉会怎样」。**

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

### 【源码】真身

#### 先把整条链走一遍（拿真工具 `read_terminal` 当例子）

细节之前先看全景。这个工具够小（89 行）、够典型，而且它**恰好会撞上本章最重要的那个坑**（注册≠暴露）。

它的全部注册代码就这一句（`tools/read_terminal_tool.py:67`）：

```python
registry.register(
    name="read_terminal",
    toolset="desktop_ui", # 所属工具集
    schema=READ_TERMINAL_SCHEMA,
    handler=lambda args, **kw: read_terminal_tool(
        start_line=args.get("start_line"), count=args.get("count"), callback=kw.get("callback")
    ),
    emoji="🖥️",
)
```

**先记住三件事**，后面每一站都要用到：

1. `handler` 是个 **lambda**，**不是**工具本体——工具本体是上面的 `read_terminal_tool()`
2. **没有 `check_fn`**——它的可用性不靠探测
3. `toolset="desktop_ui"`——**这个字符串决定它能不能被模型看见**

下面从头走到尾，一共**六站**。

---

**第 1 站：文件里有一句 `registry.register(...)`（写在模块体里，不是函数里）**

位置很关键。它必须在**模块级**——因为"发现"是靠 AST 找**模块体**里的这句调用（§2 详述）。
写在函数里的话，AST 扫不到，这个文件就**永远不会被 import**。

---

**第 2 站：AST 扫到它 → import → 注册真的发生**

```
discover_builtin_tools()
   │  遍历 tools/*.py + tools/<pkg>/tool.py
   │  对每个文件问：模块体里有 registry.register(...) 吗？
   ↓ 有
importlib.import_module("tools.read_terminal_tool")
   ↓ import 的副作用
registry.register(...) 真的执行了
   ↓
ToolEntry 落进「全局槽」self._tools["read_terminal"]
```

> 📌 **"发现"和"注册"是两件事，别混。**
> - **发现**只决定"**import 谁**"——它自己不注册任何东西
> - **注册**是模块被 import 时**自己做的**副作用
>
> 所以"自注册"这个说法的准确含义是：
> **模块负责在自己被 import 时把工具登记好；发现机制只负责把这批模块找出来。**

落进哪个槽？这次注册**没传 scope**（它不是插件），所以进**全局槽 `self._tools`**。

---

**第 3 站：⚠️ 注册完了，但模型**还看不到它**——toolset 闸门**

**这一站是原文完全没写的，也是 9 处不一致里最严重的一处。**

```
注册表里有 read_terminal                 ✓
registry.get_entry("read_terminal") 拿得到  ✓
日志里没有任何异常                        ✓
模型能看到它吗？                          ✗ ← 还不一定
```

因为 `get_tool_definitions()` 还要问第二个问题：

> `desktop_ui` 这个 toolset，**在这一轮里被启用了吗？**

而 `desktop_ui` 是**具名 toolset**，只在**桌面来源的会话**里被折进来
（`tui_gateway/server.py::_load_enabled_toolsets`——`toolsets.py:174` 的注释：
*`desktop_ui` is folded in separately by the GUI gateway for desktop sessions.*）。所以：

| 你在哪用它 | `read_terminal` 在 schema 里吗 |
|---|---|
| 终端里 `hermes -q "..."` | **不在** |
| 桌面 app 里聊 | 在 |

> 📌 **"注册"和"暴露"之间隔着一个 toolset。**
> 这就是那条硬规矩的具体样子（`tools/AGENTS.md` 原文）：
> *discovery registers the schema, but a tool is **only exposed if a toolset names it***.
>
> **只写 `tools/your_tool.py` 不改 `toolsets.py` 的后果**：
> 工具存在、无报错、任何 `get_entry()` 都拿得到——**但模型永远不知道它存在**。
> 这是本章第三次"静默失败"，也是最反直觉的一次（你以为写完注册就完了）。

---

**第 4 站：toolset → 名字集合 → `check_fn` 过滤 → 动态改写**

```
enabled / disabled toolsets
   ↓ _select_tool_names() 解析成一组工具名（复合 toolset 在这里展开）
{read_file, write_file, terminal, ..., read_terminal}
   ↓ registry.get_definitions(这组名字)
for name in sorted(tool_names):
    entry = entries_by_name.get(name)
    if not entry: continue                              # 名字没注册过 → 跳过
    if entry.check_fn and not _memo_check(entry.check_fn, memo):
        continue                                        # ★ 可用性闸门
    schema = {**entry.schema, "name": entry.name}
    if entry.dynamic_schema_overrides:
        schema.update(entry.dynamic_schema_overrides())  # 运行时覆盖
    result.append({"type": "function", "function": schema})
   ↓ _apply_dynamic_schemas()   ← 10 个 rewriter，把"运行时才知道的事实"写回 schema
最终发给模型的 tools 字段
```

`read_terminal` **没有 `check_fn`**，所以在第 4 站是"无条件通过"。

**对照一个有 `check_fn` 的**（`tools/feishu_doc_tool.py:69`）：

```python
registry.register(
    name="feishu_doc_read", ...,
    check_fn=_check_feishu, requires_env=[], ...)
```

没配飞书凭据 → `_memo_check` 返回 False → **这一行 `continue`** →
**它的 schema 根本不出现在这次请求里**。模型连"有这么个工具"都不知道。

> 📌 对比一下就清楚了**两道闸门的分工**：
>
> | 闸门 | 在第几站 | 回答的问题 | 典型依据 |
> |---|---|---|---|
> | **toolset** | 第 3 站 | "**这一类能力**在这个界面上存在吗" | 平台 / 界面（桌面、Telegram…） |
> | **`check_fn`** | 第 4 站 | "**这个环境里**它现在能用吗" | 凭据 / 服务 / 二进制 |
>
> 一个是**界面的属性**，一个是**环境的属性**。两个都过，才轮到模型看见。

---

**第 5 站：模型返回 tool_call → 分发**

```
模型: tool_call(name="read_terminal", arguments={"count": 40})
   ↓
handle_function_call("read_terminal", {"count": 40}, ...)
   ├─ 参数归一 coerce_tool_args()
   ├─ 旧工具名映射（read_terminal 不在里面）
   ├─ tool_search 桥？（不是）
   ├─ connectors？（不是）
   ├─ 请求中间件
   ├─ ★ _AGENT_LOOP_TOOLS 守卫？  read_terminal 不在那 4 个里 → 通过
   ├─ pre-dispatch 守卫（审批等）
   └─ _execute_tool() → registry.dispatch("read_terminal", args, callback=...)
```

`registry.dispatch()` 干的事很小：

```python
entry = self.get_entry(name, scope=scope)
if not entry:
    return tool_error(f"Unknown tool: {name}")            # ← 名字不存在
try:
    if entry.is_async:
        result = _run_async(entry.handler(args, **kwargs))  # ← async 桥接
    else:
        result = entry.handler(args, **kwargs)              # ← 就是那句 lambda
    return self._normalize_handler_result(name, result)
except Exception as e:
    ...  # 包成 JSON 错误（§5 详述）
```

**注意那个 `**kwargs`**：`callback=...` 是从调用方**一路传下来的**，
而它来自 `agent`（拦截点在 `agent/tool_executor.py:1550`）。

> 📌 这就是为什么 `read_terminal` 会出现在那 **14 个 inline 工具**里（§9 ①）：
> 它的执行**需要 agent 提供的 callback**，而 callback 不在注册表里、也不在参数里。
> **所以"注册表 + handler(args)"这条路走不通它**——必须由 agent loop 先把 callback 准备好再调。

---

**第 6 站：结果变成 JSON 字符串 → 进历史**

工具本体（`tools/read_terminal_tool.py:36`）：

```python
def read_terminal_tool(start_line=None, count=None, callback=None) -> str:
    """Return the in-app terminal's contents (+ line metadata) as a JSON string."""
    return read_pane(callback, (("start", start_line, 0), ("count", count, 1)), (
        "read_terminal is only available in the Hermes desktop app.",   # ← ① callback 是 None
        "start_line and count must be integers.",                       # ← ② int(val) 抛异常了
        "Failed to read terminal: ",                                    # ← ③ 回调炸了
        "No in-app terminal is open, or the read timed out.",           # ← ④ 读到空
    ))
```

> ⚠️ **更正记录（措辞）**：这条错误文案（②）说的是"**must be integers**"，
> 但触发它的条件其实是 **`int(val)` 抛异常**——而 `int()` 接受的东西**比整数多**。
> 实测（`read_pane(cb, (("start", val, 0), ("count", val, 1)), ERRORS)`）：
>
> | 输入 | 结果 |
> |---|---|
> | `40` | ✓ 接受 → `40` |
> | **`3.7`** | **✓ 接受 → `3`（静默截断！）** |
> | **`"40"`** | **✓ 接受 → `40`** |
> | `" 40 "`（带空格） | ✓ 接受 → `40` |
> | `True` | ✓ 接受 → `1`（bool 是 int 的子类） |
> | `None`（不传） | ✓ 接受（该参数被整条丢掉） |
> | `"40.5"` | ✗ 拒绝 |
> | `"abc"` / `[1,2]` | ✗ 拒绝 |
>
> 准确的说法是 **"参数无法被 `int()` 转换"**，而不是"不是整数"。
> 我在 §1 的例子里一开始照抄了这句文案（写成"参数不是整数"），已改。
>
> 📌 顺带一个观察：`3.7` 会被**静默截断成 3**。
> 对一个"行号"参数来说，这个宽松度是否合意取决于取舍——
> schema 里写的是 `"type": "integer"`（模型**应该**送整数），
> 而这里的宽松是给"模型不听话时"的兜底：**能救就救，救不了才报错。**
> 这句文案的不精确属于**提示措辞**层面的小瑕疵，不影响行为。

**四种错误文案在调用时就准备好、按位置传进去**，`read_pane` 按情况挑一个：

```python
def read_pane(callback, window, errors) -> str:
    if callback is None:
        return tool_error(errors[0])                        # ← 不在桌面环境
    try:
        kwargs = {key: max(floor, int(val)) for key, val, floor in window if val is not None}
    except (TypeError, ValueError):
        return tool_error(errors[1])                        # ← 参数类型不对
    try:
        raw = callback(**kwargs)
    except Exception as exc:
        return tool_error(f"{errors[2]}{exc}")              # ← 回调炸了
    if not raw:
        return tool_error(errors[3])                        # ← 读到空
    return passthrough_json(raw)
```

`tool_error(...)` 返回的是 **JSON 字符串** `'{"error": "..."}'`——这就是本章 §1 那条硬规矩的具体样子。

而成功路径最后那句 `passthrough_json(raw)`（`tools/desktop_ui.py:65`）也值得看一眼：

```python
def passthrough_json(raw) -> str:
    """Desktop answers with a JSON object; pass it through, else wrap the raw text."""
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False)   # 已经是 JSON → 原样透传
    except (TypeError, ValueError):
        return json.dumps({"text": str(raw)}, ensure_ascii=False)  # 不是 → 包一层
```

> 📌 **两条路都返回 JSON**，而且第二条是"**包一层**"而不是"报错"。
> 这就是"契约"的完整含义：**不是"正常情况下返回 JSON"，
> 而是"无论拿到什么，出口都是 JSON"**。
> 上游（桌面渲染进程）回什么不受这边控制，所以这里必须兜住。

> 📌 那个"错误文案表"值得多看一眼：**它把"可能怎么失败"做成了参数。**
>
> 为什么？因为 `read_pane` 这个函数体被**三个工具共用**
> （`read_terminal` / `read_preview` / `read_window`——docstring 里点明了），
> 而每个工具的错误文案不一样。
>
> **共用逻辑 + 各自文案 = 把文案做成参数。**
> 这比"在共用函数里判断是谁在调我"好得多——后者正是漂移的来源
> （`agent/inline_tool_executors.py` 的 docstring 说过它取代了
> 两条**已经漂移分家**的 if/elif 链）。
>
> **判据：共用的部分做成函数，不同的部分做成参数——不要做成 if。**

---

**整条链一张图**

```
┌─ 进程启动 ────────────────────────────────────────────────┐
│ tools/read_terminal_tool.py                                │
│   模块体里：registry.register(name="read_terminal", ...)    │
│        │                                                   │
│        │ ①AST 扫到「模块体里有这句调用」                     │
│        ↓                                                   │
│   discover_builtin_tools()  ──②import──→  ③注册动作执行     │
│        │                                        │          │
│        │                                        ↓          │
│        │                             ToolEntry 落进全局槽    │
└────────┼───────────────────────────────────────────────────┘
         │
┌─ 每一轮请求 ──────────────────────────────────────────────┐
│ ④ toolset 解析：desktop_ui 在这轮启用了吗？                │
│     没启用 ──→ 到此为止（模型看不到它）                     │
│     启用了 ↓                                               │
│ ⑤ 名字集合 → registry.get_definitions()                   │
│     check_fn 过滤（它没有 check_fn → 无条件过）             │
│ ⑥ 10 个动态 rewriter 改写 schema                           │
│ ⑦ → 写进 API 请求的 tools 字段                             │
└────────┬──────────────────────────────────────────────────┘
         │ 模型返回 tool_call(name="read_terminal", ...)
┌────────┴──────────────────────────────────────────────────┐
│ ⑧ handle_function_call()：参数归一 → 各种守卫 → _execute_tool│
│ ⑨ registry.dispatch()：查 ToolEntry → 调 handler(args,      │
│    callback=...)  ← 那个 lambda 转发到工具本体               │
│ ⑩ read_terminal_tool() → read_pane() → callback() → JSON    │
│ ⑪ '{"total_lines": 200, ...}' 变成一条 tool 消息，进历史     │
└───────────────────────────────────────────────────────────┘
```

**每一步谁负责 / 漏了会怎样：**

| 步 | 谁负责 | 漏掉的后果 |
|---|---|---|
| ① AST 发现 | `tools/registry.py` | 文件不被 import → 工具**不存在**（无报错） |
| ② import | `model_tools.py`（import 时触发） | 同上 |
| ③ 注册进槽 | 工具文件自己（模块级调用） | 同上 |
| ④ **toolset 闸门** | `toolsets.py` + 平台解析 | 工具存在但**模型看不见**（无报错）★ |
| ⑤ `check_fn` 过滤 | `tools/registry.py` | 没配好的工具也发出去 → 模型**浪费预算试错** |
| ⑥ 动态改写 | `model_tools.py` 的 10 个 rewriter | schema 里点名了不可用的工具 → 模型**幻觉调用** |
| ⑦ 进请求 | provider 层 | —— |
| ⑧ 守卫 | `model_tools.py` | agent 级工具被独立调用 → 状态错乱 |
| ⑨ 分发 | `tools/registry.py` | 名字不存在 → `Unknown tool` |
| ⑩ 执行 | **工具自己的 handler** | —— |
| ⑪ JSON 字符串 | 工具自己的 handler | 返回非 JSON → **别处解析不了**（如 todo 状态重建） |

> 📌 看这张表最右边一列：**11 步里有 5 步漏掉之后是"静默"的**
> （① ② ③ 是同一个后果，④ 单独一个，⑥ 是幻觉）。
>
> **这就是这一章真正想让你记住的东西**：
> 工具注册这条链**每一站都在新加一道"能不能看见"的闸门**，
> 而每一道闸门拦住东西的时候**都不报错**。
>
> 所以调试"为什么模型不用我那个工具"时，要**按站排查**，不要从代码里找 bug——
> 代码没 bug，是某一道闸门关着。

#### 真实签名（比原文多 4 个参数）

```python
# tools/registry.py:619
def register(
    self, name: str, toolset: str, schema: dict, handler: Callable,
    check_fn: Callable = None, requires_env: list = None, is_async: bool = False,
    description: str = "", emoji: str = "", max_result_size_chars: int | float | None = None,
    dynamic_schema_overrides: Callable = None, override: bool = False,
    scope: Optional[str] = None):
```

原文没写的四个：

| 参数 | 干什么 |
|---|---|
| `max_result_size_chars` | 单个工具结果的字符上限（各工具可以不同） |
| `dynamic_schema_overrides` | **一个零参可调用**，返回的 dict 会在**每次 `get_definitions()` 时浅合并进 schema**——给"随运行时配置变化"的字段用（`delegate_task` 的描述要反映当前的并发上限） |
| `override` | 显式允许覆盖 |
| `scope` | 插件作用域（一个进程服务多个 profile/插件世代） |

`dynamic_schema_overrides` 是最有意思的一个：**它让 schema 的一部分变成"每次取时现算"**。
这跟第 3 章的三层装配是同一个思路——**有一部分内容天然会变，就把它单独放，别拖累其他部分**。

#### `ToolEntry` 的真实字段（11 个）

```python
# tools/registry.py:181
class ToolEntry:
    """Metadata for one registered tool (identity semantics: restore/CAS paths compare ``is``)."""
    name: str
    toolset: str
    schema: dict
    handler: Callable
    check_fn: Optional[Callable]
    requires_env: list
    is_async: bool
    description: str
    emoji: str
    max_result_size_chars: int | float | None = None
    dynamic_schema_overrides: Optional[Callable] = None
```

> 📌 `description` 和 `schema` **两个字段并存**这件事本身就是原文那段话的来源。
> 它们在真实代码里是两个独立的值，**不会自动同步**。

#### 注册时会做 schema 校验（原文没提）

```python
# tools/registry.py:628
# Reject malformed schemas at registration, not at request time: a non-dict
# ``parameters`` (e.g. a list) serializes into every provider request and 400s the
# whole turn far from the offending plugin. Failing here names the culprit instead.
if not isinstance(schema, dict):
    raise ValueError(f"Tool {name!r}: schema must be a dict, got {type(schema).__name__}")
params = schema.get("parameters")
if params is not None and not isinstance(params, dict):
    raise ValueError(...)
self_name = f"Tool {name!r}: schema['parameters'] must be an object (JSON Schema dict), ...")
```

**这段注释值得单独读**——它讲的正是"为什么要在最早的地方失败"：

> 畸形 schema 在**注册时**就拒绝，而不是等到请求时：一个非 dict 的 `parameters`
> （比如列表）会被序列化进**每一次** provider 请求，让**整个 turn** 报 400 错，
> 而报错位置离出问题的插件很远。在这里失败就能点出是谁干的。

对照一下这个仓库里反复出现的模式：**"检查要对着能指出问题的那个位置"**。
第 3 章那次是校验跑错时机（注入前 vs 注入后），这次是校验跑错位置（请求时 vs 注册时）。

#### 先说清两个词：**"全局槽"和"影子"**

这两个词前面用到了但没定义。它们是理解覆盖语义的前提。

**注册不是往一个 dict 里放东西，是往两个"槽"之一放：**

```python
# tools/registry.py
def _slot(self, scope: Optional[str], *, create: bool = False) -> Dict[str, ToolEntry]:
    """The registration map for *scope*: global when None, else that profile's overlay."""
    if scope is None:
        return self._tools                                  # ← 全局槽：内置工具
    if create:
        return self._scoped_tools.setdefault(scope, {})     # ← 覆盖槽：每个插件一份
```

| 词 | 准确含义 |
|---|---|
| **全局槽**（`self._tools`） | `scope=None` 时注册进去的东西——**所有内置工具**（`tools/*.py` 里调 `register()`） |
| **覆盖槽**（`self._scoped_tools[scope]`） | 插件注册时**带 scope** 落进去的地方，**每个插件（每个 profile）一份** |
| **影子（shadow）** | 一个插件工具的名字**和全局槽里已有的工具重名**——于是它在取值时会**盖住那一个** |

**"盖住"是字面意义上的覆盖**，看取值逻辑：

```python
def _merged_tools(self, scope: Optional[str] = None) -> Dict[str, ToolEntry]:
    """Return global tools overlaid with one profile's plugin tools."""
    return {**self._tools, **self._scoped_tools.get(scope or self.current_scope_key(), {})}
```

`{**全局, **某插件的}` —— **后者盖前者**。
所以一个插件工具叫 `terminal`，就会在**它自己所在的 scope 里**把内置的 `terminal` 顶掉。
它的意思是：**这个注册如果放行，取值时它会盖住一个已经存在的东西。**

#### 覆盖语义其实是四条路径（原文压成了一句话）

```python
# tools/registry.py:647-682（化简）
plugin_override_denied = (owner is not None and not self._plugin_override_allowed(scope, owner))
# 「它想盖掉的是一个全局工具」的四条判据，缺一不可：
shadows_global = (
    owner is not None and        # ① 注册方是插件（不是内置代码）
    scope is not None and        # ② 它注册进了自己的覆盖槽
    name not in target and       # ③ 它自己槽里还没有这个名（排除重连/刷新）
    name in self._tools)         # ④ 但全局槽里有这个名字
if shadows_global:
    if not override:
        logger.error("Tool registration REJECTED: plugin %r attempted to shadow global tool %r "
                     "without override=True", owner, name)
        return                                    # ← ① 想盖全局工具、但没声明 override：拒绝 + 日志
    if plugin_override_denied:
        raise PermissionError(...)                # ← ② 声明了 override、但使用者没授权：抛异常

if existing and existing.toolset != toolset:
    if override:
        if plugin_override_denied:
            logger.error(...); raise PermissionError(...)   # ← ③ 同上，跨 toolset 版
        logger.info("Tool '%s': toolset '%s' overriding existing toolset '%s' (override=True opt-in)", ...)
    else:
        logger.error("Tool registration REJECTED: '%s' (toolset '%s') would shadow existing "
                     "tool from toolset '%s'. Pass override=True ...")
        return                                    # ← ④ 跨 toolset 影子没 override：拒绝 + 日志
```

#### 关键：替换内置工具要**两个人分别点头**

这是这段代码最重要的设计，说的是**两个不同的人**：

| 闸门 | 谁决定 | 怎么表态 |
|---|---|---|
| `override=True` | **插件作者** | 在 `register()` 调用里写（声明"我是**故意**替换它的"） |
| `allow_tool_override: true` | **使用者 / 运维** | 在 `config.yaml` 里写 `plugins.entries.<plugin_id>.allow_tool_override: true`（授权"我允许**这个**插件替换内置工具"） |

拒绝时的报错原文（`_OVERRIDE_DENIED_MSG`）：

> Plugin module {owner!r} **cannot override built-in tool** {name!r}
> **without operator opt-in** (allow_tool_override).
>
> （插件模块 {owner!r} 在**没有使用者显式同意**的情况下，**不能覆盖内置工具** {name!r}。）

> 📌 **为什么必须是两个人？** 因为这是两个不同的问题：
>
> - **作者**知道的是"**我的意图**"——比如"我这个 headed-Chrome 后端就是要替换默认的
>   `browser_navigate`"，那是合情合理的替换
> - **使用者**决定的是"**我信不信任它**"
>
> **只有第一道闸门**：任何粗心或恶意的插件都能悄悄把 `terminal` 换掉，
> 用户完全看不出来（工具名没变、schema 可能也没变）。
> **只有第二道**：那是一个全局开关，等于"以后谁都能替换我的核心工具"。
> 两道都要 = **声明意图 + 授予权限**，而且**都是逐个插件**的。

#### 两个失败形态为什么一个记日志、一个抛异常

| 情况 | 形态 | 怎么理解 |
|---|---|---|
| 想盖全局工具、**没声明 `override`** | `logger.error` + **不注册**，进程继续 | **疏忽**：没说要覆盖。善意假设 → 当没发生过 |
| 声明了 `override`、**但使用者没授权** | **`raise PermissionError`** | **越权尝试**：它**确实想**换掉你的核心工具，只是没被允许 |

> 📌 判据不是"后果严不严重"，而是**"这是疏忽还是越权"**：
> - **没声明意图** → 按疏忽处理（日志里留下线索，但不中断）→ 插件那个工具不生效，**内置的照常**
> - **声明了但没被授权** → 按越权处理（抛出）→ 因为**它本会真的改掉一个核心工具的行为**
>
> 两次的差别还有一层：**①的结果是"少了个插件功能"，用户是"少了东西"；
> ②的结果是"核心工具被静默换掉"，用户是"用着以为对的东西"**——后者才是信任边界。

> **同 toolset 重新注册是允许的**——注释专门写了理由：
> *same-toolset re-registration (MCP reconnect/refresh) stays allowed.*
> （MCP 服务器重连/刷新时会重新注册同名工具，那是正常行为。）
> 这也解释了判据 ③ `name not in target` 为什么必要：**重连时"我自己槽里已经有这个名了"，
> 那不是影子，那是刷新。**

#### 附带一个防"洗白"的设计

"这是谁注册的"（`owner`）不是看**谁在调用**，而是看**函数定义在哪个模块**：

```python
def _plugin_owner_of(self, handler: Callable) -> Optional[str]:
    """Plugin namespace that DEFINED *handler* (None for built-in/MCP handlers). Bound to
    ``handler.__globals__["__name__"]``, fixed at definition time so it cannot drift with
    call site/thread/timing; lambdas and nested functions inherit it, so a plugin cannot
    launder an override via a callback."""
```

> 📌 注释最后一句是重点：*so a plugin **cannot launder an override via a
> callback***（**插件不能通过回调把一次覆盖"洗白"**）。
>
> 也就是说：插件**不能**把内置工具的函数包一层 lambda 就冒充成自己的实现——
> 因为 lambda 继承的是**定义处**的模块名（`__globals__["__name__"]`），不是调用处的。
>
> 规律：**判定身份要选"不会被伪装/漂移的信号"。**
> "调用位置"是可以伪装的，"定义位置"是固定的。
> 这和第 3 章"schema 在导入时构建、所以不能写运行时事实"同源：
> **先想清楚哪个信号是可信的，再拿它做判据。**

#### 结果必须是 JSON 字符串

```python
# tools/registry.py:965-977
# Tool handlers must return JSON strings; these replace the ubiquitous
# ``json.dumps({"error": msg}, ensure_ascii=False)`` boilerplate.

def tool_error(message, **extra) -> str:
    """``'{"error": "<message>", **extra}'`` — the error body is bounded so a raw
    exception can't bloat history across retries."""
    return json.dumps({"error": _bound_error_text(str(message)), **extra}, ensure_ascii=False)

def tool_result(data=None, **kwargs) -> str:
    """JSON-encode a dict positional arg *or* keyword arguments (not both)."""
    return json.dumps(data if data is not None else kwargs, ensure_ascii=False)
```

这条规矩来自 `tools/AGENTS.md`：

> **All handlers return a JSON string.**

**为什么必须是 JSON 而不是人看的文本？** 因为工具结果要**从历史里被重新解析**——
`todo` 的状态重建就靠这个（第 2 章那条：网关每条消息新建一个 AIAgent，
所以状态必须能从历史里读回来）。给人看的文本解析不了。

### 【动手】mini 版现状

mini 版这边是两个不同的选择，**都是有意的**：

```python
# mini-hermes/tools/registry.py:21
def register_tool(
    name: str, *,
    description: str, parameters: Dict[str, Any],
    toolset: str = "core",
    interactive: bool = False,              # ← min 特有：强制整批串行
    requires_approval: bool = False,        # ← min 特有：审批
    danger_check: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Callable:
    """把函数注册成一个模型可调用的工具。"""
    def decorator(fn):
        with _LOCK:
            _REGISTRY[name] = {"name": name, "fn": fn, "toolset": toolset,
                               "interactive": interactive, ...,
                               "schema": {"type": "function", "function": {...}}}
        return fn
    return decorator
```

两个差异：

| | mini | Hermes |
|---|---|---|
| 调用形式 | **装饰器** `@register_tool(...)` | **普通函数调用** `registry.register(...)` |
| "执行什么" | 直接抓到 `fn` | 传一个 `handler` lambda（**转发放进去**） |
| 参数个数 | 7 | 13 |
| 结果契约 | 字符串（错误时 `[error] ...` 纯文本） | **JSON 字符串**（`{"error": ...}`） |

> ⚠️ **结果契约这条 mini 是不对的。** mini 的 `handle_function_call` 出错时返回
> `"[error] 未知工具：xxx"`——**人看的文本**。看起来更好读，但代价是
> **模型和程序都没法可靠地解析它**。
>
> 这一章要改成 JSON 字符串（并且加一句"为什么"，见 §5）。

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
`tools/registry.py` 是个 **零依赖叶子**（`tools/AGENTS.md`：*has no deps and is imported by
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

### 【源码】真身

真实的 `discover_builtin_tools()` 比原文的简化版多**五件事**。

#### ① AST 检查：其实是四条规则

```python
# tools/registry.py:58
def _is_registry_register_call(node: ast.AST) -> bool:
    """True when *node* is a ``registry.register(...)`` call expression."""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    return (isinstance(func, ast.Attribute) and func.attr == "register"
            and isinstance(func.value, ast.Name) and func.value.id == "registry")
```

它认的是**语法形状**，不是文本。四个条件缺一不可：

```
ast.Expr                    ← 语句，不是赋值
└─ ast.Call
   └─ func = ast.Attribute，attr == "register"
      └─ value = ast.Name，id == "registry"
```

也就是说 `registry.register(...)` 算，而 `r.register(...)`、`self.registry.register(...)`、
`registry["x"] = ...` 都不算。**这是"按语法查"和"按文本查"的区别**——
文本查会误伤注释和字符串，`_module_registers_tools` 的 docstring 专门交代了这一点：

```python
# tools/registry.py:68
def _module_registers_tools(module_path: Path) -> bool:
    """True when the module body (or a module-level ``for``) calls ``registry.register(...)``.
    Only module-body statements count, so helpers registering inside a function are skipped;
    a text prefilter avoids ``ast.parse`` for files lacking both words."""
    try:
        source = module_path.read_text(encoding="utf-8")
        if "registry" not in source or "register" not in source:
            return False                    # ← 文本预筛，省掉 ast.parse 的开销
        tree = ast.parse(source, filename=str(module_path))
    except (OSError, SyntaxError):
        return False
    # Table-driven modules register several tools from one loop, still at import time.
    return any(
        _is_registry_register_call(stmt)
        or (isinstance(stmt, ast.For) and any(_is_registry_register_call(s) for s in stmt.body))
        for stmt in tree.body)
```

第 4 条规则：**模块级的 `for` 循环里注册也算**。注释：*Table-driven modules register several
tools from one loop, still at import time.*（表驱动的模块用一个循环注册好几个工具，
同样发生在 import 时。）

> 📌 这是"**规则要跟着真实用法走**"的一个例子：一开始可能只有直接调用，
> 后来有人写了表驱动的模块，AST 检查就得跟着放宽——**否则那些工具会静默消失**。

#### ② 候选文件：不只 `tools/*.py`，还有包入口

```python
# tools/registry.py:86
def _tool_module_candidates(tools_path: Path) -> List[Path]:
    """Flat ``tools/*.py`` modules plus the package entry point ``tools/<pkg>/tool.py``, in one
    sorted list. Only ``tool.py`` is scanned in a package, so every other file in it is a library
    by construction. Sorted after merging: ``register()`` lets a same-name, same-toolset duplicate
    overwrite silently, so the import order must not depend on file depth."""
    candidates = list(tools_path.glob("*.py")) + list(tools_path.glob("*/tool.py"))
    return sorted(candidates)
```

三个设计点：

| 点 | 理由（注释原话） |
|---|---|
| 包只扫 `tool.py` | *every other file in it is a library by construction*——包里其他文件**按定义就是库**，不用扫 |
| 合并后**统一排序** | `register()` 允许同名同 toolset 静默覆盖，**所以 import 顺序不能依赖文件深度** |
| 为什么怕顺序 | 顺序不定 → 覆盖谁不定 → **行为不定** |

#### ③ 包里缺 `__init__.py` 会被跳过并警告

```python
# tools/registry.py:109
if len(rel_parts) > 1 and not (path.parent / "__init__.py").exists():
    # setuptools' package finder drops a directory without __init__.py, so this tool would
    # register from a checkout and vanish from an installed wheel.
    logger.warning("Skipping %s: package %s has no __init__.py", path, path.parent.name)
    continue
```

> **"在源码树里能跑，装成 wheel 就消失"** —— 这是最难查的一类 bug：
> 开发时一切正常，发布后功能静默没了。
> 所以这里**宁可跳过 + 警告，也不让一个注定在发布版里消失的工具偷偷加载**。

#### ④ 磁盘记忆化缓存（原文完全没提）

```python
# tools/registry.py:95
def discover_builtin_tools(tools_dir: Optional[Path] = None) -> List[str]:
    """Import built-in self-registering tool modules and return their module names. The
    per-file AST scan costs ~145 ms over ~100 files, so verdicts are memoized on disk keyed
    by ``(mtime_ns, size)``; a mismatch or corrupt cache re-scans that file. The write is
    best-effort and atomic, so concurrent processes race harmlessly."""
```

- 每文件 AST 扫描在 ~100 个文件上要 **~145ms**
- 结论按 `(mtime_ns, size)` 记在磁盘上
- 不匹配或缓存坏了 → **重扫那一个文件**
- 写入是 **best-effort + 原子**的 → 并发进程竞争无害

缓存路径：

```python
# tools/registry.py:143
from hermes_constants import get_hermes_home()
return Path(get_hermes_home()) / "cache" / "tool_discovery_cache.json"
```

> 📌 这里出现了本章第二次"**启动路径上的性能优化**"。
> 判据和别处一样：**这段代码在每个进程启动时都跑**，所以它的常数要抠。
> 而缓存键用 `(mtime_ns, size)` 而不是内容 hash——**改了文件就失效，没改就复用**。

#### ⑤ 数字：258 个文件里只有 43 个是工具

这是实测的、原文完全没提的规模感：

```console
$ ls tools/*.py | wc -l
258
$ grep -l "^registry.register(" tools/*.py | wc -l
43
```

**258 个 `.py` 文件里，只有 43 个含顶层 `register(`。** 其余 215 个是：
辅助模块、后端实现（`tools/environments/*`）、按主题拆的兄弟文件（`mcp_tool_*.py`、
`browser_tool_*.py`）、注册表自己……

**这就是为什么必须用 AST 检查而不是"扫目录全 import"**：

- 全 import → 215 个纯库模块会被当成工具模块加载，**副作用不可控**
- 按名字前缀筛（比如"不以下划线开头的都算"）→ 会漏掉合法的库文件和误伤工具文件
- **按"模块体里有没有那句调用"筛** → 精确对应"这个文件是不是声明了工具"

#### 关于 MCP / 插件两批发现

原文那两条是对的，位置也对：

- MCP：`tools/mcp_tool_discovery.py` 的 `discover_mcp_tools()`（由 `tools/mcp_tool.py`
  这个门面再导出）
- 插件：`hermes_cli/plugins.py` 的 `discover_plugins()`

**但原文没说一件重要的事：MCP 发现曾经就在这个文件的模块级跑，后来被移走了。**
源码里留着两段说明（`model_tools.py` 底部）：

```python
# --- Tool discovery (importing each tools/*.py triggers registry.register) ---
discover_builtin_tools()

# MCP discovery is deliberately NOT run here: it blocks up to 120 s and the
# gateway lazy-imports this module inside its event loop; each entry point
# (gateway/run.py, cli.py, tui_gateway, acp_adapter) runs it at startup.
try:  # plugin tool discovery (user/project/pip plugins)
    # MCP tool discovery (external MCP servers from config) used to run here as a module-level side effect.
    # It was removed because discover_mcp_tools() internally uses a blocking future.result(timeout=120)
    # wait, and the gateway lazy-imports this module from inside the asyncio event loop on the first user
    # message — freezing Discord/Telegram heartbeats for up to 120s whenever any configured MCP server was
    # slow or unreachable (#16856). - gateway/run.py            -> start_gateway() uses run_in_executor -
    # acp_adapter/server.py     -> asyncio.to_thread on session init
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
except Exception as e:
    logger.debug("Plugin discovery failed: %s", e)
```

**这段注释是一个完整的故障报告**，值得拆开读：

| 环节 | 内容 |
|---|---|
| **原来在哪** | 模块级副作用（import `model_tools` 时就跑） |
| **它做什么** | `discover_mcp_tools()` 内部是**阻塞**的 `future.result(timeout=120)` |
| **谁来 import 它** | **网关**在收到第一条用户消息时**懒加载**这个模块——也就是**在 asyncio 事件循环里** |
| **后果** | 任何配置过的 MCP 服务器变慢/连不上 → **冻结 Discord/Telegram 心跳最多 120 秒** |
| **issue** | #16856 |
| **修法** | 从模块级移走；由每个入口**自己**在启动时跑（网关走 `run_in_executor`、ACP 走 `asyncio.to_thread`） |

> 📌 **这个案例把几件事串起来了，而且全在"看起来无害"的层面上：**
>
> ① **"阻塞 120 秒"本身不算罪** —— 在一个普通启动路径上，慢一点只是慢。
> 罪在于**它在事件循环里**：事件循环被占住 → **心跳发不出去** →
> 用户在 Telegram 那边看到的是"bot 死了"。**同一个阻塞在两个上下文里的严重程度差一个数量级。**
>
> ② **"模块级副作用"是危险位置。** 你在模块里写一句 `discover_mcp_tools()`，
> 看起来只是在"初始化"，但你**同时决定了它会在谁的调用栈上执行**——
> 而 import 这个模块的人可能是网关、可能是 CLI、可能是 TUI、可能是 ACP。
> **一个副作用，四种上下文。**
>
> ③ 所以修法是**"把时机还给调用方"**：模块只提供函数，**什么时候调由入口决定**。
> 这和第 3 章"别重探 git"是同一条：
> **不要在你不控制时机的地方做有代价的事。**
>
> ④ 而 `_run_async`（本章 §5④）**正是为了解决"我必须在事件循环里调阻塞东西"这一类问题**
> 才存在的——它起一个一次性线程去跑。**同一个仓库里，同一个问题有两种应对**：
> 移走（MCP 发现）或桥接（async 工具）。判据是**"这个东西是不是每次都需要"**：
> 每次都要 → 桥接；只在启动时要 → 移走。

补充一条原文没说的硬规矩（`tools/AGENTS.md`）：

> **MCP trust is a per-profile record:** `mcp_tool_registration.py::_record_scope_trust` keys
> trust on the home; a secondary never adopts the launch profile's trust for a same-named
> server.

（**MCP 的"信任"是按 profile 记的**：一个次要 profile 绝不会因为主 profile 信任了某个同名
服务器就跟着信任它。）—— 这属"一个进程服务多个 profile"那条大原则，见 §3。

### 【动手】mini 版现状 ← **这一章要改的就是这里**

```python
# mini-hermes/model_tools.py:19
# 要自动发现的工具模块（对应 Hermes 的 tools/ 目录扫描）
_BUILTIN_MODULES = [
    "tools.terminal",
    "tools.file_tools",
]

_discovered = False

def discover_builtin_tools() -> List[str]:
    """import 每个工具模块，让它自己注册。只跑一次。"""
    global _discovered
    if not _discovered:
        for mod in _BUILTIN_MODULES:
            importlib.import_module(mod)
        _discovered = True
    return registry.tool_names()
```

> ⚠️ **这是 mini 版里一处"手写名单"，第 2 章写的时候就标记了它。**
>
> 当时的原话是：
> > 手写名单在只有 2 个模块时更好读，但**忘加一行就会静默少一个工具**——
> > 这是我们要在别的章节改进的地方。
>
> **第 4 章就是"别的章节"。** 手写名单的失效模式正是它最危险的地方：
>
> | | 手写名单 | AST 自动发现 |
> |---|---|---|
> | 加新工具文件 | 要记得改名单 | 放进目录就行 |
> | 忘了改的效果 | **那个工具在模型眼里不存在**（不报错） | —— |
> | 名字写错 | `ModuleNotFoundError`（还好，会炸） | —— |
> | 模块被删但名单没删 | `ModuleNotFoundError`（还好） | 自动消失 |
>
> **最坏的是"漏加"**：不是报错，是模型**不知道自己有这个工具**，
> 于是永远不去用它。而这**没有任何症状**——agent 只是显得"变笨了"。
>
> 这和第 3 章那条教训是同一个形状：
> **静默出错的地方，基本都是架构。** 忘了改名单不会让程序崩，只会让能力静默消失。

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
| `_CHECK_FN_FAILURE_GRACE_SECONDS` | **60 秒** | **抖动保护窗口**（见下，**不是**"失败也缓存 60 秒"） |
| `_CHECK_FN_CACHE_MAX` | **512** | 缓存条目上限 |
| `CHECK_FN_CACHE_BYPASS` | `""` | 一个哨兵值，用它可以**绕过缓存** |

#### ⭐ 抖动保护：失败分两种，处理方式相反

这是我想错了一次的地方，值得完整贴出来（`tools/registry.py:331`）：

```python
    exc_info = None
    try:
        value, outcome = bool(fn()), "returned False"
    except Exception as exc:
        # Keep the exception for the verdict log below (emitted outside this block, where
        # ``exc_info=True`` would resolve to nothing): a check_fn that raises is a bug in the probe
        # or its resolver, and a bare "raised" verdict reads as "nothing configured" (#87950).
        value, outcome, exc_info = False, "raised", exc
    with _check_fn_cache_lock:
        _prune_check_fn_caches(now)
        if value:
            _check_fn_last_good[cache_key] = now        # ← 记住"最后一次成功是什么时候"
            _check_fn_cache[cache_key] = (now, True)
            return True
        last_good = _check_fn_last_good.get(cache_key)
        if last_good is not None and now - last_good < _CHECK_FN_FAILURE_GRACE_SECONDS:
            # Recent success → flake: serve last-good True, do NOT cache (next call re-probes).
            logger.warning(
                "check_fn %s failed (%s) within %.0fs of last success; "
                "treating as transient and keeping tool(s) available",
                _fn_label(fn), outcome, _CHECK_FN_FAILURE_GRACE_SECONDS)
            return True                                 # ← ★ 刚成功过又失败 → 当作抖动，说"可用"

        # No recent success (or grace expired) — honor the failure. A False verdict is the
        # expected state for optional, unconfigured toolsets; only a raised probe is actionable.
        log = logger.warning if exc_info else logger.info
        log("check_fn %s %s; dependent tools will be unavailable this turn", _fn_label(fn), outcome,
            exc_info=exc_info)
        _check_fn_cache[cache_key] = (now, False)
        return False
```

**两种"失败"被区别对待：**

| 情况 | 判断 | 返回 | 缓存吗 |
|---|---|---|---|
| 从来没成功过（没配 key / 没装二进制） | 真的不可用 | **`False`** | 缓存 `False`（30s TTL） |
| **最近 60 秒内成功过，这次失败** | **抖动（flake）** | **`True`（继续可用！）** | **不缓存**——下次调用**重新探测** |

> 📌 **这个设计的价值在于它防的是"能力忽然消失"，而不是"探测变慢"。**
>
> 想象一个真实场景：`check_fn` 是"honcho 服务在不在跑"。某次网络抖动让它探测失败——
> **如果直接相信这次失败，模型眼前的一批工具会突然消失**。
> 而模型的反应是：它之前的计划里有这些工具，现在 schema 里没有了，
> 于是它开始**编造调用**或者**换一套笨办法**。
>
> 所以判据是：**"这个能力刚刚还在吗？"**
> 在 → 一次失败更可能是抖动 → **继续提供服务，但记住这次可疑**（不缓存，下次再探）。
> 不在 → 失败就是事实 → 不可用。
>
> **关键的第二个动作是"不缓存"**：如果它缓存了那个 `True`，抖动就会变成
> "假装还能用 30 秒"。**"继续乐观 + 立刻重新检查"** 才是对的组合。

#### 探测失败还分级记录，而且异常必须留着

```python
# 接上面那句 log
log = logger.warning if exc_info else logger.info
```

注释给了理由，还带一个 issue 号：

> A `False` verdict is the expected state for optional, unconfigured toolsets;
> **only a raised probe is actionable**. (#87950)
>
> （`False` 是"可选但没配"的**正常状态**；**只有"探测自己抛异常"才需要处理**。）

> 📌 **"探测返回 False"和"探测炸了"是两件事，而且严重级别相反地反直觉：**
>
> - 返回 `False`（没配 key）→ **正常的**，INFO
> - 抛异常（探测代码有 bug / 解析器坏了）→ **要处理的**，WARNING
>
> 而 #87950 那个注释还在防一个更细的问题：
> **异常必须被显式保存并在 `except` 块外面用**，否则 `exc_info=True` 在块外解析不到东西，
> 于是日志里显示成"raised"却**没有堆栈**——
> *a bare "raised" verdict reads as "nothing configured"*
> （一个光秃秃的 "raised" 判语，读起来像"什么都没配置"。）
>
> **"日志说了有事，但没说是什么事"** —— 这是排障里最难的一类：你知道出了问题，
> 但现场信息已经丢了。

配套的公开面（原文一个都没提）：

```python
def no_cache_check_fn(fn)                    # 包一层：这个 check_fn 永不缓存
def check_fn_cache_scope() -> Optional[str]  # 当前的缓存作用域（就是 profile home key）
def _check_fn_cached(fn) -> bool             # 带 TTL 的取结果
def invalidate_check_fn_cache() -> None      # 手动失效（"我刚装了 key，立刻生效"）
def get_cached_check_fn_result(fn) -> Optional[bool]   # 只读，不触发探测
```

而 `get_definitions()` 用的那个 `_memo_check` **不是**这层 TTL 缓存——它是**单次调用内的去重**：

```python
# tools/registry.py:364
def _memo_check(fn: Callable, memo: Dict[Callable, bool]) -> bool:
```

也就是说，**原文说的"Cached per-call"确实存在**（就是 `_memo_check` + `check_results` dict），
但它只是**最外面那一层**；底下还有一层进程级 TTL 缓存。原文只写了外层。

#### 为什么需要两层，而不是一层？

三层数据，问的问题不同：

| 层 | 作用域 | 回答的问题 |
|---|---|---|
| `check_results` dict（per-call） | 一次 `get_definitions()` | "这一批工具里，同一个 `check_fn` 跑过了吗？" |
| `_check_fn_cache`（TTL 30s / 512 条） | **一个进程** | "30 秒内问过了吗？（探测可能很贵：要起子进程、要网络）" |
| 探测本身 | —— | "这个能力现在到底在不在？" |

> 📌 **"per-call 去重"解决的是浪费，"进程级 TTL"解决的是延迟。**
> 前者是"同一个函数别跑两遍"，后者是"别每轮都去起一个 `playwright` 子进程探浏览器在不在"。
>
> 30 秒这个数是为了一句注释里写明的目的：
> `get_definitions` 的 docstring 说 *Probes use the ~30 s TTL cache so ``hermes tools enable``
> lands quickly.* ——**让你刚启用一个工具，30 秒内就能用上**，
> 而不是要重启进程。

#### `hermes_home_key()` —— 缓存为什么要按 profile 分键

`tools/AGENTS.md` 把这条单列出来：

> `check_fn` answers reachability/opt-in **for the profile it runs under**, never surface.
> Results are TTL-cached in `registry.py::_check_fn_cache` keyed by `hermes_home_key()`,
> and one process serves many sessions AND many profiles: a probe reads credentials through
> `agent.secret_scope.get_secret`, never bare `os.getenv` (that answers with the launch
> profile's `.env` for everyone).

（`check_fn` 回答的是"**它自己所在的 profile** 能不能到达/有没有开"。
结果按 `hermes_home_key()` 分键缓存——而**一个进程服务多个会话、也服务多个 profile**：
探测必须走 `get_secret` 读凭据，不能裸用 `os.getenv`，否则**所有人得到的都是启动 profile 的答案**。）

> 📌 这条**不能靠"更小心"来避免**。裸 `os.getenv` 不会报错，它只是**答错**——
> 而且答的是一个**存在的、合理的**值（启动 profile 的 `.env`）。
>
> 判断标准（`tools/AGENTS.md` 原文）：
> - `check_fn` 回答**可达性 / 有没有开** → 可以
> - `check_fn` 回答**"我是不是被 Electron 拉起来的"** → **不行**
>   （那是**会话的**属性，不是进程的；`check_fn` 结果是进程级缓存的，放不进会话级答案）
>
> 这条的完整版在 `AGENTS.md` 里叫 **"capability is a property of the SESSION"**。

#### 异常当"不可用"还有一层分级

`tools/AGENTS.md` 补充：`UnscopedSecretError` 在捕获点被分级——
**启动时无作用域的探测是 DEBUG 级别，不是 traceback**。

> 📌 **同一个异常，在"启动探测"和"正常调用"里的严重级别不同。**
> 启动时没有 secret scope 是**正常的**（还没有会话）；
> 运行中读到未绑定的 secret 才是 bug。**这不是"放宽检查"，是"知道自己在什么阶段"。**

#### 真实实现的五种形态（47 处注册带 `check_fn`）

机制讲完了，看看**真实的人在里面写什么**。从最简到最重：

**形态 1 · 恒真型（也要显式写出来）**

```python
# tools/delegate_tool.py:80
def check_delegate_requirements() -> bool:
    """Delegation has no external requirements -- always available."""
    return True
```

> 📌 为什么"没有依赖"也要写一个函数？因为**显式比省略更强**：
> 它说的是"**我想过了，它没有前置条件**"，而不是"某人忘了写"。
> 语义上的差别是：前者是一个**承诺**，后者是一个**缺口**。

**形态 2 · 转发型（两个工具共用前置条件）**

```python
# tools/__init__.py:6
def check_file_requirements():
    """File tools only require terminal backend availability."""
    from .terminal_tool import check_terminal_requirements
    return check_terminal_requirements()
```

> 文件工具和终端工具**共用同一个前置条件**（file tools 依赖 terminal backend）。
> 转发而不是复制——一处改动两处生效。这和"表 vs if/elif"是同一个方向：
> **同一个事实只能有一个来源。**

**形态 3 · 凭据 / 可导入型（★ 这条最能说明机制为什么存在）**

```python
# tools/feishu_lark.py:26
def _check_feishu():
    # find_spec checks importability without executing lark_oapi's __init__, which
    # eagerly loads websockets/dispatcher/every api model (~5s). This probe fires at
    # every ``hermes`` startup; the handlers still do the real import when invoked.
    import importlib.util
    try:
        return importlib.util.find_spec("lark_oapi") is not None
    except (ImportError, ValueError):
        return False
```

三个点，逐句对应注释：

| 代码 | 注释说的理由 |
|---|---|
| 用 `find_spec` 而不是 `import` | 真 import 会**立刻加载 websockets / dispatcher / 每个 API model ≈ 5 秒** |
| 为什么这 5 秒不能忍 | **这个探测在每次 `hermes` 启动时都会跑** |
| handler 里仍然真 import | *the handlers still do the real import when invoked* —— 干活时才付这笔钱 |

> 📌 **"探测要便宜，干活时才付钱"** —— 这是 `check_fn` 机制的第一性理由。
> 它不是一个"顺便查一下"的地方，它在**启动路径 / 每轮 schema 构建路径**上。

**形态 4 · 配置型 + 顺手存状态**

```python
# tools/memory_tool.py:227
@no_cache_check_fn
def check_memory_requirements() -> bool:
    """Snapshot store flags and report whether the built-in tool is available."""
    _memory_surface_flags.set(None)
    flags = get_builtin_memory_store_flags()
    _memory_surface_flags.set(flags)          # ← 顺手把结果塞进 contextvar
    return flags[0] or flags[1]
```

两个点：

1. **它标了 `@no_cache_check_fn`** —— 因为它只是**读本地配置**（便宜），
   而且**必须立刻反映**（用户改了 config 要马上生效，不能等 30 秒 TTL）
2. **它在探测的同时把状态存进了 contextvar** —— 于是后面的人不用再读一遍配置

> 📌 **一次探测可以同时回答两个问题**："能不能用" + "用的是哪份配置"。
> 这个"顺手"不是偷懒：如果两处分别读配置，就可能**读到两份不同的**（配置在两次读之间变了）。
> 同一份快照分发给两个人 = 两个人看到的一定一致。

**形态 5 · 多后端分支型（最重的一个）**

```python
# tools/browser_tool_install.py:294
def check_browser_requirements() -> bool:
    """Whether the browser tools should be advertised.

    Local mode needs the ``agent-browser`` CLI plus a Chromium build (except Lightpanda-only
    text workflows); cloud mode needs the CLI plus provider credentials (the provider hosts
    its own Chromium)."""
    _bt = _origin()
    # Browser Use CLI backend: browser_exec replaces the whole browser_* surface
    if _bt._is_browser_use_cli_mode():
        return False
    # Camofox only needs the server URL, no agent-browser CLI.
    if _bt._is_camofox_mode():
        return True
    # CDP override needs no local binary. Raw (no-I/O) check: this runs during schema build,
    # where a stale endpoint must not cost a blocking probe.
    if _cdp._get_cdp_override_raw():
        return True
    # Do not exec ``agent-browser --version`` here: Windows .cmd shims flash a console during
    # Desktop startup. Execution paths still validate.
    try:
        browser_cmd = _find_agent_browser(validate=False)
    except FileNotFoundError:
        return False
    # Termux: the bare npx fallback is too fragile to advertise as a satisfied local dependency.
    if _requires_real_termux_browser_install(browser_cmd):
        return False
    # Cloud mode also requires provider credentials; no local Chromium needed.
    provider = _cloud._get_cloud_provider()
    if provider is not None:
        return provider.is_available()
    # Lightpanda provides text/navigation tools without Chromium; screenshots/vision still
    # return install errors.
    if _lp._using_lightpanda_engine():
        return True
    # Local Chrome mode needs Chromium on disk or the CLI hangs until the command timeout.
    return _chromium_installed()
```

**这段里有两句注释值得单独读，它们都在讲"探测本身有副作用"：**

**(a) `# Do not exec agent-browser --version here: Windows .cmd shims flash a console during
Desktop startup.`**

> 在 Windows 上执行一个 `.cmd` **会闪一个黑框**。而这段代码在**桌面 app 启动时**跑。
> 所以这里用 `validate=False`（**不真跑**），真验证留给执行路径。
>
> 📌 **探测不能有用户可见的副作用。** 一个"只是想查一下"的函数，
> 在启动路径上跑一次就会有**看得见的后果**——这才是它被明确禁止的原因。

**(b) `# Raw (no-I/O) check: this runs during schema build, where a stale endpoint must not
cost a blocking probe.`**

> 这一句更直接：**它跑在 schema 构建路径上，一个过期的 endpoint 不能让每一轮都卡住。**
> 所以用 `_get_cdp_override_raw()`（**不联网**的版本）。
>
> 📌 同一个信息有两个版本：**raw（不 I/O）** 和 **真探测**。
> 在"每轮都跑"的地方用前者，在"用户主动操作"的地方用后者。
> **这是一个通用手法：给同一件事提供"昂贵但准"和"便宜但粗"两个接口，按调用位置选。**

#### `check_fn` 的契约（名字看不出来的那部分）

`check_fn` 这个名字只说了一半——"一个检查函数"。真正约束你**能在里面写什么**的是它下面这七条契约：

| 契约 | 为什么 | 从哪看出来 |
|---|---|---|
| 返回 `bool`，**抛异常当 `False`** | 调用方不 try/except（fail-safe） | `_run_check_fn_uncached` 里的 `except: return False` |
| **必须便宜** | 每 30 秒就可能跑一次，且在启动路径上 | `_CHECK_FN_TTL_SECONDS = 30` + feishu 的 ~5s 注释 |
| **不能有可见副作用** | 它会在桌面 app 启动时跑 | browser 那句 "Windows .cmd shims flash a console" |
| **不能阻塞 / 做 I/O** | 在 schema 构建路径上 | browser 那句 "must not cost a blocking probe" |
| **凭据要走 `get_secret`，不能 `os.getenv`** | 一个进程服务多个 profile | `UnscopedSecretError` 的处理分支 |
| 结果被**进程级缓存** | 所以它**不能**回答"这次会话专属"的问题 | `_check_fn_cache` + `_load_enabled_toolsets` 的分工 |
| 想不缓存：`@no_cache_check_fn` | 本地配置读，必须立刻生效 | `memory_tool` / `kanban_tools` / `browser_vault` 三处 |

> 📌 **最后那两条是同一条边界的两个方向**，值得说清楚：
>
> - **`check_fn` 回答"这个环境里它现在能用吗"** → 环境是**进程级**的，所以可以缓存
> - **`toolset` 回答"这次会话该不该给它"** → 会话是**每次不同**的，所以不能进 `check_fn`
>
> `AGENTS.md` 把这条反例写得很具体（而且给了它一个名字）：
>
> > `check_fn` answers reachability or opt-in, not surface. "Is the bridge wired?" — fine.
> > **"Was I spawned by Electron?"** — not. `check_fn` results are TTL-cached process-wide
> > (`tools/registry.py`); **a per-session answer does not belong there.**
>
> （"这座桥接好了吗" —— 可以。"我是不是被 Electron 拉起来的？" —— **不行**，
> `check_fn` 的结果是进程级缓存的，**会话级的答案不该放在那儿**。）
>
> 而且这个反例**有真实后果**：桌面 app 可能驱动
> 本地后端子进程、SSH 上的后端、URL+token 的后端、或 Hermes Cloud，
> 而 `HERMES_DESKTOP=1` 只在**前两种**上存在。用环境变量当闸门 →
> 在其他拓扑下**静默失效**：工具被从 schema 里剔掉，
> 而平台提示还在告诉模型"你在 Hermes 桌面 app 里"。
>
> 正确做法是 `AGENTS.md` 给的：**闸门放 toolset**（`desktop_ui` / `project`），
> 由 `_load_enabled_toolsets(platform)` 在**会话来源**那一层折进来。
> 一句话判据（原文）：*if the capability still makes sense with the client on another
> machine, it is session-scoped.*

### 【动手】mini 版现状

**mini 版还没有 `check_fn`。** 这是本章要加的第三样东西。

现状是"所有注册进 `_REGISTRY` 的工具都无条件发给模型"。加 `check_fn` 之后，
`get_tool_schemas()` 要变成"先问一遍可用性，再决定发不发"。

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

### 【源码】真身

#### ① 规模：60 个 toolset / 59 个核心工具

```console
$ TOOLSETS 的键数
60
$ len(_HERMES_CORE_TOOLS)
59
```

`_HERMES_CORE_TOOLS`（`toolsets.py:11`）就是原文"包含全部已知 toolset"那句背后的东西：

> `_HERMES_CORE_TOOLS` is the default bundle **every platform's base toolset inherits**,
> not dead code.
>
> （`_HERMES_CORE_TOOLS` 是**每个平台的基础 toolset 都继承**的默认捆，不是死代码。）

59 个工具里有：18 个 `browser_*`、12 个 `kanban_*`、8 个 `ha_*`、
以及 `terminal` / `read_file` / `write_file` / `patch` / `search_files` / `web_search` /
`execute_code` / `memory` / `todo_list` / `skill_*` 等。

#### ② ⚠️ **"注册 ≠ 暴露"** —— 原文漏了这条硬规矩

这是 `tools/AGENTS.md` 里写得最重的一条，开发者文档**完全没提**：

> **Required** — discovery registers the schema, but a tool is only exposed if a toolset
> names it. `_HERMES_CORE_TOOLS` is the default bundle every platform's base toolset
> inherits, not dead code.

（**必须做**——发现机制只是**注册了 schema**，但一个工具**只有在某个 toolset 点名它之后才会被暴露**。）

于是"加一个核心工具"是**两个文件**：

| 文件 | 做什么 |
|---|---|
| `tools/your_tool.py` | `registry.register(...)` —— 让工具**存在** |
| `toolsets.py` | 把名字加进 `_HERMES_CORE_TOOLS` 或新 toolset —— 让它**可见** |

**只做第一步的后果**：工具注册成功了、日志里没有任何异常、
`registry.get_entry("your_tool")` 能拿到……但**模型永远看不到它**。

> 📌 这是本章第三次出现"**静默失败**"，而且这次它的位置很反直觉：
> **你以为"注册"就等于"生效"，其实中间还隔着一个 toolset 白名单。**
>
> 对照 `tools/AGENTS.md` 给的判断方式：
> > Test the handler through the registry (real dispatch), not the bare function only;
> > assert contracts ("every registered tool has a toolset", "no schema description names
> > a tool from another toolset") rather than tool counts.
>
> （**走注册表测 handler（真分发），不要只测裸函数**；
> 断言**契约**——"每个注册的工具都有 toolset"——**而不是工具数量**。）
>
> "断言契约而不是数量"这条也值得记：数量断言是"变更探测器"，
> 每次加工具都要改测试，而且它**不告诉你任何关系**。

#### ③ 动态 schema 修补：**10 条，不是 2 条**

原文说"`execute_code` 和 `browser_navigate`"，真实是一张 10 项的表：

```python
# model_tools.py:463
_DYNAMIC_SCHEMA_REWRITERS = {
    "execute_code": _rewrite_execute_code,
    "discord": _discord_rewriter("get_dynamic_schema_core"),
    "discord_admin": _discord_rewriter("get_dynamic_schema_admin"),
    "browser_navigate": _rewrite_browser_navigate,
    "browser_cdp": _rewrite_browser_cdp,
    "browser_exec": _compose_rewriters(_rewrite_browser_exec, _rewrite_input_tool_for_vault),
    "browser_type": _rewrite_input_tool_for_vault,
    "browser_vault_list": _rewrite_browser_vault,
    "browser_vault_fill": _rewrite_browser_vault,
    "delegate_task": _rewrite_delegate_task,
}
```

执行顺序也有一句注释值得读：

```python
# model_tools.py:477
def _apply_dynamic_schemas(tool_defs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply _DYNAMIC_SCHEMA_REWRITERS in list order; the availability set is a
    snapshot taken before any rewrite (no rewriter's inputs are droppable)."""
    available = {t["function"]["name"] for t in tool_defs}   # ← 先取快照
    ...
```

> 📌 **"可用集合"在任何改写之前先取快照。**
>
> 理由在 rewriter 的真身里：`_rewrite_execute_code` 是这样用它的——
>
> ```python
> def _rewrite_execute_code(td, available):
>     """List only sandbox tools that are actually available."""
>     # Without this, the model sees "web_search is available in execute_code" even when the API key isn't
>     # configured or the toolset is disabled (#560-discord).
>     return _fn_def(build_execute_code_schema(SANDBOX_ALLOWED_TOOLS & available, ...))
> ```
>
> 注意那个 `& available`（**集合求交**）——它把"沙箱里允许的工具"和"这一轮真的有"求交，
> 于是 schema 里只会出现**真的能用**的名字。而 `_discord_rewriter` 更狠：
> **返回 `None` 就把整个工具丢掉**（`if td is not None: out.append(td)`）。
>
> 如果边改边算，后面的 rewriter 拿到的 `available` 就被前面的改动作污染了——
> **"我引用的工具可用吗"这个判断会依赖于"谁先跑"，而不是事实。**
>
> 又是**校验/判断要对着一个稳定基准**，和第 3 章那条同源。

#### ④ "禁止 schema 描述里点名别的 toolset 的工具" —— 一条硬规矩

`tools/AGENTS.md`：

> **Schema descriptions must not name tools from other toolsets** (`browser_navigate` saying
> "prefer `web_search`"). Those tools may be unavailable (missing key, disabled toolset) and
> the model hallucinates calls to them. Cross-references are added dynamically in
> `get_tool_definitions()`.

（**schema 描述里不许点名其他 toolset 的工具**。那些工具可能不可用，模型会幻觉出对它们的调用。
交叉引用由 `get_tool_definitions()` **动态**添加。）

> 📌 这一条把"**静态写死的提示词**"和"**运行时才知道的事实**"分开了：
> schema 是**导入时**构建的，那时根本不知道哪些工具会被过滤掉；
> 所以"我们还有什么工具"这句话**只能在过滤之后**才说得准。
>
> 这就是为什么需要 §4③ 那 10 个 rewriter——**它们是"把运行时事实写回 schema"的唯一通道。**

#### ⑤ toolset 别名有两套机制

原文只说了 `_LEGACY_TOOLSET_MAP`（静态映射）。真实还有**动态别名注册表**：

```python
# tools/registry.py:482
def register_toolset_alias(self, alias: str, toolset: str) -> None:
    """Register an explicit alias for a canonical toolset name。"""
    existing = self._toolset_aliases.get(alias)
    if existing and existing != toolset:
        logger.warning("Toolset alias collision: '%s' (%s) overwritten by %s", alias, existing, toolset)
    self._toolset_aliases[alias] = toolset
```

同名的别名被指向不同 toolset 时**警告 + 覆盖**（不是拒绝）——因为这是"后来者更清楚"的场景
（插件知道自己该归到哪个 toolset）。

另外还有**工具名**的旧名映射（原文也没提）：

```python
# model_tools.py:611
# Legacy tool-name aliases accepted at every dispatch seam (old sessions/saved
# prompts keep working); schemas advertise only new names.
_LEGACY_TOOL_ALIASES = {
    "todo": "todo_list", "cronjob": "cronjob_manage", "process": "process_manage",
    "tour": "gui_tour", "tip": "show_tip",
}
```

> 📌 **注意这两套别名的方向相反**：
> - **schema 只宣传新名**（模型看到的永远是 `todo_list`）
> - **dispatch 接受旧名**（老会话、存下来的提示词里写的 `todo` 还能用）
>
> 这就是"**对外收窄、对内兼容**"：**新接口只暴露一种写法，老写法在入口处兜住。**
> 顺带解释了原文里那个 `todo` 是怎么来的——**那是个旧名**（见 §9）。

#### ⑥ tool defs 也有缓存

```python
# model_tools.py:204
_TOOL_DEFS_CACHE_MAX = 8
def _clear_tool_defs_cache() -> None: ...
_the_tool_defs_cache_key(...)  # 键里含 config.yaml 的 mtime+size
```

- 上限 **8** 条（不是无限）
- 缓存键包含 **config 文件的 mtime + size** → 改了配置自动失效

> 📌 又一次"**缓存必须能被真实事件失效**"（第 3 章那条原则的应用）：
> 不是"定时刷新"，而是"**配置变了就自动失效**"——
> 而"配置变没变"用的是文件系统给的 `(mtime, size)`，不需要额外机制。

### 【动手】mini 版现状

**mini 版没有 toolset 解析。** `get_tool_schemas()` 就是"把注册表里的全发出去"：

```python
# mini-hermes/model_tools.py:37
def get_tool_schemas(include_agent_level: bool = True) -> List[Dict[str, Any]]:
    schemas = registry.schemas()
    if include_agent_level:
        from tools.todo import AGENT_LEVEL_TOOLS
        schemas.extend(AGENT_LEVEL_TOOLS.values())
    return schemas
```

注意这里有个**真实存在的落差**：mini 的 `registry` 里已经有 `toolset` 字段
（`register_tool(toolset="core")`），但**从来没人用它过滤**。
字段有了，语义没实现——这是一个"看起来做了，其实没做"的典型。

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

### 原文

> At runtime, tools are dispatched through the central registry, with agent-loop exceptions
> for some agent-level tools such as memory/todo/session-search handling.

（运行时工具经中心注册表分发，少数 agent 级工具（memory/todo/session-search 这类）走
agent loop 例外。）

#### 原文给的流程

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

### 【源码】真身

#### ① `handle_function_call()` 的真实顺序：11 步，不是 6 步

原文那张流程图是对的，但漏了**一大半**。真实的 `model_tools.py:867` 是这样：

```python
def handle_function_call(function_name, function_args, task_id=None, tool_call_id=None,
                         session_id=None, turn_id=None, api_request_id=None, user_task=None,
                         enabled_tools=None, skip_pre_tool_call_hook=False,
                         skip_tool_request_middleware=False, skip_tool_execution_middleware=False,
                         tool_request_middleware_trace=None, enabled_toolsets=None,
                         disabled_toolsets=None) -> str:
```

15 个参数。执行顺序：

| # | 步骤 | 原文有吗 |
|---|---|---|
| 1 | `coerce_tool_args()` 参数归一 | ✗ |
| 2 | **`_LEGACY_TOOL_ALIASES`** 旧工具名映射（`todo` → `todo_list`） | ✗ |
| 3 | **tool_search 桥**：`tool_search` / `tool_describe` 是目录读取，**内联处理**；`tool_call` 被**拆包**成真实工具名再往下走 | ✗ |
| 4 | **connectors**（`connectors__<name>__<tool>`）+ `manage_connections` 的可用性闸门 | ✗ |
| 5 | **请求中间件**（`_apply_request_middleware`） | ✗ |
| 6 | **agent-loop 守卫**：`if function_name in _AGENT_LOOP_TOOLS: return tool_error(...)` | ✓（原文说的是"直接由 loop 处理"） |
| 7 | **pre-dispatch 守卫**（`_pre_dispatch_guards`）——包括审批 | ✓（原文只说了 pre-hook） |
| 8 | **读循环计数重置**：非读/搜类工具会重置"连续读"计数器 | ✗ |
| 9 | `_execute_tool()` → 最终到 `registry.dispatch()` | ✓ |
| 10 | post_tool_call hook（带 `duration_ms`） | ✓ |
| 11 | 错误清洗（`_sanitize_tool_error`） | ✗ |

**第 6 步是真身里的守卫**，值得单独看：

```python
# model_tools.py:932
try:
    if function_name in _AGENT_LOOP_TOOLS:
        return tool_error(f"{function_name} must be handled by the agent loop")
```

而那个集合的定义（**注意和原文的差异，见 §9**）：

```python
# model_tools.py:607
_AGENT_LOOP_TOOLS = {"todo_list", "memory", "session_search", "delegate_task"}
```

**第 3 步那个"拆包"设计**值得展开一句：

```python
# model_tools.py:897
# Tool Search bridge: tool_search / tool_describe are catalog reads handled
# inline; tool_call is unwrapped so every downstream hook (pre/post, edit
# approval, guardrails) sees the real tool name, never the bridge.
```

（`tool_search`/`tool_describe` 是目录读取，内联处理；
`tool_call` 被**拆包**，好让每个下游 hook（pre/post、编辑审批、护栏）
看到的是**真实工具名**，而不是那层桥。）

> 📌 **"包装层不能泄漏到下游"** —— 这是一个通用设计约束。
> 如果 `tool_call(actual="terminal", args=...)` 这个桥的名字被下游 hook 看到，
> 那么**审批规则会失效**（审批表里没有叫 `tool_call` 的东西）、
> **统计会失真**、**护栏看不见真实意图**。
>
> 和第 3 章"临时层不能进历史"是同一类要求的另一面：
> **中介层的存在不应该改变被中介双方看到的事实。**

#### ② 错误包装其实是**三层**，而且错误文本有上限

原文说了两层，真实还有**第三层**（在 `registry.dispatch` **之前**的 `_execute_tool`），
以及**上限控制**：

```python
# tools/registry.py:25
_MAX_TOOL_ERROR_CHARS = 2048
_TOOL_ERROR_TRUNCATION_MARKER = "… [truncated]"
_MAX_LOGGED_ERROR_CHARS = 8192

def _bound_error_text(text: str) -> str: ...
def _bound_json_error_result(result: str) -> str: ...
```

```python
# tools/registry.py（dispatch 的 except 分支）
except Exception as e:
    # exc_info already renders the exception, so keep the message copy bounded.
    logger.exception("Tool %s dispatch error: %s", name, _bound_error_text(str(e)))
    # Sanitize so framing tokens/CDATA/fences in exception text aren't structural noise.
    raw = f"Tool execution failed: {type(e).__name__}: {e}"
    try:
        from model_tools import _sanitize_tool_error
        sanitized = _sanitize_tool_error(raw)
    except Exception:
        sanitized = raw  # defensive: never let the sanitizer block error propagation
    return tool_error(sanitized)
```

两个原文没说的关键设计：

**(a) 错误文本必须截断（2048 / 8192）。** 注释说明了理由链：

> `_bound_error_text` 的 docstring：*the error body is bounded so a raw
> exception can't bloat history across retries.*
>
> （错误体有上限，**这样一个原始异常不会在重试中把历史撑爆**。）

> 📌 **异常文本会进历史，历史会随每次请求重发，所以"一条超长异常"的成本是复利的。**
> 一个 200KB 的 traceback 会在**之后每一轮**都被重新发送。
>
> 这和第 3 章"截断"是同一个洞察：**进上下文的东西要有上限**。
> 只不过第 3 章管的是工具读回来的内容，这里管的是异常。

**(b) 错误文本必须清洗。** `_sanitize_tool_error` 的理由（注释）：

> Sanitize so framing tokens/CDATA/fences in exception text aren't structural noise.
>
> （清洗掉异常文本里的**框架标记 / CDATA / 代码围栏**，免得它们变成结构噪声。）

> 📌 这是**注入防御**：异常信息里可能**碰巧**（或恶意地）含有
> `<|im_start|>`、`]]>`、` ``` ` 这类在别处有结构性含义的字符串。
> 一个工具的报错信息不应该能把消息格式搞坏。
>
> 而且这段代码自己还包了 try/except，理由是：
> *never let the sanitizer block error propagation*——
> **清洗失败也必须让错误传出去。** 这是一个"辅助机制不能比主流程更严格"的范例。

#### ③ ⚠️ agent 级工具：**有两层不同的集合，14 个 vs 4 个**

这是本章**最重要的一处出入**。原文说"四个工具被拦截"，但真实有两层：

| 集合 | 在哪 | 数量 | 作用 |
|---|---|---|---|
| **`INLINE_TOOL_EXECUTORS`** | `agent/inline_tool_executors.py` | **14** | **执行表**：agent loop 拿它来跑 agent 级工具 |
| **`_AGENT_LOOP_TOOLS`** | `model_tools.py:607` | **4** | **守卫**：这 4 个如果到了 dispatch，返回错误 |
| 关系 | —— | —— | **前者 ⊇ 后者**（4 个全在 14 个里） |

14 个的真身：

```python
# agent/inline_tool_executors.py（顺序即历史上 execute_tool_calls_sequential 的 if/elif 顺序）
INLINE_TOOL_EXECUTORS = {
    "todo_list":        _tool("tools.todo_tool", ...),
    "message_agent":    _tool("tools.bot_mode_dm", ...),
    "session_search":   _session_search,
    "memory":           _memory,
    "clarify":          _tool("tools.clarify_tool", ...),
    "read_terminal":    _callback_tool("tools.read_terminal_tool", ...),
    "desktop_preview":  _desktop_preview,
    "drive_preview":    _callback_tool("tools.drive_preview_tool", ...),
    "annotate_preview": _callback_tool(...),
    "read_window_below": _callback_tool(...),
    "gui_tour":         _callback_tool(...),
    "manage_connections": _manage_connections,
    "setup_mcp":        _setup_mcp_shim,
    "delegate_task":    lambda agent, args, ctx: ...,
}
```

**只在 14 里、不在 4 里的那 10 个**：

```
annotate_preview, clarify, desktop_preview, drive_preview, gui_tour,
manage_connections, message_agent, read_terminal, read_window_below, setup_mcp
```

看这 10 个的**共同特征**：它们需要的是 **agent 的 callback / 会话界面**，
**不是某个 Store**：

| 工具 | 它需要什么 |
|---|---|
| `clarify` | `agent.clarify_callback`（要弹窗问用户） |
| `read_terminal` / `desktop_preview` / `drive_preview` / `annotate_preview` / `read_window_below` / `gui_tour` | 桌面/终端界面回调 |
| `message_agent` | Bot Mode 的会话身份 + task_id |
| `manage_connections` / `setup_mcp` | 网关连接配置 + session_id |

> 📌 **所以原文给的理由（"需要 agent 级状态（TodoStore、MemoryStore 等）"）是不完整的。**
>
> 准确的说法是：**凡是"执行需要 agent 对象本身"的工具**都要走这张表——
> 需要 Store 只是其中一类，**需要 callback / 界面 / 会话身份是另一类，而且是更大的一类**。
>
> 这个区分有实际后果：
> - `_AGENT_LOOP_TOOLS`（4 个 = 需要 Store 的）**还要额外挡 dispatch**，
>   因为它们**功能上不该被独立调用**（todo 的状态必须由 loop 管）
> - 那 10 个 callback 类工具**不需要那个守卫**——它们是"只有在特定界面下才有意义"，
>   如果界面不在，`check_fn` 或 toolset 解析阶段就把它过滤掉了（见 §4）
>
> 一句话：**"需要 agent 状态"和"需要 agent 界面"是两种不同的依赖，
> 原文把它们合成了一种，于是数量少算了 10 个。**

#### ④ async 桥接的**真实**三条路

原文三句话是对的，实现在 `model_tools.py:92`，比原文复杂得多：

```python
def _run_async(coro):
    """Run a coroutine from sync code; safe under a running loop (gateway/RL env)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # Inside a running loop: run in a fresh thread whose loop we keep a
        # reference to, so on timeout we can cancel the task inside it
        # (ThreadPoolExecutor.cancel() is a no-op on a running worker).
        ...
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Carry profile + approval/sudo context so get_hermes_home() resolves correctly.
        from tools.thread_context import propagate_context_to_thread
        future = pool.submit(propagate_context_to_thread(_run_in_worker))
        try:
            return future.result(timeout=300)
        except concurrent.futures.TimeoutError:
            # Cancel inside the worker's own loop so the thread can wind down.
            ...
            raise
        finally:
            pool.shutdown(wait=False)  # never block the caller on a stuck coroutine
```

**⚠️ 真实的分支判据不是"CLI / 网关"，而是"在哪个线程 + 有没有 running loop"：**

```python
# model_tools.py:142（上面那段 if 之后）
    if threading.current_thread() is not threading.main_thread():
        return _get_worker_loop().run_until_complete(coro)
    return _get_tool_loop().run_until_complete(coro)

def _get_tool_loop():
    """Long-lived event loop for async tool handlers on the main thread."""

def _get_worker_loop():
    """Persistent event loop for the current worker thread (thread-local)."""
```

| 真实条件 | 用哪个 loop | 保住了什么 |
|---|---|---|
| 有 running loop（网关 / RL 环境） | 一次性线程 + 它自己的 loop | 不嵌套，且超时时能在**那个 loop 里**取消 |
| **非主线程**、无 running loop | `_get_worker_loop()`（**线程局部**常驻） | 每个 worker 线程一个，互不干扰 |
| **主线程**、无 running loop | `_get_tool_loop()`（**进程级**常驻） | async client 的连接池活着 |

> 📌 文档那三个名字（CLI / 网关 / worker）是这三种组合的**常见场景**，不是代码里的判据。
> 区别有实际后果：**任何"新起的线程里调 async 工具"都走第二条路**，
> 不管它是不是文档说的"并发工具线程"。

这段里藏着**四个**原文没说的设计：

**a) 为什么"有 loop 时"不能直接 `run_until_complete`？**
因为**你不能在一个已经跑着的 loop 里再跑一个 loop**（`RuntimeError`）。
也不能用 `asyncio.run()`（它要求当前线程没有 running loop）。
所以只能**扔到另一个线程**去跑。

**b) 为什么不用 `ThreadPoolExecutor.cancel()` 做超时取消？** 注释直接给了答案：

> *ThreadPoolExecutor.cancel() is a no-op on a running worker*
> （对已经在跑的 worker，`cancel()` 是空操作。）

于是必须**先把 worker 里的 loop 引用拿出来**，超时时用
`loop.call_soon_threadsafe(t.cancel)` **在它自己的 loop 里**取消任务。

> 📌 **"取消"必须在被取消对象自己的调度器上做。** 这是并发里的通用规律：
> 跨线程取消一个正在跑的任务，只有"投递到它的 loop 里"是安全的。
> 对照第 2 章那条：**`threading.Event` 只是旗子，不表达"什么时候停"**——
> 这里是同一件事的另一面：**旗子要有人去看，而且要看对地方。**

**c) 超时 300 秒** —— 有明确上限，不是无限等。

**d) `finally: pool.shutdown(wait=False)`** —— 注释：*never block the caller on a
stuck coroutine*（**绝不让调用方被一个卡住的协程阻塞**）。

> 📌 这行是"**善后工作不能比主流程更危险**"的范例：
> 关线程池本来是好习惯，但如果 `wait=True`，一个卡死的协程会让**整条路径**挂住——
> 于是"清理"变成了新的故障点。**没有 wait 是一种有意的放弃。**

**e) `propagate_context_to_thread`** —— 注释说明了目的：
*Carry profile + approval/sudo context so `get_hermes_home()` resolves correctly.*
（**带上 profile + 审批/sudo 上下文，好让 `get_hermes_home()` 解析正确。**）

这是"一个进程服务多个 profile"那条大原则的又一次出现：
**跨线程会丢掉上下文，所以要显式搬运。** `tools/AGENTS.md` 甚至点名了正确做法：

> New threads from scoped code use `agent.memory_provider.spawn_context_thread`
> (a bare `threading.Thread` drops the scope).

### 【动手】mini 版现状

mini 的 `handle_function_call` 已经走对了主干（名字 → 注册表 → 执行 → 兜异常），
但缺三件（本章要补的）：

| mini 现状 | 本章要改 |
|---|---|
| 错误返回 `"[error] ..."` **纯文本** | 改成 **JSON 字符串**（`{"error": ...}`）+ 截断 |
| 没有旧工具名映射 | 加 `_LEGACY_TOOL_ALIASES`（一个 dict，几行） |
| 没有 async 桥接 | **mini 没有 async 工具** → 记录"为什么可以不做"（见下） |
| agent 级工具用 `INLINE_TOOL_EXECUTORS`（第 2 章已有） | 保留，补一个 `_AGENT_LOOP_TOOLS` 守卫 |

**关于 async 桥接**：mini 的所有工具都是同步函数（`subprocess.run` / 文件读写），
所以**没有这个问题**。但这一节的**教学价值在于"为什么 Hermes 需要它"**：

> 一个 async 工具的典型来源是**共享连接池**（aiohttp client、数据库连接）。
> 如果每次调用都 `asyncio.run()` 起一个新 loop，**连接池会跟着 loop 一起死**，
> 于是每个工具调用都要重新建连接——这正好抵消了用 async 的一切好处。
>
> 所以三条路的分工是：
> - CLI（没 loop）→ **常驻 loop** 保住 async client 的缓存
> - 网关（有 loop）→ **没法常驻**（不能嵌套），只能一次性线程
> - worker 线程 → **每线程一个常驻 loop**（线程局部存储）

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

### 【源码】真身

#### ① ⚠️ `DANGEROUS_PATTERNS` **不在 `tools/approval.py` 里**

实测位置：

```console
$ grep -rn "^DANGEROUS_PATTERNS" tools/
tools/approval_detection.py:203:DANGEROUS_PATTERNS = [
tools/approval_detection.py:440:DANGEROUS_PATTERNS_COMPILED = [(re.compile(p, _RE_FLAGS), d) for p, d in DANGEROUS_PATTERNS]
```

**它在 `tools/approval_detection.py`。** `approval.py` 是那个**门面**（1336 行），
按主题拆成了 8 个文件：

| 文件 | 该看的时候 |
|---|---|
| `approval.py`（1336 行） | 门面：公开入口 |
| **`approval_detection.py`** | **`DANGEROUS_PATTERNS` 在这里（107 条）** |
| `approval_context.py` | 审批上下文（会话/profile 作用域） |
| `approval_floors.py` | "底线"——无论如何都不能自动批准的东西 |
| `approval_human_wait.py` | 等人类回答 |
| `approval_gateway_wait.py` | 等网关（消息平台）回答 |
| `approval_prompt.py` | 提示怎么渲染 |
| `approval_smart.py` | 辅助 LLM 的自动批准 |

> 📌 这是本仓库的**门面 + 兄弟**结构（`tools/AGENTS.md` 顶层的说法：
> *Find code by topic, not by facade*）：
>
> > **Find code by topic, not by facade:** `grep -rn "def name" <dir>/<stem>_*.py`.
> > Reading the facade first is the expensive way.
>
> （**按主题找代码，不要按门面找。先读门面是更贵的做法。**）
>
> 开发者文档说"定义在 `tools/approval.py`"——**按它去找会读完 1336 行才发现在别的文件**。
> 这是"文档跟不上重构"的典型：**文件拆分之后，文档里的路径就成了误导**。

#### ② 107 条模式，而且**预编译**

```console
$ DANGEROUS_PATTERNS 的条数
107
```

原文举了 7 类，真实是 **107 条**。而且它们被**预编译一次**：

```python
# tools/approval_detection.py:440
DANGEROUS_PATTERNS_COMPILED = [(re.compile(p, _RE_FLAGS), d) for p, d in DANGEROUS_PATTERNS]
```

> 📌 **`re.compile` 缓存的意义**：每个命令都要对**全部 107 条**跑一遍匹配。
> Python 的 `re` 模块内部有编译缓存（默认 512 条），但**显式预编译**有两个好处：
> ① 不受缓存上限/淘汰影响；② `_RE_FLAGS` 统一（大小写、多行等），
> 免得某一条忘记加 flag 导致漏检。
>
> **安全检测里"某一条正则没匹配上"是静默失败**——所以要消除这种可能性。

#### ③ 8 个文件的分工说明了审批的真实复杂度

原文的 5 步流程是对的，但**"Smart 审批"和"网关模式"各自是一整个文件**。
这说明真实系统里这两个不是"分支"，而是**两套独立的等待机制**：

```
approval_human_wait.py     ← CLI：等人在终端里回答
approval_gateway_wait.py   ← 网关：等人从消息平台回答（跨进程！）
approval_smart.py          ← 辅助 LLM 判断（不用等人）
approval_floors.py         ← 底线：这些即使 smart 说 OK 也不自动批
```

**`approval_floors.py` 是最值得注意的一个**——它说明系统里有一条
"**无论辅助判断说什么都不放行**"的底线。

> 📌 **这是"LLM 判断"这类机制的必备补丁。**
> 让一个 LLM 来判断"这个危险命令其实安全吗"很方便，但它的**失效模式是放行**——
> 所以必须有**不经过它的**一层。这和"fail-safe"是同一个方向：
> **当你不确定判断对不对时，选更保守的那个默认值。**

### 【动手】mini 版现状

mini 版的审批是**故意的简化**，而且简化得很诚实：

```python
# mini-hermes/tools/registry.py 的 register_tool 参数
requires_approval: bool = False,
danger_check: Optional[Callable[[Dict[str, Any]], bool]] = None,
```

```python
# mini-hermes/tools/registry.py 的 docstring
"""requires_approval + danger_check：工具自己决定"这次调用危不危险"
（terminal 传进来的是 looks_dangerous，只有 rm -rf 这类才拦，
不是说每条命令都要弹窗 —— 那样 agent 就没法用了）。"""
```

**mini 的做法**：危险检测是**工具自己传进来的一个函数**（`terminal.py` 传 `looks_dangerous`）。
Hermes 的做法：检测是**中心化的 107 条模式表**。

两种做法的**真实差别**不在于"谁更全"，而在于：

| | mini（工具自带检测） | Hermes（中心表） |
|---|---|---|
| 新增一个危险模式 | 改那个工具的代码 | 改**一张表** |
| 模式被绕过 | 只有那个工具的检测变弱 | 所有走审批的命令一起变弱 |
| 谁能审到 | 只有 terminal | 任何工具都能挂同一个检测 |

> ⚠️ **mini 的写法有个真实风险**：危险检测的逻辑住在**每个工具自己**的文件里。
> 于是"什么算危险"这件事**没有单一事实来源**——
> 加第二个需要审批的工具时，它得自己再写一份判断。
>
> 第 2 章已经见过这个形状（工具分发写两处 → 漂移分家）。
> **这一章要做的不是把它改成 107 条正则，而是把它挪到"一处"**。

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

### 【源码】真身

#### ① 后端清单：**文档是对的**，而且这里文档比 AGENTS.md 更准

```python
# tools/terminal_tool_backends.py:54
_BUILTIN_BACKENDS = "local, docker, singularity, modal, daytona, vercel_sandbox, ssh"
```

**7 个，和原文完全一致。** 顺带一提：`tools/AGENTS.md` 里列的是
`(local, docker, ssh, modal, daytona, singularity)` —— **6 个，少了 `vercel_sandbox`**。
所以这一节**是文档更准**（虽然 AGENTS.md 那行有个 "等"字兜着）。

> 📌 这提醒一件事：**"更靠近代码的文档"也不保证更准。**
> 两边都要按文件系统核。

还有两个原文没说的分类：

```python
# tools/terminal_tool_config.py:64
_CONTAINER_BACKENDS = frozenset({"docker", "singularity", "modal", "daytona", "vercel_sandbox"})

# tools/terminal_tool.py:579
_DEFAULT_CWD_BY_BACKEND = {"ssh": "~", "vercel_sandbox": _VERCEL_SANDBOX_DEFAULT_CWD}
```

- **5 个是"容器类"后端**（会改变文件可见性、网络、生命周期）
- **默认 cwd 是每后端不同的**——`ssh` 默认 `~`（远程主机的家），
  **`local` 没有条目**（因为本地就继承当前目录）

> 📌 **"默认 cwd 按后端不同"这件事，是"同一个工具在不同后端语义不同"的最小例子。**
> 第 3 章讲过"远端后端不给主机信息"（`build_environment_hints`）——
> 那是**提示词层**的应对；这里是**执行层**的应对。
> 同一条约束在两个地方各处理一次，因为它们是两个不同的层。

#### ② 每个后端是一个"环境类" + 一个"构建器"

```python
# tools/terminal_tool_backends.py:184
"vercel_sandbox": (lambda: importlib.import_module("tools.environments.vercel_sandbox")
                   .VercelSandboxEnvironment, ...)
```

**注意那个 `lambda` + `importlib`** —— 这是**懒加载**：
只有在真的要用这个后端时才 import 对应的环境模块。

`tools/environments/` 里的实现文件：

```
base.py            ← 抽象基类
base_output.py     ← 输出处理
base_session_env.py← 会话型环境
base_wait.py       ← 等待语义
local.py           ← ★ 本地（还有 3 个 local_*.py 处理策略/路径探测）
docker.py  docker_egress.py
ssh.py
modal.py   modal_utils.py  managed_modal.py
daytona.py
singularity.py
vercel_sandbox.py
```

> 📌 **`base_*.py` 有 4 个** —— 这说明"环境"这个抽象被拆成了几个独立的关注点：
> 环境本身 / 输出 / 会话 / 等待。**不是一个大基类。**
>
> 对照 `tools/AGENTS.md` 的形状规则：
> *Adding a backend = a new sibling or provider entry in the existing table,
> never an `elif` on a backend name.*
> （加后端 = 加一个兄弟文件或在现有表里加一条，**绝不是 `elif` 后端名**。）

#### ③ ⭐ 每个 spawn 都走同一个 env 构建器（原文完全没提，但是 hard rule）

`tools/AGENTS.md` 把这条写得很重：

> **Every spawn goes through one env builder.** `environments/local.py::build_subprocess_env`
> (+ `hermes_constants.apply_subprocess_home_env`, `env_passthrough.py::resolve_passthrough_value`)
> is how a terminal, `execute_code`, background process, delegation child, ACP or MCP stdio
> child gets its environment.

（**每一次 spawn 都走同一个 env 构建器**：终端、`execute_code`、后台进程、
委派子 agent、ACP 子进程、MCP stdio 子进程——**它们的运行环境都由同一个函数决定**。）

以及 profile 版本：

```python
environments/local.py::served_profile_child_env(target_home=, inherit_credentials=)
```

它做三件事：
1. 丢掉启动 profile 的 `.env` / `TERMINAL_*` 残留（`strip_launch_profile_env`）
2. 钉住目标 home
3. **只叠加目标自己的**密钥

而禁止的做法写得非常直白：

> `os.environ.copy()` / `dict(os.environ)` **pins the launch profile**;
> contextvars **do not cross process boundaries**, so resolve before `Popen`.

（`os.environ.copy()` 会把**启动 profile** 钉进去；contextvar **不跨进程边界**，
所以要在 `Popen` 之前就解析好。）

> 📌 这是"**一个进程服务多个 profile**"这条原则在**进程创建**这一层的表现，
> 也是本章第三次看到它（前两次：`check_fn` 缓存分键、`_run_async` 搬运上下文）。
>
> 规律：**凡是"隐式继承当前进程状态"的地方，都是 profile 泄漏的入口。**
> `os.environ.copy()` 看起来是"原样传下去"（无害），实际是"把**启动** profile 传给**服务** profile"。
>
> 而且**它不会报错**——子进程会拿到一份**完整、合法、但是错的**环境。

#### ④ `process_registry` 那一大段：逐句拆

原文那段话密度很高，值得一句一句对：

| 原文（英） | 中文 | 它在防什么 |
|---|---|---|
| `process_registry_checkpoint.py` owns running-process checkpoints and PID-safe adoption | `checkpoint` 模块管**运行中进程**的检查点和**PID 安全接管** | 重启后认领"上次那个 PID 还是我要的那个吗" |
| Completed output is separate: `process_registry_results.py` writes one **atomic, redacted receipt** per process under the profile's `logs/process-results/` | **已完成**的输出分开管：每个进程一份**原子、脱敏**的"回执" | 把"进程活着"和"进程说完了什么"解耦 |
| Producers cannot overwrite another parent's results by rewriting the shared PID checkpoint | 生产者**不能**通过改共享的 PID 检查点去覆盖**另一个父**的结果 | 两个父 agent 都起了进程时，A 不能借改共享文件抹掉 B 的输出 |
| The registry persists the receipt **before** releasing its completion event | 注册表**先落盘回执、再**释放完成事件 | 顺序反了 → 等事件的人醒来时可能读不到回执 |
| one-shot linger waits on that event | 一次性 linger 等的是那个事件 | —— |
| Query methods load retained snapshots **without adopting PIDs or enqueuing notifications** | 查询方法只读快照，**不接管 PID、不入队通知** | **"看一眼"不该有副作用**——否则一次查询会意外启动接管流程 |
| Reads require the **commissioning durable session** or its compression continuation | 读要求是**下单的那个持久会话**（或它的压缩续体） | **只拿着 handle 不足以授权**读一个留存结果 |
| The registry captures that owner **before starting any output reader** | 注册表在启动任何输出读取器**之前**就记下所有者 | 读取器线程起来之后才知道 owner → 中间有个窗口 |
| preserves the producer's profile context in reader threads | 在读取器线程里保留**生产者**的 profile 上下文 | 又一次 profile 作用域（第 3 次在原文里出现） |
| Receipt redaction is **forced independently of live-output opt-out** | 回执脱敏是**强制的**，与"实时输出关不关"无关 | 有人为了隐私关掉实时输出，**不能因此让落盘的回执不脱敏** |
| retention is bounded **by age and count** | 留存有**时间和数量**两个界 | 只按时间 → 高产进程会堆爆磁盘；只按数量 → 老的敏感数据留着 |

> 📌 **这一段的每一句都是一个具体的失效模式。**
> 读它的正确姿势不是"记住这些规则"，而是**注意它一条都没说"要注意安全"**——
> 它说的全是"**谁在什么时刻能读到什么**"。
>
> 这一段的价值在于它是一个**范例**：怎么把"并发 + 多租户 + 持久化"这三件事
> 写成一串**可验证的顺序约束**（先 X 后 Y、读不触发写、owner 在启动读取器前就钉住）。

### 【动手】mini 版现状

**mini 版只有 local。** `tools/terminal.py`（78 行）直接 `subprocess.run`。
这一章**不打算实现第二个后端**——理由：

> 加一个后端（比如 docker）在 mini 版里是**几行 subprocess 参数**的事，
> 但那样学不到任何东西：真正的复杂度在 **§7④ 那一堆顺序约束**里，
> 而那需要真的有"多个父 agent 同时起进程 + 持久化 + 重启恢复"的场景。
>
> **mini 现阶段该做的是把"后端"这个概念的位置留出来**（一个表 / 一个构建器），
> 而不是塞第二个实现。这和 `AGENTS.md` 的形状规则一致：
> **加后端应该是"在表里加一条"，所以先把表做出来。**

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

按"值不值得改"排：

| # | 改什么 | 为什么值得 | 工作量 |
|---|---|---|---|
| ① | **手写名单 → AST 自动发现** | 本章核心。手写名单的失效模式是**静默少一个工具** | 中 |
| ② | **加 `check_fn` + 发 schema 前过滤** | 让"没配好就不发给模型"这件事在 mini 里也能演示 | 小 |
| ③ | **加 toolset 解析（含"注册≠暴露"）** | mini 已经有 `toolset` 字段但没人用——把语义补上 | 中 |
| ④ | **错误返回改 JSON 字符串 + 截断** | 契约问题。纯文本错误模型解析不了 | 小 |
| ⑤ | **加 `_AGENT_LOOP_TOOLS` 守卫** | 让"这 4 个不该被独立调用"变成结构保证 | 小 |
| ⑥ | **加 `_LEGACY_TOOL_ALIASES`** | 演示"对外收窄、对内兼容" | 极小 |
| ⑦ | **危险检测挪到一处** | mini 现在是"工具自己带检测"，没有单一事实来源 | 小 |
| ⑧ | 只留 local 后端，但**把后端表的位置留出来** | 见 §7 的"动手" | 小 |
| ❌ | async 桥接 | mini 没有 async 工具 —— **记录"为什么可以不做"** | — |
| ❌ | 107 条危险模式 | 把检测挪到一处就够，**抄数量没有教学价值** | — |
| ❌ | process_registry 那一整套 | 需要"多父 + 持久化 + 重启"的真实场景 | — |

---

## 附录 A · mini 版代码结构对照（本章相关）

| mini 版文件 | 状态 | 对应 Hermes |
|---|---|---|
| `tools/registry.py` | ⬜ 待改（加 `check_fn`/`toolset` 语义/JSON 错误） | `tools/registry.py`（977 行） |
| `model_tools.py` | ⬜ 待改（手写名单 → AST 发现；加别名映射；发 schema 前过滤） | `model_tools.py`（987 行） |
| （新建）`toolsets.py` | ⬜ 待建 | `toolsets.py`（485 行，60 个 toolset / 59 个核心工具） |
| （新建）`tools/approval.py` | ⬜ 待建（把危险检测从 `terminal.py` 挪出来） | `tools/approval.py` + 7 个兄弟 = 8 文件 |
| `tools/terminal.py` | ✅ 已有（78 行，只有 local） | `tools/terminal_tool.py` + `terminal_tool_backends.py` |
| `tools/file_tools.py` | ✅ 已有（92 行） | `tools/file_tools*.py` |
| `tools/todo.py` | ✅ 已有（267 行，含 `AGENT_LEVEL_TOOLS`） | `tools/todo_tool.py` + `agent/inline_tool_executors.py` |
| `run_agent.py` 的 `INLINE_TOOL_EXECUTORS` | ✅ 已有（第 2 章） | `agent/inline_tool_executors.py`（14 个 key） |

规模对比（**第 3 章末 → 第 4 章末**，动手前先记着起点）：

```text
                          第 3 章末
tools/registry.py            105 行
model_tools.py                66 行
tools/terminal.py             78 行
tools/file_tools.py           92 行
tools/todo.py                267 行
（无 toolsets.py / approval.py）
```

**对照 Hermes 的规模**：`tools/` 下 258 个 `.py`，其中 43 个是工具模块。
mini 有 4 个工具模块。**这个比例（0.1 倍不到）正是"手写名单还能活"的原因**——
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
