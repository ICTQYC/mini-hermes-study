"""第 15 章的自测：观察者 / observer hooks（A1~A6）—— **不联网、零第三方依赖**。

跑法：python3 tests/test_observers.py

## 测的是什么（6 组）

| 组 | 守的承诺 |
|---|---|
| 1. 注册契约 | 未知事件名**只警告仍存** · **不去重**（登记两次调两次）· **保序** · 只读查询 · `has()` 是唯一事实源 |
| 2. 零成本快路径 ★ | **0 观察者 ⇒ 载荷构造 0 次**（不是"构造完再丢掉"）· 有观察者 ⇒ **恰好 1 次** · `emit` 无订阅时**连 sanitize 都不跑** |
| 3. 失败放开 | 回调抛异常 ⇒ 同事件其它回照跑 · 轮次**不受影响** · 有警告（带事件名 + 异常摘要）· **返回值一律忽略** |
| 4. 载荷安全 | 键级脱敏 · **文本级脱敏**（键名正常、值里有凭证）· 超长串 · 深嵌套 · 超长序列 · **dict/list 溢出语义不同** · 总量上限两级降级 + env 下限 1000 / 坏值回落 |
| 5. 关联 id | 5 字段齐 · **None → ""** · 同轮 `turn_id` 相同 / 跨轮不同 · 每请求一个 `api_request_id`（含回退重发）· `tool_call_id` 来自 provider |
| 6. 事件触发点 ★ | 假 provider 驱动**一轮 + 一次工具调用** ⇒ 7 个事件**按序**发出 · 字段齐 · 版本字段在唯一出口注入 · 失败/被拒/解析失败三种结局用 `status` 区分 |

★ 组 2 是这一章最核心的一条**成本不变量**：观察者缺席时热路径只付一次 dict 探针。
反向验证（主代理做）会删掉调用点那道 `if self._observers.has(...)` 门 —— 本文件第 2 组
（`test_zero_cost_fast_path_builds_no_payload`）会立刻变红。

## 组织方式

测试函数的**位置**与所属**组**解耦：分组写在 `GROUPS` 表里（照 `tests/test_skills.py`），
配一条**未注册自检**：定义了 `test_` 却漏登记 ⇒ 直接退出（2）。
（"写了但从来没跑"是测试里最经典的静默失败。）

## 隔离三件套（照 `tests/test_skills.py` 的写法）

  1. `MINI_HOME` 在 **import 任何 mini 模块之前**指到临时目录（否则会去读用户真实的
     `~/.mini-hermes/` 配置与密钥文件 —— 结果就依赖"跑测试的这个人家里放了什么"）；
  2. 需要改环境变量的用例一律走 `env_var()` 上下文管理器：**摘下来、还回去**（不靠
     "跑完记得删"这种会漏的纪律）；
  3. 需要"干净 env"的验证（env 覆盖的下限/坏值）走**子进程**，显式传一份最小 env。
     `try/finally` 收尾清掉临时 `MINI_HOME`（别在系统 temp 里留垃圾）。
"""
from __future__ import annotations

import contextlib
import inspect
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★★ MINI_HOME 必须在 import 任何 mini 模块**之前**设好（见模块 docstring 的隔离三件套）。
_TEST_HOME = tempfile.mkdtemp(prefix="mini_ch15_observers_")
os.environ["MINI_HOME"] = _TEST_HOME

import model_tools  # noqa: E402
from agent import observer as obs_mod  # noqa: E402
from agent.observer import (  # noqa: E402
    JSONABLE_MAX_SEQUENCE,
    JSONABLE_MAX_STRING,
    OBSERVER_EVENTS,
    OBSERVERS,
    PAYLOAD_MAX_CHARS_ENV,
    TELEMETRY_SCHEMA_VERSION,
    TELEMETRY_SCHEMA_VERSION_KEY,
    VALID_EVENTS,
    CallIds,
    ObserverRegistry,
    hook_jsonable,
    is_sensitive_key,
    payload_max_chars,
    sanitize_payload,
)
from hermes_cli import config as cfg  # noqa: E402
from run_agent import AIAgent, ApiHTTPError  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = _TEST_HOME

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {detail}" if detail and not cond else ""))


# ── 夹具 ────────────────────────────────────────────────────────────────
SPEC = {
    "provider": "fake",
    "api_mode": "chat_completions",
    "base_url": "http://fake",
    "api_key": "fake",
    "model": "fake-model",
}

FALLBACK_ENTRY = {
    "provider": "openai",
    "model": "gpt-4o-mini",
    "base_url": "http://fallback.test/v1",
    "api_key": "***",
}

#: 7 个"载荷构造器" —— 零成本快路径要证明的就是**它们一次都没被调用**。
PAYLOAD_BUILDERS = (
    "_payload_pre_llm_call",
    "_payload_post_llm_call",
    "_payload_pre_api_request",
    "_payload_post_api_request",
    "_payload_api_request_error",
    "_payload_pre_tool_call",
    "_payload_post_tool_call",
)


def clear_config() -> None:
    """清掉临时 home 里的 config.json（别让磁盘上的回退链影响用例）。"""
    p = cfg.config_path()
    if p.exists():
        p.unlink()


def make_agent(registry: ObserverRegistry | None = None, **kw) -> AIAgent:
    """不联网的假 agent；`registry` 显式传入（默认不传 ⇒ 挂进程级 `OBSERVERS`）。"""
    clear_config()
    kw.setdefault("fallback_providers", [])
    kw.setdefault("max_turns", 5)
    return AIAgent(dict(SPEC), observer=registry, **kw)


def record_events(registry: ObserverRegistry, event: str) -> list:
    """登记一个"把所有载荷收进 list"的回调，返回那个 list。"""
    seen: list = []
    registry.add(event, lambda **kw: seen.append(kw))
    return seen


def count_payload_builds(agent: AIAgent) -> dict:
    """把 7 个载荷构造器**包一层计数**（★ 零成本快路径的度量工具）。

    包在**实例属性**上（`setattr(agent, ...)`）—— 找的是"调用次数"，不是"函数存在与否"。
    """
    counters = {name: 0 for name in PAYLOAD_BUILDERS}

    def wrapper(original, key):
        def counted(*a, **kw):
            counters[key] += 1
            return original(*a, **kw)
        return counted

    for name in PAYLOAD_BUILDERS:
        setattr(agent, name, wrapper(getattr(agent, name), name))
    return counters


def text_response(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {}}


