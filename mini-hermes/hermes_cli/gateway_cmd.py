"""`./mini gateway …` 子命令（对应 Hermes 的 `hermes gateway start|stop|status`）。

mini 与真实版的差别（都是可解释的）：

| 真实版 | mini | 为什么 |
|---|---|---|
| `start` 后台常驻（systemd / launchd / nohup 包装） | `run` **前台阻塞** | mini 不装服务；后台化会掩盖日志 |
| `stop` 认 profile 作用域 vs 全局（多路复用下停一个 profile 会被拒 78） | `stop` 只停这个 home 的 | mini 的多路复用是"一个进程服务多 home"，入口还是同一个 |
| `--all` 用 `ps aux` 全局扫描 | 支持，但靠标准匹配函数 | ★ 真实的规矩：**绝不用 argv 子串认进程** |
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path
from typing import List, Optional

from gateway import status as gw_status


def _homes(multiplex: bool = False) -> List[Path]:
    from hermes_constants import get_mini_home
    from hermes_cli.profiles import profiles_to_serve

    return profiles_to_serve(True) if multiplex else [get_mini_home()]


def cmd_run(args) -> int:
    from gateway.run import GatewayRunner

    # ★ home 由全局的 `-p/--profile` 决定（main.py 里已设 MINI_HOME）；
    #   这里的 namespace 只影响**会话键的命名空间**（多 profile 并存时区分）。
    runner = GatewayRunner(multiplex=args.multiplex, provider=args.provider,
                           model=args.model, debug=args.debug, profile=args.namespace)
    if args.echo is False:
        runner.echo = False
    return runner.run()


def cmd_status(args) -> int:
    from gateway.multiplex_mode import default_gateway_multiplexes
    from hermes_constants import get_mini_home

    # ★ 三态里的"未设置"时**不猜**：先看活网关记下的 served_profiles，再看显式开关
    #   （真实版 `default_gateway_multiplexes` 的纪律）。只看命令行旗标的话，
    #   "网关自己在多路复用"这件事在 status 里看不见。
    if args.multiplex is None:
        multiplex = bool(args.all or default_gateway_multiplexes(get_mini_home()))
    else:
        multiplex = bool(args.multiplex or args.all)
    found = False
    for home in _homes(multiplex):
        pid = gw_status.live_gateway_pid_for_home(home)
        state = gw_status.read_runtime_status(gw_status.state_file(home)) or {}
        mark = "🟢" if pid else "⚪"
        print(f"{mark} {home}")
        print(f"     网关 pid   {pid if pid else '（没有）'}")
        print(f"     状态记录   {state.get('state', '(无)')}"
              f"{'  (' + str(state.get('reason')) + ')' if state.get('reason') else ''}")
        if pid:
            found = True
    if not found:
        print("（没有任何活着的网关）")
    return 0


def cmd_stop(args) -> int:
    targets = _homes(args.all)
    stopped = 0
    for home in targets:
        pid = gw_status.live_gateway_pid_for_home(home)
        if not pid:
            continue
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped += 1
            print(f"✋ 已向 pid={pid} 发 SIGTERM（{home}）")
        except ProcessLookupError:
            print(f"（pid={pid} 已经不在了）")
        except PermissionError:
            print(f"⛔ 没有权限动 pid={pid}")
    if not stopped:
        print("（没有可停的网关）")
    return 0


def cmd_hooks(args) -> int:
    """看一眼钩子目录被解析成了什么（排查"钩子没生效"最快的一招）。"""
    from gateway.hooks import HookRegistry

    home = Path(args.home) if args.home else _homes(False)[0]
    reg = HookRegistry(hooks_dir=home / "hooks")
    loaded = reg.discover_and_load()
    print(f"hooks 目录  {home / 'hooks'}")
    print(f"加载到的钩子 {loaded or '（无）'}")
    for event in ("gateway:startup", "session:start", "session:end", "session:reset",
                  "agent:start", "agent:end", "command", "gateway:tick"):
        print(f"  {event:<16} 处理器 {reg.handler_count(event)} 个")
    if reg.errors:
        print("错误：")
        for e in reg.errors:
            print(f"  - {e}")
    return 0


def add_gateway_parser(sub) -> None:
    p = sub.add_parser("gateway", help="网关子命令（run / status / stop / hooks）")
    g = p.add_subparsers(dest="gateway_cmd", required=True)

    r = g.add_parser("run", help="前台跑网关（Ctrl-C 退出）")
    r.add_argument("--provider", default="deepseek")
    r.add_argument("-m", "--model", default=None)
    r.add_argument("--namespace", default=None,
                   help="会话键里的命名空间（多 profile 并存时区分用；"
                        "home 由全局的 `-p/--profile` 决定）")
    r.add_argument("--multiplex", dest="multiplex", action="store_true", default=None,
                   help="一个进程服务多个 profile home（密钥读取转 fail-closed）；"
                        "不给 = 三态里的\"未设置\"，由启动期预检裁决")
    r.add_argument("--no-multiplex", dest="multiplex", action="store_false",
                   help="显式钉死\"不多路复用\"（覆盖环境变量，也不跑预检）")
    r.add_argument("--debug", action="store_true")
    r.add_argument("--no-echo", dest="echo", action="store_false")
    r.set_defaults(func=cmd_run)

    s = g.add_parser("status", help="看网关活着没（PID + 启动时间指纹）")
    s.add_argument("--multiplex", dest="multiplex", action="store_true", default=None,
                   help="按多路复用看（不给 = 问活网关记的 served_profiles）")
    s.add_argument("--no-multiplex", dest="multiplex", action="store_false",
                   help="只看当前 profile 自己")
    s.add_argument("--all", action="store_true", help="所有 profile home 都看一遍")
    s.set_defaults(func=cmd_status)

    t = g.add_parser("stop", help="给网关发 SIGTERM")
    t.add_argument("--all", action="store_true")
    t.set_defaults(func=cmd_stop)

    h = g.add_parser("hooks", help="看钩子加载情况")
    h.add_argument("--home", default=None)
    h.set_defaults(func=cmd_hooks)
