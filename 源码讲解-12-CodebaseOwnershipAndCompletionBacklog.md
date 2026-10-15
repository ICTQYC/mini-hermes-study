# 源码讲解 · 第 12 章：小篇合卷 —— 代码归属图与完成回执投递（Codebase Ownership & Background Completion Backlogs）

> **本篇定位**：源码讲解为主。它是**一章两篇的合卷**（收尾路线里的"小篇合卷"）：上篇 `codebase-ownership` 是**导航表**，下篇 `completion-backlog-delivery` 是**投递机制**；逐句翻译见《中文精译-12》。
> **来源**：`官方文档-Developer-Guide/codebase-ownership.md`（**34 行**）+ `completion-backlog-delivery.md`（**52 行**）。
> **上游**：commit `f5d1926110` · `version = "0.21.3"` —— 真实侧行号一律锚它。
> **mini 侧**：行号锚本章快照 **`653b627`**。★ **本章 mini 侧没有代码可实现项**（理由见 §〇 与 §七）。

[TOC]

---

## 导言 · 机制总览：Hermes 具体怎么做的（先读这一节）

本章是**两篇小文合成的一章**，但它们在 mini 那边的处境完全不同：上篇《代码归属图》（`codebase-ownership.md`）是一张**导航表**，不是可执行机制；下篇《后台完成回执》（`completion-backlog-delivery.md`）是一套**投递机制**，而 mini 里**连生产者都不存在**（terminal 是同步阻塞的，没有进程注册表）。

下面七小节把下篇那套**投递机制**按「谁在管 / 什么时候 / 做什么 / 什么不能破 / 出错怎么办 / 跨了哪几层 / 一次完成的时间线」拆开；每条落到真实源码的 `文件:行号` 与**逐字引文**（锚 `f5d1926110`，v0.21.3）。上篇的"地图"性质留在 §一 讲。

### 0.1 谁在管（哪个模块/哪个函数是入口）

管这件事的主体是**一个共享渲染器 + 三个消费者**，而**生产者**只有一个：

| 角色 | 入口 |
|---|---|
| 生产者（谁把完成事件放进队列） | `tools/process_registry.py::_move_to_finished`（`:1358` def，入队体 `:1382-1398`） |
| 队列 | `ProcessRegistry.completion_queue`（`tools/process_registry.py:546-547`） |
| 消费者①：经典 CLI | `hermes_cli/cli_process_notifications.py::_drain_process_notifications`（`:22-43`）+ `_tui_unwrap_input`（`:45-63`） |
| 消费者②：TUI/桌面轮询器 | `tui_gateway/session_notifications.py::_notif_handle_ready`（`:533-544`）→ `_notif_dispatch_completions`（`:502-530`） |
| 消费者③：post-turn 安全网 | `tui_gateway/prompt_turn.py:434-452`（复用同一 drain + 同一 handle） |
| 共享渲染器（两个消费者共用） | `tools/process_registry_notifications.py::ProcessNotificationBatch`（`:14-36`） |

真正的**入口函数只有两个**：生产者侧的 `_move_to_finished`（进程一"完成"就在此入队，且**只有 `notify_on_complete=True` 的进程**才入队），和消费者侧的 `drain_notifications`（`tools/process_registry.py:1574-1584`，把队列掏空成 `(raw_event, formatted_text)` 对）。

逐字引（`tools/process_registry_notifications.py:14-17`，`ProcessNotificationBatch` 的类定义与 docstring）：

> ```python
> @dataclass(frozen=True, slots=True)
> class ProcessNotificationBatch:
>     """Keep completion identity until the owning surface starts its turn."""
>
>     notifications: tuple[tuple[dict, str], ...]
> ```

**要害**：这句话钉死了"谁在管"的答案——**不是队列在管，是"渲染时机"在管**。批对象拿到的是一串 `(event, text)` 元组（**事件身份原封不动**），docstring 逐字写明身份要**保留到拥有它的界面启动回合那一刻**（`until the owning surface starts its turn`）；正因为身份没在这一步被烧成字符串，消费者才有机会在**渲染之前**再判一次"这条还算不算数"。所以"谁在管"是**生产者入队 + 消费者渲染**这一对，不是某一个模块。

### 0.2 什么时候（一个后台进程完成后发生了什么）

"完成"发生在一个私有方法里：进程被移入 `_finished`、结果被持久化，随后入 `completion_queue`——**前提是该进程当初带了 `notify_on_complete`**。

逐字引（`tools/process_registry.py:1382-1384` + 入队尾与身份注释 `:1393-1396`）：

> ```python
>         if was_running and session.notify_on_complete:
>             notification = {
>                 "type": "completion",
>                 ...
>                 # Stable producer identity across checkpoint recovery (unlike a
>                 # consumer-observed completion timestamp).
>                 "started_at": session.started_at,
>             }
>             _redact_process_result(notification)
>             self.completion_queue.put(notification)
> ```

**要害**：`notify_on_complete` 是**唯一的生产闸门**——不是所有进程完成都产生回执，只有显式要求通知的那些会。事件携带 `session_id / session_key / task_id / owner_task_id / exit_code / output`（`:1384-1391`），这些字段就是**后面"归属判定"和"消费抑制"要查的身份**。注释特别点明身份用的是生产者侧的 `started_at`（`Stable producer identity`）而非消费者观测到的时间戳——因为**恢复（checkpoint recovery）之后身份仍要稳定**。

入队的容器是一个普通 `Queue`（`tools/process_registry.py:546-547`）：

> ```python
>         import queue as _queue_mod
>         self.completion_queue: _queue_mod.Queue = _queue_mod.Queue()
> ```

**什么时候取走**：两条既有路径各自在自己的节拍上"掏空队列"。经典 CLI 在 **idle tick**（`cli.py:3467`，`lambda: self._drain_process_notifications("cli-idle")`）与**回合结束后**（`cli.py:3602-3603`，`self._drain_process_notifications("cli-post-turn")`）各排一次；TUI 轮询器则在**每次醒来**先 `queue.get(timeout=0.5)` 再 `get_nowait` 掏空，形成一次"就绪快照"（`tui_gateway/session_notifications.py:663-673`）。

### 0.3 做什么（合并成一个通知回合的完整动作）

一次 drain 拿到的是"**此刻队列里所有就绪事件**"的扁平列表，随后做三件事：**切批 → 渲染 → 作为一个回合输入送入**。

**第一件，切批**，由一个只有十二行的函数做，且它**没有定时器、没有时间窗**：

逐字引（`tools/process_registry_notifications.py:39-51`）：

> ```python
> def group_process_notifications(notifications):
>     """Group consecutive completions only; watches and delegations are barriers."""
>     batch = []
>     for event, text in notifications:
>         if event.get("type", "completion") == "completion":
>             batch.append((event, text))
>         else:
>             if batch:
>                 yield tuple(batch)
>                 batch = []
>             yield ((event, text),)
>     if batch:
>         yield tuple(batch)
> ```

**要害**：合并只发生在**同一个函数的连续遍历里**——"已经就绪"（already ready）的才算进同一批，遇到 watch / 委派（非 completion）就**先 flush 再继续**（`watches and delegations are barriers`）。所以"分在不同时刻完成、但不在同一快照里的作业**不会被合并**，也**不会为了合并而等待**"——这正是文档那句 `does not add a delay or promise to combine jobs that finish at different times` 的代码落点。

**第二件，渲染**，由批对象在**拿到回合之后**做（`:136-145`）：单条直接返回原文（`if len(messages) == 1: return messages[0]`），多条才加一个固定头：

> ```python
>         header = (f"[IMPORTANT: {len(messages)} background processes completed. "
>                   "Treat these results as one batch and give one consolidated response; "
>                   "preserve failures and actionable results.]")
>         return "\n\n".join((header, *messages))
> ```

**第三件，送成一个回合**：CLI 侧批对象被放进 `self._pending_input`（`cli_process_notifications.py:42`），直到 REPL 取出它才在 `_tui_unwrap_input` 里 `render()` 并包成 `TimelineNotification`（`:51-54`）；TUI 侧由 `_notif_dispatch_completions` 在**拿到 turn**（`_notif_claim_turn` 成功）后 `render()`，再经 `_notif_submit` 作为一次 `__notif__…` 回合送入（`session_notifications.py:516-524`）。两条路都把它当**用户回合输入**，不是系统提示词。

### 0.4 不变式（保原文 · 有主才能投 · 不落系统提示词）

三条不能破的规矩，各有一条逐字注释兜底：

1. **单条保原文**——`render()` 里 `if len(messages) == 1: return messages[0]`（`tools/process_registry_notifications.py:28-29`），批对象不给孤立事件套任何壳。对应文档 `one completion keeps its original text`。
2. **有主才能投**（fail-closed）——只对"带地址的事件"要求正证明：`_notification_event_requires_owner`（`tui_gateway/session_notifications.py:106-108`）说 `evt` 是 `async_delegation` **或**带 `origin_ui_session_id`/`session_key` 才算"需证"，再由 `_session_owns_notification_event`（`:94-103`，docstring 逐字：`True iff *this* session PROVABLY owns evt`）给出**正证明**。判的是**会话键 + 压缩 lineage**，不是进程注册表本身。
3. **不落系统提示词**——逐回合的通知/note 一律走"模型可见输入"，系统提示词必须**字节稳定**。逐字引（`tui_gateway/prompt_turn.py:541-542`）：

> ```python
>     # Per-turn API-message notes: barge mid-speech, reactions, HUD surface (per-turn state
>     # that must not touch the byte-stable system prompt).
> ```

**要害**：这三条不是"优化"，是**边界**。第 1 条让"一次完成"在视觉上仍是它自己（批只是给"多条"才加头）；第 2 条让**另一个活会话永远无法收养别人的事件**（`another live session cannot adopt them`）；第 3 条是**缓存前缀不变式**——批文本要么进对话历史/用户回合，要么按 `TimelineNotification` 走 display，**不写系统提示词**，因此不因这批通知改写系统提示词；其余请求内容和 provider TTL 仍影响缓存命中。

### 0.5 出错（失败与成功同批 · 全消费不开回合 · 无主即丢弃）

- **失败与成功同批**：批头逐字写 `preserve failures and actionable results.`（`:143-144`），渲染层**不按 `exit_code` 过滤任何一条**——一个退出了 7 的进程和一个退出了 0 的进程并排躺在同一个批里。（本地探针正是靠这一点造的：`evals/completion_backlog_probe.py:90` 用 `sys.exit({7 if index == count - 1 else 0})` 让最后一条以退出码 7 结束。）
- **全消费不开回合**：如果整批在渲染时复查发现**都已被消费**，`render()` 返回 `None`，消费者**释放回合、不启动**。逐字引（`tui_gateway/session_notifications.py:518-519`）：

> ```python
>     if text is None:
>         _notif_release_turn(session)
> ```

   （CLI 侧同义：`_tui_unwrap_input` 里 `rendered and TimelineNotification(...)`，`rendered` 为 `None` 时输入被丢弃。）
- **无主即丢弃**：`_notif_handle_event` 遇到"带地址但证明不了归属"的事件**直接丢**，不收养孤儿。其 docstring 逐字：`unowned (addressed but unprovable — never adopt an orphan) → dropped`（`tui_gateway/session_notifications.py:451-452`）。

**要害**：三条错误处置的方向一致——**宁可少开一个回合、宁可丢一条证明不了归属的事件，也不把"别人的"或"已经不需要的"事件送进会话**。这是 fail-closed 的同一种姿态。

### 0.6 跨层（与进程注册表、网关自有短窗、委派账本、系统提示词的关系）

这套机制**不是孤岛**，它跨了四层：

1. **进程注册表**（抑制的来源）——"已消费即抑制"靠注册表里**两个集合**的分工。逐字引（`tools/process_registry.py:554-556`）：

> ```python
>         # Completions the agent already consumed via wait()/read_log() (output in
>         # hand): drain loops AND gateway/tui watchers skip them.
>         self._completion_consumed: set = set()
> ```

   另一个集合是 `_poll_observed`（`:557-563`，注释：`Sessions merely *observed* exited via poll(). poll() is read-only and must NOT mark consumed`）。**`poll` 故意不消费**——它只记 `_poll_observed`，避免"一次状态检查误杀 watcher 的自主投递回合"。

2. **网关自有的短窗批**（**另一套**机制）——`gateway/run_notifications.py` 有它自己的 `_completion_notification_batch_window` 默认 **0.1s**（`:1527`：`("_completion_notification_batch_window", lambda: 0.1)`），按路由键分组、超窗 flush 合并（`:1521-1543` 入队、`:1420-1448` 合并渲染）。文档逐字：`already uses its own route-keyed short-window batching. This interactive-backlog change does not replace that mechanism`——**本章的无窗口切批不替代它**，两者是并行的。

3. **委派账本**（一次委派一次投递）——批里的每个委派事件各自 claim/complete，而不是整批一个收据。逐字引（`tools/async_delegation.py:344-352`，`claim_event_delivery`）：`Claim a durable delegation event; non-durable events (and interim notices) need no token.` 完成记账见 `complete_completion_delivery`（`:399-406`）。

4. **系统提示词**——见 0.4 第 3 条：`agent/prompt_builder.py:591` 同样口径（`A per-turn fact, not a platform … so it rides the model-bound message, never the byte-stable system prompt`），`agent/AGENTS.md:125` 把"字节稳定的系统提示词"列为要断言的**消息形状不变量**。

**要害**：四层里只有第 1 层是"本章的机制"；第 2 层是**同名但不同物**的邻居（有窗 vs 无窗，极易混）；第 3 层是复用的现成基建；第 4 层是**纪律**（近似零代码）。看清这四层，就不会把"无延迟"与"0.1s 窗"混为一谈。

