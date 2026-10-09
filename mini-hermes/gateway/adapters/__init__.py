"""平台适配器（对应 Hermes 的 `gateway/platforms/`）。

真实版有 22 个适配器；mini 只有一个 **console**（stdin/stdout），
因为零依赖下它是唯一"真能端到端跑起来"的平台。但**接口形状照抄**：
`connect / disconnect / send + handle_message`。
"""
from gateway.adapters.base import BasePlatformAdapter, MessageEvent  # noqa: F401
from gateway.adapters.console import ConsoleAdapter  # noqa: F401

__all__ = ["BasePlatformAdapter", "MessageEvent", "ConsoleAdapter"]
