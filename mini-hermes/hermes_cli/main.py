"""命令行入口。

对应 Hermes 的 hermes_cli/main.py（`hermes chat` / `hermes setup` … 的 argparse 入口）。

用法：
    python3 -m hermes_cli.main                    # 交互式 REPL
    python3 -m hermes_cli.main -q "问题"          # 单次提问
    python3 -m hermes_cli.main --resume           # 恢复最近一次会话
    python3 -m hermes_cli.main --max-turns 8      # 改迭代预算
"""
from __future__ import annotations

import argparse
import os
import sys

from cli import MiniCLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mini-hermes", description="从零手写的 agent loop")
    parser.add_argument("-q", "--query", help="单次提问后退出（不进 REPL）")
    parser.add_argument("-p", "--provider", default="deepseek", help="provider 名（默认 deepseek）")
    parser.add_argument("-m", "--model", default=None, help="模型名（默认取 provider 的默认模型）")
    parser.add_argument("--max-turns", type=int, default=500, help="最多迭代多少次（默认 500）")
    parser.add_argument("--resume", action="store_true", help="恢复最近一次会话")
    parser.add_argument("--quiet", action="store_true", help="只输出最终答复（看不到工具过程）")
    parser.add_argument("--debug", action="store_true", help="异常时直接把 traceback 打到 stderr")
    args = parser.parse_args(argv)

    try:
        app = MiniCLI(
            provider=args.provider,
            model=args.model,
            max_turns=args.max_turns,
            resume=args.resume,
            quiet=args.quiet and bool(args.query),
            debug=args.debug or bool(os.environ.get("MINI_DEBUG")),
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
