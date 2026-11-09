"use strict";

const protocolLabels = {
  openai_chat_completions: "OpenAI兼容 Chat Completions",
  openai_responses: "OpenAI Responses",
  anthropic_messages: "Anthropic Messages",
};

const connectionState = {
  items: [],
  activeId: null,
  editingId: null,
  editVersion: 0,
  listVersion: 0,
  mutationVersion: 0,
  writes: [],
  pending: new Set(),
  testResults: new Map(),
  deleteTarget: null,
};

const connectionById = (id) => document.getElementById(id);
const display = (element, visible) => element.classList.toggle("hidden", !visible);

function queueConnectionWrite(write) {
  return new Promise((resolve, reject) => {
    const run = async () => {
      ++connectionState.mutationVersion;
      try { resolve(await write()); } catch (error) { reject(error); }
      finally {
        ++connectionState.mutationVersion;
        connectionState.writes.shift();
        connectionState.writes[0]?.();
      }
    };
    connectionState.writes.push(run);
    if (connectionState.writes.length === 1) run();
  });
}

async function modelRequestJson(url, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  const response = await fetch(url, { ...options, headers });
  if (response.status === 204) return null;
  let body = null;
  try { body = await response.json(); } catch (_) { /* handled below */ }
  if (!response.ok) {
    let detail = body && body.detail ? body.detail : `请求失败（HTTP ${response.status}）`;
    if (Array.isArray(detail)) detail = detail.map((item) => item.msg || "字段无效").join("；");
    const error = new Error(String(detail));
    error.status = response.status;
    throw error;
  }
  return body;
}

function showPageMessage(text, kind = "success") {
  const element = connectionById("page-message");
  element.textContent = text;
  element.classList.toggle("error", kind === "error");
  display(element, Boolean(text));
}

function setFormError(text) {
  const element = connectionById("form-error");
  element.textContent = text;
  display(element, Boolean(text));
}

function setPending(key, value) {
  if (value) connectionState.pending.add(key); else connectionState.pending.delete(key);
  renderConnectionList();
  const saving = connectionState.pending.has("save") || connectionState.pending.has("edit-load");
  connectionById("save-connection").disabled = saving;
  connectionById("save-connection").textContent = saving ? "正在保存…" : "保存配置";
}

function renderConnectionList() {
  const list = connectionById("connections-list");
  list.replaceChildren();
  display(connectionById("connections-empty"), connectionState.items.length === 0);
  const active = connectionState.items.find((item) => item.id === connectionState.activeId);
  connectionById("active-summary").textContent = active
    ? `当前使用：${active.name}。此选择只记录本机设置，不代表模型已连接。`
    : "当前没有选择模型连接。";

  for (const item of connectionState.items) {
    const article = document.createElement("article");
    article.className = "connection-item";

    const identity = document.createElement("div");
    const title = document.createElement("div");
    title.className = "connection-title";
    const name = document.createElement("strong");
    name.textContent = item.name;
    title.appendChild(name);
    if (item.is_active) {
      const badge = document.createElement("span");
      badge.className = "active-badge";
      badge.textContent = "当前使用";
      title.appendChild(badge);
    }
    const model = document.createElement("div");
    model.className = "connection-meta";
    model.textContent = `${protocolLabels[item.protocol] || item.protocol} · ${item.model}`;
    identity.append(title, model);

    const address = document.createElement("div");
    address.className = "connection-meta path-text";
    address.textContent = item.base_url;
    const keyState = document.createElement("div");
    keyState.className = "connection-meta";
    keyState.textContent = item.has_api_key ? "已保存密钥" : "未设置密钥";
    address.appendChild(keyState);

    const actions = document.createElement("div");
    actions.className = "connection-actions";
    const edit = actionButton("编辑", () => openEditor(item.id));
    const activate = actionButton(item.is_active ? "当前配置" : "设为当前", () => activateConnection(item.id));
    activate.disabled = item.is_active || connectionState.pending.has(`activate:${item.id}`);
    const remove = actionButton("删除", () => openDeleteDialog(item.id), "danger-button");
    remove.disabled = connectionState.pending.has(`delete:${item.id}`);
    const testChat = actionButton("测试普通对话", () => testConnection(item.id, "chat"));
    const testTool = actionButton("测试工具调用", () => testConnection(item.id, "tools"));
    testChat.disabled = connectionState.pending.has(`test:chat:${item.id}`);
    testTool.disabled = connectionState.pending.has(`test:tools:${item.id}`);
    actions.append(edit, activate, testChat, testTool, remove);
    for (const [kind, label] of [["chat", "普通对话"], ["tool", "工具调用"]]) {
      const result = connectionState.testResults.get(`${item.id}:${kind}`);
      if (!result || result.version !== item.updated_at) continue;
      const testStatus = document.createElement("div");
      testStatus.className = "connection-meta";
      testStatus.textContent = `${label}：${result.message}`;
      actions.appendChild(testStatus);
    }
    article.append(identity, address, actions);
    list.appendChild(article);
  }
}

