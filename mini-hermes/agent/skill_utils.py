"""技能发现与索引（对应 Hermes 的 `agent/skill_utils.py`，真实版 811 行）。

第 13 章（A1~A4）新增：**发现 + 渐进披露索引 + 一个 `skill_view`**，就这三件。

## 抄了什么，没抄什么

抄：`skills/<name>/SKILL.md` 目录契约 · frontmatter 只取 `name`/`description` ·
描述硬截 60 字符 · 坏技能**跳过并计数**（宽容） · 扫描结果排序后返回（索引字节稳定）。

没抄（都需要 mini 没有的基座，理由见第 13 章清单 B 批）：
发布/安装（无网络客户端与安装事务）· 安全扫描与四档信任（技能都是本地手写）·
条件激活（`requires_toolsets` 等五键）· `skills.config`（无配置迁移面）·
curator（无长命进程）。

## 目录契约

```text
<MINI_HOME>/skills/
    alpha/SKILL.md            ← frontmatter: name + description（正文不在索引里）
    beta/SKILL.md
    alpha/references/*.md     ← 支持目录：是"第二跳"，不是独立技能
    .git/  __pycache__/ ...   ← 排除
```

★ **每个 profile 一套技能**：路径 = `get_mini_home()/"skills"`。
`get_mini_home()` 决定当前是哪个 profile（一个 profile = 一个 home，
见 `hermes_cli/profiles.py`），所以"技能属于谁"这件事**不用另写一份 profile 逻辑**。

★ **只扫一层**（真实 Hermes 会递归走分类目录，如 `skills/github/xxx`）。
窄在哪、为什么敢窄：mini 没有"分类"这个消费者（没有 browse、没有 hub 安装），
一层够用；等真有分类需求再补递归 —— 不做投机性实现。

## 两个根：用户级 + 内置级（用户优先）

★ 技能有两个来源，**共存**：

```text
<MINI_HOME>/skills/            ← 用户级：每人一套，用户随手写、随手改（skills_dir()）
<repo>/mini-hermes/skills/     ← 内置级：随程序发布、只读、升级时可能被整体替换（bundled_skills_dir()）
```

`skill_roots()` 按 **`[用户, 内置]`** 的顺序把它们拼起来 —— **首见胜出**，
于是**用户同名技能覆盖内置技能**（PATH 语义：前面的目录优先）。真实侧
`agent/skill_utils.py:398-400` 逐字写着 *"Skill dirs: local `~/.hermes/skills/`
first, then create_dir, then external."*，mini 抄的就是"用户在前"这一条
（少了 create_dir / external 两层，因为 mini 没有那两个来源）。

★ **为什么不做信任门**：真实侧对"项目内技能"另有一道门 ——
只有 root 进了 `skills.trusted_project_dirs` 才加载（`agent/skill_utils.py:409-413`），
理由是*"a prompt-injection vector if auto-sourced from any clone"*。
判据是**来源是否可信**：那些技能可能**从任意 clone 里被自动加载**。
mini **没有这个来源** —— 两个根都可信（内置随程序发布、用户根是用户本人机器上手写的），
不存在"从外部 clone 自动加载"的路径，信任门没有可保护的攻击面 ⇒ **不做**。

## 这个文件是「技能这件事」的唯一实现

`agent/prompt_builder.load_skill_index()`（进提示词的索引）与
`tools/skills.py`（按需读正文的 `skill_view`）**都调这里的函数**。
扫目录这件事只写一遍 —— 本项目已经三次踩过"同一事实写两处 → 漂移"
（工具分发、结果回填、状态重建，见 toolsets.py 的注释）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hermes_constants import get_mini_home

logger = logging.getLogger(__name__)

__all__ = [
    "SKILL_FILENAME", "SKILL_PROMPT_DESC_LIMIT", "SKILL_INDEX_HEADING",
    "EXCLUDED_SKILL_DIRS", "SUPPORT_DIRS",
    "Skill", "SkippedSkill", "SkillScan",
    "skills_dir", "bundled_skills_dir", "skill_roots", "iter_skill_dirs",
    "split_frontmatter", "parse_frontmatter",
    "is_valid_skill_name", "truncate_description", "read_skill",
    "discover_skills", "resolve_skill", "load_skill_body",
    "build_skill_index", "has_skills",
]

#: 一个技能就是一个目录 + 这个文件名（对应真实侧 `SKILL.md`）。
SKILL_FILENAME = "SKILL.md"

#: 索引里 description 的硬上限（字符）。
#: ★ 出处：真实 Hermes `agent/skill_utils.py:741` `SKILL_PROMPT_DESC_LIMIT = 60`。
#:   为什么是**硬截**而不是"要求作者写短点"：索引每一轮请求都在上下文里，
#:   而作者规范（`skills/AGENTS.md`）说 description ≤ 60 字符 —— 截断是**兜底**，
#:   让一条没守规矩的描述不至于把索引撑爆。mini 把这个数字写死并注明出处。
SKILL_PROMPT_DESC_LIMIT = 60

#: 索引块的第一行。★ 测试靠它断言"索引就是 volatile 层的首块"，
#: 所以它是一个**契约常量**，不是随手写的一句话。
SKILL_INDEX_HEADING = "## 可用技能（技能索引）"

#: 扫描时排除的目录（对应真实 `agent/skill_utils.py:21-27` `EXCLUDED_SKILL_DIRS`）。
#: 真实版那份更长（`.hub` / `.archive` / `.curator_backups` / `.venv` / ...），
#: mini 只留"真的可能出现"的几个：.git 是仓库级忽略、__pycache__ 是解释器产物、
#: node_modules 是别人把技能库当项目目录时的常见垃圾。
EXCLUDED_SKILL_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv"})

#: 支持目录（对应真实 `agent/skill_utils.py:29-31` `SKILL_SUPPORT_DIRS`）。
#: ★ 它们是**渐进披露的第二跳**：`references/` 里的大文档既不该被当成独立技能，
#:   也不该进提示词 —— 需要时由 `skill_view` 报出目录列表，模型再决定要不要看。
#:   mini 只扫一层，所以这个常量目前只用来**说明**（不参与扫描）；这不是死代码，
#:   它是"为什么一层就够"的理由所在。
SUPPORT_DIRS = ("references", "templates", "assets", "scripts")


# ── 数据形状 ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Skill:
    """一个**可用**技能（= frontmatter 解析通过的那些）。

    ★ 注意这里**没有正文**这个字段：索引只需要名字 + 描述，
      正文只有 `skill_view` 按需读。少一个字段 = 少一条"正文不小心进提示词"的路。
    """

    name: str
    description: str
    path: Path          # SKILL.md 的路径
    dir: Path           # 技能目录


@dataclass(frozen=True)
class SkippedSkill:
    """一个被跳过的技能目录 + **可读的原因**（计数之外还要能解释）。"""

    dir_name: str
    reason: str


@dataclass(frozen=True)
class SkillScan:
    """一次扫描的结果：能用的（已按名字排序）+ 跳过的（用来计数与解释）。"""

    skills: List[Skill] = field(default_factory=list)
    skipped: List[SkippedSkill] = field(default_factory=list)


# ── 目录 ────────────────────────────────────────────────────────────────
def skills_dir() -> Path:
    """技能库根目录：``<MINI_HOME>/skills``（**唯一来源**）。

    ★ 为什么必须走 `get_mini_home()` 而不是写死 `~/.mini-hermes/skills`：
      ① profile 就是 home —— 每个 profile 天然有自己的一套技能，一行就隔离好了；
      ② 测试 / CI / E2E 会把 `MINI_HOME` 重定向到临时目录。写死的后果是
         **测试去读用户真实的技能库**（不报错，只是读错了地方）——
         这条坑在 `hermes_constants.state_db_path()` 的注释里已经踩过一次。
    """
    return get_mini_home() / "skills"


def bundled_skills_dir() -> Path:
    """**内置技能库**根目录：``<repo>/mini-hermes/skills``（随程序发布）。

    ★ 与 `skills_dir()` 的区别：`skills_dir()` 是**用户级**（`<MINI_HOME>/skills`，
      每人一套、用户随手写随手改）；这里是**随程序发布的内置技能** —— 它跟着源码走，
      不该由用户改，升级（重新拉一份代码）时可能被**整体替换**。
      这正是真实 Hermes 里 `skills/`（随包内置）与 `~/.hermes/skills/`（用户本地）的分工。
    ★ 路径 = `Path(__file__).resolve().parent.parent / "skills"`：本文件住在
      `mini-hermes/agent/` 下，上翻两级即 `mini-hermes/`，内置技能就在它的 `skills/`。
      `resolve()` 是为了让"经由符号链接启动的仓库"也指到真身。
    ★ 目录**可能不存在**（还没放内置技能的检出）—— 那不算错：发现层面安静跳过
      （见 `discover_skills`；兼容"只有用户级"的旧环境）。
    """
    return Path(__file__).resolve().parent.parent / "skills"


def skill_roots() -> List[Path]:
    """所有技能库根，**用户优先、内置兜底**（顺序 = 优先级，不得调换）。

    ``[skills_dir(), bundled_skills_dir()]`` —— 逐字对齐真实侧
    `agent/skill_utils.py:398-400` 的 *"Skill dirs: local ``~/.hermes/skills/`` first,
    then create_dir, then external."*：**第一个找到的胜出**（首见胜出），
    于是用户同名技能能覆盖内置技能 —— 这就是 PATH 的语义（前面的目录优先）。
    真实侧还多两层（`skills.create_dir` / `skills.external_dirs`），mini 没有那两个来源
    （见模块 docstring 的"没有来源这个概念"），只留"用户 → 内置"两层。
    """
    return [skills_dir(), bundled_skills_dir()]


def _root_label(root: Path) -> str:
    """技能根的**诊断短名**（让人一眼看出覆盖/命中发生在哪个根）。

    ★ 为什么不能直接用 `root.name`：用户根与内置根的 basename **都是 `skills`**，
      光看目录名分不开"谁覆盖了谁"。所以按**角色**取名：用户根 → ``用户``，
      内置根 → ``内置``；显式传入的其它根（如测试的临时目录）回退到目录名。
    ★ 用 `resolve()` 比较：用户根可能是 `/var/...`（macOS 上 `/var` 是 `/private/var`
      的软链），不解析就会与 `skills_dir()` 的写法对不上。
    """
    p = Path(root)
    try:
        rp = p.resolve()
        user_root = skills_dir().resolve()
        bundled_root = bundled_skills_dir().resolve()
    except OSError:
        return p.name or "根"
    if rp == user_root:
        return "用户"
    if rp == bundled_root:
        return "内置"
    return p.name or "根"


def _iter_one_root(base: Path) -> List[Path]:
    """**单个根**下的候选技能目录（排序后返回；跳过隐藏目录 / 排除目录 / 非目录）。

    这是 `iter_skill_dirs` 的**逐根实现**；多根只是把它按根序拼起来（扫描规则只写这一处）。
    """
    if not base.is_dir():
        return []
    out: List[Path] = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or entry.name in EXCLUDED_SKILL_DIRS:
            continue
        if entry.is_dir():
            out.append(entry)
    return out


def _within(path: Path, base: Path) -> bool:
    """``path`` 落在 ``base`` 目录内吗（不含等于 base 本身之外的特殊分支，纯前缀判定）。"""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def iter_skill_dirs(root: Optional[Path] = None) -> List[Path]:
    """**候选**技能目录（排序后返回；跳过隐藏目录 / 排除目录 / 非目录）。

    ★ 显式传 `root=` ⇒ **只看这一个根**（单根语义，调用方与测试依赖）；
      不传 ⇒ 遍历 `skill_roots()`（用户根 → 内置根），把各根结果**按根序拼接**。
    ★ 跨根顺序必须确定：**根顺序固定**（`skill_roots()` 的顺序）×
      **根内按目录名排序**（`_iter_one_root` 的 sorted）⇒ 同一批技能两次扫描得到
      同一个候选序列。这是"索引字节稳定"（A4 第一条不变量）的一半；
      另一半是 `discover_skills` 最后按**技能名**再排一次。
    ★ 真实 Hermes 也是"扫完排序再 yield"（`iter_skill_index_files`：
      `yield from map(Path, sorted(matches))`）。
    ★ 这个函数**只回答"去哪儿看"**，"看到的算不算技能"归 `read_skill()`——
      包括"符号链接目录不算"。为什么不在这里悄悄滤掉：漏看的技能
      和"看见了但拒绝"的技能在诊断时是两件事，后者要出现在 `skipped` 里（有计数、有原因）。
    """
    if root is not None:
        return _iter_one_root(Path(root))
    out: List[Path] = []
    for base in skill_roots():
        out.extend(_iter_one_root(base))
    return out


# ── frontmatter（零依赖的**子集**解析器）──────────────────────────────────
_FENCE = "---"


def split_frontmatter(text: str) -> Tuple[Optional[Dict[str, str]], str]:
    """把 SKILL.md 拆成 ``(frontmatter 字典或 None, 正文)``。

    ★ **零第三方依赖**：这是一个只支持 mini 所需**子集**的解析器，**不是 YAML**：

      | 支持 | 不支持（一律忽略） |
      |---|---|
      | 首行 `---` 围栏，下一个 `---` 收尾 | 没有围栏 / 围栏未闭合 → 返回 `None` |
      | 顶层 `key: value`，一行一条 | 缩进嵌套（`metadata.hermes.*`） |
      | 值两侧空白去掉、成对引号去掉、空值丢掉 | 列表（`- a`）、多行块（`\\|` `>` —— **标记与块内容一起丢，整个键视为不支持**）、锚点/别名 |
      | 空行与整行 `#` 注释 | 行尾注释（`x: y  # 注释` —— 会被当成值的一部分） |

    为什么敢不支持：mini 的索引只需要 `name` / `description` 两个**标量**字段。
    真实 Hermes 用真 YAML 解析器读 `platforms` / `metadata.hermes.*` / `tags`，
    但那些的消费者（条件激活 / 蓝图 / `skills.config`）都不在 A 批 ——
    **为了两个标量字段引入一个 YAML 解析器不值**（本项目硬约束：零第三方依赖）。
    真要加嵌套的那天，改这个函数 + 加一条测试即可（改动面就是这里）。
    """
    if not isinstance(text, str):
        return None, ""
    # 真实侧同样先剥 BOM（`agent/skill_utils.py:105-125`）——否则首行 `---` 对不上。
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            return _parse_scalar_lines(lines[1:i]), "\n".join(lines[i + 1:]).lstrip("\n")
    return None, text      # 围栏未闭合 = 坏 frontmatter，宽容地交回全文


def _parse_scalar_lines(lines: List[str]) -> Dict[str, str]:
    """frontmatter 体 → 字典。只认**顶层** `key: value`（见 split_frontmatter 的表）。"""
    out: Dict[str, str] = {}
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[:1].isspace():
            # 缩进行 = 嵌套结构（metadata.hermes.* / 列表项 / 多行块的**块内容**）→
            # 子集不认识，忽略。
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = _unquote(value.strip())
        if not key or not value:
            # 空值一律丢：`metadata:` / `description: ""` 在这里**分不开**
            # （一个值都没有的键对索引毫无用处），这是子集口径的一部分。
            continue
        if _is_block_scalar_marker(value):
            # ★ 多行块标记：`description: >` 后面是缩进的**块内容**（上面已被当作
            #   缩进行忽略）——如果只丢块内容、不丢标记，就会留下一个**字面值 `>`**
            #   进索引，真正的描述文本反而被丢。docstring 承诺"多行块一律忽略"，
            #   那就必须**把标记一起丢掉**（整键视为不支持）。于是这类技能会因
            #   **缺 description** 被跳过并计数，而不是以 `>` 混进提示词索引。
            continue
        out[key] = value
    return out


def _is_block_scalar_marker(value: str) -> bool:
    """值是 YAML 的**多行块标记**吗（`|` / `>`，可带 chomping/缩进指示符如 `>-` `|+2`）？

    ★ 为什么单独判这件事：`description: >` 的**块内容**是缩进行（已忽略），
      但那个 `>` 本身是**非空标量**，不挡的话会被当成描述值写进索引。
      承诺是"一律忽略"，就要连标记一起丢。
    """
    if value[:1] not in ("|", ">"):
        return False
    return all(ch in "+-0123456789" for ch in value[1:])   # 空串（纯 `>`/`|`）也成立


def _unquote(value: str) -> str:
    """成对的单/双引号去掉（`name: "alpha"` 与 `name: alpha` 等价）。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> Optional[Dict[str, str]]:
    """只要 frontmatter 字典（`split_frontmatter` 的薄壳）。不可解析 → `None`。"""
    return split_frontmatter(text)[0]


