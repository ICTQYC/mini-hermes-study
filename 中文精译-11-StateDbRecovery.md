# 中文精译 · 第 11 章：状态库与 FTS 恢复（State DB Recovery）

> **本篇定位**：精译为主（看懂"`state.db` 的 FTS 索引、或者文件本身坏掉时，Hermes 怎么恢复"）。它是第 6 章《会话存储》的**配套篇** —— 同一份 `state.db` 的故障表现与自愈路径；差异对比、源码深挖与实现日志留给《源码讲解-11》。
> **来源**：`官方文档-Developer-Guide/state-db-recovery.md`（**107 行**，H1 + 3 个 H2；副标题逐字：*How Hermes recovers state.db when the FTS index or the file itself is corrupt*）。
> **上游**：commit `f5d1926110` · `version = "0.21.3"` · 原文 **107 行**

> mini 当前维护说明见对应《源码讲解》的“发布前维护”节；体检额外验证路径 URI 编码及 FTS MATCH 可读性，结果不代表完整性证明。原文翻译与历史实测不代替当前实现契约。

[TOC]

---

## 0. 这一章在回答什么

`state.db` 存着**两类不同的数据**：

- `sessions` 与 `messages` 是**权威（canonical）转写本**，这里指会话消息记录，不特指语音转写。
- `messages_fts*` 这些表以及它们的同步触发器，是**派生索引**。

派生索引**允许被临时摘除**。但它们**绝不能**把一次现场的消息写入或搜索，变成一次**没有上界的整本转写本重建**。

> 📌 **译注（补充，2026-10-12）：一句话** —— **权威数据（转写本）要保住、要能继续写；派生数据（FTS 索引）坏了可以退场、以后再建。**（原文没有这一句，是本篇为便于抓重点加的概括。）

---

## 1. Live behavior when FTS is corrupt —— FTS 损坏时的现场行为

如果一次 FTS 写入或搜索报出损坏错误类，`SessionDB` 会：

1. 记下那条**持久**的 `fts_stale` 标记；
2. 在**同一个事务**里移除 FTS 同步触发器；
3. 在没有派生索引写入目标（derived-index sinks，即触发器同步写入的索引表）的情况下**重试权威写入**；并且
4. 通过 `LIKE` 回退，从**权威行**里提供搜索。

那次失败的现场操作**从不**运行 `FTS5('rebuild')`。既有的恢复归属关系**保持不变**：稍后的一次 `SessionDB` 打开，可以在**跨进程准入锁**与**外部持有者守卫**之下做重建。如果那次受守卫的重建跑不了，FTS 保持摘除状态、权威写入**依旧可用**，而 `hermes doctor` 会报出**显式的修复命令**。

---

## 2. Live behavior when the file itself is corrupt —— 文件本身损坏时的现场行为

如果一次现场写入报出**裸的** `SQLITE_CORRUPT` / `SQLITE_NOTADB`（`database disk image is malformed`、`file is not a database`）而**没有任何 FTS 来源（provenance）**，那损伤就在一条权威 **B 树**、schema、或**空闲页链表（freelist）**上。此时 `SessionDB` 会把那个**句柄**隔离（quarantine）（`StateDbCorruptError`）：

1. 失败的那次写入**向上传播**那个带类型的错误，**什么都不重试**；
2. 之后在该句柄上的写入**立即失败**，**不碰文件**；
3. 该句柄在 `close()` 之后**永不重开**它的连接；并且
4. `close()` **跳过**它那次显式的 WAL 检查点。

**停写就是保护。** 在现场，一个在首次结构错误之后**又持续写了约 50 分钟**的句柄，在关停时把 15 页写到了**错误的页号**上（第 1 页收到了一片 `messages_fts_trigram_data` 叶子），把一个"损坏但可读"的文件变成了一个**根本打不开**的文件。跳过那次显式检查点，是**第二道防线**；在 Python 3.12+ 上，隔离还会关掉 SQLite 自己的**最后连接检查点**（`SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`），于是 `-wal` 随行文件能在 `close()` 之后**存活下来供取证**。在 Python 3.11 上那个开关**不可用**，SQLite 仍可能在 close 时检查点一次，所以重启任何东西之前，请把 `state.db`、`state.db-wal` 和 `state.db-shm` **一起**拷走。

