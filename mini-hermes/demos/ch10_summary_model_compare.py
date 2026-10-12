"""第 10 章 · 真跑对比：摘要模型选哪个值多少（instruct vs 推理模型）。

同一份历史、同样 3 个回合的收尾钩，**只改 `auxiliary.compression.model`**：
  A) `deepseek-chat`     —— instruct，同时也是主模型（= 不配置时的默认行为）
  B) `deepseek-reasoner` —— 推理模型（会思考）

量：每个 pass 的墙上时钟 / 消息数变化 / 产出摘要长度；并打印摘要正文前 200 字。
这不只是"跑通"：它照的是上游 §8 那两条判据（**延迟占主导** · **推理模型不合适**）。

用法：`python3 demos/ch10_summary_model_compare.py`
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hermes_state  # noqa: E402
import agent.auxiliary as aux  # noqa: E402
from agent.context_compressor import is_micro_marker  # noqa: E402
from hermes_cli.runtime_provider import resolve_provider  # noqa: E402
from run_agent import AIAgent  # noqa: E402

MAIN_MODEL = "deepseek-chat"
CANDIDATES = ["deepseek-chat", "deepseek-reasoner"]
PASSES = 3

SPEC = resolve_provider("deepseek")
assert (SPEC.get("api_key") or "").strip(), "没有解析到 DeepSeek 密钥（未做真跑）"
print(f"解释器 {sys.executable} · Python {sys.version.split()[0]}")
print(f"主 runtime：provider={SPEC.get('provider')} model={SPEC.get('model')} base_url={SPEC.get('base_url')}")
print(f"对比：摘要器 = {CANDIDATES[0]}（= 默认：主模型） vs {CANDIDATES[1]}（推理）· 每个 {PASSES} 个 pass\n")


def tool_turn(cid: str, payload: int) -> list:
    return [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": "read_file", "arguments": json.dumps({"path": f"src/mod_{cid}.py"})}}]},
        {"role": "tool", "tool_call_id": cid, "content": ("代码行 " * (payload // 4))[:payload]},
    ]


def build() -> list:
    """★ 两次跑用**同一份**历史（同 seed 同内容），保证可比。"""
    msgs = [{"role": "system", "content": "You are mini-hermes. " + "规则 " * 100},
            {"role": "user", "content": "帮我重构这个项目的模块 0 到 13，逐个读文件并说明改动"}]
    for i in range(14):
        msgs += tool_turn(f"call_{i}", 6000)
        msgs.append({"role": "assistant",
                     "content": f"第 {i} 个模块已读完，建议把 {i} 拆成两个函数（src/mod_call_{i}.py）。"})
        msgs.append({"role": "user", "content": f"好，继续看模块 {i + 1}，重点看错误处理"})
    return msgs


def run_one(model: str) -> dict:
    home = tempfile.mkdtemp(prefix=f"mini-ch10-cmp-{model}-")
    os.environ["MINI_HOME"] = home
    Path(home, "config.json").write_text(json.dumps({
        "compression": {"micro_compact": True, "micro_compact_every_n_turns": 1},
        "auxiliary": {"compression": {"provider": "deepseek", "model": model}},
    }), encoding="utf-8")
    # mini 的 config 是**每次现读**（`load_config()` 无缓存），所以改完 MINI_HOME 直接生效。

    db = hermes_state.SessionDB()
    sid = hermes_state.new_session_id()
    db.create_session(sid, "cli", model=SPEC["model"])
    statuses: list = []
    agent = AIAgent(SPEC, session_db=db, session_id=sid, context_length=SPEC.get("context_length"),
                    status_callback=lambda t: (statuses.append(t), print(f"    [status] {t}"))[0])
    aux_cfg = aux.resolve_auxiliary_runtime(aux.TASK_COMPRESSION, main_runtime=agent._runtime_snapshot())
    print(f"  摘要路由：provider={aux_cfg.get('provider')} model={aux_cfg.get('model') or '(= 主模型)'}")

    messages = build()
    for m in messages:
        if m.get("role") != "system":
            db.append_message(sid, m["role"], content=m.get("content"),
                              tool_call_id=m.get("tool_call_id"), tool_calls=m.get("tool_calls"))
    passes = []
    for turn in range(1, PASSES + 1):
        before = len(messages)
        t0 = time.monotonic()
        try:
            agent._micro_compact_after_turn(messages)
            err = ""
        except Exception as exc:                                  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        dt = time.monotonic() - t0
        markers = [m for m in messages if is_micro_marker(m)]
        summary = ""
        if markers:
            body = str(markers[-1].get("content") or "")
            i = body.find("\n\n")
            summary = body[i:].replace("--- END", "").strip()
        outcome = statuses[-1] if statuses and err == "" else (err or "（无 status）")
        passes.append({"turn": turn, "seconds": dt, "before": before, "after": len(messages),
                       "summary_chars": len(summary), "outcome": outcome, "summary": summary})
        print(f"  回合 {turn}：{dt:6.2f}s · {before} → {len(messages)} 条 · 摘要 {len(summary)} 字 · {outcome[:60]}")
    db.close()
    return {"model": model, "passes": passes, "home": home}


results = {}
for m in CANDIDATES:
    print(f"═════ 摘要器 = {m}")
    results[m] = run_one(m)
    print()

print("══════════════ 对比 ══════════════")
print(f"{'摘要器':<20} {'pass 耗时(s)':<22} {'合计':<8} {'消息数':<16} {'摘要字数'}")
for m in CANDIDATES:
    ps = results[m]["passes"]
    secs = "/".join(f"{p['seconds']:.1f}" for p in ps)
    tot = sum(p["seconds"] for p in ps)
    msgs = " → ".join([str(ps[0]["before"])] + [str(p["after"]) for p in ps])
    chars = "/".join(str(p["summary_chars"]) for p in ps)
    print(f"{m:<20} {secs:<22} {tot:6.1f}s  {msgs:<16} {chars}")

best = min(results, key=lambda m: sum(p["seconds"] for p in results[m]["passes"]))
print(f"\n结论：{best} 更快（合计 {sum(p['seconds'] for p in results[best]['passes']):.1f}s "
      f"vs {sum(p['seconds'] for p in results[[m for m in CANDIDATES if m != best][0]]['passes']):.1f}s）")
for m in CANDIDATES:
    ps = results[m]["passes"]
    s = ps[-1]["summary"].replace("\n", " ")[:200]
    print(f"\n── {m} 最后一次产出的摘要（前 200 字）──\n{s}")