def tool_response(registry: ObserverRegistry, tool_name: str, args: str,
                  call_id: str = "call_prov_1", usage: dict | None = None) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": tool_name, "arguments": args}}]}}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def drive_one_tool_turn(agent: AIAgent, *, tool_name: str = "todo_list",
                        args: str = '{"todos": [], "merge": false}') -> dict:
    """跑一轮：第 1 次模型调用发起一个工具调用，第 2 次给出最终回答。

    ⇒ 一轮里 **2 次 LLM 调用 / 2 次 API 请求 / 1 次工具调用**（事件序列的固定装置）。
    """
    calls = {"n": 0}
    first = tool_response(agent._observers, tool_name, args)

    def post(cand, msgs):
        calls["n"] += 1
        return first if calls["n"] == 1 else text_response("完成")

    agent._http_post = post
    return agent.run_conversation("跑一个工具")


@contextlib.contextmanager
def env_var(key: str, value: str):
    """设一个环境变量并在收尾**还回去**（有就还原，没有就摘掉）。"""
    saved = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list = []

    def emit(self, record) -> None:      # noqa: ANN001 —— logging.Handler 的签名
        self.records.append(record)


@contextlib.contextmanager
def capture_logs(name: str = "agent.observer"):
    """抓 `agent.observer` 这个 logger 的记录（并关掉向上传播，免得污染 stderr 输出）。"""
    logger = logging.getLogger(name)
    handler = _LogCapture()
    old_level, old_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def register_echo(name: str) -> list:
    """注册一个真的会被调用的测试工具，返回"被调用记录"list（用于证明工具真的执行了）。"""
    hits: list = []

    def handler(**kw):
        hits.append(kw)
        return json.dumps({"ok": True}, ensure_ascii=False)

    model_tools.registry.register_tool(
        name, description="第 15 章测试用工具", parameters={}, toolset="test")(handler)
    return hits


# ══════════════════════════════════════════════════════════════════════════════ #
# 1. 注册契约
# ══════════════════════════════════════════════════════════════════════════════ #
def test_unknown_event_warns_but_is_still_stored() -> None:
    """★ 未知事件名 = **只警告、仍然存**（真实侧 `plugins.py:916-918` 逐字契约）。

    为什么这条重要：拼错一个事件名 **不能**把启动/注册炸掉，但也**不能**静默 ——
    静默的话观察者会"看起来注册了、其实永远收不到"。折中是"警告 + 照存"。
    """
    reg = ObserverRegistry()
    got: list = []
    with capture_logs() as logs:
        reg.add("pre_tool_calls", lambda **kw: got.append(kw))    # ← 故意的拼写错（多一个 s）
    reg.emit("pre_tool_calls", tool_name="t")

    check("未知事件名被**存下来**（没丢、没抛）",
          reg.has("pre_tool_calls") and reg.callback_count("pre_tool_calls") == 1)
    check("...存下来就是**能用**的（emit 真的调到了它）", len(got) == 1 and got[0]["tool_name"] == "t")
    warnings = [r for r in logs.records if r.levelno == logging.WARNING]
    check("★ 只警告（有一条 WARNING 记录，不是异常）", len(warnings) == 1,
          str([r.getMessage() for r in logs.records]))
    detail = warnings[0].getMessage() if warnings else ""
    check("...警告文案里带着**全部**合法事件名（拼错能被发现）",
          all(ev in detail for ev in OBSERVER_EVENTS), detail)
    check("...也点名了那个非法的事件名", "pre_tool_calls" in detail, detail)

    with capture_logs() as logs2:
        reg.add("pre_tool_call", lambda **kw: None)               # 合法名
    check("合法事件名**不**产生警告（默认路径是安静的）",
          [r for r in logs2.records if r.levelno == logging.WARNING] == [],
          str([r.getMessage() for r in logs2.records]))


def test_no_dedup_and_registration_order() -> None:
    """★ 不去重（登记两次调两次）+ **保序**（登记顺序就是调用顺序）。"""
    reg = ObserverRegistry()
    order: list = []

    def first(**kw):
        order.append("first")

    def second(**kw):
        order.append("second")

    reg.add("pre_tool_call", first)
    reg.add("pre_tool_call", second)
    reg.add("pre_tool_call", first)          # ← 同一个回调再登记一次
    reg.emit("pre_tool_call", tool_name="t")

    check("★ 不去重：登记两次就调两次（顺序 first/second/first）",
          order == ["first", "second", "first"], str(order))
    check("计数也如实反映不去重（该事件 3 个回调）", reg.callback_count("pre_tool_call") == 3)
    check("count 是全部事件的总数", reg.count == 3)
    order.clear()
    reg.emit("pre_tool_call", tool_name="t")
    check("...再发一次仍然是同一个顺序（确定性的，不靠 set 迭代顺序）",
          order == ["first", "second", "first"], str(order))


def test_read_only_queries_are_the_single_source_of_truth() -> None:
    """只读查询 + `has()` 是唯一事实源（A8/A9：不另立标志位）。"""
    reg = ObserverRegistry()
    check("空表的只读查询：没有事件、没有回调、has 为假",
          reg.registered_events() == () and reg.count == 0 and not reg.has("post_tool_call"))
    check("`has_observers()` 与 `has()` 是**同一个实现**（同一事实源，不是第二套判定）",
          all(reg.has_observers(ev) == reg.has(ev) for ev in OBSERVER_EVENTS))
    reg.add("post_tool_call", lambda **kw: None)
    reg.add("pre_llm_call", lambda **kw: None)
    check("registered_events 按**注册顺序**、每个事件只出现一次（不去重那条不适用于它）",
          reg.registered_events() == ("post_tool_call", "pre_llm_call"),
          str(reg.registered_events()))
    check("`has()` 立刻反映登记事实（没有缓存/标志位要同步）",
          reg.has("post_tool_call") and not reg.has("api_request_error"))
    check("callbacks_for 给的是**稳定快照**（tuple，回调里改表也动不了当前这轮）",
          isinstance(reg.callbacks_for("post_tool_call"), tuple)
          and len(reg.callbacks_for("post_tool_call")) == 1
          and reg.callbacks_for("api_request_error") == ())
    check("7 个事件名就是 mini 的合法表（真实侧 VALID_HOOKS 39 个的**真接线子集**）",
          len(OBSERVER_EVENTS) == 7 and VALID_EVENTS == frozenset(OBSERVER_EVENTS))
    check("默认 agent 挂的是**进程级** OBSERVERS（真实侧那张全局钩子表）",
          make_agent(None)._observers is OBSERVERS, str(make_agent(None)._observers))


