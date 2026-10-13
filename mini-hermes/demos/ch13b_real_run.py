#!/usr/bin/env python3
"""第 13b 章 · 真跑：**内置技能**与**用户技能**两级共存（用户优先）。

第 13 章只做了**用户级**技能库（`~/.mini-hermes/skills/`）。这一章补上第一级：
仓内 `mini-hermes/skills/`（**随程序发布的内置技能**）。两个根**共存**，用户同名**覆盖**内置。

隔离做法照 `demos/ch13_real_run.py`：把 `MINI_HOME` 指到临时目录（**用户级**根被隔离），
但**内置根用真仓里的那份**（`mini-hermes/skills/`）—— 要验证的正是"仓内内置技能真的能用"。
（测试里两个根都要隔离；真跑这里**故意留内置根是真的**。）

四件事（全部真调，不 mock 内部）：
  ① **只靠内置技能**就能进提示词索引、`skill_view` 能读正文（用户根为空）
  ② 用户放一个**同名**技能 ⇒ 用户版胜出、内置那份进 skipped（理由说明被谁覆盖）
  ③ 索引顺序**确定**（两次构建字节一致、按技能名升序）
  ④ 跑完清理临时 MINI_HOME
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# ── 隔离：MINI_HOME 必须在 **import mini 的模块之前** 设好 ──────────────────
_HOME = Path(tempfile.mkdtemp(prefix="mini_ch13b_real_"))
os.environ["MINI_HOME"] = str(_HOME)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import model_tools  # noqa: E402
from agent import prompt_builder, skill_utils, system_prompt  # noqa: E402

RESULTS = []
BUNDLED_NAME = "mini-repo-conventions"
BUNDLED_DESC = "本仓（mini-hermes）工程约定：改完跑对应测试、双解释器、清 __pycache__、零依赖。"
BODY_MARK = "发布纪律破了个口子"          # 只出现在内置技能**正文**里
USER_MARKER = "用户覆盖正文标记_只此一份"


def record(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {detail}" if detail and not cond else ""))


def make_agent():
    from run_agent import AIAgent

    return AIAgent(
        {
            "provider": "fake",
            "api_mode": "chat_completions",
            "base_url": "http://fake",
            "api_key": "fake",
            "model": "fake-model",
        }
    )


def skill_view(name) -> dict:
    """真调工具（走 registry 分发 → JSON 字符串）。"""
    return json.loads(model_tools.handle_function_call("skill_view", {"name": name}))


def write_user_skill(dir_name: str, name: str, description: str, body: str) -> Path:
    d = skill_utils.skills_dir() / dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / skill_utils.SKILL_FILENAME).write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8")
    return d


def main() -> int:
    model_tools.discover_builtin_tools()
    bundled = skill_utils.bundled_skills_dir()
    print(f"内置根（真仓）：{bundled}")
    print(f"用户根（临时）：{skill_utils.skills_dir()}\n")

    record("内置根 = 仓内 mini-hermes/skills（随程序发布的那份）", bundled.is_dir(), str(bundled))
    record(f"内置根里有示例技能 {BUNDLED_NAME}",
           (bundled / BUNDLED_NAME / skill_utils.SKILL_FILENAME).is_file())

    # 用户根清空 —— 这一轮**只**靠内置技能
    shutil.rmtree(skill_utils.skills_dir(), ignore_errors=True)

    # ── ① 只靠内置技能 ⇒ 进索引 + skill_view 能读正文 ────────────────────
    scan = skill_utils.discover_skills()
    names = [s.name for s in scan.skills]
    print("【发现】可用 =", names, "· 跳过 =", [(s.dir_name, s.reason) for s in scan.skipped], "\n")
    record(f"只靠内置根就能发现技能（{BUNDLED_NAME} 进库）", BUNDLED_NAME in names, str(names))
    record("has_skills() 为真（工具闸门 = 索引闸门，覆盖两个根）", skill_utils.has_skills())

    idx = prompt_builder.load_skill_index()
    record("内置技能进了**提示词索引**（块标题 + 技能行）",
           idx.startswith(skill_utils.SKILL_INDEX_HEADING) and f"- {BUNDLED_NAME}: " in idx)

    ag = make_agent()
    prompt = system_prompt.build_system_prompt(ag)
    record("真提示词里带上了内置技能（名字 + 一句话描述都在）",
           BUNDLED_NAME in prompt and BUNDLED_DESC in prompt)
    record("★ 内置技能的**正文**不在提示词里、也不在索引里（渐进披露：只有索引）",
           BODY_MARK not in prompt and BODY_MARK not in idx)

    got = skill_view(BUNDLED_NAME)
    content = got.get("content", "")
    print("【skill_view 内置】", json.dumps(got, ensure_ascii=False)[:200], "...\n")
    record("skill_view 能读出内置技能**正文**（且剥掉了 frontmatter）",
           "error" not in got and content.startswith("# mini-hermes 工程约定"), content[:60])
    record("…正文里含真内容那句（确实读到了内置文件）", BODY_MARK in content)
    record("…返回里带着它来自**内置**根（path 落在内置根下）",
           str(bundled) in got.get("path", ""), got.get("path", ""))
    record("…同时给出目录列表（渐进披露第二跳入口）",
           isinstance(got.get("files"), list)
           and any(e.get("name") == skill_utils.SKILL_FILENAME for e in got["files"]))

    # ── ② 用户同名覆盖内置 ────────────────────────────────────────────
    write_user_skill(BUNDLED_NAME, BUNDLED_NAME, "用户自己写的覆盖版", USER_MARKER)
    scan2 = skill_utils.discover_skills()
    print("【覆盖】可用 =", [(s.name, s.description) for s in scan2.skills],
          "\n        跳过 =", [(s.dir_name, s.reason) for s in scan2.skipped], "\n")
    check_skill = next((s for s in scan2.skills if s.name == BUNDLED_NAME), None)
    record("同名时**用户版胜出**（索引里的描述变成用户的）",
           check_skill is not None and check_skill.description == "用户自己写的覆盖版",
           str([(s.name, s.description) for s in scan2.skills]))
    reasons = {s.dir_name: s.reason for s in scan2.skipped}
    record("★ 内置那份进 skipped，理由说明**被哪个根的哪份**覆盖（可诊断）",
           BUNDLED_NAME in reasons and "用户/" in reasons.get(BUNDLED_NAME, ""), str(reasons))
    idx2 = prompt_builder.load_skill_index()
    record("覆盖反映到提示词：索引里是**用户版**描述、内置版那句没了",
           "用户自己写的覆盖版" in idx2 and BUNDLED_DESC not in idx2)
    got2 = skill_view(BUNDLED_NAME)
    record("skill_view 也拿到**用户版正文**（读取与索引一致）",
           USER_MARKER in got2.get("content", ""), str(got2)[:160])

    # ── ③ 索引顺序确定 ────────────────────────────────────────────────
    write_user_skill("aaa-user", "aaa-user", "用户 aaa", "正文")
    write_user_skill("zzz-user", "zzz-user", "用户 zzz", "正文")
    b1 = prompt_builder.load_skill_index()
    b2 = prompt_builder.load_skill_index()
    record("索引两次构建**字节一致**（跨根顺序确定）", b1 == b2 and bool(b1))
    order = [l.split(": ", 1)[0][2:] for l in b1.splitlines() if l.startswith("- ")]
    record("跨根合并后按**技能名**全局升序（不是\"用户全在前、内置全在后\"）",
           order == sorted(order), str(order))

    n_ok = sum(1 for _, c, _ in RESULTS if c)
    print(f"\n════ {n_ok} 通过 / {len(RESULTS) - n_ok} 失败　（共 {len(RESULTS)} 条）")
    print(f"    临时 MINI_HOME：{_HOME}（跑完清理）")
    return 1 if n_ok != len(RESULTS) else 0


if __name__ == "__main__":
    # ★ 跑完清掉临时 MINI_HOME（用 finally 保证中途抛异常也不留垃圾）。
    try:
        _code = main()
    finally:
        shutil.rmtree(_HOME, ignore_errors=True)
    sys.exit(_code)
