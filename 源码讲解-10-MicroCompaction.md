# 源码讲解 · 第 10 章：微型压缩（Micro Compaction）

> **一句话**：压缩的账单可以**分期付** —— 不必等窗口装满再吃一次长停顿，而是**每回合吸收一点**；代价是**每回合**都打断一次提示词缓存前缀。
> —— 《中文精译-10-MicroCompaction》§0

> **原文**：`website/docs/developer-guide/micro-compaction.md`（**406 行**）。
> **真实源码锚**：`$HOME/.hermes/hermes-agent/`，commit **`f5d1926110`**（`version = "0.21.3"`）。
> **mini 源码锚**：`mini-hermes/`，commit **`f261aa2`**（本章 mini **已实现**）。
> 本文所有 `文件:行号` 都对齐这两个 commit；**没实际读到的行号一律不写行号**（只写文件路径）。行数一律 `wc -l` 口径。

**口径说明**

- 每节固定四块，顺序不变：**① Hermes 怎么做的**（散文把机制讲透）→ **② 读 Hermes 的代码**（只引一段 ≤15 行，优先引注释）→ **③ mini 的实现（现状）** → **④ 与真实版的差别**。
- **mini 栏口径**：`✅ 已抄（f261aa2）` / `已对齐`（早先章节已一致或本章未引入新差异）/ `⬜ 没做（量级）` / `⏭ 不抄（理由）`。
- ★ **本章与前面几章不同：mini 已经实现了微型压缩**（提交 `f261aa2`）。所以 ③ 块写**现状**，④ 块写**与真实版还差什么** —— 不复述「该不该做」这类判断。
- 术语首次给定义：**交换（exchange）** = 一整轮 agent 回合里 **agent 那一侧**的全部产出 —— 一条 `assistant` 消息，连同它的工具结果与后续 `assistant` 迭代，直到下一条 `user` 消息为止；**不含** user 那一步。**滚动摘要（rolling summary）** = 唯一一份持续累积的摘要，每个被吸收的交换**并入**它。

**规模（`wc -l`，两侧都在各自锚 commit 上实测）**

| 侧 | 文件 | 行 / 量 | 角色 |
|---|---|---|---|
| 真实 | `agent/micro_compaction.py` | **419** | 本特性主体：`MicroCompactionMixin`（游标、滚动摘要、defrag、失败计数、遥测） |
| 真实 | `agent/turn_finalizer.py` | **674** | `finalize_turn`（`_micro_compact_after_turn` 的宿主与调用点） |
| 真实 | `agent/context_compressor.py` | **5061** | 压缩器本体（mixin 混入此类的宿主）；`_protect_head_size` / `_find_tail_cut_by_tokens` / `_MICRO_COMPACT_MAX_CONSECUTIVE_FAILURES` 都在这儿 |
| 真实 | `hermes_state_messages.py` | **1364** | `archive_and_compact`（原子软归档 + 插入） |
| 真实 | `hermes_cli/config_defaults.py` | **2922** | 四个 `compression.*` 默认值的出处 |
| 真实 | `scripts/micro_compaction_report.py` | **171** | §9 那条日志报告器 |
| mini | `agent/micro_compaction.py` | **537** | 同形 `MicroCompactionMixin` + 10 个模块级函数（本章主体） |
| mini | `agent/context_compressor.py` | 改动 **`+60 / −2`** | 三个键常量 + `is_micro_marker` + mixin 混入 + 三个构造参数 |
| mini | `hermes_cli/config.py` | 改动 **`+94 / −1`** | 四个 `compression.*` 键 + 严格整型夹取 |
| mini | `run_agent.py` | 改动 **`+75 / −1`** | 回合收尾钩 `_micro_compact_after_turn` + 落库 |
| mini | `tests/test_micro_compaction.py` | **865 行 / 119 条 / 10 组** | 契约与不变量断言（不用 pytest） |
| mini | `demos/ch10_real_run.py` | **118** | 真跑脚本（真 DeepSeek + 临时 `MINI_HOME`） |

> ⚠️ **行号口径**：mini 侧行号**已按补丁 `1770a9e`（补「摘要被截断 ⇒ 丢弃半截」判据）校正到现行代码**，
> 直接照着看即可。四个文件的当前规模：`agent/micro_compaction.py` **537** · `run_agent.py` **1272** ·
> `agent/context_compressor.py` **1046** · `tests/test_micro_compaction.py` **865**（补丁前分别是 504 / 1241 / 1012 / 704）。
> 真实侧行号仍锚上游 `f5d1926110` —— 两边锚的**不是同一个 commit**。

> 收据（由主代理核过，§九 直接引用）：双解释器全套件 **1,187 通过 / 0 失败**（10 个测试文件）；反向验证 **5/5 + 补丁 2/2 + 分支标签 3/3 干净变红**；真跑 **5/5 核验通过**。

**节 ↔ 精译对照**

| 本章节 | 《中文精译-10》对应 |
|---|---|
| 导言 · 机制总览 | §0 这一章在回答什么 · §1 What it does |
| §〇 差异表 | 附录 A 术语表 · 附录 B 实现文件 |
| §一 为什么要「分期付款」 | §0（账单不对称）· §7 提示词缓存 |
| §二 切一个交换：三条边界判据 | §1（含 `exchange` 译注框图）· §2 你的消息永不被压缩 · §3 它从不触碰什么 |
| §三 滚动摘要与游标 | §4「游标」「滚动摘要」· §2 |
| §四 一回合一个交换：成本上界与 no-op | §1 · §6 配置（频率旋钮） |
| §五 失败与降级 | §4「当摘要器失败时」· §10 失败行为 |
| §六 defrag | §4「碎片整理」 |
| §七 与会话库保持同步 | §4「与会话数据库保持同步」 |
| §八 它与批量压缩的关系 | §5 与批量压缩的交互 · §7 |
| §九 真问题与收据 | §9 度量它 ·（精译无正节 —— 实现纪事） |
| §十 差距清单 | 附录 B · 附录 C |
| 附录 A 正文放不下的引用 | （精译无对应） |
| 附录 B 三方对照矩阵 | 附录 A 术语表 · 附录 B |
| 附录 C 上游出入 | 附录 C 本篇与上游源码的出入 |
| （本章无对应） | §8 选择压缩模型 · §9 度量它 |

[TOC]

---

## 导言 · 机制总览

微型压缩要回答的问题只有一件：**「一个已经长到要压缩的会话，能不能不痛？」** 批量压缩的做法是等转写本越过阈值，把中间一大块**在一次调用里**摘要掉 —— 能行，但整张账单一次性到期：一次可见的停顿、一个很大的摘要请求，而且发生时刻完全取决于你恰好在哪里越线。微型压缩把同一张账单**摊开付**：每完成一个回合，就把**最老的那个尚未被吸收的交换**折进一份持续累积的摘要里，活还是那些活，只是**一次一小块、连续地发生**。

它不是免费的：每一次 pass 都是一次对辅助模型的**真实调用**，且跑在**回合末尾**（回答已经流完，但回合要等它跑完才关闭）；每一次 pass 还会**重写已经发出去的历史**，于是**每个回合**都打断一次 provider 的提示词缓存前缀。所以它**默认关闭** —— 打开前请先读 §7「提示词缓存」，那是反对它最强的论据。

下面七小节按「谁 / 何时 / 做什么 / 什么不能破 / 出错怎么办 / 跨了哪几层 / 一个回合的顺序」把它拆开；每条都落到真实源码的 `文件:行号` 与**逐字注释**（锚 `f5d1926110`）。

### 0.1 谁在管（哪个 mixin / 哪个函数是入口）

主体是**一个 mixin**：`agent/micro_compaction.py:36` 的 `class MicroCompactionMixin`。它**不独立存在**，而是被混进上下文压缩器 —— `agent/context_compressor.py:28` 那一行 `from agent.micro_compaction import MicroCompactionMixin` 就是接缝。微压缩因此**寄生在压缩器上**：边界、尾部预算、标记常量、辅助调用全用宿主的，自己只加法（找交换、滚动摘要、defrag、失败记账）。

入口是**一个函数**：`agent/micro_compaction.py:199` 的 `_micro_compact(messages) -> List[Dict]`。它的上游是回合收尾器里的 `agent/turn_finalizer.py:272` `_micro_compact_after_turn()`，调用点在同文件 `:291`。

逐字引（`agent/micro_compaction.py:1-5`，模块 docstring）：

> Micro-compaction mixin for ContextCompressor.
>
> Rolling per-exchange summarization that folds old user/assistant exchanges into a single
> summary marker between turns. OFF by default: every pass rewrites the prompt prefix and
> breaks the provider prompt cache.

**要害**：docstring 第一句就钉死了归属 —— **mixin for ContextCompressor**。所以「谁在管」的答案不是「一个新子系统」，而是**压缩器多了一种干活的方式**；这也是本章能复用第 5 章那套边界算法（A6）的结构前提。

### 0.2 什么时候（在哪一个回合阶段被调、频率门）

**在回合收尾的落盘步骤里**，不是在回合循环里。`finalize_turn` 把收尾收在一个内嵌函数 `_persist_step` 里（`agent/turn_finalizer.py:500-509`），顺序是：丢脚手架 → 从流里回收最终回答 → 封尾 → **微压缩** → 落盘。微压缩卡在**落盘之前**，所以它改完的转写本**一次落盘就落对**。

调用本身还有一道**成功门**（`agent/turn_finalizer.py:507-508`）：

```python
if not interrupted and not failed:
    _micro_compact_after_turn(agent, messages, final_response, logger)
```

`_micro_compact_after_turn` 内部再叠三层闸（`agent/turn_finalizer.py:282-289`）：`_micro_compact_enabled is True`（**严格 `is True`**）、`final_response` 非空、以及 `not compression_checkpoint_required` / `not _persist_disabled`。

频率门在 mixin 里（`agent/micro_compaction.py:206-213`），逐字注释：

> Cadence gate: each pass breaks prompt cache once. Counted per invocation so a no-op turn
> can't wedge it.

计数据是**按调用次数**推进的（不是按「提交成功的 pass」），所以一个没东西可吸收的回合**照样把节奏往前推**，不会把它卡住。

**要害**：时机选在**回合末尾**是刻意的 —— 那时回答已经流给用户了，一次 30 秒的摘要调用不会让人干等一个「卡住的界面」；代价是这个回合**要等 pass 跑完才关闭**。

### 0.3 做什么（一次 pass 的三种结局：吸收 / defrag / no-op）

一次 `_micro_compact` 有**且只有三种**可能的收场：

| 结局 | 触发条件 | 代价 |
|---|---|---|
| **no-op** | 没有可吸收的交换（首尾窗口吃掉了整段，或消息 < 4 条，或游标已到窗口尾） | **零 LLM 调用**、原样返回 |
| **defrag** | 滚动摘要自身 ≥ `micro_compact_defrag_threshold_tokens`（默认 2000） | 一次辅助调用，**转写本形状不变**（不 splice、不推游标） |
| **吸收（absorb）** | 找到下一个完整交换，且摘要成功 | 一次辅助调用 + 一次 splice + 一次落库 |

no-op 的落点很干净（`agent/micro_compaction.py:216-218`）：先要求 `len(messages) >= 4`（少于 4 条根本形不成一个可吸收的交换），再要求 `_next_exchange()` 返回非空 —— 否则 `if exchange is None: return messages`。

逐字引（`agent/micro_compaction.py:200-202`，入口 docstring）：

> Run one round of micro-compaction (entry point from ``finalize_turn()``). Returns the
> (possibly modified) list and syncs the session DB via ``archive_and_compact`` (the
> append-only flush alone would double-load on resume).

**要害**：三种结局里**只有 no-op 不花 LLM 钱**（defrag 也要一次辅助调用）。这不是优化，是「默认关 + 便宜开关」能不能成立的前提 —— 用户把频率设成每回合一次，也不该在没活干的回合上白烧一次调用。

### 0.4 不变式（user 永不被吸收 · 角色交替 · 提示词字节稳定 · 头部/尾部不碰）

