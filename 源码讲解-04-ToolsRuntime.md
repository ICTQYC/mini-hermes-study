# 源码讲解 · 第 4 章 Tools Runtime

> 配套《中文精译-04-ToolsRuntime.md》。那篇讲**原理**（原文说了什么 + 真实源码怎么做的），
> 这篇讲**差异**：mini 版和真实版差在哪、每处差价值不值得改，外加本章实现时抓出的真问题。
>
> 体例同《源码讲解-03》：**先给差异一览表**（一眼看全）→ 逐项「mini 怎么做的 /
> 真实 Hermes 怎么做的 / 差在哪」→ 深读 → 真问题 → 结尾差距清单。
>
> 📌 **数字与行号锚标签 `ch04-tools-runtime`**（第 4 章末状态）。
> 要精确核对：`git show ch04-tools-runtime:mini-hermes/tools/registry.py`。

[TOC]

---

## 两份代码的路径

```text
mini 版（本仓库，第 4 章末）
  $HOME/Desktop/study/hermes学习/mini-hermes/
    tools/registry.py      362 行    ←  tools/registry.py              977 行
    model_tools.py         293 行    ←  model_tools.py                 987 行
    toolsets.py            144 行    ←  toolsets.py                    485 行
    tools/terminal.py      115 行    ←  tools/terminal_tool.py + terminal_tool_backends.py
    tools/file_tools.py    147 行    ←  tools/file_tools*.py（多个文件）
    tools/search_files.py  130 行    ←  search_files（走 ripgrep）
    tools/todo.py          267 行    ←  tools/todo_tool.py + agent/inline_tool_executors.py
    tools/approval.py       84 行    ←  tools/approval.py + approval_detection.py + 6 个兄弟
    tools/registry.py     （注册表）  ←  tools/registry.py

真实版
  ~/.hermes/hermes-agent/
```

---

## 〇、差异一览表（先看这张）

按"差在哪一层"排，**从结构差异到细节差异**：

| # | 主题 | mini 版 | 真实 Hermes | 性质 | 值得改 |
|---|---|---|---|---|---|
| 1 | **发现机制** | AST 扫 `tools/*.py`，认 2 种注册写法 | AST + **磁盘缓存** + 包入口 + `__init__` 守卫，认 4 条规则 | 简化 | 规模到了再说 |
| 2 | **注册参数** | `register_tool()` 9 个 | `register()` **13 个** | 简化 | ❌（多出的都是多 profile / 插件用） |
| 3 | **覆盖语义** | 同 toolset 重注册允许，跨 toolset 拒绝 | **四条路径** + 两道闸门 + 防"洗白" | 简化 | ⬜ 等插件系统 |
| 4 | **`check_fn` 缓存** | TTL 30s + 抖动 60s + `no_cache` + 两层 | 同上 **+ 按 profile 分键 + 条数上限** | 简化（有意的） | ⬜ 支持多 profile 前必须补 |
| 5 | **toolset 规模** | **4 个** / 暴露 **6 个**工具 | **60 个** / `_HERMES_CORE_TOOLS` **59 个** | 规模 | ❌（数量不是知识） |
| 6 | **动态 schema 修补** | 无 | `_DYNAMIC_SCHEMA_REWRITERS` **10 条** | 缺失 | ⬜ 等有 `execute_code` 这类"要告诉它有哪些工具"的工具 |
| 7 | **分发层数** | **1 层**（`handle_function_call`） | **2 层**（`dispatch` + `handle_function_call`） | 简化 | ❌（mini 的注册表只做"查+调"） |
| 8 | **agent 级工具集合** | **1 个**（`todo_list`） | 两层：`INLINE_TOOL_EXECUTORS` **14** ⊇ `_AGENT_LOOP_TOOLS` **4** | 规模 | ❌（但**两层这个结构**是本章核心收获） |
| 9 | **错误包装** | 1 层，上限 2048 | **3 层**，上限 2048 | 简化 | ❌ |
| 10 | **危险模式** | **10 条** / 1 个文件 | **107 条** / **8 个文件** | 规模 | ❌ 抄"挪到一处"的结构就够 |
| 11 | **终端后端** | **1 个**（local） | **7 个** | 规模 | ❌（真复杂度在 process_registry，不在后端数） |
| 12 | **async 桥接** | 无（工具全同步） | 三条路（按线程 + running loop 判据） | 缺失 | ❌ mini 没有 async 工具 |
| 13 | **工具结果契约** | ✅ JSON 字符串 + 截断 | ✅ 同 | **已对齐** | — |
| 14 | **"注册 ≠ 暴露"** | ✅ 已实现（`toolsets.py`） | ✅ 同 | **已对齐** | — |

