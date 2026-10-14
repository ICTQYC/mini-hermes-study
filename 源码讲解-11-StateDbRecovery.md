# 源码讲解 · 第 11 章：状态库与 FTS 恢复（State DB Recovery）

> **本篇定位**：源码讲解为主（看懂 `state.db` 坏掉时 Hermes **具体怎么做**，再对照 mini 把它做出来）。它是第 6 章《会话存储》的**配套篇** —— 同一份库的故障表现与自愈路径；逐句翻译见《中文精译-11》。
> **来源**：`官方文档-Developer-Guide/state-db-recovery.md`（**107 行** · H1 + 3 个 H2）。
> **上游**：commit `f5d1926110` · `version = "0.21.3"` —— 真实侧行号一律锚它。
> **mini 侧**：行号锚本章快照 **`d269953`**；mini 现状按四档口径写（✅ 已对齐 / ⬜ 没做（量级）/ ⏭ 不抄（理由））。

[TOC]

## 发布前维护 · 版本拒绝与 LIKE 回退

未来 schema 版本在只读探测阶段被拒绝，发生在可写连接、WAL 和建表之前。FTS 降级后的 LIKE 搜索按真实 SQL 占位符顺序绑定定位词与过滤参数。另须区分显式 WAL checkpoint 与 SQLite 最后连接关闭时的自动 checkpoint；mini 尚未关闭后者，正文将其列为保护差距。

这些是当前实现的行为；下文的章节提交、规模表和实测输出保留各自历史时点。新增回归在 `mini-hermes/tests/test_publish_regressions.py`，完整收据见[发布前修复记录](发布前修复记录.md)。



---

## 导言 · 机制总览：Hermes 具体怎么做的（先读这一节）

`state.db` 会坏。坏法只有两类，而且它们**要用两种完全不同的方式处置**：

- **派生索引坏了**（`messages_fts*` 这些 FTS5 影子表，以及它们的同步触发器）—— 用户还能写、还能搜，只是搜索退化成扫表；坏的东西可以**摘掉、以后再建**。
- **文件本身坏了**（权威的 `sessions`/`messages` B 树、schema、空闲页链表）—— 再写下去只会把"还能读"的文件写成"打不开"；必须**立刻停写、隔离这个句柄**。

本章就是这两类坏法的处置链。下面七小节按「谁在管 / 什么时候 / 做什么 / 什么不能破 / 出错怎么办 / 跨了哪几层 / 一次损坏的时间线」把它拆开；每条都落到真实源码的 `文件:行号` 与**逐字注释/docstring**（锚 `f5d1926110`，v0.21.3）。

### 0.1 谁在管（哪个模块/哪个函数是入口）

主体是**门面类 `SessionDB`**（`hermes_state.py`，1674 行），但它把这件事拆给一组 sibling 模块，各管一段：

| 管什么 | 入口 |
|---|---|
| 写入环与重试 | `hermes_state.py::_execute_write`（重试环 `:941`，损坏分支 `:1020-1035`） |
| FTS 归因 → 摘索引（fail-open） | `hermes_state_fts.py::_enter_fts_fail_open`（`:353`） |
| 错误分类谓词 | `hermes_state_errors.py::is_fts_scoped_corruption_error`（`:100`）/ `classify_persistence_error`（`:236`） |
| 结构损坏隔离 | `hermes_state.py::_halt_db_corrupt`（`:1249`） |
| 开库受守卫重建 | `hermes_state_schema.py::_recover_stale_fts`（`:545`） |
| 标记与跨进程准入锁 | `hermes_state_common.py`（`FTS_STALE_KEY` `:880` / `fts_rebuild_admission` `:1168`） |

真正的**入口函数只有两个**：写路径的 `_execute_write`（所有写入都经它，也正因此"摘索引继续写"只能在这里接线），和恢复路径的 `_recover_stale_fts`（只有"开库"或"网关 tick"会调它）。

逐字引（`hermes_state_fts.py:28-31`，模块注释的 "Split DDL" 那一段）：

> ```
> # Split DDL: the table/view is safe to ensure any time; triggers are created ONLY
> # while the index is complete-or-marker-gated. A stale index must keep its
> # triggers DROPPED — an external-content 'delete' for a rowid the index never
> # held is the canonical FTS5 corruption hazard.
> ```

**要害**：这段注释钉死了"谁在管"—— 管 FTS 的其实是**触发器**（DDL 的第二种），不是索引本身。触发器只在索引"完整、或已被标记门控"时才允许存在；索引一脏，触发器必须先摘。所以"谁在管"的答案不是"某个新子系统"，而是**写入环**：它是唯一会让一条 `messages` 写入顺带写进 FTS 的地方，也是唯一能"把 FTS 从这次写入的路径上摘掉"的地方。

### 0.2 什么时候（什么错被当成损坏）

分岔口是**分类谓词**，判据只有一句："SQLite 有没有把这次损坏**归到 FTS 对象**上？"

- **有 `sqlite_errorcode` 时，码优先**：只认 `SQLITE_CORRUPT_VTAB`（267）为 FTS 范围；裸 `SQLITE_CORRUPT` / `SQLITE_NOTADB` 没有对象归属 ⇒ 归"整文件损坏"。
- **无码时**（Python < 3.11、或被 RPC 包过的字符串）才让**文本**说话，且必须命中 `messages_fts*` 对象名 **且**（`fts5:` 且含 `corrupt`）或损坏词表。

逐字引（`hermes_state_errors.py:105-110`，`is_fts_scoped_corruption_error` 的 docstring）：

> ```
> A known result code outranks prose: ``SQLITE_CORRUPT_VTAB`` is FTS-scoped even with
> the generic malformed-image text older SQLite builds emit, while bare ``SQLITE_CORRUPT``
> / ``SQLITE_NOTADB`` carry no object scope and any other known code contradicts
> FTS-looking prose, so both fail closed. Only without a code (Python < 3.11, RPC-wrapped
> strings) does the text decide, and then only an ``fts5:`` corruption report or a
> corruption marker that names a ``messages_fts*`` object counts.
> ```

**要害**："码优先"不是洁癖。老 SQLite 会把 FTS 范围的 `SQLITE_CORRUPT_VTAB` 也报成那句通用 malformed 文本；只按文本判，一个**其实健康的库**会被当成整文件损坏而隔离。反过来，裸码**必须 fail closed 当结构损坏**（`both fail closed`）：认不出范围时，宁可停写，不许猜着继续写。

### 0.3 做什么（两条自愈路径：摘索引继续写 / 隔离停写）

现场**只有两条路**，由 0.2 的分岔口选：

| 路径 | 触发 | 动作 |
|---|---|---|
| **摘索引继续写**（fail-open） | FTS 归因损坏 | 在一个事务里落 `fts_stale` 标记 + `DROP TRIGGER`，然后**重跑**那次写入 |
| **隔离停写**（quarantine） | 结构损坏 | 置粘性标志、不重试、拒绝重开、关停不 checkpoint |

逐字引（`hermes_state_fts.py:354-356`，`_enter_fts_fail_open` 的 docstring）：

> ```
> Detach corrupt FTS indexes so canonical writes can continue. Breadcrumb +
> trigger drop commit atomically: once triggers are absent the index has a
> gap of unknown extent, so nobody may reinstall them without a full rebuild.
> ```

**要害**："同一个事务"指的是 fail-open **自己开的那个** `BEGIN IMMEDIATE`（`hermes_state_fts.py:363`）—— 不是那次失败的写入事务，后者早被 `_execute_write` 的 rollback 放弃了（`hermes_state.py:963-969`）。原子性是全部要点：标记（breadcrumb）与摘除要么一起生效、要么一起不生效；崩在中间只会更保守（重启后仍认为"摘除中"）。落标记与摘除收尾在同一处（`:382-383`：`_drop_all_fts_triggers(...)` 紧跟 `commit()`）。

### 0.4 不变式（派生索引可摘而权威不可损）

三条铁律，破哪条都会把库写坏：

1. **派生索引可以摘**（`messages_fts*` + 同步触发器）—— 它是能从 `messages` 全量重建的副本。
2. **权威不可损**（`sessions` / `messages` 的行）—— 它是唯一真相；任何"为了让派生错误消失"的操作都不许删它。
3. **活路径绝不 rebuild** —— 一次 `FTS5('rebuild')` 在 25GB 库上实测持写锁约 16 分钟，且与并发写交错会踩 FTS5 external-content 的经典损坏点。

逐字引（`hermes_state.py:1028-1029`，写入环里的一句注释）：

> ```
> # Corrupt FTS shadow tables fail every write via the sync triggers while canonical
> # rows are intact: detach the derived indexes atomically and retry (never rebuild here).
> ```

**要害**：这一句同时说清两件事 —— `canonical rows are intact`（权威没坏）是"**摘**"的前提，`never rebuild here`（活路径不重建）是"**不许**把一次用户写入变成无上界重建"的红线。合起来就是本节的题面：**派生可摘、权威不可损、重建只属于恢复路径**。

至于"为什么权威非保不可"—— 现场教训逐字写在 `hermes_state_errors.py:189-191`：*a handle that kept writing after the first error / checkpointed 15 pages under wrong page numbers and turned a readable file / into "file is not a database"*（一个在首次结构错误后**还继续写了约 50 分钟**的句柄，在关停 checkpoint 时把 15 页写到**错误的页号**，把一个"还能读"的文件写成了根本打不开）。

### 0.5 出错（重建失败 / 分类认不出 / 修复失败）

三种出错，各有各的收场，但都不许让权威数据更坏：

- **受守卫重建跑不了**（外部持有者仍在 / 准入锁没抢到）⇒ **fail closed，保持摘除**。逐字引（`hermes_state_schema.py:546-548`）：
  > ```
  > Fails closed: holders or a lost admission race leave the breadcrumb set.
  > ```
  标记**不删**、FTS 保持摘除、权威写入照常、搜索走 LIKE；日志逐字 `canonical writes and LIKE search remain available.`（`:553-556`），等下一次机会（下次开库，或长命网关的 tick）。
- **分类认不出**（既不是 FTS 归因、也进不了 `corrupt` 桶）⇒ 谓词**不猜**：`_enter_fts_fail_open` 先过门禁（`hermes_state_fts.py:353-356`），不是 FTS 归因就 `False` ⇒ 不摘；`_is_structural_corruption_error`（`hermes_state.py:1235-1243`）不为真 ⇒ 不隔离，**原样抛**。两个谓词**互斥**，分类顺序错了就整条走错路。
- **修复失败** ⇒ 保留活库**与**报告出来的备份；**永远不**为了甩掉一个派生索引的错误而删权威行。文档 §3 逐字：*"If the repair fails, keep the live DB and the reported backup; never delete canonical rows to get rid of a derived-index error."*

**要害**：三条都是同一个方向 —— **塌向安全的那一边**。重建失败 = 继续降级（不冒险修）；认不出 = 不动手（不猜）；修不好 = 留证据（不销毁）。

### 0.6 跨层（与会话库、网关 flush、doctor、CLI 的关系）

降级/隔离不只是 `SessionDB` 内部的事，四条线都要跟着变：

- **网关 / agent flush 路径**：把隔离当成"文件被替换"处理 —— 待落盘的转写本改去 `sessions/<id>.jsonl` 与 `pending_messages/` 暂存池，**不重试**、**绝不**在受损文件上跑 FTS 重建。逐字引（`gateway/session_transcript.py:142-143`）：
  > ```
  > Stop SQLite writes on a replaced/quarantined handle and divert the backlog to the on-disk
  > spool + JSONL fallback: retrying cannot succeed and the FTS rebuild must never run here.
  > ```
- **doctor**：把"降级中 / 损坏中"变成**可见**，并报出显式修复命令 —— FTS 那类逐字 `run 'hermes doctor --fix' (or 'hermes sessions repair') to rebuild the FTS index`（`hermes_cli/doctor_state.py:200`）；结构性损坏那类**不给 --fix**，只叫人跑 `sessions recover`（`:206-212`，逐字含 `an FTS rebuild cannot repair it`）。
- **CLI**：`hermes sessions repair`（`hermes_cli/subcommands/sessions.py:160-168`：`--check-only` / `--no-backup`）与 `hermes sessions recover`（`:186-197`：`--source` / `--output` / `--inspect-only`）。
- **长命网关**：`SessionDB` 开一次库开几天，"下次打开"永远不来，所以另有 housekeeping tick 上的 `retry_deferred_fts_recovery` 兜底（`hermes_state_schema.py:561-566`，逐字含 `a gateway opens state.db once for days, so "next open" never comes`）。

**要害**：跨层的分界是"**谁拥有重建权**"。活路径（写、搜）**永不自建**；重建只发生在"有准入权的恢复窗口"—— 开库、网关 tick、或显式 CLI。四层都守这一条，才有"降级 ≠ 雪崩"。

### 0.7 时间线（一次损坏从发生到恢复）

1. 一条 canonical 写入经触发器触达损坏的 `messages_fts*` ⇒ **整条语句**抛 `DatabaseError`（`hermes_state.py:1020`）。
2. `_execute_write` 环里的分岔口（`:1023-1035`）：先查"文件是否被换掉"，再试 `_enter_fts_fail_open`。
3. FTS 归因 ⇒ `_enter_fts_fail_open` 起**独立事务**：`BEGIN IMMEDIATE`（`hermes_state_fts.py:363`）→ 落 `fts_stale='1'` → `DROP TRIGGER`（base+trigram+CJK）→ `commit`（`:382-383`）。
4. 回到环顶 `continue` ⇒ **同一连接重跑同一个 `fn`**（`hermes_state.py:1030-1031`）；此时触发器已摘，写不再触达坏影子表 ⇒ **权威写入成功**。
5. 搜索侧：读 `state_meta` 观测到 stale ⇒ 走 LIKE 扫权威表，路径名 `like_scan_fts_stale`（`hermes_state_search.py:1084-1087`、`:883-885`）。
6. 重启 / 开库：读到标记 ⇒ **保持摘除**（`hermes_state_schema.py:958-963`）⇒ 在**外部持有者守卫 + 跨进程准入锁**下受守卫重建（`:545-559`）；重建不了就 fail closed，留着标记。
7. 长命网关不重开库 ⇒ housekeeping tick 每 60s 的非阻塞探测重试（`gateway/run.py:4650`），真正节流是内部退避 60s→×2→3600s（`hermes_state_schema.py:41-44`）。
8. 显式修复：`hermes sessions repair` **默认先 raw-copy 备份**，再重建 + 三段验证（标记零行 / 触发器在场 / 权威行数不降）。

逐字引（`hermes_state_schema.py:41-44`，退避节奏注释）：

> ```
> # retry_deferred_fts_recovery cadence: startup paid the full admission wait once; later
> # retries are non-blocking probes whose spacing doubles up to the cap.
> _FTS_STALE_RETRY_SECONDS = 60.0
> _FTS_STALE_RETRY_MAX_SECONDS = 3600.0
> ```

**要害**：整条时间线里唯一"不得不往前走"的是**重建归属**—— fail-open 只是**推迟**重建（摘干净、留标记），真正的 `rebuild` 必须等一个"独占窗口"。所以"损坏发生了"和"数据丢了"是两件事：前者在第 3 步就结束了，后者只在有人违背 0.4 的不变式时才会发生。

### 验收四问