function actionButton(text, handler, className = "secondary-button") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = text;
  button.addEventListener("click", handler);
  return button;
}

async function testConnection(connectionId, kind) {
  const item = connectionState.items.find((candidate) => candidate.id === connectionId);
  if (!item) return;
  const pendingKey = `test:${kind}:${connectionId}`;
  if (connectionState.pending.has(pendingKey)) return;
  setPending(pendingKey, true);
  try {
    const response = await modelRequestJson(
      `/api/v1/model-connections/${encodeURIComponent(connectionId)}/test-${kind === "tool" ? "tools" : "chat"}`,
      { method: "POST" },
    );
    const current = connectionState.items.find((candidate) => candidate.id === connectionId);
    if (!current || current.updated_at !== response.connection_version) return;
    connectionState.testResults.set(`${connectionId}:${kind}`, {
      version: response.connection_version,
      message: response.passed ? response.message : `未验证通过：${response.message}`,
      passed: response.passed,
    });
    renderConnectionList();
  } catch (error) {
    const current = connectionState.items.find((candidate) => candidate.id === connectionId);
    if (current?.updated_at === item.updated_at) {
      connectionState.testResults.set(`${connectionId}:${kind}`, { version: item.updated_at, message: `未验证通过：${error.message}` });
      renderConnectionList();
    }
  } finally {
    setPending(pendingKey, false);
  }
}

async function loadConnections() {
  const version = ++connectionState.listVersion;
  const mutationVersion = connectionState.mutationVersion;
  try {
    const response = await modelRequestJson("/api/v1/model-connections");
    if (version !== connectionState.listVersion || mutationVersion !== connectionState.mutationVersion || connectionState.writes.length) return false;
    connectionState.items = response.items;
    connectionState.activeId = response.active_connection_id;
    if (connectionState.editingId && !response.items.some((item) => item.id === connectionState.editingId)) {
      closeEditor();
    }
    renderConnectionList();
    return true;
  } catch (error) {
    if (version === connectionState.listVersion && mutationVersion === connectionState.mutationVersion) showPageMessage(`无法加载配置列表：${error.message}`, "error");
    return false;
  }
}

function clearForm() {
  connectionById("connection-name").value = "";
  connectionById("connection-protocol").value = "openai_chat_completions";
  connectionById("connection-base-url").value = "";
  connectionById("connection-model").value = "";
  connectionById("connection-api-key").value = "";
  connectionById("clear-api-key").checked = false;
  setFormError("");
}

function openCreateEditor() {
  ++connectionState.editVersion;
  connectionState.editingId = null;
  setPending("edit-load", false);
  clearForm();
  connectionById("editor-heading").textContent = "新增模型连接";
  connectionById("key-status").textContent = "未设置密钥。密钥只提交到系统凭据存储。";
  display(connectionById("clear-key-row"), false);
  display(connectionById("editor-card"), true);
}

