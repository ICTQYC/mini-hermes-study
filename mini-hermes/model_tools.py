"""工具发现与分发。

对应 Hermes 的 model_tools.py（真实版本 987 行）。

第 4 章改了三处：
1. **手写名单 → AST 自动发现**（`_BUILTIN_MODULES` 删掉了）
2. **加 toolset 解析** —— 注册 ≠ 暴露
3. **分发加两层闸门** —— 旧工具名映射 + agent 级工具守卫；错误一律 JSON

Hermes 里这个文件是 tools/ 和 run_agent.py 之间的中介层：
run_agent 永远不问"这个工具怎么实现"，只问 handle_function_call。

## ① 为什么要换成 AST 自动发现

第 2 章写的是手写名单：

    _BUILTIN_MODULES = ["tools.terminal", "tools.file_tools"]

当时的原话是"手写名单在只有 2 个模块时更好读，但**忘加一行就会静默少一个工具**
——这是我们要在别的章节改进的地方"。第 4 章就是那个章节。

手写名单的失效模式：

| 情况 | 后果 |
|---|---|
| 加了新工具文件但忘了改名单 | **那个工具在模型眼里不存在**（不报错） |
| 名单里名字写错 | `ModuleNotFoundError`（还好，会炸） |
| 名单里有模块被删了 | `ModuleNotFoundError`（还好） |

**最坏的是"漏加"**：不是报错，是模型**不知道自己有这个工具**，
于是永远不去用它。而这**没有任何症状**——agent 只是显得"变笨了"。

## ② 静态判断 vs 运行时判断

发现机制要回答"哪些文件是工具模块"。三种可能的做法：

| 做法 | 问题 |
|---|---|
| 全部 import 试一遍 | **import 有副作用**，试错代价是执行了不该执行的代码 |
| 靠命名约定（不以下划线开头等） | 约定会被破坏，而且会误伤 |
| **按"模块体里有没有那句注册"** | 精确、**且静态**（`ast.parse` 不执行任何代码） |

静态的额外好处：结论可以被缓存（因为没依赖运行时状态）。
Hermes 把它按 `(mtime_ns, size)` 记在磁盘上，因为每文件 AST 扫描在 ~100 个文件上要 ~145ms。
**mini 只有几个文件，所以不缓存**——等真有 100 个文件时再补，不提前做。
"""
from __future__ import annotations

import ast
import importlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import toolsets
from tools import registry
from tools.registry import tool_error, tool_result

__all__ = ["discover_builtin_tools", "get_tool_definitions",
           "handle_function_call", "module_registers_tools", "REGISTER_DECORATOR"]

_log = logging.getLogger(__name__)

# mini 的注册写法是**装饰器**（Hermes 是模块级普通调用，见 tools/registry.py 的说明）。
REGISTER_DECORATOR = "register_tool"

_TOOLS_DIR = Path(__file__).resolve().parent / "tools"
# 这几个文件不参与发现：一个是包声明，两个是基础设施（它们自己不是工具）
_SKIP_FILES = {"__init__.py", "registry.py", "approval.py"}

_discovered = False


# ── ① AST 自动发现 ──────────────────────────────────────────────────────
def _is_register_decorator(node: ast.AST) -> bool:
    """判断一个装饰器节点是不是 `@register_tool(...)`。

    认的是**语法形状**，不是文本：

        ast.Call
        └─ func = ast.Name，id == "register_tool"

    所以注释里提到 `register_tool` 不算、字符串里提到也不算——
    这正是"按语法查"和"按文本查"的区别（文本查会误伤注释和文档字符串）。
    """
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == REGISTER_DECORATOR)


def _is_register_call_stmt(stmt: ast.stmt) -> bool:
    """`register_tool` 作为**模块体里的一条语句**被调用（非装饰器语法）。

    要认两种套法，因为写法不同、AST 形状就不同：

        register_tool(...)                  →  Call(func=Name)        ← Hermes 的写法
        register_tool(...)(handler)         →  Call(func=Call(func=Name))
                                               ↑ 手动套装饰器，mini 的普通调用形态

    第一种直接调（handler 以参数传入），第二种是先拿到装饰器再套上去。
    ⚠️ 如果只认第一种，**第二种写法会被静默跳过** —— 而"静默跳过"
    正是这个发现机制最怕的失效模式。所以两种都要认。
    """
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    func = stmt.value.func
    if isinstance(func, ast.Call):        # ← 手动套装饰器：往里再剥一层
        func = func.func
    return isinstance(func, ast.Name) and func.id == REGISTER_DECORATOR


