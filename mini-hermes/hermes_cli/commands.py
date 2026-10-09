"""斜杠命令的**声明**（对应 Hermes 的 `hermes_cli/commands.py`）。

★ 为什么一个命令要有一个数据类，而不是散在分发逻辑里的 `if`：

真实版 `hermes_cli/commands.py:21` 的 `CommandDef` 是**唯一的命令声明处** ——
名字、别名、参数字提示、以及**忙时策略**都在这里。有了它：

- 加一个命令 = 加一条声明，分发逻辑**一个字都不用改**；
- "agent 正在跑时这个命令怎么办"**跟着命令走**，不跟着调用点走。

上游代码规则里那条「键在名字/种类上的 `if/elif` 阶梯 ≥ 4 个分支，必须改成
字典/表 → 处理器」，正是靠这个数据类才做得到的（见 `gateway/run_busy` 侧的实现）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class CommandDef:
    """单个斜杠命令的声明（字段照抄真实版 `CommandDef`，只留 mini 用得上的）。"""

    name: str                     # 规范名，不含斜杠：`"new"`
    description: str              # 给人看的说明
    aliases: Tuple[str, ...] = ()  # 别名：`("reset",)`
    # 忙时（agent 正在跑）怎么办：
    #   "reject"                  → 拒绝（默认）
    #   "dispatch"                → 照常执行
    #   "interrupt_then_dispatch" → 先打断 agent，再执行
    busy_policy: str = "reject"
    # 有"运行中专用"处理器时，这里写处理器键（对应真实版的名字，如 "stop"）。
    busy_handler: Optional[str] = None


# 网关注册的命令。★ 注意 `busy_policy` 是**每条命令自己的属性**：
#   `/new` `/stop` 打断 → interrupt_then_dispatch
#   `/approve` `/deny` `/queue` `/status` `/help` 在 agent 跑着时也得能到 → dispatch
#   其余默认 reject
GATEWAY_COMMANDS: Tuple[CommandDef, ...] = (
    CommandDef("new", "重置当前会话（打断正在跑的回合）", aliases=("reset",),
               busy_policy="interrupt_then_dispatch", busy_handler="stop"),
    CommandDef("stop", "打断正在跑的回合",
               busy_policy="interrupt_then_dispatch", busy_handler="stop"),
    CommandDef("status", "看网关与当前会话的状态", busy_policy="dispatch"),
    CommandDef("help", "列出可用命令", aliases=("commands",), busy_policy="dispatch"),
    CommandDef("approve", "批准一条待确认的终端命令", busy_policy="dispatch"),
    CommandDef("deny", "拒绝一条待确认的终端命令", busy_policy="dispatch"),
    CommandDef("queue", "看排队中的消息", busy_policy="dispatch"),
    CommandDef("sessions", "列出最近的会话", busy_policy="reject"),
)

GATEWAY_KNOWN_COMMANDS = frozenset(c.name for c in GATEWAY_COMMANDS)

_BY_NAME = {c.name: c for c in GATEWAY_COMMANDS}
_BY_ALIAS = {a: c for c in GATEWAY_COMMANDS for a in c.aliases}


def command_def(name: str) -> Optional[CommandDef]:
    return _BY_NAME.get(name)


def busy_policy_for(name: str) -> str:
    """这个命令在 agent 忙时的策略（未知命令按默认 `reject`）。"""
    d = _BY_NAME.get(name)
    return d.busy_policy if d else "reject"


def resolve_command(text: str) -> Optional[Tuple[str, str]]:
    """把用户输入解析成 `(规范名, 余下参数)`；不是已知命令就返回 None。

    解析顺序（照真实版 `resolve_command()` 的形状）：
    精确规范名 → 别名 → **唯一前缀匹配**。
    ★ 前缀匹配必须是**唯一**的：`/s` 同时可能是 `stop` / `status` / `sessions`，
      这时**不猜**，返回 None（当普通消息处理）—— 猜错会打断一个正在跑的回合。
    """
    if not text.startswith("/"):
        return None
    body = text[1:].strip()
    if not body:
        return None
    name, _, rest = body.partition(" ")
    name = name.lower()

    if name in _BY_NAME:
        return name, rest.strip()
    if name in _BY_ALIAS:
        return _BY_ALIAS[name].name, rest.strip()

    hits = [c.name for c in GATEWAY_COMMANDS if c.name.startswith(name)]
    if len(hits) == 1:
        return hits[0], rest.strip()
    return None