网关和 agent 的 flush 路径把这次隔离当成**文件被替换**来对待：待处理的转写本改去 `sessions/<id>.jsonl` 和网关的 `pending_messages/` 暂存池，而不是重试队列；而且那条一次性的 FTS 重建**绝不会**在这个受损文件上跑。隔离是**按进程**的 —— 共享句柄对**每一个持有者**都保持中毒状态，直到进程在一个已修复或已恢复的文件上重启。**网关还开着时，不要跑 `hermes doctor --fix`。** 下一步：

```bash
hermes gateway stop
HERMES_HOME="$HOME/.hermes" hermes sessions recover --source "$HOME/.hermes/state.db" --inspect-only
# if recoverable:
HERMES_HOME="$HOME/.hermes" hermes sessions recover --source "$HOME/.hermes/state.db" --output "$HOME/recovered-state.db"
```

或者，从 `state-snapshots/` 恢复最新的那份快照。

---

## 3. Explicit repair —— 显式修复

修复之前，**停掉每一个能打开该 profile 数据库的进程**。在整个修复与验证窗口内，让它们**一直保持停止**。

```bash
hermes gateway stop
HERMES_HOME="$HOME/.hermes" hermes sessions repair --check-only
HERMES_HOME="$HOME/.hermes" hermes sessions repair
```

`sessions repair` 默认先建立原始字节及随行文件的取证备份（raw-copy）；内部另用 SQLite backup API 生成修复快照，见附录 C ①，并通过仓库里那条**受守卫的快照-提升（snapshot-and-promotion）路径：先生成并验证独立快照，再以它替换目标库**做结构性的活。**不要**用 `cp` **单独**去拷 `state.db`、`state.db-wal` 和 `state.db-shm`；那三个文件是**同一幅活的 SQLite 镜像**。

修复之后、重启网关之前，验证健康探针、陈旧标记、触发器集合和权威行的行数：

```bash
HERMES_HOME="$HOME/.hermes" hermes sessions repair --check-only
sqlite3 "$HOME/.hermes/state.db" \
  "SELECT key, value FROM state_meta WHERE key = 'fts_stale';"
sqlite3 "$HOME/.hermes/state.db" \
  "SELECT type, name FROM sqlite_master WHERE name IN
   ('messages_fts_insert','messages_fts_update','messages_fts_delete')
   ORDER BY name;"
sqlite3 "$HOME/.hermes/state.db" \
  "SELECT 'sessions', COUNT(*) FROM sessions
   UNION ALL SELECT 'messages', COUNT(*) FROM messages;"
```

那条标记查询应当返回零行；上面的 SQL 只检查 base 三个触发器，trigram 三个及可选 CJK 触发器还需按实际启用的 schema 检查（见附录 C ②），而权威行数**绝不允许下降**。如果修复失败，把**活库和报告出来的备份都保留下来**；**永远不要**为了甩掉一个派生索引的错误而删掉权威行。

---

## 附录 A · 译名对照

| 术语 | 译名 | 首次出现节 |
|---|---|---|
| state database | 状态库 | §0 |
| `state.db` | 状态库文件（原样，不译） | §0 |
| transcript | 转写本 | §0 |
| canonical | 权威（canonical） | §0 |
| derived index（derived search index） | 派生索引 | §0 |
| sync trigger | 同步触发器 | §0 |
| FTS | 全文检索（FTS） | §0 |
| handle | 句柄 | §2 |
| quarantine | 隔离（quarantine） | §2 |
| B-tree | B 树 | §2 |
| freelist | 空闲页链表（freelist） | §2 |
| WAL checkpoint | WAL 检查点 | §2 |
| sidecar | 随行文件（sidecar） | §2 |
| provenance（FTS provenance） | 来源（provenance） | §2 |
| spool（pending spool） | 暂存池（spool） | §2 |
| snapshot | 快照 | §2 |
| admission lock（cross-process） | 跨进程准入锁 | §1 |
| foreign-holder guard | 外部持有者守卫 | §1 |
| `LIKE` fallback | `LIKE` 回退 | §1 |
| snapshot-and-promotion | 快照-提升 | §3 |
| health probe | 健康探针 | §3 |
| stale marker | 陈旧标记 | §3 |
| canonical row counts | 权威行数 | §3 |

