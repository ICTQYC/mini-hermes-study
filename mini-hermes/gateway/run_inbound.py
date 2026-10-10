"""入站处理：授权 → 命令分发 → 忙时拦截（对应 Hermes 的 `gateway/run_inbound.py`）。

这是网关真正的"咽喉"：所有平台的入站都从这里过。

流程（照真实版）：

    1. 算会话键（**唯一来源** build_session_key）
    2. 授权（默认拒绝）
    3. 解析斜杠命令 → **表驱动**分发（没有 if 链）
    4. 如果 agent 在跑 → 按 busy_policy 处置（内联 / 拒绝）
    5. 都不是 → 跑一轮
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from gateway.session_keys import build_session_key
from hermes_cli.commands import GATEWAY_COMMANDS, busy_policy_for, resolve_command


class InboundMixin:
    """网关的入站阶段。"""

    # ------------------------------------------------------------------ #
    # 唯一入口
    # ------------------------------------------------------------------ #
    def _handle_message(self, event) -> Optional[Dict[str, Any]]:
        """入站唯一入口：**先决定这条消息属于哪个 profile，再在那个 profile 的作用域里处理**。

        ★ A5（第 9 章）：作用域绑的是"profile 的**活动**"，不是"进程启动时那个 profile"。
        真实版的 `_profile_runtime_scope` 有 30+ 个调用点（适配器连接、入站预处理、
        回合、slash 命令、通知…），这里是最关键的那一处：**整条入站路径**。
        没有它，次级 profile 的消息会读/写**活动 profile** 的配置、密钥与会话库。
        """
        from gateway.profile_scope import profile_runtime_scope

        # 1. 会话键：**所有地方都从这一个函数拿**（不许手拼）。
        #    正常情况下适配器（第 1 级守卫）已经算好了；这里只是兜底复用。
        event.session_key = event.session_key or self._key_for_source(event.source)

        home = self._profile_home_for_source(event.source) or self.home
        # ★ 一律进作用域 —— **活动 profile 也进**。真实版就是这么做的（每一处缝都套）：
        #   嵌套同名作用域是无害的，但"只在次级 profile 才套"会留下一个真实的洞：
        #   这条路径上有**在回合之前**读凭据的地方（`_open_session` 要 provider spec），
        #   而 fail-closed 会当场抛 `UnscopedSecretError`（实测踩过）。
        with profile_runtime_scope(home):
            return self._handle_message_inner(event)

    def _handle_message_inner(self, event) -> Optional[Dict[str, Any]]:
        key = event.session_key

        # 2. 授权（默认拒绝）
        if not self.authz.is_authorized(event.source):
            self._reply(event, f"⛔ 未授权：{self.authz.explain(event.source)}",
                        target=self._explicit_target(event))
            self.hooks.emit("command", command="(denied)", session_key=event.session_key)
            return {"action": "denied"}

        resolved = resolve_command(event.text)
        key = event.session_key

        # 3. **忙时处置优先于命令分发**：
        #    ★ 顺序很关键。真实版的形状是"忙时有一条自己的可执行命令白名单
        #      （`_IDLE_COMMANDS` / `_PLAIN_COMMANDS`），不在白名单里的命令
        #      **只在空闲路径分发**"。
        #      如果反过来（先按命令表分发、再查忙不忙），`/sessions` 这种
        #      `busy_policy=reject` 的命令就会在 agent 跑着的时候照样执行 ——
        #      改一个正在被使用的会话的元数据，正是忙时策略要防的事。
        running = self._running_agents.get(key)
        if running is not None:
            return self._dispatch_busy(event, resolved)

        # 4. 空闲路径：斜杠命令 → 查表分发
        if resolved:
            name, args = resolved
            handler = self._command_handler_table().get(name)
            if handler is not None:
                self.hooks.emit("command", command=name, session_key=key, args=args)
                return handler(event, args)

        # 5. 跑一轮
        return self._run_turn_for_event(event)

    # ------------------------------------------------------------------ #
    # 表驱动分发
    # ------------------------------------------------------------------ #
    def _command_handler_table(self) -> Dict[str, Any]:
        """命令名 → 处理器（真实版 `_command_handler_table()` 的形状）。

        ★ 这里的关键是：**没有 `if cmd == "stop" ... elif cmd == "new" ...`**。
        加一个命令 = 在 `GATEWAY_COMMANDS` 加一条声明 + 在这里的表里加一行；
        分发逻辑一个字不改。
        """
        return {
            "new": self._cmd_new,
            "stop": self._cmd_stop,
            "status": self._cmd_status,
            "help": self._cmd_help,
            "approve": self._cmd_approve,
            "deny": self._cmd_deny,
            "queue": self._cmd_queue,
            "sessions": self._cmd_sessions,
        }

    # ------------------------------------------------------------------ #
    # 忙时处置（第 2 级守卫）
    # ------------------------------------------------------------------ #
    def _dispatch_busy(self, event, resolved) -> Dict[str, Any]:
        """agent 在跑，命令或消息该怎么处置。

        ★ 与第 1 级的分工：第 1 级只知道"这个会话忙"，**在适配器里**就挡下了；
        第 2 级知道**这个命令是什么**，所以能按 `busy_policy` 精细处置。
        两级都必要：只有第 1 级 → 无法区分命令；只有第 2 级 → 消息已经在后台排队了。
        """
        key = event.session_key
        if resolved is None:
            # ★ 到这里为止（普通消息）**不该发生**：第 1 级守卫已经把它排队了，
            #   而排队时会通知 runner 去打断正在跑的那一轮 ——
            #   用户不发命令时不会看到"已排队"这种回话，他只会看到自己那条消息
            #   稍后被处理（要查看队列用 `/queue`）。
            return {"action": "queued"}

        name, args = resolved
        policy = busy_policy_for(name)
        agent = self._running_agents.get(key)

        if policy == "interrupt_then_dispatch":
            if agent is not None:
                agent.interrupt()
                self._interrupted.append(key)
            handler = self._command_handler_table().get(name)
            return handler(event, args) if handler else {"action": "interrupted"}

        if policy == "dispatch":
            # 忙时也得能执行的命令（/approve /deny /status /help /queue）
            handler = self._command_handler_table().get(name)
            if handler is not None:
                return handler(event, args)
            return {"action": "dispatched"}

        # policy == "reject"（默认）：**必须把拒绝说出口**，否则用户以为命令生效了
        self._busy_rejections.append((key, name))
        self._reply(event,
                    f"⏳ Agent 正在跑 —— `/{name}` 不能在回合中间执行。"
                    f"（`/stop` 可以打断）",
                    target=self._explicit_target(event))
        return {"action": "busy_rejected", "command": name}

    # ------------------------------------------------------------------ #
    # 命令处理器（每个都小到一句话能说清）
    # ------------------------------------------------------------------ #
    def _cmd_help(self, event, args: str) -> Dict[str, Any]:
        lines = ["可用命令（网关侧）："]
        for d in sorted(GATEWAY_COMMANDS, key=lambda c: c.name):
            al = f"（别名：{'、'.join(d.aliases)}）" if d.aliases else ""
            lines.append(f"  /{d.name:<9} {d.description}{al}  [忙时：{d.busy_policy}]")
        self._reply(event, "\n".join(lines), target=self._explicit_target(event))
        return {"action": "help"}

    def _cmd_stop(self, event, args: str) -> Dict[str, Any]:
        key = event.session_key
        agent = self._running_agents.get(key)
        if agent is None:
            self._reply(event, "（没有正在跑的回合）", target=self._explicit_target(event))
            return {"action": "stop_noop"}
        agent.interrupt()
        self._interrupted.append(key)
        self._reply(event, "⏹ 已请求打断。", target=self._explicit_target(event))
        return {"action": "interrupted"}

    def _cmd_new(self, event, args: str) -> Dict[str, Any]:
        key = event.session_key
        # ★ 第 8 章：`/new` 是**显式边界** —— 交给状态机走 force_new 那条路
        #   （它会结束旧会话、开新会话、打上 is_fresh_reset），不再由命令处理器自己"先 pop 再开"。
        previous = self._session_ids.get(key)
        spec = self._provider_spec()
        decision = self._router().reset(
            key, source=f"gateway:{event.source.platform}",
            model=spec.get("model"),
            model_config={k: spec.get(k) for k in ("provider", "api_mode")},
        )
        self._session_ids[key] = decision.entry.session_id
        if previous:
            # ★ **会话轮转 → 缓存里的 agent 必须立刻放掉**（真实版 `_evict_cached_agent`）。
            #   两条路都会轮转：① 自动重置（挂起/已结束）走 `_open_session`；
            #   ② 显式 `/new` 走这里。**两处都得放** —— 漏一处，缓存的 agent 就会把
            #   新会话的消息写进旧会话的转写本（缓存真正危险的地方）。
            self._agent_cache.evict(key, reason="session_rotated")
            # ★ 显式边界要**两边都报**：旧会话的 `session:end` + 新会话的 `session:reset`。
            #   第 7 章时这一步是"先 end 再开"，第 8 章把决策交给状态机之后，
            #   钩子仍然由命令处理器发（钩子是"事件通知"，不是"状态决策"）。
            self._finalize_session(previous, "session_reset")
            self.hooks.emit("session:reset", session_key=key,
                            session_id=decision.entry.session_id, previous=previous)
        self._reply(event, f"🆕 新会话已开始（{decision.entry.session_id}）。",
                    target=self._explicit_target(event))
        return {"action": "reset", "session_id": decision.entry.session_id,
                "previous": previous}

    def _cmd_status(self, event, args: str) -> Dict[str, Any]:
        key = event.session_key
        sid = self._session_ids.get(key) or "(尚未开始)"
        adapter = self.adapters.get(event.source.platform)
        busy = bool(adapter and adapter.is_busy(key))
        pend = adapter.pending_count(key) if adapter else 0
        db = self._db()
        n = db.count_messages(sid) if isinstance(sid, str) and not sid.startswith("(") else 0
        text = "\n".join([
            f"会话键   {key}",
            f"会话 id  {sid}",
            f"消息数   {n}",
            f"agent    {'正在跑' if key in self._running_agents else '空闲'}",
            f"排队     {pend} 条",
            f"网关 pid {self.pid or '(未启动)'}  home={self.home}",
        ])
        self._reply(event, text, target=self._explicit_target(event))
        return {"action": "status"}

    def _cmd_queue(self, event, args: str) -> Dict[str, Any]:
        """`/queue` = 看队列；`/queue <文本>` = **显式排队**（这件也必须做）。

        ★ 两种意图两种结构（第 8 章）：看的是**单槽 + 溢出**两层；
        带文本入队的那条进**溢出 FIFO**（不合并、按序处理）——
        而普通消息走的是单槽（后到覆盖先到）。用户想"都要做"就用这个命令。
        """
        adapter = self.adapters.get(event.source.platform)
        if adapter is None:
            self._reply(event, "（没有适配器，无法读取队列）", target=self._explicit_target(event))
            return {"action": "queue", "count": 0}
        queues = self._adapter_queues(adapter)
        key = event.session_key

        text = (args or "").strip()
        if text:
            queued = self._make_event_like(event, text)
            depth = queues.enqueue_explicit(queued)
            self._reply(event, f"📥 已排队（第 {depth} 件）：{text[:60]}\n"
                               f"它会**依次**跑完，不会被后来的消息顶掉。",
                        target=self._explicit_target(event))
            return {"action": "queue_enqueue", "count": depth, "text": text}

        depth = queues.depth(key)
        if depth == 0:
            self._reply(event, "（队列为空）", target=self._explicit_target(event))
            return {"action": "queue", "count": 0}
        lines = [f"排队中 {depth} 件："] + queues.describe(key)
        lines.append("（「最新」= 单槽，会被后来的普通消息顶掉；「已承诺」= /queue 排的，按序都会跑）")
        self._reply(event, "\n".join(lines), target=self._explicit_target(event))
        return {"action": "queue", "count": depth}

    def _make_event_like(self, event, text: str):
        """照原事件造一条"同样是这个人、这个会话"的新事件（`/queue` 入队用）。

        ★ 为什么不是直接改原事件的 text：原事件是**触发这一轮**的那条，
        已经被 agent 读进去了；改它等于篡改历史。新造一条，归属（会话键/来源）照抄。
        """
        from gateway.adapters.base import MessageEvent

        return MessageEvent(text=text, source=event.source, session_key=event.session_key,
                            message_id=f"{getattr(event, 'message_id', '') or 'msg'}-q",
                            metadata=dict(getattr(event, "metadata", None) or {}))

    def _cmd_sessions(self, event, args: str) -> Dict[str, Any]:
        db = self._db()
        rows = db.list_sessions(limit=10, source=f"gateway:{event.source.platform}")
        if not rows:
            self._reply(event, "（还没有会话）", target=self._explicit_target(event))
            return {"action": "sessions", "count": 0}
        lines = ["最近会话："]
        for r in rows:
            mark = "●" if r.get("id") == self._session_ids.get(event.session_key) else " "
            lines.append(f" {mark} {r.get('id')}  {r.get('session_key') or '-'}  "
                         f"{r.get('message_count') or 0} 条  {r.get('end_reason') or ''}")
        self._reply(event, "\n".join(lines), target=self._explicit_target(event))
        return {"action": "sessions", "count": len(rows)}

    def _cmd_approve(self, event, args: str) -> Dict[str, Any]:
        return self._resolve_approval(event, approved=True)

    def _cmd_deny(self, event, args: str) -> Dict[str, Any]:
        return self._resolve_approval(event, approved=False)

    # ------------------------------------------------------------------ #
    def _reply(self, event, text: str, *, target: Optional[str] = None) -> None:
        self.delivery.deliver(event.source, text, target=target)

    def _explicit_target(self, event) -> Optional[str]:
        """来源里带了显式投递目标就用它（对应真实版的"指定目标投递"）。"""
        t = event.metadata.get("deliver_to")
        return str(t) if t else None