四条硬规矩，破哪条都会把转写本写坏：

1. **user 永不被吸收**。交换的起点被刻意定义成**真 `assistant` 行**，所以 `user` 行一路被**跨过**。逐字引（`agent/micro_compaction.py:67`，`_find_one_exchange` docstring）：
   > Spans assistant+tool rows up to the next user message; **user turns are never absorbed** (alternation safety, verbatim user text).
2. **角色交替严格合法**。标记角色恒为 `assistant`（`agent/micro_compaction.py:373`），且交换的**边界必须能收尾**。逐字引（`agent/micro_compaction.py:85-86`）：
   > Boundary must close the turn: a mid-turn stop at tail_start would put the assistant marker
   > beside assistant/tool rows. Any other role is a safe splice (avoids wedging the cursor).
   于是 `[user][marker][user]` 恒成立。
3. **提示词字节稳定**。微压缩**只在中段干活** —— 它从不碰 `messages[0]`，也从不重建系统提示词；头部由 `_protect_head_size`（`agent/context_compressor.py:4041`）划出，`_next_exchange`（`agent/micro_compaction.py:282`）把可压缩窗口**从它之后**起算。系统提示词一旦被重写，provider 的缓存前缀就整体作废。
4. **头部 / 尾部不碰**。窗口的两端都由第 5 章既有的算法划（`_align_boundary_forward` `:4016` / `_find_tail_cut_by_tokens` `:4343`），微压缩**只决定窗口内吞哪一个交换**，从不另起一套 tail 预算。

还有一条**只对标记**的约束，它是本章最容易被写坏的地方（`agent/micro_compaction.py:382-383`）：

> Cumulative summary: keep only the newest marker. Drop an older one only if supersede AND
> it has MICRO_COMPACT_MARKER_KEY (provably absorbed); **a batch marker holds MORE history**.

**要害**：四条其实是一条 —— **真相之源（user 的话、系统的立身指令、刚发生的事）一律逐字保留，只压派生物**。滚动摘要压的是「它做了什么」，不是「你要什么」。

### 0.5 出错（摘要失败 / 长度截断 / 连续失败 3 次怎么做）

微压缩**自始至终是尽力而为**，三种出错各有各的收场，但都不改坏转写本：

- **摘要调用失败**（辅助模型不可达 / 额度用尽 / 抛异常）：`agent/micro_compaction.py:137-139` 把它记日志后返回 `None`，上层**不吸收、不落库**，转写本原样。失败**被计数**，记在**同一个游标**上。逐字引（`agent/micro_compaction.py:267`）：
  > Track consecutive failures at the same cursor to avoid busy-looping every turn.
- **长度截断**（`finish_reason=length`）：输出被打断 = 只摘了一半，**丢弃**。逐字引（`agent/micro_compaction.py:141`）：
  > A length stop means a partial merge; leave the exchange unabsorbed so a later pass retries.
- **同一交换连续失败 3 次**：游标**照样推过它**。常量在 `agent/context_compressor.py:646`（`_MICRO_COMPACT_MAX_CONSECUTIVE_FAILURES = 3`），逐字注释：
  > After this many failures at one cursor, skip the exchange to avoid busy-looping.
  被跳过的消息**留在转写本里**，交给批量压缩接手（`agent/micro_compaction.py:277` 逐字：`it stays in the transcript for batch compression/defrag`）。

最外层还有一道**兜底吞异常**（`agent/turn_finalizer.py:302-303`）：`except Exception as _mc_err: logger.info("Micro-compaction failed: %s", _mc_err)` —— 微压缩**可以降级，但不应该能弄坏一个会话**。

**要害**：三条合起来是「**有界重试 = 有界代价**」：失败留着重试（不吞半截摘要），但重试次数封顶（不永久空烧），且**永不落盘半成品**。

### 0.6 跨层（与批量压缩、与会话库、与提示词缓存）

微型压缩不是孤立的第二套压缩，它同时挂在三条线上：

- **与批量压缩：它不是替代，是推迟。** 两者共用**同一种标记格式**（`_compressed_summary` 键），所以互操作。但可被覆盖的范围严格区分 —— 逐字引（`agent/context_compressor.py:228-230`）：
  > Only micro markers may be superseded/defragged/rehydrated: a batch marker's content is NOT in the rolling micro summary, so rewriting one destroys history.
  窗口真装满时，批量压缩**仍然会触发**；微型压缩只是把它压在阈值**够低**的位置，让它**触发得少得多**。
- **与会话库：光在内存里拼接不够。** 常规落盘是**只追加**的，旧交换行会**保持 `active=1`**，一次 resume 就把摘要和它替换掉的消息**都**载进来 —— 直接把会话顶过上限。所以每次吸收还要调 `_sync_micro_compact_to_db`（`agent/micro_compaction.py:343-360` → `archive_and_compact`，`hermes_state_messages.py:567`）**原子地**软归档旧行、插入压缩后的那一组。逐字引（`agent/micro_compaction.py:344-346`）：
  > Persist the micro-compacted set to the session DB atomically and stamp rows persisted.
  > Without this the old exchange rows stay ``active=1`` and a resume double-loads both the
  > summary and the originals.
- **与提示词缓存：这是它默认关的理由。** 逐字引（`hermes_cli/config_defaults.py:569-572`）：
  > micro_compact: opt-in — after each turn fold the oldest un-absorbed exchange into a
  > rolling summary, amortizing compression cost. Off by default because every pass rewrites
  > sent history and breaks the prompt-cache prefix EVERY turn; enable only if the amortized
  > stall beats the cached-prefix discount.

另有一条**互斥**：`compression.checkpoint_required` 为真时，微压缩被**强制关掉**（`agent/agent_init.py:1880-1886` 把 `cs.micro_compact` 置 `False`，日志逐字：`micro-compaction is disabled for this agent so every lossy rewrite passes through the checkpoint-gated compressor`）—— 因为微压缩**没有 checkpoint hook**。

**要害**：三条线共用同一个代价模型 —— **每 pass 破一次缓存前缀**。所以「怎么把这次破坏摊得值」不是微压缩自己能回答的，要在§八 里跟批量压缩、剪枝一起看。

### 0.7 时间线（一个回合结束 → 吸收一个交换 → 落库 → 下一回合）

1. 回合循环正常结束，`finalize_turn()` 拿到 `(final_response, messages, …)`（`agent/turn_finalizer.py:446`）；
2. 进 `_persist_step`（`agent/turn_finalizer.py:500`）：丢脚手架 → 从流里回收最终回答 → 封尾；
3. **成功门**：`not interrupted and not failed` ⇒ 调 `_micro_compact_after_turn`（`agent/turn_finalizer.py:507-508`）；
4. `_micro_compact_after_turn` 过四道闸（`agent/turn_finalizer.py:282-289`），把 `messages` 交给 `_compressor._micro_compact(messages)`（`:291`）；
5. mixin 内：**频率门** → `_next_exchange`（划窗口 + 解析游标 + 找交换）→ **no-op / defrag / 吸收** 三选一（`agent/micro_compaction.py:206-263`）；
6. 吸收路径：`_splice_micro_compact_result` 把交换塌成**一条 assistant 标记**（`:257`）→ 游标从**拼装后**的列表重推（`:258`）→ `_sync_micro_compact_to_db` 原子归档 + 盖「已持久化」戳（`:259`）；
7. 结果写回 `messages[:] = _compacted`（`agent/turn_finalizer.py:298-299`），日志一行 `Micro-compaction: %d -> %d messages`（`:301`）；
8. 回到 `_persist_step`，`agent._persist_session(messages, conversation_history)` 落盘（`:509`）；
9. 下一回合开始时，`messages` 已经**更小**了 —— 窗口稳住，而不是朝阈值一路进军。

**要害**：顺序里唯一不可换的一步是 **splice 在落库之前**。反过来的话，内存里是新摘要、盘上是旧交换，下一次 resume 就双重加载 —— 这正是 §七 那条「不落库会坏」的理由。

### 验收四问

1. **user 的话真的一个字都没被摘要吗？** 是。交换的起点由 `_find_one_exchange` 定义为**真 `assistant` 行**、`user` 行一路被跨过（`agent/micro_compaction.py:76-77`）；真跑核验 5/5 也确认 user 文本逐字保留。（§0.4 · §二）
2. **每次 pass 真的只吸收一个交换吗？** 是。入口只在找到**一个** `(exchange_start, exchange_end)` 后走一次 splice，然后 `return result`（`agent/micro_compaction.py:216-263`），没有循环；频率门再把它拉成「每 N 回合一次」。（§0.3 · §四）
3. **标记会不会把批量压缩的成果吃掉？** 不会。supersede 只丢**带 micro 键**的标记（`agent/micro_compaction.py:384`），批量标记**永不**被丢，因为它持有滚动摘要里**没有**的历史。（§0.4 · §三）
4. **失败了会不会把会话写坏？** 不会。失败不吸收、不落库（`_micro_dirty` / `_record_micro_failure`），连续 3 次才推过那个卡住的交换，最外层 `except Exception` 兜住（`agent/turn_finalizer.py:302-303`）。（§0.5 · §五）

---

## §〇 差异表

只列**架构性**差别为主，外加「已对齐」与「量级」行，方便一眼看清两条实现是不是同一个形状。性质四取一：`架构性` / `已对齐` / `量级` / `不抄`。mini 侧行号锚 `1770a9e`（现行代码；本章实现提交是 `f261aa2`，其后补丁 `1770a9e` 已把行号校正过来）。

