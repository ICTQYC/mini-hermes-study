"""后台会话巡检（对应真实版 `gateway/run_watchers.py:40-86` 的 `_session_housekeeping`）。

**一句话**：一个每 300 秒醒一次的循环，只做三件事，而且**一件都不结束对话**。

真实版一个 tick 的顺序（逐条对得上）：

```text
① _sweep_idle_cached_agents()        清掉"太久没用"的缓存 agent
② _sweep_agent_cache_under_pressure() 内存过线 → 按 LRU 放到线下（真实版还有 #80764 那条边界注释）
③ prune_old_entries(max_age)          每小时一次；且**只有 session_store_max_age_days > 0 才做**
```

★ 为什么不把这三件事塞进消息路径：它们是**资源**问题，不是**对话**问题。
放在消息路径里，就会在用户等回复的时候去干"清缓存"这种跟他无关的事；
而它们又必须**周期性**发生 —— 不然"没人说话但不占内存"这件事永远不会发生。

★ 为什么"只放资源、不结束对话"要写成一条不变量：
这三件事每一个都在**丢东西**（缓存 agent、内存、老记录）。丢的东西必须是**可从库里重建的**。
把"清资源"写成"结束对话"，用户第二天回来就会发现对话被悄悄重置了 ——
第 8 章那条不变量（时间永不轮转）在这里的具体形式。

★ 第 ③ 件的实话：mini 的存储层**没有删除路径**（第 6 章没做 prune）。
所以这里抄的是**开关语义**（默认 0 = 关，> 0 才尝试），越线时**只记账不动数据** ——
诚实记债，不假装做过了。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

HOUSEKEEPING_INTERVAL_SECONDS = 300.0        # 真实版会话维护的间隔
HOUSEKEEPING_FIRST_DELAY_SECONDS = 60.0      # 真实版 watcher 起来先 sleep(60)
PRUNE_INTERVAL_SECONDS = 3600.0              # 剪枝是"每小时一次"，不是每个 tick
SESSION_STORE_MAX_AGE_DAYS_DEFAULT = 0.0     # ★ 默认 0 = 剪枝关闭（真实版同）


class WatchersMixin:
    # ------------------------------------------------------------------ #
    # 一个 tick
    # ------------------------------------------------------------------ #
    def _session_housekeeping_tick(self, *, now: Optional[float] = None,
                                   rss_mb: Optional[float] = None,
                                   prune: bool = True) -> Dict[str, Any]:
        """跑一次巡检，返回**收据**（谁被放了、内存多少、剪枝做了什么）。"""
        from gateway.agent_cache import process_rss_mb

        now = time.monotonic() if now is None else float(now)
        receipt: Dict[str, Any] = {
            "idle_evicted": self._agent_cache.sweep_idle(now=now),
            "pressure_evicted": [],
            "rss_mb": None,
            "pruned": None,
        }

        rss = process_rss_mb() if rss_mb is None else float(rss_mb)
        receipt["rss_mb"] = rss
        if rss is not None and rss > self._agent_cache.memory_high_mb:
            # ★ 内存过线：**先报告再动手**（真实版也是先记日志再淘汰 —— 事后才知道放了多少）
            receipt["pressure_evicted"] = self._agent_cache.sweep_under_pressure(rss_mb=rss)

        if prune:
            receipt["pruned"] = self._maybe_prune_old_entries(now=now)

        # 观察面：最近几次 tick 都留一份（诊断/测试要看"它真的在跑"）
        self._housekeeping_log.append({"at": now, **{k: v for k, v in receipt.items()
                                                     if k != "rss_mb"},
                                       "rss_mb": receipt["rss_mb"]})
        if len(self._housekeeping_log) > 20:
            del self._housekeeping_log[:-20]
        return receipt

    def _maybe_prune_old_entries(self, *, now: float) -> Optional[str]:
        """每小时一次的老记录剪枝。**返回一句说明**（做了什么 / 为什么什么都没做）。"""
        days = float(getattr(self, "session_store_max_age_days", SESSION_STORE_MAX_AGE_DAYS_DEFAULT))
        if days <= 0:
            return "跳过：session_store_max_age_days=0（默认关闭，真实版同）"
        # ★ 从没剪过 → 该剪。**不许**把"上次剪枝时间"初始化成 0.0：
        #   `time.monotonic()` 的 0 点是**开机时刻**，于是"开机不到 1 小时"的机器上
        #   第一次剪枝会被自己的节流挡掉（而且这个 bug 只在 uptime 跨过 3600s 前后
        #   表现不同 —— 典型的"时间相关偶发"）。
        if self._last_prune_at is not None and \
                now - self._last_prune_at < PRUNE_INTERVAL_SECONDS:
            return "跳过：距上次剪枝不足 1 小时"
        self._last_prune_at = now
        # ★ 诚实记债：mini 存储层没有删除路径（第 6 章没做）。
        note = (f"未执行：mini 存储层没有删除路径（阈值 {days} 天）"
                f" —— 见第 8 章差距清单「老记录剪枝」")
        self._turn_errors.append(f"housekeeping/prune: {note}")
        return note

    # ------------------------------------------------------------------ #
    # 循环
    # ------------------------------------------------------------------ #
    def _start_housekeeping(self, *, interval: Optional[float] = None,
                            first_delay: Optional[float] = None) -> Optional[threading.Thread]:
        """起巡检线程（幂等：已经在跑就返回 None）。"""
        if self._housekeeping_thread is not None and self._housekeeping_thread.is_alive():
            return None
        iv = HOUSEKEEPING_INTERVAL_SECONDS if interval is None else float(interval)
        delay = HOUSEKEEPING_FIRST_DELAY_SECONDS if first_delay is None else float(first_delay)
        self._housekeeping_stop = threading.Event()

        def _loop() -> None:
            # ★ 先等一段时间再开工（真实版同）：刚起来的网关正在做启动恢复、连适配器，
            #   这时候跑巡检只会跟它抢缓存锁。
            if self._housekeeping_stop.wait(delay):
                return
            while not self._housekeeping_stop.is_set():
                try:
                    self._session_housekeeping_tick()
                except Exception as exc:                    # 巡检坏了**不许**带死网关
                    self._turn_errors.append(f"housekeeping: {exc!r}")
                if self._housekeeping_stop.wait(iv):
                    return

        thread = threading.Thread(target=_loop, name="gw-housekeeping", daemon=True)
        self._housekeeping_thread = thread
        thread.start()
        return thread

    def _stop_housekeeping(self) -> None:
        if self._housekeeping_stop is not None:
            self._housekeeping_stop.set()
        thread = self._housekeeping_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._housekeeping_thread = None

    # ------------------------------------------------------------------ #
    # 观察
    # ------------------------------------------------------------------ #
    def housekeeping_log(self) -> List[Dict[str, Any]]:
        return list(self._housekeeping_log)
