"use strict";
const $ = (id) => document.getElementById(id);
const state = {token: "", sessions: [], session: null, mode: "offline", busy: false, events: 0,
  promptSelection: null, promptLoad: 0, chapters: [], chapter: "02", config: {}, tools: 0, started: 0, usage: false, stopped: false, approval: null};
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
    promptRequestsUI(data.id);
  } else if (kind === "evidence") {
    evidenceUI(data);
    trace(data.ok ? "本章观察已验证" : "本章观察未满足", data.label, data, data.ok ? "success" : "error");
  } else if (kind === "lesson") {
    $("lesson-title").textContent = data.title;
  } else if (kind === "trace") {
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
  $("lesson-kind").textContent = {guide: "文档导览", mechanism: "机制实验", agent: "Agent 实跑"}[item.kind];
  $("lesson-docs").replaceChildren();
  for (const kind of item.documents) { const a = node("a", "", kind === "translation" ? "阅读精译 ↗" : "阅读源码讲解 ↗"); a.href = `/docs/${item.id}/${kind}`; a.target = "_blank"; a.rel = "noopener"; $("lesson-docs").append(a); }
  $("evidence").replaceChildren(); evidence.forEach(evidenceUI);
  $("prompt").placeholder = item.prompt;
  const architecture = item.id === "01";
  $("run-lesson").hidden = architecture;
  document.querySelector(".workbench").hidden = architecture;
  $("prompt-inspector").hidden = item.id !== "03";
  document.querySelector("main").classList.toggle("prompt-mode", item.id === "03");
  if (architecture) $("lesson-focus").textContent = "本章是架构地图：阅读精译了解入口、Agent、工具与存储之间的关系，无需运行演示。";
  if (item.id === "03") promptRequestsUI();
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
    if (updated) { state.session = updated; renderMessages(updated); sessionsUI(); if (updated.chapter === "03") promptRequestsUI(state.promptSelection); }
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