> **读法**：13、14 是本章把 mini 从"看起来做了"拉到"真的做了"的两条；
> 1、4、7、8 是**结构上对齐、规模上简化**；其余是**有意不做**（理由见 §十二）。

---

## 一、注册表

### mini 版怎么做的

```python
# mini-hermes/tools/registry.py
@register_tool("read_file", description=..., parameters={...},
               toolset="file", check_fn=check_file_requirements)
def read_file_tool(path, offset=1, limit=...): ...
```

- 注册写法是**装饰器**（Hermes 是模块级普通调用 `registry.register(...)`）。两者都在 **import 时**执行。
- `ToolEntry` 有 **8** 个字段：`name / toolset / schema / fn / check_fn / interactive /
  requires_approval / danger_check`。
- 覆盖只做了最保守的一档：**同 toolset 重注册允许**（MCP 重连那种刷新），
  **跨 toolset 同名直接拒绝 + `logger.error`**。

### 真实 Hermes 怎么做的

- `register()` **13 个参数**，比原文多 4 个：`max_result_size_chars` /
  `dynamic_schema_overrides` / `override` / `scope`。
- `ToolEntry` **11 个字段**。
- **注册时会校验 schema**，而且是**故意的**：

  ```python
  # tools/registry.py:628
  # Reject malformed schemas at registration, not at request time: a non-dict
  # ``parameters`` (e.g. a list) serializes into every provider request and 400s the
  # whole turn far from the offending plugin. Failing here names the culprit instead.
  ```

  → 一个坏 schema 如果在请求时才炸，**报错地点离肇事者很远**（turn 挂了，你不知道是哪个插件干的）。

- **覆盖语义是四条路径**，不是一条。判据是四个条件（缺一不可）：
  ① 这个工具**想盖掉的是一个全局工具**（不是同 toolset 的刷新）
  ② 注册方**声明了** `override=True`
  ③ 使用者（`config.yaml`）开了 `allow_tool_override: true`
  ④ 真正的**拥有者**是注册方

- **两个失败形态，处理方式故意相反**：

  | 情况 | 处理 | 为什么 |
  |---|---|---|
  | **疏忽**（没声明 override，但名字撞了） | `logger.error` + **不注册** | 只是想加个工具，不该炸掉整个启动 |
  | **越权**（声明了 override，但使用者没授权） | **`raise PermissionError`** | 这不是疏忽，是"我说了要盖，你没同意" |

- **防"洗白"**：`_plugin_owner_of()` 用 `handler.__globals__["__name__"]` 判断"这函数是谁定义的"。
  否则插件可以把自己实现**包一层 lambda** 冒充内置函数。

### 差在哪

| | mini | Hermes |
|---|---|---|
| 注册参数 | 9 | 13 |
| 覆盖 | 1 档（同 toolset / 跨 toolset） | 4 条路径 + 2 道闸门 + 防洗白 |
| schema 校验 | 无 | **注册时**就拒绝（理由是"报错要离肇事者近"） |
| 失败形态 | 统一 `logger.error` | 疏忽→记日志；越权→**抛异常** |

**为什么 mini 不补**：多 profile（`scope`）、插件（`override`）、结果大小上限
（`max_result_size_chars`）在 mini 里**都还没有对应概念**。补了就是投机性实现
——Hermes 的 `AGENTS.md` 明确把"加没人用的配置旋钮"列为不想要的东西。

**📌 可以带走的判据**：**"疏忽"和"越权"应该是两种处理方式**。
疏忽给了容错空间（记日志、跳过），越权必须**大声失败**——因为前者是"没意识到规则"，
后者是"明知规则还违反"。这个区分比"要不要允许覆盖"本身更重要。

---

## 二、发现机制 ★ 本章核心差异

### mini 版怎么做的

第 2 章的 mini 是**手写名单**：

```python
_BUILTIN_MODULES = ["tools.terminal", "tools.file_tools"]
```

第 4 章换成 AST 自动发现。**这一步在本章的计划清单里排第一**，因为手写名单的
失效模式是**静默少一个工具**——不报错、日志干净、agent 只是显得变笨了。

```python
# mini-hermes/model_tools.py
def module_registers_tools(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if REGISTER_DECORATOR not in source:
        return False                      # ← 文本预筛，省掉 ast.parse
    tree = ast.parse(source, filename=str(path))
    for stmt in tree.body:                # ← 只看模块体
        ...                               #    装饰器形态 / 普通调用形态 / for 循环形态
```

### 真实 Hermes 怎么做的

机制的**内核和 mini 完全同构**（文本预筛 → `ast.parse` → 只看 `tree.body`），
但多了四样：