async function openEditor(connectionId) {
  const version = ++connectionState.editVersion;
  connectionState.editingId = connectionId;
  clearForm();
  connectionById("editor-heading").textContent = "正在加载配置…";
  display(connectionById("editor-card"), true);
  setPending("edit-load", true);
  try {
    const item = await modelRequestJson(`/api/v1/model-connections/${encodeURIComponent(connectionId)}`);
    if (version !== connectionState.editVersion || connectionState.editingId !== connectionId) return;
    connectionById("editor-heading").textContent = `编辑：${item.name}`;
    connectionById("connection-name").value = item.name;
    connectionById("connection-protocol").value = item.protocol;
    connectionById("connection-base-url").value = item.base_url;
    connectionById("connection-model").value = item.model;
    connectionById("connection-api-key").value = "";
    connectionById("key-status").textContent = item.has_api_key
      ? "已保存密钥；留空将保留原密钥。"
      : "未设置密钥。";
    display(connectionById("clear-key-row"), item.has_api_key);
  } catch (error) {
    if (version === connectionState.editVersion && connectionState.editingId === connectionId) {
      setFormError(`无法加载配置：${error.message}`);
    }
  } finally {
    if (version === connectionState.editVersion) setPending("edit-load", false);
  }
}

function closeEditor() {
  ++connectionState.editVersion;
  connectionState.editingId = null;
  setPending("edit-load", false);
  clearForm();
  display(connectionById("editor-card"), false);
}

function isLoopback(hostname) {
  const host = hostname.toLocaleLowerCase();
  if (host === "localhost" || host === "[::1]" || host === "::1") return true;
  const parts = host.split(".");
  return parts.length === 4 && parts[0] === "127" && parts.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255);
}

function collectPayload() {
  const name = connectionById("connection-name").value.trim();
  const protocol = connectionById("connection-protocol").value;
  const baseUrl = connectionById("connection-base-url").value.trim();
  const model = connectionById("connection-model").value.trim();
  const apiKey = connectionById("connection-api-key").value;
  if (!name || name.length > 100) throw new Error("配置名称不能为空且不能超过 100 个字符。");
  if (!Object.hasOwn(protocolLabels, protocol)) throw new Error("请选择有效的接口协议。");
  if (!model || model.length > 200) throw new Error("模型名称不能为空且不能超过 200 个字符。");
  if (apiKey.length > 1280) throw new Error("API Key 长度超过限制。");
  let parsed;
  try { parsed = new URL(baseUrl); } catch (_) { throw new Error("API 基础地址必须是完整的 HTTP 或 HTTPS 地址。"); }
  if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) throw new Error("API 基础地址必须是完整的 HTTP 或 HTTPS 地址。");
  if (parsed.username || parsed.password || parsed.search || parsed.hash) throw new Error("API 基础地址不能包含账号、密码、查询参数或片段。");
  if (parsed.protocol === "http:" && !isLoopback(parsed.hostname)) throw new Error("HTTP 仅允许本机回环地址。");

  const payload = { name, protocol, base_url: baseUrl, model };
  if (apiKey) payload.api_key = apiKey;
  if (connectionState.editingId) payload.clear_api_key = connectionById("clear-api-key").checked;
  if (payload.api_key && payload.clear_api_key) throw new Error("不能同时提交新密钥和清除密钥请求。");
  return payload;
}

