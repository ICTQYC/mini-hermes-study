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


def logs_dir() -> Path:
    """错误日志目录。

    对应 Hermes 的 `~/.hermes/logs/`（`agent.log` / `errors.log`）。
    为什么要落盘而不是只打印：**用户看到的界面会滚过去，日志不会。**
    一个只在特定时机出现的异常，事后能翻到才有意义。
    """
    d = get_mini_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d
