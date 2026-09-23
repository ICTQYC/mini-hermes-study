"""ContextCompressor —— 默认上下文引擎：有损摘要（第 5 章 §2~§4）。

对应 Hermes 的 `agent/context_compressor.py`。

## 它在整条链上的位置

    run_agent._run_conversation_turn()
      ① _ensure_system_prompt
      ② _preflight_compression(messages)     ← ★ 本章补上
      ③ _build_api_messages
      ④ _inject_ephemeral_layers
      ⑤ _apply_prompt_caching                ← ★ 本章补上
      ⑥ _validate_alternation
      ⑦ 发请求

## 四阶段算法（原文 §4）

    阶段一  剪掉尾部之外的旧工具结果        —— 便宜，不调 LLM
    阶段二  确定边界（头 / 中间 / 尾）      —— 按 token 预算，且对齐工具组
    阶段三  把中间段摘要成结构化文档        —— 一次辅助 LLM 调用
    阶段四  组装：头 + 摘要 + 尾            —— 清理孤立的 tool_call/result 配对

## 三个必须记住的设计点（都是"踩过才知道"的）

1. **阈值取自「主模型」的上下文窗口**，不是摘要模型的。摘要模型的窗口只影响
   "摘要能不能产出来"（见 `generate_summary` 里那个静默降级）。
2. **摘要失败是静默降级**：`generate_summary` 返回 None 时，中间段被**无摘要丢弃**
   —— 原文说这是"压缩质量变差最常见的原因"，所以 mini 版**必须把这件事说出来**
   （走 status_callback，不静默）。
3. **迭代式再压缩**：第二次压缩把上一次的摘要一起发过去，指令是**更新它**，
   而不是从头摘（否则损失会累积）。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agent.context_engine import ContextEngine
from agent.usage_anchor import UsageAnchor

# ════════════════════════════════════════════════════════════════════════════
# 配置常量 —— 名字和 Hermes 的 `compression.*` 键一一对应，
# 这样读代码时能直接对上文档那张参数表（§3.1）。
# ════════════════════════════════════════════════════════════════════════════

THRESHOLD = 0.50                    # compression.threshold
PROTECT_FIRST_N = 3                 # compression.protect_first_n（写死）
PROTECT_LAST_N = 20                 # compression.protect_last_n
TARGET_RATIO = 0.20                 # compression.target_ratio（legacy 模式用）
TAIL_MODE = "lean"                  # compression.tail_mode

# lean 模式的尾部：2.5% 的窗口，夹在 10K~25K 之间
LEAN_TAIL_RATIO = 0.025
LEAN_TAIL_FLOOR_TOKENS = 10_000
LEAN_TAIL_CAP_TOKENS = 25_000

# 阶段一的门槛：短于这个长度的工具结果不值得替换（标记本身也要占地方）
PRUNE_MIN_CHARS = 200
CLEARED_MARKER = "[Old tool output cleared to save context space]"

# 摘要预算
SUMMARY_RATIO = 0.20                # _SUMMARY_RATIO：content_tokens × 0.20
SUMMARY_MIN_TOKENS = 2_000
SUMMARY_MAX_RATIO = 0.05            # min(context_length × 0.05, 12_000)
SUMMARY_MAX_TOKENS = 12_000

# 粗略估算用的字符/token 比。★ 这只是**启发式**：
# 真实数字来自 provider 报出的 usage（见 agent/usage_anchor.py）。
# CJK 一个汉字约 1 token；ASCII 约 3.5 字符一个 token。
ASCII_CHARS_PER_TOKEN = 3.5

# mini 的默认窗口。Hermes 是按模型查表的；mini 只有一个模型，所以给一个常量 +
# 一个可被环境变量覆盖的口子（测试和 demo 要能改小）。
DEFAULT_CONTEXT_LENGTH = 64_000

SUMMARY_TEMPLATE = """## Goal
[用户想达成什么]

## Constraints & Preferences
[用户的偏好、代码风格、约束、重要决定]

## Progress
### Done
[已完成的工作 —— 具体文件路径、跑过的命令、结果]
### In Progress
[正在进行的工作]
### Blocked
[遇到的阻塞或问题]

## Key Decisions
[重要的技术决定及其理由]

## Relevant Files
[读过/改过/新建的文件 —— 每个附一句说明]

