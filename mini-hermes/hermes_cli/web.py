"""Loopback-only demo UI: real agent + observers, scripted offline provider, SSE.

Run: python3 -m hermes_cli.main --web
The UI's session list lives in memory. Real runs persist messages in MINI_HOME/web/state.db;
offline runs use an owned temporary project and database, removed on shutdown.
"""
from __future__ import annotations

import json
import math
import queue
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
from run_agent import AIAgent

ASSETS = Path(__file__).with_name("web_assets")
MAX_BODY = 32_768


class WebError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Turn:
    def __init__(self, session_id):
        self.session_id = session_id
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

    def emit(self, kind, **data):
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

    def close(self):
        turn = self.active
        try:
            if turn:
                turn.stop()
                turn.thread.join(timeout=10)
        finally:
            self.temp.cleanup()

    def create_session(self, mode):
        if mode not in ("offline", "live"):
            raise WebError("未知运行模式")
        with self.lock:
            if len(self.sessions) >= 20:
                raise WebError("本次运行最多保留 20 个会话，请重启演示服务", 409)
            sid = uuid.uuid4().hex
            session = {"id": sid, "mode": mode, "title": "新会话", "messages": [],
                       "agent": None, "history": [], "model": None}
            self.sessions[sid] = session
            return self._public(session)

    def _public(self, s):
        return {k: s[k] for k in ("id", "mode", "title", "model")} | {
            "messages": list(s["messages"]),
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
            turn = Turn(sid)
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

    def _scripted_provider(self, message, turn):
        """Scripted decisions; tools and their results still run through the real loop."""
        failed = "失败" in message or "不存在" in message
        code = "代码" in message or "入口" in message
        plan = [("read_file", {"path": str(self.project / "missing.txt")})] if failed else [
            ("list_dir", {"path": str(self.project)}),
            ("read_file", {"path": str(self.project / ("main.py" if code else "README.md"))})]
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
                    result = str(payload["error"]).replace(str(self.project), "sample-project")
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
            if offline:
                home = self.root / "offline-home"
                home_token = set_mini_home_override(home)
                secret_token = set_secret_scope({})
                spec = {"provider": "demo", "model": "scripted-offline", "api_key": "demo",
                        "base_url": "http://offline.invalid", "api_mode": "chat_completions",
                        "context_length": 32000}
                workspace = self.project
            else:
                spec = resolve_provider(self.provider, self.model)
                home = get_mini_home() / "web"
                workspace = Path(resolve_context_cwd())
            home.mkdir(parents=True, exist_ok=True)
            db = SessionDB(home / "state.db")
            db.create_session(session["id"], "web", model=spec["model"])
            if session["agent"] is None:
                session["agent"] = AIAgent(
                    spec, session_id=session["id"], max_turns=self.max_turns,
                    fallback_providers=[] if offline else None,
                    context_cwd=str(workspace), skip_context_files=offline)
            agent = session["agent"]
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
                agent._http_post = self._scripted_provider(message, turn)
            with self.lock:
                session["model"] = agent.model
            turn.emit("started", session_id=session["id"], model=agent.model,
                      provider=agent.provider, mode=session["mode"], workspace=workspace.name,
                      context_length=spec.get("context_length"))
            if turn.cancelled.is_set():
                turn.emit("status", text="本轮已停止")
                return
            result = agent.run_conversation(message, conversation_history=session["history"])
            with self.lock:
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

    def _json(self, status, data):
        raw = json.dumps(hook_jsonable(data), ensure_ascii=False).encode()
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
                                       "sessions": self.server.app.snapshot()})
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
                return self._json(201, app.create_session(data.get("mode", "offline")))
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
