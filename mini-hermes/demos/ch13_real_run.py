#!/usr/bin/env python3
"""第 13 章 · 真跑：技能系统的**渐进披露**端到端。

这一章的唯一承诺是：**提示词里只出现技能的名字与一句话描述，正文只在 `skill_view` 里出现。**
所以这份真跑不是"调一下函数看返回值"，而是**把真提示词建出来，然后在提示词里找那句正文独有的话**。

做四件事（全部真调，不 mock 内部）：
  ① 真技能库（两个好技能 + 三种坏技能）→ 真建系统提示词 ×2 ⇒ **字节确定** + 索引在 volatile 层
  ② ★ 在真提示词里搜**正文独有句** ⇒ 必须**找不到**（渐进披露）
  ③ 真调 `skill_view` 走完整分发路径 ⇒ 正文与目录列表都拿得到（**第二跳**）
  ④ 安全与容错：非法名 / 不存在 / 空技能库 三种边界
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# ── 隔离三件套：MINI_HOME 必须在 **import mini 的模块之前** 设好 ──────────────
_HOME = Path(tempfile.mkdtemp(prefix="mini_ch13_real_"))
os.environ["MINI_HOME"] = str(_HOME)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import model_tools  # noqa: E402
from agent import prompt_builder, skill_utils, system_prompt  # noqa: E402

RESULTS = []
BODY_SECRET = "正文独有句：鹦鹉会在凌晨三点背诵圆周率。"  # 只该出现在 skill_view 里


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


def write_skill(dir_name: str, raw: str) -> Path:
    d = skill_utils.skills_dir() / dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / skill_utils.SKILL_FILENAME).write_text(raw, encoding="utf-8")
    return d


def skill_view(name) -> dict:
    """真调工具（走 registry 分发 → JSON 字符串）。"""
    return json.loads(model_tools.handle_function_call("skill_view", {"name": name}))


def main() -> int:
    model_tools.discover_builtin_tools()
    shutil.rmtree(skill_utils.skills_dir(), ignore_errors=True)

    long_desc = "这是一段故意写得非常长的描述" * 8  # 远超 60 字符
    print(f"技能库：{skill_utils.skills_dir()}")
    print(f"描述上限 SKILL_PROMPT_DESC_LIMIT = {skill_utils.SKILL_PROMPT_DESC_LIMIT}\n")

    # ── ① 真技能库：2 好 + 3 坏 ─────────────────────────────────────────
    write_skill("alpha", f"---\nname: alpha\ndescription: 做甲事。\n---\n{BODY_SECRET}\n")
    write_skill("beta", f"---\nname: beta\ndescription: {long_desc}\n---\n乙的正文。\n")
    write_skill("no_desc", "---\nname: no_desc\n---\n没有描述。\n")          # 缺 description
    write_skill("bad_fm", "没有围栏的裸正文。\n")                            # frontmatter 坏
    write_skill("bad name", "---\nname: bad name\ndescription: 名字带空格。\n---\n坏名字。\n")

    scan = skill_utils.discover_skills()
    print("【发现】可用技能 =", [s.name for s in scan.skills],
          "· 跳过 =", [(s.dir_name, s.reason) for s in scan.skipped], "\n")
    record("发现：两个好技能进库", [s.name for s in scan.skills] == ["alpha", "beta"])
    record("发现：三种坏技能被跳过并计数", len(scan.skipped) == 3)

    # ── ② 真提示词 ×2 ⇒ 字节确定 + 渐进披露 ────────────────────────────
    agent = make_agent()
    p1 = system_prompt.build_system_prompt(agent)
    p2 = system_prompt.build_system_prompt(agent)
    record("真提示词连续两次构建 ⇒ 字节完全一致", p1 == p2, f"{len(p1)} vs {len(p2)} 字节")

    idx_pos = p1.find(skill_utils.SKILL_INDEX_HEADING)
    record("技能索引出现在真提示词里", idx_pos >= 0)
    record("索引里有名字与描述（`- alpha: …`）", "- alpha: " in p1)
    record("描述被硬截到 60 字符（含 `...`）",
           f"- beta: {long_desc[: skill_utils.SKILL_PROMPT_DESC_LIMIT - 3]}..." in p1)
    record("★ 正文独有句**不在**提示词里（渐进披露）", BODY_SECRET not in p1)
    record("索引块就是 volatile 层的开头",
           p1[idx_pos: idx_pos + len(skill_utils.SKILL_INDEX_HEADING)] == skill_utils.SKILL_INDEX_HEADING
           and idx_pos == p1.find("## 可用技能"), f"pos={idx_pos}")
    record("volatile 层里出现过的正文独有句数 = 0", p1.count("鹦鹉") == 0)

    # 无关操作后仍字节确定（换一次 invalidate + 再建）
    system_prompt.invalidate_system_prompt(agent)
    p3 = system_prompt.build_system_prompt(agent)
    record("invalidate 之后重建 ⇒ 仍字节一致", p3 == p1)

    # ── ③ 真调 skill_view（第二跳）─────────────────────────────────────
    ok = skill_view("alpha")
    print("\n【skill_view alpha】", json.dumps(ok, ensure_ascii=False)[:220], "...\n")
    # 出口契约（`tools/registry.py:349/355`）：成功 = 数据字典，失败 = `{"error": …}`
    record("skill_view 成功取回正文", "error" not in ok and BODY_SECRET in ok.get("content", ""))
    record("skill_view 同时给了目录列表", isinstance(ok.get("files"), list) and ok["files"])
    record("skill_view 拿到的正文**不**在提示词里", BODY_SECRET not in p1 and BODY_SECRET in ok.get("content", ""))

    # ── ④ 边界：非法名 / 不存在 / 空技能库 ──────────────────────────────
    bad = skill_view("../evil")
    record("非法名（`../evil`）⇒ 可读错误、不抛异常",
           "error" in bad and "不合法" in bad["error"])
    missing = skill_view("nope")
    record("不存在的名字 ⇒ 可读错误且列出可用名字",
           "error" in missing and missing.get("available") == ["alpha", "beta"])
    record("两种失败都只回错误 + 可用名单（没有正文 → 没读到目录外）",
           set(bad) == {"error", "available"} and set(missing) == {"error", "available"})

    # 空技能库：索引必须是**空串**（不是空块）
    shutil.rmtree(skill_utils.skills_dir(), ignore_errors=True)
    empty_idx = prompt_builder.load_skill_index()
    empty_prompt = system_prompt.build_system_prompt(make_agent())
    record("无技能库 ⇒ 索引是空串（不留空块）", empty_idx == "", repr(empty_idx[:40]))
    record("无技能库 ⇒ 提示词里连标题都不出现", "可用技能" not in empty_prompt)
    record("无技能库 ⇒ 提示词里也没有 skill_view 工具",
           "skill_view" not in json.dumps(model_tools.get_tool_definitions(), ensure_ascii=False))

    n_ok = sum(1 for _, c, _ in RESULTS if c)
    print(f"\n════ {n_ok} 通过 / {len(RESULTS) - n_ok} 失败　（共 {len(RESULTS)} 条）")
    print(f"    临时 MINI_HOME：{_HOME}")
    return 1 if n_ok != len(RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