**① AST 检查其实是四条规则**（mini 只认 2 条）

| 规则 | mini | Hermes |
|---|---|---|
| 模块体里的 `registry.register(...)` 语句 | ✅ | ✅ |
| 模块级 `for` 循环里的注册（表驱动模块） | ✅ | ✅ |
| 只认**模块体**（函数内部的不算） | ✅ | ✅ |
| **文本预筛**（先看有没有这个词，再 parse） | ✅ | ✅ |
| **手动套装饰器形态** `register_tool(...)(handler)` | ✅（mini 特有） | ❌ |

> mini 多认的这一条是**被迫的**：mini 的注册是装饰器风格，所以"普通调用形态"
> 在 mini 里长成 `register_tool(...)(handler)`——`stmt.value.func` 是**内层 `Call`**
> 而不是 `Name`。只认 `Name` 的话这种写法会被**静默跳过**，
> 而那正是这个机制最怕的失效模式。

**② 候选文件不只 `tools/*.py`，还有包入口**

Hermes 也扫 `tools/<pkg>/tool.py`——工具可以是一个包。mini 没有包形式的工具。

**③ 包里缺 `__init__.py` 会被跳过并警告**

这条注释值得看，因为它解释了一个"源码里能跑、装成 wheel 就消失"的坑。

**④ 磁盘记忆化缓存（原文完全没提）**

Hermes 把 AST 结论按 `(mtime_ns, size)` 存到磁盘。理由：**它在启动路径上**，
而 `~100` 个文件的逐文件 AST 扫描要 **~145ms**。

**⑤ 数字：258 个文件里只有 43 个是工具**

这是"为什么必须静态判断"的最强证据——如果按"全部 import 试一遍"来做，
代价是**执行 215 个不该执行的模块**（而 import 是有副作用的）。

**关于 MCP / 插件两批发现**：原文把它写成"核心发现之后的第二批发现"，
真实代码里**被故意从模块级移走了**：

```python
# tools/registry.py（模块级发现处）
# MCP discovery is deliberately NOT run here: it blocks up to 120 s and the
# gateway lazy-imports this module inside its event loop; each entry point
# (gateway/run.py, cli.py, tui_gateway, acp_adapter) runs it at startup.
```

`discover_mcp_tools()` 内部是阻塞的 `future.result(timeout=120)`，
而网关在 **asyncio 事件循环里**懒加载 `model_tools` → 会冻结 Discord/Telegram
**心跳最多 120 秒**（issue **#16856**）。那句话本身就是一份完整的故障报告。

### 差在哪

| | mini | Hermes |
|---|---|---|
| 发现判据 | 同构（文本预筛 + AST + 只看模块体） | 同构 |
| 认几种写法 | 3（含装饰器变形） | 3（含包入口） |
| 磁盘缓存 | ❌ | ✅（`(mtime_ns, size)`） |
| 包入口 `tools/<pkg>/tool.py` | ❌ | ✅ |
| `__init__.py` 守卫 | ❌ | ✅ |
| 文件数 | 6 | 258（43 个工具） |

**为什么 mini 不做磁盘缓存**：Hermes 需要它是因为**每次启动都要重扫 ~100 个文件**；
mini 只有 6 个 → 缓存本身的开销（读写磁盘 + 失效判断）**大于**它省下的时间。
**"优化"要按规模给，不是按样例给。**

**📌 可以带走的判据**（两条）：

1. **"发现"和"注册"是两件事。** 发现决定"import 谁"，注册是模块被 import 时
   **自己做的副作用**。混起来讲会导致"为什么我加了注册却没生效"这类问题查不清。
2. **模块级副作用同时决定了"它会在谁的调用栈上执行"。** 同一段 MCP 发现代码，
   放在模块级 = 在网关的事件循环里跑（冻结心跳）；放到各入口的启动路径 = 没事。
   Hermes 为此有**两种应对**（移走 vs `_run_async` 桥接），判据是"**是不是每次都要**"。

---

## 三、`check_fn`

### mini 版怎么做的

```python
CHECK_FN_TTL_SECONDS = 30.0            # 成功结果活 30s
CHECK_FN_FAILURE_GRACE_SECONDS = 60.0  # 抖动保护窗口
_cache: Dict[Callable, Tuple[float, bool]] = {}   # (时刻, 结果)
_last_good: Dict[Callable, float] = {}
_no_cache: Set[Callable] = set()
```

落地成三个真实形态：

| 工具 | 探测函数 | 形态 |
|---|---|---|
| `terminal` | 真检查 shell 在不在（`shutil.which`） | 真实探测 |
| `search_files` | **转发** `check_terminal_requirements()` | 转发型（真的共用前置条件——它 shell out 到 grep） |
| 文件工具 | 恒真，但**显式**写 `return True` | 恒真型 |

