"""命令行入口。

对应 Hermes 的 hermes_cli/main.py（`hermes chat` / `hermes setup` … 的 argparse 入口）。

用法：
    python3 -m hermes_cli.main                    # 交互式 REPL
    python3 -m hermes_cli.main -q "问题"          # 单次提问
    python3 -m hermes_cli.main --resume           # 恢复最近一次会话
    python3 -m hermes_cli.main --max-turns 8      # 改迭代预算
    python3 -m hermes_cli.main --list-toolsets    # 看 toolset（**不需要密钥**）
    python3 -m hermes_cli.main --doctor           # 网络/连通性体检（**不需要密钥**）
    python3 -m hermes_cli.main gateway run        # 第 7 章：跑网关（前台）
    python3 -m hermes_cli.main -p lab -q "问题"   # 第 7 章：用名为 lab 的 profile
    python3 -m hermes_cli.main profile list       # 第 7 章：有哪些 profile

★ 纪律：**只读的查询类参数必须在构造 MiniCLI 之前处理**。
  构造 agent 需要 API key（`MiniCLI.__init__` 会去解析 provider），
  而"我有哪些 toolset""我的网络通不通"跟密钥无关 ——
  放到构造之后，用户没配 key 时连这两个都问不了。
  （本文件原来还宣传过一个 `--doctor`：`doctor.py` 的 docstring 写着
   "或 `./mini --doctor`"，而启动器只是 `exec python3 -m hermes_cli.main "$@"`，
  那个参数同样不存在。两处一起修。）
"""
from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

from cli import MiniCLI


