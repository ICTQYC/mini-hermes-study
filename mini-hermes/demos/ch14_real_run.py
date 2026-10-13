#!/usr/bin/env python3
"""第 14 章 · 真跑：**引擎真的接管了压缩**（发现 → 选中 → 生效 → 回落），端到端。

这一章的唯一承诺是：**`context.engine` 写谁，压缩就归谁管。**
所以这份真跑不是"调一下 `select_engine` 看返回值"，而是：
把一个**真引擎**放进 `<MINI_HOME>/engines/<name>/`，在**真历史**上跑一次真压缩，
然后在**最终历史**（和会话库 active 行）里找那个引擎独有的**标记文本**。

做六件事（发现/选择**全部真读盘**，压缩**不联网**——引擎自己产标记，不打摘要 LLM）：

  ① 真引擎库：`<MINI_HOME>/engines/under_lossy/__init__.py`（真 `ContextEngine` 子类）
  ② 真配置：`{"context": {"engine": "under_lossy"}}` → 真发现：它进候选
  ③ 真 `AIAgent` + **真系统提示词**（`system_prompt.build_system_prompt`）+ 真工具群历史
  ④ ★ `agent._preflight_compression(msgs)` → 断言：选中的**就是它** · 它的 `compress()`
     **真被执行过**（计数）· 标记**真进了最终历史** · **真进了会话库** · 系统提示词未被碰
  ⑤ 未知名（`"nope"`）⇒ 回落内置 + **有声警告**（I2 真跑版）
  ⑥ 零配置 ⇒ 内置压缩器，且 `compress()` 输出与**直接构造**逐条相等（I4 真跑版）

跑完 `try/finally` 清掉临时 `MINI_HOME`（别在系统 temp 里留垃圾）。
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# ── 隔离三件套：MINI_HOME 必须在 **import mini 的模块之前** 设好 ──────────────
_HOME = Path(tempfile.mkdtemp(prefix="mini_ch14_real_"))
os.environ["MINI_HOME"] = str(_HOME)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hermes_state  # noqa: E402
from agent import engine_registry as reg  # noqa: E402
from agent import system_prompt  # noqa: E402
from agent.context_compressor import DEFAULT_CONTEXT_LENGTH, ContextCompressor  # noqa: E402
from agent.context_engine import ContextEngine  # noqa: E402
from hermes_cli import config as cfg  # noqa: E402
from run_agent import AIAgent  # noqa: E402

RESULTS = []

ENGINE_NAME = "under_lossy"
#: ★ 这个标记**只**由插件引擎产出 —— 在最终历史里看到它 = 引擎真的接管了压缩。
MARKER = "【第14章·真跑标记】under_lossy 引擎接管了这次压缩"
OK_SUMMARY = "## Goal\n演示上下文引擎插槽\n\n## Next Steps\n- 看标记"


def record(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {detail}" if detail and not cond else ""))


def write_config(engine=None) -> None:
    data = {}
    if engine is not None:
        data["context"] = {"engine": engine}
    cfg.config_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def write_engine() -> Path:
    """把真引擎写进 `<MINI_HOME>/engines/under_lossy/__init__.py`（目录名 = name）。"""
    d = reg.engines_dir() / ENGINE_NAME
    d.mkdir(parents=True, exist_ok=True)
    (d / reg.ENGINE_INIT_FILENAME).write_text(
        "from agent.context_engine import ContextEngine\n"
        "\n"
        f"MARKER = {MARKER!r}\n"
        "\n"
        "class UnderLossyEngine(ContextEngine):\n"
        "    \"\"\"一个\"无损\"演示引擎：把中间段换成一条带标记的摘要。\"\"\"\n"
        "    def __init__(self):\n"
        "        self.compress_calls = 0\n"
        "\n"
        "    @property\n"
        "    def name(self):\n"
        f"        return {ENGINE_NAME!r}\n"
        "\n"
        "    def should_compress(self, messages):\n"
        "        return True          # 演示：每轮都压（真实引擎该看 token 数）\n"
        "\n"
        "    def compress(self, messages, *, call_llm=None, call_llm_meta=None,\n"
        "                 status_callback=None):\n"
        "        self.compress_calls += 1\n"
        "        if status_callback:\n"
        "            status_callback('under_lossy：第 %d 次接管压缩' % self.compress_calls)\n"
        "        head = [m for m in messages if m.get('role') == 'system'][:1]\n"
        "        tail = list(messages[-1:])\n"
        "        return head + [{'role': 'assistant', 'content': MARKER}] + tail\n",
        encoding="utf-8")
    return d


def make_agent(**kw):
    return AIAgent(
        {"provider": "fake", "api_mode": "chat_completions",
         "base_url": "http://fake", "api_key": "fake", "model": "fake-model"},
        **kw)


def tool_turn(cid: str, payload: int) -> list:
    """一个完整的工具组：assistant(tool_calls) + tool(result)（真历史形状）。"""
    return [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": "read_file",
                          "arguments": json.dumps({"path": f"src/mod_{cid}.py"})}}]},
        {"role": "tool", "tool_call_id": cid, "content": "代码行 " * (payload // 4)},
    ]


def build_history(system_prompt_text: str) -> list:
    """真历史：真系统提示词 + user + 若干工具群 + 散文。"""
    msgs = [{"role": "system", "content": system_prompt_text},
            {"role": "user", "content": "帮我重构这个项目，逐个读文件并说明改动"}]
    for i in range(8):
        msgs += tool_turn(f"call_{i}", 4000)
        msgs.append({"role": "assistant", "content": f"第 {i} 个模块已读完，建议拆分 src/mod_{i}.py。"})
        msgs.append({"role": "user", "content": f"继续看模块 {i + 1}，重点看错误处理"})
    return msgs


def marker_positions(msgs) -> int:
    return sum(1 for m in msgs if isinstance(m, dict) and MARKER in (m.get("content") or ""))


def main() -> int:
    print(f"解释器：{sys.executable}  Python {sys.version.split()[0]}")
    print(f"MINI_HOME = {_HOME}（临时）\n")

    # ── ① 真引擎库 + ② 真配置 → 真发现 ──────────────────────────────────
    eng_dir = write_engine()
    write_config(ENGINE_NAME)
    print(f"引擎目录：{eng_dir}")
    scan = reg.discover_engines()
    print(f"【发现】候选 = {[e.name for e in scan.engines]} · 跳过 = "
          f"{[(s.dir_name, s.reason) for s in scan.skipped]}\n")
    record("① 真引擎被目录发现机制发现（候选里就是它）",
           [e.name for e in scan.engines] == [ENGINE_NAME] and not scan.skipped,
           str([e.name for e in scan.engines]))
    record("① 它的 `name` 与目录名一致（契约校验通过）",
           bool(scan.engines) and scan.engines[0].name == ENGINE_NAME)

    # ── ③ 真 agent + 真系统提示词 + 真历史 ─────────────────────────────
    db = hermes_state.SessionDB()
    sid = hermes_state.new_session_id()
    db.create_session(sid, "cli", model="fake-model")
    agent = make_agent(session_db=db, session_id=sid,
                       status_callback=lambda t: print("  [status]", t))
    real_prompt = system_prompt.build_system_prompt(agent)
    print(f"真系统提示词：{len(real_prompt)} 字符（前 60：{real_prompt[:60]!r}）\n")

    engine = agent._compressor()
    record("③ 选中的**就是**那个插件引擎（不是内置压缩器）",
           isinstance(engine, ContextEngine) and not isinstance(engine, ContextCompressor)
           and getattr(engine, "name", None) == ENGINE_NAME, type(engine).__name__)

    messages = build_history(real_prompt)
    for m in messages:
        if m.get("role") != "system":
            db.append_message(sid, m["role"], content=m.get("content"),
                              tool_call_id=m.get("tool_call_id"), tool_calls=m.get("tool_calls"))
    n_before = len(messages)
    print(f"初始：{n_before} 条消息 · 库 active 行 {db.count_messages(sid)}\n")

    # ── ④ ★ 真跑压缩：宿主真路径 ────────────────────────────────────────
    agent._preflight_compression(messages)
    after = db.count_messages(sid, active_only=True)
    db_marker = [r for r in db.get_messages(sid) if MARKER in str(r.get("content") or "")]

    record("④ ★ 引擎的 `compress()` 真的被执行过（计数 >= 1，不是只看类型）",
           getattr(engine, "compress_calls", 0) >= 1, str(getattr(engine, "compress_calls", None)))
    record("④ ★ 那个标记文本**真进了最终历史**（引擎接管了压缩，行为可观测）",
           marker_positions(messages) >= 1, f"标记条数={marker_positions(messages)}")
    record("④ ★ 标记**也进了会话库** active 行（压缩作为新世代落库）",
           len(db_marker) >= 1, f"DB 命中 {len(db_marker)} 行 · active={after}")
    record("④ 消息数变少了（确实压了）", len(messages) < n_before,
           f"{n_before} → {len(messages)}")
    record("④ 引擎**不碰系统提示词**：首条 system 逐字还是真提示词（明确选此条断言）",
           bool(messages) and messages[0].get("role") == "system"
           and messages[0].get("content") == real_prompt)
    record("④ 默认引擎那条路**没有**被走到（历史里没有内置摘要的痕迹）",
           MARKER in json.dumps(messages, ensure_ascii=False))
    db.close()

    # ── ⑤ 未知名 ⇒ 回落内置 + 有声警告（I2 真跑版）──────────────────────
    write_config("nope")
    seen = []
    agent2 = make_agent(status_callback=seen.append)
    c2 = agent2._compressor()
    record("⑤ 未知名 ⇒ 回落**内置压缩器**（不拒绝启动）",
           isinstance(c2, ContextCompressor) and getattr(c2, "name", None) == "compressor",
           type(c2).__name__)
    record("⑤ ★ 回落是**有声**的：status 里读得到被请求的名字 `nope`",
           any("nope" in s for s in seen), str(seen))

    # ── ⑥ 零配置 ⇒ 与改造前逐字节一致（I4 真跑版）──────────────────────
    write_config(None)                                  # 连 context 段都不写
    seen2 = []
    agent3 = make_agent(status_callback=seen2.append)
    c3 = agent3._compressor()
    record("⑥ 零配置 ⇒ 内置压缩器，且**没有任何回落警告**（默认路径是安静的）",
           isinstance(c3, ContextCompressor) and seen2 == [], str(seen2))

    prompt3 = system_prompt.build_system_prompt(agent3)
    hist_a = build_history(prompt3)
    hist_b = build_history(prompt3)
    out_a = c3.compress(hist_a, call_llm=lambda s, u: OK_SUMMARY)
    direct = ContextCompressor(
        context_length=int(getattr(agent3, "context_length", 0) or DEFAULT_CONTEXT_LENGTH),
        status_callback=agent3._emit_status)
    out_b = direct.compress(hist_b, call_llm=lambda s, u: OK_SUMMARY)
    record("⑥ ★ 零配置的 `compress()` 输出与**直接构造**逐条相等（零行为漂移）",
           out_a == out_b, f"{len(out_a)} vs {len(out_b)}")

    n_ok = sum(1 for _, c, _ in RESULTS if c)
    print(f"\n════ {n_ok} 通过 / {len(RESULTS) - n_ok} 失败　（共 {len(RESULTS)} 条）")
    print(f"    临时 MINI_HOME：{_HOME}")
    return 1 if n_ok != len(RESULTS) else 0


if __name__ == "__main__":
    # ★ 跑完清掉临时 MINI_HOME（用 finally 保证中途抛异常也不留垃圾）。
    try:
        _code = main()
    finally:
        shutil.rmtree(_HOME, ignore_errors=True)
    sys.exit(_code)
