"""系统提示词的**内容层**：每一块内容从哪来、怎么清洗。

对应 Hermes 的 `agent/prompt_builder.py`（1745 行）。第 2 章时这个文件 41 行，
第 3 章长到现在的规模。

## 为什么拆成两个文件

```text
prompt_builder.py   →  每一块"内容是什么"（读文件 / 安全扫描 / 截断）
system_prompt.py    →  这些块"按什么顺序、归属哪一层"
```

**"内容"和"排序"是两个独立的关注点：**

- 想改**排序规则**（比如"环境块永远最后"）→ 只动 `system_prompt.py`
- 想**新增一种上下文来源**（比如 `.cursor/rules`）→ 只动这个文件

Hermes 也是这么分的，而且它的两个文件都已上千行——
**如果合在一起，改排序的时候要在一堆"读文件"的代码里找，改读文件的时候又怕碰坏排序。**
"""
from __future__ import annotations

import json
import logging
import os
import platform as _platform
import re
import socket
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_mini_home

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 上下文文件的字符上限（下限）。真实动态上限按模型窗口缩放，见 get_context_file_max_chars()。
CONTEXT_FILE_MAX_CHARS = 20_000
CONTEXT_TRUNCATE_HEAD_RATIO = 0.7
CONTEXT_TRUNCATE_TAIL_RATIO = 0.2

#: 窗口的 6%。注释抄自 Hermes：
#: "a small slice of the window since context files share the cached prefix;
#:  small models stay at the floor"（上下文文件共享缓存前缀，只给它窗口的一小片；
#:  小模型就停在下限上）。
_CONTEXT_FILE_WINDOW_FRACTION = 0.06
_CONTEXT_FILE_DYNAMIC_CEILING = 500_000
_CHARS_PER_TOKEN = 3.5  # 粗略值，只用于算上限，不用于计费

#: 读上下文文件的超时。见 read_text_with_timeout 的注释。
_CONTEXT_FILE_READ_TIMEOUT_SECS = 5.0

#: 运行时环境块的边界标记。
#: ★ 这两个常量不只是给人看的排版——**它们是"存盘提示词校验"的锚点**（见 system_prompt.py）。
#:   所以用户内容里如果出现同样的字符串，必须转义（见 build_runtime_block）。
RUNTIME_ENVIRONMENT_HEADING = "# mini-hermes runtime environment"
RUNTIME_ENVIRONMENT_END = "<!-- End mini-hermes runtime environment -->"

#: 身份回退。SOUL.md 不存在时用这段。
#: 对照 Hermes 的 DEFAULT_AGENT_IDENTITY —— 同名同用途。
DEFAULT_AGENT_IDENTITY = """你是 mini-hermes，一个跑在用户电脑上的终端 AI agent。

你的工作方式和普通聊天机器人不同：你**边想边动手**。
需要看文件就读，需要跑命令就跑，拿到结果再决定下一步——
不要凭记忆猜文件内容，也不要假设命令的结果。

原则：
- 动手前先看清楚：不确定就先读文件 / 列目录 / 跑一条探测命令。
- 多步任务用 todo 工具给用户看进度。
- 工具报错时读错误信息再改，不要重复同一个失败调用。
- 答复直接用中文，说清楚"我做了什么、结果是什么"，不要复述过程。"""

#: 第 2 章用的名字，保留为别名（避免老代码/测试 import 失败）。
BASE_IDENTITY = DEFAULT_AGENT_IDENTITY

#: 工具使用指导。对应 Hermes 的 _tool_guidance_block。
TOOL_USE_GUIDANCE = """## 工具使用

你需要调用工具才能真正动手，而不是描述你打算做什么。
- 需要看文件内容：read_file（**不要**用 terminal 里的 cat）
- 需要看目录结构：list_dir（**不要**用 ls）
- 需要搜索文件内容：search_files（**不要**用 grep/find）
- 需要跑命令、装东西、跑测试：terminal
- 多步任务：todo_list（先建清单，做完一条改一条）

工具进度对用户是可见的，所以不要在文字里复述你刚做了什么。"""

