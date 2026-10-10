"""所有平台适配器共享的那一层（对应 Hermes 的 `gateway/platforms/base.py`）。

这里装的是网关最容易被忽略、却最不能省的东西：**第 1 级消息守卫**。

问题：一个会话的上一轮还在跑（可能正在等 LLM、或在等终端命令确认），
这时用户又发来一条消息。三条路都不能走：

- 直接并发跑第二轮 → 两条消息交错写进同一个会话历史（角色交替直接坏掉）；
- 静默丢掉 → 用户以为消息发了；
- 无脑排队但不通知 → 用户点了"停止"却排在一个 10 分钟的任务后面。

真实版（`platforms/base.py:1884-1885` / `:3583-3616`）的答案是**三件套**：

    _active_sessions   这个会话有没有主？（guard Event）
    _pending_messages  忙的时候消息去哪（★ 第 8 章起 = **单槽**：后到覆盖先到）
    _session_tasks     谁是主？主死了怎么办（★ **owner task + 陈旧检测**）

★ 第 8 章另加**第四件**：溢出 FIFO（用户显式 `/queue` 的，一件都不能少）。
  单槽 + 溢出组成两层队列，见 `gateway/run_busy.py`。

★ 最后那件最容易被漏掉：进程是长驻的，一次崩溃/超时会让 guard 永远挂着，
  那个会话就**永久卡死**。所以真实版有一个 `_session_task_is_stale()`
  （`:3585`），发现"记的 owner 任务已经结束"就自愈放锁。

mini 用线程实现同一套语义（真实版是 asyncio）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from gateway.session_keys import SessionSource
from gateway.run_busy import SessionQueues


@dataclass
class MessageEvent:
    """入站消息的**归一化形状**（对应真实版 `MessageEvent`）。

    ★ 每个平台适配器负责把自家的原始事件翻译成它 —— 翻译完之后，
    下游（守卫、授权、路由、回合）**再也不用知道平台差异**。
    """

    text: str
    source: SessionSource
    session_key: str = ""                 # 由 runner 用 build_session_key 填（唯一来源）
    message_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class BasePlatformAdapter:
    """适配器基类：连接、发消息、以及第 1 级守卫。"""

    name = "base"

    def __init__(self, on_message: Optional[Callable[[MessageEvent], Any]] = None,
                 on_interrupt: Optional[Callable[[str], None]] = None,
                 session_key_fn: Optional[Callable[[SessionSource], str]] = None,
                 owner_profile: Optional[str] = None):
        # 网关 runner 的入站入口（真装配时由 runner 注入）
        self._on_message = on_message
        # 忙时通知：让 runner 去 `agent.interrupt()`
        #   ★ 为什么这个回调要存在：适配器**不知道 agent 是什么**（它只认 MessageEvent）。
        #     真实版里这一步是"设置中断事件"，由网关侧把它路由给正在跑的 agent。
        self._on_interrupt = on_interrupt
        # ★ 会话键函数**由 runner 注入**，适配器绝不自己拼键。
        #   第 1 级守卫也要用键来分会话，所以键必须在**进守卫之前**就有；
        #   但"命名空间是哪个 profile"只有网关知道 —— 于是注入的是函数，不是值。
        self._session_key_fn = session_key_fn
        # ★ 第 9 章 A8：**这个适配器是哪个 profile 的 bot**。
        #   真实版 `run_adapters.py:1004-1100` 给每个 profile 建适配器时都会
        #   `set_owner_profile(name)`；`None` = 默认 profile 的 bot（归一见 `bot_profile_key`）。
        #   为什么必须有：多路复用下一个进程里可能有好几个 bot，**同一个人可以同时跟两个
        #   bot 说话**；没有这个标注，它们会算出**同一个会话键**（共享一条 lane = 互相顶掉）。
        from gateway.session_keys import bot_profile_key

        self._owner_profile: Optional[str] = bot_profile_key(owner_profile)
        # ---- 第 1 级守卫三件套 ----
        self._active_sessions: Dict[str, threading.Event] = {}
        self.queues = SessionQueues()               # 第 8 章：单槽（覆盖）+ 溢出 FIFO（不合并）
        self._on_overwrite: Optional[Callable[[str, MessageEvent], None]] = None
        self._session_tasks: Dict[str, threading.Thread] = {}
        # 忙时通知 agent 中断（真实版是 asyncio.Event，mini 用同语义的线程事件）
        self._interrupt_events: Dict[str, threading.Event] = {}
        self._connected = False
        self._log: List[str] = []            # 给人看的适配器日志（测试里当观察面）
        self._last_results: Dict[str, Any] = {}   # 会话键 → runner 给的结论

    # ------------------------------------------------------------------ #
    # 生命周期 / 出站（子类必须实现）
    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def send(self, chat_id: str, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # 第 1 级守卫
    # ------------------------------------------------------------------ #
    def set_owner_profile(self, name: Optional[str]) -> None:
        """标注"这个适配器是哪个 profile 的 bot"（真实版 `run_adapters.py:1115` 同名）。"""
        from gateway.session_keys import bot_profile_key

        self._owner_profile = bot_profile_key(name)

    def _session_key_profile(self, source: SessionSource):
        """这条消息该进哪条 lane（= 键的命名空间）—— 真实版 `session.py:906-916` 的三段解析。

        逐字顺序（真实版注释）：

            ① `source.profile`（**路由表命中**时由 `_stamp_routed_profile` 盖的章）
            ② `self._owner_profile`（收这条消息的 bot 自己的 profile）
            ③ 都没有 → `None`（= 默认 profile 的命名空间）

        ★ 第 ③ 条顺带回答了"默认 profile 的键为什么一直叫 `main`"：`None` 在
        `build_session_key` 里映射成 `main`，所以单 profile 时期的历史键**逐字节不变**。
        """
        return source.profile or self._owner_profile

    def handle_message(self, event: MessageEvent) -> str:
        """所有入站的**唯一入口**。返回这一条被怎么处理了（便于测试与观察）。"""
        # ★ A8：入站口盖"这条消息归哪个 profile"的章（真实版是适配器入口那一步）。
        #   顺序：**路由表命中**（第 9 章 A7，在 `run_inbound` 里做）→ 否则
        #   **收它的那个 bot 的 profile**（本适配器的 `_owner_profile`）。
        #   没有这一步，"一个进程里两个 bot"会算出同一个会话键。
        if not event.source.profile:
            event.source.profile = self._session_key_profile(event.source)
        # ★ 会话键必须在**进守卫之前**就有：守卫是按会话分锁的。
        #   键本身只由 build_session_key 产出（通过 runner 注入的函数），
        #   适配器不许自己拼。
        if not event.session_key and self._session_key_fn is not None:
            event.session_key = self._session_key_fn(event.source)
        key = event.session_key
        guard = self._active_sessions.get(key)

        # ★ 自愈：主已经退出（崩溃 / 超时 / 线程结束）→ 放锁。
        #   不做这件事，一次异常就会让这个会话**永久卡死**。
        if guard is not None and self._session_task_is_stale(key):
            self._release_session_guard(key)
            guard = None

        if guard is None:
            return self._start_processing(event)

        # 会话忙 —— 两类入站的处置**不同**：
        from hermes_cli.commands import resolve_command

        resolved = resolve_command(event.text)

        if resolved is not None:
            # ① **命令**：一律内联交给第 2 级守卫（runner）。
            #    为什么不在这一级就按策略处置：这一级只知道"忙"，
            #    而"这个命令在忙时该执行、该拒、还是该先打断"是**命令的属性**
            #    （`CommandDef.busy_policy`），只有第 2 级拿着命令表才判得准。
            #    而且"拒绝"也得有人把话说出口 —— 那是 runner 的事（它负责用户可见的话）。
            self._note(f"[守卫1] {key} 忙 → 命令 {resolved[0]!r} 内联交第 2 级")
            return self._dispatch_inline(event)

        # ② **普通消息**：进**单槽**（后到覆盖先到）+ 通知正在跑的 agent 打断 ——
        #    这样用户新发的话能尽快被处理，而不是等旧回合跑完。
        #    ★ 第 8 章改动：第 7 章这里是 `list` + `append`（普通消息也 FIFO 排队）——
        #      那等于"我改主意了"这个意图表达不出来（发十条就挨个跑十条）。
        #      真实版 `run_busy.py:71` 是**赋值**：`adapter._pending_messages[key] = ...`，
        #      单槽、后到覆盖先到。
        #    ★ 覆盖掉的那条**必须告诉用户**（那是他写的字，静默丢比拒绝更糟）。
        overwritten = self.queues.keep_latest(event)
        # ★ 回执（真实版 `run_busy.py:55` 逐字同名）：标上"网关已经接受它了"，
        #   交付层据此**不重复**回执 —— "调用 ≠ 接受"，这是接受的那一刻。
        setattr(event, "_gateway_accepted", True)
        ev = self._interrupt_events.get(key)
        if ev is not None:
            ev.set()
        if self._on_interrupt is not None:
            self._on_interrupt(key)
        if overwritten is not None and self._on_overwrite is not None:
            self._on_overwrite(key, overwritten)
        self._note(f"[守卫1] {key} 忙 → 进单槽"
                   + ("（覆盖了上一条排队消息）" if overwritten is not None else "")
                   + f"（深度 {self.queues.depth(key)}）")
        return "queued"

    def _start_processing(self, event: MessageEvent) -> str:
        key = event.session_key
        guard = threading.Event()
        self._active_sessions[key] = guard
        self._interrupt_events.setdefault(key, threading.Event()).clear()
        task = threading.Thread(target=self._process_background, args=(event, key),
                                name=f"gw-{key}", daemon=True)
        self._session_tasks[key] = task
        task.start()
        return "started"

    def _process_background(self, event: MessageEvent, key: str) -> None:
        try:
            if self._on_message is not None:
                self._last_results[key] = self._on_message(event)
        except Exception as exc:                      # 后台线程不许默默死掉
            self._note(f"[守卫1] 后台处理异常：{exc!r}")
        finally:
            # ★ 顺序不能反：**先放锁，再放排队消息**。
            #   反过来的话，被放出来的那条会看到"会话还忙"（锁还被自己握着），
            #   于是它又被排回队列 —— 队列就此卡死，且不报错。
            self._release_session_guard(key)
            self._drain_pending(key)

    def _dispatch_inline(self, event: MessageEvent) -> str:
        if self._on_message is not None:
            # 内联路径也把结论收进同一个观察面（不然后台/内联两条路的"结论"取法不一致）
            self._last_results[event.session_key] = self._on_message(event)
        return "inline"

    def _drain_pending(self, key: str) -> None:
        """一轮结束后，把队列里的**下一条**放出来。

        ★ 顺序（真实版 `_promote_queued_event`）：**先溢出 FIFO**（用户显式 `/queue` 的，
        一件都不能少）**再单槽**（最新的那条普通消息）。见 `gateway/run_busy.py`。

        ★ 为什么只放一条：一次只跑一轮。放出去的那条跑完会再 drain 一次 ——
        这样"排队"是**队列**而不是"一次性批量"。
        """
        nxt = self.queues.promote(key)
        if nxt is None:
            return
        self._note(f"[守卫1] {key} 空闲 → 放出排队消息（剩 {self.queues.depth(key)} 条）")
        self.handle_message(nxt)

    def _dispatch_inline_reply(self, event: MessageEvent) -> str:
        """内联分发（真实版 `_dispatch_inline_reply` 的 mini 版）。"""
        return self._dispatch_inline(event)

    def _release_session_guard(self, key: str, guard: Optional[threading.Event] = None) -> None:
        cur = self._active_sessions.get(key)
        if guard is not None and cur is not guard:
            return                                    # 别放掉别人的锁
        self._active_sessions.pop(key, None)
        self._session_tasks.pop(key, None)

    def _session_task_is_stale(self, key: str) -> bool:
        """记的 owner 线程已经退出 → 这把锁是**陈旧的**（真实版 `:3585`）。

        ★ 真实版 docstring 特别强调：**没有 owner 记录时不算陈旧** ——
        否则"在 handle_message 之外装的守卫"（测试里就这么干）会被误判自愈。
        """
        task = self._session_tasks.get(key)
        if task is None:
            return False
        return not task.is_alive()

    def is_busy(self, session_key: str) -> bool:
        return session_key in self._active_sessions

    def pending_count(self, session_key: str) -> int:
        """队列深度 = 单槽（0/1）+ 溢出（真实版 `_queue_depth` 同算法）。"""
        return self.queues.depth(session_key)

    # ------------------------------------------------------------------ #
    # 观察面（★ mini 侧新增：真实版靠事件回调/轮询状态。
    #   它只是"把后台线程的结论取出来"，不参与任何判定逻辑）
    # ------------------------------------------------------------------ #
    def last_result(self, session_key: str) -> Any:
        return self._last_results.get(session_key)

    def wait_idle(self, session_key: str, timeout: float = 60.0) -> bool:
        """等这个会话空下来（包括把队列消费完）。返回是否在超时前完成。"""
        import time as _time

        deadline = _time.time() + timeout
        while _time.time() < deadline:
            task = self._session_tasks.get(session_key)
            busy = self.is_busy(session_key)
            if not busy and (task is None or not task.is_alive()):
                return True
            _time.sleep(0.02)
        return False

    def _note(self, text: str) -> None:
        self._log.append(text)