def module_registers_tools(path: Path) -> bool:
    """这个模块**声明了工具**吗？

    只看**模块体**的语句（`tree.body`）：写在函数内部的注册不算。
    理由：只有 import 时执行的注册才有"自注册"的意义；
    写在函数里的注册要么是懒加载（另一个话题），要么是缩进错了。

    与 Hermes 一致的优化：先做**文本预筛**再 `ast.parse`——
    解析是最贵的一步，而大部分文件里连这个词都没有。
    """
    try:
        source = path.read_text(encoding="utf-8")
        if REGISTER_DECORATOR not in source:
            return False                      # ← 文本预筛，省掉 ast.parse
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        # SyntaxError 要吞掉而不是抛出：一个坏文件不该让整个发现流程崩。
        # ⚠️ 代价是坏文件会被**静默跳过** —— 所以真实项目里要配一条
        #    "发现到的模块数是否合理"的检查（见 tests/test_tools_runtime.py）。
        return False

    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_is_register_decorator(d) for d in stmt.decorator_list):
                return True
        elif _is_register_call_stmt(stmt):
            return True
        elif isinstance(stmt, ast.For) and any(_is_register_call_stmt(s) for s in stmt.body):
            # 表驱动的模块用一个循环注册好几个工具，同样发生在 import 时
            # （Hermes 的注释：*Table-driven modules register several tools from one loop*）
            return True
    return False


def _tool_module_candidates() -> List[Path]:
    """候选文件：`tools/*.py`，**排序后**返回。

    为什么必须排序：注册表允许**同 toolset 重注册**（覆盖），
    所以 import 顺序会决定谁赢——顺序不能依赖文件系统的返回顺序。
    （Hermes 的 `_tool_module_candidates` 有同样的注释。）

    Hermes 还扫 `tools/<pkg>/tool.py`（包形式的工具）。mini 没有包形式的工具，
    所以不做——等真有再说（不做投机性实现）。
    """
    return sorted(_TOOLS_DIR.glob("*.py"))


def discover_builtin_tools() -> List[str]:
    """import 每个声明了工具的模块（触发它们的自注册），返回模块名。

    ⚠️ **"发现"和"注册"是两件事**，别混：
      · **发现**决定"import 谁" —— 它自己不注册任何东西
      · **注册**是模块被 import 时**自己做的副作用**
    """
    global _discovered
    if _discovered:
        return registry.tool_names()

    imported: List[str] = []
    for path in _tool_module_candidates():
        if path.name in _SKIP_FILES:
            continue
        if not module_registers_tools(path):
            continue
        mod_name = f"tools.{path.stem}"
        try:
            importlib.import_module(mod_name)
            imported.append(mod_name)
        except Exception as exc:  # noqa: BLE001
            # optional 工具的 import 失败不该阻止其他工具加载。
            # （Hermes 也是这个策略：*Errors in optional tools are caught and logged*。）
            _log.warning("工具模块 %s 导入失败，已跳过：%s", mod_name, exc)
    _discovered = True
    return imported


