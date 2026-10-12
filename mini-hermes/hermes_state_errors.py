"""会话库的错误分类谓词（对应 Hermes 的 `hermes_state_errors.py`）。

第 11 章要做的第一件事，是**把"库坏了"拆成两种处置完全相反的处境**：

| 处境 | 到底谁坏了 | 处置 | 为什么 |
|---|---|---|---|
| (a) **FTS 派生索引损坏** | `messages_fts*` 影子表 | **fail-open**：摘掉同步触发器，继续写 | 权威行（`messages`）没坏，坏的是**派生**索引；写不进去只是被那三个触发器拖累 |
| (b) **结构化损坏** | 库文件本身（B 树页 / schema / freelist） | **停写隔离** | 这时候"接着写"只会把损坏扩散；停下才是保护 |

★ 分岔口只有一句话：**SQLite 有没有把这次损坏"归因到" FTS 索引对象上？**

真实版 `is_fts_scoped_corruption_error`（`hermes_state_errors.py:100-122`）的
**"码优先于文本"**规则（`:105-110`）：

1. **有 `sqlite_errorcode`（Python 3.11+）→ 只看码是不是 `SQLITE_CORRUPT_VTAB = 267`**。
   老引擎会把 267 也报成通用的 "malformed image" 文本；只按文本判，会把
   "FTS 坏了"误判成"整个文件坏了" ⇒ 去隔离一个其实健康的库（走错整条路）。
   反过来，裸 `SQLITE_CORRUPT`/`SQLITE_NOTADB` **不带对象范围**，
   任何别的已知码都会**推翻**看起来像 FTS 的文本 ⇒ 一律 fail closed。
2. **没有码（Python < 3.11 / 被 RPC 包过的字符串）→ 才让文本说话**，而且必须**同时**满足：
   文本里出现 `messages_fts\\w*` 这样的对象名 **且** 命中 `fts5:…corrupt` 词表或损坏词表。

⚠️ `classify_persistence_error` 里 **corrupt 桶必须排在 disk 桶之前**：
   `"database disk image is malformed"` 里含 `"disk"`，顺序反了就会把
   B 树损坏读成"去清点磁盘空间"（真实版 `:83-87` 专门为这条写过警告，见 #77386）。

★ mini 的取舍：只留真实版里跟**这条链**有关的那几个桶与谓词，砍掉它的
  compression / turn-lease / replaced / deleted_wal 那几个桶（那些对应 mini 没有的机制）。
  `StateDbCorruptError` 这一章**只定义类型**，粘性隔离（停写、不重开、跳过关停 checkpoint）
  留给下一轮。
"""
from __future__ import annotations

import re
import sqlite3

# ── "库文件结构性损坏"的文本特征（真实版 `_DB_CORRUPTION_MARKERS`，`:88-90`）──
_DB_CORRUPTION_MARKERS = (
    "malformed",
    "file is not a database",
    "not a database",
    "database corruption",
)

# 真实版 `_MALFORMED_SCHEMA_MARKERS`（`:15`）：**只认 SQLite 明说的那句 schema 坏**。
# ★ 为什么只收这一句：`sqlite_master` 自己不一致时，SQLite 在准备**第一条**语句时
#   就解析整张 schema，于是每条语句都抛（连 `PRAGMA journal_mode` 都抛）；
#   而一句泛化的 "disk image is malformed" 可能是任意一个 B 树页坏 —— 运行期必须 fail closed。
_MALFORMED_SCHEMA_MARKERS = ("malformed database schema",)
_MALFORMED_DB_MARKERS = _MALFORMED_SCHEMA_MARKERS + ("database disk image is malformed",)


def is_malformed_db_error(exc: BaseException) -> bool:
    """泛化的"镜像坏了 / schema 坏了"文本判定（真实版同名函数，`:19-24`）。

    ⚠️ 只给**诊断与离线修复**用 —— 运行时修复必须用带 provenance 的
       `is_fts_scoped_corruption_error`（真实版 docstring 逐字：
       `Diagnostics / offline recovery only — runtime repair must use`...）。
    """
    return isinstance(exc, sqlite3.DatabaseError) and any(
        marker in str(exc).lower() for marker in _MALFORMED_DB_MARKERS
    )


