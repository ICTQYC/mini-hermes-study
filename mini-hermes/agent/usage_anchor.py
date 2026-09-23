"""UsageAnchor —— 用 provider 报出的真实用量推算"现在用了多少 token"（第 5 章 §2.2.1）。

对应 Hermes 的 `agent/usage_anchor.py`。

## 它解决什么问题

本地估算**不准**：图片、加密 blob、各家分词器都不同。而误判的代价**不对称**：

   估算偏大 → 白白压缩（损失信息 + 打断提示词缓存）
   估算偏小 → 请求被 provider 拒绝（可恢复：走反应式恢复）

所以策略是：**有真实数据就用真实数据，没有才估算。**

## 锚点 = 真实用量 + 只对"之后追加的消息"的增量估算

    t0  请求 → 响应带 usage{prompt_tokens: 48000, completion_tokens: 900}
        锚点 = (48900, 指纹(当时那份 messages))
    之后又追加了 3 条消息（其中一条工具结果很大）
        现在 ≈ 48900 + 粗估(那 3 条)      ← 只估算**增量**，不重估全部

## 为什么要"内容指纹"（这一节最关键的一点）

网关**每一轮都从数据库重新读一遍历史** —— 那是**新对象**（顺序也不保证），
所以不能用对象身份、也不能用行数来认"我计价的是哪一份 transcript"。

用**内容指纹**就稳：内容一样 → 指纹一样 → 锚点还有效。
这正是 Hermes 的做法（原文：*identifies the priced transcript by a content fingerprint*）。

## 什么时候必须清掉

**压缩、会话重置、模型切换** —— 三件事都让旧用量不再代表现在。
前两者 Hermes 明确列出；模型切换的理由是：usage 是**按模型计价**的，
换个模型（或换条路由）之后那个数字没有可比性（§6.3 第 5 条）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence


def fingerprint(messages: Sequence[Dict[str, Any]]) -> str:
    """一份 transcript 的内容指纹。

    ★ 用 `json.dumps(..., sort_keys=True)` 而不是 `repr()`：
    dict 的键顺序在 JSON 里是**约定的**（我们控制输出），
    而 `repr` 受对象构造顺序影响 —— 同一个内容可能给出不同指纹，
    那锚点就会无故失效（表现为"压缩偶尔不触发"，很难查）。
    """
    h = hashlib.sha256()
    for msg in messages:
        payload = {
            "role": msg.get("role"),
            "content": msg.get("content"),
            "tool_calls": msg.get("tool_calls"),
            "tool_call_id": msg.get("tool_call_id"),
            "name": msg.get("name"),
        }
        h.update(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))
        h.update(b"\x00")                 # 分隔符：防止两条消息拼起来撞上另一组
    return h.hexdigest()[:16]


class UsageAnchor:
    """一份"真实用量 + 它的计价范围"的记录。"""

    def __init__(self) -> None:
        self._tokens: Optional[int] = None
        self._count: int = 0
        self._fp: str = ""
        self._model: str = ""
        self.record_count: int = 0       # 记过几次（测试/诊断用）

    # ── 写 ────────────────────────────────────────────────────────────────

    def record(self, usage: Dict[str, Any], messages: Sequence[Dict[str, Any]],
               *, model: str = "") -> None:
        """记下一次真实用量。

        ⚠️ 三种"不算数"的情况，都不能覆盖已有的锚点：
          · `usage` 里两个 token 字段都没有（很多 provider 在某些路由下不返回）
          · 全为 0（流式响应的首个分片、或 provider 明确不给）
        —— 用一个"零锚点"覆盖掉一个好锚点，会让下一轮**突然看不见超窗**
        （比估算不准更糟：估算是往大里偏，零锚点是往小里偏）。
        """
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if prompt is None and completion is None:
            return
        total = int(prompt or 0) + int(completion or 0)
        if total <= 0:
            return
        self._tokens = total
        self._count = len(messages)
        self._fp = fingerprint(messages)
        self._model = model or self._model
        self.record_count += 1

    def clear(self) -> None:
        """压缩 / 会话重置 / 模型切换时调用。"""
        self._tokens = None
        self._count = 0
        self._fp = ""
        self._model = ""

    # ── 读 ────────────────────────────────────────────────────────────────

    @property
    def has_anchor(self) -> bool:
        return self._tokens is not None

    def estimate(self, messages: Sequence[Dict[str, Any]]) -> Optional[int]:
        """现在大约多少 token；锚点失效时返回 `None`（让调用方回退到粗估）。

        锚点有效的判据：**当前消息的前 `_count` 条，指纹和记锚点时一样**
        （内容一致 → 说明这一批还是我当时计价的那份 transcript）。
        后面追加出来的部分**只估算增量**。
        """
        if self._tokens is None:
            return None
        if len(messages) < self._count:
            return None                   # 变短了（被压缩/被截断）→ 锚点不作数
        # 局部 import：避免 estimator ↔ anchor 的循环依赖
        from agent.context_compressor import estimate_messages_tokens_rough

        head = list(messages[: self._count])
        if fingerprint(head) != self._fp:
            return None                   # 计价的那批内容变了（编辑重发/回退）
        delta = estimate_messages_tokens_rough(messages[self._count:])
        return self._tokens + delta

    # ── 会话持久化 ────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """存进会话（Hermes 存在会话行上，这样新进程 `--resume` 能恢复它）。"""
        return {
            "tokens": self._tokens, "count": self._count,
            "fp": self._fp, "model": self._model, "records": self.record_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UsageAnchor":
        a = cls()
        a._tokens = data.get("tokens")
        a._count = int(data.get("count") or 0)
        a._fp = str(data.get("fp") or "")
        a._model = str(data.get("model") or "")
        a.record_count = int(data.get("records") or 0)
        return a

    def describe(self) -> str:
        if self._tokens is None:
            return "锚点：无"
        return f"锚点：{self._tokens} token @ {self._count} 条（记过 {self.record_count} 次）"