def _run_doctor() -> int:
    """跑 `doctor.py`（它是一个脚本，逻辑在 `__main__` 块里，所以用 runpy 跑）。

    不把它改成 `main()`：那是第 2 章的交付物，为一个入口重排它的结构不划算
    （改它就要重跑它的验证）。runpy 是 stdlib，代价 0。
    """
    script = Path(__file__).resolve().parent.parent / "doctor.py"
    if not script.exists():
        print(f"[配置错误] 找不到体检脚本：{script}", file=sys.stderr)
        return 2
    runpy.run_path(str(script), run_name="__main__")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mini-hermes", description="从零手写的 agent loop")
    parser.add_argument("-q", "--query", help="单次提问后退出（不进 REPL）")
    # ★ `-p/--profile`（第 7 章侧线）：**与真实版对齐** —— 真实版里 `-p` 就是 `--profile`
    #   （`hermes_cli/_parser.py:16`：PRE_ARGPARSE_INHERITED_FLAGS 里就有 ("-p", True)）。
    #   以前 mini 把 `-p` 当 provider 的短参数用了，这是一处已知的分歧，这里改回来；
    #   provider 保留长参数 `--provider`（不设短参数比设错短参数好）。
    parser.add_argument("-p", "--profile", default=None,
                        help="用哪个 profile（= 哪套 home/凭据/会话库）。不写就是默认 profile")
    parser.add_argument("--provider", default="deepseek", help="provider 名（默认 deepseek）")
    parser.add_argument("-m", "--model", default=None, help="模型名（默认取 provider 的默认模型）")
    parser.add_argument("--max-turns", type=int, default=500, help="最多迭代多少次（默认 500）")
    parser.add_argument("--resume", action="store_true", help="恢复最近一次会话")
    parser.add_argument("--quiet", action="store_true", help="只输出最终答复（看不到工具过程）")
    parser.add_argument("--debug", action="store_true", help="异常时直接把 traceback 打到 stderr")
    parser.add_argument(
        "--system",
        default=None,
        help="临时系统提示（只作用于本次请求，不写进历史）。"
        "也可以用环境变量 MINI_EPHEMERAL_SYSTEM_PROMPT",
    )
    parser.add_argument(
        "--platform", default="cli", choices=["cli", "tui"], help="界面平台（决定平台提示，默认 cli）"
    )
    parser.add_argument(
        "--list-toolsets",
        action="store_true",
        help="列出所有 toolset（名字 / 说明 / 工具 / 在不在默认捆里）后退出。不需要 API 密钥",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="跑网络体检（环境→DNS→逐地址连通性→分层计时→真实调用）后退出。不需要 API 密钥",
    )
    # ★ 第 7 章：`gateway` 子命令（对应 `hermes gateway …`）。
    #   它**必须在构造 MiniCLI 之前**处理 —— 网关自己要建 agent，不需要先建一个。
    # ★ 第 11 章：`sessions` 子命令（对应 `hermes sessions repair`）—— 同理，
    #   修库这件事不该先建一个 agent（库本身可能就打不开）。
    from hermes_cli.gateway_cmd import add_gateway_parser
    from hermes_cli.profile_cmd import add_profile_parser
    from hermes_cli.sessions_cmd import add_sessions_parser

    sub = parser.add_subparsers(dest="command")
    add_gateway_parser(sub)
    add_profile_parser(sub)
    add_sessions_parser(sub)

    args = parser.parse_args(argv)

    # ---- 子命令：不建 MiniCLI，直接跑 ----
    if getattr(args, "command", None) in ("gateway", "profile", "sessions"):
        return int(args.func(args))

    # ---- `-p/--profile`：**在一切取路径的代码之前**把它钉住 ----
    # ★ 两个顺序要求，都在这一小段里：
    #   ① 必须在构造 MiniCLI **之前**（MiniCLI 一构造就会解析 provider、建会话库）；
    #   ② 必须在 `--doctor` / `--list-toolsets` 之前 ——
    #      `mini -p lab --doctor` 问的是"**lab 这个 profile** 的网络通不通"。
    #   ★ 为什么是设环境变量而不是调作用域 API：CLI 里"这个进程就是那个 profile"
    #     （真实版的说法：hermes 进程的**启动 profile**）。作用域（contextvar）
    #     是给"一个进程服务多个 profile"用的，不是给"换一个 profile 启动"用的。
    if args.profile:
        from hermes_cli.profiles import ProfileError, resolve_profile_home
        from hermes_constants import display_path

        try:
            home = resolve_profile_home(args.profile)
        except ProfileError as exc:
            print(f"[配置错误] {exc}", file=sys.stderr)
            return 2
        os.environ["MINI_HOME"] = str(home)
        print(f"ⓘ profile {args.profile!r} → home={display_path(home)}", file=sys.stderr)

    # ---- 只读的查询类参数：**在构造 MiniCLI 之前**处理 ----
    # 它们问的是"这套代码里有什么""我的网络通不通"，与密钥无关。
    # 放到构造之后的话，没配 key 的人连这两个都问不了（会先撞 [配置错误]）。
    if args.list_toolsets:
        import toolsets

        print(toolsets.describe_toolsets())
        # ⚠️ 这一句留在**调用方**、不放进 describe_toolsets()：
        #    那句"进 REPL 敲 /tools"在由 /tools 自己打印时是循环的（用户已经在 /tools 里）。
        #    所以共享的那份只说 toolset 本身，各自补自己的下一步提示。
        print("\n（这只是静态清单。某个工具**此刻放不放行**由运行时的 check_fn 决定 —— "
              "例：web_search 要有 key。那道闸门看 REPL 里的 /tools。）")
        return 0
    if args.doctor:
        return _run_doctor()

    try:
        app = MiniCLI(
            provider=args.provider,
            model=args.model,
            max_turns=args.max_turns,
            resume=args.resume,
            quiet=args.quiet and bool(args.query),
            debug=args.debug or bool(os.environ.get("MINI_DEBUG")),
            ephemeral_system_prompt=args.system,
            platform=args.platform,
        )
    except RuntimeError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    if args.query:
        try:
            result = app.one_shot(args.query)
        except KeyboardInterrupt:
            app.agent.interrupt()
            print("(已中断)", file=sys.stderr)
            return 130
        if args.quiet:
            print(result["final_response"])
        # ⚠️ 退出码必须反映失败 —— 否则脚本调用方分不清"答完了"和"炸了"。
        #    （之前那个 bug：turn 里抛异常、一个字不打印、退出码还是 0。）
        if result.get("error"):
            return 1
        if result.get("interrupted"):
            return 130
        return 0

    app.repl()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
