"""`./mini profile …` 子命令（对应真实版 `hermes profile …`）。

真实版这一族有 create / list / show / delete / rename / …（还有 `--clone`、`--no-skills`）。
mini 只做**最小可用**的三个动作 —— 判据是"每个动作都要有真实消费者"：

| 动作 | 消费者（谁会用它） |
|---|---|
| `create <名字>` | 想开一个实验/沙箱 profile：`mini profile create lab --clone` |
| `list` | 忘了自己有哪些 profile（也是 `-p` 报错时提示的内容） |
| `show [名字]` | 想确认"这个 profile 的家在哪、里有什么、有几个会话" |

`delete` / `rename` **没做**：删一个 home 是破坏性操作，而 mini 没有回收站/确认流程；
用户真要删，`rm -rf` 比我们写一个更清楚（真做的话要先想清楚"删掉的会话去哪"）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from hermes_cli.profiles import (ProfileError, create_profile, default_home,
                                 list_profiles, profile_home, profile_info,
                                 profiles_root, resolve_profile_home)

# ---------------------------------------------------------------- 每个动作


def cmd_profile_create(args) -> int:
    try:
        home = create_profile(args.name, clone=args.clone, base_home=args.from_home)
    except ProfileError as exc:
        print(f"⛔ {exc}")
        return 2
    print(f"✅ 已创建 profile {args.name!r}：{home}")
    source = args.from_home or default_home()
    if args.clone:
        print(f"   （--clone：已从 {source} 复制配置与身份；**没有**复制记忆 —— 记忆不是配置）")
        print("   ⚠️ 复制的 .env 里是**默认 profile 的密钥**；"
              "要分开用/分开记账就把它们换掉（或清空重填）。")
    else:
        print("   （空凭据文件已播种 —— 从第一天起它有自己的 .env，还没有任何密钥）")
    print(f"   用它：  mini -p {args.name} --doctor")
    print(f"   跑起来：mini -p {args.name}")
    return 0


def cmd_profile_list(args) -> int:
    root = profiles_root()
    names = list_profiles()
    print(f"profile 根：{root}")
    print(f"{'●' if not names else ' '} default      → {default_home()}   （默认 profile，永远存在）")
    for name in names:
        home = profile_home(name)
        db = home / "state.db"
        size = f"{db.stat().st_size / 1024:.0f} KB" if db.exists() else "还没有库"
        print(f"● {name:<12} → {home}   （{size}）")
    if not names:
        print("  （还没有别的 profile。新建：`mini profile create <名字>`）")
    print(f"\n共 {len(names)} 个具名 profile（+ 默认）。切换：`mini -p <名字>`")
    return 0


def cmd_profile_show(args) -> int:
    try:
        home = resolve_profile_home(args.name)
    except ProfileError as exc:
        print(f"⛔ {exc}")
        return 2
    info = profile_info(home)
    label = args.name or "default"
    print(f"profile   {label}")
    print(f"home      {info['home']}")
    if not info["exists"]:
        print("（这个 home 还不存在 —— 默认 profile 的 home 是用到才建的）")
        return 0
    print(f"凭据文件   {'有' if info['env'] else '没有'}（.env）")
    # ★ 3.9 兼容：f-string 里**不许嵌套同类引号**（`f"{f'...'}"` 在 3.9 直接语法错）。
    #   而且这个项目要同时在 3.9.6 与 3.13.14 上跑 —— 所以先把字符串算出来再拼。
    if info["state_db"]:
        kb = info["state_db_bytes"] / 1024
        db_text = f"有（{kb:.0f} KB）"
    else:
        db_text = "还没有"
    print(f"会话库     {db_text}")
    if "sessions" in info:
        print(f"会话数     {info['sessions']}")
    print(f"目录       {info['dirs'] or '（无）'}")
    print(f"文件       {info['files'] or '（无）'}")
    print(f"钩子       {sorted(p.name for p in (home / 'hooks').iterdir()) if (home / 'hooks').exists() else '（无）'}")
    return 0


# ---------------------------------------------------------------- 解析器


def add_profile_parser(sub) -> None:
    p = sub.add_parser("profile", help="管理 profile（create / list / show）")
    g = p.add_subparsers(dest="profile_cmd", required=True)

    c = g.add_parser("create", help="新建一个 profile（一个 profile = 一个 home）")
    c.add_argument("name", help="profile 名（字母/数字/._-，不许空格与 ~）")
    c.add_argument("--clone", action="store_true",
                   help="从默认 profile 复制配置与身份（.env / SOUL.md / config.json）—— **不复制记忆**")
    c.add_argument("--from", dest="from_home", default=None,
                   help="指定克隆源 home（默认：默认 profile）")
    c.set_defaults(func=cmd_profile_create)

    l = g.add_parser("list", help="列出所有 profile", aliases=["ls"])
    l.set_defaults(func=cmd_profile_list)

    s = g.add_parser("show", help="看某个 profile 的家在哪、里有什么")
    s.add_argument("name", nargs="?", default=None, help="不写就是默认 profile")
    s.set_defaults(func=cmd_profile_show)
