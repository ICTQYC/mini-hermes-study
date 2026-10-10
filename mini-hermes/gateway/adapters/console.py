"""console 适配器：stdin/stdout 的一个"平台"（对应真实版 `plugins/platforms/<名字>/adapter.py`）。

★ mini 为什么要有一个真适配器，而不是写个假 mock：

真实版那些适配器难抄的原因是**需要 SDK 和凭据**，不是**逻辑复杂**。
把平台换成"一行输入 = 一条消息"，那些逻辑（守卫、授权、路由、交付、
忙时策略）就能在**零网络**的前提下被真跑一遍 ——
文档里写的每一条都能实测，而不是"理论上应该这样"。

两种用法：

- `serve_forever()`：真的从 stdin 读（`./mini gateway run` 用这个）；
- `feed(text, user_id=...)`：程序化喂一条（测试 / 演示脚本用，
  **走的是同一条 `handle_message` 路径**，不是旁路）。
"""
from __future__ import annotations

import sys
import threading
from typing import Any, Dict, Optional

from gateway.adapters.base import BasePlatformAdapter, MessageEvent
from gateway.session_keys import SessionSource

DEFAULT_CHAT_ID = "console-1"
DEFAULT_USER_ID = "local"


class ConsoleAdapter(BasePlatformAdapter):
    name = "console"

    def __init__(self, on_message=None, on_interrupt=None, session_key_fn=None, *,
                 chat_id: str = DEFAULT_CHAT_ID, user_id: str = DEFAULT_USER_ID,
                 echo: bool = True, prompt: str = "you> ",
                 owner_profile: Optional[str] = None,
                 profile_feed=None, multiplex_note: str = ""):
        super().__init__(on_message=on_message, on_interrupt=on_interrupt,
                         session_key_fn=session_key_fn, owner_profile=owner_profile)
        self.chat_id = chat_id
        self.user_id = user_id
        self.echo = echo
        self.prompt = prompt
        # ★ 第 9 章：`@<profile> 文本` 的落点（由 runner 注入；None = 单 profile 模式不使用）
        self._profile_feed = profile_feed
        self.multiplex_note = multiplex_note
        self._out: list = []
        self._stdin_thread: Optional[threading.Thread] = None

    # ---------------- 出站 ----------------
    def send(self, chat_id: str, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        self._out.append(text)
        if self.echo:
            # ★ 打印时必须**先起一行**：用户的输入和 agent 的输出挤在一行，
            #   日志就没法读了（真实版的流式交付解决的是同一类问题）。
            print(f"\n{text}\n", flush=True)

    def outbox(self):
        return list(self._out)

    # ---------------- 入站 ----------------
    def make_event(self, text: str, *, user_id: Optional[str] = None,
                   chat_id: Optional[str] = None) -> MessageEvent:
        """构造一条入站事件（**会话键由 runner 统一算**，适配器不碰）。"""
        src = SessionSource(
            platform=self.name,
            chat_id=chat_id or self.chat_id,
            chat_type="dm",
            user_id=user_id or self.user_id,
        )
        return MessageEvent(text=text, source=src)

    def feed(self, text: str, *, user_id: Optional[str] = None,
             chat_id: Optional[str] = None) -> str:
        return self.handle_message(self.make_event(text, user_id=user_id, chat_id=chat_id))

    def serve_forever(self) -> None:
        """从 stdin 逐行读；EOF（Ctrl-D）就退出。

        ★ 第 9 章：多路复用下多一条语法 —— `@<profile> <文本>` 表示
        **"这条消息是那个 profile 的 bot 收到的"**（真实版的 `bot_profile` 判别项）。
        没有 `@` 前缀的行照旧 = 当前 profile 的 bot 收到。

        没有那个 profile 时**说出来**，不许静默丢（真实版的"静默死通道"纪律）。
        """
        print("（console 适配器已连接；输入一行 = 一条消息，/help 看命令，Ctrl-D 退出）",
              flush=True)
        if self._profile_feed is not None and self.multiplex_note:
            print(self.multiplex_note, flush=True)
        for line in sys.stdin:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            if self.prompt and self.echo:
                print(f"{self.prompt}{line}", flush=True)
            routed = self._maybe_feed_profile(line)
            if routed:
                continue
            self.feed(line)

    def _maybe_feed_profile(self, line: str) -> bool:
        """`@<profile> 文本` → 喂给那个 profile 的适配器。返回是否已处理。"""
        if not line.startswith("@"):
            return False
        head, _, rest = line[1:].partition(" ")
        name = head.strip()
        if not name or not rest.strip():
            return False
        if self._profile_feed is None:
            print(f"（`@{name}` 需要网关在跑多路复用；这条按普通消息处理）", flush=True)
            return False
        if self._profile_feed(name, rest):
            return True
        # ★ 静默丢是最坏的：说清楚，并告诉他怎么看到有哪些
        print(f"⛔ 没有 profile {name!r}（或它没被服务）—— 这条消息**被丢弃**。"
              f"用 `./mini profile list` 看有哪些。", flush=True)
        return True
