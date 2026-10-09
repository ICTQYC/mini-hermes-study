"""会话路由状态机（对应真实版 `gateway/session.py` 的 `get_or_create_session` + `SessionEntry`）。

**它回答一个问题**：这条消息进来，我该用**哪个会话** —— 复用旧的、还是开新的？为什么？

真实版把这件事放在 `SessionStore.get_or_create_session()` 里，优先级**逐字**是：

    1. `suspended=True`        → 永远强制重置（硬擦除）
    2. `resume_pending=True`   → 保留 session_id（软恢复）
    3. 没有触发条件            → 返回原记录（刷新 updated_at）

mini 抄的就是这三条 + 它们背后的那台状态机。

★ **一条不变量写在这里，因为它决定本模块没有任何"时间判断"**：

    不活动与墙钟时间永远不轮转对话。只有 /new、/reset（显式边界）与
    suspended（硬擦除）会换 session_id。

所以本文件里**不会出现**任何 `if 空闲超过 N 分钟: 重置` ——
真实版那个 `SessionResetPolicy` 是**惰性兼容数据**，不是运行时策略。
测试里有一条专门锁这个不变量（把一个会话的 updated_at 倒拨 30 天，
`route()` **仍然**必须复用它）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 崩溃恢复只管"刚刚在跑的"会话（真实版 `suspend_recently_active(max_age_seconds=120)`）
DEFAULT_RESUME_MAX_AGE_SECONDS = 120.0

# 跨多少次连续重启还活跃就挂起（真实版 `_STUCK_LOOP_THRESHOLD`）
STUCK_LOOP_THRESHOLD = 3

# 三个"被打断"的原因（真实版 `_AUTO_RESUME_REASONS`，逐字）
AUTO_RESUME_REASONS = frozenset({"restart_timeout", "shutdown_timeout", "restart_interrupted"})


@dataclass
class SessionEntry:
    """一条会话记录（对应真实版 `SessionEntry`；mini 直接从库里读出来）。

    真实版内存里一份、`sessions.json` 里一份镜像、SQLite 里权威一份；
    mini 只有 SQLite 一个读者 —— 所以这个 dataclass 是**读出来的视图**，不是第二份状态。
    """

    session_key: str
    session_id: str
    open: bool = True
    suspended: bool = False
    resume_pending: bool = False
    resume_reason: Optional[str] = None
    is_fresh_reset: bool = False
    updated_at: Optional[float] = None
    ended_reason: Optional[str] = None

    def describe(self) -> str:
        flags = []
        if self.suspended:
            flags.append("suspended")
        if self.resume_pending:
            flags.append(f"resume_pending:{self.resume_reason or '?'}")
        if self.is_fresh_reset:
            flags.append("fresh_reset")
        if not self.open:
            flags.append(f"ended:{self.ended_reason or '?'}")
        return f"{self.session_id}" + (f"（{'/'.join(flags)}）" if flags else "")


@dataclass
class RouteDecision:
    """`route()` 的结论：**该用哪个会话，以及为什么**。

    ★ 为什么要把"为什么"也返出来：网关要据此决定后续动作（压缩、提示词刷新、
    日志、以及"这次是不是用户说的重置"）。只返回一个 id 的话，调用方又得自己猜。
    """

    entry: SessionEntry
    action: str                      # "created" | "reused" | "auto_reset"
    previous_session_id: Optional[str] = None
    reset_reason: Optional[str] = None
    notes: List[str] = field(default_factory=list)


class SessionRouter:
    """把"哪条消息归哪个会话"这件事收在一个地方（真实版 SessionStore 的决策部分）。"""

    def __init__(self, db: Any, *, resume_max_age_seconds: float = DEFAULT_RESUME_MAX_AGE_SECONDS):
        self.db = db
        self.resume_max_age_seconds = float(resume_max_age_seconds)

    # ------------------------------------------------------------------ #
    # 核心：路由决策
    # ------------------------------------------------------------------ #
    def route(self, session_key: str, *, source: str, force_new: bool = False,
              **create_kwargs: Any) -> RouteDecision:
        """决定这条消息用哪个会话。优先级**必须**按 1/2/3 排（见模块 docstring）。"""
        row = self.db.route_entry(session_key)
        entry = self._to_entry(session_key, row) if row else None

        # ── 情况 0：这个键从来没有会话 → 开新的 ──
        if entry is None:
            return self._create(session_key, source, reason=None, **create_kwargs)

        # ── 显式重置（/new、/reset）：唯一一种"无论如何都换"的用户意图 ──
        if force_new:
            self._end(entry.session_id, reason="session_reset")
            return self._create(session_key, source, reason="session_reset",
                                previous=entry.session_id, force_new=True, **create_kwargs)

        # ── 上一次那条已经结束了 → 开新的（结束过就是结束过，不复活）──
        if not entry.open:
            # ★ 这里的 reset_reason 保留**原来结束的原因**（reset/suspended/…）：
            #   它解释了"为什么会有这次新建"，比写一个笼统的 "ended" 有用得多。
            return self._create(session_key, source, reason=entry.ended_reason or "ended",
                                previous=entry.session_id, **create_kwargs)

        # ── 优先级 1：suspended（硬擦除）→ 强制重置，**永远赢** ──
        if entry.suspended:
            self._end(entry.session_id, reason="suspended")
            return self._create(session_key, source, reason="suspended",
                                previous=entry.session_id, **create_kwargs)

        # ── 优先级 2：resume_pending（软恢复）→ 保留 session_id ──
        #    ★ 注意这里**不清标记**：真实版 docstring 写得很清楚 ——
        #      "The marking is not cleared here — it survives until the next successful
        #       turn completes"。读的时候就清，会让"读了之后又崩"永远恢复不上。
        if entry.resume_pending:
            decision = RouteDecision(entry=entry, action="reused",
                                     notes=[f"resume_pending:{entry.resume_reason or '?'}",
                                            "标记留到这一轮跑完再清"])
            self.touch(entry.session_id)
            return decision

        # ── 优先级 3：没有触发条件 → 复用（刷新 updated_at）──
        self.touch(entry.session_id)
        notes = []
        if entry.is_fresh_reset:
            notes.append("新重置后的第一条消息（真实版据此重注入话题/技能）")
        return RouteDecision(entry=entry, action="reused", notes=notes)

    # ------------------------------------------------------------------ #
    # 事件置位（每个都对应一个具体动作，互不覆盖彼此的位）
    # ------------------------------------------------------------------ #
    def reset(self, session_key: str, *, source: str, **create_kwargs: Any) -> RouteDecision:
        """`/new` `/reset`（显式边界）。"""
        return self.route(session_key, source=source, force_new=True, **create_kwargs)

    def suspend(self, session_key: str) -> Optional[str]:
        """硬擦除：置 `suspended` → **下次访问**换新会话。

        ★ 谁配用它：**只有卡死循环升级**（`suspend_stuck_loop_sessions`）。
        ★ 谁不配：真实版的 `/stop` **不走这条路** —— 它只是"打断当前那一轮 + 清队列"
        （`_interrupt_and_clear_session`），对话本身不换。全仓 grep `suspend_session(`
        的产品级调用点只有 evals，没有 `/stop`（文档说"/stop sets suspended"是**错的**，
        见第 8 章精译附录 C）。
        """
        row = self.db.route_entry(session_key)
        if not row or not row["open"]:
            return None
        self.db.set_route_state(row["id"], suspended=True)
        return str(row["id"])

    def mark_resume_pending(self, session_id: str, reason: str) -> None:
        """drain 超时 / 崩溃恢复 → 软恢复标记。

        ★ **不许覆盖 suspended**（真实版逐字：*"Will NOT override `suspended=True`"*）——
        硬擦除是用户的明确意图，不能被一次"意外中断"撤回。
        """
        row = self.db.get_session(session_id)
        if row is None:
            return
        if int(row.get("suspended") or 0):
            return
        self.db.set_route_state(session_id, resume_pending=True, resume_reason=reason)

    def clear_resume_pending(self, session_id: str) -> None:
        """**一轮成功完成之后**清（不是读到的时候清）。"""
        self.db.set_route_state(session_id, resume_pending=False, resume_reason=None)

    def clear_fresh_reset(self, session_id: str) -> None:
        """消费掉 `is_fresh_reset`（真实版 `gateway/run_turn.py:446-449` 逐字：
        *"Consume is_fresh_reset so it doesn't leak onto later messages in the same session."*）

        ★ 为什么它必须是**一次性**的：它表达的是"**这是重置后的第一条消息**" ——
        真实版据此重注入话题/技能（否则重置之后的对话会丢掉上下文）。如果不清，
        同一会话的第二条、第三条消息都会再注入一遍，等于每轮都重放一次开场。
        mini 目前没有"话题/技能注入"这个消费者，所以这里抄的是**它的生命周期**（置位 → 消费），
        消费时机对齐真实版：这一轮跑完就清。
        """
        self.db.set_route_state(session_id, is_fresh_reset=False)

    def touch(self, session_id: str) -> None:
        """刷新"最后活动时间"（资源巡检靠它；恢复只恢复"刚刚在跑的"）。"""
        self.db.set_route_state(session_id, touch_activity=True)

    # ------------------------------------------------------------------ #
    # 崩溃恢复 / 卡死循环
    # ------------------------------------------------------------------ #
    def suspend_recently_active(self, *, max_age_seconds: Optional[float] = None) -> List[str]:
        """崩溃恢复：把**最近活跃过**的会话标成待恢复。返回被标记的会话 id。

        三条边界照抄真实版：
        - 只碰最近 N 秒内活跃过的（否则"三天没动的会话"也会被当成被打断的）；
        - 跳过已经 `resume_pending` 的（不重复标记）；
        - 跳过 `suspended` 的（硬擦除应当保持）。
        """
        age = self.resume_max_age_seconds if max_age_seconds is None else float(max_age_seconds)
        marked: List[str] = []
        for sid in self.db.recently_active_session_ids(age):
            row = self.db.get_session(sid)
            if row is None:
                continue
            if int(row.get("suspended") or 0) or int(row.get("resume_pending") or 0):
                continue
            self.db.set_route_state(sid, resume_pending=True,
                                    resume_reason="restart_interrupted")
            marked.append(sid)
        return marked

    def suspend_stuck_loop_sessions(self, counts: Dict[str, int], *,
                                    threshold: int = STUCK_LOOP_THRESHOLD) -> List[str]:
        """跨 `threshold` 次连续重启还活跃的会话 → 挂起（让用户得到干净的开始）。

        `counts` = `{session_key: 连续重启次数}`（从计数文件读出来，见 `gateway/status.py`）。
        ★ 返回的是**被挂起的会话键**：调用方还要把计数文件清掉
        （真实版逐字：*"counters start fresh after suspension"*）。
        """
        suspended: List[str] = []
        for key, n in counts.items():
            if int(n) < int(threshold):
                continue
            if self.suspend(key) is not None:
                suspended.append(key)
        return suspended

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _to_entry(self, session_key: str, row: Dict[str, Any]) -> SessionEntry:
        return SessionEntry(
            session_key=session_key,
            session_id=str(row["id"]),
            open=bool(row.get("open")),
            suspended=bool(row.get("suspended")),
            resume_pending=bool(row.get("resume_pending")),
            resume_reason=row.get("resume_reason"),
            is_fresh_reset=bool(row.get("is_fresh_reset")),
            updated_at=row.get("updated_at"),
            ended_reason=row.get("end_reason"),
        )

    def _end(self, session_id: str, *, reason: str) -> None:
        self.db.end_session(session_id, reason)

    def _create(self, session_key: str, source: str, *, reason: Optional[str],
                previous: Optional[str] = None, force_new: bool = False,
                **create_kwargs: Any) -> RouteDecision:
        import hermes_state

        session_id = hermes_state.new_session_id()
        self.db.create_session(session_id, source, session_key=session_key, **create_kwargs)
        # ★ 建完立刻打一次活动时间：否则新会话的 updated_at 是 NULL，
        #   崩溃恢复的"最近 120 秒"窗口就看不见它（刚建就崩 = 最该恢复的那种）。
        # ★ 建成"重置后的第一条"：**显式重置和自动重置都算**（真实版 `session.py:1099` 在
        #   自动重置那条路上也写 `is_fresh_reset=True`）。它是**一次性的** ——
        #   真实版在这一轮跑完时立刻消费掉它（`gateway/run_turn.py:446-449`）：
        #   *"Consume is_fresh_reset so it doesn't leak onto later messages in the same session."*
        self.db.set_route_state(session_id, touch_activity=True,
                                is_fresh_reset=bool(previous))
        entry = SessionEntry(session_key=session_key, session_id=session_id,
                            is_fresh_reset=bool(previous))
        return RouteDecision(entry=entry,
                             action="auto_reset" if previous else "created",
                             previous_session_id=previous, reset_reason=reason,
                             notes=([f"原因：{reason}"] if reason else []))
