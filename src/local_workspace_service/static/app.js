"use strict";

const PAGE_SIZE = 50;
const reasonLabels = {
  ignored_directory: "目录命中忽略规则，已整体跳过",
  ignored_file_pattern: "名称命中敏感或忽略规则",
  extension_not_allowed: "文件类型不在允许范围内",
  file_too_large: "文件超过单文件大小限制",
  file_limit_exceeded: "已达到收录文件数量限制",
  link_not_followed: "符号链接、目录联接或重解析点未跟随",
  not_a_regular_file: "不是普通文件",
  access_error: "无法安全访问或读取元数据",
};
const entryTypeLabels = { file: "文件", directory: "目录", link: "链接", other: "其他" };

const state = {
  workspaces: [],
  workspace: null,
  latest: null,
  scanFailure: null,
  loading: false,
  loadVersion: 0,
  activeScans: new Set(),
  creatingWorkspace: false,
  workspaceListVersion: 0,
  activeTab: "included",
  filter: "",
  sortKey: "path",
  sortDirection: "asc",
  pages: { included: 1, excluded: 1, errors: 1 },
};

const byId = (id) => document.getElementById(id);

async function requestJson(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { Accept: "application/json", ...(options.headers || {}) } });
  let body = null;
  try { body = await response.json(); } catch (_) { /* A non-JSON failure gets a generic message. */ }
  if (!response.ok) {
    let detail = body && body.detail ? body.detail : `请求失败（HTTP ${response.status}）`;
    if (Array.isArray(detail)) detail = detail.map((item) => item.msg || "字段无效").join("；");
    const error = new Error(String(detail));
    error.status = response.status;
    throw error;
  }
  return body;
}

function setText(id, value) { byId(id).textContent = value; }
function show(element, visible) { element.classList.toggle("hidden", !visible); }

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" }).format(date);
}

function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  const digits = index === 0 ? 0 : value >= 10 ? 1 : 2;
  return `${value.toFixed(digits)} ${units[index]}`;
}

function appendCell(row, text, className = "") {
  const cell = document.createElement("td");
  cell.textContent = text;
  if (className) cell.className = className;
  row.appendChild(cell);
  return cell;
}

function addAlert(text, kind = "info") {
  const alert = document.createElement("div");
  alert.className = `alert ${kind}`;
  alert.textContent = text;
  byId("alerts").appendChild(alert);
}

function renderWorkspaceInfo() {
  const workspace = state.workspace;
  if (!workspace) return;
  setText("workspace-name", workspace.name);
  setText("workspace-root", workspace.root_path);
  setText("workspace-extensions", workspace.allowed_extensions.join("、"));

  if (!state.latest || state.latest.status === "never_scanned") {
    setText("last-scan-time", state.latest ? "尚未扫描" : "—");
    setText("update-status", state.loading ? "正在加载" : state.latest ? "需要首次扫描" : "扫描状态未知");
    return;
  }
  setText("last-scan-time", formatDate(state.latest.result.scanned_at));
  if (state.latest.needs_rescan) {
    setText("update-status", "扫描规则已变化，需要更新");
  } else {
    setText("update-status", "规则未变化；本地文件变化未知");
  }
}

function renderSummary() {
  const summary = state.latest && state.latest.result ? state.latest.result.summary : null;
  setText("included-count", summary ? summary.included_count : 0);
  setText("excluded-count", summary ? summary.excluded_count : 0);
  setText("error-count", summary ? summary.error_count : 0);
  setText("visited-count", summary ? summary.visited_entries : 0);
}

function renderAlerts() {
  byId("alerts").replaceChildren();
  if (state.scanFailure) {
    const suffix = state.latest && state.latest.status === "available"
      ? "下面保留的是此前加载的结果，可能不是最新清单。"
      : "当前没有已加载的结果，无法确认是否曾扫描。";
    addAlert(`${state.scanFailure} ${suffix}`, "error");
  }
  if (state.loading) addAlert("正在加载工作区配置和文件清单…");
  if (!state.latest) return;
  if (state.latest.status === "never_scanned") {
    addAlert("该工作区尚未扫描。点击“重新扫描”生成第一份文件清单。", "warning");
    return;
  }
  if (state.latest.needs_rescan) {
    addAlert("扫描规则已变化，请重新扫描。当前展示的是旧规则下保存的结果。", "warning");
  }
  const result = state.latest.result;
  if (result.summary.traversal_limit_reached) {
    addAlert("目录遍历达到上限并被提前截断，当前清单不完整。", "warning");
  }
  const fileLimitCount = result.excluded.filter((item) => item.reason === "file_limit_exceeded").length;
  if (fileLimitCount > 0) {
    addAlert(`有 ${fileLimitCount} 个文件因收录数量限制被排除。`, "warning");
  }
  if (result.errors.length > 0) {
    addAlert(`扫描完成，但有 ${result.errors.length} 个条目无法安全获取元数据，请查看“扫描错误”。`, "warning");
  }
}

