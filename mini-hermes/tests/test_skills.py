"""第 13 章的自测：技能目录与渐进披露（A1~A4）—— **不联网、零第三方依赖**。

跑法：python3 tests/test_skills.py

## 测的是什么（7 组）

| 组 | 守的承诺 |
|---|---|
| 1. 索引字节稳定 ★ | 同一批技能两次构建**字节相同** · 无关操作（写无关文件 / 落一条消息 / invalidate 重建）后仍相同 · **只改 mtime 不变** · 索引在 volatile 层**首块** |
| 2. 正文只经 `skill_view` 出现 ★ | 提示词里**有**名字+描述、**没有**正文独有句 · 描述硬截 60 · 不分页 |
| 3. 发现容错 | 缺 name / 缺 description / frontmatter 坏 / 没有 SKILL.md / 名字非法 / 多行块标记 → **跳过并计数**，不抛异常、不进索引 |
| 4. 排序确定 | 乱序创建三个技能 → 索引按**名字**排序；两次构建字节一致 |
| 5. `skill_view` 安全 | `..` / 绝对路径 / 斜杠 / 空白 / 不存在 → **可读 JSON 错误**；读不到技能库以外的任何文件；符号链接**目录**与**软链 SKILL.md** 都被跳过 |
| 6. 工具闸门（注册 → toolset → 可见） | 每个注册工具都有 toolset · `skill_view` 在 `skills` 组与默认捆里 · 发现机制认得 `tools/skills.py` · **有技能才可见**（与索引同开同关） |
| 7. 内置技能与用户共存 ★ | **两个根都发现** · **用户同名覆盖内置**（内置那份进 skipped，理由说明被谁覆盖）· 跨根顺序确定 · 内置根不存在时安静 · `skill_view` 能读**内置**技能 |

★ 组 1/2 是这一章最核心的两条**不变量**（字节稳定 + 正文不进提示词）——
改 `agent/system_prompt.py` / `agent/skill_utils.py` 时最容易破坏的就是它们。

## 组织方式

测试函数在文件里的**位置**与它们所属的**组**是解耦的：分组写在 `GROUPS` 表里。
配一条**未注册自检**：定义了 `test_` 函数却没登记进 `GROUPS` → 直接退出（2）。
（"写了但从来没跑"是测试里最经典的静默失败 —— 测试数量看着在涨，覆盖的却是老东西。）

## 隔离（**两个根都要隔离**）

`MINI_HOME` 在 **import 之前**指到临时目录（见 `_TEST_HOME`）—— 这隔离了**用户级**技能根，
否则测试会去读用户真实的 `~/.mini-hermes/skills/`，结果就依赖
"跑测试的这个人家里放了什么技能"。

★ 第 13b 起技能有**两个**根，所以还必须隔离**内置级**根（`<repo>/mini-hermes/skills/`）：
仓里随程序发布的内置技能会让 `has_skills()` 为真、混进这些用例的扫描结果。
做法是在 import 之后把 `skill_utils.bundled_skills_dir` **patch 到另一个临时目录**
（monkeypatch 风格，**生产代码里不加任何测试钩子**），并在每条用例开跑前复位。
（实测：只隔离 `MINI_HOME` 时，仓里有内置技能 ⇒ 本文件红 10 条 + `test_tools_runtime.py` 红 1 条。）

每条用例自己造技能目录、开跑前先清空**两个根**（`reset_roots()`），所以用例之间顺序无关。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★★ MINI_HOME 必须在 import 任何 mini 模块**之前**设好 —— 这样"技能库跟随
#    MINI_HOME"这件事从第一个 import 起就是真的，而不是靠某一处函数兜着。
_TEST_HOME = tempfile.mkdtemp(prefix="mini_ch13_skills_")
os.environ["MINI_HOME"] = _TEST_HOME

import hermes_state  # noqa: E402
import model_tools  # noqa: E402
import toolsets  # noqa: E402
from agent import skill_utils  # noqa: E402
from agent import system_prompt as sp  # noqa: E402
from tools import registry  # noqa: E402

# ★ 第二个根：**内置级**技能库。仓里随程序发布的内置技能（`mini-hermes/skills/`）
#   必须在这里被隔离掉，否则它会混进本文件每条用例的扫描结果
#   （实测：只隔离 MINI_HOME 时红 10 条）。做法 = 把它 patch 到一个临时目录。
_TEST_BUNDLED = tempfile.mkdtemp(prefix="mini_ch13_bundled_")


def isolate_bundled_root() -> None:
    """把**内置级**技能根指到临时目录（monkeypatch 风格）。

    ★ 生产代码没有、也不该有测试钩子 ⇒ 从**测试侧**替换模块属性即可
      （`skill_utils` 内部按模块全局名 `bundled_skills_dir` 调用，替换后立即生效）。
    """
    skill_utils.bundled_skills_dir = lambda: Path(_TEST_BUNDLED)


isolate_bundled_root()      # import 之后立刻生效，之后由 reset_roots() 复位

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {detail}" if detail and not cond else ""))


HOME = Path(_TEST_HOME)


# ── 夹具 ────────────────────────────────────────────────────────────────
def make_agent(**kw):
    """一个不联网的假 agent（和 tests/test_prompt_assembly.py 用的是同一种）。"""
    from run_agent import AIAgent

    return AIAgent(
        {
            "provider": "fake",
            "api_mode": "chat_completions",
            "base_url": "http://fake",
            "api_key": "fake",
            "model": "fake-model",
        },
        **kw,
    )


def reset_roots() -> None:
    """清空**两个**技能根 + 确保内置根 patch 在位（夹具复位；彼此不串）。"""
    isolate_bundled_root()
    shutil.rmtree(skill_utils.skills_dir(), ignore_errors=True)
    shutil.rmtree(_TEST_BUNDLED, ignore_errors=True)


def reset_skills_dir() -> None:
    """清空技能库。★ 第 13b 起"技能库"是**两个根** —— 保留这个老名字当薄壳，
    老用例不用改，语义却已经是"把两个根都清干净"。"""
    reset_roots()


def write_skill(dir_name: str, *, name: str = "", description: str = "",
                body: str = "正文。", raw: str | None = None,
                root: Path | None = None) -> Path:
    """造一个技能目录（默认在**用户根**，`root=` 可指定内置根）。

    `raw` 给了就**逐字**写（用来造坏 frontmatter）；否则按 name/description 拼。
    故意支持 name 与 dir_name 不同（排序那组要证明排序键是**名字**）。
    """
    base = Path(root) if root is not None else skill_utils.skills_dir()
    d = base / dir_name
    d.mkdir(parents=True, exist_ok=True)
    if raw is None:
        front = []
        if name:
            front.append(f"name: {name}")
        if description:
            front.append(f"description: {description}")
        raw = "---\n" + "\n".join(front) + "\n---\n" + body
    (d / skill_utils.SKILL_FILENAME).write_text(raw, encoding="utf-8")
    return d


def write_bundled_skill(dir_name: str, **kw) -> Path:
    """在**内置根**里造一个技能（其余参数同 `write_skill`）。"""
    return write_skill(dir_name, root=skill_utils.bundled_skills_dir(), **kw)


def index_lines() -> list:
    """索引里的技能行（`- 名字: 描述`），按出现顺序。"""
    return [l for l in skill_utils.build_skill_index().splitlines() if l.startswith("- ")]


def line_names() -> list:
    return [l.split(": ", 1)[0][2:] for l in index_lines()]


def defnames() -> list:
    """**模型实际能看到**的工具名（每次都重新构建，不缓存）。"""
    return [d["function"]["name"] for d in model_tools.get_tool_definitions()]


def discover() -> None:
    """工具注册是 import 时的副作用 —— 显式触发一次发现。"""
    model_tools.discover_builtin_tools()


def skill_view(name) -> dict:
    """真调一次 `skill_view`（走完整分发路径），返回解析后的 JSON。"""
    raw = model_tools.handle_function_call("skill_view", {"name": name})
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"_raw": raw}


# ═══════════════════════════════ 1. 索引字节稳定 ★
def test_index_bytes_are_stable() -> None:
    reset_skills_dir()
    write_skill("alpha", name="alpha", description="做甲事")
    write_skill("beta", name="beta", description="做乙事")

    first = skill_utils.build_skill_index()
    second = skill_utils.build_skill_index()
    check("同一批技能连续两次构建索引 → **字节完全相同**",
          first == second and first != "", repr(first[:40]))

    # 无关操作①：往 home 里写一个与技能无关的文件
    (HOME / "unrelated.txt").write_text("和技能无关", encoding="utf-8")
    check("写一个与技能无关的文件之后，索引字节不变",
          skill_utils.build_skill_index() == first)

    # ★ 无关操作②：往**会话库**里落一条消息（真写盘，真跑一遍 DB 路径）
    db = hermes_state.SessionDB(HOME / "state.db")
    db.create_session("s1", "cli", model="m")
    db.append_message("s1", "user", "一条与技能无关的消息")
    db.close()
    check("往会话库里加一条消息之后，索引也不变（索引不依赖会话状态）",
          skill_utils.build_skill_index() == first)

    # ★ 索引是 volatile 层的**首块**
    ag = make_agent()
    vol = sp.build_system_prompt_parts(ag)["volatile"]
    check("索引在 volatile 层**首块**（第一行就是索引标题）",
          vol.startswith(skill_utils.SKILL_INDEX_HEADING), vol.splitlines()[0][:40])

    # 无关操作③：换一次 invalidate 再重建
    sp.invalidate_system_prompt(ag)
    vol2 = sp.build_system_prompt_parts(ag)["volatile"]
    check("换一次 invalidate 重建 → 索引块字节不变", vol == vol2)

    # ★ 只动 mtime，一个字节都不改
    for p in skill_utils.skills_dir().glob(f"*/{skill_utils.SKILL_FILENAME}"):
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + 1000))
    check("技能文件 mtime 变了、内容没变 → 索引不变（索引里不含时间戳/路径等易变字段）",
          skill_utils.build_skill_index() == first)


def test_empty_skills_dir_produces_no_block() -> None:
    """★ 没有技能 → **不输出空块**（不加标题、不留空行）。

    "空块"看起来无害，但它会往 volatile 层插一个 `\\n\\n`（或一行标题）——
    那也是**字节差异**，也是"无技能"与"有技能"两种状态的不确定来源。
    """
    reset_skills_dir()
    check("没有技能库 → 索引是空串（不是空块）", skill_utils.build_skill_index() == "")
    ag = make_agent()
    prompt = sp.build_system_prompt(ag)
    vol = sp.build_system_prompt_parts(ag)["volatile"]
    check("提示词里没有这个块、也没有它的标题", skill_utils.SKILL_INDEX_HEADING not in prompt)
    check("…volatile 层里也没有 `- 名字: 描述` 这种残留行（空块连换行都不留）",
          not any(l.startswith("- ") for l in vol.splitlines()), vol[:60])

    # 加一个技能 → 块出现；删掉 → 又消失（两种状态都是确定的）
    write_skill("alpha", name="alpha", description="做甲事")
    check("加一个技能后块出现，且第一行就是标题",
          skill_utils.build_skill_index().startswith(skill_utils.SKILL_INDEX_HEADING))


# ═══════════════════════════════ 2. 正文只经 skill_view 出现 ★
MARKER = "标记_只该出现在skill_view的正文里"


def test_body_only_enters_through_skill_view() -> None:
    reset_skills_dir()
    discover()
    write_skill("alpha", name="alpha", description="一句话说明", body=f"第一步。\n{MARKER}\n")

    prompt = sp.build_system_prompt(make_agent())
    index = skill_utils.build_skill_index()
    check("提示词里**有**技能名", "alpha" in prompt)
    check("提示词里**有**它的一句话描述", "一句话说明" in prompt)
    check("索引行是 `- 名字: 描述` 的形状", index_lines() == ["- alpha: 一句话说明"], str(index_lines()))
    check("★ 提示词里**没有**正文里的独有句（正文只经 skill_view 出现）", MARKER not in prompt)
    check("★ 索引里也没有正文", MARKER not in index)

    got = skill_view("alpha")
    check("★ 同一句话只有 `skill_view` 能拿出来", MARKER in got.get("content", ""), str(got)[:120])
    check("…返回里剥掉了 frontmatter（正文从 `第一步。` 起）",
          got.get("content", "").startswith("第一步。"), got.get("content", "")[:20])
    check("…并带上技能目录的条目列表（渐进披露的第二跳入口）",
          any(e.get("name") == skill_utils.SKILL_FILENAME for e in got.get("files", [])),
          str(got.get("files")))

    entry = registry.get("skill_view")
    props = entry.schema["function"]["parameters"]["properties"] if entry else {}
    check("工具参数只有 `name`（instructional 工具**不分页**：没有 offset/limit）",
          set(props) == {"name"}, str(sorted(props)))


def test_description_is_truncated_to_the_limit() -> None:
    reset_skills_dir()
    write_skill("long", name="long", description="很长的描述" * 20)   # 100 字符
    write_skill("short", name="short", description="短")
    value = index_lines()[0].split(": ", 1)[1]
    check("索引里的描述被**硬截**到 60 字符（含 `...`）",
          len(value) == skill_utils.SKILL_PROMPT_DESC_LIMIT, str(len(value)))
    check("…以 `...` 收尾", value.endswith("..."), value[-6:])
    check("常量出处注明（60 = 真实 Hermes `SKILL_PROMPT_DESC_LIMIT`）",
          skill_utils.SKILL_PROMPT_DESC_LIMIT == 60)
    check("没超长的描述原样保留（不画蛇添足加省略号）",
          index_lines()[1].split(": ", 1)[1] == "短")


# ═══════════════════════════════ 3. 发现容错
def test_discovery_is_tolerant_and_counts_skips() -> None:
    reset_skills_dir()
    write_skill("good", name="good", description="合规的")
    write_skill("no_name", description="只有描述没有名字")
    write_skill("no_desc", name="no_desc")
    write_skill("bad_fm", raw="---\nname: bad_fm\ndescription: 围栏没闭合\n")
    (skill_utils.skills_dir() / "no_file").mkdir(parents=True)          # 没有 SKILL.md
    write_skill("bad_name", name="../evil", description="名字里带路径")

    scan = skill_utils.discover_skills()          # ← 出了异常这条用例就红了
    check("只有合规的那个进了索引", [s.name for s in scan.skills] == ["good"],
          str([s.name for s in scan.skills]))
    check("★ 五种坏技能全部被**跳过并计数**", len(scan.skipped) == 5,
          str([(s.dir_name, s.reason) for s in scan.skipped]))
    reasons = {s.dir_name: s.reason for s in scan.skipped}
    check("跳过原因是**可读的**（缺 name / 缺 description / frontmatter 坏 / 没有文件 / 名字非法）",
          reasons.get("no_name") == "缺 name"
          and reasons.get("no_desc") == "缺 description"
          and "frontmatter" in reasons.get("bad_fm", "")
          and reasons.get("no_file") == "没有 SKILL.md"
          and "不合法" in reasons.get("bad_name", ""),
          str(reasons))
    check("坏技能**一个都没进提示词**",
          all(bad not in skill_utils.build_skill_index()
              for bad in ("no_name", "no_desc", "bad_fm", "no_file", "evil")))
    check("跳过清单也**确定序**（按目录名；诊断输出不会每次不一样）",
          [s.dir_name for s in scan.skipped] == sorted(s.dir_name for s in scan.skipped))


def test_frontmatter_parser_is_a_narrow_subset() -> None:
    """解析器是**子集**：支持的照做，不支持的安静忽略（不是崩溃）。"""
    fm, body = skill_utils.split_frontmatter(
        '---\nname: "alpha"\ndescription: 带引号\n---\n第一行\n第二行\n')
    check("成对引号去掉", fm == {"name": "alpha", "description": "带引号"}, str(fm))
    check("正文 = 围栏之后的部分（多行原样保留）", body == "第一行\n第二行", repr(body))

    fm2, _ = skill_utils.split_frontmatter("---\nname: x\nmetadata:\n  hermes:\n    tags: [a]\n---\n正文\n")
    check("嵌套/列表（metadata.hermes.*）**被忽略**而不是让解析失败（子集口径）",
          fm2 == {"name": "x"}, str(fm2))

    check("没有 frontmatter → None（当坏技能处理）",
          skill_utils.split_frontmatter("正文直接开始\n")[0] is None)
    check("围栏未闭合 → None", skill_utils.split_frontmatter("---\nname: x\n")[0] is None)


def test_multiline_block_marker_is_dropped() -> None:
    """★ frontmatter 多行块标记（`|` / `>`）⇒ 该键**整个视为不支持、丢弃**，
    不把 `>` / `|` 当字面值（否则它会被当成描述进索引，真正的多行文本却丢了）。

    这是解析器的**承诺与实现对齐**：docstring 的表说"多行块一律忽略"，
    那就必须连标记一起丢 —— 只丢块内容（缩进行）会留下一个垃圾标量。
    """
    fm, _ = skill_utils.split_frontmatter(
        "---\nname: x\ndescription: >\n  真正描述\n---\n正文\n")
    check("`description: >` ⇒ 该键被**丢弃**（不是留一个字面 `>`）", fm == {"name": "x"}, str(fm))
    fm2, _ = skill_utils.split_frontmatter(
        "---\nname: y\ndescription: |\n  行一\n  行二\n---\n正文\n")
    check("`description: |` 同理被丢弃", fm2 == {"name": "y"}, str(fm2))
    fm3, _ = skill_utils.split_frontmatter(
        "---\nname: keep\ndescription: 正常描述\ntags: >\n  a\n  b\n---\n正文\n")
    check("只丢多行块那一键：`name` 与普通标量字段**不受影响**",
          fm3 == {"name": "keep", "description": "正常描述"}, str(fm3))

    # 值以 `>` 开头但**不是**纯标记（真文本）要照留，别误伤
    fm4, _ = skill_utils.split_frontmatter(
        "---\nname: t\ndescription: > 这是正常值\n---\n正文\n")
    check("`description: > 文本`（`>` 后还有字）仍是正常值，不误伤",
          fm4 == {"name": "t", "description": "> 这是正常值"}, str(fm4))

    # ★ 发现层：多行块描述的技能应因**缺 description 被跳过并计数**，而不是以 `>` 进索引
    reset_skills_dir()
    write_skill("good", name="good", description="正常的")
    write_skill("block_desc", raw="---\nname: block_desc\ndescription: >\n  本来想写的描述\n---\n正文\n")
    scan = skill_utils.discover_skills()
    check("多行块描述的技能 ⇒ 只有合规的进索引（它因缺 description 被跳过）",
          [s.name for s in scan.skills] == ["good"], str([s.name for s in scan.skills]))
    check("…并且被**计数**（skipped 里查得到，原因是缺 description）",
          any(s.dir_name == "block_desc" and s.reason == "缺 description" for s in scan.skipped),
          str([(s.dir_name, s.reason) for s in scan.skipped]))
    idx = skill_utils.build_skill_index()
    check("…它的名字与字面 `>` **都不进索引**",
          "block_desc" not in idx and ">" not in idx, repr(idx))


# ═══════════════════════════════ 4. 排序确定
def test_index_order_is_by_name() -> None:
    reset_skills_dir()
    # 目录名与技能名**故意不同**，而且创建顺序乱：d1→zzz, d2→aaa, d3→mmm
    for dir_name, name in (("d1", "zzz"), ("d2", "aaa"), ("d3", "mmm")):
        write_skill(dir_name, name=name, description=f"{name} 的描述")

    first = skill_utils.build_skill_index()
    check("索引按**技能名**升序（不是创建顺序、不是目录名顺序）",
          line_names() == ["aaa", "mmm", "zzz"], str(line_names()))
    check("两次构建字节一致（顺序是确定的）", first == skill_utils.build_skill_index())
    check("…再来一次仍然一致（没有\"每次扫描顺序不同\"这种抖动）",
          first == skill_utils.build_skill_index())


# ═══════════════════════════════ 5. skill_view 安全
def test_skill_view_refuses_to_escape_the_skills_dir() -> None:
    reset_skills_dir()
    discover()
    secret = HOME / "secret.md"
    secret.write_text("标记_技能库外的秘密", encoding="utf-8")
    outside = HOME / "outside" / "sneaky"
    outside.mkdir(parents=True)
    (outside / skill_utils.SKILL_FILENAME).write_text(
        f"---\nname: sneaky\ndescription: 库外技能\n---\n标记_库外技能正文\n", encoding="utf-8")
    write_skill("ok", name="ok", description="正常的技能", body="正常正文。")

    link = skill_utils.skills_dir() / "link"
    linked = True
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        linked = False                    # 某些文件系统不给建符号链接

    # ★ 同一逃逸路径的**更隐蔽**一种：真目录里塞一个软链 `SKILL.md`
    #   （目录是真的 → 过得了"软链目录"那一关；但 is_file() 跟随软链 ⇒
    #    库外文件的 name/description 会进提示词索引）。
    leaked_file = HOME / "outside_skill.md"
    leaked_file.write_text(
        "---\nname: leaked_file\ndescription: 库外描述泄漏_文件软链\n---\n标记_库外技能正文\n",
        encoding="utf-8")
    linked_dir = skill_utils.skills_dir() / "linked_file"
    linked_dir.mkdir(parents=True)
    linked_skill_md = True
    try:
        (linked_dir / skill_utils.SKILL_FILENAME).symlink_to(leaked_file)
    except OSError:
        linked_skill_md = False

    attempts = ["../secret", "../secret.md", "../../etc/passwd", "/etc/passwd",
                "..", "a/b", "sneaky", "link", "不存在的技能", "", "  "]
    for bad in attempts:
        payload = skill_view(bad)
        check(f"{bad!r} → 可读的 JSON 错误（不抛异常）",
              "error" in payload and bool(payload.get("error")), str(payload)[:90])
        check(f"…错误里没有库外内容 {bad!r}",
              "标记_技能库外的秘密" not in json.dumps(payload, ensure_ascii=False)
              and "标记_库外技能正文" not in json.dumps(payload, ensure_ascii=False))

    if linked:
        check("★ 符号链接形式的技能目录在**发现阶段**就被跳过（不跟随逃逸路径）",
              "link" not in [s.name for s in skill_utils.discover_skills().skills]
              and "link" in [s.dir_name for s in skill_utils.discover_skills().skipped])
    else:
        check("（这台机器不给建符号链接，跳过这一条）", True)

    if linked_skill_md:
        # ★ 与软链目录**同一处置**：软链 SKILL.md 也在发现阶段被跳过并计数
        scan = skill_utils.discover_skills()
        check("★ 真目录里的**软链 SKILL.md** 在发现阶段被跳过（与软链目录同一处置）",
              "leaked_file" not in [s.name for s in scan.skills]
              and "linked_file" in [s.dir_name for s in scan.skipped],
              str([(s.dir_name, s.reason) for s in scan.skipped]))
        idx = skill_utils.build_skill_index()
        check("…其 name 与 description **都不在索引里**（描述层也不泄漏）",
              "leaked_file" not in idx and "库外描述泄漏_文件软链" not in idx, repr(idx))
        prompt = sp.build_system_prompt(make_agent())
        check("…也**不在系统提示词里**（名字与描述都没有）",
              "leaked_file" not in prompt and "库外描述泄漏_文件软链" not in prompt)
    else:
        check("（这台机器不给建符号链接，跳过软链 SKILL.md 这一条）", True)

    # 纵深防御：就算有人绕过发现，手工造一个指向库外的 Skill 也一样读不出来
    forged = skill_utils.Skill(name="sneaky", description="库外",
                               path=outside / skill_utils.SKILL_FILENAME, dir=outside)
    check("★ 纵深防御：库外的 SKILL.md 直接读不出来（返回 None，不是内容）",
          skill_utils.load_skill_body(forged) is None)

    good = skill_view("ok")
    check("正常的技能照读（安全边界没有误伤）", good.get("content", "").strip() == "正常正文。")
    check("成功取回时返回里带着技能**名字**（模型可核对拿到了哪个）", good.get("name") == "ok")
    bad_payload = skill_view("../secret")
    check("★ 名字非法时错误里给出**可用的技能名**（模型能自己改）",
          "available" in bad_payload, str(bad_payload)[:90])


# ═══════════════════════════════ 6. 工具闸门
def test_skill_view_is_registered_and_exposed() -> None:
    discover()
    check("`skill_view` 注册了", registry.get("skill_view") is not None)
    check("发现机制认出了 `tools/skills.py`（AST 自动发现，没改任何名单）",
          "tools.skills" in sys.modules)
    check("它归属 `skills` toolset", toolsets.toolset_of_tool("skill_view") == "skills",
          str(toolsets.toolset_of_tool("skill_view")))
    check("…而且在**默认捆**里（理由：提示词无条件宣传它，两个面必须同开同关）",
          "skill_view" in toolsets.MINI_CORE_TOOLS)
    check("`skills` toolset 有给人看的说明（/tools 与 --list-toolsets 会显示它）",
          bool(toolsets.TOOLSETS["skills"]["description"]))

    orphans = toolsets.orphan_tools(registry.tool_names())
    check("契约：每个注册的工具都归属某个 toolset（新工具也不例外）",
          not orphans, f"孤儿：{orphans}")


def test_skill_view_visibility_tracks_the_index() -> None:
    """★ 工具可见性与提示词索引**同开同关**（同一个事实源）。

    两个面不同步的后果是第 4 章那个 bug 的形状：
    提示词让模型去用 `skill_view`，而 tools 字段里没这条 schema
    （模型会去调、拿回"未知工具"，白烧一轮）。
    """
    reset_skills_dir()
    discover()
    check("空技能库 → 索引为空 ⇒ 工具**不可见**（模型连它存在都不知道）",
          skill_utils.build_skill_index() == "" and "skill_view" not in defnames(),
          str(defnames()))

    write_skill("alpha", name="alpha", description="有技能了")
    check("有技能 → 索引非空 ⇒ 工具**可见**（立刻生效，不等 30 秒 TTL）",
          skill_utils.build_skill_index() != "" and "skill_view" in defnames(),
          str(defnames()))

    reset_skills_dir()
    check("清空技能库 → 又不可见（两个面一起翻面）",
          "skill_view" not in defnames(), str(defnames()))


# ═══════════════════════════════ 7. 内置技能与用户共存（用户优先）★
def test_both_roots_are_discovered() -> None:
    """★ 两个根**都**参与发现：内置技能与用户技能合并进**同一个**索引块。"""
    reset_roots()
    write_skill("user-only", name="user-only", description="用户的技能")
    write_bundled_skill("bundled-only", name="bundled-only", description="内置的技能")

    names = [s.name for s in skill_utils.discover_skills().skills]
    check("用户根与内置根**都**被扫描（两个技能都进库）",
          names == ["bundled-only", "user-only"], str(names))
    check("skill_roots() = [用户根, 内置根]（用户在前 ⇒ 优先级高）",
          [Path(p) for p in skill_utils.skill_roots()]
          == [skill_utils.skills_dir(), Path(_TEST_BUNDLED)],
          str([str(p) for p in skill_utils.skill_roots()]))
    idx = skill_utils.build_skill_index()
    check("索引是**一个合并块**：内置与用户的技能行都在里面",
          "- bundled-only: 内置的技能" in idx and "- user-only: 用户的技能" in idx, repr(idx))


def test_user_skill_overrides_bundled() -> None:
    """★ 用户同名技能**覆盖**内置技能（首见胜出 = PATH 语义）。"""
    reset_roots()
    discover()
    write_bundled_skill("alpha", name="alpha", description="内置版", body="内置正文")
    write_skill("alpha", name="alpha", description="用户版", body="用户正文")

    scan = skill_utils.discover_skills()
    check("同名时**用户版胜出**（索引里是用户的描述）",
          [s.name for s in scan.skills] == ["alpha"] and scan.skills[0].description == "用户版",
          str([(s.name, s.description) for s in scan.skills]))
    reasons = {s.dir_name: s.reason for s in scan.skipped}
    check("★ 内置那份进了 skipped，理由是**被哪个根的哪份**覆盖（可诊断）",
          "alpha" in reasons and "用户/alpha" in reasons["alpha"], str(reasons))
    check("索引里只有一行 alpha（用户版）", index_lines() == ["- alpha: 用户版"], str(index_lines()))
    got = skill_view("alpha")
    check("读取路径也一致：skill_view('alpha') 拿到的是**用户版正文**",
          got.get("content", "").strip() == "用户正文", str(got)[:140])


def test_cross_root_order_is_deterministic() -> None:
    """★ 跨根顺序确定：两次发现**完全一致**（索引字节稳定的前提）。"""
    reset_roots()
    for n in ("zeta", "alpha", "mu"):
        write_skill(n, name=n, description=f"用户 {n}")
    for n in ("beta", "gamma"):
        write_bundled_skill(n, name=n, description=f"内置 {n}")

    first_names = [s.name for s in skill_utils.discover_skills().skills]
    second_names = [s.name for s in skill_utils.discover_skills().skills]
    check("两次发现的技能序列**完全一致**（跨根顺序确定，无抖动）",
          first_names == second_names, f"{first_names} vs {second_names}")
    first = skill_utils.build_skill_index()
    check("两次构建索引字节一致", first == skill_utils.build_skill_index() and first != "")
    check("跨根合并后按**技能名**全局升序",
          line_names() == ["alpha", "beta", "gamma", "mu", "zeta"], str(line_names()))
    check("顺序与**根**无关（不是\"用户全在前、内置全在后\"：内置的 beta 排进用户姓名之间）",
          line_names().index("beta") < line_names().index("mu"), str(line_names()))


def test_same_name_in_one_root_is_deterministic() -> None:
    """★ **同一根内**两个目录取同名时，胜出者由**扫描顺序**唯一决定（不是创建顺序）。

    `_iter_one_root` 按目录名排序 ⇒ 目录名小的先被看见 ⇒ 它胜出（首见胜出）。
    这条钉住的是"首见胜出的**确定性**"：索引最后还会按技能名再排一次，所以索引本身看不出
    差别 —— 但 `skipped` 的理由、以及 `skill_view("dup")` 拿到谁的正文，都会跟着扫描顺序漂移。
    （对照：去掉根内 `sorted()`，索引仍字节稳定，只有这条会红。）
    """
    reset_roots()
    for d in ("yankee", "arizona"):        # 故意先建 yankee、后建 arizona：创建顺序 ≠ 名字顺序
        write_skill(d, name="dup", description=f"{d} 版")

    scan = skill_utils.discover_skills()
    winner = [s.dir.name for s in scan.skills if s.name == "dup"]
    check("同根内同名 → **目录名小的胜出**（看扫描顺序，不看创建顺序）",
          winner == ["arizona"], str(winner))
    check("…落败那份进 skipped，理由里写明被**哪份**覆盖（带上胜出者所在目录名）",
          any(r.dir_name == "yankee" and "arizona" in r.reason for r in scan.skipped),
          str([(r.dir_name, r.reason) for r in scan.skipped]))


def test_missing_bundled_root_is_quiet() -> None:
    """★ 内置根不存在时**安静**跳过（兼容"只有用户级"的旧环境）。"""
    reset_roots()
    write_skill("alpha", name="alpha", description="用户技能")
    shutil.rmtree(skill_utils.bundled_skills_dir(), ignore_errors=True)   # 假装没有内置根

    scan = skill_utils.discover_skills()
    check("内置根不存在 ⇒ 用户技能照常发现、不抛异常",
          [s.name for s in scan.skills] == ["alpha"], str([s.name for s in scan.skills]))
    check("…也不产生任何 skipped（不把\"目录不存在\"当成坏技能）",
          scan.skipped == [], str([(s.dir_name, s.reason) for s in scan.skipped]))
    check("索引照常产出（只有用户技能）", index_lines() == ["- alpha: 用户技能"], str(index_lines()))

    reset_roots()
    check("两个根都不存在 ⇒ 索引是空串（不留空块）", skill_utils.build_skill_index() == "")


def test_skill_view_reads_bundled_skill() -> None:
    """★ `skill_view` 能读出**内置**技能的正文（不是"只进索引、读不出"）。"""
    reset_roots()
    discover()
    marker = "内置正文标记_只读内置根"
    write_bundled_skill("conventions", name="conventions",
                        description="随程序发布的约定", body=f"第一段。\n{marker}\n")

    got = skill_view("conventions")
    check("skill_view 能读内置技能：返回正文（含独有句）",
          "error" not in got and marker in got.get("content", ""), str(got)[:140])
    check("…返回里带着它来自**内置**根（path 落在内置根下）",
          str(skill_utils.bundled_skills_dir()) in got.get("path", ""), got.get("path", ""))
    check("…同时给出目录列表（渐进披露第二跳入口）",
          isinstance(got.get("files"), list) and bool(got["files"]), str(got.get("files"))[:80])
    check("只有内置技能时 skill_view 也**可见**（has_skills 覆盖两个根）",
          skill_utils.has_skills() and "skill_view" in defnames(), str(defnames()))


# ══════════════════════════════════════════════════════════════════════════════ #
GROUPS = [
    ("1. 索引字节稳定 ★",
     "两次构建字节相同 · 无关操作后仍相同 · 只改 mtime 不变 · 无技能时**不输出空块** · 索引在 volatile 首块",
     [test_index_bytes_are_stable,
      test_empty_skills_dir_produces_no_block]),
    ("2. 正文只经 skill_view 出现 ★",
     "提示词有名字+描述、没有正文独有句 · 描述硬截 60 · 不分页 · 返回目录列表",
     [test_body_only_enters_through_skill_view,
      test_description_is_truncated_to_the_limit]),
    ("3. 发现容错",
     "缺 name / 缺 description / frontmatter 坏 / 没有 SKILL.md / 名字非法 / 多行块标记 → 跳过并计数（不抛）",
     [test_discovery_is_tolerant_and_counts_skips,
      test_frontmatter_parser_is_a_narrow_subset,
      test_multiline_block_marker_is_dropped]),
    ("4. 排序确定",
     "乱序创建 → 索引按**名字**排序 · 两次构建一致",
     [test_index_order_is_by_name]),
    ("5. skill_view 安全",
     "`..` / 绝对路径 / 斜杠 / 空白 / 不存在 → 可读 JSON 错误 · 读不到库外文件 · 符号链接目录与**软链 SKILL.md** 都被跳过",
     [test_skill_view_refuses_to_escape_the_skills_dir]),
    ("6. 工具闸门（注册 → toolset → 可见）",
     "每个注册工具都有 toolset · `skill_view` 在 skills 组与默认捆里 · 有技能才可见",
     [test_skill_view_is_registered_and_exposed,
      test_skill_view_visibility_tracks_the_index]),
    ("7. 内置技能与用户共存（用户优先）★",
     "两个根都发现 · **用户同名覆盖内置**（内置进 skipped，理由说明被谁覆盖）· 跨根顺序确定 · "
     "内置根不存在时安静 · `skill_view` 能读**内置**技能 · **同根内同名时胜出者确定**",
     [test_both_roots_are_discovered,
      test_user_skill_overrides_bundled,
      test_cross_root_order_is_deterministic,
      test_same_name_in_one_root_is_deterministic,
      test_missing_bundled_root_is_quiet,
      test_skill_view_reads_bundled_skill]),
]


def main() -> None:
    print(f"解释器：{sys.executable}  Python {sys.version.split()[0]}")
    print(f"MINI_HOME = {_TEST_HOME}\n")

    # ★ 未注册自检：写了 test_ 却没进 GROUPS = "写了但不会跑"（最经典的测试静默失败）
    defined = {n for n in globals() if n.startswith("test_")}
    registered = {f.__name__ for _, _, fns in GROUPS for f in fns}
    missing = defined - registered
    if missing:
        print(f"⚠️ 有测试函数没登记进 GROUPS（写了但不会跑）：{sorted(missing)}")
        raise SystemExit(2)

    for title, promise, fns in GROUPS:
        print(f"══ {title} ══ {promise}")
        for fn in fns:
            # 用例抛异常 = 一条**可诊断的红**，而不是"套件崩了"（后面的组全被吞掉）
            try:
                fn()
            except Exception as exc:                     # noqa: BLE001
                check(f"{fn.__name__} 抛异常", False, f"{type(exc).__name__}: {exc}")
        print(f"   → {len(PASS)} 累计 / {len(FAIL)} 失败\n")

    print(f"{len(PASS)} 通过 / {len(FAIL)} 失败　（{len(GROUPS)} 组）")
    if FAIL:
        print("失败项：" + "; ".join(FAIL))
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
    shutil.rmtree(_TEST_BUNDLED, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