| # | 面 | 真实 Hermes | mini 现状 | 性质 |
|---|---|---|---|---|
| 1 | 开关与默认 | `micro_compact: False`（`hermes_cli/config_defaults.py:573`），理由写在注释里（`:569-572`） | ✅ 已抄（`f261aa2`）：同名同默认（`hermes_cli/config.py:80`），理由注释照抄 | 已对齐 |
| 2 | 入口与挂载 | `MicroCompactionMixin`（`agent/micro_compaction.py:36`）混进压缩器（`agent/context_compressor.py:28`）；入口 `_micro_compact`（`:199`），由 `turn_finalizer.py:291` 调 | ✅ 已抄：同形 mixin（`agent/micro_compaction.py:274`）+ 入口 `micro_compact()`（`:446`）；收尾钩在 `mini-hermes/run_agent.py:1652` | 已对齐 |
| 3 | 交换边界三条判据（A1） | `_find_one_exchange`（`agent/micro_compaction.py:62-90`） | ✅ 已抄：`find_one_exchange`（`:75`），三条判据逐条对齐 | 已对齐 |
| 4 | 滚动摘要 + 游标恢复（A2） | 只留一份累积摘要；游标失效则扫转写本最后一条标记重建（`:39-60` / `:292-301`），**不另落盘状态** | ✅ 已抄：`_resolve_micro_cursor`（`:292`）/ `rolling_summary_from_marker`（`:119`） | 已对齐 |
| 5 | 一回合一个交换 + no-op（A3） | `n_messages >= 4` + `if exchange is None: return messages`（`:216-218`） | ✅ 已抄（`:473-477`）：no-op 零 LLM 调用 | 已对齐 |
| 6 | 失败记账（A4） | `_record_micro_failure`（`:265-280`）+ 常量 `3`（`agent/context_compressor.py:646`） | ✅ 已抄：`_record_micro_failure`（`:395`）+ `MICRO_COMPACT_MAX_CONSECUTIVE_FAILURES`（`:58`） | 已对齐 |
| 7 | 标记形状与 supersede（A5） | `_splice_micro_compact_result`（`:362-390`）+ `_is_micro_marker`（`:31-33`）+ 三键常量（`agent/context_compressor.py:226-230`） | ✅ 已抄：`splice_micro_marker`（`:193`）/ `make_micro_marker`（`:152`）/ `is_micro_marker`（`agent/context_compressor.py:114`）+ 同名三键常量（`:90-94`） | 已对齐 |
| 8 | 头部/尾部保护（A6） | 复用压缩器的 `_protect_head_size`（`agent/context_compressor.py:4041`）/ `_align_boundary_forward`（`:4016`）/ `_find_tail_cut_by_tokens`（`:4343`） | ✅ 已抄：复用第 5 章 `determine_boundaries`（`agent/context_compressor.py:884`）+ `tail_token_budget`（`:569`），**未另起一套** | 已对齐 |
| 9 | 尾部预算口径 | 默认 `tail_mode=lean`：`max(10_000, min(25_000, ctx×0.025))`（`agent/context_compressor.py:1927-1931`，常量 `:757`） | 已对齐（第 5 章已做同公式） | 已对齐 |
| 10 | defrag | `_needs_defrag` / `_defrag_rolling_summary`（`:158-193`）：**就地重写最新 micro 标记**，另加 `pop(_DB_PERSISTED_MARKER)` + `_flush_scan_cursor_invalidated` 去失效有界 flush 游标（`:182-188`） | ✅ 已抄主体（`_needs_defrag` `:409` / `_defrag_rolling_summary` `:415`）；**无** flush 游标失效那套（mini 每消息即时落库、无此游标） | 架构性 |
| 11 | 落库归档 | `archive_and_compact(..., tail_count=len-1)` —— rewind-style **部分**归档（`hermes_state_messages.py:567`） | 用第 6 章既有的 `record_compaction` **整份**归档（`mini-hermes/run_agent.py:1701`）；同样原子、同样不双载，粒度不同 | 架构性 |
| 12 | 遥测 | `_emit_micro_compaction_telemetry`（`:309-342`）：**19 字段** JSON 行 + `passes_total` / `tokens_saved_total` 跨会话累计 | ⬜ 没做（量级）：`status_callback` 吐一行人话（`mini-hermes/run_agent.py:1682-1685`），无累计、无 `occupancy_pct` | 量级 |
| 13 | 回收量闸门 | **没有** —— 只有频率旋钮 `micro_compact_every_n_turns`；对比剪枝有 `proactive_prune_min_reclaim_tokens`（默认 4096，`config_defaults.py:568`） | 同：**没有**（mini 的 `proactive_prune_min_reclaim_tokens` 键只做了默认值/夹取，未接剪枝路径） | 已对齐 |
| 14 | 与批量压缩的互操作 | 标记格式相同；只有 micro 标记可被 supersede/defrag/rehydrate（`agent/context_compressor.py:228-230`） | ✅ 已抄：`is_micro_marker` 只认带 micro 键的标记（`agent/context_compressor.py:114-122`）；批量标记永不被丢 | 已对齐 |
| 15 | 跨进程 resume 回标 | `_resolve_compact_cursor` 会把扫到的**最后一条标记（含批量）**就地打上 `MICRO_COMPACT_MARKER_KEY`（`:55-57`），从而让它可被 supersede | ⏭ 不抄（B4）：mini **只读回正文**、**不回标** —— 回标会让批量标记可被 supersede，风险大 | 不抄 |
| 16 | `checkpoint_required` 互斥 | 为真时强制把微压缩关掉（`agent/agent_init.py:1880-1886`），因为它没有 pre-compress checkpoint hook | ⬜ 没做：mini 没有 checkpoint-gated 压缩路径，这个互斥无对象 | 不抄 |
| 17 | 辅助调用中断保护 | `aux_interrupt_protection()` 上下文管理器 + `main_runtime` 五字段透传（`agent/micro_compaction.py:126-136`） | ⏭ 不抄（B5）：mini 的辅助调用是**同步一次性**（`agent/auxiliary.py`），无 aux 线程中断语义；只透传一份只读 `main_runtime` 快照（`mini-hermes/run_agent.py:1524`） | 不抄 |
| 18 | 摘要提示词与标记包装 | 合并指令逐字（`:92-114`）+ `_render_micro_marker_content` 用 `SUMMARY_PREFIX` 上百行长引导块（`:393-396`）；另有日志报告器 `scripts/micro_compaction_report.py`（171 行） | ✅ 已抄合并指令（逐字，`build_micro_summary_prompt` `:235`）；标记包装用第 5 章 `COMPACTION_HEADER` + `SUMMARY_END_MARKER` 两行（B6）；记报告器 ⬜ 没做（量级） | 量级 |

## §一 为什么要"分期付款"：账单不对称

**① Hermes 怎么做的**：批量压缩与微型压缩做的是**同一份活** —— 把老对话摘要成更少的 token。区别只在**账单什么时候到期**。批量压缩是**一次性到期**：上下文越过阈值，会话停下来，一个很大的摘要请求打出去，用户看到一次**长停顿**；而且这次停顿多大，取决于"你恰好在哪里越线" —— 时机与规模由**阈值位置**决定，不由你决定。微型压缩把**同一张账单分期**：每个回合结束后只吸收**一个**交换（exchange = 一次来回里 agent 那一侧的全部产出，从一条真 assistant 起、到换角色为止，**不含** user 那句话），于是长停顿被摊成一串**每回合几秒的增量**。原文的账这么算（精译 §7 表）：

| | 只有批量压缩（默认） | 微型压缩开启 |
|---|---|---|
| 压缩停顿 | 在阈值处一次长停顿 | 摊到各个回合 |
| 上下文占用 | 一直锯齿摆动到阈值 | 保持低而平 |
| 缓存前缀 | 两次压缩之间保持完整 | 每个回合都打断 |

要害在最后一行 —— **代价不对称**：批量压缩的代价是**用户可感知的停顿**（大、稀、由阈值触发），微型压缩的代价是**每回合一次缓存前缀断裂**（小、频繁、由你开没开这个开关决定）。一个 pass 会**重写已经发出去的历史**，而 provider 的提示词缓存折扣只在前缀字节不变时才存活 —— 从重写点往后，前缀作废、被缓存的输入改按未缓存价计费。正因为两边都不是白送，它**默认关**（`micro_compact: False`），配置注释还明说：只有当"摊销下来的停顿"比"保住缓存前缀省下的钱"更划算时才开。换句话说，**在某些配置下这份成本超过收益**：缓存折扣越深、前缀越大、会话越长，每回合作废就越可能亏。

**② 读 Hermes 的代码**：`hermes_cli/config_defaults.py:569-572`（逐字）：

```
        # micro_compact: opt-in — after each turn fold the oldest un-absorbed exchange into a
        # rolling summary, amortizing compression cost. Off by default because every pass rewrites
        # sent history and breaks the prompt-cache prefix EVERY turn; enable only if the amortized
        # stall beats the cached-prefix discount.
```

要害：默认关不是"没做好"，而是一条**成本判断**写死在默认值里 —— "amortizing"（摊销）是卖点，"breaks the prefix EVERY turn"是代价，两者一起交给用户去称。

**③ mini 的实现（现状）**：✅ 已抄（`f261aa2`）。`agent/micro_compaction.py:9-15` 把上面那段注释**逐字**搬进模块 docstring，当"为什么默认关"的理由；配置层在 `hermes_cli/config.py:74-76` 定义三个键、`:296-298` 给出默认值表（`micro_compact=False` / `every_n_turns=1` / `defrag_threshold_tokens=2000`）；压缩器构造处 `agent/context_compressor.py:491-493` 收参数、`:514-517` 做夹取（`max(1, int)`）；回合钩子 `mini-hermes/run_agent.py:1664` 用 `if not getattr(c, "micro_compact_enabled", False): return` 落实"默认关 = 零 LLM 调用"。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 默认值 | `micro_compact=False`（`config_defaults.py:573`，真值解析 `agent_init.py:1507`） | 已对齐（`config.py:309-316` + `mini-hermes/run_agent.py:1664` 同默认） |
| 回收量闸门 | 主动剪枝有 `proactive_prune_min_reclaim_tokens=4096`（`config_defaults.py:566-568`）；微压缩**没有** | ⏭ 不抄（微压缩只有频率旋钮） |
| 与 checkpoint 互斥 | `checkpoint_required` 为真时**强制关微压缩**（`agent_init.py:1880-1886`，因它无 checkpoint hook） | ⬜ 没做（mini 无 checkpoint 概念） |

## §二 切一个交换：三条边界判据

**① Hermes 怎么做的**：一个 pass 只处理**一个**交换，所以第一件事是**从游标出发，切出下一个完整交换**。判据三条，缺一不可：

1. **跳过** user 行与（assistant 角色的）摘要标记，够到**第一条真 assistant** 才起手。为什么不是"第一条 assistant 就行"？因为摘要标记本身也是 `role="assistant"` —— 从盘上恢复的游标若把**标记自己**当成一个回合吞下去，摘要就会把自己的产物再摘要一遍。
2. 从那条 assistant 起，**连续吃** `assistant` / `tool` 行，直到碰到**别的角色**为止。一个 agent 回合可能长成 `assistant(tool_calls) → tool → assistant → tool → assistant(终答)` 这样一条链，整条链是**一个**交换。
3. **必须能收尾**：切点停下时，下一条**不得**仍是 `assistant`/`tool`。若走到尾部保护窗口的边界时下一条还是 assistant/tool，说明这是**轮中途停** —— 返回 `None`、**这一轮什么都不吸收**。因为标记是 assistant 角色，把它贴在 assistant/tool 行旁边会**破坏严格角色交替**；任何**别的**角色（user，或干脆不存在）才是安全切点。

为什么取**整轮**而不是"一组 assistant+tools"：**交替的合法性**。标记是一条 `assistant` 消息，而一整轮**两端总被 user 夹住**，于是 `[user][marker][user]` 依然严格交替；半截回合会让 marker 紧挨 assistant/tool 行，正是判据 3 要拒掉的形状。

为什么**从 assistant 起**、绝不从 user 起：**user 永不被摘要**。assistant 产出的是"它做了什么"的记述（读了哪个文件、跑了哪条命令、拿到什么结果），这类叙述摘要之后几乎不掉信息；而 user 的指令是**一切其余内容所派生自的意图**，而且**无法从后续工作里重建** —— 把"用现成的 retry helper，别新加一个"压成摘要，就是六轮之后 agent 满怀自信地做你叫它别做的事。所以交换**只含 assistant+tool 行**，user 那一侧永远逐字留在转写本里。代价是中间段有个**地板**（user 行只增不减、永不被吸收），但一条提示词通常只占单条工具结果成本的一丁点。

**② 读 Hermes 的代码**：`agent/micro_compaction.py:85-89`（`_find_one_exchange` 尾部的收尾判据，含逐字注释）：

```
        # Boundary must close the turn: a mid-turn stop at tail_start would put the assistant marker
        # beside assistant/tool rows. Any other role is a safe splice (avoids wedging the cursor).
        boundary = messages[idx] if idx < len(messages) else None
        if not isinstance(boundary, dict) or boundary.get("role") in ("assistant", "tool"):
            return None
```

要害：判据 3 是**拒绝**而不是修补 —— 切不出一整轮就 `return None`，宁可这轮不吸收，也不留半截回合去污染交替（"avoids wedging the cursor"：也避免游标卡在一个永远切不干净的起点上）。同一函数的 docstring（`:65-67`）逐字给出判据 1 的理由与总原则：*"user turns are never absorbed (alternation safety, verbatim user text)."*

**③ mini 的实现（现状）**：✅ 已抄（`f261aa2`）。`agent/micro_compaction.py::find_one_exchange`（`:75-112`）三条判据逐条落地：内部 `_turn_row`（`:95-97`）用 `role in roles and not is_context_summary_message(m)` 一步完成"跳 user + 跳标记"；`:99-103` 跳过非 assistant 行、`:104-107` 连续吃 assistant/tool；收尾判据在 `:109-112`，与真实版同形（边界非 dict 或仍是 assistant/tool ⇒ `return None`）。契约断言在 `tests/test_micro_compaction.py:176-182`（"边界是 assistant ⇒ 必须能收尾不成立 ⇒ 返回 None（这轮不吞）"）。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 承载形式 | `MicroCompactionMixin._find_one_exchange` 方法（`micro_compaction.py:62-90`） | 模块级 `find_one_exchange(messages, start, limit)`（`:75-112`），由 mixin 调用 |
| 三条判据 | 跳 user/标记 · 连续吃 · 必须能收尾 | 已对齐（判据与返回形状逐条一致） |
| 边界上限 | `limit = min(tail_start, len(messages))` | 已对齐（`:93` 同式） |

