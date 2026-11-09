"use strict";

const agentState = { workspaces: [], connection: null, contextVersion: 0, enabledFingerprint: null, taskId: null, busy: false, enabling: false };
const agentElement = (id) => document.getElementById(id);
const agentDisplay = (element, visible) => element.classList.toggle("hidden", !visible);

async function agentJson(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { Accept: "application/json", ...(options.headers || {}) } });
  let body = null;
  try { body = await response.json(); } catch (_) {}
  if (!response.ok) {
    const detail = Array.isArray(body?.detail) ? body.detail.map((item) => item.msg).join("；") : body?.detail;
    const error = new Error(detail || `请求失败（HTTP ${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return body;
}

function agentMessage(text, error = false) {
  const element = agentElement("agent-message");
  element.textContent = text;
  element.classList.toggle("error", error);
  agentDisplay(element, Boolean(text));
}

function fingerprint() {
  const workspaceId = agentElement("agent-workspace").value;
  const item = agentState.connection;
  return item ? `${workspaceId}|${item.id}|${item.updated_at}|${item.base_url}` : "";
}

async function initializeAgent() {
  try {
    const [workspaces, connections] = await Promise.all([agentJson("/api/v1/workspaces"), agentJson("/api/v1/model-connections")]);
    agentState.workspaces = workspaces;
    agentState.connection = connections.items.find((item) => item.id === connections.active_connection_id) || null;
    const select = agentElement("agent-workspace");
    const previous = select.value;
    select.replaceChildren();
    for (const workspace of workspaces) {
      const option = document.createElement("option"); option.value = workspace.workspace_id; option.textContent = workspace.name; select.appendChild(option);
    }
    if (!workspaces.length) { const option = document.createElement("option"); option.textContent = "尚未注册工作区"; option.value = ""; select.appendChild(option); }
    if (workspaces.some((workspace) => workspace.workspace_id === previous)) select.value = previous;
    if (agentState.enabledFingerprint !== fingerprint()) agentState.enabledFingerprint = null;
    renderAgentContext();
  } catch (error) { agentMessage(`无法加载问答上下文：${error.message}`, true); }
}

function renderAgentContext() {
  const item = agentState.connection;
  const enabled = Boolean(fingerprint()) && agentState.enabledFingerprint === fingerprint();
  agentElement("agent-connection").textContent = item ? `${item.name} · ${item.model}` : "尚未选择当前模型连接";
  agentElement("agent-destination").textContent = item ? `外发目的地：${item.base_url}（${item.protocol}）` : "请先在模型连接设置中选择当前配置。";
  agentElement("enable-cloud").disabled = !item || !agentElement("agent-workspace").value || agentState.enabling;
  agentElement("send-question").disabled = !enabled || agentState.busy;
  agentElement("enable-status").textContent = enabled ? "已对当前工作区、连接版本和地址启用。" : "尚未启用；更换工作区、连接版本或服务地址后需要重新启用。";
}

function changeAgentContext() {
  agentState.contextVersion += 1;
  agentState.taskId = null;
  agentState.busy = false;
  agentState.enabling = false;
  agentState.enabledFingerprint = null;
  agentDisplay(agentElement("task-status"), false);
  agentDisplay(agentElement("answer-card"), false);
  agentElement("source-dialog").close();
  agentMessage("");
  renderAgentContext();
}

async function enableCloud() {
  if (!agentState.connection || agentState.enabling || !agentElement("agent-workspace").value) return;
  const version = agentState.contextVersion;
  const workspaceId = agentElement("agent-workspace").value;
  const item = agentState.connection;
  agentState.enabling = true;
  renderAgentContext();
  try {
    const enabled = await agentJson("/api/v1/agent/cloud-enable", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: workspaceId, connection_id: item.id, connection_version: item.updated_at }) });
    if (version !== agentState.contextVersion) return;
    if (enabled.connection_version !== item.updated_at || enabled.base_url !== item.base_url || agentState.connection !== item) return;
    agentState.enabledFingerprint = fingerprint();
    agentMessage("已启用当前外发目的地；尚未发送问题或材料。", false);
  } catch (error) {
    if (version !== agentState.contextVersion) return;
    if (error.status === 409 || error.status === 404) await initializeAgent();
    if (version === agentState.contextVersion) agentMessage(`启用失败：${error.message}`, true);
  } finally {
    if (version === agentState.contextVersion) {
      agentState.enabling = false;
      renderAgentContext();
    }
  }
}

async function sendQuestion() {
  if (agentState.busy || !fingerprint() || agentState.enabledFingerprint !== fingerprint()) return;
  const question = agentElement("agent-question").value.trim();
  if (!question) { agentMessage("请输入问题。", true); return; }
  const version = agentState.contextVersion;
  const workspaceId = agentElement("agent-workspace").value;
  const item = agentState.connection;
  agentState.busy = true;
  agentState.taskId = null;
  agentDisplay(agentElement("task-status"), false);
  agentDisplay(agentElement("answer-card"), false);
  agentMessage("正在提交问题…");
  renderAgentContext();
  try {
    const requestKey = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    const task = await agentJson("/api/v1/agent/tasks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: workspaceId, question, request_key: requestKey, connection_id: item.id, connection_version: item.updated_at }) });
    if (version !== agentState.contextVersion || task.workspace_id !== workspaceId) return;
    agentState.taskId = task.task_id;
    renderTask(task);
    if (!["succeeded", "failed", "limit_reached"].includes(task.status)) await pollTask(task.task_id, version);
  } catch (error) {
    if (version !== agentState.contextVersion) return;
    if ([403, 404, 409].includes(error.status)) {
      agentState.enabledFingerprint = null;
      await initializeAgent();
    }
    if (version === agentState.contextVersion) agentMessage(`提交失败：${error.message}`, true);
  } finally {
    if (version === agentState.contextVersion) {
      agentState.busy = false;
      renderAgentContext();
    }
  }
}

async function pollTask(taskId, contextVersion) {
  try {
    while (agentState.taskId === taskId && agentState.contextVersion === contextVersion) {
      const task = await agentJson(`/api/v1/agent/tasks/${encodeURIComponent(taskId)}`);
      if (agentState.taskId !== taskId || agentState.contextVersion !== contextVersion) return;
      renderTask(task);
      if (["succeeded", "failed", "limit_reached"].includes(task.status)) return;
      await new Promise((resolve) => setTimeout(resolve, 700));
    }
  } catch (error) { if (agentState.taskId === taskId && agentState.contextVersion === contextVersion) agentMessage(`任务状态获取失败：${error.message}`, true); }
}

function renderTask(task) {
  agentMessage("");
  agentDisplay(agentElement("task-status"), true);
  const labels = { queued: "等待执行", running: "执行中", succeeded: "成功", failed: "失败", limit_reached: "达到限制而停止" };
  agentElement("task-status-title").textContent = labels[task.status] || task.status;
  const events = agentElement("task-events"); events.replaceChildren();
  for (const event of task.events) { const line = document.createElement("li"); line.textContent = event.message; events.appendChild(line); }
  if (task.status === "failed" || task.status === "limit_reached") agentMessage(task.error_message || "任务未完成。", true);
  if (task.status !== "succeeded") return;
  agentElement("agent-answer").textContent = task.answer || "";
  const citations = agentElement("agent-citations"); citations.replaceChildren();
  for (const source of task.citations) {
    const button = document.createElement("button"); button.type = "button"; button.className = "secondary-button";
    button.textContent = `${source.source_id} · ${source.relative_path} · ${source.location}`;
    button.addEventListener("click", () => showSource(task.task_id, source.source_id, agentState.contextVersion));
    citations.appendChild(button);
  }
  agentDisplay(agentElement("answer-card"), true);
}

async function showSource(taskId, sourceId, contextVersion) {
  try {
    const source = await agentJson(`/api/v1/agent/tasks/${encodeURIComponent(taskId)}/sources/${encodeURIComponent(sourceId)}`);
    if (contextVersion !== agentState.contextVersion || taskId !== agentState.taskId) return;
    agentElement("source-meta").textContent = `${source.relative_path} · ${source.location} · SHA-256 ${source.version}`;
    agentElement("source-text").textContent = source.excerpt;
    agentElement("source-dialog").showModal();
  } catch (error) {
    if (contextVersion === agentState.contextVersion && taskId === agentState.taskId) {
      agentMessage(`无法取得回答时原文：${error.message}`, true);
    }
  }
}

document.addEventListener("DOMContentLoaded", () => {
  agentElement("agent-workspace").addEventListener("change", changeAgentContext);
  agentElement("enable-cloud").addEventListener("click", enableCloud);
  agentElement("send-question").addEventListener("click", sendQuestion);
  agentElement("close-source").addEventListener("click", () => agentElement("source-dialog").close());
  initializeAgent();
});
