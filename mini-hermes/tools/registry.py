"""工具中央注册表。

对应 Hermes 的 tools/registry.py：
- 零依赖（谁也不 import，只被 import）
- 工具模块在 **import 时自注册**（靠 @register_tool 装饰器）
- 注册表只存"名字 → 函数 + schema + 元数据"，不关心工具怎么实现

这是 Hermes 依赖链的第一环：
    tools/registry.py  ←  tools/*.py  ←  model_tools.py  ←  run_agent.py
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

# 名字 → 工具条目
_REGISTRY: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()


def register_tool(
    name: str,
    *,
    description: str,
    parameters: Dict[str, Any],
    toolset: str = "core",
    interactive: bool = False,
    requires_approval: bool = False,
    danger_check: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Callable:
    """把函数注册成一个模型可调用的工具。

    interactive=True 的工具会**强制整批工具串行执行**
    （Hermes 里 clarify 就是这个待遇：它要弹窗等用户，不能和别人并发）。

    requires_approval + danger_check：工具自己决定"这次调用危不危险"
    （terminal 传进来的是 looks_dangerous，只有 rm -rf 这类才拦，
    不是说每条命令都要弹窗 —— 那样 agent 就没法用了）。
    """

    def decorator(fn: Callable) -> Callable:
        with _LOCK:
            _REGISTRY[name] = {
                "name": name,
                "fn": fn,
                "toolset": toolset,
                "interactive": interactive,
                "requires_approval": requires_approval,
                "danger_check": danger_check,

                # OpenAI function-calling schema，直接塞进 API 请求的 tools 字段
                "schema": {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": parameters,
                    },
                },
            }
        return fn

    return decorator


def get(name: str) -> Optional[Dict[str, Any]]:
    return _REGISTRY.get(name)


def tool_names() -> List[str]:
    return sorted(_REGISTRY)


def schemas(names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """收集要发给模型的工具 schema，顺序稳定（利于 prompt 缓存）。"""
    picked = tool_names() if names is None else [n for n in names if n in _REGISTRY]
    return [_REGISTRY[n]["schema"] for n in picked]


def is_interactive(name: str) -> bool:
    entry = _REGISTRY.get(name)
    return bool(entry and entry["interactive"])


def requires_approval(name: str) -> bool:
    entry = _REGISTRY.get(name)
    return bool(entry and entry["requires_approval"])


def looks_approval_needed(name: str, args: Dict[str, Any]) -> bool:
    """这次调用要不要弹窗问用户？

    只有注册时声明了 requires_approval 且带 danger_check 的工具才可能返回 True，
    并且最终由工具自己的 danger_check 说了算。
    """
    entry = _REGISTRY.get(name)
    if not entry or not entry["requires_approval"]:
        return False
    checker = entry.get("danger_check")
    if checker is None:
        return True  # 声明了要审批但没给判断函数 → 一律问
    try:
        return bool(checker(args or {}))
    except Exception:  # noqa: BLE001 - 判断函数自己炸了，保守起见走审批
        return True