## §三 滚动摘要与游标：唯一一份摘要，转写本是真相之源

**① Hermes 怎么做的**：只维护两个状态 —— **游标**（第一条尚未被吸收的消息的下标）与**滚动摘要**（一份**累积**的摘要文本）。

**只留一份摘要**：不是给每个交换各存一条"逐交换摘要"，而是**恰好一份**持续累积的摘要 —— 每个新交换被**并入**它（辅助模型被要求把决策/需求/文件路径/未决问题折进去、丢掉已解决的细节、保持既有结构、并把任何凭据换成 `[REDACTED]`）。这直接换来一条清理规则：转写本里**只保留最新那条标记**，更早的 micro 标记是**严格冗余的**（当前摘要已含它们持有的一切），在被取代（supersede）时就丢掉。**为何非丢不可**：不丢就会叠起同一段文本的**近似重复副本**，每份都带各自的标题与结束标记脚手架，于是转写本**每个回合都在变长、而不是变短** —— 正好和本特性的目的相反。

**游标丢失靠扫转写本恢复**：内存游标在新进程/恢复的会话里会缺或越界。这时它**扫描转写本找最后一条摘要标记、从它之后接着往下**，并从那条标记的正文里**抠回**滚动摘要文本（好让下一个 pass 是**并入**而不是**重写**）。关键设计是：**转写本本身就是真相之源** —— 恢复会话**不会重新摘要已经做过的活**，也**不需要额外落盘一份游标/摘要状态**（避免两处状态双写不一致）。

**supersede 后要合并相邻 user 行**：丢旧 micro 标记会留下一处**结构裂缝**。设想 `[user A][user B][marker]`（那条 marker 取代了原本夹在 A、B 之间的交换）—— 两条 user 行被 supersede 挤成**相邻**，违反严格交替。修法是**把相邻的两条纯文本 user 行用 `\n\n` 拼成一条**。**为何不算破坏"user 逐字"保证**：拼接**只添一个分隔符**，两段文本**逐字原样**复制过去（`"\n\n".join(...)`），一个字都不改写、不摘要、不润色；而且判据严格 —— 只合并 `role=="user"`、非摘要标记、且 `content` 是**字符串**的行，列表内容（多模态 parts）不动。也就是说，被压掉的从来**不是** user 的内容，只是**行数**：这是保交替，不是改文本。

**② 读 Hermes 的代码**：`agent/micro_compaction.py:399-402`（`_merge_adjacent_user_turns` 的 docstring，逐字）：

```
        """Merge consecutive plain-text real user turns left by a supersede. Same ``\n\n`` join as
        ``repair_message_sequence`` pass 2, done here so the marker and cursor are never collateral
        damage of the downstream repair. Lists untouched."""
```

要害：合并**不是**这里发明的 —— 它与 `repair_message_sequence` 的第 2 遍用**同一个** `\n\n` 连接符；提前在这里做，是为了让 marker 与游标**别成为下游修补的连带损伤**（"Lists untouched" 划出安全边：只拼纯文本）。

**③ mini 的实现（现状）**：✅ 已抄（`f261aa2`）。`rolling_summary_from_marker`（`agent/micro_compaction.py:119-139`）从标记正文里抠回摘要；`_resolve_micro_cursor`（`:292-316`）实现"内存游标在窗口内就用它、否则扫窗口内最后一条摘要标记重建"，且**不额外落盘状态**；`splice_micro_marker`（`:193-214`）在 `supersede=True` 时 `stale = [... is_micro_marker(m)][:-1]`（`:209`）**只丢 micro 标记**，随后 `merge_adjacent_user_turns`；`merge_adjacent_user_turns`（`:169-190`）用 `"\n\n".join(c for c in (prev["content"], msg["content"]) if c)`（`:187`）逐字拼接，判据严格。契约断言见 `tests/test_micro_compaction.py:220 / 228 / 408-440`。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 唯一一份摘要 | 累积，只留最新标记（`micro_compaction.py:382-384`） | 已对齐（`splice_micro_marker` 的 supersede 分支） |
| 游标恢复 | 内存有效用它，否则 `last marker + 1`（`:39-60`），不额外落盘 | 已对齐（`_resolve_micro_cursor`，不额外落盘） |
| 相邻 user 合并 | `\n\n` 拼后还 `drop_stale_api_content(prev)` 让 api_content 侧车失效（`:414-416`） | 已对齐；mini 无 api_content 侧车，故无该调用 |
| rehydrate 回标 | 给最后那条标记**打上 micro 键**（`:55-57` 逐字 *"Rehydration proves containment"*） | ⏭ 不抄（B4：会让批量标记可被 supersede，有毁历史风险） |

## §四 一回合一个交换：成本上界与 no-op

**① Hermes 怎么做的**：**"一回合一个交换"不是描述、是预算** —— 一个 pass 最多吸收**一个**交换 ⇒ 每回合最多一次辅助 LLM 调用、最多一次缓存前缀断裂。无论会话多长，单回合成本**有上界**。这条同时决定了频率门的语义单位：`micro_compact_every_n_turns` 调的是"每 N 个回合跑一次 pass"，不是"每次尽量多吞几个"；而且计数器**按回合推进、不是按提交成功的 pass 推进**，所以一个空转的回合**照样把节奏往前推**、不会把门卡死。

**no-op 三条路径**（每条都零 LLM 调用、零代价）：

1. **消息太少** —— `len(messages) < 4` 根本形不成"user + 交换 + 边界"的形状，直接返回原列表。
2. **中间段里切不出可吸收的交换** —— `_find_one_exchange` 返回 `None`（收不了尾，或窗口里没有真 assistant）。
3. **头部或尾部保护罩住了整段** —— 头部 + 尾部保护窗口已经把整份转写本罩住（`compress_start >= compress_end`，中间段为空），或游标已推到窗口末端（`cursor >= compress_end`）⇒ 无可压缩区。

第三条尤其重要：它意味着"会话刚开始时**合法地**可能一次 pass 都不发生"（精译 §9：回收只有在越过尾部预算之后才起步）。而且这条路径**不能**靠"伸进尾部去找点东西压"来绕开 —— 尾部是逐字保留的，动它就破坏了它存在的前提。no-op 零成本是"默认关 + 便宜开关"的前提：**没东西可吸收的回合不花任何代价**，所以"每回合都跑一次 pass"这种激进设置也不至于白烧钱。实现上这件事被钉在一处细节上 —— 遥测基线只在**已经找到交换之后**才取，no-op 回合连计时/估 token 都不做。

**② 读 Hermes 的代码**：`agent/micro_compaction.py:215-218`（`_micro_compact` 的 no-op 门槛）：

```
        n_messages = len(messages)
        exchange = self._next_exchange(messages) if n_messages >= 4 else None
        if exchange is None:
            return messages
```

要害：三条 no-op 路径在这里**收口成一条 `if`** —— 只要有**任何**理由切不出交换，就 `return messages`，原列表原样返回、不落库、不发调用；紧邻的 `:221` 注释逐字 *"Telemetry baseline; taken only once an exchange exists so no-op turns don't pay."* 把它钉成契约。

**③ mini 的实现（现状）**：✅ 已抄（`f261aa2`）。`agent/micro_compaction.py::micro_compact`（`:446-507`）把三条路径分开落：`:461-462` 未开则返回、`:465-470` 频率门（`every_n`，按**调用次数**计）、`:473-474` `if len(messages) < 4: return messages`、`:475-477` `exchange is None` ⇒ `return messages   # ★ A3：no-op，零 LLM 调用`。第三条（头尾罩住整段）落在 `_next_micro_exchange`（`:320-338`）：`compress_start >= compress_end` 返回 `None`（`:333-334`）、`cursor >= compress_end` 返回 `None`（`:336-337`）。契约断言两条**独立**覆盖：`tests/test_micro_compaction.py:278-287`（无交换 ⇒ 零调用、不置 dirty）与 `:678-697`（**没有中间段** ⇒ 零调用、绝不伸进尾部）。

★ 第三条 no-op 路径正是反向验证 **M4**（把可压缩窗口挪进尾部保护**内部**）**干净变红**时拓出来的：改坏后，"尾部窗口内的消息**逐条原样**保留"（`tests/test_micro_compaction.py:473`）与"没有中间段就 no-op"（`:678`）两条断言同时炸 —— 说明这条路径是**真实差异**，不是理论边界。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 成本上界 | 每 pass 最多一个交换 ⇒ 一回合一次调用 | 已对齐（`micro_compact` 单次 absorb） |
| "太少"门 | `n_messages >= 4` 内联三元（`:216`） | 已对齐（`:473-474` 提前 return，等价） |
| 第三条 no-op | `compress_start >= compress_end`（`:286-287`）/ `cursor >= compress_end`（`:289`） | 已对齐（`_next_micro_exchange:333-337`） |
| no-op 零成本 | 遥测基线延后到有交换时（`:221`） | 已对齐（无交换直接 return，不算 token/不置 dirty） |

## §五 失败与降级

**① Hermes 怎么做的**：微压缩全程 **best-effort**，它**可以降级**，但**不应该能弄坏一个会话**。可能失败的地方有三处，后果各异，判据也各异：

- **辅助调用抛异常 / 返回空** —— 辅助模型不可达、额度用尽，或者这个交换**莫名其妙摘不动**。此时记一条日志、把结果当 `None`，**转写本逐字保持原样**，这次失败被**计数**（不是静默吞掉）。
- **`finish_reason == "length"`** —— 摘要是撞上 `max_tokens` 上限被截断的。这时 HTTP 是 200、也**有内容**，但内容是一份**半截合并**（*a partial merge*）。所以判据不是“成没成功”，而是“**语义上完不完整**”：**丢结果、不吸收、留着重试**。若不丢，那份缺了一截的摘要会被并进滚动摘要，而滚动摘要**此后每回合都进提示词** —— 缺口被永久固化，且再没有第二次机会补上。
- **同一交换连续 3 次失败** —— 游标**照样推过它**。没有这一条，一个坏掉的交换会在**每个回合**上被永远重试：每回合一次真实 LLM 调用 + 一次缓存前缀破裂，一次坏运气就变成**永久开销**。推过之后那些消息**留在转写本里**，由下一批批量压缩 / defrag 接手。

还有一条横贯三者的闸门：**失败不落库**。只有**真的**吸收或 defrag 了才写会话库；失败路径连库都不碰，于是“内存没改、库也没改”，两边一致。这条闸门在 mini 侧的名字就是 `_micro_dirty`。

| 情形 | 结果 | 交换去向 |
|---|---|---|
| 调用抛异常 / 返回空 | 转写本原样，失败计数 | 留在原地，下回合重试 |
| `finish_reason == "length"` | 结果丢弃，失败计数 | 留在原地，下回合重试 |
| 同一交换连续 3 次失败 | 游标推过它，计数清零 | 留在转写本，交给批量压缩 |

**② 读 Hermes 的代码**：

```text
141|        # A length stop means a partial merge; leave the exchange unabsorbed so a later pass retries.
142|        if _cc()._response_finish_reason(response) == "length":
143|            logger.warning(
144|                "micro-summarization output hit the token cap (finish_reason=length) — discarding partial summary",
145|            )
146|            return None
```
要害：这是全模块**唯一**一个“拿到了正常响应、也拿到了内容，却仍然拒收”的出口。它防的不是网络故障，是**语义半成品** —— 一段被截断的合并长得像成功，一旦吸收就永久污染那份每回合都发出去的滚动摘要。`return None` 让控制流落进下面那段失败计数（`:265-280`，注释逐字 *Track consecutive failures at the same cursor to avoid busy-looping every turn.*），于是同一个交换**下一回合还会被重试**，而不是被当成“这个交换处理完了”。