---

## 附录 B · 本篇提到的文件与实测行数

行数一律 `git show f5d1926110:<路径> | wc -l` 实测，路径先经 `git ls-tree` 确认存在。**实测锚 `f5d1926110`。**
原文本身**没有按源码路径点名任何文件**：它点名的是**数据路径**（`state.db`、`state-snapshots/`、`pending_messages/`、`sessions/<id>.jsonl`）与**符号**（`SessionDB`、`StateDbCorruptError`、`fts_stale`、`SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`、`hermes sessions repair --check-only`、`hermes sessions recover … --inspect-only`、`hermes doctor`）。下表把那些符号落到锚点上的**宿主源码文件**。

| 文件 | 实测 | 角色 |
|---|---|---|
| `hermes_state.py` | **1674 行** | 门面 `SessionDB`：隔离句柄（`_halt_db_corrupt` / `_corrupt_error`）、`_disable_close_time_checkpoint`、`close()` 跳过 WAL 检查点、`_reopen_after_close_locked` 拒绝重开 |
| `hermes_state_errors.py` | **264 行** | §2 的 `StateDbCorruptError` 类（`:186`）与 `classify_persistence_error` |
| `hermes_state_fts.py` | **430 行** | §1 的 FTS 损坏现场处理：同一事务内摘除同步触发器、置 `_fts_stale` |
| `hermes_state_common.py` | **1224 行** | `FTS_STALE_KEY = "fts_stale"`（`:880`）、`_FTS_TRIGGERS`（`:321`）、跨进程准入锁 `fts_rebuild_admission` |
| `hermes_state_schema.py` | **1298 行** | §1 的受守卫重建：`_recover_stale_fts` / `retry_deferred_fts_recovery` |
| `hermes_state_search.py` | **1343 行** | §1 的 `LIKE` 回退：`_search_messages_like_fallback`（`:979`）、路径名 `like_scan_fts_stale` |
| `hermes_state_holders.py` | **363 行** | §1 的外部持有者守卫：`_foreign_state_db_holders`（`:188`） |
| `hermes_state_repair.py` | **1152 行** | §3 的 `repair_state_db_schema`（`:849`）、`_backup_db_file`（`:478`）、快照-提升路径 |
| `hermes_cli/doctor_state.py` | **491 行** | §1/§2 的"`hermes doctor` 报出修复命令"（`:200` / `_STATE_DB_STRUCTURAL_ISSUE`） |
| `hermes_cli/sessions_cmd.py` | **1021 行** | §2/§3 的 `sessions recover` / `sessions repair` 命令实现（`--inspect-only` / `--check-only`） |
| `gateway/session_transcript.py` | **535 行** | §2 的"转往暂存池 + JSONL 回退、FTS 重建绝不在此跑" |
| `gateway/shutdown_flush.py` | **296 行** | §2 的 `pending_messages/` 暂存池（`:5`） |
| `tests/hermes_state/test_state_db_corrupt_quarantine.py` | **332 行** | §2 现场证据（"约 50 分钟" / "15 页" / `messages_fts_trigram_data` 叶子）的出处（`:3-7`） |

---

## 附录 C · 上游文档与源码的出入

