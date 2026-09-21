"""工具发现与分发。

对应 Hermes 的 model_tools.py：
- discover_builtin_tools()：把 tools/ 下每个模块 import 一遍，触发它们自注册
- get_tool_schemas()：收集要发给模型的 schema
- handle_function_call()：**唯一的工具调用入口** —— 名字 → 查注册表 → 执行

Hermes 里这个文件是 tools/ 和 run_agent.py 之间的中介层。
run_agent 永远不问"这个工具怎么实现"，只问 handle_function_call。
"""
from __future__ import annotations

import importlib
from typing import Any, Dict, List, Optional

from tools import registry

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


def get_tool_schemas(include_agent_level: bool = True) -> List[Dict[str, Any]]:
    """发给模型的 tools 字段。

    ⚠️ Hermes 的铁律：**每次 API 调用都发全部工具**。
    这就是为什么新加一个核心工具的门槛极高（见 AGENTS.md 的 "narrow waist"）。
    """
    schemas = registry.schemas()
    if include_agent_level:
        from tools.todo import AGENT_LEVEL_TOOLS

        schemas.extend(AGENT_LEVEL_TOOLS.values())
    return schemas


def handle_function_call(name: str, args: Dict[str, Any], task_id: Optional[str] = None) -> str:
    """执行一次工具调用，永远返回**字符串**（进消息历史的东西必须是文本）。

    工具内部抛的任何异常都在这里被兜住，变成给模型看的错误文本——
    模型看到错误能自己改，看到 traceback 只会浪费上下文。
    """
    entry = registry.get(name)
    if entry is None:
        return f"[error] 未知工具：{name}（可用：{', '.join(registry.tool_names())}）"
    try:
        result = entry["fn"](**(args or {}))
    except TypeError as exc:
        return f"[error] 参数不匹配：{exc}"
    except Exception as exc:  # noqa: BLE001
        return f"[error] 工具执行异常：{type(exc).__name__}: {exc}"
    return result if isinstance(result, str) else str(result)
