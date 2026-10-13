"""第 13 章的自测：技能目录与渐进披露（A1~A4）—— **不联网、零第三方依赖**。

跑法：python3 tests/test_skills.py

## 测的是什么（6 组）

| 组 | 守的承诺 |
|---|---|
| 1. 索引字节稳定 ★ | 同一批技能两次构建**字节相同** · 无关操作（写无关文件 / 落一条消息 / invalidate 重建）后仍相同 · **只改 mtime 不变** · 索引在 volatile 层**首块** |
| 2. 正文只经 `skill_view` 出现 ★ | 提示词里**有**名字+描述、**没有**正文独有句 · 描述硬截 60 · 不分页 |
| 3. 发现容错 | 缺 name / 缺 description / frontmatter 坏 / 没有 SKILL.md → **跳过并计数**，不抛异常、不进索引 |
| 4. 排序确定 | 乱序创建三个技能 → 索引按**名字**排序；两次构建字节一致 |
| 5. `skill_view` 安全 | `..` / 绝对路径 / 斜杠 / 空白 / 不存在 → **可读 JSON 错误**；读不到技能库以外的任何文件；符号链接目录被跳过 |
| 6. 工具闸门（注册 → toolset → 可见） | 每个注册工具都有 toolset · `skill_view` 在 `skills` 组与默认捆里 · 发现机制认得 `tools/skills.py` · **有技能才可见**（与索引同开同关） |

★ 组 1/2 是这一章最核心的两条**不变量**（字节稳定 + 正文不进提示词）——
改 `agent/system_prompt.py` / `agent/skill_utils.py` 时最容易破坏的就是它们。

## 组织方式

测试函数在文件里的**位置**与它们所属的**组**是解耦的：分组写在 `GROUPS` 表里。
配一条**未注册自检**：定义了 `test_` 函数却没登记进 `GROUPS` → 直接退出（2）。
（"写了但从来没跑"是测试里最经典的静默失败 —— 测试数量看着在涨，覆盖的却是老东西。）

## 隔离

`MINI_HOME` 在 **import 之前**指到临时目录（见 `_TEST_HOME`）——
否则测试会去读用户真实的 `~/.mini-hermes/skills/`，结果就依赖
"跑测试的这个人家里放了什么技能"。每条用例自己造技能目录、开跑前先清空
（`reset_skills_dir()`），所以用例之间的顺序无关。
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


def reset_skills_dir() -> None:
    """清空技能库（每条用例自己造，彼此不串）。"""
    shutil.rmtree(skill_utils.skills_dir(), ignore_errors=True)


def write_skill(dir_name: str, *, name: str = "", description: str = "",
                body: str = "正文。", raw: str | None = None) -> Path:
    """造一个技能目录。

    `raw` 给了就**逐字**写（用来造坏 frontmatter）；否则按 name/description 拼。
    故意支持 name 与 dir_name 不同（排序那组要证明排序键是**名字**）。
    """
    d = skill_utils.skills_dir() / dir_name
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

    # 纵深防御：就算有人绕过发现，手工造一个指向库外的 Skill 也一样读不出来
    forged = skill_utils.Skill(name="sneaky", description="库外",
                               path=outside / skill_utils.SKILL_FILENAME, dir=outside)
    check("★ 纵深防御：库外的 SKILL.md 直接读不出来（返回 None，不是内容）",
          skill_utils.load_skill_body(forged) is None)

    good = skill_view("ok")
    check("正常的技能照读（安全边界没有误伤）", good.get("content", "").strip() == "正常正文。")
    check("名字非法时错误里给出**可用的技能名**（模型能自己改）", good.get("name") == "ok")
    bad_payload = skill_view("../secret")
    check("…非法名字的错误里也带着可用清单", "available" in bad_payload, str(bad_payload)[:90])


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
     "缺 name / 缺 description / frontmatter 坏 / 没有 SKILL.md / 名字非法 → 跳过并计数（不抛）",
     [test_discovery_is_tolerant_and_counts_skips,
      test_frontmatter_parser_is_a_narrow_subset]),
    ("4. 排序确定",
     "乱序创建 → 索引按**名字**排序 · 两次构建一致",
     [test_index_order_is_by_name]),
    ("5. skill_view 安全",
     "`..` / 绝对路径 / 斜杠 / 空白 / 不存在 → 可读 JSON 错误 · 读不到库外文件 · 符号链接目录被跳过",
     [test_skill_view_refuses_to_escape_the_skills_dir]),
    ("6. 工具闸门（注册 → toolset → 可见）",
     "每个注册工具都有 toolset · `skill_view` 在 skills 组与默认捆里 · 有技能才可见",
     [test_skill_view_is_registered_and_exposed,
      test_skill_view_visibility_tracks_the_index]),
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
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