**③ mini 的实现（现状）**：`agent/micro_compaction.py::MicroCompactionMixin._record_micro_failure`（`:395-405`）+ 常量 `MICRO_COMPACT_MAX_CONSECUTIVE_FAILURES = 3`（`:58`）—— 与真实版 `context_compressor.py:646` 同值同语义。`_micro_summarize_one` 有四种返回 `None`：没接调用方 / 调用抛异常 / 返回空（`:365-381`）。入口里 `updated is None` ⇒ 记账后 `return messages`（`:494-496`），**只有**成功分支才置 `_micro_dirty = True`（`:506`；初值 `agent/context_compressor.py:520`）。回落盘在 `run_agent._micro_compact_after_turn`（`:1240-1247`）：`_micro_dirty` 为假就**完全不碰** `session_db`。已对齐。测试：`tests/test_micro_compaction.py:303 test_failure_does_not_absorb_or_persist`、`:314 test_summarize_exception_is_failure_not_crash`、`:321 test_three_consecutive_failures_push_cursor`。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 半截合并判据 | 读响应对象的 `finish_reason == "length"` 显式拒收（`:141-146`） | **✅ 已补（`1770a9e`）**：新增 `_summary_llm_meta()` 把 `finish_reason` 透出来，`agent/micro_compaction.py:377` 与 `agent/context_compressor.py:814` **两条路径都判** `length ⇒ 丢弃半截、留着重试`（与「空内容」同一条失败路径） |
| 失败计数 | `_record_micro_failure` 返回 outcome 字符串并发射遥测 | 同款计数（`:395-405`），outcome 走 `status_callback` 一行人话（清单 B2） |
| 3 次推过 | `_MICRO_COMPACT_MAX_CONSECUTIVE_FAILURES = 3`（`context_compressor.py:646`） | 同名常量 = 3（`:58`），✅ 已抄（`f261aa2`） |
| 失败闸门 | turn_finalizer 只在条数变化时记日志；落库封在 `_sync_micro_compact_to_db` 内 | `_micro_dirty` 布尔（`:460 / :506`），调用方据此决定是否落库 |

## §六 defrag：摘要自身的碎片整理

**① Hermes 怎么做的**：往一份摘要里**并得太勤**，它就会**变松垮** —— 啰嗦、而且比它承载的材料所应有的更大。每并一个交换，摘要就多一层“本次又加了什么”的措辞，却少了删除与收敛；几十个回合之后，**摘要自己**长成一个几千 token 的怪物，而它的存在意义恰恰是替掉几千 token 的历史。defrag 是这条路的**自清洁**：当滚动摘要**自身**越过一个 token 阈值（默认 `micro_compact_defrag_threshold_tokens = 2000`），**下一次 pass 不吸收交换**，而是把这份摘要**本身**再摘成一份新鲜紧凑版，转写本里那条摘要标记被**就地重写**。

“就地”二字是这一节的全部要点 —— defrag **不吸收、不推游标、不动任何消息**：

- **边界不动** ⇒ 不存在“把半截回合切坏”“把 user 行卷进来”的风险；
- **游标不动** ⇒ 下个回合照原计划吸收“下一个交换”，defrag 只让摘要更小，不改变进度；
- **它只处理摘要文本、从不处理对话消息** ⇒ “**user 永不被压缩**”这条保证在**这条路上依然成立** —— user 行根本不进它的输入。

阈值的量法是 `estimate_tokens_rough`（粗估，不是真 tokenizer），所以是**近似触发**：越过 2000 大概就触发，不承诺精确。

**② 读 Hermes 的代码**：

```text
163|        """Re-summarize the rolling summary text and rewrite the marker in place.
164|        Transcript-shape-neutral (no splice, no cursor move). Returns True when it rewrote."""
165|        old_summary = self._micro_compact_rolling_summary
166|        if not old_summary.strip():
167|            return False
168|        # Empty base turns the merge prompt into a rewrite-compactly instruction.
169|        self._micro_compact_rolling_summary = ""
170|        fresh_summary = self._micro_summarize_one(old_summary)
171|        self._micro_compact_rolling_summary = fresh_summary or old_summary
```
要害：`:168-169` 是这一节最巧的一手 —— 它**没有**另写一个“请重写紧凑一点”的提示词，而是把 base 清空，**复用同一段 merge 提示词**：原模板里 “## Current Running Summary” 此时为空，剩下的语义就退化成“把 `## Next Exchange to Merge` 那段紧凑地记下来”。一条提示词两种用途，不必维护两份。`:171` 的 `fresh_summary or old_summary` 是失败降级：defrag 摘崩了就**保留旧摘要**，绝不把摘要清成空 —— 否则等于把历史抹了。

**③ mini 的实现（现状）**：`agent/micro_compaction.py::MicroCompactionMixin._needs_defrag`（`:409-413`）+ `_defrag_rolling_summary`（`:415-442`），与真实版同形三点：**清空 base 复用 merge 提示词**（`:432`）、**只改最新那条 micro 标记**（`:439`，逆序首条 `is_micro_marker`，批量标记不动）、**失败保留旧摘要**（`:435-437`）。阈值来自 `micro_compact_defrag_threshold_tokens`，在 `agent/context_compressor.py:516-517` 夹取 `max(1, int)`（默认 2000）。入口 `micro_compact` 里 `_needs_defrag()` 命中就**只** defrag、**不**吸收本回合，成功才置 `_micro_dirty`（`:481-487`）。已对齐。测试：`tests/test_micro_compaction.py:524 test_defrag_rewrites_marker_in_place`、`:543 test_defrag_failure_leaves_summary_intact`。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 触发阈值 | `estimate_tokens_rough` ≥ 2000（`config_defaults.py:577-578`） | `estimate_text_tokens` ≥ 2000（`micro_compaction.py:411`），✅ 已抄 |
| 清空 base 复用 merge 提示词 | `:168-169` | `:432`，✅ 已抄 |
| 只改最新 micro 标记 | “Rewrite only the newest MICRO marker”（`:174-176`） | `:439`，✅ 已抄 |
| 改写后失效有界 flush 游标 | `pop(_DB_PERSISTED_MARKER)` + `_flush_scan_cursor_invalidated`（`:182-188`） | ⏭ 不抄（B3）：mini 每消息即时落库、无那个游标 |

## §七 与会话库保持同步

**① Hermes 怎么做的**：光在内存里拼接还不够。会话的**常规落盘是只追加的**：每条消息一旦产生就 append 一行、`active = 1`。`splice` 只在**内存**的 `messages` 列表里把旧交换换成那条标记 —— 数据库里那些**原始行仍标记为 active**。于是下次恢复，加载逻辑把 **active 行全读回来**：你**既**得到了新摘要，**又**得到了它刚刚替换掉的那些原始消息 —— **双重加载**，直接把会话顶过上下文上限（还可能让恢复后立刻触发一次批量压缩：花了力气，反而更满）。

修法：每次 pass 都调 `archive_and_compact`，它在**一个事务**里**原子地**做两件事 ——

1. 把 active 行**软归档**（`active = 0, compacted = 1`：摘要掉了，但仍**可搜索**，不是删除）；
2. 把压缩后的那一组作为**新 active 行**插入。

软归档是关键：恢复只读 active，所以旧交换不再出现；但它们没被销毁，检索 / FTS 仍找得到。随后这些新插入的行被**盖戳为“已持久化”**（`_DB_PERSISTED_MARKER`），使**紧跟其后**的那次只追加落盘**跳过它们** —— 否则那次 append 会把同一批内容再插一遍，就成了同一世代内的第二次双重加载。盖戳**只在 commit 成功后**做，这是它与归档之间的契约（`:311-319` 的 docstring 逐字：*Call ONLY after the commit succeeded*）。

失败时：数据库这步抛异常 ⇒ 记一条 log、**会话继续**。代价是**明确**的 —— 在下一批批量压缩清理之前，恢复会**双重加载**。也就是说这一节的失败是**降级**而不是崩溃：把“内存是新的、库是旧的”这种不一致，留到下一次批量压缩收口。

**② 读 Hermes 的代码**：

```text
343|    def _sync_micro_compact_to_db(self, compacted_messages: List[Dict[str, Any]]) -> None:
344|        """Persist the micro-compacted set to the session DB atomically and stamp rows persisted.
345|        Without this the old exchange rows stay ``active=1`` and a resume double-loads both the
346|        summary and the originals."""
```
要害：这四行把“为什么**必须**有这一步”写成了一句可证伪的因果 —— 不落库 ⇒ 旧交换行仍 `active=1` ⇒ 恢复**同时**载入摘要与原件。紧随其后的调用 `archive_and_compact(session_id, compacted_messages, tail_count=max(0, len(compacted_messages) - 1))`（`:352`）说明它不止“整份重写”：最后一条（那条新标记）是新内容，前面每条都是**搬过来的原件**，所以只把倒数 `len-1` 行标成 rewind-style，让它们的旧副本不在检索里重复命中。

**③ mini 的实现（现状）**：`run_agent._micro_compact_after_turn`（`:1240-1247`）只在 `_micro_dirty` 为真时调 `_persist_micro_compaction`（`:1251-1268`），后者走第 6 章既有的 `record_compaction`（`hermes_state_messages.py:105-145`）：**一个事务**里把老世代整份置 `active = 0, compacted = 1`，再把压缩后的上下文**整份**作新世代插入 —— 同样原子、同样**不双载**。落库失败经 `status_callback` 浮到用户眼前而不吞（`:1267-1268`），回合不崩。已对齐（“不双载”这条不变量成立）。测试：`tests/test_micro_compaction.py:640 test_wiring_absorb_notifies_and_persists`、`:654 test_wiring_failure_no_persist`。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 归档粒度 | rewind-style **部分**归档 + `tail_count=len-1`（`hermes_state_messages.py:567-628`） | 整份世代归档 + 整份重插（`record_compaction`）—— 粒度不同，结果同：不双载 |
| “已持久化”盖戳 | `stamp_db_persisted_markers` / `_DB_PERSISTED_MARKER`（`context_compressor.py:311-319`）使随后追加落盘跳过它们 | ⏭ 不抄（B3）：mini 逐条即时 append、不逐回合全表重扫 ⇒ 无需盖戳 |
| 落库闸门 | 无 dirty 概念，靠 `_micro_compact` 内部只在成功后调 `_sync...` | `_micro_dirty` 布尔（`micro_compaction.py:460 / :506`），调用方依此决定是否落库 |
| DB 步骤失败 | 记 log、会话继续，代价是下次批量压缩前恢复双载（`:356-360`） | 同款语义，经 `status_callback` 示意（`mini-hermes/run_agent.py:1700-1704`） |

## §八 它与批量压缩的关系

**① Hermes 怎么做的**：先钉一句话 —— 微型压缩**不替代**批量压缩，它是**推迟**批量压缩。三件事说明这对关系：

1. **阈值那条路仍然在、仍然会触发**。批量压缩（第 5 章）的触发逻辑一个字没改；窗口真装满了它照旧停下来做一次大摘要。微压缩只是把转写本**压在阈值之下足够低**，让那条路**很少**被走到 —— 理想是整段会话**零次**批量压缩。
2. **两份摘要标记格式相同，所以互操作**。micro 标记与 batch 标记都带 `_compressed_summary` 键（同一把 `COMPRESSED_SUMMARY_METADATA_KEY`），差别只在 micro 标记**多一个** `_micro_compact_marker` 键。于是恢复时“扫最后一条标记、重建滚动摘要”对两者都成立；而 supersede / defrag 只要看**多出来的那个键**，就能区分“可重写、可丢弃的 micro 标记”与“**绝不能**重写的 batch 标记”（batch 标记里含 micro 摘要**没有**的历史）。
3. **它没有 min_reclaim 闸门 —— 只有频率旋钮**。这是两种思路的分野，也是原文自己点出的一处**真实差异**。控住代价有两种办法：**等一次更大的胜利**（主动剪枝的做法：这次重写回收 ≥ `proactive_prune_min_reclaim_tokens`（默认 4096）才提交，否则退回原样，并等一次触发大小（runway）的占用重新长出来再武装），或者**少动手**（微压缩的做法：`micro_compact_every_n_turns` 每 N 回合才跑一次 pass）。两者服务**同一个目的** —— 让打断缓存前缀这件事更稀、更阶段性 —— 但**路子不同**：一个靠“攒够量再动”，一个靠“不管量、减少次数”。微压缩**每次 pass 都可提交**，无论这次吸收的那个交换**碰巧**省了多少，代价固定是一次缓存前缀破裂。原文的原话是：给微压缩加一个回收阈值是“显而易见、但**目前还不存在**”的后续动作。

