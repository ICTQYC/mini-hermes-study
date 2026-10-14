# 代码解读 · mini-hermes

> 配套《中文精译-02-AgentLoop.md》。那篇讲**原理**（Hermes 为什么这么设计），这篇讲**代码**（我们怎么写出来的、每一步为什么这么写）。
>
> 代码在 `mini-hermes/`，共 3174 行，零第三方依赖。
> 每个引用都带 `文件:行号`，可以直接跳过去。
>
> ⚠️ **本文描述的是「第 2 章末」的代码状态**，行号对应的就是那个版本。
> 第 3 章实现之后行号已经漂了（`run_agent.py` 744 → 906 行），所以**别拿这些行号去对当前代码**。
> 要精确核对，把仓库切到那个标签即可：
>
> ```bash
> git checkout db6229c              # 本文描述的状态（3,174 行；`ch02-agent-loop` 标签**最初**指的提交）
> git checkout ch03-prompt-assembly # 第 3 章末的状态
> ```
>
> **为什么锚到提交而不是标签**：行号是"会漂的引用"，每次改代码都得重算一遍，
> 而且算错了看不出来。★ 标签也**不是**不动的 —— 它的语义是"那一章末的完整状态"，该章的文档后来被改
> （或跨章回改牵连到它）时标签会**前移**（见 `AGENTS.md` 的版本管理条）。所以本文锚**提交**：
> 提交不可变，标签只是"当时的名字"。第 3 章的代码解读见《源码讲解-03-PromptAssembly.md》，同样标注了对应标签。
[TOC]

---

## 〇、先说怎么读（这一步比什么都重要）

**别按文件名字母序读，也别从 `run_agent.py` 开始读。**

`run_agent.py` 是中枢，它 import 了 6 个自己的模块。一上来读它，你会不断遇到
"这个 `registry.schemas()` 是什么""`build_system_prompt()` 干嘛的"——
每遇到一个就跳走一次，最后什么都没读懂。**这是读代码最常见的失败方式。**

**正确的顺序是按依赖链，从下往上：**

```text
① hermes_constants.py      （27 行，零依赖）
② tools/registry.py        （105 行，零依赖 ← 只被 import，不 import 谁）
③ tools/terminal.py        （78 行）
④ tools/file_tools.py      （92 行）
⑤ tools/todo.py           （267 行）
⑥ model_tools.py           （66 行）
⑦ agent/iteration_budget.py（38 行）
⑧ agent/prompt_builder.py  （41 行）
⑨ run_agent.py            （744 行）★ 中枢
⑩ cli.py                  （196 行）
⑪ hermes_state.py / hermes_cli/*.py
```

**为什么这个顺序有效**：每一层只依赖它**上面**（已读过的）层。
读到任何一行，它调用的东西你都已经见过了——**不会出现"这个函数从哪来的"的悬案**。

这和 Hermes 自己的依赖链是完全一致的：

```text
tools/registry.py ← tools/*.py ← model_tools.py ← run_agent.py ← cli.py
   （无依赖）       （自注册）    （分发中介）      （中枢）      （壳）
```

> 💡 **这不是巧合，是分层架构的红利。** 一个设计得好的项目，依赖图是一张有向无环图，
> 所以**存在一个自下而上的阅读顺序**。反过来，如果两个模块互相 import，
> 读代码就会变成"鸡生蛋"，这本身就是设计出问题的信号。

前 8 个小文件加起来只有 700 行，40 分钟能读完。而且它们会告诉你**中枢手里有哪些牌**。
真到 `run_agent.py` 时，你已经认识它用到的每一个工具了。

---

## 一、`hermes_constants.py`（27 行）—— 路径只在一处解析

**职责**：所有路径从这一个文件出去。

```python
def get_mini_home() -> Path:
    env = os.environ.get("MINI_HOME")
    home = Path(env).expanduser() if env else Path.home() / ".mini-hermes"
    home.mkdir(parents=True, exist_ok=True)
    return home
```

