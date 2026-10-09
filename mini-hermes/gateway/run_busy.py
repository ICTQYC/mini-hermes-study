"""忙时会话队列（对应真实版 `gateway/run_busy.py` 的 `_promote_queued_event` / `_queue_depth`）。

## 为什么要**两层**队列

一个会话同时只能跑一轮。忙的时候又来了消息，两种意图**必须分开**：

| 用户干了什么 | 意图 | 结构 | 行为 |
|---|---|---|---|
| 又发了一条普通消息 | "**我改主意了**" —— 后来那条才是现在想要的 | **单槽**（`_slot`） | 后到**覆盖**先到；被顶掉的那条要**通知**用户（那是他写的字） |
| `/queue 把这件也做了` | "**这几件都要做**，一件都不能少" | **溢出 FIFO**（`_overflow`） | 尾部入队、按序处理、**永不合并** |

真实版逐字（`run_busy.py:60-64`）：

```text
Promote the next overflow item after the slot drained.
``pending_event`` None → the overflow head becomes the pending event; otherwise the head is
staged into the slot for the NEXT recursion. Returns the (possibly updated) pending_event.
```

★ 注意真实版的单槽是**单值**，不是列表 —— `run_busy.py:71` 里是**赋值**：

```python
adapter._pending_messages[session_key] = overflow.pop(0)
```

（第 7 章的 mini 把它写成了 `list` + `append`：那等于"普通消息也按 FIFO 排队"，
于是"我改主意了"这种意图**表达不出来** —— 用户发十条，十条都会挨个跑完。
第 8 章按真实版改成单槽 + 溢出 FIFO。）

## 晋升顺序：**先溢出、后单槽**

溢出里的东西是用户**显式**排的队（`/queue`），它们被接受的那一刻就承诺了"会做"；
单槽里只是"最新的那条普通消息"。先做已承诺的、再做最新的，顺序才对得上用户的预期。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class SessionQueues:
    """一个适配器持有的两层队列（按会话键分桶）。"""

    def __init__(self) -> None:
        self._slot: Dict[str, Any] = {}
        self._overflow: Dict[str, List[Any]] = {}

    # ------------------------------------------------------------------ #
    # 入队
    # ------------------------------------------------------------------ #
    def keep_latest(self, event: Any) -> Optional[Any]:
        """普通消息 → 单槽（**后到覆盖先到**）。

        ★ 返回被顶掉的那条事件（没有则 None）：调用方**必须**把"你的前一条被覆盖了"
        告诉用户 —— 静默丢掉用户写的字，比拒绝更糟。
        """
        key = event.session_key
        overwritten = self._slot.get(key)
        self._slot[key] = event
        return overwritten

    def enqueue_explicit(self, event: Any) -> int:
        """`/queue <text>` → 溢出 FIFO 尾部。返回入队后的总深度。"""
        key = event.session_key
        self._overflow.setdefault(key, []).append(event)
        return self.depth(key)

    # ------------------------------------------------------------------ #
    # 出队
    # ------------------------------------------------------------------ #
    def promote(self, key: str) -> Optional[Any]:
        """取下一个要跑的事件：**先溢出 FIFO，再单槽**（真实版 `_promote_queued_event` 的顺序）。"""
        overflow = self._overflow.get(key) or []
        if overflow:
            nxt = overflow.pop(0)
            if not overflow:
                self._overflow.pop(key, None)
            return nxt
        return self._slot.pop(key, None)

    # ------------------------------------------------------------------ #
    # 观察
    # ------------------------------------------------------------------ #
    @property
    def slots(self) -> Dict[str, Any]:
        """单槽的视图（真实版里这个东西就叫 `adapter._pending_messages`，而且是**单值**）。"""
        return self._slot

    def depth(self, key: str) -> int:
        """总深度 = 单槽（0 或 1）+ 溢出条数（真实版 `_queue_depth` 也是这么算的）。"""
        return (1 if key in self._slot else 0) + len(self._overflow.get(key) or ())

    def slot_event(self, key: str) -> Optional[Any]:
        return self._slot.get(key)

    def overflow_items(self, key: str) -> List[Any]:
        return list(self._overflow.get(key) or ())

    def describe(self, key: str) -> List[str]:
        """给人看的队列内容（`/queue` 用）。**说清楚层级**，别混成一个数。"""
        lines: List[str] = []
        slot = self._slot.get(key)
        if slot is not None:
            lines.append(f"  最新（会在忙完后跑）：{_preview(getattr(slot, 'text', ''))}")
        for i, ev in enumerate(self._overflow.get(key) or (), 1):
            lines.append(f"  #{i}（已承诺，会依次跑）：{_preview(getattr(ev, 'text', ''))}")
        return lines

    def clear(self, key: str) -> None:
        self._slot.pop(key, None)
        self._overflow.pop(key, None)


def _preview(text: str, limit: int = 40) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


class BusyMixin:
    """runner 侧的观察面（方法名对齐真实版 `gateway/run_busy.py`）。

    ★ 队列本身**住在适配器里**（守卫 1 在那一级，它才是"消息去哪"的决定者）；
    runner 只是**问**它 —— 所以这里是转发，不是第二份状态。真实版也是这个形状：
    `_queue_depth(session_key, *, adapter=None)` 拿着适配器读 `adapter._pending_messages`。
    """

    def _adapter_queues(self, adapter: Any) -> SessionQueues:
        queues = getattr(adapter, "queues", None)
        if queues is None:                      # 适配器没实现第 8 章的队列 → 惰性挂上
            queues = SessionQueues()
            adapter.queues = queues
        return queues

    def _queue_depth(self, session_key: str, *, adapter: Any = None) -> int:
        if adapter is None:
            return 0
        return self._adapter_queues(adapter).depth(session_key)

    def _promote_queued_event(self, session_key: str, pending_event: Any = None,
                              *, adapter: Any = None) -> Optional[Any]:
        if adapter is None:
            return pending_event
        if pending_event is not None:
            return pending_event
        return self._adapter_queues(adapter).promote(session_key)

    def _notify_overwrite(self, session_key: str, overwritten: Any) -> None:
        """单槽被顶掉 → **告诉用户**（不能静默丢他写的字）。

        ★ 为什么这条通知必须有：单槽覆盖是**有损**的 —— 用户第二条消息进来时，
        第一条（还没开始跑）就没了。用户不知道这件事的话，他会以为两条都发了。
        真实版在覆盖路径上也会回执给用户（`_gateway_accepted` 那套的对应面）。

        ★ 第 7 章曾经把"已排队"那句回复当**死代码**删掉（当时队列是批量 FIFO，
        每条都会被跑到，回话反而多余）。第 8 章它不是死代码了：**覆盖是信息损失**，
        必须说出口，并且给出正确的替代动作（`/queue`）。
        """
        try:
            preview = " ".join(str(getattr(overwritten, "text", "") or "").split())[:60]
            self._reply(overwritten,
                        f"⚠️ 你刚才那条还没开始跑，就被后来的消息顶掉了：\n「{preview}」\n"
                        f"两件都要做的话，用 `/queue <文本>` 排队（它不会被顶掉）。")
        except Exception as exc:                      # 通知失败不该影响任何主流程
            self._turn_errors.append(f"notify_overwrite: {exc!r}")
