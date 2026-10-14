"""每次迭代的预算是有限的。

对应 Hermes 的 agent/iteration_budget.py（几乎照抄，只是删掉了 execute_code 退款逻辑）。

为什么需要它：agent loop 是 `while True` 形状的——
模型可以永远"再调一个工具"。没有预算，一个跑偏的 agent 会烧掉你的钱，
因此 mini 默认设 500 次；上游锚 f5d1926110 主 agent 默认 sys.maxsize，子 agent 250 次。
"""
from __future__ import annotations

import threading


class IterationBudget:
    """线程安全（子 agent / 并发工具会同时问"还能再来一次吗"）。"""

    def __init__(self, max_total: int = 500):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """消耗一次迭代。返回 False = 预算耗尽，agent 必须停下并总结。"""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)
