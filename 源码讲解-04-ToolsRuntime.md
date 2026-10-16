# 源码讲解 · 第 4 章：工具运行时（Tools Runtime）

> 维护说明：章节交付时的统计与实测输出保留原口径；当前发布前修复、套件数字与验证收据见《发布前修复记录.md》。mini 代码引用按维护后的实现核对。

> 配套《中文精译-04-ToolsRuntime.md》。那篇讲**原理**（原文说了什么 + 真实源码怎么做的），
> 这篇讲**差异**：mini 版和真实版差在哪、每处差价值不值得改，外加本章实现时抓出的真问题。
>
> 体例同《源码讲解-03》：**先给差异一览表**（一眼看全）→ 逐项「mini 怎么做的 /
> 真实 Hermes 怎么做的 / 差在哪」→ 深读 → 真问题 → 结尾差距清单。
>
> 📌 **主体代码与规模锚 `db47e97`；搜索侧线锚 `0d8d3ff`**；当前维护另行注明。
> 要精确核对：`git show db47e97:mini-hermes/tools/registry.py`。

[TOC]

## 发布前维护 · 工具探测与失败结果

`tools/registry.py` 的探测 TTL 缓存与 last-good 抖动保护都按 `(check_fn, MINI_HOME 的解析绝对路径)` 分键，避免复用其他 profile 的可用性结果。`tools/terminal.py` 的非零退出状态通过 `tool_error` 返回，仍保留 `exit_code`、`stdout`、`stderr`，不会仅因进程成功启动就报工具成功。

这些是当前实现的行为；下文的章节提交、规模表和实测输出保留各自历史时点。新增回归在 `mini-hermes/tests/test_publish_regressions.py`，完整收据见[发布前修复记录](发布前修复记录.md)。



---

## 两份代码的路径

```text
mini 版（本仓库，第 4 章末）
  $HOME/projects/hermes-study/mini-hermes/
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
| 4 | **`check_fn` 缓存** | TTL 30s + 抖动 60s + `no_cache` + 两层 + 按 home 分键 | 同上 **+ 条数上限** | 简化（有意的） | ✅ profile 分键已补；条数上限仍未做 |
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
| 15 | **工具进度/完成回调** | 一个回调：`tool_progress_callback(name, args, phase[, status, summary])`；`done` 带 **成败 + 一行摘要** | 一对 `tool_start_callback` + **`tool_complete_callback(..., result)`**（进度回调另带 `preview`），verbose 下打结果全文 | 简化（**信息量已补齐**） | ✅ 已抄（§十三 第 1 条） |

> ★ **第 15 行是 2026-10-14 解冻时补的**：mini 不引第二套回调（第 4 章 B 批砍的是「分发第二层钩子」那类），但把真实版 `tool_complete_callback` 的**信息量**（结果 → 成败 + 摘要）补在了 `done` 阶段上 —— 起因是一次手工使用：失败的工具在终端上和成功长得一模一样。
>
> **读法**：13、14 是本章把 mini 从"看起来做了"拉到"真的做了"的两条；
> 1、4、7、8 是**结构上对齐、规模上简化**；其余是**有意不做**（理由见 §十三）。

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

### 自注册换掉了什么：一张中央表的三个代价

> 精译 §0.5 给了点题版；这里是完整证据。

**假想的"中央表"写法**：

```python
# tools/_table.py
from tools.terminal import terminal_tool
from tools.file_tools import read_file_tool, write_file_tool, list_dir_tool
from tools.search_files import search_files_tool