### 0.7 时间线（一个完成回执到用户看到一条通知）

1. 一个带 `notify_on_complete` 的后台进程退出 ⇒ `_move_to_finished` 组装事件并入 `completion_queue`（`tools/process_registry.py:1382-1398`）。
2. 消费者在自己的节拍上 drain：CLI 在 idle tick / 回合后（`cli.py:3467` / `:3602-3603`）；TUI 轮询器 `get(timeout=0.5)` 后掏空成就绪快照（`tui_gateway/session_notifications.py:663-673`）。
3. drain 出口先做**抑制筛**：`_drain_should_skip` 跳过已消费（`_completion_consumed`）或（CLI 侧）已被 poll 观测（`_poll_observed`）的事件（`tools/process_registry.py:1538-1547`，调用点 `:1601-1604`）。
4. **切批**：`group_process_notifications` 把连续的 completion 合成若干批，watch/委派是屏障（`tools/process_registry_notifications.py:39-51`）。
5. **归属判定**：TUI 侧逐事件判 `_session_owns_notification_event`（`tui_gateway/session_notifications.py:94-103`）；不属本会话的**requeue 原事件**（`:457-461`，`queue.put(evt)`），带地址但证明不了归属的**丢弃**（`:464-475`）。
6. **回合准入**：`_notif_claim_turn(session)` 失败（会话忙）⇒ 把**结构化事件**放回 `completion_queue`（不是渲染字符串），CLI 侧则进 `deferred`（`session_notifications.py:508-513`）。
7. **渲染**：拿到回合后 `batch.render(registry)`，`_live()` 在**此刻**再复查一次 `is_completion_consumed`（`tools/process_registry_notifications.py:20-22` + `:136-145`）。
8. **投递**：CLI 经 `_pending_input` → `_tui_unwrap_input`（`cli_process_notifications.py:51-54`）→ `chat`；TUI 经 `_notif_submit` → `_run_prompt_submit`（`session_notifications.py:158-162`）。用户看到**一条**合并后的通知回合。

**要害**：整条链里"抑制"发生在**两个时点**——drain 时（第 3 步，事件**已出队**）与渲染时（第 7 步，批**已入输入队列**）。文档那句 `even after it has left the process registry and entered the input queue` 说的正是**后者**；只做前者会漏。

### 验收四问

1. **"批"是不是等于"延迟窗口"？** 不是。`group_process_notifications` 里**没有任何 sleep / 定时器**（`tools/process_registry_notifications.py:39-51`），它只合并**同一次 drain 快照里已经就绪**的连续 completion；带 0.1s 窗的是网关的**另一套**机制（`gateway/run_notifications.py:1527`）。（§0.3 · §0.6）
2. **一条完成回执在被显式消费后，还能不能补投？** 不能。抑制发生在**两个时点**：drain 跳过（`tools/process_registry.py:1538-1547`）与渲染复查（`ProcessNotificationBatch._live`，`tools/process_registry_notifications.py:20-22`）——"已离开注册表、进入输入队列"之后依然有效。（§0.4 · §0.7）
3. **一个忙会话被 requeue 的是什么？** **结构化事件**，不是渲染后的批字符串（`session_notifications.py:508-513` 放回的是 `event`）。requeue 字符串会重复渲染、丢归属、令消费复查失效。（§0.5 · §0.7）
4. **批或投递状态会不会被写进系统提示词？** 不会。批文本作为**回合输入**进入（CLI `_pending_input` / TUI `_run_prompt_submit`），系统提示词必须字节稳定（`tui_gateway/prompt_turn.py:541-542`、`agent/prompt_builder.py:591`、`agent/AGENTS.md:125`）——这是**缓存前缀不变式**，不是"省 token"。（§0.4 · §0.6）

---

## §〇 差异表

★ **本章 mini 侧没有代码可实现项。** 原因只有一条：**A 篇是"地图/约定"型篇章，B 篇那套投递机制在 mini 里连生产者都不存在**——mini 的 `terminal` 是**同步阻塞**的（`tools/terminal.py:94-119`，`subprocess.run` 一次性返回），**没有后台进程**、没有进程注册表、没有 `process_manage`、没有委派/账本/watch、没有"通知回合"这个概念。**没有完成事件的**生产者**，就不可能有合并它的**消费者**——所以下表的主口径是 `⏭ 不抄（mini 无生产者）`，而不是"漏做"。唯一落地的是**文档级**的 A 篇归属图（最后一行）。真实侧锚 `f5d1926110`（v0.21.3），mini 侧锚**本章快照 `653b627`**。

| # | 面 | 真实 Hermes | mini 现状 | 性质 |
|---|---|---|---|---|
| 1 | 批渲染器 `ProcessNotificationBatch` | frozen dataclass 持有 `(event, text)`；`_live()` 渲染时复查消费；单条保原文、多条加 `[IMPORTANT: N …]` 头（`tools/process_registry_notifications.py:14-36`） | ❌ 无。没有完成事件可渲染（`tools/terminal.py:94-119` 同步阻塞，无 `notify_on_complete`） | ⏭ 不抄（mini 无生产者） |
| 2 | 无窗口切批 `group_process_notifications` | `tools/process_registry_notifications.py:39-51`：只合并连续 completion，watch/委派是屏障，**无 sleep / 无窗口** | ❌ 无。且 mini 没有 watch/委派这两种"屏障"概念 | ⏭ 不抄（mini 无生产者） |
| 3 | 通知压队（生产者闸门） | `_move_to_finished` 里 `if was_running and session.notify_on_complete:` ⇒ 入 `completion_queue`（`tools/process_registry.py:1382-1398`；队列 `:546-547`） | ❌ 无。`terminal` 没有 `background` 参数（schema 只有 `command` / `timeout`），没有进程可"完成" | ⏭ 不抄（mini 无生产者） |
| 4 | CLI 三件套（idle 排出 / post-turn 排出 / 最终解包） | `hermes_cli/cli_process_notifications.py:22-43`（drain）+ `:45-63`（unwrap，`:51-54` 是渲染点）；调用点 `cli.py:3467` / `cli.py:3602-3603` | ❌ 无。mini CLI 唯一汇报口是 `cli.py:152 _on_text` ← `run_agent.py:370-371`，**没有"通知回合"入口** | ⏭ 不抄（mini 无生产者） |
| 5 | 消费抑制（两集合 + 三入口 + 两处判断 · poll 不消费） | 两集合 `_completion_consumed` / `_poll_observed`（`tools/process_registry.py:554-563`）；三入口 wait/log/kill（`:1797-1799` / `:1763-1764` / `:1871-1887`，`kill` 的 `consume_output` docstring `:1845-1852`）；drain 跳过 `:1538-1547`+`:1601-1604`、渲染复查 `process_registry_notifications.py:20-22`；`poll` 故意不消费 `:1726-1731` | ❌ 无。依赖进程注册表；且"已消费"的前提是**有人能消费**（`process_manage` 不存在） | ⏭ 不抄（mini 无生产者） |
| 6 | 归属判定（会话键 + 压缩 lineage） | `_session_owns_notification_event`（`tui_gateway/session_notifications.py:94-103`）+ `_notification_event_requires_owner`（`:106-108`）+ `_notif_resolve_event_key` 压缩轮转解析（`:38-46`）；跨 profile fail-closed `:80-91` | ❌ 无。需要多会话/多消费者模型；mini 已有多会话网关，但没有后台进程通知的共享队列（网关会话键在 `gateway/run_turn.py:75-84` 是**作用域**，不是通知归属） | ⏭ 不抄（mini 无生产者） |
| 7 | 全消费的批不开回合 | `render()` 返回 `None`（`tools/process_registry_notifications.py:26-27`）⇒ `if text is None: _notif_release_turn(session)`（`tui_gateway/session_notifications.py:518-519`） | ❌ 无。同上，无批可"全消费" | ⏭ 不抄（mini 无生产者） |
| 8 | TUI 快照分组 + 结构化 requeue | 轮询器掏空成 ready 快照（`session_notifications.py:663-673`）；`_notif_handle_ready` 逐事件判归属、遇非 completion 先 flush（`:533-544`）；忙会话 requeue **原事件**（`:508-513`） | ❌ 无。mini 无 TUI/桌面消费者；网关同会话串行、不同会话可在线程中并发；没有后台完成事件通知通道 | ⏭ 不抄（mini 无生产者） |
| 9 | post-turn 安全网（TUI） | `tui_gateway/prompt_turn.py:434-452`：同一 `drain_notifications`（`skip_poll_observed=False`）+ `_notif_handle_ready(..., owned=True)` | ❌ 无。同 #8 | ⏭ 不抄（mini 无生产者） |
| 10 | 网关**自有**的路由键短窗批（0.1s） | `gateway/run_notifications.py`：窗默认 0.1s（`:1527`）、按路由键分组入队（`:1521-1543`）、合并渲染（`:1420-1448`，最多展示 10 条）。文档逐字：本次改动**不**替代它 | ❌ 无。mini 网关出站唯一出口是 `gateway/delivery.py:47-63`（`Delivery.deliver` → `adapter.send()`），没有通知批 | ⏭ 不抄（**抄形状不抄窗**：与 #2 的无窗口设计相反） |
| 11 | headless / ACP 自主通知循环 | 文档**负向声明**：`this change does not create a new autonomous notification loop for an API request, ACP client, or one-shot CLI`（`completion-backlog-delivery.md:31-32`）；代码侧**只有反证**、无逐字否定 | ❌ 无。不存在的机制无从抄 | ⏭ 不抄（负向声明） |
| 12 | 不落系统提示词（缓存前缀不变式） | 批/投递状态走**回合输入**（CLI `_pending_input` / TUI `_run_prompt_submit`）；不变式 `tui_gateway/prompt_turn.py:541-542`、`agent/prompt_builder.py:591`、`agent/AGENTS.md:125` | ⚠️ 天然满足但**无载体**：mini 是同步 REPL，没有"逐回合通知"这回事；`_prepend_note` 式纪律不存在 | ⏭ 不抄（无生产者；纪律本身零代码） |
| 13 | 本地验证探针 | `evals/completion_backlog_probe.py`：spawn 真本地 shell 子进程、读真完成事件、驱动生产 CLI/TUI/post-turn 路由，loopback HTTP 回合汇点替代 `chat`（文档 `completion-backlog-delivery.md:42-49`）；批用例含非零退出（`:90` `sys.exit({7 …})`） | ❌ 无。没有完成事件可喂给任何探针 | ⏭ 不抄（mini 无生产者） |
| 14 | **本章落地：README 归属映射表** | 真实版 A 篇是**镜像目录 + 一篇地图**：`codebase-ownership.md`（17 行子系统↔目录↔文档表）+ `AGENTS.md:346` 的镜像测试规则 | ✅ **已落地（文档级）**：`mini-hermes/README.md:279` 起新增节「改动落在哪、要跑哪些测试（第 12 章落地的『归属图』）」——**11 行映射表**（`:286-298`）+ 三条约定对应物（`:300-307`）；顺带把 README 陈旧的全套件数字 792 → **1,309 / 1,305**（`:322`） | 已对齐（文档） |

**两条准确性备注**

- 行 10 与行 2 常被混为一谈，但它们是**两套相反的机制**：本章的 `group_process_notifications` **无窗口**（只合并已就绪的连续 completion），网关的 `run_notifications.py` **有 0.1s 短窗**。文档明确说本章改动**不替代**后者；抄 mini 时"抄形状不抄窗"，别把两者合并。
- 行 12 的"⏭ 不抄"指的是**没有逐回合通知这条载体**（无从落纪律）；不是说 mini 违反了字节稳定不变式——mini 的 `tests/test_prompt_assembly.py` 本就守"字节稳定性"（README `:315`）。

---

## §一 A 篇：归属图是什么（地图型篇章）

**① Hermes 怎么做的**：这一篇不是机制，是一张**导航索引**——它回答的不是"怎么跑"，而是"**从一个改动 → 找到正确的源码目录 / 文档入口 / 测试目录**"。Hermes 是个大仓库，而**绝大多数贡献恰好只碰一个子系统**；于是文档把每个子系统映射到三样东西：它的**源码目录**、动它之前该先读的**文档入口**、以及它的**镜像测试目录**。这是典型的 **contributor 心智模型 / 仓库卫生规范**：没有运行时行为、没有 API、没有可复制的算法，它的产物就是**一张表**。

17 行表的三列（节选几行，全表见《中文精译-12》第一部分）：

