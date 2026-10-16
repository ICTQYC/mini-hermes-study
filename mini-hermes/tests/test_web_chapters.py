"""Detailed chapter demos: real conversations, stateful labs, and visible boundaries."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from tests.test_web import fixture, request, events
from hermes_cli import web, web_labs, web_lessons
from agent.secret_scope import set_secret_scope, reset_secret_scope
from hermes_constants import set_mini_home_override, reset_mini_home_override

PASS, FAIL = [], []
def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    print(("✓ " if ok else "✗ ")+label+(" → "+str(detail) if not ok else ""))

def lab_fixture(callback):
    with tempfile.TemporaryDirectory(prefix='chapter-lab-test-') as area:
        home=Path(area);ht=set_mini_home_override(home);st=set_secret_scope({})
        try: callback(home)
        finally: reset_secret_scope(st);reset_mini_home_override(ht)


def test_labs():
    def run(home):
        tool_home=home/'tools'
        read=web_labs.run('04','execute',{'tool':'read_file'},tool_home,{})
        check('工具 schema 包含文件工具', any(d['function']['name']=='read_file' for d in read['steps'][1]['value']))
        check('实际读取实验文件', '初始内容' in str(read['steps'][-1]['value']))
        web_labs.run('04','execute',{'tool':'write_file','text':'CHANGED_LAB_FILE'},tool_home,{})
        read=web_labs.run('04','execute',{'tool':'read_file'},tool_home,{})
        check('先写后读反映真实文件副作用', 'CHANGED_LAB_FILE' in str(read['steps'][-1]['value']))
        blocked=web_labs.run('04','execute',{'tool':'terminal','toolset':'file'},tool_home,{})
        check('白名单外工具被阻止而非假执行', blocked['steps'][-1]['title']=='调用被阻止' and blocked['steps'][-1]['value']=={'dispatched':False} and not blocked['steps'][-2]['value']['allowed'])
        state={}; first=web_labs.run('05','reset',{},home/'compression',state)
        check('重置不会调用摘要或提前压缩', first['metrics']['summary_calls']==0 and first['metrics']['before_messages']==first['metrics']['after_messages'])
        initial=copy.deepcopy(state['messages'])
        compressed=web_labs.run('05','compress',{},home/'compression',state)
        after=state['messages']
        check('按阈值调用真正压缩器并减少消息', compressed['metrics']['summary_calls']>0 and len(after)<len(initial),compressed['metrics'])
        check('摘要输入输出在报告中可审计', bool(compressed['steps'][2]['value']) and 'system' in compressed['steps'][2]['value'][0])
        check('系统规则与最近交换仍保留', initial[0]['content'] in after[0]['content'] and after[-2:]==initial[-2:])
        check('压缩后的工具配对有效', compressed['metrics']['tool_pair_errors']==[])
        check('每条原始消息的去向可核对',len(compressed.get('message_map',[]))==len(initial) and any(row['state']=='不再原样保留' for row in compressed.get('message_map',[])))
        check('新上下文图来自实际输出并标出摘要',len(compressed.get('context_map',[]))==len(after) and any(row['state']=='摘要消息' for row in compressed.get('context_map',[])))
        again=web_labs.run('05','compress',{},home/'compression',state)
        check('低于阈值再次点击不会伪造摘要调用', again['metrics']['summary_calls']==0)
        micro={};web_labs.run('10','reset',{},home/'micro',micro)
        a=web_labs.run('10','step',{},home/'micro',micro)
        b=web_labs.run('10','step',{},home/'micro',micro)
        check('连续微压缩每次实际吸收一个交换', a['metrics']['action']==b['metrics']['action']=='absorb' and a['metrics']['summary_calls']==b['metrics']['summary_calls']==1)
        check('微压缩未按消息条数猜测是否发生', a['metrics']['before_messages']==a['metrics']['after_messages'] and a['metrics']['action']=='absorb')
        check('第二步从前一步状态继续，工具配对不破坏', b['metrics']['before_messages']==a['metrics']['after_messages'] and not b['metrics']['tool_pair_errors'])
        for code,count,error in ((429,2,False),(400,1,True),(503,2,False)):
            report=web_labs.run('08','run',{'status':code},home/str(code),{})
            attempted=report['steps'][1]['value'];result=report['steps'][-1]['value']
            check(str(code)+' 的真实重试路径与结果',len(attempted)==count and result['error']==error)
            requests=[d for d in report['steps'][2]['value'] if d.get('event')=='pre_api_request']
            check(str(code)+' 每次模型尝试有独立请求 ID',len({d['api_request_id'] for d in requests})==count)
        recovery=home/'recovery'
        healthy=web_labs.run('11','inspect',{},recovery,{})
        degraded=web_labs.run('11','degrade',{},recovery,{})
        fixed=web_labs.run('11','repair',{},recovery,{})
        check('健康→持久降级→显式修复状态真实变化',healthy['steps'][0]['value']['healthy'] and not degraded['steps'][-2]['value']['healthy'] and fixed['steps'][-2]['value']['healthy'])
        backup=fixed['steps'][1]['value'].get('backup_path')
        check('修复的备份真实存在',bool(backup) and Path(backup).is_file())
        check('修复后原始消息仍然可检索','缓存实验' in str(fixed['steps'][-1]['value']))
        default=web_labs.run('14','select',{'engine':'compressor'},home/'engines',{})
        plugin=web_labs.run('14','select',{'engine':'recent-turns'},home/'engines',{})
        unknown=web_labs.run('14','select',{'engine':'unknown'},home/'engines',{})
        check('插件由真实发现机制加载并选中',plugin['steps'][1]['value']['selected']=='recent-turns' and 'recent-turns' in plugin['steps'][1]['value']['discovered'])
        check('相同输入经不同策略产生不同输出',len(default['steps'][-1]['value']['messages'])>len(plugin['steps'][-1]['value']['messages'])==3)
        check('未知引擎有声回落并保留默认行为',bool(unknown['steps'][1]['value']['warning']) and unknown['steps'][1]['value']['selected']=='compressor' and unknown['steps'][-1]['value']==default['steps'][-1]['value'])
        rejected=False
        try: web_labs.run('05','compress',{'window':'bad'},home/'bad',{})
        except ValueError: rejected=True
        check('非法实验参数可诊断地拒绝',rejected)
    lab_fixture(run)


def create(server,chapter):
    return json.loads(request(server,'/api/sessions',{'chapter':chapter})[2])['id']
def action(server,sid,name,**kw):
    return request(server,'/api/chapter-action',{'session_id':sid,'action':name,**kw})


def test_conversation():
    with fixture() as (app,server,home):
        sid=create(server,'02')
        stream=events(request(server,'/api/turn',{'session_id':sid,'message':'读 README'})[2])
        rows=app.sessions[sid]['prompt_requests']
        check('循环章保存每次实际请求及响应',len(rows)==3 and all('response' in row for row in rows))
        calls=rows[0].get('response',{}).get('choices',[{}])[0].get('message',{}).get('tool_calls',[])
        ids={c['id'] for c in calls}
        check('工具响应通过同一调用 ID 回填到下一请求',bool(ids) and any(m.get('tool_call_id') in ids for m in rows[1]['messages']))
        check('最终请求响应结束于自然语言而非凭空补完',not rows[-1]['response']['choices'][0]['message'].get('tool_calls') and any(k=='result' for k,d in stream))
        failure=create(server,'02')
        request(server,'/api/turn',{'session_id':failure,'message':'不存在的文件'})
        detail=json.loads(request(server,'/api/chapter/'+failure)[2])
        check('真实工具失败仍可沿事件追踪',any(d['event']=='post_tool_call' and d['status']=='error' for d in detail['events']))
        stored=create(server,'06')
        request(server,'/api/turn',{'session_id':stored,'message':'读取 README 并记住 Hello'})
        db_view=app.sessions[stored]['storage'];old_ids=[r['id'] for r in db_view['rows']]
        check('会话页展示来自真实 DB 的完整消息行',len(old_ids)>2 and all('active' in row and 'id' in row for row in db_view['rows']))
        old_agent=app.sessions[stored]['agent']
        restored=json.loads(action(server,stored,'restore')[2])['storage']
        check('恢复明确丢弃 Agent 内存后读取当前库',app.sessions[stored]['agent'] is None and restored.get('memory_reloaded') and app.sessions[stored]['history']==restored['restored'])
        request(server,'/api/turn',{'session_id':stored,'message':'继续读取入口代码'})
        check('恢复后续聊创建新 Agent 且旧记录不重复',app.sessions[stored]['agent'] is not old_agent and [r['id'] for r in app.sessions[stored]['storage']['rows']][:len(old_ids)]==old_ids)
        searched=json.loads(action(server,stored,'search',query='README')[2])['storage']['search']
        check('FTS 搜索仅返回当前会话实际匹配',bool(searched['hits']) and all(r['session_id']==stored for r in searched['hits']))
        skill=create(server,'13')
        request(server,'/api/turn',{'session_id':skill,'message':'先用 skill_view 读取技能，再读代码'})
        samples=app.sessions[skill]['prompt_requests']
        first=samples[0]['messages'];later=samples[1]['messages'] if len(samples)>1 else []
        check('技能先以索引出现，正文尚未进入首个请求','demo-review' in str(first) and 'BODY_LOADED_ON_DEMAND' not in str(first))
        check('正文经 skill_view 的真实结果进入下一请求',any(m.get('role')=='tool' and 'BODY_LOADED_ON_DEMAND' in str(m.get('content')) for m in later))
        observer=create(server,'15');action(server,observer,'observer',enabled=True)
        reply=events(request(server,'/api/turn',{'session_id':observer,'message':'读取入口代码'})[2])
        observed=json.loads(request(server,'/api/chapter/'+observer)[2])
        check('失败观察者被实际调用但不阻止 Agent 回答',observed['observer_faults']>0 and any(k=='result' and not d['error'] for k,d in reply))
        check('观察者页保留真实关联 ID 和各类事件',len({d['turn_id'] for d in observed['events']})==1 and len({d['api_request_id'] for d in observed['events'] if d.get('event')=='pre_api_request'})==3)
        check('切换后 API 仍能回看旧会话请求',json.loads(request(server,f"/api/prompts/{sid}/{rows[0]['id']}")[2])['response']==rows[0]['response'])


def test_endpoints():
    with fixture() as (app,server,home):
        catalog=json.loads(request(server,'/api/bootstrap')[2])['chapters']
        check('选择性展示类型准确', {c['id'] for c in catalog if c['presentation']=='lab'}==set(web_labs.LABS))
        for code in web_lessons.GUIDE_CHAPTERS:
            sid=create(server,code)
            check(code+' 导览不伪造运行',request(server,'/api/turn',{'session_id':sid,'message':'运行'})[0]==400)
        sid=create(server,'04')
        check('实验操作使用现有来源和页面令牌校验',request(server,'/api/chapter-action',{'session_id':sid,'action':'inspect'},{'X-Mini-Token':''})[0]==403)
        status,_,body=action(server,sid,'inspect')
        report=json.loads(body)['lab']
        check('HTTP 实验返回完整步骤和可审计数据',status==200 and len(report['steps'])==2 and 'schema' in report['steps'][1]['title'])
        check('实验不写入用户数据目录',not (home/'mini').exists())
        check('未知动作及无效工具返回清晰错误',action(server,sid,'invalid')[0]==400 and action(server,sid,'execute',tool='arbitrary')[0]==400)
        check('实验目录由服务拥有并按会话隔离',(app.root/'labs'/sid).is_dir())
        other=create(server,'04');action(server,other,'execute',tool='read_file')
        check('不同实验会话不共享文件内容',(app.root/'labs'/other/'note.txt').read_text()=='实验文件：初始内容。')
        fake='chapter-fixture-key-not-real';app.configure_model({'api_key':fake})
        check('已知密钥不能误写到实验文件',action(server,sid,'execute',tool='write_file',text=fake)[0]==400 and fake not in (app.root/'labs'/sid/'note.txt').read_text())
        live=app.create_session('live','05')['id']
        action(server,live,'compress')
        captured=[]
        spec={'provider':'fixture','model':'fixture','api_key':'fixture-key','base_url':'http://fixture.invalid','api_mode':'chat_completions'}
        def respond(candidate,messages):
            captured.append(str(messages))
            return {'choices':[{'message':{'role':'assistant','content':'fixture explanation'}}]}
        with patch.object(web,'resolve_provider',return_value=spec), patch.object(web.AIAgent,'_http_post',side_effect=respond):
            request(server,'/api/turn',{'session_id':live,'message':'解释我刚才那次压缩'})
        check('真实模型入口收到本次实验的实际参数与摘要片段',bool(captured) and '最新实验操作与参数' in captured[0] and '较早交换的决定已汇总' in captured[0])
        turn=web.Turn(sid);app.active=turn
        check('运行中禁止恢复或修改实验状态',action(server,sid,'inspect')[0]==409)
        app.active=None
        check('非法详情路径不能读取任意文件',request(server,'/api/chapter/../../private')[0]==404)
        check('实验输入不会修改服务商配置',app.web_model=='deepseek-flash')

GROUPS=[('真实模块的可操作实验',test_labs),('当前对话的章节过程',test_conversation),('接口与隔离',test_endpoints)]
if __name__=='__main__':
    for name,test in GROUPS:
        print('\n'+name)
        try: test()
        except Exception as exc: check(name+' 无用例异常',False,type(exc).__name__+': '+str(exc))
    print(f'\n{len(PASS)} 通过 / {len(FAIL)} 失败')
    raise SystemExit(1 if FAIL else 0)
