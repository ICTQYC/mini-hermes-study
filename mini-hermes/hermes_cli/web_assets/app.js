"use strict";
const $ = (id) => document.getElementById(id);
const state = {token: "", sessions: [], session: null, mode: "offline", busy: false, events: 0,
  tools: 0, started: 0, usage: false, stopped: false, approval: null};
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
    ? "固定 provider + 真实 Agent 与工具，无需密钥或网络。"
    : "使用后端模型配置；工具按现有权限读写当前工作区。";
  $("mode-badge").textContent = mode === "offline" ? "DEMO" : "LIVE";
  $("usage-note").textContent = mode === "offline" ? "离线用量为演示估算；耗时为实际测量。" : "用量来自模型响应；未返回时显示 —。";
  $("model").textContent = mode === "offline" ? "scripted-offline" : "等待后端解析模型";
  $("provider").textContent = mode === "offline" ? "OFFLINE" : "LIVE";
  $("workspace").textContent = mode === "offline" ? "sample-project" : "当前工作区";
}
function resetTrace() {
  state.events = 0; state.tools = 0; state.usage = false;
  $("trace").replaceChildren(); $("event-count").textContent = "0 EVENTS";
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
  state.session = session; modeUI(session.mode); resetTrace();
  $("session-short").textContent = session.id.slice(0, 8);
  renderMessages(session);
  if (session.model) $("model").textContent = session.model;
  setBusy(Boolean(session.running), session.running ? "正在另一页面运行" : "就绪");
  sessionsUI();
  if (session.running) watchRunning(session.id);
}
async function createSession(mode = state.mode) {
  if (state.busy) return;
  const response = await api("/api/sessions", {mode});
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
    $("workspace").textContent = data.workspace; $("context-window").textContent = data.context_length?.toLocaleString() || "未配置";
    state.session.model = data.model;
    trace("回合开始", data.mode === "offline" ? "离线 provider · 真实 Agent 循环" : "真实模型 · 本地工具");
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
  const data = await (await api("/api/bootstrap")).json(); state.token = data.token; state.sessions = data.sessions; sessionsUI();
}
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
    if (updated) { state.session = updated; renderMessages(updated); sessionsUI(); }
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
