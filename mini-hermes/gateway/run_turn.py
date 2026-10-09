"""跑一轮：绑作用域 → 取历史 → 建 agent → 出站（对应 Hermes 的 `gateway/run_turn.py`）。

★ 这个文件里有两个"看起来可以省、其实不能省"的动作：

1. **绑 profile 作用域**（`profile_runtime_scope`）：进程是常驻的，
   而 `os.environ`/全局量里装的是启动 profile 的值。不绑 = 次级 profile 的回合
   用默认 profile 的密钥与 home 跑（**不报错**）。
2. **每条消息新建 AIAgent，但历史从库里取**：真实版就是这么做的
   （`run_agent.py` 的注释里 mini 第 2 章已记过这条）。好处是 agent 无状态、
   天然适配"一个进程服务 N 个会话"；代价是每轮要有一次历史重建，
   所以**历史的正确性完全落在存储层**（第 6 章的债，在这里被还上）。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from gateway.profile_scope import profile_runtime_scope


class TurnMixin:
    # ------------------------------------------------------------------ #
    # 会话归属
    # ------------------------------------------------------------------ #
    def _open_session(self, event) -> str:
        """给一个会话键开一个会话行（库是权威，内存只是缓存）。"""
        import hermes_state

        db = self._db()
        sid = db.latest_session_id_for_key(event.session_key,
                                          source=f"gateway:{event.source.platform}")
        if sid is None:
            sid = hermes_state.new_session_id()
            spec = self._provider_spec()
            db.create_session(sid, f"gateway:{event.source.platform}",
                              model=spec.get("model"),
                              model_config={k: spec.get(k) for k in ("provider", "api_mode")},
                              session_key=event.session_key)
        self._session_ids[event.session_key] = sid
        self.hooks.emit("session:start", session_key=event.session_key, session_id=sid)
        return sid

    # ------------------------------------------------------------------ #
    # 跑一轮
    # ------------------------------------------------------------------ #
    def _run_turn_for_event(self, event) -> Dict[str, Any]:
        from run_agent import AIAgent

        key = event.session_key
        sid = self._session_ids.get(key) or self._open_session(event)

        # ★ 作用域绑定：整个回合（含所有工具调用、子进程）都在这个 profile 上跑
        with profile_runtime_scope(self.home):
            db = self._db()
            history = db.get_messages_as_conversation(sid, include_ancestors=True)
            spec = self._provider_spec()
            agent = AIAgent(
                spec,
                max_turns=self.max_turns,
                text_callback=None,
                tool_progress_callback=self._on_tool_progress,
                status_callback=None,
                approval_callback=self._approval_callback,      # 网关里的"确认"是异步的
                context_length=spec.get("context_length"),
                session_db=db,
                session_id=sid,
                platform=event.source.platform,
                debug=self.debug,
            )
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

            self.hooks.emit("agent:end", session_key=key, session_id=sid,
                            response=result.get("final_response", ""))
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
        if not usage:
            return
        in_tok = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        out_tok = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        try:
            self._db().record_usage(
                session_id,
                input_tokens=int(in_tok),
                output_tokens=int(out_tok),
                api_calls=int(result.get("iterations_used") or 0),
            )
        except Exception as exc:
            self._turn_errors.append(f"record_usage: {exc!r}")

    # ------------------------------------------------------------------ #
    # 审批：网关里"确认"必须异步（人等不了终端提示）
    # ------------------------------------------------------------------ #
    def _approval_callback(self, command: str) -> bool:
        """agent 要求确认一条终端命令时，网关**发一条消息出去**，然后等 `/approve`。

        ★ 这就是"为什么一定有网关在前"的一个具体后果：
        没有 TTY 可以阻塞着问，所以"确认"必须变成**一次带状态的对话**。
        mini 用 `threading.Event` 等（真实版走 approval 表 + delivery_obligations）。
        """
        holder: Dict[str, Any] = {"event": threading.Event(), "approved": False}
        self._pending_approval = {"command": command, "holder": holder}
        self._reply_to_key(self._last_active_key,
                           f"🔐 需要确认：`{command}`\n回复 `/approve` 或 `/deny`。")
        ok = holder["event"].wait(timeout=self.approval_timeout)
        self._pending_approval = None
        if not ok:
            self._reply_to_key(self._last_active_key, "⌛ 确认超时 → 按拒绝处理。")
        return bool(ok and holder["approved"])

    def _resolve_approval(self, event, *, approved: bool) -> Dict[str, Any]:
        pend = getattr(self, "_pending_approval", None)
        if not pend:
            self._reply(event, "（没有待确认的命令）", target=self._explicit_target(event))
            return {"action": "approve_noop"}
        pend["holder"]["approved"] = approved
        pend["holder"]["event"].set()
        self._reply(event, "✅ 已批准" if approved else "🚫 已拒绝",
                    target=self._explicit_target(event))
        return {"action": "approved" if approved else "denied"}