# ── 名字合法性（**安全边界**）────────────────────────────────────────────
def is_valid_skill_name(name: str) -> bool:
    """技能名合法吗？—— 这是安全边界，不是代码风格。

    拒：空/全是空白 · 前后带空白 · 含 ``..`` · 含路径分隔符（``/`` ``\\``）·
    含 NUL · 以 ``.`` 开头 · 含任何空白字符。

    ★ 为什么这么保守：技能名只该是"提示词索引里那行的名字"。
      真实 Hermes 的 `skill_view` 会把名字解析成路径片段，所以它对每个名字都要
      走一遍路径校验；mini 的做法更省事也更强 —— **名字只用来做相等比较**
      （见 `resolve_skill`），路径永远来自扫描结果，于是"越界"在结构上不可能发生。
      这一层在这里的意义是"**早点给出可读的拒绝理由**"，而不是唯一防线。
    """
    if not isinstance(name, str):
        return False
    if not name or name != name.strip():
        return False
    if name.startswith(".") or ".." in name:
        return False
    if any(ch in name for ch in ("/", "\\", "\x00")):
        return False
    if any(ch.isspace() for ch in name):
        return False
    return True


# ── 读一个技能目录 ───────────────────────────────────────────────────────
def read_skill(skill_dir: Path) -> Tuple[Optional[Skill], str]:
    """读一个技能目录 → ``(Skill, "")`` 或 ``(None, 跳过原因)``。

    ★ **宽容**：坏技能**跳过并计数**，永不抛异常、永不"带病进索引"
      （真实侧同样宽容：frontmatter 读不出来就当没有）。
      但有一处 mini **刻意不照抄**：真实侧 `name` 缺失时回落到**目录名**
      （`tools/skills_tool.py:604` `frontmatter.get("name", skill_md.parent.name)`）。
      mini 不回落到目录名 —— 名字是 `skill_view` 的**寻址键**，
      猜出来的名字会让模型去 load 一个它以为存在的东西。
      少一个技能（有计数、有原因），比多一个错名字好。
    """
    path = skill_dir / SKILL_FILENAME
    if skill_dir.is_symlink():
        # ★ 技能库是本地目录，**符号链接是一条逃逸路径**
        #   （`skills/evil -> ~/.ssh` 会把库外的文件读进上下文）。
        #   这里拒绝、并让它出现在 skipped 里（"漏看"和"看见了但拒绝"是两件事）。
        return None, "符号链接目录（不跟随：可能是逃逸路径）"
    if path.is_symlink():
        # ★ 同一处置，更高的精度：**真目录内的软链 `SKILL.md`**
        #   （`skills/ok/SKILL.md -> <库外文件>`）。目录本身是真的、能过上面那关，
        #   但 `is_file()` 会跟随软链 —— 于是库外文件的 frontmatter **name/description
        #   就进了提示词索引**（正文虽被 load_skill_body 的纵深防御挡下，描述层却已泄漏）。
        #   既然软链是逃逸路径，就得在**发现阶段**一并拒掉，而不是只挡正文。
        return None, "符号链接 SKILL.md（不跟随：可能是逃逸路径）"
    if not path.is_file():
        return None, "没有 SKILL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"SKILL.md 读不出来：{type(exc).__name__}"
    front = parse_frontmatter(text)
    if front is None:
        return None, "frontmatter 缺失或 `---` 围栏未闭合"
    name = (front.get("name") or "").strip()
    description = (front.get("description") or "").strip()
    if not name:
        return None, "缺 name"
    if not description:
        return None, "缺 description"
    if not is_valid_skill_name(name):
        return None, f"name 不合法：{name!r}"
    return Skill(name=name, description=description, path=path, dir=skill_dir), ""