核对锚：上游 commit `f5d1926110`（v0.21.3）。原文**点名**的每一样东西都在源码里命中：`StateDbCorruptError`（`hermes_state_errors.py:186`）、`fts_stale` 标记（`hermes_state_common.py:880` `FTS_STALE_KEY = "fts_stale"`）、`SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`（`hermes_state.py:407`、`:1268`）、`hermes sessions repair --check-only`（`hermes_cli/subcommands/sessions.py:166`）、`hermes sessions recover … --inspect-only`（`:197`）、`state-snapshots/`（`hermes_cli/backup.py:37` `_QUICK_SNAPSHOTS_DIR = "state-snapshots"`）、`pending_messages/`（`gateway/shutdown_flush.py:5`）、`hermes doctor` 报修复命令（`hermes_cli/doctor_state.py:200`）；§2 那段现场证据（"约 50 分钟"、"15 页"、"`messages_fts_trigram_data` 叶子"）在 `tests/hermes_state/test_state_db_corrupt_quarantine.py:3-7` **逐字命中**。**另有 2 处出入 + 1 处不完整**，值得记下：

**1. §3 把 `sessions repair` 的默认备份称为 "a SQLite backup"，源码里它是**原始字节拷贝**（raw-copy），不是 SQLite-API 备份。**

- 原文 §3 逐字：*"`sessions repair` creates a SQLite backup by default"*。
- 源码里那份**面向用户、由 `--no-backup` 控制**的备份是 raw copy：`hermes_state_repair.py:849` `def repair_state_db_schema(db_path: Path, *, backup: bool = True)`；其 docstring `hermes_state_repair.py:855` 逐字：*"transactionally, so canonical rows are never modified by a failed attempt. **A raw backup is taken first**"*；实现 `hermes_state_repair.py:479` 逐字：*"**Raw-copy** a (possibly malformed) DB plus sidecars to a timestamped backup."*；调用点 `hermes_state_repair.py:991` `bpath, backup_error = _backup_db_file(db_path)`；备份文件名形如 `state.db.malformed-backup-<stamp>`。`--no-backup` 门控的正是它（`hermes_cli/sessions_cmd.py:115`）。
- 本修复模块中 SQLite **在线备份 API** 的调用是 `hermes_state_repair.py:684` `source.backup(destination, pages=256, progress=_check_deadline, sleep=_REPAIR_LOCK_POLL_SECONDS)`，属于**内部修复快照** `_copy_database_snapshot`（`:664`），**不受** `--no-backup` 控制、也不是 CLI 告知用户的那份备份。
- **结论**：术语层面**对不上** —— 面向用户的"备份"是**原始字节拷贝**，而非文档所说的 "SQLite backup"。

**2. §3 的触发器验证查询只列了 3 个触发器名，而 schema 定义的是 6 个。**

- 原文 §3 的验证 SQL 逐字：`WHERE name IN ('messages_fts_insert','messages_fts_update','messages_fts_delete')`，随后正文说 *"the expected FTS triggers should be present"*。
- 源码 `hermes_state_common.py:321-322` 逐字：
  ```
  _FTS_TRIGGERS = ("messages_fts_insert", "messages_fts_delete", "messages_fts_update",
                   "messages_fts_trigram_insert", "messages_fts_trigram_delete", "messages_fts_trigram_update")
  ```
- **结论**：原文这条"预期的 FTS 触发器都在场"的验证 SQL **看不到 trigram 那三个触发器**，对 trigram 索引而言**验证不完全**。

**补充（不完整，非矛盾）：§1 把重建只归到 "a later `SessionDB` open"，漏了网关的进程内重试。**

- 原文 §1 逐字：*"a later `SessionDB` open may rebuild under the cross-process admission lock"*。
- 源码 `hermes_state_schema.py:561-566`（`retry_deferred_fts_recovery` 的 docstring）逐字：*"Retry a deferred stale-FTS rebuild (gateway housekeeping tick). ``_recover_stale_fts`` fails closed at open, leaving search on LIKE; live write/search paths must never start a full rebuild, and **a gateway opens state.db once for days, so "next open" never comes.**"*
- **结论**：重建并不只发生在"下一次打开" —— 长命网关**根本不会再打开**，所以另有 housekeeping tick 上的 `retry_deferred_fts_recovery` 兜底。原文只提"下一次打开"，**操作含义上不完整**。
