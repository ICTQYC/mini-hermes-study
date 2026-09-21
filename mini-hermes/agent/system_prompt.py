"""系统提示词的**排序层**：每一块归属哪一层、按什么顺序拼。

对应 Hermes 的 `agent/system_prompt.py`（817 行）。第 3 章新建。

## 三层与排序原则

```text
stable    ← 整个会话不变（身份、指导）
context   ← 换目录/换 worktree 就变（项目文件、工作区快照、平台提示）
volatile  ← 重建时最可能变（记忆、时间戳、运行时环境）
```

**排序原则一句话：按「变化频率」从低到高排。** 越不容易变的排越前。

为什么这条原则等于钱：prompt 缓存是**最长前缀**匹配——从第一个字节开始逐字节比，
比到第一个不同的字节就**从那里往后全部作废**。所以：

```text
谁排前面，谁就免费搭上前缀；谁排后面，谁一变就废掉它后面所有东西。
```

**这条原则在这一章出现三次：**

| 位置 | 排在前面（不易变） | 排在后面（易变） |
|---|---|---|
| **context 层内** | `AGENTS.md`（跨 worktree 不变） | git 工作区快照 |
| **层之间** | stable 层 | context 层 → volatile 层 |
| **volatile 层内** | 记忆快照 | …时间戳 → **运行时环境块（含 cwd）** |

## 还差什么（对照真实 Hermes）

Hermes 的 volatile 层**以"技能索引"打头**（理由：变化点放本层最前面 = 爆炸半径最小），
而且它的 stable 层里有个"先占位、后回填"的技巧——因为用哪版帮助指引取决于**还没建好的**
技能索引。**mini 还没有技能系统，所以这两处都还不存在。第 10 章补技能时回来加。**
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent import prompt_builder as pb

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #


def _join_tier(parts: "Sequence[Optional[str]]") -> str:
    """拼一层：丢掉空项。

    ★ 为什么"丢空项"是个功能而不是顺手：**空字符串进去也会产生多余的
      ``\\n\\n``——那也是字节差异**，也会让缓存从那里断掉。
      对应 Hermes 的 ``_join_tier``。
    """
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


# --------------------------------------------------------------------------- #
# 各层的内容
# --------------------------------------------------------------------------- #


def _identity_parts(agent: Any, ctx_len: Optional[int]) -> Tuple[List[Optional[str]], bool]:
    """身份：SOUL.md 优先，缺省回退硬编码身份。

    对应 Hermes 的 ``_identity_parts``。第二个返回值 ``soul_loaded`` 要往下传——
    用来防止 SOUL.md 出现两次（一次当身份、一次当上下文文件）。
    """
    soul = (
        pb.load_soul_md(ctx_len)
        if getattr(agent, "load_soul_identity", True)
        and not getattr(agent, "skip_context_files", False)
        else None
    )
    return [soul or pb.DEFAULT_AGENT_IDENTITY], bool(soul)


def _guidance_parts(agent: Any) -> List[str]:
    """工具使用指导（静态文本）。

    对应 Hermes 的 ``_guidance_parts``——它那边有 7 个条件块
    （任务完成 / 并行调用 / 工具感知 / steer 通道 / 工具使用强制* / Google 专属* / 执行指导*），
    靠 `_model_gate(setting, model, default_models)` 做模型门控。

    **mini 没有模型门控**：它的指导块是通用的。要加门控的话，
    抄 Hermes 那个 `_model_gate` 的三态语义就够了——`"auto"`（用硬编码模型名列表）
    / `true|false`（强制）/ 一个字符串列表（自定义匹配）。
    """
    return [pb.TOOL_USE_GUIDANCE]


def _workspace_parts(agent: Any) -> List[str]:
    """git 工作区快照——**钉住的**，每个会话只探一次。

    对应 Hermes 的 ``_coding_parts`` 里那个 ``_frozen_workspace_snapshot``。

    ★ **为什么要钉住（Hermes docstring 的理由，值得整段读）：**

      *The workspace block is a live git probe after project context, ahead of the whole
      volatile band; re-probing at the compaction rebuild re-emits different bytes for any
      repo that moved and defeats the keep-prompt fast path. So the bytes are pinned per
      session on the agent, keyed by the resolved cwd (a gateway serves many cwds), and
      replayed on rebuilds; ``reset_session_state`` drops the pin at a session boundary.*

      翻成人话：**重探一次就可能得到不同的字节**（你刚改了文件，"已修改 1"就变了），
      那就等于**亲手把提示词搞不稳定**——而提示词稳定是这一章的立章之本。

    ★ 三个细节：

      | 细节 | 为什么 |
      |---|---|
      | 按 **cwd 作 key** 钉住 | 一个网关进程同时服务多个会话，不能只存一份 |
      | ``""`` 算**真实值** | "这里不是 git 仓库"也是一个结论，不该被当成"没钉住"而重探 |
      | 会话边界才清 pin | 只有新会话才该重新探 git |
    """
    cwd = getattr(agent, "context_cwd", None) or pb.resolve_context_cwd()
    pinned = getattr(agent, "_frozen_workspace_snapshot", None)
    if pinned is not None and pinned[0] == cwd:
        # "" 是真实值（不是 git 仓库）—— 只有 cwd 变了才重探
        replay: Optional[str] = pinned[1]
    else:
        replay = None
    if replay is None:
        replay = pb.workspace_snapshot(cwd)
        agent._frozen_workspace_snapshot = (cwd, replay)
    return [replay] if replay else []


def _post_workspace_parts(agent: Any) -> List[str]:
    """工作区快照之后的块：**平台提示**。

    对应 Hermes 的 ``_post_workspace_parts``（它那边有四样：
    环境探测行、bot-mode 协议、profile 行、平台提示）。mini 只有平台提示。
    """
    overrides = pb.load_platform_hint_overrides()
    hint = pb.resolve_platform_hint(getattr(agent, "platform", "cli"), overrides)
    return [hint] if hint else []


def _context_files_part(agent: Any, ctx_len: Optional[int], soul_loaded: bool) -> List[str]:
    """项目上下文文件（AGENTS.md 等）。skip_context_files 时整块跳过。

    对应 Hermes 的 ``_context_files_part``。
    """
    if getattr(agent, "skip_context_files", False):
        return []
    cwd = getattr(agent, "context_cwd", None) or pb.resolve_context_cwd()
    block = pb.build_context_files_prompt(
        cwd=cwd, skip_soul=soul_loaded, context_length=ctx_len
    )
    return [block] if block else []


def _memory_parts(agent: Any) -> List[str]:
    """记忆快照 + 用户画像快照。

    对应 Hermes 的 ``_memory_parts``。**它们住在 volatile 层，不是 stable**——
    因为模型随时可能写记忆，它们是"最可能变"的那一类。
    """
    return [pb.load_memory_block(), pb.load_user_profile_block()]


def _timestamp_parts(agent: Any) -> List[str]:
    """时间戳行 + Session/Model/Provider/Platform 四行 trailer。

    对应 Hermes 的 ``_timestamp_line``。
    """
    session_start = getattr(agent, "session_start", None) or datetime.now()
    line = pb.timestamp_line(session_start)
    trailer = (
        ("Session ID", getattr(agent, "session_id", None)),
        ("Model", getattr(agent, "model", None)),
        ("Provider", getattr(agent, "provider", None)),
        ("Platform", getattr(agent, "platform", None)),
    )
    line += "".join(f"\n{label}: {value}" for label, value in trailer if value)
    return [line]


def _runtime_env_part(agent: Any) -> List[str]:
    """运行时环境块——**永远结束 volatile 层**。

    ★ 为什么它必须是最后一个：它含 **cwd**，而 cwd 是整份提示词里最可能变的东西
      （用户 `cd` 一下就变）。按"变化频率从低到高排"的原则，它在最后。

    ★ 注意 Hermes 在**没有 workspace 快照**时仍把它放最后——
      两个"最易变"的东西排序不冲突。
    """
    backend = os.environ.get("MINI_TERMINAL_BACKEND", "local")
    cwd = getattr(agent, "context_cwd", None) or pb.resolve_context_cwd()
    block = pb.build_runtime_block(backend=backend, cwd=cwd)
    return [block] if block else []


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


def build_system_prompt_parts(
    agent: Any, system_message: Optional[str] = None
) -> Dict[str, str]:
    """把系统提示词装配成三个有序的缓存层。

    返回 ``{"stable": ..., "context": ..., "volatile": ...}``。

    ★ 返回值是**分层的 dict**，不是一个字符串——为什么？
      因为"stable 那一层"要能单独取出来（Hermes 把它存在
      ``agent._cached_system_prompt_static``，用于"存盘提示词还是不是这一份"的校验）。
    """
    ctx_len = getattr(agent, "context_length", None)

    # ── stable 层（与任务、目录、时间全都无关）────────────────────
    stable_parts, soul_loaded = _identity_parts(agent, ctx_len)
    stable_parts.extend(_guidance_parts(agent))

    # ── context 层（与当前项目/目录/界面有关）─────────────────────
    context_parts: List[str] = []
    if system_message is not None:
        context_parts.append(system_message)
    context_parts.extend(_context_files_part(agent, ctx_len, soul_loaded))

    workspace_parts = _workspace_parts(agent)
    post_workspace_parts = _post_workspace_parts(agent)
    if workspace_parts:
        # 有工作区快照 → 快照之后的块跟着进 context 层
        context_parts.extend([*workspace_parts, *post_workspace_parts])
    else:
        # ★ 没有快照时，**留在更稳定的 stable 层**。
        #   Hermes 的原话：*Preserve the stable placement for non-workspace sessions;
        #   there is no worktree snapshot whose later position would improve their prefix.*
        #   翻成人话：**没有 worktree 快照，就没有"把它往后放能改善前缀"这回事**——
        #   放后面对缓存没有任何好处，那就别动它。
        #
        #   📌 这是全章最容易被忽略的一课：**一个块放哪一层，
        #      取决于"把它放前面/后面，对共享前缀有没有影响"，
        #      不是"它语义上算配置还是算环境"。**
        stable_parts.extend(post_workspace_parts)

    # ── volatile 层（重建时最可能变，刻意放最后）────────────────
    volatile_parts: List[str] = [
        *_memory_parts(agent),
        *_timestamp_parts(agent),
        *_runtime_env_part(agent),
    ]

    return {
        "stable": _join_tier(stable_parts),
        "context": _join_tier(context_parts),
        "volatile": _join_tier(volatile_parts),
    }


def build_system_prompt(agent: Any, system_message: Optional[str] = None) -> str:
    """拼出完整系统提示词，**并缓存在 agent 上**。

    对应 Hermes 的 ``build_system_prompt``：

    > *Assemble the full prompt; cached on ``agent._cached_system_prompt`` and
    > only rebuilt after compression.*

    ★ **缓存是这一章的立章之本，不能省。** 如果不缓存：
      - 每轮都会重读一遍 SOUL.md / AGENTS.md / MEMORY.md（浪费）
      - 而且**每轮的结果可能不同**（记忆文件被改了、cwd 变了、跨天了）
      - 后果是"同一会话内系统提示词字节不稳定"——**这个 bug 没有症状，只有账单。**

    ★ 缓存键里带上 ``system_message``：调用方显式传了不同的 system_message 时，
      应该重新拼（那是他明确要求换的），但平常传 None 时一直命中缓存。
    """
    cached = getattr(agent, "_cached_system_prompt", None)
    cached_for = getattr(agent, "_cached_system_prompt_key", "__unset__")
    if cached is not None and cached_for == system_message:
        return cached

    parts = build_system_prompt_parts(agent, system_message=system_message)
    agent._cached_system_prompt_static = parts["stable"]
    prompt = "\n\n".join(
        p for p in (parts["stable"], parts["context"], parts["volatile"]) if p
    )
    agent._cached_system_prompt = prompt
    agent._cached_system_prompt_key = system_message

    # 截断警告要浮现到聊天里，不能只写日志（Hermes 同样处理）。
    for warning in pb.drain_truncation_warnings():
        emit = getattr(agent, "_emit_status", None)
        if callable(emit):
            try:
                emit(warning)
            except Exception:  # noqa: BLE001 —— 提示失败不该影响提示词
                pass
    return prompt


def invalidate_system_prompt(agent: Any) -> None:
    """强制下次重建。

    对应 Hermes 的 ``invalidate_system_prompt``——它在**上下文压缩之后**被调用，
    而且会**顺便从磁盘重新加载记忆**，好让重建的提示词捕获本会话里发生的写入。

    所以记忆的更新路径是：

    ```text
    模型调用 memory 工具写记忆
           ↓
    写入 MEMORY.md（立刻，磁盘）
           ↓
    但系统提示词里的那份是"冻结的快照"，本轮不变
           ↓
    什么时候生效？→ 新会话 / 压缩触发的重建 / 显式 invalidate（就是这里）
    ```

    **这个"延迟生效"看起来是缺陷，其实是缓存约束下的必然选择**：
    如果每次记忆写入就更新提示词，那一字节之后整个缓存作废，
    而模型可能每几轮就写一次记忆 → 整个会话几乎没有缓存命中。

    mini 还没有压缩（第 5 章），所以现在只有"开新会话"和显式调用会触发重建；
    这个函数先把接口留好。
    """
    agent._cached_system_prompt = None
    agent._cached_system_prompt_key = "__unset__"
    agent._cached_system_prompt_static = None
    # 工作区快照的 pin 也要清（对应 Hermes 的 reset_session_state）
    agent._frozen_workspace_snapshot = None


def prompt_preview(agent: Any, system_message: Optional[str] = None) -> str:
    """给 ``/prompt`` 命令用：把三层分开显示，方便肉眼验证归属和顺序。"""
    parts = build_system_prompt_parts(agent, system_message=system_message)
    out: List[str] = []
    for tier, note in (
        ("stable", "整个会话不变"),
        ("context", "换目录/换 worktree 就变"),
        ("volatile", "重建时最可能变"),
    ):
        body = parts[tier]
        out.append(f"═══ {tier} 层（{note}）─── {len(body)} 字符 ═══")
        out.append(body if body else "(空)")
        out.append("")
    return "\n".join(out)
