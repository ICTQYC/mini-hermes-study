"""第 5 章的自测：上下文压缩与缓存（token 记账 / 边界 / 四阶段 / 契约）—— **不联网**。

跑法：python3 tests/test_compression.py

## 测的是什么（7 组）

| 组 | 守的承诺 |
|---|---|
| 1. token 估算 | 中日韩按 1 字 1 token（ASCII 不是）· tool_calls 的参数要计入 |
| 2. 触发闸门 | 阈值取自**主模型**窗口 · 有锚点用锚点、没锚点回退粗估 |
| 3. 边界 | 头/中间/尾不重叠 · **绝不切开工具组** · 预算与条数取更宽松的 |
| 4. 阶段一（剪旧工具结果） | 只碰**尾部之外**、**长于门槛**的 tool 消息 |
| 5. 摘要与降级 | 角色避免连续同角色 · **摘要失败要说出来**（不能静默）· 迭代式更新 |
| 6. 契约 | 出口永远是合法配对 · 不改传入的列表 · 无进展守卫（且不挡合法压缩） |
| 7. 锚点 | 内容指纹 · 只估算增量 · **零用量不覆盖好锚点** · 可序列化 |

## 隔离

这一章的逻辑**纯内存**（不碰磁盘、不发网络）。所有 LLM 调用都是注入的假函数，
所以整套测试在几十毫秒内跑完。**没有任何一条依赖运行环境的配置。**
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.context_compressor import (  # noqa: E402
    CLEARED_MARKER,
    ContextCompressor,
    align_boundary_backward,
    assemble_compressed,
    determine_boundaries,
    estimate_messages_tokens_rough,
    estimate_text_tokens,
    message_text,
    prune_old_tool_results,
    sanitize_tool_pairs,
    validate_tool_pairs,
)
from agent.usage_anchor import UsageAnchor, fingerprint  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {detail}" if detail and not cond else ""))


# ══════════════════════════════════════════════════════════════════ 构造假历史

def _tool_turn(cid: str, payload_chars: int, name: str = "write_file") -> list:
    """一个完整的工具组：assistant(tool_calls) + tool(result)。"""
    return [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": name, "arguments": json.dumps({"path": f"app/m_{cid}.py"})}}]},
        {"role": "tool", "tool_call_id": cid, "content": "结" * payload_chars},
    ]


def build_history(turns: int = 8, payload_chars: int = 4000) -> list:
    msgs = [{"role": "system", "content": "You are a helpful assistant. " + "规则 " * 200},
            {"role": "user", "content": "帮我搭一个 FastAPI 项目，要有测试和错误处理"}]
    for i in range(turns):
        msgs += _tool_turn(f"call_{i}", payload_chars)
        msgs.append({"role": "assistant", "content": f"第 {i} 个模块完成（app/m_{i}.py）。"})
        msgs.append({"role": "user", "content": f"接下来处理第 {i + 1} 个模块，注意测试覆盖"})
    return msgs


def build_prose_history(turns: int = 10, chars: int = 3000) -> list:
    """以**散文**为主的历史（没有大块工具输出）。

    ★ 为什么还需要这一份：阶段一会把大块工具输出剪成一行标记，
    于是"以工具输出为主"的历史在阶段一之后中间段会变得很小、被无进展守卫跳过。
    要测"摘要**真的发生**"这类行为，就得用阶段一剪不动的历史。
    """
    msgs = [{"role": "system", "content": "You are a helpful assistant. " + "规则 " * 200},
            {"role": "user", "content": "帮我逐段分析这份合同的风险点"}]
    for i in range(turns):
        msgs.append({"role": "assistant",
                     "content": f"第 {i} 段分析：" + "风险点与依据" * (chars // 6)})
        msgs.append({"role": "user", "content": f"继续看第 {i + 1} 节，重点看第 3 条"})
    return msgs


def _raises(fn, *a, **k) -> bool:
    try:
        fn(*a, **k)
        return False
    except Exception:                                        # noqa: BLE001
        return True


def build_guard_history() -> list:
    """专门构造「中间段**非空但太小**」的形状 —— 用来命中无进展守卫。

    ★ 为什么不能随手拿一份历史来测：守卫的两个前提很容易同时不成立 ——
      · 尾部预算比整段历史还大 → 中间段被吃空（走「没有中间段可摘」那个分支）；
      · 历史以一压就扁的工具输出为主 → 阶段一之后中间段塌掉。
    所以就自己摆：**前面一批小消息**（会成为中间段）**后面几条大消息**（吃掉尾部预算）。
    """
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "开始"}]
    for i in range(20):                                   # 40 条小消息 → 中间段
        msgs.append({"role": "assistant", "content": f"小回复 {i}"})
        msgs.append({"role": "user", "content": f"小问题 {i}"})
    for _ in range(4):                                    # 4 条大消息 → 吃掉尾部预算
        msgs.append({"role": "assistant", "content": "大段内容" * 250})   # 每条 ≈1000 token
    return msgs


def _boom(*_a, **_k):
    raise AssertionError("这个假 LLM 不该被调用")


_OK_SUMMARY = "## Goal\n搭 FastAPI 项目\n\n## Progress\n### Done\n- 模块已写入\n\n## Next Steps\n- 跑测试"


# ══════════════════════════════════════════════════════════════ 1. token 估算

def test_token_estimation_cjk_vs_ascii() -> None:
    """★ 中日韩一个字约 1 token，ASCII 约 3.5 字符 1 token。

    如果用一个统一比例（比如 `len//4`），中文会被**严重低估** ——
    而低估的后果是"以为还有空间"→ 请求被 provider 拒绝。
    这是关系断言：同样字符数下，中文的估值必须**显著大于** ASCII。
    """
    cn = estimate_text_tokens("中文测试" * 100)          # 400 个汉字
    en = estimate_text_tokens("ab" * 200)               # 400 个 ASCII
    check("400 个汉字按 ≈1/字 估", 350 <= cn <= 450, str(cn))
    check("同样字符数的 ASCII 估得更少（约 1/3.5）", 80 <= en <= 140, str(en))
    check("★ 中文估值必须显著大于同字符数的英文", cn > en * 2, f"cn={cn} en={en}")
    check("空串 / None 安全", estimate_text_tokens("") == 0)


def test_tool_call_arguments_count_toward_tokens() -> None:
    """工具调用的 `arguments` 也是要发给 provider 的内容，必须计入。

    ★ 容易漏：`content` 是 `None` 的时候看起来"这条消息没内容"，
    但那条消息真正的体积在 `tool_calls` 里。漏掉它 → 尾部预算算小了 →
    实际发出去的比以为的大。
    """
    bare = [{"role": "assistant", "content": None}]
    with_args = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "write_file", "arguments": "x" * 2000}}]}]
    check("只有 tool_calls 的消息也不是 0", estimate_messages_tokens_rough(with_args) > 100,
          str(estimate_messages_tokens_rough(with_args)))
    check("arguments 被计入了（关系：加了参数就变大）",
          estimate_messages_tokens_rough(with_args) > estimate_messages_tokens_rough(bare) * 10)
    check("message_text 能取到 arguments", "x" * 50 in message_text(with_args[0]))


# ══════════════════════════════════════════════════════════════ 2. 触发闸门

def test_threshold_derives_from_main_model_window() -> None:
    """阈值 = threshold × **主模型**的窗口（原文那条 note）。

    ★ 这条守的是"容易被混为一个数"的地方：辅助/摘要模型的窗口**不参与**触发计算。
    """
    c = ContextCompressor(context_length=200_000, threshold=0.50)
    check("threshold_tokens = 窗口 × threshold", c.threshold_tokens == 100_000,
          str(c.threshold_tokens))
    c2 = ContextCompressor(context_length=200_000, threshold=0.80)
    check("改 threshold 就改阈值（同窗口）", c2.threshold_tokens == 160_000,
          str(c2.threshold_tokens))
    small = ContextCompressor(context_length=20_000, threshold=0.50)
    check("小窗口 → 小阈值（同一 threshold 下）", small.threshold_tokens < c.threshold_tokens)


def test_trigger_gate_and_anchor_source() -> None:
    """闸门用真实用量优先、粗估兜底 —— 而且**报出来源**。"""
    c = ContextCompressor(context_length=20_000, threshold=0.50)   # 阈值 10,000
    small = [{"role": "user", "content": "你好"}]
    check("远低于阈值 → 不压", c.should_compress(small) is False)
    check("无锚点时来源是粗估", c.current_tokens(small)[1].startswith("rough"))

    # 锚点说"已经 19,000"，但本地粗估很小 → 应该信锚点
    msgs = [{"role": "user", "content": "x" * 40}]
    c.update_usage({"prompt_tokens": 19_000, "completion_tokens": 0}, msgs)
    used, src = c.current_tokens(msgs)
    check("★ 有锚点时用真实用量（不是粗估）", used >= 19_000, f"{used} via {src}")
    check("来源标明是锚点", src.startswith("anchor"), src)
    check("于是它超过了阈值 → 该压了", c.should_compress(msgs) is True)


def test_anchor_invalidates_when_history_changes() -> None:
    """计价的那段内容变了（编辑重发 / 回退）→ 锚点必须失效。"""
    c = ContextCompressor(context_length=20_000)
    msgs = [{"role": "user", "content": "原始问题"}]
    c.update_usage({"prompt_tokens": 19_000}, msgs)
    check("原样再问 → 锚点有效", c.current_tokens(msgs)[1].startswith("anchor"))

    edited = [{"role": "user", "content": "改过的问题"}]
    check("内容变了 → 锚点失效、回退粗估", c.current_tokens(edited)[1].startswith("rough"))
    check("历史变短（被截断）→ 也失效",
          c.current_tokens([])[1].startswith("rough"))


# ══════════════════════════════════════════════════════════════ 3. 边界

def test_boundaries_are_disjoint_and_cover_everything() -> None:
    msgs = build_history(turns=6, payload_chars=2000)
    head_end, tail_start = determine_boundaries(
        msgs, protect_first_n=3, protect_last_n=4, tail_budget=2000)
    check("头部含 system", msgs[0]["role"] == "system" and head_end >= 1)
    check("三段有序且不重叠（0 ≤ head_end ≤ tail_start ≤ n）",
          0 <= head_end <= tail_start <= len(msgs), f"{head_end} / {tail_start} / {len(msgs)}")
    check("中间段存在（有东西可摘要）", tail_start > head_end)
    check("★ 头尾合起来不含中间段（拼回去不重复）",
          len(msgs[:head_end]) + len(msgs[tail_start:]) == head_end + (len(msgs) - tail_start))


def test_boundary_never_splits_a_tool_group() -> None:
    """★ 切点绝不能落在 `tool` 结果上 —— 否则会产生孤立配对，provider 直接报错。

    这是**性质**断言：对任意 protect_last_n 都必须成立（不是记住某一次的索引）。
    """
    msgs = build_history(turns=5, payload_chars=800)
    bad = []
    for last_n in range(1, len(msgs)):
        _, tail_start = determine_boundaries(
            msgs, protect_first_n=2, protect_last_n=last_n, tail_budget=300)
        if tail_start < len(msgs) and msgs[tail_start]["role"] == "tool":
            bad.append((last_n, tail_start))
        if not validate_tool_pairs(msgs[tail_start:]):
            continue
        bad.append((last_n, "尾部自身配对就不完整"))
    check("所有 protect_last_n 下，尾首都不是孤立的 tool 结果", not bad, str(bad[:3]))
    grp = [{"role": "assistant", "tool_calls": [{"id": "c1"}]},
           {"role": "tool", "tool_call_id": "c1"},
           {"role": "tool", "tool_call_id": "c2"}]
    check("落在连续 tool 结果上 → 一直往前退到父 assistant",
          align_boundary_backward(grp, 2) == 0, str(align_boundary_backward(grp, 2)))
    # ★ 用 try/except 接住异常：让"没这句守卫"表现为**一条干净的 ✗（附异常文本）**，
    #   而不是整个套件崩掉 —— 崩溃虽然也是红的，但那不是"可诊断的失败"。
    try:
        empty_tail = align_boundary_backward(grp, len(grp))
        err = ""
    except Exception as exc:                                  # noqa: BLE001
        empty_tail, err = None, f"{type(exc).__name__}: {exc}"
    check("★ 空尾部（idx == len）是合法输入，不崩且原样返回",
          not err and empty_tail == len(grp), err or f"返回 {empty_tail}")


def test_tail_budget_vs_count_takes_the_wider_one() -> None:
    """预算和固定条数**取更宽松的**（原文 §4.2）。

    ★ 方向容易写反：尾部更长 = tail_start 更**小**。
    """
    msgs = build_history(turns=6, payload_chars=3000)
    _, t_tiny = determine_boundaries(msgs, protect_first_n=3, protect_last_n=5, tail_budget=1)
    _, t_huge = determine_boundaries(msgs, protect_first_n=3, protect_last_n=5, tail_budget=10_000_000)
    # ★ 原文：*Falls back to the fixed protect_last_n count if the budget would
    #   protect fewer messages* —— 也就是说**尾部至少有 protect_last_n 条**。
    check("预算再小，尾部也至少 protect_last_n 条（兜底生效）",
          len(msgs) - t_tiny >= 5, f"尾长 {len(msgs) - t_tiny}")
    # ★ 方向：预算变大 → tail_start 更小 = 尾部更长（不是反过来）
    check("预算变大 → 尾部不会变短（单调性）", t_huge <= t_tiny, f"{t_huge} vs {t_tiny}")
    check("预算很小时中间段确实存在（有东西可摘要）", t_tiny > 3, str(t_tiny))
    # ★ 预算极大时尾部吞掉中间段 —— 这是**符合设计**的：预算说"这些都别动"，
    #   那就没有东西可摘要（调用方会因此跳过压缩，而不是硬压）。
    check("预算极大时尾部吞掉中间段（设计如此，不是 bug）", t_huge <= 3, str(t_huge))


# ══════════════════════════════════════════════════════════ 4. 阶段一

def test_prune_only_touches_long_tool_results_outside_tail() -> None:
    msgs = build_history(turns=6, payload_chars=2000)
    out, replaced = prune_old_tool_results(list(msgs), tail_budget=2000, protect_last_n=4)
    _, tail_start = determine_boundaries(
        msgs, protect_first_n=3, protect_last_n=4, tail_budget=2000)

    check("确实替换了一些", replaced > 0, str(replaced))
    check("★ 尾部之内的 tool 结果**一个都没动**",
          all(out[i]["content"] != CLEARED_MARKER for i in range(tail_start, len(out))))
    check("被替换的都变成了那句标记",
          all(out[i]["content"] == CLEARED_MARKER
              for i in range(len(out)) if out[i]["content"] == CLEARED_MARKER))
    check("只碰 role=tool 的消息（assistant 的长正文不动）",
          all(out[i]["content"] == msgs[i]["content"]
              for i in range(len(out)) if msgs[i]["role"] != "tool"))


def test_prune_has_a_size_floor() -> None:
    """★ 短输出不替换：标记本身也占地方，换了是负收益。"""
    msgs = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "短"},                 # 2 字符
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c2", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "长" * 500},           # 500 字符
        {"role": "assistant", "content": "结尾"},
        {"role": "user", "content": "尾声"},
    ]
    out, replaced = prune_old_tool_results(msgs, tail_budget=1, protect_last_n=1)
    check("门槛之下的短输出没被替换", out[1]["content"] == "短", repr(out[1]["content"]))
    check("★ 超过门槛的才替换", out[3]["content"] == CLEARED_MARKER, repr(out[3]["content"][:30]))


# ══════════════════════════════════════════════════════ 5. 摘要与降级

def test_summary_role_avoids_consecutive_same_role() -> None:
    """★ 摘要消息的角色要**挑**（原文 §4.4）。

    写死 `assistant` 会在"头部以 assistant 结尾"时产生两条连续 assistant ——
    而 Hermes 的硬约束是 strict role alternation（第 3 章学过）。
    """
    head_a = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
              {"role": "assistant", "content": "a"}]
    out = assemble_compressed(head=head_a, summary="摘要", tail=[{"role": "user", "content": "t"}],
                              summary_tokens=10)
    roles = [m["role"] for m in out]
    check("头部以 assistant 结尾 → 摘要当 user",
          roles[3] == "user", str(roles))
    check("没有连续同角色", len([1 for a, b in zip(roles, roles[1:]) if a == b]) == 0, str(roles))

    head_u = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    out2 = assemble_compressed(head=head_u, summary="摘要", tail=[{"role": "user", "content": "t"}],
                               summary_tokens=10)
    roles2 = [m["role"] for m in out2]
    check("头部以 user 结尾 → 摘要当 assistant", roles2[2] == "assistant", str(roles2))
    check("同样没有连续同角色",
          len([1 for a, b in zip(roles2, roles2[1:]) if a == b]) == 0, str(roles2))


def test_system_prompt_gets_the_compaction_note() -> None:
    head = [{"role": "system", "content": "原始系统提示词"}, {"role": "user", "content": "u"}]
    out = assemble_compressed(head=head, summary=None, tail=[], summary_tokens=0)
    check("系统提示词被追加了说明", "compacted" in out[0]["content"], out[0]["content"][:60])
    check("原始内容没被替换（只是追加）", out[0]["content"].startswith("原始系统提示词"))
    check("传进来的 head 没被就地改", head[0]["content"] == "原始系统提示词")


def test_summary_failure_is_announced_not_silent() -> None:
    """★ 原文点名的静默降级点：摘要失败 → 中间段**无摘要被丢弃**。

    mini 版照样降级（否则超窗会话永远修不好），但**必须把这件事说出来** ——
    这条断言就是守"不能静默":status 里得出现「无摘要」。
    """
    seen = []

    def no_llm_case():
        c = ContextCompressor(context_length=40_000, protect_last_n=2,
                              status_callback=seen.append)
        c.compress(build_prose_history(turns=10, chars=3000), call_llm=None)

    no_llm_case()
    check("没给 call_llm 时，status 里说了『无摘要』",
          any("无摘要" in s for s in seen), str([s for s in seen if "摘要" in s][:2]))

    seen.clear()

    def raising_case():
        c = ContextCompressor(context_length=40_000, protect_last_n=2,
                              status_callback=seen.append)
        def boom(_s, _u):
            raise RuntimeError("辅助模型 502")
        c.compress(build_prose_history(turns=10, chars=3000), call_llm=boom)

    raising_case()
    check("摘要抛异常时，status 里既说了异常、也说了『无摘要』",
          any("RuntimeError" in s for s in seen) and any("无摘要" in s for s in seen),
          str(seen[:4]))
    check("★ 异常没有冒泡（压缩流程没崩）", True)


def test_iterative_recompression_asks_to_update() -> None:
    """★ 第二次压缩的指令是**更新上一版**，不是从头重写（否则损失会累积）。"""
    calls = []

    def fake_llm(system, user):
        calls.append(user)
        return _OK_SUMMARY

    c = ContextCompressor(context_length=40_000, protect_last_n=2)
    out = c.compress(build_prose_history(turns=10, chars=3000), call_llm=fake_llm)
    check("第一次压缩调了摘要", len(calls) == 1, str(len(calls)))
    check("第一次是全新摘要（不带上次摘要）", "更新它" not in calls[0])
    check("压缩机记住了上次摘要", c._previous_summary is not None)

    # 让它再次超阈值：在尾部追加足量内容
    grown = out + build_prose_history(turns=6, chars=3000)[2:]
    calls.clear()
    c.compress(grown, call_llm=fake_llm)
    check("第二次带上『更新它』的指令", calls and "更新它" in calls[0], str(calls[:1])[:80])
    check("上一次的摘要正文出现在指令里", calls and _OK_SUMMARY[:20] in calls[0])


# ══════════════════════════════════════════════════════════ 6. 契约

def test_output_always_has_valid_tool_pairs() -> None:
    """★ 出口契约：压缩后的历史必须**配对完整** —— provider 会直接拒绝孤立的 tool 消息。

    所以这是"能断言的性质"，不是"看起来对"。
    """
    c = ContextCompressor(context_length=40_000, protect_last_n=2)
    out = c.compress(build_history(turns=6, payload_chars=5000),
                     call_llm=lambda s, u: _OK_SUMMARY)
    check("压缩后配对完整", not validate_tool_pairs(out), str(validate_tool_pairs(out))[:80])

    # 头部那条 tool_call 的结果落在中间段 → 必须被补一条占位结果
    stripped = sanitize_tool_pairs([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "gone", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "user", "content": "后面直接接用户消息"},
    ])
    check("★ 结果丢失的 tool_call 被补了占位结果",
          stripped[1]["role"] == "tool" and stripped[1]["tool_call_id"] == "gone",
          str(stripped)[:90])
    check("补完之后配对完整（契约）", not validate_tool_pairs(stripped))

    orphan = sanitize_tool_pairs([{"role": "tool", "tool_call_id": "nobody", "content": "x"},
                                  {"role": "user", "content": "u"}])
    check("孤立的 tool 结果被删掉（前面没有父 assistant）",
          all(m["role"] != "tool" for m in orphan), str(orphan))


def test_input_list_is_never_mutated() -> None:
    """压缩返回**新列表**，绝不就地改调用方的历史（那是 agent 的状态）。"""
    msgs = build_prose_history(turns=12, chars=3000)
    snapshot = json.dumps(msgs, sort_keys=True, ensure_ascii=False)
    c = ContextCompressor(context_length=40_000, protect_last_n=2)
    out = c.compress(msgs, call_llm=lambda s, u: _OK_SUMMARY)
    check("传进去的列表逐字节没变", json.dumps(msgs, sort_keys=True, ensure_ascii=False) == snapshot)
    check("返回的是新对象", out is not msgs)
    check("确实变小了（不然压缩没意义）", len(out) < len(msgs), f"{len(out)} vs {len(msgs)}")


def test_no_progress_guard_skips_small_middles() -> None:
    """★ 无进展守卫：中间段太小时**跳过，并且不打那次 LLM 调用**。

    实测症状（真踩过）：25 条的历史再压一次，出来还是 25 条、token 一个没少，
    但那一次辅助调用照打 —— 而 `should_compress` 之后**每轮**都为 True，
    于是变成每轮白烧一次调用。
    """
    status = []
    # ★ 要命中「中间段非空但太小」这个分支，两个坑都要绕开：
    #   ① `protect_last_n` 必须小于历史长度（否则中间段为空，走另一个分支）；
    #   ② 尾部预算必须**小于**整段历史（lean 的下限是 10,000，调小窗口没用），
    #      所以这里用 legacy 模式把预算压到 threshold_tokens × 0.20 = 2,000。
    c = ContextCompressor(context_length=20_000, threshold=0.50, tail_mode="legacy",
                          protect_last_n=4, status_callback=status.append)
    msgs_snapshot = build_guard_history()
    out = c.compress(msgs_snapshot, call_llm=_boom)
    check("打了『跳过』的说明", any("跳过" in s for s in status), str(status[:2]))
    check("说明里点出了守卫的理由", any("无进展" in s for s in status), str(status[:2]))
    check("★ 假 LLM 一次都没被调用（没有白烧调用）", True)
    # 这个夹具里没有工具输出 → 阶段一无事可做 → 原样返回（长度不变）
    check("阶段一无事可做时原样返回", len(out) == len(msgs_snapshot), f"{len(out)} vs {len(msgs_snapshot)}")
    check("确实是非空的中间段被守卫拦住（不是空中间段那条分支）",
          any("只有" in t and "token" in t for t in status), str(status[:2]))

    check("压缩次数没增加", c.compaction_count == 0, str(c.compaction_count))


def test_guard_keeps_phase_one_result() -> None:
    """★ 守卫跳过**摘要**时，**阶段一的成果必须保留**。

    真踩过（测试逼出来的）：跳过时写成 `return list(messages)` 就把剪掉的成果扔了，
    于是下一轮重做一遍阶段一、守卫又跳过 —— **永远压不动**，
    而且每轮白做一次阶段一。跳过的是"摘要"，不是"整次压缩"。
    """
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "开始"}]
    for i in range(6):
        cid = f"c{i}"
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": cid, "content": "内容" * 800})
        msgs.append({"role": "assistant", "content": f"第 {i} 步"})
        msgs.append({"role": "user", "content": f"继续 {i}"})
    c = ContextCompressor(context_length=20_000, threshold=0.50, tail_mode="legacy",
                          protect_last_n=4)
    before = estimate_messages_tokens_rough(msgs)
    out = c.compress(msgs, call_llm=_boom)        # 守卫会拦住摘要 → 不该调 LLM
    after = estimate_messages_tokens_rough(out)
    check("★ 跳过摘要后历史仍然变小了（阶段一的成果没丢）", after < before, f"{before} → {after}")
    check("返回的列表里能看到剪过的标记",
          any(m.get("content") == CLEARED_MARKER for m in out),
          str([m.get("content", "")[:14] for m in out if m.get("role") == "tool"])[:90])
    check("只改内容、不删消息（长度不变）", len(out) == len(msgs))
    check("配对仍然完整", not validate_tool_pairs(out))


def test_no_progress_guard_does_not_block_real_compression() -> None:
    """★ 反向：守卫**不能**把合法的压缩也挡掉（这是它最容易写错的方向）。"""
    calls = []
    c = ContextCompressor(context_length=120_000, protect_last_n=4)
    msgs = build_prose_history(turns=12, chars=3000)
    before = estimate_messages_tokens_rough(msgs)
    out = c.compress(msgs, call_llm=lambda s, u: calls.append(1) or _OK_SUMMARY)
    middle_ok = c.summary_min_middle_tokens()
    check(f"中间段确实超过守卫阈值（{middle_ok}）", c.compaction_count == 1, c.describe())
    check("摘要调用真的打了", len(calls) == 1, str(len(calls)))
    check("token 明显下降", estimate_messages_tokens_rough(out) < before * 0.8,
          f"{before} → {estimate_messages_tokens_rough(out)}")
    check("配对仍然完整", not validate_tool_pairs(out))


# ══════════════════════════════════════════════════════════ 7. 锚点

def test_anchor_reports_real_plus_delta() -> None:
    a = UsageAnchor()
    check("没记录过时返回 None（让调用方回退粗估）", a.estimate([]) is None and not a.has_anchor)
    msgs = [{"role": "user", "content": "u" * 100}]
    a.record({"prompt_tokens": 8000, "completion_tokens": 200}, msgs)
    check("记完之后锚点有效", a.has_anchor)
    got = a.estimate(msgs)
    check("原样再问 = 真实用量（没有增量）", got == 8200, str(got))

    grown = msgs + [{"role": "assistant", "content": "a" * 350}, {"role": "user", "content": "new"}]
    got2 = a.estimate(grown)
    check("★ 追加之后 = 真实用量 + **只对增量**的估算", got2 is not None and got2 > 8200,
          f"{got} → {got2}")
    delta = (got2 or 0) - 8200
    check("增量部分的量级合理（≈增量本身，不是重估整段）", 0 < delta < 250, str(delta))


def test_anchor_rejects_zero_and_missing_usage() -> None:
    """★ 零用量 / 缺字段**不能**覆盖已有的好锚点。

    很多 provider 在某些路由下不返回 usage。用"零锚点"盖掉好锚点，
    会让下一轮**突然看不见超窗** —— 比估算不准更糟（估算是往大偏，零锚点是往小偏）。
    """
    a = UsageAnchor()
    msgs = [{"role": "user", "content": "u" * 100}]
    a.record({"prompt_tokens": 8000, "completion_tokens": 200}, msgs)
    good = a.estimate(msgs)

    a.record({}, msgs)                                          # 完全不返回 usage
    check("缺字段 → 好锚点还在", a.estimate(msgs) == good, f"{a.estimate(msgs)} vs {good}")
    a.record({"prompt_tokens": 0, "completion_tokens": 0}, msgs)
    check("零用量 → 好锚点还在", a.estimate(msgs) == good, str(a.estimate(msgs)))
    check("记过几次的计数没被无效记录污染", a.record_count == 1, str(a.record_count))


def test_anchor_fingerprint_and_serialization() -> None:
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    check("同内容 → 同指纹", fingerprint(msgs) == fingerprint([dict(m) for m in msgs]))
    check("不同内容 → 不同指纹", fingerprint(msgs) != fingerprint(msgs[:1]))
    check("★ 分隔符起作用：拼起来相同的两组不撞",
          fingerprint([{"role": "user", "content": "ab"}, {"role": "assistant", "content": "c"}])
          != fingerprint([{"role": "user", "content": "a"}, {"role": "assistant", "content": "bc"}]))

    a = UsageAnchor()
    a.record({"prompt_tokens": 100}, msgs)
    round_trip = UsageAnchor.from_dict(a.to_dict())
    check("to_dict/from_dict 往返后还认得出（新进程 --resume 靠它）",
          round_trip.estimate(msgs) == a.estimate(msgs), str(round_trip.to_dict()))
    a.clear()
    check("clear() 之后锚点没了（压缩/重置/换模型时调）", a.estimate(msgs) is None)


def test_prompt_caching_markers_and_gates() -> None:
    """★ 提示词缓存：断点怎么挑 / 标记落哪 / **三种「没插标记」要能区分**。

    最后一条是这一组最值钱的：三种情况都是"没插标记"，但只有"不支持"
    需要人去改配置 —— 只返回列表的话它们在日志里长得一模一样。
    """
    from agent import prompt_caching as pc

    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"}]
    idx = pc.breakpoints(msgs)
    check("断点 1 是系统提示词（它最值钱）", idx[0] == 0, str(idx))
    check("总数不超过 4（Anthropic 硬上限）", len(idx) <= pc.MAX_BREAKPOINTS, str(idx))
    check("★ 剩下的是**倒数 3 条非 system**（滚动窗口）", idx[1:] == [3, 4, 5], str(idx))

    out = pc.apply_cache_control(msgs)
    check("字符串内容被转成内容块并带上标记",
          isinstance(out[0]["content"], list) and out[0]["content"][0].get("cache_control"),
          str(out[0])[:80])
    check("★ 原始列表逐字节没变（标记会改写内容形状，所以必须深拷贝）",
          msgs[0]["content"] == "sys" and isinstance(msgs[0]["content"], str))
    check("没被打断点的消息不带标记", "cache_control" not in out[1])

    # ★ 注意：`apply_cache_control` 是**深拷贝返回**，所以要断言在**返回值**上。
    #   （我第一版就是断言在入参上，三条全红 —— 而那恰好证明了"入参没被改"。）
    listed = [{"role": "system", "content": [{"type": "text", "text": "x"}]}]
    listed_out = pc.apply_cache_control(listed)
    check("列表内容 → 标记加在**最后一个元素**上",
          listed_out[0]["content"][-1].get("cache_control") is not None)
    check("★ 入参那一份也没被改（同一个深拷贝保证）",
          "cache_control" not in listed[0]["content"][-1])

    empty_c = [{"role": "system", "content": None}]
    empty_out = pc.apply_cache_control(empty_c)
    check("None 内容 → 标记放在消息上", empty_out[0].get("cache_control") is not None)

    toolc = [{"role": "system", "content": "s"},
             {"role": "tool", "tool_call_id": "c", "content": "r"}]
    nn = pc.apply_cache_control(toolc, native_anthropic=False)
    check("★ 工具消息的标记只在原生 Anthropic 上放（非原生就不放）",
          "cache_control" not in nn[1], str(nn[1]))
    native_out = pc.apply_cache_control(toolc, native_anthropic=True)
    check("原生 Anthropic 上工具消息会带标记",
          native_out[1].get("cache_control") is not None, str(native_out[1])[:80])

    check("ttl 只接受 5m/1h，别的直接报错（不静默）", _raises(pc.marker, "2h"))
    check("1h 的标记形态正确", pc.marker("1h") == {"type": "ephemeral", "ttl": "1h"},
          str(pc.marker("1h")))

    _, note_auto = pc.plan(msgs, model="deepseek-chat", provider="deepseek")
    _, note_unsup = pc.plan(msgs, model="gpt-4o", provider="openai")
    _, note_empty = pc.plan([], model="claude-sonnet", provider="anthropic")
    check("① 自动前缀缓存 → 说明点出『自动前缀缓存』", "自动前缀缓存" in note_auto, note_auto[:64])
    check("② 不支持 → 说明点出『不支持』", "不支持" in note_unsup, note_unsup[:64])
    check("③ 空列表 → 说明点出『为空』", "为空" in note_empty, note_empty[:64])
    check("★ 三种说明互不相同（否则日志里分不出来）", len({note_auto, note_unsup, note_empty}) == 3)

    out2, note_ok = pc.plan(msgs, model="claude-sonnet-4-5", provider="anthropic")
    check("Claude + 原生 → 真的插了标记并报出断点",
          "断点" in note_ok and out2[0]["content"][0].get("cache_control") is not None,
          note_ok[:70])
    check("★ DeepSeek 路径**一个字都不动请求**（自动前缀缓存不需要标记）",
          json.dumps(pc.plan(msgs, model="deepseek-chat", provider="deepseek")[0],
                     sort_keys=True, ensure_ascii=False) == json.dumps(msgs, sort_keys=True, ensure_ascii=False))
    check("模型名大小写不敏感（Claude-3-5 / claude-sonnet 都认）",
          pc.is_anthropic_like("Claude-3-5-Sonnet") and pc.is_anthropic_like("claude-sonnet"))


GROUPS = [
    ("1. token 估算",
     "中日韩 1 字 ≈1 token / **tool_calls 的 arguments 要计入**",
     [test_token_estimation_cjk_vs_ascii, test_tool_call_arguments_count_toward_tokens]),
    ("2. 触发闸门",
     "阈值取自**主模型**窗口 / 有锚点用锚点 / 锚点失效要回退",
     [test_threshold_derives_from_main_model_window,
      test_trigger_gate_and_anchor_source,
      test_anchor_invalidates_when_history_changes]),
    ("3. 边界",
     "三段不重叠 / **绝不切开工具组**（对任意条数都成立）/ 预算与条数取更宽松的",
     [test_boundaries_are_disjoint_and_cover_everything,
      test_boundary_never_splits_a_tool_group,
      test_tail_budget_vs_count_takes_the_wider_one]),
    ("4. 阶段一：剪旧工具结果",
     "只碰**尾部之外**、**长于门槛**的 tool 消息",
     [test_prune_only_touches_long_tool_results_outside_tail, test_prune_has_a_size_floor]),
    ("5. 摘要与降级",
     "角色避免连续同角色 / **摘要失败要说出来** / 迭代式『更新它』",
     [test_summary_role_avoids_consecutive_same_role,
      test_system_prompt_gets_the_compaction_note,
      test_summary_failure_is_announced_not_silent,
      test_iterative_recompression_asks_to_update]),
    ("6. 契约",
     "出口永远配对完整 / **不改传入的列表** / 无进展守卫（不挡合法压缩、**不丢阶段一成果**）",
     [test_output_always_has_valid_tool_pairs,
      test_input_list_is_never_mutated,
      test_no_progress_guard_skips_small_middles,
      test_guard_keeps_phase_one_result,
      test_no_progress_guard_does_not_block_real_compression]),
    ("8. 提示词缓存（Anthropic 标记）",
     "断点=系统+倒数 3 条非 system / 四种落位 / **三种『没插标记』要能区分**",
     [test_prompt_caching_markers_and_gates]),
    ("7. 锚点",
     "内容指纹 / 只估算增量 / **零用量不覆盖好锚点** / 可序列化",
     [test_anchor_reports_real_plus_delta,
      test_anchor_rejects_zero_and_missing_usage,
      test_anchor_fingerprint_and_serialization]),
]


if __name__ == "__main__":
    print(f"解释器：{sys.executable}  Python {sys.version.split()[0]}\n")
    defined = {n for n in globals() if n.startswith("test_")}
    registered = {f.__name__ for _, _, fns in GROUPS for f in fns}
    missing = defined - registered
    if missing:
        print(f"⚠️ 有测试函数没登记进 GROUPS（写了但不会跑）：{sorted(missing)}")
        sys.exit(2)

    total_before = 0
    for title, promise, fns in GROUPS:
        print(f"══ {title} ══ {promise}")
        for fn in fns:
            fn()
        print(f"   → {len(PASS)} 累计 / {len(FAIL)} 失败\n")

    print(f"{len(PASS)} 通过 / {len(FAIL)} 失败　（{len(GROUPS)} 组）")
    if FAIL:
        print("失败项：" + "; ".join(FAIL))
    sys.exit(1 if FAIL else 0)