### 真实 Hermes 怎么做的

机制内核**完全对齐**（TTL + 抖动保护 + `no_cache` + 两层缓存），另外三处 mini 没有：

**① 缓存必须按 profile 分键**：`_check_fn_cache` 的 key 是 `(fn, hermes_home_key())`。
一个 Hermes 进程服务多个 profile（`~/.hermes/profiles/<name>/`），
不分键会让 A profile 的探测结果被 B profile 复用 → **能力串味**。

**② 条数上限**：`_CHECK_FN_CACHE_MAX = 512`。

**③ 分级日志**：探测抛异常 → `WARNING`；返回 False → `INFO`。
（例外本身要留着，否则"为什么这个工具没出现"查不出来。）

**④ tool defs 也有缓存**：`_TOOL_DEFS_CACHE_MAX = 8`（`model_tools.py:204`）。

### 差在哪

| | mini | Hermes |
|---|---|---|
| TTL / 抖动窗口 | 30s / 60s | 30s / 60s ✅ 同 |
| 两层缓存（memo + TTL） | ✅ | ✅ 同 |
| `@no_cache_check_fn` | ✅ | ✅ 同 |
| 按 profile 分键 | ❌ | ✅ |
| 条数上限 | ❌ | 512 |
| tool defs 缓存 | ❌ | 最多 8 份 |
| 分级日志 | ⚠️ 只有 warning | WARNING / INFO 分级 |

**为什么 mini 不做 profile 分键**：mini 是**单 profile** 的（没有 `profiles/` 目录）。
⚠️ 但这是**有意的简化**，代价写在 `tools/registry.py` 的模块 docstring 里：
**如果以后支持多 profile，一个 profile 的探测结果会被另一个复用**——那时必须补。

**📌 这个"代价写在原地"的做法值得照抄**：简化本身不可怕，
可怕的是**简化了但没人知道代价在哪**。下次有人加多 profile 时，
他会在读注册表的第一屏看到这句话。

---

## 四、Toolset

### mini 版怎么做的

新建 `toolsets.py`，**4 个 toolset** / 默认暴露 **6 个**工具（5 注册 + `todo_list`）。

这一节是本章的**核心修复**——因为动手前 mini 的 `toolset` 参数**一直存在、
每个工具都填了，但没有任何代码读它来做暴露决定**。参数名对、赋值对、
注释也写了，唯一的问题是**没有消费者**。这是"看起来做了、其实没做"的典型。

配套一条**契约断言**（不是数量断言）：

```python
# tests/test_tools_runtime.py
orphans = toolsets.orphan_tools(registry.tool_names())
check("每个注册的工具都归属某个 toolset（否则它永远不会被暴露）", not orphans)
```

### 真实 Hermes 怎么做的

- **60 个 toolset**，`_HERMES_CORE_TOOLS` **59 个**工具。每个平台继承核心捆再收窄/扩展。
- ★ **"注册 ≠ 暴露"是硬规矩**（`tools/AGENTS.md`，原文完全没提）：
  discovery 只**注册 schema**，一个工具**只有被某个 toolset 点名才会被暴露**。
  所以"加一个核心工具"是**两个文件**：`tools/your_tool.py` **+** `toolsets.py`。
  只做第一步 → 工具存在、无报错、但**模型永远看不见**。
- **`_DYNAMIC_SCHEMA_REWRITERS` 有 10 条**（不是原文说的 2 条），
  在 `get_tool_definitions()` 里**动态**修补 schema 文本。
- ★ **一条硬规矩：静态 schema 不许点名别的 toolset 的工具。**
  交叉引用**只能**由 `get_tool_definitions()` 动态加。
  理由：静态写在描述里的"你有 X 工具"在 X 不可用时会变成**幻觉调用**（模型去调一个不存在的工具）。
- **可用集合在改写之前先取快照**，否则"我引用的工具可不可用"这个判断会
  **依赖"谁先跑"**——这是最隐蔽的一类不确定性。
- **toolset 别名有两套机制**（旧名兼容）。

### 差在哪

| | mini | Hermes |
|---|---|---|
| toolset 数 / 核心工具数 | 4 / 6 | 60 / 59 |
| 注册≠暴露 | ✅ 已实现 | ✅ 同 |
| 孤儿工具检查 | ✅ 契约断言 | ✅（有对应测试要求） |
| 动态 schema 修补 | ❌ | 10 条 |
| "不许点名别的 toolset" 这条硬规矩 | ⚠️ mini 无动态修补 → **暂时不会犯** | ✅ |