def discover_skills(root: Optional[Path] = None) -> SkillScan:
    """扫一遍技能库 → `SkillScan`（能用的已按名字排序 + 跳过清单）。

    ★ 每次调用都**真读盘**（没有进程内缓存）：索引是"按需构建"的
      （提示词重建时 / `skill_view` 调用时），而技能是本地文件，读起来便宜。
      真实 Hermes 有磁盘快照 + 进程内 LRU（`_SKILLS_PROMPT_CACHE`，32 条），
      那是为了 ~100 个技能 × 每轮重建；mini 现在不需要 —— 等真慢了再补
      （代价说清：同一批技能会被重复解析，数量级是几个文件）。
    ★ 扫描面 = `skill_roots()`（**用户根 → 内置根**），所以内置技能与用户技能**合并**进
      同一个 `SkillScan`（索引里也是同一块）。
    ★ 同名技能：**首见胜出**（对应真实侧），所以按根序 **用户同名覆盖内置**；
      落败的那份进跳过清单，理由带上它被哪个根的哪份覆盖
      （`<根名>/<目录名>`，例：``名字与 用户/alpha 重复（首见胜出）``）——
      否则 `skill_view(name)` 就有了两个答案，也没法诊断"到底谁覆盖了谁"。
    """
    skills: List[Skill] = []
    skipped: List[SkippedSkill] = []
    seen: Dict[str, str] = {}
    for directory in iter_skill_dirs(root):
        skill, reason = read_skill(directory)
        if skill is None:
            skipped.append(SkippedSkill(dir_name=directory.name, reason=reason))
            continue
        if skill.name in seen:
            skipped.append(
                SkippedSkill(dir_name=directory.name,
                             reason=f"名字与 {seen[skill.name]} 重复（首见胜出）")
            )
            continue
        # ★ 记的是 `<根名>/<目录名>` 而不是光目录名：跨根重名时，落败者要能看出
        #    "被哪个根的哪份覆盖了"（两个根的 basename 都是 `skills`，光看目录名分不开）。
        seen[skill.name] = f"{_root_label(directory.parent)}/{directory.name}"
        skills.append(skill)
    skills.sort(key=lambda s: s.name)          # ← 索引字节稳定的前提
    skipped.sort(key=lambda s: s.dir_name)     # ← 跳过清单也确定序（方便测试/诊断）
    return SkillScan(skills=skills, skipped=skipped)


