"""Loopback-only demo UI: real agent + observers, scripted offline provider, SSE.

Run: python3 -m hermes_cli.main --web
The UI's session list lives in memory. Real runs persist messages in MINI_HOME/web/state.db;
offline runs use an owned temporary project and database, removed on shutdown.
"""
from __future__ import annotations

import json
import math
import queue
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from agent.iteration_budget import IterationBudget
from agent.observer import OBSERVER_EVENTS, ObserverRegistry, hook_jsonable
from agent.secret_scope import reset_secret_scope, set_secret_scope
from hermes_constants import (get_mini_home, reset_mini_home_override,
                              resolve_context_cwd, set_mini_home_override)
from hermes_cli.runtime_provider import resolve_provider
from hermes_state import SessionDB
from run_agent import AIAgent, ApiHTTPError
from hermes_cli import web_lessons
from hermes_cli.web_prompts import capture_request

ASSETS = Path(__file__).with_name("web_assets")
MAX_BODY = 32_768


class WebError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Turn:
    def __init__(self, session_id, redact=lambda value: value):
        self.session_id = session_id
        self.redact = redact
        self.traces = []
        self.events = queue.Queue(maxsize=128)
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self.approval = None
        self.approval_lock = threading.Lock()
        self.agent = None
        self.thread = None

    def stop(self):
        self.cancelled.set()
        if self.agent is not None:
            self.agent.interrupt()

    def emit(self, kind, /, **data):
        data = self.redact(data)
        if kind == "trace":
            self.traces.append({k: data.get(k) for k in ("event", "turn_id", "tool_name", "status", "tool_call_id")})
        item = {"event": kind, "data": hook_jsonable(data)}
        while True:
            if self.cancelled.is_set():
                if self.agent is not None:
                    self.agent.interrupt()
                if kind not in ("result", "error"):
                    return
                try:
                    self.events.put_nowait(item)
                except queue.Full:
                    self.events.get_nowait()
                    self.events.put_nowait(item)
                return
            try:
                self.events.put(item, timeout=0.2)
                return
            except queue.Full:
                pass

    def approve(self, command):
        # Several tools can request approval in parallel; the page has one dialog.
        with self.approval_lock:
            return self._approve_one(command)

    def _approve_one(self, command):
        gate = {"id": uuid.uuid4().hex, "ready": threading.Event(), "allowed": False}
        self.approval = gate
        self.emit("approval", approval_id=gate["id"], command=command)
        deadline = time.monotonic() + 90
        try:
            while not self.cancelled.is_set() and time.monotonic() < deadline:
                if gate["ready"].wait(0.2):
                    return gate["allowed"] and not self.cancelled.is_set()
            return False
        finally:
            self.approval = None