async function saveConnection(event) {
  if (event) event.preventDefault();
  if (connectionState.pending.has("save") || connectionState.pending.has("edit-load")) return;
  let payload;
  try { payload = collectPayload(); } catch (error) { setFormError(error.message); return; }
  const targetId = connectionState.editingId;
  const editorVersion = connectionState.editVersion;
  setFormError("");
  setPending("save", true);
  await queueConnectionWrite(async () => {
    try {
      const url = targetId ? `/api/v1/model-connections/${encodeURIComponent(targetId)}` : "/api/v1/model-connections";
      const saved = await modelRequestJson(url, {
        method: targetId ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      connectionState.items = [...connectionState.items.filter((item) => item.id !== saved.id), saved];
      connectionState.items.sort((left, right) => left.name.localeCompare(right.name));
      connectionState.testResults.delete(`${saved.id}:chat`);
      connectionState.testResults.delete(`${saved.id}:tool`);
      if (editorVersion === connectionState.editVersion && connectionState.editingId === targetId) {
        connectionById("connection-api-key").value = "";
        closeEditor();
      }
      showPageMessage("已保存，尚未验证连接。", "success");
    } catch (error) {
      if (editorVersion === connectionState.editVersion && connectionState.editingId === targetId) {
        setFormError(`保存失败：${error.message}`);
      } else {
        showPageMessage(`配置保存失败：${error.message}`, "error");
      }
    } finally {
      setPending("save", false);
    }
  });
}

async function activateConnection(connectionId) {
  const pendingKey = `activate:${connectionId}`;
  if (connectionState.pending.has(pendingKey)) return;
  setPending(pendingKey, true);
  await queueConnectionWrite(async () => {
    try {
      const response = await modelRequestJson(`/api/v1/model-connections/${encodeURIComponent(connectionId)}/activate`, { method: "PUT" });
      connectionState.activeId = response.id;
      connectionState.items = connectionState.items.map((item) => ({ ...item, is_active: item.id === response.id }));
      renderConnectionList();
      showPageMessage(`已将“${response.name}”设为当前配置；尚未验证连接。`, "success");
    } catch (error) {
      showPageMessage(`切换当前配置失败：${error.message}`, "error");
    } finally {
      setPending(pendingKey, false);
    }
  });
}

function openDeleteDialog(connectionId) {
  const item = connectionState.items.find((candidate) => candidate.id === connectionId);
  if (!item) return;
  connectionState.deleteTarget = item;
  connectionById("delete-message").textContent = `确定删除模型连接配置“${item.name}”吗？`;
  display(connectionById("delete-active-warning"), item.is_active);
  connectionById("delete-dialog").showModal();
}

function cancelDelete() {
  connectionState.deleteTarget = null;
  connectionById("delete-dialog").close();
}

async function confirmDelete() {
  const item = connectionState.deleteTarget;
  if (!item) return;
  const pendingKey = `delete:${item.id}`;
  if (connectionState.pending.has(pendingKey)) return;
  setPending(pendingKey, true);
  connectionById("confirm-delete").disabled = true;
  await queueConnectionWrite(async () => {
    try {
      await modelRequestJson(`/api/v1/model-connections/${encodeURIComponent(item.id)}`, { method: "DELETE" });
      connectionState.items = connectionState.items.filter((candidate) => candidate.id !== item.id);
      connectionState.testResults.delete(`${item.id}:chat`);
      connectionState.testResults.delete(`${item.id}:tool`);
      if (connectionState.activeId === item.id) connectionState.activeId = null;
      if (connectionState.editingId === item.id) closeEditor();
      if (connectionState.deleteTarget === item) cancelDelete();
      renderConnectionList();
      showPageMessage("配置及其已保存密钥已删除。", "success");
    } catch (error) {
      showPageMessage(`删除失败：${error.message}`, "error");
    } finally {
      connectionById("confirm-delete").disabled = false;
      setPending(pendingKey, false);
    }
  });
}

function initializeModelConnectionEvents() {
  connectionById("new-connection").addEventListener("click", openCreateEditor);
  connectionById("cancel-editor").addEventListener("click", closeEditor);
  connectionById("connection-form").addEventListener("submit", saveConnection);
  connectionById("cancel-delete").addEventListener("click", cancelDelete);
  connectionById("confirm-delete").addEventListener("click", confirmDelete);
}

document.addEventListener("DOMContentLoaded", () => {
  initializeModelConnectionEvents();
  loadConnections();
});