function includedRows() {
  if (!state.latest || !state.latest.result) return [];
  const needle = state.filter.trim().toLocaleLowerCase();
  let rows = state.latest.result.included.filter((item) => {
    if (!needle) return true;
    return item.name.toLocaleLowerCase().includes(needle) || item.relative_path.toLocaleLowerCase().includes(needle);
  });
  if (state.sortKey === "path") {
    rows = rows.slice();
  } else {
    rows = rows.map((item, index) => ({ item, index })).sort((left, right) => {
      const a = state.sortKey === "size" ? left.item.size_bytes : new Date(left.item.modified_at).getTime();
      const b = state.sortKey === "size" ? right.item.size_bytes : new Date(right.item.modified_at).getTime();
      return a === b ? left.index - right.index : a - b;
    }).map((entry) => entry.item);
  }
  if (state.sortDirection === "desc") rows.reverse();
  return rows;
}

function emptyIncludedMessage(rows) {
  if (!state.latest) return state.loading ? "正在加载文件清单…" : "文件清单未加载，扫描状态未知。";
  if (state.latest.status === "never_scanned") return "尚未扫描，没有可展示的文件清单。";
  if (state.filter && rows.length === 0) return "没有符合当前文件名或路径筛选条件的文件。";
  const result = state.latest.result;
  if (result.included.length > 0) return "";
  if (result.summary.visited_entries === 0 && result.excluded.length === 0 && result.errors.length === 0) {
    return "工作区为空，上次扫描没有发现任何目录项。";
  }
  if (result.excluded.length > 0 && result.errors.length === 0) return "没有符合当前扫描策略的文件；发现的条目均被排除。";
  if (result.errors.length > 0) return "扫描完成但没有收录文件；部分条目发生访问错误。";
  return "上次扫描没有收录文件。";
}

function renderPagination(container, key, total, renderAgain) {
  container.replaceChildren();
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  state.pages[key] = Math.min(Math.max(1, state.pages[key]), pageCount);
  if (total <= PAGE_SIZE) return;

  const previous = document.createElement("button");
  previous.type = "button";
  previous.textContent = "上一页";
  previous.disabled = state.pages[key] === 1;
  previous.addEventListener("click", () => { state.pages[key] -= 1; renderAgain(); });

  const label = document.createElement("span");
  label.textContent = `第 ${state.pages[key]} / ${pageCount} 页，共 ${total} 条`;

  const next = document.createElement("button");
  next.type = "button";
  next.textContent = "下一页";
  next.disabled = state.pages[key] === pageCount;
  next.addEventListener("click", () => { state.pages[key] += 1; renderAgain(); });
  container.append(previous, label, next);
}

function renderIncluded() {
  const rows = includedRows();
  const body = byId("included-body");
  body.replaceChildren();
  const empty = byId("included-empty");
  const message = emptyIncludedMessage(rows);
  show(empty, rows.length === 0);
  empty.textContent = message;

  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  state.pages.included = Math.min(state.pages.included, pageCount);
  const start = (state.pages.included - 1) * PAGE_SIZE;
  for (const item of rows.slice(start, start + PAGE_SIZE)) {
    const row = document.createElement("tr");
    appendCell(row, item.name);
    appendCell(row, item.relative_path, "path-text");
    const modified = appendCell(row, formatDate(item.modified_at));
    modified.title = item.modified_at;
    const size = appendCell(row, formatSize(item.size_bytes));
    size.title = `${item.size_bytes.toLocaleString()} 字节`;
    const status = appendCell(row, "");
    const pill = document.createElement("span");
    pill.className = "status-pill";
    pill.textContent = "已扫描，尚未读取";
    status.appendChild(pill);
    body.appendChild(row);
  }
  renderPagination(byId("included-pagination"), "included", rows.length, renderIncluded);
}