class WebApp:
    """Single active turn, bounded in-memory sessions; no process-global monkeypatches."""
    def __init__(self, provider="deepseek", model=None, max_turns=12):
        self.provider, self.model, self.max_turns = provider, model, max_turns
        self.live_secrets = {}
        self.web_model = "deepseek-flash"
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.sessions = {}
        self.active = None
        self.temp = TemporaryDirectory(prefix="mini-web-")
        self.root = Path(self.temp.name)
        self.project = self.root / "sample-project"
        self.project.mkdir()
        (self.project / "README.md").write_text(
            "# Sample project\n\n这是供 mini-hermes 离线演示使用的项目。\n"
            "main.py 是程序入口，运行后输出 Hello, agent!。\n", encoding="utf-8")
        (self.project / "main.py").write_text(
            'def main():\n    print("Hello, agent!")\n\nif __name__ == "__main__":\n    main()\n', encoding="utf-8")

        self.prompt_project = self.root / "prompt-project"
        self.prompt_project.mkdir()
        for filename in ("README.md", "main.py"):
            (self.prompt_project / filename).write_bytes((self.project / filename).read_bytes())
        (self.prompt_project / "AGENTS.md").write_text(
            "# Demo project rules\n先阅读 README.md，再根据文件内容回答；不要凭文件名猜测。\n", encoding="utf-8")

    def close(self):
        turn = self.active
        try:
            if turn:
                turn.stop()
                turn.thread.join(timeout=10)
        finally:
            self.live_secrets.clear()
            for session in self.sessions.values():
                session["agent"] = None
            self.temp.cleanup()

    def redact(self, value):
        # Redact BEFORE persistence/observer truncation, including provider error bodies.
        if isinstance(value, str):
            for secret in self.live_secrets.values():
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {self.redact(k): self.redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(v) for v in value]
        return value

    def model_config(self):
        return {"configured": bool(self.live_secrets), "model": self.web_model,
                "provider": "deepseek", "storage": "memory"}

    def configure_model(self, data):
        key, model = data.get("api_key"), data.get("model", self.web_model)
        clear = data.get("clear", False)
        if not isinstance(clear, bool) or not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", model):
            raise WebError("模型名或清除参数无效")
        if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in key)):
            raise WebError("API Key 必须是无空白的可打印英文字符")
        with self.lock:
            if self.active:
                raise WebError("请先等待当前回合结束或停止", 409)
            # Credentials never enter session dictionaries, browser storage or .env files.
            for session in self.sessions.values():
                if session["mode"] == "live":
                    session["agent"] = None
            if clear:
                self.live_secrets.clear()
            elif key is not None:
                self.live_secrets = {"DEEPSEEK_API_KEY": key}
            self.web_model = model
            return self.model_config()

    def create_session(self, mode, chapter_id="02"):
        try:
            web_lessons.chapter(chapter_id)
        except ValueError as exc:
            raise WebError(str(exc))
        if mode not in ("offline", "live"):
            raise WebError("未知运行模式")
        with self.lock:
            if len(self.sessions) >= 20:
                raise WebError("本次运行最多保留 20 个会话，请重启演示服务", 409)
            sid = uuid.uuid4().hex
            session = {"id": sid, "mode": mode, "title": "新会话", "messages": [],
                       "agent": None, "history": [], "model": None, "chapter": chapter_id, "evidence": [], "prompt_requests": []}
            self.sessions[sid] = session
            return self._public(session)

    def _public(self, s):
        return {k: s[k] for k in ("id", "mode", "title", "model", "chapter", "evidence")} | {
            "messages": list(s["messages"]),
            "prompt_requests": [{k: row[k] for k in ("id", "turn", "iteration", "model")} for row in s["prompt_requests"]],
            "running": bool(self.active and self.active.session_id == s["id"])}

    def snapshot(self):
        with self.lock:
            return [self._public(s) for s in self.sessions.values()]

    def start(self, sid, message):
        if not isinstance(message, str) or not message.strip() or len(message) > 6000:
            raise WebError("请输入 1–6000 个字符")
        with self.lock:
            if self.active:
                raise WebError("已有回合正在运行，请等待或先停止", 409)
            session = self.sessions.get(sid)
            if session is None:
                raise WebError("会话不存在", 404)
            if session["chapter"] == "01":
                raise WebError("本章只提供架构导览，无需运行演示")
            if any(key in message for key in self.live_secrets.values()):
                raise WebError("请在模型设置中填写密钥，不要发送到对话")
            turn = Turn(sid, self.redact)
            self.active = turn
            session["messages"].append({"role": "user", "content": message.strip()})
            session["title"] = message.strip()[:26]
            turn.thread = threading.Thread(target=self._run, args=(session, message.strip(), turn),
                                           daemon=True, name="mini-web-turn")
            turn.thread.start()
            return turn

    def stop(self, sid):
        with self.lock:
            turn = self.active
            if not turn or turn.session_id != sid:
                raise WebError("该会话没有正在运行的回合", 409)
            turn.stop()

    def approve(self, sid, approval_id, allowed):
        if not isinstance(allowed, bool):
            raise WebError("审批结果必须是布尔值")
        with self.lock:
            turn = self.active
            gate = turn.approval if turn and turn.session_id == sid else None
            if not gate or turn.cancelled.is_set() or gate["id"] != approval_id or gate["ready"].is_set():
                raise WebError("审批已过期", 409)
            gate["allowed"] = allowed
            gate["ready"].set()

    @staticmethod
    def _tool_payload(message):
        # The existing budget layer may append a warning after a tool's JSON value.
        raw = str(message.get("content", ""))
        try:
            value, _ = json.JSONDecoder().raw_decode(raw.lstrip())
            return value if isinstance(value, dict) else {"content": str(value)}
        except ValueError:
            return {"error": raw}

    def _scripted_provider(self, message, turn, project=None):
        """Scripted decisions; tools and their results still run through the real loop."""
        project = project or self.project
        failed = "失败" in message or "不存在" in message
        code = "代码" in message or "入口" in message
        plan = [("read_file", {"path": str(project / "missing.txt")})] if failed else [
            ("list_dir", {"path": str(project)}),
            ("read_file", {"path": str(project / ("main.py" if code else "README.md"))})]
        index = 0

        def respond(candidate, messages):
            nonlocal index
            if turn.cancelled.wait(0.3):
                turn.stop()
            input_chars = len(json.dumps(messages, ensure_ascii=False))
            if index < len(plan):
                name, args = plan[index]
                content = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "demo_" + uuid.uuid4().hex[:12], "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}
            else:
                tools = [m for m in messages if m.get("role") == "tool"]
                # Summaries use actual tool content; the scripted provider is labelled in the UI.
                payload = self._tool_payload(tools[-1]) if tools else {"error": "没有工具结果"}
                if payload.get("error"):
                    result = str(payload["error"]).replace(str(project), "sample-project")
                    text = "工具返回了文件不存在的错误：\n" + result + "\n\nAgent 已收到这个结果，并正常结束本轮。"
                else:
                    lines = payload.get("content", "").splitlines()
                    content_text = "\n".join(line.partition("|")[2] if "|" in line else line for line in lines)
                    listing = next((self._tool_payload(m) for m in tools
                                    if '"entries"' in str(m.get("content", ""))), {})
                    names = "、".join(entry["name"] for entry in listing.get("entries", []))
                    text = "演示项目包含：" + names + "。\n\n"
                    text += ("入口代码 main.py：\n" if code else "README 内容：\n") + content_text
                    text += "\n\n这些内容来自刚才的实际工具结果。你可以在右侧展开每一步，查看原始参数和返回值。"
                content = {"role": "assistant", "content": text}
            index += 1
            output_chars = len(json.dumps(content, ensure_ascii=False))
            return {"choices": [{"message": content}], "usage": {
                "prompt_tokens": math.ceil(input_chars / 4),
                "completion_tokens": math.ceil(output_chars / 4),
                "total_tokens": math.ceil(input_chars / 4) + math.ceil(output_chars / 4)}}
        return respond

    def _run(self, session, message, turn):
        db = None
        home_token = secret_token = None
        started = time.perf_counter()
        try:
            offline = session["mode"] == "offline"
            lesson = web_lessons.chapter(session["chapter"])
            # Every fixture uses its own empty scope and temporary home, even in live mode.
            with TemporaryDirectory(prefix="lesson-", dir=str(self.root)) as experiment_home:
                ht = set_mini_home_override(Path(experiment_home))
                st = set_secret_scope({})
                evidence = []
                try:
                    evidence = web_lessons.run_experiment(session["chapter"], experiment_home,
                        lambda kind, **data: evidence.append(data))
                finally:
                    reset_secret_scope(st)
                    reset_mini_home_override(ht)
            if offline and lesson["kind"] != "agent":
                turn.emit("started", session_id=session["id"], model="机制实验（未调用模型）",
                          provider="experiment", mode="offline", workspace="隔离临时夹具", context_length=None)
                turn.emit("lesson", **lesson)
                for row in evidence:
                    turn.emit("evidence", **row)
                answer = lesson["title"] + "\n" + "\n".join(
                    ("✓ " if row["ok"] else "✗ ") + row["label"] + "：" + json.dumps(row["observed"], ensure_ascii=False) for row in evidence)
                with self.lock:
                    session["evidence"] = evidence
                    session["messages"].append({"role": "assistant", "content": answer})
                    session["model"] = "机制实验（未调用模型）"
                turn.emit("result", text=answer, usage=None, iterations=0,
                          error=any(not row["ok"] for row in evidence),
                          duration_ms=round((time.perf_counter()-started)*1000))
                return
            if offline:
                home = self.root / ("prompt-home" if session["chapter"] == "03" else "offline-home")
                if session["chapter"] == "03":
                    home.mkdir(exist_ok=True)
                    for name, text in (("SOUL.md", "你是演示用的 mini-hermes 助手。回答以工具读取的内容为依据。"), ("MEMORY.md", "演示记忆：这个项目的入口是 main.py。")):
                        if not (home / name).exists(): (home / name).write_text(text, encoding="utf-8")
                home_token = set_mini_home_override(home)
                secret_token = set_secret_scope({})
                spec = {"provider": "demo", "model": "scripted-offline", "api_key": "demo",
                        "base_url": "http://offline.invalid", "api_mode": "chat_completions",
                        "context_length": 32000}
                workspace = self.prompt_project if session["chapter"] == "03" else self.project
            else:
                if self.live_secrets:
                    secret_token = set_secret_scope(dict(self.live_secrets))
                    spec = resolve_provider("deepseek", self.web_model,
                                            explicit_base_url="https://api.deepseek.com")
                else:
                    spec = resolve_provider(self.provider, self.model)
                # Resolve existing configured credentials as well; never echo them in errors.
                active_key = spec.get("api_key", "")
                if active_key:
                    original_redact = turn.redact
                    def redact_active(value):
                        if isinstance(value, str): return original_redact(value.replace(active_key, "[REDACTED]"))
                        if isinstance(value, dict): return {k: redact_active(v) for k,v in value.items()}
                        if isinstance(value, (list, tuple)): return [redact_active(v) for v in value]
                        return value
                    turn.redact = redact_active
                home = get_mini_home() / "web"
                workspace = Path(resolve_context_cwd())
            home.mkdir(parents=True, exist_ok=True)
            db = SessionDB(home / "state.db")
            db.create_session(session["id"], "web", model=spec["model"])
            if session["agent"] is None:
                session["agent"] = AIAgent(
                    spec, session_id=session["id"], max_turns=self.max_turns,
                    fallback_providers=[],
                    thinking={"type": "disabled"} if spec["provider"] == "deepseek" else None,
                    context_cwd=str(workspace), skip_context_files=offline and session["chapter"] != "03")
            agent = session["agent"]
            agent.ephemeral_system_prompt = (
                "当前演示章节：" + lesson["title"] + "。重点：" + lesson["focus"] +
                "。请围绕本章解释，不要把夹具验证描述为生产能力。夹具摘要为固定生成，不代表真实模型压缩。实测证据：" +
                json.dumps(evidence, ensure_ascii=False))
            if session["chapter"] == "03":
                agent.ephemeral_system_prompt = "请根据当前用户问题完成任务；文件内容以实际工具读取为准。提示词由页面从请求中展示，不要编造分层内容或缓存命中。"
            turn.agent = agent
            # A new connection per worker avoids sharing SQLite connections between threads.
            agent.session_db = db
            # A cached agent must receive a fresh budget for each submitted turn.
            agent.budget = IterationBudget(self.max_turns)
            agent.approval_callback = turn.approve
            agent.status_callback = lambda text: turn.emit("status", text=text)
            observer = ObserverRegistry()
            for name in OBSERVER_EVENTS:
                observer.add(name, lambda **payload: turn.emit("trace", **payload))
            agent._observers = observer
            if offline:
                agent._http_post = self._scripted_provider(message, turn, workspace)
            else:
                # Bind only once per cached agent; sanitize before the core logs/persists responses.
                transport = getattr(agent, "_web_transport", agent._http_post)
                agent._web_transport = transport
                def sanitized_request(candidate, messages, _transport=transport):
                    try:
                        return turn.redact(_transport(candidate, messages))
                    except ApiHTTPError as exc:
                        raise ApiHTTPError(exc.code, turn.redact(exc.body), turn.redact(exc.url)) from None
                    except Exception as exc:
                        raise RuntimeError(turn.redact(str(exc))) from None
                agent._http_post = sanitized_request
            if session["chapter"] == "03":
                transport = agent._http_post
                def inspected_request(candidate, messages):
                    # Capture the already assembled request copy, before any provider call.
                    snapshot = capture_request(agent, candidate, messages, session["prompt_requests"], turn.redact)
                    with self.lock:
                        session["prompt_requests"].append(snapshot)
                    turn.emit("prompt_request", **{k: snapshot[k] for k in ("id", "turn", "iteration", "model")})
                    return transport(candidate, messages)
                agent._http_post = inspected_request
            with self.lock:
                session["model"] = agent.model
            turn.emit("started", session_id=session["id"], model=agent.model,
                      provider=agent.provider, mode=session["mode"], workspace=workspace.name,
                      context_length=spec.get("context_length"))
            turn.emit("lesson", **lesson)
            for row in evidence:
                turn.emit("evidence", **row)
            if turn.cancelled.is_set():
                turn.emit("status", text="本轮已停止")
                return
            result = agent.run_conversation(message, conversation_history=session["history"])
            if lesson["kind"] == "agent" and session["chapter"] != "03":
                if session["chapter"] == "02":
                    calls = [d for d in turn.traces if d["event"] == "pre_tool_call"]
                    returns = [d for d in turn.traces if d["event"] == "post_tool_call"]
                    evidence.append({"label": "实际工具调用与返回", "observed": {"calls": len(calls), "returns": len(returns)},
                                     "ok": bool(calls) and len(calls) == len(returns), "source": lesson["source"]})
                else:
                    names = sorted({d["event"] for d in turn.traces})
                    ids = {d["turn_id"] for d in turn.traces if d["turn_id"]}
                    evidence.append({"label": "观察到的事件类别与回合关联", "observed": {"events": names, "turn_ids": len(ids)},
                                     "ok": bool(names) and len(ids) == 1, "source": lesson["source"]})
                for row in evidence:
                    turn.emit("evidence", **row)
            with self.lock:
                session["evidence"] = evidence
                session["history"] = result["messages"]
                answer = result.get("final_response", "")
                if answer:
                    session["messages"].append({"role": "assistant", "content": answer})
            with self.lock:
                session["model"] = agent.model
            turn.emit("result", text=answer, usage=result.get("usage"),
                      model=agent.model, provider=agent.provider,
                      interrupted=bool(result.get("interrupted")), error=bool(result.get("error")),
                      budget_exhausted=bool(result.get("budget_exhausted")),
                      iterations=result.get("iterations_used"),
                      duration_ms=round((time.perf_counter() - started) * 1000))
        except Exception as exc:
            turn.emit("error", message=str(exc), error_type=type(exc).__name__)
        finally:
            if session["agent"] is not None:
                session["agent"].session_db = None
            try:
                if db is not None:
                    db.close()
            finally:
                if secret_token is not None:
                    reset_secret_scope(secret_token)
                if home_token is not None:
                    reset_mini_home_override(home_token)
                with self.lock:
                    self.active = None
                turn.finished.set()


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        if address[0] != "127.0.0.1":
            raise ValueError("演示服务只允许绑定 127.0.0.1")
        self.app = app
        super().__init__(address, WebHandler)


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _headers(self, status, content_type, size=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        if size is not None:
            self.send_header("Content-Length", str(size))
        self.end_headers()

    def _json(self, status, data, exact=False):
        raw = json.dumps(data if exact else hook_jsonable(data), ensure_ascii=False).encode()
        self._headers(status, "application/json; charset=utf-8", len(raw))
        self.wfile.write(raw)

    def _check_host(self):
        port = self.server.server_address[1]
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise WebError("请求来源不受支持", 403)
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + host:
            raise WebError("跨站请求已拒绝", 403)

    def do_GET(self):
        try:
            self._check_host()
            path = urlsplit(self.path).path
            if path == "/api/bootstrap":
                return self._json(200, {"token": self.server.app.token,
                                       "sessions": self.server.app.snapshot(),
                                       "chapters": web_lessons.catalog(), "model_config": self.server.app.model_config()})
            if path.startswith("/api/prompts/"):
                parts = path.split("/")
                app = self.server.app
                with app.lock:
                    session = app.sessions.get(parts[3]) if len(parts) == 5 else None
                    snapshot = next((row for row in session["prompt_requests"] if row["id"] == parts[4]), None) if session else None
                if snapshot is None:
                    raise WebError("提示词请求不存在", 404)
                return self._json(200, snapshot, exact=True)
            if path.startswith("/docs/"):
                parts = path.split("/")
                try:
                    document = web_lessons.documents(parts[2]).get(parts[3]) if len(parts) == 4 else None
                except ValueError:
                    document = None
                if not document or not document.is_file():
                    raise WebError("文档不存在", 404)
                raw = document.read_bytes()
                self._headers(200, "text/plain; charset=utf-8", len(raw))
                self.wfile.write(raw)
                return
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path not in assets:
                raise WebError("页面不存在", 404)
            name, content_type = assets[path]
            raw = (ASSETS / name).read_bytes()
            self._headers(200, content_type, len(raw))
            self.wfile.write(raw)
        except WebError as exc:
            self._json(exc.status, {"error": str(exc)})

    def do_POST(self):
        turn = None
        try:
            self._check_host()
            if not secrets.compare_digest(self.headers.get("X-Mini-Token", ""), self.server.app.token):
                raise WebError("页面令牌已失效，请刷新后重试", 403)
            if self.headers.get_content_type() != "application/json":
                raise WebError("请求必须使用 JSON", 415)
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise WebError("无效请求长度")
            if not 0 < size <= MAX_BODY:
                raise WebError("请求过大或为空", 413)
            try:
                data = json.loads(self.rfile.read(size))
            except (ValueError, UnicodeDecodeError):
                raise WebError("无效 JSON")
            if not isinstance(data, dict):
                raise WebError("请求必须是 JSON 对象")
            path = urlsplit(self.path).path
            app = self.server.app
            sid = data.get("session_id")
            if sid is not None and not isinstance(sid, str):
                raise WebError("无效会话 ID")
            if path == "/api/sessions":
                return self._json(201, app.create_session(data.get("mode", "offline"), data.get("chapter", "02")))
            if path == "/api/model-config":
                return self._json(200, app.configure_model(data))
            if path == "/api/stop":
                app.stop(sid)
                return self._json(200, {"ok": True})
            if path == "/api/approve":
                app.approve(sid, data.get("approval_id"), data.get("allowed"))
                return self._json(200, {"ok": True})
            if path != "/api/turn":
                raise WebError("接口不存在", 404)
            turn = app.start(sid, data.get("message"))
            self._headers(200, "text/event-stream; charset=utf-8")
            while True:
                try:
                    item = turn.events.get(timeout=0.3)
                except queue.Empty:
                    if turn.finished.is_set():
                        # Recheck after finished: the producer may have queued its last result.
                        try:
                            item = turn.events.get_nowait()
                        except queue.Empty:
                            self.wfile.write(b'event: done\ndata: {}\n\n')
                            self.wfile.flush()
                            break
                    else:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                packet = "event: " + item["event"] + "\ndata: " + json.dumps(item["data"], ensure_ascii=False) + "\n\n"
                self.wfile.write(packet.encode())
                self.wfile.flush()
        except WebError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError, OSError):
            if turn:
                turn.stop()


def run_web(provider="deepseek", model=None, port=8765, max_turns=12):
    app = WebApp(provider, model, max_turns)
    server = None
    try:
        server = WebServer(("127.0.0.1", port), app)
        print(f"演示页面：http://127.0.0.1:{server.server_address[1]}", flush=True)
        print("默认离线演示，不需要 API 密钥。按 Ctrl-C 停止。", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"无法启动演示服务：{exc}")
        return 2
    finally:
        if server:
            server.server_close()
        app.close()
