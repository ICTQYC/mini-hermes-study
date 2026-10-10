"""第 7 章的自测：网关（会话键 / 两级守卫 / 授权 / 命令表 / 交付 / 钩子 / 进程身份 / profile 作用域）—— **不联网**。

跑法：python3 tests/test_gateway.py

## 测的是什么（12 组）

| 组 | 守的承诺 |
|---|---|
| 1. 会话键 | **唯一来源** `build_session_key` · 布局 · `main~`（~ 不在 profile id 字母表里）· 无 chat_id 时退到发送者 |
| 2. 授权 | 四层顺序 · **默认拒绝**（fail-closed）· 拒绝时**说得出理由** |
| 3. 命令解析 | 规范名 / 别名 / **唯一**前缀（歧义不猜）· 不是命令就当普通消息 |
| 4. 第 1 级守卫 | 忙时普通消息 → 排队（不丢、不并发）· 命令 → 内联 · **陈旧锁自愈** |
| 5. 第 2 级守卫 + 忙时策略 | reject 会把话说出口 · dispatch 能穿过 · interrupt_then_dispatch 先打断 |
| 6. 忙时处置优先于分发 | `busy_policy=reject` 的命令在 agent 跑着时**不许执行** |
| 7. 队列消费 | 一轮结束后**先放锁再 drain**（否则队列卡死且不报错） |
| 8. 会话归属 | 会话键落库 · 按键找回 · `/new` 之后**找回的是新会话** |
| 9. 交付 | 直接回复回来源 · 指定目标要 `platform:chat_id` · 没有适配器就报错 |
| 10. 钩子 | 目录发现 · 事件分发 · 坏钩子不影响别人 · **路径在调用时解析** |
| 11. 进程身份 | PID + **启动时间指纹**（防号被回收）· 状态记录 · 全 token 匹配 · 退出码 78 |
| 12. profile 作用域 | contextvar 改 home · 密钥作用域**不改 os.environ** · 多路复用 fail-closed · **A→B→A** |
| 13. profile 枚举 | profile 根 **HOME 锚定** · `profiles_to_serve` · status 文本 |
| 14. profile 入口层（★ 侧线） | `create` 建什么/播种什么 · id 字母表（**`~` 必须被拒**）· 重名 · `--clone` 带配置**不带记忆** · 解析**不静默回落** · 两 profile **物理分家** · 真 CLI 子进程 |

## 隔离

每条用例都在自己的临时 `MINI_HOME` 下跑，跑完还原环境变量与被 monkeypatch 的模块属性。
**没有一条依赖运行环境的配置（密钥 / 已存会话 / 网关进程），也没有一条会碰用户的真实 home。**
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f"　[{detail}]" if detail and not cond else ""))


@contextmanager
def temp_home(prefix: str = "mh-gw-test-"):
    """临时 MINI_HOME（**import 之后**也能生效：所有路径都解析在调用时）。"""
    prev = os.environ.get("MINI_HOME")
    home = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["MINI_HOME"] = str(home)
    try:
        yield home
    finally:
        if prev is None:
            os.environ.pop("MINI_HOME", None)
        else:
            os.environ["MINI_HOME"] = prev
        shutil.rmtree(home, ignore_errors=True)


@contextmanager
def env(**kv):
    """临时环境变量（None = 删掉）。"""
    prev = {k: os.environ.get(k) for k in kv}
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def fake_agent(gate: threading.Event | None = None, calls: list | None = None):
    """把 AIAgent 换成假的（不联网）。gate 设了就在 run_conversation 里阻塞。

    `calls` 给了就把每条消息的文本**按顺序**记下来（第 8 章测队列顺序要用：
    `_last_results` 只留最后一条，看不出"先跑哪个后跑哪个"）。
    """
    import run_agent

    class _Fake:
        def __init__(self, spec, **kw):
            self.kw = kw
            self.interrupted = False

        def run_conversation(self, text, **kw):
            if calls is not None:
                calls.append(text)
            if gate is not None:
                gate.wait(timeout=8)
            if self.interrupted:
                return {"final_response": "", "interrupted": True,
                        "usage": {}, "iterations_used": 0}
            return {"final_response": f"(stub) {text}",
                    # ★ 用**真的键名**（AIAgent 内部就是这两个）：这样这条用例
                    #   守的就是"prompt_tokens → input_tokens"这个映射，
                    #   而不是"随便什么键都能填进去"。
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                    "iterations_used": 1, "messages": []}

        def interrupt(self):
            self.interrupted = True

    original = run_agent.AIAgent
    run_agent.AIAgent = _Fake
    try:
        yield _Fake
    finally:
        run_agent.AIAgent = original


@contextmanager
def temp_profiles_home():
    """钉住 `Path.home`（**profile 根是 HOME 锚定的**）—— 不钉就会写用户真实 home。

    ★ 这条教训来自第 7 章：`profiles_root()` 故意不看 `MINI_HOME`，所以只设
    `MINI_HOME` 的测试**仍然会写** `~/.mini-hermes/profiles/...`。
    真实版仓库有同一条规矩（profile 测试要连 `Path.home` 一起 mock）。
    yield 出默认 profile 的 home（`<临时>/.mini-hermes`）。
    """
    import pathlib

    real_home = pathlib.Path.home
    with temp_home() as mini_home:
        fake_root = mini_home / "fakehome"
        (fake_root / ".mini-hermes").mkdir(parents=True)
        pathlib.Path.home = staticmethod(lambda: fake_root)
        try:
            yield fake_root / ".mini-hermes"
        finally:
            pathlib.Path.home = real_home


def _runner(home: Path, **kw):
    """建一个已启动的网关（默认全放行，方便测别的机制）。"""
    from gateway.run import GatewayRunner
    from gateway.authz import GatewayAuthz
    from gateway.session_keys import SessionSource, build_session_key

    runner = GatewayRunner(home=home, echo=False, **kw)
    runner.authz = GatewayAuthz(platform_allow_all=True)
    assert runner.start() == 0
    src = SessionSource(platform="console", chat_id="console-1", user_id="local")
    return runner, src, build_session_key(src)


def _feed(runner, text, key, timeout=20.0):
    """喂一条并等它跑完 → (适配器判定, runner 结论)。"""
    adapter = runner.adapters["console"]
    verdict = runner.feed(text)
    adapter.wait_idle(key, timeout=timeout)
    return verdict, adapter.last_result(key)


# ══════════════════════════════════════════════════════════════════════════════ #
def test_session_key_is_single_source():
    from gateway.session_keys import SessionSource, build_session_key, session_key_namespace

    src = SessionSource(platform="console", chat_id="c1", chat_type="dm", user_id="u1")
    check("布局 = <ns>:<platform>:<chat_type>:<chat_id>",
          build_session_key(src) == "main:console:dm:c1")
    check("thread_id 追加在最后",
          build_session_key(SessionSource(platform="t", chat_id="c", thread_id="th"))
          == "main:t:dm:c:th")
    check("没有 chat_id 时退到发送者 id（否则所有无 chat_id 的 DM 共享一个 agent）",
          build_session_key(SessionSource(platform="t", chat_id="", user_id="u9"))
          == "main:t:dm:u9")
    check("无 chat_id 也无 user_id 时退到每平台一个",
          build_session_key(SessionSource(platform="t", chat_id="", user_id=""))
          == "main:t:dm")


def test_namespace_and_main_tilde():
    from gateway.session_keys import session_key_namespace

    check("默认 profile → main（与历史键逐字节一致）", session_key_namespace(None) == "main")
    check("'default' → main", session_key_namespace("default") == "main")
    check("具名 profile → 它的名字", session_key_namespace("work") == "work")
    check("★ 字面叫 main 的 profile → main~（否则与默认 profile 共享一切）",
          session_key_namespace("main") == "main~")
    check("~ 不在 profile id 的字母表里 → 这个标记不可能撞上真 profile 名",
          "~" not in "abcdefghijklmnopqrstuvwxyz0123456789-_")


def test_authz_layers_and_default_deny():
    from gateway.authz import GatewayAuthz
    from gateway.session_keys import SessionSource

    src = SessionSource(platform="console", chat_id="c", user_id="u1")
    check("★ 默认拒绝（fail-closed）：什么都不配 → 拒",
          GatewayAuthz().is_authorized(src) is False)

    a = GatewayAuthz(platform_allowlist=frozenset({"u1"}))
    check("平台白名单命中 → 放行", a.is_authorized(src) is True)
    check("不在白名单 → 拒",
          a.is_authorized(SessionSource(platform="console", chat_id="c", user_id="u2")) is False)

    check("平台全放行 → 全过（压过白名单）",
          GatewayAuthz(platform_allow_all=True).is_authorized(
              SessionSource(platform="console", chat_id="c", user_id="zzz")) is True)
    check("全局全放行 → 过", GatewayAuthz(global_allow_all=True).is_authorized(src) is True)

    with env(CONSOLE_ALLOWED_USERS="u1,u2", CONSOLE_ALLOW_ALL_USERS=None):
        g = GatewayAuthz.from_env("console")
        check("from_env 读 <PLATFORM>_ALLOWED_USERS（逗号分隔）",
              g.is_authorized(src) and g.is_authorized(
                  SessionSource(platform="console", chat_id="c", user_id="u2")))
        check("不在名单里的仍然拒",
              g.is_authorized(SessionSource(platform="console", chat_id="c", user_id="u3")) is False)

    msg = GatewayAuthz().explain(src)
    check("拒绝时**说得出理由与补救方法**（否则运维只能猜）",
          "默认拒绝" in msg and "CONSOLE_ALLOWED_USERS" in msg, msg)


def test_command_resolution():
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, busy_policy_for, resolve_command

    check("规范名", resolve_command("/status") == ("status", ""))
    check("带参数", resolve_command("/new 额外") == ("new", "额外"))
    check("别名", resolve_command("/reset") == ("new", ""))
    check("唯一前缀可以缩写", resolve_command("/he") == ("help", ""))
    check("★ 歧义前缀**不猜**（/s 可能是 stop/status/sessions）→ 当普通消息",
          resolve_command("/s") is None)
    check("不是命令 → None", resolve_command("你好") is None)
    check("空斜杠 → None", resolve_command("/") is None)
    check("命令表里有忙时策略（默认 reject）",
          busy_policy_for("sessions") == "reject" and busy_policy_for("stop") == "interrupt_then_dispatch"
          and busy_policy_for("status") == "dispatch")
    check("未知命令的策略按 reject（安全默认）", busy_policy_for("不存在") == "reject")
    check("已知命令集合非空且含 help", "help" in GATEWAY_KNOWN_COMMANDS)


def test_guard1_queue_and_inline():
    with temp_home() as home:
        gate = threading.Event()
        with fake_agent(gate):
            runner, src, key = _runner(home)
            adapter = runner.adapters["console"]

            t = threading.Thread(target=lambda: runner.feed("第一轮"), daemon=True)
            t.start()
            time.sleep(0.4)
            check("会话忙（第 1 级守卫按会话分锁）", adapter.is_busy(key))

            v = runner.feed("第二轮")
            check("普通消息 → 排队（不是丢掉、也不是并发）", v == "queued", v)
            check("排队计数正确", adapter.pending_count(key) == 1)
            check("★ 排队时通知 runner 打断旧回合（新消息不用等旧的跑完）",
                  key in runner._interrupted)

            v2 = runner.feed("/sessions")
            check("命令 → 内联交给第 2 级（第 1 级不判策略）", v2 == "inline", v2)

            gate.set()
            t.join(timeout=10)
            check("放行后回到空闲且队列被消费",
                  adapter.wait_idle(key, timeout=15) and adapter.pending_count(key) == 0)
            runner.shutdown()


def test_guard2_busy_policy():
    with temp_home() as home:
        gate = threading.Event()
        with fake_agent(gate):
            runner, src, key = _runner(home)
            adapter = runner.adapters["console"]
            t = threading.Thread(target=lambda: runner.feed("占住"), daemon=True)
            t.start()
            time.sleep(0.4)

            runner.feed("/sessions")            # policy = reject
            time.sleep(0.3)
            res = adapter.last_result(key)
            check("busy_policy=reject → 明确拒绝", res["action"] == "busy_rejected", res)
            check("拒绝**说了话**（否则用户以为命令生效了）",
                  "不能在回合中间执行" in adapter.outbox()[-1])
            check("拒绝被记账（可观测）", (key, "sessions") in runner._busy_rejections)

            runner.feed("/status")              # policy = dispatch
            time.sleep(0.3)
            check("busy_policy=dispatch → 照常执行",
                  adapter.last_result(key)["action"] == "status")

            runner.feed("/stop")                # policy = interrupt_then_dispatch
            time.sleep(0.3)
            check("busy_policy=interrupt_then_dispatch → 先打断再执行",
                  adapter.last_result(key)["action"] == "interrupted")

            gate.set()
            t.join(timeout=10)
            runner.shutdown()


def test_busy_check_precedes_command_dispatch():
    """★ 顺序不变量：忙时处置必须在命令分发**之前**。

    反例（曾经的写法）：先按命令表分发、再查忙不忙 → `/sessions` 这种
    reject 策略的命令会在 agent 跑着的时候照样执行。
    """
    with temp_home() as home:
        gate = threading.Event()
        with fake_agent(gate):
            runner, src, key = _runner(home)
            adapter = runner.adapters["console"]
            t = threading.Thread(target=lambda: runner.feed("占住"), daemon=True)
            t.start()
            time.sleep(0.4)
            before = adapter.wait_idle(key, timeout=0.2)   # 应该 False（还在忙）
            check("前置条件：会话确实在忙", before is False)
            runner.feed("/sessions")
            time.sleep(0.3)
            check("忙时 reject 命令**没有**执行", adapter.last_result(key)["action"] == "busy_rejected")
            gate.set()
            t.join(timeout=10)
            # ★ 等一下回合**真的**结束：t.join 只等到"后台任务已启动"，
            #   不代表这一轮跑完了（这是测并发时最容易写错的一处）。
            check("等回合结束", adapter.wait_idle(key, timeout=15))
            # 空闲之后同一条命令可以执行（说明拒绝是"时机"而不是"没实现"）
            _, res = _feed(runner, "/sessions", key)
            check("空闲后同一命令能执行（拒绝是时机，不是缺失）", res["action"] == "sessions", str(res))
            runner.shutdown()


def test_stale_guard_self_heals():
    """★ 陈旧锁自愈：owner 线程死了，锁不能永远挂着（否则会话永久卡死）。"""
    with temp_home() as home:
        with fake_agent():
            runner, src, key = _runner(home)
            adapter = runner.adapters["console"]

            dead = threading.Thread(target=lambda: None)
            dead.start()
            dead.join()
            adapter._active_sessions[key] = threading.Event()   # 装一把锁
            adapter._session_tasks[key] = dead                  # 但 owner 已经死了
            check("前置：锁挂着且 owner 已退出",
                  adapter.is_busy(key) and adapter._session_task_is_stale(key))

            v = runner.feed("/status")
            time.sleep(0.4)
            check("handle_message 发现陈旧 → 自愈放锁并正常处理", v == "started", v)
            check("自愈后跑出了结论", adapter.last_result(key)["action"] == "status")

            # 没有 owner 记录时**不算**陈旧（否则测试里手工装的守卫会被误判）
            adapter._active_sessions[key] = threading.Event()
            adapter._session_tasks.pop(key, None)
            check("没有 owner 记录 → 不算陈旧（不误伤手工装的守卫）",
                  adapter._session_task_is_stale(key) is False)
            adapter._release_session_guard(key)
            runner.shutdown()


def test_session_ownership_and_reset():
    with temp_home() as home:
        with fake_agent():
            runner, src, key = _runner(home)
            _, res = _feed(runner, "你好", key)
            sid = res["session_id"]
            row = runner._db().get_session(sid)
            check("会话行存了 session_key（**重启连续性的地基**）", row["session_key"] == key)
            check("source 标出平台来源", row["source"] == "gateway:console")
            check("用量被回填到会话行", (row["input_tokens"] or 0) > 0)
            check("按路由键能找回当前会话", runner._db().latest_session_id_for_key(key) == sid)

            _, res2 = _feed(runner, "/new", key)
            sid2 = res2["session_id"]
            check("/new 开了新会话", sid2 != sid, f"{sid} vs {sid2}")
            # ★ 第 8 章把原因串对齐真实版：`/new` 的结束原因是 `session_reset`
            #   （真实版 `_finish_route_transition(..., end_reason=decision.reset_reason or "session_reset")`）
            check("/new 结束了旧会话", runner._db().get_session(sid)["end_reason"] == "session_reset")
            check("★ /new 之后按键找回的是**新**会话（否则用户一重置就回到旧对话）",
                  runner._db().latest_session_id_for_key(key) == sid2)

            # ★ 另一个方向：**所有**会话都结束了 → 找回必须是 None。
            #   这一条才是"找回时排除已结束的"这个机制的存在理由：
            #   去掉过滤的话，网关重启后会把一条早就结束的会话**复活**（用户以为自己重置过）。
            runner._db().end_session(sid2, "gateway_stop")
            check("★ 全部会话都已结束 → 找回为 None（不复活已结束的会话）",
                  runner._db().latest_session_id_for_key(key) is None)
            runner.shutdown()


def test_delivery_routes():
    from gateway.delivery import Delivery, DeliveryError, parse_target

    check("显式目标必须带平台", parse_target("console:c9") == ("console", "c9"))
    try:
        parse_target("c9")
        check("缺平台的裸 chat_id 要报错", False, "没抛异常")
    except DeliveryError:
        check("缺平台的裸 chat_id 要报错（否则要靠猜平台）", True)

    with temp_home() as home:
        with fake_agent():
            runner, src, key = _runner(home)
            adapter = runner.adapters["console"]
            _, res = _feed(runner, "你好", key)
            check("直接回复回到来源聊天", adapter.outbox()[-1] == "(stub) 你好")

            ev = adapter.make_event("带目标的消息")
            ev.metadata["deliver_to"] = "console:console-2"
            adapter.feed("你好")           # 走默认目标，先把上面那条的路径验完
            adapter.handle_message(ev)
            adapter.wait_idle(key, timeout=15)
            check("指定目标投递到别的 chat_id",
                  ("console", "console-2", "(stub) 带目标的消息") in runner.delivery.sent,
                  str(runner.delivery.sent[-2:]))

            d = Delivery({})
            try:
                d.deliver(src, "x")
                check("没有适配器要报错", False, "没抛异常")
            except DeliveryError as exc:
                check("没有那个平台的适配器 → 报错（不是静默丢弃）", "没有" in str(exc))
            runner.shutdown()


def test_hooks_registry():
    from gateway.hooks import HookRegistry

    with temp_home() as home:
        reg = HookRegistry(hooks_dir=home / "hooks")
        check("没有 hooks 目录 → 加载 0 个且不报错", reg.discover_and_load() == [])

        for name, events, explode in (("good", "agent:end, session:start", False),
                                      ("bad", "agent:end", True),
                                      ("nomanifest", None, False)):
            d = home / "hooks" / name
            d.mkdir(parents=True)
            if events is None:
                continue
            (d / "HOOK.yaml").write_text(f"name: {name}\nevents: {events}\n", encoding="utf-8")
            (d / "handler.py").write_text(
                "import os, pathlib\n"
                "def handle(event, payload):\n"
                + ("    raise RuntimeError('钩子坏了')\n" if explode else
                   "    p = pathlib.Path(os.environ['MINI_HOME']) / 'hook.log'\n"
                   "    p.write_text((p.read_text() if p.exists() else '') + event + '\\n')\n")
                + "    return event\n", encoding="utf-8")

        loaded = reg.discover_and_load()
        check("扫目录加载有效钩子，跳过没有清单的", sorted(loaded) == ["bad", "good"], str(loaded))
        check("没有 HOOK.yaml 的目录被记为错误（可观测，不是静默）",
              any("nomanifest" in e for e in reg.errors), str(reg.errors))
        check("事件 → 处理器计数正确",
              reg.handler_count("agent:end") == 2 and reg.handler_count("session:start") == 1)

        reg.emit("agent:end", x=1)
        log = (home / "hook.log")
        check("事件真的被派发（好钩子写了文件）",
              log.exists() and "agent:end" in log.read_text())
        check("★ 坏钩子抛异常 → 其它钩子照跑、网关不受影响",
              any("坏钩子" not in e and "钩子坏了" in e for e in reg.errors), str(reg.errors))

    # ★ 路径在**调用时**解析：两个 home 各自的 hooks 目录互不影响
    with temp_home() as home_a:
        (home_a / "hooks" / "onlyA").mkdir(parents=True)
        (home_a / "hooks" / "onlyA" / "HOOK.yaml").write_text("name: onlyA\nevents: gateway:tick\n")
        (home_a / "hooks" / "onlyA" / "handler.py").write_text("def handle(e, p):\n    return 1\n")
        a = HookRegistry(hooks_dir=home_a / "hooks").discover_and_load()
    with temp_home() as home_b:
        b = HookRegistry(hooks_dir=home_b / "hooks").discover_and_load()
    check("路径按调用时的 home 解析（A 有钩子、B 没有）", a == ["onlyA"] and b == [])


def test_process_identity():
    from gateway import status as st

    with temp_home() as home:
        check("启动前：没有活着的网关", st.live_gateway_pid_for_home(home) is None)

        st.write_pid_record(home, os.getpid())
        check("写下 pid 记录后认得出自己（PID + 启动时间指纹）",
              st.live_gateway_pid_for_home(home) == os.getpid())

        # ★ 号被回收：记录里的指纹与"现在这个号"的启动时间不一致 → 不算自己
        rec = json.loads(st.pid_file(home).read_text())
        rec["start_fingerprint"] = "Mon Jan  1 00:00:00 2001"
        st.pid_file(home).write_text(json.dumps(rec))
        check("★ 指纹不匹配 → 判为**不是**自己的网关（#109680 那个 bug 类）",
              st.live_gateway_pid_for_home(home) is None)

        # 文件被删但进程还活着（#110166）：第二判据（运行时状态）救回来
        st.write_runtime_status(home, "running", pid=os.getpid(),
                               start_fingerprint=st.process_start_fingerprint(os.getpid()))
        check("pid 文件不在时，状态记录仍能证明它活着（#110166）",
              st.live_gateway_pid_for_home(home) == os.getpid())

        st.write_runtime_status(home, "stopped", pid=os.getpid())
        check("状态=stopped → 不再算活着", st.live_gateway_pid_for_home(home) is None)

        st.write_runtime_status(home, "startup_failed", pid=os.getpid())
        check("状态=startup_failed → 不算活着（启动失败的进程不该被当成服务中）",
              st.live_gateway_pid_for_home(home) is None)

        # 别的 profile 的身份不许借用
        st.write_pid_record(home, os.getpid())
        check("记录里的 home 与预期不符 → 拒绝（别的 profile 的身份不外借）",
              st.get_running_pid(st.pid_file(home), expected_home=Path("/tmp/other")) is None)

        check("不存在的 pid → 没有指纹", st.process_start_fingerprint(999999) is None)

    check("命令行匹配按**完整 token**（真实版禁掉 argv 子串那类写法）",
          st.looks_like_gateway_command_line("python -m hermes_cli.main gateway run")
          and not st.looks_like_gateway_command_line("python -m gateway_server run"))
    check("退出码 78 = EX_CONFIG（给监督者看的信号）",
          st.GATEWAY_FATAL_CONFIG_EXIT_CODE == 78)
    check("启动期冲突的错误码族认得出（*_lock / lock_conflict）",
          st.is_global_startup_conflict("telegram_lock") and st.is_global_startup_conflict("lock_conflict")
          and not st.is_global_startup_conflict("rate_limited"))


def test_secret_scope_fail_closed():
    import agent.secret_scope as ss

    with temp_home() as home:
        (home / ".env").write_text("DEEPSEEK_API_KEY=from-home-A\n")
        check("build_profile_secret_scope 读 .env", ss.build_profile_secret_scope(home)["DEEPSEEK_API_KEY"]
              == "from-home-A")

        os.environ["DEEPSEEK_API_KEY"] = "from-environ"
        ss.set_multiplex_active(False)
        check("单 profile：没有作用域时回落到 os.environ（那时它是对的）",
              ss.get_secret("DEEPSEEK_API_KEY") == "from-environ")

        ss.set_multiplex_active(True)
        try:
            ss.get_secret("DEEPSEEK_API_KEY")
            check("★ 多路复用 + 没有作用域 → 抛 UnscopedSecretError", False, "没抛")
        except ss.UnscopedSecretError:
            check("★ 多路复用 + 没有作用域 → 抛 UnscopedSecretError（把静默用错凭据变成报错）", True)

        token = ss.set_secret_scope({"DEEPSEEK_API_KEY": "scoped"})
        try:
            check("有作用域时以作用域为准", ss.get_secret("DEEPSEEK_API_KEY") == "scoped")
            check("作用域**不改 os.environ**（子进程不会继承别的 profile 的密钥）",
                  os.environ["DEEPSEEK_API_KEY"] == "from-environ")
        finally:
            ss.reset_secret_scope(token)
            ss.set_multiplex_active(False)
            os.environ.pop("DEEPSEEK_API_KEY", None)


def test_profile_scope_aba():
    """★ A→B→A：真实版 AGENTS.md 明确要求的证明方式（一个临时 home 证明不了作用域）。"""
    import hermes_constants as hc
    from gateway.profile_scope import profile_runtime_scope
    import agent.secret_scope as ss

    with temp_home() as base:
        a, b = base / "profiles" / "a", base / "profiles" / "b"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        (a / ".env").write_text("K=value-A\n")
        (b / ".env").write_text("K=value-B\n")
        ss.set_multiplex_active(True)
        try:
            def probe() -> tuple:
                return (str(hc.get_mini_home()), ss.get_secret("K"))

            with profile_runtime_scope(a):
                check("A 作用域：home 与密钥都指向 A", probe() == (str(a), "value-A"), str(probe()))
            with profile_runtime_scope(b):
                check("B 作用域：换成 B", probe() == (str(b), "value-B"), str(probe()))
            with profile_runtime_scope(a):
                check("★ 回到 A：还认得 A（残留作用域 = 那个静默的 bug 类）",
                      probe() == (str(a), "value-A"), str(probe()))
            check("出了作用域：没有作用域（再读密钥会 fail closed）",
                  ss.current_secret_scope() is None)
        finally:
            ss.set_multiplex_active(False)

        # 子进程环境：摘掉 profile 相关的路径与密钥，再钉上目标 home
        from gateway.profile_scope import served_profile_child_env

        child = served_profile_child_env(b, base_env={"MINI_HOME": str(a),
                                                      "DEEPSEEK_API_KEY": "leak",
                                                      "PATH": "/bin"})
        check("子进程环境被摘掉密钥、且 MINI_HOME 指向目标 profile",
              child["MINI_HOME"] == str(b) and "DEEPSEEK_API_KEY" not in child
              and child.get("PATH") == "/bin")


def test_profiles_to_serve_and_gateway_status():
    """★ 这一组必须**同时**钉住两个 home：

    `profiles_root()` 是 **HOME 锚定**的（`Path.home()/.mini-hermes/profiles`），
    所以它**不看 `MINI_HOME`** —— 这正是"`-p x profile list` 要能看见所有 profile"的前提。
    因此测试要连 `Path.home()` 一起钉到临时目录，否则**会写用户的真实 home**
    （真实版仓库里有一条一模一样的规矩：profile 测试要把 `Path.home` 一起 mock）。
    """
    import pathlib

    import hermes_cli.profiles as prof

    real_home = pathlib.Path.home
    with temp_home() as mini_home:
        fake_root = mini_home / "fake-home"
        (fake_root / ".mini-hermes" / "profiles").mkdir(parents=True)
        pathlib.Path.home = staticmethod(lambda: fake_root)
        try:
            (prof.profiles_root() / "coder").mkdir()
            (prof.profiles_root() / "writer").mkdir()
            check("列得出 profile", prof.list_profiles() == ["coder", "writer"])

            check("★ **HOME 锚定**：profile 根不看 MINI_HOME（否则 `-p x profile list` 只见一个）",
                  str(prof.profiles_root()) == str(fake_root / ".mini-hermes" / "profiles")
                  and str(mini_home) != str(fake_root))

            homes = prof.profiles_to_serve(True)
            check("多路复用：默认 profile 排第一 + 各 profile home",
                  len(homes) == 3 and homes[0] == prof.default_home()
                  and {p.name for p in homes[1:]} == {"coder", "writer"}, str(homes))
            check("不多路复用时只服务默认 profile",
                  prof.profiles_to_serve(False) == [prof.default_home()])
        finally:
            pathlib.Path.home = real_home

        with fake_agent():
            # ★ 第 9 章：三态里的"显式关"。不给这个参数时是 `None` = **未设置** →
            #   启动期预检裁决（本机有 2 个 profile 时会**默认开**，这是对齐真实版的行为）。
            #   要断言"没开就不该提"，就得**显式钉死**。
            runner, src, key = _runner(mini_home, multiplex=False)
            text = runner.status_text()
            check("网关 status 文本含 home / 存活 / 状态记录",
                  "home" in text and "状态记录" in text, text)
            check("显式 --no-multiplex 时**不**列 profile（没开就不该提）", "服务 profile" not in text)
            check("显式关 → 裁决 source=config 且 enabled=False",
                  runner.multiplex_decision is not None
                  and runner.multiplex_decision.source == "config"
                  and runner.multiplex_decision.enabled is False,
                  runner.multiplex_decision)
            runner.shutdown()


def test_gateway_single_instance_exit_code():
    """★ 单实例必须**跨进程**测：同一个进程里"第二个网关"看到的 pid 就是自己。"""
    import subprocess

    from gateway import status as st
    from gateway.run import GatewayRunner

    with temp_home() as home:
        with fake_agent():
            runner, src, key = _runner(home)

            code = subprocess.run(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, %r);\n"
                 "from gateway.run import GatewayRunner\n"
                 "raise SystemExit(GatewayRunner(echo=True).start())"
                 % str(Path(__file__).resolve().parent.parent)],
                env=dict(os.environ), capture_output=True, text=True, timeout=120,
            )
            check("同一 home 第二个网关（**另一个进程**）→ 退出码 78",
                  code.returncode == 78, f"实际 {code.returncode}\n{code.stdout[-300:]}")
            check("启动失败时把话说明白（不是静默退出）",
                  "已经有网关在跑" in code.stdout, code.stdout[:200])
            state = st.read_runtime_status(st.state_file(home)) or {}
            check("状态记为 startup_failed（可观测）", state.get("state") == "startup_failed", str(state))
            check("记下了冲突对手（便于排查）", state.get("conflict_with") == os.getpid())

            runner.shutdown()
            check("关掉之后 pid 文件摘掉、状态=stopped",
                  not st.pid_file(home).exists()
                  and st.read_runtime_status(st.state_file(home))["state"] == "stopped")

            code2 = subprocess.run(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, %r);\n"
                 "from gateway.run import GatewayRunner\n"
                 "r = GatewayRunner(echo=False); c = r.start(); r.shutdown()\n"
                 "raise SystemExit(c)" % str(Path(__file__).resolve().parent.parent)],
                env=dict(os.environ), capture_output=True, text=True, timeout=120,
            )
            check("真关掉之后，另一个进程能正常启动（说明不是永远拒绝）",
                  code2.returncode == 0, f"实际 {code2.returncode}\n{code2.stderr[-300:]}")


# ══════════════════════════════════════════════════════════════════════════════ #
# 第 7 章侧线：profile 的"入口层"（`mini -p <名字>` / `mini profile …`）
# ══════════════════════════════════════════════════════════════════════════════ #
def test_profile_create_and_seed():
    from hermes_cli.profiles import (ENV_HEADER, PROFILE_DIRS, ProfileError,
                                     create_profile, profiles_root)

    with temp_profiles_home() as base:
        home = create_profile("lab")
        check("create 建出这个 profile 的 home",
              home == base / "profiles" / "lab" and home.is_dir(), str(home))
        check("建出的目录 = mini 真的会读写的那些（logs / hooks）",
              set(PROFILE_DIRS) == {"logs", "hooks"}
              and all((home / d).is_dir() for d in PROFILE_DIRS))
        env = home / ".env"
        check("播种一份**凭据文件**（从第一天起它有自己的 .env）",
              env.exists() and env.read_text(encoding="utf-8") == ENV_HEADER)
        check("播种内容是空的（没有密钥；也不是从默认 profile 抄来的）",
              "API_KEY" not in env.read_text(encoding="utf-8"))
        check("没有顺手建一个会话库（用到才建）", not (home / "state.db").exists())
        check("profile 根是 HOME 锚定的（profiles/ 在默认 home 底下）",
              profiles_root() == base / "profiles")

        try:
            create_profile("lab")
            check("重名 → 拒绝", False, "没抛异常")
        except ProfileError as exc:
            check("重名 → 拒绝，且说得出补救方法（不覆盖）",
                  "已经存在" in str(exc) and "profile list" in str(exc), str(exc))


def test_profile_name_alphabet():
    """★ id 字母表：`~` 必须被拒 —— 否则 §一 的「靠字母表」那招就成了假的。"""
    from hermes_cli.profiles import ProfileError, create_profile, validate_profile_name

    with temp_profiles_home() as base:
        for bad, why in (("main~", "`~` 不在字母表里（默认 profile 的标记形态靠它）"),
                         ("default", "保留名（在会话键里等同于默认 profile）"),
                         ("a b", "空格"),
                         ("中文名", "非 ASCII"),
                         ("-lead", "不能以符号开头"),
                         ("", "空")):
            try:
                create_profile(bad)
                check(f"拒绝非法 profile 名 {bad!r}（{why}）", False, "没抛异常")
            except ProfileError:
                check(f"拒绝非法 profile 名 {bad!r}（{why}）", True)
        check("这些 bad 名一个目录都没建出来", not list(base.glob("profiles/*"))
              or all(p.name not in ("main~", "default", "中文名") for p in base.glob("profiles/*")))
        for good in ("lab", "lab-2", "a.b_c", "L9"):
            validate_profile_name(good)
        check("合法名（字母/数字/._-，字母或数字开头）都通过", True)


def test_profile_clone_keeps_config_not_memory():
    from hermes_cli.profiles import create_profile

    with temp_profiles_home() as base:
        (base / ".env").write_text("DEEPSEEK_API_KEY=from-default\n", encoding="utf-8")
        (base / "SOUL.md").write_text("默认 profile 的身份\n", encoding="utf-8")
        (base / "MEMORY.md").write_text("只属于默认 profile 的私人记忆\n", encoding="utf-8")
        (base / "state.db").write_bytes(b"pretend-db")

        home = create_profile("lab", clone=True)
        check("--clone 带配置与身份（.env + SOUL.md）",
              (home / ".env").exists() and (home / "SOUL.md").exists())
        check("★ --clone **不带记忆**（MEMORY.md 是「经历过什么」，不是配置）",
              not (home / "MEMORY.md").exists())
        check("--clone 不带会话数据（state.db / sessions 一个都不抄）",
              not (home / "state.db").exists() and not (home / "sessions").exists())

        # 不 clone：源里有 SOUL.md 也不该出现在新 profile 里
        home2 = create_profile("lab2")
        check("不 --clone → 只有播种的 .env（不偷配置）",
              (home2 / ".env").exists() and not (home2 / "SOUL.md").exists())


def test_profile_resolve_never_silently_falls_back():
    """★ 解析的第三条：**不存在就报错**。静默回落默认 home 是最危险的一种（不报错）。"""
    from hermes_cli.profiles import ProfileError, create_profile, resolve_profile_home

    with temp_profiles_home() as base:
        check("不给 / 'default' → 默认 home（老行为一个字节不变）",
              resolve_profile_home(None) == base and resolve_profile_home("default") == base)
        home = create_profile("lab")
        check("存在 → 那个 profile 的 home", resolve_profile_home("lab") == home)
        try:
            resolve_profile_home("ghost")
            check("★ 不存在 → 报错（不静默用默认 home）", False, "没抛异常")
        except ProfileError as exc:
            msg = str(exc)
            check("★ 不存在 → 报错，且列出现有的 + 给出补救命令",
                  "没有名为" in msg and "lab" in msg and "profile create ghost" in msg, msg)


def test_two_profiles_share_nothing():
    """★ A→B→A：两套 home 的记忆/会话/检索**互不可见**（物理分家，不是过滤）。"""
    import hermes_constants
    import hermes_state
    from hermes_cli.profiles import create_profile

    with temp_profiles_home() as base:
        a, b = create_profile("a"), create_profile("b")
        os.environ["MINI_HOME"] = str(a)
        check("A 的库在 A 的家底下（路径唯一来源跟着 MINI_HOME 走）",
              str(hermes_constants.state_db_path()).startswith(str(a)))
        db_a = hermes_state.SessionDB()
        sid = hermes_state.new_session_id()
        db_a.create_session(sid, "cli", session_key="main:console:dm:c1")
        db_a.append_message(sid, "user", "项目代号猎户座")
        check("A 里搜得到自己写的东西", len(db_a.search_messages("猎户座")) >= 1)
        db_a.close()

        os.environ["MINI_HOME"] = str(b)
        check("切到 B：库路径也换了（两个文件，不是一个文件的两种查法）",
              str(hermes_constants.state_db_path()).startswith(str(b)))
        db_b = hermes_state.SessionDB()
        check("★ B 看不到 A 的会话", db_b.latest_session_id() is None)
        check("★ B 搜不到 A 的内容", len(db_b.search_messages("猎户座")) == 0)
        check("★ 拿 A 的会话 id 去 B 读 → 空", db_b.get_messages_as_conversation(sid) == [])
        db_b.close()

        os.environ["MINI_HOME"] = str(a)
        db_a2 = hermes_state.SessionDB()
        check("★ 回到 A：东西还在（A→B→A，中间没被 B 覆盖）",
              len(db_a2.search_messages("猎户座")) >= 1)
        db_a2.close()


def test_profile_cli_end_to_end():
    """走**真 CLI**（子进程）：`profile create/list` 与 `-p` —— 都不需要密钥。"""
    import subprocess

    with temp_profiles_home() as base:
        repo = Path(__file__).resolve().parent.parent
        launcher = repo / "mini"
        env = dict(os.environ)
        env["HOME"] = str(base.parent)          # 子进程里 Path.home() 看 $HOME
        env.pop("MINI_HOME", None)

        def run(*argv):
            return subprocess.run([str(launcher), *argv], cwd=str(repo), env=env,
                                  capture_output=True, text=True, timeout=180)

        r = run("profile", "create", "lab")
        check("`mini profile create lab` 退出码 0", r.returncode == 0, r.stdout + r.stderr)
        check("它把建在哪说清楚了", "profiles/lab" in r.stdout, r.stdout)

        r2 = run("profile", "list")
        check("`profile list` 同时列出默认与具名 profile",
              "default" in r2.stdout and "lab" in r2.stdout, r2.stdout)

        r3 = run("-p", "lab", "--list-toolsets")
        check("`-p lab` 生效（并把切到哪个 home 说出来）",
              r3.returncode == 0 and "profile 'lab'" in (r3.stderr + r3.stdout),
              r3.stderr[:200])

        r4 = run("-p", "ghost", "--list-toolsets")
        check("★ `-p 不存在的名字` → 退出码 2 + 明确报错（**不静默用默认 home**）",
              r4.returncode == 2 and "没有名为" in r4.stderr, r4.stderr[:200])

        r5 = run("profile", "show", "lab")
        check("`profile show` 能看出这个 profile 家里有什么",
              r5.returncode == 0 and "logs" in r5.stdout and ".env" in r5.stdout, r5.stdout)

        # ★ 子进程只证明了"没报错"；真正要证明的是"home 真的换了"。
        #   在**本进程**里跑一次 main()，断言它把 MINI_HOME 钉到了那个 profile ——
        #   这才是 `-p` 的全部效果（改回旧写法"解析了但不用"时，这一条会红）。
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from hermes_cli.main import main as cli_main

        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            rc = cli_main(["-p", "lab", "--list-toolsets"])
        check("`-p` 真的把 home 钉到了那个 profile（不只是「没报错」）",
              rc == 0 and os.environ.get("MINI_HOME") == str(base / "profiles" / "lab"),
              f"rc={rc} MINI_HOME={os.environ.get('MINI_HOME')}")


# ══════════════════════════════════════════════════════════════════════════════ #
# ══════════════════════════════════════════════════════════════════════════════ #
# 第 8 章：会话生命周期（状态机 / 显式边界 / 重启恢复 / 卡死循环）
def test_lifecycle_priority_and_hard_wins():
    """优先级：软(待恢复) → 复用；硬(挂起) → 换新；硬永远赢过软；跑完才清标记。"""
    with temp_home() as home:
        with fake_agent():
            runner, src, key = _runner(home)
            db, router = runner._db(), runner._router()
            _, res = _feed(runner, "一", key)
            sid1 = res["session_id"]

            # ── 软恢复：保留归属 ──
            router.mark_resume_pending(sid1, "shutdown_timeout")
            d = router.route(key, source="gateway:console")
            check("软(待恢复) → 复用**同一个**会话",
                  d.action == "reused" and d.entry.session_id == sid1)
            check("★ 读到**不清**标记（清了的话「读了之后又崩」就永远恢复不上）",
                  bool(db.get_session(sid1)["resume_pending"]))
            check("decision 说得出为什么复用",
                  any("resume_pending" in n for n in d.notes), str(d.notes))

            # ── 硬擦除：赢 ──
            router.suspend(key)
            d2 = router.route(key, source="gateway:console")
            check("★ 硬(挂起) 赢过 软(待恢复)：换新会话",
                  d2.action == "auto_reset" and d2.entry.session_id != sid1)
            check("旧会话结束原因 = suspended", db.get_session(sid1)["end_reason"] == "suspended")
            check("新会话带 is_fresh_reset（真实版据此重注入话题/技能）",
                  bool(db.get_session(d2.entry.session_id)["is_fresh_reset"]))

            # ── 软标记不许覆盖硬擦除（**先硬后软**才是这条规则）──
            router.suspend(key)
            router.mark_resume_pending(d2.entry.session_id, "restart_interrupted")
            check("★ 软标记**不许覆盖**硬擦除（用户的明确意图不能被一次意外撤回）",
                  bool(db.get_session(d2.entry.session_id)["suspended"]) and
                  not bool(db.get_session(d2.entry.session_id)["resume_pending"]))

            # ── 跑完一轮 → 一次性标记清掉（用真 runner 走完整路径）──
            _, res3 = _feed(runner, "二", key)
            sid3 = res3["session_id"]
            check("换过会话之后，这一轮跑完不带 resume_pending",
                  not bool(db.get_session(sid3)["resume_pending"]))
            check("★ is_fresh_reset 被**第一条消息消费掉**（真实版逐字：Consume is_fresh_reset "
                  "so it doesn't leak onto later messages in the same session）",
                  not bool(db.get_session(sid3)["is_fresh_reset"]))
            runner.shutdown()


def test_explicit_boundary_never_time_based():
    """时间/重启永不轮转；能轮转的只有显式边界（且动作枚举里没有"第四种"）。"""
    with temp_home() as home:
        with fake_agent():
            runner, src, key = _runner(home)
            db = runner._db()
            _, res = _feed(runner, "一", key)
            sid = res["session_id"]

            # 把活动时间倒拨 30 天 + 重启一遍
            db._conn.execute("UPDATE sessions SET started_at = 0, updated_at = 0 WHERE id = ?",
                             (sid,))
            db._conn.commit()
            runner.shutdown()
            runner2, _, key2 = _runner(home)
            # ★ 这条必须放在 route() **之前**：route() 命中复用时会 touch（刷新活动时间），
            #   那之后就看不出"关闭有没有刷过"了。
            check("（前提）关闭**没有**把 updated_at 刷新成'刚刚活动'"
                  "（否则恢复扫描的 120 秒窗口就没意义了）",
                  (db.get_session(sid)["updated_at"] or 0) == 0,
                  str(db.get_session(sid)["updated_at"]))
            d = runner2._router().route(key2, source="gateway:console")
            check("★ 30 天不活动 + 重启 → 仍复用同一会话（时间永不轮转）",
                  d.action == "reused" and d.entry.session_id == sid)

            # 结构断言：动作枚举里只有三种出口，没有"按时间轮转"那一种
            import re
            from pathlib import Path as _Path
            mod = (_Path(__file__).resolve().parent.parent / "gateway" / "session_state.py"
                   ).read_text(encoding="utf-8")
            actions = set(re.findall(r'action="([a-z_]+)"', mod))
            actions |= set(re.findall(r'else "([a-z_]+)"', mod))     # 三元表达式里的那一支
            check("状态机的出口只有 created/reused/auto_reset（没有基于时间的第四种）",
                  actions == {"created", "reused", "auto_reset"}, str(actions))
            runner2.shutdown()


def test_restart_recovery_marker_and_sweep():
    """恢复：干净退出跳过整段 · 崩溃只标"开着的、最近活跃的" · 计数 +1。"""
    with temp_home() as home:
        with fake_agent():
            import hermes_state
            from gateway import status as gw_status
            from gateway.run import GatewayRunner

            runner, src, key = _runner(home)
            db = runner._db()
            _, res = _feed(runner, "一", key)
            sid = res["session_id"]

            # 造一个"刚刚活动过、但已经结束"的会话：恢复**不该**碰它
            dead = hermes_state.new_session_id()
            db.create_session(dead, "gateway:console", session_key=key + ":dead")
            runner._router().touch(dead)
            db.end_session(dead, "session_reset")

            runner.shutdown()
            check("优雅关闭写了 .clean_shutdown 标记",
                  gw_status.clean_shutdown_marker(home).exists())
            check("★ 关闭**不结束**会话（进程退出不是对话边界）",
                  db.get_session(sid)["ended_at"] is None)

            r2 = GatewayRunner(home=home, echo=False)
            rec2 = r2._recover_sessions()
            check("干净退出 → 跳过恢复（一个会话都不碰）",
                  rec2["clean_shutdown"] is True and rec2["resume_pending"] == [])
            check("……标记已被消费（它只说明「上一次」）",
                  not gw_status.clean_shutdown_marker(home).exists())

            gw_status.clean_shutdown_marker(home).unlink(missing_ok=True)   # 模拟崩溃
            r3 = GatewayRunner(home=home, echo=False)
            rec3 = r3._recover_sessions()
            check("脏启动 → 把「开着的、最近活跃的」会话标成待恢复",
                  rec3["resume_pending"] == [sid], str(rec3["resume_pending"]))
            check("★ 已结束的会话**不在**恢复名单里（结束过就是结束过）",
                  dead not in rec3["resume_pending"])
            check("库里 resume_pending 置位 + 原因写清",
                  bool(db.get_session(sid)["resume_pending"]) and
                  db.get_session(sid)["resume_reason"] == "restart_interrupted")
            check("连续重启计数 +1（供卡死升级用）",
                  gw_status.read_stuck_loop_counts(home) == {key: 1},
                  str(gw_status.read_stuck_loop_counts(home)))
            runner.shutdown()


def test_stuck_loop_escalation_and_completion_reset():
    """卡死循环：阈值判定 · 越线挂起 + 计数清零 · 会话跑完 → 自己那条清零。"""
    with temp_home() as home:
        with fake_agent():
            from gateway import status as gw_status
            from gateway.run import GatewayRunner

            runner, src, key = _runner(home)
            db, router = runner._db(), runner._router()
            _, res = _feed(runner, "一", key)
            sid = res["session_id"]

            # ── ① 阈值判定：偶发重启不该被当成卡死 ──
            check("2 次不够阈值 → 不挂起", router.suspend_stuck_loop_sessions({key: 2}) == [])
            check("……且不碰库（会话照旧开着）", not bool(db.get_session(sid)["suspended"]))
            check("3 次越线 → 挂起该键", router.suspend_stuck_loop_sessions({key: 3}) == [key])
            check("库里 suspended 置位", bool(db.get_session(sid)["suspended"]))

            # ── ② 启动恢复那条**完整**路径：读文件 → 越线 → 挂起 + 清文件 ──
            #    （★ 这条同时锁住第 6 个真 bug：升级不该依赖"本次扫描标了谁"）
            gw_status.write_stuck_loop_counts(home, {key: 3})
            gw_status.clean_shutdown_marker(home).unlink(missing_ok=True)
            r4 = GatewayRunner(home=home, echo=False)
            rec4 = r4._recover_sessions()
            check("★ 启动恢复：计数越线 → 挂起",
                  rec4["suspended"] == [key], str(rec4["suspended"]))
            check("★ 挂起后计数清零（真实版逐字：「counters start fresh after suspension」）",
                  gw_status.read_stuck_loop_counts(home) == {})

            # ── ③ 会话跑完 → 它自己那条计数清零（不是"只加不减"）──
            gw_status.write_stuck_loop_counts(home, {key: 2})
            _, res2 = _feed(runner, "二", key)          # 该键此前已被挂起 → 这轮换新会话
            check("会话跑完 → 它的连续重启计数清零",
                  gw_status.read_stuck_loop_counts(home) == {},
                  str(gw_status.read_stuck_loop_counts(home)))
            check("……新会话是干净的（不是「读到就清」，是「跑完才清」）",
                  not bool(db.get_session(res2["session_id"])["resume_pending"]))
            runner.shutdown()


def test_busy_slot_overwrites_and_notifies():
    """单槽：普通消息**后到覆盖先到**（"我改主意了"），被顶掉的那条要通知用户。"""
    gate = threading.Event()
    calls: list = []
    with temp_home() as home:
        with fake_agent(gate, calls):
            runner, src, key = _runner(home)
            adapter = runner.adapters["console"]
            check("第一条：开始跑（占住这个会话）", runner.feed("第一条") == "started")
            check("忙时第二条：排队（不是丢）", runner.feed("第二条") == "queued")
            check("★ 忙时第三条：**覆盖**第二条 —— 单槽，不是 FIFO",
                  runner.feed("第三条") == "queued")
            check("深度 = 1（普通消息只占单槽）", adapter.pending_count(key) == 1,
                  str(adapter.pending_count(key)))
            check("槽里是**最新**那条", getattr(adapter.queues.slot_event(key), "text", None)
                  == "第三条")
            check("★ 被顶掉的第二条**收到通知**（静默丢用户写的字比拒绝更糟）",
                  any("顶掉" in t for t in adapter.outbox()), str(adapter.outbox()[-1:]))
            check("通知里给了正确的替代动作（`/queue`）",
                  any("/queue" in t for t in adapter.outbox()))

            gate.set()
            check("放开后跑完（队列被消费干净）", adapter.wait_idle(key, timeout=30)
                  and adapter.pending_count(key) == 0)
            check("★ 跑掉的是**最新**那条：被顶掉的第二条不跑",
                  calls == ["第一条", "第三条"], str(calls))
            runner.shutdown()


def test_explicit_queue_never_merges_and_runs_in_order():
    """`/queue` 排的是**承诺**：不合并、按序、一件都不少；且优先于单槽。"""
    gate = threading.Event()
    calls: list = []
    with temp_home() as home:
        with fake_agent(gate, calls):
            runner, src, key = _runner(home)
            adapter = runner.adapters["console"]
            check("起一轮（占住会话）", runner.feed("A") == "started")

            # 忙时发命令：走内联（第 1 级只把它交给第 2 级）
            runner.feed("/queue 第二件")
            res = adapter.last_result(key)
            check("显式排队立刻有回执（第 N 件）",
                  res.get("action") == "queue_enqueue" and res.get("count") == 1, str(res))
            runner.feed("/queue 第三件")
            check("★ 显式排队**不合并**（深度 2）", adapter.pending_count(key) == 2,
                  str(adapter.pending_count(key)))

            runner.feed("/queue")                     # 只看一眼
            res2 = adapter.last_result(key)
            check("看队列：说得出两层、深度对", res2.get("action") == "queue"
                  and res2.get("count") == 2, str(res2))
            check("runner 的 `_queue_depth` 与适配器一致（真实版同名方法）",
                  runner._queue_depth(key, adapter=adapter) == 2)

            runner.feed("第四件（普通）")
            check("总深度 = 溢出 2 + 单槽 1", adapter.pending_count(key) == 3,
                  str(adapter.pending_count(key)))

            gate.set()
            ok = adapter.wait_idle(key, timeout=30)
            check("★ 按序跑完：**先已承诺的两件，最后才是最新的那条**",
                  ok and calls == ["A", "第二件", "第三件", "第四件（普通）"], str(calls))
            check("跑完深度归零", adapter.pending_count(key) == 0)
            runner.shutdown()


def test_gateway_accepted_receipt():
    """`_gateway_accepted`：**网关接受**那一刻置位（真实版 `run_busy.py:55` 逐字同名）。"""
    gate = threading.Event()
    with temp_home() as home:
        with fake_agent(gate):
            runner, src, key = _runner(home)
            adapter = runner.adapters["console"]
            runner.feed("一")
            runner.feed("二")
            slot = adapter.queues.slot_event(key)
            check("★ 排队事件带上了回执（接受 ≠ 调用成功，这里才是'接受'）",
                  getattr(slot, "_gateway_accepted", False) is True)
            check("回执与队列深度对得上（每条被接受的事件都有）",
                  adapter.pending_count(key) == 1)
            gate.set()
            adapter.wait_idle(key, timeout=30)
            runner.shutdown()


def test_agent_cache_lifecycle_and_three_protections():
    """缓存 agent：命中 · 会话轮转必淘汰 · 三条"不许淘汰" · TTL 只放资源不结束对话。"""
    with temp_home() as home:
        with fake_agent():
            runner, src, key = _runner(home)
            cache = runner._agent_cache
            _, res = _feed(runner, "一", key)
            sid = res["session_id"]
            check("跑完一轮 → 缓存里有这个会话", cache.get(key) is not None)
            check("这一轮的 agent 与 session_id 绑定",
                  cache.entry(key).session_id == sid)
            check("★ 跑完就地解除保护（跑着的 / 没落盘的）",
                  not cache.entry(key).busy and not cache.entry(key).pending_flush)

            _, res2 = _feed(runner, "二", key)
            check("同一会话再来一轮 → **命中**（不新建 agent）",
                  cache.hits == 1 and cache.misses == 1, str(cache.stats()))
            check("……会话没换（时间/轮次都不轮转）", res2["session_id"] == sid)

            # ★ 会话轮转 → 必须淘汰（否则新消息会写进旧会话）
            _, res3 = _feed(runner, "/new", key)
            check("★ `/new` 之后该键被淘汰了（真实版 `_evict_cached_agent`）",
                  cache.get(key) is None, str(cache.stats()))
            check("淘汰原因记了 session_rotated",
                  ("session_rotated" in [r for _, r in cache.evictions]))
            check("★ 轮转淘汰**不受三条保护拦截**（它是正确性要求，不是资源决策）",
                  cache.get(key) is None and cache.stats()["protected"] >= 0)
            check("新会话 id 与旧的不同", res3["session_id"] != sid)

            # ── 三条保护（★ 先跑一轮把缓存填上，否则这些断言全在空集上"通过"）──
            _, res4 = _feed(runner, "三", key)
            sid_new = res4["session_id"]
            check("（前提）新会话跑了一轮 → 缓存里有新的 agent",
                  cache.get(key) is not None and cache.entry(key).session_id == sid_new)

            # ── ★ "没落盘的不许淘汰"：**被打断的那一轮**必须保留标记 ──
            #   （真实版判据 `transcript_persistence_caught_up`：没落盘的 agent 不许淘汰。
            #    被打断的回合转写本可能是半截的 —— 宁可多留一个 agent，也不丢一轮对话。）
            #   ★ 先把这个键淘汰：否则这一轮会**复用**先前缓存的 agent（它属于上一个
            #     `fake_agent()` 实例，不带闸门）—— 那一轮会瞬间跑完，根本来不及打断。
            #    这条"测得准"的前提本身就是"缓存真的在复用"的证据。
            cache.evict(key, reason="force")
            gate2 = threading.Event()
            with fake_agent(gate2):
                runner.feed("一条会被打断的")
                time.sleep(0.2)
                runner._interrupt_running_agent(key)
                gate2.set()
                adapter2 = runner.adapters["console"]
                adapter2.wait_idle(key, timeout=30)
            check("★ 被打断的那一轮**保留**'未落盘'标记（下次成功跑完才清）",
                  cache.entry(key).pending_flush is True, str(cache.stats()))
            check("……而「正在跑」的标记已经放开（那一轮确实结束了）",
                  cache.entry(key).busy is False)
            cache.mark_pending_flush(key, False)          # 还给后面几条用例一个干净的起点

            cache.mark_busy(key, True)
            future = time.monotonic() + 10 ** 6
            check("① 正在跑的：TTL 清扫**不动**它",
                  cache.sweep_idle(now=future) == [] and cache.get(key) is not None)
            cache.mark_busy(key, False)
            cache.mark_pending_flush(key, True)
            check("② 没落盘的：TTL 清扫**不动**它",
                  cache.sweep_idle(now=future) == [] and cache.get(key) is not None)
            cache.mark_pending_flush(key, False)
            check("③ 最近 N 个（保护带）：TTL 清扫也不动它",
                  cache.sweep_idle(now=future) == [] and cache.get(key) is not None
                  if cache.protect_recent > 0 else True)
            # 把保护带让开（protect_recent=0）后，TTL 才生效
            cache.protect_recent = 0
            dropped = cache.sweep_idle(now=future)
            check("★ 三条保护都解除后，TTL 才放它", dropped == [key], str(dropped))
            check("★ 放 agent **不是**结束对话（库里那行还开着）",
                  runner._db().get_session(sid_new)["ended_at"] is None)

            # ── 容量上限：按 LRU 放 ──
            #   ★ 断言要**紧**：max_size=1 时插两条，结果必须正好 1 条
            #     （写 `<= 2` 的话，"完全不淘汰"也能通过 —— 反向验证抓过这条）。
            cache.max_size = 1
            cache.get_or_create("other:key", "sid-other", lambda: object())
            cache.get_or_create("third:key", "sid-third", lambda: object())
            check("超过上限 → 按 LRU 放掉最旧的（容量不是硬失败）",
                  len(cache._entries) == 1, str(cache.stats()))
            check("放掉的原因里有 lru_cap",
                  any(r == "lru_cap" for _, r in cache.evictions))

            # ── 内存压力：过线才动手，且跳过受保护的 ──
            cache.max_size = 128
            before = len(cache._entries)
            cache.mark_busy("other:key", True)
            under = cache.sweep_under_pressure(rss_mb=1.0, high_mb=2048.0)
            check("没过线 → 一个都不放", under == [] and len(cache._entries) == before)
            over = cache.sweep_under_pressure(rss_mb=99999.0, high_mb=2048.0)
            check("过线 → 放掉（且跳过正在跑的）",
                  "other:key" not in over, str(over))
            runner.shutdown()


def test_housekeeping_tick_prune_switch_and_thread():
    """巡检 tick：三件事各司其职 · 剪枝开关语义 · 线程真的在跑 · 不结束任何对话。"""
    with temp_home() as home:
        with fake_agent():
            runner, src, key = _runner(home)
            _, res = _feed(runner, "一", key)
            sid = res["session_id"]

            receipt = runner._session_housekeeping_tick(rss_mb=1.0)
            check("收据四件套齐全",
                  set(receipt) == {"idle_evicted", "pressure_evicted", "rss_mb", "pruned"},
                  str(receipt))
            check("① 空闲清扫被调用了（结果是列表）", isinstance(receipt["idle_evicted"], list))
            check("内存没过线 → ② 不动手", receipt["pressure_evicted"] == [])
            check("③ 剪枝默认**关**（session_store_max_age_days=0）",
                  receipt["pruned"].startswith("跳过"), str(receipt["pruned"]))

            # 打开开关 → 只记账、不动数据（mini 存储层没有删除路径）
            # ★ 显式传 `now`：让"剪枝节流"与机器 uptime 无关（否则这个用例的结果
            #   取决于"这台机器开机多久了"—— 反向验证会变成偶发）。
            runner.session_store_max_age_days = 30
            r2 = runner._session_housekeeping_tick(now=100.0, rss_mb=1.0)
            check("★ 开关打开 → 尝试一次并**诚实记债**（没删任何东西）",
                  r2["pruned"].startswith("未执行"), str(r2["pruned"]))
            check("……债记在 `_turn_errors` 里", any("prune" in e for e in runner._turn_errors))
            r3 = runner._session_housekeeping_tick(now=101.0, rss_mb=1.0)
            check("剪枝是**每小时一次**，不是每个 tick",
                  r3["pruned"].startswith("跳过"), str(r3["pruned"]))

            # ★ 巡检**不结束对话**（第 8 章的不变量在资源路径上同样成立）
            check("巡检过后：会话还开着",
                  runner._db().get_session(sid)["ended_at"] is None)
            check("……而且按键找回的还是同一个会话",
                  runner._db().latest_session_id_for_key(key) == sid)

            # 线程：真起得来、真停得掉
            runner._start_housekeeping(interval=0.05, first_delay=0.0)
            deadline = time.time() + 3
            while time.time() < deadline and not runner.housekeeping_log():
                time.sleep(0.02)
            check("★ 巡检线程真的在跑（日志里有 tick）", bool(runner.housekeeping_log()),
                  str(runner.housekeeping_log()[:1]))
            runner._stop_housekeeping()
            n_after_stop = len(runner.housekeeping_log())
            time.sleep(0.2)
            check("停掉之后不再 tick（收尾不会留野线程）",
                  len(runner.housekeeping_log()) == n_after_stop)
            runner.shutdown()


def test_multiplex_mode_three_states_and_preflight():
    """★ 第 9 章 A1+A3+A4：开关的**三态**、预检三条、裁决不许致命、裁决理由落盘。

    为什么值得单独守：**"默认开"必须配预检**。没有它，一次安装里两个进程会去抢同一个
    bot token —— 那不是报错，是**间歇性错乱**（真实版 `gateway_multiplex_mode.py:75-107`）。

    三态的意义（真实版 `gateway/config.py:552` 逐字：*"a request the gateway settles at boot,
    not a verdict"*）：`None` = 待裁决、`True/False` = 有人已经定了。
    """
    from unittest import mock

    import gateway.multiplex_mode as mm
    from gateway import status as gw_status
    from hermes_cli import profiles as prof

    env_key = mm.MULTIPLEX_ENV_VAR
    saved = os.environ.get(env_key)
    try:
        os.environ.pop(env_key, None)
        # ── 三态与词表 ──
        decision = mm.resolve_multiplex_mode(True)
        check("显式 True → source=config（显式值永不复核）",
              decision.enabled is True and decision.source == "config", str(decision))
        os.environ[env_key] = "   "
        check("空白 env ≠ False（部署占位不算覆盖）", mm.env_multiplex_override() is None)
        os.environ[env_key] = "On"
        decision = mm.resolve_multiplex_mode(None)
        check("env=On → config（大小写无关）",
              decision.enabled is True and decision.source == "config", str(decision))
        os.environ[env_key] = "nope"
        check("未识别的词 → None（回落到下一道来源）", mm.env_multiplex_override() is None)
        os.environ.pop(env_key, None)
        check("bool_token 只认词表",
              mm.bool_token("0") is False and mm.bool_token("yes") is True
              and mm.bool_token("") is None)

        with temp_profiles_home() as default_home:
            (prof.profiles_root() / "coder").mkdir(parents=True, exist_ok=True)
            (prof.profiles_root() / "writer").mkdir(parents=True, exist_ok=True)
            served = prof.profiles_to_serve(multiplex=True)
            check("服务集合：默认排第一 + 两个具名 profile",
                  len(served) == 3 and served[0] == default_home,
                  str([p.name for p in served]))

            check("预检 ②：只有一个 home → 不白开（fail-closed 对它毫无用处）",
                  mm.implicit_multiplex_blocker(served=[default_home]) == mm.SINGLE_PROFILE_REASON)
            blocker = mm.implicit_multiplex_blocker(served=served, active="coder")
            check("预检 ①：非默认 profile 自己的网关 → 拦下",
                  blocker is not None and "profile 'coder'" in str(blocker), str(blocker))
            with mock.patch("gateway.status.live_gateway_pid_for_home", return_value=987654):
                blocker = mm.implicit_multiplex_blocker(served=served, active="default",
                                                        own_pid=os.getpid())
            check("预检 ③：有 secondary 还在跑 → 拦下并点名 pid",
                  blocker is not None and "'coder' (pid 987654)" in str(blocker), str(blocker))

            with mock.patch("gateway.status.live_gateway_pid_for_home", return_value=None):
                check("三条都没拦 → 可以开", mm.implicit_multiplex_blocker(served=served) is None)
                decision = mm.resolve_multiplex_mode(None)
                check("未设置 + 预检过 → default（开）",
                      decision.enabled is True and decision.source == "default", str(decision))

                # ── 真网关端到端：裁决进 start()，落进状态文件与服务集合 ──
                with fake_agent():
                    runner, src, key = _runner(default_home)      # 不给 multiplex = **未设置**
                    check("网关按裁决开启多路复用（不是 None）",
                          runner.multiplex is True
                          and runner.multiplex_decision is not None
                          and runner.multiplex_decision.source == "default",
                          str(runner.multiplex_decision))
                    check("served_homes = 裁决后的服务集合",
                          len(runner.served_homes) == 3,
                          str([p.name for p in runner.served_homes]))
                    payload = gw_status.read_runtime_status(
                        gw_status.state_file(default_home)) or {}
                    check("状态文件记下裁决（非 guard → standalone 原因为空）",
                          payload.get("multiplex") is True
                          and payload.get("multiplex_source") == "default"
                          and payload.get("multiplex_standalone_reason") is None, str(payload))
                    check("★ 记裁决不能抹掉别人写的字段（state / pid 还在）",
                          payload.get("state") == "running" and payload.get("pid") == os.getpid())
                    runner.shutdown()
                    # ★ 真跑逼出来的不变量：`write_runtime_status` 是**全量写**，
                    #   关机写 `stopped` 时若不带上裁决栏，状态文件里就查不到
                    #   "它为什么多路复用/为什么没多路复用"了（实测踩过）。
                    after = gw_status.read_runtime_status(
                        gw_status.state_file(default_home)) or {}
                    check("★ 关机写 stopped 之后，裁决栏仍然在（全量写不许抹掉它）",
                          after.get("state") == "stopped"
                          and after.get("multiplex") is True
                          and after.get("multiplex_source") == "default", str(after))

            # ── guard：理由必须落盘；预检崩了不许把网关带走 ──
            mm.record_multiplex_decision(default_home,
                                         mm.MultiplexDecision(False, "guard", "有人还在跑"))
            payload = gw_status.read_runtime_status(gw_status.state_file(default_home)) or {}
            check("guard → 写 multiplex_standalone_reason（运维查得到为什么没开）",
                  payload.get("multiplex_standalone_reason") == "有人还在跑", str(payload))
            with mock.patch("gateway.multiplex_mode.implicit_multiplex_blocker",
                            side_effect=RuntimeError("boom")):
                decision = None
                escaped = None
                try:
                    decision = mm.resolve_multiplex_mode(None)
                except Exception as exc:              # ★ 兜住：若把兜底删掉，
                    escaped = exc                     #   这一条要"可诊断地红"，
            check("预检抛异常 → guard（a broken preflight must not take the gateway down）",
                  escaped is None and decision is not None and decision.enabled is False
                  and decision.source == "guard" and "预检失败" in decision.reason,
                  f"escaped={escaped!r} decision={decision}")
    finally:
        if saved is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = saved


def test_secret_whitelist_and_dotenv_boundary():
    """★ 第 9 章 A9+A13：密钥的**全局白名单**（管"读"）与 dotenv 的**多路复用边界**（管"灌"）。

    两件事同一根因：**进程环境不是 profile 的存储**。
    没有白名单 → 多路复用下读 `PATH` 会抛、或拿到空值；
    没有加载边界 → 别人的 `.env` 会先进 `os.environ`，之后任何读进程环境的代码都绕过作用域。
    """
    from agent import secret_scope as ss

    with temp_profiles_home() as default_home:
        saved_path = os.environ.get("PATH")
        saved_openai = os.environ.get("OPENAI_API_KEY")
        os.environ["PATH"] = "/usr/bin:/bin"
        # ★ 这个键**故意**在进程环境里放一个值：只有这样，"作用域未命中 → 返回 default"
        #   才能和"作用域未命中 → 回落 os.environ"区分开（反向验证 ⑩ 抓到的缺口）。
        os.environ["OPENAI_API_KEY"] = "sk-process"
        ss.set_multiplex_active(True)
        try:
            token = ss.set_secret_scope({"DEEPSEEK_API_KEY": "sk-profile"})
            check("白名单键（PATH）在有作用域时读**进程真值**",
                  ss.get_secret("PATH") == "/usr/bin:/bin")
            check("白名单键（MINI_HOME）同样豁免",
                  ss.get_secret("MINI_HOME") == os.environ.get("MINI_HOME"))
            check("作用域里的凭据 → 读作用域", ss.get_secret("DEEPSEEK_API_KEY") == "sk-profile")
            check("作用域未命中 → 返回 default（**不**回落 os.environ —— 进程里明明有 sk-process）",
                  ss.get_secret("OPENAI_API_KEY", "dflt") == "dflt")
            ss.reset_secret_scope(token)
            try:
                ss.get_secret("DEEPSEEK_API_KEY")
                check("无作用域 + 多路复用 → 抛 UnscopedSecretError", False)
            except ss.UnscopedSecretError:
                check("无作用域 + 多路复用 → 抛 UnscopedSecretError", True)
            check("凭据键**不在**白名单（MINI_SEARCH_API_KEY）",
                  ss.is_global_env("MINI_SEARCH_API_KEY") is False)
            check("★ 没有 `MINI_` 前缀（加它 = 把凭据放行）", ss.GLOBAL_ENV_PREFIXES == ())
            check("行为设置也不进名单（拿不准就当 profile 密钥）",
                  ss.is_global_env("MINI_CORE_TOOLS") is False)
        finally:
            ss.set_multiplex_active(False)
            if saved_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = saved_path
            if saved_openai is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = saved_openai

        # ── A13：dotenv 加载器的边界（对应真实版 env_loader.py:340-350）──
        from hermes_cli import runtime_provider as rp
        from hermes_constants import reset_mini_home_override, set_mini_home_override

        env_file = default_home / "fake.env"
        env_file.write_text("MINI_T_LEAK=leaked\n", encoding="utf-8")
        saved_env_file = os.environ.get("MINI_ENV_FILE")
        os.environ["MINI_ENV_FILE"] = str(env_file)
        os.environ.pop("MINI_T_LEAK", None)
        try:
            used = rp.load_env_file()
            check("没有边界时照旧加载（启动期要能读默认 profile 的 .env）",
                  used == [env_file] and os.environ.get("MINI_T_LEAK") == "leaked", str(used))

            os.environ.pop("MINI_T_LEAK", None)
            ss.set_multiplex_active(True)
            rp._SCOPED_SKIP_LOGGED.clear()
            token = set_mini_home_override(str(default_home / "profiles" / "coder"))
            try:
                used = rp.load_env_file()
            finally:
                reset_mini_home_override(token)
            check("多路复用 + 有 profile-home 覆盖 → **跳过**进程级加载",
                  used == [] and "MINI_T_LEAK" not in os.environ, str(used))
            check("跳过只记一次（每个 home 一条，不刷屏）", len(rp._SCOPED_SKIP_LOGGED) == 1)

            # ★ 边界是"**两个条件都要**"：多路复用开着但**没有** home 覆盖 = 启动期那道加载，
            #   必须照旧 —— 否则默认 profile 自己的 .env 都读不进来，网关根本起不来。
            os.environ.pop("MINI_T_LEAK", None)
            used = rp.load_env_file()
            check("多路复用开着但**没有** home 覆盖 → 照旧加载（启动期）",
                  used == [env_file] and os.environ.get("MINI_T_LEAK") == "leaked", str(used))
            os.environ.pop("MINI_T_LEAK", None)
            ss.set_multiplex_active(False)
        finally:
            if saved_env_file is None:
                os.environ.pop("MINI_ENV_FILE", None)
            else:
                os.environ["MINI_ENV_FILE"] = saved_env_file
            os.environ.pop("MINI_T_LEAK", None)


GROUPS = [
    ("1. 会话键", "唯一来源 · 布局 · 退路", [test_session_key_is_single_source]),
    ("2. 命名空间", "默认 main / 真名 main → main~", [test_namespace_and_main_tilde]),
    ("3. 授权", "四层顺序 · 默认拒绝 · 说得出理由", [test_authz_layers_and_default_deny]),
    ("4. 命令解析", "规范名/别名/唯一前缀 · 歧义不猜", [test_command_resolution]),
    ("5. 第 1 级守卫", "排队 · 内联 · 陈旧锁自愈", [test_guard1_queue_and_inline, test_stale_guard_self_heals]),
    ("6. 第 2 级守卫", "忙时策略三态", [test_guard2_busy_policy]),
    ("7. 顺序不变量", "忙时处置先于命令分发", [test_busy_check_precedes_command_dispatch]),
    ("8. 会话归属", "键落库 · 找回 · /new 之后找回新的", [test_session_ownership_and_reset]),
    ("9. 交付", "直接回复 · 指定目标 · 缺适配器报错", [test_delivery_routes]),
    ("10. 钩子", "发现 · 分发 · 坏钩子隔离 · 路径调用时解析", [test_hooks_registry]),
    ("11. 进程身份", "PID+指纹 · 状态记录 · 退出码 78", [test_process_identity, test_gateway_single_instance_exit_code]),
    ("12. profile 作用域", "contextvar · fail-closed · A→B→A", [test_secret_scope_fail_closed, test_profile_scope_aba]),
    ("13. profile 枚举", "HOME 锚定 · profiles_to_serve", [test_profiles_to_serve_and_gateway_status]),
    ("14. profile 入口层（★ 侧线）", "create/播种 · id 字母表 · clone 不带记忆 · 解析不静默回落 · 物理分家 · 真 CLI",
     [test_profile_create_and_seed, test_profile_name_alphabet,
      test_profile_clone_keeps_config_not_memory,
      test_profile_resolve_never_silently_falls_back,
      test_two_profiles_share_nothing, test_profile_cli_end_to_end]),
    ("15. 会话生命周期（★ 第 8 章）", "硬>软 · 时间不轮转 · 恢复标记与扫描 · 卡死升级 + 计数",
     [test_lifecycle_priority_and_hard_wins, test_explicit_boundary_never_time_based,
      test_restart_recovery_marker_and_sweep, test_stuck_loop_escalation_and_completion_reset]),
    ("16. 两层队列（★ 第 8 章）", "单槽覆盖 + 通知 · 溢出 FIFO 不合并 · 先承诺后最新 · 回执",
     [test_busy_slot_overwrites_and_notifies, test_explicit_queue_never_merges_and_runs_in_order,
      test_gateway_accepted_receipt]),
    ("17. 缓存与巡检（★ 第 8 章）", "命中/轮转必淘汰 · 三条不许淘汰 · 容量 LRU · 压力过线 · tick 与剪枝开关",
     [test_agent_cache_lifecycle_and_three_protections,
      test_housekeeping_tick_prune_switch_and_thread]),
    ("18. 多路复用开关（★ 第 9 章）", "三态 · 词表 · 预检三条 · 裁决不致命 · 理由落盘 · 端到端",
     [test_multiplex_mode_three_states_and_preflight]),
    ("19. 密钥白名单与 dotenv 边界（★ 第 9 章）", "白名单豁免进程键 · 凭据不进名单 · 有覆盖就跳过加载",
     [test_secret_whitelist_and_dotenv_boundary]),
]


def main() -> None:
    for title, promise, fns in GROUPS:
        print(f"══ {title} ══ {promise}")
        for fn in fns:
            # ★ 用例抛异常 = 一条**可诊断的红**（不是"套件崩了"）——
            #   反向验证要求"改回旧写法 → 干净变红"。
            try:
                fn()
            except Exception as exc:                  # noqa: BLE001
                check(f"{fn.__name__} 抛异常", False, f"{type(exc).__name__}: {exc}")
        print(f"   → {len(PASS)} 累计 / {len(FAIL)} 失败\n")

    print(f"{len(PASS)} 通过 / {len(FAIL)} 失败　（{len(GROUPS)} 组）")
    if FAIL:
        print("失败项：" + "; ".join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