TOOLS = {
    "terminal":     terminal_tool,
    "read_file":    read_file_tool,
    "write_file":   write_file_tool,
    "list_dir":     list_dir_tool,
    "search_files": search_files_tool,
}
```

| # | 代价 | 具体表现 |
|---|---|---|
| ① | **加工具 = 改一个共享文件** | 要在 `_table.py` 改两处（import + 表项）。而这个文件**出现在每个人的 diff 里** → 天天撞冲突（Hermes 有 **59** 个核心工具） |
| ② | ★ **一个名字变成两个真相** | 表里的 key 和 `@register_tool("...")` 里的名字要**手动对齐**。改一处忘另一处 → 名字与实现对不上，而**没有任何机制会报错** |
| ③ | **删工具会留下悬空项** | 表里那行还在，指向一个不存在的函数 |

第 ② 条是根本的：自注册保住了 **`name` 只有一个来源**——
那个参数同时喂了台账的 key 和 schema 的名字，所以"两边一致"是**结构保证**。
中央表把它换成"记得两边都改"。

**依赖方向的真实现测**（`grep` 出来的 import 关系）：

```text
tools/registry.py   →  （空）                          ← 叶子，谁都不依赖
tools/terminal.py   →  tools.approval, tools.registry, hermes_constants
tools/file_tools.py →  tools.registry（+ hermes_constants）
model_tools.py      →  toolsets, tools.registry         ← 发现器也不认识具体工具
run_agent.py        →  model_tools, agent.*, tools.todo
```

**单向**：registry（叶子）← 工具 ← 发现器 ← agent。
**工具之间零依赖**——`terminal.py` 不认识 `file_tools.py`，反之亦然。
中央表版会把这条链反过来：`_table.py` 必须 import 每一个工具实现。

**判据：怎么看出一个项目有没有退化成中央表？**
搜"同时点名了多个工具名"的文件。mini 里只有两个命中，而且**都不是中央表**：

- `tools/file_tools.py` 点名 3 个 —— 那 3 个是**它自己实现的**（每个名字只出现在它自己的注册调用里）
- `toolsets.py` 点名 5 个 —— 这是**暴露表**，是另一回事（§四）

**配套**：自注册只回答"谁来登记"，不回答"谁去 import 那些文件"。
所以必须配发现机制（§二）。两个机制是一对，缺一个都不成立。

**边界**：自注册 ≠ 自动暴露 —— 加工具仍要改 `toolsets.py`。

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

**① 分开比较注册语法与筛选规则**（不能混用数量）

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
| 注册语法与入口 | 装饰器或普通调用，支持模块级 for | 普通调用，支持模块级 for；另发现包入口 |
| 磁盘缓存 | ❌ | ✅（`(mtime_ns, size)`） |
| 包入口 `tools/<pkg>/tool.py` | ❌ | ✅ |
| `__init__.py` 守卫 | ❌ | ✅ |
| 文件数 | 6 | 258（43 个工具） |

**为什么 mini 不做磁盘缓存**：Hermes 需要它是因为**每次启动都要重扫 ~100 个文件**；
mini 只有 6 个 → 尚无证据说明磁盘缓存能明显改善这一级规模的启动耗时；读写与失效判断也有成本。
**"优化"要按规模给，不是按样例给。**

**📌 可以带走的判据**（两条）：

1. **"发现"和"注册"是两件事。** 发现决定"import 谁"，注册是模块被 import 时
   **自己做的副作用**。混起来讲会导致"为什么我加了注册却没生效"这类问题查不清。
2. **模块级副作用同时决定了"它会在谁的调用栈上执行"。** 同一段 MCP 发现代码，
   放在模块级 = 在网关的事件循环里跑（冻结心跳）；放到各入口的启动路径 = 没事。
   Hermes 为此有**两种应对**（启动时卸到线程 vs `_run_async` 同步桥接）；不能只按调用频率决定调度方式。

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

机制内核**完全对齐**（TTL + 抖动保护 + `no_cache` + 两层缓存），其中 profile 分键已在发布前维护中补齐；其余差异如下：

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
| 按 profile 分键 | ✅ `(fn, MINI_HOME 的解析绝对路径)`；TTL 与抖动保护均分键 | ✅ |
| 条数上限 | ❌ | 512 |
| tool defs 缓存 | ❌ | 最多 8 份 |
| 分级日志 | ⚠️ 只有 warning | WARNING / INFO 分级 |

**历史与现状**：第 4 章快照没有按 profile 分键；第 7 章后已支持多 profile，发布前维护补上了 `(fn, home)` 键。不能继续用“单 profile”解释当前实现。若只给 TTL 分键、却让 last-good 抖动保护共用一个键，仍可能跨 profile 借到旧的成功结果。

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

### 工具清单在请求里的位置（**第 3 章 ↔ 第 4 章的接缝**，原文没讲）

> 这一节是**第 3 章和第 4 章的接缝**。读第 4 章时很容易把工具清单想成"提示词的一部分"，
> 从而以为它该归到第 3 章的三层里。**位置不对，但耦合是真的** —— 下面把两半都说清。

**① 物理位置：它是 payload 的另一个顶层字段，不在三层里面**

```python
payload = {
    "model": ...,
    "messages": api_messages,      # ← 第 3 章那三层提示词住在这里面（messages[0] 是 system）
    "tools": model_tools.get_tool_definitions(),   # ← 工具清单在**旁边**，和 messages 平级
    "tool_choice": "auto",
    "temperature": 0.3,
}
```

第 3 章的三层（stable / context / volatile）划分的是**系统提示词那个字符串**。
工具清单是另一个块，**形式上不属于任何一层**。

**② 但它和三层共享同一个「缓存前缀」—— 这才是关键**

第 3 章的核心原则是"按**变化频率**从低到高排，让最长公共前缀尽量长"。
工具清单虽然不在字符串里，但它**吃同一个缓存前缀**。三处证据：

| # | 证据 | 说明 |
|---|---|---|
| ① | `agent/bedrock_adapter.py:916` | 缓存点被**显式打在 tools 块的末尾**：`{"tools": converse_tools + [dict(_CACHE_POINT)] if "tools" in cache_at else ...}` |
| ② | Hermes 顶层 `AGENTS.md` | 把 **swaps toolsets** 和 **rebuilds the system prompt** 列成**同一类**缓存失效事件：*Anything that mutates past context, **swaps toolsets**, reloads memories, or **rebuilds the system prompt** mid-conversation invalidates that cache* |
| ③ | **提示词里埋的「能力指纹」**（`agent/conversation_loop.py:609` 的 `_bot_chat_prompt_stale()`，机制在 `tools/bot_mode_probe.py`） | 存盘的提示词里**嵌了一个能力指纹**，对不上就**故意重建一次**。docstring 原话：*a deliberate **once-per-change** rebuild*。⚠️ 这处是 **Bot Chat 专属**的，不是通用机制 —— 但它证明"**能力面变了 → 提示词该重建**"这个模式在 Hermes 里是**被显式处理**的，而不是碰巧 |

> 📌 ③ 那处还有两个细节值得抄（都是这条原则的落地方式）：
>
> - **"一次性"**：重建后打上标记（stamped），**不会反复触发**——否则每轮都失效。
> - **探测失败 fail closed 到"复用"**：docstring 原话
>   *probe failures fail closed to "reuse" so the cache is kept* ——
>   宁可留着旧提示词，也不误伤缓存。
>   （判据方向和 `check_fn` 的 fail-safe **相反**：那里失败=不可用（少给），
>   这里失败=复用旧的（保住缓存）。**方向由"失败时哪边代价更大"决定。**）

**为什么"共享缓存前缀/绑同一失效事件"比"归到 volatile 层"更准**：

- volatile 层只是**拼装顺序上的一个位置**（在字符串里排最后）。
- 缓存前缀是**代价** —— 它被失效要重新 prefill 整个上下文，而且**随上下文增长**
  （第 3 章实测每轮 +806 字符）。
- 所以真正的约束不是"它该排第几"，而是"**它的变化必须绑到一个已经要失效的事件上**"。

> 📌 一句话：**三层管的是"提示词字符串内部怎么排"；工具清单是并排的另一个块，
> 但它和提示词共享同一个缓存前缀、同一个失效事件。**
> 这条和第 3 章里"时间戳该绑在压缩边界上"是同一条原则的第二次应用：
> **任何会变的东西，都要绑到一个已经发生失效的事件上，而不是引入新的失效点。**

**③ 顺带钉死一个常见混淆：AST 检查 / toolset / `check_fn` 判的不是同一件事**

| 阶段 | 它回答的问题 | 判据 | 不通过的后果 |
|---|---|---|---|
| **AST 检查**（§二） | 这个文件里有工具吗？→ **import 它吗** | 模块体里有没有那句注册 | 文件不被 import → 工具**根本不存在** |
| **toolset**（§四） | 这类能力**在这个界面上**存在吗 | 界面 / 会话属性 | 工具存在但**模型看不见** |
| **`check_fn`**（§三） | **这个环境里**它现在能用吗 | 凭据 / 服务 / 二进制在不在 | 同上（也不进那包 JSON） |

★ **AST 检查跟"暴露"无关** —— 它只管"发现"，决定的是工具**存不存在**。
"存在但不暴露"是一种正常状态，`tests/test_tools_runtime.py` 里有一条就是钉它的：
注册进台账、但没被任何 toolset 点名 → **不在那包 JSON 里**。

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
`manage_connections` / `message_agent` / `read_terminal` / `read_window_below` / `setup_mcp`。

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

**只有 local 后端，未建立后端表**。`tools/terminal.py` 顶部说明了为什么等第二个后端出现时再抽表，不能把这段取舍注释算成已经预留了接口。

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

缺少 `lark_oapi` SDK → `_memo_check` 返回 False → **这一行 `continue`** →
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

### 10.4 ★ 工具跑在**另一个目录**里（复检抓到的，最严重的一个）

**症状**：让 agent 读用户项目里的 `src/app.py`，它报告
「`/tmp/myproj` 下只有 agent/、demos/、hermes_cli/、tests/、tools/，没有 src/」
—— **那是 mini 的安装树**，不是用户的项目。而 `/tmp/myproj/src/app.py` 明明存在。

**根因：同一个事实有两个来源。**

| | 读的是 | 结果 |
|---|---|---|
| 提示词（`resolve_context_cwd`） | `MINI_LAUNCH_CWD` | `/tmp/myproj` ✓ |
| 工具（`tools/terminal.py` 的 `cwd=os.getcwd()`） | 进程 cwd | **安装树** ✗ |

`mini` 启动器为了 import 必须 `cd "$MINI_DIR"`（脚本目录进 `sys.path`），
于是**进程 cwd 变成安装树**。第 3 章修过一次这个问题，但只修了**提示词那半**
（AGENTS.md 发现 / 工作区快照），**工具执行那半没修** ——
所以模型被告知"你在 A"，实际在 B 里干活。

**为什么危险**：不是崩溃，是**静默跑在错的地方**。
`read_file src/app.py` 报"文件不存在"、`list_dir .` 列出别人的目录，
模型会据此得出错误结论（这次它诚实地停下来问了，所以没造成破坏）。

**修法 —— 让"我在哪"只有一份实现**：

```text
hermes_constants.resolve_context_cwd()   ← 唯一的实现（双方都依赖的叶子模块）
        ↑                    ↑
