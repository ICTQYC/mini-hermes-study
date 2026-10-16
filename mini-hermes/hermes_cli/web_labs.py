"""Interactive chapter experiments on owned fixtures, using the implementation's public APIs.

No lab calls an external model or loads a user's plugin. Summary/transport fixtures
are labelled as such; selection, compaction, dispatch, recovery and routing are real.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import time
from pathlib import Path

LABS = {
    "04": {"title": "工具从注册到执行", "actions": ["inspect", "execute"]},
    "05": {"title": "上下文达到阈值之后", "actions": ["reset", "compress"]},
    "08": {"title": "故障怎样触发回退", "actions": ["run"]},
    "10": {"title": "每次只吸收一组交换", "actions": ["reset", "step"]},
    "11": {"title": "只读体检与显式修复", "actions": ["inspect", "degrade", "repair"]},
    "14": {"title": "同一宿主怎样选择不同引擎", "actions": ["select"]},
}


def integer(data, key, default, low, high):
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{key} 必须是 {low}–{high} 的整数")
    return value


def transcript(count=8, repeat=1):
    rows = [{"role": "system", "content": "项目要求：保留目标、关键结论与下一步。"}]
    for i in range(count):
        rows.extend([{"role": "user", "content": f"任务 {i+1}：检查缓存策略。" + "约束：不能改变用户原始要求。"*repeat},
                     {"role": "assistant", "content": f"结论 {i+1}：已记录输入、输出与后续步骤。" + "需要保留已经确认的决定。"*repeat}])
    rows[5:5] = [{"role": "assistant", "content": None, "tool_calls": [{"id": "lab-read", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"README.md"}'}}]},
                 {"role": "tool", "tool_call_id": "lab-read", "content": "项目要求：零第三方依赖。"}]
    return rows


def run(chapter, action, data, home, state):
    if chapter not in LABS or action not in LABS[chapter]["actions"]:
        raise ValueError("本章不支持该实验操作")
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    report = {"title": LABS[chapter]["title"], "chapter": chapter, "action": action, "steps": [], "parameters": {}}
    def step(title, value, explanation):
        report["steps"].append({"title": title, "value": value, "explanation": explanation})
    if chapter == "04":
        import model_tools
        from tools import registry
        model_tools.discover_builtin_tools()
        selected = data.get("toolset", "file")
        if selected not in ("file", "default"):
            raise ValueError("只支持 file 或 default 工具集")
        definitions = model_tools.get_tool_definitions(["file"] if selected == "file" else None)
        visible = [d["function"]["name"] for d in definitions]
        report["parameters"] = {"toolset": selected}
        step("注册表里的工具", registry.tool_names(), "注册仅表示分发器知道这个工具，不代表模型能看见它。")
        step("经过 toolset 与可用性过滤后的 schema", definitions, "这是 get_tool_definitions 的实际结果。todo_list 属于 Agent 级工具，可能不在普通注册表中。")
        name = data.get("tool", "read_file")
        if name not in ("read_file", "write_file", "list_dir", "terminal"):
            raise ValueError("未知演示工具")
        target = home/"note.txt"
        if not target.exists(): target.write_text("实验文件：初始内容。", encoding="utf-8")
        if action == "execute":
            text = data.get("text", "实验文件：写入的新内容。")
            if not isinstance(text, str) or len(text)>1000: raise ValueError("文件内容最多 1000 字符")
            args = {"path": str(home)} if name == "list_dir" else {"path": str(target)}
            if name == "write_file": args["content"] = text
            step("可见性闸门", {"requested": name, "visible": visible, "allowed": name in visible}, "先判断该工具是否在本次模型可见集合中，再决定是否分发。")
            if name not in visible:
                step("调用被阻止", {"dispatched": False}, "白名单外的工具不能因为已经注册就绕过闸门。")
            elif name == "terminal":
                step("实验范围", {"dispatched": False}, "本页仅展示 terminal 是否可见，不执行终端命令；文件工具只操作生成的实验目录。")
            else:
                result = json.loads(model_tools.handle_function_call(name, args))
                step("真实分发与结果", {"arguments": args, "result": result}, "调用现有 handle_function_call；下一次读取同一文件可看到实际写入结果。")
    elif chapter in ("05", "10"):
        from agent.context_compressor import ContextCompressor, validate_tool_pairs, estimate_messages_tokens_rough
        first = integer(data, "protect_first", 1, 1, 3)
        last = integer(data, "protect_last", 2, 2, 8)
        window = integer(data, "window", 16000, 4000, 64000)
        if "messages" not in state or action == "reset":
            state.clear()
            state["messages"] = transcript(12, 220)
        engine = state.get("engine")
        if engine is None or chapter == "05":
            engine = ContextCompressor(context_length=window, protect_first_n=first, protect_last_n=last,
                micro_compact=True, micro_compact_defrag_threshold_tokens=100000)
            state["engine"] = engine
        engine.protect_first_n, engine.protect_last_n, engine.context_length = first, last, window
        report["parameters"] = {"protect_first": first, "protect_last": last, "window": window, "summary": "固定离线摘要夹具，不调用真实模型"}
        before = copy.deepcopy(state["messages"])
        calls = []
        def summary(system, user):
            text = "## Goal\n验证缓存策略，保持用户原始要求。\n## Progress\n较早交换的决定已汇总。\n## Next Steps\n继续检查剩余步骤。"
            calls.append({"system": system, "user": user, "response": text})
            return text
        needed = engine.should_compress(before)
        step("操作前的完整对话", before, "这些是生成的实验消息，包含一个真实格式的工具调用/返回对；可逐条核对去向。")
        step("触发条件", {"estimated_tokens": estimate_messages_tokens_rough(before), "threshold_tokens": engine.threshold_tokens, "should_compress": needed}, "token 数是本地估算。全量压缩遵守阈值；微压缩按交换吸收，与全量阈值不同。")
        after = before
        if action != "reset":
            if chapter == "05" and needed:
                after = engine.compress(before, call_llm=summary)
            elif chapter == "10":
                after = engine.micro_compact(before, call_llm=summary)
        state["messages"] = copy.deepcopy(after)
        from agent.context_compressor import is_context_summary_message
        report["message_map"] = [
            {"index":i+1, "role":m.get("role"), "state":"原样保留" if m in after else "系统规则保留并附加说明" if m.get("role")=="system" and any(m.get("content", "") in a.get("content", "") for a in after if a.get("role")=="system") else "不再原样保留",
             "preview":str(m.get("content") or m.get("tool_calls"))[:100]}
            for i,m in enumerate(before)]
        report["context_map"] = [{"index":i+1, "role":m.get("role"), "state":"摘要消息" if is_context_summary_message(m) else "保留消息", "preview":str(m.get("content") or m.get("tool_calls"))[:160]} for i,m in enumerate(after)]
        step("摘要调用的实际输入与固定输出", calls, "只在压缩器确实请求摘要时才出现记录；空列表表示本次没有摘要调用。")
        step("操作后的完整对话", after, "可对照看到摘要、受保护的头尾以及工具调用配对；连续点击微压缩可观察滚动摘要如何更新。")
        report["metrics"] = {"before_messages": len(before), "after_messages": len(after), "summary_calls": len(calls),
            "tool_pair_errors": validate_tool_pairs(after), "action": getattr(engine, "_micro_last_action", "") if chapter=="10" else ("compressed" if calls else "unchanged")}
    elif chapter == "08":
        from run_agent import AIAgent, ApiHTTPError
        from agent.observer import ObserverRegistry, OBSERVER_EVENTS
        from agent.fallback import fallback_state
        status = integer(data, "status", 429, 400, 599)
        events = []; requests = []
        observer = ObserverRegistry()
        for name in OBSERVER_EVENTS: observer.add(name, lambda **payload: events.append(payload))
        agent = AIAgent({"provider":"demo", "model":"primary", "api_key":"fixture", "base_url":"http://offline.invalid", "api_mode":"chat_completions"},
            fallback_providers=[{"provider":"openai", "model":"backup", "base_url":"http://offline.invalid", "api_key":"fixture"}], observer=observer, skip_context_files=True, context_cwd=str(home))
        def transport(candidate, messages):
            requests.append({"model":candidate["model"], "messages":messages, "injected_http_status":status if candidate["model"]=="primary" else 200})
            if candidate["model"] == "primary": raise ApiHTTPError(status, "实验注入的 HTTP 故障")
            return {"choices":[{"message":{"role":"assistant", "content":"备用模型的固定演示响应"}}]}
        agent._http_post = transport
        result = agent.run_conversation("请验证本次故障处理。")
        report["parameters"] = {"http_status":status, "transport":"固定离线响应；没有外部 API 请求"}
        step("主模型与备用链", {"primary":"primary", "backup":["backup"]}, "使用真实 Agent 的 fallback 路径；故障由实验传输函数注入。")
        step("实际请求尝试", requests, "429/5xx 等可恢复错误可能触发备用；400 等确定性错误应直接结束。")
        step("实际观察者事件", events, "每次请求都有自己的 api_request_id；请求错误与回退不是工具事件。")
        step("回合结果与当前路由", {"answer":result.get("final_response"), "error":bool(result.get("error")), "runtime":fallback_state(agent)}, "结果来自真正的回退状态机，不按状态码预先画一条成功路线。")
    elif chapter == "11":
        from hermes_state import SessionDB
        from hermes_state_repair import inspect_state_db, repair_state_db_schema
        path = home/"state.db"
        if not path.exists():
            db = SessionDB(path)
            try:
                db.create_session("recovery-demo", "web")
                db.append_message("recovery-demo", "user", content="缓存实验的原始记录")
            finally: db.close()
        before = inspect_state_db(path)
        step("操作前只读体检", before, "体检不会自动修复或偷偷改动数据库。")
        if action == "degrade":
            conn = sqlite3.connect(path)
            try:
                conn.execute("INSERT OR REPLACE INTO state_meta(key,value) VALUES('fts_stale','1')"); conn.commit()
            finally: conn.close()
            step("注入降级标记", {"fts_stale":True}, "这里只设置实验库的 FTS 降级标记，不物理损坏文件，也不触碰使用者的库。")
        if action == "repair":
            fixed = repair_state_db_schema(path)
            step("显式备份与修复结果", fixed, "只在点击修复时调用 repair_state_db_schema。备份来自这次实际修复。")
        after = inspect_state_db(path)
        step("操作后体检", after, "连续操作可观察健康 → 降级 → 修复后的状态变化。")
        db = SessionDB(path)
        try:
            step("原始记录与当前搜索结果", {"messages":db.get_messages("recovery-demo"), "hits":db.search_messages("缓存")}, "派生索引恢复不应丢失原始消息。")
        finally: db.close()
    elif chapter == "14":
        from agent.context_compressor import ContextCompressor
        from agent.engine_registry import select_engine, discover_engines
        selected = data.get("engine", "compressor")
        if selected not in ("compressor", "recent-turns", "unknown"): raise ValueError("未知实验引擎选项")
        folder = home/"engines/recent-turns";folder.mkdir(parents=True, exist_ok=True)
        code = ('from agent.context_engine import ContextEngine\nclass RecentTurns(ContextEngine):\n'
                '    @property\n    def name(self): return "recent-turns"\n'
                '    def should_compress(self, messages): return len(messages)>5\n'
                '    def compress(self, messages, **kwargs): return [*messages[:1], *messages[-2:]]\n')
        (folder/"__init__.py").write_text(code, encoding="utf-8")
        chosen = select_engine(selected, root=home/"engines", default_factory=ContextCompressor)
        before = transcript(4)
        needed = chosen.engine.should_compress(before)
        after = chosen.engine.compress(before) if needed else before
        report["parameters"] = {"engine":selected}
        step("实验插件源码", code, "固定的示例插件只保留首条与最后一组消息，刻意演示有损策略；不是推荐的生产压缩器。只加载生成目录中的代码。")
        step("发现与选择", {"discovered":[e.name for e in discover_engines(home/"engines").engines], "requested":selected, "selected":chosen.engine.name, "warning":chosen.warning}, "相同宿主调用接口。未知引擎回落时，警告必须可见。")
        step("同一份输入对话", before, "切换默认、示例、未知引擎，比较实际选择与输出。")
        step("引擎实际输出", {"should_compress":needed, "messages":after}, "由选中引擎的方法决定行为，宿主不按引擎名手写输出。")
    report["duration_ms"] = round((time.perf_counter()-started)*1000)
    return report


def notes(report):
    """Bounded observed excerpts for an optional live-model explanation, not another lab run."""
    def compact(value, depth=0):
        if depth > 5: return "（层级已截取，完整内容见实验面板）"
        if isinstance(value, str): return value[:800] + ("（已截取）" if len(value)>800 else "")
        if isinstance(value, dict): return {k:compact(v,depth+1) for k,v in value.items()}
        if isinstance(value, list): return [compact(v,depth+1) for v in value[:3]] + ([{"omitted_items":len(value)-3}] if len(value)>3 else [])
        return value
    return {"action":report["action"], "parameters":report["parameters"], "metrics":report.get("metrics"),
            "notice":"以下为本次实验的实测片段；长内容已截取，完整记录在实验面板。摘要与故障响应为离线夹具。",
            "steps":[{"title":row["title"], "observed":compact(row["value"])} for row in report["steps"]]}