1. **一次 FTS 损坏会不会让权威写入失败？** 不会。fail-open 在同一事务里摘掉触发器后 `continue` 重跑同一个 `fn`，权威写入照样成功（`hermes_state_fts.py:354-356` · `hermes_state.py:1030-1031`）。（§0.3 · §二）
2. **活路径会不会偷偷跑一次全量 rebuild？** 不会。三处注释钉死"never rebuild here" —— 写路径（`hermes_state.py:1028-1029`）、搜索（`hermes_state_search.py:917-920`）、fail-open（`hermes_state_fts.py:354-356`）；`rebuild` 只出现在受守卫恢复路径（`hermes_state_schema.py:644`）。（§0.4 · §三）
3. **结构损坏之后，还会不会继续往坏文件里写？** 不会。粘性标志（`hermes_state.py:574`）+ 写前守卫（`:1340-1343`，调用点 `:942`）让之后每次写入**立即失败且不碰文件**；`close()` 还不 checkpoint。（§0.5 · §五）
4. **降级中搜索还能用吗？** 能。`search_messages` 在 `_fts_stale` 为真时走 LIKE 扫权威表（`hermes_state_search.py:1085-1087`），并把路径名标成 `like_scan_fts_stale`，让"降级在服务"一眼可见。（§0.6 · §三）

---
## §〇 差异表

只列**架构性**差别为主，外加"已对齐"与"量级"行，方便一眼看清两条实现是不是同一个形状。真实侧锚 `f5d1926110`（v0.21.3），mini 侧锚**本章快照 `d269953`**（本章实现提交）。性质四取一：`架构性` / `已对齐` / `量级` / `不抄`。

| # | 面 | 真实 Hermes | mini 现状 | 性质 |
|---|---|---|---|---|
| 1 | 分类谓词 | `is_fts_scoped_corruption_error`（码优先只认 `SQLITE_CORRUPT_VTAB=267`，无码才让文本命中 `messages_fts\w*`+词表，`hermes_state_errors.py:100`）；`classify_persistence_error` 的 `"fts_index"` 桶（`:256-257`） | ✅ 已对齐（d269953）：`hermes_state_errors.py:82-110`（谓词）+ `:115-147`（`classify_persistence_error`）+ `:149-170`（`is_structural_corruption_error`，判定式子照抄） | 已对齐 |
| 2 | `state_meta` + `fts_stale` 标记 | 表 `state_meta(key,value)`；`FTS_STALE_KEY = "fts_stale"`（`hermes_state_common.py:878-880`）；写 `value='1'`（`hermes_state_fts.py:365-369`） | ✅ 已对齐：纯加表 `hermes_state_common.py:175-178`；键 `:70-81`；读/写/清三个助手 `hermes_state_fts.py:92-122` | 已对齐 |
| 3 | fail-open：同事务摘触发器 | `_enter_fts_fail_open`：`BEGIN IMMEDIATE` → 落标记 → `_drop_all_fts_triggers` → commit（`hermes_state_fts.py:353-403`） | ✅ 已对齐：`enter_fts_fail_open`（`hermes_state_fts.py:146-182`），门禁 + 同事务形状逐条对齐（"同一个事务 = 自己开的那个"写进 docstring） | 已对齐 |
| 4 | 写入环降级重跑 | `hermes_state.py:1020-1035`：fail-open 为真 ⇒ `continue` 重跑同一个 `fn`；结构损坏 ⇒ `_halt_db_corrupt` | ✅ 已对齐：`_execute_write` 分岔（`hermes_state.py:195-260`）+ `_degrade_or_raise_on_corruption`（`:261-291`） | 已对齐 |
| 5 | LIKE 回退接线 | 入口 `_search_messages_like_fallback`（`hermes_state_search.py:979-981`）；条件 `_fts_stale`（`:1085-1087`）；路径名 `:884-885` | ✅ 已对齐：条件由 `has_cjk` 扩成 `has_cjk(query) or self._fts_stale`（`hermes_state_search.py:173`）；路径名 `:178` + `_describe_search_path` `:191-212` | 已对齐 |
| 6 | 开库保持摘除 | 读标记 ⇒ `if self._fts_stale: self._drop_all_fts_triggers(cursor)`（`hermes_state_schema.py:958-963`） | ✅ 已对齐：`hermes_state.py:140-148`（读一次标记 + 再摘一次，抵掉 `CREATE TRIGGER IF NOT EXISTS` 的复活） | 已对齐 |
| 7 | 结构损坏隔离：粘性标志 | `self._db_corrupt` 粘性（`hermes_state.py:574`）+ `_halt_db_corrupt`（`:1249-1267`，抛类型化错误、**不重试**） | ✅ 已对齐：`hermes_state.py:126-132`（标志）+ `:304-317`（`_halt_db_corrupt`） | 已对齐 |
| 8 | 结构损坏隔离：写前守卫 | `_raise_if_db_corrupt`（`hermes_state.py:1340-1343`），调用点 `:942`（每轮 while 顶部，不碰文件） | ✅ 已对齐：`hermes_state.py:319-327`，调用点 `:226` | 已对齐 |
| 9 | 结构损坏隔离：拒重开 | `_reopen_after_close_locked`（`hermes_state.py:888-904`） | ✅ 已对齐：`_reopen_after_close`（`hermes_state.py:329-352`），调用点 `:229-230`（健康句柄懒重开、被隔离的抛） | 已对齐 |
| 10 | 结构损坏隔离：close 不 checkpoint | `close()` 跳过显式 WAL checkpoint（`hermes_state.py:1449-1457`） | ✅ 已对齐：`close()` 显式不 checkpoint（`hermes_state.py:382-407`；mini 本就裸关 ⇒ 天然满足，本轮把这句写实） | 已对齐 |
| 11 | 受守卫重建（准入锁 + 外部持有者守卫） | `_recover_stale_fts`（外部持有者守卫 + `fts_rebuild_admission`，`hermes_state_schema.py:545-559`；锁 `hermes_state_common.py:1168-1224`；守卫 `hermes_state_holders.py:187-232`） | ⏭ 不抄（B1/B2）：mini 未实现并发重建准入与 `/proc` 持有者侦查；重建只在 `repair` 显式路径（`hermes_state_fts.py:187-217`），**开库不自动重建** | 不抄 |
| 12 | 长命网关退避重试 | `retry_deferred_fts_recovery` 60s→×2→3600s（`hermes_state_schema.py:561-620`、常量 `:41-44`）；网关节拍 `gateway/run.py:4566-4579`、调度 `:4650` | ⏭ 不抄（B3）：mini 已有常驻网关，但尚未接入 FTS 自动重建的退避路径 | 不抄 |
| 13 | 替代落盘 sink（JSONL + 暂存池） | 隔离时转写本改去 `sessions/<id>.jsonl`（`agent/session_persistence.py:256-262`）+ `pending_messages/`（`gateway/shutdown_flush.py:37` / `:117`）；`gateway/session_transcript.py:140-160` | ⏭ 不抄（B4）：需要 mini 完全没有的 sink 基座（第 7 章差距清单已挂"未做"）；隔离机制本身不依赖它 | 不抄 |
| 14 | raw-copy 备份 | `_backup_db_file`（`hermes_state_repair.py:478-490`，逐字 `Raw bytes on purpose: the DB won't open cleanly, so preserve them exactly for forensics.`；产物 `state.db.malformed-backup-<stamp>`） | ✅ 已对齐：`hermes_state_repair.py:153-183`（raw-copy + 三件 sidecar + 同秒避让） | 已对齐 |
| 15 | 三段验证 | 文档 §3：① 标记查询**零行** · ② 预期触发器**在场** · ③ 权威行数**不得下降** | ✅ 已对齐：`_verify_repair`（`hermes_state_repair.py:194-213`），跑在**新开的只读连接**上看磁盘事实 | 已对齐 |
| 16 | `sessions repair` CLI | `hermes_cli/subcommands/sessions.py:160-168`（`--check-only` / `--no-backup`）；实现 `hermes_cli/sessions_cmd.py:101-115` | ✅ 已对齐（参数面裁剪）：`hermes_cli/sessions_cmd.py:34-66`（`--check-only`）/ `:76-118`（repair）；注册 `hermes_cli/main.py:85-99`。**无** `--no-backup` / `--source` / `--output` / `--inspect-only`（B9） | 已对齐 |
| 17 | `state-snapshots/` 快照 | `_QUICK_SNAPSHOTS_DIR = "state-snapshots"`（`hermes_cli/backup.py:37`）+ `_create_quick_snapshot_locked`（`:1212`）+ `restore_quick_snapshot`（`:1302`） | ⏭ 不抄（B6）：运维设施，与"恢复机制的形状"无关 | 不抄 |
| 18 | DBCONFIG（`SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`） | Python 3.12+ 关掉 SQLite 自己那次关停 checkpoint（`hermes_state.py:405-408` / `:1267-1281`） | ⏭ 不抄（B8）：mini 不发显式关停 checkpoint，但 SQLite 仍可能在最后连接关闭时自动 checkpoint；当前未实现这第三道防线，不能把它算成已满足 | 不抄 |
| 19 | 触发器全集（六触发器） | base 3 + trigram 3（`hermes_state_common.py:321-322`）+ CJK 3（`:872`） | ⬜ 没做（量级）：mini 只有 base 一套 3 个（`hermes_state_common.py:57-60`，DDL `:242-262`） | 量级 |

**两条准确性备注**

- 行 11 的"不抄"指的是**跨进程守卫**（准入锁 + 外部持有者侦查）不抄；mini 的**重建动作本身**是有的（`hermes_state_fts.py:187-217`），只是入口收窄成 `repair` 一条，不开库自动跑。别把这一行读成"mini 不会重建"。
- （已解决）A1 轮在 `hermes_state_errors.py` 里留过一句"粘性隔离留给下一轮"的旧注释；A7（`d269953`）把隔离落在 `hermes_state.py` 之后，该注释已在 `950fe6c` 改成指明四个真实落点（`_halt_db_corrupt` / `_raise_if_db_corrupt` / `_reopen_after_close` / `close()`）。

---

## §一 两类数据：权威转写本 vs 派生索引

**① Hermes 怎么做的**：本章所有设计的起点，是 `state.db` 里那**两堆性质完全不同**的数据：

- **权威（canonical）转写本** = `sessions` 与 `messages` 两张表。它们是**唯一真相**：一次对话"到底说了什么"，只能从这里读。
- **派生索引（derived index）** = `messages_fts*` 这些 FTS5 影子表，**以及它们的同步触发器**。它们只是副本，可以随时由 `messages` 全量重建。

这两堆数据的**耦合方式**决定了故障的形状。FTS 不是"异步后台索引"，而是**写入时同步**的：`messages` 上的那一套同步触发器，让任何一次 `INSERT` / `DELETE` / `UPDATE` 都顺带写进 FTS 影子表。于是派生索引一坏，**坏的不只是搜索** —— **整条 canonical 写入会一起失败**（触发器把 `INSERT` 送进坏影子表 ⇒ 整条语句抛 `DatabaseError`）。如果不对这条耦合做任何处理，一个**健康的转写本**会**因为副本坏了而写不进去** —— 等于把"写一条用户消息"变成"一次没有上界的整本重建"的开端。

设计给这条耦合划了一条**可摘的边界**：

1. 派生索引**允许被临时摘除**（`DROP TRIGGER` + 持久标记 `fts_stale`），权威写入立刻恢复；
2. 但**绝不能**把一次活写 / 活搜，变成一次**无上界的全量重建** —— `FTS5('rebuild')` 只属于"有独占准入权的恢复窗口"。

为什么第 2 条这么硬？两个理由，一个关于规模、一个关于正确性：

- **规模**：真实测到一次阻塞式重建在 25GB 的库上**持写锁约 16 分钟**（`hermes_state_fts.py:405-407` 的现场注释）。把这种操作塞进一次用户写入，等于把一次毫秒级动作变成十几分钟挂起，还和前台并发写互相锁死。
- **正确性**：触发器一摘，索引就有一段**范围未知的空窗**（触发器缺席期间写进来的行没进索引）。在活路径半路重装触发器 / 重建，会与"已落库但没进索引"的窗口交错 —— 而 external-content 表上一条 `'delete'` 命令如果作用于"索引从未持有的 rowid"，正是 FTS5 的经典损坏点（`hermes_state_fts.py:28-31`）。所以重建**只属于**恢复路径：独占准入 + 一个写事务（`hermes_state_schema.py:644` 那句 `INSERT INTO messages_fts(messages_fts) VALUES('rebuild')`，收尾在 `:650` 删标记）。

一句话：**权威要保住、要能继续写；派生坏了可以退场、以后再建**。这两类的边界，就是本章所有机制（fail-open、LIKE 回退、隔离、受守卫重建、repair）的服务对象。

**② 读 Hermes 的代码**：`hermes_state_common.py:878-880`（`FTS_STALE_KEY` 常量及其上方两行注释，逐字）：

```
# Set when a base/trigram FTS index was detached after runtime corruption; startup must rebuild the complete
# index before reinstalling sync triggers (rows written while they were absent leave an unknown gap).
FTS_STALE_KEY = "fts_stale"
```

**要害**：这三行把"派生索引"的整个生命周期压成了一枚**持久标记**，并一句话说清了边界 —— 索引一旦被摘（detached），就必须记下一个**跨重启可见**的事实；否则重开库会以为索引是好的、把触发器装回去，而"触发器缺席期间写进来的行"那段空窗（`rows written while they were absent leave an unknown gap`）就永远补不上。标记的语义因此是：**未全量重建之前，谁都不准重装触发器**。权威表（`sessions`/`messages`）在这三行里**一个字都没出现** —— 这本身就是"权威不可损、派生可摘"这条分界的文字凭证。

**③ mini 的实现（现状）**：✅ 已对齐（`d269953`）。mini 有**同样形状**的两类数据：

- 权威表在 `SCHEMA_SQL`（`hermes_state_common.py:84-179`）：`system_prompts` / `sessions` / `messages` / `schema_version`。
- 派生索引是 `messages_fts` 外部内容型 FTS5 表（`hermes_state_common.py:219-224`，`content='messages', content_rowid='id'`），加**三个**同步触发器（`:242-262`，INSERT / DELETE / UPDATE 各一）。
- 第 11 章给 mini 补上了"边界"的载体：`state_meta(key,value)` 表 + `FTS_STALE_KEY`（`hermes_state_common.py:175-178`、`:70-81`），配读 / 写 / 清三个助手（`hermes_state_fts.py:92-122`）。

mini 的**耦合方式与真实版同形**：触发器写在 `POST_RECONCILE_SQL` 尾部（`hermes_state_common.py:267-269`），任何 `messages` 写入都经它们同步进 FTS；`enter_fts_fail_open` 摘的就是这**同一份名单**（`hermes_state_fts.py:125-133` 遍历 `_FTS_TRIGGERS`），`rebuild_fts` 装回来的也是它，顺序固定为 **重建索引 → 装触发器 → 清标记**（`hermes_state_fts.py:187-217`，docstring 写死"顺序不能换"）。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 派生索引套数 | base + trigram + CJK 三套（`hermes_state_common.py:321-322` / `:872`） | ⬜ 没做（量级）：只有 base 一套（`hermes_state_common.py:219-224`） |
| 同步触发器 | 6 个（+CJK 3）（`hermes_state_common.py:321-322`） | ⬜ 没做（量级）：3 个（`hermes_state_common.py:242-262`），形状对齐、套数不同 |
| 边界的载体 | `state_meta` + `fts_stale` 持久标记（`hermes_state_common.py:878-880`） | ✅ 已对齐（d269953）：同表同键（`hermes_state_common.py:175-178` / `:70-81`） |
| 重建归属 | 只在受守卫恢复窗口（准入锁 + 外部持有者守卫） | ⏭ 不抄（B1/B2）：只在 `repair` 显式路径（`hermes_state_fts.py:187-217`）；`rebuild_fts()` 不在任何活写 / 活搜路径上 |

---

## §二 FTS 损坏的现场自愈（fail-open）

