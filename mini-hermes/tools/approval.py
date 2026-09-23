"""危险命令检测 —— **"什么算危险"的唯一事实来源**。

对应 Hermes 的 tools/approval_detection.py（真实版本里 `DANGEROUS_PATTERNS`
有 **107 条**，而且有预编译版本 `DANGEROUS_PATTERNS_COMPILED`）。

## 第 4 章为什么把它从 terminal.py 挪出来

挪之前：危险检测住在 `tools/terminal.py` 里。问题是——
**"什么算危险"没有单一事实来源**。加第二个需要审批的工具时，
它得自己再写一份判断，两份迟早不一致。

这和另外两处是同一个形状：
  · 第 2 章：工具分发写成两条 if/elif 链 → 漂移分家
  · 第 2 章：状态重建写两处 → 只有跨进程测试才露出来
  · 第 4 章：危险检测写在各工具里 → 新增工具时各自写一份

## 边界：检测 vs 审批流程

这个文件**只回答"这条命令危不危险"**，不回答"接下来问谁、等多久、记不记得"。

Hermes 把那另一半拆成了 4 个文件（`approval_human_wait` / `approval_gateway_wait` /
`approval_smart` / `approval_floors`），因为"谁来回答"有四种：
  · CLI：人在同一个终端      → 同步问
  · 网关：人在 Telegram      → 跨进程、异步、可能等几分钟
  · Smart：辅助 LLM 自动判断 → 一次额外 API 调用
  · 允许清单：上次已决定过   → 读配置

**工具不该知道这些**。所以分界线是：
  工具说"这个调用危险"（danger_check）
  审批层说"那么接下来问谁、等多久、记不记得"
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

__all__ = ["DANGEROUS_PATTERNS", "detect_dangerous_command", "looks_dangerous"]

# 格式和 Hermes 一致：`(正则, 人话描述)`。
# 描述不是装饰——审批提示要告诉用户"为什么拦你"，只给正则用户看不懂。
#
# ⚠️ 数量上 mini **故意只抄结构不抄数量**（8 条 vs 107 条）。
#    抄 107 条正则学不到任何东西；学到的是"检测要在一张表里、带可读描述、
#    而且要预编译"。真需要更多规则时，往这张表里加行就行。
DANGEROUS_PATTERNS: List[Tuple[str, str]] = [
    (r"\brm\s+-[a-z]*r[a-z]*f", "递归强制删除（rm -rf）"),
    (r"\bsudo\b", "提权执行（sudo）"),
    (r"\bmkfs\b", "格式化文件系统（mkfs）"),
    (r"\bdd\s+if=", "裸设备写入（dd if=）"),
    (r">\s*/dev/[sh]d[a-z]", "直接写块设备"),
    (r"\bchmod\s+777\b", "把权限开到 777"),
    (r"curl[^|]*\|\s*(ba)?sh", "把远程脚本直接管进 shell（curl | sh）"),
    (r"\bshutdown\b|\breboot\b", "关机 / 重启"),
]

# **预编译**。对应 Hermes 的 `DANGEROUS_PATTERNS_COMPILED`。
#
# 为什么值得单独一行：每条命令都要对**全部**规则跑一遍匹配。
# Python 的 re 内部有编译缓存（默认 512 条），但显式预编译有两个好处：
#   ① 不受缓存上限/淘汰影响
#   ② 一条统一的正则选项，免得某条规则忘了加 flag 导致**漏检**
#
# 注意最后一点的性质：**安全检测里"某条正则没匹配上"是静默失败**。
# 所以任何能消除这种可能性的做法都值得做。
_COMPILED = [(re.compile(p), desc) for p, desc in DANGEROUS_PATTERNS]


def detect_dangerous_command(command: str) -> Optional[str]:
    """命中就返回**人话描述**，没命中返回 None。

    返回描述而不是 bool，是为了让审批提示能说清"为什么拦你"。
    （Hermes 的 `detect_dangerous_command()` 同样返回描述。）
    """
    if not command:
        return None
    for pattern, description in _COMPILED:
        if pattern.search(command):
            return description
    return None


def looks_dangerous(command: str) -> bool:
    """`danger_check` 要的 bool 形式。"""
    return detect_dangerous_command(command) is not None
