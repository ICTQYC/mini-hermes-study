"""技能工具：`skill_view` —— 渐进披露的**第二跳**。

对应 Hermes 的 `tools/skills_tool.py`（真实版 700+ 行，一个模块里装了四个工具：
`skills_list` / `skill_view` / hub 安装 / 发布）。第 13 章只做一个：`skill_view`。

真实版模块 docstring 的原话（逐字）：

    Skills Tool — list and view skill documents (progressive disclosure). A skill is a directory
    holding SKILL.md (YAML frontmatter + instructions) plus optional references/, templates/, assets/,
    scripts/. `skills_list` returns name/description only; `skill_view` returns full content and
    linked files.

## 为什么它走**普通**注册路径（不像 todo_list 那样进 agent loop）

判据不是"它看起来简不简单"，而是 **执行它需不需要 agent 对象**：
`todo_list` 要（状态挂在 agent 上，执行由 `run_agent.INLINE_TOOL_EXECUTORS` 接管）；
`skill_view` **只读文件、不写任何状态**，所以它就是普通注册工具 ——
`@register_tool` + `toolsets.py` 点名，**不用碰** `run_agent.py` / `model_tools.py`
（AST 自动发现会认它）。

## 硬规矩：instructional 工具不分页

Hermes 的根 AGENTS.md：*No `offset`/`limit` on instructional tools (skills, prompts,
playbooks) — models read page 1 and skip the rest*。代价说清：分了页，模型就拿着
**半份说明书**干活。`read_file` 那种**数据**工具正相反：必须分页，否则一个大文件炸掉上下文。
（同一条规矩在 `tools/file_tools.py` 的 docstring 里也写着，两处说的是同一件事。）

## 安全边界

技能名是**寻址键**，不是路径片段：解析走 `agent.skill_utils.resolve_skill()`
（按名字查已发现的技能，结构上不可能越界），非法名字（`..`、斜杠、绝对路径）
直接给可读错误。**永不**把模型/用户给的字串拼进路径。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from agent import skill_utils
from tools.registry import no_cache_check_fn, register_tool, tool_error, tool_result

__all__ = ["skill_view_tool", "check_skills_available", "MAX_SKILL_LIST_ENTRIES"]

#: 目录列表的条数上限（和 `tools/file_tools.list_dir` 同一个理由：
#: 一个塞了几千个文件的技能目录不该把工具结果撑爆）。
MAX_SKILL_LIST_ENTRIES = 200


@no_cache_check_fn
def check_skills_available() -> bool:
    """技能库里至少有一个可用技能才暴露这个工具。

    ★ **与提示词里的索引同一个事实源**（`skill_utils.has_skills()`）：
      两个面必须同开同关 —— 索引里列了技能而工具不在，模型照着提示词去 load 会撞墙；
      工具在而索引空，模型根本不知道该 load 什么（白白常驻一条 schema）。
    ★ 为什么用 `no_cache_check_fn`：探测只是**读本地目录**（便宜），
      而且用户刚丢进一个 `SKILL.md` 就该立刻生效，不该等 30 秒 TTL ——
      这正是 `tools/registry.py` 里 no_cache 那条用法（"探测只读本地配置、
      而且必须立刻反映"）。
    """
    return skill_utils.has_skills()


@register_tool(
    "skill_view",
    description=(
        "读一个技能的完整正文（SKILL.md）和它的目录列表。"
        "技能索引在系统提示词里（只有名字与一句话描述）；正文必须用这个工具按需读取。"
        "**不分页**：一次返回全文。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "技能名 —— 就是提示词索引里那行 `- 名字: 描述` 的名字",
            },
        },
        "required": ["name"],
    },
    toolset="skills",
    check_fn=check_skills_available,
)
def skill_view_tool(name: str = "") -> str:
    """读技能正文 + 目录列表 → JSON 字符串（出口契约见 tools/registry.py）。

    三条失败路径都返回**可读的 JSON 错误**，一条异常都不抛：
      ① 名字非法（`..` / 斜杠 / 绝对路径 / 空白）—— 安全边界；
      ② 名字合法但库里没有 —— 报错时**列出可用的名字**，模型能据此自己改；
      ③ 找到了但正文读不出来（文件被删 / 权限 / 编码）。
    """
    scan = skill_utils.discover_skills()
    available = [s.name for s in scan.skills]

    if not skill_utils.is_valid_skill_name(name):
        return tool_error(
            f"技能名不合法：{name!r}。技能名就是提示词索引里 `- 名字: 描述` 的那个名字，"
            f"不含路径分隔符、不含 `..`，也不是路径。",
            available=available,
        )

    wanted = name.strip()
    skill = next((s for s in scan.skills if s.name == wanted), None)
    if skill is None:
        hint = "、".join(available) if available else "（技能库是空的）"
        return tool_error(f"没有名为 {wanted!r} 的技能。可用：{hint}", available=available)

    body = skill_utils.load_skill_body(skill)
    if body is None:
        return tool_error(
            f"技能 {skill.name!r} 的正文读不出来（文件被删 / 权限 / 编码问题）：{skill.path}"
        )

    return tool_result(
        name=skill.name,
        description=skill.description,
        path=str(skill.path),
        content=body,
        content_chars=len(body),
        # ★ 目录列表 = 渐进披露的"第二跳的入口"：references/ 里的大文档靠这一步
        #   被发现（模型看到目录，自己决定要不要再要一份）。正文里**没有**分页，
        #   但这份列表让"技能还可以带附件"这件事是可见的。
        files=_list_dir_entries(skill.dir),
    )


def _list_dir_entries(skill_dir: Path) -> List[Dict[str, Any]]:
    """技能目录的条目列表（排序、有上限；读不出来就返回空列表，不抛）。"""
    try:
        entries = sorted(skill_dir.iterdir(), key=lambda x: (x.is_file(), x.name))
    except OSError:
        return []
    return [
        {"type": "dir" if e.is_dir() else "file", "name": e.name}
        for e in entries[:MAX_SKILL_LIST_ENTRIES]
    ]