**① Hermes 怎么做的**：FTS 索引不是真相，它是**派生索引（derived index）**——真相在 `messages` 表（权威转写本），索引只是它的可搜版本。触发器把 `messages` 上每一次 INSERT / DELETE / UPDATE 同步进 `messages_fts*` 影子表。于是当影子表坏掉，**不是「消息写不进去」，而是「写一条消息顺带触达了一个坏掉的派生物」**：那条 INSERT 连同它自己一起抛 `sqlite3.DatabaseError`，canonical 行本身**没坏**。这里有一个不对称，整条机制都建立在它上面——**派生索引可再生（rebuild 就能回来），权威行不可再生（丢了就是丢了）**。所以处置只有一个方向：**把不可信的派生物摘掉，让权威写入先过去**。这就是 fail-open（失败降级）。摘触发器 = 断开 `messages → 影子表` 那条同步链，写入立刻畅通；代价是搜索暂时降级（§三）。

下面四个问题最容易被读错，逐个钉死：

**（一）「同一个事务」到底指哪个事务？**
指的是 fail-open **自己开的那个** `BEGIN IMMEDIATE`（真实 `hermes_state_fts.py:363`），它把两件事圈在一个事务里：**写 `fts_stale` 面包屑** + **`DROP` 掉同步触发器**，然后一起 commit。**不是**那条失败的 canonical 写入事务——它早被 `_execute_write` 的 `except` 分支 `ROLLBACK` 放弃了。fail-open 是**另起一个事务**去收拾残局，收拾完再靠外层重试环 `continue` 去重跑写入。**为什么这两件事必须同事务**：若崩在「摘了触发器」与「留下面包屑」之间，下次开库读不到标记 ⇒ 以为索引完整 ⇒ 把触发器装回去 ⇒ 「触发器缺席期间写进来的那些行」**永远补不进索引**（一个范围未知的空窗）。同事务 ⇒ 要么都发生、要么都没发生，**崩也只往保守那侧崩**。

**（二）为什么绝不是 rebuild？**
`FTS5('rebuild')` 会**全量重扫** `messages` 重建整个索引——真实实测在一张 **25GB 的库上持写锁约 16 分钟**（`hermes_state_fts.py:405-407` 的现场数据）。把这种动作塞进一次活写，等于把一次毫秒级动作变成十几分钟挂起，还和前台并发写互相锁死。更硬的理由：触发器一摘，索引就有**范围未知的空窗**（`hermes_state_fts.py:355-356`）；在活路径半路重装触发器/重建，会与「已经有并发写落库、但触发器缺席」的窗口交错——external-content 的 `delete` 作用于索引从未持有的 rowid，**正是 FTS5 的经典损坏点**（`hermes_state_fts.py:29-31`）。所以活路径只做「**摘**」（便宜、与库大小无关），**重建只属于受守卫的恢复路径**（§三末 / 第 7 章）。

**（三）重跑同一个 `fn` 为什么安全？**
外层 `while True:` 环捕获 `DatabaseError` → `_enter_fts_fail_open` 返回真 → `continue` → **用同一连接重跑同一个 `fn`**（真实 `hermes_state.py:1030-1031`）。安全的前提是**幂等**，而幂等是 `_execute_write` 本来就写在契约里的：`BEGIN IMMEDIATE` 先拿写锁、**整块**重试（不是重试某一条语句），所以回调**必须能承受被完整跑第二遍**。第一次失败时事务已 `ROLLBACK`，第二次是**从头再来**、不是「接着半截写」；而此刻触发器已摘，写不再触达坏影子表，于是正常 `COMMIT`。**判据之一是「分不分得清」**：门禁要求这次损坏**确实归因到 FTS**（`_is_fts_write_corruption_error`），分不清就**不摘**——摘错了会把一个健康索引整成空窗。

| 问题 | 答案 | 真实落点 |
|---|---|---|
| 摘的是什么 | 三个同步触发器（`messages_fts_{insert,delete,update}`） | `hermes_state_fts.py:319-325` |
| 同事务指哪个 | fail-open 自己开的 `BEGIN IMMEDIATE`，非失败的那条写入事务 | `hermes_state_fts.py:363` |
| 为什么不 rebuild | 25GB 库持写锁 ~16 分钟 + 空窗期重装触发器的经典损坏 | `hermes_state_fts.py:355-356`、`:405-407` |
| 重跑为何安全 | `fn` 幂等（契约）+ 首次已 ROLLBACK（从头再来） | `hermes_state.py:1020-1035` |

**② 读 Hermes 的代码**：`hermes_state_fts.py:354-356`（`_enter_fts_fail_open` 的 docstring，逐字）：

```text
354:        """Detach corrupt FTS indexes so canonical writes can continue. Breadcrumb +
355:        trigger drop commit atomically: once triggers are absent the index has a
356:        gap of unknown extent, so nobody may reinstall them without a full rebuild."""
```

要害：这一句同时钉死了本节的三个要点——`Breadcrumb + trigger drop commit atomically` 就是「同事务」（哪个事务见①）；`gap of unknown extent` 解释了摘完为什么**不准随便装回去**；`without a full rebuild` 则是「重建只能在受守卫路径上」的出处。摘除与留痕**绑成一个原子动作**，是本机制能成立的唯一前提——分开做，两个方向都会留下不一致。

**③ mini 的实现（现状）**：✅ 已抄（`d269953`）。三个模块分工，判据、事务形状、重跑逻辑逐条对齐。

- **分类谓词**（`hermes_state_errors.py`）：`is_fts_scoped_corruption_error`（`:82-110`）实现真实版那条唯一的「归因规则」——**有码时码说了算**（`sqlite_errorcode == SQLITE_CORRUPT_VTAB`，常数 `:68`），**无码时才让文本说话**，且必须同时命中 `messages_fts\w*`（`_FTS_OBJECT_RE :79`）与「`fts5:` + `corrupt`」或损坏词表（`_DB_CORRUPTION_MARKERS :38-46`）。同一文件里 `classify_persistence_error`（`:115-147`）把桶分成 `locked / fts_index / corrupt / disk / unknown`，纪律是 **provenance 先于文本、corrupt 桶排在 locked/disk 之前**；`is_structural_corruption_error`（`:149-170`）是「裸 `CORRUPT`/`NOTADB` 且**无** FTS provenance」那半边，两桶**互斥**。`StateDbCorruptError`（`:173-193`）继承 `sqlite3.DatabaseError`，让每个按父类分类的降级路仍然认得它。
- **fail-open 本体**（`hermes_state_fts.py`）：`enter_fts_fail_open`（`:146-184`）——门禁 `if not self._fts_enabled or not is_fts_scoped_corruption_error(exc)`（`:164`）→ `BEGIN IMMEDIATE`（`:168`）→ `_write_fts_stale_marker`（`:171`）→ `_drop_fts_triggers`（`:173`）→ `COMMIT`（`:175`），`BaseException` 则 `ROLLBACK` 后 `raise`（`:176-178`）；顶端三个助手 `_read / _write / _clear_fts_stale_marker`（`:92 / :106 / :114`）读写的就是 `state_meta(key='fts_stale')`，摘除用 `_drop_fts_triggers`（`:125-133`，逐个 `DROP TRIGGER IF EXISTS`）。
- **写入环降级路**（`hermes_state.py`）：`_execute_write` 里 `OperationalError` 非锁分支先走一次损坏分岔口（`:242-249`，因为 `OperationalError` 也是 `DatabaseError` 子类，不这样做会被漏掉），`DatabaseError` 分支在 `:253-259`：

```text
253:            except sqlite3.DatabaseError as exc:
257:                if self._degrade_or_raise_on_corruption(exc):
258:                    continue
259:                raise
```

`_degrade_or_raise_on_corruption`（`:261` 起）的核心就是「FTS 派生损坏 ⇒ `enter_fts_fail_open` 为真 ⇒ 调用方 `continue` 重跑同一个 `fn`」（`:279-280`）。`IntegrityError` 既不是 FTS 归因也不在 corrupt 桶 ⇒ `return False`、原样抛，行为不变。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 归因规则（码优先 + 文本兜底） | `is_fts_scoped_corruption_error`（`hermes_state_errors.py:100-122`）；裸 CORRUPT/NOTADB 必然落 corrupt 桶 | ✅ 已抄（`d269953`）：`hermes_state_errors.py:82-110`，判定式子逐条对齐 |
| 摘除的范围 | base 3 + trigram 3 + CJK 3 共 **9** 个触发器（`hermes_state_schema.py:212-216`） | ✅ 已对齐：`drop_all_fts_triggers`（`:136-143`）语义是「别只摘一半」，眼下只有 base 一套 3 个 |
| 同事务（marker + drop） | `_enter_fts_fail_open`（`hermes_state_fts.py:353-403`，含 CJK 触发器在则另写 `fts_cjk_stale`） | ✅ 已抄主体（`enter_fts_fail_open` `:146-184`）；无 CJK 标记（mini 无第二套索引） |
| 写前代际守卫 | `_raise_if_db_corrupt()` + `_halt_if_db_generation_changed()` 两道前置（`:359-360`） | 部分：写前守卫 `_raise_if_db_corrupt` 已接（A7）；**无** `generation_changed` 代际检测（⬜ 没做） |
| 触发器复活防线 | 开库时 `if self._fts_stale: self._drop_all_fts_triggers(cursor)`（`hermes_state_schema.py:958-963`） | ✅ 已抄（见 §三末那段）：`hermes_state.py:140-151` |

---

## §三 搜索降级与"活路径绝不 rebuild"

**① Hermes 怎么做的**：fail-open 摘掉触发器，搜索就得换条路——**绕开不可信的派生索引，直接 LIKE 扫权威表 `messages`**。为什么「必须绕开」而不是「接着用坏索引」：触发器摘掉意味着索引有**范围未知的空窗**（`messages` 里那些「触发器缺席期间写进来」的行从未进过索引），而影子表本身还坏着。此时走 `MATCH` 只有两种结局，**都不可接受**：要么直接抛 `DatabaseError`；要么更坏——**静默漏项**（索引与权威表不一致，搜索结果少东西却不报错）。搜索是给人看的，**漏项比慢更坏**。所以降级的答案不是「勉强用索引」，而是「换一条不依赖索引的路」。

这条路只在**权威表**上做：`LIKE` 全表扫 `messages`。它**慢**（无 FTS 排名、无分词、全表过一遍），但它**只读权威表、不依赖任何派生状态、也永远不会去动索引**，所以它给出的结果对 canonical 行而言是**完整且正确**的——降级丢的是**速度**，不是**准确性**。

**为什么重建只能在受守卫的开库路径上**：一次「活搜索」绝不允许变成一次无界全量重建（同样是那张 25GB 库上 ~16 分钟写锁的账）。重建需要**独占准入**（跨进程锁 + 外部持有者守卫）和**一个写事务**，这份等待只有「**下次开库**」那条受守卫路径付得起（第 7 章）；长命网关则用 housekeeping tick 上的**非阻塞**重试（`timeout=0`）在进程内补上——两条路都受同一把准入锁管，只是超时预算不同。活搜索**永远**只做降级，**永不**做重建，这条不变式在真实侧被三处注释钉死（写路径、搜索、fail-open 各一处）。

**② 读 Hermes 的代码**：`hermes_state_search.py:916-920`（`_match_rows` 的 docstring，逐字）：

```text
916:        """Run one MATCH against *table*; ``None`` when the query cannot execute (tokenizer /
917:        syntax) so the caller falls back. *fail_open* names the index for the
918:        substring-capable routes: a corruption-class ``DatabaseError`` there detaches the
919:        derived indexes (``_enter_fts_fail_open``) and answers from canonical rows — a live
920:        search never runs the unbounded rebuild. Other ``DatabaseError``s propagate."""
```

要害：最后那半句 `a live search never runs the unbounded rebuild` 就是「活路径绝不 rebuild」在**搜索侧**的原文出处——搜索命中损坏时，动作只有「摘 + 从权威行作答」，**不是**「顺手重建一下」。前半句 `answers from canonical rows` 说明降级的兜底永远是权威表；`Other DatabaseError's propagate` 则划出边界：**只有** corruption 类才降级，语法/分词类错误照旧上抛（不能拿降级掩盖真正的查询错误）。

**③ mini 的实现（现状）**：✅ 已抄（`d269953`）。路由判据加了一条腿——`hermes_state_search.py:173` 由 `has_cjk(query)`（FTS5 默认分词器不切 CJK，第 6 章既有）扩成：

```text
173:        if has_cjk(query) or self._fts_stale:
178:            route = "like_scan_fts_stale" if self._fts_stale else "like"
179:            return self._search_like(query, route=route,
```

即「**含中文**」与「**索引降级**」两个理由都走 LIKE 扫权威表。路径名只在「**因降级**而走 LIKE」时换成真实版那个名字 `like_scan_fts_stale`（真实 `:884-885`）；中文那条路**仍保持 `"like"`**（第 6 章既有契约，不能动）。更细的观测名字在 `_describe_search_path`（`:191-212`）：首条判据就是 `if self._fts_stale: return "like_scan_fts_stale"`（`:196-197`），其余非 CJK → `"fts5"`、CJK → `"like_scan"`。`_search_like`（`:237`）的参数 `route: str = "like"` 就是这个路径名的落点，函数 docstring 里逐字引用了真实侧那句 `a live search never runs the unbounded rebuild`，把不变量钉在这里。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 降级触发判据 | 入口先 `_refresh_fts_stale_state()` 跨进程观测标记，再 `if self._fts_stale:` 走 LIKE（`hermes_state_search.py:1084-1087`） | ✅ 已对齐语义：`if has_cjk(query) or self._fts_stale:`（`hermes_state_search.py:173`） |
| 跨进程观测标记 | `_refresh_fts_stale_state` 读 `state_meta` 发现**别的进程**发起的 fail-open（`:991-1001`） | ⬜ 没做：mini 单进程，开库时读一次标记即可（`hermes_state.py:140`） |
| 搜索自身命中损坏 | `except sqlite3.DatabaseError → _enter_fts_fail_open` 再 LIKE（`:1106-1115`） | ⬜ 没做：mini 的 `_search_fts` 未接损坏兜底（降级靠写入环触发） |
| 路径名 | `like_scan_fts_stale` / `like_scan` / `fts5`（`:881-896`） | ✅ 已抄三个名字（`_describe_search_path:191-212`）；结果字段 `route` 保守起见只在降级时改名 |
| 不许活搜索 rebuild | 三处注释钉死（`:918-920` 等） | ✅ 已抄：模块 docstring + `_search_like` docstring 逐字引该句（`hermes_state_search.py:26-32` / `:240-248`） |

### ★ mini 多出来的一条非平凡设计：开库时「标记还在 ⇒ 再次摘掉触发器」

fail-open 本体之外，mini 还顺手做了**开库时的一次保持摘除**——`hermes_state.py:140-151`：

```text
140:        self._fts_stale = self._read_fts_stale_marker()
141:        if self._fts_stale:
142:            self._fts_enabled = False
150:            if not read_only:
151:                self.drop_all_fts_triggers()
```

**它非平凡在哪**：开库要跑 `_ensure_schema()`，而那里的建表/建触发器语句是 `CREATE TRIGGER IF NOT EXISTS` ——它是**幂等的**，不管「上次是谁把触发器摘了」，只要 `IF NOT EXISTS` 判定为不存在（因为确实被摘了），它就**把触发器原样复活**。若开库时不重新摘一次，那个 `fts_stale` 标记就变成**一句谎话**：「标记说索引在摘除中，库里却装着触发器」。而触发器一复活，写入又会去触达（可能仍坏的）影子表，fail-open 的不变量当场破。所以这条不是可选装饰，是**让标记保持诚实**的必需一步：**标记还在 = 这库里就不该有触发器**，每次开库都照此执行。

