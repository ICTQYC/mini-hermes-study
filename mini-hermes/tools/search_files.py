"""search_files —— 搜索文件内容。

对应 Hermes 的 `search_files`（它的实现走 ripgrep）。

## 第 4 章新增这个工具，是为了修一个真 bug

系统提示词里一直写着：

    需要搜索文件内容：search_files（**不要**用 grep/find）

但 `registry.tool_names()` 里**没有它** —— 提示词在**点名一个不存在的工具**。

这正是第 4 章 §4 讲的那条硬规矩（Hermes tools/AGENTS.md）：

    **Schema descriptions must not name tools from other toolsets.**
    Those tools may be unavailable and the model **hallucinates calls to them**.

（schema 描述里不许点名可能不可用的工具，否则模型会**幻觉出对它们的调用**。）

Hermes 用 10 个动态 rewriter 在运行时把这类交叉引用补上/抹掉；
**mini 这里的修法更直接：把那个工具做出来。**

## `check_fn` 在这里是真正的"转发型"（形态 2）

搜索要靠外部程序（`grep`），所以它的前置条件和 `terminal` **是同一个**：
都得有一个可用的 shell + 命令。
所以这里**转发**给 `check_terminal_requirements()`，而不是另写一份判断。

这不是为了凑一个"转发型"的例子——**如果两份判断各写各的，它们迟早不一致**，
而"什么算可用"一旦有两个来源，就会出现"A 说能用 B 说不能"的状态。
（同一个形状在本项目里出现过三次：工具分发、状态重建、危险检测。）
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Optional

from tools.registry import register_tool, tool_error, tool_result
from hermes_constants import resolve_context_cwd

__all__ = ["search_files_tool", "check_search_requirements"]

MAX_MATCHES = 50
DEFAULT_TIMEOUT = 30
MAX_LINE_CHARS = 400


def check_search_requirements() -> bool:
    """搜索的前置条件 = 有 shell + 有 grep/rg。

    **转发**给 terminal 的判断（`check_fn` 的**形态 2**）——同一个事实只允许有一个来源。
    """
    from tools.terminal import check_terminal_requirements

    if not check_terminal_requirements():
        return False
    return shutil.which("rg") is not None or shutil.which("grep") is not None


@register_tool(
    "search_files",
    description=(
        "在目录下递归搜索文件**内容**（正则）。返回 JSON："
        "{matches:[{path,line,text}], truncated}。用它代替 terminal 里的 grep/find。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "要搜索的正则表达式"},
            "path": {"type": "string", "description": "从哪个目录开始搜，默认当前目录"},
            "glob": {"type": "string", "description": "只搜匹配这个通配符的文件，如 '*.py'"},
        },
        "required": ["pattern"],
    },
    toolset="search",
    check_fn=check_search_requirements,
)
def search_files_tool(pattern: str, path: str = ".", glob: Optional[str] = None) -> str:
    if not pattern:
        return tool_error("pattern 不能为空")

    # ★ 注意这里是 `subprocess.run([...])` 而不是 `shell=True`。
    #
    #   terminal 工具**必须**用 shell=True（它就是给你跑 shell 的），
    #   但 search_files 收到的 pattern 是**模型可控的输入**，
    #   如果拼进 shell，一个 `"; rm -rf ~` 就成了命令注入。
    #
    #   传成 argv 数组就没有这个问题：pattern 永远是 grep 的一个参数，
    #   永远不会被当成语法。**判据：这段输入是"数据"还是"代码"？**
    if shutil.which("rg"):
        cmd = ["rg", "--line-number", "--no-heading", "--color=never"]
        if glob:
            cmd += ["--glob", glob]
        cmd += [pattern, path]
    else:
        cmd = ["grep", "-rn", "--color=never"]
        if glob:
            cmd += ["--include", glob]
        cmd += [pattern, path]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=DEFAULT_TIMEOUT,
                              # 同上：搜索的相对基准是用户的项目目录，不是进程 cwd
                              cwd=resolve_context_cwd())
    except subprocess.TimeoutExpired:
        return tool_error(f"搜索超时（>{DEFAULT_TIMEOUT}s）")

    # grep 的退出码 1 = 没匹配到（不是错误）；2 = 真出错。
    # 这个区分很重要：**"没有结果"和"搜索失败"是两件事**，
    # 如果都返回 error，模型会把"没找到"当成"工具坏了"然后换个方法重试。
    if proc.returncode not in (0, 1):
        return tool_error((proc.stderr or "").strip() or f"搜索失败（exit={proc.returncode}）")

    matches = []
    for line in (proc.stdout or "").splitlines():
        # 格式：路径:行号:内容（路径里可能有冒号，所以从**右边**切两刀）
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        fpath, lineno, text = parts
        matches.append({
            "path": fpath,
            "line": lineno,
            "text": text[:MAX_LINE_CHARS],
        })
        if len(matches) >= MAX_MATCHES:
            break

    return tool_result(matches=matches, total_shown=len(matches),
                       truncated=len(matches) >= MAX_MATCHES)