## Next Steps
[接下来要做什么]

## Critical Context
[具体的值、错误信息、配置细节]"""

REWRITE_INSTRUCTION = """上一次的摘要在这里，**请更新它**，不要从头重写：

<previous_summary>
{previous}
</previous_summary>

规则：
- 已经完成的条目从"进行中"移到"已完成"；
- 新增这期间的进展；
- **删掉已经过时/被推翻的信息**（但"为什么当初那么决定"要留在 Key Decisions 里）；
- 保留所有具体的标识符（文件路径、命令、错误串、PR 号）**原样**，不要转述。"""


# ════════════════════════════════════════════════════════════════════════════
# token 估算
# ════════════════════════════════════════════════════════════════════════════

def estimate_text_tokens(text: str) -> int:
    """粗略估算一段文本的 token 数。

    ★ 为什么要有这个函数而不是直接 `len(text)//4`：
    中日韩字符在多数分词器里是**一个字一个 token**，而 ASCII 平均 3.5 字符才一个。
    用单一比例会把中文严重低估（而 Hermes 的用户里中文不少）。

    这个数**只用在不值得去问 provider 的地方**（触发前的粗判、尾部预算的扫描）。
    真实的用量走 UsageAnchor。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return cjk + int(math.ceil(other / ASCII_CHARS_PER_TOKEN))


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3040 <= o <= 0x30FF) or (0x3400 <= o <= 0x4DBF) or (0x4E00 <= o <= 0x9FFF) \
        or (0xF900 <= o <= 0xFAFF) or (0xAC00 <= o <= 0xD7AF)


def message_text(msg: Dict[str, Any]) -> str:
    """把一条消息的可计入文本拼出来（正文 + 工具调用的参数）。"""
    parts: List[str] = []
    content = msg.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
    for call in (msg.get("tool_calls") or []):
        fn = (call or {}).get("function") or {}
        parts.append(str(fn.get("name") or ""))
        parts.append(str(fn.get("arguments") or ""))
    if msg.get("name"):
        parts.append(str(msg["name"]))
    return "\n".join(p for p in parts if p)


def estimate_messages_tokens_rough(messages: Sequence[Dict[str, Any]]) -> int:
    """整段历史的粗估（对应 Hermes 的 `estimate_messages_tokens_rough`）。

    每条消息再 +4 是角色/分隔符的固定开销。
    """
    return sum(estimate_text_tokens(message_text(m)) + 4 for m in messages)


# ════════════════════════════════════════════════════════════════════════════
# 边界对齐（阶段二的一部分，单独放是为了能单独测）
# ════════════════════════════════════════════════════════════════════════════

def _is_tool_result(msg: Dict[str, Any]) -> bool:
    return msg.get("role") == "tool"


def _has_tool_calls(msg: Dict[str, Any]) -> bool:
    return bool(msg.get("tool_calls"))


def align_boundary_backward(messages: Sequence[Dict[str, Any]], idx: int) -> int:
    """把切点**往前**挪，直到不落在"工具组"的中间。

    ★ 为什么必须这样做：一条 assistant 的 `tool_calls` 和紧随其后的若干
    `role: tool` 结果是**一个整体**。从中间切开会产生两种坏状态：

      · 有 tool 结果，但它的父 assistant 消息被摘掉了 → provider 拒绝请求
        （"tool 消息没有对应的 tool_call"）；
      · 有 tool_calls，但结果全部落在摘要里 → 模型看到自己调了工具却永远不知道结果。

    Hermes 的 `_align_boundary_backward()` 就是走这个函数：越过连续的工具结果，
    找到它们的父 assistant 消息，把切点放在**它前面**。
    """
    # ★ `idx == len(messages)` 是合法输入，意思是「尾部为空」（什么都不保护）。
    #   没这一句会 IndexError —— 测试抓到的（公开辅助函数不能假设调用方一定传有效下标）。
    if idx >= len(messages):
        return len(messages)
    i = max(0, idx)
    while i > 0 and _is_tool_result(messages[i]):
        i -= 1
    # 走到的那条应该是父 assistant（带 tool_calls）。它本身必须留在切点之后，
    # 所以再往前挪一格。
    if i > 0 and _has_tool_calls(messages[i]):
        return i
    return i