agent/prompt_builder.py    tools/terminal.py
（提示词，重新导出）        tools/search_files.py（cwd=）
                          tools/file_tools.py（_abs() 解析相对路径）
```

- `resolve_context_cwd*` 从 `agent/prompt_builder.py` **搬到 `hermes_constants.py`**。
  放 prompt_builder 会让 `tools/` 反向依赖 `agent/`（层级颠倒）。
  `prompt_builder.resolve_context_cwd is hermes_constants.resolve_context_cwd` → True，
  所以 `system_prompt.py` 和已有测试的调用点一行都不用改。
- 三个工具改用 `resolve_context_cwd()`（`search_files` 原本是 `cwd=None` = 继承进程 cwd）。

**为什么不靠运行中 `os.chdir()` 传项目目录**：它改变进程级 cwd，会影响同进程的其他线程与会话，不能实现 profile 隔离。原来的 import 理由经双解释器复核不成立：`python -m` 的 `sys.path[0]` 在本次 3.9 / 3.13 实测中是启动目录的绝对路径，切换 cwd 后仍可导入启动目录里的模块；不能把它与 `python -c` 的空字符串路径混为一谈。

**为什么不"启动器不 cd"**：`cd` 的作用是把安装树放进 `sys.path`；
去掉它，用户项目里若有同名的 `tools/`、`agent/` 就会**抢掉 mini 自己的模块**
（`sys.path[0]` 仍是 cwd 且排在 `PYTHONPATH` 前面）。

### 10.5 装饰器绑到了错误的函数上（同一个复检，顺手撞到）

把新写的 helper `_abs` 插在

```python
@register_tool("read_file", ...)      # ← 装饰器在这里
def _abs(...): ...                    # ← 我插到这里
def read_file_tool(...): ...          # ← 它才是该被注册的那个
```

**之间** → 装饰器绑到了 `_abs`：`read_file` 的 handler 变成 `_abs`，
`read_file_tool` **根本没注册**。

**症状极具误导性**：`TypeError: Object of type PosixPath is not JSON serializable`
——看起来像 JSON 契约坏了（第 4 章刚立的规矩），实际是注册绑错了函数。
**报错离肇事者非常远。**

已固化成契约测试（**不是**数量断言）：

```python
for name in registry.tool_names():
    fn = registry.get(name).fn
    if fn.__name__ not in (f"{name}_tool", name):
        bad.append(f"{name} → {fn.__name__}")     # read_file → _abs
