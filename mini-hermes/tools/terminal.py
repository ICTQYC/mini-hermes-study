"""终端工具 —— 最危险、也最有用的一个。

对应 Hermes 的 tools/terminal_tool.py + tools/terminal_tool_backends.py
（真实版本管 **7 种后端**：local / docker / ssh / singularity / modal / daytona /
vercel_sandbox）。mini 只有 local。

第 4 章改了三处：
1. **危险检测挪出去了** → `tools/approval.py`（"什么算危险"要有唯一事实来源）
2. **加了 `check_fn`** → 没有可用 shell 时，这个工具不该发给模型
3. **结果改成 JSON 字符串** → Hermes 的硬规矩：*All handlers return a JSON string*

## 关于第 8 项改造（后端表）：故意不做

第 4 章的清单里有"把后端表的位置留出来"。**我选择不做**，理由：

加 docker 后端在 mini 里只是几行 subprocess 参数的事，但那样**学不到任何东西**——
真正的复杂度在 Hermes 那两个 process_registry 文件里（多父 agent 同时起进程 +
持久化 + 重启恢复 + 越权读防护），而那需要真的有那些场景。

**在只有一种后端的时候做一张"后端表"，就是投机性基础设施**
（Hermes 自己把 *Speculative infrastructure* 列在"不要做"的第一条：
 加钩子容易、等别人依赖上之后再拆就难了）。
所以：等真有第二个后端时再抽表，那时候表格的形状会是对的。
"""
from __future__ import annotations

import os
import shutil
import subprocess

from tools.approval import detect_dangerous_command
from tools.registry import register_tool, tool_error, tool_result

__all__ = ["terminal_tool", "check_terminal_requirements"]

DEFAULT_TIMEOUT = 60
MAX_OUTPUT_CHARS = 30_000


def check_terminal_requirements() -> bool:
    """有没有一个可用的 shell？

    这是 `check_fn` 的**形态 3（本地二进制型）**：探测外部程序在不在。

    两个设计点（对照 Hermes 的 `_check_feishu()`）：
    · 用 `shutil.which`（**只查路径**）而不是真跑一次 `bash -c :`
      —— 探测要便宜，而且**不能有可见副作用**
      （Hermes 的浏览器 check_fn 里明确禁止跑 `agent-browser --version`，
       因为在 Windows 上执行 .cmd 会闪一个黑框，而那段代码在桌面 app 启动时跑）
    · 这是 mini 里唯一一个真的可能返回 False 的 check_fn，
      所以它让"过滤"这件事在真实运行中可见

    注意：这个函数被 `read_file` 那一族**转发**调用（见 file_tools.py）——
    这是 `check_fn` 的**形态 2（转发型）**。
    """
    return shutil.which("bash") is not None or shutil.which("sh") is not None


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """输出必须有上限。

    理由和错误文本一样（见 `registry.tool_error`）：工具结果会进历史，
    历史随**每次**请求重发，所以一条超长输出的成本是**复利**的。
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [输出被截断，原长 {len(text)} 字符]"


@register_tool(
    "terminal",
    description=(
        "在本机 shell 里执行一条命令。返回 JSON：{exit_code, stdout, stderr}。"
        "注意：工作目录在同一会话内会保留。"
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
    toolset="terminal",
    # ★ 可用性探测：没有 shell 就不发这个工具（见 §3）
    check_fn=check_terminal_requirements,
    requires_approval=True,
    danger_check=lambda args: detect_dangerous_command(args.get("command", "")) is not None,
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
        # ★ 超时必须报错：一条 `tail -f` 就能把 agent 卡到天荒地老。
        return tool_error(f"命令超时（>{timeout}s）已被终止：{command}", timeout=timeout)

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    # ★ JSON 字符串，不是人看的文本。exit_code 单独给一个字段，
    #   这样"命令失败了"和"命令没有输出"能区分开 —— 纯文本形式区分不了。
    return tool_result(
        exit_code=proc.returncode,
        stdout=_truncate(out) if out else "",
        stderr=_truncate(err) if err else "",
    )
