"""发布前审查的跨模块回归测试：真实 agent / 网关 / SQLite，只替换网络。

运行：python3 tests/test_publish_regressions.py。每项独立临时 HOME；不读个人凭据。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.context_engine import ContextEngine
from agent.secret_scope import get_secret, set_multiplex_active
from cli import MiniCLI
from gateway.authz import GatewayAuthz
from gateway.profile_scope import profile_runtime_scope
from gateway.run import GatewayRunner
from hermes_constants import get_mini_home
from hermes_state import SessionDB
from hermes_state_schema import SchemaTooNewError
from run_agent import AIAgent, AgentInterrupted, ApiHTTPError

PASS, FAIL = [], []
SPEC = {"provider": "deepseek", "api_mode": "chat_completions",
        "base_url": "https://example.invalid/v1", "api_key": "test-key-not-real",
        "model": "deepseek-chat", "context_length": 128000}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✓' if cond else '✗'} {name}" + (f" → {detail}" if detail and not cond else ""))


@contextlib.contextmanager
def isolated_home():
    root = Path(tempfile.mkdtemp(prefix="mini-publish-test-"))
    home = root / "home"; home.mkdir()
    base = home / ".mini-hermes"; base.mkdir()
    (base / ".env").write_text("DEEPSEEK_API_KEY=test-key-not-real\n", encoding="utf-8")
    env = {"HOME": str(home), "MINI_HOME": str(base), "MINI_ENV_FILE": str(base / ".env"),
           "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
           "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(root)}
    try:
        with patch.dict(os.environ, env, clear=True):
            yield root, base
    finally:
        set_multiplex_active(False)
        shutil.rmtree(root)


def response(text="answer"):
    return {"choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}}


def make_runner(home):
    return GatewayRunner(home=home, authz=GatewayAuthz(platform_allow_all=True), echo=False)


def feed(runner, text):
    adapter = runner.adapters["console"]
    ev = adapter.make_event(text)
    key = runner._key_for_source(ev.source)
    adapter.handle_message(ev)
    if not adapter.wait_idle(key, 10):
        raise RuntimeError("网关测试回合未在限时内结束")
    return key, adapter.last_result(key)


def close_runner(runner):
    for db in runner._dbs.values():
        db.close()


def test_gateway_history_and_restarts():
    with isolated_home() as (_, home):
        requests = []
        def post(agent, cand, messages):
            requests.append([dict(m) for m in messages])
            return response()
        runner = make_runner(home)
        try:
            with patch.object(AIAgent, "_http_post", post):
                key, first = feed(runner, "remember marker")
                _, second = feed(runner, "what marker?")
            users = [m["content"] for m in requests[-1] if m["role"] == "user"]
            check("F01 第二轮模型请求带上一轮用户消息", users == ["remember marker", "what marker?"], str(users))
            check("F01 第二轮带上一轮模型回答", any(m.get("content") == "answer" for m in requests[-1]))
            sid = runner._session_ids[key]
            check("F01 两轮消息逐条落库且不重复", runner._db_for(home).count_messages(sid) == 4)
        finally:
            close_runner(runner)
        runner2 = make_runner(home)
        try:
            with patch.object(AIAgent, "_http_post", post):
                key, _ = feed(runner2, "after restart")
            check("F01 重建网关后仍复用原会话", runner2._session_ids[key] == sid)
            check("F01 重建后请求仍带重启前历史", len([m for m in requests[-1] if m["role"] == "user"]) == 3)
        finally:
            close_runner(runner2)


def test_thread_profile_scopes():
    with isolated_home() as (_, home):
        secondary = home / "profiles" / "lab"; secondary.mkdir(parents=True)
        (secondary / ".env").write_text("AUDIT_API_KEY=test-scope-key\n", encoding="utf-8")
        seen = {}
        def post(agent, cand, messages):
            seen["home"] = get_mini_home()
            seen["key_correct"] = get_secret("AUDIT_API_KEY") == "test-scope-key"
            return response()
        set_multiplex_active(True)
        with profile_runtime_scope(secondary), patch.object(AIAgent, "_http_post", post):
            agent = AIAgent(SPEC, fallback_providers=[])
            result = agent._interruptible_api_call([{"role": "user", "content": "scope"}], "scope")
        check("F02 API worker 保留所属 profile 的 home", seen.get("home") == secondary)
        check("F02 多路复用 worker 能读取所属作用域的假凭据", seen.get("key_correct") and bool(result))


def test_parallel_skills_scope():
    with isolated_home() as (_, home):
        secondary = home / "profiles" / "lab"
        for directory, text in ((home, "DEFAULT_BODY"), (secondary, "LAB_BODY")):
            d = directory / "skills" / "audit"; d.mkdir(parents=True)
            (d / "SKILL.md").write_text("---\nname: audit\ndescription: audit\n---\n" + text, encoding="utf-8")
        calls = [{"id": str(i), "function": {"name": "skill_view", "arguments": '{"name":"audit"}'}} for i in range(2)]
        with profile_runtime_scope(secondary):
            agent = AIAgent(SPEC, fallback_providers=[])
            messages = []
            agent._execute_tool_calls({}, calls, messages, "scope")
        bodies = [json.loads(m["content"]).get("content") for m in messages]
        check("F03 两个并行技能调用都读所属 profile", bodies == ["LAB_BODY", "LAB_BODY"], str(bodies))
        check("F03 并行完成后调用线程作用域已复位", get_mini_home() == home)


def test_tool_probe_cache_profile_isolation():
    from tools import registry
    with isolated_home() as (_, home):
        secondary = home / "profiles" / "lab"; secondary.mkdir(parents=True)
        (home / ".env").write_text("AUDIT_API_KEY=test-key-not-real\n", encoding="utf-8")
        def probe(): return bool(get_secret("AUDIT_API_KEY"))
        registry.invalidate_check_fn_cache()
        try:
            with profile_runtime_scope(home): a = registry.check_fn_cached(probe)
            with profile_runtime_scope(secondary): b = registry.check_fn_cached(probe)
            with profile_runtime_scope(home): again = registry.check_fn_cached(probe)
            check("profile 工具探测缓存按 home 隔离", a and not b and again)
        finally:
            registry.invalidate_check_fn_cache()


def test_subcommands_honor_profile():
    from hermes_cli.main import main
    from hermes_cli.gateway_cmd import cmd_stop
    from argparse import Namespace
    with isolated_home() as (_, home):
        lab = home / "profiles" / "lab"; lab.mkdir(parents=True)
        with SessionDB(lab / "state.db") as db:
            db.create_session("lab", "cli")
        paths = []
        with patch("hermes_cli.sessions_cmd._check_only", side_effect=lambda p: paths.append(p) or 0), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(["-p", "lab", "sessions", "repair", "--check-only"])
        check("F04 sessions repair 检查所选 profile 的库", code == 0 and paths == [lab / "state.db"], str(paths))
        with patch("gateway.status.live_gateway_pid_for_home", return_value=None) as lookup, contextlib.redirect_stdout(io.StringIO()):
            main(["-p", "lab", "gateway", "status", "--no-multiplex"])
        check("F04 gateway status 读取所选 home", lookup.call_args_list and lookup.call_args_list[0].args[0] == lab)
        with patch("gateway.status.live_gateway_pid_for_home", return_value=None) as lookup, contextlib.redirect_stdout(io.StringIO()):
            cmd_stop(Namespace(all=False))
        check("F04 gateway stop 使用当前 home", lookup.call_args_list and lookup.call_args_list[0].args[0] == lab)
        with patch("gateway.run.GatewayRunner") as cls:
            cls.return_value.run.return_value = 0
            with contextlib.redirect_stderr(io.StringIO()): main(["-p", "lab", "gateway", "run"])
            check("F04 gateway run 构造前已选定 home", get_mini_home() == lab)
        before = os.environ["MINI_HOME"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(["-p", "missing", "sessions", "repair", "--check-only"])
        check("F04 不存在的 profile 明确失败且不换 home", code == 2 and os.environ["MINI_HOME"] == before)


def test_incomplete_turn_can_continue():
    for problem in (AgentInterrupted(), RuntimeError("fake network error")):
        with isolated_home() as (_, home), SessionDB(home / "state.db") as db:
            db.create_session("s", "cli")
            agent = AIAgent(SPEC, fallback_providers=[], session_db=db, session_id="s", skip_context_files=True)
            with patch.object(agent, "_interruptible_api_call", side_effect=problem), patch.object(agent, "_log_exception"):
                first = agent.run_conversation("keep original question")
            with patch.object(agent, "_http_post", return_value=response("continued")), patch.object(agent, "_log_exception"):
                second = agent.run_conversation("continue", conversation_history=first["messages"])
            check("F05 中断/失败后仍可继续：" + type(problem).__name__, not second.get("error") and second["final_response"] == "continued", second.get("error", ""))
            check("F05 未完成轮次仍保留用户原话：" + type(problem).__name__, any(m.get("content") == "keep original question" for m in second["messages"]))
            fresh = AIAgent(SPEC, fallback_providers=[], session_db=db, session_id="s", skip_context_files=True)
            with patch.object(fresh, "_http_post", return_value=response("resumed")), patch.object(fresh, "_log_exception"):
                third = fresh.run_conversation("resume")
            check("F05 重新读库后的会话仍可继续：" + type(problem).__name__, not third.get("error") and third["final_response"] == "resumed")
    with isolated_home() as (_, home), SessionDB(home / "state.db") as db:
        db.create_session("crash", "cli"); db.append_message("crash", "user", "before crash")
        agent = AIAgent(SPEC, fallback_providers=[], session_db=db, session_id="crash", skip_context_files=True)
        with patch.object(agent, "_http_post", return_value=response()), patch.object(agent, "_log_exception"):
            result = agent.run_conversation("after crash")
        check("F05 旧库末尾只有 user 的未完成轮次可恢复", not result.get("error"), result.get("error", ""))


def test_narrow_engine_contract():
    class Narrow(ContextEngine):
        name = "narrow"
        def should_compress(self, messages): return self.compress_now
        def compress(self, messages):
            self.called = True
            return list(messages)
    with isolated_home():
        engine = Narrow(); engine.compress_now = False; engine.called = False
        agent = AIAgent(SPEC, fallback_providers=[], skip_context_files=True)
        agent._compressor_obj = engine
        with patch.object(agent, "_http_post", return_value=response()), patch.object(agent, "_log_exception"):
            no_compress = agent.run_conversation("no compression")
            engine.compress_now = True
            compressed = agent.run_conversation("compression")
        check("F06 最小引擎未触发压缩时正常运行", not no_compress.get("error"), no_compress.get("error", ""))
        check("F06 窄 compress 签名可接受宿主调用", engine.called and not compressed.get("error"), compressed.get("error", ""))


def test_failed_tool_batch_can_resume():
    from agent.context_compressor import validate_tool_pairs
    with isolated_home() as (_, home), SessionDB(home / "state.db") as db:
        db.create_session("s", "cli")
        target = home / "effect.txt"
        calls = [{"id": "write", "function": {"name": "write_file", "arguments": json.dumps({"path": str(target), "content": "effect"})}},
                 {"id": "bad", "function": {"name": "todo_list", "arguments": '{"todos":42}'}}]
        agent = AIAgent(SPEC, fallback_providers=[], session_db=db, session_id="s", skip_context_files=True)
        with patch.object(agent, "_http_post", return_value={"choices": [{"message": {"tool_calls": calls}}]}), patch.object(agent, "_log_exception"):
            failed = agent.run_conversation("do both")
        check("F05 并行工具异常不抹掉已经发生的副作用", target.read_text() == "effect")
        check("F05 未完成调用有可诊断结果，工具配对合法", bool(failed.get("error")) and not validate_tool_pairs(failed["messages"]))
        check("F05 占位结果如实声明未知，未假称没有执行", any("未知" in m.get("content", "") for m in failed["messages"] if m["role"] == "tool"))
        with patch.object(agent, "_http_post", return_value=response("continue")):
            resumed = agent.run_conversation("continue")
        check("F05 并行工具失败后从库恢复也能继续", not resumed.get("error"))


def test_like_filters_and_snippets():
    with isolated_home() as (_, home), SessionDB(home / "state.db") as db:
        db.create_session("s", "cli")
        db.append_message("s", "user", "x" * 200 + "审计目标" + "y" * 200)
        for stale in (False, True):
            if stale:
                db._fts_stale = True
            hits = db.search_messages("审计目标", source_filter=["cli"], role_filter=["user"], exclude_sources=["gateway:console"])
            check("F07 LIKE 过滤不漏命中：" + str(stale), len(hits) == 1)
            check("F07 LIKE 片段包含命中词：" + str(stale), bool(hits and "审计目标" in hits[0]["snippet"]))
            check("F07 LIKE 仍排除错误来源：" + str(stale), not db.search_messages("审计目标", source_filter=["tui"]))


def test_future_db_not_modified():
    with isolated_home() as (_, home):
        p = home / "future.db"
        conn = sqlite3.connect(p)
        conn.executescript("CREATE TABLE schema_version(version INTEGER); INSERT INTO schema_version VALUES(999);")
        before_schema = conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
        before_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]; conn.close()
        before_bytes = p.read_bytes()
        rejected = False
        try:
            db = SessionDB(p); db.close()
        except SchemaTooNewError:
            rejected = True
        conn = sqlite3.connect(p)
        after_schema = conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
        after_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]; conn.close()
        check("F08 版本过新的库被明确拒绝", rejected)
        check("F08 被拒绝的库 schema 没有变化", before_schema == after_schema)
        check("F08 被拒绝的库 journal mode 没有变化", before_mode == after_mode)
        check("F08 被拒绝的库文件字节没有变化", before_bytes == p.read_bytes())
        check("F08 拒绝过程不创建 WAL 侧文件", not Path(str(p) + "-wal").exists())


def test_http_scheme_and_query():
    import run_agent
    with isolated_home():
        calls = []
        class FakeConnection:
            status = 200
            def __init__(self, kind, host, port): calls.append([kind, host, port])
            def request(self, method, path, **kwargs): calls[-1].append(path)
            def getresponse(self): return self
            def read(self): return json.dumps(response()).encode()
            def close(self): pass
        http = lambda host, port: FakeConnection("http", host, port)
        https = lambda host, port: FakeConnection("https", host, port)
        with patch.object(run_agent, "_TwoTimeoutHTTPConnection", http, create=True), patch.object(run_agent, "_TwoTimeoutHTTPSConnection", https):
            agent = AIAgent(SPEC, fallback_providers=[])
            for url in ("http://localhost/v1?route=test", "https://example.invalid/v1", "http://localhost:8000/v1"):
                agent._http_post(dict(SPEC, base_url=url), [{"role": "user", "content": "audit"}])
        check("F09 HTTP 使用明文连接和默认 80 端口", calls[0][:3] == ["http", "localhost", 80], str(calls))
        check("F09 保留 URL query 且正确追加请求路径", calls[0][3] == "/v1/chat/completions?route=test", str(calls[0]))
        check("F09 HTTPS 保持 TLS 和默认 443 端口", calls[1][:3] == ["https", "example.invalid", 443])
        check("F09 自定义 HTTP 端口保持不变", calls[2][:3] == ["http", "localhost", 8000])


def test_approval_owner_and_cleanup():
    with isolated_home() as (_, home):
        runner = make_runner(home); adapter = runner.adapters["console"]
        event = adapter.make_event("request", chat_id="A"); event.session_key = runner._key_for_source(event.source)
        other = adapter.make_event("/approve", chat_id="B"); other.session_key = runner._key_for_source(other.source)
        result = {}; errors = []
        def ask():
            try: result["approved"] = runner._approval_callback("fake risky command", event=event)
            except Exception as exc: errors.append(type(exc).__name__)
        task = threading.Thread(target=ask)
        task.start()
        try:
            deadline = time.monotonic() + 2
            while task.is_alive() and not adapter.outbox() and time.monotonic() < deadline: time.sleep(0.01)
            check("F11 审批通知确实发给用户", any("需要确认" in text for text in adapter.outbox()), str(errors))
            runner._resolve_approval(other, approved=True)
            check("F10 其他会话不能批准当前命令", task.is_alive() and not result.get("approved"))
            same_chat_other_user = adapter.make_event("/approve", chat_id="A", user_id="another")
            same_chat_other_user.session_key = event.session_key
            runner._resolve_approval(same_chat_other_user, approved=True)
            check("F10 同一聊天的其他用户也不能替发起者批准", task.is_alive() and not result.get("approved"))
            event.text = "/approve"
            runner._resolve_approval(event, approved=True)
            task.join(2)
            check("F11 发起者可正常批准且没有属性异常", result.get("approved") is True and not errors, str(errors))
            check("F11 审批完成后清理待确认状态", not getattr(runner, "_pending_approvals", {"old": 1}))
        finally:
            runner._resolve_approval(event, approved=False)
            task.join(2)


def test_api_call_counts_are_per_turn():
    with isolated_home() as (_, home), patch("cli.resolve_provider", return_value=SPEC), contextlib.redirect_stdout(io.StringIO()):
        app = MiniCLI()
        try:
            with patch.object(AIAgent, "_http_post", return_value=response()):
                first = app.one_shot("one"); second = app.one_shot("two")
            check("F12 两轮各自报告一次请求", first.get("api_calls") == second.get("api_calls") == 1)
            check("F12 两轮累计实际调用数是 2", app.db.get_session(app.session_id)["api_call_count"] == 2)
            check("F12 迭代数按本轮返回，预算仍按会话累计", first["iterations_used"] == second["iterations_used"] == 1 and app.agent.budget.used == 2)
            calls = []
            def retry(cand, messages):
                calls.append(cand)
                if len(calls) == 1: raise ApiHTTPError(429, "fake")
                return response()
            with patch.object(app.agent, "_http_post", retry), patch.object(app.agent, "_try_activate_fallback", return_value=True):
                third = app.one_shot("fallback")
            check("F12 CLI 按请求数落库，包含同一迭代的回退", third.get("api_calls") == 2 and app.db.get_session(app.session_id)["api_call_count"] == 4)
        finally:
            app.db.close()
    with isolated_home():
        agent = AIAgent(SPEC, fallback_providers=[], skip_context_files=True)
        attempt = []
        def post(cand, messages):
            attempt.append(cand)
            if len(attempt) == 1: raise ApiHTTPError(429, "fake")
            return response()
        def fallback(**kw): return True
        with patch.object(agent, "_http_post", post), patch.object(agent, "_try_activate_fallback", fallback):
            result = agent.run_conversation("retry")
        check("F12 回退重发算请求，不算新迭代", result.get("api_calls") == 2 and result["iterations_used"] == 1)
        before = agent._api_call_count
        with patch.object(agent, "_http_post", return_value=response()): agent._summary_llm_meta("summary", "history")
        check("F12 辅助摘要也计入请求数", agent._api_call_count == before + 1)


def test_profile_names_do_not_escape():
    from hermes_cli.profiles import resolve_profile_home, ProfileError
    with isolated_home() as (_, home):
        lab = home / "profiles" / "lab"; lab.mkdir(parents=True)
        for name in (str(home), "../", "../../", "name with spaces"):
            rejected = False
            try: resolve_profile_home(name)
            except ProfileError: rejected = True
            check("F14 选择 profile 拒绝非法路径：" + ("absolute" if name.startswith("/") else name), rejected)
        check("F14 合法名字仍可解析", resolve_profile_home("lab") == lab)
        check("F14 default 保留名仍指向默认 home", resolve_profile_home("default") == home)


def test_new_clears_session_state():
    with isolated_home(), patch("cli.resolve_provider", return_value=SPEC), contextlib.redirect_stdout(io.StringIO()):
        app = MiniCLI()
        old_id = app.session_id
        app.agent.todo.write([{"id": "old", "content": "OLD_TASK", "status": "pending"}])
        compressor = app.agent._compressor(); compressor._previous_summary = "OLD_SUMMARY"
        compressor._micro_rolling_summary = "OLD_MICRO"
        with patch("builtins.input", side_effect=["/new", "/exit"]): app.repl()
        check("F15 /new 开新会话", app.session_id != old_id)
        check("F15 /new 清空旧任务", not app.agent.todo.read())
        new_compressor = app.agent._compressor()
        check("F15 /new 清空压缩摘要", not new_compressor._previous_summary)
        check("F15 /new 清空微压缩摘要", not new_compressor._micro_rolling_summary)
        check("F15 /new 新提示词中不带旧会话 id", app.agent._cached_system_prompt is None)


def test_terminal_nonzero_is_visible():
    from tools.terminal import terminal_tool
    from run_agent import _tool_result_observer_fields, summarize_tool_result
    with isolated_home():
        raw = terminal_tool("exit 7")
        fields = _tool_result_observer_fields(raw)
        check("F16 终端仍保留非零退出码", json.loads(raw)["exit_code"] == 7)
        check("F16 非零退出在进度与观察者上可见", fields[0] == "error")
        check("F16 摘要带退出码", "7" in summarize_tool_result(raw, fields[0], fields[2]))


def test_route_state_not_bypassed():
    with isolated_home() as (_, home):
        runner = make_runner(home)
        try:
            with patch.object(AIAgent, "_http_post", return_value=response()):
                key, _ = feed(runner, "first")
                sid = runner._session_ids[key]; db = runner._db_for(home)
                db._execute_write(lambda conn: conn.execute("UPDATE sessions SET updated_at=0 WHERE id=?", (sid,)))
                feed(runner, "second")
                check("F17 缓存命中也更新用户活动时间", db.get_session(sid)["updated_at"] > 0)
                db.set_route_state(sid, suspended=True)
                feed(runner, "after suspension")
                check("F17 挂起会话必须轮转", runner._session_ids[key] != sid)
                check("F17 轮转后的模型不带旧会话历史", db.count_messages(runner._session_ids[key]) == 2)
        finally:
            close_runner(runner)


def test_budget_exhaustion_exit_status():
    from hermes_cli.main import main
    with isolated_home(), patch("cli.resolve_provider", return_value=SPEC), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        code = main(["--max-turns", "0", "-q", "audit"])
        check("F18 任务未完成时 CLI 返回非零退出码", code != 0)


def test_exception_releases_busy_cache():
    with isolated_home() as (_, home):
        runner = make_runner(home)
        try:
            with patch.object(AIAgent, "run_conversation", side_effect=RuntimeError("fake setup failure")):
                key, result = feed(runner, "fail")
            entry = runner._agent_cache.entry(key)
            check("F19 前置异常仍向用户报告失败", result["action"] == "error")
            check("F19 前置异常后缓存 busy 被清理", not entry.busy)
            check("F19 未确认落盘时仍保留 pending_flush 保护", entry.pending_flush)
        finally:
            close_runner(runner)


def test_gateway_stop_uses_shutdown():
    with isolated_home() as (_, home):
        env = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT))
        child = None
        try:
            code = "from gateway.run import GatewayRunner; raise SystemExit(GatewayRunner(echo=False,multiplex=False).run())"
            child = subprocess.Popen([sys.executable, "-c", code], env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            ready = select.select([child.stdout], [], [], 5)[0]
            if not ready or "console" not in child.stdout.readline():
                raise RuntimeError("测试网关未进入输入循环")
            stop = subprocess.run([sys.executable, "-m", "hermes_cli.main", "gateway", "stop"],
                                  env=env, capture_output=True, text=True, timeout=5)
            check("F20 stop 找到正确 home 的网关", "SIGTERM" in stop.stdout, stop.stdout)
            if "SIGTERM" not in stop.stdout:
                child.send_signal(signal.SIGTERM)       # 沙箱不能查 PID 时仍验证信号本身
            child.communicate(timeout=10)
            state = json.loads((home / "gateway_state.json").read_text())
            check("F20 SIGTERM 通过有序收尾退出", child.returncode == 0, str(child.returncode))
            check("F20 停止后的状态为 stopped", state["state"] == "stopped", state["state"])
            check("F20 停止后写干净退出标记", (home / ".clean_shutdown").exists())
            check("F20 停止后删除自己写的 PID 文件", not (home / "gateway.pid").exists())
        finally:
            if child is not None and child.poll() is None:
                child.kill(); child.communicate()


def test_shutdown_drains_active_turn():
    with isolated_home() as (_, home):
        gate = threading.Event(); entered = threading.Event()
        def post(agent, cand, messages):
            entered.set(); gate.wait(5)
            return response()
        runner = make_runner(home)
        try:
            with patch.object(AIAgent, "_http_post", post):
                adapter = runner.adapters["console"]
                event = adapter.make_event("running")
                key = runner._key_for_source(event.source)
                adapter.handle_message(event)
                if not entered.wait(2): raise RuntimeError("测试请求未开始")
                sid = runner._session_ids[key]
                runner.shutdown()
                check("F20 活跃回合被中断且 worker 回合已结束", not runner._running_agents and adapter.wait_idle(key, 1))
                with SessionDB(home / "state.db", read_only=True) as db:
                    row = db.get_session(sid)
                    check("F20 活跃会话退出后保持 open 和软恢复标记", row["ended_at"] is None and bool(row["resume_pending"]))
                    check("F20 中断结尾已落盘", db.get_messages_as_conversation(sid)[-1]["role"] == "assistant")
                check("F20 drained 后释放数据库连接", not runner._dbs)
        finally:
            gate.set()
            runner.adapters["console"].wait_idle(key, 5)
            close_runner(runner)


def test_root_readme_launcher():
    repo = PROJECT_ROOT.parent
    readme = (repo / "README.md").read_text(encoding="utf-8")
    check("D01 根 README 写出实际存在的启动器", "./mini-hermes/mini --list-toolsets" in readme)
    with isolated_home():
        result = subprocess.run(["bash", str(PROJECT_ROOT / "mini"), "--list-toolsets"],
                                cwd=repo, capture_output=True, text=True, timeout=10)
        check("D01 从仓库根调用 README 启动器成功", result.returncode == 0, result.stderr)


GROUPS = [
    ("会话历史与恢复", [test_gateway_history_and_restarts, test_incomplete_turn_can_continue, test_failed_tool_batch_can_resume, test_new_clears_session_state]),
    ("profile 隔离与 CLI", [test_thread_profile_scopes, test_parallel_skills_scope, test_tool_probe_cache_profile_isolation,
                         test_subcommands_honor_profile, test_profile_names_do_not_escape]),
    ("引擎/传输/计数", [test_narrow_engine_contract, test_http_scheme_and_query, test_api_call_counts_are_per_turn]),
    ("存储保护与检索", [test_future_db_not_modified, test_like_filters_and_snippets]),
    ("审批/状态/退出", [test_approval_owner_and_cleanup, test_route_state_not_bypassed, test_budget_exhaustion_exit_status,
                       test_exception_releases_busy_cache, test_terminal_nonzero_is_visible, test_gateway_stop_uses_shutdown, test_shutdown_drains_active_turn]),
    ("发布入口", [test_root_readme_launcher]),
]


def main():
    for title, tests in GROUPS:
        print(f"══ {title} ══")
        for test in tests:
            try: test()
            except Exception as exc: check(test.__name__ + " 抛异常", False, type(exc).__name__ + ": " + str(exc))
    print(f"\n{len(PASS)} 通过 / {len(FAIL)} 失败　（{len(GROUPS)} 组）")
    if FAIL: print("失败项：" + "; ".join(FAIL))
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