**为什么必须有这么个文件**：Hermes 的 `AGENTS.md` 里专门立了一条规矩
（*Never hardcode `~/.hermes`*），因为曾经有 **5 个 bug (PR #3575)** 都是硬编码路径造成的——
每个 profile 走的路径不同，硬编码的地方就读错了别人的数据。

对我们的 mini 版，它还有第二个作用：**测试可以用 `MINI_HOME` 环境变量把状态重定向到临时目录**，
不会污染你真实的家目录。

> 💡 27 行、只做一件事，看着小题大做。但它是"**以后要改就只改这里**"的兑现方式。

---

## 二、`tools/registry.py`（105 行）—— 整个项目的基石

**这是最该慢慢读的一个文件。** 整个 Hermes 的扩展能力都长在这个模式上。

### 它是什么

一张表 + 一个装饰器：

```python
_REGISTRY: Dict[str, Dict[str, Any]] = {}

def register_tool(name, *, description, parameters, toolset="core",
                  interactive=False, requires_approval=False, danger_check=None):
    def decorator(fn):
        with _LOCK:
            _REGISTRY[name] = {
                "name": name, "fn": fn, "toolset": toolset,
                "interactive": interactive, "requires_approval": requires_approval,
                "danger_check": danger_check,
                "schema": {"type": "function", "function": {
                    "name": name, "description": description, "parameters": parameters}},
            }
        return fn
    return decorator
```

### 三个关键设计

**① 它谁也不 import。** `registry.py` 的第一行 import 是 `threading`——标准库。
它是依赖链的**根**，所以谁都能 import 它，它不会绕回来。

**② 注册发生在 import 时。** `tools/terminal.py` 里那个 `@register_tool(...)` 装饰器，
一旦这个模块被 import 就执行。于是"有哪些工具"这件事**不需要维护任何名单**——
这是 Hermes 的 `tools/*.py` 自动发现的机制。（`tools/AGENTS.md`：
*Any `tools/*.py` with a top-level `registry.register()` is imported automatically — no manual import list.*）

**③ 表里存的是"元数据 + 函数"，不是"函数怎么实现"。** 所以：
- `schemas()`（第 74 行）能从表里**生成**发给模型的 json schema
- `is_interactive()`（第 80 行）能让中枢知道"这个工具要不要打断并发"
- `looks_approval_needed()`（第 90 行）能问工具"这次调用危不危险"

### 一个值得注意的细节：危险判断的归属

```python
def looks_approval_needed(name: str, args: Dict[str, Any]) -> bool:
    entry = _REGISTRY.get(name)
    if not entry or not entry["requires_approval"]:
        return False
    checker = entry.get("danger_check")
    if checker is None:
        return True          # 声明了要审批但没给判断函数 → 一律问
    try:
        return bool(checker(args or {}))
    except Exception:
        return True          # 判断函数自己炸了 → 保守起见走审批
```

注意 `danger_check` 是**工具自己传进来的函数**（`terminal.py` 传的是 `looks_dangerous`）。
注册表不判断"什么算危险"——**它只负责问**。

**这就是"核心决定何时、壳决定怎么"的又一个实例**（见精译文档第 7 节）：
危险模式的清单属于 terminal 工具（它才知道自己怕什么），
"要不要弹窗"的决策属于中枢，注册表只是传递者。

另外注意两个 `return True`：**判断不出来的时候选择"问用户"，不是"放行"**。
安全相关的默认值永远选保守的那边。

---

## 三、`tools/terminal.py`（78 行）—— 最危险也最有用的一个

```python
_DANGEROUS_PATTERNS = [
    r"\brm\s+-[a-z]*r[a-z]*f", r"\bsudo\b", r"\bmkfs\b", r"\bdd\s+if=",
    r">\s*/dev/[sh]d[a-z]", r"\bchmod\s+777\b",
    r"curl[^|]*\|\s*(ba)?sh", r"\bshutdown\b|\breboot\b",
]
```

**为什么是"模式匹配"而不是"白名单命令"**：白名单会让 agent 几乎什么都干不了
（你没法列举所有安全的命令）。黑名单会漏（模式可以绕过）。
Hermes 的选择是**黑名单 + 弹窗让用户兜底**——记住这条：
**危险检测的目标不是"挡住所有危险"，而是"让危险的那一档慢下来，经过人"**。

```python
def terminal_tool(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    try:
        proc = subprocess.run(command, shell=True, capture_output=True,
                              text=True, timeout=timeout, cwd=os.getcwd())
    except subprocess.TimeoutExpired:
        return f"[error] 命令超时（>{timeout}s）已被终止：{command}"
```

两个细节：

**① 必须有超时。** 否则模型跑一条 `tail -f` 或 `ping`，整个 agent 就永久卡住了。
`subprocess.run(timeout=)` 会杀掉子进程——但注意它杀的是 `shell=True` 那个 shell，
**孙进程可能留下**（Hermes 用 6 种后端 + 专门的进程管理来解决这个，我们这章先不管）。

**② 返回值是给模型看的文本，不是异常。** 出错了也 `return`，不 `raise`。
因为**模型看到错误信息能自己改，看到 traceback 只会浪费上下文**。
（`model_tools.handle_function_call` 里还有一层兜底 try/except，见下。）

---

## 四、`tools/file_tools.py`（92 行）—— 模型碰文件的唯一途径

三个工具：`read_file` / `write_file` / `list_dir`。

`read_file` 有个分页（`MAX_READ_LINES = 400`）和续读提示：

```python
tail = f"\n... 还有 {total - end} 行，用 offset={end + 1} 继续读" if end < total else ""
```

> 📌 **这里要对照 Hermes 的一条反直觉规矩**：`tools/AGENTS.md` 里写着
> *No `offset`/`limit` on instructional tools (skills, prompts, playbooks) —
> models read page 1 and skip the rest*。
>
> 也就是说：**分页只给"数据"，不给"必须读完的指令"**。
> `read_file` 读的是数据，可以分页；但加载技能、规则文件那种"你必须整篇读完"的工具，
> 一旦给了分页，模型就会只读第一页然后跳过剩下全部。
>
> **同一套 API 形状，用在两种内容上会得到相反的结果。** 这就是"工具设计"不只是接口设计的例子。

---

## 五、`tools/todo.py`（267 行）—— agent 的私有状态

这个文件最长，因为它的设计被三个真实问题逼出来（详见精译文档第 6 节）：
**放内存 → 但网关每条消息新建 agent → 所以要从历史重建 → 而这又开了个注入面。**

读的时候抓四条线：

| 行 | 是什么 | 为什么 |
|---|---|---|
| 26 `class TodoStore` | 一份清单 + `_revision` | 内存态，挂在 AIAgent 上 |
| 38 `write(todos, merge)` | 整表替换或按 id 合并，**返回全量** | 全量是为了能从历史重建 |
| 47 `if self._items != before: self._revision += 1` | **内容没变不推进版本** | 否则"重写一遍一样的"被当成真更新 |
| 88 `restore(todos, revision=)` | 恢复快照，**不制造新版本** | 制造了下次判新旧就错 |

**注意 `write` 和 `restore` 是一对反义操作**：

- `write` = **模型在改清单** → 内容变了就要 `revision += 1`
- `restore` = **把历史里的既成事实搬回来** → 版本号必须**照抄**

这两个方法长得像，语义相反。**如果 restore 也 +1，第二次恢复就会觉得"历史比内存旧"然后拒绝覆盖**——
这种 bug 要跑三次恢复才会显形。

还有一个"小"函数值得看：

```python
def _dedupe_by_id(todos):    # 第 118 行附近
    """同一个 id 出现多次时，**后面的赢** —— 模型一次给了两份更新，以最新的为准。"""
```

**为什么要有它**：模型可能在一次调用里给同一个 id 两条更新（写岔了）。
不处理的话，行为取决于遍历顺序——**不确定的行为比错误的确定行为更糟**。

---

## 六、`model_tools.py`（66 行）—— 分发中介

```python
_BUILTIN_MODULES = ["tools.terminal", "tools.file_tools"]

def discover_builtin_tools() -> List[str]:
    global _discovered
    if not _discovered:
        for mod in _BUILTIN_MODULES:
            importlib.import_module(mod)     # ← import 触发自注册
        _discovered = True
    return registry.tool_names()
```

**为什么要有这个文件**：`run_agent.py` 不该知道"有哪些工具、怎么找它们"。
它只问 `handle_function_call(name, args)`，剩下的是这一层的事。

（注意我们这里是**手写名单**，Hermes 是真·目录扫描。手写名单在只有 2 个模块时更好读，
但**忘加一行就会静默少一个工具**——这是我们要在别的章节改进的地方。）

`handle_function_call` 的异常兜底是这一层的核心价值：

```python
try:
    result = entry["fn"](**(args or {}))
except TypeError as exc:
    return f"[error] 参数不匹配：{exc}"
except Exception as exc:
    return f"[error] 工具执行异常：{type(exc).__name__}: {exc}"
```

**工具永远不抛异常，只返回文本。** 两个理由：

1. 进对话历史的东西**必须是文本**（消息格式约束）
2. **一个工具崩了不该让整个 turn 崩** —— 它是 agent 的一个动作，不是主流程

> ⚠️ 但我们这里有个**真实的缺口**（已实测验证）：`run_agent.py` 里的
> `INLINE_TOOL_EXECUTORS` 走的是另一条路，**没有这层兜底**：
>
> ```console
> -- 普通工具（经 handle_function_call）--
>   结果: [error] 参数不匹配：...
> -- agent 级工具（经 INLINE_TOOL_EXECUTORS）--
>   ✗ 异常冒出来了: RuntimeError: 我炸了     ← 直接炸穿了 turn
> ```
>
> 同一个"工具抛异常"，两条分发路径一个兜住、一个炸穿。
> 这是"分发有两条路"带来的不一致——**值得你自己发现并补上**（见第 14 节练习 1）。

### 🎯 而这个缺口，Hermes 上游**真的踩过并且修过**

我原本以为这只是我们教学版的疏漏。结果去读 `agent/inline_tool_executors.py`，
它的模块文档字符串第一段就是：

> *Agent-level ("inline") tool executors shared by the sequential and concurrent tool paths.
> These tools need live `AIAgent` state (stores, callbacks, session DB) and therefore bypass
> the tool registry. Each executor is `fn(agent, args, ctx) -> result`;
> **the table replaces two hand-maintained if/elif chains (`invoke_tool` and
> `execute_tool_calls_sequential`) that had drifted apart.***

翻译一下最后半句：**"这张表取代了两条手写的 if/elif 链条，
它们（本来是同一套逻辑）已经各自漂移、分家了。"**

对照一下我们的代码：

| | 顺序路径 | 并发路径 | 结果 |
|---|---|---|---|
| **我们** | `_run_one_tool`（两份代码共用，但内部仍有 registry / inline 两条路） | 同 | 异常处理不一致（就是上面那个缺口） |
| **Hermes 曾经如此** | `invoke_tool` 的 if/elif | `execute_tool_calls_sequential` 的 if/elif | **"had drifted apart"** |
| **Hermes 现在** | 同一张 `INLINE_TOOL_EXECUTORS` 表 | 同 | 结构上不可能再漂 |

> 🎓 **这就是"用表代替 if/elif 阶梯"这条形状规矩的真实来源。**
> 它不是审美洁癖，是**修一个真实 bug 的产物**：
> 两条路径各维护一份名单，改了其中一份、忘了另一份，就会出现
> "顺序执行时工具能跑、并发执行时工具找不到"这种只在特定条件下显形的故障。
>
> **把它抽成一张表之后，"两边不一致"从"需要小心维护"变成了"结构上不可能发生"。**
>
> 这是本章最值得记住的一课：**好的代码形状是 bug 的纪念碑。**
> 你看到一条奇怪的规矩（"不许写 4 个以上分支的 if/elif"），
> 背后通常有一起事故。

顺带说一句：真实 Hermes 的表格 key 是 `session_search / memory / desktop_preview /
manage_connections / setup_mcp_shim` 这些（`agent/inline_tool_executors.py:175`），
我们的表里只有 `todo_list` —— **同一个模式，规模不同**。
而且注意它的 executor 签名是 `fn(agent, args, ctx)`，比我们多一个 `ctx`
（装着 task_id、工具钩子 id 之类）——因为它的钩子体系更复杂。

### agent 级工具的全貌：三张表、两条路 ★

这一节是"读懂机制"和"只背下结论"的分界线。前面讲了**为什么**要有拦截，
这里把**实现**摊开——它一共只有三张表和两条路。

#### 先澄清一个中文歧义：「拦截」不是「挡住」

> 英文原词是 **intercepted**（半路截住），**不是** blocked / denied（挡住、拒绝）。
>
> 如果读成"被拦下了、没执行"，直觉会打架：*"被拦截了怎么还能用？"*
>
> 真实含义是：**这个调用不往通用分发器送，半路截住，由 `run_agent.py` 自己处理完。**
> 它照样执行、照样把结果回填进历史——只是执行者换了一个。

#### 三张表

**表 ① `_REGISTRY`（`tools/registry.py:17`）——普通工具，一张表装完**

```python
_REGISTRY["terminal"] = {
    "name": "terminal",
    "fn": terminal_tool,                    # ← 执行函数
    "schema": {...},                        # ← 给模型的描述
    "toolset": "core",
    "interactive": False,                   # ← 元数据
    "requires_approval": True,
    "danger_check": looks_dangerous,
}
```

**schema 和执行函数在同一个条目里。一个工具 = 表里一项，完整自足。**

**表 ②③ —— agent 级工具被迫拆成两张**

```python
# 表②  tools/todo.py 末尾 —— 只有 schema
AGENT_LEVEL_TOOLS = {"todo_list": TODO_LIST_SCHEMA}

# 表③  run_agent.py:131 —— 只有执行函数
def _execute_todo_list(agent: "AIAgent", args: Dict[str, Any]) -> str:
    agent.todo.write(args.get("todos"), bool(args.get("merge", False)))
    return agent.todo.to_payload()

INLINE_TOOL_EXECUTORS: Dict[str, Callable[["AIAgent", Dict[str, Any]], str]] = {
    "todo_list": _execute_todo_list,
}
```

> **最该记住的一句话：普通工具"一张表装完"，agent 级工具"被迫拆成两张"。**

**拆分不是设计偏好，是被逼的：**

- **schema 是"工具自身的定义"** → 自然跟工具实现放一起（`tools/todo.py`）
- **执行函数必须靠近 agent 实例** → 只能放中枢（`run_agent.py`）
- **它进不了 `_REGISTRY`**，因为注册表那条路的调用形式是 `fn(**args)`，**给不了 `self`**

**关键差别就在函数签名上：**

```python
entry["fn"](**(args or {}))              # 注册表：只有 args
INLINE_TOOL_EXECUTORS[name](self, args)  # 拦截表：有 self  ← 全部区别
```

拆成两处的**唯一代价**是"必须同步改两边"——所以有一条契约测试盯着
（`test_inline_executor_contract`，断言两个字典的 key 集合完全相等）。

#### 两条路的分派（真实代码）

```python
# run_agent.py:605-628
def _run_one_tool(self, p: Dict[str, Any], task_id: str) -> str:
    name, args = p["name"], p["args"]
    ...
    if "parse_error" in p:
        content = f"[error] {p['parse_error']}"
    elif name in INLINE_TOOL_EXECUTORS:                          # 613 ← ★ 拦截点
        content = INLINE_TOOL_EXECUTORS[name](self, args)        # 615
    else:
        if model_tools.registry.looks_approval_needed(name, args):   # 审批只对普通工具
            ...
        content = model_tools.handle_function_call(name, args, task_id=task_id)   # 624
```

```text
【发请求前】
  model_tools.get_tool_schemas()
      registry.schemas()            → 4 个普通工具 schema
      + AGENT_LEVEL_TOOLS.values()  → 1 个 agent 级 schema
      ────────────────────────────
      5 个 schema ──→ 塞进请求的 "tools" 字段（模型看不出区别）

【模型返回 tool_calls】
  _execute_tool_calls()  →  _run_one_tool()  逐个处理
        │
        ├─ 名字在执行表里？
        │     是 → INLINE_TOOL_EXECUTORS[name](self, args)   ← 状态写进 self.todo
        │     否 → handle_function_call(name, args)
        │             → registry.get(name) → entry["fn"](**args)
        │
        └─ 结果 append 成 {"role": "tool", ...}   ← 两条路在这里又汇合
```

**注意最后一步：两条路的结果在"回填历史"处又汇合了。** 模型收到的是一条普通的
tool 消息，它无从分辨这条结果是怎么产生的。**分岔只在中枢内部，边界上完全一致**——
这是这个设计能成立的关键。

#### 全景对照

| | 普通工具 | agent 级工具 |
|---|---|---|
| **schema 在哪** | `_REGISTRY` 里（和执行函数同表） | `AGENT_LEVEL_TOOLS`（表②） |
| **执行函数在哪** | 同一张 `_REGISTRY` | `INLINE_TOOL_EXECUTORS`（表③） |
| **函数签名** | `fn(**args)` | `fn(agent, args)` ← 有 `self` |
| **谁执行** | `model_tools.handle_function_call` | `run_agent._run_one_tool` 自己 |
| **走审批吗** | 可以（`requires_approval` + `danger_check`） | 不走（改的是 agent 自己，不是外部世界） |
| **能标记 interactive 吗** | 能 | **不能** ← 见下面的缺口 |
| **并发还是串行** | **外层同一套规则** | **外层同一套规则**（两类一起排队） |
| **返回值** | 字符串 | 字符串（而且必须是 JSON，为了能从历史重建） |

#### 探针实测：两条路真的各走各的

在两条路上各装一个探针（观察真实调用，不是复制判断逻辑），跑一次真实任务：

```console
★ 拦截表 INLINE_TOOL_EXECUTORS 被调用：todo_list
    → 通用分发 handle_function_call 被调用：list_dir
    → 通用分发 handle_function_call 被调用：terminal
    ★ 拦截表 INLINE_TOOL_EXECUTORS 被调用：todo_list
```

`todo_list` 走拦截表，`list_dir` / `terminal` 走通用分发——**同一个 agent，同一轮对话，两条路并存。**

#### 「清单非空就说明拦截生效了」——补上缺的那条推理链

这句话本身**不构成证明**，它只说明"有东西执行了"。完整推理是这样：

**第一步：模型改不了 Python 对象。** 它能做的只是输出一段文本：

```json
{"name": "todo_list", "arguments": "{\"todos\":[{\"id\":\"1\",\"content\":\"查看...\",\"status\":\"completed\"}]}"}
```

**从"这段 JSON 文本"到"Python 对象里有内容"，中间必须有代码去执行它。** 那代码在哪？grep 全项目：

```console
=== 谁能写入 agent.todo？===
run_agent.py:127    agent.todo.write(...)      ← 在 _execute_todo_list 里（来自模型）
run_agent.py:663    self.todo.restore(...)     ← 在 _hydrate_todo_store 里（来自历史重建）

=== _execute_todo_list 只在哪被引用？===
run_agent.py:132    "todo_list": _execute_todo_list,    ← 就是 INLINE_TOOL_EXECUTORS 那个字典值
```

**整个项目只有两处能改这个清单**，而 `restore()` 那条恢复的内容**本身也只能来自
历史里一条 todo 工具结果**——追根到底还是得 `write()` 先跑过。而：

```text
① 清单有内容了                          ← 观察
② ⟹ 一定是 write() 跑过                ← 只有两个写入者，另一个也只能追溯到 write
③ ⟹ 一定是 _execute_todo_list 跑过     ← 因为 write() 只在那里被调用
④ ⟹ 一定是 INLINE_TOOL_EXECUTORS 被用过 ← 那个函数只存在于这张表里
⑤ ⟹ 拦截生效了                          ← 取这张表的唯一地方就是 _run_one_tool:613
```

> 💡 **核心那句话**：**模型改不了 Python 对象。它只会"发出请求"；必须有代码去"接"。**
> 清单有内容 = 有人接了 = 接它的只能是拦截表。

#### ⚠️ 一个必须修正的直觉：并发**不是**"普通工具的属性"

很容易推出一个错的结论："普通工具可以并发，agent 级工具走拦截所以是串行的。"
**不对。** 这是两个**正交**的层：

```text
_execute_tool_calls()            ← 外层：决定"这一批怎么跑"
    force_sequential = 只有 1 个调用？ 或 有 interactive 工具？
    ├─ 是 → 挨个跑
    └─ 否 → 线程池并发跑
             │
             └─ 对每一个调用 → _run_one_tool()   ← 内层：决定"这个谁执行"
                    名字在拦截表里？ 是→拦截表  否→通用分发
```

实测：一轮里放 **2 个普通工具 + 1 个 agent 级工具**：

```console
_t_slow      走【普通工具】  线程 ThreadPoolExecutor-0_0
todo_list    走【拦截表】    线程 ThreadPoolExecutor-0_1
_t_fast      走【普通工具】  线程 ThreadPoolExecutor-0_1

→ 用了 2 个不同线程 ⇒ 3 个调用确实并发跑了，其中 todo_list 走的是拦截表
```

**"串行还是并发"是整批一起决定的**（看这一轮有几个调用、有没有 interactive 工具），
agent 级工具也一样排在这批里——它没有"只串行"的待遇。

> 🕳️ **顺带暴露一个真实缺口**：`INLINE_TOOL_EXECUTORS` **根本没有 `interactive` 字段**，
> 所以 `registry.is_interactive("todo_list")` 永远返回 `False`——agent 级工具**无法要求串行**。
>
> 现在没事（`todo_list` 不需要问用户）。但哪天要加一个"需要问用户"的 agent 级工具
> （比如 `clarify`），它就没办法触发"整批退化成串行"了——**而那个契约是必须的**
> （见 `demos/why_interactive_is_serial.py` 的三个故障）。
> 补法是给执行表加元数据，或者让拦截表读一个共享的标记位。

#### 加一个新的 agent 级工具 = 三步

```python
# ① tools/xxx.py：写 schema（跟普通工具一样）
XXX_SCHEMA = {"type": "function", "function": {"name": "xxx", "description": "...", "parameters": {...}}}
AGENT_LEVEL_TOOLS = {"todo_list": TODO_LIST_SCHEMA, "xxx": XXX_SCHEMA}      # ← 加这一行

# ② run_agent.py：写执行函数（签名必须带 agent）
def _execute_xxx(agent, args) -> str:
    agent.something.change(...)
    return json.dumps(...)

INLINE_TOOL_EXECUTORS = {"todo_list": _execute_todo_list, "xxx": _execute_xxx}  # ← 加这一行

# ③ 跑契约测试 —— 它替你检查两边都加齐了
python3 tests/test_loop_mechanisms.py
```

**全套改动就是两个字典里各加一行。** 没有注册表改动、没有分派逻辑改动、
没有 `if/elif` 加长——**这就是"用表代替分支"换来的东西。**

---

## 七、`agent/iteration_budget.py`（38 行）—— 唯一一个几乎照抄的文件

```python
def consume(self) -> bool:
    with self._lock:
        if self._used >= self.max_total:
            return False
        self._used += 1
        return True
```

**为什么必须加锁**：并发工具 / 子 agent 会同时问"我还能再来一次吗"。
不加锁，两个线程可能都读到 `used == max_total - 1` 然后双双通过。

**为什么耗尽时返回 False 而不是抛异常**：见 `run_agent.py` 第 195 行——
拿到 False 的一方要**返回一份"已完成工作"的总结**。一个跑了 500 轮的 agent
抛个 `BudgetExceededError` 然后什么都不留，等于 500 轮的算力扔了。

---

## 八、`agent/prompt_builder.py`（41 行）—— 故意很简陋

第 3 章（`prompt-assembly.md`）会把它扩展成"记忆 + 技能 + 项目上下文 + 人格"。
现在它只做两件事：身份描述 + 当前工作目录。

**但有一件事现在就必须做对，注释里写了：**

```python
def build_system_prompt(cwd=None, tools_hint=None):
    """注意：这里**故意不塞时间戳**。...
    每轮重新拼、往里塞"当前时间"这类东西 = 每次都打断 prompt 缓存 = 用户账单翻倍。"""
```

**这是整个项目里最容易犯、代价最大、又最难发现的一个错误。**
（Hermes 的 `AGENTS.md` 把它列为两大不变量之一：*Per-conversation prompt caching is sacred.*）

---

## 九、`run_agent.py`（744 行）—— 中枢 ★

**现在你认识的每一个名字都用得上了。**

### 9.1 读的顺序

不要从第 1 行读。按这个顺序：

```text
① 42-137   常量 + 两段超时连接类 + INLINE_TOOL_EXECUTORS（agent 级工具的执行表）
② 140-200  class AIAgent.__init__（它手里握着什么）
③ 202-266  run_conversation()（9 步生命周期的 1~3.5）
④ 268-316  _run_conversation_turn()  ★★★ 这就是 loop 本体
⑤ 318-414  消息层（5 个小方法）
⑥ 415-534  可中断 API 调用 + 连接层（4 个方法）
⑦ 536-632  工具执行（2 个方法）
⑧ 634-733  状态重建（5 个方法，含 _log_exception）
⑨ 735-744  _preflight_compression（空函数，第 5 章填）
```

**④ 只有 45 行，是全局最该读透的 45 行。** 它的编号注释和文档第 4 节的 9 步一一对应。

> 📌 行号会漂（这个文件被别人改过好几轮），**别把它当坐标用**——按方法名 `grep -n "def _run_conversation_turn" run_agent.py` 定位更可靠。

### 9.2 loop 本体（每行都值得看）

```python
def _run_conversation_turn(self, messages, task_id):          # 268
    while True:
        if not self.budget.consume():                          # 271  ← 出口之一
            return {"final_response": f"（迭代预算...已用尽...）", "budget_exhausted": True}   # 272

        self._preflight_compression(messages)                  # 278  4.
        api_messages = self._build_api_messages(messages)      # 281  5.
        api_messages = self._inject_ephemeral_layers(api_messages)  # 284  6.
        self._apply_prompt_caching(api_messages)               # 287  7.

        response = self._interruptible_api_call(api_messages, task_id)  # 293  8.

        assistant_msg, tool_calls = self._parse_response(response)      # 299  9.
        messages.append(assistant_msg)                         # 300

        if tool_calls:                                         # 302
            self._execute_tool_calls(assistant_msg, tool_calls, messages, task_id)  # 304
            continue                                           # 307  ★ 回到第 5 步 = 环
        return {"final_response": text, "iterations": ...}     # 312  ← 出口之二
```

**三个"出口"**：预算耗尽、有文本响应、异常中断。**只要模型一直调工具，这个 `while` 就永远转**——
所以那个 `budget.consume()` 不是可选项。**没有预算的 agent loop 就是一台没法关的机器。**

**注意第 5 步在环里、第 3 步不在。** 每次迭代都重新构建 API 消息（因为历史变长了），
但系统提示词只在 loop 外构建一次（因为**它必须字节稳定**）。

### 9.3 可中断调用（第 415 行）—— 最容易写错的一段

```python
def _interruptible_api_call(self, api_messages, task_id):
    result_q: queue.Queue = queue.Queue(maxsize=1)

    def worker():
        try:
            result_q.put(self._post_chat_completions(api_messages, task_id))
        except BaseException as exc:
            result_q.put(exc)                    # ← 异常也要交回主线程

    thread = threading.Thread(target=worker, daemon=True, name="mini-api-call")
    thread.start()
    try:
        while True:
            if self._interrupt.is_set():
                raise AgentInterrupted()
            try:
                result = result_q.get(timeout=0.1)   # ← 轮询！
                break
            except queue.Empty:
                continue
    except KeyboardInterrupt:
        self._interrupt.set()
        raise AgentInterrupted() from None
```

**三个容易写错的地方：**

1. **`get(timeout=0.1)` 而不是 `get()`。** 写成阻塞的 `get()`，就永远检查不到中断事件——
   你在等的两件事（结果、中断）变成了只等一件。**这是"可中断"能不能成立的分水岭。**
2. **worker 里 `except BaseException`。** 如果只捕获 `Exception`，worker 线程会静默死掉，
   主线程在 `queue.get` 里永远等一个永远不会到的结果 → **永久挂起**。
   把异常塞进队列交回主线程，是"线程里出错"的标准姿势。
3. **`daemon=True`。** 中断时我们**抛弃**这个线程（响应直接扔掉），
   不希望它在退出时把进程拖住。

### 9.4 工具执行（第 536 行）—— 两个坑都在这里

```python
force_sequential = len(parsed) <= 1 or any(
    model_tools.registry.is_interactive(p["name"]) for p in parsed
)

if force_sequential:
    results = [self._run_one_tool(p, task_id) for p in parsed]
else:
    with ThreadPoolExecutor(max_workers=min(8, len(parsed))) as pool:
        results = list(pool.map(lambda p: self._run_one_tool(p, task_id), parsed))
```

**坑 1**：`is_interactive` 一票否决整个批次。原因见 `demos/why_interactive_is_serial.py`
（三个可复现故障）。

**坑 2**：`pool.map` 而不是 `as_completed`。
`map` **保持入参顺序**，`as_completed` 按完成顺序返回。
用错了，回填进历史的结果顺序就和模型发出的顺序不一致——
模型会**把 B 的结果当成 A 的**，然后基于错误前提继续推理，而且自信满满。

（测试里专门断言了这件事：先发起的 A 慢 0.3s、后发起的 B 快 0.05s，
*完成顺序 = [B, A]* 且 *回填顺序 = [A的结果, B的结果]*，两条必须同时成立。）

### 9.5 状态重建（第 645 行）

```python
def refresh_state(self, history):      # 公开：谁都能要求重建
    self._hydrate_todo_store(history)

def _hydrate_todo_store(self, history):
    found = self._latest_todo_response(history)
    if found is None:
        return
    todos, revision = found
    if revision > self.todo.revision:               # ← 单向：只接受更新的
        self.todo.restore(todos, revision=revision)
```

`_latest_todo_response` 用**单次前向扫描**同时做两件事：

```python
for msg in history:
    if msg.get("role") == "assistant":
        for call in msg.get("tool_calls") or []:
            if fn.get("name") in AGENT_LEVEL_TOOLS:
                valid_ids.add(call.get("id") or "")      # 收集"模型真的发起过哪些调用"
    elif msg.get("role") == "tool":
        if msg.get("tool_call_id") in valid_ids:         # ← 配对校验（安全边界）
            latest = ...                                 # 后面的自然覆盖前面的
```

**这个写法比"从后往前找"更省心**：因为"调用在前、结果在后"这个时序要求
是**天然满足**的——`valid_ids` 只在遇到 assistant 消息时才增长，
所以一条 tool 消息能配对，必然意味着调用已经在它前面了。不用额外写检查。

> ⚠️ 这里就是那个**安全边界**（GHSA-5g4g-6jrg-mw3g）：
> 没有 `tool_call_id in valid_ids` 这一句，任何人往对话里塞一条裸的 `role: tool`
> 就能篡改 agent 的任务清单。

### 9.6 一个真实的 75 秒悬案：连接超时 ≠ 读取超时

**这一节不是"设计模式"，是一次真实的故障排查。** 起因是你跑 `./mini` 感觉"非常慢"。

**量出来的事实**（`demos/why_slow_connect_matters.py` 可复现）：

| 阶段 | 耗时 | 正常吗 |
|---|---|---|
| DNS 解析 | 4 毫秒 | ✓ |
| **TCP 连接** | **60 秒**（另一次 75 秒） | ✗ 正常是 20~50 毫秒 |
| TLS 握手 | 30 毫秒 | ✓ |
| 首字节 | 60.1 秒 | ✗ |

**根因**：`api.deepseek.com` 的 DNS 返回**两个** IP，其中一个从你的网络**完全连不通**：

```console
60.28.220.199     TCP 0.011s ✓  （4/4 成功）
123.125.246.121   TCP 超时   ✗  （4/4 失败）
```

系统默认的连接超时是 **~75 秒**。所以"DNS 挑到坏 IP"就等于整轮对话白等 75 秒。

**修法**：`socket.create_connection` 本来就会**依次尝试** `getaddrinfo` 返回的每个地址。
问题从来不是"没试第二个"，而是**"试第一个等太久了"**。给它一个短超时即可：

```console
connect timeout = 3s        →   3.02s 成功，落到好 IP     ✓
connect timeout = 系统默认   →  75.02s 成功，落到好 IP     ✗
```

**但这里有个陷阱**：不能直接给 `urllib.request.urlopen` 传个小 timeout——

```python
urllib.request.urlopen(req, timeout=5)     # ✗ 这样会掐断正常的长回答
```

因为 **urlopen 的 timeout 是一个值同时管连接和读取**。而这两件事的合理上限差两个数量级：

```text
┌── 连接阶段 ──┐┌──────── 读取阶段（模型在生成）────────┐
0    5s        5s                                    185s
坏地址只能占这么久 ↑                        ↑ 这里可以很久
```

- 给 5 秒 → 模型生成超过 5 秒的正常回答**会被掐断**
- 给 180 秒 → 坏地址**就白等 180 秒**

**必须分开。** 所以 `_http_post` 里绕过了 `urlopen`，自己搭一个两段超时的 HTTPS 连接：

```python
class _TwoTimeoutHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)  # 2s
        sock.settimeout(READ_TIMEOUT)                     # ← 连上之后立刻换成 180s
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
```

**效果（坏 IP 排头时，走我们真实的 `_http_post` 路径实测）**：

| 版本 | 一次请求耗时 |
|---|---|
| 修复前（系统默认 ~75s） | **75.02s** |
| `CONNECT_TIMEOUT = 5.0` | 6.56s |
| **`CONNECT_TIMEOUT = 2.0`（现状）** | **3.19s** ← 2s 等待 + ~1.1s 正常请求 |

**为什么敢降到 2s**：域内 API 的握手正常在 50ms 量级，2s 留了 40 倍余量；
真遇到更慢的链路，`create_connection` 会继续试下一个地址，**不会因此失败**——只是多试一次。
换句话说：**调小它只降最坏情况，不增加失败风险。**

> ⚠️ **一个必须说清的前提**：这个代价**不是每次调用都付**。
> `getaddrinfo` 返回的顺序在一段时间内是**固定**的，但**会变**：
> 我早先查是坏 IP 排头，过一会儿再连测 12 次全是好 IP 排头。
> 所以：
>
> | DNS 顺序 | 每次调用的代价 |
> |---|---|
> | 好 IP 排头 | 0.01s（不付代价） |
> | 坏 IP 排头 | 付满 connect 超时（我们 5s / Hermes 10s / 默认客户端 75s） |
>
> **这就是"有时快有时卡"的完整解释**——不是随机抖动，是 DNS 顺序固定一段时间，
> 那段时间里每次调用都卡。修复的价值是**把最坏情况从 75s 压到 5s**，不是消除它。

> 🎓 **这一节值得单独记的通用知识**：几乎所有生产级 HTTP 客户端
> （`requests` / `httpx` / `okhttp` / Go 的 `net/http`）都把
> **connect timeout 和 read timeout 设成两个独立参数**。
> 这不是 API 冗余，是血换来的。
>
> 顺带一条经验：**"慢"和"卡"要分开量。**
> 我们一开始猜"是模型生成慢"和"是没流式"，都被数据否掉了
> （流式首字节也是 75s → 说明卡在连接阶段，流式救不了）。
> 只有把 DNS / TCP / TLS / 首字节**分段计时**，才定位得到 60 秒那一段。
> `curl -w "%{time_connect} %{time_appconnect} %{time_starttransfer}"` 就是干这个的。

### 9.7 一个"没反应"的 bug：汇报出口只能有一处

**这也是从你的一次实际使用里挖出来的**：跑 `./mini -q "..."`，屏幕上只有转圈，
然后**什么都没有，退出码 0**。既没答案也没报错——纯粹"没反应"。

**根因**：`text_callback` 被调用的地方**有两处，而且只有一处被写了**：

```python
# ❌ 修改前
# loop 里（成功路径）
if self.text_callback and text:
    self.text_callback(text)          # ← 只有成功会汇报
...
# run_conversation 里（失败路径）
except Exception as exc:
    final_response = f"[agent 异常] {exc}"   # ← 算出来了，但没人打印它
```

而 `hermes_cli/main.py` 只在 `--quiet` 时才打印最终结果（非 quiet 模式依赖回调）。
于是**"turn 里抛异常" = 一个字不打印 + 退出码 0**。

**修法不是"在异常分支里也加一句 print"**——那只是把"两处"变成"三处"，下次还会漏。
正确做法是**收敛成一个出口**：

```python
# ✅ 修改后：run_conversation 是唯一的汇报出口
if self.text_callback:
    self.text_callback(final_response)

return {"final_response": final_response, ..., **result}
```

并且明确规矩：**成功、失败、被中断、超预算——四条路都从这里出去。**

配套还补了两个"失败必须可见"的缺口：

| 缺口 | 修法 |
|---|---|
| 退出码永远是 0，脚本分不清"答完了"和"炸了" | `error` → 退出 1；`interrupted` → 退出 130 |
| traceback 被丢掉了（"报了错但没法查"） | 写进 `$MINI_HOME/logs/errors.log`；`--debug` 直接看 |

实测修完之后：

```console
$ DEEPSEEK_BASE_URL=https://127.0.0.1:9/v1 ./mini -q "hi"
provider=deepseek model=deepseek-chat api_mode=chat_completions
[agent 异常] ConnectionRefusedError: [Errno 61] Connection refused     ← 现在看得见了
$ echo $?
1                                                                      ← 退出码也对了
# stderr: （完整 traceback 已写入 ~/.mini-hermes/logs/errors.log；加 --debug 可直接看到）
```

> 🎓 **这是本章第三次遇到同一类 bug 了**，值得单独记一句：
>
> | 场景 | 形态 |
> |---|---|
> | 工具分发 | 注册表 / agent 级工具**两条路**，异常处理不一致 |
> | 工具结果回填 | 顺序一条路对、一条路错 |
> | **结果汇报** | **成功路径汇报、失败路径不汇报 → 静默失败** |
>
> 共同点：**同一个逻辑被写在两处，然后漂移。**
> 所以"消除重复"在架构上不是审美洁癖，而是：
> **把"需要小心维护"变成"结构上不可能出错"。**
>
> 最后一条还额外暴露了一个原则：**静默失败比崩溃危险得多。**
> 崩溃至少告诉你"出事了"；静默失败让你以为"它在想"。

### 9.8 消息层里最"小"但最不能省的一个方法

```python
def _validate_alternation(self, messages):
    prev = None
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == prev and role in ("user", "assistant"):
            raise ValueError(f"消息 {i} 违反交替规则：连续两条 {role}")
        if role == "tool" and prev not in ("tool", "assistant"):
            raise ValueError(f"消息 {i}：tool 结果必须紧跟 assistant/tool，实际前一条是 {prev}")
        prev = role
```

**15 行，但它是"本地断言 vs 远端报错"的分界线。**

不写它：provider 返回 400 `invalid message sequence`，你在 agent 日志里只看到
"API 调用失败"，然后开始查网络、查 key、查模型名——**排查路径长得离谱**。
写了它：错误在**产生的那一行**被抓住，而且带消息下标。

---

## 十、`cli.py`（196 行）—— 壳

**读这个文件时数一下：里面有几行是在决定"agent 该做什么"？**

答案应该是 **0**。它全部在做：

| 行 | 干什么 |
|---|---|
| 49 `__init__` | 解析 provider、建 agent、把回调挂上去 |
| 78-114 `_on_text` / `_on_thinking` / `_on_reasoning` / `_on_tool_progress` / `_on_step` | **回调 → 打印** |
| 117 `_ask_approval` | 问一个 y/N |
| 133 `one_shot` | 调 agent + 存会话 |
| 141 `repl` | 读一行 → 分派 `/命令` 或交给 agent |

**回调那一组是"壳/脑分离"的字面体现**：核心不 `print`，它只**在正确时机喊一声**，
这一声变成终端上的彩色文字、Telegram 消息，还是 IDE 进度条，核心不关心。

⚠️ **但注意 `_on_thinking` 用 `\r` 重绘同一行**——这是故障 1 的根源
（`demos/why_interactive_is_serial.py` 跑出来的）。**壳自己的渲染手法会和生产代码打架。**

`_ask_approval` 里有一句值得看的：

```python
if threading.current_thread() is not threading.main_thread():
    print(f"[审批] 危险命令在并发线程里出现，无人可问 → 拒绝：{command}")
    return False
```

**"没人在"是一个环境事实，从壳上报给核心。** 对照真实 Hermes：
`cli.py:4449` 的 `-q` 模式也是同一个处理（*the approval gate takes the deterministic path*）。

---

## 十一、剩下三个小文件

- **`hermes_state.py`（46 行）**：JSON 存会话。**它必须被换掉**，理由不是"JSON 不够优雅"，
  而是三个具体需求：全文检索（FTS5）、并发写、会话 lineage。第 6 章换 SQLite 时你会看到为什么。
- **`hermes_cli/runtime_provider.py`（127 行）**：provider → `api_mode + base_url + api_key + model`。
  密钥发现链是三级（`MINI_ENV_FILE` → `$MINI_HOME/.env` → `~/.hermes/.env`），
  最后一级是"复用你已装好的 Hermes"。**别人机器上没有最后一级**——所以报错必须告诉他写哪。
- **`hermes_cli/main.py`（57 行）**：argparse 入口，`-q` 单次 / `--resume` / `--max-turns`。

---

## 十二、一条消息穿过全部代码（带行号）

以"帮我看看这个目录有多少 py 文件"为例，**模型需要调两个工具**：

```text
①  mini（启动脚本）
      cd 到项目根 → 挑解释器 → exec python3 -m hermes_cli.main
②  hermes_cli/main.py:20      argparse 解析
③  cli.py:49                  MiniCLI.__init__
                                → runtime_provider.resolve_provider()  解析 key/url/model
                                → AIAgent(spec, 回调们)                 run_agent.py:140
④  cli.py:148                 REPL 读到一行 → 分派（不是 /命令）→ one_shot()
⑤  cli.py:135                 agent.run_conversation(prompt, history)  (:141)
⑥  run_agent.py:202           run_conversation
       :210~211                 task_id = task_xxx
       :216                     messages.append({"role":"user", ...})
       :220                     build_system_prompt()   ← 只在这里拼一次
       :223~224                 refresh_state()         ← 从历史重建私有状态
⑦  run_agent.py:268           _run_conversation_turn ── 进入 while
       :271                     budget.consume()        ← 第 1 次迭代
       :281                     _build_api_messages → _validate_alternation（本地断言）
       :284                     _inject_ephemeral_layers（只加进副本，不进历史）
       :293                     _interruptible_api_call   (:415)
       :415                       worker 线程 → _post_chat_completions (:455) → _http_post (:483)
                                     · _TwoTimeoutHTTPSConnection：连接 2s / 读取 180s
                                     · 主线程 queue.get(timeout=0.1) 轮询
                                     · 结果回来了
       :299                     _parse_response → assistant_msg（带 2 个 tool_calls）
       :300                     messages.append(assistant_msg)
       :302                     if tool_calls:           ← 成立
       :304                       _execute_tool_calls    (:536)
                                     2 个工具、都不 interactive → 并发
                                     每个调用进 _run_one_tool (:605) 分派两条路
                                     按原始顺序回填 2 条 tool 消息
       :307                     continue ── 回到 :268 的 while 顶部
       :271                     budget.consume()        ← 第 2 次迭代
       :277~293                 同样的 4~8 步（历史比刚才长）
       :299                     这次没有 tool_calls → 文本
       :312                     return {"final_response": ...}
                                （⚠️ 这里故意不调 text_callback，见 9.7）
⑧  run_agent.py:252           run_conversation 统一汇报 → text_callback (:253)
⑨  cli.py:135                 one_shot 拿到结果 → hermes_state.save_session (:143)
                              ⚠️ **第 6 章起这段已作废**：`save_session()` 连整个 JSON 版一起删了，
                                 改成 agent 在回合里**逐条 append_message** 落 SQLite
                                 （`run_agent._persist_message`，见《源码讲解-06》§七）。
                                 本走读是第 2 章末的快照，不再回头改。
⑩  cli.py:80                  _on_text 已经把答复打出来了
```

**对着这张表读代码，每一步都能落地。** 注意 ⑦ 里 `continue` 那一跳——
**那就是整个 agent 的心脏**。

---

## 十三、读代码时该问自己的 5 个问题

每读一个函数，问：

1. **它在哪一层？** 界面逻辑还是 agent 行为逻辑？（判据：换个入口还需不需要）
2. **它失败了会怎样？** 返回错误文本 / 抛异常 / 静默吞掉 —— 选哪个，为什么？
3. **它有没有顺序依赖？** 并发时会乱吗？（工具结果回填就是典型）
4. **它的默认值选哪边？** 安全相关的一定选保守侧（`looks_approval_needed` 的两个 `return True`）
5. **它和谁共享状态？** 内存 / 历史 / 磁盘 —— 谁在什么时候重建它？

---

## 十四、读完之后可以自己改的 5 个练习（按难度）

| # | 练习 | 会学到什么 |
|---|---|---|
| 1 | 补上 `_run_one_tool` 里 agent 级工具的异常兜底（见第 6 节末尾那个缺口） | "分发两条路"的不一致性 |
| 2 | 加一个 `/history` 命令，打印 `messages` 里每条的角色和长度 | 亲眼看消息序列怎么长出来的 |
| 3 | 把 `_BUILTIN_MODULES` 手写名单改成真·目录扫描 | 自动发现的代价（顺序、失败处理） |
| 4 | 给 `terminal` 加一个 `interactive=True` 的工具（比如 `confirm`），观察整批工具变成串行 | 亲手验证故障 3 的语义问题 |
| 5 | 把 `run_agent.py` 拆成 facade + 3 个兄弟模块（照着 Hermes 的 `agent/turn_*.py`） | **"拆完之后反而更难读了"** 这个体感 |

第 5 个是**故意推荐你做的**——因为只有拆过，你才会理解 Hermes 为什么要
在 `AGENTS.md` 里立那么多条防退化的规矩（"文件过 2000 行必须拆""新功能不许 append 到 facade"）。
**光看是看不出代价的。**

---

## 附：本项目的四类自检

```bash
cd "<仓库目录>/mini-hermes"

python3 tests/test_loop_mechanisms.py       # 54 条：loop 的机制（不联网）
python3 tests/test_packaging.py             #  7 条：零依赖 / 无硬编码路径 / 密钥链
python3 demos/why_interactive_is_serial.py  #  3 个可复现故障（interactive 为何必须串行）
python3 demos/why_slow_connect_matters.py   #  75s vs 2s（连接超时 ≠ 读取超时，要等 80 秒）
./mini -q "..."                             # 真跑（联网、花 API 的钱）
```

**前四个不花钱、不联网**（第五个要等约 80 秒，因为它真的在等 75 秒超时）。
**改完代码先跑它们，再花 API 的钱。**