```

> 📌 两条可迁移的判据：
> ① **插入代码时，锚点行要选"装饰器上面"，不是"函数定义上面"** ——
>    中间那一行是装饰器和函数的**绑定区**，插进去会改变装饰对象。
> ② **报错位置离肇事者远时，先怀疑"绑定/注册"这一层**，别顺着报错往下查。

---

## 十一、测试覆盖：89 条在守什么

| 组 | 守的承诺 | 条数 |
|---|---|---|
| 1. AST 自动发现 | 按**语法形状**判断（不按文本）/ 只看模块体 / 坏文件不炸 / 发现真实工具 | 12 |
| 2. toolset：注册 ≠ 暴露 ★ | 三种入参 / **注册了但没点名 → 模型看不到** / 契约：每个工具都有 toolset | 10 |
| 3. `check_fn` ★ | TTL 缓存 / **抖动保护** / fail-safe / 两层缓存 / no_cache | 13 |
| 4. 结果契约 | 未知工具 / 参数不匹配 / 内部异常 → 都是 JSON；错误有上限；非字符串被包一层 | 7 |
| 5. agent 级工具守卫 | 绕过 agent loop 直接分发 → 错误要指出**调用方不对** | 4 |
| 6. 端到端 | 模型实际看到的 tools 字段 / toolset 能收窄 / 工具结果是 JSON | 6 |
| 7. **工作目录与注册完整性**（复检新增） | 「我在哪」**单源** / **工具跑在提示词报的那个目录** / handler 没绑错函数 | 9 |
| 8. **凭据型 `check_fn` 与 toolset 闸门**（以 `web_search` 为例） | 没 key 就**不发** / 探测不联网 / 出口是 JSON / **两道闸门彼此独立** | 11 |
| 9. **密钥链** | **留空 = 没写**（不挡后面的真值）/ 引号剥离 / 真环境变量优先 | 5 |
| 10. **双供应商**（Tavily / Serper 按 key 自动选） | 只配 Tavily 也走得通 / **厂商字段名不外泄** / 优先级 = 表顺序 + 前提自检 | 10 |

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
> **自动报警**。Hermes 的 `tools/AGENTS.md` 明确要求写后者（契约断言）。

---

## 十二、实操：从零加一个工具（以 `web_search` 为例）

前面 §一~§十一 讲的是**机制**；这一节是**把机制用一遍**——加一个新工具到底要碰什么、
怎么验、会在哪里踩坑。`web_search` 就是照这个流程真加进去的（提交 `dea41f1`）。

### 只有两个文件是必须改的

| # | 步骤 | 碰什么 | 不改会怎样 | 怎么验 |
|---|---|---|---|---|
| 1 | 写工具文件 | 新建 `tools/web_search.py`（`@register_tool` 装饰器） | 工具**不存在** | — |
| 2 | （可选）`check_fn` | 同文件 | 没配好也发出去 → 模型**白试错** | 配/不配 key 各跑一次 |
| 3 | **放进 toolset** | 改 `toolsets.py` | ★ **静默看不见**（无报错） | 看 schema 里有没有 |
| 4 | —— | **发现机制不用碰** | —— | AST 自动扫到 |
| 5 | 写测试 | `tests/test_tools_runtime.py` 新组 + 契约断言 | 下次改坏不知道 | 跑一遍 |
| 6 | **反向验证** | 删掉关键一行 | 测试可能只是**摆设** | 必须变红 |
| 7 | 真跑 | —— | 只信 agent 说"做完了" | 去文件系统核实副作用 |
| 8 | 同步文档数字 | README / 精译 / 本节 | 数字**腐烂且看不出来** | `grep` 残留 |

**第 4 步是这一章的收益**：手写名单时代（`_BUILTIN_MODULES`），加工具要改第三个文件；
换成 AST 发现之后不用了——文件里只要有那句注册就会被发现。

### 真跑记录

```text
① AST 自动发现（没改任何名单）
   discovered = ['tools.file_tools', 'tools.search_files', 'tools.terminal', 'tools.web_search']
   注册表     = [..., 'web_search', ...]

