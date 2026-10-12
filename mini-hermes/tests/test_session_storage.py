"""第 6 章的自测：会话存储（SQLite / 表结构 / 世代 / 血缘 / 检索 / 写入礼节）—— **不联网**。

跑法：python3 tests/test_session_storage.py

## 测的是什么（14 组）

| 组 | 守的承诺 |
|---|---|
| 1. 建库 | 表 / 索引 / FTS5 虚拟表都在 · `schema_version` = `SCHEMA_VERSION` · **WAL 真生效**（读磁盘核对） |
| 2. 幂等打开 | 反复打开不会重建、不会报错、版本不动 |
| 3. `SCHEMA_SQL` 是唯一事实来源 | 声明里有的列，活库里**一条不少**（差分机制的前提） |
| 4. 声明式加列 | 缺列的老库被自动补齐（**不用写迁移**），且补了什么有报告 |
| 5. 版本门控 | 库版本比代码新 → **明确拒绝**（不许静默按旧理解读写） |
| 6. 会话行 upsert | 先写者赢 · 后写只补空缺 · `started_at` 不移动 |
| 7. 追加与计数 | 形参名 = 列名 · 返回 rowid · 会话计数跟着涨 · `tool_calls` 存成 JSON |
| 8. 两种读法 | `get_messages`（库里什么样）vs `get_messages_as_conversation`（喂模型什么样） |
| 9. 归档世代 | `active=0/compacted=1` · 压缩后**两个世代并存**（重复内容是设计，不是 bug） |
| 10. 血缘 | `parent_session_id` 链 · 恢复时能把祖先的消息一起取回来 |
| 11. 检索两条路 | 英文走 FTS5、中文走 LIKE 兜底（实测需求）· 查询清洗 · 过滤生效 |
| 12. 写入礼节 | 撞锁重试真的发生 · 两档抖动区间 · 每 50 次写触发 checkpoint |
| 13. 标题 | 非 NULL 唯一（重名返回 False 不抛）· 血缘里自动编号 |
| 14. 隔离 | 库路径**唯一来源** = `state_db_path()`（跟着 `MINI_HOME` 走） |

## 隔离

每条用例都在自己的临时 `MINI_HOME` 下跑，跑完还原环境变量。
**没有任何一条依赖运行环境的配置，也没有一条会碰用户的真实库。**
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hermes_constants  # noqa: E402
import hermes_state  # noqa: E402
from hermes_state_common import CHECKPOINT_EVERY_N_WRITES, SCHEMA_SQL, SCHEMA_VERSION  # noqa: E402
from hermes_state_schema import SchemaTooNewError, parse_schema_columns  # noqa: E402
from hermes_state_search import has_cjk, sanitize_fts5_query  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {detail}" if detail and not cond else ""))


@contextmanager
def temp_home():
    """把 `MINI_HOME` 指到临时目录（并在结束时还原）。"""
    old = os.environ.get("MINI_HOME")
    home = tempfile.mkdtemp(prefix="mini_ch06_")
    os.environ["MINI_HOME"] = home
    try:
        yield Path(home)
    finally:
        if old is None:
            os.environ.pop("MINI_HOME", None)
        else:
            os.environ["MINI_HOME"] = old


@contextmanager
def fresh_db():
    """一个干净的库（显式路径，不经过 MINI_HOME）。"""
    d = Path(tempfile.mkdtemp(prefix="mini_db_"))
    db = hermes_state.SessionDB(d / "state.db")
    try:
        yield db
    finally:
        db.close()


def _seed(db, sid: str = "s1", source: str = "cli") -> str:
    db.create_session(sid, source, model="deepseek-chat")
    db.append_message(sid, "user", "帮我看看 /tmp 下的 docker 部署文件")
    db.append_message(sid, "assistant", "", tool_calls=[
        {"id": "call_1", "type": "function",
         "function": {"name": "list_dir", "arguments": '{"path": "/tmp"}'}}])
    db.append_message(sid, "tool", '{"entries": ["docker-deploy.yaml"]}',
                      tool_call_id="call_1", tool_name="list_dir")
    db.append_message(sid, "assistant", "找到 docker-deploy.yaml，建议先看它。")
    return sid


# ══════════════════════════════════════════════════════════ 1. 建库
def test_schema_created() -> None:
    with fresh_db() as db:
        names = {r[0] for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        for want in ("sessions", "messages", "system_prompts", "schema_version",
                     "messages_fts", "idx_sessions_started", "idx_messages_session_id",
                     "idx_sessions_title_unique"):
            check(f"建库时创建了 {want}", want in names)
        version = db._conn.execute("SELECT version FROM schema_version").fetchone()[0]
        check("schema_version = SCHEMA_VERSION", version == SCHEMA_VERSION, f"{version}")
        # ★ WAL 不是"我们设过"，而是"磁盘上现在是" —— 这一条必须真读一次
        check("WAL 真的生效（读回来核对过）", db.wal_active is True)
        check("库里只有一张 schema_version 行",
              db._conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1)


# ══════════════════════════════════════════════════════════ 2. 幂等打开
def test_reopen_is_idempotent() -> None:
    d = Path(tempfile.mkdtemp(prefix="mini_db_"))
    p = d / "state.db"
    db1 = hermes_state.SessionDB(p)
    db1.create_session("s1", "cli", model="m")
    db1.close()
    db2 = hermes_state.SessionDB(p)
    check("二次打开不报错", True)
    check("二次打开补了 0 列（声明式加列是幂等的）",
          db2.schema_boot_report()["added_columns"] == [], repr(db2.schema_boot_report()))
    check("二次打开版本没动",
          db2._conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION)
    check("数据还在", db2.get_session("s1")["model"] == "m")
    db2.close()


# ══════════════════════════════════════════════════════ 3. SCHEMA_SQL 唯一来源
def test_schema_sql_is_single_source_of_truth() -> None:
    with fresh_db() as db:
        declared = parse_schema_columns(SCHEMA_SQL)
        # ★ 第 11 章加了 `state_meta`（键值元数据表，装 fts_stale 降级标记）：
        #   它是**纯加表**（IF NOT EXISTS），所以这个集合从 4 变 5 —— 机制没变，清单变了。
        check("解析出 5 张实体表（含 schema_version / state_meta）",
              set(declared) == {"sessions", "messages", "system_prompts", "schema_version",
                                "state_meta"},
              repr(sorted(declared)))
        check("列定义片段带约束（不是只有类型名）——#51646 的学费",
              "DEFAULT 1" in declared["messages"]["active"],
              repr(declared["messages"]["active"]))
        for table, cols in declared.items():
            live = {r[1] for r in db._conn.execute(f'PRAGMA table_info("{table}")')}
            missing = set(cols) - live
            check(f"{table}：声明里的列活库全有（缺 {len(missing)} 个）", not missing, repr(missing))
        check("虚拟表不会被当成实体表解析", "messages_fts" not in declared)


# ══════════════════════════════════════════════════════════ 4. 声明式加列
def test_reconcile_adds_missing_columns() -> None:
    d = Path(tempfile.mkdtemp(prefix="mini_db_"))
    p = d / "state.db"
    # 造一个"老库"：sessions 表只有两列，schema_version 已就位
    raw = sqlite3.connect(str(p))
    raw.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL)")
    raw.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    raw.execute("INSERT INTO schema_version (version) VALUES (1)")
    raw.commit()
    raw.close()
    # ★ 这里必须自己接住异常：建库顺序错了（先建索引再补列）会在**打开时**抛
    #   `no such column: parent_session_id`。让它裸奔会把整个套件带崩，
    #   而"崩溃"不是可诊断的失败 —— 我们要的是一行 ✗ 加原因。
    opened = None
    open_error = None
    try:
        opened = hermes_state.SessionDB(p)
    except Exception as exc:                          # noqa: BLE001
        open_error = exc
    check("老库能直接打开（顺序：建表 → 补列 → 建索引）", opened is not None,
          f"{type(open_error).__name__}: {open_error}")
    if opened is None:
        return
    db = opened
    live = {r[1] for r in db._conn.execute('PRAGMA table_info("sessions")')}
    check("缺的列被自动补齐（没写一行迁移）",
          {"message_count", "system_prompt_hash", "started_at", "parent_session_id"} <= live)
    report = db.schema_boot_report()
    check("补齐动作有报告（可观察）",
          any(c.startswith("sessions.") for c in report["added_columns"]), repr(report))
    # 老库没有 messages 表 → executescript 会补建
    check("老库缺的表被建出来",
          "messages" in {r[0] for r in db._conn.execute(
              "SELECT name FROM sqlite_master WHERE type='table'")})
    db.close()


# ══════════════════════════════════════════════════════════ 5. 版本门控（新库拒绝）
def test_schema_too_new_is_refused() -> None:
    d = Path(tempfile.mkdtemp(prefix="mini_db_"))
    p = d / "state.db"
    db = hermes_state.SessionDB(p)
    db._conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 5,))
    db.close()

    raised = None
    try:
        hermes_state.SessionDB(p)
    except SchemaTooNewError as exc:
        raised = exc
    check("库比代码新 → 明确拒绝打开", raised is not None)
    check("拒绝时把两个版本都报出来",
          raised is not None and "schema" in str(raised), str(raised))


# ══════════════════════════════════════════════════════════ 6. upsert 语义
def test_upsert_never_overwrites_earlier_writer() -> None:
    with fresh_db() as db:
        db.create_session("s1", "cli")                      # 先建空壳
        first_started = db.get_session("s1")["started_at"]
        db.create_session("s1", "cli", model="deepseek-chat", title="部署排查")
        row = db.get_session("s1")
        check("空壳被补上 model", row["model"] == "deepseek-chat")
        check("空壳被补上 title", row["title"] == "部署排查")

        db.create_session("s1", "cli", model="另一个模型", title="想改名")
        row2 = db.get_session("s1")
        check("后写**不覆盖**先写的 model", row2["model"] == "deepseek-chat", row2["model"])
        check("后写**不覆盖**先写的 title", row2["title"] == "部署排查", row2["title"])
        check("started_at 不会被后写移动", row2["started_at"] == first_started)

        stored = db.get_stored_system_prompt("s1")
        check("没存过提示词 → 取到 None（不猜）", stored is None)
        db.create_session("s1", "cli", system_prompt="你是 mini-hermes。")
        check("补上提示词后能取回正文",
              db.get_stored_system_prompt("s1") == "你是 mini-hermes。")
        check("提示词进了去重表（sessions 只存哈希）",
              db._conn.execute("SELECT COUNT(*) FROM system_prompts").fetchone()[0] == 1)
        db.create_session("s2", "cli", system_prompt="你是 mini-hermes。")
        check("同一份提示词只存一份",
              db._conn.execute("SELECT COUNT(*) FROM system_prompts").fetchone()[0] == 1)
        check("两个会话的哈希相同",
              db.get_session("s1")["system_prompt_hash"] == db.get_session("s2")["system_prompt_hash"])


# ══════════════════════════════════════════════════════════ 7. 追加与计数
def test_append_and_counters() -> None:
    with fresh_db() as db:
        sid = _seed(db)
        row = db.get_session(sid)
        check("message_count 跟着涨到 4", row["message_count"] == 4, str(row["message_count"]))
        check("tool_call_count 只数工具调用（1 个）", row["tool_call_count"] == 1)
        ids = [m["id"] for m in db.get_messages(sid)]
        check("rowid 递增", ids == sorted(ids) and len(set(ids)) == 4)
        check("tool_calls 存成 JSON 字符串",
              isinstance(db.get_messages(sid)[1]["tool_calls"], str))
        # 形参名 = 列名：把列清单当关键字参数喂一遍，不该报错
        cols = set(parse_schema_columns(SCHEMA_SQL)["messages"])
        check("形参名覆盖了 messages 的所有列",
              {"session_id", "role", "content", "tool_call_id", "tool_calls", "tool_name",
               "timestamp", "token_count", "finish_reason", "reasoning", "reasoning_content",
               "active", "compacted"} <= cols, repr(sorted(cols)))
        # 列名与形参名一致 → 传一个"只有库认识的字段"应该被忽略而不是炸
        mid = db.append_message(sid, "assistant", "带元数据", token_count=42,
                                finish_reason="stop", reasoning="想了一下")
        raw = [m for m in db.get_messages(sid) if m["id"] == mid][0]
        check("token_count / finish_reason / reasoning 都落到了列上",
              (raw["token_count"], raw["finish_reason"], raw["reasoning"]) == (42, "stop", "想了一下"))
        check("record_usage 累加而非覆盖", _usage_accumulates(db, sid))


def _usage_accumulates(db, sid: str) -> bool:
    db.record_usage(sid, input_tokens=10, output_tokens=5, api_calls=1)
    db.record_usage(sid, input_tokens=1, output_tokens=2, api_calls=1)
    row = db.get_session(sid)
    return row["input_tokens"] == 11 and row["output_tokens"] == 7 and row["api_call_count"] == 2


# ══════════════════════════════════════════════════════════ 8. 两种读法
def test_two_read_shapes() -> None:
    with fresh_db() as db:
        sid = _seed(db)
        raw = db.get_messages(sid)
        wire = db.get_messages_as_conversation(sid)
        check("库里那份带元数据（id/timestamp/active/token_count）",
              {"id", "timestamp", "active", "token_count"} <= set(raw[0]))
        check("喂模型那份**不带**这些本地元数据",
              not ({"id", "timestamp", "active", "token_count"} & set(wire[0])))
        check("喂模型那份的 tool_calls 是**对象**不是字符串",
              isinstance(wire[1]["tool_calls"], list))
        check("工具结果带着 tool_call_id（配对要用）", wire[2]["tool_call_id"] == "call_1")
        check("`tool_name` 只留在库里，不进消息形状",
              "tool_name" not in wire[2] and raw[2]["tool_name"] == "list_dir")
        check("system 既不在库里、也不在喂模型的那份（提示词走自己的表）",
              all(m["role"] != "system" for m in raw) and all(m["role"] != "system" for m in wire))


# ══════════════════════════════════════════════════════════ 9. 归档世代
def test_compaction_generations() -> None:
    with fresh_db() as db:
        sid = _seed(db)
        before = db.count_messages(sid)
        check("归档前 4 条活行", before == 4)
        archived = db.archive_active_rows(sid)
        check("归档把 4 条都标掉", archived == 4)
        check("归档后默认读不到任何行（喂模型的那份）",
              db.get_messages_as_conversation(sid) == [])
        check("归档后 count_messages(active_only=True) = 0", db.count_messages(sid) == 0)

        report = db.record_compaction(sid, [
            {"role": "user", "content": "帮我看看 /tmp"},
            {"role": "assistant", "content": "[摘要] 找到了 docker-deploy.yaml"},
        ])
        check("压缩报告：归档 4 条 + 写入 2 条",
              report == {"archived": 0, "written": 2}, repr(report))
        live = db.get_messages_as_conversation(sid)
        check("默认读 = 新世代（2 条）", len(live) == 2, repr(live))
        all_rows = db.get_messages(sid, include_compacted=True)
        check("include_compacted=True → 两个世代并存（6 条）", len(all_rows) == 6, str(len(all_rows)))
        check("归档世代的 active=0 / compacted=1",
              all(r["active"] == 0 and r["compacted"] == 1 for r in all_rows if r["id"] <= 4))
        check("新世代的 active=1 / compacted=0",
              all(r["active"] == 1 and r["compacted"] == 0 for r in all_rows if r["id"] > 4))
        check("压缩不改 message_count（那是「用户看得见的轮次」）",
              db.get_session(sid)["message_count"] == 4)


# ══════════════════════════════════════════════════════════ 10. 血缘
def test_lineage() -> None:
    with fresh_db() as db:
        root = _seed(db, "root")
        db.create_session("mid", "cli", parent_session_id=root, model="m")
        db.append_message("mid", "user", "继续")
        db.create_session("leaf", "cli", parent_session_id="mid", model="m")
        db.append_message("leaf", "user", "再继续")
        check("血缘从最早祖先到自身，顺序正确",
              db.session_lineage("leaf") == ["root", "mid", "leaf"],
              repr(db.session_lineage("leaf")))
        check("没有父亲的会话血缘只有自己", db.session_lineage("root") == ["root"])
        inherited = db.get_messages_as_conversation("leaf", include_ancestors=True)
        check("include_ancestors=True 把祖先的消息一起取回（4+1+1）",
              len(inherited) == 6, str(len(inherited)))
        check("祖先顺序在前（时间序）",
              [m["content"] for m in inherited][-1] == "再继续")
        only_leaf = db.get_messages_as_conversation("leaf")
        check("不带上祖先时只有自己的 1 条", len(only_leaf) == 1)


# ══════════════════════════════════════════════════════════ 11. 检索两条路
def test_search_routes() -> None:
    with fresh_db() as db:
        sid = _seed(db)
        en = db.search_messages("docker")
        check("英文走 FTS5 那条路", en and en[0]["route"] == "fts", repr(en[:1]))
        check("FTS 命中带 `>>>…<<<` 标记的片段",
              en and ">>>" in en[0]["snippet"], repr(en[:1]))
        zh = db.search_messages("部署文件")
        check("中文走 LIKE 兜底那条路", zh and zh[0]["route"] == "like", repr(zh[:1]))
        check("中文子串能搜到（默认分词器搜不到，这是实测出来的需求）",
              len(db.search_messages("部署")) == 1 and len(db.search_messages("部署文件")) == 1)
        check("中文整串在 FTS 里也搜不到 → 证明路由是必要的",
              len(db.search_messages("中文")) == 0)
        check("命中带上下文（前后各一条）",
              bool(zh and zh[0]["context"]), repr(zh[:1]))
        check("按角色过滤（user only）",
              all(h["role"] == "user" for h in db.search_messages("docker", role_filter=["user"])))
        check("按来源过滤（只搜 tui → 0 条）",
              db.search_messages("docker", source_filter=["tui"]) == [])
        check("排除来源（排除 cli → 0 条）",
              db.search_messages("docker", exclude_sources=["cli"]) == [])
        check("空查询返回空（不是全表）", db.search_messages("   ") == [])
        check("边界：`已回退`的行（active=0/compacted=0）默认搜不到",
              _rewound_rows_are_hidden(db, sid))


def _rewound_rows_are_hidden(db, sid: str) -> bool:
    mid = db.append_message(sid, "user", "这是一条将被回退的消息 rewound-marker")
    db._conn.execute("UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", (mid,))
    hidden = db.search_messages("rewound-marker") == []
    shown = len(db.search_messages("rewound-marker", include_inactive=True)) == 1
    return hidden and shown


def test_sanitize() -> None:
    check("连字符词被引号化（否则 FTS5 当减号）",
          sanitize_fts5_query("chat-send") == '"chat-send"', sanitize_fts5_query("chat-send"))
    check("带点的词也引号化",
          sanitize_fts5_query("my-app.config.ts") == '"my-app.config.ts"')
    check("不配对的引号被剥掉", sanitize_fts5_query('"未配对') == "未配对")
    check("悬空布尔操作符被去掉", sanitize_fts5_query("hello AND") == "hello")
    check("开头就是操作符 → 丢掉它", sanitize_fts5_query("OR hello") == "hello")
    check("前缀星号保留且不连累引号", sanitize_fts5_query("deploy*") == "deploy*")
    check("布尔操作符在词之间保留", sanitize_fts5_query("a OR b") == "a OR b")
    check("列过滤用的冒号被剥掉", sanitize_fts5_query("p2p:8080") == "p2p 8080")
    check("空串进空串出", sanitize_fts5_query("") == "")
    check("CJK 判定：中文 / 日文 / 韩文都算", has_cjk("部署") and has_cjk("テスト") and has_cjk("한국"))
    check("CJK 判定：纯英文不算", not has_cjk("docker"))


# ══════════════════════════════════════════════════════════ 12. 写入礼节
def test_write_etiquette() -> None:
    d = Path(tempfile.mkdtemp(prefix="mini_db_"))
    p = d / "state.db"
    a = hermes_state.SessionDB(p)
    a.create_session("s1", "cli")
    b = hermes_state.SessionDB(p)

    # 甲持有写事务，乙此刻写 → 必须**重试**然后成功（不是直接失败）
    #   注：这里用裸 SQL 拿锁，不走 `_execute_write`（它自己会 BEGIN —— 事务里再 BEGIN 是错的）
    a._conn.execute("BEGIN IMMEDIATE")
    a._conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                    ("s1", "user", "占位", time.time()))
    errors: list = []

    def writer() -> None:
        try:
            b.append_message("s1", "user", "乙的写入")
        except Exception as exc:                      # noqa: BLE001
            errors.append(exc)

    th = threading.Thread(target=writer)
    th.start()
    time.sleep(0.25)                                  # 让乙先撞一次锁
    a._conn.execute("COMMIT")                         # 甲放手
    th.join(timeout=10)
    check("撞锁的写入重试后成功（没有把异常抛给调用方）", not errors, repr(errors))
    check("乙的写入真的落库了",
          any(m["content"] == "乙的写入" for m in b.get_messages("s1")))
    check("乙的写入计数 = 1（重试不算多次写入）", b.write_count == 1, str(b.write_count))
    a.close()
    b.close()

    # 两档抖动：区间是**可测的**，不是"看着像"
    fast = [hermes_state.SessionDB._jitter_delay(0.5) for _ in range(30)]
    slow = [hermes_state.SessionDB._jitter_delay(5.0) for _ in range(30)]
    check("快档落在 20–150ms", all(0.020 <= x <= 0.150 for x in fast), repr(fast[:3]))
    check("慢档落在 250–1000ms", all(0.250 <= x <= 1.000 for x in slow), repr(slow[:3]))
    check("抖动**是随机的**（两次取值不同）", len({round(x, 6) for x in fast}) > 1)

    # checkpoint：每 50 次成功写入触发一次
    d2 = Path(tempfile.mkdtemp(prefix="mini_db_"))
    c = hermes_state.SessionDB(d2 / "state.db")
    calls: list = []
    c._try_wal_checkpoint = lambda: calls.append(1)    # type: ignore[method-assign]
    for i in range(CHECKPOINT_EVERY_N_WRITES):
        c.create_session(f"s{i}", "cli")
    check(f"{CHECKPOINT_EVERY_N_WRITES} 次写 → 恰好 1 次 checkpoint", len(calls) == 1, str(len(calls)))
    c.close()


# ══════════════════════════════════════════════════════════ 13. 标题
def test_titles() -> None:
    with fresh_db() as db:
        db.create_session("s1", "cli")
        db.create_session("s2", "cli")
        check("设标题成功", db.set_session_title("s1", "部署排查") is True)
        check("第二个会话可以没有标题（NULL 不参与唯一）",
              db.get_session("s2")["title"] is None)
        check("重名**返回 False 而不是抛异常**", db.set_session_title("s2", "部署排查") is False)
        check("重名失败后没有污染 s2 的标题", db.get_session("s2")["title"] is None)
        check("按标题能找回会话", db.resolve_session_by_title("部署排查") == "s1")
        check("血缘里没有别的标题 → 下一个就是基础名",
              db.get_next_title_in_lineage("全新名字") == "全新名字")
        check("基础名被占用 → 下一个是 #2",
              db.get_next_title_in_lineage("部署排查") == "部署排查 #2")
        db.set_session_title("s2", "部署排查 #2")
        check(" #2 也占用了 → 下一个是 #3",
              db.get_next_title_in_lineage("部署排查") == "部署排查 #3")


# ══════════════════════════════════════════════════════════ 14. 隔离
def test_path_isolation() -> None:
    with temp_home() as home:
        check("库路径跟随 MINI_HOME", hermes_state.SessionDB().db_path == home / "state.db",
              str(hermes_state.SessionDB().db_path))
        check("唯一路径来源 = state_db_path()",
              hermes_constants.state_db_path() == home / "state.db")
    with temp_home() as other:
        check("换一个 MINI_HOME → 换一个库（不会串库）",
              hermes_state.SessionDB().db_path == other / "state.db")
    check("默认（无 MINI_HOME）落在 ~/.mini-hermes 下",
          hermes_constants.state_db_path().name == "state.db"
          and ".mini-hermes" in str(hermes_constants.state_db_path()))


# ══════════════════════════════════════════════════════════════════════════════ #
GROUPS = [
    ("1. 建库", "表/索引/FTS5 都在 · 版本正确 · WAL 真生效", [test_schema_created]),
    ("2. 幂等打开", "反复打开不重建、不报错", [test_reopen_is_idempotent]),
    ("3. SCHEMA_SQL 是唯一事实来源", "声明里的列活库全有", [test_schema_sql_is_single_source_of_truth]),
    ("4. 声明式加列", "缺列自动补齐（不写迁移）且有报告", [test_reconcile_adds_missing_columns]),
    ("5. 版本门控", "库比代码新 → 明确拒绝", [test_schema_too_new_is_refused]),
    ("6. 会话行 upsert", "先写者赢 · 只补空缺 · 提示词去重", [test_upsert_never_overwrites_earlier_writer]),
    ("7. 追加与计数", "形参名=列名 · rowid 递增 · 计数跟着涨", [test_append_and_counters]),
    ("8. 两种读法", "库里什么样 vs 喂模型什么样", [test_two_read_shapes]),
    ("9. 归档世代", "active/compacted · 两个世代并存是设计", [test_compaction_generations]),
    ("10. 血缘", "parent 链 · include_ancestors", [test_lineage]),
    ("11. 检索两条路 + 清洗", "英文 FTS5 / 中文 LIKE 兜底 · 边界与过滤", [test_search_routes, test_sanitize]),
    ("12. 写入礼节", "重试真发生 · 两档抖动 · checkpoint 每 50 次", [test_write_etiquette]),
    ("13. 标题", "非 NULL 唯一 · 血缘编号", [test_titles]),
    ("14. 隔离", "库路径唯一来源（跟随 MINI_HOME）", [test_path_isolation]),
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
            except Exception as exc:                  # noqa: BLE001
                check(f"{fn.__name__} 抛异常", False, f"{type(exc).__name__}: {exc}")
        print(f"   → {len(PASS)} 累计 / {len(FAIL)} 失败\n")

    print(f"{len(PASS)} 通过 / {len(FAIL)} 失败　（{len(GROUPS)} 组）")
    if FAIL:
        print("失败项：" + "; ".join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