def sanitize_tool_pairs(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """清理孤立的 tool_call / tool_result 配对（阶段四）。

    原文给的两条规则：
      · 引用了**已被移除**的调用的工具结果 → 删掉
      · 结果**已被移除**的工具调用 → 注入一条占位结果

    ⚠️ 为什么"注入占位"而不是"删掉那个 tool_call"：那条 assistant 消息
    是**历史事实**（模型确实调过），删掉它会改变对话的形状；
    而补一条结果只是补上"我们没保留了"这个事实，角色交替也不动。
    """
    out: List[Dict[str, Any]] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if msg.get("role") == "assistant" and _has_tool_calls(msg):
            calls = [c for c in (msg.get("tool_calls") or []) if (c or {}).get("id")]
            out.append(msg)
            # 收集紧随其后的工具结果
            j = i + 1
            seen: Dict[str, Dict[str, Any]] = {}
            while j < n and _is_tool_result(messages[j]):
                cid = messages[j].get("tool_call_id")
                if cid:
                    seen[cid] = messages[j]
                j += 1
            for call in calls:
                cid = call.get("id")
                if cid in seen:
                    out.append(seen[cid])
                else:
                    out.append({
                        "role": "tool",
                        "tool_call_id": cid,
                        "content": json.dumps(
                            {"note": "结果在压缩时被丢弃（内容已进摘要）"},
                            ensure_ascii=False,
                        ),
                    })
            i = j
            continue
        if _is_tool_result(msg):
            # 走到这里说明它前面没有带 tool_calls 的 assistant → **孤立结果**
            i += 1
            continue
        out.append(msg)
        i += 1
    return out


def validate_tool_pairs(messages: Sequence[Dict[str, Any]]) -> List[str]:
    """校验配对是否完整，返回问题列表（空列表 = 没问题）。

    ★ 这是一个**契约检查**而不是美化：provider 会对孤立的 tool 消息直接报错，
    所以它必须是"能断言的性质"，而不是"看起来对"。
    """
    problems: List[str] = []
    pending: List[str] = []
    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant" and _has_tool_calls(msg):
            if pending:
                problems.append(f"[{i}] 上一条 assistant 的 {len(pending)} 个 tool_call 还没配到结果")
            pending = [c.get("id") for c in msg["tool_calls"] if (c or {}).get("id")]
        elif role == "tool":
            cid = msg.get("tool_call_id")
            if not pending:
                problems.append(f"[{i}] 孤立的 tool 结果（{cid}）：前面没有带 tool_calls 的 assistant")
            elif cid in pending:
                pending.remove(cid)
            else:
                problems.append(f"[{i}] tool 结果的 id {cid} 不在待配对列表里 {pending}")
    if pending:
        problems.append(f"末尾还有 {len(pending)} 个 tool_call 没有结果：{pending}")
    return problems


# ════════════════════════════════════════════════════════════════════════════
# 压缩器
# ════════════════════════════════════════════════════════════════════════════

class ContextCompressor(ContextEngine):
    """默认引擎：有损摘要。用法见模块 docstring 的四阶段。"""

    def __init__(
        self,
        *,
        context_length: int = DEFAULT_CONTEXT_LENGTH,
        threshold: float = THRESHOLD,
        protect_first_n: int = PROTECT_FIRST_N,
        protect_last_n: int = PROTECT_LAST_N,
        tail_mode: str = TAIL_MODE,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"threshold 必须在 (0, 1] 内，收到 {threshold}")
        if tail_mode not in ("lean", "legacy"):
            raise ValueError(f"tail_mode 只能是 lean/legacy，收到 {tail_mode}")
        self.context_length = int(context_length)
        self.threshold = float(threshold)
        self.protect_first_n = max(1, int(protect_first_n))
        self.protect_last_n = max(1, int(protect_last_n))
        self.tail_mode = tail_mode
        self.status_callback = status_callback
        self.anchor = UsageAnchor()
        # ★ 迭代式再压缩的状态：上一次的摘要正文
        self._previous_summary: Optional[str] = None
        # 统计（给 /compress 和测试看，不参与决策）
        self.compaction_count = 0

    # ── 阈值与触发 ──────────────────────────────────────────────────────────

    @property
    def threshold_tokens(self) -> int:
        """阈值 token 数 = threshold × **主模型**的窗口（原文那条 note）。"""
        return int(self.context_length * self.threshold)

    def tail_token_budget(self, main_context_length: Optional[int] = None) -> int:
        """尾部选择预算。

        ★ 两种模式的差别（§3.3）：
          · `lean`   —— 2.5% × **主模型**窗口，夹在 10K~25K；连续性由摘要承担
          · `legacy` —— threshold_tokens × target_ratio（大窗口模型上能到 100K+）
        """
        ctx = int(main_context_length or self.context_length)
        if self.tail_mode == "legacy":
            return int(self.threshold_tokens * TARGET_RATIO)
        raw = int(ctx * LEAN_TAIL_RATIO)
        return max(LEAN_TAIL_FLOOR_TOKENS, min(raw, LEAN_TAIL_CAP_TOKENS))

    def summary_token_budget(self, content_tokens: int) -> int:
        """摘要预算：content_tokens × 0.20，夹在 [2000, min(窗口×5%, 12000)]。"""
        cap = min(int(self.context_length * SUMMARY_MAX_RATIO), SUMMARY_MAX_TOKENS)
        return max(SUMMARY_MIN_TOKENS, min(int(content_tokens * SUMMARY_RATIO), cap))

    def summary_min_middle_tokens(self) -> int:
        """中间段至少要有这么多 token 才值得摘要（无进展守卫的阈值）。

        ★ 判据不是拍脑袋：摘要**自身**的预算下限就是 `SUMMARY_MIN_TOKENS`（2,000），
        所以中间段如果不到这个量级，"摘要 + 尾部"可能反比原来更大。
        取它的 2 倍是给"摘要实际会写得更长"留余量。
        """
        return SUMMARY_MIN_TOKENS * 2

    def current_tokens(self, messages: Sequence[Dict[str, Any]]) -> Tuple[int, str]:
        """现在用了多少 token，以及这个数字**从哪来**。

        ★ 返回来源是刻意的（第 3 章的教训：只给"值"不给"来源"会丢可信度）。
        调用方能区分"provider 说的"和"我猜的"，日志里也能看出来。
        """
        real = self.anchor.estimate(messages)
        if real is not None:
            return real, "anchor(provider+增量)"
        return estimate_messages_tokens_rough(messages), "rough(字符估算)"

    def should_compress(self, messages: Sequence[Dict[str, Any]]) -> bool:
        """到阈值了吗？

        ⚠️ mini 版**故意不做** Hermes 那条 `should_defer_preflight_to_real_usage`
        （"粗估超阈值也先等一轮拿真实数据"）。理由写在《源码讲解-05》的差异表里：
        那条规则需要一个**持续运行的回合循环 + provider 一定会回 usage** 的前提，
        而 mini 的 preflight 在每轮之前跑一次、且要能回答"这一轮要不要压"。
        这里保留 `current_tokens` 的来源标记，等于把那条策略的**信息基础**留好了。
        """
        used, _ = self.current_tokens(messages)
        return used >= self.threshold_tokens

    def update_usage(self, usage: Dict[str, Any], messages: Sequence[Dict[str, Any]]) -> None:
        """记下 provider 报出的真实用量（下一轮的锚点）。"""
        self.anchor.record(usage, messages)

    def describe(self) -> str:
        used, src = self.current_tokens([])
        return (
            f"ContextCompressor(threshold={self.threshold:.2f} → {self.threshold_tokens} token · "
            f"tail={self.tail_mode} · 已压缩 {self.compaction_count} 次 · "
            f"上次摘要 {len(self._previous_summary or '')} 字符)"
        )

    def update_model(self, model: str, provider: str = "") -> None:
        """mini 只有一张小表；模型换了就重设窗口。"""
        self.context_length = DEFAULT_CONTEXT_LENGTH
        self._previous_summary = None
        self.anchor.clear()

    # ── 四阶段 ──────────────────────────────────────────────────────────────

    def compress(
        self,
        messages: List[Dict[str, Any]],
        *,
        call_llm: Optional[Callable[[str, str], Optional[str]]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> List[Dict[str, Any]]:
        """执行压缩。返回新的消息列表（**不改传进来的那份**）。

        `call_llm(system_prompt, user_prompt) -> str | None` 是注入点：
        agent 传进来的实现去发辅助 LLM 调用；测试传假函数。
        返回 None 表示**摘要失败**（见 `generate_summary`）。
        """
        status = status_callback or self.status_callback or (lambda _t: None)
        before, src = self.current_tokens(messages)

        # ── 阶段一：剪掉旧工具结果（便宜，不调 LLM）
        pruned, pruned_count = prune_old_tool_results(
            list(messages),
            tail_budget=self.tail_token_budget(),
            protect_last_n=self.protect_last_n,
        )

        # ── 阶段二：确定边界
        head_end, tail_start = determine_boundaries(
            pruned,
            protect_first_n=self.protect_first_n,
            protect_last_n=self.protect_last_n,
            tail_budget=self.tail_token_budget(),
        )
        middle = pruned[head_end:tail_start]
        if not middle:
            if pruned_count:
                status(f"压缩跳过摘要：没有中间段可摘；阶段一剪掉 {pruned_count} 条旧工具输出仍生效")
                return pruned
            status("压缩跳过：可摘要的中间段是空的")
            return list(messages)

        # ★ 无进展守卫（原文提到 Hermes 用 "no-progress guards" 把恢复过程关在有界范围内）。
        #   中间段太小时摘要它**纯亏**：摘要自身至少要占 2,000 token 预算，
        #   而且要多打一次辅助 LLM 调用（付费 + 延迟）。
        #   实测症状：25 条的历史再压一次，出来还是 25 条、token 一个没少，
        #   但那一次 LLM 调用照打 —— 而 `should_compress` 之后**每轮**都会为 True
        #   （因为历史没变小），于是变成每轮白烧一次调用。
        middle_tokens = estimate_messages_tokens_rough(middle)
        floor = self.summary_min_middle_tokens()
        if middle_tokens < floor:
            # ★ 跳过的是「摘要」，**不是阶段一**。阶段一是免费的（不调 LLM），
            #   丢了它会导致：下一轮重做一遍、守卫又跳过 —— 于是永远压不动。
            #   （这个坑是测试逼出来的：跳过时 `return list(messages)` 把剪掉的成果扔了。）
            if pruned_count:
                status(
                    f"压缩跳过摘要：剪完旧工具输出后中间段只剩 {middle_tokens} token"
                    f"（< {floor}），摘要它省不下东西、只会多一次调用（无进展守卫）。"
                    f"**但阶段一的成果已生效**：{pruned_count} 条旧工具输出被剪掉，"
                    f"约省 {before - estimate_messages_tokens_rough(pruned)} token。"
                )
                return pruned
            status(
                f"压缩跳过：中间段只有 {middle_tokens} token（< {floor}），"
                f"摘要它省不下东西、只会多一次调用（无进展守卫）"
            )
            return list(messages)

        # ── 阶段三：摘要
        content_tokens = estimate_messages_tokens_rough(middle)
        budget = self.summary_token_budget(content_tokens)
        summary = self.generate_summary(middle, call_llm=call_llm, budget_tokens=budget, status_callback=status)
        if summary is None:
            # ★ 原文点名的静默降级点。mini 版照样降级（不能因为摘要失败就不压缩，
            #   否则超窗的会话永远修不好），但**必须把这件事说出来**。
            status(
                f"⚠️ 摘要失败：{len(middle)} 条中间消息将**无摘要**被丢弃"
                f"（损失约 {content_tokens} token 上下文）。最常见的原因是"
                f"辅助模型的上下文窗口比主模型小。"
            )
        else:
            self._previous_summary = summary

        # ── 阶段四：组装
        out = assemble_compressed(
            head=pruned[:head_end],
            summary=summary,
            tail=pruned[tail_start:],
            summary_tokens=(estimate_text_tokens(summary) if summary else 0),
            boundary_note=True,
        )
        out = sanitize_tool_pairs(out)

        self.compaction_count += 1
        after, _ = self.current_tokens(out)
        status(
            f"压缩完成：{len(messages)} → {len(out)} 条 · token 约 {before} → {after}（{src}）"
            f" · 摘要 {len(summary or '')} 字符 · 阶段一清掉 {pruned_count} 条旧工具输出"
        )
        return out

    def generate_summary(
        self,
        middle: Sequence[Dict[str, Any]],
        *,
        call_llm: Optional[Callable[[str, str], Optional[str]]],
        budget_tokens: int,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        """把中间段交给辅助 LLM 摘要。

        ★ 三种返回 None 的情况，都要让调用方知道（不能静默）：
          ① 没接 `call_llm`（比如 `--no-summary` 模式或测试）；
          ② 调用抛异常；
          ③ 调用返回空。

        ★ 迭代式再压缩：有 `_previous_summary` 时指令是**更新**而不是重写。
        """
        status = status_callback or self.status_callback or (lambda _t: None)
        if call_llm is None:
            status("摘要跳过：没有提供辅助 LLM 调用（压缩将不带摘要）")
            return None

        system = (
            "你是一个上下文压缩器。把下面这段对话历史摘要成一份结构化文档，"
            "供另一个 agent 接手工作。**只保留对继续工作有用的信息**，"
            "具体路径/命令/错误串要原样保留，不要转述。"
            f"\n\n输出格式：\n{SUMMARY_TEMPLATE}"
            f"\n\n长度控制在 {budget_tokens} token 以内。"
        )
        user = "要摘要的对话历史：\n\n" + render_for_summary(middle)
        if self._previous_summary:
            user = REWRITE_INSTRUCTION.format(previous=self._previous_summary) + "\n\n" + user

        try:
            text = call_llm(system, user)
        except Exception as exc:                                  # noqa: BLE001
            status(f"摘要调用抛异常：{type(exc).__name__}: {exc}")
            return None
        text = (text or "").strip()
        if not text:
            status("摘要调用返回空")
            return None
        return text


# ════════════════════════════════════════════════════════════════════════════
# 阶段一：剪掉旧工具结果
# ════════════════════════════════════════════════════════════════════════════

def prune_old_tool_results(
    messages: List[Dict[str, Any]],
    *,
    tail_budget: int,
    protect_last_n: int,
) -> Tuple[List[Dict[str, Any]], int]:
    """尾部之外的旧工具结果替换成一行标记。返回 (新列表, 替换了几条)。

    ★ 为什么这能省"大量"token：工具输出（文件内容、终端输出、搜索结果）
    通常是长对话里**最长的单体内容**，而它们的**信息密度最低** ——
    模型当时读它是为了做判断，做完判断后那段原文就没用了，
    "读过什么"这件事留在摘要里就够。

    ⚠️ 为什么有个 `PRUNE_MIN_CHARS` 门槛：标记本身也占地方。
    一条 50 字符的输出换成 49 字符的标记是负收益。
    """
    _, tail_start = determine_boundaries(
        messages, protect_first_n=PROTECT_FIRST_N, protect_last_n=protect_last_n,
        tail_budget=tail_budget,
    )
    out = list(messages)
    replaced = 0
    for i in range(len(out)):
        if i >= tail_start:
            break
        msg = out[i]
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= PRUNE_MIN_CHARS:
            continue
        new = dict(msg)
        new["content"] = CLEARED_MARKER
        out[i] = new
        replaced += 1
    return out, replaced


# ════════════════════════════════════════════════════════════════════════════
# 阶段二：确定边界
# ════════════════════════════════════════════════════════════════════════════

def determine_boundaries(
    messages: Sequence[Dict[str, Any]],
    *,
    protect_first_n: int,
    protect_last_n: int,
    tail_budget: int,
) -> Tuple[int, int]:
    """返回 (head_end, tail_start)：`[0:head_end]` 是头部，`[tail_start:]` 是尾部，
    中间 `[head_end:tail_start]` 是要被摘要的。

    尾部的确定方式是**从末尾往前走，累加 token 直到预算用完**（原文 §4.2）；
    如果按预算保护下来的**比固定条数还少**，就回退到 `protect_last_n` 条
    —— 即"预算和条数取更宽松的那个"。

    切点都要过 `align_boundary_backward`，避免切开工具组。
    """
    n = len(messages)
    head_end = min(protect_first_n, n)

    tail_start = n
    acc = 0
    while tail_start > head_end and acc < tail_budget:
        tail_start -= 1
        acc += estimate_text_tokens(message_text(messages[tail_start])) + 4

    # 预算只保护到很少的消息时，回退到固定条数（取更宽松的）
    by_count = max(head_end, n - protect_last_n)
    tail_start = min(tail_start, by_count)

    tail_start = align_boundary_backward(messages, tail_start)
    head_end = min(head_end, tail_start)
    return head_end, tail_start


# ════════════════════════════════════════════════════════════════════════════
# 阶段三的辅助：把消息渲染成给摘要模型的文本
# ════════════════════════════════════════════════════════════════════════════

def render_for_summary(messages: Sequence[Dict[str, Any]], *, max_chars: int = 60_000) -> str:
    """把消息列表渲染成纯文本。超长时**均匀采样**并打省略标记（原文 §3.1 的 lean 说明）。

    ★ 为什么是"均匀采样"而不是"截断尾部"：中间段里最值钱的东西
    （决定、错误、路径）**可能出现在任何位置**，尤其是结尾的"当前状态"。
    均匀采样能保住分布，截尾会系统性地丢掉后半段。
    """
    chunks: List[str] = []
    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            chunks.append(f"[tool:{msg.get('tool_call_id')}] {message_text(msg)}")
        elif _has_tool_calls(msg):
            for call in msg["tool_calls"]:
                fn = (call or {}).get("function") or {}
                chunks.append(f"[assistant→调用 {fn.get('name')}] {fn.get('arguments')}")
            if msg.get("content"):
                chunks.append(f"[assistant] {msg['content']}")
        else:
            chunks.append(f"[{role}] {message_text(msg)}")

    joined = "\n".join(chunks)
    if len(joined) <= max_chars:
        return joined

    # 均匀采样：按比例从每段里取，保证头尾都在
    keep = max(200, max_chars // max(1, len(chunks)))
    sampled = []
    for c in chunks:
        if len(c) <= keep:
            sampled.append(c)
        else:
            half = keep // 2
            sampled.append(f"{c[:half]}\n…（此处省略 {len(c) - keep} 字符，原文过长已均匀采样）…\n{c[-half:]}")
    out = "\n".join(sampled)
    if len(out) > max_chars * 2:
        out = out[:max_chars] + f"\n…（整体仍有 {len(out) - max_chars} 字符被省略）…"
    return out


# ════════════════════════════════════════════════════════════════════════════
# 阶段四：组装
# ════════════════════════════════════════════════════════════════════════════

COMPACTION_HEADER = "[CONTEXT COMPACTION] 早先的对话回合已被压缩成下面这份摘要。"
SYSTEM_NOTE = (
    "\n\n[Note: Some earlier conversation turns have been compacted into a summary. "
    "The summary is an assistant message in the history. If you need anything that was "
    "compacted away, say so explicitly instead of guessing.]"
)


def assemble_compressed(
    *,
    head: Sequence[Dict[str, Any]],
    summary: Optional[str],
    tail: Sequence[Dict[str, Any]],
    summary_tokens: int,
    boundary_note: bool = True,
) -> List[Dict[str, Any]]:
    """拼出「头 + 摘要 + 尾」。

    ★ 摘要消息的**角色要挑**（原文 §4.4 的 "role chosen to avoid consecutive
    same-role violations"）：如果头部最后一条是 assistant，摘要就只能当 `user`；
    反之亦然。**不能**直接写死 `assistant` —— 那会在"头部以 assistant 结尾"
    的情况下产生两条连续的 assistant 消息，而 Hermes 的硬约束是
    *strict role alternation*（第 3 章学过）。这个坑 mini 版第 3 章踩过一次。
    """
    out: List[Dict[str, Any]] = []

    for msg in head:
        m = dict(msg)
        if boundary_note and m.get("role") == "system" and SYSTEM_NOTE not in str(m.get("content") or ""):
            m["content"] = str(m.get("content") or "") + SYSTEM_NOTE
        out.append(m)

    if summary:
        last_role = out[-1].get("role") if out else "system"
        role = "user" if last_role == "assistant" else "assistant"
        text = f"{COMPACTION_HEADER}\n\n{summary}"
        out.append({"role": role, "content": text})

        # ★ 保证摘要是最后一个 assistant（模型的注意力锚点）：如果摘要是 user，
        #   后面再补一句 assistant 的"接手"行。这是 mini 版的显式处理，
        #   目的是让"谁说的"不产生歧义。
        if role == "user" and tail and tail[0].get("role") != "assistant":
            out.append({
                "role": "assistant",
                "content": "（已接手上面的压缩摘要，继续。）",
            })

    out.extend(dict(m) for m in tail)
    return out
