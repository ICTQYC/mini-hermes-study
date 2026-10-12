"""`./mini sessions …` 子命令（对应真实版 `hermes_cli/subcommands/sessions.py` + `hermes_cli/sessions_cmd.py`）。

第 11 章 §3：`hermes sessions repair` —— 把"修一个坏掉的库"变成一个**能真跑出收据**的入口。

★ 子命令面**只有两项**（B9 裁剪；真实版参数面见 `hermes_cli/subcommands/sessions.py:160-207`）：

| 真实版 | mini | 为什么 |
|---|---|---|
| `sessions repair` | **保留** | 本章唯一能"真跑"的入口（备份 + 重建 + 三段验证） |
| `sessions repair --check-only` | **保留** | 只体检不改；修复前必须先能看一眼 |
| `sessions repair --no-backup` | **不做** | mini 把"默认先 raw-copy 备份"定为**无开关的纪律** —— 备份是取证镜像（全策略失败时的唯一恢复源），不该能一键关掉 |
| `sessions recover --source/--output/--inspect-only` | **不做** | 离线恢复整条链（页级打捞 / lost_and_found / 复制 bundle + 指纹校验）超出本章最小版 |
| `--work-dir` / `--chunk-size` / `--allow-partial` / `--report` | **不做** | 都是 `recover` 的参数，随 recover 一起裁掉 |

★ 与真实版一致的**分工**：本文件只做 **CLI 外壳** —— 解析参数、打印人类可读结果、给退出码。
  真正的修复逻辑在 `hermes_state_repair.py`（真实版 `hermes_cli/sessions_cmd.py:103` 就是
  `from hermes_state_repair import _db_opens_cleanly, repair_state_db_schema`，只 import 不实现）。
"""
from __future__ import annotations

from hermes_constants import display_path, state_db_path

# 退出码常量（真实版这里写的是裸数字 `return 1`；mini 提成常量，让"退出码区分健康/不健康"
# 这句话有一个可被引用的落点 —— 脚本调用方靠它判断"该不该跑修复"）。
EXIT_HEALTHY = 0
EXIT_UNHEALTHY = 1
EXIT_NO_DB = 2


def _mark(ok: bool) -> str:
    return "✅ 是" if ok else "❌ 否"


def _check_only(db_path) -> int:
    """只体检不改（对应真实版 `_cmd_repair` 里 `--check-only` 提前 `return` 的那一段 `:107-112`）。

    逐项打印**人类可读**结果：健康探针 / `fts_stale` 标记 / 触发器集合 / 权威行数。
    退出码：健康 = 0，不健康 = 1（不健康又分两种病因 —— 见打印的结论行）。
    """
    from hermes_state_repair import inspect_state_db

    v = inspect_state_db(db_path)
    print("会话库体检（--check-only，不改任何东西）")
    print("  库文件     %s" % display_path(db_path))
    if not v["opens_cleanly"]:
        print("  健康探针   ❌ 打不开：%s" % v["reason"])
        print("\n结论：**不健康**（结构性损坏 —— FTS 重建修不了它；请走离线恢复或从备份恢复）")
        return EXIT_UNHEALTHY
    print("  健康探针   ✅ 能打开、schema 能解析")
    if not v.get("marker_readable"):
        print("  fts_stale  ❓ 读不到（%s）—— 这是**旧库**：第 11 章之前建的库没有 `state_meta` 表；"
              "跑 `sessions repair` 会补上" % (v.get("reason_marker") or "未知原因"))
    elif v["marker"] is None:
        print("  fts_stale  ✅ 没有（FTS 索引处于“完整”状态）")
    else:
        print("  fts_stale  ❌ 标记在场（值 = %r）—— FTS 处于“摘除/降级”状态" % v["marker"])
    if v["missing_triggers"]:
        print("  触发器     ❌ 缺 %d 个（在场 %d / 预期 %d）：%s"
              % (len(v["missing_triggers"]), len(v["triggers"]),
                 len(v["expected_triggers"]), ", ".join(v["missing_triggers"])))
    else:
        print("  触发器     ✅ 齐（在场 %d / 预期 %d）"
              % (len(v["triggers"]), len(v["expected_triggers"])))
    rows = v["canonical_rows"]
    print("  权威行数   %s（messages 表）" % ("读不出来（%s）" % v.get("reason_rows") if rows is None else rows))
    if v["healthy"]:
        print("\n结论：**健康**（标记零行 · 预期触发器在场）")
        return EXIT_HEALTHY
    if not v.get("marker_readable"):
        print("\n结论：**不健康**（旧库缺 `state_meta` 表 ⇒ 跑 `mini sessions repair` 升级并重建索引）")
        return EXIT_UNHEALTHY
    print("\n结论：**不健康**（FTS 降级 ⇒ 跑 `mini sessions repair` 重建索引）")
    return EXIT_UNHEALTHY