# ══════════════════════════════════════════════════════════════════════════════ #
# 2. 零成本快路径 ★（本章核心）
# ══════════════════════════════════════════════════════════════════════════════ #
def test_zero_cost_fast_path_builds_no_payload() -> None:
    """★★ 没有观察者时，**载荷一次都不许构造**（不是"构造完再丢掉"）。

    场景是"一轮 + 一次工具调用" —— 一轮里有 2 次 LLM 调用、2 次 API 请求、1 次工具调用，
    7 个事件全都有触发点。所以这条断言覆盖了**每一个** fire-site 的那道门。

    反向验证：删掉任意一个调用点的 `if self._observers.has(...)`，本条立刻变红。
    """
    reg = ObserverRegistry()                      # ← 空的：没有任何订阅者
    ag = make_agent(reg)
    counters = count_payload_builds(ag)
    result = drive_one_tool_turn(ag)

    check("轮次**照常完成**（观察者缺席不影响任何行为）",
          result["final_response"] == "完成" and result["iterations_used"] == 2,
          str(result.get("final_response")))
    check("★ 0 观察者 ⇒ **载荷构造 0 次**",
          all(v == 0 for v in counters.values()), str(counters))
    check("...7 个构造器**每一个**都是 0（含 `post_llm_call` 那条 finally 路径、"
          "`post_tool_call` 的唯一出口）",
          sum(counters.values()) == 0 and len(counters) == 7, str(counters))
    check("...轮次里真的发生了 API 与工具活动（不是「因为什么都没干所以 0 次」）",
          result["usage"]["total_tokens"] == 15
          and any(m.get("role") == "tool" for m in result["messages"]),
          str(result["usage"]))


def test_one_observer_builds_each_payload_at_most_once_per_event() -> None:
    """有观察者 ⇒ 该事件的载荷**恰好构造 1 次**（一次模型调用）；其余事件仍为 0。"""
    reg = ObserverRegistry()
    seen = record_events(reg, "pre_llm_call")
    ag = make_agent(reg)
    counters = count_payload_builds(ag)
    ag._http_post = lambda cand, msgs: text_response("完成")
    ag.run_conversation("你好")

    check("★ 只订阅 `pre_llm_call` ⇒ 它的载荷**恰好 1 次**",
          counters["_payload_pre_llm_call"] == 1, str(counters))
    check("...其余 6 个构造器仍是 0（快路径是**按事件**开的，不是全局开关）",
          [k for k, v in counters.items() if v] == ["_payload_pre_llm_call"], str(counters))
    check("回调真的收到了那一发（不是空跑）",
          [k["event"] for k in seen] == ["pre_llm_call"], str(seen))
    check("...载荷里带着 5 个关联 id 的空串（线协议）与版本字段",
          all(k in seen[0] for k in ("task_id", "session_id", "tool_call_id", "turn_id",
                                     "api_request_id", TELEMETRY_SCHEMA_VERSION_KEY))
          and seen[0][TELEMETRY_SCHEMA_VERSION_KEY] == TELEMETRY_SCHEMA_VERSION,
          str(sorted(seen[0])))


def test_emit_returns_immediately_when_nobody_subscribes() -> None:
    """`emit` 内部**也要**再探一次：没人听就立刻返回（连 sanitize 都不跑）。"""
    reg = ObserverRegistry()
    sanitized: list = []
    original = obs_mod.sanitize_payload
    obs_mod.sanitize_payload = lambda v: (sanitized.append(v), original(v))[1]
    try:
        returned = reg.emit("post_tool_call", tool_name="t", result="x" * 100)
    finally:
        obs_mod.sanitize_payload = original

    check("★ 无订阅者时 emit 立刻返回（**连载荷装配都不做**）", sanitized == [], str(sanitized))
    check("...返回值是 None（观察者接口不把结果交给任何人）", returned is None)
    reg.add("post_tool_call", lambda **kw: None)
    original = obs_mod.sanitize_payload
    obs_mod.sanitize_payload = lambda v: (sanitized.append(v), original(v))[1]
    try:
        reg.emit("post_tool_call", tool_name="t")
    finally:
        obs_mod.sanitize_payload = original
    check("有订阅者时才走装配（对照：这时 sanitize 被调了一次）", len(sanitized) == 1)


# ══════════════════════════════════════════════════════════════════════════════ #
# 3. 失败放开
# ══════════════════════════════════════════════════════════════════════════════ #
def test_bad_callback_cannot_break_the_turn() -> None:
    """★ 一个坏回调**只能丢自己**：其它回调照跑，轮次与工具执行都不受影响。

    反向验证：删掉 `ObserverRegistry.emit` 里那个 `try/except Exception`，
    本组第一条（"a 和 c 都跑到了"）与"轮次正常完成"会一起变红。
    """
    reg = ObserverRegistry()
    hits: list = []

    def bad(**kw):
        raise RuntimeError("插件崩了")

    reg.add("pre_tool_call", lambda **kw: hits.append("a"))
    reg.add("pre_tool_call", bad)
    reg.add("pre_tool_call", lambda **kw: hits.append("c"))
    ag = make_agent(reg)
    with capture_logs() as logs:
        result = drive_one_tool_turn(ag)

    check("★ 坏回调**之前**和**之后**的回调都跑到了（跳过它，不是中断整条链）",
          hits == ["a", "c"], str(hits))
    check("★ 轮次**不受影响**：最终回答照旧、迭代数照旧",
          result["final_response"] == "完成" and result["iterations_used"] == 2, str(result))
    check("工具结果也照常回填进历史（观察者崩 ≠ 工具没跑）",
          any(m.get("role") == "tool" for m in result["messages"]), str(result["messages"])[-200:])
    msgs = [r.getMessage() for r in logs.records]
    check("★ 有警告，且**点名了事件与异常**（不是静默吞掉）",
          any("pre_tool_call" in m and "RuntimeError" in m for m in msgs), str(msgs))
    check("...警告里带着异常摘要（能看出是哪一类失败）",
          any("插件崩了" in m for m in msgs), str(msgs))