**为什么 mini 不做动态修补**：它的第一条（`execute_code` 的"可用工具清单"）需要
mini 先有 `execute_code` 这个工具。**没有那条需求时，"修补"机制是空转的。**

**📌 这里有个正面结论**：mini 因为**没有**动态修补，反而**暂时不会犯**那条硬规矩的错。
但这是"运气"不是"设计"——等 mini 真的有工具需要在描述里点名别的工具时，
**必须同时把修补机制和那条硬规矩一起加上**，否则就会开始出幻觉调用。


### 真实实现的五种形态（47 处注册带 `check_fn`）

> 以下从《中文精译-04》搬来。机制在上面，这一段是「**真实的人在里面写什么**」。
> （首行标题与上一段重复，正文从第二行起。）


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



---

## 五、分发（Dispatch）

### mini 版怎么做的

**一层**：`model_tools.handle_function_call()`。顺序是四道闸门：

① 旧工具名映射 → ② agent 级工具守卫 → ③ 查注册表（不存在时**在错误里列出可用工具**）
→ ④ 执行 + 兜异常。出口**永远是 JSON 字符串**。

### 真实 Hermes 怎么做的

- **两层**：`registry.dispatch()` 一层 + `handle_function_call()` 一层。
- **`handle_function_call()` 的真实顺序是 11 步**（原文只给 6 步）。
- **错误包装是三层**，错误文本上限 **2048**（`_MAX_TOOL_ERROR_CHARS`）。
- ★ **agent 级工具有两层不同的集合**（本章最重要的一条，见下表）。
- **async 桥接的真实三条路**，判据是**"在哪个线程 + 有没有 running loop"**，
  不是"CLI 还是网关"：

  ```python
  # model_tools.py:142
  if threading.current_thread() is not threading.main_thread():
      return _get_worker_loop().run_until_complete(coro)
  ```

### 差在哪

| | mini | Hermes |
|---|---|---|
| 层数 | 1 | 2 |
| 分发前闸门 | 4 步 | 11 步 |
| 错误包装 | 1 层 | 3 层 |
| 错误文本上限 | 2048 | 2048 ✅ 同 |
| agent 级工具 | 1 个（`todo_list`），1 层集合 | **14 ⊇ 4**，两层集合 |
| async 桥接 | 无 | 3 条路 |

**为什么 mini 用一层**：Hermes 的第二层（`dispatch`）要做插件钩子、edit approval、
guardrails、`tool_call` 桥接解包（`tool_call` 会被**解包成真实工具名**，
好让下游每个钩子看到的是真名而不是"桥"）。**mini 没有钩子体系 → 第二层是空的。**

> Hermes 那个解包有个精确的理由值得记：
> *so every downstream hook (pre/post, edit approval, guardrails) sees the real tool name,
> never the bridge.* —— **桥接层不能把"我包了一层"泄漏给下游。**

**📌 关于"两层集合"**（mini 只有一层，但**这个概念必须带走**）：

| 集合 | 位置 | 数量 | 含义 |
|---|---|---|---|
| `INLINE_TOOL_EXECUTORS` | `agent/inline_tool_executors.py` | **14** | 执行需要 **agent 对象/callback/界面** 的工具 |
| `_AGENT_LOOP_TOOLS` | `model_tools.py:607` | **4** | 分发层**拒绝**处理、必须由 agent loop 接管的工具 |

前者 **⊇** 后者。原文说的"四个工具被拦截"**只描述了下面那 4 个**，
漏掉了更大的那一类——只在 `INLINE` 里、不在 `AGENT_LOOP` 里的 10 个：
`annotate_preview` / `clarify` / `desktop_preview` / `drive_preview` / `gui_tour` /
`manage_connections` / `memory` / `message_agent` / `read_window_below` / `session_search`。

它们共同的形状：**需要 callback 或会话界面**，不需要 Store。
（原文给的理由"需要 TodoStore/MemoryStore"只盖了需要 **Store** 的那一类。）

---

## 六、危险命令审批

### mini 版怎么做的

第 4 章把危险检测从 `tools/terminal.py` **挪到独立的 `tools/approval.py`**——
理由是"**唯一事实来源**"：检测住在工具文件里时，任何一个新工具想复用"什么算危险"
都得 import 终端工具，而终端是最重的那个模块。

**10 条**模式，`(regex, 描述)` 元组，命中返回描述。**只做检测**，
"问谁 / 等多久 / 记不记得"在调用方。

### 真实 Hermes 怎么做的

- ⚠️ **`DANGEROUS_PATTERNS` 不在 `tools/approval.py` 里**，在
  **`tools/approval_detection.py:203`**——**107 条**，而且有**预编译版本**
  （`DANGEROUS_PATTERNS_COMPILED`）。