**与真实版的关系**：它严格对应真实版的 `hermes_state_schema.py:958-963`——读 `state_meta` 标记后 `if self._fts_stale: self._drop_all_fts_triggers(cursor)`，同一句语义（「先前的进程因损坏摘了 FTS，在全量重建前保持摘除」）。所以在「抄没抄」的口径上，它是 ✅ 已抄，不是 mini 自创；说它「多出来」，是相对 fail-open（A1–A6）那批而言——它严格属于下一轮「开库受守卫重建」的**前半**，但**不做会自相矛盾**，所以提前照抄了这保守的一半（准入锁 / 外部持有者守卫 / 受守卫重建留待下一轮）。

---

## §四 恢复归属与受守卫重建（B 批：为什么 mini 不做）

**① Hermes 怎么做的**：FTS 摘除（§二 §三）之后，索引处在一段**范围未知的空窗**里 —— 触发器被摘掉期间写进来的行没有被同步进索引，谁也不知道缺了多少条。要恢复它，只有一次**全量重建**（`FTS5('rebuild')`）能做到。问题于是变成：**这次重建归谁做？**

两个"看起来顺手"的答案都被否掉：

- **不是那次失败的活写/活搜。** 把一次无界全量重建塞进一条活写/活搜，等于把一次用户消息写入变成十几分钟的写锁挂起（真实实测：一张 25GB 库上重建持写锁约 16 分钟），而且会与前台并发写互锁。
- **不是"就地在这个句柄上修"。** 触发器等价于"索引和 messages 表之间的同步契约"；在活路径半路摘了又重装触发器/重建，会与"已经有并发写落库、但触发器缺席"的窗口交错 —— external-content 的 `delete` 作用于索引从未持有的 rowid，正是 FTS5 的经典损坏点。

于是 Hermes 把**归属**交给**下一个打开 `state.db` 的人**。打开时刻是天然的安静点：新进程刚拿到库、还没开始服务，可以支付一次准入等待。重建在那里被**两道守卫**罩住：

1. **打开时先读标记、保持摘除**（`hermes_state_schema.py:958-963`）：读 `state_meta` 的 `fts_stale` 标记；若在，立刻 `_drop_all_fts_triggers` 再摘一次 —— 因为 `_ensure_schema()` 里的 `CREATE TRIGGER IF NOT EXISTS` 会**幂等复活**触发器，不管上次是谁摘的；不补这一次，那段空窗就永远补不上了。
2. **真正触发受守卫重建**（`hermes_state_schema.py:1163-1168`）：走到这里才调 `_recover_stale_fts`。它先过**外部持有者守卫**、再过**跨进程准入锁**；两门都过才在**一个 `BEGIN IMMEDIATE` 写事务**里 `INSERT INTO messages_fts(messages_fts) VALUES('rebuild')` + **删掉 `fts_stale` 标记** + COMMIT（`:644` 的重建语句、`:650` 的删标记语句）。任一门没过（或重建失败）都 **fail closed**：标记**留着**、四个 enable 标志清 False、FTS 保持摘除、canonical 写入与 LIKE 搜索照常可用。

**两道守卫各自在防什么**

| 守卫 | 在哪 | 判据 / 动作 |
|---|---|---|
| **外部持有者守卫** | `hermes_state_holders.py:187-232`（委托 `hermes_state.py:430-432`） | 取 `state.db` / `-wal` / `-shm` 三者 realpath 的 `(st_dev, st_ino)`，在 Linux 遍历 `/proc/<pid>/fd` 找**谁还打开着同一个 inode**（排除本进程）；Windows 直接返回空（`:194-195`）。若持有者仍在 ⇒ `_defer_stale_fts_for_holders`（`hermes_state_schema.py:467-476`）写一条诊断键、返回 True ⇒ **放弃重建**。防的是"结构维护在还有活持有者时撕裂 B 树页"。 |
| **跨进程准入锁** | `fts_rebuild_admission`，`hermes_state_common.py:1168-1224` | 锁文件 `{db_path}.fts_rebuild.lock`，`fcntl.flock`（Windows `msvcrt.locking`）；超时 `_FTS_REBUILD_LOCK_TIMEOUT_SECONDS = 120.0`、轮询 `_FTS_REBUILD_LOCK_POLL_SECONDS = 0.1`（`:988-989`）。拿不到（或锁文件打不开）= **fail closed**，`yield False` ⇒ 不重建、留标记等下次重试。 |

**长命进程为什么需要另一条路**：`_recover_stale_fts` 是"打开时**一次性**、支付**满额**准入等待（120s）"的。但 gateway 可能**一次打开 `state.db` 就运行好几天** —— "next open" 永远不来，那条路就永远走不到。于是有 `retry_deferred_fts_recovery`（`hermes_state_schema.py:561-620`）：它由**网关已有的 housekeeping tick**（`gateway/run.py:4566-4579` 的定义、`:4650` 的登记 `every=1`、tick 间隔默认 60s 在 `:4622`/`:4663`）在**进程内**调用，用**退避**节流（`_FTS_STALE_RETRY_SECONDS = 60.0` 起、每次 ×2、封顶 `_FTS_STALE_RETRY_MAX_SECONDS = 3600.0`，`:41-44`）与**非阻塞准入**（`timeout_seconds=0.0`，`:607`）—— 持锁未释放就跳过、下次再来；**不新开线程、绝不抛异常**。它和"打开时"那条受**同一把锁 + 同一个外部持有者守卫**管辖，只是超时预算不同（满额等待 vs 非阻塞探测）。

**② 读 Hermes 的代码**：`hermes_state_schema.py:562-571`（`retry_deferred_fts_recovery` 的 docstring，逐字）：

```
        """Retry a deferred stale-FTS rebuild (gateway housekeeping tick). ``_recover_stale_fts``
        fails closed at open, leaving search on LIKE; live write/search paths must never
        start a full rebuild, and a gateway opens state.db once for days, so "next open"
        never comes. Bounded doubling backoff, non-blocking admission, no new thread. True
        only when the index was rebuilt and sync triggers restored. Never raises.

        This is the in-process retry: bounded backoff from ``_FTS_STALE_RETRY_SECONDS`` doubling to
        ``_FTS_STALE_RETRY_MAX_SECONDS``, non-blocking admission (``timeout=0``) so a live holder is skipped
        and tried again later, no new thread — the caller is an existing periodic tick (gateway
        housekeeping). See #100108, #97940."""
```

要害：`a gateway opens state.db once for days, so "next open" never comes` 一句把两条路的分工钉死 —— 打开时那条是"一次性、可付满等待"；长命网关**不重开**，才必须另接一条**进程内的非阻塞重试**。同一 docstring 又用 `live write/search paths must never start a full rebuild` 重申了 §二 §三 的"活路径绝不 rebuild"纪律：连这条补救路径也**不**在活写/活搜上开重建。

**③ mini 的实现（现状）**：`⏭ 不抄（理由）`。mini 只做了这道守卫的**前半句** —— `hermes_state.py:138-151` 在开库时**读一次** `state_meta` 的 `fts_stale` 标记（`:140`），若在则置 `_fts_enabled = False` 并 `drop_all_fts_triggers()`（`:151`）"保持摘除"（对应真实版 `hermes_state_schema.py:958-963`）。**受守卫重建本身在 mini 没有对应物**，理由有三条：

- mini **已有常驻网关和独立 CLI**，它们可能同时持有同一个 profile 的库；“单进程、没有外部持有者”不是事实。
- mini 未接入受守卫的自动重建与后台退避重试；FTS 陈旧时持续走 LIKE，直到显式修复。
- mini 的重建**只发生在显式 `sessions repair` 里**（`rebuild_fts()`）。它没有上游完整的外部持有者扫描，因此执行前必须停掉该 profile 的网关及其他数据库使用者；“由人触发”不等于“没有并发持有者”。

所以 mini 的现状是"**标记还在 ⇒ 保持摘除**"（更保守的那侧：宁可不重建，也不在没把握时动索引），而"**下一个打开者受守卫重建**"整条留给真实版。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 恢复归属 | 交给"下一个打开 `state.db` 的人"；打开时受守卫重建（`hermes_state_schema.py:1163-1168`） | ⏭ 不抄：重建只走显式 `sessions repair`（`hermes_state_fts.py:187-217`），无"开库即重建" |
| 开库读标记 / 保持摘除 | `hermes_state_schema.py:958-963` | ✅ 已抄（`hermes_state.py:138-151`） |
| 跨进程准入锁 | `fts_rebuild_admission`（`hermes_state_common.py:1168-1224`，flock + 120s 超时） | ⏭ 不抄：单进程，无并发重建者 |
| 外部持有者守卫 | 扫 `/proc/<pid>/fd` 比 inode（`hermes_state_holders.py:187-232`） | ⏭ 不抄：无多进程持有者模型 |
| 长命退避重试 | `retry_deferred_fts_recovery`（`hermes_state_schema.py:561-620`）+ 网关 tick（`run.py:4566-4579/4650`） | ⏭ 未实现：mini 已有常驻网关与巡检，但尚未接入 FTS 自动重建的退避路径 |

## §五 结构损坏：停写就是保护

**① Hermes 怎么做的**：一句总纲 —— **Stopping the writes is the protection**。当一次活写报出**裸的** `SQLITE_CORRUPT` / `SQLITE_NOTADB`（`database disk image is malformed` / `file is not a database`）、且**没有 FTS 来源（provenance）**时，损坏在**权威 B 树 / schema / freelist** 里 —— 这不是"索引坏了"那种可以摘掉继续写的处境，而是"文件本身已经撕裂"。此时唯一安全的动作是**立刻住手**。Hermes 落四件事：

1. **失败的写入向上传播、不重试。** `_execute_write` 的重试只覆盖 `locked` / `busy` / `no more rows` / `IOERR`（`hermes_state.py:996`、`:1000`、`:1011`）；结构损坏走 `_is_structural_corruption_error`（`:1235-1243`）⇒ `_halt_db_corrupt(exc)`（`:1034`），后者 `raise err from exc`（`:1265`），**不进任何重试分支**。为什么不重试：一次结构损坏说明页已经撕裂，把同一笔写重来一遍，只会把更多帧按错页号落下去。
2. **之后任何写入立即失败、不碰文件。** `_halt_db_corrupt` 先置**粘性**标志 `self._db_corrupt = True`（初始化在 `:574`）；此后每轮 `_execute_write` 的 `while` 顶部（`:942`）先调**写前守卫** `_raise_if_db_corrupt`（`:1340-1343`）：标志为真就 `raise` —— **在 `BEGIN` 之前、文件碰都不碰**。"不碰文件"是关键：真正的杀伤来自写入本身，而不是那句错误。
3. **`close()` 后永不重开连接。** `_reopen_after_close_locked`（`:888-905`）里，被隔离的句柄 `raise self._corrupt_error(...)` 拒绝重开（`:900-904`）。注释 `:899` 写明理由：重开会把**路径再解析一遍**，被替换的文件会顺着过期的 WAL/shm 假设被写；**被隔离的句柄绝不能把一条新连接交给一个受损文件**。
4. **`close()` 跳过显式 WAL 检查点。** `close()`（`:1415`）在锁内先算 `_quarantine_reason()`（`:1362-1375`，隔离中返回 `structural corruption (<原因>)`），非空就打印 warning 并**不跑** `PRAGMA wal_checkpoint(PASSIVE)`（`:1449-1457`）。这是**第二道防线** —— 停写之后，连"收尾时顺手 checkpoint 一下"也要忍住。

在此之上还有**第三道**：`SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`（Python 3.12+）。SQLite 自己在 `close()` 里还有一次"最后连接检查点"，隔离时用 `Connection.setconfig` 把它也关掉（能力探测 `:405-408`、真正开关 `_disable_close_time_checkpoint` `:1267-1283`，`setconfig(flag, True)` 在 `:1281`），于是 `-wal` 侧车在 `close()` 后**存活供取证**。**3.11 没有 `setconfig`** ⇒ 那次内部检查点**不可避免**（`StateDbCorruptError` docstring `hermes_state_errors.py:199-200`；`:599-600` 的 `_prepare_connection_retirement` 分支）⇒ 文档因此要求把 `state.db` / `-wal` / `-shm` **三件套一起拷**。

**现场事故（为什么"多写"比"不写"更坏）** —— `tests/hermes_state/test_state_db_corrupt_quarantine.py:3-9` 逐字记录：网关在第一次报结构损坏（`database disk image is malformed`）后**仍重试写入约 50 分钟**；SIGTERM 时的关停 `PRAGMA wal_checkpoint(PASSIVE)` 把 **15 页**写到**错误的页号**上（第 1 页收到一个 `messages_fts_trigram_data` 叶子），把一个**"还能读"**的文件变成了**"完全打不开"**。

这件事的教训要掰开讲，因为"多写"的坏处不是线性的：

- 一个"**打不开**"的文件是"确定死了"；一个"**还能读**"的文件是"**还有得救**" —— 离线恢复、取证、页级打捞、人工修复，全靠它还能读这一点。持续写，会把这份最后的剩余价值也烧掉。
- 更硬的原因是 **checkpoint 是"按页号把 WAL 帧搬进主文件"**。页号一旦错位，写进去的**不是**"多出来的几行数据"，而是"另一个页的内容被盖到了这里" —— 这是**结构级污染**（把一个叶子页安到第 1 页，等于把库头都埋了）。普通写入错行顶多丢数据；错页号的 checkpoint 是**把文件地图本身改错**。
- 所以处置顺序是：**先停写**（第 1~3 件：不重试 / 立即失败 / 拒重开）→ **再跳检查点**（第 4 件：`close()` 跳过显式 checkpoint）→ **最后**才轮到 3.12 的 `NO_CKPT_ON_CLOSE` 关掉 SQLite 自己那次。三道防线**依次**兜底，每一道都在防同一件事：文件被写得更坏。

**② 读 Hermes 的代码**：`hermes_state.py:1249-1253`（`_halt_db_corrupt` 的 docstring + 头三行，逐字）：

```
    def _halt_db_corrupt(self, exc: BaseException) -> None:
        """Quarantine this handle and raise; never run in-file repair here."""
        self._db_corrupt = True
        self._db_corrupt_reason = str(exc)
        self._disable_close_time_checkpoint()
```

要害：`Quarantine this handle and raise; never run in-file repair here.` 一行同时钉死两件事 —— **隔离**（置粘性标志、之后写不进去）与**绝不在活句柄上就地修**。紧接着 `_disable_close_time_checkpoint()` 说明隔离一发生就顺手把 SQLite 自己那次关停检查点也关了（3.12+）。"修"这件事全被推到**离线**路径（`sessions repair` / `recover`）：一个活着的句柄没有能力安全地改自己的 B 树页。

**③ mini 的实现（现状）**：✅ 已抄（`d269953`）。四件事逐条落地在 `hermes_state.py`：

- **粘性标志 + 抛**：`_halt_db_corrupt`（`:304-317`）置 `self._db_corrupt = True`（`:315`）后 `raise`（`:317`）；结构损坏分支在 `_degrade_or_raise_on_corruption` 里"先 `_halt_db_corrupt(exc)` 再抛"（`:288-289`）。
- **写前守卫**：`_raise_if_db_corrupt`（`:319-327`）在 `_execute_write` 每轮 `while` 顶部调用（`:226`）—— 已隔离 ⇒ 立即抛、**不碰文件**。
- **拒重开**：`_reopen_after_close`（`:329-351`）在 `_execute_write` 接线（`:228-230`）；健康句柄照常懒重连，被隔离的抛 `StateDbCorruptError`。
- **`close()`**：`:382-407` 只做裸关（`:402` 置 `_closed`、`:404` `self._conn.close()`），**这条关停路径不发显式 checkpoint，但 SQLite 仍可能自动执行**（`:401` 一行显式声明）。

