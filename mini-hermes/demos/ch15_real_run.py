#!/usr/bin/env python3
"""第 15 章 · 真跑：**观察者真的在旁边看着，而且什么都没改**（端到端、不联网）。

这一章的唯一承诺是：**agent 内的三个边界（回合 / API 请求 / 工具）能被只读地观测到，
而观测这件事本身不花冤枉钱、也永远不会拖垮一轮对话。**
所以这份真跑不是"调一下 `emit()` 看返回值"，而是：

  ① **真 agent + 假 provider**（`_http_post` 换成罐头响应）驱动一轮真对话 + 一次真工具调用，
     把 7 个事件名的**流水**逐条打出来，并在**真实载荷**上断言字段；
  ② 观测 **`api_request_error` 那一路**：主 provider 429 → 回退换 provider → **重发**
     （一次 LLM 调用 ⇒ 两个 `api_request_id`、`attempt` 递增）；
  ③ ★ **零成本快路径**：同一轮对话跑两遍 —— **0 观察者时 7 个载荷构造器总计 0 次**，
     有观察者时按事件各构造若干次（这就是这一章的成本不变量）；
  ④ **载荷安全**：把真凭证 / 超长串 / 深嵌套 / 大 dict 塞进工具参数与结果，
     观察者拿到的载荷里它们分别已经是 `<redacted>` / `[REDACTED]` / `truncated` / 降级预览；
  ⑤ **失败放开**：三个回调、中间那个抛异常 —— 另外两个照跑、轮次照常收尾、日志里有警告。

跑完 `try/finally` 清掉临时 `MINI_HOME`（别在系统 temp 里留垃圾）。
"""
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

# ── 隔离三件套：MINI_HOME 必须在 **import mini 的模块之前** 设好 ──────────────
_HOME = Path(tempfile.mkdtemp(prefix="mini_ch15_real_"))
os.environ["MINI_HOME"] = str(_HOME)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import model_tools  # noqa: E402
from agent.observer import (  # noqa: E402
    OBSERVER_EVENTS,
    PAYLOAD_MAX_CHARS_ENV,
    ObserverRegistry,
    payload_max_chars,
)
from run_agent import AIAgent, ApiHTTPError  # noqa: E402

RESULTS = []
SPEC = {"provider": "fake", "api_mode": "chat_completions", "base_url": "http://fake",
        "api_key": "fake", "model": "fake-model"}
FALLBACK_ENTRY = {"provider": "openai", "model": "gpt-4o-mini",
                  "base_url": "http://fallback.test/v1", "api_key": "***"}
PAYLOAD_BUILDERS = ("_payload_pre_llm_call", "_payload_post_llm_call",
                    "_payload_pre_api_request", "_payload_post_api_request",
                    "_payload_api_request_error", "_payload_pre_tool_call",
                    "_payload_post_tool_call")

#: 演示用的假凭证（**故意**让它们出现在工具参数与结果里）。
GH_TOKEN = "ghp_" + "a" * 24
SK_TOKEN = "sk-" + "b" * 24
BIG = "长" * 9000


def record(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {detail}" if detail and not cond else ""))


def make_agent(registry=None, **kw) -> AIAgent:
    kw.setdefault("fallback_providers", [])
    kw.setdefault("max_turns", 5)
    return AIAgent(dict(SPEC), observer=registry, **kw)


def text_response(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {}}


def tool_response(tool_name: str, args: dict, call_id: str = "call_prov_1") -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": tool_name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
    }


def subscribe_all(registry: ObserverRegistry) -> list:
    """把 7 个事件全订上，返回"载荷流水"list（顺序 = 真发顺序）。"""
    stream: list = []
    for ev in OBSERVER_EVENTS:
        registry.add(ev, (lambda e: (lambda **kw: stream.append(kw)))(ev))
    return stream