# ── ② 构建发给模型的 schema ─────────────────────────────────────────────
def get_tool_definitions(enabled_toolsets: Optional[List[str]] = None,
                         disabled_toolsets: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """算出这一轮发给模型的 `tools` 字段。

    三步（对应 Hermes 的 `get_tool_definitions()`）：

    ① **toolset 解析** → 该暴露哪些**名字**
    ② **注册表过滤** → 对每个名字跑 `check_fn`（不通过的整条 schema 都不要）
    ③ **agent 级工具的 schema 补进来**（它们不在注册表里，见下）

    ⚠️ 第 ① 步是第 4 章新增的。在这之前 mini 是"注册表里有什么就发什么"，
    于是 `toolset` 字段**存在但没人用** —— 一个"看起来做了、其实没做"的典型。

    ⚠️ 还有一条铁律（Hermes 的 narrow waist）：**每次 API 调用都发全部工具**。
    这就是为什么新加一个核心工具的门槛极高——它是**每一轮都在付的固定成本**。
    """
    names = toolsets.get_enabled_tool_names(enabled_toolsets, disabled_toolsets)

    # ② 注册表里的（check_fn 过滤在 registry.get_definitions 里做）
    registered = [n for n in names if registry.get(n) is not None]
    defs = registry.get_definitions(registered)

    # ③ agent 级工具：schema 不在注册表里（mini 的设计，见 tools/todo.py）。
    #    它们也要过 toolset 闸门 —— 所以 toolsets.py 那张默认捆里必须有 `todo_list`。
    from tools.todo import AGENT_LEVEL_TOOLS

    for name, schema in AGENT_LEVEL_TOOLS.items():
        if name in names:
            defs.append(schema)

    # 顺序稳定（利于 prompt 缓存）：按工具名排序
    return sorted(defs, key=lambda d: d["function"]["name"])


# ── ③ 分发 ──────────────────────────────────────────────────────────────
# `todo_list` 是 **agent 级**工具：**执行**需要 agent 对象本身，不走注册表。
# 它在到达这里之前就被 run_agent 的 INLINE_TOOL_EXECUTORS 吃掉了。
#
# ★ 这个集合是一个**守卫**，不是"清单"。
#   注册表里根本没有它，所以正常路径永远走不到这里；
#   但如果某条路径**绕过**了 agent loop 直接调 handle_function_call，
#   我们要给出一个能指出问题的错误，而不是含糊的"未知工具"。
#
# 对应 Hermes 的 `_AGENT_LOOP_TOOLS = {"todo_list","memory","session_search","delegate_task"}`
# （注意：Hermes 那个是 **4 个**，而它的 `INLINE_TOOL_EXECUTORS` 有 **14 个** ——
#   两层集合，见第 4 章 §9①。mini 只有一个 agent 级工具，所以这里只有一条。）
_AGENT_LOOP_TOOLS = {"todo_list"}

# 旧工具名 → 新工具名（对应 Hermes 的 `_LEGACY_TOOL_ALIASES`）。
#
# 语义是"**对外收窄、对内兼容**"：
#   · schema 只宣传新名（模型看到的永远是新的）
#   · 分发接受旧名（老会话、存下来的提示词里写的旧名还能用）
#
# Hermes 的例子：{"todo": "todo_list", "cronjob": "cronjob_manage", ...}（5 条）
# mini 现在还不需要改名，所以这张表是**空的**——但**位置留着**，
# 因为"将来会改名"这件事是确定的。空 dict 的代价是 0，而"以后再加"要动结构。
_LEGACY_TOOL_ALIASES: Dict[str, str] = {}


def handle_function_call(name: str, args: Dict[str, Any],
                         task_id: Optional[str] = None) -> str:
    """执行一次工具调用，**永远返回 JSON 字符串**。

    顺序（每一步都是"在真正执行之前"的一道闸门）：

    ① 旧工具名映射
    ② agent 级工具守卫
    ③ 查注册表（不存在 → 错误里**列出可用的**，让模型能自己改）
    ④ 执行 + 兜异常

    异常一律变成给模型看的 JSON 错误：模型看到错误能自己改，
    看到 traceback 只会浪费上下文。

    （Hermes 的包装是**两层**：`registry.dispatch()` 一层、`handle_function_call()`
      再来一层。mini 合并成一层——因为 mini 的注册表只做"查 + 调"。
      **层数应该跟着需要走，不是跟着样例走。**）
    """
    original = name
    name = _LEGACY_TOOL_ALIASES.get(name, name)

    if name in _AGENT_LOOP_TOOLS:
        return tool_error(
            f"{name} 是 agent 级工具，必须由 agent loop 处理"
            f"（mini 里是 run_agent.INLINE_TOOL_EXECUTORS），不能直接分发"
        )

    entry = registry.get(name)
    if entry is None:
        # 错误里带上可用工具名：模型能据此自己改，而不是干瞪眼
        return tool_error(f"未知工具：{original}", available=registry.tool_names())

    try:
        result = entry.fn(**(args or {}))
    except TypeError as exc:
        return tool_error(f"参数不匹配：{exc}")
    except Exception as exc:  # noqa: BLE001 - 工具层要把任何异常变成给模型的 JSON
        return tool_error(f"工具执行异常：{type(exc).__name__}: {exc}")

    # 契约兜底：handler 应该返回 JSON 字符串，但**出口必须是 JSON**——
    # 所以拿到别的东西就包一层，而不是原样吐出去。
    # （对照 Hermes 的 `passthrough_json`：不是 JSON 就包成 {"text": ...}。）
    if isinstance(result, str):
        return result
    return tool_result(result)
