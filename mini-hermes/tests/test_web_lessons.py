"""Chapter evidence and memory-only Web credentials; all providers are fakes."""
from __future__ import annotations
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_web import fixture, request, events
from hermes_cli import web, web_lessons
from agent.secret_scope import get_secret, set_secret_scope, reset_secret_scope
from hermes_constants import set_mini_home_override, reset_mini_home_override
import run_agent

PASS, FAIL = [], []
def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    print(("✓ " if ok else "✗ ") + label + (" → " + str(detail) if not ok else ""))


def test_experiments():
    rows = web_lessons.catalog()
    check("目录覆盖所有章节且无重复", len(rows) == 17 and len({r['id'] for r in rows}) == 17)
    check("未新增子系统章节明确作为导览", {r['id'] for r in rows if r['kind']=='guide'} == {'01','09','12'})
    check("每章都有真实源码和精译入口", all((web_lessons.MINI/r['source']).is_file() and 'translation' in r['documents'] for r in rows))
    with tempfile.TemporaryDirectory(prefix='lessons-test-') as area:
        for row in rows:
            home = Path(area)/row['id']
            ht = set_mini_home_override(home); st = set_secret_scope({})
            try:
                output = []
                measured = web_lessons.run_experiment(row['id'], home, lambda kind, **data: output.append((kind,data)))
                if row['kind']=='agent':
                    check(row['id']+' 证据由实际回合产生，夹具不冒充', measured == [])
                else:
                    check(row['id']+' 实测断言全部成立', bool(measured) and all(d['ok'] for d in measured), measured)
                    check(row['id']+' 事件与实测结果一致', [d for _,d in output] == measured)
            finally:
                reset_secret_scope(st); reset_mini_home_override(ht)


def test_chapter_http():
    with fixture() as (app, server, home):
        catalog=json.loads(request(server,'/api/bootstrap')[2])['chapters']
        check("HTTP 目录没有被观察者截断", len(catalog)==17 and catalog[-1]['id']=='15')
        check("非法章节被拒绝", request(server,'/api/sessions',{'chapter':'../../etc/passwd'})[0]==400)
        check("非字符串章节被拒绝", request(server,'/api/sessions',{'chapter':[]})[0]==400)
        check("可信文档完整返回而不经事件截断", len(request(server,'/docs/05/translation')[2])>8000)
        check("文档路由拒绝任意文件", request(server,'/docs/05/../../run_agent.py')[0]==404)
        for chapter in ('05','06','11','15'):
            session=json.loads(request(server,'/api/sessions',{'chapter':chapter})[2])
            stream=events(request(server,'/api/turn',{'session_id':session['id'],'message':'运行本章'})[2])
            evidence=[d for k,d in stream if k=='evidence']
            check(chapter+' HTTP 回合产生本章证据', bool(evidence) and all(d['ok'] for d in evidence), stream)
            check(chapter+' 完成后保留证据供切换查看', app.sessions[session['id']]['evidence']==evidence)
            if chapter != '15':
                check(chapter+' 机制实验不伪造模型事件或用量', not any(k=='trace' for k,_ in stream) and any(k=='result' and d['iterations']==0 and d['usage'] is None for k,d in stream))
        check("夹具临时目录逐回合清理", not list(app.root.glob('lesson-*')))