def print_stream(stream: list, limit: int = 12) -> None:
    print(f"  ── 事件流水（共 {len(stream)} 发）")
    for i, kw in enumerate(stream[:limit], 1):
        extra = ""
        if kw.get("api_request_id"):
            extra += f" req={kw['api_request_id'].rsplit(':', 1)[-1]}/{kw.get('attempt')}"
        if kw.get("tool_name"):
            extra += f" tool={kw['tool_name']}/{kw.get('status', '')}"
        if kw.get("status"):
            extra += f" status={kw['status']}"
        print(f"    {i:>2}. {kw['event']:<18}{kw.get('turn_id', '')[-9:]:<10}"
              f" iter={kw.get('iteration', '-')} msgs={kw.get('message_count', '-')}{extra}")
    if len(stream) > limit:
        print(f"    …（后面还有 {len(stream) - limit} 发）")


def count_payload_builds(agent: AIAgent) -> dict:
    """把 7 个载荷构造器包一层计数（★ 零成本快路径的度量工具）。"""
    counters = {n: 0 for n in PAYLOAD_BUILDERS}

    def wrapper(original, key):
        def counted(*a, **kw):
            counters[key] += 1
            return original(*a, **kw)
        return counted

    for name in PAYLOAD_BUILDERS:
        setattr(agent, name, wrapper(getattr(agent, name), name))
    return counters


def drive_turn(agent: AIAgent, tool_name: str, args: dict) -> dict:
    """真 agent + 假 provider：第 1 次响应要工具、第 2 次给最终回答。"""
    calls = {"n": 0}
    first = tool_response(tool_name, args)

    def post(cand, msgs):
        calls["n"] += 1
        return first if calls["n"] == 1 else text_response("完成")

    agent._http_post = post
    return agent.run_conversation("跑一个工具，然后收尾")