- approval 相关拆成 **8 个文件**。这 8 个文件的分工本身就说明了审批的真实复杂度：
  检测 / 人机等待 / 会话级记忆 / 永久允许清单 / 权限模式 / 门面…
- 还有 **Smart 审批**（用辅助 LLM 自动批准明显安全的命令）。

### 差在哪

| | mini | Hermes |
|---|---|---|
| 模式条数 | 10 | 107 |
| 预编译 | ❌ | ✅ |
| 文件数 | 1 | 8 |
| 永久允许清单（落盘） | ❌ | ✅（`config.yaml` 的 `command_allowlist`） |
| Smart 审批（辅助 LLM） | ❌ | ✅ |
| 检测 vs 决策的分离 | ✅ 分开了 | ✅ 分开了 |

**为什么 mini 不抄 107 条**：**覆盖面不是知识，结构才是**。
mini 留 10 条就能证明"检测挪到一处"这个结构，补到 107 条只会让 diff 变长。

**📌 这一节最值得带走的是那个"分层"**：

```text
检测（这条命令危险吗）      →  每次都要算，纯函数  →  tools/approval_detection.py
决策（问谁 / 等多久 / 记不记得）→  有状态、有界面    →  其余 7 个文件
```

mini 的 `approval.py` 只做了**前一半**——这是有意的，因为"决策"那半需要界面和会话状态。

---

## 七、终端 / 运行时环境

### mini 版怎么做的

**只有 local 后端**。但按计划**把"后端表"的位置留出来了**——
`tools/terminal.py` 顶部的注释写清了"如果加第二个后端，接口该长什么样"。

### 真实 Hermes 怎么做的

- **7 个后端**：local / docker / ssh / singularity / modal / daytona / vercel_sandbox。
  （这处**文档比 `tools/AGENTS.md` 更准**——后者少列了 `vercel_sandbox`。）
- **每个后端是一个"环境类" + 一个"构建器"**。
- ★ **每个 spawn 都走同一个 env 构建器**（原文完全没提，但是 hard rule）。
- `process_registry` 那一大段（后台进程的持久化/接管）。

### 差在哪

| | mini | Hermes |
|---|---|---|
| 后端数 | 1 | 7 |
| 环境类 + 构建器的分层 | ❌ | ✅ |
| 统一 env 构建器 | ❌（只有一个后端，直接 `subprocess`） | ✅ hard rule |
| `process_registry` | ❌ | ✅ |

**为什么 mini 不做第二个后端**：真正的复杂度**不在后端数量**，
而在"**多父 + 持久化 + 重启后接管**"（`process_registry` 那套）。
只加一个 docker 后端而不加那套，得到的是一份**看起来更完整、实际没有新机制**的代码。

---

## 八、并发（原文只有一句话）

### mini 版怎么做的

第 2 章就有 `force_sequential`：**只要这批里有 interactive 工具，整批串行**。

### 真实 Hermes 怎么做的

**工具批次是「分段」，不是「整批并发/串行」**。`_plan_tool_batch_segments` 拆成
有序的 `("parallel"|"sequential", calls)` 段，三层判据：

① **屏障**：`_NEVER_PARALLEL_TOOLS` / 参数不可解析 / 非并行安全 → 该调用单独串行
② ★ **路径冲突**：**路径重叠且至少一个写者 → 关闭并行段**，让这个调用排在冲突者落地之后；
   **读↔读重叠仍然并行**
③ **降级与合并**：一段 < 2 个调用 → 降级串行；相邻串行段合并

### 差在哪

| | mini | Hermes |
|---|---|---|
| 粒度 | 整批 | **分段** |
| interactive | 整批串行 | 该调用单独串行 |
| 路径冲突 | ❌ | ✅（`read a.py` + `write a.py` **不并发**） |
| planner | ❌ | ✅ `_should_parallelize_tool_batch` |

**📌 用户学到的规则是对的，粗糙的是实现它的方式。**
"interactive 必须打断并发"这条结论**正确**；mini 的问题是**实现粒度太粗**——
它把"这一个调用要串行"放大成了"这一批都要串行"，于是丢掉了
`read x.py + write y.py` 这种本可以并发的组合。

（完整分析在《源码讲解-02-AgentLoop.md》，这里只作为**差异归档**列一次。）

---

## 九、【深读】一个工具从注册到结果：把整条链走一遍

