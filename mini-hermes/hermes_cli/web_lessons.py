"""Chapter catalog and bounded, isolated mechanism experiments for the Web UI.

These are real mini-hermes calls against generated fixtures, not recordings. Model
summaries in compression fixtures are explicitly scripted; the live model separately
receives the measured evidence. No experiment mutates the user's project/database.
"""
from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
MINI = ROOT / "mini-hermes"
# chapter, title, concrete focus, prompt, implementation owner, experiment kind
_ROWS = [
 ("01", "架构总览", "入口、Agent、工具与存储如何连接", "结合本章架构图，解释一条消息经过哪些模块。", "run_agent.py", "guide"),
 ("02", "Agent 循环", "模型决策 → 工具执行 → 结果回填 → 再次请求", "看看演示项目有哪些文件，并读一下 README。", "run_agent.py", "agent"),
 ("03", "提示词装配", "系统提示词分层、缓存字节稳定", "解释本次实验中提示词为何保持不变。", "agent/system_prompt.py", "mechanism"),
 ("04", "工具运行时", "注册不等于暴露，toolset 白名单决定可见工具", "解释文件工具集和默认工具集的区别。", "model_tools.py", "mechanism"),
 ("05", "上下文压缩与缓存", "压缩前后、保护头尾、工具配对", "根据压缩实验的实测结果解释压缩过程。", "agent/context_compressor.py", "mechanism"),
 ("06", "会话存储", "逐条落盘、关闭后恢复、FTS 搜索", "解释为何重开数据库还能恢复历史。", "hermes_state.py", "mechanism"),
 ("07", "网关入口", "消息先进入会话路由；相同来源复用会话", "解释网关消息为什么先确定会话归属。", "gateway/session_state.py", "mechanism"),
 ("07b", "会话生命周期", "复用与显式重置，两层队列的先后顺序", "解释显式排队和普通消息覆盖的区别。", "gateway/run_busy.py", "mechanism"),
 ("07c", "多 profile 网关", "按 home 隔离数据库与凭据作用域", "解释为什么两个 profile 不能共用凭据。", "agent/secret_scope.py", "mechanism"),
 ("08", "Provider 运行时", "服务商别名归一、可回退和不可回退错误", "解释为什么 429 可以回退而 400 不应回退。", "agent/fallback.py", "mechanism"),
 ("09", "程序化集成", "对照官方 API 与 mini 的现有边界；本章未新增子系统", "解释 mini 的 run_conversation 返回协议及集成边界。", "run_agent.py", "guide"),
 ("10", "微压缩", "每次吸收有限交换，摘要标记仍保留工具配对", "解释微压缩与全量压缩各自何时工作。", "agent/micro_compaction.py", "mechanism"),
 ("11", "状态库恢复", "只读探测 → 显式备份修复 → 再检查", "解释 FTS 派生索引恢复为什么不等于修复坏文件。", "hermes_state_repair.py", "mechanism"),
 ("12", "代码归属与投递债", "按子系统找实现和测试；未实现的投递系统不伪装成已实现", "解释本项目的代码归属规则和未实现边界。", "hermes_cli/web.py", "guide"),
 ("13", "技能系统", "先发现描述索引，再按需读取正文", "解释技能索引为何不直接加载全部正文。", "agent/skill_utils.py", "mechanism"),
 ("14", "上下文引擎插槽", "默认引擎选择与未知引擎的有声回落", "根据引擎选择结果解释可替换插槽。", "agent/engine_registry.py", "mechanism"),
 ("15", "只读观察者", "实跑观察事件类别与同一回合的关联 ID", "读取示例代码，解释入口函数。", "agent/observer.py", "agent"),
]
CATALOG = {row[0]: dict(zip(("id", "title", "focus", "prompt", "source", "kind"), row)) for row in _ROWS}


def chapter(chapter_id):
    if not isinstance(chapter_id, str) or chapter_id not in CATALOG:
        raise ValueError("未知章节")
    return CATALOG[chapter_id]


def documents(chapter_id):
    chapter(chapter_id)
    return {kind: next(ROOT.glob(prefix + "-" + chapter_id + "-*.md"), None)
            for kind, prefix in (("translation", "中文精译"), ("walkthrough", "源码讲解"))}


def catalog():
    return [{**row, "documents": [kind for kind, p in documents(row["id"]).items() if p]}
            for row in CATALOG.values()]