**关键差异（差异表要写明）**：mini 的 `close()` **从来就不 checkpoint** ⇒ 上游"`close()` 跳过显式 WAL 检查点"这**第二道防线**在 mini 是**天然满足**的（不是"为安全而跳过"，而是"原本就没有可跳的检查点"）。mini 的写入路径确实每 50 次成功写入做一次 PASSIVE 检查点（`_try_wal_checkpoint`，`:364-374`），但那是**写路径**的收尾，**不是关停**检查点。mini **未实现** `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`（B8），因此 SQLite 自己在最后连接关闭时的自动检查点仍可能发生；没有显式 `PRAGMA` 不代表没有自动检查点。这是实际的保护差距。另外，mini 把"拒重开"**显式化**成一个被接线的方法（真实版连接是懒开的，mini 的连接 `__init__` 只建一次 ⇒ 只留注释的守卫**不可测**；显式化方向与真实版一致，但**扩大了** mini 的语义）。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 失败写入不重试 | `_halt_db_corrupt` 抛 `StateDbCorruptError`，不在 locked/busy/IOERR 重试分支（`hermes_state.py:1251-1265`） | ✅ 已抄（`hermes_state.py:304-317/288-289`） |
| 之后写立即失败 | `_raise_if_db_corrupt`（`:1340-1343`），调用点 `:942` | ✅ 已抄（`hermes_state.py:319-327`，调用点 `:226`） |
| `close()` 后拒重开 | `_reopen_after_close_locked`（`:888-905`） | ✅ 已抄（`hermes_state.py:329-351`），做成被接线的方法 |
| `close()` 跳检查点 | `close()` 隔离中不跑 `PRAGMA wal_checkpoint(PASSIVE)`（`:1449-1457`） | ✅ **天然满足**：mini `close()` 本就裸关、永不 checkpoint（`hermes_state.py:382-407`） |
| `NO_CKPT_ON_CLOSE`（3.12+） | `setconfig(SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)`（`:405-408/1267-1283`） | ⏭ 不抄（B8）：mini 无"要关掉的关停检查点" |
| 3.11 三件套一起拷 | 无 `setconfig` ⇒ 内部检查点不可避免（`hermes_state_errors.py:199-200`） | 差异同上：mini 3.9–3.13 均裸关，无此告警面 |

## §六 隔离的边界（两种"认不出的损坏"）

**① Hermes 怎么做的**：§二 与 §五 的两条桶（`fts_index` vs `corrupt`）是由 `classify_persistence_error`（`hermes_state_errors.py:236`）**按词表 + result code** 判出来的。词表只有四个子串：

```
"malformed", "file is not a database", "not a database", "database corruption"
```

而在**词表之外**，SQLite 还会报出别的损坏文案。现场实测（真造损坏、真跑 `mini` 代码）撞到两种**"认不出"**的损坏，它们共同定义了隔离的**边界**：

1. **`vtable constructor failed: messages_fts`（result code 11 = `SQLITE_CORRUPT`）。** 这是"FTS 虚拟表的构造器起不来"的报法 —— 文案里**既不含** FTS 损坏词（没有 `fts5:` / `corrupt`）、**也不含**上面词表里的四个子串。后果是：`is_fts_scoped_corruption_error` 判**假**（码 11 ≠ 267，文本也无 FTS 对象归属），`classify_persistence_error` 一路落到底 `return "unknown"`（`:264`）⇒ **两个桶都不进** ⇒ `_execute_write` 的 `DatabaseError` 分支**原样 `raise`**：既不 fail-open（它不敢对来路不明的报错摘索引），也不隔离（它不敢对一个可能只是 vtable 暂时起不来的库停掉整个句柄）。
   **核对上游词表**：把**同一句**喂给上游 `hermes_state_errors.py:88` 的 `_DB_CORRUPTION_MARKERS` 与 `:236` 的 `classify_persistence_error`，实测**同样** `classify = "unknown"`、`is_fts_scoped = False`（有码 11 与无码两种都给 `unknown`）。⇒ 这是**与上游共同的边界**，**不是 mini 缺陷**：上游也认不出这句，也同样原样抛。
2. **新句柄 open 撞上 NOTADB 抛 SQLite 裸错。** 上游 §2 的**隔离判定接在 live write 上**（`_execute_write` 的 `DatabaseError` 分支，`hermes_state.py:1020-1035`）。一个**全新句柄**在 `__init__` 打开库时若第一条语句就撞 `SQLITE_NOTADB`，拿到的是 SQLite 的**裸 `DatabaseError`**（未分类、未隔离）—— 隔离的**观测面**是"已经在服务的活写"，**不是**"开库"。

**为什么这样切（边界在哪、为什么）**：

- 两条桶的**切法**是"**按能不能安全自动处置**"，**不是**"按损坏严重度"。能**归因到 FTS 索引**（码 `SQLITE_CORRUPT_VTAB = 267`，或——无码时——一个点名 `messages_fts*` 的 `fts5:` 报告）⇒ 可以安全地**摘索引继续写**（fail-open）；能**归因到文件本身**（命中四个子串）⇒ **停写隔离**。判据是**来源（provenance）**，不是形容词。
- **认不出的就"两个桶都不进"**：既不 fail-open、也不隔离，**原样抛**给调用方。这是**故意的保守** —— "不知道是哪一种"时，任何自动处置都可能**比不处置更坏**：对一个**真损坏**的文件摘触发器，会把原本还保得住的东西弄得更没法修；对一个**只是 vtable 起不来**的库停掉整个句柄，会误伤一个其实还能用的库。
- 所以"原样抛"是 fail-open 的**反面**：把判断权**交回上层 / 人**，而不是替它猜。代价也如实：这类损坏**不会自愈、也不会触发隔离**，只会反复把 `DatabaseError` 抛给调用方。**把边界画出来、写进文档**，正是为了让人知道"到这里工具就不管了，该人接手"。

**② 读 Hermes 的代码**：`hermes_state_errors.py:88-90`（词表）与 `:258-264`（`classify_persistence_error` 的兜底，逐字）：

```
88  _DB_CORRUPTION_MARKERS = (
89      "malformed", "file is not a database", "not a database", "database corruption",
90  )
...
258      text = str(exc_or_str).lower()
259      for markers, cause in _PERSISTENCE_CAUSE_BY_PHRASE:
260          if any(marker in text for marker in markers):
261              return cause
262      if is_disk_full_error(exc_or_str) or any(m in text for m in ("disk", "readonly", "read-only")):
263          return "disk"
264      return "unknown"
```

要害：**词表就是隔离能"看见"的全部。** `"vtable constructor failed: messages_fts"` 不匹配 `:89` 的任何一个子串，也不会被 FTS 归因谓词（`:100-122`，码优先只看 267）认领，于是一路滑到 `:264` 的 `return "unknown"` —— 这就是边界的确切位置。（同段 `:256` 先做 `is_fts_scoped_corruption_error` 分流，`:262` 的 `disk` 桶排在最后，呼应 `:83-87` 那句 "malformed" 含 "disk"、损坏桶必须排在前面的警告。）

**③ mini 的实现（现状）**：**与上游一致**。mini 的 `classify_persistence_error`（`hermes_state_errors.py:115-146`）与上游**同形**：`:139` 命中 `_DB_CORRUPTION_MARKERS` → `"corrupt"`；`:146` 一条 `return "unknown"` 兜底；桶集合 `PERSISTENCE_ERROR_CAUSES`（`:112`）与上游同名同义。**实测**：同一句 `vtable constructor failed: messages_fts`（带码 11 与不带码）在 mini 与上游**都**判 `unknown`、`fts_scoped=False`；而 `database disk image is malformed`（码 11）两边**都**判 `corrupt`。mini 的写入分岔口 `_degrade_or_raise_on_corruption` 对 `unknown` 走 `return False` ⇒ **原样抛**（`hermes_state.py:256` 注释逐字："它既不是 FTS 归因、也不在'结构性损坏'桶里 ⇒ 原样抛出，行为不变"）。

**真跑收据**（`demos/ch11_real_run.py`，真造损坏 + 真跑，0 失败）：

- `:144-155` 探针 A：覆写坏页后 `append_message`（要编译 FTS 同步触发器）撞上的正是 **`DatabaseError: vtable constructor failed: messages_fts`**，**码 11** ⇒ `classify = unknown` ⇒ **原样抛**（脚本注释 `:146-148` 明写"与上游同边界，非 mini 缺陷"）。
- `:157-164` 探针 B：同一库改用 `create_session`（**不牵动 FTS vtable**，只碰 `sessions` 表）⇒ SQLite 读到坏页、报**含词表里的 `malformed`** ⇒ 归位到 `corrupt` 桶 ⇒ 抛**类型化** `StateDbCorruptError`。**这两条探针并列，正好把"边界"演示成"同一份损坏，报什么文案决定进哪个桶"**。
- `:186-192` 开库边界（如实记录）：新句柄 open 撞上 NOTADB 时抛的是 **SQLite 裸 `DatabaseError`** —— 上游 §2 的隔离判定接在 **live write** 上，开库这一侧不在它范围内。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 词表 | `_DB_CORRUPTION_MARKERS = ("malformed","file is not a database","not a database","database corruption")`（`hermes_state_errors.py:88-90`） | ✅ 已抄（`hermes_state_errors.py:38-43`，同一组子串） |
| `unknown` 兜底 | `classify_persistence_error` 末尾 `return "unknown"`（`hermes_state_errors.py:264`） | ✅ 已抄（`hermes_state_errors.py:146`） |
| `vtable constructor failed: messages_fts`（码 11） | 实测 `classify = unknown` ⇒ 原样抛 | **同一结果**（实测 `classify = unknown` ⇒ 原样抛）—— **共同边界** |
| 开库撞 NOTADB | 隔离判定只在 live write（`hermes_state.py:1020-1035`）；新句柄 open 抛裸错 | 同：新句柄 open 抛 SQLite 裸 `DatabaseError`（真跑 `:186-192`） |

---

## §七 显式修复：备份 → 重建 → 三段验证

**① Hermes 怎么做的**

前两节让系统在损坏面前**不倒**：派生索引坏了就摘掉触发器继续写（fail-open），文件真坏了就停写。但这两条都只是"止血"——坏掉的东西还在盘上，用户看见的还是"会话不见了"。所以还差一条**显式、离线、人能触发**的修复路径：`hermes sessions repair`。

这条链要解决的核心难题是：**修一个已经打不开的库，本身可能把它毁得更彻底。** 于是真实实现把"改什么"和"改在哪"拆成两层：

- **先备份，再动手。** 默认先做一次 **raw-copy 备份**（原样拷字节，产物名 `state.db.malformed-backup-<stamp>`）。它**故意不保证 SQLite 一致性**——库既然可能已经打不开，唯一还能保存它的方式就是把字节原封不动拷出来。这份"取证镜像"是**全部修复策略都失败时唯一的恢复源**，所以一旦它被拒，repair 直接 **HARD STOP**（宁可不动）。`--check-only` 只看不改、`--no-backup` 才跳过这份备份。
- **不在原地修，改成"受守卫的快照-提升"。** 真正的修复在 **scratch 副本**上跑：先按排他守卫把库快照到 `*.repair-scratch`，在副本上按**从轻到重**的策略阶梯跑（`rebuild_fts` → `reindex_btree` → `dedup_schema` → 最后 `drop_fts_rebuild` 含 `VACUUM`），只有证明"能干净打开"了，才用 **SQLite 在线 backup API 事务性地回写**回主文件。为什么不能原地：最后一条策略是 `VACUUM`，它按"SQLite 还能解析的 schema"重建整个文件——若损坏恰恰在 **schema b-tree**，`VACUUM` 会把挂在那段不可读页上的表**静默丢掉**，越修越少。为什么回写**绝不 `os.replace`**：Windows 会在句柄占用下拒绝替换，POSIX 会把旧句柄留在旧 inode 上；改为通过备份 API 在事务里写入，`state.db` 就永远不会从句柄底下被换掉。
- **修完证明修好了（三段验证）。** 修复的成功不是靠"没报错"判定，而是三条**查询事实**：① `fts_stale` 标记查询应**零行**；② 预期的 FTS 触发器**在场**；③ 权威行数**不得下降**。三条全过才叫修好。

这里有个容易混的点：**repair 里有两种备份，用途和受众完全不同。**

| | raw-copy（`_backup_db_file`） | 在线 `backup()`（`_copy_database_snapshot`） |
|---|---|---|
| 拷贝方式 | 原样拷字节 + 三件 sidecar | SQLite 在线 backup API，事务一致、不换 inode |
| 受众 | **人**（取证 / 人工修复） | **机器**（repair 内部 scratch 的建立与回写） |
| 保证 | **故意不保证** SQLite 一致性 | 保证事务一致 |
| 受 `--no-backup` 控制 | 是 | **否**（内部动作） |

**② 读 Hermes 的代码**

`hermes_state_repair.py:969-981`（`_repair_state_db_schema_locked` 的 docstring，只引这一段）：

```
    Strategies run on a SCRATCH COPY, copied back through SQLite's transactional backup API only once proven to open
    cleanly, so a failed repair cannot modify or lose committed data (a WAL checkpoint of committed frames on guard
    release is not a repair mutation). WHY not in place: the final strategy ends in ``VACUUM``, which rebuilds the
    file from the schema SQLite can still parse — when the damage IS in the schema b-tree (the ``malformed database
    schema ()`` class) every table hanging off the unreadable part is silently dropped, the probe still reports
    malformed, and repair returned ``repaired=False`` having destroyed what it was asked to save.

    The pre-repair backup (#69603) does not close this: it is a forensic artefact that nothing reads back,
    so recovery still depends on a human noticing a ``.malformed-backup-*`` file and knowing what to do with
    it. Not mutating the original in the first place is the property that holds without a human in the loop.
```

**要害在哪**：这段把两个"为什么"钉死了——**为什么先 scratch 再回写**（失败不能污染唯一一份数据），以及**为什么连"先备份"都不够**（取证镜像没人读回来，仍然依赖人）；真正的安全性质是"**压根别去改原件**"。回写那一侧的实现注释在 `hermes_state_repair.py:1018-1019` 逐字补上后半句：`# Never ``os.replace`` the live DB: Windows rejects replacement under open handles and POSIX would leave those handles on the old inode.`

**③ mini 的实现（现状）**

mini 把这条链压到"**能真跑出收据的最小版**"，落在两个新文件：修复引擎 `hermes_state_repair.py`（291 行）与 CLI 外壳 `hermes_cli/sessions_cmd.py`（139 行），并在 `hermes_cli/main.py:88-99` 注册 `sessions` 子命令。

- 健康探针 `_db_opens_cleanly`（`hermes_state_repair.py:61`）：问的是"能不能打开、schema 能不能解析"，**不**跑全量 `PRAGMA integrity_check`——因为"schema 能读"恰恰是允许重建 FTS 的前提，把 FTS 损坏也算成"打不开"会把唯一能修的情形挡在门外。
- raw-copy 备份 `_backup_db_file`（`:153`）：原样拷字节 + 三件 sidecar（`:52`），产物名 `state.db.malformed-backup-<stamp>`（`:168`），同秒避让序号；返回 `(None, reason)` 时 repair 视为 HARD STOP。
- 修复入口 `repair_state_db_schema`（`:226`）：顺序写死 **0 探针（`:254`，打不开就别猜能修）→ 1 备份（`:260`）→ 2 `rebuild_fts()`（`:274`，重建索引 + 装回触发器 + 清标记，整段一个写事务）→ 3 三段验证（`:282`）**。
- 三段验证 `_verify_repair`（`:194`）：跑在**新开的只读连接**上看磁盘事实，判据三条逐字对应文档 §3（`marker_clear` / `triggers_present` / `rows_not_decreased`，`:213-221`）。
- CLI：`cmd_repair`（`hermes_cli/sessions_cmd.py:76`）默认"备份 → 重建 → 打印三段验证"；`--check-only` 走 `_check_only`（`:34`），**四态**：健康退出码 0；FTS 降级 = 1；结构损坏（打不开）= 1；**旧库**（无 `state_meta` 表）单独成一路 = 1。"健康"的口径 = 探针过 **且** 标记无 **且** 触发器齐 **且** 行数读得出。

