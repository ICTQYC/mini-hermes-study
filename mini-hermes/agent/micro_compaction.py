"""微型压缩（micro-compaction）—— 滚动式逐交换摘要。第 10 章。

对应 Hermes 的 `agent/micro_compaction.py`（`MicroCompactionMixin`，419 行）。
本章是第 5 章 `context-compression-and-caching` 的**兄弟篇**：

    第 5 章 = 上下文**超阈值**时，一次性把**整段中间**摘要掉（批量压缩）。
    第 10 章 = **每回合**把**最老的一个交换**折进一份**滚动摘要**，用摊销换代价。

## 为什么默认关（真实版 `config_defaults.py:569-572` 的理由逐字）

    every pass rewrites sent history and breaks the prompt-cache prefix EVERY turn;
    enable only if the amortized stall beats the cached-prefix discount.

也就是：每 pass 都重写\"已发的历史\" → 打破提供商侧的**前缀缓存**。只有当
\"摊销下来的压缩停顿\"比\"保住缓存前缀省的钱\"更划算时才开。

## 一章一句话的算法

    ① 频率门：每 N 回合才跑一次（`every_n`，默认 1）。
    ② 在\"可压缩窗口\"内找**下一个完整交换**（`_find_one_exchange`）。
       —— 找不到就 **no-op**（零 LLM 调用，不花任何代价）。
    ③ 把它的文本 + 当前滚动摘要交给辅助 LLM，要求**更新**摘要。
    ④ 用**一条 assistant 摘要标记**替换那段交换（`splice`）。
       —— 用户行**永不**被吸收；尾部窗口**绝不**被碰。

## 抄了什么（第 10 章清单 A 批）

  A1  `find_one_exchange` 三条边界判据（跳 user/标记 · 连续吃 assistant+tool · **必须能收尾**）
  A2  滚动摘要 + 内存游标（游标丢失时**扫描转写本里最后一条标记**恢复，不另落盘状态）
  A3  一回合最多吸收**一个**交换；no-op 不花代价
  A4  失败不吸收、不落盘；**同一交换连续 3 次失败就推过它**
  A5  标记形状（assistant 角色 + 三键）；**只丢 micro 标记，不丢批量标记**
  A6  头部/尾部保护**沿用第 5 章**（`determine_boundaries`），不另起一套 tail 预算

## 不抄了什么（B 批，理由见 `/tmp/ch10-checklist.md`）

  B1  rewind-style 部分归档（mini 用 `record_compaction` 行级全归档）
  B2  17 字段 JSON 遥测（mini 用 status_callback 一行人话）
  B3  `_flush_scan_cursor_invalidated` / `_DB_PERSISTED_MARKER` 那套（mini 无有界 flush 游标）
  B4  重水化时把批量标记**回标**成 micro 标记（有毁历史风险，mini 只读回正文）
  B5  `aux_interrupt_protection()` / `main_runtime` 五字段透传（mini 辅助调用是同步一次性）
  B6  `SUMMARY_PREFIX` 上百行长引导块（mini 用第 5 章的 `COMPACTION_HEADER` 两行）

## 与三条不变量的关系

  ① 严格角色交替 —— 标记恒为 assistant；交换起于真 assistant、其前一条是 user；
     边界必非 assistant/tool ⇒ `[user][marker][user]` 合法。supersede 丢旧 micro 标记后
     **合并相邻纯文本 user 行**（`\n\n` 拼）保交替。**绝不**插合成 user 行。
  ② 系统提示词字节稳定 —— 微压缩**不碰** `messages[0]`，也**不重建**系统提示词。
  ③ 会话归属不串 —— 摘要调用走 `run_agent._summary_llm` → `agent/auxiliary.py`（A8）
     + 作用域-aware `get_secret`；落库用 `self.session_id`。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

# 同一交换连续失败到这个次数就把游标推过它（真实版 `context_compressor.py:646`）
MICRO_COMPACT_MAX_CONSECUTIVE_FAILURES = 3


def _cc():
    """惰性取 `agent.context_compressor` —— 避免循环 import（它 import 本模块的 mixin）。

    与真实版 `micro_compaction.py:20-24` 同款：属性在**调用时**才读，所以测试 patch
    `agent.context_compressor.X` 也有效。
    """
    from agent import context_compressor
    return context_compressor


# ════════════════════════════════════════════════════════════════════════════
# A1 · 找"下一个完整交换"（决定 user 永不被吸收）
# ════════════════════════════════════════════════════════════════════════════

def find_one_exchange(
    messages: List[Dict[str, Any]], start: int, limit: int,
) -> Optional[Tuple[int, int]]:
    """从 `start` 起找**下一个完整交换**，返回 `(exchange_start, exchange_end)` 或 `None`。

    对应真实版 `MicroCompactionMixin._find_one_exchange`（`micro_compaction.py:62-90`）。
    三条判据，缺一不可：

      ① **跳过 `user` 行与（assistant 角色的）摘要标记**，直到第一条**真 assistant**
         —— 否则\"重水化后的游标\"会把标记本身吃掉。
      ② **连续吃 `assistant` / `tool`** 直到换角色为止（一个 agent 回合可能
         assistant→tool→assistant→tool…，整段是一个\"交换\"）。
      ③ **必须能收尾**：边界那条**不得**仍是 `assistant` / `tool`。轮中途停
         （边界撞上 tail_start）会把 assistant 标记贴在 assistant/tool 行旁边 ——
         破坏交替。任何**别的**角色都是安全切点，所以返回 `None` 让这轮不吞。

    ★ 这就是\"**user 永不被吸收**\"的实现：交换只含 assistant+tool 行。
    """
    limit = min(limit, len(messages))

    def _turn_row(idx: int, roles: Tuple[str, ...]) -> bool:
        m = messages[idx]
        return m.get("role") in roles and not _cc().is_context_summary_message(m)

    idx = start
    while idx < limit and not _turn_row(idx, ("assistant",)):
        idx += 1
    if idx >= limit:
        return None
    exchange_start = idx
    idx += 1
    while idx < limit and _turn_row(idx, ("assistant", "tool")):
        idx += 1

    boundary = messages[idx] if idx < len(messages) else None
    if not isinstance(boundary, dict) or boundary.get("role") in ("assistant", "tool"):
        return None
    return (exchange_start, idx)


# ════════════════════════════════════════════════════════════════════════════
# A2 · 滚动摘要的恢复（游标丢失时从转写本重建）
# ════════════════════════════════════════════════════════════════════════════

def rolling_summary_from_marker(content: Any) -> str:
    """从一条摘要标记的正文里**抠回**滚动摘要文本（跨进程/新建 agent 的恢复）。

    对应真实版 `_rolling_summary_from_marker`（`:292-301`）。标记正文的形状是

        [CONTEXT COMPACTION] 早先的对话回合…\n\n<摘要正文>\n\n<SUMMARY_END_MARKER>

    所以取\"压缩头之后、结束标记之前\"那一段。**不额外落盘一份摘要状态** ——
    转写本里那条标记**就是**事实源。
    """
    cc = _cc()
    if not isinstance(content, str) or not content.strip():
        return ""
    # 从后往前找结束标记，剖出正文
    end = content.find(cc.SUMMARY_END_MARKER)
    body = content[:end] if end != -1 else content
    # 去掉压缩头那一行（若在）
    header = cc.COMPACTION_HEADER
    if body.startswith(header):
        body = body[len(header):]
    return body.strip()


# ════════════════════════════════════════════════════════════════════════════
# A5 · 标记的渲染 / 拼装（含 supersede + 相邻 user 合并）
# ════════════════════════════════════════════════════════════════════════════

def render_micro_marker_content(summary_text: str) -> str:
    """把摘要正文包成标记正文（第 5 章的两个字符串，够用）。"""
    cc = _cc()
    return f"{cc.COMPACTION_HEADER}\n\n{summary_text.strip()}\n\n{cc.SUMMARY_END_MARKER}"


def make_micro_marker(summary_text: str) -> Dict[str, Any]:
    """造一条 **micro** 摘要标记（assistant 角色 + 三个键）。

    真实版 `_splice_micro_compact_result`（`:372-379`）逐字同形状。
    """
    cc = _cc()
    return {
        "role": "assistant",
        "content": render_micro_marker_content(summary_text),
        cc.COMPRESSED_SUMMARY_METADATA_KEY: True,
        # micro 键：可被 supersede / defrag；批量标记永远不带它。
        cc.MICRO_COMPACT_MARKER_KEY: True,
        # micro 只吸收 assistant/tool；user 行留在转写本里。
        cc.COMPRESSED_SUMMARY_HAS_USER_TURN_KEY: False,
    }


def merge_adjacent_user_turns(result: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """supersede 丢掉一条旧 micro 标记后，可能留下两条**相邻的纯文本 user 行** ——
    用 `\n\n` 拼成一条（与第 5 章 `repair_message_sequence` pass 2 同款连接符）。

    ★ 判据严格：只合并 `role == "user"`、**非**摘要标记、且 `content` 是**字符串**的行。
      列表内容（多模态 parts）不动 —— 拼接会破坏结构。
    """
    def _plain_user(m: Any) -> bool:
        return (
            isinstance(m, dict) and m.get("role") == "user"
            and not _cc().is_context_summary_message(m)
            and isinstance(m.get("content"), str)
        )

    merged: List[Dict[str, Any]] = []
    for msg in result:
        prev = merged[-1] if merged else None
        if prev is not None and _plain_user(msg) and _plain_user(prev):
            prev["content"] = "\n\n".join(c for c in (prev["content"], msg["content"]) if c)
        else:
            merged.append(msg)
    return merged


def splice_micro_marker(
    messages: List[Dict[str, Any]], splice_start: int, splice_end: int,
    summary_text: str, *, supersede: bool,
) -> List[Dict[str, Any]]:
    """把 `messages[splice_start:splice_end]` 换成一条 assistant 摘要标记。

    对应真实版 `_splice_micro_compact_result`（`:362-390`）。`supersede=True` 表示这份摘要
    **累积**（含更早的 micro 标记）⇒ 丢掉**旧的 micro 标记**，只留最新那条。

    ★ **只丢 micro 标记**（`is_micro_marker`）：批量标记里含滚动摘要里**没有**的历史。
    """
    if not summary_text.strip():
        return messages
    marker = make_micro_marker(summary_text)
    result = messages[:splice_start] + [marker] + messages[splice_end:]
    if supersede:
        stale = [i for i, m in enumerate(result) if _cc().is_micro_marker(m)][:-1]
        if stale:
            result = merge_adjacent_user_turns(
                [m for i, m in enumerate(result) if i not in stale]
            )
    return result


def cursor_after_marker(result: List[Dict[str, Any]], fallback: int) -> int:
    """拼装后，游标 = 最新那条摘要标记的**后一格**。

    ★ 为什么必须从**拼装后**的列表推：一次 splice 把若干行塌成一条标记（还可能丢掉被
      supersede 的那条），拼装前的下标会落进后面某个交换里，**静默跳过**它。
      （真实版 `_cursor_after_splice` 的注释，`:303-307`。）
    """
    cc = _cc()
    return next(
        (idx + 1 for idx in range(len(result) - 1, -1, -1) if cc.is_context_summary_message(result[idx])),
        fallback,
    )


# ════════════════════════════════════════════════════════════════════════════
# 摘要提示词（真实版 `_build_micro_summary_prompt`，`:92-114`）
# ════════════════════════════════════════════════════════════════════════════

def build_micro_summary_prompt(existing_summary: str, exchange_text: str) -> Tuple[str, str]:
    """返回 `(system, user)` 两段提示词。真实版逐字要点：

      · 把交换里的**决策 / 需求 / 文件路径 / 未决问题**并进摘要；
      · **保持摘要的结构**；
      · **丢掉已解决的细节**；
      · 凭据一律 `[REDACTED]`；
      · **只返回摘要文本**，没有前言/解释，且不要把这段指令块带进输出。
    """
    summary_block = existing_summary if existing_summary.strip() else "(还没有摘要。)"
    system = "You are a conversation summarization assistant."
    user = (
        "You are a summarization agent creating a compact record of an "
        "ongoing conversation.  You are given a running summary and the "
        "next exchange from the conversation.  Merge the exchange's key "
        "decisions, requirements, file paths, and open questions into the "
        "summary.  Preserve the summary's structure.  Drop resolved details "
        "that are no longer relevant.  Add new decisions, file paths, and "
        "open questions.\n\n"
        "NEVER include API keys, tokens, passwords, secrets, credentials, "
        "or connection strings in the summary — replace any that appear "
        "with [REDACTED].\n\n"
        f"## Current Running Summary\n{summary_block}\n\n"
        f"## Next Exchange to Merge\n{exchange_text}\n\n"
        "Return ONLY the updated summary text, no preamble or explanation. "
        "Do not include this instruction block in your output."
    )
    return system, user


def serialize_exchange(turns: List[Dict[str, Any]]) -> str:
    """把一个交换渲染成给摘要模型看的文本（复用第 5 章的 `render_for_summary`）。"""
    return _cc().render_for_summary(turns)


# ════════════════════════════════════════════════════════════════════════════
# mixin —— 混进 ContextCompressor（与真实版同形）
# ════════════════════════════════════════════════════════════════════════════

class MicroCompactionMixin:
    """滚动微压缩。宿主必须是 `ContextCompressor`（提供边界/尾部预算那套）。

    状态全在宿主上（`micro_compact_enabled` 等），本 mixin 只放行为 ——
    与真实版 `MicroCompactionMixin` 同款。**默认关**。
    """

    # ── 游标状态 ────────────────────────────────────────────────────────────

    def reset_micro_compact_state(self) -> None:
        """忘掉滚动摘要与游标/失败记账（真实版 `_reset_micro_compact_cursor_state`）。"""
        self._micro_cursor = 0
        self._micro_rolling_summary = ""
        self._micro_consecutive_failures = 0
        self._micro_last_failure_cursor = -1

    # ── A2 · 游标解析（内存有效就用内存；丢了就扫转写本重建）────────────────

    def _resolve_micro_cursor(
        self, messages: List[Dict[str, Any]], compress_start: int, compress_end: int,
    ) -> int:
        """\"第一条**还没被吸收**的消息\"的下标。

        内存游标有效（在窗口内）就用它；否则**扫描窗口内最后一条摘要标记**，
        从它的正文重建滚动摘要（跨进程恢复：**不额外落盘一份状态**）。
        """
        if compress_start < self._micro_cursor < compress_end:
            return self._micro_cursor
        cc = _cc()
        last = -1
        for i in range(compress_start, compress_end):
            if cc.is_context_summary_message(messages[i]):
                last = i
        if last >= compress_start:
            if not self._micro_rolling_summary.strip():
                recovered = rolling_summary_from_marker(messages[last].get("content"))
                if recovered:
                    self._micro_rolling_summary = recovered
            cursor = last + 1
        else:
            cursor = compress_start
        self._micro_cursor = cursor
        return cursor

    # ── A6 · 下一个交换（沿用第 5 章的边界/尾部）────────────────────────────

    def _next_micro_exchange(self, messages: List[Dict[str, Any]]) -> Optional[Tuple[int, int]]:
        """窗口**内**的下一个未吸收交换，或 `None`。

        ★ **复用** `determine_boundaries` —— 窗口怎么划是第 5 章的事；微压缩只决定
          \"窗口里吞哪个\"。头部/尾部**绝不**另起一套预算。
        """
        head_end, tail_start = _cc().determine_boundaries(
            messages,
            protect_first_n=self.effective_protect_first_n(),
            protect_last_n=self.protect_last_n,
            tail_budget=self.tail_token_budget(),
        )
        compress_start, compress_end = head_end, tail_start
        if compress_start >= compress_end:
            return None
        cursor = self._resolve_micro_cursor(messages, compress_start, compress_end)
        if cursor >= compress_end:
            return None
        return find_one_exchange(messages, cursor, compress_end)

    # ── 摘要一次（失败返回 None，绝不抛）────────────────────────────────────

    def _micro_summarize_one(
        self, exchange_text: str, *, call_llm: Optional[Callable[[str, str], Optional[str]]],
    ) -> Optional[str]:
        """把一个交换并进滚动摘要（经**辅助 LLM**，失败返回 `None`）。

        `call_llm(system, user) -> str | None` 是第 5 章既定的**注入点** ——
        `run_agent._summary_llm` 会经 `agent/auxiliary.py`（A8）选 provider/model，
        凭据走作用域-aware `get_secret`。微压缩**复用**这条路由，不另起一套。

        ★ 三种返回 `None`：没接调用方 / 调用抛异常 / 返回空 —— 都不吸收、留着重试。
        """
        if call_llm is None:
            return None
        system, user = build_micro_summary_prompt(self._micro_rolling_summary, exchange_text)
        try:
            text = call_llm(system, user)
        except Exception:                                        # noqa: BLE001
            return None
        text = (text or "").strip()
        if not text:
            return None
        cc = _cc()
        # 弱模型会把结束标记一起复述 —— 剥掉，否则存进滚动摘要、下次再拼一次。
        if text.endswith(cc.SUMMARY_END_MARKER):
            text = text[: -len(cc.SUMMARY_END_MARKER)].rstrip()
        # 出口再脱敏一遍（摘要会被持久化、此后每轮都进提示词）。
        return cc.redact_sensitive_text(text) or None

    # ── A4 · 失败记账 ───────────────────────────────────────────────────────

    def _reset_micro_failures(self) -> None:
        self._micro_consecutive_failures = 0
        self._micro_last_failure_cursor = -1

    def _record_micro_failure(self, exchange_start: int, exchange_end: int) -> None:
        """在**同一游标**上的连续失败计数；到阈值就把游标推过这个卡住的交换。"""
        same_cursor = exchange_start == self._micro_last_failure_cursor
        self._micro_consecutive_failures = (
            self._micro_consecutive_failures + 1 if same_cursor else 1
        )
        self._micro_last_failure_cursor = exchange_start
        if self._micro_consecutive_failures >= MICRO_COMPACT_MAX_CONSECUTIVE_FAILURES:
            # 推过卡住的交换；它留在转写本里，交给批量压缩 / defrag 兜底。
            self._micro_cursor = exchange_end
            self._reset_micro_failures()

    # ── defrag（可后置；阈值默认 2000）─────────────────────────────────────

    def _needs_defrag(self) -> bool:
        """滚动摘要自身大到该重压了？（真实版 `_needs_defrag`，`:158-160`）"""
        return _cc().estimate_text_tokens(self._micro_rolling_summary) >= int(
            getattr(self, "micro_compact_defrag_threshold_tokens", 2000)
        )

    def _defrag_rolling_summary(
        self, messages: List[Dict[str, Any]], *,
        call_llm: Optional[Callable[[str, str], Optional[str]]],
    ) -> bool:
        """就地重压滚动摘要并改写**最新那条 micro 标记**（转写本形状不变）。

        真实版 `_defrag_rolling_summary`（`:162-193`）：把 base 清空，于是 merge 提示词
        变成\"紧凑重写\"指令；只改**最新 micro 标记**（批量标记不能动）。返回是否改写成功。

        ★ mini 差异（B3）：真实版还会 `pop(_DB_PERSISTED_MARKER)` 并置
          `_flush_scan_cursor_invalidated` 去失效解有界 flush 游标 —— mini 每消息即时
          落库、**没有**那个游标，所以不需要。drop 掉整块。
        """
        old_summary = self._micro_rolling_summary
        if not old_summary.strip():
            return False
        self._micro_rolling_summary = ""
        fresh = self._micro_summarize_one(old_summary, call_llm=call_llm)
        self._micro_rolling_summary = fresh or old_summary
        if not fresh:
            return False
        cc = _cc()
        entry = next((e for e in reversed(messages) if cc.is_micro_marker(e)), None)
        if entry is not None:
            entry["content"] = render_micro_marker_content(fresh)
        return True

    # ── A3 · 入口：一回合一次 pass，最多吸收一个交换 ────────────────────────

    def micro_compact(
        self, messages: List[Dict[str, Any]], *,
        call_llm: Optional[Callable[[str, str], Optional[str]]] = None,
    ) -> List[Dict[str, Any]]:
        """跑一轮微压缩。返回（可能改过的）列表。

        ★ **不改传进来的那份**是第 5 章的契约；但这里为对齐真实版的\"游标记账\"，
          内部改动是本对象的状态（游标/摘要），返回**新列表**由调用方写回历史。
        ★ `dirty` 标志：本次**真的**吸收/defrag 了 → 调用方据此落库（失败不落库）。
        """
        self._micro_dirty = False
        if not getattr(self, "micro_compact_enabled", False):
            return messages

        # 频率门：each pass breaks prompt cache once；按**调用次数**计，no-op 不会把它卡住。
        every_n = max(1, int(getattr(self, "micro_compact_every_n_turns", 1) or 1))
        if every_n > 1:
            self._micro_turns_since_pass += 1
            if self._micro_turns_since_pass < every_n:
                return messages
            self._micro_turns_since_pass = 0

        # 少于 4 条根本形不成一个可吸收的交换（真实版门：`n_messages >= 4`）。
        if len(messages) < 4:
            return messages
        exchange = self._next_micro_exchange(messages)
        if exchange is None:
            return messages                                    # ★ A3：no-op，零 LLM 调用
        exchange_start, exchange_end = exchange

        # defrag 就地改摘要/标记（不 splice、不推游标），而不是吸收本回合。
        if self._needs_defrag():
            if self._defrag_rolling_summary(messages, call_llm=call_llm):
                self._reset_micro_failures()
                self._micro_passes += 1
                self._micro_dirty = True
            return messages

        # 本次摘要是否**累积**（含更早的 micro 标记）—— 摘要前捕获。
        cumulative = bool(self._micro_rolling_summary.strip())
        exchange_text = serialize_exchange(messages[exchange_start:exchange_end])
        updated = self._micro_summarize_one(exchange_text, call_llm=call_llm)
        if updated is None:
            self._record_micro_failure(exchange_start, exchange_end)
            return messages                                    # ★ A4：失败不吸收、不落库

        self._micro_rolling_summary = updated
        self._micro_cursor = exchange_end
        self._reset_micro_failures()
        result = splice_micro_marker(
            messages, exchange_start, exchange_end, updated, supersede=cumulative,
        )
        self._micro_cursor = cursor_after_marker(result, exchange_start + 1)
        self._micro_passes += 1
        self._micro_dirty = True
        return result

    def micro_compact_describe(self) -> str:
        return (
            f"micro-compact({'on' if getattr(self, 'micro_compact_enabled', False) else 'off'}"
            f" · every_n={getattr(self, 'micro_compact_every_n_turns', 1)}"
            f" · 已跑 {getattr(self, '_micro_passes', 0)} 次"
            f" · 滚动摘要 {len(self._micro_rolling_summary)} 字符"
            f" · 游标 {self._micro_cursor})"
        )


__all__ = [
    "MICRO_COMPACT_MAX_CONSECUTIVE_FAILURES",
    "MicroCompactionMixin",
    "build_micro_summary_prompt",
    "cursor_after_marker",
    "find_one_exchange",
    "make_micro_marker",
    "merge_adjacent_user_turns",
    "render_micro_marker_content",
    "rolling_summary_from_marker",
    "serialize_exchange",
    "splice_micro_marker",
]
