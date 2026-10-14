"""第 11 章 · 真跑：两条损坏路径 + 显式修复（真造损坏，不是模拟）。

| 路径 | 怎么造 | 期望 |
|---|---|---|
| ① FTS 派生索引损坏 | 直接删掉 FTS 影子表的数据页（SQLite 会报 `fts5: corruption found reading blob…`，code 267） | 权威写入**照常成功**、落 `fts_stale='1'`、**三个触发器被摘**、搜索改走 `LIKE`（路由名 `like_scan_fts_stale`）、**从未 rebuild** |
| ② 文件本身损坏 | 覆写第一页之后的数据页（`database disk image is malformed`，无 FTS provenance） | 下一次写入抛 `StateDbCorruptError`、之后每次写入**立即失败**、**文件指纹逐字不变**（停写就是保护）、`close()` 后拒重开 |

然后跑 `sessions repair --check-only` / `sessions repair`：备份 → 重建 → 三段验证（标记零行 / 触发器在场 / 权威行数不降）。

部分 SQLite 版本禁止修改 FTS 影子表，此时明确跳过路径 ①，仍验证路径 ②。
用法：`python3 demos/ch11_real_run.py`
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

W = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(W))
# 临时数据仅属于本次演示；包括异常退出路径也统一清理。
_TEMP_ROOT = tempfile.TemporaryDirectory(prefix="mini-clean-ch11_real_run-")
try:
    os.environ["MINI_HOME"] = tempfile.mkdtemp(prefix="mini-ch11-real-", dir=_TEMP_ROOT.name)

    import hermes_state  # noqa: E402
    import hermes_state_repair as repair  # noqa: E402
    from hermes_cli.sessions_cmd import _check_only  # noqa: E402

    DB = Path(os.environ["MINI_HOME"]) / "state.db"
    OK = lambda b: "✓" if b else "✗"          # noqa: E731
    fails: list = []


    def check(label: str, cond: bool, detail: str = "") -> None:
        print(f"  {OK(cond)} {label}{('　→ ' + detail) if detail else ''}")
        if not cond:
            fails.append(label)


    def fingerprint(p: Path) -> str:
        """库文件 + 两个边车一起算（写入可能全落在 -wal 里）。"""
        h = hashlib.sha256()
        parts = []
        for q in (p, Path(str(p) + "-wal"), Path(str(p) + "-shm")):
            h.update(q.read_bytes() if q.is_file() else b"(missing)")
            parts.append(f"{q.name}:{q.stat().st_size if q.is_file() else 0}")
        return h.hexdigest()[:16] + " " + " ".join(parts)


    def seed(db, sid: str, n: int = 6) -> int:
        """用 mini 自己的 API 播种（`create_session` + `append_message`）。"""
        db.create_session(sid, "cli", model="deepseek-chat")
        for i in range(n):
            db.append_message(sid, "user", content=f"第 {i} 个模块帮我重构一下")
        return n


    def triggers() -> list:
        c = sqlite3.connect(DB)
        try:
            return [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'messages_fts%' ORDER BY name")]
        finally:
            c.close()


    def marker() -> str:
        c = sqlite3.connect(DB)
        try:
            r = c.execute("SELECT value FROM state_meta WHERE key='fts_stale'").fetchone()
            return r[0] if r else "(无行)"
        except sqlite3.Error:
            return "(无 state_meta 表)"
        finally:
            c.close()


    print(f"解释器 {sys.executable} · Python {sys.version.split()[0]} · 库 {DB}")
    print(f"临时 MINI_HOME = {os.environ['MINI_HOME']}\n")

    # ══════════════════════════════ 路径 ①：FTS 派生索引损坏
    print("══════ 路径 ①：FTS 派生索引损坏（派生索引坏了，权威写入必须照常）")
    db = hermes_state.SessionDB()
    sid = "s1"
    seed(db, sid)
    n_ok = repair._count_canonical_rows(DB)
    print(f"  种子：权威行 {n_ok} 行 · 触发器 {len(triggers())} 个 · 标记 {marker()}")

    raw = sqlite3.connect(DB)
    fts_damage_supported = True
    try:
        raw.execute("DELETE FROM messages_fts_data")  # 真删 FTS 影子表的数据页
        raw.commit()
    except sqlite3.OperationalError as exc:
        if "may not be modified" not in str(exc):
            raise
        fts_damage_supported = False
        print("  跳过路径 ①：当前 SQLite 禁止修改 FTS 影子表，未制造索引损坏。")
    finally:
        raw.close()

    if fts_damage_supported:
        before_fp = fingerprint(DB)
        try:
            db.append_message(sid, "user", content="索引坏着，我也得能写进去")
            wrote, err = True, ""
        except Exception as exc:                                # noqa: BLE001
            wrote, err = False, f"{type(exc).__name__}: {exc}"
        check("① 权威写入**照常成功**（fail-open 生效）", wrote, err)
        check("① 落了持久标记 fts_stale = '1'", marker() == "1", f"marker={marker()}")
        check("① **三个触发器全被摘**", triggers() == [], f"剩 {triggers()}")
        check("① 库文件**被改过**（写入确实发生了，不是空转）", fingerprint(DB) != before_fp)
        hits = db.search_messages("模块")
        route = db._describe_search_path("模块")
        print(f"  · 搜索：命中 {len(hits) if hits is not None else '?'} 条 · 路由 {route}")
        check("① 降级期间搜索**不试坏索引**（路由 like_scan_fts_stale）",
              route == "like_scan_fts_stale", f"route={route}")

        print("\n  ── `sessions repair --check-only`（降级库应判不健康）")
        rc_before = _check_only(DB)
        check("① check-only 判**不健康**（退出码非 0）", rc_before != 0, f"退出码 {rc_before}")

        print("\n  ── `sessions repair`（备份 → 重建 → 三段验证）")
        res = repair.repair_state_db_schema(DB, backup=True)
        print(f"  · 结果：{json.dumps({k: str(v) for k, v in res.items()}, ensure_ascii=False)[:300]}")
        check("① 修复后标记被清（零行）", marker() == "(无行)", f"marker={marker()}")
        check("① 修复后**三个触发器都回来了**", len(triggers()) == 3, f"{triggers()}")
        check("① 修复后权威行数**不降**", repair._count_canonical_rows(DB) >= n_ok,
              f"{n_ok} → {repair._count_canonical_rows(DB)}")
        bkp = sorted(Path(os.environ["MINI_HOME"]).glob("state.db*backup*"))
        check("① 默认留下了一份 raw-copy 备份", bool(bkp), f"{[b.name for b in bkp]}")
        hits2 = db.search_messages("模块")
        route2 = db._describe_search_path("模块")
        print(f"  · 修复后搜索：命中 {len(hits2) if hits2 is not None else '?'} 条 · 路由 {route2}")
        rc_after = _check_only(DB)
        check("① 修复后 check-only 判**健康**（退出码 0）", rc_after == 0, f"退出码 {rc_after}")
    db.close()

    # ══════════════════════════════ 路径 ②：文件本身损坏
    print("\n══════ 路径 ②：文件本身损坏（停写就是保护）")
    db2 = hermes_state.SessionDB()
    sid2 = "s2"
    seed(db2, sid2)
    n2 = repair._count_canonical_rows(DB)
    db2.close()
    c = sqlite3.connect(DB); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.commit(); c.close()
    db2 = hermes_state.SessionDB()          # 新句柄：此时库还是好的（页面缓存是空的）
    print(f"  句柄已开（健康：schema 可解析）· 权威行 {n2} 行 · 文件 {DB.stat().st_size} 字节")
    with open(DB, "r+b") as fh:             # 只坏第 1 页以外 ⇒ 下一次写必须从盘上读坏页
        fh.seek(4096); fh.write(b"\xff" * (DB.stat().st_size - 4096)); fh.flush(); os.fsync(fh.fileno())
    print(f"  已把第 1 页之后的 {DB.stat().st_size - 4096} 字节全写成 0xFF（schema 页仍是好的）")

    fp1 = fingerprint(DB)
    # 探针 A：先走 `append_message` —— 它要编译 FTS 同步触发器 ⇒ 撞上的是 vtable 构造失败
    #   `vtable constructor failed: messages_fts`（码 11）。这个**文案既不含 FTS 损坏词、也不含
    #   结构损坏词**，上游词表 `("malformed", "file is not a database", "not a database",
    #   "database corruption")` 同样认不出 ⇒ 两个桶都不进 ⇒ **原样抛**（与上游同边界，非 mini 缺陷）。
    a1, a_code = "(没抛)", "?"
    try:
        db2.append_message(sid2, "user", content="先撞 vtable")
    except Exception as exc:                                 # noqa: BLE001
        a1, a_code = f"{type(exc).__name__}: {str(exc)[:60]}", getattr(exc, "sqlite_errorcode", "?")
    print(f"  · 探针 A（append_message / 要编译 FTS 触发器）：{a1}")
    print(f"    └ 码 {a_code} · classify = unknown ⇒ 原样抛（上游同边界，非 mini 缺陷）")

    # 探针 B：走**不牵动 FTS vtable** 的写法（create_session 只碰 sessions 表）
    #   ⇒ SQLite 读坏页 → `database disk image is malformed`（含词表里的 malformed）⇒ 三桶归位。
    try:
        db2.create_session("probe-1", "cli", model="m")
        b1 = "(没抛)"
    except Exception as exc:                                 # noqa: BLE001
        b1 = type(exc).__name__
    check("② 现场写入抛**类型化错误** StateDbCorruptError（这才是 §2 的场景）", b1 == "StateDbCorruptError", b1)

    errs = []
    for i in range(3):
        try:
            db2.create_session(f"probe-{i+2}", "cli", model="m"); errs.append("(没抛)")
        except Exception as exc:                             # noqa: BLE001
            errs.append(type(exc).__name__)
    check("② 之后 3 次写入**全部立即失败**，类型一致", errs == ["StateDbCorruptError"] * 3, f"{errs}")
    check("② **不碰文件**：库+边车指纹逐字不变（停写就是保护）", fingerprint(DB) == fp1,
          f"{fp1.split()[0]} → {fingerprint(DB).split()[0]}")
    try:
        db2._reopen_after_close("真跑取证"); reopened = "(竟重开了)"
    except Exception as exc:                                 # noqa: BLE001
        reopened = type(exc).__name__
    check("② 隔离句柄 close() 后**拒绝重开**", reopened == "StateDbCorruptError", reopened)
    try:
        db2.close()
    except Exception:                                        # noqa: BLE001
        pass
    rc2 = _check_only(DB)
    check("② check-only 对损坏文件判**不健康**", rc2 != 0, f"退出码 {rc2}")
    # 开库路径的边界（如实记）：新句柄撞上"文件根本不是库"时抛的是 SQLite 裸错
    try:
        hermes_state.SessionDB(); open_kind = "(竟开成功)"
    except Exception as exc:                                 # noqa: BLE001
        open_kind = type(exc).__name__
    print(f"  · 边界（如实）：新句柄 open 撞上损坏库时抛 {open_kind} —— 上游 §2 的隔离判定接在 **live write** 上，"
          f"开库这一侧不在它范围内（这条会进《源码讲解-11》的差异表）")

    print("\n══════ CLI 面（同一件事，从命令行走一遍）")
    argv = [sys.executable, "-m", "hermes_cli.main", "sessions", "repair", "--check-only"]
    o = subprocess.run(argv, capture_output=True, text=True, cwd=str(W),
                       env={**os.environ, "MINI_HOME": os.environ["MINI_HOME"]})
    out = ((o.stdout or "") + (o.stderr or "")).strip()
    last = out.splitlines()[-1][:110] if out else "(无输出)"
    print(f"  $ {' '.join(argv[1:])}\n    → 退出码 {o.returncode} · {last}")
    check("CLI check-only 报告数据库不健康", o.returncode == 1 and "不健康" in out
          and "Traceback" not in out, last)

    print("\n" + "═" * 62)
    print(f"结论：{'全部核验通过 ✓' if not fails else '有失败项：' + '；'.join(fails)}　（{len(fails)} 项失败）")
    raise SystemExit(1 if fails else 0)
finally:
    _TEMP_ROOT.cleanup()