mini 的**简化点**是：它的修复只做 `rebuild_fts`（**不动 canonical 数据**），所以**不需要 scratch 副本、也不需要把结果提升回主文件**——`_repair_state_db_schema_locked` 那一整层在 mini 里被拿掉，只在 docstring 里逐条写明"真实版有、mini 没有、为什么"。

**④ 与真实版的差别**

| 面 | 真实 Hermes（`f5d1926110`） | mini 现状 |
|---|---|---|
| CLI 面 | `sessions repair`（`--check-only`/`--no-backup`）+ `sessions recover` 整族（`hermes_cli/subcommands/sessions.py:160-207`） | 只有 `sessions repair [--check-only]`；`recover` 及 `--output`/`--allow-partial` 等整族裁掉（B9） |
| 修复算法 | 受守卫的 **snapshot-and-promotion**：scratch 副本 → 策略阶梯 → 在线 backup 事务提升（禁 `os.replace`） | 只做 `rebuild_fts`（**不动 canonical**）⇒ 无需 scratch、无需提升 |
| 备份 | **两种**：raw-copy 取证镜像（`_backup_db_file:478`，受 `--no-backup` 控）＋ 在线 `backup()`（`_copy_database_snapshot:664`，内部） | 只有 raw-copy（`_backup_db_file:153`），且定为**无开关的纪律**（不接受 `--no-backup`） |
| 并发守卫 | 跨进程修复锁 + 排他守卫连接（`_exclusive_repair_db_guard:641`） | 单进程，不做（B1/B2） |
| 策略阶梯 | 4 条（`rebuild_fts`/`reindex_btree`/`dedup_schema`/`drop_fts_rebuild`+VACUUM） | 只留 `rebuild_fts` |
| 三段验证 | 文档 §3 三条 | 逐字实现（`_verify_repair:194`） |
| 修复账本/去重 | `.repair-attempts.json`（`_MAX_PERSISTENT_REPAIR_ATTEMPTS=3`）+ 备份去重 + 保留上限 3 | 不做（纯运维防御；一次真实事故攒了 89GB 雷同备份） |

---

## §八 mini 的实现（整体、契约测试、真跑收据）

本章 mini 侧共落 **7 个既有文件的改动 + 4 个新文件**（提交 `d269953`）。规模对照（mini 锚本章快照 `d269953`，真实侧锚 `f5d1926110`）：

| 文件 | 前 → 后行数（mini） | 性质 | 真实侧行数 |
|---|---|---|---|
| `hermes_state_errors.py` | **0 → 202**（新） | A1 分类谓词 + `StateDbCorruptError` | 264 |
| `hermes_state_fts.py` | **0 → 219**（新） | A2 标记助手 + A3 fail-open + `rebuild_fts()` + A6 | 430 |
| `hermes_state_common.py` | 200 → **270** | A2 `state_meta` 表 + `FTS_STALE_KEY` + 触发器名单 | 1224 |
| `hermes_state.py` | 223 → **417** | A2 开库读标记 + A4 写入环降级 + A7 粘性隔离/守卫 | 1674 |
| `hermes_state_search.py` | 248 → **317** | A5 LIKE 回退接线 + 路径名 + A6 | 1343 |
| `hermes_state_repair.py` | **0 → 291**（新） | A8 修复引擎（探针/体检/备份/三段验证） | 1152 |
| `hermes_cli/sessions_cmd.py` | **0 → 139**（新） | A8 CLI 外壳（`--check-only` 四态） | 1021 |
| `hermes_cli/main.py` | 171 → **175** | A8 注册 `sessions` 子命令 | — |
| `tests/test_state_db_recovery.py` | **0 → 846**（新） | 契约测试 **12 组** | — |
| `tests/test_session_storage.py` | 511 → **514** | A2 连带（`state_meta` 进 `SCHEMA_SQL`，1 条精合集断言 4→5） | — |
| `tests/test_packaging.py` | 417 → **420** | 零第三方依赖白名单加 3 个新模块名 | — |
| `demos/ch11_real_run.py` | **0 → 208**（新） | 真跑脚本（两条损坏路径 + 修复） | — |

真实侧还有四个 mini **没有对应物**的模块：`hermes_state_holders.py`（363）、`hermes_state_schema.py`（1298）、`hermes_cli/doctor_state.py`（491）、`gateway/shutdown_flush.py`（296）——它们服务的是 B 批里"不抄"的那几条（跨进程守卫 / FTS 受守卫重建的节拍接线 / doctor 体检 / 落盘 sink）。

**契约测试：12 组覆盖面**（`tests/test_state_db_recovery.py`，`GROUPS` 见 `:799-823`）

| 组 | 守的承诺 |
|---|---|
| 1. 损坏分类谓词 | **码优先于文本** · FTS 派生损坏 / 结构化损坏 / 无关错误**三组不混** · `StateDbCorruptError` 只是类型 |
| 2. `fts_stale` 标记跨重启 | 标记落在 `state_meta` · 关掉重开库 `_fts_stale` 仍为真 · 触发器不会被 `IF NOT EXISTS` 复活 |
| 3. fail-open 让权威写入继续 | FTS 派生损坏时写入**仍然成功** · `fts_stale='1'` · **三个触发器全被摘** · **从未 rebuild** |
| 4. 降级是持久的 | 同一连接后续写入照常 · 搜索改走 LIKE 且路径名标对 |
| 5. 结构化损坏停写 | 立即失败、类型是 `StateDbCorruptError`、**不重试** · 而且**不**误摘触发器 |
| 6. 不变量：活路径不重建 | 写入与搜索都不跑 `FTS5('rebuild')` · 三处"活路径不许重建"的文字凭证在位 |
| 7. 重建之后 | 标记被清 · 触发器回来 · 索引里那段空窗被补上 · 搜索又走 FTS |
| 8. 隔离与名单 | 库路径唯一来源 = `MINI_HOME` · 摘除/重装看**同一份**触发器名单 |
| **9. 结构损坏隔离（A7）** | **粘性标志**置真 · 之后每次写入**立即失败**且错误是 `StateDbCorruptError` · **不碰文件**（字节指纹不变） |
| **10. close 后拒绝重开（A7）** | 被隔离的句柄 `close()` 后**拒重开** · 健康句柄照常重开 · 关停不 checkpoint 的文字凭证在位 |
| **11. `repair --check-only`（A8）** | 健康库=0 / 降级库=1 / 结构损坏库=1 / **旧库=1** 的判定与退出码 |
| **12. `repair` 真修回来（A8）** | 三段验证（标记零行 · 触发器在场 · 权威行数不降）· 默认留 raw-copy 备份 · 标记清/触发器回/搜索回 FTS |

第 9/10 组用**异常驱动**的结构性损坏（一条形状与现场真错逐字相同的 `sqlite3.DatabaseError("database disk image is malformed")`）；第 12 组的"降级库"也用异常驱动的 FTS 派生损坏——这样 `repair` 的重建能把它修回来（若影子表被**物理**破坏，重建自己就会失败，那不是 §3 要复演的处境）。

**双解释器：121 / 117，差 4 条的成因**

| | `/usr/local/bin/python3` 3.13（SQLite 3.53） | `/usr/bin/python3` 3.9（SQLite 3.51） |
|---|---|---|
| `tests/test_state_db_recovery.py` | **121 通过 / 0 失败**（12 组） | **117 通过 / 0 失败**（12 组） |

- **双解释器全套件 1,309 通过 / 0 失败**（`/usr/local/bin/python3` 3.13）· **1,305 / 0 失败**（`/usr/bin/python3` 3.9）—— 11 个测试文件；差额 4 条同上（3.9 的 SQLite 拒 `DROP messages_fts_data`）。

差 4 条**全部来自第 8 组**那条"**真删 FTS 影子表**"的分支：3.13 允许 `DROP TABLE messages_fts_data` 并真造出 FTS 损坏，于是那一路的 4 条断言成立；3.9 / SQLite 3.51 直接**拒绝**（`table messages_fts_data may not be dropped`），该分支改记为一条"本机 SQLite 禁止删 FTS5 影子表"的实测事实。本次新增的 9~12 组在两边**条数完全一致**。

**反向验证：8 个有效改法 → 干净变红**

把抄来的写法改回旧/坏写法，对应断言应**干净**变红（进程正常退出、`失败项`里出现预期断言名，而不是崩溃）：

| 改法 | 改回什么 | 干净变红 |
|---|---|---|
| RV1 | 不落 `fts_stale` 标记 | 11 条 |
| RV2 | 不摘 FTS 触发器 | 6 条 |
| RV3 | 写入环不重跑同一个 `fn` | 6 条 |
| RV4 | LIKE 回退不接线（条件退回 `has_cjk`） | 3 条 |
| RV5 | 开库不保持摘除 | 2 条 |
| RV6 | 粘性标志不置位 | 9 条 |
| RV7 | 写前守卫失效 | 5 条 |
| RV8 | 拒重开失效 | 1 条 |

**真跑收据**（真造损坏，非模拟；脚本 `demos/ch11_real_run.py`，`MINI_HOME` 指临时目录，**0 失败**）

- **路径 ①·FTS 派生索引损坏**：直接删掉 `messages_fts_data` 的数据页（SQLite 报 `fts5: corruption found reading blob…`，`sqlite_errorcode = 267`）。结果：权威写入**照常成功**；落 `fts_stale='1'`；**三个触发器全被摘**（剩 `[]`）；搜索改走 `LIKE`（路由名 `like_scan_fts_stale`）；**从未 rebuild**。接着 `repair --check-only` 判**不健康**（退出码 1）→ `sessions repair`（备份 → 重建 → 三段验证）→ 标记被清（零行）、三个触发器回来、权威行数 6 → 7（**不降**）、默认留下一份 raw-copy 备份（主文件 + `-wal`/`-shm`）→ 再 `--check-only` 判**健康**（退出码 0）。
- **路径 ②·文件本身损坏**：把第 1 页之后的字节全写成 `0xFF`（schema 页仍是好的）。结果：现场写入抛**类型化错误** `StateDbCorruptError`；之后 3 次写入**全部立即失败**、类型一致；**库 + 边车指纹逐字不变**（停写就是保护）；隔离句柄 `close()` 后**拒绝重开**；`--check-only` 判**不健康**（退出码 1）。

**真跑拓出的两个真问题（全部真实发生）**

**真问题 1 · 旧库缺 `state_meta` 会被误诊——已修 + 补回归测试。** 真跑在**真实 home** 上跑体检时撞出来：第 11 章之前建的库**没有 `state_meta` 表**，旧写法一句 `SELECT value FROM state_meta …` 一抛 `OperationalError: no such table: state_meta` 就把整份体检 early-return 掉 ⇒ **明明在场的三个触发器被报成"缺 3 个"、权威行数报 `None`**。这是**误诊比不诊更坏**：会把人骗去跑修复，或让人以为索引坏了。修法是**逐项隔离**（`hermes_state_repair.py:120-124` 的注释写明这起事故）：读取标记、触发器、行数各自 `try/except`，旧库单列一个病因 `marker_readable=False`，健康判定=「读得到标记 ∧ 标记不在场 ∧ 触发器齐 ∧ 行数读得出」（`:146-148`）；CLI 侧把"旧库"打印成独立结论（`hermes_cli/sessions_cmd.py:50-52` / `:69-71`）。回归测试 `test_legacy_db_without_state_meta_is_diagnosed_not_confused`（`tests/test_state_db_recovery.py:764`）逐条钉住：不崩、触发器**照旧数得对**（在场 3，不是被误报成缺 3）、行数**读得出来**、标记读不到 ⇒ 单列病因、判**不健康**（旧库待 repair 升级）。

**真问题 2 · 一种**任何一张词表都认不出**的现场错误：`vtable constructor failed`。** 路径 ② 里，若先走 `append_message`，SQLite 撞上的不是"malformed"，而是 `DatabaseError: vtable constructor failed: messages_fts`（`sqlite_errorcode = 11`）——因为要编译 FTS 同步触发器就得构造 FTS5 虚表。这行文案**既不含 FTS 损坏词、也不含结构损坏词**，上游词表 `("malformed", "file is not a database", "not a database", "database corruption")`（`hermes_state_errors.py:88-90`）同样认不出 ⇒ **两个桶都不进 ⇒ 原样抛**（`demos/ch11_real_run.py:145-155` 如实记为"与上游同边界，非 mini 缺陷"）。**这才是 §2 的场景**：改走**不牵动 FTS vtable** 的写法（`create_session` 只碰 `sessions` 表），SQLite 读坏页得到 `database disk image is malformed`（含词表里的 `malformed`）⇒ 三桶归位、抛 `StateDbCorruptError`。另有一条**开库侧**的边界同样如实记下：新句柄 open 撞上"文件根本不是库"时抛的是 SQLite 裸错（`DatabaseError`）——上游 §2 的隔离判定接在 **live write** 上，开库这一侧不在它范围内（这条进差异表）。

---

## §九 差距清单（A 批 / B 批）

**口径**：mini 侧已实现（提交 `d269953`），行号指该提交后的树；真实侧锚 `f5d1926110`。A 批 = 已抄（只抄"决定架构形状"的）；B 批 = **不抄**，每条给理由。

### A 批（已抄，9 条）