#: 平台提示。对应 Hermes 的 PLATFORM_HINTS（它里面有 10+ 个平台）。
#: mini 只有两个界面，所以只有两条。
PLATFORM_HINTS: Dict[str, str] = {
    "cli": (
        "你在一个终端里和用户对话。用简单的纯文本，不要用 Markdown 语法"
        "（终端不渲染 **粗体**、# 标题、表格）。列表用短横线或数字。"
    ),
    "tui": (
        "你在 Hermes 的 TUI 里和用户对话。支持方块字符但**不支持表格**，"
        "结构化数据用短横线或带标签的行。"
    ),
}

# --------------------------------------------------------------------------- #
# 安全扫描
# --------------------------------------------------------------------------- #

#: 提示注入模式。对应 Hermes 的 _scan_for_threats(scope="context")。
#: 它那边是一整套规则引擎，我们只留三条最典型的 + 一条中文的。
_INJECTION_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (
        "不可见 unicode",
        re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064]"),
    ),
    (
        "忽略之前的指令",
        re.compile(
            r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+"
            r"(instructions?|prompts?|rules?|messages?)",
            re.I,
        ),
    ),
    (
        "忽略之前的指令（中文）",
        re.compile(r"(忽略|无视|不要理(会)?)(之前|以上|前面|先前)的?(所有)?(指令|指示|要求|规则)"),
    ),
    (
        "凭据外泄尝试",
        re.compile(
            r"(curl|wget|fetch)\b[^\n]{0,120}"
            r"(\.env|api[_-]?key|secret|token|password|credential)",
            re.I,
        ),
    ),
)

#: 截断警告收集。对应 Hermes 的同名 ContextVar。
#: ★ 为什么要有它：**"我的 AGENTS.md 没生效"是个非常难猜的问题**——
#:   用户会以为是模型不听话，而不是"文件被截断了"。所以警告要能浮现到聊天里，不只写日志。
_truncation_warnings: List[str] = []


def drain_truncation_warnings() -> List[str]:
    """取出并清空累积的截断警告（对应 Hermes 的 drain_truncation_warnings）。"""
    global _truncation_warnings
    out, _truncation_warnings = _truncation_warnings, []
    return out


def scan_context_content(
    content: str, filename: str, *, user_authored: bool = False
) -> str:
    """扫提示注入；命中就把整块内容换成 BLOCKED 标记。

    ★ 关键设计：**同一个命中，处理方式取决于"谁写的"，而不是"写了什么"。**

    ====================  ==========================  ==========================
    来源                  信任等级                    命中时
    ====================  ==========================  ==========================
    ``SOUL.md``（用户家） 写入要过文件工具的审批门     **警告 + 照常加载**
    项目上下文文件        跟着仓库克隆进来的           **阻断，替换成 BLOCKED**
    ====================  ==========================  ==========================

    为什么这么分（抄 Hermes #112570 的理由，改成 mini 的说法）：

    - 用户的 ``SOUL.md`` 是**他自己写的**——一个用户如果在自己的安全指引里
      **记录**了"忽略之前的指令"这句话（比如一篇讲提示注入的笔记），
      **不该因此丢掉整个身份文件**。
    - 项目目录里的 ``AGENTS.md`` / ``.cursorrules`` 是**跟着仓库克隆进来的**，
      可能是别人写的。它会**逐字进入提示词**，所以只能阻断。

    另外：**开头的 UTF-8 BOM 是 Windows 编辑器的产物，不是注入。**
    一个 BOM 字符就会让用户的身份文件被替换成 BLOCKED——
    **这种误报的代价（agent 失去人格）远高于收益。**
    """
    if content.startswith("\ufeff"):
        content = content[1:]

    hits = [name for name, pattern in _INJECTION_PATTERNS if pattern.search(content)]
    if not hits:
        return content

    if user_authored:
        logger.warning(
            "上下文文件 %s 命中提示注入模式 %s；因为是用户自己写的，仍然加载。",
            filename,
            hits,
        )
        return content

    logger.warning("上下文文件 %s 命中提示注入模式 %s；已阻断。", filename, hits)
    return (
        f"[BLOCKED: {filename} 命中提示注入模式（{'、'.join(hits)}），内容未加载。"
        f"这可能只是误报——比如一篇讲提示注入的文档天然会命中。"
        f"如果这是你自己的文件，把它放到 mini home 里改名为 SOUL.md（用户文件只警告不阻断）。]"
    )


