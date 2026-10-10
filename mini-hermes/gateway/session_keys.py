"""会话键：**所有路由身份的唯一来源**（对应 Hermes 的 `gateway/session.py`）。

上游文档里那句 *Never construct session keys manually* 不是风格建议：
会话键同时是**路由键**（消息归哪个会话）、**并发键**（哪个 agent 正在跑）、
**存储键**（`sessions.session_id`）。任何一处手拼出不一样的键，
就会得到一个"看起来在工作、其实开了第二个会话"的静默错误。

所以这里只有一个函数 `build_session_key()`，谁都从它拿键。

键的布局（照真实版）：

    <命名空间>:<平台>:<聊天类型>[:<chat_id>][:<thread_id>]

真实版的完整布局还带 Slack 的 `scope_id` 和群聊的用户段，mini 不抄那两段
（见第 7 章差距清单 B10）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

DEFAULT_NAMESPACE = "main"


@dataclass
class SessionSource:
    """一条消息的**来源**（对应真实版的 `SessionSource`）。

    网关里所有"这条消息从哪来"的问题都靠它回答 —— 授权、路由、回复、钩子
    都只认这个对象，而不是各自去翻原始事件。
    """

    platform: str                      # "console" / "telegram" / ...
    chat_id: str                       # 平台里的频道/会话标识
    chat_type: str = "dm"              # "dm" | "group" | "thread"
    user_id: str = ""                  # 发送者
    thread_id: str = ""                # 话题/线程
    # ★ 第 9 章新增：这条消息**归哪个 profile**（网关在入站口盖的章）。
    #   真实版 `SessionSource.profile` 的 mini 版 —— 它有两个来源，顺序固定：
    #   ① 路由表命中（`profile_routing.match_profile_route`，第 9 章 A7）；
    #   ② 没命中 → 收到它的那个 **bot 自己的 profile**（A8：适配器的 `_owner_profile`）。
    #   为什么必须存在：会话键、锁、缓存全按 profile 分桶 —— 没有这个章，
    #   "一个进程里两个 bot"会算出**同一个键**，于是互相排队、互相覆盖。
    profile: Optional[str] = None
    metadata: Dict[str, object] = field(default_factory=dict)

    def describe(self) -> str:
        who = self.user_id or "?"
        return f"{self.platform}/{self.chat_type}/{self.chat_id} (user={who})"


def bot_profile_key(name: Optional[str]) -> Optional[str]:
    """把 profile 名归一成"bot 归属"键：默认 profile ↔ `None`（其余原样）。

    真实版逐字（`gateway/profile_routing.py:101-104`）：

        ``None`` for the default profile, else the profile name (mirrors ``set_owner_profile``).

    ★ 为什么要有这一层：判等时**不能出现两个写法**（`None` / `"default"`）——
      否则"默认 profile 的 bot"与"没标注的 bot"会被判成不同，路由的 `bot_profile`
      判别项就失效了。真实版因此让会话键、适配器 owner、路由三处**共用同一个归一函数**。
    """
    key = (name or "").strip()
    return None if not key or key == "default" else key


def session_key_namespace(profile: Optional[str]) -> str:
    """会话键里的命名空间段。

    ★ 抄真实版 `gateway/session.py:625-634` 的三条规则：

    1. 默认 profile（None / "default"）→ `main`，**与历史上所有键逐字节一致**；
    2. 具名 profile → 它的名字，于是两个 profile 服务同一个聊天时**永不撞**；
    3. **一个字面就叫 `main` 的 profile 会被标成 `main~`** ——
       否则它会产出默认的命名空间，从而与默认 profile **共享一切**
       （路由索引、agent 缓存、存储）。`~` **不在 profile id 的字母表里**，
       所以这个标记形态**不可能**是别的 profile 的名字。

    第 3 条的手法值得记：**不靠约定，靠字母表** —— 用一个不可能作为合法值的字符做区分。
    """
    if profile is None or profile in ("", "default"):
        return DEFAULT_NAMESPACE
    if profile == DEFAULT_NAMESPACE:
        return f"{DEFAULT_NAMESPACE}~"
    return profile


def build_session_key(source: SessionSource, *,
                      profile: Optional[str] = None) -> str:
    """从来源构造会话键（**唯一入口**）。"""
    ns = session_key_namespace(profile)
    parts = [ns, source.platform, source.chat_type]

    chat_id = source.chat_id
    if not chat_id:
        # ★ 没有 chat_id 就退回发送者 id，再退回"每平台一个"。
        #   真实版在 DM 分支里写得很直白：不这样退，"所有没有 chat_id 的 DM
        #   会共享一个 agent"。
        chat_id = source.user_id
    if chat_id:
        parts.append(chat_id)
    if source.thread_id:
        parts.append(source.thread_id)
    return ":".join(parts)