def test_observer_return_values_are_ignored() -> None:
    """★ 返回值**一律忽略**（只读遥测）—— 观察者不是第二套控制流。"""
    reg = ObserverRegistry()
    reg.add("pre_tool_call", lambda **kw: {"action": "block", "message": "不许跑（应被忽略）"})
    reg.add("post_tool_call", lambda **kw: "被替换的结果（应被忽略）")
    reg.add("pre_llm_call", lambda **kw: {"context": "注入上下文（mini 不采纳）"})
    name = "_test_obs_echo_ignored"
    hits = register_echo(name)
    try:
        ag = make_agent(reg)
        result = drive_one_tool_turn(ag, tool_name=name, args="{}")
        tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
    finally:
        model_tools.registry.deregister(name)

    check("★ `{\"action\": \"block\"}` 被忽略：工具**真的执行了**", len(hits) == 1, str(hits))
    check("...工具结果没被 `post_tool_call` 的返回值替换",
          bool(tool_msgs) and "被替换的结果" not in str(tool_msgs[0]["content"]),
          str(tool_msgs))
    check("...`pre_llm_call` 的 context 也没进历史（mini 不做注入那一路）",
          "注入上下文" not in json.dumps(result["messages"], ensure_ascii=False))
    check("emit 的返回值恒为 None（调用点拿不到任何东西去用）",
          reg.emit("pre_tool_call", tool_name="t") is None)


# ══════════════════════════════════════════════════════════════════════════════ #
# 4. 载荷安全（键级 + 文本级 + 三级截断 + 总量上限）
# ══════════════════════════════════════════════════════════════════════════════ #
def test_sensitive_keys_are_redacted() -> None:
    """① 敏感**键**：命中就换成固定标记（真实侧 `"<redacted>"`）。"""
    check("固定键集命中（api_key / authorization / proxy_authorization / cookie / set_cookie）",
          all(is_sensitive_key(k) for k in ("api_key", "authorization", "proxy_authorization",
                                            "cookie", "set_cookie")))
    check("★ 先 `lower()` 再把 `-` 换成 `_`：`API-Key` / `Proxy-Authorization` 也命中",
          is_sensitive_key("API-Key") and is_sensitive_key("Proxy-Authorization")
          and is_sensitive_key("Set-Cookie"))
    check("后缀规则 `*_api_key`：`openai_api_key` / `DEEPSEEK-API-KEY` 命中",
          is_sensitive_key("openai_api_key") and is_sensitive_key("DEEPSEEK-API-KEY"))
    check("普通键**不**命中（别把 tool_name / model 也脱敏了）",
          not any(is_sensitive_key(k) for k in ("tool_name", "model", "arguments", "key", "tokens")))

    out = hook_jsonable({"api_key": "sk-real-key", "nested": {"authorization": "Bearer abc"},
                         "ok": 1, "list": [{"cookie": "session=1"}]})
    check("值被换成 `<redacted>`（原始字符串**一个字都不剩**）",
          out["api_key"] == "<redacted>" and out["nested"]["authorization"] == "<redacted>"
          and out["list"][0]["cookie"] == "<redacted>", json.dumps(out, ensure_ascii=False))
    check("...非敏感键照原样（脱敏是**定点**的，不是整份糊掉）",
          out["ok"] == 1 and out["nested"] != "<redacted>")


def test_text_level_redaction_catches_secrets_inside_values() -> None:
    """★ ② 文本级脱敏：键名正常、**值里**有凭证 —— 键级挡不住这种，所以两层都要。

    （真实侧同样两层：键级在 `_is_sensitive_hook_key`，文本级在 `agent/redact.py`；
      mini 的文本级复用第 5 章那 7 条模式。）
    """
    gh = "ghp_" + "a" * 24
    sk = "sk-" + "b" * 24
    out = hook_jsonable({
        "note": f"用这个 token={gh} 再试一次",
        "header": f"Authorization: Bearer {sk}",
        "aws": "AKIAIOSFODNN7EXAMPLE",
        "plain": "这一条不该被动（没有凭证形状）",
    })
    check("★ `token=ghp_…` 这种（键名正常）被文本级脱敏抹掉", gh not in out["note"],
          out["note"])
    check("★ `Authorization: Bearer sk-…` 那一行也被抹掉", sk not in out["header"], out["header"])
    check("AWS access key 形状也被认出来", "AKIAIOSFODNN7EXAMPLE" not in out["aws"], str(out["aws"]))
    check("没有凭证形状的普通文本**不许**被动（脱敏是定点替换，不是整份打码）",
          out["plain"] == "这一条不该被动（没有凭证形状）", out["plain"])


def test_string_and_sequence_limits() -> None:
    """② JSON 化截断：超长串 / 超深嵌套 / 超长序列（逐字对齐真实侧阈值）。"""
    check("阈值就是真实侧那三个默认：depth 8 / string 8000 / sequence 200",
          (obs_mod.JSONABLE_MAX_DEPTH, JSONABLE_MAX_STRING, JSONABLE_MAX_SEQUENCE) == (8, 8000, 200))

    over = JSONABLE_MAX_STRING + 500
    out = hook_jsonable({"s": "x" * over})
    expected_tail = f"...[truncated {over - JSONABLE_MAX_STRING} chars]"
    check("★ 超长串截到 8000 并**注明丢了几个字符**（标记逐字同真实侧）",
          out["s"] == "x" * JSONABLE_MAX_STRING + expected_tail, out["s"][-60:])
    check("短串原样（连一个字符都不动）", hook_jsonable({"s": "短串"})["s"] == "短串")
    check("bytes → `<N bytes>`（不把二进制塞进 JSON）", hook_jsonable({"b": b"12345"})["b"] == "<5 bytes>")

    deep: object = {"leaf": 1}
    for _ in range(12):
        deep = {"n": deep}
    dumped = json.dumps(hook_jsonable(deep), ensure_ascii=False)
    check("★ 嵌套超过 8 层 ⇒ `<dict depth limit>`（不递归爆栈）",
          "<dict depth limit>" in dumped, dumped[:120])


