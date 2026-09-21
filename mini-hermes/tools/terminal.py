"""终端工具 —— 最危险、也最有用的一个。

对应 Hermes 的 tools/terminal_tool.py（真实版本管 6 种后端：
local / docker / ssh / modal / daytona / singularity；我们只用 local）。

这里体现两个 Hermes 的关键设计：
1. **危险命令检测**（对应 tools/approval.py）：命中模式先问用户，不问就拒绝。
2. **超时**：子进程必须有上限，否则 agent 会被一条 `tail -f` 卡死到天荒地老。
"""
from __future__ import annotations

import os
import re
import subprocess

from tools.registry import register_tool

DEFAULT_TIMEOUT = 60

# 危险模式：命中就把决定权交回用户
_DANGEROUS_PATTERNS = [
    r"\brm\s+-[a-z]*r[a-z]*f",     # rm -rf
    r"\bsudo\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r">\s*/dev/[sh]d[a-z]",
    r"\bchmod\s+777\b",
    r"curl[^|]*\|\s*(ba)?sh",       # curl ... | sh
    r"\bshutdown\b|\breboot\b",
]


def looks_dangerous(command: str) -> bool:
    return any(re.search(p, command) for p in _DANGEROUS_PATTERNS)


@register_tool(
    "terminal",
    description=(
        "在本机 shell 里执行一条命令并返回 stdout/stderr。"
        "注意：工作目录和环境变量在同一会话内会保留。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "timeout": {
                "type": "integer",
                "description": f"超时秒数，默认 {DEFAULT_TIMEOUT}",
            },
        },
        "required": ["command"],
    },
    toolset="core",
    requires_approval=True,
    danger_check=lambda args: looks_dangerous(args.get("command", "")),
)
def terminal_tool(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.getcwd(),
        )
    except subprocess.TimeoutExpired:
        return f"[error] 命令超时（>{timeout}s）已被终止：{command}"

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parts = [f"exit_code={proc.returncode}"]
    if out:
        parts.append(f"stdout:\n{out}")
    if err:
        parts.append(f"stderr:\n{err}")
    return "\n".join(parts)