② 没配 key → 模型看到的 6 个里**没有** web_search      ← check_fn 挡住
③ 配上 key → 还是**没有**（见下）                      ← ★ 30s TTL 缓存
④ 清掉探测缓存 → 出现了（7 个）                         ← 这时才真正生效
⑤ 禁掉 search 组 → web_search 和 search_files 一起消失   ← toolset 是整组开关
⑥ 真实分发（假响应）→ {"query":…, "results":[…], "answer":…}
⑦ 没配 key 直接调 → {"error":"MINI_SEARCH_API_KEY 未配置"}  ← 出口永远是 JSON
```

### ⚠️ 第 ③ 步是个必踩的坑：`check_fn` 有 30 秒 TTL

**"改完配置发现没生效"是正常的。** `check_fn` 的结果按 30s TTL 进程级缓存
（§三），所以：

```python
os.environ["MINI_SEARCH_API_KEY"] = "..."   # 配上了
# → 立刻再看 schema，还是看不见（缓存里那个 False 还在）
registry.invalidate_check_fn_cache()        # ← Hermes 里 `hermes tools enable` 干的就是这件事
# → 看见了
```

这是"注册成功、日志无异常、模型就是看不见"最容易被误判成 bug 的一次。

### 测试怎么写：一条数量断言的实证

加完 `web_search`，一条**老测试立刻报红**：

```text
✗ 发现了 3 个工具模块          ← 数量断言：加第 4 个工具它就必须改
```

改成**契约断言**（断言关系，不断言数量）：

```python
✓ 文件工具被发现了   ✓ 终端被发现了   ✓ 搜索被发现了
✓ web_search 被发现了（新加的工具，**没改任何名单**）
✓ 没把 registry / approval / __init__ 误当工具
```

**那条红本身就是"数量断言 vs 契约断言"的实证**——它红得对，不该去"修数字"，
该去改断言的性质。

新组（第 8 组，11 条）守的承诺：

| 断言 | 守什么 |
|---|---|
| 台账里**有**它 + JSON 里**没有**它 | 凭据型 `check_fn`。**两条要同时成立**——只断言后者的话，工具压根没注册也会"通过" |
| 探测里不发网络请求（把网络层换成"一调用就断言失败"） | `check_fn` 的契约：**只读配置、不出网** |
| 成功/失败/空参数都是 JSON | 出口契约 |
| 配了 key 但禁掉 toolset → 照样不给 | ★ **两道闸门彼此独立** |

**反向验证**：把 `web_search` 从 `toolsets.py` 拿掉 → 2 条立刻变红
（"配上 key 后它出现了" / "key 配了 → check_fn 放行"）。**能变红，测试才算数。**

### 加第二个供应商：只加一行表项

`web_search` 现在支持两个供应商（Tavily / Serper），**按"哪个 key 在"自动选**：

```python
PROVIDERS = (
    {"name": "serper", "key_env": "MINI_SEARCH_API_KEY", "build": _serper_build, "parse": _serper_parse},
    {"name": "tavily", "key_env": "TAVILY_API_KEY",      "build": _tavily_build, "parse": _tavily_parse},
)
```

`check_fn`、handler、schema **都读这张表**，所以加一家只加一行 —— 不用碰别的。
`TAVILY_API_KEY` 这个名字是**故意**选的：它和 Hermes 自己的 `web_search`
（`tools/web_tools.py` 的自动探测）同名，**一个 key 两份工具用**。

⚠️ 但**放哪个文件决定谁读得到**（实测过，方向是单向的）：

| key 放在 | Hermes 自己的 `web_search` | mini 的 `web_search` |
|---|---|---|
| `~/.hermes/.env` | ✓（它只读这个） | ✓（回退链第 3 项） |
| `~/.mini-hermes/.env` | ✗ **看不到** | ✓ |

所以 `TAVILY_API_KEY` 的值放 `~/.hermes/.env`（单一来源），mini 那份留空。

**顺序就是优先级**，mini 当前把 **Serper 放在前面**。这是实现的选择；账户额度、计费周期及两程序是否共用账户由使用者配置决定，不能从顺序推断。

⚠️ 测试要**自己隔离这两个 key**（`_isolate_search_keys`）：机器上真配了
`TAVILY_API_KEY` 时，Serper 那几条会被悄悄切走 —— 假红，看起来像代码坏了。

### 想换一个搜索服务，改哪里

代码里只有三处和厂商有关（都留了名字，不散落）：

```python
DEFAULT_ENDPOINT = "https://google.serper.dev/search"   # 或 MINI_SEARCH_ENDPOINT 覆盖
headers={"X-API-KEY": key, ...}                          # 认证方式
payload.get("organic")                                   # 结果字段名
```

**别让厂商形状长进提示词里**：`description` 写的是"联网搜索网页。返回 JSON：
`{query, results:[{title,url,snippet}]}`"——**这是我们自己的结果形状**，
换供应商只动上面三行，模型看到的契约不变。
（这也对应 §五 那条可迁移判据：**桥接层不能把"我包了一层"泄漏给下游。**）

---

## 十三、差距清单（每条标注值不值得改）

| 差距 | 影响 | 值得改吗 | 何时 |
|---|---|---|---|
| **工具进度回调不带结果**（`done` 只有名字） | 成功与失败在终端上**长得一样**（都 `✓ 完成`），失败原因只在模型的消息里 ⇒ 用户分不清哪次失败、不知道模型在重试什么 | ✅ **已改**（本次解冻） | 已做（2026-10-14） |
| **`check_fn` 缓存按 profile 分键** | 缓存和失败宽限都不能跨 home 复用 | ✅ **已改**（发布前修复） | 已做；回归测试验证 A→B→A |
| 发现结果的磁盘缓存 | 启动慢（第 4 章工具规模很小；尚未提供启动耗时实测） | ❌ 不值得 | 真有启动耗时实测表明需要缓存时 |
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

> ★ **2026-10-14 解冻补的第 1 条**（本章范围）：`done` 阶段现在带 `(status, summary)` ——
> 判定与观察者**同源**（`_tool_result_observer_fields`），摘要生成见 `mini-hermes/run_agent.py::summarize_tool_result`
> （成功「结果 N 字符」/ 失败「错误首行」，截断 80 字符）；两个消费方（`cli.py` / `gateway/run.py`）同步改形状。
> 回归测试：`tests/test_loop_mechanisms.py` 第 8 组（6 条）+ `tests/test_fallback_and_aux.py`（回退事件的 `phase`）。
>
> ★ **同一条回调上的第二个 bug 顺手修了**：回退事件发的是 `phase="start"`，而界面判的是 `phase == "fallback"`
> ⇒ 那条「主模型失败 → 切换备用」的提示**从来没打印过**（还会被当成一次工具开始、污染 `_turn_started` 计时）。
> 契约改成「**非工具事件用自己的 phase**」。
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
tests/test_tools_runtime.py（无）→ 89 条 / 10 组（搜索工具侧线完成后）

注册 6 个工具；默认暴露 6 个（5 个 + todo_list，web_search 因未配 key 被挡住）
搜索侧线完成后的 0d8d3ff：源码 4915 行 · 测试 3047 行 · demo 276 行（wc -l）
测试合计 310 条（154 + 54 + 13 + 89），Python 3.13.14 与 3.9.6 都过
```