| # | 机制 | 真实 `文件:行号` | mini 落点 | 判据 |
|---|---|---|---|---|
| **A1** | **损坏分类谓词**：有码只看 `SQLITE_CORRUPT_VTAB=267`，无码才让文本说话（须命中 `messages_fts\w*` + `fts5:…corrupt` 词表） | `hermes_state_errors.py:100-122`（`is_fts_scoped_corruption_error`）+ 词表 `:88-90` + bucket `:256-257` | `hermes_state_errors.py:82`（+ 词表 `:38`、`classify_persistence_error:115`、`is_structural_corruption_error:149`） | ✅ 已对齐（d269953） |
| **A2** | **`state_meta` 表 + `fts_stale` 持久标记**（"降级中"跨进程/跨重启可见的唯一载体） | `hermes_state_common.py:878-880`（`FTS_STALE_KEY`）+ `hermes_state_fts.py:365-369`（写 `value='1'`） | `hermes_state_common.py:175`（表，纯加免迁移）+ `:70`（`FTS_STALE_KEY`）+ `hermes_state_fts.py:92/106`（读/写助手） | ✅ 已对齐（d269953） |
| **A3** | ★ **fail-open：同一事务摘触发器 + 落标记**（"同一个事务"是它自己开的那个 `BEGIN IMMEDIATE`，不是那次失败的写入事务） | `hermes_state_fts.py:353-403`（`_enter_fts_fail_open`；`BEGIN IMMEDIATE:363` → 标记 `:365-369` → `_drop_all_fts_triggers:382` → commit `:383`） | `hermes_state_fts.py:146`（`enter_fts_fail_open`）+ 不变量 docstring `:155-166` | ✅ 已对齐（d269953） |
| **A4** | **写入环降级重试**：fail-open 为真 ⇒ `continue` 重跑同一个 `fn`；只有**结构化**损坏才停 | `hermes_state.py:1020-1035`（`except sqlite3.DatabaseError` → FTS 分岔 → `_is_structural_corruption_error` → `_halt_db_corrupt`） | `hermes_state.py:195`（`_execute_write`，`DatabaseError`/`OperationalError` 双分支）+ `:261`（`_degrade_or_raise_on_corruption`） | ✅ 已对齐（d269953） |
| **A5** | **`LIKE` 回退接线**：损坏降级时也走 LIKE（路径名 `like_scan_fts_stale`） | `hermes_state_search.py:979-989`（入口）+ `:1085-1087`（条件 `self._fts_stale`）+ `:884-885`（路径名） | `hermes_state_search.py:173`（条件 `has_cjk(query) or self._fts_stale`）+ `:178`（路径名）+ `_describe_search_path:191` | ✅ 已对齐（d269953） |
| **A6** | **不变量注释（活路径绝不 rebuild）**：三处逐字文字凭证 | `hermes_state.py:1028-1029`、`hermes_state_search.py:918-920`、`hermes_state_fts.py:354-356` | `hermes_state_fts.py:50-64`（模块 docstring）+ `hermes_state.py:181-185` + `hermes_state_search.py:26-32` | ✅ 已对齐（d269953） |
| **A7** | ★ **结构损坏隔离（停写就是保护）**：置粘性标志、不重试、写前守卫立即失败、拒重开、`close()` 跳检查点（现场：50 分钟重写 ⇒ 15 页写到错页号 ⇒ 文件再也打不开） | `hermes_state_errors.py:186`（`StateDbCorruptError`）→ `_halt_db_corrupt:1249` → 写前守卫 `_raise_if_db_corrupt:1340`（调用点 `:942`）→ 拒重开 `_reopen_after_close_locked:888/900-904` → `close():1449-1457` | `hermes_state.py:130`（粘性标志）+ `:304`（`_halt_db_corrupt`）+ `:319`（写前守卫）+ `:329`（`_reopen_after_close`）+ `:382`（`close()` 显式不 checkpoint）；`close()` 在 mini 本就裸关 ⇒ 变注释 + 文字凭证 | ✅ 已对齐（d269953） |
| **A8** | **`sessions repair`（最小版：备份 + 重建 + 三段验证）** | CLI `hermes_cli/subcommands/sessions.py:160-207`；入口 `hermes_cli/sessions_cmd.py:101/144`、校验 `:156-168`；`repair_state_db_schema:849`；raw-copy `_backup_db_file:478` | `hermes_state_repair.py:226` + `hermes_cli/sessions_cmd.py:34/76/128` + `hermes_cli/main.py:88-99` 注册 | ✅ 已对齐（d269953） |
| **A9** | （可选）**`doctor` 加一条库体检**（让"降级中"这件事可见） | `hermes_cli/doctor_state.py:206-212`（文案）+ `:241-247`（判定 `state_db_has_structural_damage`） | 无（`doctor.py` 仍是纯网络体检，grep `state\|db\|sqlite` 零命中） | ⬜ 没做（量级 ~20 行；本章按任务不做） |

### B 批（不抄，9 条）

| # | 机制 | 真实 `文件:行号` | 不抄理由 |
|---|---|---|---|
| **B1** | 跨进程准入锁（flock `<db>.fts_rebuild.lock`，超时 120s） | `hermes_state_common.py:1168-1224` | mini 未实现跨进程重建准入锁；独立 CLI 与网关可能同时持库，显式修复前必须停掉其他持有者 |
| **B2** | 外部持有者守卫（扫 `/proc/<pid>/fd` 比 db/`-wal`/`-shm` inode） | `hermes_state_holders.py:187-232` | Linux 专属 + 多进程侦查；mini 的 CLI 与网关可能同时持库；当前未实现上游的外部持有者扫描 |
| **B3** | 长命网关的退避重试（60s→×2→3600s + housekeeping tick 60s） | `hermes_state_schema.py:561-620` + `gateway/run.py:4566-4579/4650/4662` | mini 已有常驻网关与巡检，但尚未接入受守卫的 FTS 重建退避路径 |
| **B4** | 替代落盘 sink（JSONL + `pending_messages/` 暂存池） | `session_persistence.py:256-262`、`session_transcript.py:140-160/238-240`、`shutdown_flush.py:37/117` | 需要 mini **完全没有**的 sink 基座（第 7 章差距清单已挂"未做"）；隔离机制本身不依赖它 |
| **B5** | 内部在线备份 API 快照（`source.backup(pages=256,…)` + 进度/截止） | `hermes_state_repair.py:664/684` | mini 用 raw-copy 备份就够；`pages=`/进度/截止是给长库修复用的 |
| **B6** | `state-snapshots/` 快照目录与快速回滚（`/snapshot list` + `restore`） | `hermes_cli/backup.py:37/1212/1255/1302` | 运维设施，与"恢复机制的形状"无关 |
| **B7** | 第二/第三套 FTS 索引 + 6 触发器全集（含 trigram 三个） | `hermes_state_common.py:321-322` | 纯规模：mini 一套 bigram + 3 触发器已够本机制演示（**差异表已记明**） |
| **B8** | `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`（3.12+，关掉 SQLite 自己的关停检查点） | `hermes_state.py:405-408/1267/1275-1281` | mini 不发显式 checkpoint，但 SQLite 自动关停 checkpoint 仍可能发生；当前未关闭它，第三道防线尚未满足 |
| **B9** | 参数面与命名细节（`--output` / 备份名 `state.db.malformed-backup-<stamp>` / `--no-backup` 语义） | `hermes_cli/subcommands/sessions.py:166-207` | 细节；A8 保留 `--check-only` 与"默认先备份"两条即可 |

---

## 附录 A · 从正文挪过来的引用

> 本附录把 §一~§九 正文放不下、或不必塞进「② 读 Hermes 的代码」单块的 `文件:行号` 集中归档，**按文件分组**。每条给：`文件:行号` + 该行/该段首 20 字 + 一句它证明什么。
>
> - **真实侧**锚 `f5d1926110`：`git -C ~/.hermes/hermes-agent show f5d1926110:<path> | sed -n 'Np'`
> - **mini 侧**锚本章快照（`d269953`）：`sed -n 'Np' mini-hermes/<path>`
> - 全部行号**逐条打印核对过**；括号里的说明只是「它在说什么」，不当逐字引文用。

### 真实侧（锚 `f5d1926110`）

