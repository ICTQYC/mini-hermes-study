"""mini-hermes 全局常量与路径解析。

对应 Hermes 的 hermes_constants.py：所有路径都从一处解析，禁止散落硬编码。
"""
from __future__ import annotations

import contextvars
import os
from pathlib import Path
from typing import Tuple

APP_NAME = "mini-hermes"

# ★ profile 作用域：进程级"当前 home"的覆盖层（对应 Hermes 的
#   `hermes_constants.set_hermes_home_override`）。
#   用 **contextvar** 而不是改 os.environ：一个进程服务多 profile 时，
#   并发跑的两个回合必须各自看到自己的 home；写 os.environ 会互相打架，
#   而且**子进程会继承**上一个 profile 的值。
_HOME_OVERRIDE: contextvars.ContextVar = contextvars.ContextVar(
    "mini_home_override", default=None)


def set_mini_home_override(path) -> object:
    """绑定当前执行上下文的最小 home（返回 token，交给 reset 用）。"""
    return _HOME_OVERRIDE.set(str(path))


def reset_mini_home_override(token) -> None:
    _HOME_OVERRIDE.reset(token)


def current_home_override():
    return _HOME_OVERRIDE.get()


def get_mini_home() -> Path:
    """minihome 目录（对应 Hermes 的 get_hermes_home()）。

    解析顺序：**作用域覆盖（contextvar）** → 环境变量 MINI_HOME → ``~/.mini-hermes``。
    测试用 MINI_HOME 重定向；一个进程服务多 profile 时由作用域绑定决定
    （见 `gateway/profile_scope.py::profile_runtime_scope`）。
    """
    override = _HOME_OVERRIDE.get()
    if override:
        return Path(override)
    env = os.environ.get("MINI_HOME")
    home = Path(env).expanduser() if env else Path.home() / ".mini-hermes"
    home.mkdir(parents=True, exist_ok=True)
    return home


def state_db_path() -> Path:
    """会话数据库路径（**唯一来源**，对应 Hermes 的 `get_hermes_home() / "state.db"`）。

    ★ 为什么路径必须只有一个来源：库是"按 home 隔离"的边界（Hermes 里一个 profile
      一个库）。一旦别处硬编码 `~/.mini-hermes/state.db`，测试重定向 `MINI_HOME` 时
      那条路径就会**悄悄指向用户的真实库** —— 不报错，只是写错了地方。
    """
    return get_mini_home() / "state.db"


def display_path(p: Path) -> str:
    """把绝对路径显示成人看的样子（在用户 home 底下就缩成 ``~/...``）。"""
    try:
        return f"~/{Path(p).relative_to(Path.home())}"
    except ValueError:
        return str(p)             # 不在 home 底下（比如 /tmp/mh_e2e），照实显示


def display_mini_home() -> str:
    """给人看的 MINI_HOME（对应 Hermes 的 ``display_hermes_home()``）。

    ★ **为什么显示和取路径要分成两个函数**：``MINI_HOME`` 是可以被重定向的
    （测试、E2E、CI 都这么干）。如果打印时写死 ``~/.mini-hermes``，
    那个提示就会**把用户指到一个不存在的文件上**——比不打印还坏。
    （对应的 Hermes 规矩也是两条：取路径用 ``get_hermes_home()``，
    给人看用 ``display_hermes_home()``，**不许硬编码**。）
    """
    return display_path(get_mini_home())


def logs_dir() -> Path:
    """错误日志目录。

    对应 Hermes 的 `~/.hermes/logs/`（`agent.log` / `errors.log`）。
    为什么要落盘而不是只打印：**用户看到的界面会滚过去，日志不会。**
    一个只在特定时机出现的异常，事后能翻到才有意义。
    """
    d = get_mini_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_context_cwd() -> str:
    """决定"哪个目录算当前项目目录"（只要路径）。见 ``resolve_context_cwd_source``。

    ★ **这里是唯一的实现** —— 提示词和**工具执行**都读它（见下方 source 的说明）。
    """
    return resolve_context_cwd_source()[0]


def resolve_context_cwd_source() -> Tuple[str, str]:
    """返回 ``(目录, 来源)``。来源取值：``"context_env"`` / ``"launch_env"`` / ``"getcwd"``。

    优先级（对应 Hermes 的 ``resolve_context_cwd`` + ``TERMINAL_CWD``）：

    =========================  ==============  ====================================
    来源                       返回值         什么时候
    =========================  ==============  ====================================
    ``MINI_CONTEXT_CWD``       context_env     显式指定（网关：一进程服务多会话）
    ``MINI_LAUNCH_CWD``        launch_env      用户敲命令时所在的目录
    ``os.getcwd()``            getcwd          兜底
    =========================  ==============  ====================================

    ★ **为什么要返回"来源"而不只是路径**：来源决定**可信度**。
      ``getcwd`` 是系统**猜**出来的——它可能是 mini-hermes 自己的安装目录
      （有人绕过 `mini` 启动脚本直接跑模块时就是这样）。
      而"兜底猜出来的 cwd"不该获得"加载项目规则"的权力，
      否则**仓库里的 `AGENTS.md` 会被当成用户的项目规则注入**。
      见 ``project_context_suppressed``。

    ★ **为什么需要这一层**：``mini`` 启动脚本会 ``cd`` 到项目根——
      因为 Python 靠"脚本目录在 sys.path 上"才能 import 自己的模块。
      但那一步会把**用户的目录换掉**，后果是：

      - ``AGENTS.md`` 的发现会去看 mini-hermes 自己的目录（而不是用户的项目）
      - git 工作区快照会探到 mini-hermes 那个仓库

      **"进程从哪个目录启动"和"用户的项目在哪个目录"是两件事。**
      Hermes 管这个叫 launch artifact（启动残留物）。
    """
    for env_key, source in (("MINI_CONTEXT_CWD", "context_env"), ("MINI_LAUNCH_CWD", "launch_env")):
        value = os.environ.get(env_key)
        if value and Path(value).is_dir():
            return value, source
    return os.getcwd(), "getcwd"
