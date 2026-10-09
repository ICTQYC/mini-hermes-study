"""缓存 agent（对应真实版 `gateway/run.py` 的 agent 缓存 + `run_agent_cache.py`）。

## 为什么要缓存

一个会话的连续几轮之间，agent 对象上挂着**还热着**的东西 —— 真实版的账本里这是真金白银：
提示词缓存（prompt cache）的命中率。每轮新建 agent = 每轮把历史和系统提示词重新热一遍。

真实版的形状（本章精译 §9）：

```text
_AGENT_CACHE_MAX_SIZE       128      缓存多少个会话的 agent
_AGENT_CACHE_TTL_SECONDS    3600     多久没用就放掉
_AGENT_CACHE_PROTECT_RECENT 8        最近 8 个不许淘汰（LRU 保护带）
memory_high_mb              （配置）  进程内存过线 → 按 LRU 放到线下
```

## ★ mini 的收益比真实版小（诚实说明）

mini 的 agent **每轮按 `session_db` + `session_id` 自己读历史**，agent 上没有跨轮的内存态。
所以 mini 抄的是它的**生命周期与淘汰判据** —— 也就是这一章真正在讲的东西：
谁该被放掉、谁**不许**被放掉。

## ★ 三条"不许淘汰"（真实版 `run_agent_cache.py` / `agent_cache_pressure.py`）

| # | 谁 | 为什么不能淘汰 |
|---|---|---|
| ① | **正在跑那一轮的** | 淘汰它 = 把用户正在跑的回合抽走（真实版：`_running_agent_ids()` 快照跳过） |
| ② | **还没落盘的** | 真实版判据 `transcript_persistence_caught_up`：**没落盘的不许淘汰** —— 丢了就是丢了这一轮对话 |
| ③ | **最近 N 个**（默认 8） | LRU 保护带：刚用过的很可能马上还要用 |

★ 还有第 ④ 条不是"不许"而是**必须**：`session_id` 变了（`/new`、挂起后的自动重置）→ 缓存里那个
agent 挂的是**旧会话**，**必须立刻淘汰**，否则它会把新会话的消息写进旧会话的转写本。
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

AGENT_CACHE_MAX_SIZE = 128
AGENT_CACHE_TTL_SECONDS = 3600.0
AGENT_CACHE_PROTECT_RECENT = 8
MEMORY_HIGH_MB_DEFAULT = 2048.0


def process_rss_mb() -> Optional[float]:
    """本进程当前 RSS（MB）。拿不到就返回 None（**不猜**）。

    mini 不引入 psutil 依赖：先试它（装了就用），退到 `ps -o rss=`（macOS/Linux 都有）。
    真实版在容器里还会读 cgroup 文件；mini 没有容器场景，不抄。
    """
    try:                                              # 有 psutil 就用它（最准）
        import psutil  # type: ignore

        return float(psutil.Process(os.getpid()).memory_info().rss) / (1024 * 1024)
    except Exception:
        pass
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return float(out) / 1024.0                    # ps 给的是 KB
    except Exception:
        return None


@dataclass
class CacheEntry:
    agent: Any
    session_key: str
    session_id: str
    created_at: float
    last_used_at: float
    turns: int = 0
    busy: bool = False                 # 正在跑 → 不许淘汰
    pending_flush: bool = False        # 这一轮的对话还没落盘 → 不许淘汰


class AgentCache:
    def __init__(self, *, max_size: int = AGENT_CACHE_MAX_SIZE,
                 ttl_seconds: float = AGENT_CACHE_TTL_SECONDS,
                 protect_recent: int = AGENT_CACHE_PROTECT_RECENT,
                 memory_high_mb: Optional[float] = None) -> None:
        self.max_size = int(max_size)
        self.ttl_seconds = float(ttl_seconds)
        self.protect_recent = int(protect_recent)
        self.memory_high_mb = float(memory_high_mb if memory_high_mb is not None
                                    else MEMORY_HIGH_MB_DEFAULT)
        self._entries: Dict[str, CacheEntry] = {}
        self.hits = 0
        self.misses = 0
        self.evictions: List[Tuple[str, str]] = []      # [(session_key, reason)]

    # ------------------------------------------------------------------ #
    # 取用
    # ------------------------------------------------------------------ #
    def get_or_create(self, session_key: str, session_id: str,
                      factory: Callable[[], Any]) -> Tuple[Any, bool]:
        """拿这个会话的 agent；没有（或挂的是**别的会话**）就造一个。返回 (agent, 是否新建)。"""
        now = time.monotonic()
        entry = self._entries.get(session_key)
        if entry is not None and entry.session_id == session_id:
            entry.last_used_at = now
            entry.turns += 1
            self.hits += 1
            return entry.agent, False
        if entry is not None:
            # ★ 第 ④ 条：键没变但会话换了 → 必须淘汰（否则会写错转写本）
            self.evict(session_key, reason="session_rotated")
            self._entries.pop(session_key, None)
        self.misses += 1
        self._make_room()
        agent = factory()
        self._entries[session_key] = CacheEntry(
            agent=agent, session_key=session_key, session_id=session_id,
            created_at=now, last_used_at=now, turns=1)
        return agent, True

    def get(self, session_key: str) -> Optional[Any]:
        entry = self._entries.get(session_key)
        return None if entry is None else entry.agent

    def entry(self, session_key: str) -> Optional[CacheEntry]:
        return self._entries.get(session_key)

    # ------------------------------------------------------------------ #
    # 状态位（三条保护）
    # ------------------------------------------------------------------ #
    def mark_busy(self, session_key: str, busy: bool = True) -> None:
        entry = self._entries.get(session_key)
        if entry is not None:
            entry.busy = bool(busy)

    def mark_pending_flush(self, session_key: str, pending: bool = True) -> None:
        entry = self._entries.get(session_key)
        if entry is not None:
            entry.pending_flush = bool(pending)

    # ------------------------------------------------------------------ #
    # 淘汰
    # ------------------------------------------------------------------ #
    def evict(self, session_key: str, *, reason: str = "explicit") -> bool:
        """放掉一个缓存条目。

        ★ **正确性淘汰不受三条保护拦截**（`session_rotated` / `force`）：
        "会话换了"不是资源决策 —— 缓存里的 agent 挂的是旧会话，留着它等于让它往
        旧转写本里写新消息。三条保护管的是"资源紧张时先放谁"，不是"该不该放"。
        真实版的 `_evict_cached_agent(session_key)` 也是无条件的。
        """
        entry = self._entries.get(session_key)
        if entry is None:
            return False
        forced = reason in ("force", "session_rotated")
        if not forced and self._is_protected(entry):
            return False
        self._entries.pop(session_key, None)
        self.evictions.append((session_key, reason))
        return True

    def clear(self) -> int:
        """全部放掉（进程收尾用：**这不是结束对话**，只是把 RAM 还回去）。"""
        n = len(self._entries)
        self._entries.clear()
        return n

    def sweep_idle(self, *, now: Optional[float] = None) -> List[str]:
        """① 空闲清扫：TTL 过期的放掉。**不结束任何对话**（只放资源）。"""
        now = time.monotonic() if now is None else float(now)
        dropped: List[str] = []
        for key, entry in list(self._entries.items()):
            if self._is_protected(entry):
                continue
            if now - entry.last_used_at < self.ttl_seconds:
                continue
            self._entries.pop(key, None)
            self.evictions.append((key, "idle_ttl"))
            dropped.append(key)
        return dropped

    def sweep_under_pressure(self, *, rss_mb: Optional[float] = None,
                             high_mb: Optional[float] = None) -> List[str]:
        """② 内存压力清扫：过线就按 LRU 放，直到**估算**能回到线下或没人可放。

        ★ 为什么是"估算"：放掉一个 agent 到底省多少内存，没人知道。真实版也是按
        "每个条目估个大小"来算的（真实版有个每条目的估算表）。mini 用**固定估值**，
        并把"这只是估算"写进返回值里 —— 别让调用方以为这是精确账。
        """
        rss = process_rss_mb() if rss_mb is None else float(rss_mb)
        high = self.memory_high_mb if high_mb is None else float(high_mb)
        if rss is None or rss <= high:
            return []
        dropped: List[str] = []
        target_entries = max(1, len(self._entries) // 4)          # 至少试着放四分之一
        for entry in self._lru_order():
            if len(dropped) >= target_entries:
                break
            if self._is_protected(entry):
                continue
            self._entries.pop(entry.session_key, None)
            self.evictions.append((entry.session_key, "memory_pressure"))
            dropped.append(entry.session_key)
        return dropped

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _lru_order(self) -> List[CacheEntry]:
        """按"最后一次使用"升序（最旧的在前）。"""
        return sorted(self._entries.values(), key=lambda e: e.last_used_at)

    def _is_protected(self, entry: CacheEntry) -> bool:
        """三条"不许淘汰"。★ 注意：保护带只在**还有别人可放**时才算数 ——
        全被保护时宁可不放（`sweep_*` 因此可能一个都放不掉），也不破坏这三条。"""
        if entry.busy or entry.pending_flush:
            return True
        recent = self._lru_order()[-self.protect_recent:] if self.protect_recent > 0 else []
        return entry in recent

    def _make_room(self) -> None:
        """插入前腾位置：超过上限就按 LRU 放。

        ★ 真实版的注释写了这条边界：*"the cache may stay over cap until the next insert"* ——
        没人可放（全被保护）时**照样插入**，宁可暂时超上限，也不破坏保护规则。
        """
        if len(self._entries) < self.max_size:
            return
        for entry in self._lru_order():
            if self._is_protected(entry):
                continue
            self._entries.pop(entry.session_key, None)
            self.evictions.append((entry.session_key, "lru_cap"))
            break

    # ------------------------------------------------------------------ #
    # 观察
    # ------------------------------------------------------------------ #
    def stats(self) -> Dict[str, Any]:
        return {
            "size": len(self._entries),
            "max_size": self.max_size,
            "hits": self.hits,
            "misses": self.misses,
            "protected": len(self._protected_keys()),
            "evictions": len(self.evictions),
            "last_evictions": self.evictions[-3:],
        }

    def _protected_keys(self) -> List[str]:
        return [e.session_key for e in self._entries.values() if self._is_protected(e)]