# --------------------------------------------------------------------------- #
# 读取与截断
# --------------------------------------------------------------------------- #


def read_text_with_timeout(
    path: Path, timeout: float = _CONTEXT_FILE_READ_TIMEOUT_SECS
) -> Optional[str]:
    """在**守护线程**里读文件，超时就当这个文件不存在。

    ★ 为什么需要这个（Hermes 同名函数的理由，值得单独记）：

    上下文文件可能在一个**卡住的网络挂载盘**上（NFS / sshfs / 断线的共享盘）。
    一次普通的 ``read_text()`` 就能让 agent **启动不了**——用户看到的是
    "敲了命令之后什么都不发生"。

    用守护线程 + 超时：
      - 读不完就放弃（返回 None，当作没有这个文件），**不是"最多等 5 秒"**
      - 守护线程不会阻止进程退出，所以超时后主线程能正常往下走

    📌 可迁移的原则：**"读取用户提供的路径"是一个可能永远不返回的操作。**
    任何"启动时读取用户环境"的代码都该有这个保护。
    """
    box: List[Optional[str]] = [None]

    def _read() -> None:
        try:
            box[0] = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 —— 读不到就是没有，不该阻塞启动
            box[0] = None

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    thread.join(timeout)
    return box[0]


def resolve_context_cwd() -> str:
    """决定"哪个目录算当前项目目录"。

    优先级（对应 Hermes 的 ``resolve_context_cwd`` + ``TERMINAL_CWD``）：

    =========================  ==================================================
    来源                       什么时候
    =========================  ==================================================
    ``MINI_CONTEXT_CWD``       显式指定（网关场景：一个进程服务多个会话）
    ``MINI_LAUNCH_CWD``        用户敲命令时所在的目录（启动器在 `cd` 之前记下）
    ``os.getcwd()``            兜底
    =========================  ==================================================

    ★ **为什么需要这一层**：``mini`` 启动脚本会 ``cd`` 到项目根——
      因为 Python 靠"脚本目录在 sys.path 上"才能 import 自己的模块。
      但那一步会把**用户的目录换掉**，后果是：

      - ``AGENTS.md`` 的发现会去看 mini-hermes 自己的目录（而不是用户的项目）
      - git 工作区快照会探到 mini-hermes 那个仓库

      **"进程从哪个目录启动"和"用户的项目在哪个目录"是两件事。**
      Hermes 管这个叫 launch artifact（启动残留物）——它的
      ``_context_cwd_is_launch_artifact`` 就是干这个的，而且它还会因此
      **拒绝加载 Hermes 安装树里自带的 AGENTS.md**，免得把自己的开发规则
      当成用户的项目规则注入进去。
    """
    for env_key in ("MINI_CONTEXT_CWD", "MINI_LAUNCH_CWD"):
        value = os.environ.get(env_key)
        if value and Path(value).is_dir():
            return value
    return os.getcwd()


