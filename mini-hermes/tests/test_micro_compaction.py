"""第 10 章的自测：**微型压缩**（滚动逐交换摘要）—— **不联网**。

跑法：python3 tests/test_micro_compaction.py

## 测的是什么（8 组）

| 组 | 守的承诺 |
|---|---|
| 1. 交换边界（A1） | 跳 user/标记 · 连续吃 assistant+tool · **必须能收尾** |
| 2. 滚动摘要与游标（A2） | 只留**一份**累积摘要 · 游标丢了靠**扫描转写本最后的标记**恢复 |
| 3. 一回合一个 + no-op（A3） | 一次 pass 最多吸收**一个**交换 · no-op **零 LLM 调用** |
| 4. 失败（A4） | 失败**不吸收** · 连续 3 次失败**把游标推过**卡住的交换 |
| 5. 标记形状（A5） | assistant 角色 + 三键 · supersede **只丢 micro 标记** · 相邻 user 合并 |
| 6. 边界复用与不变量（A6） | **尾部窗口不碰** · 角色交替仍合法 · **系统提示词不动** · 默认关是恒等 |
| 7. defrag | 摘要超阈值就地重压**最新** micro 标记（不加消息） |
| 8. 配置夹取（默认值表） | 四个键的默认 + `max(1/0, int)` 夹取 · bool 不冒充数字 |

## 隔离

逻辑**纯内存**（所有 LLM 调用都是注入的假函数）。涉及配置读盘的用例把
`MINI_HOME` 钉在临时目录（`hermes_constants.set_mini_home_override`），
**绝不写用户真实 home**。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.context_compressor import (  # noqa: E402
    COMPACTION_HEADER,
    COMPRESSED_SUMMARY_METADATA_KEY,
    COMPRESSED_SUMMARY_HAS_USER_TURN_KEY,
    MICRO_COMPACT_MARKER_KEY,
    SUMMARY_END_MARKER,
    ContextCompressor,
    is_context_summary_message,
    is_micro_marker,
    redact_sensitive_text,
)
from agent.micro_compaction import (  # noqa: E402
    MICRO_COMPACT_MAX_CONSECUTIVE_FAILURES,
    build_micro_summary_prompt,
    cursor_after_marker,
    find_one_exchange,
    make_micro_marker,
    merge_adjacent_user_turns,
    rolling_summary_from_marker,
    splice_micro_marker,
)
from hermes_cli.config import get_compression_config, parse_config_int  # noqa: E402
import hermes_constants  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {detail}" if detail and not cond else ""))


# ══════════════════════════════════════════════════════════════════ 构造假历史

# 测试进程把 MINI_HOME 钉到临时目录：任何意外的 home 访问都落在这里，**不碰真实 home**。

# 所有夹具目录归本脚本所有，入口 finally 清理整个根目录。
_TEMP_ROOT = tempfile.TemporaryDirectory(prefix="mini-clean-test_micro_compaction-")
_TMP_HOME = tempfile.mkdtemp(prefix="mini-ch10-", dir=_TEMP_ROOT.name)
_HOME_TOKEN = hermes_constants.set_mini_home_override(_TMP_HOME)


def _tool_turn(cid: str, payload_chars: int = 4000, name: str = "read_file") -> list:
    """一个完整工具组：assistant(tool_calls) + tool(result)。"""
    return [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": name, "arguments": json.dumps({"path": f"app/m_{cid}.py"})}}]},
        {"role": "tool", "tool_call_id": cid, "content": "x" * payload_chars},
    ]


def build_history(turns: int = 12, payload_chars: int = 4000) -> list:
    """够大的历史：尾部预算（lean，10K token）装不下整段 ⇒ 有可压缩的中间段。"""
    msgs = [{"role": "system", "content": "You are a helpful assistant. " + "规则 " * 200},
            {"role": "user", "content": "帮我搭一个 FastAPI 项目，要有测试"}]
    for i in range(turns):
        msgs += _tool_turn(f"call_{i}", payload_chars)
        msgs.append({"role": "assistant", "content": f"第 {i} 个模块完成（app/m_{i}.py）。"})
        msgs.append({"role": "user", "content": f"接下来处理第 {i + 1} 个模块，注意测试覆盖"})
    return msgs


def build_batch_marker(text: str = "这是批量压缩的摘要") -> dict:
    """一条**批量**摘要标记（带 `_compressed_summary` 键，**不带** micro 键）。"""
    return {
        "role": "assistant",
        "content": f"{COMPACTION_HEADER}\n\n{text}\n\n{SUMMARY_END_MARKER}",
        COMPRESSED_SUMMARY_METADATA_KEY: True,
    }


def _alt_violations(msgs: list) -> list:
    """返回角色交替违规（空 = 合法）。与 `run_agent._validate_alternation` 同口径。"""
    bad, prev = [], None
    for i, m in enumerate(msgs):
        r = m.get("role")
        if r == prev and r in ("user", "assistant"):
            bad.append(f"[{i}] 连续两条 {r}")
        if r == "tool" and prev not in ("tool", "assistant"):
            bad.append(f"[{i}] tool 结果前一条是 {prev}")
        prev = r
    return bad


def _recording_llm(text: str = "## Goal\n搭项目\n## Progress\n- 模块完成", *, raise_exc=False):
    """返回 (call_llm, calls) —— calls 记录每次 (system, user)。"""
    calls = []

    def _llm(system, user):
        calls.append((system, user))
        if raise_exc:
            raise RuntimeError("boom")
        return text

    return _llm, calls


def _none_llm():
    calls = []

    def _llm(system, user):
        calls.append((system, user))
        return None

    return _llm, calls


def _meta_llm(text, finish):
    """返回 (call_llm_meta, calls) —— calls 记录每次 (system, user)。

    `call_llm_meta` 形状：`(system, user) -> (text | None, finish_reason | None)`，
    用来模拟 `run_agent._summary_llm_meta` 透出 `finish_reason`。
    """
    calls = []

    def _llm(system, user):
        calls.append((system, user))
        return text, finish

    return _llm, calls


def _fresh(payload_chars: int = 4000, **kw) -> tuple:
    c = ContextCompressor(context_length=64_000, micro_compact=True, **kw)
    return c, build_history(payload_chars=payload_chars)


# ══════════════════════════════════════════════════════════════════ 1. 交换边界

def test_find_exchange_skips_user_and_summary_markers() -> None:
    """A1①：跳过 user 行与（assistant 角色的）摘要标记，取第一条**真 assistant**。"""
    msgs = [
        {"role": "user", "content": "u"},                                   # 0 user
        make_micro_marker("旧摘要"),                                        # 1 摘要标记（assistant 角色）
        {"role": "user", "content": "u2"},                                  # 2 user
        {"role": "assistant", "content": "真回复"},                         # 3 ← 应从这里起
        {"role": "user", "content": "u3"},                                  # 4 边界
    ]
    got = find_one_exchange(msgs, 0, len(msgs))
    check("★ 跳过 user 和摘要标记，找到真 assistant 起", got == (3, 4), f"got={got}")
    check("标记不被当成交换起点（否则会自己吃掉自己）",
          find_one_exchange(msgs, 1, 2) is None, "1..2 里只有标记/user，应无交换")


def test_find_exchange_must_close() -> None:
    """A1③：**必须能收尾** —— 边界撞上 assistant/tool（轮中途停）就返回 None。"""
    msgs = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c", "content": "r"},
        {"role": "assistant", "content": "继续"},           # 2 —— 边界落在 assistant 上
    ]
    check("★ 边界是 assistant ⇒ 必须能收尾不成立 ⇒ 返回 None（这轮不吞）",
          find_one_exchange(msgs, 0, 3) is None)
    msgs2 = msgs + [{"role": "user", "content": "u"}]
    check("补一条 user 边界后就能收尾", find_one_exchange(msgs2, 0, 4) == (0, 3))


def test_find_exchange_absorbs_assistant_tool_run() -> None:
    """A1②：连续吃 assistant + tool，直到换角色为止。"""
    msgs = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "b", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "b", "content": "rb"},
        {"role": "assistant", "content": "收尾"},
        {"role": "user", "content": "下一个"},
    ]
    got = find_one_exchange(msgs, 0, len(msgs))
    check("★ 一个交换吃掉整段 assistant/tool 连续行（tool 组不拆）", got == (1, 6), f"got={got}")


def test_never_absorbs_user_rows() -> None:
    """A1 的核心契约：**user 行永不被吸收**。"""
    c, msgs = _fresh()
    users_before = [m["content"] for m in msgs if m.get("role") == "user"]
    llm, _ = _recording_llm()
    out = c.micro_compact(msgs, call_llm=llm)
    users_after = [m["content"] for m in out if m.get("role") == "user"]
    check("★ 吸收后 user 行**逐字**还在、条数不变",
          users_after == users_before, f"{len(users_before)} → {len(users_after)}")


# ══════════════════════════════════════════════════════════════════ 2. 滚动摘要/游标

def test_absorbs_exactly_one_marker() -> None:
    c, msgs = _fresh()
    llm, calls = _recording_llm()
    out = c.micro_compact(msgs, call_llm=llm)
    check("一次 pass 造出**恰好一条** micro 标记",
          sum(1 for m in out if is_micro_marker(m)) == 1,
          f"标记 {sum(1 for m in out if is_micro_marker(m))} 条")
    check("一次 pass 后条数**不增**（吸收 1 个交换）", len(out) <= len(msgs), f"{len(msgs)} → {len(out)}")
    # 第二次 pass 吸收的是一个完整工具组（多行），条数应**严格变少**。
    out2 = c.micro_compact(out, call_llm=llm)
    check("★ 连续两次 pass：吸收到多行交换后条数**严格变少**",
          len(out2) < len(msgs) - 1, f"{len(msgs)} → {len(out2)}")
    check("旧 micro 标记被 supersede，只留最新一条",
          sum(1 for m in out2 if is_micro_marker(m)) == 1,
          f"标记 {sum(1 for m in out2 if is_micro_marker(m))} 条")


def test_rolling_summary_recovered_from_marker() -> None:
    """A2：游标丢失（新压缩器实例）时，靠**扫描转写本里最后一条标记**恢复滚动摘要。"""
    c1, msgs = _fresh()
    llm1, _ = _recording_llm("## Goal\n关键决定：用 SQLite\n## Progress\n- 模块0完成")
    out = c1.micro_compact(msgs, call_llm=llm1)
    check("先造出一条带正文的 micro 标记",
          any("关键决定：用 SQLite" in str(m.get("content")) for m in out if is_micro_marker(m)))

    # 全新压缩器 —— 内存状态全丢，模拟新进程 / 新 agent。
    c2, _ = _fresh()
    llm2, calls2 = _recording_llm("合并后的新摘要")
    c2.micro_compact(out, call_llm=llm2)
    check("★ 恢复的滚动摘要出现在下一次提示词的『当前摘要』里",
          bool(calls2) and "关键决定：用 SQLite" in calls2[0][1],
          calls2[0][1][:120] if calls2 else "(没调用)")
    check("★ 没有额外落盘一份摘要状态（只靠转写本恢复）",
          c2._micro_rolling_summary.startswith("合并后的新摘要"))


def test_rolling_summary_from_marker_strips_wrapper() -> None:
    body = "## Goal\n做 X\n## Progress\n- y"
    content = f"{COMPACTION_HEADER}\n\n{body}\n\n{SUMMARY_END_MARKER}"
    check("从标记正文里抠回摘要（去掉压缩头与结束标记）",
          rolling_summary_from_marker(content) == body)
    check("非字符串/空 → 空串", rolling_summary_from_marker(None) == ""
          and rolling_summary_from_marker("  ") == "")


# ══════════════════════════════════════════════════════════════════ 3. 一回合一个 / no-op

def test_one_exchange_per_pass() -> None:
    """A3：一次 pass 最多吸收**一个**交换（不是能吞多少吞多少）。"""
    c, msgs = _fresh()
    llm, calls = _recording_llm()
    out1 = c.micro_compact(msgs, call_llm=llm)
    check("第一次 pass 只调用一次 LLM（一个交换一次摘要）", len(calls) == 1, f"calls={len(calls)}")
    check("第一次 pass 只造一条标记", sum(1 for m in out1 if is_micro_marker(m)) == 1)
    before = len(calls)
    out2 = c.micro_compact(out1, call_llm=llm)
    check("第二次 pass 再吸收一个（游标推进了）", len(calls) == before + 1)
    check("总标记数仍 ≤ 1（旧 micro 标记被 supersede 只留最新）或为 2 的合理上界",
          sum(1 for m in out2 if is_micro_marker(m)) == 1,
          f"标记 {sum(1 for m in out2 if is_micro_marker(m))} 条")


def test_noop_pass_calls_model_zero_times() -> None:
    """A3：没有可吸收的交换就 **no-op**，一次 LLM 调用都不花。"""
    c = ContextCompressor(context_length=64_000, micro_compact=True)
    small = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
             {"role": "assistant", "content": "a"}, {"role": "user", "content": "u2"}]
    llm, calls = _recording_llm()
    out = c.micro_compact(small, call_llm=llm)
    check("★ 没有可吸收交换 ⇒ no-op，零 LLM 调用", len(calls) == 0, f"calls={len(calls)}")
    check("no-op 返回原列表（条数不变）", len(out) == len(small))
    check("no-op 不置 dirty（调用方不落库）", c._micro_dirty is False)


def test_cadence_gate_every_n() -> None:
    """A3：频率门 —— 每 N 回合才跑一次 pass。"""
    c, msgs = _fresh(micro_compact_every_n_turns=3)
    llm, calls = _recording_llm()
    c.micro_compact(msgs, call_llm=llm)      # 第 1 回合：被频率门挡下
    c.micro_compact(msgs, call_llm=llm)      # 第 2 回合：仍被挡
    check("每 3 回合才跑一次：前两回合零调用", len(calls) == 0, f"calls={len(calls)}")
    c.micro_compact(msgs, call_llm=llm)      # 第 3 回合：放行
    check("第三个回合放行，调用一次", len(calls) == 1, f"calls={len(calls)}")


# ══════════════════════════════════════════════════════════════════ 4. 失败

def test_failure_does_not_absorb_or_persist() -> None:
    """A4：摘要失败 ⇒ **不吸收、不落 dirty**（留着重试）。"""
    c, msgs = _fresh()
    llm, calls = _none_llm()
    out = c.micro_compact(msgs, call_llm=llm)
    check("★ 摘要返回 None ⇒ 消息列表原样返回（不吸收）", out is msgs)
    check("没造任何 micro 标记", sum(1 for m in out if is_micro_marker(m)) == 0)
    check("★ 失败不置 dirty（调用方据此**不落库**）", c._micro_dirty is False)
    check("滚动摘要仍为空", c._micro_rolling_summary == "")


def test_summarize_exception_is_failure_not_crash() -> None:
    c, msgs = _fresh()
    llm, _ = _recording_llm(raise_exc=True)
    out = c.micro_compact(msgs, call_llm=llm)                 # 不应抛
    check("摘要调用抛异常被吞成失败（不崩回合）", len(out) == len(msgs) and not c._micro_dirty)


def test_three_consecutive_failures_push_cursor() -> None:
    """A4：**同一交换连续 3 次失败** ⇒ 把游标推到卡住的交换**之后**，别每轮空烧。"""
    check("阈值就是 3（硬编码，不让测试自我循环）",
          MICRO_COMPACT_MAX_CONSECUTIVE_FAILURES == 3)
    c, msgs = _fresh()
    first = c._next_micro_exchange(msgs)
    check("先解出第一个可吸收的交换", first is not None, f"first={first}")
    ex_start, ex_end = first
    llm, calls = _none_llm()
    for _ in range(3):                       # ★ 硬编码 3 次
        c.micro_compact(msgs, call_llm=llm)
    check("★ 连续 3 次失败后游标被推到**这个交换之后**（== exchange_end）",
          c._micro_cursor == ex_end, f"cursor={c._micro_cursor} != exchange_end={ex_end}")
    check("失败计数复位（下一次失败重新从 1 数）", c._micro_consecutive_failures == 0)
    # 第 4 次：游标已过第一个交换，应解到**更靠后**的一个
    c.micro_compact(msgs, call_llm=llm)
    check("★ 第 4 次尝试的是一个**新的**交换（游标推过了第一个）",
          c._micro_last_failure_cursor > ex_start,
          f"last_failure_cursor={c._micro_last_failure_cursor} > ex_start={ex_start}")


def test_truncated_summary_is_discarded() -> None:
    """★ `finish_reason == "length"` ⇒ 半截合并 ⇒ 丢弃、不吸收、留着重试。

    与"摘要返回空"是**两条不同的**判据：截断时 HTTP 200、**有内容**，只有 finish 判得住。
    """
    c, msgs = _fresh()
    llm, calls = _meta_llm("半截摘要（被截断）", "length")
    out = c.micro_compact(msgs, call_llm_meta=llm)
    check("确实调了一次（截断发生在拿到响应之后）", len(calls) == 1, f"calls={len(calls)}")
    check("★ 截断 ⇒ 消息列表原样返回（丢弃半截、不吸收）", out is msgs)
    check("没造任何 micro 标记", sum(1 for m in out if is_micro_marker(m)) == 0)
    check("★ 截断不置 dirty（调用方据此**不落库**）", c._micro_dirty is False)
    check("★ 半截没进滚动摘要（仍为空）", c._micro_rolling_summary == "")
    check("★ 失败计数 +1（照常记失败、留着重试）", c._micro_consecutive_failures == 1,
          f"failures={c._micro_consecutive_failures}")


def test_stop_finish_summary_is_absorbed() -> None:
    """★ `finish_reason == "stop"` ⇒ 正常结束 ⇒ 照常吸收。"""
    c, msgs = _fresh()
    llm, calls = _meta_llm("## Goal\n完整摘要正文", "stop")
    out = c.micro_compact(msgs, call_llm_meta=llm)
    check("确实调了一次", len(calls) == 1, f"calls={len(calls)}")
    check("★ 正常结束 ⇒ 恰好吸收一条 micro 标记",
          sum(1 for m in out if is_micro_marker(m)) == 1,
          f"标记 {sum(1 for m in out if is_micro_marker(m))} 条")
    check("★ 正常吸收 ⇒ dirty 为 True（调用方据此落库）", c._micro_dirty is True)
    check("滚动摘要写入了完整正文", "完整摘要正文" in c._micro_rolling_summary)
    # 同时给了旧 call_llm 与新 call_llm_meta ⇒ **优先** meta（能判截断）。
    c2, msgs2 = _fresh()
    meta, meta_calls = _meta_llm("来自 meta 的摘要", "stop")
    old, old_calls = _recording_llm("来自旧 call_llm 的摘要")
    c2.micro_compact(msgs2, call_llm=old, call_llm_meta=meta)
    check("★ 两个注入点都给了 ⇒ 用 meta（判得了截断的那个）",
          len(meta_calls) == 1 and len(old_calls) == 0,
          f"meta={len(meta_calls)} old={len(old_calls)}")
    check("吸收的是 meta 的摘要", "来自 meta 的摘要" in c2._micro_rolling_summary)


def test_legacy_call_llm_still_absorbed() -> None:
    """③ 兼容性回归：只给旧 `call_llm`（finish 未知）时照常吸收，**不**被当成截断。"""
    c, msgs = _fresh()
    llm, calls = _recording_llm("## Goal\n旧注入点的摘要")
    out = c.micro_compact(msgs, call_llm=llm)
    check("★ 只有旧 call_llm ⇒ 照常吸收一条标记",
          sum(1 for m in out if is_micro_marker(m)) == 1,
          f"标记 {sum(1 for m in out if is_micro_marker(m))} 条")
    check("旧注入点也置 dirty", c._micro_dirty is True)
    check("签名兼容：不传 call_llm_meta 不报错", len(calls) == 1)


# ══════════════════════════════════════════════════════════════════ 5. 标记形状

def test_marker_shape_and_keys() -> None:
    """A5：标记形状 —— assistant 角色 + 三个键。"""
    m = make_micro_marker("摘要正文")
    check("角色是 assistant", m.get("role") == "assistant")
    check("带 `_compressed_summary` 键", m.get(COMPRESSED_SUMMARY_METADATA_KEY) is True)
    check("带 `_micro_compact_marker` 键", m.get(MICRO_COMPACT_MARKER_KEY) is True)
    check("`_compressed_summary_has_user_turn` 为 False（micro 只吸收 assistant/tool）",
          m.get(COMPRESSED_SUMMARY_HAS_USER_TURN_KEY) is False)
    check("正文含压缩头 + 结束标记",
          COMPACTION_HEADER in m["content"] and SUMMARY_END_MARKER in m["content"])
    check("是摘要标记、也是 micro 标记", is_context_summary_message(m) and is_micro_marker(m))


def test_supersede_drops_only_micro_markers() -> None:
    """A5：supersede 只丢 micro 标记；**批量标记绝不能丢**（它含滚动摘要里没有的历史）。"""
    micro_old = make_micro_marker("旧 micro 摘要")
    batch = build_batch_marker("批量摘要（历史更多）")
    base = [
        {"role": "user", "content": "u1"},
        micro_old,                              # 1 micro（可丢）
        batch,                                  # 2 批量（不可丢）
        {"role": "user", "content": "u2"},      # 3
        {"role": "assistant", "content": "a"},  # 4 ← 被替换成新标记
        {"role": "user", "content": "u3"},      # 5
    ]
    out = splice_micro_marker(base, 4, 5, "新 micro 摘要", supersede=True)
    check("★ 旧 micro 标记被丢掉", not any(m is micro_old for m in out))
    check("★ 批量标记**仍在**（不能当旧版丢）", any(m is batch for m in out))
    check("结果里只留**一条** micro 标记",
          sum(1 for m in out if is_micro_marker(m)) == 1,
          f"micro 标记 {sum(1 for m in out if is_micro_marker(m))} 条")
    check("新标记正文是新摘要", any("新 micro 摘要" in str(m.get("content")) for m in out if is_micro_marker(m)))


def test_supersede_merges_adjacent_user_turns() -> None:
    """A5：丢旧标记后可能留下相邻 user 行 ⇒ 用 `\\n\\n` 拼（保交替）。"""
    micro_old = make_micro_marker("旧")
    base = [
        {"role": "user", "content": "U1"},
        micro_old,                              # 1 —— 丢了之后 U1/U2 相邻
        {"role": "user", "content": "U2"},
        {"role": "assistant", "content": "a"},  # 3
        {"role": "user", "content": "U3"},
    ]
    out = splice_micro_marker(base, 3, 4, "新", supersede=True)
    check("★ 相邻两条纯文本 user 被合并成一条（`\\n\\n` 拼）",
          any(m.get("content") == "U1\n\nU2" for m in out),
          str([m.get("content") for m in out]))
    check("合并后无相邻 user 违规", not [x for x in _alt_violations(out) if "user" in x])
    # 非字符串内容的 user 行不动
    base2 = [{"role": "user", "content": [{"type": "text", "text": "块"}]}, micro_old,
             {"role": "user", "content": [{"type": "text", "text": "块2"}]}]
    out2 = merge_adjacent_user_turns(base2)
    check("列表内容的 user 行**不**被拼接（结构不动）", len(out2) == 3)


def test_cursor_after_marker_uses_spliced_list() -> None:
    """A5：游标必须从**拼装后**的列表推（拼装前下标会落进后面的交换）。"""
    msgs = [
        {"role": "user", "content": "u"},
        make_micro_marker("摘要"),
        {"role": "user", "content": "u2"},
    ]
    check("游标 = 标记的后一格", cursor_after_marker(msgs, fallback=99) == 2)
    check("没有标记时回落 fallback", cursor_after_marker([{"role": "user", "content": "x"}], 7) == 7)


# ══════════════════════════════════════════════════════════════════ 6. 边界复用/不变量

def test_tail_window_untouched() -> None:
    """A6：尾部窗口**绝不**被碰。"""
    c, msgs = _fresh()
    _, tail_start = _boundaries(c, msgs)
    llm, _ = _recording_llm()
    out = c.micro_compact(msgs, call_llm=llm)
    # 尾部各条（按对象身份）应在输出里原样存在
    tail = msgs[tail_start:]
    kept = [m for m in tail if any(m is o for o in out)]
    check("★ 尾部窗口内的消息**逐条原样**保留（对象身份不变）",
          len(kept) == len(tail), f"{len(kept)}/{len(tail)}")


def _boundaries(c: ContextCompressor, msgs: list):
    from agent.context_compressor import determine_boundaries
    return determine_boundaries(
        msgs, protect_first_n=c.effective_protect_first_n(),
        protect_last_n=c.protect_last_n, tail_budget=c.tail_token_budget())


def test_head_window_untouched() -> None:
    c, msgs = _fresh()
    head_end, _ = _boundaries(c, msgs)
    llm, _ = _recording_llm()
    out = c.micro_compact(msgs, call_llm=llm)
    check("★ 头部窗口（含系统提示词）消息原样保留",
          all(any(msgs[i] is o for o in out) for i in range(head_end)))


def test_output_alternation_valid() -> None:
    """不变量①：吸收后角色交替仍合法。"""
    c, msgs = _fresh()
    llm, _ = _recording_llm()
    out = c.micro_compact(msgs, call_llm=llm)
    check("★ 吸收后角色交替合法（无相邻同角色 user/assistant）",
          not _alt_violations(out), "; ".join(_alt_violations(out)))


def test_system_prompt_untouched() -> None:
    """不变量②：微压缩**不**重建也不改系统提示词。"""
    c, msgs = _fresh()
    sys_before = msgs[0]["content"]
    llm, _ = _recording_llm()
    out = c.micro_compact(msgs, call_llm=llm)
    check("★ 系统提示词字节不变（且仍在 index 0）",
          out[0].get("role") == "system" and out[0]["content"] == sys_before)


def test_disabled_is_identity_zero_calls() -> None:
    """默认关：恒等返回、零调用。"""
    c = ContextCompressor(context_length=64_000, micro_compact=False)
    msgs = build_history()
    llm, calls = _recording_llm()
    out = c.micro_compact(msgs, call_llm=llm)
    check("★ 微压缩关（默认）⇒ 恒等返回原对象", out is msgs)
    check("★ 关着时零 LLM 调用", len(calls) == 0, f"calls={len(calls)}")


# ══════════════════════════════════════════════════════════════════ 7. defrag

def test_defrag_rewrites_marker_in_place() -> None:
    """摘要自身超阈值 ⇒ 就地把最新 micro 标记重压（不加消息、不推游标）。"""
    c, msgs = _fresh()
    llm, _ = _recording_llm("很长的滚动摘要" + "内容" * 50)
    out = c.micro_compact(msgs, call_llm=llm)
    n_before = len(out)
    markers_before = sum(1 for m in out if is_micro_marker(m))
    # 把阈值调到极小 ⇒ 下一次 pass 命中 defrag
    c.micro_compact_defrag_threshold_tokens = 1
    llm2, calls2 = _recording_llm("紧凑重写后的摘要")
    out2 = c.micro_compact(out, call_llm=llm2)
    check("defrag 触发（调了一次 LLM）", len(calls2) == 1, f"calls={len(calls2)}")
    check("★ 条数不变（就地改写，不 splice）", len(out2) == n_before, f"{n_before} → {len(out2)}")
    check("标记数不变", sum(1 for m in out2 if is_micro_marker(m)) == markers_before)
    check("★ 最新 micro 标记正文被改写", any("紧凑重写后的摘要" in str(m.get("content"))
                                            for m in out2 if is_micro_marker(m)))
    check("defrag 也置 dirty（调用方据此落库）", c._micro_dirty is True)
    check("★ 分支标签 = defrag", c._micro_last_action == "defrag", f"action={c._micro_last_action}")


def test_defrag_failure_leaves_summary_intact() -> None:
    c, msgs = _fresh()
    llm, _ = _recording_llm("初始摘要")
    out = c.micro_compact(msgs, call_llm=llm)
    c.micro_compact_defrag_threshold_tokens = 1
    before = c._micro_rolling_summary
    llm2, _ = _none_llm()
    out2 = c.micro_compact(out, call_llm=llm2)
    check("defrag 失败时不改摘要、不置 dirty",
          c._micro_rolling_summary == before and c._micro_dirty is False)


# ══════════════════════════════════════════════════════════════════ 8. 配置夹取

def test_config_defaults_and_clamping() -> None:
    d = get_compression_config({})
    check("默认：micro_compact=False（默认关）", d["micro_compact"] is False)
    check("默认：every_n_turns=1", d["micro_compact_every_n_turns"] == 1)
    check("默认：defrag_threshold=2000", d["micro_compact_defrag_threshold_tokens"] == 2000)
    check("默认：min_reclaim=4096", d["proactive_prune_min_reclaim_tokens"] == 4096)

    clamp = get_compression_config({"compression": {
        "micro_compact": "yes",
        "micro_compact_every_n_turns": -5,
        "micro_compact_defrag_threshold_tokens": 0,
        "proactive_prune_min_reclaim_tokens": -9,
    }})
    check("★ every_n 夹到下界 1（max(1,int)）", clamp["micro_compact_every_n_turns"] == 1)
    check("★ defrag 夹到下界 1", clamp["micro_compact_defrag_threshold_tokens"] == 1)
    check("★ min_reclaim 夹到 0（0=无闸门）", clamp["proactive_prune_min_reclaim_tokens"] == 0)
    check("真值字符串 'yes' → True", clamp["micro_compact"] is True)


def test_parse_config_int_rejects_bool_and_fraction() -> None:
    check("★ bool 不冒充数字（YAML true→1 的坑）", parse_config_int(True, 1) == 1)
    check("小数被拒（2.5 不是合法的每 N 回合）", parse_config_int(2.5, 1) == 1)
    check("整数值的浮点收下", parse_config_int(3.0, 1) == 3)
    check("字符串数字尽力解析", parse_config_int("4", 1) == 4)
    check("乱值回落默认", parse_config_int("abc", 7) == 7 and parse_config_int(None, 7) == 7)


def test_config_reads_from_mini_home() -> None:
    """配置从 `$MINI_HOME/config.json` 读（不是真实 home）。"""
    import json as _json
    from pathlib import Path
    tmp = tempfile.mkdtemp(prefix="mini-ch10-cfg-", dir=_TEMP_ROOT.name)
    (Path(tmp) / "config.json").write_text(_json.dumps({
        "compression": {"micro_compact": True, "micro_compact_every_n_turns": 4,
                        "micro_compact_defrag_threshold_tokens": 8000}}), encoding="utf-8")
    token = hermes_constants.set_mini_home_override(tmp)
    try:
        d = get_compression_config()
    finally:
        hermes_constants.reset_mini_home_override(token)
    check("★ 从临时 MINI_HOME 的 config.json 读到 micro_compact=True", d["micro_compact"] is True)
    check("every_n 读到 4", d["micro_compact_every_n_turns"] == 4)
    check("defrag 读到 8000", d["micro_compact_defrag_threshold_tokens"] == 8000)
    check("未配的 min_reclaim 回落 4096", d["proactive_prune_min_reclaim_tokens"] == 4096)


# ══════════════════════════════════════════════════════════════════ 9. 接线（run_agent 收尾钩）

class _StubAgent:
    """最小宿主：验证 `AIAgent._micro_compact_after_turn` 的三个门。"""

    def __init__(self, compressor, llm, db=None, sid="sess-1"):
        self._c, self._llm, self.session_db, self.session_id = compressor, llm, db, sid
        self.statuses = []
        self.persisted = 0

    def _compressor(self):
        return self._c

    def _summary_llm(self, system, user):
        return self._llm(system, user)

    def _summary_llm_meta(self, system, user):
        # 接线后 `_micro_compact_after_turn` 会同时传 call_llm_meta；这里给一个
        # finish="stop" 的桩，等价于"没被截断"（旧 call_llm 的行为）。
        return self._llm(system, user), "stop"

    def _emit_status(self, text):
        self.statuses.append(text)

    def _persist_micro_compaction(self, messages):
        self.persisted += 1


def test_wiring_disabled_zero_calls() -> None:
    from run_agent import AIAgent
    c = ContextCompressor(context_length=64_000, micro_compact=False)
    llm, calls = _recording_llm()
    stub = _StubAgent(c, llm)
    AIAgent._micro_compact_after_turn(stub, build_history())
    check("接线：关着 ⇒ 零调用、零落库", len(calls) == 0 and stub.persisted == 0)


def test_wiring_absorb_notifies_and_persists() -> None:
    from run_agent import AIAgent
    c = ContextCompressor(context_length=64_000, micro_compact=True)
    llm, calls = _recording_llm()
    stub = _StubAgent(c, llm)
    msgs = build_history()
    before = len(msgs)
    AIAgent._micro_compact_after_turn(stub, msgs)
    check("接线：开着 ⇒ 吸收一次（调了一次 LLM）、条数不增",
          len(calls) == 1 and len(msgs) <= before, f"calls={len(calls)}, {before}→{len(msgs)}")
    check("接线：吸收后**落库一次**", stub.persisted == 1)
    check("接线：发了一条 status", any("微压缩" in s for s in stub.statuses))


def test_wiring_failure_no_persist() -> None:
    from run_agent import AIAgent
    c = ContextCompressor(context_length=64_000, micro_compact=True)
    llm, _ = _none_llm()
    stub = _StubAgent(c, llm)
    msgs = build_history()
    AIAgent._micro_compact_after_turn(stub, msgs)
    check("★ 接线：摘要失败 ⇒ **不落库**", stub.persisted == 0)


# ══════════════════════════════════════════════════════════════════ 提示词契约

def test_summary_prompt_contract() -> None:
    system, user = build_micro_summary_prompt("(no prev)", "EXCHANGE")
    check("system 是摘要助手", system.startswith("You are a conversation summarization assistant"))
    check("提示词要求并入决策/文件路径/未决问题",
          "decisions" in user and "file paths" in user and "open questions" in user)
    check("提示词要求保持结构、丢已解决细节",
          "Preserve the summary's structure" in user and "Drop resolved details" in user)
    check("提示词要求凭据 [REDACTED]", "REDACTED" in user)
    check("提示词要求只返回摘要文本", "Return ONLY the updated summary text" in user)
    check("现有摘要嵌进提示词", "Current Running Summary" in user and "(no prev)" in user)


def test_no_middle_segment_is_noop_when_tail_covers_all() -> None:
    """A6：整段历史都落在**头部+尾部保护**里（没有中间段）⇒ no-op，**绝不伸进尾部**。

    ★ 这条与 `test_noop_pass_calls_model_zero_times` 覆盖的是**两条不同的** no-op 路径：
    那条是头部罩住整段，这条是**尾部**罩住整段 —— 反向验证时只有后者能照出"尾部保护失效"。
    """
    c = ContextCompressor(context_length=64_000, micro_compact=True)
    msgs = [{"role": "system", "content": "s" * 200},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "tool", "tool_call_id": "t1", "content": "x" * 400},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"}]
    head_end, tail_start = _boundaries(c, msgs)
    check("（前提）尾部把整段历史都罩住了（head_end >= tail_start）",
          head_end >= tail_start, f"head={head_end} tail={tail_start}")
    llm, calls = _recording_llm()
    before = list(msgs)
    out = c.micro_compact(msgs, call_llm=llm)
    check("★ 没有中间段 ⇒ no-op（零 LLM 调用）", len(calls) == 0, f"calls={len(calls)}")
    check("★ 消息一条都没动（不许伸进尾部）",
          len(out) == len(before) and all(a is b for a, b in zip(out, before)))
    check("没有造出 micro 标记", sum(1 for m in out if is_micro_marker(m)) == 0)


# ══════════════════════════════════════════════════════════════════ 10. 分支标签（真跑抓出的回归）

def test_action_label_is_absorb_when_count_unchanged() -> None:
    """★ 回归：吸收**不一定**改变消息条数 —— 交换只含 1 条消息时是"删 1 加 1"。

    旧写法（`run_agent._micro_compact_after_turn` 的早期版本）按"消息条数变没变"
    反推 absorb / defrag ⇒ 把这种吸收**误报成 defrag**（真跑两次的回合 1 都是）。
    契约：分支只能由**当次真的走了哪条**决定 —— 压缩器记 `_micro_last_action`。
    """
    c, msgs = _fresh(payload_chars=6000)
    llm, calls = _recording_llm("## 摘要\n- 进度：读完模块 0")
    out = c.micro_compact(msgs, call_llm=llm)
    n_markers = sum(1 for m in out if is_micro_marker(m))
    check("★ 这个形状下吸收**不改变条数**（1 条消息的交换：删 1 加 1）",
          len(out) == len(msgs) and n_markers == 1, f"{len(msgs)} → {len(out)} · 标记 {n_markers}")
    check("★ 分支标签 = absorb（不是 defrag）", c._micro_last_action == "absorb",
          f"action={c._micro_last_action}")
    check("确实只有一次 LLM 调用", len(calls) == 1, f"calls={len(calls)}")
    check("吸收仍然置 dirty（调用方据此落库）", c._micro_dirty is True)


def test_hook_status_text_follows_branch_not_count() -> None:
    """★ 回归（**用户可见面**）：钩子文案必须按分支标 —— 条数没变也可能是吸收。

    这一条直接钉住真跑抓出的那个缺陷：`run_agent._micro_compact_after_turn`
    曾用 `len(messages) != before` 决定文案，于是"吸收但条数不变"被报成
    "微压缩：defrag 重压了滚动摘要（条数不变）"。
    """
    import run_agent

    c, msgs = _fresh(payload_chars=6000)

    class _Stub:
        """只实现钩子真正用到的那几个方法（不联网、不落库）。"""

        def __init__(self) -> None:
            self.statuses: list = []
            self.persisted: list = []

        def _compressor(self):
            return c

        def _summary_llm(self, system, user):
            return "## 摘要\n- 进度：读完模块 0"

        def _summary_llm_meta(self, system, user):
            return "## 摘要\n- 进度：读完模块 0", "stop"

        def _emit_status(self, text: str) -> None:
            self.statuses.append(text)

        def _persist_micro_compaction(self, messages) -> None:
            self.persisted.append(list(messages))

    stub = _Stub()
    before = len(msgs)
    run_agent.AIAgent._micro_compact_after_turn(stub, msgs)
    check("★ 这次吸收**条数没变**（形状前提）", len(msgs) == before, f"{before} → {len(msgs)}")
    check("★ 钩子文案 = 吸收（**不是** defrag 重压）",
          bool(stub.statuses) and "吸收" in stub.statuses[-1] and "defrag" not in stub.statuses[-1],
          f"statuses={stub.statuses}")
    check("吸收 ⇒ 走了落库", len(stub.persisted) == 1, f"persisted={len(stub.persisted)}")


def test_action_label_none_on_noop_and_disabled() -> None:
    """`none` = 这一轮压根没动手（关着 / 无可吸收交换 / 失败）。"""
    llm, calls = _recording_llm()
    c, msgs = _fresh()
    c.micro_compact_enabled = False
    c.micro_compact(msgs, call_llm=llm)
    check("关着时分支 = none", c._micro_last_action == "none")
    c2 = ContextCompressor(context_length=64_000, micro_compact=True)
    c2.micro_compact([{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
                      {"role": "assistant", "content": "a"}, {"role": "user", "content": "u2"}],
                     call_llm=llm)
    check("no-op（无可吸收交换）时分支 = none", c2._micro_last_action == "none")
    check("两条路径都零调用", len(calls) == 0, f"calls={len(calls)}")


GROUPS = [
    ("1. 交换边界（A1）",
     "跳 user/标记 · 连续吃 assistant+tool · **必须能收尾** · **user 永不被吸收**",
     [test_find_exchange_skips_user_and_summary_markers,
      test_find_exchange_must_close,
      test_find_exchange_absorbs_assistant_tool_run,
      test_never_absorbs_user_rows]),
    ("2. 滚动摘要与游标（A2）",
     "只留**一份**累积摘要 · 游标丢了靠**扫描最后的标记**恢复 · 不额外落盘",
     [test_absorbs_exactly_one_marker,
      test_rolling_summary_recovered_from_marker,
      test_rolling_summary_from_marker_strips_wrapper]),
    ("3. 一回合一个 + no-op（A3）",
     "一次 pass 最多吸收**一个** · no-op **零调用** · 频率门每 N 回合",
     [test_one_exchange_per_pass,
      test_noop_pass_calls_model_zero_times,
      test_cadence_gate_every_n]),
    ("4. 失败（A4）",
     "失败**不吸收**/不落库 · 异常被吞 · **连续 3 次失败把游标推过** · "
     "**截断（finish_reason=length）当失败、丢弃半截** · stop 正常吸收 · 旧注入点兼容",
     [test_failure_does_not_absorb_or_persist,
      test_summarize_exception_is_failure_not_crash,
      test_three_consecutive_failures_push_cursor,
      test_truncated_summary_is_discarded,
      test_stop_finish_summary_is_absorbed,
      test_legacy_call_llm_still_absorbed]),
    ("5. 标记形状（A5）",
     "assistant 角色 + 三键 · supersede **只丢 micro 标记** · 相邻 user 合并 · 游标从拼装后推",
     [test_marker_shape_and_keys,
      test_supersede_drops_only_micro_markers,
      test_supersede_merges_adjacent_user_turns,
      test_cursor_after_marker_uses_spliced_list]),
    ("6. 边界复用与不变量（A6）",
     "**尾部窗口不碰** · 头部不碰 · 交替合法 · **系统提示词不动** · 默认关是恒等",
     [test_tail_window_untouched,
      test_no_middle_segment_is_noop_when_tail_covers_all,
      test_head_window_untouched,
      test_output_alternation_valid,
      test_system_prompt_untouched,
      test_disabled_is_identity_zero_calls]),
    ("7. defrag",
     "摘要超阈值就地重压**最新** micro 标记（不加消息）· 失败不改",
     [test_defrag_rewrites_marker_in_place,
      test_defrag_failure_leaves_summary_intact]),
    ("8. 配置夹取（默认值表）",
     "四个键默认 + `max(1/0,int)` 夹取 · bool 不冒充数字 · 从 MINI_HOME 读",
     [test_config_defaults_and_clamping,
      test_parse_config_int_rejects_bool_and_fraction,
      test_config_reads_from_mini_home]),
    ("9. 接线（run_agent 收尾钩）+ 提示词",
     "关⇒零调用 · 吸收⇒落库一次+通知 · 失败⇒不落库 · 提示词契约",
     [test_wiring_disabled_zero_calls,
      test_wiring_absorb_notifies_and_persists,
      test_wiring_failure_no_persist,
      test_summary_prompt_contract]),
    ("10. 分支标签（真跑抓出的回归）",
     "分支由**当次真的走了哪条**决定 · 「吸收但条数不变」不许被报成 defrag",
     [test_action_label_is_absorb_when_count_unchanged,
      test_hook_status_text_follows_branch_not_count,
      test_action_label_none_on_noop_and_disabled]),
]


if __name__ == "__main__":
    try:
        print(f"解释器：{sys.executable}  Python {sys.version.split()[0]}\n")
        defined = {n for n in globals() if n.startswith("test_")}
        registered = {f.__name__ for _, _, fns in GROUPS for f in fns}
        missing = defined - registered
        if missing:
            print(f"⚠️ 有测试函数没登记进 GROUPS（写了但不会跑）：{sorted(missing)}")
            sys.exit(2)

        for title, promise, fns in GROUPS:
            print(f"══ {title} ══ {promise}")
            for fn in fns:
                fn()
            print(f"   → {len(PASS)} 累计 / {len(FAIL)} 失败\n")

        print(f"{len(PASS)} 通过 / {len(FAIL)} 失败　（{len(GROUPS)} 组）")
        if FAIL:
            print("失败项：" + "; ".join(FAIL))
        sys.exit(1 if FAIL else 0)
    finally:
        _TEMP_ROOT.cleanup()
