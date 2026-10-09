"""启动恢复（对应真实版 `gateway/run_startup.py` 里的恢复那一段）。

**它解决的场景**：网关崩了 / 被 drain 超时杀了 / 机器掉电 ——
"正在跑的那一轮"没了，但**对话不该跟着没**。

真实版的启动恢复序列（文档 §7，四个动作的顺序不能换）：

```text
网关启动
   │
   ▼  ① 有 .clean_shutdown 标记吗？── 有 ──► 跳过挂起（上次是干净退出）
   │ 没有
   ▼  ② suspend_recently_active(120s)：把最近活跃过的会话标成 resume_pending
   ▼  ③ _suspend_stuck_loop_sessions()：跨 3+ 次重启还活跃的 → 挂起
   ▼  ④ 对每个 resume_pending 的会话：合造 MessageEvent → 跑 _handle_message 续跑
```

★ mini 在第 ④ 步**有意做得不一样**（原因写在这里，因为它是一处行为差异）：

真实版是"**主动**续跑"—— 造一条合成的消息事件，走和用户发消息完全一样的那条路，
让 agent 自己接着往下干（连"恢复"都不需要单独的逻辑，这是它设计上最漂亮的一点）。

mini 的 `AIAgent`（第 2 章的交付物）**要求一条用户消息才能起一轮**，没有"无人值守地
接着上一个转写本继续"这条路径。所以 mini 的等价效果是：

    保留 `resume_pending` 标记（不清）→ **下一条真实消息进来时，
    `route()` 会给它同一个 session_id** → 用户看到的是"对话还在"。

代价明确：**网关不会自己接着跑**（没有用户消息就一直停着）。
要做"主动续跑"，得先给 `AIAgent` 加一条"没有用户消息也能起轮"的入口 —— 那是第 2 章的地盘。
"""
from __future__ import annotations

from typing import Any, Dict, List

from gateway import status as gw_status


class StartupMixin:
    def _recover_sessions(self) -> Dict[str, Any]:
        """启动恢复。返回一份收据（给 banner / 测试 / 诊断看）。"""
        receipt: Dict[str, Any] = {
            "clean_shutdown": False,
            "resume_pending": [],
            "suspended": [],
            "stuck_counts": {},
        }

        # ── ① 上次是干净退出吗？读过即删（标记只说明"上一次"）──
        if gw_status.consume_clean_shutdown(self.home):
            receipt["clean_shutdown"] = True
            return receipt

        router = self._router()

        # ── ② 崩溃恢复：把最近活跃过的会话标成待恢复 ──
        marked = router.suspend_recently_active()
        receipt["resume_pending"] = marked

        # ── ③ 卡死循环：跨 3+ 次重启还活跃 → 挂起 ──
        #    先把"这次重启还活着"的会话计数 +1；**然后不管有没有新标记都读文件** ——
        #    权威是**持久化的计数**（真实版 `_suspend_stuck_loop_sessions()` 也是直接读文件，
        #    不依赖本次扫描标了谁）。★ 第 6 个真 bug：原来写成 `if keys:` 才检查，
        #    于是"会话已被挂起 → 本次没有新标记 → 卡死升级永远不触发"。
        db = self._db()
        keys = [k for k in (db.session_key_for_id(sid) for sid in marked) if k]
        if keys:
            gw_status.bump_stuck_loop_counts(self.home, keys)
        counts = gw_status.read_stuck_loop_counts(self.home)
        suspended = router.suspend_stuck_loop_sessions(counts)
        receipt["stuck_counts"] = counts
        receipt["suspended"] = suspended
        if suspended:
            # 真实版逐字：*"Clear the file — counters start fresh after suspension"*
            gw_status.clear_stuck_loop_counts(self.home)

        # ── ④ mini 的"续跑"：不清标记，下一条真实消息会接着同一个会话 ──
        #    （真实版在这里合造消息主动续跑；差异与代价见模块 docstring）
        for sid in marked:
            self.hooks.emit("session:resume_pending", session_id=sid, home=str(self.home))
        return receipt

    def _recovery_banner(self, receipt: Dict[str, Any]) -> List[str]:
        """把收据变成人能读的几行（启动横幅用）。**只报事实，不报没测过的话。**"""
        lines: List[str] = []
        if receipt["clean_shutdown"]:
            lines.append("恢复     上次是干净退出 → 跳过崩溃恢复")
            return lines
        n = len(receipt["resume_pending"])
        if n:
            lines.append(f"恢复     {n} 个会话被标记为待恢复（最近活跃过；下一条消息接着原会话）")
            lines.append(f"         它们：{', '.join(receipt['resume_pending'][:3])}"
                         + ("…" if n > 3 else ""))
        if receipt["suspended"]:
            lines.append(f"恢复     ⚠️ {len(receipt['suspended'])} 个会话跨 3+ 次重启仍活跃 → "
                         f"已挂起（下次访问换新会话）：{', '.join(receipt['suspended'])}")
        if not n and not receipt["suspended"]:
            lines.append("恢复     上次非干净退出，但没有需要恢复的会话")
        return lines
