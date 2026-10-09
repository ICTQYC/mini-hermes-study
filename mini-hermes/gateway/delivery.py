"""出站投递（对应 Hermes 的 `gateway/delivery.py`）。

真实版有四种投递，mini 抄两种（消费者存在的两种）：

| 类型 | mini | 说明 |
|---|---|---|
| **直接回复** | ✅ | 发回**发起它的那个聊天** |
| 主频道投递 | ❌ | 需要"主频道"配置 + cron 这类后台产出的消费者 |
| **指定目标投递** | ✅ | 发送方显式给 `platform:chat_id` |
| 跨平台投递 | ⚠️ 半 | mini 的 registry 里只有一个适配器，所以"跨平台"退化成"指定平台" |

★ 一条真实版的细节值得记：**cron 任务的投递不会镜像进网关会话历史** ——
它们只活在自己的 cron 会话里。原因不是省事，而是**消息角色交替**：
把后台产出塞进用户会话，会让两条同角色消息相邻（第 2 章那条不变量）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from gateway.session_keys import SessionSource


class DeliveryError(RuntimeError):
    pass


def parse_target(target: str) -> Tuple[str, str]:
    """把 `"console:chat-1"` 解析成 `("console", "chat-1")`。

    ★ 显式目标必须**自带平台**，不允许"只给一个 chat id"——
    否则投递要靠猜平台，而猜错的后果是**把消息发给另一个平台的同号聊天**。
    """
    if ":" not in target:
        raise DeliveryError(f"目标必须写成 platform:chat_id，收到 {target!r}")
    platform, _, chat_id = target.partition(":")
    platform, chat_id = platform.strip(), chat_id.strip()
    if not platform or not chat_id:
        raise DeliveryError(f"目标不完整：{target!r}")
    return platform, chat_id


class Delivery:
    def __init__(self, adapters: Dict[str, Any]):
        self.adapters = adapters
        self.sent: list = []                      # 观察面：测试里断言用

    def deliver(self, source: SessionSource, text: str,
                target: Optional[str] = None) -> str:
        """投递一条出站消息。返回实际用的 `platform:chat_id`。"""
        if target:
            platform, chat_id = parse_target(target)
        else:
            platform, chat_id = source.platform, source.chat_id

        adapter = self.adapters.get(platform)
        if adapter is None:
            raise DeliveryError(f"没有 {platform!r} 的适配器（已注册：{sorted(self.adapters)}）")
        if not chat_id:
            raise DeliveryError("投递缺少 chat_id（既没给目标，来源里也没有）")

        adapter.send(chat_id, text, metadata={"session_key": getattr(source, "platform", "")})
        self.sent.append((platform, chat_id, text))
        return f"{platform}:{chat_id}"
