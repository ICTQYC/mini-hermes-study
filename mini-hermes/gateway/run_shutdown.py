"""收尾（对应 Hermes 的 `gateway/run_shutdown.py`）。

⚠️ 名字对账（真实版第 7 章附录 C 记的那处出入）：真实文档写的是
`gateway/run_shutdown.py::_finalize_session`，而源码里实际叫
**`_finalize_session_off_loop`**（`:1117`）—— 因为那里的收尾动作要在
事件循环**之外**跑（关库、提交记忆 provider 都要做阻塞 IO）。
mini 没有事件循环，所以就叫 `_finalize_session`。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class ShutdownMixin:
    def _finalize_session(self, session_id: str, reason: str) -> None:
        """结束一个会话：写结束原因 + 发 `session:end` 钩子。

        ★ 为什么"结束会话"必须和"清理资源"分开（真实版的 flush 生命周期）：

        | 情况 | 会不会终结会话 |
        |---|---|
        | 显式对话边界（`/new`） | ✅ 会 |
        | 空闲时间、每日边界、内存压力 | ❌ **不会** |
        | 进程退出 | ✅ 会（reason=gateway_stop） |

        把"资源紧张"当成"对话结束"，用户就会发现自己的对话被**悄悄重置**了。
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
        """进程级收尾：结束所有已知会话 → 摘掉身份文件 → 状态置 stopped。"""
        for key, sid in list(self._session_ids.items()):
            self._finalize_session(sid, reason)
            self._session_ids.pop(key, None)

        for adapter in self.adapters.values():
            try:
                adapter.disconnect()
            except Exception as exc:
                self._turn_errors.append(f"disconnect: {exc!r}")

        from gateway import status as gw_status
        try:
            if self.pid:
                gw_status.write_runtime_status(self.home, "stopped", pid=self.pid)
            # ★ 只删**自己写的**身份文件（真实版 `live_gateway_pid_for_home` 的
            #   "Never unlinks home's identity files" 是给读方定的规矩：
            #   读方不清理，写方才清理）。
            gw_status.pid_file(self.home).unlink(missing_ok=True)
        except Exception as exc:
            self._turn_errors.append(f"unlink pid: {exc!r}")
        self._pid = None
