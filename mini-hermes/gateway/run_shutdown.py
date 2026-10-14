"""收尾（对应 Hermes 的 `gateway/run_shutdown.py`）。

⚠️ 名字对账（真实版第 7 章附录 C 记的那处出入）：真实文档写的是
`gateway/run_shutdown.py::_finalize_session`，而源码里实际叫
**`_finalize_session_off_loop`**（`:1117`）—— 因为那里的收尾动作要在
事件循环**之外**跑（关库、提交记忆 provider 都要做阻塞 IO）。
mini 没有事件循环，所以就叫 `_finalize_session`。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from gateway.profile_scope import profile_runtime_scope


class ShutdownMixin:
    def _finalize_session(self, session_id: str, reason: str) -> None:
        """结束一个会话：写结束原因 + 发 `session:end` 钩子。

        ★ 只有**真正的对话边界**才配用它：`/new` `/reset`、硬擦除、压缩轮转。
        **进程退出不算边界**（第 8 章更正）—— 关闭/重启只对正在跑的会话打软恢复标记。

        ★ 为什么"结束会话"必须和"清理资源"分开（真实版的 flush 生命周期）：

        | 情况 | 会不会终结会话 |
        |---|---|
        | 显式对话边界（`/new`） | ✅ 会 |
        | 空闲时间、每日边界、内存压力 | ❌ **不会** |
        | 进程退出 / 重启 | ❌ **不会**（只对正在跑的会话打"待恢复"标记） |

        把"资源紧张"或"进程要重启了"当成"对话结束"，用户就会发现自己的对话被**悄悄重置**了 ——
        第 8 章那条不变量（时间永不轮转）在进程生命周期上同样成立。
        """
        if not session_id:
            return
        try:
            self._db().end_session(session_id, reason)
        except Exception as exc:
            self._turn_errors.append(f"end_session: {exc!r}")
        self.hooks.emit("session:end", session_id=session_id, reason=reason)

    def shutdown(self, *, reason: str = "gateway_stop",
                 exit_code: int = 0) -> None:
        """进程级收尾。

        ★ **第 8 章的更正**：进程退出**不是对话边界** —— 所以这里**不再**结束任何会话。
        真实版在关闭/重启的 drain 路径里只做一件事（`gateway/run_shutdown.py:786-795`）：

        ```python
        reason = "restart_timeout" if self._restart_requested else "shutdown_timeout"
        await self.async_session_store.mark_resume_pending(_sk, reason)
        ```

        而且只对**正在跑**的会话标这一下（`_mark_running_sessions_resume_pending`）。
        其余会话什么都不做 —— 它们保持 open，下次启动时按普通复用接着用。
        （第 7 章这里对每个会话写了 `end_session("gateway_stop")`：那会让"重启之后对话还在"
        这条承诺直接失效 —— 每次重启都等于给所有用户开了新会话。已按真实版语义改掉。）
        """
        # ★ 第 8 章：进程收尾 —— 停巡检线程 + 把缓存 agent 全放掉。
        #   **这不是结束对话**：转写本在库里，放掉 agent 只是把 RAM 还回去
        #   （真实版同：缓存淘汰从来不写 `ended_at`）。
        self._stop_housekeeping()
        self._stopping.set()
        adapters = list(self.adapters.values()) + [a for group in self._profile_adapters.values()
                                                  for a in group.values()]
        # 审批等待也是回合的一部分；退出时先唤醒它们，按拒绝处理。
        with self._approval_lock:
            for pending in self._pending_approvals.values():
                pending["holder"]["approved"] = False
                pending["holder"]["event"].set()

        # ★ 只标记"被打断的那一轮"（正在跑的会话）→ 软恢复
        for key in list(self._running_agents):
            sid = self._session_ids.get(key)
            if sid:
                try:
                    home = Path(self._turn_homes.get(key, str(self.home)))
                    with profile_runtime_scope(home):
                        self._router().mark_resume_pending(sid, "shutdown_timeout")
                except Exception as exc:
                    self._turn_errors.append(f"mark_resume_pending: {exc!r}")
            self._interrupt_running_agent(key)

        tasks = [task for adapter in adapters
                 for task in list(getattr(adapter, "_session_tasks", {}).values())]
        deadline = time.monotonic() + self.shutdown_timeout
        for task in tasks:
            if task is not threading.current_thread() and task.is_alive():
                task.join(max(0.0, deadline - time.monotonic()))
        drained = not any(task.is_alive() for task in tasks)
        self._agent_cache.clear()

        # ★ **不 touch**（第 8 章更正）：`updated_at` 表达的是"**用户活动**"，
        #   不是"进程活动"。原来这里给每个会话 touch 一下，后果是：
        #   任何一次优雅关闭之后，网关知道的**所有**会话都变成"刚刚活跃"——
        #   于是崩溃恢复的"最近 120 秒"窗口形同虚设（下次脏启动会把它们全标成待恢复，
        #   哪怕用户上一次说话是几小时前）。真实版关闭路径里也**没有** touch，
        #   它只对**正在跑**的会话打一个 `resume_pending`（上面那段）。
        self._session_ids.clear()

        for adapter in adapters:
            try:
                adapter.disconnect()
            except Exception as exc:
                self._turn_errors.append(f"disconnect: {exc!r}")
        if drained:
            for db in self._dbs.values():
                db.close()
            self._dbs.clear()
            self._routers.clear()
            if self._multiplex_before_start is not None:
                from agent.secret_scope import set_multiplex_active
                set_multiplex_active(self._multiplex_before_start)
                self._multiplex_before_start = None

        from gateway import status as gw_status
        try:
            if self.pid:
                # ★ 带上裁决栏：`write_runtime_status` 是全量写，不带就等于把
                #   "为什么多路复用/为什么没多路复用"从状态文件里抹掉（真跑实测踩过）。
                gw_status.write_runtime_status(self.home, "stopped", pid=self.pid,
                                               **getattr(self, "_multiplex_status_fields", {}))
            # ★ 第 8 章：**优雅关闭的末尾**写下"干净退出"标记 ——
            #   下次启动据此跳过崩溃恢复（否则每次 `restart` 都会被当成被打断的）。
            #   只在退出码 0 时写：启动失败（78）不是"干净退出"。
            if exit_code == 0 and drained:
                gw_status.mark_clean_shutdown(self.home)
            # ★ 只删**自己写的**身份文件（真实版 `live_gateway_pid_for_home` 的
            #   "Never unlinks home's identity files" 是给读方定的规矩：
            #   读方不清理，写方才清理）。
            gw_status.pid_file(self.home).unlink(missing_ok=True)
        except Exception as exc:
            self._turn_errors.append(f"unlink pid: {exc!r}")
        self._pid = None
