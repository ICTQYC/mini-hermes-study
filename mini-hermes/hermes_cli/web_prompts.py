"""Inspect actual transport-bound messages, with captured assembly tiers and byte comparisons."""
from __future__ import annotations
import hashlib
import json
import uuid


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def fingerprint(text):
    raw = text.encode("utf-8")
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def common_prefix(a, b):
    a, b = a.encode("utf-8"), b.encode("utf-8")
    for index, (left, right) in enumerate(zip(a, b)):
        if left != right:
            return index
    return min(len(a), len(b))


def capture_request(agent, candidate, messages, previous, redact):
    # Bounded by explicit rejection, never silently truncating a purported full prompt.
    safe = redact(json.loads(json.dumps(messages, ensure_ascii=False)))
    if len(encoded(safe)) > 1_000_000 or len(previous) >= 64:
        raise ValueError("提示词演示已达保存上限（单次 1 MB / 会话 64 次请求），请新建会话")
    system = next((m.get("content", "") for m in safe if m.get("role") == "system"), "")
    parts = redact(dict(getattr(agent, "_cached_system_prompt_parts", {})))
    base = "\n\n".join(parts.get(name, "") for name in ("stable", "context", "volatile") if parts.get(name))
    mapped = bool(base) and isinstance(system, str) and system.startswith(base)
    layers = [{"name": name, "text": parts.get(name, ""), **fingerprint(parts.get(name, ""))}
              for name in ("stable", "context", "volatile")] if mapped else []
    temporary = system[len(base):] if mapped else ""
    turn = agent._current_turn_id
    iteration = 1 + sum(row["turn"] == turn for row in previous)
    old = previous[-1] if previous else None
    comparison = None
    if old:
        before = old["system"]
        comparison = {"previous_id": old["id"], "system_equal": system == before,
                      "system_prefix_bytes": common_prefix(before, system),
                      "previous_messages": len(old["messages"]), "current_messages": len(safe),
                      "layers_equal": {name: next((row["text"] for row in old["layers"] if row["name"] == name), None) == parts.get(name)
                                       for name in ("stable", "context", "volatile")},
                      "temporary_equal": old["temporary"] == temporary}
    return {"id": uuid.uuid4().hex, "turn": turn, "iteration": iteration,
            "model": candidate["model"], "messages": safe, "system": system,
            "layers": layers, "mapped": mapped, "temporary": temporary,
            "system_fingerprint": fingerprint(system), "comparison": comparison,
            "explanation": "同一 Agent 缓存完整系统提示词，正常续聊复用三层；时间和记忆不会每次重读。压缩重建、显式系统指令变化或新会话可能改变它。临时指导仅拼入请求副本，工具结果与新问题使 messages 继续增长。字节相同不等于服务商缓存已命中。"}
