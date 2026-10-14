#!/usr/bin/env python3
"""演示：agent 对象上「挂着」什么，以及「每轮新建」vs「缓存」的差别 —— 全部实测。

跑法：python3 demos/why_cache_the_agent.py
（不联网：只造对象 + 拼提示词，不发起请求）
"""
import hashlib
import os
import pathlib
import sys
import tempfile
from pathlib import Path

MINI = Path(__file__).resolve().parent.parent          # 项目根（不许写死机器路径）
sys.path.insert(0, str(MINI))

# ── 造一个"有身份、有记忆、有项目上下文"的环境（否则读文件次数全是 0，对比看不出来）──
# 临时数据仅属于本次演示；包括异常退出路径也统一清理。
_TEMP_ROOT = tempfile.TemporaryDirectory(prefix="mini-clean-why_cache_the_agent-")
try:
    HOME = Path(tempfile.mkdtemp(prefix="mh-hot-home-", dir=_TEMP_ROOT.name))
    PROJ = Path(tempfile.mkdtemp(prefix="mh-hot-proj-", dir=_TEMP_ROOT.name))
    (HOME / "SOUL.md").write_text("# 我是谁\n我是一个用来演示的 agent。\n", encoding="utf-8")
    (HOME / "MEMORY.md").write_text("# 记忆\n- 用户的终端是 iTerm2\n- 项目叫 mini-hermes\n", encoding="utf-8")
    (PROJ / "AGENTS.md").write_text("# 项目约定\n- 这个仓库用中文注释\n", encoding="utf-8")
    os.environ["MINI_HOME"] = str(HOME)
    os.environ["MINI_CONTEXT_CWD"] = str(PROJ)          # mini 的"用户项目目录"

    # ── 装置：数一数"拼提示词时读了几次文件" ──
    reads: list[str] = []
    _real_read_text = pathlib.Path.read_text


    def counting_read_text(self, *a, **k):
        reads.append(self.name)
        return _real_read_text(self, *a, **k)


    pathlib.Path.read_text = counting_read_text

    from agent.system_prompt import build_system_prompt  # noqa: E402
    from run_agent import AIAgent  # noqa: E402


    def make_agent():
        return AIAgent({"provider": "fake", "api_mode": "chat_completions",
                        "base_url": "http://fake", "api_key": "fake", "model": "fake-model"},
                       max_turns=5)


    print("═" * 74)
    print("① 一个 agent 对象上「挂着」什么（真身：它的变量表）")
    a = make_agent()
    keys = sorted(k for k in a.__dict__ if not k.startswith("__"))
    print(f"   对象 {type(a).__name__} 一共有 {len(keys)} 个变量。其中这几件是「跨轮活着」的：")
    for k in ("_cached_system_prompt", "_cached_system_prompt_static", "todo", "usage",
              "_interrupt", "budget", "session_id"):
        v = a.__dict__.get(k)
        preview = ("（还没拼过）" if v is None and "prompt" in k
                   else type(v).__name__ + " " + str(v)[:38].replace("\n", "⏎"))
        print(f"     {k:28s} = {preview}")

    print()
    print("═" * 74)
    print("② 拼系统提示词：同一个对象拼两次 vs 每轮新建再拼")
    reads.clear()
    p1 = build_system_prompt(a)
    n1, files1 = len(reads), sorted(set(reads))
    reads.clear()
    p2 = build_system_prompt(a)
    n2 = len(reads)
    print(f"   同一个 agent 拼第 1 次 → 读了 {n1} 个文件：{files1}")
    print(f"   同一个 agent 拼第 2 次 → 读了 {n2} 个文件")
    print(f"   两次拿到的是同一个字符串对象吗：{p1 is p2}（提示词 {len(p1)} 字符）")
    print(f"   提示词指纹 sha256[:12] = {hashlib.sha256(p1.encode()).hexdigest()[:12]}")

    brand_new = make_agent()
    reads.clear()
    build_system_prompt(brand_new)
    nb1 = len(reads)
    fabricated = make_agent()
    reads.clear()
    build_system_prompt(fabricated)
    nb2 = len(reads)
    print(f"   ★ 「每轮新建」：新对象 A 拼一次 → 读了 {nb1} 个文件：{sorted(set(reads))}")
    print(f"   ★ 「每轮新建」：新对象 B 拼一次 → 读了 {nb2} 个文件（每轮都从头来）")

    print()
    print("═" * 74)
    print("③ 那份「拼好的东西」长什么样（截前 300 字符 —— 这就是被「挂着」的东西）")
    print("   ┌" + "─" * 70)
    for line in p1[:300].splitlines():
        print("   │ " + line[:68])
    print("   └" + "─" * 70 + f"   …（共 {len(p1)} 字符；每轮都要把它发出去）")

    print()
    print("═" * 74)
    print("④ 另一个「热着」的东西：agent 私有状态（todo 清单）")
    print(f"   同一个 agent：todo 对象地址 = {id(a.todo)}")
    print(f"   新建的 agent：todo 对象地址 = {id(brand_new.todo)}　← 换了一块内存，清单是空的")
    print("   → 缓存：下一轮还是同一个 todo 对象（内存里的清单直接可用）")
    print("   → 每轮新建：清单归零，只能**从历史里重放**（mini 的 `_hydrate_todo_store` 专干这个）")

    print()
    print("═" * 74)
    print("⑤ 一句话账：")
    print(f"   「缓存」的代价 = {n2} 次读文件、复用已缓存的提示词（直接返回那个 `_cached_system_prompt`）")
    print(f"   「每轮新建」的代价 = 重新读 {nb1} 个文件 + 重新拼这 {len(p1)} 字符"
          f"（≈{len(p1)//4} token），而且拼出来的字节稍有不同，")
    print(f"        服务商那边的前缀缓存就**从那一位往后全部作废**（第 3 章讲过这个代价模型）")
finally:
    if "_real_read_text" in globals():
        pathlib.Path.read_text = _real_read_text
    _TEMP_ROOT.cleanup()