def test_container_overflow_semantics_differ_on_purpose() -> None:
    """★ dict 与 list 的溢出行为**故意不同**（照抄真实侧，不许"统一"成一种）：

      · list：`seq[:200]` **切片** + 末尾**追加**一条 `{"_truncated_items": 剩余数}`；
      · dict：边遍历边判，越界就 **`break`**（第 201 个键**之后**的根本没被访问），
        标记以**普通键** `_truncated_items` 塞进同一个 dict。

    下面用一个 `items()` 会计数的 dict 子类把"**提前 break**"这件事变成可断言的：
    若哪天有人把它改成"先全量构造再切片"，计数会从 201 跳到 300。
    """
    seq = hook_jsonable(list(range(JSONABLE_MAX_SEQUENCE + 100)))
    check("★ list 溢出：**保留前 200 个**，再**追加**一条 `_truncated_items`（剩余数）",
          len(seq) == JSONABLE_MAX_SEQUENCE + 1 and seq[:3] == [0, 1, 2]
          and seq[JSONABLE_MAX_SEQUENCE - 1] == JSONABLE_MAX_SEQUENCE - 1
          and seq[-1] == {"_truncated_items": 100}, str(seq[-3:]))
    check("...没到上限的序列不加标记（`== 200` 也不算溢出）",
          len(hook_jsonable(list(range(JSONABLE_MAX_SEQUENCE)))) == JSONABLE_MAX_SEQUENCE)

    class _CountingDict(dict):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.iterated = 0

        def items(self):
            for k, v in super().items():
                self.iterated += 1          # ← 每被访问一个键就 +1
                yield k, v

    counted = _CountingDict({f"k{i}": i for i in range(JSONABLE_MAX_SEQUENCE + 100)})
    dct = hook_jsonable(counted)
    check("★ dict 溢出：**break**（后面的键全丢）+ 一条 `_truncated_items`（剩余数）",
          len(dct) == JSONABLE_MAX_SEQUENCE + 1 and dct["_truncated_items"] == 100
          and "k199" in dct and "k250" not in dct and "k299" not in dct, str(sorted(dct)[-3:]))
    check("★★ 那个 `break` 是**真的提前停止**（只访问了 201 个键，不是全量遍历再切）",
          counted.iterated == JSONABLE_MAX_SEQUENCE + 1, str(counted.iterated))
    check("...两种容器的溢出**载体不同**：list 的标记是末尾元素，dict 的标记是普通键",
          seq[-1] == {"_truncated_items": 100}
          and dct["_truncated_items"] == 100 and len(seq) == len(dct) == 201)
    check("tuple / set 走 list 那条路，且出参统一是 list",
          hook_jsonable((1, 2, 3)) == [1, 2, 3] and isinstance(hook_jsonable({1, 2}), list))


def test_total_char_cap_degrades_then_previews() -> None:
    """③ 总字符上限：**全量 → 降级 caps → 截断预览**；env 可覆盖、下限 1000、坏值回落。"""
    check("默认上限 50000（env 没设时）", payload_max_chars() == 50000,
          str(payload_max_chars()))
    check("默认上限下不降级（小载荷原样返回）",
          sanitize_payload({"a": 1}) == {"a": 1})

    with env_var(PAYLOAD_MAX_CHARS_ENV, "1000"):
        check("★ env 覆盖生效", payload_max_chars() == 1000)
        check("★ 下限 1000：写 `5` 也被抬到 1000（照 `max(1000, int(raw))`）",
              payload_max_chars() == 1000)
        degraded = sanitize_payload({"items": [f"item{i}" for i in range(300)]})
        check("★ 超限第一级：**降级 caps**（max_string=1000 / max_sequence=50）后返回，不是丢弃",
              isinstance(degraded, dict) and "_truncated" not in degraded
              and len(degraded["items"]) == 51 and degraded["items"][-1] == {"_truncated_items": 250},
              str(degraded)[:160])
        preview = sanitize_payload({"big": "y" * 5000})
        check("★ 还超 ⇒ 换成 `{\"_truncated\": True, \"original_type\", \"preview\"[:limit]}`",
              preview.get("_truncated") is True and preview.get("original_type") == "dict"
              and len(preview["preview"]) == 1000, str(preview)[:160])
        check("...preview 是 **JSON 前缀**（能读，不是空载荷）",
              preview["preview"].startswith('{"big": "yyy'), preview["preview"][:40])

    with env_var(PAYLOAD_MAX_CHARS_ENV, "abc"):
        check("★ 坏值回落默认 50000（不抛、不变成 0）", payload_max_chars() == 50000)
    with env_var(PAYLOAD_MAX_CHARS_ENV, ""):
        check("空串也回落默认（`int('')` 抛 ValueError → 兜住）", payload_max_chars() == 50000)
    check("★ env 已被还回去（`env_var()` 摘/还，不留残留）",
          PAYLOAD_MAX_CHARS_ENV not in os.environ, str(os.environ.get(PAYLOAD_MAX_CHARS_ENV)))


def test_env_override_in_a_clean_child_process() -> None:
    """★ 第三条隔离：**子进程 + 干净 env** —— 证明这个开关只靠环境变量本身。

    （不依赖"本进程已经 import 过什么"、不依赖 pytest 的 monkeypatch 环境。）
    """
    code = (
        "import sys; sys.path.insert(0, %r); "
        "from agent.observer import payload_max_chars as f; print(f())" % ROOT
    )

    def run(extra: dict) -> str:
        env = {"MINI_HOME": HOME, "PATH": os.environ.get("PATH", ""),
               "PYTHONIOENCODING": "utf-8"}
        env.update(extra)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           timeout=180, env=env)
        return (r.stdout + r.stderr).strip()

    check("子进程（干净 env）里 env 覆盖同样生效",
          run({PAYLOAD_MAX_CHARS_ENV: "1234"}) == "1234", run({PAYLOAD_MAX_CHARS_ENV: "1234"}))
    check("...下限 1000 在子进程里也成立",
          run({PAYLOAD_MAX_CHARS_ENV: "0"}) == "1000", run({PAYLOAD_MAX_CHARS_ENV: "0"}))
    check("...没设 env 时是默认 50000",
          run({}) == "50000", run({}))


# ══════════════════════════════════════════════════════════════════════════════ #
# 5. 关联 id
# ══════════════════════════════════════════════════════════════════════════════ #
def test_call_ids_wire_contract() -> None:
    """5 个字段 + `hook_kwargs()` 的 **None → ""**（真实侧 `model_tools.py:643-653` 同形）。"""
    ids = CallIds().hook_kwargs()
    check("5 个字段齐：task / session / tool_call / turn / api_request",
          set(ids) == {"task_id", "session_id", "tool_call_id", "turn_id", "api_request_id"},
          str(sorted(ids)))
    check("★ 全 None ⇒ 全是 `\"\"`（线协议：没有这个 id 就是空串，不是 None）",
          all(v == "" for v in ids.values()), str(ids))
    check("部分有值：有的原样、缺的仍然是空串",
          CallIds(task_id="t", turn_id="u").hook_kwargs()
          == {"task_id": "t", "session_id": "", "tool_call_id": "", "turn_id": "u",
              "api_request_id": ""},
          str(CallIds(task_id="t", turn_id="u").hook_kwargs()))
    check("是**冻结**的 dataclass（观察者改不动载荷里的 id）",
          CallIds.__dataclass_params__.frozen is True)  # type: ignore[attr-defined]