---

## 附录 B · 源码深挖长版（从《中文精译-04》搬来）

**这一份东西为什么在这里、而不是在精译里**

精译的职责是「让我看懂原文在讲什么」；下面这 44 段是**长版源码深挖**
（真实签名、字段清单、规模数字、边角设计、逐句拆解）。它们读起来有信息量，
但读它们**不是看懂原文的前提**——所以从精译搬出来了。

> 📌 **和前面 §一 ~ §八 的关系**：前面那八节是**压缩版**（差异 + 结论，够你判断
> "值不值得改"）；这一份是**长版**（同样的机制，但带上全部细节和出处）。
> 你要查某个细节 → 来这一份；你要判断差异 → 看前面。

> 📌 **如果某一段你希望它回到精译里**（比如读原文时就看不懂那一处），
> 直接说是哪一段，我搬回去 —— 这正是"看不懂就问"那个机制。

共 **1317 行**，按原章节顺序：

### 来自《中文精译-04》· ## 1. 工具注册模型（`registry.register()`）

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
    raise ValueError(f"Tool {name!r}: schema['parameters'] must be an object (JSON Schema dict), ...")  # 错误文案节选
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
        return self._scoped_tools.setdefault(scope, {})     # ← 覆盖槽：每个 scope 一份
```

| 词 | 准确含义 |
|---|---|
| **全局槽**（`self._tools`） | `scope=None` 时注册进去的东西——**所有内置工具**（`tools/*.py` 里调 `register()`） |
| **覆盖槽**（`self._scoped_tools[scope]`） | 插件注册时**带 scope** 落进去的地方，**按 scope 分组（通常对应 profile）；同一 scope 可有多个插件** |
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
> **只有第二道**：使用者的逐插件授权没有表达本次注册是否有意覆盖，仍需作者用 `override=True` 明示意图。
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
> 因为 lambda 继承的是**定义处**的模块名（`__globals__["__name__"]`），不是调用处的。这里绑定的是实现归属，不能把这道检查理解成对任意恶意 Python 插件的沙箱。
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
所以状态必须能从历史里读回来）。任意散文不适合稳定重建程序状态；这不是说模型无法阅读纯文本。

### 来自《中文精译-04》· ## 2. 发现机制（`discover_builtin_tools()`）

#### ① AST 检查：语法条件与模块级 for 支持

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

另外，**模块级的 `for` 循环里注册也算**。注释：*Table-driven modules register several
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
> 缓存键用 `(mtime_ns, size)` 而不是内容 hash；只有检测到这些元数据变化才失效，保留相同时间戳与大小的改写可能漏检。

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

### 来自《中文精译-04》· ## 3. 工具可用性检查（`check_fn`）

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

> 📌 **「真实实现的五种形态」（47 处注册带 `check_fn` 的实地样本）已移到《源码讲解-04-ToolsRuntime.md》§三末**。
> 那一节是"**真实的人在里面写什么**"的清单式调研；本节只留 `check_fn` 的机制本身
> （抖动保护、两层缓存、契约）。

#### `check_fn` 的契约（名字看不出来的那部分）

`check_fn` 这个名字只说了一半——"一个检查函数"。真正约束你**能在里面写什么**的是它下面这七条契约：

| 契约 | 为什么 | 从哪看出来 |
|---|---|---|
| 返回 `bool`，**抛异常当 `False`** | 调用方不 try/except（fail-safe） | `_run_check_fn_uncached` 里的 `except: return False` |
| **必须便宜** | 每 30 秒就可能跑一次，且在启动路径上 | `_CHECK_FN_TTL_SECONDS = 30` + feishu 的 ~5s 注释 |
| **不能有可见副作用** | 它会在桌面 app 启动时跑 | browser 那句 "Windows .cmd shims flash a console" |
| **避免昂贵或长时间阻塞的探测**；本地配置与文件检查可做有限 I/O | browser 的 raw endpoint 检查专门避免阻塞网络探测 | browser 那句 "must not cost a blocking probe" |
| **凭据要走 `get_secret`，不能 `os.getenv`** | 一个进程服务多个 profile | `UnscopedSecretError` 的处理分支 |
| 结果被**进程级缓存** | 所以它**不能**回答"这次会话专属"的问题 | `_check_fn_cache` + `_load_enabled_toolsets` 的分工 |
| 想不缓存：`@no_cache_check_fn` | 本地配置读，必须立刻生效 | `memory_tool` / `kanban_tools` / `browser_vault` 三处 |

> 📌 **最后那两条是同一条边界的两个方向**，值得说清楚：
>
> - **`check_fn` 回答"这个环境里它现在能用吗"** → 答案按 **profile/home** 隔离后才可在进程内缓存，不能把所有 profile 当成同一个环境
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

### 来自《中文精译-04》· ## 4. Toolset 解析

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

### 来自《中文精译-04》· ## 5. 分发（Dispatch）

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

这段里藏着**五个**原文没说的设计：

**a) 为什么"有 loop 时"不能直接 `run_until_complete`？**
因为**你不能在一个已经跑着的 loop 里再跑一个 loop**（`RuntimeError`）。
也不能用 `asyncio.run()`（它要求当前线程没有 running loop）。
所以只能**扔到另一个线程**去跑。

**b) 为什么不能靠 `Future.cancel()` 取消正在执行的工作？** 注释直接给了答案：

> *ThreadPoolExecutor.cancel() is a no-op on a running worker*
>
> 译注：这句源码注释把 API 名字写错了；实际是 `Future.cancel()`，`ThreadPoolExecutor` 没有 `cancel()` 方法。
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

### 来自《中文精译-04》· ## 6. 危险命令审批（`DANGEROUS_PATTERNS`）

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

### 来自《中文精译-04》· ## 7. 终端 / 运行时环境

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

### 后续维护：可操作的章节演示

Web 工具章可切换 file/default，查看真实注册表与 get_tool_definitions 输出，并对生成文件进行读写/列目录。执行前检查可见性，terminal 仅展示闸门，不实际执行。 操作与验证边界见 [逐章演示指南](WEB_CHAPTER_DEMOS.md)。