def resolve_skill(name: str, root: Optional[Path] = None) -> Optional[Skill]:
    """按**名字**在已发现的技能里找（找不到 / 名字非法 → `None`）。

    ★ 为什么是"查索引"而不是"拼路径"（`skills_dir()/name/SKILL.md`）：
      拼路径等于把模型/用户给的字串直接喂给文件系统，安全边界就只剩
      "名字里不许有 `/`、`..`"这一层纸。查表则**结构上不可能越界**：
      名字只用来和已发现的技能名做**相等比较**，路径永远来自扫描结果。
      非法名字连扫都不用扫（`is_valid_skill_name` 先拦一道）。
    """
    if not is_valid_skill_name(name):
        return None
    wanted = name.strip()
    for skill in discover_skills(root).skills:
        if skill.name == wanted:
            return skill
    return None


def load_skill_body(skill: Skill, root: Optional[Path] = None) -> Optional[str]:
    """读技能**正文**（剥掉 frontmatter）。读不到 → `None`。

    ★ 纵深防御（两道都过才返回）：
      ① 路径必须落在**某个技能库根内** —— 挡"库外文件"（比如有人在技能目录里塞符号链接）；
         没有显式 `root=` 时对 `skill_roots()` **逐个根**判定 —— 内置技能住在内置根里，
         只看用户根会把内置技能的正文全挡下（内含技能也就成了"进索引但读不出"）。
      ② `Skill` 对象**只能**由 `read_skill()` 造出来 —— 挡"这不是技能"。
      因此"读出技能目录以外的文件"这条路在实现上是关着的，不是靠调用方自觉。
    """
    if root is not None:
        bases = [Path(root).resolve()]
    else:
        bases = [r.resolve() for r in skill_roots()]
    try:
        path = skill.path.resolve()
    except OSError:
        logger.warning("技能路径解析失败，拒绝读取：%s", skill.path)
        return None
    if not any(_within(path, base) for base in bases):
        logger.warning("技能路径逃出技能库，拒绝读取：%s", skill.path)
        return None
    try:
        text = skill.path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return split_frontmatter(text)[1]