function renderIssueTable(key) {
  const result = state.latest && state.latest.result;
  const rows = result ? result[key] : [];
  const body = byId(`${key}-body`);
  body.replaceChildren();
  const empty = byId(`${key}-empty`);
  show(empty, rows.length === 0);
  empty.textContent = !state.latest
    ? (state.loading ? "正在加载…" : "结果未加载。")
    : state.latest.status === "never_scanned" ? "尚未扫描。"
    : key === "excluded" ? "上次扫描没有排除项。" : "上次扫描没有记录到条目错误。";

  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  state.pages[key] = Math.min(state.pages[key], pageCount);
  const start = (state.pages[key] - 1) * PAGE_SIZE;
  for (const item of rows.slice(start, start + PAGE_SIZE)) {
    const row = document.createElement("tr");
    appendCell(row, item.relative_path, "path-text");
    appendCell(row, entryTypeLabels[item.entry_type] || item.entry_type);
    appendCell(row, "—");
    appendCell(row, "—");
    const reason = reasonLabels[item.reason] || `未知原因（${item.reason}）`;
    const reasonCell = appendCell(row, reason);
    reasonCell.title = item.detail || "";
    body.appendChild(row);
  }
  renderPagination(byId(`${key}-pagination`), key, rows.length, () => renderIssueTable(key));
}

function renderResults() {
  renderWorkspaceInfo();
  renderSummary();
  renderAlerts();
  renderIncluded();
  renderIssueTable("excluded");
  renderIssueTable("errors");
}

function renderScanButton() {
  const scanning = state.workspace && state.activeScans.has(state.workspace.workspace_id);
  byId("scan-button").disabled = !state.workspace || scanning;
  byId("scan-button").textContent = scanning ? "正在扫描…" : "重新扫描";
}

