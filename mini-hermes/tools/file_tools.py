"""文件工具：read_file / write_file / list_dir。

对应 Hermes 的 tools/file_tool.py。规则很简单：模型要动文件，
只能通过工具，永远不能"凭空想象"文件内容。

注意 Hermes 的一条硬规矩：**instructional 工具不做分页**，
但 read_file 这种数据工具可以（否则一个大文件会炸掉上下文）。
"""
from __future__ import annotations

from pathlib import Path

from tools.registry import register_tool

MAX_READ_LINES = 400


@register_tool(
    "read_file",
    description="读一个文本文件的内容，带行号。超过 400 行会被截断（会提示继续读的 offset）。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，支持 ~ 展开"},
            "offset": {"type": "integer", "description": "从第几行开始（1-indexed）"},
            "limit": {"type": "integer", "description": "最多读多少行"},
        },
        "required": ["path"],
    },
    toolset="core",
)
def read_file_tool(path: str, offset: int = 1, limit: int = MAX_READ_LINES) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"[error] 文件不存在：{p}"
    if p.is_dir():
        return f"[error] 这是目录不是文件：{p}"

    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:  # noqa: BLE001 - 工具层要把任何异常变成给模型的文本
        return f"[error] 读取失败：{exc}"

    total = len(lines)
    start = max(1, offset)
    end = min(total, start + max(1, limit) - 1)
    chunk = [f"{i}|{lines[i - 1]}" for i in range(start, end + 1)]
    head = f"{p}  （共 {total} 行，显示 {start}-{end}）"
    tail = f"\n... 还有 {total - end} 行，用 offset={end + 1} 继续读" if end < total else ""
    return head + "\n" + "\n".join(chunk) + tail


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
    toolset="core",
)
def write_file_tool(path: str, content: str) -> str:
    p = Path(path).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return f"[error] 写入失败：{exc}"
    return f"已写入 {p}（{len(content)} 字符）"


@register_tool(
    "list_dir",
    description="列出一个目录下的文件和子目录。",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "目录路径"}},
        "required": ["path"],
    },
    toolset="core",
)
def list_dir_tool(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_dir():
        return f"[error] 不是目录：{p}"
    entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
    lines = [f"{'d' if e.is_dir() else '-'} {e.name}" for e in entries[:200]]
    return f"{p}（{len(entries)} 项）\n" + "\n".join(lines)