def get_context_file_max_chars(context_length: Optional[int] = None) -> int:
    """上下文文件的字符上限：配置优先，否则按窗口缩放。

    对应 Hermes 的 ``_get_context_file_max_chars`` / ``_dynamic_context_file_max_chars``。
    """
    configured = _read_config().get("context_file_max_chars")
    if isinstance(configured, (int, float)) and configured > 0:
        return int(configured)
    if not isinstance(context_length, int) or context_length <= 0:
        return CONTEXT_FILE_MAX_CHARS  # 窗口未知 → 用固定下限（防御性默认）
    budget = int(context_length * _CHARS_PER_TOKEN * _CONTEXT_FILE_WINDOW_FRACTION)
    # 永远是"夹在上下限中间"的那个值
    return max(CONTEXT_FILE_MAX_CHARS, min(budget, _CONTEXT_FILE_DYNAMIC_CEILING))


def truncate_content(
    content: str,
    filename: str,
    max_chars: Optional[int] = None,
    read_path: Optional[str] = None,
    queue_warning: bool = True,
) -> str:
    """头 70% + 尾 20% + 中间一个标记。

    ★ 三个细节（都抄自 Hermes 的实测实现）：

    **① 70 + 20 = 90，不是 100。** 剩下的 10% 留给标记本身——
       否则"截断后的结果"会超过你自己设的上限。

    **② 头 70 / 尾 20 的不对称是有理由的**：文件开头通常是"是什么/怎么用"，
       结尾通常是"注意事项/已知问题"。**中间最可能是大段细节，切掉损失最小。**

    **③ 标记里必须带"怎么拿回全文"的指令。**
       Agent 被截断之后不是无助的——它被明确告知了补救动作。
       这比干巴巴写一句"内容被截断"有用得多。
    """
    if max_chars is None:
        max_chars = get_context_file_max_chars()
    if len(content) <= max_chars:
        return content

    head_chars = int(max_chars * CONTEXT_TRUNCATE_HEAD_RATIO)
    tail_chars = int(max_chars * CONTEXT_TRUNCATE_TAIL_RATIO)
    remedy = (
        "把这个文件改小，或把 mini home 里 config.json 的 "
        "context_file_max_chars 调大。"
        if queue_warning
        else f"完整文件仍然可以用 read_file 读：{read_path or filename}"
    )
    msg = (
        f"⚠️  上下文文件 {filename} 被截断：{len(content)} 字符超过上限 "
        f"{max_chars} —— {remedy}"
    )
    logger.warning(msg)
    if queue_warning:
        _truncation_warnings.append(msg)

    marker = (
        f"\n\n[...已截断 {filename}：保留了 {head_chars}+{tail_chars} / {len(content)} 字符。"
        f"中间部分被省略 —— 如果你需要完整内容，用 read_file 工具读完整的文件："
        f"{read_path or filename}]\n\n"
    )
    return content[:head_chars] + marker + content[-tail_chars:]


# --------------------------------------------------------------------------- #
# 身份：SOUL.md
# --------------------------------------------------------------------------- #


def soul_md_path() -> Path:
    """``~/.mini-hermes/SOUL.md`` 的路径。"""
    return get_mini_home() / "SOUL.md"


def load_soul_md(
    context_length: Optional[int] = None, require_exists: bool = True
) -> Optional[str]:
    """读 ``~/.mini-hermes/SOUL.md`` 当身份。不存在返回 None（调用方回退硬编码身份）。

    对应 Hermes 的 ``load_soul_md()``。三步：**读 → 扫 → 截**。

    ★ 注意 ``user_authored=True``：SOUL.md 命中注入模式时**只警告、照常加载**。
      理由见 ``scan_context_content``。

    ★ 还有一步不在这里：**防重复**。SOUL.md 已经当了身份，
      所以 ``build_context_files_prompt(skip_soul=True)`` 要阻止它再当一次上下文文件。
      （Hermes 同名函数也带这个参数。）
    """
    path = soul_md_path()
    if require_exists and not path.exists():
        return None
    raw = read_text_with_timeout(path)
    if raw is None:
        return None
    content = raw.strip()
    content = scan_context_content(content, "SOUL.md", user_authored=True)
    content = truncate_content(content, "SOUL.md", read_path=str(path))
    return content or None


