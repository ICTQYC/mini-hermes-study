"""系统提示词拼装（极简版）。

对应 Hermes 的 agent/prompt_builder.py。Hermes 真实版本拼的是：
    身份 + 人格 + 记忆(MEMORY.md/USER.md) + 技能列表 + 项目上下文(AGENTS.md)
    + 工具用法 + 平台提示 + 当前日期 ...

我们这一章只拼最小可用的一份，剩下的层次留给 prompt-assembly 那一章。

⚠️ 抄一条 Hermes 的硬规矩：**系统提示词在一次对话里必须字节级稳定。**
每轮重新拼、往里塞"当前时间"这类东西 = 每次都打断 prompt 缓存 = 用户账单翻倍。
"""
from __future__ import annotations

import os

BASE_IDENTITY = """你是 mini-hermes，一个跑在用户电脑上的终端 AI agent。

你的工作方式和普通聊天机器人不同：你**边想边动手**。
需要看文件就读，需要跑命令就跑，拿到结果再决定下一步——
不要凭记忆猜文件内容，也不要假设命令的结果。

原则：
- 动手前先看清楚：不确定就先读文件 / 列目录 / 跑一条探测命令。
- 多步任务用 todo 工具给用户看进度。
- 工具报错时读错误信息再改，不要重复同一个失败调用。
- 答复直接用中文，说清楚"我做了什么、结果是什么"，不要复述过程。
"""


def build_system_prompt(cwd: str | None = None, tools_hint: list[str] | None = None) -> str:
    """拼出本次对话的系统提示词。

    注意：这里**故意不塞时间戳**。Hermes 会把"当前日期"放进系统提示词，
    但只在会话开始时算一次，之后不再变——否则缓存每轮都失效。
    """
    cwd = cwd or os.getcwd()
    parts = [BASE_IDENTITY]
    parts.append(f"\n当前工作目录：{cwd}")
    if tools_hint:
        parts.append("可用工具：" + "、".join(tools_hint))
    return "\n".join(parts)
