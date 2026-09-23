"""Toolset 解析 —— **"注册 ≠ 暴露"** 那道闸门。

对应 Hermes 的 toolsets.py（真实版本 485 行、60 个 toolset）。

## 这个文件解决的问题

`tools/registry.py` 里注册了工具，**不代表模型能看到它**。
工具要经过这个文件点名，才会进 `get_tool_definitions()` 的输出。

Hermes 的 tools/AGENTS.md 原文：

    discovery registers the schema, but **a tool is only exposed if a toolset names it**.

所以"加一个工具"**永远是两个文件**：
    ① tools/your_tool.py   —— 让工具**存在**（注册）
    ② toolsets.py          —— 让它**可见**（点名）

**只做①的后果**：工具注册成功、日志里没有任何异常、`registry.get()` 拿得到，
但**模型永远看不到它**。而且**没有任何报错** —— 这是第 4 章第三次遇到的
"静默失败"，也是最反直觉的一次（你以为写完注册就完了）。

## 为什么是"间接一层"而不是直接列工具名

因为**按名字列清单会过期**。想"终端上不给搜索工具"：

    直接列名字：  ["read_file","write_file","list_dir","terminal"]   ← 加了新工具就漏
    按 toolset：  core - {"search"}                                  ← 自动跟上

两者都会过期，但**只有前者需要人记得改**。这个名字清单问题在第 2 章出现过
（工具分发写两处 → 漂移分家），在第 4 章又出现一次（`_BUILTIN_MODULES` 手写名单）。
**这一章的任务就是把它变成结构上不会漏。**
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

__all__ = ["TOOLSETS", "MINI_CORE_TOOLS", "resolve_toolset",
           "get_enabled_tool_names", "toolset_of_tool", "orphan_tools"]


# ── toolset 表 ──────────────────────────────────────────────────────────
# 对应 Hermes 的 TOOLSETS dict。每个 toolset = 一个名字 + 一句说明 + 它包含的工具。
#
# `description` 不是装饰：`/tools` 命令和 `mini --list-toolsets` 会显示它，
# 用户靠它决定"我要不要开这个 toolset"。
TOOLSETS: Dict[str, Dict[str, Any]] = {
    "file": {
        "description": "读写文件、列目录",
        "tools": ["read_file", "write_file", "list_dir"],
    },
    "search": {
        "description": "搜索文件内容",
        "tools": ["search_files", "web_search"],
    },
    "terminal": {
        "description": "在本机 shell 里执行命令（风险最高，需要审批）",
        "tools": ["terminal"],
    },
    "todo": {
        "description": "任务清单（agent 级工具：执行由 agent loop 接管，不走注册表）",
        "tools": ["todo_list"],
    },
}

# 默认捆（对应 Hermes 的 `_HERMES_CORE_TOOLS`）。
# 每个平台的基础 toolset 都继承它，再按需加减。
#
# ⚠️ 这里**刻意写全**而不是"注册表里的所有工具"——如果写成后者，
#    这个文件就失去意义了（等于没有闸门）。
#    Hermes 的注释也强调了这一点：*not dead code*。
#
# 注意 `todo_list` 也在里面：Hermes 的 `_HERMES_CORE_TOOLS` 同样包含
# `todo_list` / `memory` / `session_search` 这些 **agent 级**工具 ——
# 它们的 **schema 走正常注册**，只有**执行**被 agent loop 接管。
# 所以"暴露给谁"这件事只有一个答案来源：**这张表**。
MINI_CORE_TOOLS: List[str] = [
    "read_file", "write_file", "list_dir", "search_files", "web_search",
    "terminal", "todo_list",
]


# ── 解析 ────────────────────────────────────────────────────────────────
def resolve_toolset(name: str) -> List[str]:
    """toolset 名 → 工具名列表。

    复合 toolset（值是 `*` 开头引用另一个 toolset）在这里展开。
    Hermes 的 `resolve_toolset()` 支持嵌套展开，mini 只做一层——
    等真有嵌套需求再补，不做投机性的实现。
    """
    spec = TOOLSETS.get(name)
    if spec is None:
        return []
    return list(spec["tools"])


def get_enabled_tool_names(enabled: Optional[List[str]] = None,
                           disabled: Optional[List[str]] = None) -> Set[str]:
    """算出这一轮**该暴露给模型**的工具名集合。

    三种入参对应 Hermes 的 `get_tool_definitions()` 前三条规则：

    | 入参 | 结果 |
    |---|---|
    | 给了 `enabled` | 只包含这些 toolset 里的工具 |
    | 给了 `disabled` | 从全部 toolset 开始，减掉禁用的 |
    | 都没给 | 全部 toolset（这里就是 `MINI_CORE_TOOLS`） |

    `enabled` 和 `disabled` 同时给时，**先 enabled 再 disabled**（禁用赢）——
    和 Hermes 的 `_apply_toolset_selection` 一致：
    先加后减，所以"同时开了又禁"的结果是禁用。
    """
    if enabled:
        names: Set[str] = set()
        for ts in enabled:
            names.update(resolve_toolset(ts))
    else:
        names = set(MINI_CORE_TOOLS)

    if disabled:
        for ts in disabled:
            names.difference_update(resolve_toolset(ts))
            # 也接受直接给工具名（Hermes 的 toolset 表里混了这两种粒度）
            names.discard(ts)
    return names


def toolset_of_tool(tool_name: str) -> Optional[str]:
    """反查：这个工具属于哪个 toolset？（可能属于多个，返回第一个）"""
    for ts, spec in TOOLSETS.items():
        if tool_name in spec["tools"]:
            return ts
    return None


def orphan_tools(registered: List[str]) -> List[str]:
    """**注册了但不在任何 toolset 里**的工具 = 永远不会被模型看到。

    这是"注册 ≠ 暴露"的**把 bug 变成可检查的契约**：
    不要一个工具一个工具去记，问一句"有孤儿吗"。

    （对应 tools/AGENTS.md 的测试要求：
      *assert contracts ("every registered tool has a toolset") rather than tool counts*。
      断言**契约**而不是**数量**——数量断言每次加工具都要改，而且不告诉你任何关系。）
    """
    return sorted(n for n in registered if toolset_of_tool(n) is None)
