"""第 10 章 真跑收据：真 DeepSeek + 临时 MINI_HOME，让微压缩**真的**吸收一个交换。

不联网跑不了的话就诚实报「未做」。核验点（去文件系统/DB 核）：
  1) 转写本里**恰好一条** micro 标记（assistant 角色 + 压缩头）
  2) **user 行逐字还在**（条数+内容都不变）
  3) 消息数变少
  4) DB 行数一致（active 行数 == 当前消息条数；恰好一条 active 摘要行）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOME = tempfile.mkdtemp(prefix="mini-ch10-run-")
os.environ["MINI_HOME"] = HOME
Path(HOME, "config.json").write_text(json.dumps({
    "compression": {"micro_compact": True, "micro_compact_every_n_turns": 1},
}), encoding="utf-8")

import hermes_state  # noqa: E402
from agent.context_compressor import (  # noqa: E402
    COMPACTION_HEADER, is_micro_marker,
)
from hermes_cli.config import get_compression_config  # noqa: E402
from hermes_cli.runtime_provider import resolve_provider  # noqa: E402
from run_agent import AIAgent  # noqa: E402

print(f"解释器：{sys.executable}  Python {sys.version.split()[0]}")
print(f"MINI_HOME={HOME}（临时）")
print("配置：", get_compression_config())

spec = resolve_provider("deepseek")
assert (spec.get("api_key") or "").strip(), "没有解析到 DeepSeek 密钥（未做真跑）"
print(f"provider={spec.get('provider')} model={spec.get('model')} base_url={spec.get('base_url')} "
      f"api_key=<len {len(spec['api_key'])}>")
print("→ 摘要调用将走 agent/auxiliary.py（A8）→ 真 DeepSeek\n")


def tool_turn(cid: str, payload: int) -> list:
    return [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": "read_file", "arguments": json.dumps({"path": f"src/mod_{cid}.py"})}}]},
        {"role": "tool", "tool_call_id": cid, "content": ("代码行 " * (payload // 4))[:payload]},
    ]


def build():
    msgs = [{"role": "system", "content": "You are mini-hermes. " + "规则 " * 100},
            {"role": "user", "content": "帮我重构这个项目的模块 0 到 13，逐个读文件并说明改动"}]
    for i in range(14):
        msgs += tool_turn(f"call_{i}", 6000)
        msgs.append({"role": "assistant",
                     "content": f"第 {i} 个模块已读完，建议把 {i} 拆成两个函数（src/mod_call_{i}.py）。"})
        msgs.append({"role": "user", "content": f"好，继续看模块 {i + 1}，重点看错误处理"})
    return msgs


db = hermes_state.SessionDB()
sid = hermes_state.new_session_id()
db.create_session(sid, "cli", model=spec["model"])
agent = AIAgent(spec, session_db=db, session_id=sid,
                context_length=spec.get("context_length"),
                status_callback=lambda t: print("  [status]", t))

messages = build()
# 把非 system 的历史灌进库（模拟一个真实会话）
for m in messages:
    if m.get("role") != "system":
        db.append_message(sid, m["role"], content=m.get("content"),
                          tool_call_id=m.get("tool_call_id"), tool_calls=m.get("tool_calls"))
users_before = [m["content"] for m in messages if m.get("role") == "user"]
n_before = len(messages)
print(f"初始：{n_before} 条消息 · 库 active 行 {db.count_messages(sid)} · user 行 {len(users_before)}\n")

# 跑 3 个「回合」的收尾钩（每回合最多吸收 1 个交换，各打 1 次真 DeepSeek）
for turn in range(1, 4):
    agent._micro_compact_after_turn(messages)
    print(f"  回合 {turn} 后：{len(messages)} 条消息\n")

# ── 核验 ────────────────────────────────────────────────────────────────
markers = [m for m in messages if is_micro_marker(m)]
users_after = [m["content"] for m in messages if m.get("role") == "user"]
active = db.count_messages(sid, active_only=True)
all_rows = db.count_messages(sid, active_only=False)
active_summary = [r for r in db.get_messages(sid) if COMPACTION_HEADER in str(r.get("content") or "")]

print("══════════ 核验 ══════════")
print(f"① 转写本里 micro 标记条数：{len(markers)}（期望 1）"
      f" · 角色={markers[0]['role'] if markers else None}")
# ★ user 文本是**逐字**保留的，但**相邻 user 行会被合并成一条**（supersede 丢旧标记后，
#   `merge_adjacent_user_turns` 用 "\n\n" 拼起来保持角色交替）—— 所以要比**拼接后的文本**，不是比条数。
joined_before = "\n\n".join(users_before)
joined_after = "\n\n".join(users_after)
merged = len(users_before) - len(users_after)
print(f"② user 文本逐字保留：{joined_after == joined_before}"
      f"（条数 {len(users_before)} → {len(users_after)}，其中合并 {merged} 条 —— 设计如此：supersede 时相邻 user 行拼成一条，内容逐字不动）")
print(f"③ 消息数：{n_before} → {len(messages)}（变少：{len(messages) < n_before}）")
print(f"④ DB active 行 {active} == 当前消息（去 system）{len([m for m in messages if m.get('role')!='system'])}"
      f" · 全量行 {all_rows}")
print(f"⑤ DB 里 active 摘要行条数：{len(active_summary)}（期望 1）")
if markers:
    print("\n── 真 DeepSeek 产出的滚动摘要（前 260 字）──")
    body = str(markers[0]["content"])
    idx = body.find("\n\n")
    print(body[body.find("摘要。") + 3: body.find("--- END")][:260].strip() or body[:260])

ok = (len(markers) == 1 and joined_after == joined_before and len(messages) < n_before
      and active == len([m for m in messages if m.get("role") != "system"])
      and len(active_summary) == 1)
db.close()
print(f"\n结论：{'全部核验通过 ✓' if ok else '有核验未通过 ✗'}")
sys.exit(0 if ok else 1)