def run_experiment(chapter_id, home, emit):
    """Return measured evidence. Callers bind an empty secret scope and temporary home."""
    item = chapter(chapter_id)
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    evidence = []

    def record(label, observed, ok, source=None):
        result = {"label": label, "observed": observed, "ok": bool(ok),
                  "source": source or item["source"]}
        evidence.append(result)
        emit("evidence", **result)

    if item["kind"] == "guide":
        source = (MINI / item["source"]).read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
        required = {"01": ["AIAgent", "run_conversation"], "09": ["run_conversation"], "12": ["WebApp", "WebHandler"]}[chapter_id]
        record("源码入口可定位（静态导览，非运行验证）", [n for n in names if n in required], all(n in names for n in required))
        record("本章配套文档存在", [k for k,p in documents(chapter_id).items() if p], bool(documents(chapter_id)["translation"]))
        return evidence
    if chapter_id in ("02", "15"):
        # The Web adapter derives these chapters' evidence from the real running agent.
        return evidence
    if chapter_id == "03":
        from agent.system_prompt import build_system_prompt
        from run_agent import AIAgent
        agent = AIAgent({"provider": "demo", "model": "offline", "api_key": "demo"},
                        fallback_providers=[], skip_context_files=True, context_cwd=str(home))
        first = build_system_prompt(agent)
        second = build_system_prompt(agent)
        record("同一 Agent 两次装配的字节完全相等", {"first_bytes": len(first.encode()), "second_bytes": len(second.encode())}, first == second)
        record("提示词不是空串", {"characters": len(first)}, bool(first))
    elif chapter_id == "04":
        import model_tools
        file_defs = model_tools.get_tool_definitions(["file"])
        all_defs = model_tools.get_tool_definitions()
        names = [d["function"]["name"] for d in file_defs]
        record("file toolset 仅暴露文件工具", names, set(names) == {"read_file", "write_file", "list_dir"})
        record("默认工具集与 file 集不同", [d["function"]["name"] for d in all_defs], len(all_defs) > len(file_defs))
    elif chapter_id in ("05", "10"):
        from agent.context_compressor import ContextCompressor, validate_tool_pairs
        messages = [{"role": "system", "content": "保留系统规则"}]
        for i in range(18):
            messages.extend([{"role": "user", "content": f"任务 {i} " + "任务背景与约束。" * 800},
                             {"role": "assistant", "content": f"结果 {i} " + "完成的步骤与检查。" * 800}])
        messages[9:9] = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "fixture-call", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "fixture-call", "content": "夹具工具结果"}]
        compressor = ContextCompressor(context_length=16000, protect_first_n=1, protect_last_n=2,
                                       micro_compact=True, micro_compact_defrag_threshold_tokens=100000)
        if chapter_id == "05":
            after = compressor.compress(messages, call_llm=lambda *_: "## Goal\n保留任务目标\n## Progress\n已完成实验步骤\n## Next Steps\n继续验证")
            record("压缩后消息数量减少（摘要由固定夹具生成）", {"before": len(messages), "after": len(after)}, len(after) < len(messages))
        else:
            after = compressor.micro_compact(messages, call_llm=lambda *_: "已完成较早的一组交换，保留任务信息。")
            record("微压缩实际吸收一组交换（摘要为固定夹具）", {"action": compressor._micro_last_action, "before": len(messages), "after": len(after)}, compressor._micro_last_action == "absorb")
        record("系统规则保留", {"role": after[0]["role"], "original_rule": messages[0]["content"], "added_note_characters": len(after[0]["content"])-len(messages[0]["content"])}, after[0]["role"] == "system" and messages[0]["content"] in after[0]["content"])
        record("工具调用与返回仍配对", validate_tool_pairs(after), not validate_tool_pairs(after))
        record("末尾两条消息逐字保留", {"tail_roles": [m["role"] for m in after[-2:]]}, after[-2:] == messages[-2:])
    elif chapter_id in ("06", "07", "07b", "11"):
        from hermes_state import SessionDB
        db_path = home / "experiment.db"
        db = SessionDB(db_path)
        try:
            if chapter_id in ("07", "07b"):
                from gateway.session_state import SessionRouter
                router = SessionRouter(db)
                first = router.route("demo:chat", source="console")
                second = router.route("demo:chat", source="console")
                record("同一来源复用会话", {"first": first.entry.session_id, "second": second.entry.session_id}, first.entry.session_id == second.entry.session_id)
                if chapter_id == "07b":
                    third = router.route("demo:chat", source="console", force_new=True)
                    record("显式重置产生新会话", {"old": second.entry.session_id, "new": third.entry.session_id}, third.entry.session_id != second.entry.session_id)
                    from gateway.run_busy import SessionQueues
                    queues = SessionQueues()
                    for text in ("旧普通消息", "最新普通消息"):
                        queues.keep_latest(SimpleNamespace(session_key="s", text=text))
                    for text in ("显式排队 A", "显式排队 B"):
                        queues.enqueue_explicit(SimpleNamespace(session_key="s", text=text))
                    order = [queues.promote("s").text for _ in range(3)]
                    record("先履行 FIFO，再处理最新单槽", order, order == ["显式排队 A", "显式排队 B", "最新普通消息"])
                return evidence
            db.create_session("fixture", "web")
            db.append_message("fixture", "user", content="压缩与缓存实验")
            db.append_message("fixture", "assistant", content="保存实验记录")
        finally:
            db.close()
        if chapter_id == "06":
            db = SessionDB(db_path)
            try:
                rows = db.get_messages_as_conversation("fixture")
                hits = db.search_messages("实验")
                record("关闭并重开后恢复消息", {"rows": len(rows)}, len(rows) == 2)
                record("全文检索找到记录", {"hits": len(hits or [])}, bool(hits))
            finally:
                db.close()
        else:
            from hermes_state_repair import inspect_state_db, repair_state_db_schema
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("INSERT OR REPLACE INTO state_meta(key,value) VALUES('fts_stale','1')")
                conn.commit()
            finally:
                conn.close()
            before = inspect_state_db(db_path)
            record("持久降级标记让体检失败（未物理破坏文件）", {"healthy": before["healthy"]}, not before["healthy"])
            fixed = repair_state_db_schema(db_path)
            after = inspect_state_db(db_path)
            record("显式修复后基础体检通过", {"repaired": fixed["repaired"], "healthy": after["healthy"]}, fixed["repaired"] and after["healthy"])
            record("修复留下备份", {"backup_created": bool(fixed.get("backup_path"))}, bool(fixed.get("backup_path")))
    elif chapter_id == "07c":
        from agent.secret_scope import get_secret, reset_secret_scope, set_secret_scope
        from hermes_constants import reset_mini_home_override, set_mini_home_override, get_mini_home
        from hermes_state import SessionDB
        found = []
        for name in ("a", "b"):
            token = set_secret_scope({"DEMO_PROFILE_NAME": name})
            ht = set_mini_home_override(home / name)
            db = None
            try:
                db = SessionDB()
                db.create_session("shared", "web")
                db.append_message("shared", "user", content=name)
                found.append({"profile": get_secret("DEMO_PROFILE_NAME"), "home": get_mini_home().name,
                              "messages": db.get_messages_as_conversation("shared")[0]["content"]})
            finally:
                if db: db.close()
                reset_mini_home_override(ht); reset_secret_scope(token)
        record("同名会话在两个 profile 中互不串库", found, found[0]["messages"] == "a" and found[1]["messages"] == "b")
    elif chapter_id == "08":
        from agent.fallback import is_fallbackable_status
        from providers.aliases import normalize_provider
        statuses = {str(code): is_fallbackable_status(code) for code in (400, 401, 422, 429, 503)}
        record("回退判据区分请求错误与可恢复故障", statuses, statuses == {"400":False,"401":True,"422":False,"429":True,"503":True})
        record("服务商别名归一", {"deep-seek": normalize_provider("deep-seek")}, normalize_provider("deep-seek") == "deepseek")
    elif chapter_id == "13":
        from agent.skill_utils import discover_skills, build_skill_index, load_skill_body
        skills = home / "skills"; folder = skills / "demo-skill"; folder.mkdir(parents=True)
        marker = "BODY_ONLY_AFTER_EXPLICIT_LOAD"
        (folder / "SKILL.md").write_text("---\nname: demo-skill\ndescription: 演示按需加载\n---\n" + marker, encoding="utf-8")
        scan = discover_skills(skills)
        index = build_skill_index(skills)
        body = load_skill_body(scan.skills[0], skills)
        record("发现描述索引但不提前注入正文", {"index": index}, "demo-skill" in index and marker not in index)
        record("显式读取后才得到技能正文", {"body": body}, marker in body)
    elif chapter_id == "14":
        from agent.engine_registry import select_engine
        from agent.context_compressor import ContextCompressor
        default = select_engine(default_factory=ContextCompressor, root=home / "engines")
        unknown = select_engine("missing-fixture-engine", default_factory=ContextCompressor, root=home / "engines")
        record("无配置选择内置引擎", {"engine": type(default.engine).__name__, "warning": default.warning}, isinstance(default.engine, ContextCompressor) and not default.warning)
        record("未知引擎有声回落", {"engine": type(unknown.engine).__name__, "warning": unknown.warning}, isinstance(unknown.engine, ContextCompressor) and bool(unknown.warning))
    return evidence