| 子系统 | 源码目录 | 文档入口 |
|---|---|---|
| Agent core（循环、传输、压缩） | `agent/`, `run_agent.py` | [Agent Loop](https://hermes-agent.nousresearch.com/docs/developer-guide/agent-loop), [Context Compression & Caching](https://hermes-agent.nousresearch.com/docs/developer-guide/context-compression-and-caching) |
| Built-in tools | `tools/` | [Adding Tools](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-tools), [Tools Runtime](https://hermes-agent.nousresearch.com/docs/developer-guide/tools-runtime) |
| Messaging gateway | `gateway/`, `plugins/platforms/` | [Gateway Internals](https://hermes-agent.nousresearch.com/docs/developer-guide/gateway-internals), [Adding Platform Adapters](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters) |
| Session storage | `hermes_state.py`, `hermes_state_*.py` | [Session Storage](https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage) |
| Tests | `tests/`, `tests-js/` | [Contributing → Before Submitting](https://hermes-agent.nousresearch.com/docs/developer-guide/contributing#before-submitting) |

表下有**三条约定**，它们的判据各不相同（这正是这一篇真正的"内容"——表是索引，约定才是判据）：

1. **改动留在自己的子系统内** —— 判据是"**耦合边界**"：一个需要改核心文件的插件，说明它越过了自己的边界，是**设计坏味道**（design smell）；正确方向是**把通用的插件接口加宽**，而不是往核心里塞特例。
2. **每个碰过的源码目录，都跑它对应的镜像测试目录** —— 判据是"**目录镜像**"：改 `plugins/platforms/telegram/` 就要 `tests/plugins/platforms/` 绿，而不是只跑你恰好想到的那个测试文件。
3. **两个子系统都涉及时，更窄的那个拥有改动** —— 判据是"**代价**"：优先在**适配器或插件**里修，而不是在 agent core 里加分支；因为核心是**窄腰**（narrow waist），**在那里每加一样东西，会扩大共享核心的影响范围；具体调用开销取决于执行路径**。

所以这一篇的"机制"其实是**三条约定的判据**：边界越界 ⇒ 加宽接口；目录改过 ⇒ 跑镜像；核心与边缘冲突 ⇒ 边缘（更窄）拥有。

**② 读 Hermes 的代码**：这一篇没有可执行代码可引，"要害"落在它指向的**评审准则**上——那三条约定第 1 条自己就把判据外包给了仓库根的 `AGENTS.md`。逐字引（`AGENTS.md:31-36`）：

> ```
> ## Contribution Rubric — What We Want / What We Don't
>
> The project's intent layer. It serves humans aiming a contribution AND the automated triage
> sweeper, which may only close on `implemented_on_main`, `cannot_reproduce`, or `incoherent`.
> Taste-based "out of scope" closes are a human maintainer's call; the sweeper's job is to
> recognize design intent and *avoid wrongly closing a legitimate contribution*.
> ```

**要害**：归属图把"该不该改核心"这个判断**外包**给了这段评审准则，而这段准则的落点是**故意的克制**——`AGENTS.md` 紧接着逐字写：`expansive at the edges, conservative at the waist`（边缘放开、腰部收紧）。与它配套的还有镜像测试规则的**真身**（`AGENTS.md:346`）：`- **Placement mirrors the source tree.** A test lives in tests/<top-level source dir>/`。也就是说，归属图不是自说自话的表格——**它的两条硬约定都各有一份"根准则"撑腰**。这一篇的形状因此是"**一张地图 + 两条根准则**"，地图负责定位，准则负责裁决。

**③ mini 的实现（现状）**：mini 侧**没有代码项**——归属图是**索引/文档**，不是可执行机制。本章唯一的落地是**文档动作**：`mini-hermes/README.md` 新增一节「**改动落在哪、要跑哪些测试（第 12 章落地的『归属图』）**」（`README.md:279`），把真实版三条约定翻译成 mini 的现实：

- **一张 11 行映射表**（`README.md:286-298`），每行是 `你改了 | 必须跑`。它**不是照搬**真实版的镜像层次，而是**按 mini 的实际布局反推**（README 逐字：`下表由各测试文件实际 import 的模块反推，不是凭印象列的`）。例：`hermes_state_errors.py` · `hermes_state_fts.py` · `hermes_state_repair.py` · `hermes_cli/sessions_cmd.py` ⇒ `tests/test_state_db_recovery.py`（+ `test_session_storage.py`）；`tools/` · `tools/registry.py` · `toolsets.py` ⇒ `tests/test_tools_runtime.py`。
- **三条约定在 mini 的对应物**（`README.md:300-307`）：①改动留在子系统内 = mini 的**两道闸门**（`tools/` 下工具靠 AST 自动发现 + `toolsets.py:53-70` 的 `TOOLSETS` 白名单——不登记就永远看不见）；②跑镜像测试 = mini **没有镜像层次**（`tests/` 是**平铺**的 11 个 `test_*.py`），只能用上面那张表，拿不准就全套跑（双解释器）；③更窄的拥有改动 = 现成例子 `gateway/run_turn.py:75-84`（回合作用域绑在**这条消息所属的** profile 上——更窄处，而不是网关启动时的 `self.home`）。

归属映射最初落在 `mini-hermes/README.md`，当前根 `AGENTS.md` 也已包含这套协作约定。
读者使用 README 导航，续工时同时遵守 AGENTS.md；新增子系统应同步两处对应映射。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 载体形态 | **镜像目录 + 一篇地图**：`tests/` 镜像源码树（`tests/tools/`、`tests/plugins/<type>/`…，真实 `tests/` 共 4527 个跟踪文件），地图文档 `codebase-ownership.md`（17 行表） | **平铺 + 一张表**：`tests/` 只有一个目录、11 个 `test_*.py` 平铺；表在 `README.md:286-298`（11 行） |
| 约定 2（跑测试） | "跑**镜像**测试目录"（`AGENTS.md:346` `Placement mirrors the source tree`） | ⏭ **落不了地**：没有镜像层次 ⇒ 用显式映射表替代（`README.md:305`） |
| 约定 1（留在子系统内） | 靠"插件不得改核心"的**设计坏味道**判断 | ✅ **部分可落地（形态不同）**：mini 是 `tools/` + `toolsets.py` **两道真实闸门**（`toolsets.py:53-70`），但没有插件系统 ⇒ 真实版那个具体场景不存在 |
| 约定 3（更窄的拥有） | "窄腰"论（`AGENTS.md` 评审准则） | ✅ **可落地为一条原则**：已有现成例子 `gateway/run_turn.py:75-84`，只是缺一个正式的名字 |
| 落点文件 | 仓库根 `AGENTS.md`（评审准则 `:31`）+ 镜像测试规则 `:346` | **README.md**（`:279` 起）；`mini-hermes/AGENTS.md` **不存在**，根 `AGENTS.md` 当前也已包含归属映射 |

---

## §二 批合并：没有窗口的合并

**① Hermes 怎么做的**：一个会话里可能有好几个后台进程先后完成。若每个完成回执各开一个回合，模型就会被叫醒 N 次、对 N 条互不相干的输出各答一遍——用户看到的是 N 条碎通知，模型浪费的是 N 个回合。本章要解决的就是这件事：把**同一次「就绪」里已经躺在队列中的连续 completion** 收成一条通知回合。

所谓「就绪（already ready）」不是「最近几秒内完成的」，而是**在同一个 drain / 轮询快照里被一次性弹出**的那批事件。两个消费者各自定义了「就绪快照」：经典 CLI 的 `drain_notifications` 一次把队列掏空（内部 `while not self.completion_queue.empty()`），TUI 轮询器先 `queue.get(timeout=0.5)`、再按 `queue.qsize()` 用 `get_nowait` 掏空（真实 `tui_gateway/session_notifications.py:664-673`）。**快照的形成方式决定了「就绪」的边界**：能进同一个快照的，就属于同一批候选；凡是「现在还没到队列里」的，一律不算。

切批函数只做一件事：**在这一次遍历里，把连续的 completion 攒起来；一碰到非 completion 就先把手里那一段交出去**。判据是「事件类型」，不是「时间」：

| 遍历中遇到 | 动作 |
|---|---|
| `type == "completion"` | 追加进当前 `batch` |
| `type == "watch_match"`（watch 命中） | **屏障**：先 flush 当前批，再把这条**单独** yield |
| `type == "async_delegation"`（异步委派） | **屏障**：同上，单独 yield |

于是「watch 与委派保持独立、且保持原序」不是例外规则，而是**屏障的副产品**：它们把两段 completion 切成两个批，也就可能多开一个回合——这是**刻意的保序代价**，不是漏合并。

**这不是「延迟合批」——这是本节最容易被读错的一点。** 文档那句逐字是 *coalesce consecutive background-process completions that are already ready for one conversation into a single notification turn*；紧跟的第二句把边界钉死：*This does not add a delay or promise to combine jobs that finish at different times.* 对应到实现，`group_process_notifications`（`tools/process_registry_notifications.py:39-51`）**没有任何 `sleep`、没有定时器、没有时间窗**——它是个**纯函数**，只对「已经收集好的那个列表」做一次线性切段。**「不同时刻完成的作业」只要不落在同一个快照里，就永远不会被合并，也不会为了合并而多等哪怕一毫秒。** 反过来理解：如果给这个函数加一个「等 0.1s 收集更多」的窗口，那就等于答应「合并不同时刻完成的作业」——正是文档用整句话否认掉的东西。

**别把它与网关那套批处理混为一谈**：消息网关**自有**一套按**路由键 + 短窗**的批处理，窗口默认 **0.1s**（真实 `gateway/run_notifications.py:1527`：`("_completion_notification_batch_window", lambda: 0.1)`），按 `_event_route_key(evt, _COMPLETION_BATCH_KEY_FIELDS)` 分组、超窗才 `_flush_process_completion_batch`。**这是两套机制**：网关照的是「同一会话/平台/聊天室的并发完成，短暂攒一下再发一条」，交互侧的 `group_process_notifications` 照的是「同一次快照里已就绪的连续完成，**不等待**」。文档明说本次交互侧改动 *does not replace that mechanism or alter adapter sends*——所以「无窗口」与「0.1s 窗」是**相反**的两句话，不能互相借词。

最后是**批内不做取舍**：`render()`（真实 `:24-33`）把「活着的」条目原样拼起来——**失败与成功同批**（`preserve failures and actionable results` 就在那段头里），渲染层**不按 `exit_code` 过滤任何一条**；而且**单条保留原文**（`if len(messages) == 1: return messages[0]`），只有多条才加 `[IMPORTANT: N background processes completed. …]` 的头。这与网关侧那条最多展示 10 条的限幅是两回事——交互侧**不截断条目数**。

**② 读 Hermes 的代码**：`tools/process_registry_notifications.py:39-51`（`group_process_notifications`，逐字）：

```text
39: def group_process_notifications(notifications):
40:     """Group consecutive completions only; watches and delegations are barriers."""
41:     batch = []
42:     for event, text in notifications:
43:         if event.get("type", "completion") == "completion":
44:             batch.append((event, text))
45:         else:
46:             if batch:
47:                 yield tuple(batch)
48:                 batch = []
49:             yield ((event, text),)
50:     if batch:
51:         yield tuple(batch)
```

要害：整个函数体里**没有一次等待**——它只遍历一个**已经拿到手**的列表（`notifications`），所以「合并」的边界等于「这次调用拿到的列表的边界」，也就是「就绪快照的边界」。第 40 行 docstring 是本节的题面：`consecutive completions only`（只并连续的）+ `watches and delegations are barriers`（watch/委派是屏障）；第 45-49 行就是「屏障」的落地——遇到非 completion 先把 `batch` 交出（`:47`）、再单独交这一条（`:49`）。**没有 `time.sleep`、没有 `asyncio.sleep`、没有 deadline**，这正是文档「不新增延迟、不承诺合并不同时刻完成的作业」的代码出处；把这个函数与网关那个 `_completion_notification_batch_window = 0.1` 并排读，两套机制的差别一眼可见。

**③ mini 的实现（现状）**：⏭ **不抄——mini 没有生产者，无物可合**。合并的前提是「队列里有完成回执」，而 mini **不生产**这种事件：

- mini 的终端工具只有一个**同步阻塞**的 `terminal_tool`（`tools/terminal.py:94`），它直接 `subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout, cwd=resolve_context_cwd())`（`:96-106`）——**前台、跑完即返回**；schema 里**没有 `background` 参数**（`:77-87`），超时就杀（`:107-109`），结果即时以 `tool_result(exit_code=…, stdout=…, stderr=…)` 返回（`:115-119`）。它**不产生「以后才完成」的事件**：一次调用就是一个回合内的一个同步结果。
- mini **没有进程注册表**。全仓唯一出现 `process_registry` 这个词的地方是 `tools/terminal.py:17-18` 的**注释**，而且是在**明说「故意不做」**：*「真正的复杂度在 Hermes 那两个 process_registry 文件里（多父 agent 同时起进程 + 持久化 + 重启恢复 + 越权读防护），而那需要真的有那些场景。」*紧接着 `:20` 给出纪律原话：*「在只有一种后端的时候做一张"后端表"，就是投机性基础设施」*。

**推理链只有一步**：合并是**消费端**对「一批已就绪的 completion」做的事；mini 既没有后台进程、也就**没有 completion 事件**，那么「合并」这个动作**在 mini 里没有输入**——不是「做得薄」，是**没有物可合**。硬要造一个 `group_process_notifications` 等价物，它会是一个**永远收不到参数、也永远没人调用**的死函数（mini 的 CLI 是同步串行 REPL，回合之间没有队列、没有「回合后的通知排出」），这正撞在 `tools/terminal.py:20` 那条纪律上：为不存在的场景抽基础设施，就是投机。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 生产者（完成事件） | `notify_on_complete` 的进程移入 `_finished` 后入 `completion_queue`（`tools/process_registry.py:1382-1398`；队列定义 `:546-547`） | ❌ 无：`terminal_tool` 同步 `subprocess.run`，无后台、无队列（`tools/terminal.py:94-119`） |
| 「就绪快照」的来源 | CLI `drain_notifications` 一次掏空 / TUI 轮询器 `get`+`get_nowait` 掏空（`tui_gateway/session_notifications.py:664-673`） | ❌ 无：同步 REPL，回合间无队列 |
| 切批函数 | `group_process_notifications`：只并连续 completion、watch/委派为屏障、**无窗口**（`tools/process_registry_notifications.py:39-51`） | ⏭ 不抄（mini 无 completion 事件，函数无输入） |
| 合并出的一条批 | `ProcessNotificationBatch.render`：单条保原文、多条加 `[IMPORTANT: N …]` 头、失败与成功同批（`:24-33`） | ⏭ 不抄（同上） |
| 「另一个窗口」参照 | 网关自有的**路由键短窗 0.1s**，与本节无窗口设计**相反**且**不互相替代**（`gateway/run_notifications.py:1527`） | ⏭ 不抄（mini 网关通知面只有 `gateway/delivery.py::deliver`） |

---

## §三 消费抑制与归属：谁能认领，谁说了算

**① Hermes 怎么做的**：一个完成事件从「进程退出」到「用户看到」要穿过好几个队列与对象——生产入 `completion_queue`、被 drain 弹出、被 claim、被装进批、放进输入队列、最后才渲染成一个用户回合。这段旅程里有一个**必须被保住的东西**：事件的**身份**（`session_id` 等）。因为「这条回执该不该出现」的最终答案，可能是在**旅程末段**才被改写——代理在同一个回合里显式 `wait`/`read_log`/`kill` 拿到了输出，那条完成回执就不该再弹一次。若在旅程**起点**就把身份丢掉（比如早早渲染成一段字符串、只留文本），到末段就**无从复查**了。所以 `ProcessNotificationBatch` 的 docstring 逐字写的是：*Keep completion identity until the owning surface starts its turn.*——身份**保留到拥有它的那个界面真正开始回合的那一刻**，那一刻才 `render()`。

这条「保留到派发」的机制落在一个双时点的**复查**上。**需要两个时点**，是因为抑制信号可能来得比 drain 晚：drain 时事件可能还没被消费（事件已经离开注册表、进了输入队列），此时再发生的显式消费，只能在**渲染时**复查到——这正是文档那句 *even after it has left the process registry and entered the input queue* 的落点。两处一个都不能少：只做 drain 那一处，会漏掉「已入队之后才被消费」的窗口。

进程注册表为此维护**两个不同的集合**，分工是全章最巧的一处：

| 集合 | 谁写 | 含义 | 谁读 |
|---|---|---|---|
| `_completion_consumed` | `wait()` / `read_log()`（只读到底）/ `kill_process(consume_output=True)` | **真消费**：调用方手里已经有输出 | drain 跳过 + **TUI/渲染复查**都认 |
| `_poll_observed` | 仅 `poll()` | **只读观测**：仅够 CLI 同回合去重 | 只被 CLI 的 `drain_notifications` 读；**网关/TUI 轮询器刻意忽略** |

关键在 `poll` **故意不消费**（真实 `tools/process_registry.py:1726-1731`）：`poll()` 是**只读**的状态检查，若它把会话记进 `_completion_consumed`，就等于「查一眼状态 = 认领了结果」——**会误杀 watcher 的自主投递回合**（把本该自动送达的完成通知抑制掉）。所以 `poll` 只记 `_poll_observed`（吃住 CLI 的「同一回合内已经内联看到结果、别再插一条 `[SYSTEM: …]`」），而 `_drain_should_skip` 对 CLI 传默认 `skip_poll_observed=True`、对 TUI 传 `False`（`prompt_turn.py:442`）——**同一批事件，两个消费者的跳过语义不同**，这不是不一致，是各自场景的要求。两集合属于注册表实例；常驻注册表使它们长期存活，只在会话**彻底不再被追踪**时才清（`:2186-2190`：`self._completion_consumed &= tracked`）。

**归属判定则完全不看注册表，只看「会话键 + UI 起点 + 压缩 lineage」。** 一条 completion 要被投递，必须先证明「**本会话确凿地拥有它**」——这是一个 **fail-closed** 的正证明：`_session_owns_notification_event`（`tui_gateway/session_notifications.py:94-103`）先查 `session` 是否已 `_finalized`（是→`False`），再比 `origin_ui_session_id == sid`，最后才比 `session_key`——注意最后一句用的是 `_notif_resolve_event_key(evt_key, session)` 把**被压缩轮转的父键**解析到「续接尖端」（`:38-46`，走 `db.resolve_resume_session_id`）。**判据是会话键与压缩 lineage，不是进程注册表**：注册表提供的是「消费没消费」（`is_completion_consumed` `:1434-1436`），归属提供的是「该不该给你」。两者是正交的两问，缺一不可。

哪类事件**必须**过这道门？`_notification_event_requires_owner`（`:104-108`）逐字：`evt.get("type") == "async_delegation"` **或** `bool(evt.get("origin_ui_session_id") or evt.get("session_key"))`——即「委派」与「任何带归属线索的 addressed 事件」都必须正证明；无主（ownerless legacy、进程全局）才可放行。**判不过就丢弃，不收养孤儿**：`_notif_handle_event`（`:449-474`）的 docstring 逐字 *unowned (addressed but unprovable — never adopt an orphan) → dropped*，代码里对应 `return True`（丢弃）+ 一条 warning/debug 日志，**绝不**因为「反正没人认领」就塞给当前会话。跨 profile 时另有补强：`_notif_other_profile_session_owns`（`:80-91`）——因为每个轮询器都 drain **同一条进程全局队列**，而 lineage 只在**各自**的 store 里查，若不跨 profile 找一遍，profile B 弹出一个属于 profile A 压缩父键的事件就会「到处找不到主」而被永久丢弃。

最后两条收口：**整批都被消费 ⇒ 不开回合**。`render()` 在 `_live()` 过滤后若一条不剩，返回 `None`（`:26-27` `if not messages: return None`）；TUI 侧 `_notif_dispatch_completions` 拿到 `text is None` 就只 `_notif_release_turn(session)`、**不提交回合**（`:518-519`）。CLI 侧同理：`_tui_unwrap_input` 里 `rendered and TimelineNotification(...)`，`rendered` 为 `None` 时输入被丢弃。这就是文档 *An entirely consumed batch starts no turn* 的落地——**先拿到回合名额、再渲染、发现空了就还名额**，顺序不能反（否则会先扣名额又不开回合）。

**② 读 Hermes 的代码**：`tui_gateway/session_notifications.py:94-103`（`_session_owns_notification_event`，逐字）：

```text
94: def _session_owns_notification_event(sid: str, session: dict, evt: dict) -> bool:
95:     """True iff *this* session PROVABLY owns ``evt`` (UI origin is this live session, or ``session_key`` raw/compression-
96:     resolved matches) — the fail-closed gate for addressed notifications, without the orphan-adoption fallback."""
97:     if session.get("_finalized"):
98:         return False
99:     if str(evt.get("origin_ui_session_id") or "") == str(sid or ""):
100:         return True
101:     evt_key = str(evt.get("session_key") or "")
102:     current_keys = _notif_current_keys(sid, session)
103:     return bool(evt_key) and (evt_key in current_keys or _notif_resolve_event_key(evt_key, session) in current_keys)
```

要害：这段是「谁说了算」的**全部判据**，也是 fail-closed 的模板。三道门按「先死、再强、后弱」排：已 `_finalized` 的会话**直接否**（`:97-98`，死会话不该再认领任何东西）；`origin_ui_session_id` 是本活会话的 sid 则**直接是**（`:99-100`，这是最强证据——事件就是在这个界面上被发起的）；两门都不过，才退回**会话键比对**（`:101-103`），且键比对**两种形态都认**：原始键在 `current_keys` 里，**或**由 `_notif_resolve_event_key` 把压缩父键解析成续接尖端后命中也算。docstring 最后半句 `without the orphan-adoption fallback` 就是「**无主直接丢弃、不收养孤儿**」的原文出处——正证明不通过，就是**没有主**，不猜、不塞。

**③ mini 的实现（现状）**：⏭ **不抄——mini 既没有注册表，也没有消费者**。这一整节的两根支柱在 mini 里都不存在：

- **没有「消费」这一半**：消费抑制要挂在 `wait/log/kill/poll` 这些**显式入口**上，而这些入口属于 `process_manage` 与进程注册表——mini **全仓 grep `process_manage|process_registry` 零命中**（唯一命中是 `tools/terminal.py:17` 那句「故意不做」的注释）。既然没有「谁在消费」，也就无所谓 `_completion_consumed` / `_poll_observed` 的分工。
- **没有「归属」这一半**：归属的前提是**多会话 / 多消费者**共用一条队列，而 mini 的 CLI 是串行交互，而网关已支持多会话；这里仍缺共享后台完成队列——CLI 是同步串行 REPL（`mini-hermes/cli.py` 的 `repl`：`input()` → `agent.run_conversation()`），唯一汇报口是 `cli.py::_on_text`（`mini-hermes/cli.py:153` 的 `print(text)`），由 `mini-hermes/run_agent.py:497` 的**唯一一处** `self.text_callback(final_response)` 调用。缺的是共享完成队列及其通知消费者，而不是缺少多个网关会话；`origin_ui_session_id`、压缩 lineage 解析、跨 profile 归属这些判据统统无对象可判。
- 顺带，连**「通知回合」这个概念**在 mini 里都不存在（没有 `cli_process_notifications.py` / `session_notifications.py` / `prompt_turn.py` 的等价物），`_notif_release_turn` / 「全消费的批不开回合」也就没有落点。

**一句话**：本节两条机制（消费抑制、归属判定）都是**为「多个消费者抢同一条队列」这件事服务的**；mini 里这件事**不存在**，所以不是「做得浅」，是**没有要解决的问题**。硬抄会得到一对**永远不触发**的集合与谓词——又是一个「为不存在的场景抽基础设施」的例子（mini 自己的纪律原话见 `tools/terminal.py:20`）。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 身份保留 | `ProcessNotificationBatch` 冻结 `(event, text)`，`render` 时才复查消费（`tools/process_registry_notifications.py:14-36`） | ⏭ 不抄（无批对象、无「保留到派发」的时点） |
| 消费抑制两时点 | drain 跳过（`tools/process_registry.py:1538-1547`，调用点 `:1601-1604`）+ 渲染复查（`_live` `:20-22`） | ⏭ 不抄（无进程注册表，无 drain/render 两时点） |
| 两集合分工 + poll 不消费 | `_completion_consumed` / `_poll_observed`（`:554-563`）；`poll` 只记 `_poll_observed`（`:1726-1731`） | ⏭ 不抄（无 `wait/log/kill/poll` 入口） |
| 归属判定（fail-closed 正证明） | `_session_owns_notification_event`：sid + `origin_ui_session_id` + 压缩 lineage（`tui_gateway/session_notifications.py:94-103`）；无主丢弃不收养（`:449-474`） | ⏭ 不抄（mini 缺少后台完成通知生产者与对应消费者） |
| 全消费的批不开回合 | `render` 返回 `None` → `_notif_release_turn`（`session_notifications.py:518-519`） | ⏭ 不抄（无「回合名额」概念） |

---

## §四 为什么不落系统提示词

**① Hermes 怎么做的**：批与它的投递状态**都不**进系统提示词（`Neither a batch nor its delivery status is persisted into the system prompt`）。这不是为了省 token，而是一条**缓存不变式**——**每会话的系统提示词必须字节稳定**。它是"缓存前缀"：提供商侧按请求开头**逐字节**比对，系统提示词里一旦多出一行"第 3 个后台进程完成了"，整段缓存前缀当场作废，变化点之后的旧前缀无法沿用；新前缀建立缓存后，后续请求仍可能命中，不会因此永久全量计费。文档结尾那句原话把口径钉死：*"It measures turn admission at the notification boundary, not model token savings."*（上游 doc 第 52 行）——这条设计量的是**通知边界上的回合准入**，不是模型层省下的 token。所以"不落提示词"是**正确性**要求（缓存前缀不许被逐回合事实污染），不是**优化**。

于是批的投递走**另一条路**：作为**回合输入**进模型。CLI 侧，批对象先放进 `_pending_input` 队列，**直到 REPL 取出它才 `render()`**，再包成 `TimelineNotification` 当作**用户回合输入**进入会话（`hermes_cli/cli_process_notifications.py:22-43` 排出 → `:45-63` 解包；调用点 `cli.py:3467`（idle）/ `cli.py:3603`（post-turn））。TUI 侧经 `_notif_submit` → `_run_prompt_submit`（`tui_gateway/session_notifications.py:158-162`）提交。它要么进对话历史 / 用户回合，要么按 `TimelineNotification` 走 display，**从不写进系统提示词**。

这条纪律在真实侧有**四处逐字凭证**，全都指向"逐回合的通知 / note 一律骑在**模型可见输入**上，不碰系统提示词"：

- `tui_gateway/prompt_turn.py:541-542`：逐回合 API-message note 是"**must not touch the byte-stable system prompt**"的 per-turn state；
- `tui_gateway/session_notifications.py:762-763`（`_prepend_note` docstring）：note 前缀加到 MODEL INPUT 上，"leaving the prompt alone … **the cached prefix survives**"；
- `agent/prompt_builder.py:591`（`hud_surface_note` docstring）："A per-turn fact, not a platform … so it rides the **model-bound message, never the byte-stable system prompt**.";
- `agent/AGENTS.md:125`：测试该断言 "message-shape invariants (alternation, **byte-stable system prompt**) rather than snapshotting prompt text".

一句总纲：**系统提示词字节稳定是前提，任何"这一轮才发生的事"（barge mid-speech、reaction、HUD surface、后台完成回执）都必须走模型输入，不落提示词。** 这就是"不落"这条约束的全部动机；批只是这条规矩在"后台完成"场景下的一次应用。

**② 读 Hermes 的代码**：`tui_gateway/prompt_turn.py:541-542`（逐字）：

```
    # Per-turn API-message notes: barge mid-speech, reactions, HUD surface (per-turn state
    # that must not touch the byte-stable system prompt).
```

**要害**：两行注释把 "per-turn state" 与 "byte-stable system prompt" 直接**对立**起来——凡**逐回合**才存在的事实，都定义在"不许碰系统提示词"这一侧。它一个字没提"省 token"，只说 **byte-stable**：这条约束的性质是**缓存前缀的正确性**。看懂这句，"批不落提示词"就不难理解了——批是逐回合产生的，天然属于"per-turn state"。

**③ mini 的实现（现状）**：mini 侧**没有**完成回执这套投递（无生产者，见 §五③），但这条**不变式本身**在 mini 是**已落地**的，而且从第 3 / 10 章起就在守：

- 系统提示词**装配一次、缓存在 agent 上**——`agent/system_prompt.py:292` 的 `build_system_prompt` 命中 `_cached_system_prompt` 直接返回（`:307-310`），在压缩、技能重载、引擎上下文更新等明确边界重建；其 docstring 逐字："**缓存是这一章的立章之本，不能省**……后果是'同一会话内系统提示词字节不稳定'——**这个 bug 没有症状，只有账单**。"
- "逐回合的话"在 mini 也走**请求副本、不动历史**——`run_agent.py:564-569` 的 `ephemeral_system_prompt` 只拼到**本次 API 调用**的"有效系统消息"末尾，`messages` 本身不动（注释逐字：`messages stays untouched`）；`_build_api_messages` 返回的副本"**就是发给提供商的那份请求**……**动它就是在动缓存**"。
- 契约测试 `tests/test_prompt_assembly.py:12` 第 2 组就叫"**字节稳定性 ★**"（同一会话反复构建字节完全相同、重建后仍相同、记忆写入**不**立即进提示词）。

也就是说：真实的"批不落提示词"这条规矩，其**基础系统提示词缓存已经存在；但 mini 的 `ephemeral_system_prompt` 会修改本次请求的系统消息，不能当成上游“把动态通知放到用户输入”的完全等价实现，只是 mini 现在还没有"批"这种东西来骑它。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| "不落提示词"的动机 | 每会话提示词字节稳定 = 缓存前缀不变式（`prompt_turn.py:541-542` 等四处凭证） | ✅ 同口径（653b627）：缓存系统提示词（`agent/system_prompt.py:292`）+ 逐回合话走请求副本（`run_agent.py:564-569`） |
| 批与投递状态 | **不**进系统提示词；批走 `_pending_input` / `_run_prompt_submit` 作为回合输入 | ⏭ 不抄（mini 无生产者）：`_pending_input` 与 `_run_prompt_submit` 这两个入口在 mini 都不存在 |
| 载体 | 逐回合 note 一律骑模型可见输入 | ✅ 有载体（提示词字节稳定测试组在）；**缺**骑上去的"批" |

---

## §五 五个消费者的归属与入口

**① Hermes 怎么做的**：同一条"完成回执"，在 Hermes 里有**五个投递面**，各管一段：Classic CLI、TUI/Desktop 轮询器、post-turn 安全网、消息网关、无头/非交互。它们**CLI 与 TUI 共享批渲染器，消息网关另有自己的 `_format_coalesced_process_completions`，但**归属入口各不相同**——这正是本节的题面。

**为什么必须共享一个渲染器**：批的**形状**（单条保原文、多条加 `[IMPORTANT: N background processes completed…]` 头、`_live()` 在渲染时复查消费）是一条**跨消费者的契约**。若每个消费者各写一份渲染，同一批在 CLI 与桌面会渲染成**不同文本**，`preserve failures and actionable results` 这条纪律立刻出现漂移（"CLI 保住了失败项、桌面没有"）；更硬的是，**消费抑制的复查点就在渲染里**（`render()` → `_live()` → `is_completion_consumed`），各写一份就是各造一个**可能忘记复查**的抑制点。所以渲染器只有一个，消费者只负责"把已就绪的结构化事件**按归属认领、排空、投给这个渲染器**"。

五个面的归属与入口：

| 消费者 | 归属与入口 | 关键点 |
|---|---|---|
| **Classic CLI** | `hermes_cli/cli_process_notifications.py:22-63`（`_drain_process_notifications` + `_tui_unwrap_input`）；调用点 `cli.py:3467`（idle）/ `cli.py:3603`（post-turn） | 排出即认领（`claim_event_delivery`），把**批对象**放进 `_pending_input`，直到 REPL 取出才 `render()` |
| **TUI/Desktop** | `tui_gateway/session_notifications.py:502-544`（分派 + 就绪快照分组）+ `:663-673`（轮询器掏空队列成 ready） | **忙会话 requeue 结构化事件，而非渲染后的批字符串**（`registry.completion_queue.put(event)`，`:508-513`） |
| **post-turn 安全网** | `tui_gateway/prompt_turn.py:434-452` | 复用同一 `drain_notifications` + `_notif_handle_ready(..., owned=True)`（归属已在外层证过，跳过重复门禁） |
| **消息网关** | `gateway/run_notifications.py:1420-1448`（`_format_coalesced_process_completions`）+ `:1527`（窗口默认 `0.1`s） | **自有**的"路由键 + 短窗"批；本次改动**不替代**它 |
| **无头/非交互** | 文档负向声明：*"this change does not create a new autonomous notification loop for an API request, ACP client, or one-shot CLI."* | ★ **只有反证，无逐字代码否定**（如实，见下） |

两条最容易混的边界，逐条钉死：

- **网关是另一套机制，不是本章这个。** CLI / TUI / post-turn 走的 `group_process_notifications` 是**无窗口**的（只合并**同一次排出快照里已就绪**的事件，**不为合并而等**）；网关侧另有一条**路由键 + 0.1s 短窗**的批（`_completion_notification_batch_window` 默认 `0.1`，按 `_event_route_key` 分组、超窗 `_flush` 成一条合成事件，`_format_coalesced_process_completions` 最多展示 10 条）。文档明说这条 interactive-backlog 改动**不替代**网关这套、也不改 adapter 的发送。把两者混为一谈，就会给"无窗口"这批**加一个 0.1s 窗**——那是网关的做法，不是本章的。
- **无头/非交互是一句"负向声明"。** ★ **如实标注**：文档里"不新增自主通知循环"这句话，**没有**一条注释**逐字**否定此事；取证只能靠"全仓未见为 API / ACP / one-shot 新增投递循环 + 新增路径仅 CLI / TUI 两条"来**反证**。因此它**不能**给出正向 `文件:行号`——本节按"**负向声明（只有反证）**"记，**不**写成"代码里证明了"。

**② 读 Hermes 的代码**：`tui_gateway/session_notifications.py:663-673`（轮询器"就绪快照"的成因，逐字）：

```
        try:
            evt = queue.get(timeout=0.5)
        except Exception:
            continue
        ready = [evt]
        for _ in range(queue.qsize()):
            try:
                ready.append(queue.get_nowait())
            except Exception:
                break
        handle(ready, None)
```

**要害**：这段钉死了"**就绪快照**"这个词的物理含义——先阻塞 `get(timeout=0.5)` 拿到**至少一个**事件，再 `get_nowait` 把此刻队列里**已经就绪**的其余事件一次掏空成 `ready`。**它没有等待窗口**：掏到什么就是什么，以 `qsize()` 取数时的轮询次数限制读取；并发消费者及新事件可能影响实际快照，它不是严格原子的时间截面。所以"批"的边界就是**这一次快照的边界**，不是"0.1 秒内到齐的"。这既是"无窗口合并"能成立的证据，也是与网关那套（真有一个 0.1s 窗）的分水岭——共享渲染器、不共享排空策略。

**③ mini 的实现（现状）**：mini 侧这**五个消费者一个都不存在**。mini 的汇报面只有两条，且都不是"完成回执"：

- **CLI 唯一汇报口** = `cli.py::_on_text`（`cli.py:152-154`，函数体即 `print(text)`），它由 `run_agent.py::run_conversation` 的**唯一一处**汇报口调用：`if self.text_callback: self.text_callback(final_response)`（`run_agent.py:370-371`）。调用点上方注释逐字："**规矩：成功、失败、被中断、超预算——都从这里出去。**" REPL 是**同步串行**的（`input()` → `run_conversation()`），回合之间没有队列、没有"回合后排出的通知"。
- **网关唯一出站** = `gateway/delivery.py::Delivery.deliver`（`:47-63`）→ `adapter.send()`，两种投递（直接回复 / 显式目标 `platform:chat_id`）。

mini **没有** `cli_process_notifications.py` / `session_notifications.py` / `prompt_turn.py` 的等价物，**没有** `ProcessNotificationBatch`，也**没有"通知回合（notification turn）"这个概念**。所以"五个消费者共享一个渲染器"在 mini 既**没有渲染器**、也**没有消费者**（根因是缺生产者，见 §四③）。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| Classic CLI 排出 | `hermes_cli/cli_process_notifications.py:22-63`，调用点 `cli.py:3467/3603` | ⏭ 不抄（mini 无生产者）：CLI 唯一汇报口 `cli.py:152 _on_text` ← `run_agent.py:370-371`，无回合后排出 |
| TUI/桌面 | `tui_gateway/session_notifications.py:502-544/663-673`（忙会话 requeue 结构化事件） | ⏭ 不抄：mini 无 TUI/桌面 |
| post-turn 安全网 | `tui_gateway/prompt_turn.py:434-452` | ⏭ 不抄：无"通知回合"可兜 |
| 消息网关 | `gateway/run_notifications.py:1420-1448/1527`（**自有**路由键短窗批，本次不替代） | ⏭ 不抄：网关出站只有 `gateway/delivery.py:47-63` |
| 无头/非交互 | 文档负向声明（**只有反证**，无逐字否定） | ⏭ 不抄：无自主通知循环（同理，亦只有反证） |
| 共享渲染器 | `ProcessNotificationBatch` 被 CLI / TUI 共用 | ⏭ 不抄：无批对象可渲 |

---

## §六 本地验证探针：验什么、不验什么

**① Hermes 怎么做的**：验证这件事本身有**边界**，而探针把边界写进了自己的 docstring。`evals/completion_backlog_probe.py` 的用法是 `evals/completion_backlog_probe.py REPO OUTPUT.json`——它拿**真本地 shell 子进程**喂 `ProcessRegistry`（子进程里跑 `sys.exit({7 if index == count - 1 else 0})`，即**最后一个子进程以非零退出码 7 结束**），再用一个 **loopback HTTP turn sink** 替换 `chat` / `_run_prompt_submit`；但**只替换这两处**，docstring 逐字：*"The sink replaces chat/_run_prompt_submit, NOT ownership, consumption, queue draining, batching, or formatting."*。

它**不覆盖**三类东西（docstring 逐字：*"Local I/O probe for completion batching, not a hosted-model or native UI test."*）：

1. **模型推理**（不是 hosted-model 测试）；
2. **原生渲染器的交互**（不是 native UI test）；
3. **托管平台 / ACP 客户端**。

它检查的**五类情形**——探针把三个投递面（`cli` / `poller` / `post-turn`）× 五个场景跑成 **15 次**：

- `backlog`（12 个就绪完成 ⇒ 合并成批）；
- `single`（1 个完成 ⇒ 单条保原文，断言 `single_exact`）；
- `consumed`（先 `wait()` 真消费再排出 ⇒ 抑制）；
- `foreign`（会话键被改成不受本会话拥有 ⇒ 丢弃）；
- `mixed`（批里插入合成 watch 与委派信封 ⇒ 屏障 + 委派各自单独 claim）。

它断言**五条**（在返回的 dict 里）：`wire_turns`（起了几个回合）、`payload_order`（每个负载落在第几个回合，单调不减）、`delegation_delivered_once`（委派恰好投递一次）、`all_payloads_preserved`（每个负载都出现过）、`single_exact`（单条时文本精确相等）。

**测试样本（fixture）与真账本**：合成的 watch 与委派信封是**内联的测试样本**，但**委派认领用的是真临时 SQLite 账本**——`ad._persist_dispatch(delegation)` / `ad._persist_completion(delegation, {...})`（`:109-110`）真写进账本，`claim` 走真事务 `with ad._transaction() as conn:`（`:157-159`）。所以"委派恰好投递一次"是在**真账本**上读出来的，不是内存桩。

**两条"如实标注"的未确认项**（取证笔记明写"不要写成结论"）：

- ★ **合成 watch 与委派信封属于标注的测试样本（labeled fixtures）**。标注可以写在样本文本中，不必另建 label 字段；探针里的 `goal: Fixture delegation` 已表达这种性质。不能把没有独立字段当作未落实。
- ★ **"批用例包含失败与成功同批"：** `sys.exit(7 …)`（**非零退出码**）**逐字存在**（`:90`），但"**失败项与成功项确实落在同一回合**"这一点，测试文件**没有单独断言**——它只断言了 `wire_turns` / `all_payloads_preserved` 等；失败项是否与成功项同批需依赖 probe 的 `texts` 内容判断。**未运行，不下定论**。

**② 读 Hermes 的代码**：`evals/completion_backlog_probe.py:1-6`（模块 docstring，逐字；用法与边界都在这里）：

```
"""Local I/O probe for completion batching, not a hosted-model or native UI test.

Run with the repo's Python: evals/completion_backlog_probe.py REPO OUTPUT.json.
Real shell children feed ProcessRegistry; production CLI/poller/post-turn routing
feeds a loopback HTTP turn sink. The sink replaces chat/_run_prompt_submit, NOT
ownership, consumption, queue draining, batching, or formatting.
"""
```

**要害**：这一段是"验什么、不验什么"的**唯一权威口径**——前半句划出**不验**（hosted-model / native UI），后半句划出**验**（真子进程 + 真路由，只把"模型回合"这一个端点换成 loopback sink），末句再用一个 `NOT` 名单**点名不属于替换范围**的机制（归属 / 消费 / 排空 / 合并 / 渲染照走生产代码）。有这句，读者才知道"探针通过"**不等于**"端到端通过"。

**③ mini 的实现（现状）**：mini **没有**这个探针，也**没有**可被它验的东西（无生产者）。但 mini 有**同一种传统**：真跑脚本放在 `demos/` 下、按章一脚本——第 10 章 `demos/ch10_real_run.py`、第 11 章 `demos/ch11_real_run.py`（真造损坏 + 真跑 + 打印收据，0 失败），另有 `demos/why_*` 系列解释脚本。它们与 `evals/completion_backlog_probe.py` 的**取向一致**：**真跑一个子流程、打印可核对的收据**，而不是"断言某个函数被调过"。

本章**没有新代码**，所以本章的"真跑"不是"跑一个 ch12 脚本"，而是**跑一遍全套件作"未破坏"收据**：**1,309 / 1,305 通过 / 0 失败**（双解释器）。这与第 11 章的"真跑收据"（有 ch11 脚本）不同——第 12 章是**地图 / 约定 + 投递债**，mini 侧的唯一落地是 README 里一张映射表（§七），没有可跑的运行时行为，也就**没有反向验证**（"改回旧写法→测试变红"不成立，因为没有旧写法）。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 验证探针 | `evals/completion_backlog_probe.py`（真子进程 + loopback sink，3 面 × 5 场景 = 15 次） | ⏭ 不抄（mini 无生产者）：无探针 |
| 验什么 | I/O 的合并 / 归属 / 消费 / 排空 / 渲染；**不**验模型推理 / 原生渲染器 / 托管平台 | ——（无可验对象） |
| 测试样本 | 合成 watch / 委派信封（★ "labeled" 源码无独立标签字段）；委派用**真 SQLite 账本** | ⏭ 不抄：无 watch / 委派 / 账本 |
| "真跑"的形式 | 探针跑 15 次 + 断言 5 条 | 本章**无新代码** ⇒ 真跑 = 跑一遍全套件作"未破坏"收据（1,309 / 1,305） |

---

## §七 mini 的实现（现状）：本章唯一落地 + 为什么不做

本章 mini 侧**没有一行新代码** —— 唯一落地是一条**文档动作**（`mini-hermes/README.md` 新增节，提交 `653b627`）；而 B 篇那套机制之所以不做，不是没预算，是 mini 里**连它的生产者都不存在**。

**① 唯一落地：`mini-hermes/README.md` 新增节「改动落在哪、要跑哪些测试（第 12 章落地的『归属图』）」**

真实版 `codebase-ownership`（= 精译-12 第一部分）的三条约定里，第 2 条是"**跑镜像测试目录**"——`tests/` 镜像源码树（`tests/tools/` ↦ `tools/`，`tests/plugins/<type>/` ↦ `plugins/<type>/`）。mini 的 `tests/` 是**平铺**的（11 个 `test_*.py` 全在 `tests/` 一层，无子目录），这条"目录镜像"约定在 mini 落不成形状，只能落成**一张显式映射表**。所以本章在 mini 的落地就是 README 里新增的一节（`mini-hermes/README.md:279`）：**11 行映射表**（`README.md:286-298`，左边"你改了"、右边"必须跑"），表由各测试文件**实际 import 的模块**反推（不是凭印象列的），并含一条硬约定「**新增/删除模块 · 动依赖 · 改模块白名单 ⇒ 永远跑 `tests/test_packaging.py`**」（`README.md:298`，它是"发布前自检"）。

三条约定在 mini 的对应物（`README.md:300-307`）：

| 真实版约定 | mini 的对应物 | 载体 |
|---|---|---|
| ① 改动留在自己的子系统内 | 两道**真实闸门**：`tools/` 下工具靠 AST 自动发现（加工具只需改 `tools/` + `toolsets.py`），而 `toolsets.py:53-70` 的 `TOOLSETS` 是**白名单**——不登记进 toolset 的工具，模型永远看不到它 | `README.md:302-304` |
| ② 跑源码目录对应的镜像测试 | mini 无镜像层次，用上面那张表；拿不准就**全套跑（双解释器）** | `README.md:305` |
| ③ 两子系统都涉及，更窄的那个拥有改动 | 现成例子：`gateway/run_turn.py:75-84`——回合作用域绑在**这条消息所属的** profile 上（更窄处），而不是网关启动时的 `self.home` | `README.md:306-307` |

**顺带修掉的陈旧数字**：README 里"全套件：792 条断言"改成 `README.md:322-323` 的 **1,309 条断言 / 0 失败（3.13.14）· 1,305 / 0 失败（3.9.6）**，并注明差额 4 条的成因（3.9 的 SQLite 拒 `DROP messages_fts_data`，第 8 组那条"真删 FTS 影子表"造不出来）。

**当前载体**：本章最初在 README 登记映射，根 `AGENTS.md` 现也包含对应规则。这里记录历史落地位置，不能再说该映射尚未进入 AGENTS.md。

**② 为什么不做 B 篇：mini 连生产者都没有**

这是本章最诚实、也最有教学价值的一段。完成回执投递的**生产者**，是"一个后台进程跑完了"这个事件；mini 里这个事件**生不出来**：

- **terminal 同步阻塞**。`tools/terminal.py:94-119` 只有一个 `terminal_tool`，体内一句 `subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout, cwd=resolve_context_cwd())`（`:96-106`）。**前台、阻塞、一次性返回**：命令没跑完函数不返回，函数一返回事件就已在**同一个回合**里被消费——不存在"稍后送达"的完成事件可供合并。没有 `background` 参数（schema 只有 `command`/`timeout`，`:77-87`），后端只有 `local` 一种（真实版 7 种）。
- **没有进程注册表**。全仓 `grep process_manage|process_registry` 只命中 `tools/terminal.py:17` **一句 docstring**，而那句所在的两行（`:12-23`）逐字是在明说"**故意不做**"——`tools/terminal.py:20` 逐字："**在只有一种后端的时候做一张'后端表'，就是投机性基础设施**"（`:21-22` 接着说"Hermes 自己把 *Speculative infrastructure* 列在'不要做'的第一条：加钩子容易、等别人依赖上之后再拆就难了"）。
- **顺着往下全无**：没有 `process_manage`（无 wait/log/kill/poll）、没有委派（`delegate_task` 只在 `model_tools.py:232` 的注释里出现）、没有账本（无 `async_delegations` 表）、没有 watch、没有"通知回合（notification turn）"这个概念（`gateway/hooks.py:28-37` 的 `HOOK_EVENTS` 是**生命周期事件**，不是通知投递通道）。

所以 B1~B8 的每一条都撞在同一面墙上：**没有完成事件的生产者，就不可能有合并它的消费者。** 给一个没有后台进程、没有委派、没有 TUI/桌面消费者的仓库抽"完成回执"的表，就是"**加钩子容易、等别人依赖上之后再拆就难**"的现场演示。

mini 自己的纪律也指向同一个结论：

- `tools/terminal.py:20-23`（逐字"投机性基础设施" + "*Speculative infrastructure* 列在'不要做'的第一条"）。
- 根 `AGENTS.md:4`："目标是**搞懂架构决策**（抄的是决策，不是代码量），不是抄代码量"；`:57`："零第三方依赖是**设计选择**，不做 `pip install`"；`:58-59`："测试必须能**反向验证**：把产物改回旧写法，测试要变红"。

**结论：有些章节的正确做法是不做。** 在 mini 里，本章 A 篇是"地图/约定"（无运行时机制），B 篇"无物可投"（无生产者）——两者都没有可实现的机制。更硬的一条：mini 的交付标准是"**真跑 + 反向验证**"，而无生产者 ⇒ **没有旧写法可改回、没有真实完成事件可红 ⇒ 反向验证根本无从做起**。（如实记一条素材与仓库的偏差：规格/清单把"不做投机性基础设施"记成根 `AGENTS.md` 的话，逐字核对后它其实在 `tools/terminal.py:20`；根 `AGENTS.md` 里对应的是 `:4`/`:57` 那两条纪律，`AGENTS.md:20-23` 讲的是"以 git 为准核实真实状态"，不是这句。）

**③ 收据：无新代码 ⇒ 无反向验证；真跑 = 跑一遍全套件**

本章无新代码，故**没有反向验证**（无"改回旧写法"可做）。"真跑"改为**跑一遍全套件**作"未破坏"的收据——本轮实跑（清 `__pycache__` 后，`mini-hermes/` 下，**11 个测试文件 / 双解释器**）：

| 测试文件 | `/usr/local/bin/python3` 3.13.14 | `/usr/bin/python3` 3.9.6 |
|---|---|---|
| `tests/test_compression.py` | 157 通过 / 0 失败（9 组） | 157 / 0（9 组） |
| `tests/test_fallback_and_aux.py` | 113 / 0（4 组） | 113 / 0（4 组） |
| `tests/test_gateway.py` | 266 / 0（21 组） | 266 / 0（21 组） |
| `tests/test_loop_mechanisms.py` | 57 / 0 | 57 / 0 |
| `tests/test_micro_compaction.py` | 119 / 0（10 组） | 119 / 0（10 组） |
| `tests/test_packaging.py` | 18 / 0 | 18 / 0 |
| `tests/test_prompt_assembly.py` | 155 / 0（13 组） | 155 / 0（13 组） |
| `tests/test_provider_runtime.py` | 106 / 0（6 组） | 106 / 0（6 组） |
| `tests/test_session_storage.py` | 108 / 0（14 组） | 108 / 0（14 组） |
| `tests/test_state_db_recovery.py` | **121 / 0（12 组）** | **117 / 0（12 组）** |
| `tests/test_tools_runtime.py` | 89 / 0（10 组） | 89 / 0（10 组） |
| **合计** | **1,309 通过 / 0 失败** | **1,305 通过 / 0 失败** |

差 4 条全部来自 `test_state_db_recovery.py` 第 8 组那条"真删 FTS 影子表"分支（3.13 允许 `DROP messages_fts_data`，3.9/SQLite 3.51 直接拒）。**本章唯一落地（README 文档）不触碰任何 `.py`，套件计数与本条收据完全一致。**

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 本章的性质 | A 篇=归属图的**地图/约定**；B 篇=**投递债**调度纪律 | 同一份地图**已用别的形式存在**（README 章节表 + 各文件 docstring 的"对应 Hermes 的 `<路径>`"注）；投递债**无物可投** |
| 落地物 | `tests/` 镜像源码（4527 文件） | **一条文档**：`README.md:279` 的 11 行映射表（平铺布局的替代） |
| 完成回执生产者 | `tools/process_registry.py`（2376 行，后台进程 + `Popen`） | **无**（`tools/terminal.py:94-119` 同步阻塞；无进程注册表） |
| 反向验证 | 本章真实侧也无独立 commit 圈定边界（见附录 C） | **无反向验证**（无新代码、无生产者）；真跑=全套件 1,309/1,305 |

---

## §八 差距清单（A 批 / B 批）

**口径**：本章 mini 侧定位是提交 `653b627`（本章快照）。真实侧锚 `f5d1926110`。A 批 = 抄（本章**无代码可实现项**）；B 批 = **不抄**，每条给理由。

**开头点明：A 批 = 0 条代码实现项 + 1 条文档级（已做）。** 本章 mini 侧没有一行新代码；唯一落地是 `mini-hermes/README.md` 新增的一节（A1 + A2 同属这一节，`653b627`）。B 批 8 条**全部不抄**，理由统一落在"**mini 无生产者**"（`tools/terminal.py:94-119` 同步阻塞、无进程注册表），逐条再补一条各自的具体缺口。

### A 批（抄：0 条代码 + 1 条文档级，已做）

| # | 事项 | 真实侧判据 | mini 落点 | 状态 |
|---|---|---|---|---|
| **A1** | 在 `mini-hermes/README.md` 加一节「**改动 → 该跑哪些测试**」映射表 | `codebase-ownership.md:8/33`：约定 2「跑镜像测试目录」（`tests/` 镜像源码树） | `README.md:279`（节标题）+ `:286-298`（**11 行映射表**，由各测试文件实际 import 的模块反推） | ✅ 已做（`653b627`） |
| **A2** | 三条约定在 mini 的对应物 | `codebase-ownership.md:8`（地图）+ `:32/33/34`（三条约定） | `README.md:300-307`：① 两道闸门（`tools/` AST 自动发现 + `toolsets.py:53-70` 白名单）· ② 平铺布局只能落成那张表 · ③ 现成例子 `gateway/run_turn.py:75-84`（作用域绑更窄处） | ✅ 已做（`653b627`） |

> 附注：本章历史落地在 README；根 AGENTS.md 当前也有归属映射。下面测试数字为本章交付时的记录，最新数字见 README。

### B 批（不抄，8 条）

| # | 机制 | 真实 `文件:行号` | 不抄理由（统一落在"mini 无生产者"） |
|---|---|---|---|
| **B1** | 共享批渲染器 `ProcessNotificationBatch`（`_live()` 渲染时复查消费；单条保原文、多条加 `[IMPORTANT: N …]` 头） | `tools/process_registry_notifications.py:14-36` | **没有完成事件可渲染**——渲染器的输入是"一批已就绪的 completion 事件"，mini 生不出这种事件（terminal 同步阻塞，事件在同回合被消费） |
| **B2** | 无窗口切批 `group_process_notifications`（只合并连续 completion；watch/委派是屏障） | `tools/process_registry_notifications.py:39-51` | 同上；且 mini **没有 watch/委派**这两种"屏障"概念（`watch_` 零命中、无 `delegate_task`） |
| **B3** | CLI 三件套（idle/post-turn 排出 · 压缩感知归属 · 最终输入解包） | `hermes_cli/cli_process_notifications.py:22-63`；调用点 `cli.py:3467`（idle）/ `cli.py:3603`（post-turn） | mini 的 CLI 唯一汇报口是 `cli.py:152 _on_text` ← `run_agent.py:370-371`，**没有"通知回合"这个入口**；REPL 是同步串行（回合间无队列、无"回合后排出"） |
| **B4** | 消费抑制（三入口 add + 两集合 `_completion_consumed`/`_poll_observed`，drain 与 render 两处抑制） | `tools/process_registry.py:554-563`（两集合）、`:1797`（wait 消费）/ `:1763-1764`（log）/ `:1871-1887`（kill）、`:1538-1547,1601-1604`（两处抑制）；`poll` 故意不消费 `:1726-1731` | 依赖进程注册表；且"**已消费**"的前提是**有人能消费**——mini 没有 `process_manage`（无 wait/log/kill/poll），没有可消费的入口 |
| **B5** | 归属判定（会话键 + `origin_ui_session_id` + 压缩 lineage；unowned 直接丢弃不收养） | `tui_gateway/session_notifications.py:94-108`（正证明）/ `:449-474`（判不过即丢，不收养孤儿）；跨 profile `:80-91` | 需要**多会话/多消费者**模型；mini 已有多会话网关，但没有后台进程通知的共享队列，无 TUI/桌面轮询器，没有"另一个 live session 可能认领我的事件"这种处境 |
| **B6** | 全消费的批不启动回合 | `tools/process_registry_notifications.py:26-27`（`render` 返回 `None`）→ `tui_gateway/session_notifications.py:518-519`（`if text is None: _notif_release_turn(session)`） | 同上：依赖"批 + 消费标记 + 回合占用"三件基座，mini 全无 |
| **B7** | 网关**自有**的路由键短窗批（0.1s 窗） | `gateway/run_notifications.py:1527`（窗口默认值 `0.1`）· `:1420-1448`（`_format_coalesced_process_completions`）· `:1521-1543`（入队）· `:1456-1493`（flush）；路由键 `:70-71` | **抄形状不抄窗**：这是**另一套**机制（带 0.1s 窗，与本章"无窗口"设计相反），上游文档 `:28-30` 明确"本次改动**不**替代它"；mini 网关的通知面只有 `gateway/delivery.py:47-63` 的 `deliver`（无本地短窗批） |
| **B8** | headless/ACP 的自主通知循环 | 文档**负向声明**「this change does not create a new autonomous notification loop…」（`completion-backlog-delivery.md:31-32`）；代码侧**只有反证**，无逐字否定 | 不存在的机制无从抄；全仓 `git grep "autonomous notification loop"` **只命中文档本身**，无从给出正向 `文件:行号`（见附录 C ②） |

---

## 附录 A · 从正文挪过来的引用

> 本附录把 §一~§八 正文放不下、或不必塞进「② 读 Hermes 的代码」单块的 `文件:行号` 集中归档，**按文件分组**。每条给：`文件:行号` + 该行/该段首 20 字 + 一句它证明什么。
>
> - **真实侧**锚 `f5d1926110`：`git -C ~/.hermes/hermes-agent show f5d1926110:<path> | sed -n 'Np'`
> - **mini 侧**锚本章快照（`653b627`）：`sed -n 'Np' mini-hermes/<path>`
> - 全部行号**逐条打印核对过**；括号里的说明只是「它在说什么」，不当逐字引文用。

### 真实侧（锚 `f5d1926110`）

**`tools/process_registry_notifications.py`（411 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:14-36` | `@dataclass(frozen=True` | 共享批渲染器 `ProcessNotificationBatch`：`_live()`（`:20-22`）渲染时复查消费；`render()`（`:24-33`）单条保原文、多条加 `[IMPORTANT: N …]` 头；`display_text()`（`:35-36`）出 UI 短标题。 |
| `:39-51` | `def group_process_notifications` | **无窗口切批**：合并**只在一次函数的连续遍历里**做，无 sleep/定时器；非 completion（watch/委派）是屏障（docstring 逐字 `Group consecutive completions only; watches and delegations are barriers.`）。 |
| `:316-330` | `def process_completion_display_text` | UI 紧凑标题（`N Background Processes Finished` / `Background Process Failed (exit 7): …`）；模型文本保留完整输出。 |
| `:333-344` | `class TimelineNotification(str)` | 队列里的模型文本（字符串兼容）+ `display_text`/`display_kind`——批渲染后包成它作回合输入。 |
| `:384-398` | `if evt_type in ("watch_disabled"` | 事件分流：`async_delegation` → `_format_async_delegation`；`watch_match` 走"匹配输出"分支——watch/委派保持**独立且保序**，不折进 completion 批。 |

**`tools/process_registry.py`（2376 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:546-547` | `self.completion_queue: _queue_mod.Queue = _queue_mod.Queue()` | 完成事件队列定义——生产者与消费者之间的唯一通道。 |
| `:1382-1398` | `if was_running and session.notify_on_complete:` | **生产者**：进程完成入队；事件带 `session_id/session_key/owner_task_id/exit_code/output`；注释逐字 `Stable producer identity across checkpoint recovery`——身份在恢复后仍稳定（"身份保留到 dispatch"的前提）。 |
| `:1538-1547` | `def _drain_should_skip(self, session_id: str, *, skip_poll_observed: bool = True) -> bool:` | **抑制时点①（drain）**：CLI drain 跳过「本回合已消费（wait/log）或已 inline 观察（poll）」的完成。 |
| `:1601-1604` | `if evt.get("type") == "completion" and self._drain_should_skip(` | `drain_notifications` 里上述跳过的调用点。 |
| `:1622-1623` | `if text := format_process_notification(evt):` | 每个事件单独格式化（不预先合并）——切批留给消费者。 |
| `:1726-1731` | `# Read-only: record in _poll_observed` | `poll()` **故意不消费**：只记 `_poll_observed`（仅 CLI 同回合去重），不进 `_completion_consumed`（否则会误杀 watcher 的自主投递回合）。 |
| `:1763-1764` | `if session.exited and observed_completion_output:` | `read_log()` 消费（仅当读到尾部/整段）。 |
| `:1797-1799` | `if session.exited: self._completion_consumed.add(session_id)` | `wait()` 消费。 |
| `:1845-1852` | `def kill_process(` | `kill` 的 `consume_output=True` 默认；docstring 逐字 *"Bulk cleanup passes false so it doesn't suppress an autonomous completion notification"*。 |
| `:1871-1887` | `if consume_output: self._completion_consumed.add(session_id)` | `kill_process` 两处消费 add（含"先捕获输出、标消费、再暴露 `exited`"以关掉延迟通知竞态）。 |
| `:1434-1436` | `def is_completion_consumed(self, session_id: str) -> bool:` | 消费查询接口——`ProcessNotificationBatch._live()` 渲染时复查它。 |
| `:2186-2190` | `# Belt-and-suspenders against module-lifetime growth` | 两集合的清理（会话彻底不再被追踪时才清）——"身份保留"**不是**忘了清。 |
| `:2291-2294` | `"poll": (lambda sid, a: process_registry.poll(sid), True),` | 工具 action → 消费函数的映射表（`poll/log/wait/kill` 四项）。 |

**`tui_gateway/session_notifications.py`（773 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:38-46` | `def _notif_resolve_event_key(evt_key: str, session: dict | None = None) -> str:` | 会话键解析（压缩轮转）：把被压缩父键映射到续接尖端（在 `session` 自己的库里查）。 |
| `:80-91` | `def _notif_other_profile_session_owns(sid: str, session: dict, evt: dict) -> bool:` | 跨 profile 归属补强：另一个 profile 的 live session 有正证明时必须 requeue，**不得被本会话收养**。 |
| `:94-103` | `def _session_owns_notification_event(...)` | fail-closed **正证明**：`origin_ui_session_id == sid`，或 `session_key` 原值/压缩解析后命中本会话当前键；`_finalized` 直接 False。 |
| `:106-108` | `def _notification_event_requires_owner(evt: dict) -> bool:` | 哪些事件**必须**先证明归属才投递：`async_delegation` 或带 `origin_ui_session_id`/`session_key` 者。 |
| `:158-162` | `def _notif_submit(rid: str, sid: str, session: dict, text: str, what: str, **kwargs) -> None:` | 批文本经此 → `_run_prompt_submit` 作**回合输入**（不写系统提示词，见附录 C ③）。 |
| `:449-474` | `def _notif_handle_event(sid, session, evt, emitted, registry, fmt, deferred, …)` | 单事件路由：foreign → requeue 或 deferred；**unowned（addressed 但不可证明）→ 丢弃，绝不收养孤儿**；ours → 至多一次 status.update + 空闲则起回合。 |
| `:475-476` | `if evt_type == "completion" and registry.is_completion_consumed(` | TUI 侧单独判 consumed（不走 `_poll_observed`）。 |
| `:502-530` | `def _notif_dispatch_completions(sid, session, notifications, registry, deferred):` | 占用会话即起一个回合；忙则把**结构化事件**放回 `completion_queue`（`:508-513`，**不是**渲染后的批字符串）；`text is None` → 释放回合、**不起回合**（`:518-519`）；拿到 turn 才 `batch.render()`（`:517`）。 |
| `:533-544` | `def _notif_handle_ready(sid, session, events, emitted, registry, fmt, deferred, *, owned=False):` | 就绪快照分组：遇任何非 completion 前先 **flush** 已积累的 completions（保证原序）。 |
| `:663-673` | `evt = queue.get(timeout=0.5)` | 轮询器：一次 `get` 后掏空队列成 `ready` 快照（= "已就绪"的定义）。 |
| `:760-768` | `def _prepend_note(run_message: Any, note: str) -> Any:` | 逐回合 note 前缀到**模型输入**（`leaving the prompt alone` … `the cached prefix survives`）——系统提示词字节稳定不变式的一处文字凭证。 |

**`hermes_cli/cli_process_notifications.py`（64 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:5-20` | `def _owns_process_notification(self, event: dict) -> bool:` | CLI 的**压缩感知归属**：`session_key` 与 `self.session_id` 比，不等则用 `resolve_resume_session_id` 解析续接键再比，缺键 fail-closed。 |
| `:22-43` | `def _drain_process_notifications(self, consumer: str) -> None:` | CLI idle/post-turn 排出：`drain_notifications(owns_event=…)` → `claim_event_delivery` → `group_process_notifications` → 结构体放 `_pending_input`。 |
| `:45-63` | `def _tui_unwrap_input(self, user_input):` | **最终输入解包**：`ProcessNotificationBatch` 在取出时才 `render()`（`:51-54` 是渲染点）——"身份保留到 dispatch"。 |

**CLI / 网关 / 提示词 / 测试**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `cli.py:3467` | `lambda: self._drain_process_notifications("cli-idle"),` | idle drain 调用点（`_tui_idle_tick` 的 housekeeping 步骤）。 |
| `cli.py:3603` | `self._drain_process_notifications("cli-post-turn")` | post-turn drain 调用点。 |
| `cli.py:3477-3479` | `user_input, is_voice_input, is_seeded_query = self._tui_unwrap_input(user_input)` / `if not user_input: return` | 批在 REPL 取出时解包；`rendered` 为 `None` ⇒ 输入被丢弃（全消费的批不产生回合）。 |
| `tui_gateway/prompt_turn.py:434-452` | `# Safety net for completion events that arrived mid-turn.` | post-turn 安全网：同一 `drain_notifications`（`skip_poll_observed=False`）+ `_notif_handle_ready(..., owned=True)`；不可认领者 requeue 给轮询器。 |
| `tui_gateway/prompt_turn.py:541-542` | `# Per-turn API-message notes: barge mid-speech, reactions, HUD surface` | 不变式：逐回合状态**不得**触碰字节稳定的系统提示词（附录 C ③）。 |
| `agent/prompt_builder.py:591` | `model-bound message, never the byte-stable system prompt.` | 同口径：逐回合事实走模型绑定消息，**绝不**走字节稳定系统提示词。 |
| `agent/AGENTS.md:125` | `message-shape invariants (alternation, byte-stable system prompt)` | 仓库级纪律：断言消息形状不变式（而非快照提示词文本）。 |
| `gateway/run_notifications.py:1527/1420-1448/1521-1543/1456-1493/70-71` | 见 §八 B7 行 | 网关**自有**的路由键短窗批（0.1s 窗）——"抄形状不抄窗"。 |
| `evals/completion_backlog_probe.py`（探针） | `"""Local I/O probe for completion batching, not a hosted-model or native UI test.` | 本地验证探针：真起 shell 子进程 → 驱动 CLI/TUI/post-turn 三条路 → loopback HTTP turn sink 替换 `chat`/`_run_prompt_submit`（不验模型推理）。 |
| `evals/completion_backlog_probe.py:85-115` | `count = 1 if scenario == 'single' else 12` | fixture：`single`=1 / 其余=12 个子进程（`sys.exit(7 if index == count - 1 else 0)`，**批含非零退出码**）；`mixed` 注入硬编码 watch + 真 SQLite 委派。 |
| `tests/hermes_cli/test_completion_backlog.py:5-24` | `def test_ready_completions_share_one_turn_across_interactive_routes(tmp_path):` | 5 条断言逐字：`wire_turns`（mixed=5 / 其余=1）、`payload_order` 单调、`delegation_delivered_once`、`all_payloads_preserved`、`single_exact`；另 `test_consumed_or_foreign_completions_never_start_a_turn`（`wire_turns == 0`）。**逐字读源确认、未运行**（probe 会 spawn 真子进程 + 起 loopback HTTP）。 |
| `website/docs/developer-guide/completion-backlog-delivery.md:1-52` | 上游文档（52 行，H1 + 引子 + 2 个 H2） | 本章 B 篇的原文。 |
| `website/docs/developer-guide/codebase-ownership.md:1-34` | 上游文档（34 行，H1 + 17 行表 + 3 条约定） | 本章 A 篇的原文。 |

### mini 侧（锚本章快照 `653b627`）

**`README.md`（620 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:279` | `## 改动落在哪、要跑哪些测试（第 12 章落地的「归属图」）` | 本章唯一落地的节标题。 |
| `:281-284` | `真实版有一篇 codebase-ownership（= 《中文精译-12》第一部分）：…` | 立论：mini `tests/` 平铺 ⇒ 镜像约定只能落成一张显式映射表。 |
| `:286-298` | `\| 你改了 \| 必须跑 \|` | **11 行映射表**（改哪 → 跑哪个 `test_*.py`），含"新增/删除模块 · 动依赖 ⇒ 永远跑 `test_packaging.py`"。 |
| `:300-307` | `三条从归属图带出来的约定，在 mini 里的对应物：` | 三条约定 → mini 对应物（两道闸门 / 那张表 / `run_turn.py:75-84`）。 |
| `:322-323` | `# 全套件：1,309 条断言 / 0 失败（3.13.14）· 1,305 / 0 失败（3.9.6）` | 顺带修掉的陈旧数字（792 → 1,309/1,305）与差额成因。 |

**核心源文件（本章"无生产者"的证据）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `tools/terminal.py:12-23` | `## 关于第 8 项改造（后端表）：故意不做` | docstring 逐字"故意不做"——mini 无进程注册表/后端表的**纪律来源**。 |
| `tools/terminal.py:17-18` | `真正的复杂度在 Hermes 那两个 process_registry 文件里（多父 agent 同时起进程 +` | 全仓唯一出现 `process_registry` 一词的地方，且在明说"不做"。 |
| `tools/terminal.py:94-119` | `def terminal_tool(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:` | 同步阻塞的 terminal（`subprocess.run`）——**无完成事件生产者**。 |
| `toolsets.py:14-16` | `所以"加一个工具"**永远是两个文件**：` | "改动留在子系统内"的闸门注释。 |
| `toolsets.py:53-70` | `TOOLSETS: Dict[str, Dict[str, Any]] = {` | `TOOLSETS` 白名单（4 toolset / 7 工具）——约定①的对应物。 |
| `cli.py:152-154` | `def _on_text(self, text: str) -> None:` | CLI 唯一汇报出口（无"通知回合"入口）。 |
| `run_agent.py:370-371` | `if self.text_callback:` / `self.text_callback(final_response)` | 唯一汇报口的调用点。 |
| `gateway/run_turn.py:75-84` | `# ★ 作用域绑定：整个回合…都在**这条消息所属的** profile 上跑。` | 约定③（更窄者拥有改动）的现成例子。 |
| `gateway/delivery.py:47-63` | `def deliver(self, source: SessionSource, text: str,` | 网关唯一出站——没有本地短窗批的对应面。 |
| `model_tools.py:232` | `# 对应 Hermes 的 _AGENT_LOOP_TOOLS = {…"delegate_task"}` | 委派只在注释里出现（mini 无 `delegate_task`）。 |
| `gateway/hooks.py:28-37` | `HOOK_EVENTS` | 网关事件表是**生命周期事件**，不是通知投递通道。 |

---

## 附录 B · 三方对照矩阵

> 列：`机制 | 真实 Hermes（文件:行号）| mini | 精译-12 节位`。真实侧锚 `f5d1926110`，mini 侧锚 `653b627`；`——` 表示 mini 无对应实现（本章 B 批统一不抄）。

| 机制 | 真实 Hermes（文件:行号） | mini | 精译-12 节位 |
|---|---|---|---|
| **① 归属图（子系统 ↔ 源码目录 ↔ 文档入口）** | `codebase-ownership.md:10-28`（H1 + 17 行表） | 同职能已用别的形式存在：`README.md:10-22`（章节表）+ 各源文件 docstring 的"对应 Hermes 的 `<路径>`"注 | §0 / 第一部分 |
| **② 约定①：改动留在子系统内** | `codebase-ownership.md:32`（rubric 在仓库 `AGENTS.md`） | `tools/` AST 自动发现 + `toolsets.py:53-70` 白名单（`toolsets.py:14-16` 注释）| 第一部分 |
| **③ 约定②：跑镜像测试目录** | `codebase-ownership.md:8/33`（`tests/` 镜像源码） | mini 平铺：**`README.md:286-298` 的 11 行显式映射表**（目录镜像落不成，改落成表）| 第一部分 |
| **④ 约定③：两子系统时更窄者拥有改动** | `codebase-ownership.md:34` | `gateway/run_turn.py:75-84`（回合作用域绑本消息所属 profile）| 第一部分 |
| **⑤ 完成事件入队（生产者）** | `tools/process_registry.py:1382-1398`（`notify_on_complete` 压队）；队列 `:546-547` | ——（无生产者：`tools/terminal.py:94-119` 同步阻塞）| 第二部分 |
| **⑥ 共享批渲染器 `ProcessNotificationBatch`** | `tools/process_registry_notifications.py:14-36`（`_live` `:20-22`；`render` `:24-33`）；UI 标题 `:316-330` | ——（B1 不抄：没有完成事件可渲染）| 第二部分 |
| **⑦ 无窗口切批（连续 completion；watch/委派为屏障）** | `tools/process_registry_notifications.py:39-51` | ——（B2 不抄：同上，且无 watch/委派两种屏障）| 第二部分 |
| **⑧ CLI drain 三件套（idle/post-turn 排出 · 压缩感知归属 · 最终解包）** | `hermes_cli/cli_process_notifications.py:22-63`（归属 `:5-20`）；调用点 `cli.py:3467/3603` | ——（B3 不抄：CLI 唯一汇报口 `cli.py:152 _on_text` ← `run_agent.py:370-371`，无"通知回合"入口）| 消费者与归属 |
| **⑨ TUI/桌面轮询器：就绪快照分组** | `tui_gateway/session_notifications.py:663-673`（快照）+ `:533-544`（分组）| ——（B 批不抄：mini 无 TUI/桌面）| 消费者与归属 |
| **⑩ post-turn TUI 安全网** | `tui_gateway/prompt_turn.py:434-452` | ——（同⑨）| 消费者与归属 |
| **⑪ 归属判定（会话键 + `origin_ui_session_id` + 压缩 lineage；unowned 丢弃不收养）** | `tui_gateway/session_notifications.py:94-108/106-108/449-474`；跨 profile `:80-91`；键解析 `:38-46` | ——（B5 不抄：需多会话多消费者；mini 已有多会话网关，但没有后台进程通知的共享队列）| 消费者与归属 |
| **⑫ 消费抑制（两集合 + 三入口 + 两时点）** | 两集合 `tools/process_registry.py:554-563`；wait `:1797`·log `:1763-1764`·kill `:1871-1887`；drain 跳过 `:1538-1547/1601-1604`；render 复查 `process_registry_notifications.py:20-22`；`poll` 不消费 `:1726-1731`；清理 `:2186-2190` | ——（B4 不抄：无进程注册表、无 `process_manage`，"已消费"无人可消费）| 第二部分 |
| **⑬ 全消费的批不启动回合** | `process_registry_notifications.py:26-27` + `session_notifications.py:518-519` | ——（B6 不抄：同⑫）| 第二部分 |
| **⑭ 网关自有路由键短窗批（0.1s 窗，本次**不替代**）** | `gateway/run_notifications.py:1527/70-71/1420-1448/1521-1543/1456-1493` | ——（B7 **抄形状不抄窗**：另一套机制；mini 出站唯一 `gateway/delivery.py:47-63`）| 消费者与归属 / 第二部分 |
| **⑮ 非交互/headless 消费者不建循环（负向声明）** | `completion-backlog-delivery.md:31-32`（无正向代码）| ——（B8 不抄：不存在的机制无从抄）| 第二部分 |
| **⑯ 本地验证探针（真子进程 + loopback HTTP）** | `evals/completion_backlog_probe.py`（`:85-115` fixture）+ `tests/hermes_cli/test_completion_backlog.py:5-24`（5 条断言）| ——（无消费者可验；本章 mini 真跑 = 全套件 1,309/1,305）| 本地验证及其边界 |

---

## 附录 C · 上游文档与源码的出入

> 核对锚：上游 commit `f5d1926110`（v0.21.3）。本页三条均已在本轮**逐条 `git show f5d1926110:<path> | sed -n` 真身打印**确认行号与文字相符。
>
> **结论：三条均为「澄清 / 边界」，不是出入——未发现真出入。** 逐条理由如下。

**1. 网关那套"路由键短窗批"是它**自有**的另一套机制，上游文档已**自行界定**，不是出入。**

```console
$ git -C ~/.hermes/hermes-agent show f5d1926110:gateway/run_notifications.py | sed -n '1420p'
    def _format_coalesced_process_completions(entries: list[tuple[str, dict, asyncio.Future]]) -> str:
$ git -C ~/.hermes/hermes-agent show f5d1926110:website/docs/developer-guide/completion-backlog-delivery.md | sed -n '28,30p'
- **Messaging gateway:** `gateway/run_notifications.py` already uses its own
  route-keyed short-window batching. This interactive-backlog change does not
  replace that mechanism or alter adapter sends.
```

- 原文逐字（`:28-30`）：*"`gateway/run_notifications.py` **already uses its own** route-keyed short-window batching. This interactive-backlog change **does not replace** that mechanism…"*
- 源码侧确实有一套**同类但独立**的合并：`gateway/run_notifications.py:1527`（`_completion_notification_batch_window`，默认 **0.1s**）、`:1420-1448`（`_format_coalesced_process_completions`）、`:1521-1543`（入队）、`:1456-1493`（flush）、`:70-71`（路由键字段）。
- **澄清（非出入）**：原文自己就把它和本章的"无窗口 `group_process_notifications`"**分开讲明了**（"another mechanism / does not replace"）。真正的坑只是读者容易把两套 batching 混为一谈——**章节正文已标为"抄形状不抄窗"**，此处只记边界，不记出入。

**2. "非交互消费者不建循环"只有**反证**（无逐字代码否定）⇒ 如实标为"无法从源码证伪的散文断言"。**

```console
$ git -C ~/.hermes/hermes-agent show f5d1926110:website/docs/developer-guide/completion-backlog-delivery.md | sed -n '31,32p'
- **Noninteractive/headless consumers:** this change does not create a new
  autonomous notification loop for an API request, ACP client, or one-shot CLI.
$ git -C ~/.hermes/hermes-agent grep -n -i "autonomous notification loop" f5d1926110
f5d1926110:website/docs/developer-guide/completion-backlog-delivery.md:32:  autonomous notification loop for an API request, ACP client, or one-shot CLI.
```

- 原文是一句**负向声明**（*"does not create a new autonomous notification loop…"*），**没有**对应新代码、也**没有**一条注释逐字否定此事。全仓 grep 该短语**只命中文档本身**（`…delivery.md:32`），**零命中**在任何 `.py`。
- 能给出的只有**反证**：本轮全仓范围核对——为 API 请求 / ACP 客户端 / one-shot CLI 新增的投递循环**未见**；本章相关的新增路径只有**已有的两条**（CLI drain 与 TUI poller）。
- **结论**：这是一条"**无法从源码证伪的散文断言**"——方向可信（无正向证据矛盾），但**不能**给出 `文件:行号`。如实标注，不写成源码结论。

**3. "批不落系统提示词"只有**不变式层 + 路径层**证据，没有一句原地逐字声明 ⇒ 同样如实标注。**

```console
$ git -C ~/.hermes/hermes-agent show f5d1926110:website/docs/developer-guide/completion-backlog-delivery.md | sed -n '35p'
Neither a batch nor its delivery status is persisted into the system prompt.
$ git -C ~/.hermes/hermes-agent show f5d1926110:tui_gateway/prompt_turn.py | sed -n '541,542p'
    # Per-turn API-message notes: barge mid-speech, reactions, HUD surface (per-turn state
    # that must not touch the byte-stable system prompt).
$ git -C ~/.hermes/hermes-agent show f5d1926110:tui_gateway/session_notifications.py | sed -n '760,763p'
def _prepend_note(run_message: Any, note: str) -> Any:
    """Prefix a per-turn note onto the MODEL INPUT, leaving the prompt alone: everything the model must know that the
    user did not type (interrupted reply, reactions, surface) arrives this way, so no scaffolding reaches the
    transcript and no sent message is rewritten — the cached prefix survives."""
$ git -C ~/.hermes/hermes-agent show f5d1926110:agent/prompt_builder.py | sed -n '591p'
    model-bound message, never the byte-stable system prompt. Each sentence is gated on the tool it names (an
$ git -C ~/.hermes/hermes-agent show f5d1926110:agent/AGENTS.md | sed -n '125p'
message-shape invariants (alternation, byte-stable system prompt) rather than snapshotting prompt
```

- 原文逐字（`:35`）：*"Neither a batch nor its delivery status is persisted into the system prompt."*
- **不变式层**（四处文字凭证，口径完全一致："逐回合事实一律走**模型可见输入**，系统提示词必须**字节稳定**"）：`tui_gateway/prompt_turn.py:541-542`、`tui_gateway/session_notifications.py:762-763`、`agent/prompt_builder.py:591`、`agent/AGENTS.md:125`。
- **路径层**：批文本作为**回合输入**进入模型——CLI 经 `_pending_input` → `cli.py:3477`（`_tui_unwrap_input`）→ chat；TUI 经 `session_notifications.py:158-162`（`_notif_submit` → `_run_prompt_submit`）。它只进对话历史/用户回合或走 `TimelineNotification` display，**路径上不经过系统提示词**。
- **缺一条原地引文**：没有任何一处注释**逐字**写"the batch is not persisted into the system prompt"。结论方向明确（不变式 + 路径双重指向），但**缺一条原地声明** ⇒ 如实标注为"**方向明确、缺原地引文的散文断言**"，不写成源码结论。

**4. 其余核对**：本轮为写本章共 `sed -n` 真身打印了 §七/§八/附录 A/B 涉及的全部真实侧与 mini 侧引用（清单见附录 A），**未发现新的上游文档与源码出入**。