def test_credentials():
    fake='web-fixture-secret-never-real'
    reply={'choices':[{'message':{'role':'assistant','content':'answer '+fake}}],
           'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
    with fixture() as (app,server,home):
        status,_,body=request(server,'/api/model-config',{'api_key':fake,'model':'fixture-model'})
        check("Web 密钥保存为仅内存状态", status==200 and json.loads(body)['configured'] and fake not in body)
        check("启动数据不回显密钥", fake not in request(server,'/api/bootstrap')[2])
        check("密钥未写入使用者目录", not any(home.rglob('.env')))
        for data in ({'api_key':'a\nb'}, {'model':'bad model'}, {'api_key':12}, {'clear':'yes'}):
            check("非法配置返回可诊断错误 "+repr(data), request(server,'/api/model-config',data)[0]==400)
        sid=json.loads(request(server,'/api/sessions',{'mode':'live','chapter':'08'})[2])['id']
        check("已知密钥不能误发聊天", request(server,'/api/turn',{'session_id':sid,'message':fake})[0]==400 and app.sessions[sid]['messages']==[])
        observed={}
        def resolve(provider,model,**kw):
            observed.update(provider=provider,model=model,base=kw.get('explicit_base_url'),key=get_secret('DEEPSEEK_API_KEY'))
            return {'provider':'deepseek','model':model,'api_key':get_secret('DEEPSEEK_API_KEY'), 'base_url':kw['explicit_base_url'],'context_length':32000}
        with patch.object(web,'resolve_provider',side_effect=resolve), patch.object(web.AIAgent,'_http_post',return_value=reply):
            body=request(server,'/api/turn',{'session_id':sid,'message':'解释实验'})[2]
        agent=app.sessions[sid]['agent']
        prompt_sid=json.loads(request(server,'/api/sessions',{'mode':'live','chapter':'03'})[2])['id']
        with patch.object(web,'resolve_provider',side_effect=resolve), patch.object(web.AIAgent,'_http_post',return_value=reply):
            for message in ('解释本次提示词','再追问一次'):
                request(server,'/api/turn',{'session_id':prompt_sid,'message':message})
        check("真实模式续聊也逐次捕获，且传输包装不递归", len(app.sessions[prompt_sid]['prompt_requests'])==2 and len(app.sessions[prompt_sid]['messages'])==4)
        check("凭据走密钥作用域并固定到官方端点", observed=={'provider':'deepseek','model':'fixture-model','base':'https://api.deepseek.com','key':fake})
        check("DeepSeek Web 回合显式关闭思考模式", agent.thinking=={'type':'disabled'})
        check("真实路径返回值在核心落盘前脱敏", fake not in body+str(app.sessions[sid]['history']) and '[REDACTED]' in body)
        from hermes_state import SessionDB
        db=SessionDB(home/'mini/web/state.db')
        try:
            check("SQLite 中没有响应回显的密钥", fake not in str(db.get_messages_as_conversation(sid)))
        finally: db.close()
        # HTTP error bodies are sanitized before observer callbacks and user errors.
        agent._web_transport=lambda *_: (_ for _ in ()).throw(web.ApiHTTPError(400,'echo '+fake))
        with patch.object(web,'resolve_provider',side_effect=resolve):
            body=request(server,'/api/turn',{'session_id':sid,'message':'再次请求'})[2]
        check("错误体与 SSE 不泄露服务商回显密钥", fake not in body and '[REDACTED]' in body)
        turn=web.Turn(sid);app.active=turn
        check("运行中不能变更模型凭据", request(server,'/api/model-config',{'clear':True})[0]==409)
        app.active=None
        request(server,'/api/model-config',{'api_key':'replacement-fixture','model':'other-model'})
        check("更新设置使旧 Agent 缓存失效", app.sessions[sid]['agent'] is None and app.web_model=='other-model')
        request(server,'/api/model-config',{'clear':True})
        check("清除后服务内存不再保留 Web 密钥", app.live_secrets=={} and not app.model_config()['configured'])
        check("浏览器脚本没有凭据持久存储", all(word not in (web.ASSETS/'app.js').read_text() for word in ('localStorage','sessionStorage','indexedDB')))


def test_thinking_transport():
    captured=[]
    class Connection:
        def __init__(self,*a,**kw): pass
        def request(self,method,path,body,headers): captured.append(json.loads(body))
        def getresponse(self): return self
        status=200
        def read(self): return b'{"choices":[]}'
        def close(self): pass
    with tempfile.TemporaryDirectory(prefix='thinking-test-') as area:
        ht=set_mini_home_override(Path(area));st=set_secret_scope({})
        try:
            spec={'provider':'fixture','model':'fixture','api_key':'fake','base_url':'https://fixture.invalid'}
            with patch.object(run_agent,'_TwoTimeoutHTTPSConnection',Connection):
                for thinking in (None,{'type':'disabled'}):
                    agent=run_agent.AIAgent(spec,fallback_providers=[],skip_context_files=True,thinking=thinking)
                    agent._http_post(spec,[{'role':'user','content':'hi'}])
            check("默认请求协议不增加 thinking", 'thinking' not in captured[0])
            check("显式 thinking 到达真实 HTTP 请求体", captured[1].get('thinking')=={'type':'disabled'} and captured[1]['model']=='fixture')
            blocked=False
            try: run_agent.AIAgent(spec,thinking={'messages':'override'})
            except ValueError: blocked=True
            check("附加协议字段不能覆盖模型或消息", blocked)
        finally:
            reset_secret_scope(st);reset_mini_home_override(ht)

def test_prompt_conversation():
    from hermes_cli.web_prompts import common_prefix
    check("公共前缀按 UTF-8 字节测量", common_prefix("中a", "中b") == 3)
    with fixture() as (app,server,home):
        sid=json.loads(request(server,'/api/sessions',{'chapter':'03'})[2])['id']
        # Large, real project instructions verify that the exact endpoint does not clip at 8000 characters.
        (app.prompt_project/'AGENTS.md').write_text("# Demo rules\n" + "保留这段项目规则。"*1600,encoding='utf-8')
        seen=[]; original=app._scripted_provider
        def provider(message,turn,project=None):
            respond=original(message,turn,project)
            def capture(candidate,messages):
                seen.append(json.loads(json.dumps(messages)))
                return respond(candidate,messages)
            return capture
        with patch.object(app,'_scripted_provider',side_effect=provider):
            stream=events(request(server,'/api/turn',{'session_id':sid,'message':'读一下 README'})[2])
            snapshots=app.sessions[sid]['prompt_requests']
            check("提示词章进入实际 Agent 回合并逐次捕获", len(snapshots)==3 and len([1 for k,d in stream if k=='prompt_request'])==3, stream)
            check("快照与真正交给 provider 的 messages 一致", [row['messages'] for row in snapshots]==seen)
            check("每次请求均可反查三层和临时指导", all(row['mapped'] and '\n\n'.join(layer['text'] for layer in row['layers'] if layer['text'])+row['temporary']==row['system'] for row in snapshots))
            first=snapshots[0]
            layer_text={row["name"]: row["text"] for row in first["layers"]}
            check("展示真实身份、项目规则、记忆和环境", "演示用的 mini-hermes" in layer_text.get("stable", "") and "保留这段项目规则" in layer_text.get("context", "") and "演示记忆" in layer_text.get("volatile", ""))
            check("同一工具回合中系统稳定而消息增长", all(row['comparison']['system_equal'] and row['comparison']['current_messages']>row['comparison']['previous_messages'] for row in snapshots[1:]))
            detail=json.loads(request(server,f"/api/prompts/{sid}/{first['id']}")[2])
            check("完整提示词端点不受事件 8000 字符截断", len(detail['system'])>8000 and detail['system']==first['system'] and 'truncated' not in detail['system'])
            (app.root/'prompt-home/MEMORY.md').write_text('CHANGED_MEMORY_AFTER_FIRST_TURN',encoding='utf-8')
            request(server,'/api/turn',{'session_id':sid,'message':'继续读入口代码'})
            snapshots=app.sessions[sid]['prompt_requests']
            check("续聊保留前次请求并新增独立对话回合", len(snapshots)==6 and len({row['turn'] for row in snapshots})==2 and snapshots[3]['iteration']==1)
            check("记忆文件变化不使同一 Agent 的缓存悄悄变化", snapshots[3]['system']==first['system'] and 'CHANGED_MEMORY_AFTER_FIRST_TURN' not in snapshots[3]['system'])
            check("新问题与工具调用在完整请求中可见", '继续读入口代码' in str(snapshots[-1]['messages']) and any(m.get('tool_calls') for m in snapshots[-1]['messages']))
            check("临时提示只进请求副本，不污染历史", first['temporary'].strip() not in app.sessions[sid]['history'][0]['content'])
            newer=json.loads(request(server,'/api/sessions',{'chapter':'03'})[2])['id']
            request(server,'/api/turn',{'session_id':newer,'message':'读一下 README'})
            check("新会话重建时读取新的记忆快照", 'CHANGED_MEMORY_AFTER_FIRST_TURN' in app.sessions[newer]['prompt_requests'][0]['system'])
        check("提示词读取路由拒绝未知会话或请求", request(server,'/api/prompts/no-session/no-request')[0]==404)
        architecture=json.loads(request(server,'/api/sessions',{'chapter':'01'})[2])['id']
        check("架构章仅导览，后端拒绝运行演示", request(server,'/api/turn',{'session_id':architecture,'message':'运行'})[0]==400 and not app.sessions[architecture]['messages'])
        check("切换会话后的启动索引仍含全部请求入口", len(next(row for row in json.loads(request(server,'/api/bootstrap')[2])['sessions'] if row['id']==sid)['prompt_requests'])==6)

GROUPS=[('当前对话的提示词装配',test_prompt_conversation),('隔离章节实验',test_experiments),('章节 HTTP',test_chapter_http),('内存凭据',test_credentials),('请求协议',test_thinking_transport)]
if __name__=='__main__':
    for name,test in GROUPS:
        print('\n'+name)
        try: test()
        except Exception as exc: check(name+' 无用例异常',False,type(exc).__name__+': '+str(exc))
    print(f'\n{len(PASS)} 通过 / {len(FAIL)} 失败')
    raise SystemExit(1 if FAIL else 0)