# --------------------------------------------------------------------------- #
# 项目上下文文件（4 级优先级）
# --------------------------------------------------------------------------- #

#: 优先级顺序：**首个匹配者胜出，只加载一种。**
#: （True = 从 cwd 一路向上走到 git root；False = 只看 cwd）
CONTEXT_FILE_CANDIDATES: Tuple[Tuple[Tuple[str, ...], bool], ...] = (
    ((".mini.md", "MINI.md"), True),  # 1. mini 原生项目配置（走到 git root）
    (("AGENTS.md",), False),  # 2. 通用 agent 指令文件（只看 cwd）
    (("CLAUDE.md",), False),  # 3. 兼容 Claude Code
    ((".cursorrules",), False),  # 4. 兼容 Cursor
)


def _find_git_root(start: Path) -> Optional[Path]:
    """最近的含 ``.git`` 的祖先目录（含自身），没有就 None。"""
    current = start
    for _ in range(64):  # 上限，防止在奇怪的文件系统上无限走
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def _find_context_file(
    cwd: Path, names: Tuple[str, ...], walk_up: bool
) -> Optional[Path]:
    """在 cwd（可选一路向上到 git root）里找这些文件名，返回第一个存在的。"""
    if not walk_up:
        for name in names:
            candidate = cwd / name
            if candidate.is_file():
                return candidate
        return None
    root = _find_git_root(cwd) or cwd
    current = cwd
    while True:
        for name in names:
            candidate = current / name
            if candidate.is_file():
                return candidate
        if current == root or current.parent == current:
            return None
        current = current.parent


def _strip_yaml_frontmatter(content: str) -> str:
    """去掉可选的 ``---`` YAML frontmatter，只留正文。

    对应 Hermes 的 ``_strip_yaml_frontmatter``
    （那边的 frontmatter 是给未来的配置覆盖预留的）。
    """
    if not content.startswith("---"):
        return content
    lines = content.splitlines()
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[i + 1 :]).lstrip("\n")
    return content


def build_context_files_prompt(
    cwd: Optional[str] = None,
    skip_soul: bool = False,
    context_length: Optional[int] = None,
) -> str:
    """项目上下文文件 + （可选）SOUL.md，拼成 ``# Project Context`` 块。

    对应 Hermes 的同名函数。

    ★ **"只加载一种"是靠"找到就 break"实现的**（Hermes 用 ``or`` 链）。
      语义上有个坑要注意：**一个空文件会被当成"没有"**，于是加载下一个。
      这通常是你想要的（空文件没有信息量），但要知道有这个行为。

    ★ **为什么不全部合并**：一个仓库里同时有 AGENTS.md / CLAUDE.md / .cursorrules
      很常见。全塞进去 → ① 上下文被吃掉 ② 三份规则互相矛盾时没有优先级语义，
      模型不知道该听谁的。**只取第一个，优先级语义清楚。**
    """
    cwd_path = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    max_chars = get_context_file_max_chars(context_length)

    project_context = ""
    for names, walk_up in CONTEXT_FILE_CANDIDATES:
        found = _find_context_file(cwd_path, names, walk_up)
        if found is None:
            continue
        raw = read_text_with_timeout(found)
        if raw is None or not raw.strip():
            continue  # 空文件当作没有，继续试下一个
        body = _strip_yaml_frontmatter(raw)
        body = scan_context_content(body, found.name, user_authored=False)
        body = truncate_content(
            body, found.name, max_chars=max_chars, read_path=str(found)
        )
        project_context = f"## {found.name}\n{body}"
        break

    sections: List[str] = []
    if project_context:
        sections.append(project_context)

    # SOUL.md 独立于项目上下文（它在家目录，不在项目里）
    if not skip_soul:
        soul = load_soul_md(context_length)
        if soul:
            sections.append(f"## SOUL.md\n{soul}")

    if not sections:
        return ""

    return (
        "# Project Context\n\n"
        "以下项目上下文文件已加载，应当遵守：\n\n" + "\n\n".join(sections)
    )