def cmd_repair(args) -> int:
    """`mini sessions repair [--check-only]`（真实版 `hermes_cli/sessions_cmd.py:101` `_cmd_repair`）。

    默认：先 raw-copy 备份 → 重建 FTS 索引（+ 触发器 + 清标记）→ 打印三段验证查询的结果。
    """
    from hermes_state_repair import repair_state_db_schema

    db_path = state_db_path()
    if not db_path.exists():
        print("⛔ 找不到会话库：%s（还没有会话，或 profile 选错了）" % display_path(db_path))
        return EXIT_NO_DB
    if getattr(args, "check_only", False):
        return _check_only(db_path)

    print("修复会话库：%s" % display_path(db_path))
    report = repair_state_db_schema(db_path, backup=True)

    # ① raw-copy 备份
    if report["backup_path"]:
        print("  ① raw-copy 备份   %s" % display_path(report["backup_path"]))
    else:
        print("  ① raw-copy 备份   未生成")

    # ② 修复
    if report["error"] and not report["repaired"]:
        print("  ② 修复            失败：%s" % report["error"])
        if not report["backup_path"]:
            print("\n⛔ 未能修好。结构性损坏请走离线恢复或从备份恢复，别在原地重试。")
        else:
            print("\n⛔ 未能修好（取证镜像已留下：%s）。" % display_path(report["backup_path"]))
        return EXIT_UNHEALTHY
    print("  ② 修复策略        %s（重建 FTS 索引 + 装回触发器 + 清 fts_stale 标记）"
          % report["strategy"])

    # ③ 三段验证查询
    v = report["verification"] or {}
    print("  ③ 三段验证查询：")
    print("     ① 标记查询零行   %s（fts_stale 行数 = %s）"
          % (_mark(bool(v.get("marker_clear"))), v.get("marker_rows")))
    print("     ② 预期触发器在场 %s（%d/%d）"
          % (_mark(bool(v.get("triggers_present"))), len(v.get("triggers") or []),
             len(v.get("expected_triggers") or [])))
    print("     ③ 权威行数不降   %s（%s → %s）"
          % (_mark(bool(v.get("rows_not_decreased"))), v.get("rows_before"), v.get("rows_after")))

    if report["repaired"]:
        print("\n✅ 已修好：FTS 索引完整、触发器在场、权威行数没少。")
        return EXIT_HEALTHY
    print("\n⛔ 修复完成但三段验证未全过：%s" % report["error"])
    return EXIT_UNHEALTHY


def add_sessions_parser(sub) -> None:
    """把 `sessions` 子命令注册进主解析器（真实版 `subcommands/sessions.py:160`）。"""
    p = sub.add_parser("sessions", help="会话库维护（repair）")
    g = p.add_subparsers(dest="sessions_cmd", required=True)

    r = g.add_parser("repair", help="修复受损的 state.db（备份 + 重建 FTS 索引 + 三段验证）")
    r.add_argument(
        "--check-only", action="store_true",
        help="只体检不改（健康探针 + 标记 + 触发器集合 + 权威行数）；"
             "退出码 0=健康 / 1=不健康",
    )
    r.set_defaults(func=cmd_repair)
