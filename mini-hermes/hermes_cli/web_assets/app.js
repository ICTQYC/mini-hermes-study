"use strict";
const $ = (id) => document.getElementById(id);
const state = {token: "", sessions: [], session: null, mode: "offline", busy: false, events: 0,
  promptSelection: null, promptLoad: 0, chapters: [], chapter: "02", config: {}, chapterView: 0, chapterChoice: null, tools: 0, started: 0, usage: false, stopped: false, approval: null};
const labels = {pre_llm_call: "Agent 请求模型", post_llm_call: "模型返回决策", pre_api_request: "发送模型请求",
  post_api_request: "收到模型响应", api_request_error: "模型请求失败", pre_tool_call: "开始执行工具", post_tool_call: "工具执行完成"};
function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
function notice(message) {
  $("notice").textContent = message; $("notice").hidden = false;
  clearTimeout(notice.timer); notice.timer = setTimeout(() => { $("notice").hidden = true; }, 6000);
}
async function api(path, data) {
  const response = await fetch(path, data === undefined ? {cache: "no-store"} : {
    method: "POST", headers: {"Content-Type": "application/json", "X-Mini-Token": state.token},
    body: JSON.stringify(data)});
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `请求失败 (${response.status})`);
  }
  return response;
}
function setBusy(value, status) {
  state.busy = value;
  $("send").disabled = value; $("prompt").disabled = value;
  $("mode-offline").disabled = value; $("mode-live").disabled = value;
  document.querySelectorAll("#lab-controls button,#chapter-controls button").forEach(el => { el.disabled = value; });
  $("chapter-select").disabled = value; $("run-lesson").disabled = value; $("model-settings").disabled = value;
  $("new-session").disabled = value; $("stop").hidden = !value;
  $("run-status").textContent = status || (value ? "运行中" : "就绪");
  $("run-status").classList.toggle("busy", value);
  document.querySelectorAll(".session-button,.presets button").forEach(el => { el.disabled = value; });
}
function modeUI(mode) {
  state.mode = mode;
  $("mode-offline").classList.toggle("selected", mode === "offline");
  $("mode-live").classList.toggle("selected", mode === "live");
  $("mode-description").textContent = mode === "offline"
    ? "隔离机制实验；循环章节使用固定 provider，无需密钥或网络。"
    : "真实模型解释本章实测证据；工具可读写服务当前工作区。";
  $("mode-badge").textContent = mode === "offline" ? "DEMO" : "LIVE";
  $("usage-note").textContent = mode === "offline" ? "离线用量为演示估算；耗时为实际测量。" : "用量来自模型响应；未返回时显示 —。";
  $("model").textContent = mode === "offline" ? "scripted-offline" : "等待后端解析模型";
  $("provider").textContent = mode === "offline" ? "OFFLINE" : "LIVE";
  $("workspace").textContent = mode === "offline" ? "sample-project" : "当前工作区";
}
function resetTrace() {
  state.events = 0; state.tools = 0; state.usage = false;
  $("trace").replaceChildren(); $("evidence").replaceChildren(); $("event-count").textContent = "0 EVENTS";
  $("tool-count").textContent = "0";
  for (const id of ["input-tokens", "output-tokens", "duration", "context-window"]) $(id).textContent = "—";
}
function message(role, text) {
  $("welcome")?.remove();
  const row = node("article", "message " + role);
  row.append(node("span", "avatar", role === "user" ? "你" : "✳"));
  const body = node("div", "message-body");
  body.append(node("div", "message-author", role === "user" ? "你" : "mini-hermes"), node("div", "message-text", text));
  row.append(body); $("messages").append(row); $("messages").scrollTop = $("messages").scrollHeight;
}
function sessionsUI() {
  $("sessions").replaceChildren();
  for (const session of [...state.sessions].reverse()) {
    const button = node("button", "session-button" + (session.id === state.session?.id ? " active" : ""));
    button.append(node("span", "", session.mode === "offline" ? "◈" : "◎"), node("span", "", session.title));
    button.disabled = state.busy; button.title = session.title;
    button.addEventListener("click", () => selectSession(session));
    $("sessions").append(button);
  }
}
const welcome = $("welcome").cloneNode(true);
function bindPresets() {
  document.querySelectorAll("[data-prompt]").forEach(button => button.addEventListener("click", () => {
    if (state.busy) return;
    $("prompt").value = button.dataset.prompt;
    $("composer").requestSubmit();
  }));
}
function renderMessages(session) {
  $("messages").replaceChildren();
  if (!session.messages.length) {
    const start = welcome.cloneNode(true);
    const lesson = state.chapters.find(c => c.id === session.chapter);
    if (lesson) {
      start.querySelector("h2").textContent = lesson.title;
      start.querySelector("p").textContent = lesson.focus;
      const presets = start.querySelector(".presets"); presets.replaceChildren();
      const button = node("button", "", "运行本章场景 ↗"); button.dataset.prompt = lesson.prompt; presets.append(button);
      start.querySelector(".welcome-footnote").textContent = lesson.kind === "guide" ? "文档与源码导览，不代表新增了独立子系统。" : "实测结果会显示在上方；可展开轨迹核对原始数据。";
    }
    if (lesson?.id === "03") start.querySelector(".welcome-footnote").textContent = "在同一会话继续追问，右侧逐次展示实际提示词。";
    if (session.mode === "live") {
      start.querySelector(".welcome-footnote").textContent = "真实模式使用启动服务时的工作区与现有工具权限。";
      start.querySelectorAll("[data-prompt]").forEach(button => {
        button.dataset.prompt = button.dataset.prompt.replaceAll("演示项目", "当前项目").replaceAll("示例代码", "项目代码");
      });
    }
    $("messages").append(start); bindPresets();
  }
  for (const msg of session.messages) message(msg.role, msg.content);
}
async function watchRunning(id) {
  try {
    while (state.busy && state.session?.id === id) {
      await new Promise(resolve => setTimeout(resolve, 500));
      await refresh();
      const session = state.sessions.find(s => s.id === id);
      if (session && !session.running) {
        setBusy(false); selectSession(session); $("run-status").textContent = "运行已结束"; return;
      }
    }
  } catch (error) { setBusy(false, "连接失败"); notice(error.message); }
}
function selectSession(session) {
  if (state.busy) return;
  state.session = session; state.chapter = session.chapter; modeUI(session.mode); resetTrace(); lessonUI(session.evidence || []);
  $("session-short").textContent = session.id.slice(0, 8);
  renderMessages(session);
  if (session.model) $("model").textContent = session.model;
  setBusy(Boolean(session.running), session.running ? "正在另一页面运行" : "就绪");
  sessionsUI();
  if (session.running) watchRunning(session.id);
}
async function createSession(mode = state.mode) {
  if (state.busy) return;
  const response = await api("/api/sessions", {mode, chapter: state.chapter});
  const session = await response.json(); state.sessions.push(session); selectSession(session);
}
function trace(title, subtitle, data, kind = "") {
  if (!state.events) $("trace").replaceChildren();
  state.events += 1; $("event-count").textContent = `${state.events} EVENTS`;
  const row = node("div", "trace-row " + kind);
  const heading = node("div", "trace-title");
  heading.append(node("span", "", title), node("span", "trace-time", ((performance.now() - state.started) / 1000).toFixed(1) + "s"));
  row.append(heading);
  if (subtitle) row.append(node("div", "trace-subtitle", subtitle));
  if (data) { const details = node("details"); details.append(node("summary", "", "查看详情"), node("pre", "", JSON.stringify(data, null, 2))); row.append(details); }
  $("trace").append(row);
  if ($("trace").children.length > 200) $("trace").firstElementChild.remove();
  $("trace").scrollTop = $("trace").scrollHeight;
}
function consume(kind, data) {
  if (kind === "started") {
    $("model").textContent = data.model; $("provider").textContent = data.provider.toUpperCase();
    if (data.provider === "experiment") $("usage-note").textContent = "机制实验未调用模型；耗时为实际测量。";
    $("workspace").textContent = data.workspace; $("context-window").textContent = data.context_length?.toLocaleString() || "未配置";
    state.session.model = data.model;
    trace("回合开始", data.provider === "experiment" ? "实际机制实验 · 未调用模型" : data.mode === "offline" ? "离线 provider · 真实 Agent 循环" : "真实模型 · 本地工具");
  } else if (kind === "prompt_request") {
    state.session.prompt_requests ||= [];
    state.session.prompt_requests.push(data);
    if (state.chapter === "03") promptRequestsUI(data.id);
    else scheduleChapter();
  } else if (kind === "evidence") {
    evidenceUI(data);
    trace(data.ok ? "本章观察已验证" : "本章观察未满足", data.label, data, data.ok ? "success" : "error");
  } else if (kind === "lesson") {
    $("lesson-title").textContent = data.title;
  } else if (kind === "trace") {
    scheduleChapter();
    if (data.event === "pre_tool_call") $("tool-count").textContent = String(++state.tools);
    if (data.event === "post_api_request" && (data.usage?.prompt_tokens != null || data.usage?.completion_tokens != null)) {
      state.usage = true;
      $("input-tokens").textContent = data.usage.prompt_tokens?.toLocaleString() ?? "—";
      $("output-tokens").textContent = data.usage.completion_tokens?.toLocaleString() ?? "—";
    }
    const error = data.status === "error" || data.status === "denied" || data.event === "api_request_error";
    const title = error ? (data.tool_name ? "工具返回错误或拒绝" : "模型请求失败") : labels[data.event] || data.event;
    const subtitle = [data.tool_name || data.model, data.duration_ms != null ? `${data.duration_ms} ms` : "", data.status].filter(Boolean).join(" · ");
    trace(title, subtitle, data, error ? "error" : data.tool_name ? "tool" : data.status === "ok" ? "success" : "");
  } else if (kind === "status") {
    trace("Agent 状态", data.text);
  } else if (kind === "approval") {
    state.approval = data.approval_id;
    $("approval-command").textContent = data.command;
    $("approval-dialog").returnValue = "deny";
    $("approval-dialog").showModal(); trace("等待命令审批", "请确认页面中的命令", null, "tool");
  } else if (kind === "result") {
    if (data.model) $("model").textContent = data.model;
    if (data.provider) $("provider").textContent = data.provider.toUpperCase();
    if (data.text) { message("assistant", data.text); state.session.messages.push({role: "assistant", content: data.text}); }
    if (state.usage && data.usage) {
      $("input-tokens").textContent = data.usage.prompt_tokens.toLocaleString();
      $("output-tokens").textContent = data.usage.completion_tokens.toLocaleString();
    }
    $("duration").textContent = (data.duration_ms / 1000).toFixed(2) + "s";
    const status = data.interrupted || state.stopped ? "已停止" : data.error || data.budget_exhausted ? "未完成" : "已完成";
    trace(status, `${data.iterations ?? 0} 次模型迭代 · ${data.duration_ms} ms`, null, status === "已完成" ? "success" : "error");
    $("run-status").textContent = status;
  } else if (kind === "error") {
    notice(data.message); trace("运行失败", data.message, null, "error"); $("run-status").textContent = "运行失败";
  }
}
async function stream(response) {
  const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "", doneSeen = false;
  try {
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
      let end;
      while ((end = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, end); buffer = buffer.slice(end + 2);
        const event = block.split("\n").find(line => line.startsWith("event: "))?.slice(7);
        const raw = block.split("\n").filter(line => line.startsWith("data: ")).map(line => line.slice(6)).join("\n");
        if (event === "done") { doneSeen = true; return; }
        if (event && raw) consume(event, JSON.parse(raw));
      }
      if (done) { if (!doneSeen) throw new Error("连接提前结束，请检查执行状态后重试"); break; }
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}
async function refresh() {
  const data = await (await api("/api/bootstrap")).json(); state.token = data.token; state.sessions = data.sessions; state.chapters = data.chapters; state.config = data.model_config; sessionsUI();
}
function evidenceUI(row) {
  const el = node("details", "evidence-row " + (row.ok ? "passed" : "failed"));
  const count = row.observed?.before != null && row.observed?.after != null ? ` · ${row.observed.before} → ${row.observed.after}` : "";
  el.append(node("summary", "", (row.ok ? "✓ " : "✗ ") + row.label + count), node("pre", "", JSON.stringify(row.observed, null, 2)), node("small", "", row.source));
  $("evidence").append(el);
}
function lessonUI(evidence = []) {
  const select = $("chapter-select"); select.replaceChildren();
  for (const item of state.chapters) { const option = node("option", "", item.id + " · " + item.title); option.value = item.id; select.append(option); }
  select.value = state.chapter;
  const item = state.chapters.find(c => c.id === state.chapter); if (!item) return;
  $("lesson-title").textContent = item.title; $("lesson-focus").textContent = item.focus;
  $("lesson-kind").textContent = {guide:"文档导览",lab:"互动实验",conversation:"当前对话"}[item.presentation];
  $("lesson-docs").replaceChildren();
  for (const kind of item.documents) { const a = node("a", "", kind === "translation" ? "阅读精译 ↗" : "阅读源码讲解 ↗"); a.href = `/docs/${item.id}/${kind}`; a.target = "_blank"; a.rel = "noopener"; $("lesson-docs").append(a); }
  $("evidence").replaceChildren(); evidence.forEach(evidenceUI);
  $("prompt").placeholder = item.prompt;
  const architecture = item.presentation === "guide";
  $("run-lesson").hidden = architecture || item.presentation === "lab";
  document.querySelector(".workbench").hidden = architecture || (item.presentation === "lab" && state.mode === "offline");
  $("prompt-inspector").hidden = item.id !== "03";
  document.querySelector("main").classList.toggle("prompt-mode", item.id === "03");
  document.querySelector(".mode-bar").hidden = architecture;
  if (architecture) $("lesson-focus").textContent = item.id === "01" ? "本章是架构地图，无需运行演示。" : item.id.startsWith("07") ? "本章依赖网关消息入口与多会话生命周期，保留文档导览，不把 Web 对话冒充网关。" : "本章主要讲架构归属与集成边界，没有独立的运行演示。";
  $("lab-panel").hidden = item.presentation !== "lab";
  $("chapter-inspector").hidden = !["02","06","13","15"].includes(item.id);
  document.querySelector("main").classList.toggle("detail-mode", ["02","06","13","15"].includes(item.id));
  if (item.presentation === "lab") labControls(item.id);
  if (["02","06","13","15"].includes(item.id)) refreshChapter();
  if (item.id === "03") promptRequestsUI();
}
const chapterTitles = {"02":"一次任务怎样变成多次模型请求", "06":"这段对话实际怎样存进数据库", "13":"技能正文在什么时候进入模型上下文", "15":"观察者看到了什么、怎样关联事件"};
function detailBlock(title, value, explanation = "", open = false) {
  const block = node("details", "process-block"); block.open = open;
  block.append(node("summary", "", title));
  if (explanation) block.append(node("p", "prompt-intro", explanation));
  block.append(node("pre", "", typeof value === "string" ? value : JSON.stringify(value, null, 2)));
  return block;
}
function control(label, values, id) {
  const wrapper = node("div", "lab-field"), caption = node("label", "", label); caption.htmlFor=id; const select = node("select"); select.id = id;
  for (const [value,text] of values) { const option = node("option", "", text); option.value = value; select.append(option); }
  wrapper.append(caption,select); return wrapper;
}
function actionButton(label, action, target) {
  const button = node("button", "send-button", label); button.type="button";
  button.addEventListener("click", action); target.append(button); return button;
}
async function chapterAction(data) {
  const sid = state.session.id;
  const result = await (await api("/api/chapter-action", {session_id:sid, ...data})).json();
  if (sid === state.session?.id) { if(result.lab) {state.session.title=result.lab.title; sessionsUI();} renderChapter(result); }
  return result;
}
function labControls(chapter) {
  $("lab-controls").replaceChildren(); $("lab-result").replaceChildren(node("p","prompt-intro","调整参数后执行操作；每次结果均来自真实模块。"));
  const box = $("lab-controls");
  const titles = {"04":"注册 ≠ 模型可见 ≠ 实际执行", "05":"从完整对话到摘要：保留了什么", "08":"同一 Agent 遇到 400、429、503", "10":"一步一步更新滚动摘要", "11":"数据库从健康到降级，再显式修复", "14":"同一份输入，不同引擎策略"};
  $("lab-title").textContent=titles[chapter];
  const add=(label,values,id)=>box.append(control(label,values,id));
  if(chapter==="04") {
    add("工具集",[["file","file：文件工具"],["default","default：默认工具集"]],"lab-toolset");
    add("请求工具",[["read_file","读取实验文件"],["write_file","写入实验文件"],["list_dir","列实验目录"],["terminal","terminal（只展示闸门，不执行）"]],"lab-tool");
    const label=node("label","lab-field","写入内容"); const input=node("input");input.id="lab-text";input.maxLength=1000;input.value="这是浏览器写入的实验内容。";label.append(input);box.append(label);
  }
  if(chapter==="05" || chapter==="10") {
    add("上下文窗口",[[16000,"16000"],[4000,"4000"],[64000,"64000"]],"lab-window");
    add("头部保护条数",[[1,"1"],[2,"2"],[3,"3"]],"lab-first");
    add("尾部保护条数",[[2,"2"],[4,"4"],[8,"8"]],"lab-last");
  }
  if(chapter==="08") add("注入 HTTP 状态",[[429,"429 限流"],[400,"400 请求错误"],[503,"503 上游故障"]],"lab-status");
  if(chapter==="14") add("引擎",[["compressor","内置 compressor"],["recent-turns","实验插件 recent-turns"],["unknown","未知引擎 → 警告回落"]],"lab-engine");
  const buttons={"04":[["查看注册与 schema","inspect"],["尝试执行选中工具","execute"]],"05":[["生成/重置长对话","reset"],["按阈值压缩","compress"]],"08":[["运行一次故障回合","run"]],"10":[["生成/重置长对话","reset"],["吸收下一个交换","step"]],"11":[["只读体检","inspect"],["注入 FTS 降级标记","degrade"],["显式备份修复","repair"]],"14":[["选择引擎并执行同一份输入","select"]]};
  for(const [label,action] of buttons[chapter]) actionButton(label,async()=>{
    const sid=state.session.id; const data={action};
    if(chapter==="04") Object.assign(data,{toolset:$("lab-toolset").value,tool:$("lab-tool").value,text:$("lab-text").value});
    if(chapter==="05" || chapter==="10") Object.assign(data,{window:Number($("lab-window").value),protect_first:Number($("lab-first").value),protect_last:Number($("lab-last").value)});
    if(chapter==="08") data.status=Number($("lab-status").value);
    if(chapter==="14") data.engine=$("lab-engine").value;
    setBusy(true,"实验运行中");
    try { await chapterAction(data); } catch(error){notice(error.message);}
    finally { if(state.session.id===sid)setBusy(false); }
  },box);
  refreshChapter();
}
function rowTable(rows, columns) {
  const wrap=node("div","data-table-wrap"), table=node("table","data-table"), head=node("tr");
  for(const [key,label] of columns)head.append(node("th","",label));
  table.append(head);
  for(const row of rows){const tr=node("tr"); for(const [key,label] of columns)tr.append(node("td","",row[key] == null ? "—" : String(row[key])));table.append(tr);}
  wrap.append(table);return wrap;
}
function renderLab(report) {
  if(!report)return;
  $("lab-result").replaceChildren(node("p","prompt-comparison",`实际操作：${report.action} · ${report.duration_ms} ms`));
  $("lab-result").append(detailBlock("本次参数与实验说明",report.parameters,"参数只影响隔离实验，不修改你的配置。"));
  if(report.metrics) {
    const m=report.metrics; $("lab-result").append(node("p","prompt-comparison",`消息 ${m.before_messages} → ${m.after_messages} · 摘要调用 ${m.summary_calls} 次 · 实际动作 ${m.action} · 工具配对错误 ${m.tool_pair_errors.length}`), detailBlock("完整实测指标",m,"摘要调用只在压缩器确实请求时记录。"));
  }
  if(report.chapter==="04"){
    const registered=report.steps[0].value, defs=report.steps[1].value;
    $("lab-result").append(node("p","prompt-comparison",`已注册 ${registered.length} 个工具 · 本次模型可见 ${defs.length} 个 schema（可能含 Agent 级工具）`), rowTable(defs.map(d=>({name:d.function.name,registered:registered.includes(d.function.name)?"普通工具注册表":"Agent 级工具",description:d.function.description})),[["name","模型可见名称"],["registered","归属"],["description","说明"]]));
  }
  if(report.chapter==="08"){
    $("lab-result").append(node("p","prompt-comparison",report.steps[1].value.map(row=>`${row.model} → HTTP ${row.injected_http_status}`).join("  →  ")));
  }
  if(report.message_map){
    const old=node("details","process-block"), current=node("details","process-block"); current.open=true;
    old.append(node("summary","",`${report.message_map.length} 条原始消息分别去了哪里`),rowTable(report.message_map,[["index","原序号"],["role","角色"],["state","去向"],["preview","原文片段（完整内容见下方）"]]));
    current.append(node("summary","",`${report.context_map.length} 条操作后的上下文结构`),rowTable(report.context_map,[["index","新序号"],["role","角色"],["state","类型"],["preview","实际内容片段"]]));
    $("lab-result").append(old,current);
  }
  report.steps.forEach((step,index)=>$("lab-result").append(detailBlock(`${index+1}. ${step.title}`,step.value,step.explanation,index===1 && !["04","05","08","10"].includes(report.chapter))));
}
function scheduleChapter(){
  if(!["02","06","13","15"].includes(state.chapter))return;
  clearTimeout(scheduleChapter.timer);scheduleChapter.timer=setTimeout(()=>refreshChapter(),150);
}
async function refreshChapter(){
  if(!state.session)return;
  const sid=state.session.id, version=++state.chapterView;
  try {
    const data=await(await api(`/api/chapter/${sid}`)).json();
    if(version!==state.chapterView||sid!==state.session?.id)return;
    renderChapter(data);
  }catch(error){notice(error.message);}
}
async function renderRequest(id, chapter) {
  const sid=state.session.id; state.chapterChoice=id;
  try {
    const row=await(await api(`/api/prompts/${sid}/${id}`)).json();
    if(sid!==state.session?.id||id!==state.chapterChoice)return;
    const content=$("chapter-content");content.replaceChildren();
    const reply=row.response?.choices?.[0]?.message;
    const calls=reply?.tool_calls||[];
    if(chapter==="13")content.append(detailBlock("1. 这次请求中的系统提示词与技能索引",row.system,"技能索引一般只有名字和描述；正文需要通过 skill_view 读取。这里展示实际系统消息。"));
    else content.append(detailBlock("1. 送给模型的完整 messages",row.messages,`对话 ${row.turn} · 请求 ${row.iteration}；工具结果只有回填到 messages，模型下一次才能看见。`));
    content.append(detailBlock(chapter==="13"?"2. 模型是否请求读取技能正文":"2. 模型实际返回的决策",reply||row.transport_error||"当前请求尚未完成",calls.length?"由实际模型响应中的 tool_calls 触发。":"没有 tool_calls 时，模型可能已经给出最终回答，也可能请求失败。",true));
    const list=state.session.prompt_requests||[], index=list.findIndex(item=>item.id===id), next=list[index+1];
    if(calls.length && next && next.turn===row.turn){
      const later=await(await api(`/api/prompts/${sid}/${next.id}`)).json();
      if(sid!==state.session?.id||id!==state.chapterChoice)return;
      const ids=new Set(calls.map(c=>c.id)); const tools=later.messages.filter(m=>m.role==="tool"&&ids.has(m.tool_call_id));
      content.append(detailBlock("3. 实际进入下一次请求的工具结果",tools,"用 tool_call_id 关联工具调用和返回；这里只展示确实进入后续请求的内容。",true));
      content.append(detailBlock("4. 下一次请求新增/保留的完整上下文",later.messages,"后续决策来自含工具结果的新请求，而不是模型自行知道了文件或技能内容。"));
    } else content.append(node("p","prompt-intro",calls.length?"尚未在下一次模型请求中观察到工具回填；运行中或中断时可能停在这里。":"这次没有新的工具调用。实际回答在左侧对话中，旧请求仍可回看。"));
  }catch(error){notice(error.message);}
}
function renderChapter(data){
  if(data.chapter!==state.chapter)return;
  if(data.lab)renderLab(data.lab);
  if(!chapterTitles[data.chapter])return;
  $("chapter-inspector-title").textContent=chapterTitles[data.chapter];
  const controls=$("chapter-controls"), content=$("chapter-content"); controls.replaceChildren();
  if(["02","13"].includes(data.chapter)){
    $("chapter-inspector-intro").textContent=data.chapter==="02"?"依次查看实际输入、模型决策、工具结果和下一次输入。一次用户任务可能包含多次模型请求。":"先看请求里的技能索引，再看 skill_view 的调用、正文返回，以及正文何时出现在下一次请求中。";
    const rows=state.session.prompt_requests||[],turns=[...new Set(rows.map(row=>row.turn))];
    const select=node("select");select.id="chapter-request-select"; select.setAttribute("aria-label","选择本章模型请求");
    for(const row of rows){const option=node("option","",`对话 ${turns.indexOf(row.turn)+1} · 请求 ${row.iteration} · ${row.model}`);option.value=row.id;select.append(option);}
    controls.append(select);
    if(!rows.length){content.replaceChildren(node("p","prompt-intro","发送一条任务后，这里展示实际请求。"));return;}
    const selected=rows.some(row=>row.id===state.chapterChoice)?state.chapterChoice:rows[rows.length-1].id;
    select.value=selected; select.addEventListener("change",()=>renderRequest(select.value,data.chapter));renderRequest(selected,data.chapter);
  } else if(data.chapter==="06"){
    $("chapter-inspector-intro").textContent="发送消息后，直接查看当前会话的 SQLite 行。丢弃 Agent 内存再从库里恢复，然后继续追问，观察同一会话如何续接。";
    actionButton("读取当前库",()=>chapterAction({action:"snapshot"}).catch(e=>notice(e.message)),controls);
    actionButton("丢弃 Agent 内存并从库恢复",()=>chapterAction({action:"restore"}).catch(e=>notice(e.message)),controls);
    const query=node("input");query.placeholder="当前会话搜索关键词";query.setAttribute("aria-label","会话搜索关键词");query.maxLength=200; controls.append(query);
    actionButton("搜索记录",()=>chapterAction({action:"search",query:query.value}).catch(e=>notice(e.message)),controls);
    content.replaceChildren(); const storage=data.storage;
    if(!storage){content.append(node("p","prompt-intro","先发送一条任务，生成当前会话的真实消息行。"));return;}
    content.append(node("p","prompt-comparison",`session_id：${storage.session_id} · 数据库 ${storage.rows.length} 行 · 可恢复上下文 ${storage.restored.length} 条${storage.memory_reloaded?" · Agent 内存已丢弃，历史已从库恢复":""}`));
    content.append(rowTable(storage.rows.map(row=>({...row,preview:String(row.content||row.tool_calls||"").slice(0,90)})),[["id","行 ID"],["role","角色"],["active","active"],["compacted","compacted"],["preview","内容片段"]]));
    content.append(detailBlock("SQLite 原始行：id / role / active / compacted / tool_call_id",storage.rows,"这些是当前会话真实数据库记录，system 不在消息表中。",true),detailBlock("恢复后可交给 Agent 的历史",storage.restored,"从库恢复使用活跃消息，字段形状与模型协议一致。"),detailBlock("会话元数据",storage.metadata,"系统提示词以哈希关联去重表；会话身份与消息行分开保存。"));
    if(storage.search)content.prepend(detailBlock("搜索结果与实际检索路径",storage.search,"只展示当前会话的匹配；route 表示 FTS 或降级搜索路径。",true));
  } else {
    $("chapter-inspector-intro").textContent="观察者接收的是经过脱敏和大小限制的事件副本。下面按实际发生顺序展示事件及关联 ID；未发生的错误事件不会补成成功动画。";
    actionButton(data.observer_fault?"关闭失败观察者":"追加一个会抛异常的观察者",()=>chapterAction({action:"observer",enabled:!data.observer_fault}).catch(e=>notice(e.message)),controls);
    content.replaceChildren(node("p","prompt-comparison",`累计观察到 ${data.events.length} 个事件；失败观察者被调用 ${data.observer_faults} 次${data.dropped_events?`；较早 ${data.dropped_events} 个事件已因内存上限移除`:""}。开关将在下一次发送任务时生效。`));
    const turns=[...new Set(data.events.map(e=>e.turn_id))];
    for(const turn of turns){
      const rows=data.events.filter(e=>e.turn_id===turn);
      const group=node("details","event-group");group.open=turn===turns[turns.length-1];group.append(node("summary","",`对话 ${turns.indexOf(turn)+1} · ${rows.length} 个事件 · ${turn}`));
      for(const event of rows)group.append(detailBlock(labels[event.event]||event.event,event,`event=${event.event} · api_request_id=${event.api_request_id||"—"} · tool_call_id=${event.tool_call_id||"—"}`));
      content.append(group);
    }
  }
}
const layerLabels = {stable: ["稳定层 stable", "身份与工具使用指导；按变化频率排在最前。"], context: ["项目上下文层 context", "项目规则、工作区快照和相关平台提示；具体归属取决于装配条件。"], volatile: ["重建时可能变化的层 volatile", "技能索引、记忆、会话时间与运行环境。此层也随整份系统提示词缓存，不会每轮刷新。"]};
async function loadPrompt(id) {
  const version = ++state.promptLoad, sid = state.session.id;
  state.promptSelection = id;
  try {
    const data = await (await api(`/api/prompts/${sid}/${id}`)).json();
    if (version !== state.promptLoad || state.session.id !== sid) return;
    $("prompt-layers").replaceChildren();
    for (const layer of data.layers) {
      const card = node("details", "prompt-layer"); card.open = layer.name === "context";
      const [title, description] = layerLabels[layer.name];
      card.append(node("summary", "", title + ` · ${layer.bytes} 字节`), node("p", "", description), node("small", "", "SHA-256 " + layer.sha256.slice(0,16)), node("pre", "", layer.text || "（本次没有此层内容，不插入额外空行）"));
      $("prompt-layers").append(card);
    }
    const temp = node("details", "prompt-layer temporary");
    temp.append(node("summary", "", "本次临时指导 · 只影响请求副本"), node("pre", "", data.temporary || "（本次没有额外临时指导）"));
    $("prompt-layers").append(temp);
    const c = data.comparison;
    $("prompt-inspector").dataset.request = data.id;
    $("prompt-comparison").textContent = !data.mapped ? "当前系统消息无法完整对应缓存分层，下面保留实际消息，不猜测分层。" : c
      ? `与上一次模型请求相比：系统消息${c.system_equal ? "逐字节相同" : "发生变化"}；公共前缀 ${c.system_prefix_bytes} 字节；消息数量 ${c.previous_messages} → ${c.current_messages}。` + Object.entries(c.layers_equal).map(([name,equal]) => `${layerLabels[name][0]}${equal ? "未变" : "变化"}`).join("；") + `；临时指导${c.temporary_equal ? "未变" : "变化"}。`
      : `这是本会话第一次模型请求。系统消息 ${data.system_fingerprint.bytes} 字节，含 ${data.messages.length} 条消息；继续追问后可逐次比较。`;
    $("prompt-system").textContent = data.system;
    $("prompt-raw").textContent = JSON.stringify(data.messages, null, 2);
    $("prompt-message-list").replaceChildren();
    data.messages.forEach((m,index) => $("prompt-message-list").append(node("span", "message-role", `${index+1}. ${m.role}${m.tool_calls ? " → 工具调用" : ""}`)));
    $("prompt-explanation").textContent = data.explanation;
  } catch (error) { if (version === state.promptLoad) notice(error.message); }
}
function promptRequestsUI(selected) {
  const rows = state.session?.prompt_requests || [], select = $("prompt-request-select");
  select.replaceChildren();
  const turns = [...new Set(rows.map(row => row.turn))];
  for (const row of rows) { const option = node("option", "", `对话 ${turns.indexOf(row.turn)+1} · 模型请求 ${row.iteration} · ${row.model}`); option.value = row.id; select.append(option); }
  select.disabled = rows.length === 0;
  if (!rows.length) { ++state.promptLoad; state.promptSelection = null; $("prompt-layers").replaceChildren(); $("prompt-system").textContent = ""; $("prompt-raw").textContent = ""; $("prompt-message-list").replaceChildren(); $("prompt-comparison").textContent = "发送一条消息后查看分层提示词；在同一会话继续追问，对比每次模型请求。"; return; }
  const id = rows.some(row => row.id === selected) ? selected : rows[rows.length-1].id;
  select.value = id; loadPrompt(id);
}
$("prompt-request-select").addEventListener("change", () => loadPrompt($("prompt-request-select").value));
$("chapter-select").addEventListener("change", async () => {
  state.chapter = $("chapter-select").value;
  try { await createSession(); } catch (error) { state.chapter = state.session.chapter; lessonUI(state.session.evidence); notice(error.message); }
});
$("run-lesson").addEventListener("click", () => {
  const item = state.chapters.find(c => c.id === state.chapter);
  if (item && !state.busy) { $("prompt").value = item.prompt; $("composer").requestSubmit(); }
});
$("model-settings").addEventListener("click", () => {
  $("api-key").value = ""; $("model-name").value = state.config.model;
  $("model-config-status").textContent = state.config.configured ? "Web 密钥已在服务内存中；不回显，未自动验证。" : "尚未配置 Web 密钥。";
  $("model-dialog").showModal();
});
$("close-model").addEventListener("click", () => $("model-dialog").close());
$("model-dialog").addEventListener("close", () => { $("api-key").value = ""; });
async function saveModel(clear = false) {
  const key = $("api-key").value.trim(); const model = $("model-name").value.trim();
  $("api-key").value = "";
  const data = {model, clear}; if (key && !clear) data.api_key = key;
  try {
    state.config = await (await api("/api/model-config", data)).json();
    $("model-dialog").close(); notice(clear ? "Web 密钥已清除，真实模式恢复后端配置" : "设置已保存到服务内存；发送任务时验证连接");
  } catch (error) { notice(error.message); }
}
$("model-form").addEventListener("submit", event => { event.preventDefault(); saveModel(); });
$("clear-model").addEventListener("click", () => saveModel(true));
$("composer").addEventListener("submit", async (event) => {
  event.preventDefault(); const prompt = $("prompt").value.trim(); if (!prompt || state.busy) return;
  try {
    if (!state.session) await createSession();
    state.started = performance.now(); state.stopped = false; resetTrace(); setBusy(true);
    message("user", prompt); state.session.messages.push({role: "user", content: prompt});
    $("prompt").value = "";
    await stream(await api("/api/turn", {session_id: state.session.id, message: prompt}));
  } catch (error) { notice(error.message); $("run-status").textContent = "运行失败"; }
  finally {
    state.approval = null;
    if ($("approval-dialog").open) $("approval-dialog").close();
    setBusy(false, $("run-status").textContent === "运行中" ? "已结束" : $("run-status").textContent);
    await refresh().catch(error => notice(error.message));
    const updated = state.sessions.find(s => s.id === state.session?.id);
    if (updated) { state.session = updated; renderMessages(updated); sessionsUI(); if (updated.chapter === "03") promptRequestsUI(state.promptSelection); else if (["02","06","13","15"].includes(updated.chapter)) refreshChapter(); }
    $("prompt").focus();
  }
});
$("prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("composer").requestSubmit(); }
});
$("new-session").addEventListener("click", () => createSession().catch(error => notice(error.message)));
for (const mode of ["offline", "live"]) $("mode-" + mode).addEventListener("click", () => {
  if (state.mode !== mode) createSession(mode).catch(error => notice(error.message));
});
$("stop").addEventListener("click", async () => {
  try { await api("/api/stop", {session_id: state.session.id}); state.stopped = true; $("run-status").textContent = "正在停止";
 }
  catch (error) { notice(error.message); }
});
$("approval-dialog").addEventListener("close", async () => {
  const approvalId = state.approval;
  state.approval = null;
  if (!approvalId || !state.session) return;
  try { await api("/api/approve", {session_id: state.session.id, approval_id: approvalId,
    allowed: $("approval-dialog").returnValue === "allow"}); }
  catch (error) { notice(error.message); }
});
(async () => {
  try {
    await refresh();
    if (state.sessions.length) selectSession(state.sessions[state.sessions.length - 1]);
    else await createSession("offline");
  } catch (error) { notice(error.message); $("run-status").textContent = "连接失败"; }
})();