# ── 结果码 ────────────────────────────────────────────────────────────────
# 真实版 `hermes_state_errors.py:93` 逐字：
#   The module constant exists on Python 3.11+; the numeric value is stable across SQLite releases.
SQLITE_CORRUPT_VTAB = getattr(sqlite3, "SQLITE_CORRUPT_VTAB", 267)

# mini 补的两个"裸码"常量（真实版没把它们单列成常量：它靠文本词表 + classify 达成等价判定）。
# 用 getattr 兜底是因为 `sqlite3.SQLITE_CORRUPT` 也是 Python 3.11 才有的属性，
# 而 mini 要同时跑 3.9（`/usr/bin/python3`）与 3.13。
SQLITE_CORRUPT = getattr(sqlite3, "SQLITE_CORRUPT", 11)     # sqlite3.h: SQLITE_CORRUPT
SQLITE_NOTADB = getattr(sqlite3, "SQLITE_NOTADB", 26)       # sqlite3.h: SQLITE_NOTADB

# 真实版 `hermes_state_errors.py:95-97` 逐字：
#   Every FTS object hangs off this prefix: the virtual tables and their _data/_idx/_content/
#   _docsize/_config shadow b-trees. FTS5 names the table in its own corruption reports.
_FTS_OBJECT_RE = re.compile(r"\bmessages_fts\w*")


def is_fts_scoped_corruption_error(exc_or_str) -> bool:
    """(a) 这次损坏，SQLite 把范围**落在 FTS 索引层**了吗？（真实版 `:100-122`）

    这条"归因规则"是全项目唯一的出处，三处共用（真实版 docstring 逐字）：
    写入修复门（`SessionDB._is_fts_write_corruption_error`）、网关 transcript 重试、
    以及 :func:`classify_persistence_error`。

    - **有码时码说了算**：`SQLITE_CORRUPT_VTAB`（267）算 FTS 范围，哪怕文本是
      老引擎那句通用的 malformed image；裸 `SQLITE_CORRUPT`/`SQLITE_NOTADB`
      没有对象范围，任何别的已知码都与"像 FTS 的文本"矛盾 ⇒ 两条都 fail closed。
    - **无码时才看文本**，且必须同时满足：命中 `messages_fts\\w*` **且**
      （`fts5:` 开头 + 含 `corrupt`）或命中损坏词表。

    ``exc_or_str`` 取 sqlite3 异常或字符串（真实版同样支持后者：RPC 包过的错误文本会
    丢掉异常类型，但字符串还在）。
    """
    if exc_or_str is None:
        return False
    code = getattr(exc_or_str, "sqlite_errorcode", None)
    if code is not None:
        return code == SQLITE_CORRUPT_VTAB
    text = (exc_or_str if isinstance(exc_or_str, str) else str(exc_or_str)).lower()
    if not _FTS_OBJECT_RE.search(text):
        return False
    if text.startswith("fts5:") and "corrupt" in text:
        return True
    return any(marker in text for marker in _DB_CORRUPTION_MARKERS)


# 每个 classify 桶的名字；调用方可以枚举它（真实版 `PERSISTENCE_ERROR_CAUSES`，`:76-80`）。
PERSISTENCE_ERROR_CAUSES = ("locked", "fts_index", "corrupt", "disk", "unknown")