def test_turn_ids_are_per_turn_and_shared_within_a_turn() -> None:
    """`turn_id` 每轮一个：同轮所有事件相同、跨轮必然不同。"""
    reg = ObserverRegistry()
    seen: list = []
    for ev in OBSERVER_EVENTS:
        reg.add(ev, (lambda e: (lambda **kw: seen.append(kw)))(ev))
    ag = make_agent(reg)
    drive_one_tool_turn(ag)
    first = list(seen)
    seen.clear()
    drive_one_tool_turn(ag)
    second = list(seen)

    check("一轮里 7 个事件都发了（两轮共 20 发：10 + 10）",
          len(first) == 10 and len(second) == 10, f"{len(first)} / {len(second)}")
    check("★ 同轮内所有事件的 `turn_id` **完全相同**",
          len({e["turn_id"] for e in first}) == 1 and bool(first[0]["turn_id"]),
          str({e["turn_id"] for e in first}))
    check("★ 跨轮 `turn_id` **不同**",
          {e["turn_id"] for e in first}.isdisjoint({e["turn_id"] for e in second}))
    check("`turn_id` 里带 session_id 与 task_id（能反查这一轮是谁的）",
          all(e["turn_id"].startswith(f"{ag.session_id}:") for e in first)
          and all(f":{e['task_id']}:" in e["turn_id"] for e in first),
          str(first[0]["turn_id"]))
    check("`session_id` 跨轮**相同**（会话级身份，不是轮级的）",
          {e["session_id"] for e in first} == {e["session_id"] for e in second} == {ag.session_id})
    check("`task_id` 每轮不同（mini 的 task_id 本来就是\"每回合 id\"）",
          {e["task_id"] for e in first}.isdisjoint({e["task_id"] for e in second}))
    check("★ 每个 API 请求一个 `api_request_id`（一轮 2 次请求 ⇒ 2 个不同的 id）",
          len({e["api_request_id"] for e in first if e["api_request_id"]}) == 2,
          str([e.get("api_request_id") for e in first]))
    check("...id 形状是 `{turn_id}:api:{序号}`（真实侧 `conversation_loop.py:1536` 同形）",
          all(e["api_request_id"].startswith(e["turn_id"] + ":api:") for e in first
              if e["api_request_id"]))
    check("★ `tool_call_id` 来自 **provider**（响应里那个 `call_prov_1` 原样进载荷）",
          [e["tool_call_id"] for e in first if e["event"] in ("pre_tool_call", "post_tool_call")]
          == ["call_prov_1", "call_prov_1"], str([e.get("tool_call_id") for e in first]))
    check("同一轮里 `pre_tool_call` 与 `post_tool_call` 带**同一个** tool_call_id（能配上对）",
          len({e["tool_call_id"] for e in first if e["event"].endswith("tool_call")}) == 1)


def test_api_request_id_is_per_http_request_including_fallback_resend() -> None:
    """回退重发 = **一次新的请求**（新 `api_request_id`、`attempt` 递增）。"""
    reg = ObserverRegistry()
    pre = record_events(reg, "pre_api_request")
    err = record_events(reg, "api_request_error")
    ag = make_agent(reg, fallback_providers=[dict(FALLBACK_ENTRY)])
    providers: list = []

    def post(cand, msgs):
        providers.append(cand["provider"])
        if len(providers) == 1:
            raise ApiHTTPError(429, "rate limited", "http://x")
        return text_response("ok")

    ag._http_post = post
    ag._post_chat_completions([{"role": "user", "content": "hi"}], "t")

    check("回退确实重发了一次（主 + 回退各一次）", providers == ["fake", "openai"], str(providers))
    check("★ 两次请求**两个不同的** `api_request_id`（重发是新请求）",
          len({e["api_request_id"] for e in pre}) == 2, str([e["api_request_id"] for e in pre]))
    check("★ `attempt` 递增 1 → 2", [e["attempt"] for e in pre] == [1, 2], str([e["attempt"] for e in pre]))
    check("...重发那一发在载荷里能看到**换了谁**（provider/model 已原地变）",
          pre[1]["provider"] == "openai" and pre[1]["model"] == "gpt-4o-mini", str(pre[1]))
    check("★ 抛错那次发了 `api_request_error`：`error.type` / `message` / `status_code`",
          len(err) == 1 and err[0]["error"]["type"] == "ApiHTTPError"
          and "rate limited" in err[0]["error"]["message"] and err[0]["status_code"] == 429,
          str(err))
    check("...`retryable=True`（429 会被回退链接手）", err[0]["retryable"] is True)
    check("...失败那发**没有** `post_api_request`（成对关系没被破坏）",
          not [e for e in pre if e["attempt"] == 1 and e.get("status")])


def test_deterministic_error_is_not_retryable_in_the_payload() -> None:
    """400/422 这种确定性错误：`retryable=False`（换谁都一样）。"""
    reg = ObserverRegistry()
    err = record_events(reg, "api_request_error")
    ag = make_agent(reg, fallback_providers=[dict(FALLBACK_ENTRY)])

    def post(cand, msgs):
        raise ApiHTTPError(422, "bad request", "http://x")

    ag._http_post = post
    try:
        ag._post_chat_completions([{"role": "user", "content": "hi"}], "t")
    except ApiHTTPError:
        pass
    check("★ 422 那发 `retryable=False`（确定性错误不进回退链）",
          len(err) == 1 and err[0]["retryable"] is False and err[0]["status_code"] == 422, str(err))
    check("...只发了一次（没有重发 ⇒ 只有一个 api_request_id）", len(err) == 1)


