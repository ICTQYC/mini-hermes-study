"""文件工具：read_file / write_file / list_dir。

对应 Hermes 的 tools/file_tools.py（真实版本拆成多个兄弟文件，合计几千行）。

第 4 章改了两处：
1. **结果改成 JSON 字符串** —— 契约：*All handlers return a JSON string*
   （`read_file` 的人读部分仍然在 `content` 字段里，形如 `1|行内容`
    —— 这一点和 Hermes 一致：它的 `read_file` 也返回
    `{"content": "1|foo\\n2|bar", ...}`，`LINE_NUM|CONTENT` 是 content **里面**的格式）
2. **加了 `check_fn`** —— 显式声明"没有外部前置条件"

## 关于第 2 点：为什么要写一个恒真的 check_fn

Hermes 的 `tools/delegate_tool.py`：

    def check_delegate_requirements() -> bool:
        \"\"\"Delegation has no external requirements -- always available.\"\"\"
        return True

**没有依赖也显式写出来**，因为这两句话不一样：
    · 写了 return True  → "**我想过了**，它没有前置条件"（一个承诺）
    · 不写              → "某人忘了写"（一个缺口）

读者没法从代码上区分这两者，所以显式声明是有价值的。

## 硬规矩：instructional 工具不做分页

Hermes 的根 AGENTS.md：*No `offset`/`limit` on instructional tools (skills, prompts,
playbooks) — **models read page 1 and skip the rest***。

但 `read_file` 这种**数据**工具**可以**分页——因为一个大文件会炸掉上下文。
判据不是"要不要省 token"，而是：**这个工具返回的东西，模型是不是必须看完**。
  · skill_view / 提示词 / playbook → 必须看完 → 不许分页（否则它只看第一页）
  · read_file / search_files      → 按需看   → 必须分页（否则一次塞爆）
"""
from __future__ import annotations

from pathlib import Path

from tools.registry import register_tool, tool_error, tool_result

__all__ = ["read_file_tool", "write_file_tool", "list_dir_tool", "check_file_requirements"]

MAX_READ_LINES = 400
MAX_LIST_ENTRIES = 200


def check_file_requirements() -> bool:
    """文件工具没有外部前置条件 —— 但显式写出来（`check_fn` 的**形态 1**）。"""
    return True


@register_tool(
    "read_file",
    description=(
        "读一个文本文件。返回 JSON：{path, total_lines, shown, content, next_offset}。"
        f"content 里每行带行号（形如 `1|行内容`）。一次最多 {MAX_READ_LINES} 行，"
        "超出时用 next_offset 继续读。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，支持 ~ 展开"},
            "offset": {"type": "integer", "description": "从第几行开始（1-indexed）"},
            "limit": {"type": "integer", "description": "最多读多少行"},
        },
        "required": ["path"],
    },
    toolset="file",
    check_fn=check_file_requirements,
)
def read_file_tool(path: str, offset: int = 1, limit: int = MAX_READ_LINES) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return tool_error(f"文件不存在：{p}")
    if p.is_dir():
        return tool_error(f"这是目录不是文件：{p}")

    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:  # noqa: BLE001 - 工具层要把任何异常变成给模型的 JSON
        return tool_error(f"读取失败：{exc}")

    total = len(lines)
    start = max(1, offset)
    end = min(total, start + max(1, limit) - 1)
    chunk = [f"{i}|{lines[i - 1]}" for i in range(start, end + 1)]
    return tool_result(
        path=str(p),
        total_lines=total,
        shown=f"{start}-{end}",
        content="\n".join(chunk),
        # ★ 分页信息是**结构化字段**而不是一句提示文本。
        #   结构化之后模型和程序都能可靠地用它；一句话提示只能靠模型读懂。
        next_offset=end + 1 if end < total else None,
    )


@register_tool(
    "write_file",
    description="把内容完整写入文件（会覆盖已有内容），父目录不存在会自动创建。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "完整文件内容"},
        },
        "required": ["path", "content"],
    },
    toolset="file",
    check_fn=check_file_requirements,
)
def write_file_tool(path: str, content: str) -> str:
    p = Path(path).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return tool_error(f"写入失败：{exc}")
    return tool_result(path=str(p), written_chars=len(content))


@register_tool(
    "list_dir",
    description="列出一个目录下的文件和子目录。",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "目录路径"}},
        "required": ["path"],
    },
    toolset="file",
    check_fn=check_file_requirements,
)
def list_dir_tool(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_dir():
        return tool_error(f"不是目录：{p}")
    entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
    return tool_result(
        path=str(p),
        total=len(entries),
        entries=[
            {"type": "dir" if e.is_dir() else "file", "name": e.name}
            for e in entries[:MAX_LIST_ENTRIES]
        ],
        truncated=len(entries) > MAX_LIST_ENTRIES,
    )
