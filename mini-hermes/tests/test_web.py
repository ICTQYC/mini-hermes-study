"""Web integration checks: HTTP/SSE, real offline tools, isolation and cancellation.
Run directly with Python; no browser, network provider or third-party packages required.
"""
from __future__ import annotations
import contextlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hermes_cli import web

PASS, FAIL = [], []
def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    print(("✓ " if ok else "✗ ") + label + (" → " + str(detail) if not ok and detail else ""))


@contextlib.contextmanager
def fixture():
    with tempfile.TemporaryDirectory(prefix="mini-web-test-") as area:
        home = Path(area) / "home"
        home.mkdir()
        with patch.dict(os.environ, {"HOME": str(home), "MINI_HOME": str(home / "mini"),
                                     "MINI_ENV_FILE": str(home / "absent.env"),
                                     "PATH": os.environ.get("PATH", "")}, clear=True):
            app = web.WebApp()
            server = web.WebServer(("127.0.0.1", 0), app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield app, server, home
            finally:
                server.shutdown()
                server.server_close()
                app.close()
                thread.join(timeout=3)
            check("服务关闭后删除离线临时根", not app.root.exists())


def request(server, path, data=None, headers=None, raw=None):
    c = http.client.HTTPConnection(*server.server_address, timeout=15)
    default = {"X-Mini-Token": server.app.token, "Content-Type": "application/json"}
    if headers: default.update(headers)
    c.request("GET" if data is None and raw is None else "POST", path,
              body=raw if raw is not None else json.dumps(data) if data is not None else None,
              headers=default)
    r = c.getresponse()
    try:
        return r.status, dict(r.getheaders()), r.read().decode()
    finally:
        r.close(); c.close()


def events(body):
    out = []
    for block in body.split("\n\n"):
        lines = block.splitlines()
        name = next((s[7:] for s in lines if s.startswith("event: ")), None)
        data = next((s[6:] for s in lines if s.startswith("data: ")), None)
        if name and data: out.append((name, json.loads(data)))
    return out


def session(server, mode="offline"):
    return json.loads(request(server, "/api/sessions", {"mode": mode})[2])["id"]


def test_transport():
    with fixture() as (app, server, home):
        status, headers, body = request(server, "/")
        check("首页包含可用输入与执行轨迹", status == 200 and 'id="prompt"' in body and 'id="trace"' in body)
        check("页面限制外部脚本及嵌入", "script-src 'self'" in headers.get("Content-Security-Policy", "")
              and "frame-ancestors 'none'" in headers.get("Content-Security-Policy", ""))
        check("JS 与 CSS 可独立加载", all(request(server, p)[0] == 200 for p in ("/app.js", "/style.css")))
        check("文件遍历不能读取工作区文件", request(server, "/../run_agent.py")[0] == 404)
        check("拒绝任意 Host", request(server, "/api/bootstrap", headers={"Host": "evil.example"})[0] == 403)
        check("拒绝跨站 Origin", request(server, "/api/sessions", {}, {"Origin": "https://evil.example"})[0] == 403)
        check("拒绝缺失页面令牌", request(server, "/api/sessions", {}, {"X-Mini-Token": ""})[0] == 403)
        check("拒绝非 JSON 请求类型", request(server, "/api/sessions", {}, {"Content-Type": "text/plain"})[0] == 415)
        check("拒绝无效 UTF-8", request(server, "/api/sessions", raw=b"\xff")[0] == 400)
        check("拒绝无效 JSON", request(server, "/api/sessions", raw="{")[0] == 400)
        check("拒绝 JSON 数组", request(server, "/api/sessions", raw="[]")[0] == 400)
        check("拒绝无效模式", request(server, "/api/sessions", {"mode": "unknown"})[0] == 400)
        check("拒绝过大请求", request(server, "/api/sessions", raw='"' + 'x' * web.MAX_BODY + '"')[0] == 413)
        sid = session(server)
        check("拒绝空输入", request(server, "/api/turn", {"session_id": sid, "message": " "})[0] == 400)
        check("拒绝无效会话 ID 类型", request(server, "/api/turn", {"session_id": [], "message": "hi"})[0] == 400)
        check("拒绝不存在的会话", request(server, "/api/turn", {"session_id": "absent", "message": "hi"})[0] == 404)
        check("未运行时停止返回可诊断冲突", request(server, "/api/stop", {"session_id": sid})[0] == 409)
        blocked = False
        try: web.WebServer(("0.0.0.0", 0), app)
        except ValueError: blocked = True
        check("禁止绑定公网监听地址", blocked)


def test_offline():
    with fixture() as (app, server, home):
        app.max_turns = 3
        sid = session(server)
        with patch.object(web, "resolve_provider", side_effect=AssertionError("offline resolved real credentials")), \
             patch.object(web.AIAgent, "_http_post", side_effect=AssertionError("offline used real network")):
            status, headers, body = request(server, "/api/turn", {"session_id": sid, "message": "浏览项目"})
        stream = events(body)
        trace = [data for kind, data in stream if kind == "trace"]
        results = [data for kind, data in stream if kind == "result"]
        check("离线 HTTP 以 SSE 返回并正常结束", status == 200 and "text/event-stream" in headers.get("Content-Type", "") and stream[-1][0] == "done")
        check("真实工具按序执行列目录和读文件", [d['tool_name'] for d in trace if d['event'] == 'pre_tool_call'] == ['list_dir', 'read_file'])
        check("工具结果包含真实示例文件内容", any('Hello, agent!' in str(d.get('result')) for d in trace if d['event'] == 'post_tool_call'))
        check("模型与工具事件来自同一回合", len({d['turn_id'] for d in trace}) == 1)
        check("模型请求分别有独立请求 ID", len({d['api_request_id'] for d in trace if d['event'] == 'pre_api_request'}) == 3)
        check("正常结果保留用量与实际迭代数", len(results) == 1 and results[0]['iterations'] == 3 and results[0]['usage']['total_tokens'] > 0)
        check("离线未创建使用者数据目录", not (home / 'mini').exists())
        check("离线消息写入独立数据库", (app.root / 'offline-home/state.db').is_file())
        agent = app.sessions[sid]['agent']
        second = events(request(server, "/api/turn", {"session_id": sid, "message": "入口代码"})[2])
        check("缓存 Agent 的下一回合有独立预算", any(k == 'result' and not d['budget_exhausted'] and d['iterations'] == 3 for k,d in second))
        check("同一会话复用 Agent 且保留历史", app.sessions[sid]['agent'] is agent and len(app.sessions[sid]['messages']) == 4)
        other = session(server)
        check("新会话不共享旧对话", app.sessions[other]['history'] == [] and app.sessions[other]['messages'] == [])
        failure = events(request(server, '/api/turn', {'session_id': other, 'message': '不存在的文件'})[2])
        check("实际文件错误可见于工具事件", any(k == 'trace' and d['event'] == 'post_tool_call' and d['status'] == 'error' for k,d in failure))
        check("工具失败后 Agent 仍能返回完整结果", any(k == 'result' and '文件不存在' in d['text'] for k,d in failure))


def test_concurrency_and_stop():
    with fixture() as (app, server, home):
        sid = session(server)
        c = http.client.HTTPConnection(*server.server_address, timeout=5)
        c.request('POST', '/api/turn', json.dumps({'session_id': sid, 'message': '浏览项目'}),
                  {'Content-Type': 'application/json', 'X-Mini-Token': app.token})
        response = c.getresponse()
        check("事件在完成前即可读取", response.readline().startswith(b'event: started'))
        running = app.active
        check("并发回合被明确拒绝", request(server, '/api/turn', {'session_id': sid, 'message': '重复'})[0] == 409)
        check("停止接口可中断运行回合", request(server, '/api/stop', {'session_id': sid})[0] == 200)
        remainder = response.read().decode(); response.close(); c.close()
        check("停止后 SSE 正常收口", ('done', {}) in events(remainder))
        check("停止传递给 Agent 且释放单回合锁", running.cancelled.is_set() and running.finished.wait(3) and app.active is None)
        # Disconnect while the scripted provider is still waiting; heartbeat detects it.
        c = http.client.HTTPConnection(*server.server_address, timeout=5)
        c.request('POST', '/api/turn', json.dumps({'session_id': sid, 'message': '浏览项目'}),
                  {'Content-Type': 'application/json', 'X-Mini-Token': app.token})
        response = c.getresponse(); response.readline(); running = app.active
        response.close(); c.close()
        check("浏览器断开后回合最终释放", running.finished.wait(5) and app.active is None)
        check("断开连接实际触发停止", running.cancelled.is_set())


def test_approval():
    with fixture() as (app, server, home):
        for allowed in (False, True):
            turn = web.Turn('approval-session'); app.active = turn; result = []
            turn.thread = threading.Thread(target=lambda: result.append(turn.approve('fixture command')), daemon=True)
            turn.thread.start()
            event = turn.events.get(timeout=2)
            check("审批事件可到达浏览器", event['event'] == 'approval' and event['data']['command'] == 'fixture command')
            body = {'session_id': turn.session_id, 'approval_id': event['data']['approval_id']}
            check("错误审批 ID 不放行", request(server, '/api/approve', {**body, 'approval_id': 'wrong', 'allowed': True})[0] == 409)
            check("字符串不能冒充审批布尔值", request(server, '/api/approve', {**body, 'allowed': 'false'})[0] == 400)
            check("审批接口正常响应", request(server, '/api/approve', {**body, 'allowed': allowed})[0] == 200)
            turn.thread.join(timeout=2)
            check("审批原样传回允许或拒绝", result == [allowed])
            app.active = None
        turn = web.Turn('parallel-approval'); app.active = turn; results = {}
        turn.thread = threading.Thread(target=lambda: results.update(first=turn.approve('first command')), daemon=True)
        turn.thread.start(); first = turn.events.get(timeout=2)
        entered = threading.Event()
        def second_request():
            entered.set()
            results['second'] = turn.approve('second command')
        second_thread = threading.Thread(target=second_request, daemon=True)
        second_thread.start(); entered.wait(2)
        check("并发审批不覆盖当前对话框", turn.approval['id'] == first['data']['approval_id'])
        request(server, '/api/approve', {'session_id': turn.session_id,
            'approval_id': first['data']['approval_id'], 'allowed': False})
        second = turn.events.get(timeout=2)
        check("第一个审批结束后才发出第二个审批", second['data']['command'] == 'second command'
              and second['data']['approval_id'] != first['data']['approval_id'])
        request(server, '/api/approve', {'session_id': turn.session_id,
            'approval_id': second['data']['approval_id'], 'allowed': True})
        turn.thread.join(timeout=2); second_thread.join(timeout=2)
        check("并发请求各自收到正确审批结果", results == {'first': False, 'second': True})
        app.active = None
        turn = web.Turn('cancelled-approval'); app.active = turn; result = []
        turn.thread = threading.Thread(target=lambda: result.append(turn.approve('fixture command')), daemon=True)
        turn.thread.start(); event = turn.events.get(timeout=2)
        turn.stop()
        check("已停止回合的审批不能放行", request(server, '/api/approve', {
            'session_id': turn.session_id, 'approval_id': event['data']['approval_id'], 'allowed': True})[0] == 409)
        turn.thread.join(timeout=2)
        check("停止会使待决审批返回拒绝", result == [False])
        app.active = None


def test_live_boundary():
    spec = {'provider': 'fixture', 'model': 'fixture-live', 'api_key': 'private-web-fixture-credential',
            'base_url': 'http://fixture.invalid', 'api_mode': 'chat_completions', 'context_length': 32000}
    reply = {'choices': [{'message': {'role': 'assistant', 'content': 'fixture live answer'}}],
             'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7}}
    with fixture() as (app, server, home):
        sid = session(server, 'live')
        with patch.object(web, 'resolve_provider', return_value=spec), patch.object(web.AIAgent, '_http_post', return_value=reply):
            status, _, body = request(server, '/api/turn', {'session_id': sid, 'message': 'hi'})
        check("真实模式沿用 Agent 请求路径", status == 200 and 'fixture live answer' in body)
        check("模型密钥不进入 SSE 或启动数据", spec['api_key'] not in body + request(server, '/api/bootstrap')[2])
        check("真实模式使用独立 Web 数据库", (home / 'mini/web/state.db').is_file())
        check("真实与离线数据隔离", not (app.root / 'offline-home/state.db').exists())
        other = session(server, 'live')
        with patch.object(web, 'resolve_provider', side_effect=RuntimeError('fixture: missing credentials')):
            stream = events(request(server, '/api/turn', {'session_id': other, 'message': 'hi'})[2])
        check("配置失败是可见错误事件", any(k == 'error' and 'missing credentials' in d['message'] for k,d in stream))
        check("配置失败也关闭事件流并释放锁", stream[-1][0] == 'done' and app.active is None)


GROUPS = [("HTTP 与来源校验", test_transport), ("离线真实执行与隔离", test_offline),
          ("并发、中断与断开", test_concurrency_and_stop), ("审批", test_approval), ("真实模式边界", test_live_boundary)]
if __name__ == '__main__':
    for name, test in GROUPS:
        print('\n' + name)
        try: test()
        except Exception as exc: check(name + ' 无用例异常', False, type(exc).__name__ + ': ' + str(exc))
    print(f'\n{len(PASS)} 通过 / {len(FAIL)} 失败')
    raise SystemExit(1 if FAIL else 0)