# --------------------------------------------------------------------------- #
# 记忆 / 用户画像
# --------------------------------------------------------------------------- #


def _load_home_markdown(filename: str, heading: str) -> str:
    """读 mini home 下的一个 md 文件，返回带标题的块（不存在返回空串）。"""
    path = get_mini_home() / filename
    if not path.is_file():
        return ""
    raw = read_text_with_timeout(path)
    if raw is None or not raw.strip():
        return ""
    body = truncate_content(raw.strip(), filename, read_path=str(path))
    # 记忆和 SOUL.md 同一信任等级：都是用户自己写的
    body = scan_context_content(body, filename, user_authored=True)
    if not body.strip():
        return ""
    return f"{heading}\n{body}"


def load_memory_block() -> str:
    """``~/.mini-hermes/MEMORY.md`` → 跨会话记忆快照。"""
    return _load_home_markdown("MEMORY.md", "## 持久记忆（MEMORY.md）")


def load_user_profile_block() -> str:
    """``~/.mini-hermes/USER.md`` → 用户画像快照。"""
    return _load_home_markdown("USER.md", "## 用户画像（USER.md）")


# --------------------------------------------------------------------------- #
# 工作区快照（git）
# --------------------------------------------------------------------------- #


def workspace_snapshot(cwd: Optional[str] = None) -> str:
    """git 工作区快照：当前分支 + 改动计数。

    对应 Hermes 的 ``coding_context._git()`` + ``_parse_status()``。

    ★ 它为什么必须**钉住**（pin）而不能每轮重探：
      见 system_prompt.py 里 ``_workspace_parts`` 的注释——
      **重探一次就可能得到不同的字节，那就等于亲手把提示词搞不稳定。**

    ★ 为什么它是**最末端的上下文块**：
      它是唯一"跨 worktree 会变"的东西，所以要排在共享文件（AGENTS.md）之后。
      这条规则让不同 worktree 的会话能共享整个 context 层的前缀（见 §2）。
    """
    cwd = cwd or os.getcwd()
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain=2", "--branch"],
            capture_output=True,
            text=True,
            timeout=5,  # ★ 也可能是卡住的网络盘 / 巨大的仓库
        )
    except Exception:  # noqa: BLE001 —— 探测失败绝不能影响提示词构建
        return ""
    if proc.returncode != 0:
        return ""

    branch = ""
    ahead = behind = ""
    counts = {"staged": 0, "modified": 0, "untracked": 0, "conflicts": 0}
    for line in proc.stdout.splitlines():
        if line.startswith("# branch.head"):
            parts = line.split(maxsplit=2)
            if len(parts) >= 3:
                branch = parts[-1]
        elif line.startswith("# branch.ab"):
            parts = line.split()
            if len(parts) >= 4:
                ahead = parts[2].lstrip("+")
                behind = parts[3].lstrip("-")
        elif line.startswith(("1 ", "2 ")):
            fields = line.split(maxsplit=2)
            if len(fields) >= 2 and len(fields[1]) >= 2:
                xy = fields[1]
                if xy[0] not in ".?":
                    counts["staged"] += 1
                if xy[1] != ".":
                    counts["modified"] += 1
        elif line.startswith("? "):
            counts["untracked"] += 1
        elif line.startswith("u "):
            counts["conflicts"] += 1

    if not branch:
        return ""

    bits = [f"当前分支：{branch}"]
    if ahead or behind:
        bits.append(f"与上游相差：领先 {ahead or 0} / 落后 {behind or 0} 个提交")
    dirty = [
        f"{label} {counts[key]}"
        for key, label in (
            ("staged", "已暂存"),
            ("modified", "已修改"),
            ("untracked", "未跟踪"),
            ("conflicts", "冲突"),
        )
        if counts[key]
    ]
    bits.append("工作区改动：" + ("、".join(dirty) if dirty else "干净"))
    return "# 工作区状态\n\n" + "\n".join(bits)