> 这一节是从《中文精译-04》搬过来的**全景深读**。它讲的是一条链而非一个机制
> —— 拿一个真实的小工具（`read_terminal`，89 行）从注册一路走到结果，
> 因为本章**最重的那个坑**（注册 ≠ 暴露）只有在整条链上才看得见。
>
> 单个机制请回前面各节看；这一节回答的是「**为什么模型不用我那个工具**」。

### 先把整条链走一遍（拿真工具 `read_terminal` 当例子）

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



---

## 十、本章实现时抓出的真问题

> 体例同《源码讲解-03》的「四个真问题」：**实现过程中真炸过、且值得记住的**。
> 这一章有三条 —— 两条是 mini 自己的 bug，一条是**我的测试装置写错了**。

### 10.1 计划外的：**这一章的原则抓出了 mini 自己的两个 bug**

**① 提示词在宣传一个不存在的工具**

动手前的注册表是：

```python
['list_dir', 'read_file', 'terminal', 'write_file']
```

而 system prompt 里一直有这么一行：

```text
需要搜索文件内容：search_files（**不要**用 grep/find）
```

`search_files` **根本不存在**。这正是本章反复讲的那种病：
**描述里点名了模型实际调不到的工具**。
区别只在于 Hermes 是在 `_DYNAMIC_SCHEMA_REWRITERS` 里防这个
（发出去之前把"可用工具清单"重写一遍），
mini 是在**提示词的散文里**犯的同一个错。

修法是**把工具补上**，不是把提示词那行删掉——因为
"不要用 grep/find" 这个指引本身是对的：`grep` 的输出**没有边界、没有结构**，
一个递归 grep 能把上下文冲掉一半。

**② `read_file` 到底返不返回 JSON？**

动手前先核对过一遍，因为看起来矛盾：
`AGENTS.md` 说 *All handlers return a JSON string*，
而模型看到的 `read_file` 输出格式说明写的是 `LINE_NUM|CONTENT`。

```python
# tools/file_tools.py:457（Hermes 真身）
"content": file_ops._add_line_numbers(page_text, offset) if page_text else "",
```

**不矛盾**：handler 返回的是 JSON，`LINE_NUM|CONTENT` 是 `content` **字段里面**的格式。
所以 mini 也是这个形态：`{"path": …, "content": "1|foo\n2|bar"}`——
**人读的部分留在字段里，外壳是 JSON**。

### 10.2 一处"看起来做了、其实没做"被这一章点破

第 2、3 章的 mini 里 `register_tool(…, toolset="…")` 这个参数**一直存在**，
每个工具文件也都老老实实填了。但**没有任何代码用它决定"要不要发给模型"**——
注册表里有什么，模型就看到什么。

这就是"**看起来做了、其实没做**"：参数名对、赋值对、注释也写了，
唯一的问题是**没有消费者**。而且它在测试里也不会露馅——
除非专门去断言"这个工具**不在** schema 里"。

把它接上（§10 ③）之后，"注册 ≠ 暴露"这句话才第一次在 mini 里
成为**可验证的事实**，而不是一条注释。

### 10.3 一处测试设计错误（反向教训）

写 `check_fn` 抖动保护的测试时，我第一版把 `TTL` 设成 0 来"让每次调用都重新探测"，
然后断言"探测了 3 次"——想以此证明"兜底的 True 没被缓存"。

**这个装置证明不了这件事。** `TTL=0` 时，"没缓存"和"缓存了但立刻过期"
表现**完全一样**，两种实现都会得到同样的探测次数。
我当时的断言只是**记住了某次运行的数字**，不是一条性质。

改法：把 `registry.time` 换成一个**可拨的假时钟**
（`registry.time` 只是模块级名字，直接替换即可——**不需要为可测性去改生产代码**），
然后构造一个真能区分的场景：

```text
t=1000  探测成功            → 缓存 (1000, True)
t=1010  再问                → 命中缓存，0 次探测
t=1031  缓存过期 + 探测失败  → 抖动保护 → 返回 True，**不写缓存**
t=1032  宽限期关掉再问       → 若上面写了缓存，这里会命中并返回 True（错的）；
                             实际重新探测 → False（对的）
```

教训与第 3 章那条同源：**别拿间接信号当证据**。
"探测次数"是间接信号；能区分两种实现的是**时序**。

---



---

## 十一、测试覆盖：54 条在守什么

