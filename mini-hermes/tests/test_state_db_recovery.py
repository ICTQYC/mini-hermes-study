"""第 11 章的自测：FTS 损坏自愈链（分类 / 标记 / fail-open / 写入降级 / LIKE 回退 / 重建）—— **不联网**。

跑法：python3 tests/test_state_db_recovery.py

## 测的是什么（12 组）

| 组 | 守的承诺 |
|---|---|
| 1. 损坏分类谓词 | **码优先于文本** · FTS 派生损坏 / 结构化损坏 / 无关错误**三组不混** · `StateDbCorruptError` 只是类型 |
| 2. `fts_stale` 标记跨重启 | 标记落在 `state_meta` · 关掉重开库 `_fts_stale` 仍为真 · 触发器**不会**被 `IF NOT EXISTS` 复活 |
| 3. fail-open 让权威写入继续 | FTS 派生损坏时写入**仍然成功** · `fts_stale='1'` · **三个触发器全被摘** · **从未 rebuild** |
| 4. 降级是持久的 | 同一连接后续写入照常 · 搜索改走 LIKE 且路径名标对 |
| 5. 结构化损坏停写 | 立即失败、类型是 `StateDbCorruptError`、**不重试** · 而且**不**误摘触发器 |
| 6. 不变量：活路径不重建 | 写入与搜索都不跑 `FTS5('rebuild')` · 三处"活路径不许重建"的文字凭证在位 |
| 7. 重建之后 | 标记被清 · 触发器回来 · 索引里那段空窗被补上 · 搜索又走 FTS |
| 8. 隔离与名单 | 库路径唯一来源 = `MINI_HOME` · 摘除/重装看**同一份**触发器名单 |
| **9. 结构损坏隔离（A7）** | **粘性标志**置真 · 之后每次写入**立即失败**且错误是 `StateDbCorruptError` · **不碰文件**（字节指纹不变） |
| **10. close 后拒绝重开（A7）** | 被隔离的句柄 `close()` 后**拒重开** · 健康句柄照常重开 · `close()` 关停不 checkpoint 的文字凭证在位 |
| **11. `repair --check-only`（A8）** | 健康库=0 / 降级库=1 / 结构损坏库=1；结构损坏时 `repair` 拒绝假装能修 |
| **12. `repair` 真修回来（A8）** | 三段验证（标记零行 · 触发器在场 · 权威行数不降）· 默认留 raw-copy 备份 · 标记清/触发器回/搜索回 FTS |

★ 第 9/10 组（A7）用的是**异常驱动**的结构性损坏：一条形状与现场真错逐字相同的
`sqlite3.DatabaseError("database disk image is malformed")` —— 双解释器行为一致。
第 12 组（A8）的"降级库"用的也是异常驱动的 FTS 派生损坏，所以 `repair` 的重建能把它修回来
（若 FTS 影子表被**物理**破坏，重建自己就会失败 —— 那不是 §3 要复演的处境）。

## 隔离

`MINI_HOME` 在 **import 之前**指到临时目录（见下面 `_HOME`），
每条用例再在它下面开一个自己的子目录 —— 一条用例一个库文件，彼此不串，
也**绝不会碰用户的真实库**。

## 关于"怎么造一个 FTS 损坏"

真造得出来就真造（组 3 的第二个用例：删掉 `messages_fts_data` 影子表）。
但**双解释器下真造不总是可行** —— 两个实测事实（这就是为什么要留一条"异常驱动"的路）：

| 解释器 | 删 FTS5 影子表 | 真损坏时 SQLite 报什么 |
|---|---|---|
| `/usr/local/bin/python3` 3.13（SQLite 3.53） | **允许** | `DatabaseError: fts5: corruption found reading blob 1 from table "messages_fts"`（`sqlite_errorcode = 267`） |
| `/usr/bin/python3` 3.9（SQLite 3.51） | **拒绝**（`table messages_fts_data may not be dropped`） | —— 造不出来 |

另外，**文件级**的真损坏（把页写坏）得到的是 `sqlite_errorcode = 11` +
`database disk image is malformed` —— 那**不带 FTS provenance**，按设计就该走"结构化损坏"
那条路（组 5 测它）。所以"FTS 派生损坏"这条路在 3.9 上只能用一条**形状与真错逐字相同**的
`sqlite3.DatabaseError` 来驱动 —— 组 3/4/6/7 就是这么做的，测的是"写入环怎么分类、
怎么降级、怎么重跑"这条契约。
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★★ MINI_HOME 必须在 import 任何 mini 模块**之前**设好 —— 这样"库路径跟随 MINI_HOME"
#    这件事从第一个 import 起就是真的，而不是靠某一处函数兜着。
_HOME = tempfile.mkdtemp(prefix="mini_ch11_")
os.environ["MINI_HOME"] = _HOME

import hermes_constants  # noqa: E402
import hermes_state  # noqa: E402
import hermes_state_repair  # noqa: E402
from hermes_cli import sessions_cmd  # noqa: E402
from hermes_state_common import FTS_STALE_KEY, _FTS_TRIGGERS  # noqa: E402
from hermes_state_errors import (  # noqa: E402
    SQLITE_CORRUPT,
    SQLITE_CORRUPT_VTAB,
    StateDbCorruptError,
    classify_persistence_error,
    is_fts_scoped_corruption_error,
    is_malformed_db_error,
    is_structural_corruption_error,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {detail}" if detail and not cond else ""))


# ── 三个"损坏形状"常量 ──────────────────────────────────────────────────────
# FTS 派生损坏：**逐字**取自 3.13 上的真实报错（见模块 docstring 的实测表）。
# 不带 `sqlite_errorcode` 的构造异常 ⇒ 3.9 与 3.13 都走"文本判定"分支，行为一致。
FTS_CORRUPT_TEXT = 'fts5: corruption found reading blob 1 from table "messages_fts"'
# 结构化损坏：不出现 FTS 对象名 ⇒ 无 provenance ⇒ 结构化桶（真实事故现场也是这句话）。
STRUCTURAL_CORRUPT_TEXT = "database disk image is malformed"
# 无关错误：既不 FTS、也不损坏（锁）⇒ 两个谓词都必须为假。
IRRELEVANT_ERROR_TEXT = "database is locked"


def _make_exc(text: str, code=None) -> sqlite3.DatabaseError:
    """造一个 sqlite3 异常；给了 `code` 就带上 `sqlite_errorcode`（模拟 3.11+ 的结果码）。"""
    exc = sqlite3.DatabaseError(text)
    if code is not None:
        exc.sqlite_errorcode = code          # py3.11+ 的驱动异常本来就带这个属性
    return exc


# ── 库与数据的夹具 ─────────────────────────────────────────────────────────
_SEQ = itertools.count()


@contextmanager
def fresh_db():
    """在 `MINI_HOME` 下开一个**独享**的库（一条用例一个文件，绝不串库）。"""
    d = Path(_HOME) / ("db%d" % next(_SEQ))
    db = hermes_state.SessionDB(d / "state.db")
    try:
        yield db
    finally:
        db.close()


def _seed(db, sid: str = "s1", source: str = "cli") -> str:
    db.create_session(sid, source, model="deepseek-chat")
    db.append_message(sid, "user", "帮我看看 /tmp 下的 docker 部署文件 zetaseed11")
    db.append_message(sid, "assistant", "找到 docker-deploy.yaml，建议先看它。")
    return sid


# ── 观察点（都取"权威可查的事实"，不碰实现细节）────────────────────────────
def _marker(db):
    """`state_meta` 里的降级标记值（没有就是 None）。"""
    row = db._conn.execute(
        "SELECT value FROM state_meta WHERE key = ?", (FTS_STALE_KEY,)).fetchone()
    return None if row is None else row[0]


def _triggers(db):
    """活库里现存的 FTS 同步触发器名字（排序，便于与整套名单比对）。"""
    return sorted(r[0] for r in db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'messages_fts%'"))


def _fts_match_count(db, term: str) -> int:
    """索引里能 MATCH 到 *term* 的条数 —— **索引里有没有这一行**的直接证据（空窗看得见）。"""
    return int(db._conn.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?", (term,)).fetchone()[0])


def _canonical_rows(db, sid: str) -> int:
    return int(db._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)).fetchone()[0])


def _write_through_degraded_loop(db, sid: str, content: str,
                                 corrupt_text: str = FTS_CORRUPT_TEXT):
    """走**真实的写入礼节**写一条权威行，但让第一次尝试抛 FTS 派生损坏。

    模拟的正是现场：`messages` 的 AFTER INSERT 触发器把这次写入拖进坏影子表，
    于是**整条语句**失败（canonical 行本身没坏）。

    返回 `(fn 实际被调用的次数, rowid)` —— 次数是"重跑同一个 fn"的直接证据。
    """
    seen = {"n": 0}

    def fn(conn: sqlite3.Connection):
        seen["n"] += 1
        if seen["n"] == 1:
            raise _make_exc(corrupt_text)
        cur = conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
            (sid, "user", content, time.time()))
        return cur.lastrowid

    rowid = db._execute_write(fn)
    return seen["n"], rowid


def _spy_rebuild(db):
    """把 `rebuild_fts` 换成计数器 —— 用来抓"活路径偷偷重建了"这种违规。"""
    calls = []

    def _spy(*args, **kwargs):
        calls.append(1)

    db.rebuild_fts = _spy                        # type: ignore[method-assign]
    return calls


def _try_break_fts_physically(db) -> bool:
    """真造一个坏掉的 FTS 索引：删掉 `messages_fts_data` 影子表。

    3.13 上成功；3.9 带的 SQLite 会拒绝（`may not be dropped`）⇒ 返回 False。
    """
    try:
        db._conn.execute("DROP TABLE messages_fts_data")
    except sqlite3.Error:
        return False
    return True


# ── 第 11 章 A7/A8 新用例用的夹具与观察点 ────────────────────────────────────
def _raise_structural(_conn: sqlite3.Connection):
    """一个"撞上结构性损坏"的写入回调（供 A7 用例把句柄钉进隔离态）。"""
    raise _make_exc(STRUCTURAL_CORRUPT_TEXT)


def _db_fingerprint(db_path) -> str:
    """库文件（主文件 + 三件 sidecar）的字节指纹 —— 用来证明"隔离后写入不碰文件"。"""
    digest = hashlib.sha256()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            digest.update(suffix.encode())
            digest.update(p.read_bytes())
    return digest.hexdigest()


def _can_write(db, sid: str) -> bool:
    """试写一条，看能不能成功（隔离句柄应恒为 False）。"""
    try:
        db.append_message(sid, "user", "隔离之后仍然不许写 zetatry1")
        return True
    except StateDbCorruptError:
        return False


def _make_degraded_db(path):
    """造一个**降级库**：真跑一次 fail-open（标记='1' + 触发器被摘），返回 ``(db, sid)``。

    注意：这里用的是"异常驱动"的 FTS 派生损坏（索引本身没被物理破坏），
    所以之后 `repair` 的重建能把它修回来 —— 正是 §3 要复演的处境。
    """
    db = hermes_state.SessionDB(path)
    sid = _seed(db)
    _write_through_degraded_loop(db, sid, "降级窗口里的行 zetaquar11")
    return db, sid


def _nuke_db_file(path) -> None:
    """把库**主文件**的字节写坏（拿走 sidecar）—— 造一个"文件级结构性损坏"。"""
    for suffix in ("-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    with open(str(path), "r+b") as fh:
        fh.write(b"THIS-IS-NOT-A-SQLITE-DATABASE" * 4)


@contextmanager
def _home_scope(home):
    """把 `MINI_HOME` 指到 *home*（让 CLI 的 `state_db_path()` 正好解析到 *home*/state.db）。"""
    saved = os.environ.get("MINI_HOME")
    os.environ["MINI_HOME"] = str(home)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("MINI_HOME", None)
        else:
            os.environ["MINI_HOME"] = saved


def _ns(check_only: bool = False):
    """构造 `sessions repair` 的 argparse namespace（CLI 外壳的最小入参）。"""
    return argparse.Namespace(check_only=check_only)


# ══════════════════════════════════════════════════════ 1. 损坏分类谓词
def test_corruption_classification() -> None:
    # ── ① 真 FTS 错文本：归因到派生索引，不是文件损坏 ────────────────────
    fts_exc = _make_exc(FTS_CORRUPT_TEXT)
    check("FTS 错文本 → is_fts_scoped 为真", is_fts_scoped_corruption_error(fts_exc))
    check("FTS 错文本 → 不算结构化损坏", not is_structural_corruption_error(fts_exc))
    check("FTS 错文本 → 归因桶 = fts_index",
          classify_persistence_error(fts_exc) == "fts_index", classify_persistence_error(fts_exc))
    check("FTS 错文本用**字符串**判定也一样（RPC 包过的错误丢类型不丢文本）",
          is_fts_scoped_corruption_error(FTS_CORRUPT_TEXT))

    # ── ② 真结构错文本：裸 malformed，无 FTS provenance ⇒ 文件损坏 ────────
    struct_exc = _make_exc(STRUCTURAL_CORRUPT_TEXT)
    check("结构错文本 → 不算 FTS 归因", not is_fts_scoped_corruption_error(struct_exc))
    check("结构错文本 → 算结构化损坏", is_structural_corruption_error(struct_exc))
    check("结构错文本 → 归因桶 = corrupt",
          classify_persistence_error(struct_exc) == "corrupt", classify_persistence_error(struct_exc))
    check("结构错文本被 is_malformed_db_error 认出（诊断用）", is_malformed_db_error(struct_exc))

    # ── ③ 无关错文本：既不 FTS、也不损坏 ⇒ 两个谓词都为假 ────────────────
    other = _make_exc(IRRELEVANT_ERROR_TEXT)
    check("无关错文本 → 两个谓词都为假",
          not is_fts_scoped_corruption_error(other) and not is_structural_corruption_error(other))
    check("无关错文本 → 归因桶 = locked（是「忙」，去重试而不是去隔离）",
          classify_persistence_error(other) == "locked", classify_persistence_error(other))

    # ── ④ ★ 码优先于文本（这一条判错就整条走错路）──────────────────────
    check("码优先：SQLITE_CORRUPT_VTAB(267) + 通用 malformed 文本 → 仍算 FTS 归因",
          is_fts_scoped_corruption_error(_make_exc(STRUCTURAL_CORRUPT_TEXT, SQLITE_CORRUPT_VTAB)))
    check("码优先：裸 SQLITE_CORRUPT(11) + 看着像 FTS 的文本 → **不算** FTS 归因（fail closed）",
          not is_fts_scoped_corruption_error(_make_exc(FTS_CORRUPT_TEXT, SQLITE_CORRUPT)))
    # 最强的一组演示：**同一条文本**，有没有结果码 → 走两条完全相反的路
    # （真实那句 `malformed database schema (messages_fts_data)` 正好两者兼有：
    #   它既点了 FTS 对象名、又是一句通用 malformed ⇒ 全靠"码优先"来分岔）
    object_named_text = "malformed database schema (messages_fts_data)"
    check("同一文本 · 无码：文本说了算 → FTS 归因（索引坏，接着写）",
          is_fts_scoped_corruption_error(object_named_text)
          and classify_persistence_error(object_named_text) == "fts_index")
    check("同一文本 · 带裸 SQLITE_CORRUPT(11)：码推翻文本 → 结构化损坏（停写）",
          not is_fts_scoped_corruption_error(_make_exc(object_named_text, SQLITE_CORRUPT))
          and is_structural_corruption_error(_make_exc(object_named_text, SQLITE_CORRUPT)))
    check("两桶互斥：同一条 FTS 错文本不会同时进两个桶",
          not (is_fts_scoped_corruption_error(fts_exc) and is_structural_corruption_error(fts_exc)))
    check("常量值稳定（跨 SQLite 版本）：267 / 11",
          (SQLITE_CORRUPT_VTAB, SQLITE_CORRUPT) == (267, 11), f"{SQLITE_CORRUPT_VTAB}/{SQLITE_CORRUPT}")

    # ── ⑤ 类型：本章只定义类型，但继承关系是契约 ────────────────────────
    check("StateDbCorruptError 继承 sqlite3.DatabaseError（所有 DatabaseError 分支仍认得它）",
          issubclass(StateDbCorruptError, sqlite3.DatabaseError))
    check("已经是 StateDbCorruptError 的错误不再被重判为「别的」（桶恒为 corrupt）",
          classify_persistence_error(StateDbCorruptError()) == "corrupt")


# ═════════════════════════════════════════════ 2. fts_stale 标记跨重启可见
def test_marker_is_durable_across_reopen() -> None:
    d = Path(_HOME) / ("db%d" % next(_SEQ))
    path = d / "state.db"
    db = hermes_state.SessionDB(path)
    sid = _seed(db)
    check("新库里 state_meta 是空的（没降级过）", _marker(db) is None)
    check("新库里三个触发器都在", _triggers(db) == sorted(_FTS_TRIGGERS), repr(_triggers(db)))
    check("新库 _fts_stale 为假", db._fts_stale is False)

    # 触发一次 fail-open（写入环自己跑的）
    _write_through_degraded_loop(db, sid, "降级期间写进来的 zetamark11")
    check("fail-open 后标记 = '1'", _marker(db) == "1", repr(_marker(db)))
    check("fail-open 后 state_meta 里恰好一行降级标记",
          db._conn.execute("SELECT COUNT(*) FROM state_meta WHERE key = ?",
                           (FTS_STALE_KEY,)).fetchone()[0] == 1)
    db.close()

    # ★ 关掉重开：标记是持久的，内存里那份"降级状态"是从库里读回来的
    db2 = hermes_state.SessionDB(path)
    check("关掉重开后 _fts_stale 仍为真（读的是 state_meta，不是上次的内存）",
          db2._fts_stale is True)
    check("重开后 _fts_enabled 为假（不再往坏索引里同步）", db2._fts_enabled is False)
    check("重开后触发器**仍然被摘**（不许被 CREATE TRIGGER IF NOT EXISTS 复活）",
          _triggers(db2) == [], repr(_triggers(db2)))
    check("重开后搜索直接走 LIKE（不试坏索引）",
          [h["route"] for h in db2.search_messages("zetaseed11")] == ["like_scan_fts_stale"],
          repr([h["route"] for h in db2.search_messages("zetaseed11")]))
    check("重开后权威写入照常成功（降级是跨重启的，不是一次性的）",
          db2.append_message(sid, "user", "重开之后照样写 zetamark11") is not None)
    db2.close()


# ═══════════════════════════════════════════ 3. fail-open：权威写入仍然成功
def test_fail_open_keeps_canonical_writes_alive() -> None:
    with fresh_db() as db:
        sid = _seed(db)
        before = _canonical_rows(db, sid)
        writes_before = db.write_count
        rebuilds = _spy_rebuild(db)

        calls, rowid = _write_through_degraded_loop(db, sid, "FTS 坏了这条也要写进去 zetadegrade11")
        check("第一次尝试撞上 FTS 派生损坏后**重跑了同一个 fn**（调用 2 次）", calls == 2, str(calls))
        check("权威写入**仍然成功**（没有把异常抛给调用方）", rowid is not None, repr(rowid))
        check("权威行真的落库了（比之前多 1 行）",
              _canonical_rows(db, sid) == before + 1, f"{before} → {_canonical_rows(db, sid)}")
        check("重跑同一个 fn 只算**一次**成功写入（重试不重复计数）",
              db.write_count == writes_before + 1,
              f"{writes_before} → {db.write_count}")

        check("同一事务里落下了 fts_stale='1'（面包屑）", _marker(db) == "1", repr(_marker(db)))
        check("**三个触发器全被摘**（不是只摘一半）", _triggers(db) == [], repr(_triggers(db)))
        check("内存标志跟上：_fts_stale 真 / _fts_enabled 假",
              db._fts_stale is True and db._fts_enabled is False)
        check("★ 现场路径**从未**跑 rebuild（活写入不许变成无界全量重建）",
              rebuilds == [], str(len(rebuilds)))

        # 行为证据（不是只看 spy）：索引里**没有**那条降级期间写进来的行 —— 因为没人重建它
        check("索引里查不到降级期间写进来的行（空窗确实存在，说明没被偷偷重建）",
              _fts_match_count(db, "zetadegrade11") == 0,
              str(_fts_match_count(db, "zetadegrade11")))


# ═══════════════════════════════════════════ 4. 降级是持久的 + 搜索走 LIKE
def test_degradation_persists_and_search_falls_back() -> None:
    with fresh_db() as db:
        sid = _seed(db)
        check("降级前：ASCII 查询走 FTS",
              [h["route"] for h in db.search_messages("zetaseed11")] == ["fts"])
        check("降级前：路径名是 fts5", db._describe_search_path("zetaseed11") == "fts5")

        _write_through_degraded_loop(db, sid, "降级期间写进来的 zetap13")

        check("降级后：同一条 ASCII 查询改走 LIKE 兜底（索引不可信）",
              [h["route"] for h in db.search_messages("zetaseed11")] == ["like_scan_fts_stale"],
              repr([h["route"] for h in db.search_messages("zetaseed11")]))
        check("降级后：路径名标成 like_scan_fts_stale（一眼看出「这是降级在服务」）",
              db._describe_search_path("zetaseed11") == "like_scan_fts_stale")
        check("降级后搜索**真的还能搜到东西**（从权威表扫，不是空手而归）",
              len(db.search_messages("zetaseed11")) == 1)
        check("降级后中文那条路也照走（CJK 判据没被 stale 判据顶掉）",
              db._describe_search_path("部署文件") == "like_scan_fts_stale")

        # ★ 持久：同一连接上后续写入照常（降级不是"一次性放过这一条"）
        ok = []
        for i in range(3):
            try:
                db.append_message(sid, "user", f"降级之后第 {i} 条 zetap14")
                ok.append(True)
            except Exception:                            # noqa: BLE001
                ok.append(False)
        check("同一连接上后续 3 条写入都成功（降级状态是持久的）", all(ok), repr(ok))
        check("后续写入让权威行数继续增长", _canonical_rows(db, sid) == 6,
              str(_canonical_rows(db, sid)))
        check("后续写入仍然不许偷偷重建", _fts_match_count(db, "zetap14") == 0)


# ═══════════════════════════════════════ 5. 结构化损坏：停写、不重试、类型化
def test_structural_corruption_halts_without_retry() -> None:
    with fresh_db() as db:
        sid = _seed(db)
        seen = {"n": 0}

        def fn(conn: sqlite3.Connection):
            seen["n"] += 1
            raise _make_exc(STRUCTURAL_CORRUPT_TEXT)

        raised = None
        try:
            db._execute_write(fn)
        except StateDbCorruptError as exc:
            raised = exc
        except Exception as exc:                         # noqa: BLE001
            raised = exc
        check("结构化损坏 → 抛出的类型是 StateDbCorruptError",
              isinstance(raised, StateDbCorruptError), f"{type(raised).__name__}: {raised}")
        check("结构化损坏 → **不重试**（fn 只被调用 1 次）", seen["n"] == 1, str(seen["n"]))
        check("结构化损坏 → **没有**落降级标记（没走 fail-open 那条路）", _marker(db) is None)
        check("结构化损坏 → 触发器**一个都没被摘**（两桶互斥，不许误摘健康索引）",
              _triggers(db) == sorted(_FTS_TRIGGERS), repr(_triggers(db)))
        check("结构化损坏 → _fts_stale 仍为假", db._fts_stale is False)

        # 结论落库还是能查：权威数据本身没被动过
        check("抛错之后权威行数不变（失败的那次写入整体回滚）",
              _canonical_rows(db, sid) == 2, str(_canonical_rows(db, sid)))


# ═════════════════════════════════════ 6. 不变量：活路径（写/搜）绝不 rebuild
def test_live_paths_never_rebuild() -> None:
    with fresh_db() as db:
        sid = _seed(db)
        rebuilds = _spy_rebuild(db)

        _write_through_degraded_loop(db, sid, "降级期间写进来的 zetainv11")
        for _ in range(3):
            db.search_messages("zetaseed11")             # 走 LIKE
            db.search_messages("zetainv11")
            db.append_message(sid, "user", "活路径继续写 zetainv11")
        check("★ 一次写入 + 六次搜索之后：rebuild 调用次数 = 0（活路径绝不重建）",
              rebuilds == [], str(len(rebuilds)))
        check("索引里始终没有那段空窗的内容（没人重建过它）",
              _fts_match_count(db, "zetainv11") == 0)
        check("降级标记也没被谁顺手清掉（只有重建才允许清）", _marker(db) == "1")

    # ★ 文字凭证（A6 的三处引文）：注释也是契约的一部分 —— 它是"为什么不会雪崩"的记录
    root = Path(__file__).resolve().parent.parent
    write_src = (root / "hermes_state.py").read_text(encoding="utf-8")
    fts_src = (root / "hermes_state_fts.py").read_text(encoding="utf-8")
    search_src = (root / "hermes_state_search.py").read_text(encoding="utf-8")
    check("写路径写着 `never rebuild here`（真实版 hermes_state.py:1028-1029）",
          "never rebuild here" in write_src)
    check("搜索写着 `a live search never runs the unbounded rebuild`"
          "（真实版 hermes_state_search.py:918-920）",
          "a live search never runs the unbounded rebuild" in search_src)
    check("fail-open 写着 `nobody may reinstall them without a full rebuild`"
          "（真实版 hermes_state_fts.py:354-356）",
          "nobody may reinstall them without a full rebuild" in fts_src)


# ═════════════════════════════════════════════════════ 7. 重建：活儿干完的样子
def test_rebuild_restores_fts() -> None:
    with fresh_db() as db:
        sid = _seed(db)
        _write_through_degraded_loop(db, sid, "降级期间写进来的 zetarebuild11")
        check("前置：标记在、触发器空、索引里查不到那条（空窗成立）",
              _marker(db) == "1" and _triggers(db) == []
              and _fts_match_count(db, "zetarebuild11") == 0)

        db.rebuild_fts()

        check("重建后：标记被清（state_meta 里那行没了）", _marker(db) is None, repr(_marker(db)))
        check("重建后：三个触发器回来了（整套，与名单逐字一致）",
              _triggers(db) == sorted(_FTS_TRIGGERS), repr(_triggers(db)))
        check("重建后：内存标志复位（_fts_stale 假 / _fts_enabled 真）",
              db._fts_stale is False and db._fts_enabled is True)
        check("重建后：**空窗被补上**（降级期间写进来的行现在能 MATCH 到了）",
              _fts_match_count(db, "zetarebuild11") == 1,
              str(_fts_match_count(db, "zetarebuild11")))
        check("重建后：ASCII 查询又走 FTS",
              [h["route"] for h in db.search_messages("zetaseed11")] == ["fts"],
              repr([h["route"] for h in db.search_messages("zetaseed11")]))
        check("重建后：路径名回到 fts5", db._describe_search_path("zetaseed11") == "fts5")
        check("重建后：新写入立刻重新进索引（触发器真的在工作）",
              db.append_message(sid, "user", "重建之后的活写入 zetapost11") is not None
              and _fts_match_count(db, "zetapost11") == 1)
        # 重建后新开一个连接：标记已清 ⇒ 不再是降级态
        db.close()
        db2 = hermes_state.SessionDB(db.db_path)
        check("重建后重开库：不再是降级态（_fts_stale 假、触发器在）",
              db2._fts_stale is False and _triggers(db2) == sorted(_FTS_TRIGGERS))
        db2.close()


# ═══════════════════════════════════════════ 8. 隔离、真损坏、触发器名单
def test_isolation_physical_damage_and_registry() -> None:
    # ① 隔离：库路径的唯一来源是 MINI_HOME（这一组的前置在模块顶部就完成了）
    check("库路径落在 MINI_HOME 下", hermes_state.SessionDB().db_path == Path(_HOME) / "state.db",
          str(hermes_state.SessionDB().db_path))
    check("唯一路径来源 = state_db_path()",
          hermes_constants.state_db_path() == Path(_HOME) / "state.db")

    # ② 触发器名单是唯一事实来源：活库里的三个名字必须**正好**是名单
    with fresh_db() as db:
        check("活库里的触发器 = 名单（摘除与重装看同一份）",
              _triggers(db) == sorted(_FTS_TRIGGERS), repr(_triggers(db)))
        db.drop_all_fts_triggers()
        check("drop_all_fts_triggers() 把名单里每一个都摘掉（不是只摘一部分）",
              _triggers(db) == [], repr(_triggers(db)))
        db.drop_all_fts_triggers()
        check("重复摘除无害（幂等：fail-open 可能连着来两次）", _triggers(db) == [])
        db.rebuild_fts()
        check("rebuild_fts() 把名单里每一个都装回来", _triggers(db) == sorted(_FTS_TRIGGERS))

    # ③ 真损坏（能造才造）：删掉 FTS 影子表然后真的写
    with fresh_db() as db:
        sid = _seed(db)
        if _try_break_fts_physically(db):
            rowid = db.append_message(sid, "user", "真损坏之后仍要写进去 zetaphys11")
            check("真 FTS 影子表被删后，权威写入仍然成功（真损坏，不是模拟）", rowid is not None)
            check("真损坏触发了 fail-open：标记 = '1'", _marker(db) == "1", repr(_marker(db)))
            check("真损坏：三个触发器全被摘", _triggers(db) == [], repr(_triggers(db)))
            check("真损坏：搜索改走 LIKE 并仍能搜到权威行",
                  [h["route"] for h in db.search_messages("zetaseed11")] == ["like_scan_fts_stale"]
                  and len(db.search_messages("zetaseed11")) == 1)
            db.close()
            db2 = hermes_state.SessionDB(db.db_path)
            check("真损坏的降级状态跨重启可见", db2._fts_stale is True)
            check("真损坏后重开库，权威写入照常",
                  db2.append_message(sid, "user", "真损坏 + 重开之后继续写 zetaphys11") is not None)
            db2.close()
        else:
            # 3.9 分支：本机 SQLite 禁止动 FTS5 影子表 —— 这本身是一条实测事实，如实记下
            check("本机 SQLite 禁止删 FTS5 影子表（3.9 分支）⇒ 真损坏改由异常驱动组覆盖", True,
                  "table messages_fts_data may not be dropped")
            check("该分支下库保持健康（触发器在、无降级标记）",
                  _triggers(db) == sorted(_FTS_TRIGGERS) and _marker(db) is None)


# ═══════════════════════════════════════ 9. 结构损坏隔离（粘性标志 + 写前守卫）
def test_structural_quarantine_stops_writes_and_freezes_file() -> None:
    with fresh_db() as db:
        sid = _seed(db)
        seen = {"n": 0}

        def fn(conn: sqlite3.Connection):
            seen["n"] += 1
            raise _make_exc(STRUCTURAL_CORRUPT_TEXT)

        raised = None
        try:
            db._execute_write(fn)
        except StateDbCorruptError as exc:
            raised = exc
        except Exception as exc:                         # noqa: BLE001
            raised = exc
        check("结构性损坏 → 抛出的类型是 StateDbCorruptError",
              isinstance(raised, StateDbCorruptError), f"{type(raised).__name__}: {raised}")
        check("结构性损坏 → **不重试**（fn 只被调用 1 次）", seen["n"] == 1, str(seen["n"]))
        # ★ A7 的核心：置**粘性**标志（上一轮只抛类型、不置标志）
        check("隔离标志是粘性的：_db_corrupt=True 且记下了原因",
              db._db_corrupt is True and bool(db._db_corrupt_reason),
              f"{db._db_corrupt} / {db._db_corrupt_reason!r}")
        check("_quarantine_reason() 如实报出隔离原因",
              "structural corruption" in (db._quarantine_reason() or ""),
              repr(db._quarantine_reason()))

        # ② 隔离后写入：立即失败 + **不碰文件**
        frozen = _db_fingerprint(db.db_path)
        kinds = []
        for i in range(3):
            try:
                db.append_message(sid, "user", "隔离之后这条不许写进去 zetafreeze%d" % i)
            except Exception as exc:                     # noqa: BLE001
                kinds.append(type(exc).__name__)
        check("隔离后 3 次写入**全部立即失败**，类型都是 StateDbCorruptError",
              kinds == ["StateDbCorruptError"] * 3, repr(kinds))
        check("隔离后写入**不碰文件**（库文件指纹逐字不变）",
              _db_fingerprint(db.db_path) == frozen)
        check("隔离后权威行数没变（失败写入整体没落库）",
              _canonical_rows(db, sid) == 2, str(_canonical_rows(db, sid)))

        # 写前守卫本身可单独测（"不碰文件即抛"）
        guard_err = None
        try:
            db._raise_if_db_corrupt()
        except StateDbCorruptError as exc:
            guard_err = exc
        check("写前守卫 `_raise_if_db_corrupt()` 直接抛 StateDbCorruptError",
              isinstance(guard_err, StateDbCorruptError))
        check("隔离状态是持久的：再写一次仍然立刻失败（不是一次性）",
              not _can_write(db, sid))


# ═══════════════════════════════════ 10. close() 后拒绝重开 + 关停不 checkpoint
def test_close_refuses_reopen_and_never_checkpoints() -> None:
    # ① 被隔离的句柄：close() 之后拒绝重开（真实版 `_reopen_after_close_locked` :888/900）
    with fresh_db() as db:
        _seed(db)
        try:
            db._execute_write(_raise_structural)
        except StateDbCorruptError:
            pass
        db.close()
        check("close() 之后 _closed=True", db._closed is True)
        reopen_err = None
        try:
            db._reopen_after_close("write")
        except StateDbCorruptError as exc:
            reopen_err = exc
        check("被隔离的句柄 close() 后**拒绝重开**（StateDbCorruptError）",
              isinstance(reopen_err, StateDbCorruptError), type(reopen_err).__name__)
        write_err = None
        try:
            db._execute_write(lambda c: None)
        except StateDbCorruptError as exc:
            write_err = exc
        check("close() 后被隔离句柄再写入也立即失败（写前守卫先拦）",
              isinstance(write_err, StateDbCorruptError))

    # ② 健康句柄：close() 之后仍可重开（只有被隔离的才拒 —— 隔离是"对受损文件的保护"）
    with fresh_db() as db:
        _seed(db)
        db.close()
        conn = db._reopen_after_close("read")
        check("健康句柄 close() 后照常重开（只有被隔离的才拒）",
              conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2)
        db.close()

    # ③ 文字凭证：close() 明确"关停不 checkpoint" + 引用真实版行号（A7 第 4 条）
    root = Path(__file__).resolve().parent.parent
    src = (root / "hermes_state.py").read_text(encoding="utf-8")
    check("close() 写着这是 §2 的**第二道防线**（关停不 checkpoint）", "第二道防线" in src)
    check("close() 引用真实版 `:1449-1457`（跳过 wal_checkpoint 那一处）", ":1449-1457" in src)
    check("模块 docstring 写明 `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE` **不实现**（B8）",
          "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE" in src and "不实现" in src)


# ════════════════════════════════════════ 11. repair --check-only 的三态判定
def test_repair_check_only_verdicts() -> None:
    # ① 健康库 → 健康（exit 0）
    d = Path(_HOME) / ("repair%d" % next(_SEQ))
    path = d / "state.db"
    with _home_scope(d):
        db = hermes_state.SessionDB(path)
        _seed(db)
        db.close()
        v = hermes_state_repair.inspect_state_db(path)
        check("健康库：探针通过 · 无标记 · 触发器齐 · healthy=True",
              v["opens_cleanly"] and v["marker"] is None
              and not v["missing_triggers"] and v["healthy"], repr(v["marker"]))
        check("健康库：`repair --check-only` 退出码 = EXIT_HEALTHY(0)",
              sessions_cmd.cmd_repair(_ns(check_only=True)) == sessions_cmd.EXIT_HEALTHY)

    # ② 降级库（FTS 已 fail-open）→ 不健康（exit 1）：能开、但标记在场 + 触发器缺
    d2 = Path(_HOME) / ("repair%d" % next(_SEQ))
    path2 = d2 / "state.db"
    with _home_scope(d2):
        db, sid = _make_degraded_db(path2)
        db.close()
        v2 = hermes_state_repair.inspect_state_db(path2)
        check("降级库：探针通过，但标记='1' + 三个触发器全缺 ⇒ healthy=False",
              v2["opens_cleanly"] and v2["marker"] == "1"
              and len(v2["missing_triggers"]) == 3 and not v2["healthy"],
              f"marker={v2['marker']!r} missing={v2['missing_triggers']}")
        check("降级库：`repair --check-only` 退出码 = EXIT_UNHEALTHY(1)",
              sessions_cmd.cmd_repair(_ns(check_only=True)) == sessions_cmd.EXIT_UNHEALTHY)

    # ③ 结构损坏库（文件级坏）→ 不健康（exit 1），且 repair 拒绝假装能修
    d3 = Path(_HOME) / ("repair%d" % next(_SEQ))
    path3 = d3 / "state.db"
    with _home_scope(d3):
        db = hermes_state.SessionDB(path3)
        _seed(db)
        db.close()
        _nuke_db_file(path3)
        check("结构损坏库：健康探针失败（第一条语句就抛 file is not a database）",
              hermes_state_repair.inspect_state_db(path3)["opens_cleanly"] is False,
              repr(hermes_state_repair.inspect_state_db(path3)["reason"]))
        check("结构损坏库：`repair --check-only` 退出码 = EXIT_UNHEALTHY(1)",
              sessions_cmd.cmd_repair(_ns(check_only=True)) == sessions_cmd.EXIT_UNHEALTHY)
        rep = hermes_state_repair.repair_state_db_schema(path3, backup=True)
        check("结构损坏库：repair 拒绝假装能修（repaired=False 且说明 FTS 重建修不了）",
              rep["repaired"] is False and "修不了" in (rep["error"] or ""),
              repr(rep["error"]))


# ═════════════════════════ 12. repair 真修回来 + 三段验证 + raw-copy 备份
def test_repair_fixes_verifies_and_leaves_raw_backup() -> None:
    d = Path(_HOME) / ("repair%d" % next(_SEQ))
    path = d / "state.db"
    with _home_scope(d):
        db, sid = _make_degraded_db(path)
        before_rows = _canonical_rows(db, sid)
        check("修复前置：标记='1' · 触发器空 · 索引里查不到降级窗口那条（空窗成立）",
              _marker(db) == "1" and _triggers(db) == []
              and _fts_match_count(db, "zetaquar11") == 0)
        db.close()

        rep = hermes_state_repair.repair_state_db_schema(path, backup=True)
        check("repair 成功：repaired=True，策略 = rebuild_fts",
              rep["repaired"] is True and rep["strategy"] == "rebuild_fts", repr(rep["error"]))

        # 三段验证查询（文档 §3）
        v = rep["verification"]
        check("三段验证 · ① 标记查询**零行**", v["marker_rows"] == 0 and v["marker_clear"])
        check("三段验证 · ② 预期触发器**在场**（与名单逐字一致）",
              v["triggers_present"] and v["triggers"] == sorted(_FTS_TRIGGERS), repr(v["triggers"]))
        check("三段验证 · ③ 权威行数**不得下降**",
              v["rows_not_decreased"] and v["rows_after"] >= before_rows,
              f"{v['rows_before']} → {v['rows_after']}")

        # ⑥ repair 默认留下 raw-copy 备份
        bp = Path(rep["backup_path"])
        check("repair 默认留下一份 raw-copy 备份文件", bool(rep["backup_path"]) and bp.exists())
        check("备份名形如 `state.db.malformed-backup-<stamp>`",
              ".malformed-backup-" in bp.name, bp.name)
        check("备份是**原样字节**拷贝（与源库主文件同大小）",
              bp.stat().st_size == path.stat().st_size)

        # ⑤ 修完之后：标记清空 · 触发器回来 · 搜索又走 FTS
        db2 = hermes_state.SessionDB(path)
        check("修复后：标记被清（state_meta 里那行没了）", _marker(db2) is None, repr(_marker(db2)))
        check("修复后：三个触发器回来了", _triggers(db2) == sorted(_FTS_TRIGGERS),
              repr(_triggers(db2)))
        check("修复后：降级窗口那条行终于能 MATCH 到（空窗被补上）",
              _fts_match_count(db2, "zetaquar11") == 1,
              str(_fts_match_count(db2, "zetaquar11")))
        check("修复后：搜索又走 FTS（route 回到 fts）",
              [h["route"] for h in db2.search_messages("zetaquar11")] == ["fts"],
              repr([h["route"] for h in db2.search_messages("zetaquar11")]))
        check("修复后：新写入立刻重新进索引（触发器真在工作）",
              db2.append_message(sid, "user", "修复之后的活写入 zetapostquar") is not None
              and _fts_match_count(db2, "zetapostquar") == 1)
        db2.close()

        check("修复后 `repair --check-only` 退出码 = 0（已健康）",
              sessions_cmd.cmd_repair(_ns(check_only=True)) == sessions_cmd.EXIT_HEALTHY)


# ══════════════════════════════════════════════════════════════════════════════ #
def test_legacy_db_without_state_meta_is_diagnosed_not_confused() -> None:
    """★ 回归（真跑在**真实 home** 上撞出来的）：第 11 章之前建的库**没有 `state_meta` 表**。

    旧写法里 `SELECT value FROM state_meta ...` 一抛 `OperationalError` 就把整份体检
    early-return 掉 ⇒ 明明在场的三个触发器被报成"缺 3 个"、行数报 `None`
    （**误诊比不诊更坏**：会把人骗去跑修复，或让人以为索引坏了）。
    契约：**逐项隔离** —— 触发器与行数照旧数得对，旧库单列一个病因。
    """
    import os, sqlite3, tempfile
    from hermes_state_repair import inspect_state_db

    home = tempfile.mkdtemp(prefix="mini_ch11_legacy_")
    os.environ["MINI_HOME"] = home
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import hermes_state
    importlib.reload(hermes_state)
    db = hermes_state.SessionDB()
    db.create_session("legacy", "cli", model="m")
    db.append_message("legacy", "user", content="旧库的一条")
    db.close()
    conn = sqlite3.connect(os.path.join(home, "state.db"))
    conn.execute("DROP TABLE state_meta")          # 把"第 11 章之前"这件事造出来
    conn.commit(); conn.close()

    v = inspect_state_db(os.path.join(home, "state.db"))
    check("★ 不崩、探针通过", v["opens_cleanly"] is True, str(v.get("reason")))
    check("★ 触发器**照旧数得对**（在场 3，不是被误报成缺 3）",
          not v["missing_triggers"] and len(v["triggers"]) == 3, f"{v['triggers']}")
    check("★ 行数**读得出来**（不再报 None）", v["canonical_rows"] == 1, str(v["canonical_rows"]))
    check("★ 标记读不到 ⇒ 单列病因（不冒充\"没有标记\"）", v["marker_readable"] is False)
    check("★ 判不健康（旧库待 repair 升级）", v["healthy"] is False)


def test_inspection_uri_preserves_special_path_characters() -> None:
    """Read-only URI must address the requested file, including URI metacharacters."""
    with tempfile.TemporaryDirectory(prefix="mini_inspect_uri_") as root:
        for index, label in enumerate(("profile#lab", "profile?lab", "profile%23lab")):
            path = Path(root) / label / "state.db"
            db = hermes_state.SessionDB(path)
            try:
                _seed(db)
                for extra in range(index):
                    db.append_message("s1", "user", "unique URI fixture %d" % extra)
            finally:
                db.close()
            before = path.read_bytes()
            siblings = set(Path(root).iterdir())
            v = hermes_state_repair.inspect_state_db(path)
            check("URI 特殊字符仍读取目标库：" + label,
                  v["healthy"] and v["canonical_rows"] == 2 + index, repr(v))
            check("只读体检不创建错误路径的库：" + label,
                  set(Path(root).iterdir()) == siblings)
            check("只读体检不改目标主文件：" + label, path.read_bytes() == before)


def test_inspection_detects_missing_fts_without_stale_marker() -> None:
    """Canonical rows and trigger names alone cannot prove the FTS index is readable."""
    with tempfile.TemporaryDirectory(prefix="mini_inspect_fts_") as root:
        path = Path(root) / "state.db"
        db = hermes_state.SessionDB(path)
        try:
            _seed(db)
        finally:
            db.close()
        conn = sqlite3.connect(path)
        try:
            conn.execute("DROP TABLE messages_fts")
            conn.commit()
        finally:
            conn.close()
        v = hermes_state_repair.inspect_state_db(path)
        check("FTS 缺失时权威消息与触发器仍在场",
              v["canonical_rows"] == 2 and not v["missing_triggers"] and v["marker"] is None)
        check("FTS 缺失不能被误报健康", not v["healthy"])
        check("FTS 读取失败留下独立病因", not v.get("fts_readable") and bool(v.get("reason_fts")))
        with _home_scope(root):
            check("FTS 缺失时 check-only 返回失败",
                  sessions_cmd.cmd_repair(_ns(check_only=True)) == sessions_cmd.EXIT_UNHEALTHY)


GROUPS = [
    ("1. 损坏分类谓词", "码优先于文本 · 三组不混 · 类型只是类型", [test_corruption_classification]),
    ("2. fts_stale 标记跨重启", "落在 state_meta · 关掉重开仍为真 · 触发器不被复活",
     [test_marker_is_durable_across_reopen]),
    ("3. fail-open 让权威写入继续", "写入仍成功 · 标记=1 · 三个触发器全摘 · 从未 rebuild",
     [test_fail_open_keeps_canonical_writes_alive]),
    ("4. 降级是持久的", "后续写入照常 · 搜索走 LIKE 且路径名标对",
     [test_degradation_persists_and_search_falls_back]),
    ("5. 结构化损坏停写", "StateDbCorruptError · 不重试 · 不误摘触发器",
     [test_structural_corruption_halts_without_retry]),
    ("6. 不变量：活路径不重建", "写/搜都不跑 rebuild · 三处文字凭证在位",
     [test_live_paths_never_rebuild]),
    ("7. 重建之后", "标记清 · 触发器回 · 空窗补上 · 搜索回 FTS", [test_rebuild_restores_fts]),
    ("8. 隔离 / 真损坏 / 名单", "库路径跟 MINI_HOME · 能真造坏就真造 · 名单唯一来源",
     [test_isolation_physical_damage_and_registry]),
    ("9. 结构损坏隔离（A7）", "粘性标志 · 写前守卫立即失败且不碰文件 · 不重试",
     [test_structural_quarantine_stops_writes_and_freezes_file]),
    ("10. close 后拒绝重开", "被隔离的句柄拒重开 · 健康句柄照常 · 关停不 checkpoint 文字凭证",
     [test_close_refuses_reopen_and_never_checkpoints]),
    ("11. repair --check-only 三态（+ 旧库）", "健康库/降级库/结构损坏库/旧库的判定与退出码",
     [test_repair_check_only_verdicts,
      test_legacy_db_without_state_meta_is_diagnosed_not_confused,
      test_inspection_uri_preserves_special_path_characters,
      test_inspection_detects_missing_fts_without_stale_marker]),
    ("12. repair 真修回来（A8）", "三段验证 · raw-copy 备份 · 标记清/触发器回/搜索回 FTS",
     [test_repair_fixes_verifies_and_leaves_raw_backup]),
]


def main() -> None:
    for title, promise, fns in GROUPS:
        print(f"══ {title} ══ {promise}")
        for fn in fns:
            # ★ 用例抛异常 = 一条**可诊断的红**（不是"套件崩了"）。
            #   反向验证的要求是"把机制改回旧写法 → 干净变红"：
            #   崩溃会把后面的组一起吞掉，那就看不出是哪条承诺破了。
            try:
                fn()
            except Exception as exc:                     # noqa: BLE001
                check(f"{fn.__name__} 抛异常", False, f"{type(exc).__name__}: {exc}")
        print(f"   → {len(PASS)} 累计 / {len(FAIL)} 失败\n")

    print(f"{len(PASS)} 通过 / {len(FAIL)} 失败　（{len(GROUPS)} 组）")
    if FAIL:
        print("失败项：" + "; ".join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