async function loadLatest(workspaceId, failurePrefix = "无法加载工作区配置或已保存清单") {
  const version = ++state.loadVersion;
  const isCurrent = () => version === state.loadVersion && state.workspace?.workspace_id === workspaceId;
  state.scanFailure = null;
  state.loading = true;
  renderResults();
  try {
    const base = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}`;
    const [workspace, latest] = await Promise.all([
      requestJson(base), requestJson(`${base}/scan-result`),
    ]);
    if (!isCurrent()) return false;
    // Commit both responses together; failed refreshes keep the previous view.
    state.workspace = workspace;
    state.workspaces = state.workspaces.map((item) => item.workspace_id === workspaceId ? workspace : item);
    state.latest = latest;
    state.pages = { included: 1, excluded: 1, errors: 1 };
    return true;
  } catch (error) {
    if (isCurrent()) state.scanFailure = `${failurePrefix}：${error.message}`;
    return false;
  } finally {
    if (isCurrent()) {
      state.loading = false;
      renderResults();
    }
  }
}

async function selectWorkspace(workspaceId) {
  ++state.loadVersion;
  state.workspace = state.workspaces.find((item) => item.workspace_id === workspaceId) || null;
  state.latest = null;
  state.scanFailure = null;
  state.loading = false;
  state.pages = { included: 1, excluded: 1, errors: 1 };
  renderScanButton();
  if (state.workspace) await loadLatest(state.workspace.workspace_id);
}

async function loadWorkspaces() {
  const version = ++state.workspaceListVersion;
  try {
    const workspaces = await requestJson("/api/v1/workspaces");
    if (version !== state.workspaceListVersion) return;
    state.workspaces = workspaces;
  } catch (error) {
    if (version !== state.workspaceListVersion) return;
    show(byId("no-workspaces"), true);
    byId("no-workspaces").querySelector("strong").textContent = "无法读取工作区列表";
    byId("no-workspaces").querySelector("p").textContent = error.message;
    return;
  }
  renderWorkspaceOptions();
  if (state.workspaces.length) await selectWorkspace(byId("workspace-select").value);
}

function renderWorkspaceOptions(selectedId = state.workspace?.workspace_id) {
  const hasWorkspaces = state.workspaces.length > 0;
  show(byId("no-workspaces"), !hasWorkspaces);
  show(byId("workspace-content"), hasWorkspaces);
  show(byId("results-section"), hasWorkspaces);
  if (!hasWorkspaces) return;

  const selector = byId("workspace-select");
  selector.replaceChildren();
  for (const workspace of state.workspaces) {
    const option = document.createElement("option");
    option.value = workspace.workspace_id;
    option.textContent = workspace.name;
    selector.appendChild(option);
  }
  selector.value = state.workspaces.some((item) => item.workspace_id === selectedId)
    ? selectedId : state.workspaces[0].workspace_id;
}

function workspaceCreateMessage(message, isError = false) {
  const element = byId("workspace-create-message");
  element.textContent = message;
  element.className = isError ? "alert error" : "alert";
  show(element, Boolean(message));
}

async function createWorkspace(event) {
  if (event) event.preventDefault();
  if (state.creatingWorkspace) return;
  const pathInput = byId("workspace-root-input");
  const nameInput = byId("workspace-name-input");
  let rootPath = pathInput.value.trim();
  // Windows Explorer's "Copy as path" includes a matching pair of quotes.
  if (rootPath.startsWith('"') && rootPath.endsWith('"')) rootPath = rootPath.slice(1, -1);
  if (!rootPath) {
    workspaceCreateMessage("请输入已有文件夹的绝对路径。", true);
    return;
  }
  const folderName = rootPath.replace(/[\\/]+$/, "").split(/[\\/]/).pop();
  const name = nameInput.value.trim() || (folderName || "本地工作区").slice(0, 100);
  state.creatingWorkspace = true;
  const button = byId("create-workspace-button");
  button.disabled = pathInput.disabled = nameInput.disabled = true;
  button.textContent = "正在注册…";
  workspaceCreateMessage("");
  try {
    const workspace = await requestJson("/api/v1/workspaces", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, root_path: rootPath }),
    });
    // Prevent an older initial list request from hiding the new workspace.
    ++state.workspaceListVersion;
    state.workspaces = [...state.workspaces.filter((item) => item.workspace_id !== workspace.workspace_id), workspace];
    renderWorkspaceOptions(workspace.workspace_id);
    state.filter = "";
    byId("file-filter").value = "";
    pathInput.value = nameInput.value = "";
    workspaceCreateMessage("工作区已创建并选中。点击“重新扫描”即可生成文件清单。");
    await selectWorkspace(workspace.workspace_id);
  } catch (error) {
    const message = error.status === 409
      ? "该文件夹已注册，请在工作区列表中选择它；若列表尚未更新，请刷新页面。"
      : error.status === 422
        ? "无法注册：请填写存在且可访问的本机文件夹绝对路径；文件、网络路径和目录链接不受支持。"
        : `创建失败：${error.message}。可保留输入后重试；若已创建，请刷新列表查看。`;
    workspaceCreateMessage(message, true);
  } finally {
    state.creatingWorkspace = false;
    button.disabled = pathInput.disabled = nameInput.disabled = false;
    button.textContent = "设为工作区";
  }
}

async function rescan() {
  if (!state.workspace) return;
  const workspaceId = state.workspace.workspace_id;
  if (state.activeScans.has(workspaceId)) return;
  state.activeScans.add(workspaceId);
  ++state.loadVersion;
  state.loading = false;
  renderScanButton();
  state.scanFailure = null;
  renderAlerts();
  try {
    await requestJson(`/api/v1/workspaces/${encodeURIComponent(workspaceId)}/scan`, { method: "POST" });
    if (state.workspace?.workspace_id !== workspaceId) return;
    const loaded = await loadLatest(workspaceId, "扫描已完成，但新清单加载失败");
    if (loaded) addAlert("重新扫描已完成，文件清单已保存并更新。", "info");
  } catch (error) {
    if (state.workspace?.workspace_id !== workspaceId) return;
    state.scanFailure = error.status === 409 && error.message.includes("already being scanned")
      ? "该工作区已有扫描正在进行，请稍后重试。"
      : `重新扫描失败：${error.message}`;
    renderResults();
  } finally {
    state.activeScans.delete(workspaceId);
    renderScanButton();
  }
}

function initializeEvents() {
  byId("workspace-create-form").addEventListener("submit", createWorkspace);
  byId("workspace-select").addEventListener("change", (event) => selectWorkspace(event.target.value));
  byId("scan-button").addEventListener("click", rescan);
  byId("file-filter").addEventListener("input", (event) => {
    state.filter = event.target.value;
    state.pages.included = 1;
    renderIncluded();
  });
  byId("sort-key").addEventListener("change", (event) => {
    state.sortKey = event.target.value;
    state.pages.included = 1;
    renderIncluded();
  });
  byId("sort-direction").addEventListener("change", (event) => {
    state.sortDirection = event.target.value;
    state.pages.included = 1;
    renderIncluded();
  });
  document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => {
    state.activeTab = button.dataset.tab;
    document.querySelectorAll(".tab").forEach((item) => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.add("hidden"));
    byId(`${state.activeTab}-panel`).classList.remove("hidden");
  }));
}

document.addEventListener("DOMContentLoaded", () => {
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "浏览器本地时区";
  setText("timezone-note", `文件修改时间按浏览器本地时区显示：${timeZone}`);
  initializeEvents();
  loadWorkspaces();
});