def main() -> int:
    print(f"解释器：{sys.executable}  Python {sys.version.split()[0]}")
    print(f"MINI_HOME = {_HOME}（临时）\n")

    # ══════════════════════════════════════════════════════════════════
    # ① 真跑一轮：7 个事件名的流水 + 字段
    # ══════════════════════════════════════════════════════════════════
    echo = "_test_ch15_echo"
    model_tools.registry.register_tool(
        echo, description="第 15 章演示用回显工具", parameters={}, toolset="test")(
        lambda **kw: json.dumps({"ok": True, "echoed": sorted(kw)}, ensure_ascii=False))
    try:
        reg = ObserverRegistry()
        stream = subscribe_all(reg)
        ag = make_agent(reg)
        res = drive_turn(ag, echo, {"path": "src/app.py"})
        print("【① 真跑一轮（1 次工具调用 ⇒ 2 次 LLM 调用 / 2 次请求）】")
        print_stream(stream)
        order = [k["event"] for k in stream]
        record("① 一轮正常完成 = **10 发**事件，且逐条按序（pre 在 post 之前、"
               "工具夹在两次 LLM 调用之间）",
               order == ["pre_llm_call", "pre_api_request", "post_api_request", "post_llm_call",
                         "pre_tool_call", "post_tool_call",
                         "pre_llm_call", "pre_api_request", "post_api_request", "post_llm_call"],
               str(order))
        by_event: dict = {}
        for k in stream:
            by_event.setdefault(k["event"], k)
        record("① 六个事件名都在真流水里出现了（`api_request_error` 只在出错那一路，见 ②）",
               set(order) == set(OBSERVER_EVENTS) - {"api_request_error"}, str(sorted(set(order))))
        record("① 载荷带 5 个关联 id（task/session/tool_call/turn/api_request）+ 版本字段",
               all({"task_id", "session_id", "tool_call_id", "turn_id", "api_request_id",
                    "telemetry_schema_version"} <= set(k) for k in stream))
        record("① `turn_id` 一轮一个：本轮 10 发的 turn_id **完全相同**",
               len({k["turn_id"] for k in stream}) == 1, str({k["turn_id"] for k in stream}))
        record("① 两个 API 请求两个 `api_request_id`（形状 `{turn_id}:api:{n}`）",
               len({k["api_request_id"] for k in stream if k["api_request_id"]}) == 2
               and all(k["api_request_id"].startswith(k["turn_id"] + ":api:")
                       for k in stream if k["api_request_id"]))
        record("① `tool_call_id` 来自 **provider**（`call_prov_1` 原样进载荷）",
               by_event["pre_tool_call"]["tool_call_id"] == "call_prov_1"
               and by_event["post_tool_call"]["tool_call_id"] == "call_prov_1")
        record("① 工具**真的执行了**：`post_tool_call.result` 是那个回显工具的返回值",
               by_event["post_tool_call"]["status"] == "ok"
               and '"ok": true' in str(by_event["post_tool_call"]["result"]).replace("'", '"'),
               str(by_event["post_tool_call"]["result"])[:80])
        record("① 观察者**没改结果**：最终回答与迭代数就是 agent 原本的产物",
               res["final_response"] == "完成" and res["iterations_used"] == 2,
               str(res["final_response"]))

        # ══════════════════════════════════════════════════════════════
        # ② `api_request_error` 那一路：429 → 回退 → 重发
        # ══════════════════════════════════════════════════════════════
        reg2 = ObserverRegistry()
        stream2 = subscribe_all(reg2)
        ag2 = make_agent(reg2, fallback_providers=[dict(FALLBACK_ENTRY)])
        seen_providers: list = []

        def flaky(cand, msgs):
            seen_providers.append(cand["provider"])
            if len(seen_providers) == 1:
                raise ApiHTTPError(429, "rate limited", "http://fake/chat/completions")
            return text_response("ok")

        ag2._http_post = flaky
        ag2._post_chat_completions([{"role": "user", "content": "hi"}], "t")
        print("\n【② 429 → 回退换 provider → 重发（一次 LLM 调用、两次请求）】")
        print_stream(stream2, limit=6)
        errs = [k for k in stream2 if k["event"] == "api_request_error"]
        pres = [k for k in stream2 if k["event"] == "pre_api_request"]
        posts = [k for k in stream2 if k["event"] == "post_api_request"]
        record("② 抛错那发真的发了 `api_request_error`（`error.type` + `status_code`）",
               len(errs) == 1 and errs[0]["error"]["type"] == "ApiHTTPError"
               and errs[0]["status_code"] == 429, str(errs))
        record("② ★ `retryable=True`（429 会被回退链接手）——这条字段把「为什么换了 provider」说清楚了",
               errs[0]["retryable"] is True)
        record("② 两次请求 **两个 api_request_id**、`attempt` 递增 1 → 2",
               len({k["api_request_id"] for k in pres}) == 2
               and [k["attempt"] for k in pres] == [1, 2], str([k["attempt"] for k in pres]))
        record("② 重发那一发在载荷里能看到**换了谁**（provider/model 已原地换）",
               pres[1]["provider"] == "openai" and pres[1]["model"] == "gpt-4o-mini",
               str(pres[1])[:120])
        record("② 成对关系没坏：只有成功那一发有 `post_api_request`",
               len(posts) == 1 and posts[0]["status"] == "ok", str(posts))

        # ══════════════════════════════════════════════════════════════
        # ③ ★ 零成本快路径
        # ══════════════════════════════════════════════════════════════
        cold = make_agent(ObserverRegistry())          # 空的注册表
        cold_counters = count_payload_builds(cold)
        cold_res = drive_turn(cold, echo, {"path": "src/app.py"})
        warm_registry = ObserverRegistry()
        with_obs = make_agent(warm_registry)
        subscribe_all(warm_registry)
        warm_counters = count_payload_builds(with_obs)
        drive_turn(with_obs, echo, {"path": "src/app.py"})

        print("\n【③ 零成本快路径：同一轮对话跑两遍】")
        print(f"    0 观察者  ：{cold_counters}")
        print(f"    7 观察者  ：{warm_counters}")
        print("    （`post_llm_call` / `post_tool_call` 的载荷会把公共字段那一层调一次 —— "
              "所以 pre_* 的计数是 2 次直发 + 2 次被 post 复用）")
        record("③ ★ **0 观察者 ⇒ 载荷构造 0 次**（不是「构造完再丢掉」）",
               sum(cold_counters.values()) == 0 and cold_res["final_response"] == "完成",
               str(cold_counters))
        record("③ 有观察者 ⇒ 每个事件的载荷都跟着**流水条数**走"
               "（pre/post_llm 各 2 发、pre/post_api 各 2 发、工具前后各 1 发）",
               warm_counters["_payload_post_llm_call"] == 2
               and warm_counters["_payload_pre_api_request"] == 2
               and warm_counters["_payload_post_api_request"] == 2
               and warm_counters["_payload_post_tool_call"] == 1
               and warm_counters["_payload_api_request_error"] == 0, str(warm_counters))
        record("③ 成本与「有没有人听」**同源**（`has()` 就是唯一事实源，没有旁路开关）",
               cold._observers.has("pre_llm_call") is False
               and with_obs._observers.has("pre_llm_call") is True)

        # ══════════════════════════════════════════════════════════════
        # ④ 载荷安全：真凭证 / 超长串 / 深嵌套 / 大 dict
        # ══════════════════════════════════════════════════════════════
        reg4 = ObserverRegistry()
        stream4 = subscribe_all(reg4)
        ag4 = make_agent(reg4)
        deep = {"leaf": 1}
        for _ in range(11):
            deep = {"n": deep}
        drive_turn(ag4, echo, {
            "api_key": "sk-real-key-不该出门",
            "headers": {"Authorization": f"Bearer {SK_TOKEN}"},
            "note": f"顺手记一下 token={GH_TOKEN}",
            "big": BIG,
            "deep": deep,
            "many": {f"k{i}": i for i in range(260)},
        })
        pre_tool = [k for k in stream4 if k["event"] == "pre_tool_call"][0]
        args = pre_tool["args"]
        print("\n【④ 载荷安全：塞进去的是真凭证与超大结构，观察者看到的是这样】")
        print(f"    api_key      → {args['api_key']!r}")
        print(f"    Authorization→ {args['headers']['Authorization']!r}")
        print(f"    note         → {args['note']!r}")
        print(f"    big          → {args['big'][:28]!r}…（{len(args['big'])} 字符）")
        print(f"    deep         → {json.dumps(args['deep'], ensure_ascii=False)[:70]}…")
        print(f"    many         → {len(args['many'])} 个键，末尾 {json.dumps(list(args['many'].items())[-1], ensure_ascii=False)}")
        record("④ ★ 键级脱敏：`api_key` / `Authorization` 的值变成固定标记",
               args["api_key"] == "<redacted>" and args["headers"]["Authorization"] == "<redacted>",
               str(args["api_key"]))
        record("④ ★ 文本级脱敏：键名正常的 `note` 里那句 `token=ghp_…` 也被抹掉",
               GH_TOKEN not in args["note"] and SK_TOKEN not in json.dumps(args, ensure_ascii=False),
               args["note"])
        record("④ 超长串截到 8000 并**注明丢了几个字符**",
               args["big"].endswith(f"...[truncated {len(BIG) - 8000} chars]")
               and args["big"].startswith("长" * 10), args["big"][-40:])
        record("④ 深嵌套在 8 层处停下（`<dict depth limit>`，不会递归爆栈）",
               "<dict depth limit>" in json.dumps(args["deep"], ensure_ascii=False))
        record("④ 大 dict 被**提前 break**：只留 200 个键 + 一条 `_truncated_items`",
               len(args["many"]) == 201 and args["many"]["_truncated_items"] == 60,
               f"{len(args['many'])} 个键")

        os.environ[PAYLOAD_MAX_CHARS_ENV] = "1000"        # ★ 把总上限压到 1000（下限）
        try:
            reg5 = ObserverRegistry()
            stream5 = subscribe_all(reg5)
            ag5 = make_agent(reg5)
            drive_turn(ag5, echo, {"blob": "x" * 5000})
            post_tool = [k for k in stream5 if k["event"] == "post_tool_call"][0]
            print(f"\n    （`{PAYLOAD_MAX_CHARS_ENV}=1000` 时）post_tool_call 载荷 → "
                  f"keys={sorted(post_tool)[:4]}… preview={len(post_tool.get('preview', ''))} 字符")
            record("④ ★ 总上限：超限先**降级 caps**、还超就换成 `_truncated` + 预览"
                   "（观察者永远能读到东西，不是空载荷）",
                   post_tool.get("_truncated") is True and post_tool.get("original_type") == "dict"
                   and len(post_tool["preview"]) == 1000,
                   str(post_tool)[:120])
            record("④ env 覆盖 + **下限 1000** 都生效（照真实侧 `max(1000, int(raw))`）",
                   payload_max_chars() == 1000, str(payload_max_chars()))
        finally:
            os.environ.pop(PAYLOAD_MAX_CHARS_ENV, None)
        record("④ env 已被还回去（默认上限 50000）", payload_max_chars() == 50000)

        # ══════════════════════════════════════════════════════════════
        # ⑤ 失败放开：坏回调崩不掉这一轮
        # ══════════════════════════════════════════════════════════════
        warnings: list = []

        class _Capture(logging.Handler):
            def emit(self, record_obj) -> None:      # noqa: ANN001
                warnings.append(record_obj.getMessage())

        capture = _Capture(level=logging.DEBUG)
        obs_logger = logging.getLogger("agent.observer")
        obs_logger.addHandler(capture)
        old_propagate = obs_logger.propagate
        obs_logger.propagate = False
        try:
            reg6 = ObserverRegistry()
            hits: list = []

            def bad(**kw):
                raise RuntimeError("这个观察者坏了（演示用）")

            reg6.add("pre_tool_call", lambda **kw: hits.append("before"))
            reg6.add("pre_tool_call", bad)
            reg6.add("pre_tool_call", lambda **kw: hits.append("after"))
            ag6 = make_agent(reg6)
            res6 = drive_turn(ag6, echo, {"path": "src/app.py"})
        finally:
            obs_logger.removeHandler(capture)
            obs_logger.propagate = old_propagate

        print("\n【⑤ 失败放开：三个回调，中间那个抛异常】")
        print(f"    实际跑到的回调 = {hits}")
        print(f"    日志警告 = {warnings}")
        record("⑤ ★ 坏回调**只丢自己**：它前后的两个都跑到了",
               hits == ["before", "after"], str(hits))
        record("⑤ ★ 轮次**不受影响**：最终回答照旧（观察者绝不是控制流的一段）",
               res6["final_response"] == "完成" and res6["iterations_used"] == 2,
               str(res6["final_response"]))
        record("⑤ 有警告留下（点名事件 + 异常摘要，不是静默吞掉）",
               any("pre_tool_call" in w and "RuntimeError" in w for w in warnings), str(warnings))
        record("⑤ 返回值一律忽略：观察者拿不到任何「决定权」（阻断要走 `approval_callback`）",
               reg6.emit("pre_tool_call", tool_name="t") is None)
    finally:
        model_tools.registry.deregister(echo)

    n_ok = sum(1 for _, c, _ in RESULTS if c)
    print(f"\n════ {n_ok} 通过 / {len(RESULTS) - n_ok} 失败　（共 {len(RESULTS)} 条）")
    print(f"    临时 MINI_HOME：{_HOME}")
    return 1 if n_ok != len(RESULTS) else 0


if __name__ == "__main__":
    # ★ 跑完清掉临时 MINI_HOME（用 finally 保证中途抛异常也不留垃圾）。
    try:
        _code = main()
    finally:
        shutil.rmtree(_HOME, ignore_errors=True)
    sys.exit(_code)