| 组 | 守的承诺 | 条数 |
|---|---|---|
| 1. AST 自动发现 | 按**语法形状**判断（不按文本）/ 只看模块体 / 坏文件不炸 / 发现真实工具 | 12 |
| 2. toolset：注册 ≠ 暴露 ★ | 三种入参 / **注册了但没点名 → 模型看不到** / 契约：每个工具都有 toolset | 10 |
| 3. `check_fn` ★ | TTL 缓存 / **抖动保护** / fail-safe / 两层缓存 / no_cache | 13 |
| 4. 结果契约 | 未知工具 / 参数不匹配 / 内部异常 → 都是 JSON；错误有上限；非字符串被包一层 | 7 |
| 5. agent 级工具守卫 | 绕过 agent loop 直接分发 → 错误要指出**调用方不对** | 4 |
| 6. 端到端 | 模型实际看到的 tools 字段 / toolset 能收窄 / 工具结果是 JSON | 6 |

**为什么这样分组**：每一组对应一个**闸门**。这一章讲的所有机制最终都体现在
"那一个 `tools` 字段里有什么"——所以第 6 组是端到端（抓真实请求），
前 5 组是把闸门一个个单独按住。

**两条特别值得看的断言**（它们把"静默失败"变成了"测试会红"）：

```python
# ① 注册了但没被 toolset 点名 → 必须**不在** schema 里
check("它不在发给模型的 schema 里（注册 ≠ 暴露）", "_orphan_tool" not in names)

# ② 契约，不是数量：每个注册的工具都必须归属某个 toolset
check("每个注册的工具都归属某个 toolset", not toolsets.orphan_tools(registry.tool_names()))
```

> 📌 **契约断言 vs 数量断言**：数量断言（"应该有 5 个工具"）每次加工具都要改，
> 而且不告诉你任何**关系**；契约断言（"每个工具都有 toolset"）在有人忘了加 toolset 时
> **自动报警**。Hermes 的 `tools/AGENTS.md` 明确要求写前者。

---

## 十二、差距清单（每条标注值不值得改）

| 差距 | 影响 | 值得改吗 | 何时 |
|---|---|---|---|
| **`check_fn` 缓存按 profile 分键** | 多 profile 会**能力串味** | ⭐ **必须改** | 首次支持多 profile 之前 |
| 发现结果的磁盘缓存 | 启动慢（mini 只有 6 个文件，<1ms） | ❌ 不值得 | 工具文件 >50 个 |
| 包入口 `tools/<pkg>/tool.py` | 工具不能是包 | ❌ 不值得 | 真有工具需要多文件时 |
| `__init__.py` 守卫 | 无（mini 不打包成 wheel） | ❌ 不值得 | 真要发布成包时 |
| 动态 schema 修补（10 条） | 描述里不能点名别的工具 | ⬜ 有需求再补 | 有 `execute_code` 那类工具时 |
| toolset 别名两套机制 | 改名时会断旧会话 | ⬜ 有改名需求时 | 真的要改名时（`_LEGACY_TOOL_ALIASES` 已留位） |
| 分发型覆盖 / 影子 / 防洗白 | 无插件系统 | ⬜ 第 10 章 | 有插件系统时 |
| 分发第二层（钩子） | 无 pre/post 钩子 | ❌ 不值得 | 有插件体系时 |
| 错误包装 3 层 | 错误信息略简 | ❌ 不值得 | 无所谓 |
| 107 条危险模式 | 覆盖面窄 | ❌ 不值得 | **永远不抄**（抄结构不抄数量） |
| 第二个终端后端 | 只有本地 | ❌ 不值得 | **大概永远不需要** |
| `process_registry` | 无后台进程持久化 | ⬜ 有需求时 | "重启后恢复进程"成为需求时 |
| async 桥接 | async 工具会卡住 loop | ❌ 不值得 | mini 出现 async 工具时 |
| 分段式并发 planner | 路径冲突时**可能**并发读+写 | ⬜ 有真故障再补 | 见到"并发改文件"类故障时 |

> **一句话取舍**：这一章的价值 70% 在**"发现机制 + 注册≠暴露 + 两层 agent 级工具集合"**
> 这三件**结构**上的事，30% 在 `check_fn` / JSON 契约 / 审批分层这些**局部**改进。
> 后端数量、模式条数、async 桥接都是"等有真实需求再补"。

---

## 附录 · 本章末的 mini 状态（别再从头数）

```text
tools/registry.py      105  →  362 行
model_tools.py          66  →  293 行
toolsets.py            （无） →  144 行
tools/approval.py      （无） →   84 行
tools/terminal.py       78  →  115 行
tools/file_tools.py     92  →  147 行
tools/search_files.py  （无） →  130 行
tools/todo.py          267  →  267 行（未动）
tests/test_tools_runtime.py（无）→ 546 行 / 54 条 / 6 组

注册 5 个工具 / 暴露 6 个（+todo_list）
源码 4678 行 · 测试 2682 行 · demo 276 行
测试合计 275 条（154 + 54 + 13 + 54），Python 3.13.14 与 3.9.6 都过
```