**`hermes_state_errors.py`（264 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:88` | `_DB_CORRUPTION_MARKE` | 损坏**词表**元组定义处（`malformed` / `not a database` 等文本 marker 的唯一来源）。 |
| `:100-122` | `def is_fts_scoped_co` | FTS 归因谓词 `is_fts_scoped_corruption_error`：**码优先于文本**——`SQLITE_CORRUPT_VTAB` 直接算 FTS 范围，裸 `CORRUPT`/`NOTADB` 因无对象范围而 fail closed；无码时才看文本。 |
| `:186` | `class StateDbCorruptError` | 类型化损坏错误 `StateDbCorruptError(sqlite3.DatabaseError)`——隔离时抛出的那个类。 |
| `:231` | `(_DB_CORRUPTION_MARK` | 归因**桶表**里把损坏词表映射到 `"corrupt"` 桶的那一行。 |
| `:236` | `def classify_persist` | 粗粒度归因函数 `classify_persistence_error`：`locked` / `fts_index` / `corrupt` / `disk` / `unknown`。 |

**`hermes_state_fts.py`（430 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:353-403` | `def _enter_fts_fail_` | fail-open 全流程 `_enter_fts_fail_open`：同一事务里落 `fts_stale` 面包屑 + DROP 同步触发器 + commit；**绝不 rebuild**。 |
| `:354-356` | `"""Detach corrupt FT` | 该函数的 docstring 三行（逐字）：*"Detach corrupt FTS indexes so canonical writes can continue. Breadcrumb + trigger drop commit atomically: once triggers are absent the index has a gap of unknown extent, so nobody may reinstall them without a full rebuild."* |

**`hermes_state.py`（1674 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:942` | `self._raise_if_db_co` | 写入重试环 `while True:` **顶部**的写前守卫调用点——已隔离就立刻抛，碰都不碰文件。 |
| `:1020-1035` | `if isinstance(exc, s` | 写入环 `except` 里的降级分支：先探「文件被换掉」，再 fail-open，最后才判定结构损坏。 |
| `:1033-1034` | `if self._is_structur` | 环内最后一道：结构损坏 ⇒ `self._halt_db_corrupt(exc)`（停写就是保护）。 |
| `:1235-1247` | `def _is_structural_c` | 结构谓词 `_is_structural_corruption_error`（docstring 逐字：*Bare SQLITE_CORRUPT/NOTADB with no FTS provenance: canonical B-tree/schema/freelist damage, never repairable from the live write path.*）。 |
| `:1249-1267` | `def _halt_db_corrupt` | 隔离句柄 `_halt_db_corrupt`：置 `_db_corrupt`、关关停检查点、抛 `StateDbCorruptError`；*never run in-file repair here*。 |
| `:1340-1343` | `def _raise_if_db_cor` | 写前守卫本体 `_raise_if_db_corrupt`：`if self._db_corrupt: raise self._corrupt_error()`。 |
| `:888-904` | `def _reopen_after_cl` | `close()` 后重开守卫 `_reopen_after_close_locked`：被隔离的句柄**拒绝**重开（注释:899 *a quarantined handle must never hand a fresh connection to a damaged file*）。 |
| `:1449-1457` | `retire_without_close` | `close()` 里**跳过**关停 WAL checkpoint 的分支（隔离/代际丢失时）。 |
| `:405-408` | `def _close_time_chec` | 能力探测 `_close_time_checkpoint_configurable`：3.12+ 有 `setconfig` + `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`。 |
| `:1267` | `def _disable_close_t` | `_disable_close_time_checkpoint` 定义行（隔离时关掉 SQLite 自己的最后连接检查点）。 |
| `:1275-1281` | `flag = getattr(sqlit` | 上述函数体内取 `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE` + 逐实例 `setconfig` 的段落。 |

**`hermes_state_common.py`（1224 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:321-322` | `_FTS_TRIGGERS = ("me` | 同步触发器**名单 6 个**：base 3 + trigram 3（`messages_fts_{insert,delete,update}` + `messages_fts_trigram_{...}`）。 |
| `:878-880` | `# Set when a base/tr` | `FTS_STALE_KEY = "fts_stale"`（注释逐字：detached 后 *startup must rebuild the complete index before reinstalling sync triggers*）——持久标记的键名与理由。 |
| `:1168-1224` | `@contextlib.contextm` | 跨进程**准入锁** `fts_rebuild_admission`（contextmanager）：拿不到权威就 fail closed、让面包屑保留可重试。 |

**`hermes_state_search.py`（1343 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:918-920` | `substring-capable ro` | 注释：损坏类 `DatabaseError` 在那条路径上会触发 `_enter_fts_fail_open` 并从权威行应答——**a live search never runs the unbounded rebuild**。 |
| `:979-989` | `def _search_messages` | LIKE 回退本体 `_search_messages_like_fallback`（*Search canonical messages while derived FTS state is stale.*）。 |
| `:1085-1087` | `if self._fts_stale:` | 搜索路由判据：`_fts_stale` ⇒ 直接切 LIKE 回退。 |

**`hermes_state_holders.py`（363 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:187-232` | `def foreign_state_db` | 外部持有者守卫 `foreign_state_db_holders`：扫 `state.db` 及其 `-wal`/`-shm` 的**foreign holders**，扫描失败按「未知持有者」处理（结构性维护不得假设静默）。 |

**`hermes_state_schema.py`（1298 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:545-559` | `def _recover_stale_f` | 受守卫重建 `_recover_stale_fts`：先查 foreign holders，再在 `fts_rebuild_admission` 下重建；拿不到权威就 fail closed。 |
| `:561-620` | `def retry_deferred_f` | 长命网关的**进程内重试** `retry_deferred_fts_recovery`（docstring 逐字：*a gateway opens state.db once for days, so "next open" never comes*）。 |
| `:958-963` | `fts5_available = sel` | **开库保持摘除**：读 `state_meta` 的 `fts_stale` 行 ⇒ 置 `_fts_stale` 并 `_drop_all_fts_triggers`（防止 DDL 把触发器复活）。 |

**`hermes_state_repair.py`（1152 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:478-479` | `def _backup_db_file(` | 面向用户的备份是 **raw-copy**（docstring: *Raw-copy a (possibly malformed) DB plus sidecars*）。 |
| `:664` | `def _copy_database_s` | 内部快照 `_copy_database_snapshot` 定义——**唯一**用 SQLite 在线备份 API 的地方。 |
| `:684` | `source.backup(destin` | 在线备份 API 调用点 `source.backup(destination, pages=256, ...)`——属**内部快照**，不受 `--no-backup` 控制。 |
| `:849` | `def repair_state_db_` | `repair_state_db_schema(db_path, *, backup: bool=True)`——三段式修复入口。 |
| `:855` | `transactionally, so` | 其 docstring 逐字：*... A raw backup is taken first*——**再次**把「面向用户的备份」定性为 raw。 |
| `:982` | `scratch = db_path.wi` | 快照-提升路径：`*.repair-scratch` 暂存文件。 |
| `:1008` | `_copy_database_snaps` | 活库 → scratch 的快照拷贝（`_copy_database_snapshot(db_path, scratch, ...)`）。 |
| `:1018` | `# Never ``os.replace` | 注释：**绝不** `os.replace` 活库（Windows 拒、POSIX 会穿过过期 WAL/shm 假设）。 |
| `:1021` | `_copy_database_snaps` | 提升方向：scratch → 活库的快照拷贝（`_copy_database_snapshot(scratch, db_path, ...)`）。 |

**CLI / doctor / 网关 / 测试**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `hermes_cli/subcommands/sessions.py:166` | `_flag(sessions_repai` | `sessions repair` 的 `--check-only` 开关定义处。 |
| `hermes_cli/subcommands/sessions.py:197` | `_flag(sessions_recov` | `sessions recover` 的 `--inspect-only` 开关定义处。 |
| `hermes_cli/sessions_cmd.py:101` | `def _cmd_repair(args` | `sessions repair` 命令实现入口。 |
| `hermes_cli/sessions_cmd.py:115` | `report = repair_stat` | `--no-backup` 门控点：`repair_state_db_schema(db_path, backup=not getattr(args, "no_backup", False))`。 |
| `hermes_cli/sessions_cmd.py:144` | `def _cmd_recover(arg` | `sessions recover` 命令实现入口。 |
| `hermes_cli/sessions_cmd.py:156-168` | `usage_errors = (` | recover 的参数互斥校验（`--output` 与 `--inspect-only`/`--allow-partial` 不可同时用；报告不可覆盖）。 |
| `hermes_cli/doctor_state.py:196-200` | `_STATE_DB_REPAIRS = ` | doctor 报出的 FTS 修复文案表（含 *run 'hermes doctor --fix' (or 'hermes sessions repair')*）。 |
| `hermes_cli/doctor_state.py:206-212` | `_STATE_DB_STRUCTURAL` | doctor 对**结构损坏**的处置文案：停网关 → `sessions recover --inspect-only` → 视情 `--output`。 |
| `hermes_cli/doctor_state.py:241-247` | `from hermes_state_re` | doctor 用 `state_db_has_structural_damage` 探结构损坏并调 `_repair_state_db(..., "structural")`。 |
| `hermes_cli/backup.py:37` | `_QUICK_SNAPSHOTS_DIR` | `_QUICK_SNAPSHOTS_DIR = "state-snapshots"`——§2 提到的快照目录名。 |
| `gateway/shutdown_flush.py:5` | `All hooks write atom` | 模块 docstring：hooks 把原子 JSON 写到 `<hermes_home>/pending_messages/`（§2 的暂存池）。 |
| `gateway/shutdown_flush.py:37` | `flush_dir = get_herm` | `pending_messages/` 目录的实际拼接点。 |
| `gateway/shutdown_flush.py:117` | `def spool_dropped_tr` | `spool_dropped_transcript_message`——转写本落到暂存池的那条路径。 |
| `gateway/run.py:4566-4579` | `def _housekeeping_de` | 网关 housekeeping tick 上的 `_housekeeping_deferred_fts_retry`（docstring：*the gateway stays up for days*）。 |
| `gateway/run.py:4650` | `(1, "Deferred FTS re` | 该 chore 登进 housekeeping 表（槽位 1）。 |
| `gateway/run.py:4662` | `_housekeeping_chore(` | tick 里 `_housekeeping_chore(label, fn)` 的实际派发。 |
| `tests/hermes_state/test_state_db_corrupt_quarantine.py:3-7` | `Field evidence (the` | §2「约 50 分钟 / 15 页 / `messages_fts_trigram_data` 叶子」现场证据的出处（逐字）。 |

### mini 侧（锚本章快照 `d269953`）

**`hermes_state.py`（417 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:114-124` | `self._write_count = ` | `_fts_enabled`（本进程还愿不愿意同步）与 `_fts_stale`（权威表里有没有空窗）两个标志的**注释块**；两份职责分工写死在这。 |
| `:138-151` | `# ★ 开库时**读一次**降级` | **开库保持摘除**：读 `state_meta` 标记 ⇒ 置 `_fts_stale` 且 `drop_all_fts_triggers()`（防 DDL 复活触发器）；对应真实 `hermes_state_schema.py:958-963`。 |
| `:226` | `self._raise_if_db_co` | 写入重试环 `while` 顶部调 `_raise_if_db_corrupt()`（写前守卫调用点，对齐真实 `:942`）。 |
| `:304-317` | `def _halt_db_corrupt` | 隔离句柄 `_halt_db_corrupt`：**粘性**置位 + 抛错 + 绝不做就地修复（docstring 引真实 `:1249-1267`）。 |
| `:319-327` | `def _raise_if_db_cor` | 写前守卫 `_raise_if_db_corrupt`（对齐真实 `:1340-1343`）。 |
| `:329-351` | `def _reopen_after_cl` | `_reopen_after_close`：健康句柄照常重连、被隔离句柄**拒重开**（对齐真实 `_reopen_after_close_locked :888-904`）。 |
| `:382-407` | `def close(self) -> N` | `close()`：**绝不** WAL checkpoint（对齐真实 `:1449-1457` 的「跳过」分支）；`SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE` 标为不实现（B8）。 |

**`hermes_state_fts.py`（219 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:92-122` | `def _read_fts_stale_` | 标记三件套 `_read/_write/_clear_fts_stale_marker`（读 `state_meta`；写走 upsert；清 = 删行）。 |
| `:136` | `def drop_all_fts_tri` | `drop_all_fts_triggers`——fail-open 与「开库保持摘除」共用的摘除动作。 |
| `:146-182` | `def enter_fts_fail_o` | fail-open 主体 `enter_fts_fail_open`：门禁只对 FTS 归因损坏动手；`BEGIN IMMEDIATE` → 落标记 → 摘触发器 → commit，**绝不 rebuild**（对齐真实 `:353-403`）。 |
| `:187` | `def rebuild_fts(self` | `rebuild_fts`——**只**能被显式恢复路径调用，证明「活路径不 rebuild」是结构性的。 |

**`hermes_state_errors.py`（202 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:82` | `def is_fts_scoped_co` | mini 版 FTS 归因谓词（对齐真实 `:100-122`）——**码优先于文本**的规则逐字照抄。 |
| `:115` | `def classify_persist` | mini 版 `classify_persistence_error`（真实 `:236-264` 的子集；判定顺序：provenance 先于文本、corrupt 先于 lock/disk）。 |
| `:149` | `def is_structural_co` | mini 版结构谓词（提为模块函数，判定式照抄真实 `:1235-1247`）。 |
| `:173` | `class StateDbCorrupt` | mini 的 `StateDbCorruptError`（对齐真实 `:186`；继承 `sqlite3.DatabaseError`，docstring 引 15 页现场教训）。 |

**`hermes_state_common.py`（270 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:57` | `_FTS_TRIGGERS = (` | mini 的触发器名单**只有 base 3 个**（真实是 6 个）；注释点明「真实版是 6 个，mini 只有 base 一套」。 |
| `:70` | `FTS_STALE_KEY = "fts` | `FTS_STALE_KEY = "fts_stale"`（对齐真实 `:878-880`）。 |
| `:175-178` | `CREATE TABLE IF NOT ` | `state_meta(key TEXT PRIMARY KEY, value TEXT)` 建表——第 11 章新增、持久标记的载体。 |

**`hermes_state_search.py`（317 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:173` | `if has_cjk(query) or` | 搜索路由：**CJK 或 `_fts_stale`** 都走 LIKE（第 11 章新增了后者这条理由）。 |
| `:178` | `route = "like_scan_f` | 降级在服务时把 `route` 标成真实版那个名字 `like_scan_fts_stale`（中文路仍保持 `"like"` 契约）。 |

**`hermes_state_repair.py`（291 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:87` | `def inspect_state_db` | 只读体检 `inspect_state_db`（`--check-only` 用）：健康探针 / 标记 / 触发器集合 / 权威行数；**逐项隔离**，一条读不了不带崩整份。 |
| `:153` | `def _backup_db_file(` | mini 的备份 `_backup_db_file`——raw-copy 语义（对齐真实 `:478`）。 |
| `:180` | `return None, "raw-c` | raw-copy 失败文案，坐实「此备份是原始字节拷贝」。 |
| `:194` | `def _verify_repair(d` | 三段验证 `_verify_repair`：标记零行 / 触发器齐 / 权威行数不降。 |
| `:226` | `def repair_state_db_` | `repair_state_db_schema(db_path, *, backup=True)`——备份 → 重建 → 验证入口。 |

**`hermes_cli/sessions_cmd.py`（139 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:34` | `def _check_only(db_p` | `_check_only`——`--check-only` 的人类可读三态打印（健康 / FTS 降级 / 结构损坏 + 旧库）。 |
| `:76` | `def cmd_repair(args)` | `cmd_repair`——`mini sessions repair [--check-only]` 入口（对齐真实 `_cmd_repair :101`）。 |
| `:128` | `def add_sessions_par` | `add_sessions_parser`——`sessions` 子命令注册处（新章新增）。 |

**测试与真跑**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `tests/test_state_db_recovery.py:799-825` | `GROUPS = [` | 12 组契约测试登记表（846 行 / 12 组）；组名即验收面。 |
| `demos/ch11_real_run.py`（208 行） | （脚本） | 两条损坏路径 + 修复的真跑 demo，收据见 §八。 |

---

## 附录 B · 三方对照矩阵

> 列：`机制 | 真实 Hermes（文件:行号）| mini | 精译-11 节位`。真实侧锚 `f5d1926110`，mini 侧锚 `d269953`；`——` 表示 mini 无对应实现。

| 机制 | 真实 Hermes（文件:行号） | mini | 精译-11 节位 |
|---|---|---|---|
| **① 损坏分类（词表 + FTS 归因谓词）** | `hermes_state_errors.py:88`（词表）`：100-122`（谓词：码优先于文本） | `hermes_state_errors.py:38`（词表）`：82-113`（谓词，规则照抄） | §1、§2 |
| **② fail-open：同事务落标记 + 摘触发器** | `hermes_state_fts.py:353-403`（docstring `:354-356`） | `hermes_state_fts.py:146-182` | §1、§二 |
| **③ 持久 `fts_stale` 标记** | `hermes_state_common.py:878-880`（键名 + 理由） | `hermes_state_common.py:70` + 建表 `:175-178` | §1、§3 |
| **④ 同步触发器名单** | `hermes_state_common.py:321-322`（**6 个** = base 3 + trigram 3） | `hermes_state_common.py:57`（**3 个** = base） | §3 |
| **⑤ 搜索降级走 LIKE** | `hermes_state_search.py:1085-1087`（判据）+ `:979-989`（回退本体） | `hermes_state_search.py:173,178`（`like_scan_fts_stale`） | §1、§三 |
| **⑥ 活路径绝不 `FTS5('rebuild')`** | `hermes_state_search.py:918-920`、`hermes_state.py:1028-1029`、`hermes_state_fts.py:354-356` | `hermes_state_fts.py:40-54`（模块 docstring 三处凭证） | §1、§三 |
| **⑦ 写前守卫（停写）** | `hermes_state.py:942`（调用点）`：1340-1343`（本体） | `hermes_state.py:226`（调用点）`：319-327`（本体） | §2、§五 |
| **⑧ 结构损坏隔离（类型 + halt + 结构谓词）** | `hermes_state_errors.py:186`（类型）；`hermes_state.py:1235-1247`（谓词）`：1249-1267`（`_halt_db_corrupt`） | `hermes_state_errors.py:149`（谓词）`：173`（类型）；`hermes_state.py:304-317`（halt） | §2、§五 |
| **⑨ `close()` 跳过 WAL 检查点** | `hermes_state.py:1449-1457` | `hermes_state.py:382-407` | §2、§五 |
| **⑩ `close()` 后拒重开** | `hermes_state.py:888-904`（`_reopen_after_close_locked`） | `hermes_state.py:329-351`（`_reopen_after_close`） | §2、§五 |
| **⑪ `NO_CKPT_ON_CLOSE`（3.12+）** | `hermes_state.py:405-408`（探测）`：1267`（入口）`：1275-1281`（设置） | ——（**B8 不抄**，理由见 `hermes_state_errors.py:173` docstring） | §2、§五 |
| **⑫ 开库保持摘除** | `hermes_state_schema.py:958-963` | `hermes_state.py:138-151` | §1、§四 |
| **⑬ 受守卫重建（准入锁 + 外部持有者守卫）** | `hermes_state_schema.py:545-559`；`hermes_state_common.py:1168-1224`；`hermes_state_holders.py:187-232` | ——（**B 批不抄**：缺跨进程基座） | §1、§四 |
| **⑭ 长命网关的进程内重试** | `hermes_state_schema.py:561-620`；`gateway/run.py:4566-4579`（tick）`：4650`（槽位） | ——（**B 批不抄**：无 housekeeping tick 对应面） | §1、§四 |
| **⑮ 显式 repair（备份 → 重建 → 三段验证）** | `hermes_state_repair.py:849`（入口）`：478-479`（raw 备份）`：664/684`（内部快照）`：982/1008/1018/1021`（快照-提升） | `hermes_state_repair.py:226`（入口）`：153`（raw 备份）`：194`（三段验证） | §3、§七 |
| **⑯ CLI 出口（`sessions repair`/`recover` + doctor）** | `hermes_cli/subcommands/sessions.py:166/197`；`hermes_cli/sessions_cmd.py:101/115/144/156-168`；`hermes_cli/doctor_state.py:196-200/206-212/241-247` | `hermes_cli/sessions_cmd.py:34/76/128`（仅 `repair`；无 recover、doctor 不碰库） | §2、§3、§七 |

---

## 附录 C · 上游文档与源码的出入

> 核对锚：上游 commit `f5d1926110`（v0.21.3）。以下三条均已在本轮**逐条 `git show f5d1926110:<path> | sed -n` 真身打印**确认行号与文字相符。

**1. §3 把 `sessions repair` 的默认备份称为 "a SQLite backup"，源码里它是「原始字节拷贝」（raw-copy）。**

```console
$ git -C ~/.hermes/hermes-agent show f5d1926110:hermes_state_repair.py | sed -n '855p'
    transactionally, so canonical rows are never modified by a failed attempt. A raw backup is taken first
$ git -C ~/.hermes/hermes-agent show f5d1926110:hermes_state_repair.py | sed -n '478,479p'
def _backup_db_file(db_path: Path) -> "Tuple[Optional[Path], Optional[str]]":
    """Raw-copy a (possibly malformed) DB plus sidecars to a timestamped backup.
$ git -C ~/.hermes/hermes-agent show f5d1926110:hermes_cli/sessions_cmd.py | sed -n '115p'
    report = repair_state_db_schema(db_path, backup=not getattr(args, "no_backup", False))
$ git -C ~/.hermes/hermes-agent show f5d1926110:hermes_state_repair.py | sed -n '684p'
        source.backup(destination, pages=256, progress=_check_deadline, sleep=_REPAIR_LOCK_POLL_SECONDS)
```

- 原文 §3 逐字：*"`sessions repair` creates a **SQLite backup** by default"*。
- 源码：面向用户、由 `--no-backup` 门控的那份备份是 **raw-copy**——`hermes_state_repair.py:855` 逐字 *"A raw backup is taken first"*，实现 `:478-479` 逐字 *"**Raw-copy** a (possibly malformed) DB plus sidecars to a timestamped backup."*，门控点 `hermes_cli/sessions_cmd.py:115` 正是 `backup=not ...no_backup`。
- 全仓唯一一次 SQLite **在线备份 API**（`source.backup(...)`）在 `hermes_state_repair.py:684`，属**内部修复快照** `_copy_database_snapshot`（`:664`），**不受** `--no-backup` 控制、也不是 CLI 告知用户的那份。
- **结论**：术语层面**对不上**——面向用户的「备份」是原始字节拷贝，而非文档所说的 "SQLite backup"。

**2. §3 的触发器验证 SQL 只列了 3 个名字，而 schema 定义的是 6 个。**

```console
$ git -C ~/.hermes/hermes-agent show f5d1926110:hermes_state_common.py | sed -n '321,322p'
_FTS_TRIGGERS = ("messages_fts_insert", "messages_fts_delete", "messages_fts_update",
                 "messages_fts_trigram_insert", "messages_fts_trigram_delete", "messages_fts_trigram_update")
```

- 原文 §3 的验证 SQL 逐字：`WHERE name IN ('messages_fts_insert','messages_fts_update','messages_fts_delete')`，正文接着说 *"the expected FTS triggers should be present"*。
- 源码 `hermes_state_common.py:321-322` 是 **6 个**：`messages_fts_{insert,delete,update}` + `messages_fts_trigram_{...}`。
- **结论**：那条「预期触发器都在场」的验证 SQL **看不到 trigram 那三个**，对 trigram 索引而言**验证不完全**（mini 只有 base 三个，故 mini 侧清单恰好三条——差异见附录 B ④）。

**3. §1 把重建只归到 "a later `SessionDB` open"，漏了长命网关的进程内重试（不完整，非矛盾）。**

```console
$ git -C ~/.hermes/hermes-agent show f5d1926110:hermes_state_schema.py | sed -n '562,565p'
    """Retry a deferred stale-FTS rebuild (gateway housekeeping tick). ``_recover_stale_fts``
    fails closed at open, leaving search on LIKE; live write/search paths must never
    start a full rebuild, and a gateway opens state.db once for days, so "next open"
    never comes. Bounded doubling backoff, non-blocking admission, no new thread. True
```

- 原文 §1 逐字：*"a later `SessionDB` open may rebuild under the cross-process admission lock"*。
- 源码 `hermes_state_schema.py:562-565`（`retry_deferred_fts_recovery` 的 docstring）逐字含：*"a gateway opens state.db once for days, so "next open" never comes"*；派发点在网关 housekeeping tick（`gateway/run.py:4566-4579`，槽位 `:4650`）。
- **结论**：重建并不只发生在「下一次打开」——长命网关**根本不会再打开**，故另有 tick 上的 `retry_deferred_fts_recovery` 兜底。原文只提「下一次打开」，**操作含义上不完整**。