def classify_persistence_error(exc_or_str) -> str:
    """粗粒度归因桶，让"给用户的话"跟处境对得上（真实版 `:236-264` 的 mini 子集）。

    | 桶 | 意思 | 该怎么处置 |
    |---|---|---|
    | `"locked"` | BUSY/锁：过一会儿就好 | 重试 |
    | `"fts_index"` | **SQLite 把损坏限定在 FTS 索引**（权威表没坏） | fail-open：摘触发器接着写 |
    | `"corrupt"` | 库**文件**结构性损坏 | 停写隔离 + 走修复路径（**不是**去腾磁盘空间） |
    | `"disk"` | 满了 / 只读 / 权限 | 运维 |
    | `"unknown"` | 说不清 | —— |

    ★ 判定顺序即纪律（真实版 `:248-253` 逐字注释）：**provenance 先于文本**；
      **corruption 必须排在 lock / disk 之前**（"disk image is malformed" 含 "disk"）。
    """
    if exc_or_str is None:
        return "unknown"
    # ① 已经被隔离的错误不再重判（真实版靠类型表 `:214-223` 做到）
    if isinstance(exc_or_str, StateDbCorruptError):
        return "corrupt"
    # ② provenance 先于文本：FTS 范围内的损坏**绝不是**整文件损坏
    if is_fts_scoped_corruption_error(exc_or_str):
        return "fts_index"
    text = str(exc_or_str).lower()
    # ③ 文件结构性损坏（含 "malformed"）—— 必须排在 disk 桶前，见模块 docstring 的警告
    if any(marker in text for marker in _DB_CORRUPTION_MARKERS):
        return "corrupt"
    # ④ "现在忙" ≠ "坏了"
    if "locked" in text or "busy" in text:
        return "locked"
    if any(marker in text for marker in ("disk", "readonly", "read-only")):
        return "disk"
    return "unknown"


def is_structural_corruption_error(exc: BaseException) -> bool:
    """(b) **裸** `SQLITE_CORRUPT`/`SQLITE_NOTADB`，且**没有** FTS provenance。

    真实版是 `SessionDB._is_structural_corruption_error`（`hermes_state.py:1235-1247`，逐字
    docstring）：`Bare SQLITE_CORRUPT/NOTADB with no FTS provenance: canonical
    B-tree/schema/freelist damage, never repairable from the live write path.`

    mini 把它从类方法提成模块函数（它只用谓词，不需要实例状态），
    但**判定式子照抄**：`DatabaseError` 且 不是 `StateDbCorruptError` 且 不是 FTS 归因
    且 `classify_persistence_error == "corrupt"`。

    ★ "裸码"这条规则的落点：`is_fts_scoped_corruption_error` 的**码优先**意味着
      裸 `SQLITE_CORRUPT`/`SQLITE_NOTADB` 永远拿不到 FTS provenance，
      于是它们必然落到 `"corrupt"` 桶 → 这里为真 → 上层停写隔离。
      两个谓词是**互斥**的，不是程度差异：分类顺序错了就整条走错路。
    """
    return (
        isinstance(exc, sqlite3.DatabaseError)
        and not isinstance(exc, StateDbCorruptError)
        and not is_fts_scoped_corruption_error(exc)
        and classify_persistence_error(exc) == "corrupt"
    )


class StateDbCorruptError(sqlite3.DatabaseError):
    """一个活的 `SessionDB` 观测到（非 FTS、非"文件被换掉"的）结构性损坏。

    真实版 `hermes_state_errors.py:186-201`。★ 本章**只定义类型**：粘性隔离
    （停写、拒绝自动重开、跳过关停时的 WAL checkpoint）留给下一轮。
    真实版为什么要把这些做出来，教训逐字写在它的 docstring 里：

        a handle that kept writing after the first error checkpointed 15 pages
        under wrong page numbers and turned a readable file into "file is not a
        database" — 多写约 50 分钟，最后一页收到了错误的页号，文件再也打不开。

    这里保留的最重要一点是**继承关系**：它是 `sqlite3.DatabaseError` 的子类
    （真实版逐字：`Subclasses sqlite3.DatabaseError so every degrade path keeps working.`）——
    所以它抛出后，任何"按 DatabaseError 分类"的上层代码仍然认得它。
    """

    def __init__(self, message: str = ""):
        super().__init__(message or _STATE_DB_CORRUPT_MSG)


# 隔离时的报错文案（形状照抄真实版 `_STATE_DB_CORRUPT_MSG`，`:204-211`；
# 内容按 mini 现状改写：mini 还没有 `sessions recover` 子命令 —— 那句在下一轮）。
_STATE_DB_CORRUPT_MSG = (
    "state.db 报出结构性损坏（损坏落在 FTS 影子表**之外**）：这个句柄拒绝继续写入。"
    "接着写只会把损坏扩散（真实事故：第一次报错后继续写，把可读的文件写成了"
    "「file is not a database」）。请停下写者、用修复路径（下一轮：sessions repair）"
    "或从备份恢复后再开。"
)
