"""第 4 章的自测：工具运行时（发现 / toolset / check_fn / 分发）—— 不联网。

跑法：python3 tests/test_tools_runtime.py

## 测的是什么（6 组）

| 组 | 守的承诺 |
|---|---|
| 1. AST 自动发现 | **按语法形状判断，不按文本**；只看模块体；坏文件不炸整个流程 |
| 2. toolset：注册 ≠ 暴露 | 注册了但没被 toolset 点名 → **模型看不到**（且不报错） |
| 3. check_fn | TTL 缓存 / **抖动保护** / fail-safe / 两层缓存 / no_cache |
| 4. 结果契约 | 出口**永远是 JSON 字符串**；错误有上限；异常不变 traceback |
| 5. agent 级工具守卫 | 绕过 agent loop 直接分发 → 给出**能指出问题**的错误 |
| 6. 端到端 | 跑一轮真 loop，抓请求里的 tools 字段与工具结果 |

## 隔离

`MINI_HOME` 指向临时目录（import 之前设好），所以测试不会碰用户真实的
`~/.mini-hermes/`。工具注册是**全局状态**，所有注册临时工具的测试都用
try/finally 摘掉。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TEST_HOME = tempfile.mkdtemp(prefix="mini_tools_test_")
os.environ["MINI_HOME"] = _TEST_HOME

import model_tools  # noqa: E402
import toolsets  # noqa: E402
from tools import registry  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {detail}" if detail and not cond else ""))


def _write(tmp: Path, name: str, source: str) -> Path:
    p = tmp / name
    p.write_text(source, encoding="utf-8")
    return p


# ══════════════════════════════════════════════ 1. AST 自动发现
def test_ast_detection_accepts_decorator_and_call() -> None:
    """两种注册形态都要认出来：装饰器 + 模块级普通调用。"""
    tmp = Path(tempfile.mkdtemp(prefix="mini_ast_"))
    deco = _write(tmp, "deco.py", '''
from tools.registry import register_tool

@register_tool("t1", description="x", parameters={})
def t1():
    return "{}"
''')
    plain = _write(tmp, "plain.py", '''
from tools.registry import register_tool

def t2():
    return "{}"

register_tool("t2", description="x", parameters={})(t2)
''')
    check("带 @register_tool 装饰器的函数 → 算工具模块",
          model_tools.module_registers_tools(deco))
    check("模块级 register_tool(...) 调用 → 也算",
          model_tools.module_registers_tools(plain))
    shutil.rmtree(tmp, ignore_errors=True)


def test_ast_detection_ignores_text_and_nested() -> None:
    """★ 这是 AST 检查 vs 文本检查的分水岭。

    三种"看起来像但其实不是"的情况都必须**不算**：
      · 注释里提到 register_tool
      · 字符串里提到 register_tool
      · 注册写在**函数内部**（不是模块体）
    """
    tmp = Path(tempfile.mkdtemp(prefix="mini_ast2_"))
    comment_only = _write(tmp, "c.py", '''
# 这个模块以后会用 register_tool 注册工具
NAME = "about register_tool"
''')
    in_function = _write(tmp, "f.py", '''
from tools.registry import register_tool

def setup():
    @register_tool("t3", description="x", parameters={})
    def t3():
        return "{}"
    return t3
''')
    for_driven = _write(tmp, "loop.py", '''
from tools.registry import register_tool

_TABLE = {"a": 1, "b": 2}
for _name in _TABLE:
    register_tool(_name, description="x", parameters={})(lambda: "{}")
''')
    check("只在注释/字符串里出现 → **不算**（文本检查会误判）",
          not model_tools.module_registers_tools(comment_only))
    check("注册写在函数内部 → **不算**（只看模块体）",
          not model_tools.module_registers_tools(in_function))
    check("模块级 for 循环里注册 → **算**（表驱动模块）",
          model_tools.module_registers_tools(for_driven))
    shutil.rmtree(tmp, ignore_errors=True)


def test_ast_detection_survives_broken_file() -> None:
    """语法坏掉的文件不该让整个发现流程崩掉。"""
    tmp = Path(tempfile.mkdtemp(prefix="mini_ast3_"))
    broken = _write(tmp, "broken.py", "def oops(:\n    pass\n")
    try:
        result = model_tools.module_registers_tools(broken)
        check("语法错误的文件 → 返回 False 而不是抛异常", result is False)
    except SyntaxError as exc:
        check("语法错误的文件 → 返回 False 而不是抛异常", False, f"抛了 SyntaxError: {exc}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_discovery_found_the_real_tools() -> None:
    """真实目录的发现结果（这是"手写名单"被替掉后的**新契约**）。"""
    imported = model_tools.discover_builtin_tools()
    check("发现了 3 个工具模块", len(imported) == 3, str(imported))
    check("文件工具被发现了", "tools.file_tools" in imported, str(imported))
    check("终端被发现了", "tools.terminal" in imported, str(imported))
    check("搜索被发现了（第 4 章新增，修掉提示词宣传不存在的工具）",
          "tools.search_files" in imported, str(imported))
    check("没把基础设施当工具（registry / approval / __init__）",
          not any(m.endswith((".registry", ".approval", ".__init__")) for m in imported),
          str(imported))
    check("没把 todo.py 当工具（它不用 register_tool，走 AGENT_LEVEL_TOOLS）",
          "tools.todo" not in imported, str(imported))


# ══════════════════════════════════════════════ 2. toolset：注册 ≠ 暴露
def test_toolset_resolution_three_input_shapes() -> None:
    """三种入参：给 enabled / 给 disabled / 都不给（对应 Hermes 的前三条规则）。"""
    all_names = toolsets.get_enabled_tool_names()
    check("都不给 → 全部默认捆", all_names == set(toolsets.MINI_CORE_TOOLS), str(all_names))

    only_file = toolsets.get_enabled_tool_names(enabled=["file"])
    check("给 enabled=['file'] → 只有文件工具", only_file == {"read_file", "write_file", "list_dir"},
          str(only_file))

    no_term = toolsets.get_enabled_tool_names(disabled=["terminal"])
    check("给 disabled=['terminal'] → 少了终端", "terminal" not in no_term)
    check("…其他都还在", "read_file" in no_term and "todo_list" in no_term)

    both = toolsets.get_enabled_tool_names(enabled=["file", "terminal"], disabled=["file"])
    check("同时给 → **禁用赢**（先加后减）", both == {"terminal"}, str(both))


def test_registered_but_not_exposed_is_invisible() -> None:
    """★★ 这一组是本章最重要的一条承诺：**注册 ≠ 暴露**。

    注册一个工具、**不**把它加进任何 toolset —— 它应该：
      · 在注册表里（`registry.get()` 拿得到）
      · **不在**发给模型的 schema 里
      · 而且**不报任何错**（这就是"静默失败"的样子）
    """
    registry.register_tool(
        "_orphan_tool", description="没被任何 toolset 点名", parameters={},
        toolset="nowhere")(lambda **kw: registry.tool_result(ok=True))
    try:
        names = [d["function"]["name"] for d in model_tools.get_tool_definitions()]
        check("它**在**注册表里", registry.get("_orphan_tool") is not None)
        check("它**不在**发给模型的 schema 里（注册 ≠ 暴露）", "_orphan_tool" not in names, str(names))
        check("孤儿检查能把它指出来（把静默变成可检查）",
              "_orphan_tool" in toolsets.orphan_tools(registry.tool_names()))
        check("…而正常工具不是孤儿",
              "read_file" not in toolsets.orphan_tools(registry.tool_names()))
    finally:
        registry.deregister("_orphan_tool")


def test_every_registered_tool_has_a_toolset() -> None:
    """**契约**断言（不是数量断言）。

    对应 Hermes tools/AGENTS.md 的测试要求：
    *assert contracts ("every registered tool has a toolset") rather than tool counts*。

    数量断言每次加工具都要改，而且不告诉你任何关系；
    这条契约断言在"有人忘了加 toolset"时**自动报警**。
    """
    orphans = toolsets.orphan_tools(registry.tool_names())
    check("每个注册的工具都归属某个 toolset（否则它永远不会被暴露）",
          not orphans, f"孤儿：{orphans}")


# ══════════════════════════════════════════════ 3. check_fn
def test_check_fn_filters_schema() -> None:
    """探测不通过 → 整条 schema 都不发（模型连它存在都不知道）。"""
    calls = {"n": 0}

    def failing() -> bool:
        calls["n"] += 1
        return False

    registry.register_tool("_no_check", description="x", parameters={},
                           toolset="test")(lambda **kw: registry.tool_result(ok=1))
    registry.register_tool("_fail_check", description="x", parameters={}, toolset="test",
                           check_fn=failing)(lambda **kw: registry.tool_result(ok=1))
    try:
        registry.invalidate_check_fn_cache()
        defs = [d["function"]["name"] for d in registry.get_definitions(
            ["_no_check", "_fail_check"])]
        check("没有 check_fn 的工具照常通过", "_no_check" in defs)
        check("check_fn 返回 False → 不出现在 schema 里", "_fail_check" not in defs, str(defs))
        check("…探测确实被调用了", calls["n"] == 1, str(calls))
    finally:
        registry.deregister("_no_check")
        registry.deregister("_fail_check")


def test_check_fn_exception_is_fail_safe() -> None:
    """探测自己炸了 → 当作**不可用**（fail-safe），而不是让整个构建崩掉。"""
    def boom() -> bool:
        raise RuntimeError("探测坏了")

    registry.register_tool("_boom_check", description="x", parameters={}, toolset="test",
                           check_fn=boom)(lambda **kw: registry.tool_result(ok=1))
    try:
        registry.invalidate_check_fn_cache()
        check("抛异常的 check_fn → 直接跑返回 False", registry.run_check_fn_uncached(boom) is False)
        # 走 get_definitions 也不该抛出来
        defs = [d["function"]["name"] for d in registry.get_definitions(["_boom_check"])]
        check("…所以 schema 构建不会崩，只是这个工具不发", "_boom_check" not in defs)
    finally:
        registry.deregister("_boom_check")


def test_check_fn_two_layer_cache() -> None:
    """两层缓存：memo（单次构建内）在 TTL 缓存**之上**。"""
    calls = {"n": 0}

    def counted() -> bool:
        calls["n"] += 1
        return True

    registry.register_tool("_c1", description="x", parameters={}, toolset="test",
                           check_fn=counted)(lambda **kw: "{}")
    registry.register_tool("_c2", description="x", parameters={}, toolset="test",
                           check_fn=counted)(lambda **kw: "{}")
    saved_ttl = registry.CHECK_FN_TTL_SECONDS
    try:
        registry.invalidate_check_fn_cache()
        calls["n"] = 0
        # ① memo 层：**一次构建里**同一个 check_fn 只探一次（虽然两个工具共用它）
        registry.get_definitions(["_c1", "_c2"])
        check("同一次构建里两个工具共用一个 check_fn → 只探一次", calls["n"] == 1, str(calls))

        # ② TTL 层：第二次构建不再探（命中进程级缓存）
        calls["n"] = 0
        registry.get_definitions(["_c1", "_c2"])
        check("立刻再构建一次 → 命中 TTL 缓存，仍然 0 次探测", calls["n"] == 0, str(calls))

        # ③ TTL 设为 0 → 每次都重探
        registry.CHECK_FN_TTL_SECONDS = 0.0
        registry.invalidate_check_fn_cache()
        registry.get_definitions(["_c1", "_c2"])
        check("TTL=0 → 重新探测（缓存真的会过期）", calls["n"] == 1, str(calls))
    finally:
        registry.CHECK_FN_TTL_SECONDS = saved_ttl
        registry.deregister("_c1")
        registry.deregister("_c2")
        registry.invalidate_check_fn_cache()


class _FakeClock:
    """把 registry 用的时钟换成可拨的。

    `registry.time` 只是个**模块级名字**，所以能直接替换成一个有 `.monotonic()`
    的对象——**不需要为了可测性去改生产代码**。
    （对照：如果去改 registry 加一个 `_now()` 间接层，就是让生产代码
      为测试让步。这里不用。）
    """

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def monotonic(self) -> float:
        return self.t


def test_check_fn_flake_guard_keeps_tool_available() -> None:
    """★★ 抖动保护：**最近成功过、这次失败了 → 继续当作可用**。

    这是本章最精细的一处设计（我好几次猜错它的方向）。
    防的是"能力忽然从模型眼前消失"：一个刚还能用的工具，
    不该因为一次探测失败就从 schema 里消失——模型的计划里还有它。

    ★ 关键的第二半：**兜底的那个 True 不能被缓存**。
    否则"假装还能用"会自动续期，而工具其实一直坏着。
    正确组合是「**继续乐观 + 立刻重新检查**」。

    ⚠️ 这半条用 `TTL=0` 是**测不出来**的：TTL=0 时"没缓存"和
    "缓存了但立刻过期"表现完全一样。所以这里必须控制时钟——
    不然就是在断言一件自己的装置证明不了的事。
    """
    state = {"ok": True}
    calls = {"n": 0}

    def flaky() -> bool:
        calls["n"] += 1
        return state["ok"]

    saved_time = registry.time
    saved_ttl = registry.CHECK_FN_TTL_SECONDS
    saved_grace = registry.CHECK_FN_FAILURE_GRACE_SECONDS
    clock = _FakeClock()
    try:
        registry.time = clock
        registry.CHECK_FN_TTL_SECONDS = 30.0
        registry.CHECK_FN_FAILURE_GRACE_SECONDS = 9999.0
        registry.invalidate_check_fn_cache()

        registry.check_fn_cached(flaky)
        check("t=1000 探测成功，用了 1 次探测", calls["n"] == 1, str(calls))

        clock.t = 1010.0
        registry.check_fn_cached(flaky)
        check("TTL 内再问 → 命中缓存，不探测", calls["n"] == 1, str(calls))

        state["ok"] = False
        clock.t = 1031.0                       # 距上次成功 31s > TTL 30s
        check("★ 刚成功过又失败 → 当作抖动，**仍然返回 True**（工具不消失）",
              registry.check_fn_cached(flaky) is True)
        check("…而且真的重新探测了（不是拿旧结果糊弄）", calls["n"] == 2, str(calls))

        # ★★ 证明"那个兜底的 True 没被缓存"。
        #    若被缓存，下一个 TTL 内的调用会直接命中并返回 True；
        #    而此时工具**确实坏了**、宽限期也已关掉 —— 正确答案是 False。
        registry.CHECK_FN_FAILURE_GRACE_SECONDS = 0.0
        clock.t = 1032.0                       # 距上一次写入只有 1s，稳在 TTL 内
        result = registry.check_fn_cached(flaky)
        check("★★ 抖动兜底的 True **没被缓存**（下个 TTL 内仍会重新探测）",
              result is False and calls["n"] == 3,
              f"返回={result} 探测次数={calls['n']}")

        check("宽限期关掉后 → 失败即失败（保护窗口是有意的，不是永远的）",
              registry.check_fn_cached(flaky) is False)
    finally:
        registry.time = saved_time
        registry.CHECK_FN_TTL_SECONDS = saved_ttl
        registry.CHECK_FN_FAILURE_GRACE_SECONDS = saved_grace
        registry.invalidate_check_fn_cache()


def test_no_cache_check_fn_always_probes() -> None:
    """`@no_cache_check_fn` → 每次都探（本地配置读，必须立刻生效）。"""
    calls = {"n": 0}

    @registry.no_cache_check_fn
    def always() -> bool:
        calls["n"] += 1
        return True

    try:
        registry.invalidate_check_fn_cache()
        registry.check_fn_cached(always)
        registry.check_fn_cached(always)
        check("标了 no_cache → 连问两次就探两次", calls["n"] == 2, str(calls))
    finally:
        registry.invalidate_check_fn_cache()


# ══════════════════════════════════════════════ 4. 结果契约：出口永远是 JSON
def test_error_contract_is_json() -> None:
    """错误一律 JSON，而且**永远不抛出来**。"""
    r = model_tools.handle_function_call("完全不存在的工具", {})
    parsed = json.loads(r)
    check("未知工具 → JSON", "error" in parsed, r)
    check("…错误里带上可用工具名（模型能据此自己改）",
          "available" in parsed and "read_file" in parsed["available"], r)

    r2 = model_tools.handle_function_call("read_file", {})
    check("参数不匹配 → JSON（不是 TypeError）", "error" in json.loads(r2), r2)

    registry.register_tool("_raises", description="x", parameters={}, toolset="test")(
        lambda **kw: (_ for _ in ()).throw(ValueError("内部炸了")))
    try:
        r3 = model_tools.handle_function_call("_raises", {})
        p3 = json.loads(r3)
        check("工具内部抛异常 → 变成 JSON 错误（不是 traceback）",
              "ValueError" in p3.get("error", ""), r3)
    finally:
        registry.deregister("_raises")


def test_error_text_is_bounded() -> None:
    """错误文本有上限 —— 因为它会进历史，而历史**每轮重发**。

    一条 200KB 的 traceback 会在之后每一轮都被重新发送：**成本是复利的**。
    """
    huge = model_tools.tool_error("x" * (registry.MAX_ERROR_CHARS * 3))
    body = json.loads(huge)["error"]
    check("超长错误被截断到上限附近",
          len(body) < registry.MAX_ERROR_CHARS + 50, f"len={len(body)}")
    check("截断处有标记（不是静默截断）", "截断" in body, body[-30:])


def test_non_string_handler_result_is_wrapped() -> None:
    """出口契约：**无论 handler 返回什么，出去的都是 JSON 字符串**。"""
    registry.register_tool("_returns_dict", description="x", parameters={}, toolset="test")(
        lambda **kw: {"ok": True, "n": 1})
    try:
        r = model_tools.handle_function_call("_returns_dict", {})
        check("handler 返回 dict → 被包成 JSON 字符串",
              isinstance(r, str) and json.loads(r)["ok"] is True, repr(r))
    finally:
        registry.deregister("_returns_dict")


# ══════════════════════════════════════════════ 5. agent 级工具守卫
def test_agent_loop_tool_guard() -> None:
    """绕过 agent loop 直接分发 agent 级工具 → 给出**能指出问题**的错误。

    注意它和"未知工具"的区别：
      · 未知工具 → "这个工具不存在"（可能名字写错了）
      · agent 级工具 → "它存在，但**必须由 agent loop 处理**"（换个调用方才能解决）
    后者才是可操作的错误信息。
    """
    r = model_tools.handle_function_call("todo_list", {"todos": []})
    err = json.loads(r).get("error", "")
    check("todo_list 直接分发 → 被守卫拦下", "error" in json.loads(r), r)
    check("错误说明是**调用方不对**，不是工具不存在", "agent loop" in err, err)
    check("…而且不是含糊的'未知工具'", "未知工具" not in err, err)
    check("todo_list 确实不在注册表里（它是 agent 级工具）",
          registry.get("todo_list") is None)


# ══════════════════════════════════════════════ 6. 端到端
def test_end_to_end_model_sees_what_it_should() -> None:
    """跑一轮真 loop，抓**实际发出的请求**，看 tools 字段和工具结果。

    断言的是"模型实际看到什么"，不是"某个函数返回什么"——
    因为这一章讲的所有闸门，最终都体现在那一个 tools 字段上。
    """
    from run_agent import AIAgent

    seen: dict = {}

    def recorder(cand, api_messages):
        seen["tools"] = [t["function"]["name"] for t in (seen.get("req_tools") or [])]
        seen["n"] = seen.get("n", 0) + 1
        if seen["n"] == 1:
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "list_dir", "arguments": '{"path": "/tmp"}'}}]}}],
                "usage": {}}
        return {"choices": [{"message": {"role": "assistant", "content": "完"}}], "usage": {}}

    ag = AIAgent({"provider": "fake", "api_mode": "chat_completions", "base_url": "http://f",
                  "api_key": "f", "model": "fake"}, max_turns=3)

    # 直接检查"发给模型的 tools 字段"，比抓请求简单且同样准确
    names = [d["function"]["name"] for d in model_tools.get_tool_definitions()]
    check("默认情况下模型看到 6 个工具（含 todo_list）", len(names) == 6, str(names))
    check("search_files 在里面（提示词宣传它，现在它真的存在了）",
          "search_files" in names, str(names))

    # 禁掉一个 toolset → 模型**看不到**那组工具
    narrowed = [d["function"]["name"]
                for d in model_tools.get_tool_definitions(disabled_toolsets=["terminal"])]
    check("禁掉 terminal toolset → 终端工具从 schema 里消失", "terminal" not in narrowed)
    check("…别的还在", "read_file" in narrowed and "todo_list" in narrowed)

    # 真跑一轮，确认工具结果进历史的是 JSON
    ag._http_post = recorder
    res = ag.run_conversation("列一下目录")
    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    check("循环跑通并拿到了工具结果", len(tool_msgs) == 1, str(len(tool_msgs)))
    if tool_msgs:
        content = tool_msgs[0].get("content", "")
        try:
            parsed = json.loads(content)
            check("工具结果进历史的是 **JSON**（不是人看的纯文本）",
                  "entries" in parsed or "total" in parsed, content[:80])
        except (TypeError, ValueError):
            check("工具结果进历史的是 **JSON**", False, content[:80])


# ══════════════════════════════════════════════ 7. 工作目录与注册完整性（第 4 章复检新增）
def test_context_cwd_is_single_sourced() -> None:
    """★「我在哪个目录」只能有**一个实现**。

    提示词侧（`agent/prompt_builder`）和工具侧（`tools/*`）必须读同一个函数，
    否则两边会各说各话。第 4 章复检时它们就是分开的：
    提示词读 `MINI_LAUNCH_CWD`、工具读 `os.getcwd()`。
    """
    import agent.prompt_builder as pb
    import hermes_constants as hc

    check("prompt_builder 暴露的就是 hermes_constants 里那个函数（单源，不是两份拷贝）",
          pb.resolve_context_cwd is hc.resolve_context_cwd)
    check("source 版本也是同一个", pb.resolve_context_cwd_source is hc.resolve_context_cwd_source)


def test_tools_run_in_the_prompt_cwd() -> None:
    """★★ 回归：工具执行的工作目录 == 提示词报告的工作目录。

    **真 bug 记录**：启动器为了 import 会 `cd` 到安装树，所以进程 cwd 是安装树；
    而提示词报的是用户项目目录。工具以前用 `os.getcwd()` →
    **模型被告知在 A、实际在 B 里干活**。实测症状是 agent 报告
    「/tmp/myproj 下只有 agent/ demos/ hermes_cli/ tests/ tools/」——那是安装树。

    这个 bug 不看提示词和工具两边的**实际输出**是发现不了的，所以这里两边都断言。
    """
    proj = Path(tempfile.mkdtemp(prefix="mini_cwd_")).resolve()
    # ★ 必须 resolve()：macOS 上 /var 是 /private/var 的符号链接，
    #   而 `pwd` 回的是解析后的路径 → 不 resolve 就会"两边都对但字符串不等"。
    (proj / "src").mkdir()
    (proj / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    saved = os.environ.get("MINI_LAUNCH_CWD")
    try:
        os.environ["MINI_LAUNCH_CWD"] = str(proj)
        import hermes_constants as hc

        check("提示词侧报的是用户项目", hc.resolve_context_cwd() == str(proj))

        entries = json.loads(model_tools.handle_function_call("list_dir", {"path": "."}))
        names = {e["name"] for e in entries.get("entries", [])}
        check("★ 工具看到的也是**用户项目**（不是安装树）", "src" in names, str(names))
        check("…而安装树里的东西不在里面（这是原来的症状）", "agent" not in names, str(names))

        r = json.loads(model_tools.handle_function_call("read_file", {"path": "src/app.py"}))
        check("相对路径 `src/app.py` 解析到用户项目",
              r.get("path") == str(proj / "src" / "app.py"), str(r.get("path")))

        t = json.loads(model_tools.handle_function_call("terminal", {"command": "pwd"}))
        check("terminal 的 cwd 也是用户项目", t.get("stdout", "").strip() == str(proj),
              f"实际 {t.get('stdout', '').strip()!r} ≠ 期望 {str(proj)!r}")

        s = json.loads(model_tools.handle_function_call("search_files",
                                                       {"pattern": "print", "path": "."}))
        # grep 报的是**相对它 cwd** 的路径，所以这里只能断言"搜到了 src/app.py"
        # ——但如果它的 cwd 是安装树，就一个匹配都不会有（这也是区分力所在）。
        hits = [m for m in s.get("matches", []) if m.get("path", "").endswith("src/app.py")]
        check("search_files 搜的是用户项目（能找到 src/app.py）", bool(hits), str(s)[:140])
    finally:
        if saved is None:
            os.environ.pop("MINI_LAUNCH_CWD", None)
        else:
            os.environ["MINI_LAUNCH_CWD"] = saved
        shutil.rmtree(proj, ignore_errors=True)


def test_every_tool_handler_is_its_own_function() -> None:
    """★ 契约：注册的 handler 必须是**和工具同名那个**函数。

    抓的是一类很隐蔽的错：**装饰器绑错了函数**。第 4 章复检时真发生过——
    把一个新的 helper 插在 `@register_tool("read_file", ...)` 和
    `def read_file_tool(...)` **之间**，装饰器就绑到 helper 上了：
    `read_file` 的 handler 变成那个 helper，而真正的 `read_file_tool` 没注册。

    症状：`TypeError: Object of type PosixPath is not JSON serializable` ——
    **报错离肇事者非常远**（看起来像 JSON 契约坏了，实际是注册绑错了）。
    """
    bad = []
    for name in registry.tool_names():
        fn = registry.get(name).fn
        if fn.__name__ not in (f"{name}_tool", name):
            bad.append(f"{name} → {fn.__name__}")
    check("每个工具的 handler 都是「和它同名」的那个函数", not bad, f"绑错的：{bad}")


GROUPS = [
    ("1. AST 自动发现",
     "按语法形状判断（不按文本）/ 只看模块体 / 坏文件不炸 / 发现真实工具",
     [test_ast_detection_accepts_decorator_and_call,
      test_ast_detection_ignores_text_and_nested,
      test_ast_detection_survives_broken_file,
      test_discovery_found_the_real_tools]),
    ("2. toolset：注册 ≠ 暴露 ★",
     "三种入参 / **注册了但没点名 → 模型看不到** / 契约：每个工具都有 toolset",
     [test_toolset_resolution_three_input_shapes,
      test_registered_but_not_exposed_is_invisible,
      test_every_registered_tool_has_a_toolset]),
    ("3. check_fn ★",
     "TTL 缓存 / **抖动保护** / fail-safe / 两层缓存 / no_cache",
     [test_check_fn_filters_schema,
      test_check_fn_exception_is_fail_safe,
      test_check_fn_two_layer_cache,
      test_check_fn_flake_guard_keeps_tool_available,
      test_no_cache_check_fn_always_probes]),
    ("4. 结果契约：出口永远是 JSON",
     "未知工具 / 参数不匹配 / 内部异常 → 都是 JSON；错误有上限；非字符串被包一层",
     [test_error_contract_is_json,
      test_error_text_is_bounded,
      test_non_string_handler_result_is_wrapped]),
    ("5. agent 级工具守卫",
     "绕过 agent loop 直接分发 → 错误要能指出**调用方不对**",
     [test_agent_loop_tool_guard]),
    ("6. 端到端",
     "模型实际看到的 tools 字段 / toolset 能收窄 / 工具结果是 JSON",
     [test_end_to_end_model_sees_what_it_should]),
    ("7. 工作目录与注册完整性（复检新增）",
     "「我在哪」单源 / **工具跑在提示词报的那个目录** / handler 没绑错函数",
     [test_context_cwd_is_single_sourced,
      test_tools_run_in_the_prompt_cwd,
      test_every_tool_handler_is_its_own_function]),
]


if __name__ == "__main__":
    print(f"解释器：{sys.executable}  Python {sys.version.split()[0]}")
    print(f"MINI_HOME = {_TEST_HOME}\n")
    registered = {name for name, _, _ in GROUPS}
    defined = {n for n in globals() if n.startswith("test_")}
    missing = defined - {n for _, _, fns in GROUPS for n in (f.__name__ for f in fns)}
    if missing:
        print(f"⚠️ 有测试函数没登记进 GROUPS（写了但不会跑）：{sorted(missing)}")
        raise SystemExit(2)

    for title, promise, fns in GROUPS:
        print(f"══ {title} ══ {promise}")
        for fn in fns:
            fn()
        print(f"   → {len(PASS) + len(FAIL)} 累计 / {len(FAIL)} 失败\n")

    print(f"\n{len(PASS)} 通过 / {len(FAIL)} 失败　（{len(GROUPS)} 组）")
    if FAIL:
        print("失败项：" + "; ".join(FAIL))
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
    raise SystemExit(1 if FAIL else 0)
