"""网关授权（对应 Hermes 的 `gateway/authz_mixin.py` 里的那套分层判断）。

★ 这里唯一重要的一条：**默认拒绝**（fail-closed）。

真实版是五层，顺序求值，最后落到"未授权 → 拒"：

    平台级全放行 → 平台白名单 → 私聊配对 → 全局全放行 → 默认拒

mini 抄四层（跳过配对，原因是单用户单平台没有第二个用户可配对，见差距清单 B3），
顺序一模一样。判断顺序**不能重排**：全放行要能压过白名单，否则运维改了全局开关
却发现被一条旧白名单挡住，只能靠猜。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, FrozenSet, Optional

from gateway.session_keys import SessionSource


def _split_env(value: Optional[str]) -> FrozenSet[str]:
    if not value:
        return frozenset()
    return frozenset(x.strip() for x in value.replace(";", ",").split(",") if x.strip())


@dataclass
class GatewayAuthz:
    """从一个**已解析的配置字典**读授权设置。

    ★ 为什么构造时要传字典而不是自己读 `os.environ`：
    第 7 章的多 profile 一节里，"配置来源"是每条消息都要**按当年 profile 重新解析**的
    东西。类里直接读环境变量，就等于把启动 profile 的配置钉死在对象里。
    """

    platform_allow_all: bool = False
    platform_allowlist: FrozenSet[str] = field(default_factory=frozenset)
    global_allow_all: bool = False
    pairing_store: Optional[Any] = None         # mini 不用；留着说明真实版这一层在哪

    @classmethod
    def from_config(cls, *, platform: str, allow_all: bool = False,
                    allowlist: str = "") -> "GatewayAuthz":
        return cls(
            platform_allow_all=allow_all,
            platform_allowlist=_split_env(allowlist),
            global_allow_all=False,
        )

    @classmethod
    def from_env(cls, platform: str) -> "GatewayAuthz":
        """从环境变量读（照真实版的 `<PLATFORM>_ALLOWED_USERS` / `_ALLOW_ALL_USERS` 命名）。"""
        upper = platform.upper()
        return cls(
            platform_allow_all=os.environ.get(f"{upper}_ALLOW_ALL_USERS", "") not in ("", "0", "false"),
            platform_allowlist=_split_env(os.environ.get(f"{upper}_ALLOWED_USERS")),
            global_allow_all=os.environ.get("MINI_GATEWAY_ALLOW_ALL_USERS", "") not in ("", "0", "false"),
        )

    def is_authorized(self, source: SessionSource) -> bool:
        """按固定顺序判：全放行 → 白名单 → 配对 → 全局放行 → **拒**。"""
        if self.platform_allow_all:
            return True
        if source.user_id and source.user_id in self.platform_allowlist:
            return True
        if self.pairing_store is not None:                      # pragma: no cover - mini 不启用
            if self._paired(source):                            # 真实版的第 3 层
                return True
        if self.global_allow_all:
            return True
        return False                                            # ★ 默认拒绝

    def _paired(self, source: SessionSource) -> bool:            # pragma: no cover
        try:
            return bool(self.pairing_store.is_paired(source.platform, source.user_id))
        except Exception:
            return False

    def explain(self, source: SessionSource) -> str:
        """给拒绝消息用：**告诉用户为什么被拒**（否则运维只能猜）。"""
        if self.platform_allow_all:
            return "该平台已开全放行"
        if self.platform_allowlist:
            return (f"用户 {source.user_id or '?'} 不在 {source.platform.upper()}_ALLOWED_USERS 里")
        if self.global_allow_all:
            return "全局全放行已开"
        return (f"默认拒绝：请设置 {source.platform.upper()}_ALLOWED_USERS="
                f"{source.user_id or '<你的用户 id>'}（或 {source.platform.upper()}_ALLOW_ALL_USERS=1）")