| 键 | 默认 | 作用 |
|---|---|---|
| `micro_compact` | `False` | 总开关，默认关（每次 pass 打破缓存前缀） |
| `micro_compact_every_n_turns` | `1` | **频率**旋钮（`max(1, int)`） |
| `micro_compact_defrag_threshold_tokens` | `2000` | defrag 阈值（§六） |
| `proactive_prune_min_reclaim_tokens` | `4096` | **另一个特性**的**回收量**闸门（对照用；微压缩无对应键） |

**② 读 Hermes 的代码**（把对照的那道闸门调出来）：主动剪枝的提交判据 ——

```text
3016|        # Prompt-cache hysteresis: commit only when the reclaim is meaningful.
3017|        after = sum(_estimate_msg_budget_tokens(m) for m in pruned_msgs)
3018|        reclaimed = max(0, before - after)
3019|        if reclaimed < self.proactive_prune_min_reclaim_tokens:
3020|            self._warn_reclamation_no_op("prune:reclaim_below_minimum", current_tokens, before=before)
3021|            return messages, 0
```
要害：注释第一句 *commit only when the reclaim is meaningful* 就是“**等一次更大的胜利**”那句话的原文。而 `agent/micro_compaction.py` 全文里 `reclaim` **零命中** —— 微压缩没有任何对应的收获量判断，它把“能不能提交”交给**频率**而不是**收获量**。把这两段并排读，本节的核心差异一眼可见：一边是“量够才动”，一边是“少动”。

**③ mini 的实现（现状）**：开关与夹取均已抄 —— `hermes_cli/config.py`：`DEFAULT_MICRO_COMPACT = False`（`:80`）、`DEFAULT_MICRO_COMPACT_EVERY_N_TURNS = 1`（`:81`）、`DEFAULT_MICRO_COMPACT_DEFRAG_THRESHOLD_TOKENS = 2000`（`:82`）、`DEFAULT_PROACTIVE_PRUNE_MIN_RECLAIM_TOKENS = 4096`（`:83`），`parse_config_int` 拒绝 bool / 小数（`:259-265`）。**频率门**在 `agent/micro_compaction.py:467-473`（`every_n > 1` 才计数，no-op 不会把它卡住）。**标记互操作**也成立：`make_micro_marker`（`:152-166`）带 `COMPRESSED_SUMMARY_METADATA_KEY` + `MICRO_COMPACT_MARKER_KEY`，而批量标记走第 5 章的同一把 `COMPRESSED_SUMMARY_METADATA_KEY`。已对齐；微压缩侧**同样没有** min_reclaim 闸门。测试：`tests/test_micro_compaction.py:557 test_config_defaults_and_clamping`、`:290 test_cadence_gate_every_n`。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 与批量压缩的关系 | 推迟、不替代；阈值那条路仍然在、仍然会触发 | 同（批量压缩是第 5 章既有实现，未改动） |
| 标记格式互操作 | 同键 `_compressed_summary`，micro 多 `_micro_compact_marker` | 同（`agent/micro_compaction.py:152-166`） |
| 回收量闸门 | 微压缩**无**（`reclaim` 零命中）；prune 有 4096（`context_compressor.py:3019`） | 微压缩同样无闸门；prune 的默认值已抄（`config.py:83`），但 prune 本身在第 5 章 |
| 频率旋钮 | `micro_compact_every_n_turns`（`config_defaults.py:574-576`） | 同名键 + 同夹取（`config.py:81`；门在 `micro_compaction.py:464-470`），✅ 已抄 |

## §九 真问题与收据

**① 收据**（三组数字，主代理已复跑/复验，本章文档直接引用）：

- **双解释器全套件 1,187 通过 / 0 失败**（10 个测试文件）；本章新增 `tests/test_micro_compaction.py` **119 条 / 10 组**，只写契约与不变量断言（不测实现细节）。

- **反向验证 5/5 干净变红**（把抄来的写法改回旧/坏写法 → 对应断言**干净**变红，即进程正常退出、`失败项` 里出现预期断言名，而不是崩溃）：

  | 变异 | 改回什么 | 干净变红 |
  |---|---|---|
  | M1 | 起点判据里**不跳 user / 摘要标记** | 4 条 |
  | M2 | 去掉边界判据③「**必须能收尾**」 | 1 条 |
  | M3 | 失败阈值 `3 → 99`（卡住的交换不再被推过） | 4 条 |
  | M4 | **尾部保护失效**（可压缩窗口挪进尾部） | 3 条 |
  | M5 | **不 supersede**（旧 micro 标记不被丢弃） | 5 条 |

- **真跑 5/5 核验通过**（真 DeepSeek + 临时 `MINI_HOME`，脚本 `mini-hermes/demos/ch10_real_run.py` 可复跑）：① 转写本里**恰好 1 条** micro 标记（角色 `assistant`）；② **user 文本逐字保留**（条数 15→14，其中合并 1 条 —— 设计如此）；③ 消息数 58→51（变少）；④ DB active 行 == 当前消息数；⑤ DB 里 active 摘要行**恰好 1 条**。

**② 真问题（本章实现期拓出的，全部真实发生）**：

**真问题 1 · 反向验证「没红」还有第三种原因：那条路径原本就没有测试。** M4（尾部保护失效）第一次改完**全绿** —— 既不是改法把代码改崩、也不是测试写错，而是**那条路径原本就没有测试覆盖**：旧的 no-op 用例 `test_noop_pass_calls_model_zero_times` 只覆盖了「**头部**罩住整段」（`system + user + assistant + user` 这种小列表，头部保护就把整段吃光），而 M4 把可压缩窗口挪进**尾部**后，那条用例的输入根本走不到尾部保护，于是照样全绿。补一条「**尾部**罩住整段 ⇒ no-op」的用例（`tests/test_micro_compaction.py:678` 的 `test_no_middle_segment_is_noop_when_tail_covers_all`，并在 docstring 里点明它与旧用例守的是**两条不同的** no-op 路径）+ 重做 M4 的改法 ⇒ **3 条干净变红**。教训：反向验证「没红」= ① 改法崩 / ② 测试写错 / ③ **该路径没有测试**（覆盖空洞）。看到「绿」要先问一句「这条路径到底有没有断言」。

**真问题 2 · 真跑收据把「设计上的合并」当成失败。** 真跑核验「user 行逐字还在」时，最初拿**条数**去比，结果做对的一次运行被判红：user 行**条数**确实会减 —— supersede 丢掉一条旧 micro 标记后，`merge_adjacent_user_turns` 会把因此相邻的两条**纯文本** user 行按 `\n\n` 拼成一条（保角色交替，`agent/micro_compaction.py:169-190`）。但**文本是逐字保留的**（拼出来正好等于原来的 `\n\n` 连接）。所以收据必须改口径：按**拼接后的文本**核（`"\n\n".join(users_after) == "\n\n".join(users_before)`），并把**合并了几条**单独印出来（`demos/ch10_real_run.py:98-102`）。否则一条正确的运行会被自己的验收标准误杀。

**真问题 3 · 子代理 600s 超时（卡在真跑那一步）→ 分工教训。** 写实现的子代理在真跑那一步（真 DeepSeek 调用）超 600s 且无报告；所幸它超时前已把清单（`/tmp/ch10-checklist.md`）、模块、测试、接线**全部落盘**，主代理得以复跑套件确认全绿、自己补反向验证 / 真跑 / 收据修正与提交。复盘出的分工规则：**子代理交代码 + 契约测试；真跑、反向验证、提交由主代理做** —— 真跑要真凭据、且单次可能远超子代理的时间盒，不该压进去。

---


### 真跑 · 摘要模型选哪个值多少（instruct vs 推理）

`demos/ch10_summary_model_compare.py`：**同一份 58 条历史**、同样 3 个回合收尾钩、真 DeepSeek、临时 home，**只换 `auxiliary.compression.model`**。

| 摘要器 | 每个 pass 耗时 | 合计 | 消息数轨迹 | 滚动摘要字数 |
|---|---|---|---|---|
| `deepseek-chat`（instruct —— **不配置时的实际默认**：就是主模型） | 1.1 / 1.2 / 1.3 s | **3.7 s** | 58 → 58 → 55 → 51 | 245 / 350 / 455 |
| `deepseek-reasoner`（推理，会思考） | 1.7 / 1.6 / 1.9 s | **5.3 s**（+43%） | 58 → 58 → 55 → 51 | 210 / 335 / 438 |

两条读法：

1. **方向与上游 §8 那两条判据一致**：推理模型在这件"把交换并进摘要"的机械活上更慢（每 pass 多 ~0.5 s），**而输出并没有更好** —— 两份摘要的段落结构相同（进度 / 决策 / 文件路径 / 待办），推理版甚至略短（438 vs 455 字）。差别是"形状相同、多花时间"。
2. **默认就是主模型**：`deepseek-chat` 那一列不是"另配的小模型"，而是**不配置 `auxiliary.compression.model` 时的实际解析结果**（`auto` = 主模型，见 §〇 差异表那一行）。所以要打开微压缩，先把它钉到便宜快的 instruct 小模型上，否则**每回合**都用主模型多打一次摘要调用。

诚实边界：单机单次观测、这一档规模（每次送"滚动摘要 + 一个交换"、几千 token）—— 当**形状示例**读，不是基准；交换越大，推理模型多出的思考开销越明显。

★ 这次真跑顺带抓出并**修掉**了一处 mini 状态文案缺陷。原写法（`run_agent._micro_compact_after_turn`）用"消息条数变没变"反推 absorb / defrag，而**吸收只吞 1 条消息时是"删 1 加 1"**（交换区间 `(4,5)`）⇒ 一次**真正的吸收**被误报成"微压缩：defrag 重压了滚动摘要（条数不变）"。本次两次跑的回合 1 都是这种情况（诊断实测：滚动摘要 0 → 135 字符、游标 0 → 5、`needs_defrag=False`，走的是**吸收**分支）。

修法：**分支由当次真的走了哪条决定**，不再由结果形状反推 —— 压缩器在三条出口各记一个 `_micro_last_action`（`agent/micro_compaction.py:463` 复位 / `:490` `defrag` / `:511` `absorb`），钩子照它标文案（`mini-hermes/run_agent.py:1682-1685`）。回归证据：新增 3 条契约测试（第 10 组），其中钩子那条把旧写法还原后会**干净变红**并原样复现旧症状（`statuses=['微压缩：defrag 重压了滚动摘要（条数不变）']`）；反向验证 **3/3 红**（钩子退回按条数 → 1 红 · 压缩器不记 absorb / defrag → 各 1 红）。教训：**状态文案要按"当次走了哪条分支"标，不能按"结果形状"反推**。

## §十 差距清单

**口径**：本章 mini **已实现**（提交 `f261aa2`），mini 侧行号指该提交后的树。A 批＝已抄（只抄「决定架构形状」的）；B 批＝**没做 / 不抄**，每条给三类理由之一（**① 基座** ＝ mini 没有那条前提 / **② 规模** ＝ 纯细节或规模差距 / **③ 用不上的复杂度**），并写清 mini 现状。

### A 批（已抄，6 条）

