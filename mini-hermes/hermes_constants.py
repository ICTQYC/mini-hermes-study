"""mini-hermes 全局常量与路径解析。

对应 Hermes 的 hermes_constants.py：所有路径都从一处解析，禁止散落硬编码。
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "mini-hermes"


def get_mini_home() -> Path:
    """minihome 目录（对应 Hermes 的 get_hermes_home()）。

    优先读环境变量 MINI_HOME，方便测试时重定向到临时目录。
    """
    env = os.environ.get("MINI_HOME")
    home = Path(env).expanduser() if env else Path.home() / ".mini-hermes"
    home.mkdir(parents=True, exist_ok=True)
    return home


def sessions_dir() -> Path:
    d = get_mini_home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


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