# ══════════════════════════════════════════════════════════════════════════════ #
# 6. 事件触发点 ★（假 provider 驱动真 agent）
# ══════════════════════════════════════════════════════════════════════════════ #
def test_full_turn_fires_the_seven_events_in_order() -> None:
    """★★ 一轮 + 一次工具调用 ⇒ 7 个事件**按序**发出，字段齐。

    期望序列（一轮 = 2 次 LLM 调用 / 2 次请求 / 1 次工具）：
        [pre_llm_call, pre_api_request, post_api_request, post_llm_call,
         pre_tool_call, post_tool_call]                    ← 迭代 1（发起了工具）
        [pre_llm_call, pre_api_request, post_api_request, post_llm_call]   ← 迭代 2（收尾）
    """
    reg = ObserverRegistry()
    seen: list = []
    for ev in OBSERVER_EVENTS:
        reg.add(ev, (lambda e: (lambda **kw: seen.append(kw)))(ev))
    name = "_test_obs_echo_order"
    hits = register_echo(name)
    try:
        ag = make_agent(reg)
        result = drive_one_tool_turn(ag, tool_name=name, args='{"x": 1}')
    finally:
        model_tools.registry.deregister(name)

    order = [e["event"] for e in seen]
    expected = ["pre_llm_call", "pre_api_request", "post_api_request", "post_llm_call",
                "pre_tool_call", "post_tool_call",
                "pre_llm_call", "pre_api_request", "post_api_request", "post_llm_call"]
    check("★ 10 发、**逐条按序**（先 pre 后 post，工具在两次 LLM 调用之间）",
          order == expected, str(order))
    check("一轮正常完成会发的 6 个事件**全部**出现过（`api_request_error` 只在出错那一路发，"
          "由本组最后一条用例与第 5 组分别覆盖）",
          set(order) == set(OBSERVER_EVENTS) - {"api_request_error"}, str(sorted(set(order))))
    check("工具**真的执行了**（观察者观测到的是一次真调用）", len(hits) == 1, str(hits))

    by_event: dict = {}
    for e in seen:                       # 每个事件取**第一次**那一发（迭代 1）
        by_event.setdefault(e["event"], e)
    common_ids = {"task_id", "session_id", "tool_call_id", "turn_id", "api_request_id"}
    check("每份载荷都带**版本字段**（唯一出口注入，值 = mini.observer.v1）",
          all(e.get(TELEMETRY_SCHEMA_VERSION_KEY) == TELEMETRY_SCHEMA_VERSION for e in seen),
          str([e.get(TELEMETRY_SCHEMA_VERSION_KEY) for e in seen]))
    check("★ 版本字段与 `event` 一样**只在唯一出口注入**"
          "（`run_agent.py` 的载荷构造里一个字都没写）",
          "telemetry_schema_version" not in inspect.getsource(AIAgent))
    check("每份载荷都带 5 个关联 id",
          all(common_ids <= set(e) for e in seen), str([sorted(set(e)) for e in seen][:2]))
    check("`pre_llm_call` 字段齐（iteration / model / provider / platform / message_count）",
          {"iteration", "model", "provider", "platform", "message_count"} <= set(by_event["pre_llm_call"])
          and by_event["pre_llm_call"]["model"] == "fake-model",
          str(sorted(by_event["pre_llm_call"])))
    check("`post_llm_call`：`status=ok`、`tool_call_count=1`（这一发确实要了工具）、"
          "`duration_ms` 是个非负数",
          by_event["post_llm_call"]["status"] == "ok"
          and by_event["post_llm_call"]["tool_call_count"] == 1
          and isinstance(by_event["post_llm_call"]["duration_ms"], int)
          and by_event["post_llm_call"]["duration_ms"] >= 0,
          str(by_event["post_llm_call"]))
    check("`pre_api_request`：`attempt=1`、`api_mode`、`message_count` 来自**真请求**",
          by_event["pre_api_request"]["attempt"] == 1
          and by_event["pre_api_request"]["api_mode"] == "chat_completions"
          and by_event["pre_api_request"]["message_count"] >= 2,
          str(by_event["pre_api_request"]))
    check("`post_api_request`：`status=ok` + **provider 报的 usage**（token 桶）",
          by_event["post_api_request"]["status"] == "ok"
          and by_event["post_api_request"]["usage"]["total_tokens"] == 15,
          str(by_event["post_api_request"]))
    check("`pre_tool_call`：`tool_name` / `args` 与那次调用逐字一致",
          by_event["pre_tool_call"]["tool_name"] == name
          and by_event["pre_tool_call"]["args"] == {"x": 1},
          str(by_event["pre_tool_call"]))
    check("`post_tool_call`：`status=ok` + 工具**真实返回值**",
          by_event["post_tool_call"]["status"] == "ok"
          and '"ok": true' in str(by_event["post_tool_call"]["result"]).replace("'", '"'),
          str(by_event["post_tool_call"])[:120])
    check("事件发完**不影响轮次结果**（最终回答与迭代数都没变）",
          result["final_response"] == "完成" and result["iterations_used"] == 2)


def test_failed_llm_call_still_fires_post_llm_call() -> None:
    """★ 失败的模型调用**也要**发 `post_llm_call`（用 `status` 区分）—— 放在 finally 里正是为此。"""
    reg = ObserverRegistry()
    pre = record_events(reg, "pre_llm_call")
    post = record_events(reg, "post_llm_call")
    err = record_events(reg, "api_request_error")
    ag = make_agent(reg)

    def boom(cand, msgs):
        raise ApiHTTPError(400, "bad request", "http://x")

    ag._http_post = boom
    result = ag.run_conversation("你好")

    check("★ 失败那发 `post_llm_call` 照发（`status=error`）",
          [e["status"] for e in post] == ["error"], str([e["status"] for e in post]))
    check("...`pre_llm_call` 与它成对（各一发）", len(pre) == 1 and len(post) == 1)
    check("...`api_request_error` 也发了（HTTP 层那一发）", len(err) == 1, str(err))
    check("异常**没有被观察者吞掉**：轮次照旧把它兜成 final_response",
          "ApiHTTPError" in result["final_response"], result["final_response"])
    check("失败轮次里 `post_llm_call` 仍带完整 id 与版本字段（不只是个空壳）",
          post[0]["turn_id"] and post[0][TELEMETRY_SCHEMA_VERSION_KEY] == TELEMETRY_SCHEMA_VERSION)