# --------------------------------------------------------------------------- #
# 平台提示
# --------------------------------------------------------------------------- #


def resolve_platform_hint(
    platform_key: str, overrides: Optional[Dict[str, Any]] = None
) -> str:
    """应用 ``platform_hints.<平台>`` 配置覆盖：``replace`` 替换、``append`` 追加。

    对应 Hermes 的 ``_resolve_platform_hint``。

    ★ 配置格式（对应 Hermes config.yaml 顶层的 ``platform_hints``）：

    ```json
    {
      "platform_hints": {
        "cli": { "append": "表格用对齐的空格画，别用竖线。" },
        "tui": "回答尽量短。"
      }
    }
    ```

    裸字符串 = ``append`` 的简写；两者同时存在时 **``replace`` 胜出**。

    ★ **坏配置的默认行为是"当作没配"，不是报错、也不是拒绝启动。**
      理由（抄 Hermes 的 docstring）：*bad config can never break prompt
      assembly or leak across platforms*（坏配置永远不能弄坏提示词装配，
      也不能跨平台泄露）。
    """
    default_hint = PLATFORM_HINTS.get(platform_key, "")
    spec = (overrides or {}).get(platform_key)
    if isinstance(spec, str):
        spec = {"append": spec}  # 裸字符串 → append
    if not isinstance(spec, dict):
        return default_hint
    replace_text = spec.get("replace")
    append_text = spec.get("append")
    replace_text = replace_text.strip() if isinstance(replace_text, str) else ""
    append_text = append_text.strip() if isinstance(append_text, str) else ""
    base = replace_text or default_hint  # replace 优先，靠 or 短路
    return f"{base}\n\n{append_text}".strip() if append_text else base


# --------------------------------------------------------------------------- #
# 时间戳
# --------------------------------------------------------------------------- #


def timestamp_line(session_start: datetime, now: Optional[datetime] = None) -> str:
    """会话开始日 + （跨天时）重建当日。

    对应 Hermes 的 ``_timestamp_line``。两行设计：

    =========================  ==========  ==============================
    行                         何时出现    稳定性
    =========================  ==========  ==============================
    ``Conversation started``   总是        永不变（会话开始日）
    ``Today's date``           只在跨天    只在重建时刷
    =========================  ==========  ==============================

    ★ 为什么第一行**只有日期没有时刻**（*byte-stable for the day*）：
      如果写上时分秒，**每一轮都变，缓存每轮都断**。
      只到日期，当天之内这一行的字节完全不变。

    ★ 第二行为什么只在跨天时出现（这段理由本身就是一份设计文档）：
      长期会话会跨越很多天。**孤零零一个"出生日期"会让模型以为自己还活在老日子里。**
      而提示词在重建时会重算这一行——**重建恰好也是缓存前缀本来就要失效的时刻**，
      所以这次刷新是**免费的**。

      📌 可迁移的原则：**把"必须更新的东西"绑在一个已经发生失效的事件上，
      而不是引入新的失效点。**
    """
    now = now or datetime.now()
    zone = _zone_suffix(now)
    line = f"Conversation started: {session_start.strftime('%A, %B %d, %Y')}{zone}"
    if now.strftime("%Y%m%d") != session_start.strftime("%Y%m%d"):
        line += (
            f"\nToday's date (as of the last context rebuild): "
            f"{now.strftime('%A, %B %d, %Y')} — trust this over the start date "
            f"for what day it is now; query the terminal for exact time."
        )
    return line