| # | 条目 | 真实出处 | mini 落点 | 状态 |
|---|---|---|---|---|
| **A1** | `_find_one_exchange` 三条边界判据：① 跳过 `user` 与摘要标记取第一条真 `assistant`；② 连续吃 `assistant`/`tool` 到换角色；③ **必须能收尾**（边界仍是 assistant/tool ⇒ `None`，这轮不吞） | `micro_compaction.py:62-90` | `agent/micro_compaction.py::find_one_exchange`（:75-112） | ✅ 已抄（f261aa2） |
| **A2** | 滚动摘要 + 内存游标：只留**一份**累积摘要；游标丢失时扫描转写本里**最后一条标记**恢复，不额外落盘状态 | `micro_compaction.py:39-60`、`:292-301` | `MicroCompactionMixin._resolve_micro_cursor`（:292-316）+ `rolling_summary_from_marker`（:119-139） | ✅ 已抄（f261aa2） |
| **A3** | 一回合最多吸收**一个**交换 + no-op 零代价（没可吸收交换 / 少于 4 条 ⇒ 一次模型调用都不花） | `micro_compaction.py:199-218` | `MicroCompactionMixin.micro_compact`（:446-477） | ✅ 已抄（f261aa2） |
| **A4** | 失败**不吸收、不落盘**；同一交换**连续 3 次失败**就把游标推过它 | `micro_compaction.py:265-280` + 常量 `context_compressor.py:646` | `_record_micro_failure`（:395-405）+ `run_agent._micro_compact_after_turn`（:1243-1245，只在 `_micro_dirty` 时落库） | ✅ 已抄（f261aa2） |
| **A5** | 标记形状：`role="assistant"` + 三键；supersede 只丢**带 micro 键的**旧标记，**批量标记绝不能丢**；丢后合并相邻纯文本 user 行 | `micro_compaction.py:362-390`、`:31-33` + `context_compressor.py:226-230` | `make_micro_marker`（:152-166）/ `splice_micro_marker`（:193-214）/ `merge_adjacent_user_turns`（:169-190）+ 三个常量（`context_compressor.py:90-94`） | ✅ 已抄（f261aa2） |
| **A6** | 头部/尾部保护**沿用第 5 章那一套**（`_protect_head_size` + `_find_tail_cut_by_tokens`）；外加 defrag + 原子归档 | `micro_compaction.py:282-289`、`:158-193`、`:343-360` | `_next_micro_exchange`（复用 `determine_boundaries` :320-338）/ `_defrag_rolling_summary`（:415-445）/ `_persist_micro_compaction`（`mini-hermes/run_agent.py:1687-1704`，走 `record_compaction`） | ✅ 已抄（f261aa2） |

### B 批（不抄 / 没做）

| # | 条目 | 真实出处 | 理由 | mini 现状 |
|---|---|---|---|---|
| **B1** | `archive_and_compact(..., tail_count=len-1)` 的 **rewind 式部分归档** | `micro_compaction.py:352` + `hermes_state_messages.py:567` | **① 基座**：mini 没有「部分活跃行 / 携带尾部」的概念 | ⏭ 用第 6 章 `record_compaction` **整份世代归档**（老世代 active=0 全归档 + 新世代重插），同样原子、同样不双载，只是粒度不同 |
| **B2** | `_emit_micro_compaction_telemetry` 的 **19 字段 JSON 遥测** + `tokens_saved_total` 跨会话累计 | `micro_compaction.py:309-342` | **② 规模**：mini 用 `status_callback` 的**一行人话**代替（第 5 章同款口径） | ⏭ `mini-hermes/run_agent.py:1682-1685` 只发 `微压缩：吸收 1 个交换 · N → M 条消息` / defrag 一行 |
| **B3** | `_flush_scan_cursor_invalidated` / `_DB_PERSISTED_MARKER` / `stamp_db_persisted_markers` | `micro_compaction.py:182-188` + `context_compressor.py:246,311-319` | **① 基座**：那套是「有界 flush 扫描游标」的产物 | ⏭ mini 每消息**即时落库**、无该游标，defrag 直接改写标记即可（`_defrag_rolling_summary` 注释 :425-427 写明） |
| **B4** | 跨进程 resume 时把**批量标记回标**成 micro 标记（rehydrate 升级） | `micro_compaction.py:55-57` | **③ 用不上的复杂度**：回标会让批量标记变成可被 supersede ⇒ 有毁历史风险 | ⏭ mini 只**读回**摘要正文（`rolling_summary_from_marker`），**不回标** |
| **B5** | `aux_interrupt_protection()` 上下文管理器 + `main_runtime` 五字段透传 | `micro_compaction.py:118,129-132` | **① 基座**：mini 的辅助调用是**同步一次性**（`agent/auxiliary.py`），无 aux 线程中断语义 | ⏭ 摘要调用走 `run_agent._summary_llm` 直接调，无中断保护壳 |
| **B6** | `_render_micro_marker_content` 里的 `SUMMARY_PREFIX` **上百行长引导块**（别工具调用 / 别收尾 等指令） | `context_compressor.py:181-221`、`micro_compaction.py:396` | **② 规模**：mini 用第 5 章的 `COMPACTION_HEADER` + `SUMMARY_END_MARKER` **两行** | ⏭ `render_micro_marker_content`（:146-149）就两行，够用 |
| **B7** | 遥测**报告脚本** `scripts/micro_compaction_report.py`（171 行：聚合 outcome / occupancy / 净省 token / 平均交换大小 / 耗时） | `scripts/micro_compaction_report.py` | **② 规模**：它是把 B2 的 JSON 日志变成答案的工具 | ⬜ 没做（量级）：mini 没有遥测面，也就没有可聚合的日志行 |
| **B8** | **原生 Responses 服务端压缩**联动（`codex_responses_native` / `codex_responses_compact_threshold`，把服务端 `context_management=[{"type":"compaction",...}]` 与本地压缩串起来） | `agent/native_compaction.py`（352 行）+ `agent_init.py:1469,1896` | **③ 用不上的复杂度**：那条路要 `gpt-5.6` + 直连 OpenAI/Codex 后端，且把本地压缩降为 fallback | ⏭ 不抄（理由）：mini 只有一个 provider，没有服务端压缩可联动 |
| **B9** | 与**网关侧会话维护**的两层分工：网关会话卫生强制压缩（`hygiene_hard_message_limit` / `hygiene_timeout_seconds`）+ `compression.checkpoint_required` 与 `micro_compact` **互斥**（武装时强制关微压缩，因为它没有 checkpoint hook） | `hermes_cli/config_defaults.py:579-584` + `agent_init.py:1880-1886` | **① 基座**：mini 无网关、无会话卫生层、无 checkpoint 门 | ⏭ 不抄（理由）：这两个旋钮在 mini 没有宿主 |

---

## 附录 A · 从正文挪过来的引用

> 下列 `文件:行号` 的**行号均已逐条 `sed` 核对**（真实侧 `$HOME/.hermes/hermes-agent/` 锚 `f5d1926110`；mini 侧 `mini-hermes/` 提交 `f261aa2`）。每条一句「它在说什么」+ ≤3 行逐字引文。

**A-1** `agent/micro_compaction.py:3-5` —— 模块定位：滚动式逐交换摘要、**默认关**。
```
Rolling per-exchange summarization that folds old user/assistant exchanges into a single
summary marker between turns. OFF by default: every pass rewrites the prompt prefix and
breaks the provider prompt cache.
```

**A-2** `agent/micro_compaction.py:20-22` —— `_cc()` 惰性解析宿主模块，让 patch 生效。
```
keeps tests that patch
``agent.context_compressor.X`` effective (attributes are read at call time).
```

**A-3** `agent/micro_compaction.py:31-33` —— micro 标记判据（「能被证明已吸收」的才算）。
```
"""True for a summary marker provably absorbed into the rolling summary (micro, not batch)."""
return _is_summary_marker(entry) and bool(entry.get(_cc().MICRO_COMPACT_MARKER_KEY))
```

**A-4** `agent/micro_compaction.py:42-43` —— 内存游标在窗口内就直接用。
```
if head_end < self._micro_compact_cursor < tail_start:
    return self._micro_compact_cursor
```

**A-5** `agent/micro_compaction.py:48-49` —— 恢复会话时「**合并**，不是替换」。
```
# Resumed session: rehydrate the rolling summary from the surviving marker so the next
# pass merges, not replaces.
```

**A-6** `agent/micro_compaction.py:55-57` —— 重水化即「证明包含」⇒ 该标记才成为可 supersede。
```
# Rehydration proves containment: this marker (batch or micro) becomes
# supersede/defrag-eligible; unabsorbed markers never get the key.
messages[last][_cc().MICRO_COMPACT_MARKER_KEY] = True
```

**A-7** `agent/micro_compaction.py:66-67` —— 交换定义 + **user 永不被吸收**。
```
user message; user turns are never absorbed (alternation safety, verbatim user text)."""
```

**A-8** `agent/micro_compaction.py:73-74` —— 为何必须先跳过 user 与摘要标记。
```
# Skip user messages and (assistant-role) summary markers to reach a real assistant message;
# otherwise a rehydrated cursor could absorb the marker itself.
```

**A-9** `agent/micro_compaction.py:85-86` —— 边界必须收尾（轮中途停会破坏交替）。
```
# Boundary must close the turn: a mid-turn stop at tail_start would put the assistant marker
# beside assistant/tool rows. Any other role is a safe splice (avoids wedging the cursor).
```

**A-10** `agent/micro_compaction.py:141-142` —— `finish_reason=length` ⇒ 丢弃半截摘要、不吸收。
```
# A length stop means a partial merge; leave the exchange unabsorbed so a later pass retries.
if _cc()._response_finish_reason(response) == "length":
```

**A-11** `agent/micro_compaction.py:168-169` —— defrag 诀窍：清空 base 把「merge」提示词变成「紧凑重写」。
```
# Empty base turns the merge prompt into a rewrite-compactly instruction.
self._micro_compact_rolling_summary = ""
```

**A-12** `agent/micro_compaction.py:174-176` —— 只重写**最新 micro 标记**（批量标记持有更多历史）。
```
# Rewrite only the newest MICRO marker (resume rehydrates from it); a batch marker holds
# history we lack.
entry = next((e for e in reversed(messages) if _is_micro_marker(e)), None)
```

**A-13** `agent/micro_compaction.py:179-181` —— defrag 后清 `_db_persisted`，让 DB 同步重写该行。
```
# Content changed: clear the persisted stamp so the DB sync rewrites the row. An
# in-place pop on a live dict would be identity-skipped by the bounded flush scan;
# flag the finalizer.
```

**A-14** `agent/micro_compaction.py:206-207` —— 频率门语义：每 pass 破一次缓存、按**调用**计数（no-op 不卡死）。
```
# Cadence gate: each pass breaks prompt cache once. Counted per invocation so a no-op turn
# can't wedge it.
```

**A-15** `agent/micro_compaction.py:216-218` —— 少于 4 条 / 无可吸收交换 ⇒ 原样返回。
```
exchange = self._next_exchange(messages) if n_messages >= 4 else None
if exchange is None:
    return messages
```

**A-16** `agent/micro_compaction.py:242-243` —— 摘要是否「累积」要在摘要**之前**捕获。
```
# Cumulative iff it subsumes an earlier marker; captured before summarizing.
_cumulative = bool(self._micro_compact_rolling_summary.strip())
```

**A-17** `agent/micro_compaction.py:267-268` —— 失败计数按**游标**计，避免每轮空转。
```
# Track consecutive failures at the same cursor to avoid busy-looping every turn.
same_cursor = exchange_start == self._micro_compact_last_failure_cursor
```

**A-18** `agent/micro_compaction.py:277-278` —— 推过卡住的交换，留给批量压缩 / defrag。
```
# Skip the stuck exchange; it stays in the transcript for batch compression/defrag.
self._micro_compact_cursor = exchange_end
```

**A-19** `agent/micro_compaction.py:304-306` —— 游标必须从**拼装后**列表推（否则静默跳过）。
```
list: a splice collapses several rows into one marker (and may drop a superseded one), so
pre-splice indices land inside a later exchange and silently skip it.
```

