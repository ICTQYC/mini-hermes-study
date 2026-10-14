"""跑一轮：绑作用域 → 取历史 → 建 agent → 出站（对应 Hermes 的 `gateway/run_turn.py`）。

★ 这个文件里有两个"看起来可以省、其实不能省"的动作：

1. **绑 profile 作用域**（`profile_runtime_scope`）：进程是常驻的，
   而 `os.environ`/全局量里装的是启动 profile 的值。不绑 = 次级 profile 的回合
   用默认 profile 的密钥与 home 跑（**不报错**）。
2. **agent 可以缓存，但历史每轮从库里取**：AIAgent 未收到显式历史时读取活世代。
   缓存节省提示词重建，不能代替会话路由或历史加载。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from gateway.profile_scope import profile_runtime_scope


class TurnMixin:
    # ------------------------------------------------------------------ #
    # 会话归属（第 8 章：交给状态机决定，不再"自己看库"）
    # ------------------------------------------------------------------ #
    def _router(self):
        """路由状态机（每个库一个）。"""
        from gateway.session_state import SessionRouter

        cache = getattr(self, "_routers", None)
        if cache is None:
            cache = self._routers = {}
        key = str(self._db().db_path)
        if key not in cache:
            cache[key] = SessionRouter(self._db())
        return cache[key]

    def _open_session(self, event) -> str:
        """给这个会话键决定"用哪个会话"（复用 / 软恢复 / 硬擦除后新建）。

        ★ 第 8 章起这里**不是**"找不到就新建"了 —— 决策交给 `SessionRouter.route()`，
        它按真实版的优先级（`suspended` > `resume_pending` > 复用）办，
        并且**没有任何时间判断**（时间永不轮转，见该模块的 docstring）。
        """
        spec = self._provider_spec()
        decision = self._router().route(
            event.session_key, source=f"gateway:{event.source.platform}",
            model=spec.get("model"),
            model_config={k: spec.get(k) for k in ("provider", "api_mode")},
        )
        self._session_ids[event.session_key] = decision.entry.session_id
        if decision.action in ("created", "auto_reset"):
            # ★ 会话轮转了 → 缓存里那个 agent 挂的是**旧会话**，必须立刻放掉
            #   （真实版 `_evict_cached_agent(session_key)`）。不清的话，缓存的 agent
            #   会把新会话的消息写进**旧会话**的转写本 —— 这是缓存真正危险的地方。
            self._agent_cache.evict(event.session_key, reason="session_rotated")
            # 只有"真的开了新会话"才发这个钩子 —— 复用不该每轮都喊一次。
            self.hooks.emit("session:start", session_key=event.session_key,
                            session_id=decision.entry.session_id,
                            action=decision.action, reason=decision.reset_reason)
            if decision.previous_session_id:
                self._turn_errors.append(
                    f"会话轮转：{decision.previous_session_id} → {decision.entry.session_id}"
                    f"（{decision.reset_reason}）")
        return decision.entry.session_id

    # ------------------------------------------------------------------ #
    # 跑一轮
    # ------------------------------------------------------------------ #
    def _run_turn_for_event(self, event) -> Dict[str, Any]:
        from run_agent import AIAgent

        key = event.session_key

        # ★ 作用域绑定：整个回合（含所有工具调用、子进程）都在**这条消息所属的** profile 上跑。
        #   A5 修正（第 9 章）：第 7/8 章这里写的是 `self.home`（**启动**那个 profile）——
        #   多路复用下那会把次级 profile 的回合**绑回活动 profile**，覆盖掉外层入站作用域。
        home = self._profile_home_for_source(event.source) or self.home
        # ★ 观测面：这一轮**实际跑在哪个 home**。
        #   为什么必须单独记：会话行是 `_open_session` 在**外层**作用域里写的，
        #   所以"回合体的作用域对不对"从库的内容上**看不出来** —— 实测踩过
        #   （把回合作用域改回 `self.home`，测试照样全绿）。第 9 章的验收靠这一栏。
        self._turn_homes[event.session_key] = str(home)
        with profile_runtime_scope(home):
            sid = self._open_session(event)              # 缓存命中也必须经过路由状态机
            db = self._db()
            spec = self._provider_spec()

            # ── 第 8 章：agent 从**缓存**取，不再每轮新建（真实版网关就是缓存 agent 的）──
            #   历史仍然由 agent 自己按 session_db + session_id 读（所以缓存里的
            #   agent 必须与 session_id 一致 —— 轮转时上面的 evict 保证这一点）。
            #   AIAgent 在追加本轮 user 之前，按 session_db/session_id 读取活世代。
            def _build() -> Any:
                return AIAgent(
                    spec,
                    max_turns=self.max_turns,
                    text_callback=None,
                    tool_progress_callback=self._on_tool_progress,
                    status_callback=None,
                    approval_callback=None,                         # 每轮绑定当前来源事件
                    context_length=spec.get("context_length"),
                    session_db=db,
                    session_id=sid,
                    platform=event.source.platform,
                    debug=self.debug,
                )

            agent, created = self._agent_cache.get_or_create(key, sid, _build)
            agent.approval_callback = lambda command: self._approval_callback(command, event=event)
            if created:
                self._turn_errors.append(f"缓存未命中 → 新建 agent（{key}，会话 {sid}）")
            self._agent_cache.mark_busy(key, True)          # 跑着的：不许淘汰
            self._agent_cache.mark_pending_flush(key, True)  # 这一轮还没跑完：不许淘汰
            self._running_agents[key] = agent
            self.hooks.emit("agent:start", session_key=key, session_id=sid, text=event.text)

            result: Dict[str, Any] = {}
            try:
                result = agent.run_conversation(event.text)
            except Exception as exc:                            # 网关**不许**因一轮失败而死
                self._turn_errors.append(f"{key}: {exc!r}")
                self._reply(event, f"⚠️ 这一轮失败了：{exc}",
                            target=self._explicit_target(event))
                return {"action": "error", "error": repr(exc)}
            finally:
                self._running_agents.pop(key, None)
                self._agent_cache.mark_busy(key, False)     # 包括前置异常和被中断的回合

            # ★ "没落盘的不许淘汰"（真实版 `transcript_persistence_caught_up`）：
            #   **跑完**才解除保护；被打断的那一轮保留标记 —— 宁可多留一个 agent，
            #   也不丢一轮对话（TTL 清扫会跳过它；下一次成功跑完就清）。
            completed = not any(result.get(flag) for flag in ("interrupted", "error", "budget_exhausted"))
            if completed:
                self._agent_cache.mark_pending_flush(key, False)

            self.hooks.emit("agent:end", session_key=key, session_id=sid,
                            response=result.get("final_response", ""))
            # ★ 第 8 章：**一轮真的跑完了**才清"待恢复"标记（真实版由网关在
            #   `run_conversation()` 返回真实响应之后调用 `clear_resume_pending()`）。
            #   "读到就清"会让"读了之后又崩"永远恢复不上。
            if completed:
                self._router().clear_resume_pending(sid)
                # ★ 第 8 章：这一轮**跑完了** → 清掉它自己的连续重启计数
                #   （真实版 `_clear_restart_failure_count`）。跑完说明它不卡了。
                # ★ is_fresh_reset 是**一次性**的：这一轮跑完就消费掉
                #   （真实版 `gateway/run_turn.py:446-449`："Consume is_fresh_reset so it
                #    doesn't leak onto later messages in the same session."）
                self._router().clear_fresh_reset(sid)
                from gateway import status as gw_status
                gw_status.clear_one_stuck_loop_count(home, key)
            self._reply(event, result.get("final_response", "") or "（空答复）",
                        target=self._explicit_target(event))
            self._sync_session_meta(sid, result)
            return {"action": "replied", "session_id": sid, "result": result}

    def _sync_session_meta(self, session_id: str, result: Dict[str, Any]) -> None:
        """把这一轮的用量写回会话行（对应真实版网关侧的用量回填）。

        ★ 键名映射是**真的踩到过**的一处：`AIAgent` 内部的 usage 用的是
        `prompt_tokens` / `completion_tokens`（照 OpenAI 的响应字段命名），
        而存储层的列叫 `input_tokens` / `output_tokens`。
        不做映射，回填就是"静默写 0" —— 真跑时看列全是 0 才发现。
        """
        usage = result.get("usage") or {}
        in_tok = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        out_tok = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        try:
            self._db().record_usage(
                session_id,
                input_tokens=int(in_tok),
                output_tokens=int(out_tok),
                api_calls=int(result.get("api_calls", result.get("iterations_used")) or 0),
            )
        except Exception as exc:
            self._turn_errors.append(f"record_usage: {exc!r}")

    # ------------------------------------------------------------------ #
    # 审批：网关里"确认"必须异步（人等不了终端提示）
    # ------------------------------------------------------------------ #
    def _approval_callback(self, command: str, *, event: Any) -> bool:
        """agent 要求确认一条终端命令时，网关**发一条消息出去**，然后等 `/approve`。

        ★ 这就是"为什么一定有网关在前"的一个具体后果：
        没有 TTY 可以阻塞着问，所以"确认"必须变成**一次带状态的对话**。
        mini 用 `threading.Event` 等（真实版走 approval 表 + delivery_obligations）。
        """
        holder: Dict[str, Any] = {"event": threading.Event(), "approved": False}
        key = event.session_key
        pending = {"command": command, "holder": holder, "owner": event.source.user_id}
        with self._approval_lock:
            if key in self._pending_approvals or self._stopping.is_set():
                return False                       # 不覆盖同一会话已有的确认
            self._pending_approvals[key] = pending
        try:
            self._reply(event, f"🔐 需要确认：`{command}`\n回复 `/approve` 或 `/deny`。",
                        target=self._explicit_target(event))
            ok = holder["event"].wait(timeout=self.approval_timeout)
            if not ok:
                self._reply(event, "⌛ 确认超时 → 按拒绝处理。", target=self._explicit_target(event))
            return bool(ok and holder["approved"])
        finally:
            with self._approval_lock:
                if self._pending_approvals.get(key) is pending:
                    self._pending_approvals.pop(key, None)

    def _resolve_approval(self, event, *, approved: bool) -> Dict[str, Any]:
        with self._approval_lock:
            pend = self._pending_approvals.get(event.session_key)
            accepted = bool(pend and pend["owner"] == event.source.user_id
                            and not pend["holder"]["event"].is_set())
            if accepted:
                pend["holder"]["approved"] = approved
                pend["holder"]["event"].set()
        if not accepted:
            self._reply(event, "（没有待确认的命令）", target=self._explicit_target(event))
            return {"action": "approve_noop"}
        self._reply(event, "✅ 已批准" if approved else "🚫 已拒绝",
                    target=self._explicit_target(event))
        return {"action": "approved" if approved else "denied"}