def test_tool_outcomes_are_distinguishable_by_status() -> None:
    """★ 工具三种结局都用 `status` 区分：`ok` / `denied` / `error`（解析失败与工具抛异常）。"""
    danger = "_test_obs_danger"
    boom_tool = "_test_obs_boom"
    model_tools.registry.register_tool(
        danger, description="需要审批的测试工具", parameters={}, toolset="test",
        requires_approval=True)(lambda **kw: "不该跑到这里")

    def explode(**kw):
        raise ValueError("工具自己炸了")

    model_tools.registry.register_tool(
        boom_tool, description="会抛异常的测试工具", parameters={}, toolset="test")(explode)
    try:
        # ① denied：审批回调拒了
        reg1 = ObserverRegistry()
        post1 = record_events(reg1, "post_tool_call")
        pre1 = record_events(reg1, "pre_tool_call")
        ag1 = make_agent(reg1, approval_callback=lambda shown: False)
        res1 = drive_one_tool_turn(ag1, tool_name=danger, args='{"command": "rm -rf /"}')
        check("★ 审批被拒 ⇒ `status=denied`，且 `pre_tool_call` **也**发了（三路都发）",
              [e["status"] for e in post1] == ["denied"] and len(pre1) == 1, str(post1))
        check("...被拒的结果就是那句 `[denied]`（观察者没改它）",
              str(post1[0]["result"]).startswith("[denied]"), str(post1[0]["result"])[:60])
        check("...轮次照常收尾", res1["final_response"] == "完成", str(res1["final_response"]))

        # ② error（参数不是合法 JSON）
        reg2 = ObserverRegistry()
        post2 = record_events(reg2, "post_tool_call")
        ag2 = make_agent(reg2)
        drive_one_tool_turn(ag2, tool_name="todo_list", args="{不是 JSON")
        check("★ 参数解析失败 ⇒ `status=error` + `error_type=parse_error`",
              [e["status"] for e in post2] == ["error"]
              and post2[0]["error_type"] == "parse_error", str(post2))
        check("...`error_message` 也在（不只是个状态码）", bool(post2[0]["error_message"]))

        # ③ error（工具自己失败 —— mini 的工具运行时把异常转成 `tool_error` JSON 结果，
        #    所以观察者**从结果里**看出失败：`error_type=tool_error`，照真实侧判据）
        reg3 = ObserverRegistry()
        post3 = record_events(reg3, "post_tool_call")
        ag3 = make_agent(reg3)
        res3 = drive_one_tool_turn(ag3, tool_name=boom_tool, args="{}")
        check("★ 工具自己失败了 ⇒ 也发 `status=error` + `error_type=tool_error`"
              "（判据来自**结果**，不是异常）",
              [e["status"] for e in post3] == ["error"]
              and post3[0]["error_type"] == "tool_error", str(post3))
        check("...`error_message` 带着工具失败的原话（ValueError）",
              "ValueError" in str(post3[0]["error_message"]), str(post3[0]["error_message"]))
        check("...工具失败**不炸轮次**：模型收到错误结果后照常给最终回答",
              res3["final_response"] == "完成", str(res3["final_response"]))
        check("...错误结果原样进了历史（观察者没改动它）",
              any("ValueError" in str(m.get("content")) for m in res3["messages"]
                  if m.get("role") == "tool"))

        # ④ ok：正常返回
        reg4 = ObserverRegistry()
        post4 = record_events(reg4, "post_tool_call")
        ag4 = make_agent(reg4)
        drive_one_tool_turn(ag4, tool_name="todo_list", args='{"todos": [], "merge": false}')
        check("★ 正常返回 ⇒ `status=ok` 且 `error_type` 为空（三个结局互不混淆）",
              [e["status"] for e in post4] == ["ok"] and post4[0]["error_type"] is None, str(post4))

        # ⑤ 「结果之外」的意外（审批回调自己炸了）：先给观察者一个结局，再把异常原样穿出去
        reg5 = ObserverRegistry()
        post5 = record_events(reg5, "post_tool_call")

        def explode_approval(shown):
            raise RuntimeError("审批弹窗坏了")

        ag5 = make_agent(reg5, approval_callback=explode_approval)
        res5 = drive_one_tool_turn(ag5, tool_name=danger, args='{"command": "rm -rf /"}')
        check("★ 审批回调抛异常 ⇒ 也先发一发 `status=error`（`error_type` 是异常类名）",
              [e["status"] for e in post5] == ["error"]
              and post5[0]["error_type"] == "RuntimeError", str(post5))
        check("...然后异常**原样穿出去**（被轮次兜成 final_response，没被观察者吞掉）",
              "RuntimeError" in res5["final_response"], str(res5["final_response"]))
    finally:
        model_tools.registry.deregister(danger)
        model_tools.registry.deregister(boom_tool)


# ══════════════════════════════════════════════════════════════════════════════ #
GROUPS = [
    ("1. 注册契约",
     "未知事件名**只警告仍存**（警告带全部合法名）· **不去重**（登记两次调两次）· **保序** · "
     "只读查询 · `has()` 是唯一事实源 · 默认挂进程级 OBSERVERS",
     [test_unknown_event_warns_but_is_still_stored,
      test_no_dedup_and_registration_order,
      test_read_only_queries_are_the_single_source_of_truth]),
    ("2. 零成本快路径 ★",
     "**0 观察者 ⇒ 载荷构造 0 次**（不是构造完再丢掉）· 有观察者 ⇒ 恰好 1 次（按事件开）· "
     "`emit` 无订阅时连 sanitize 都不跑",
     [test_zero_cost_fast_path_builds_no_payload,
      test_one_observer_builds_each_payload_at_most_once_per_event,
      test_emit_returns_immediately_when_nobody_subscribes]),
    ("3. 失败放开",
     "坏回调只丢自己（前后照跑）· 轮次与工具执行不受影响 · 有警告（事件名 + 异常摘要）· "
     "**返回值一律忽略**（不是第二套控制流）",
     [test_bad_callback_cannot_break_the_turn,
      test_observer_return_values_are_ignored]),
    ("4. 载荷安全（键级 + 文本级 + 三级截断 + 总量上限）",
     "敏感键命中 `<redacted>` · 值里的凭证也被文本级脱敏 · 超长串带 `truncated N chars` · "
     "深度 8 · 序列 200 · **dict/list 溢出语义故意不同** · 总量上限两级降级 + env（下限 1000 / 坏值回落 / 子进程）",
     [test_sensitive_keys_are_redacted,
      test_text_level_redaction_catches_secrets_inside_values,
      test_string_and_sequence_limits,
      test_container_overflow_semantics_differ_on_purpose,
      test_total_char_cap_degrades_then_previews,
      test_env_override_in_a_clean_child_process]),
    ("5. 关联 id",
     "5 字段齐 · **None → \"\"** · 同轮 `turn_id` 相同 / 跨轮不同 · 每请求一个 `api_request_id`"
     "（含回退重发、`attempt` 递增）· `tool_call_id` 来自 provider · 确定性错误 `retryable=False`",
     [test_call_ids_wire_contract,
      test_turn_ids_are_per_turn_and_shared_within_a_turn,
      test_api_request_id_is_per_http_request_including_fallback_resend,
      test_deterministic_error_is_not_retryable_in_the_payload]),
    ("6. 事件触发点 ★（假 provider 驱动真 agent）",
     "一轮 + 一次工具调用 ⇒ 7 个事件**按序**发出（10 发）· 字段齐 · 版本字段只在唯一出口注入 · "
     "失败也发 `post_llm_call` · 工具三结局用 `status` 区分（ok/denied/error）",
     [test_full_turn_fires_the_seven_events_in_order,
      test_failed_llm_call_still_fires_post_llm_call,
      test_tool_outcomes_are_distinguishable_by_status]),
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


if __name__ == "__main__":
    # ★ 跑完清掉临时 MINI_HOME（用 finally 保证中途抛异常也不留垃圾）。
    try:
        main()
    finally:
        shutil.rmtree(_TEST_HOME, ignore_errors=True)
    sys.exit(1 if FAIL else 0)