def _zone_suffix(now: datetime) -> str:
    """带时区名 + 偏移，让工具不必猜 EST 还是 EDT（抄 Hermes 的注释）。"""
    try:
        name = now.astimezone().tzname() or ""
    except Exception:  # noqa: BLE001
        name = ""
    try:
        offset = now.astimezone().strftime("%z")
    except Exception:  # noqa: BLE001
        offset = ""
    bits = [b for b in (name, offset) if b]
    return f" ({', '.join(bits)})" if bits else ""


# --------------------------------------------------------------------------- #
# 运行时环境块
# --------------------------------------------------------------------------- #


def build_environment_hints(backend: str = "local", cwd: Optional[str] = None) -> str:
    """执行环境块：**本地后端**给主机 OS / home / cwd；远端后端只给后端自己的状态。

    对应 Hermes 的 ``build_environment_hints``（那边还处理 WSL / embedder / 7 种后端）。

    ★ **这条原则是这次读源码才发现的，值得单独记：**

      ===========  ============================  ==========================
      后端          给什么                         为什么
      ===========  ============================  ==========================
      local        主机的 OS / home / **cwd**      agent 的工具直接作用在这台机器上
      远端/沙箱     只有后端自己的状态              工具**碰不到**主机
      ===========  ============================  ==========================

      Hermes 的原话：*the agent's tools cannot touch the host*。

      **如果 agent 跑在容器里，而提示词写着宿主机的 cwd，
      它会以为 ``read_file /app/project/x.py`` 是可达的——实际不可达。**

      📌 **"正确的信息 + 错误的执行环境"比"没有信息"更糟。**
      提示词必须描述**真实**的执行环境，不能描述你以为的环境。
    """
    cwd = cwd or os.getcwd()
    if backend != "local":
        return f"终端后端：{backend}（远端/沙箱）。你的工具作用于该后端，不是本机。"

    home = str(Path.home())
    return "\n".join(
        [
            f"主机操作系统：{_platform.system()} {_platform.release()} "
            f"({_platform.machine()})",
            f"用户 home 目录：{home}",
            f"主机名：{socket.gethostname()}",
            # ★ cwd 放最后：它是这一层里最可能变的东西（见 §2 的排序原则）
            f"当前工作目录：{cwd}",
        ]
    )


def build_runtime_block(backend: str = "local", cwd: Optional[str] = None) -> str:
    """给运行时环境块套上边界标记。

    ★ **转义那一行是防御性的，不是排版**：
      如果环境块**自己的内容里**出现了边界标题（比如主机名就叫这个），
      外层的解析就会错——而"存盘提示词校验"正是靠这个标题当锚点。
      加个 ``"> "`` 是最小代价的消歧。
    """
    hints = build_environment_hints(backend, cwd)
    if not hints:
        return ""
    escaped = hints.replace(
        RUNTIME_ENVIRONMENT_HEADING, "> " + RUNTIME_ENVIRONMENT_HEADING
    )
    return (
        f"{RUNTIME_ENVIRONMENT_HEADING}\n\n{escaped}\n\n{RUNTIME_ENVIRONMENT_END}"
    )


# --------------------------------------------------------------------------- #
# 配置读取
# --------------------------------------------------------------------------- #


def _read_config() -> Dict[str, Any]:
    """读 ``~/.mini-hermes/config.json``（对应 Hermes 的 ``config.yaml``）。

    ★ 坏配置永远不抛异常——**一个用户的笔误不该让 agent 起不来。**
    """
    path = get_mini_home() / "config.json"
    if not path.is_file():
        return {}
    raw = read_text_with_timeout(path)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        logger.warning("config.json 解析失败，按空配置处理：%s", path)
        return {}
    return data if isinstance(data, dict) else {}


def load_platform_hint_overrides() -> Dict[str, Any]:
    """从 config.json 取 ``platform_hints`` 段。"""
    value = _read_config().get("platform_hints")
    return value if isinstance(value, dict) else {}