**A-20** `agent/micro_compaction.py:320-323` —— 遥测只读缓存值（`/models` 探测绝不能挡住一个回合）。
```
# Cached reads only: the lazy properties can fire a synchronous /models probe.
... telemetry must never be the thing that
# blocks a turn. Unresolved simply reports null.
```

**A-21** `agent/micro_compaction.py:344-346` —— 不落库 ⇒ resume 会**双载**摘要与原文。
```
Without this the old exchange rows stay ``active=1`` and a resume double-loads both the
summary and the originals.
```

**A-22** `agent/micro_compaction.py:382-384` —— 只在 supersede **且**带 micro 键时才丢旧标记。
```
# Cumulative summary: keep only the newest marker. Drop an older one only if supersede AND
# it has MICRO_COMPACT_MARKER_KEY (provably absorbed); a batch marker holds MORE history.
```

**A-23** `agent/context_compressor.py:226-230` —— 三个键 + 「只 micro 可 supersede」。
```
COMPRESSED_SUMMARY_METADATA_KEY = "_compressed_summary"
COMPRESSED_SUMMARY_HAS_USER_TURN_KEY = "_compressed_summary_has_user_turn"
... a batch marker's content is NOT in the rolling micro summary ...
MICRO_COMPACT_MARKER_KEY = "_micro_compact_marker"
```

**A-24** `agent/context_compressor.py:357` —— 结束标记的用途（弱模型别把引用头当新输入）。
```
# Explicit end boundary: weak models otherwise read quoted headers as fresh
# user input or replay an assistant-role summary as their own output.
_SUMMARY_END_MARKER = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
```

**A-25** `agent/context_compressor.py:645-646` —— 失败阈值常量。
```
# After this many failures at one cursor, skip the exchange to avoid busy-looping.
_MICRO_COMPACT_MAX_CONSECUTIVE_FAILURES = 3
```

**A-26** `agent/context_compressor.py:1929-1931` —— 默认 `lean` 尾部（2.5% × 窗口，夹 10K~25K）。
```
if getattr(self, "tail_mode", "lean") == "lean":
    # Lean mode: tail is a small clamped recency window; the summary carries continuity.
    self._tail_token_budget = max(LEAN_TAIL_FLOOR_TOKENS, min(LEAN_TAIL_CAP_TOKENS, int(self.context_length * 0.025)))
```

**A-27** `agent/turn_finalizer.py:272-273` —— 回合计尾入口。
```
def _micro_compact_after_turn(agent, messages, final_response, logger) -> None:
    """Post-turn micro-compaction: absorb the oldest uncompacted exchange into the
    rolling summary before persist, amortizing compression across turns."""
```

**A-28** `agent/turn_finalizer.py:277-281` —— 严格 `is True` + 两道门（checkpoint 武装 / persist-disabled 都不跑）。
```
# Strict `is True` + callable gates: plugin context engines and MagicMock
# compressors pass duck checks and would wipe the transcript. Never run while
# compression.checkpoint_required is armed (no checkpoint hook here), nor for
# persistence-isolated agents ...
```

**A-29** `hermes_cli/config_defaults.py:569-573` —— 默认关 + 立章理由（每 pass 破缓存前缀）。
```
# micro_compact: opt-in — after each turn fold the oldest un-absorbed exchange into a
# rolling summary, amortizing compression cost. Off by default because every pass rewrites
# sent history and breaks the prompt-cache prefix EVERY turn; ...
"micro_compact": False,
```

**A-30** `mini-hermes/tests/test_micro_compaction.py:681-682` —— 为何要补第二条 no-op 用例（真问题 1 的落点）。
```
★ 这条与 `test_noop_pass_calls_model_zero_times` 覆盖的是**两条不同的** no-op 路径：
那条是头部罩住整段，这条是**尾部**罩住整段 —— 反向验证时只有后者能照出"尾部保护失效"。
```

---

## 附录 B · 三方对照矩阵

> 列序 `| 精译节 | 真实侧（文件:函数） | mini 落点 | 状态 |`。真实侧行号锚 `f5d1926110`，mini 侧锚 `1770a9e`。**A-1~A-29 是真实侧引用，A-30 起才是 mini 侧**。

| 精译节 | 真实侧（文件:函数） | mini 落点 | 状态 |
|---|---|---|---|
| **§0** 这一章在回答什么（摊销 / 默认关） | `micro_compaction.py:1-5`；`config_defaults.py:569-573` | `agent/micro_compaction.py:1-15` 模块 docstring；`hermes_cli/config.py:80` `DEFAULT_MICRO_COMPACT` | ✅ 已抄（f261aa2） |
| **§1** What it does（三步 / 一回合一个） | `turn_finalizer.py:272-303` `_micro_compact_after_turn`；`micro_compaction.py:199-263` `_micro_compact` | `mini-hermes/run_agent.py:1652-1685` `_micro_compact_after_turn`；`micro_compaction.py:446-507` `micro_compact` | ✅ 已抄（f261aa2） |
| **§2** 你的消息永不被压缩 | `micro_compaction.py:62-90` `_find_one_exchange` | `micro_compaction.py:75-112` `find_one_exchange` | ✅ 已抄（f261aa2） |
| **§3** 它从不触碰什么（头 / 尾） | `context_compressor.py:4041-4048` `_protect_head_size`；`:4343+` `_find_tail_cut_by_tokens` | `context_compressor.py:884-913` `determine_boundaries`；`:569-580` `tail_token_budget` | ✅ 已抄（复用第 5 章） |
| **§4** 游标 | `micro_compaction.py:39-60` `_resolve_compact_cursor` | `micro_compaction.py:292-316` `_resolve_micro_cursor` | ✅ 已抄（f261aa2） |
| **§4** 滚动摘要 | `micro_compaction.py:116-156` `_micro_summarize_one`；`:242-263` | `micro_compaction.py:342-387` `_micro_summarize_one` | ✅ 已抄（f261aa2） |
| **§4** 碎片整理（defrag） | `micro_compaction.py:158-193` `_needs_defrag` / `_defrag_rolling_summary` | `micro_compaction.py:409-442` 同名两个方法 | ✅ 已抄（f261aa2） |
| **§4** 与会话数据库同步 | `micro_compaction.py:343-360` `_sync_micro_compact_to_db`；`hermes_state_messages.py:567` `archive_and_compact` | `mini-hermes/run_agent.py:1687-1704` `_persist_micro_compaction`（走 `record_compaction`） | ✅ 已抄（粒度不同，见 §十 B1） |
| **§4** 摘要器失败 | `micro_compaction.py:265-280` `_record_micro_failure` | `micro_compaction.py:395-405` `_record_micro_failure` | ✅ 已抄（f261aa2） |
| **§5** 与批量压缩的交互 | `micro_compaction.py:382-389`（supersede 只丢 micro / 不丢批量） | `micro_compaction.py:193-214` `splice_micro_marker` + `is_micro_marker` | ✅ 已抄（f261aa2） |
| **§6** Configuration | `config_defaults.py:569-578`；`agent_init.py:1506-1515` | `hermes_cli/config.py:289-325` `get_compression_config`（四键 + 夹取） | ✅ 已抄（f261aa2） |
| **§7** Prompt caching（你要付的成本） | `micro_compaction.py:206-207`；`config_defaults.py:569-573` 注释 | `mini-hermes/run_agent.py:1658-1661` 三个门；`config.py:80`（每次 pass 破前缀） | ✅ 已抄（无回收量闸门、只有频率旋钮） |
| **§8** 选择压缩模型 | `micro_compaction.py:126-132`（`summary_model` / `main_runtime`） | `run_agent._summary_llm` → `agent/auxiliary.py`（A8 路由） | ✅ 已抄（f261aa2） |
| **§9** Measuring it（遥测 / 报告脚本） | `micro_compaction.py:309-341` `_emit_micro_compaction_telemetry`；`scripts/micro_compaction_report.py` | —（无遥测面、无报告脚本；`status_callback` 一行人话） | ⏭ 不抄（B2 / B7） |
| **§10** Failure behaviour（尽力而为） | `turn_finalizer.py:303` `except` 吞异常 | `mini-hermes/run_agent.py:1670-1672` `try/except` 吞 | ✅ 已抄（f261aa2） |
| **附录 A** 术语表 | —（术语） | —（术语） | — |
| **附录 B** 本篇提到的实现文件 | 六个真实文件（`f5d1926110`） | 对应 mini 模块（`agent/micro_compaction.py` **531 行** 等） | — |
| **附录 C** 本篇与上游的出入 | — | — | 本轮新增两处，见下附录 C |

---

## 附录 C · 上游文档与源码的出入

> 精译-10 附录 C 已写三处，先照录，再接**本轮新核的两处**。核对对象：上游文档 `website/docs/developer-guide/micro-compaction.md`（406 行，`f5d1926110` 树内）vs 源码。

**（已写三处，照录）**
1. **§7 那句标为「config comment」的引文非逐字命中。** 文档写 *"one big episodic break instead of a tiny break every tool iteration."* 并称是 "the words of the config comment"；锚点源码里没有这个逐字串，`config_defaults.py:560-568` 只说到 *"the min_reclaim gate below keeps those breaks episodic"*，更接近的完整措辞在**用户指南文档**而非配置注释。→ 语义无误，**出处标注对不上**。
2. **§9 表里的「尾部预算 64,000 token（窗口的 16%）」不是当前默认路径的数字。** `64,000 = threshold_tokens(320K) × target_ratio(0.20)`，是 **`tail_mode: legacy`** 的公式；默认 `tail_mode` 是 **`lean`**（`context_compressor.py:1929-1931`：`max(10_000, min(25_000, window × 0.025))`），对 400K 窗口算出 **10,000**。→ 「回收只在越过尾部预算之后才起步」这句只在 legacy 下复现。
3. **§9 那条遥测 JSON 是示意行。** 实际发射点用 `json.dumps(..., sort_keys=True)`（键按字母序）且总是带上文档示例里省略的 `session_id` / `messages_before` / `messages_after` / `cursor` / `main_model` / `aux_model`。→ 字段名与含义都对得上，**顺序与完整度是示例性的**（原文那行末尾本就带 `...}`）。

**（本轮新核，2 处）**
4. **§9 那句「median around 31」（秒）与它**自己那张表**矛盾。** 文档 §9 的表（第 345-351 行）给出的 pass 耗时是 `2.2 / 4.5 / 9.1 / 36.2 / 31.2` 秒，中位数是 **9.1 秒**（我按表内数字算：排序后 `[2.2, 4.5, 9.1, 31.2, 36.2]`，取第 3 个 = 9.1）；而紧接着那句 "passes ran 2 to 37 seconds, **median around 31**" 的 31 秒，其实是 §8「选择压缩模型」里本地 7B 模型的样例值（*"~31s per pass"*），被串到了这里。→ 「2 到 37」的**范围**与表一致，**中位数 31 与表不符**（表算出来是 9.1）。
5. **§4「Staying in step with the session database」把 `archive_and_compact` 说成「软归档 active 行」，但微压缩实际走的是 `tail_count=len-1` 的部分归档。** 文档原文：*"each pass also calls `archive_and_compact`, which atomically soft-archives the active rows and inserts the compacted set."* 源码里微压缩传的是 `tail_count=max(0, len-1)`（`micro_compaction.py:352`），而 `archive_and_compact` 对该尾部原文行打的是 **rewind 旗 `active=0, compacted=0`**（`hermes_state_messages.py:613-617` + 函数 docstring :573-576），**不是**「软归档」的 `compacted=1`（那个状态「仍可搜」，rewind 旗是「被取代的重复」）。→ 结论「不双载」没错，但「soft-archives the active rows」对**携带尾部的原文行**不成立，文档省略了 `tail_count` 这一层。

**未发现**：其余文档断言（默认值、`every_n` 夹取、三次失败跳过、`occupancy_pct` 定义、报告脚本字段、`finalize_turn` 包异常等）与锚点源码**逐条对得上**；另有几处为**示意 / 无法离线核**（§8 的 MLX「~31s」实测、§9 那张真实会话表的原始日志），本轮**不凑数**，标为不可核。