# ── 索引（渐进披露的第一跳）──────────────────────────────────────────────
def truncate_description(description: str) -> str:
    """把描述截到 `SKILL_PROMPT_DESC_LIMIT` 字符。

    ★ 算法照真实实现（`agent/skill_utils.py:750-754`）：
      ``desc[:LIMIT - 3] + "..."`` —— 注意是**含省略号一共 60 字符**，
      不是"60 字符再加省略号"。
    """
    if len(description) <= SKILL_PROMPT_DESC_LIMIT:
        return description
    return description[: SKILL_PROMPT_DESC_LIMIT - 3] + "..."


def build_skill_index(root: Optional[Path] = None) -> str:
    """把技能库渲染成**提示词里的索引块**（渐进披露的第一跳）。

    形状（每行只有**名字 + 截断后的描述**）::

        ## 可用技能（技能索引）

        这里只有名字与一句话描述；技能正文不在提示词里 —— 需要时用 skill_view(name) 读取。
        - alpha: 做甲事。
        - beta: 做乙事。

    ★ **正文绝不进这里**：这是本章的核心承诺 —— 索引只负责让模型"知道存在什么"，
      正文按需经 `skill_view` 读取。所以这个函数只碰 frontmatter，
      连正文都不加载（`Skill` 里根本没有正文字段，见它的注释）。
    ★ **没有技能 → 返回空串**（不加标题、不留空行）：块由 `_join_tier` 拼进
      volatile 层，空串 = 这个块不存在。于是"无技能"与"有技能"两种提示词
      各自都是**字节确定**的（"空块留一个换行"是这一章最容易被忽略的字节差异）。
    """
    skills = discover_skills(root).skills
    if not skills:
        return ""
    lines = [
        SKILL_INDEX_HEADING,
        "",
        "这里只有名字与一句话描述；技能正文不在提示词里 —— 需要时用 skill_view(name) 读取。",
    ]
    for skill in skills:
        lines.append(f"- {skill.name}: {truncate_description(skill.description)}")
    return "\n".join(lines)


def has_skills(root: Optional[Path] = None) -> bool:
    """技能库里**至少有一个可用技能**吗？

    `skill_view` 的 `check_fn` 用它（`tools/skills.py`）——
    工具可见性与提示词索引必须**同开同关**，所以两边都问这一个函数。
    """
    return bool(discover_skills(root).skills)
