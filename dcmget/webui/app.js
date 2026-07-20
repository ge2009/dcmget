const DETAIL_LIMIT = 200;
const MAX_LOG_ENTRIES = 600;
const ERROR_LOG_LEVELS = new Set(["ERROR", "CRITICAL"]);

const TASK_STATUS = {
  idle: ["等待开始", "neutral"],
  queued: ["排队中", "neutral"],
  preflight: ["预检中", "working"],
  starting: ["启动接收器", "working"],
  starting_receiver: ["启动接收器", "working"],
  running: ["下载中", "working"],
  downloading: ["下载中", "working"],
  pausing: ["暂停中", "warning"],
  pause_pending: ["等待暂停", "warning"],
  paused: ["已暂停", "warning"],
  stopping: ["停止中", "warning"],
  interrupted: ["下载已中断", "warning"],
  download_retryable: ["可继续下载", "warning"],
  pdi_pending: ["等待导出 PDI", "working"],
  pdi_running: ["正在导出 PDI", "working"],
  pdi_retryable: ["PDI 可重试", "warning"],
  verifying: ["正在校验 PDI", "working"],
  verification_completed: ["PDI 校验完成", "success"],
  verification_failed: ["PDI 校验失败", "error"],
  verification_cancelled: ["PDI 校验已取消", "neutral"],
  shutting_down: ["后台正在退出", "warning"],
  shutdown_failed: ["后台退出异常", "error"],
  recovery_error: ["恢复点异常", "error"],
  locked: ["Profile 已占用", "error"],
  stopped: ["后台已停止", "neutral"],
  completed: ["已完成", "success"],
  partial: ["部分成功", "warning"],
  partial_success: ["部分成功", "warning"],
  failed: ["失败", "error"],
  cancelled: ["已取消", "neutral"],
  canceled: ["已取消", "neutral"],
  no_data: ["无数据", "warning"],
};

const TERMINAL_STATUSES = new Set([
  "completed", "partial", "partial_success", "failed", "cancelled", "canceled",
  "verification_completed", "verification_failed", "verification_cancelled",
]);

const state = {
  bootstrap: {},
  config: {},
  task: null,
  parsedAccessions: [],
  parseStats: { blank: 0, duplicate: 0, invalid: 0 },
  preflightOk: false,
  preflightSignature: "",
  csrfToken: "",
  eventSource: null,
  logs: [],
  showDetailedLogs: false,
  directoryPurpose: "destination",
  directoryTarget: null,
  currentDirectory: "",
  startedAt: 0,
  refreshTimer: 0,
  preflightTimer: 0,
  accessionInputTimer: 0,
  preflightRequestId: 0,
  directoryRequestId: 0,
  taskActionPending: false,
  pdiActionPending: false,
  taskEditorCollapsed: false,
  initialized: false,
  localSession: true,
  managerMode: false,
  profiles: [],
  windowsService: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

class ApiError extends Error {
  constructor(message, status = 0, details = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

async function api(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  let body = options.body;

  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-CSRF-Token", state.csrfToken || "");
  }
  const rawBody = body instanceof FormData
    || body instanceof Blob
    || body instanceof ArrayBuffer
    || ArrayBuffer.isView(body)
    || body instanceof URLSearchParams;
  if (body !== undefined && body !== null && !rawBody) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }

  const response = await fetch(path, {
    method,
    headers,
    body,
    credentials: "same-origin",
    cache: "no-store",
  });

  const contentType = response.headers.get("content-type") || "";
  let payload = null;
  if (response.status !== 204) {
    payload = contentType.includes("application/json")
      ? await response.json().catch(() => null)
      : await response.text().catch(() => "");
  }

  const rotatedToken = response.headers.get("X-CSRF-Token")
    || payload?.csrf_token
    || payload?.csrfToken;
  if (rotatedToken) state.csrfToken = rotatedToken;

  if (!response.ok) {
    if (response.status === 401 && path !== "/api/bootstrap") {
      state.csrfToken = "";
      window.setTimeout(loadBootstrap, 0);
    }
    const message = payload?.message || payload?.error || payload?.detail
      || `请求失败（HTTP ${response.status}）`;
    throw new ApiError(String(message), response.status, payload);
  }
  return payload ?? {};
}

function setText(selector, value) {
  const element = typeof selector === "string" ? $(selector) : selector;
  if (element) element.textContent = value == null || value === "" ? "—" : String(value);
}

function setButtonLabel(button, value) {
  const label = $("span", button);
  if (label && $("svg.icon", button)) label.textContent = String(value);
  else button.textContent = String(value);
}

function showToast(message, duration = 3200) {
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.textContent = String(message);
  $("#toast-region").append(toast);
  window.setTimeout(() => toast.remove(), duration);
}

function showAlert(message, title = "操作失败") {
  setText("#global-alert-title", title);
  setText("#global-alert-message", message);
  $("#global-alert").hidden = false;
}

function clearAlert() {
  $("#global-alert").hidden = true;
}

function normalizeStatus(value) {
  return String(value || "idle").trim().toLowerCase().replaceAll("-", "_").replaceAll(" ", "_");
}

function statusPresentation(status) {
  return TASK_STATUS[normalizeStatus(status)] || [String(status || "未知状态"), "neutral"];
}

function setStatusBadge(element, status, explicitLabel = "") {
  if (!element) return;
  const [label, tone] = statusPresentation(status);
  element.className = `status-badge status-badge--${tone}`;
  element.textContent = explicitLabel || label;
}

function formatBytesPerSecond(value) {
  let bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B/s";
  const units = ["B/s", "KB/s", "MB/s", "GB/s"];
  let index = 0;
  while (bytes >= 1024 && index < units.length - 1) {
    bytes /= 1024;
    index += 1;
  }
  const decimals = bytes >= 100 || index === 0 ? 0 : bytes >= 10 ? 1 : 2;
  return `${bytes.toFixed(decimals)} ${units[index]}`;
}

function formatDuration(value) {
  const totalSeconds = Math.max(0, Math.floor(Number(value || 0)));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours) return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function parseAccessions(raw, { schedule = true } = {}) {
  const values = [];
  const seen = new Set();
  let blank = 0;
  let duplicate = 0;
  let invalid = 0;

  const source = String(raw || "");
  const lines = source ? source.split(/\r?\n/) : [];
  lines.forEach((line) => {
    const value = line.trim();
    if (!value) {
      blank += 1;
      return;
    }
    if (/[\\*?\u0000-\u001f\u007f]/.test(value)) {
      invalid += 1;
      return;
    }
    if (seen.has(value)) {
      duplicate += 1;
      return;
    }
    seen.add(value);
    values.push(value);
  });

  state.parsedAccessions = values;
  state.parseStats = { blank, duplicate, invalid };
  renderImportSummary();
  invalidatePreflight(schedule);
  return values;
}

function renderImportSummary() {
  setText("#accession-valid-count", state.parsedAccessions.length);
  setText("#accession-duplicate-summary", `重复 ${state.parseStats.duplicate}`);
  setText("#accession-blank-summary", `空行 ${state.parseStats.blank}`);
  const error = $("#accession-error");
  if (state.parseStats.invalid) {
    error.textContent = `${state.parseStats.invalid} 个检查号包含不允许的字符，已忽略。`;
    error.hidden = false;
  } else {
    error.hidden = true;
  }
  setStatusBadge(
    $("#task-readiness"),
    state.parsedAccessions.length ? "queued" : "idle",
    state.parsedAccessions.length ? `${state.parsedAccessions.length} 个检查号` : "等待输入",
  );
  updateStartAvailability();
}

function taskDraft() {
  return {
    accessions: [...state.parsedAccessions],
    destination: $("#destination-input").value.trim(),
    pdi: {
      enabled: $("#quick-pdi-enabled").checked,
      output_folder: $("#quick-pdi-folder").value.trim(),
    },
  };
}

function draftSignature() {
  return JSON.stringify(taskDraft());
}

function invalidatePreflight(schedule = true) {
  if (state.preflightSignature && state.preflightSignature !== draftSignature()) {
    state.preflightOk = false;
    state.preflightSignature = "";
    resetPreflightChecks();
  }
  updateStartAvailability();
  if (schedule && state.initialized) schedulePreflight();
}

function schedulePreflight(delay = 450) {
  window.clearTimeout(state.preflightTimer);
  if (!state.initialized || !$("#destination-input").value.trim()) return;
  state.preflightTimer = window.setTimeout(
    () => runPreflight({ requireAccessions: false, silent: true }),
    delay,
  );
}

function resetPreflightChecks() {
  $(".preflight-block").classList.remove("is-success");
  $$("#preflight-list li").forEach((item) => {
    item.dataset.state = "pending";
    setText($(".check-icon", item), "·");
    setText($("small", item), "尚未检查");
  });
}

function updateStartAvailability() {
  const hasInput = state.parsedAccessions.length > 0;
  const hasDestination = Boolean($("#destination-input").value.trim());
  const ready = hasInput && hasDestination && state.preflightOk;
  $("#start-task-button").disabled = !ready;
  if (!hasInput) setText("#start-task-hint", "请先输入至少一个有效检查号。");
  else if (!hasDestination) setText("#start-task-hint", "请选择运行 DcmGet 主机上的保存目录。");
  else if (!state.preflightOk) setText("#start-task-hint", "请先运行启动预检。");
  else setText("#start-task-hint", "预检通过，可以开始下载。");
}

function normalizeChecks(payload) {
  const source = payload?.checks || payload?.items || payload || {};
  if (Array.isArray(source)) {
    return Object.fromEntries(source.map((item) => [item.key || item.name, item]));
  }
  return Object.fromEntries(Object.entries(source).map(([key, value]) => [
    key,
    typeof value === "object" && value !== null ? value : { ok: Boolean(value) },
  ]));
}

function renderPreflight(payload, signature = draftSignature()) {
  const checks = normalizeChecks(payload);
  const aliases = {
    config: ["config", "configuration"],
    dcmtk: ["dcmtk", "tools"],
    destination: ["destination", "directory", "storage"],
    receiver: ["receiver", "port", "storage_port"],
  };
  let allOk = payload?.ok !== false;
  for (const [displayKey, keys] of Object.entries(aliases)) {
    const item = $(`#preflight-list li[data-check="${displayKey}"]`);
    const check = keys.map((key) => checks[key]).find(Boolean);
    const ok = check ? Boolean(check.ok ?? check.success ?? check.ready) : false;
    allOk = allOk && ok;
    item.dataset.state = ok ? "ok" : "error";
    setText($(".check-icon", item), ok ? "✓" : "×");
    setText($("small", item), check?.message || check?.detail || (ok ? "已就绪" : "未通过"));
  }
  state.preflightOk = allOk;
  state.preflightSignature = allOk ? signature : "";
  $(".preflight-block").classList.toggle("is-success", allOk);
  setStatusBadge($("#task-readiness"), allOk ? "completed" : "failed", allOk ? "预检通过" : "预检未通过");
  updateStartAvailability();
}

async function runPreflight({ requireAccessions = true, silent = false } = {}) {
  if (!silent) clearAlert();
  parseAccessions($("#accession-input").value, { schedule: false });
  if (requireAccessions && !state.parsedAccessions.length) {
    showAlert("请先输入至少一个有效检查号。", "无法预检");
    $("#accession-input").focus();
    return false;
  }
  if (requireAccessions && !$("#destination-input").value.trim()) {
    showAlert("请选择运行 DcmGet 主机上的保存目录。", "无法预检");
    $("#destination-input").focus();
    return false;
  }

  const button = $("#run-preflight-button");
  const signature = draftSignature();
  const requestId = ++state.preflightRequestId;
  button.disabled = true;
  setButtonLabel(button, "检查中…");
  try {
    const result = await api("/api/preflight", { method: "POST", body: taskDraft() });
    if (requestId !== state.preflightRequestId || signature !== draftSignature()) return false;
    renderPreflight(result, signature);
    if (!state.preflightOk && !silent) showAlert(result.message || "部分检查未通过，请根据红色项目修复后重试。", "预检未通过");
    return state.preflightOk;
  } catch (error) {
    if (requestId !== state.preflightRequestId || signature !== draftSignature()) return false;
    renderPreflight({ ok: false }, signature);
    if (!silent) showAlert(error.message, "预检失败");
    return false;
  } finally {
    if (requestId === state.preflightRequestId) {
      button.disabled = false;
      setButtonLabel(button, "重新检查");
    }
  }
}

async function startTask() {
  window.clearTimeout(state.accessionInputTimer);
  parseAccessions($("#accession-input").value, { schedule: false });
  if (!state.preflightOk || state.preflightSignature !== draftSignature()) {
    const ok = await runPreflight();
    if (!ok) return;
  }
  const draft = taskDraft();
  const signature = JSON.stringify(draft);
  if (!state.preflightOk || state.preflightSignature !== signature) {
    showAlert("任务内容已经改变，请重新完成预检。", "无法开始下载");
    return;
  }
  const profile = state.bootstrap.profile || {};
  const config = state.config || {};
  const profileName = profile.name || profile.id || state.bootstrap.profile_name || "default";
  const pacs = `${config.pacs_server_ip || "—"}:${config.pacs_server_port || "—"} / ${config.pacs_ae_title || "—"}`;
  const pdi = draft.pdi.enabled ? `开启${draft.pdi.output_folder ? `（${draft.pdi.output_folder}）` : ""}` : "关闭";
  await confirmAction(
    "确认开始下载",
    `Profile：${profileName}\nPACS：${pacs}\n保存目录：${draft.destination}\nPDI：${pdi}\n\n将提交 ${draft.accessions.length.toLocaleString()} 个检查号，确认后立即交给后台执行。`,
    () => submitStartTask(draft, signature),
    { confirmLabel: "确认并开始", tone: "primary" },
  );
}

async function submitStartTask(draft, signature) {
  window.clearTimeout(state.accessionInputTimer);
  parseAccessions($("#accession-input").value, { schedule: false });
  if (signature !== draftSignature() || state.preflightSignature !== signature) {
    showAlert("确认期间任务内容发生了变化，请重新预检后再开始。", "任务未启动");
    updateStartAvailability();
    return;
  }
  const button = $("#start-task-button");
  button.disabled = true;
  setButtonLabel(button, "正在创建…");
  try {
    const result = await api("/api/task/start", {
      method: "POST",
      body: draft,
    });
    state.task = result.task || result;
    state.startedAt = Date.now();
    renderTask(state.task);
    showToast("任务已交给后台执行，关闭浏览器不会停止下载。");
  } catch (error) {
    showAlert(error.message, "任务启动失败");
    updateStartAvailability();
  } finally {
    setButtonLabel(button, "开始下载");
  }
}

function taskItems(task) {
  const completed = task?.items || task?.results;
  if (Array.isArray(completed) && completed.length) return completed;
  if (!Array.isArray(task?.accessions)) return [];
  return task.accessions.map((item) => (
    item && typeof item === "object"
      ? item
      : { accession: String(item), status: "等待" }
  ));
}

function taskCount(task, ...keys) {
  for (const key of keys) {
    if (task?.[key] != null) return Number(task[key]) || 0;
    if (task?.summary?.[key] != null) return Number(task.summary[key]) || 0;
    if (task?.status_counts?.[key] != null) return Number(task.status_counts[key]) || 0;
  }
  return 0;
}

function renderTask(task) {
  if (!task || !task.id && normalizeStatus(task.status) === "idle") {
    showTaskEditor();
    return;
  }

  state.task = task;
  const status = normalizeStatus(task.status);
  const total = taskCount(task, "total", "total_count", "accession_count") || taskItems(task).length;
  const processed = taskCount(task, "processed", "processed_count", "finished_count");
  const percent = total ? Math.min(100, Math.round(processed / total * 100)) : 0;
  const current = task.current_accession || task.current?.accession || task.accession || "—";
  const fileCount = taskCount(task, "file_count", "received_files", "files");
  const speed = task.speed_bytes_per_second ?? task.speed_bps ?? task.current_speed ?? 0;
  const elapsed = task.elapsed_seconds ?? (state.startedAt ? (Date.now() - state.startedAt) / 1000 : 0);

  $("#task-editor").hidden = true;
  $("#task-runtime").hidden = false;
  setText("#runtime-title", statusPresentation(status)[0]);
  setText("#runtime-subtitle", task.message || task.detail || (TERMINAL_STATUSES.has(status)
    ? "任务结果已保留，可以打开目录或继续处理失败项。"
    : "任务由 DcmGet 后台服务持续执行，关闭浏览器不会停止。"));
  setStatusBadge($("#runtime-status"), status);
  setText("#progress-text", `${processed.toLocaleString()} / ${total.toLocaleString()}`);
  setText("#progress-percent", `${percent}%`);
  $("#progress-fill").style.width = `${percent}%`;
  const progress = $(".progress-track");
  progress.setAttribute("aria-valuenow", String(percent));
  setText("#metric-accession", current);
  setText("#metric-files", fileCount.toLocaleString());
  setText("#metric-speed", formatBytesPerSecond(speed));
  setText("#metric-duration", formatDuration(elapsed));

  const actions = task.actions || {};
  const running = ["preflight", "starting", "starting_receiver", "running", "downloading", "queued"].includes(status);
  const resumable = ["paused", "interrupted", "download_retryable"].includes(status);
  $("#pause-task-button").hidden = !(actions.can_pause ?? running);
  $("#resume-task-button").hidden = !(actions.can_resume ?? resumable);
  $("#cancel-task-button").hidden = !(actions.can_cancel ?? !TERMINAL_STATUSES.has(status));
  $("#retry-task-button").hidden = !(actions.can_retry_failed ?? (
    ["interrupted", "download_retryable", "failed", "partial", "partial_success"].includes(status)
    && taskCount(task, "failed", "failed_count", "失败") > 0
  ));
  $("#accept-partial-button").hidden = !(actions.can_accept_partial ?? (
    ["download_retryable", "failed", "partial", "partial_success"].includes(status) && fileCount > 0
  ));
  $("#new-task-button").hidden = !TERMINAL_STATUSES.has(status);
  renderPdiState(task.pdi || task.pdi_result || null);
  renderTaskDetails(task, total);
}

function renderPdiState(pdi) {
  const root = $("#pdi-runtime");
  root.hidden = !pdi;
  if (!pdi) return;
  const status = normalizeStatus(pdi.status || "queued");
  setStatusBadge($("#pdi-runtime-status"), status, statusPresentation(status)[0]);
  setText("#pdi-runtime-message", pdi.message || pdi.detail || pdi.output_folder || "PDI 导出状态已更新。");
  const finished = ["completed", "partial", "partial_success"].includes(status);
  $("#open-pdi-button").hidden = !finished || !state.localSession;
  $("#verify-pdi-button").hidden = !finished;
  $("#retry-pdi-button").hidden = !["failed", "partial", "partial_success"].includes(status);
}

function showTaskEditor() {
  state.task = null;
  $("#task-editor").hidden = false;
  $("#task-runtime").hidden = true;
  setTaskEditorCollapsed(false);
}

function setTaskEditorCollapsed(collapsed) {
  state.taskEditorCollapsed = Boolean(collapsed);
  const editor = $("#task-editor");
  const button = $("#toggle-task-editor");
  editor.classList.toggle("is-collapsed", state.taskEditorCollapsed);
  $("#task-editor-body").hidden = state.taskEditorCollapsed;
  button.setAttribute("aria-expanded", String(!state.taskEditorCollapsed));
  setText("#toggle-task-editor-label", state.taskEditorCollapsed ? "展开" : "收起");
}

function renderTaskDetails(task, total) {
  const items = taskItems(task);
  const large = total > DETAIL_LIMIT;
  $("#large-task-summary").hidden = !large;
  $("#task-table-wrap").hidden = large || items.length === 0;
  if (large) {
    setText("#large-task-text", `共 ${total.toLocaleString()} 条检查号，超过 ${DETAIL_LIMIT} 条；为保持页面流畅，已隐藏逐项列表。`);
    const summary = $("#status-summary");
    summary.replaceChildren();
    const values = [
      ["完成", taskCount(task, "completed", "completed_count", "完成")],
      ["无数据", taskCount(task, "no_data", "no_data_count", "无数据")],
      ["部分成功", taskCount(task, "partial", "partial_count", "部分成功")],
      ["失败", taskCount(task, "failed", "failed_count", "失败")],
      ["已取消", taskCount(task, "cancelled", "cancelled_count", "已取消")],
      ["文件", taskCount(task, "file_count", "received_files", "files")],
    ];
    values.forEach(([label, count]) => {
      const item = document.createElement("span");
      item.textContent = `${label} ${Number(count).toLocaleString()}`;
      summary.append(item);
    });
    return;
  }

  const body = $("#task-table-body");
  body.replaceChildren();
  items.slice(0, DETAIL_LIMIT).forEach((item) => {
    const row = document.createElement("tr");
    const status = statusPresentation(item.status)[0];
    const values = [
      item.accession || item.accession_number || item.value || "—",
      status,
      Number(item.file_count || item.files || 0).toLocaleString(),
      formatDuration(item.elapsed_seconds || item.duration_seconds || 0),
      item.error_summary || item.message || item.detail || "—",
    ];
    values.forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = String(value);
      row.append(cell);
    });
    body.append(row);
  });
}

async function taskAction(action, body = {}) {
  if (state.taskActionPending) return;
  const controls = [
    "#pause-task-button", "#resume-task-button", "#cancel-task-button",
    "#retry-task-button", "#accept-partial-button", "#new-task-button",
  ].map((selector) => $(selector));
  state.taskActionPending = true;
  $("#task-runtime").setAttribute("aria-busy", "true");
  controls.forEach((button) => { button.disabled = true; });
  try {
    const result = await api(`/api/task/${action}`, { method: "POST", body });
    state.task = result.task || result;
    renderTask(state.task);
  } catch (error) {
    showAlert(error.message, "任务操作失败");
  } finally {
    state.taskActionPending = false;
    $("#task-runtime").removeAttribute("aria-busy");
    controls.forEach((button) => { button.disabled = false; });
  }
}

async function runPdiAction(callback) {
  if (state.pdiActionPending) return;
  const controls = ["#open-pdi-button", "#verify-pdi-button", "#retry-pdi-button"]
    .map((selector) => $(selector));
  state.pdiActionPending = true;
  $("#pdi-runtime").setAttribute("aria-busy", "true");
  controls.forEach((button) => { button.disabled = true; });
  try {
    await callback();
  } finally {
    state.pdiActionPending = false;
    $("#pdi-runtime").removeAttribute("aria-busy");
    controls.forEach((button) => { button.disabled = false; });
  }
}

async function confirmAction(
  title,
  message,
  callback,
  { confirmLabel = "确认", tone = "danger" } = {},
) {
  const dialog = $("#confirm-dialog");
  const confirmButton = $("#confirm-action-button");
  setText("#confirm-title", title);
  setText("#confirm-message", message);
  confirmButton.textContent = confirmLabel;
  confirmButton.classList.toggle("button--primary", tone === "primary");
  confirmButton.classList.toggle("button--danger", tone !== "primary");
  dialog.returnValue = "";
  const onClose = async () => {
    dialog.removeEventListener("close", onClose);
    if (dialog.returnValue === "confirm") await callback();
  };
  dialog.addEventListener("close", onClose);
  dialog.showModal();
}

function addLog(entry) {
  const normalized = {
    timestamp: entry.timestamp || entry.time || new Date().toISOString(),
    level: String(entry.level || "INFO").toUpperCase(),
    source: entry.source || entry.component || "应用",
    message: entry.message || entry.text || String(entry),
  };
  state.logs.push(normalized);
  if (state.logs.length > MAX_LOG_ENTRIES) state.logs.splice(0, state.logs.length - MAX_LOG_ENTRIES);
  renderLogs();
}

function visibleLogs() {
  return state.showDetailedLogs ? state.logs : state.logs.filter((entry) => ERROR_LOG_LEVELS.has(entry.level));
}

function renderLogs() {
  const list = $("#log-list");
  const keepAtBottom = list.hidden || list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  const logs = visibleLogs();
  list.replaceChildren();
  $("#empty-log-state").hidden = logs.length > 0;
  list.hidden = logs.length === 0;
  logs.forEach((entry) => {
    const row = document.createElement("li");
    row.className = "log-entry";
    row.dataset.level = entry.level;
    const time = document.createElement("time");
    const date = new Date(entry.timestamp);
    time.textContent = Number.isNaN(date.getTime()) ? String(entry.timestamp) : date.toLocaleTimeString("zh-CN", { hour12: false });
    const source = document.createElement("span");
    source.className = "log-source";
    source.textContent = `${entry.source} · ${entry.level}`;
    const message = document.createElement("span");
    message.className = "log-message";
    message.textContent = entry.message;
    row.append(time, source, message);
    list.append(row);
  });
  if (keepAtBottom && list.lastElementChild) list.scrollTop = list.scrollHeight;
}

function requestedPage(fallback = "home") {
  const requested = new URLSearchParams(window.location.search).get("page");
  return ["home", "settings", "operations"].includes(requested) ? requested : fallback;
}

function showPage(pageName, { syncUrl = true } = {}) {
  const allowedPage = state.managerMode ? "operations" : pageName;
  $$("[data-page-panel]").forEach((panel) => {
    const active = panel.dataset.pagePanel === allowedPage;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  });
  $$(".nav-item[data-page]").forEach((button) => {
    const active = button.dataset.page === allowedPage;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (syncUrl) {
    const url = new URL(window.location.href);
    url.searchParams.set("page", allowedPage);
    window.history.replaceState({}, "", url);
  }
  $("#main-content").focus({ preventScroll: true });
  if (allowedPage === "operations") refreshOperations();
}

function showApplication() {
  $("#app-shell").hidden = false;
}

function applyBootstrap(payload) {
  state.bootstrap = payload || {};
  state.csrfToken = payload.csrf_token || payload.csrfToken || state.csrfToken;
  state.managerMode = payload.profile?.mode === "manager";
  document.body.classList.toggle("manager-mode", state.managerMode);
  showApplication();
  state.config = payload.config || {};
  populateSettings(state.config);
  $("#destination-input").value = state.config.dicom_destination_folder || "";
  renderEnvironment(payload);
  renderLicense(payload.license || {});
  if (state.managerMode) {
    document.title = "DcmGet · 管理中心";
    $("#manager-overview").hidden = false;
    setText("#operations-eyebrow", "Windows 服务工作台");
    setText("#operations-title", "DcmGet 管理中心");
    setText("#operations-description", "统一管理这台 Windows 主机上的 Profile，并通过独立链接进入各自任务页面。");
    setConnectionState("connected", "管理中心已连接");
    state.initialized = true;
    showPage("operations", { syncUrl: false });
    return;
  }
  const task = payload.task || payload.active_task;
  if (task) renderTask(task);
  else showTaskEditor();
  connectEvents();
  state.initialized = true;
  showPage(requestedPage("home"), { syncUrl: false });
  if (!state.task && $("#destination-input").value.trim()) schedulePreflight(0);
}

async function loadBootstrap() {
  try {
    const payload = await api("/api/bootstrap");
    applyBootstrap(payload);
  } catch (error) {
    showApplication();
    setConnectionState("disconnected", "后台连接失败");
    showAlert(`无法连接 DcmGet 后台：${error.message}`, "连接失败");
  }
}

function renderEnvironment(payload) {
  const profile = payload.profile || {};
  const config = payload.config || {};
  const receiver = payload.receiver || {};
  const web = payload.web || {};
  state.localSession = web.local_session !== false;
  if (state.managerMode) {
    setText("#header-profile", "Windows 管理中心");
    setText("#header-pacs", "kayisoft-dcmget");
    setText("#header-receiver", `管理端口 ${profile.manager_port || window.location.port || "8786"}`);
  } else {
    setText("#header-profile", `Profile ${profile.name || profile.id || payload.profile_name || "default"}`);
    setText("#header-pacs", `PACS ${config.pacs_server_ip || "—"}:${config.pacs_server_port || "—"} · ${config.pacs_ae_title || "—"}`);
    setText("#header-receiver", `接收 ${config.storage_ae_title || "—"}:${config.storage_port || "—"}`);
  }
  setText("#operation-profile", profile.name || profile.id || payload.profile_name || "default");
  setText("#operation-data-dir", profile.data_dir || payload.data_dir);
  setText("#operation-version", payload.version || payload.app_version);
  setText("#operation-dcmtk-version", payload.dcmtk?.version || payload.dcmtk_version || receiver.dcmtk_version);

  const url = web.lan_url || web.url || payload.lan_url || window.location.origin;
  setText("#operation-web-url", url);
  setText("#lan-url", url);
  $("#lan-notice").hidden = !(web.lan_enabled ?? config.web_lan_enabled ?? payload.lan_enabled);
  $("#profile-management-card").hidden = !(state.localSession || state.managerMode);
  [
    "#open-destination-button",
    "#open-log-directory-button",
    '[data-operation="open-data-directory"]',
    '[data-operation="open-log-directory"]',
    '[data-operation="acceptance-report"]',
    "#shutdown-service-button",
  ].forEach((selector) => {
    const control = $(selector);
    if (control) control.hidden = !state.localSession;
  });
  if (!$("#destination-input").value) $("#destination-input").value = config.dicom_destination_folder || "";
  $("#quick-pdi-enabled").checked = Boolean(config.pdi_export_enabled);
  $("#quick-pdi-folder").value = config.pdi_output_folder || "";
  $("#quick-pdi-folder-row").hidden = !$("#quick-pdi-enabled").checked;
  updateStartAvailability();
}

function renderLicense(license) {
  const summary = $("#license-summary");
  const licensed = Boolean(license.licensed ?? license.registered);
  const status = normalizeStatus(license.status || (licensed ? "licensed" : "trial"));
  summary.dataset.state = status;
  let label = license.customer || license.edition || "";
  if (licensed || status === "licensed") label = label ? `已授权 · ${label}` : "产品已授权";
  else if (license.trial_remaining != null) label = `试用剩余 ${license.trial_remaining} 次`;
  else label = license.message || "授权状态未知";
  setText(summary.lastElementChild, label);
  setText("#operation-license-status", label);
  if (license.machine_code) setText("#license-machine-code", license.machine_code);
}

function populateSettings(config) {
  const form = $("#settings-form");
  const derived = {
    ...config,
    web_lan_enabled: !["127.0.0.1", "::1"].includes(config.web_bind_address),
    pdi_volume_size_gb: config.pdi_volume_size_gb ?? (Number(config.pdi_volume_size_bytes || 0) / 1024 ** 3),
    minimum_free_space_gb: config.minimum_free_space_gb ?? (Number(config.minimum_free_space_bytes || 0) / 1024 ** 3),
    max_log_file_size_mb: config.max_log_file_size_mb ?? (Number(config.max_log_file_size_bytes || 0) / 1024 ** 2),
  };
  for (const element of form.elements) {
    if (!element.name || !(element.name in derived)) continue;
    if (element.type === "checkbox") element.checked = Boolean(derived[element.name]);
    else element.value = derived[element.name] ?? "";
  }
  syncPdiSettingsUi();
}

function syncPdiSettingsUi() {
  const enabled = $("#setting-pdi-enabled").checked;
  $("#setting-pdi-options").hidden = !enabled;
  setStatusBadge(
    $("#pdi-setting-state"),
    enabled ? "completed" : "idle",
    enabled ? "已启用" : "默认关闭",
  );
}

function settingsPayload() {
  const form = $("#settings-form");
  const data = {};
  for (const element of form.elements) {
    if (!element.name) continue;
    data[element.name] = element.type === "checkbox" ? element.checked : element.value.trim();
  }
  const integerFields = [
    "pacs_server_port", "storage_port", "auto_retry_attempts",
    "auto_retry_backoff_seconds", "circuit_breaker_failures", "web_port", "web_session_timeout_minutes",
  ];
  integerFields.forEach((key) => {
    if (data[key] !== "") data[key] = Number.parseInt(data[key], 10);
  });
  data.minimum_free_space_bytes = Math.round(Number(data.minimum_free_space_gb || 0) * 1024 ** 3);
  data.max_log_file_size_bytes = Math.round(Number(data.max_log_file_size_mb || 0) * 1024 ** 2);
  data.pdi_volume_size_bytes = Math.round(Number(data.pdi_volume_size_gb || 0) * 1024 ** 3);
  data.web_bind_address = data.web_lan_enabled ? "0.0.0.0" : "127.0.0.1";
  delete data.minimum_free_space_gb;
  delete data.max_log_file_size_mb;
  delete data.pdi_volume_size_gb;
  delete data.web_lan_enabled;
  return data;
}

async function saveSettings(event) {
  event.preventDefault();
  const buttons = $$("button[type='submit']", $("#settings-form").parentElement);
  buttons.forEach((button) => { button.disabled = true; });
  setText("#settings-status", "正在保存…");
  try {
    const result = await api("/api/config", { method: "PUT", body: settingsPayload() });
    state.config = result.config || result;
    populateSettings(state.config);
    renderEnvironment({ ...state.bootstrap, config: state.config, web: result.web || state.bootstrap.web });
    setText("#settings-status", `${result.message || "设置已保存"}。正在运行的任务继续使用原配置快照。`);
    showToast("设置已保存");
  } catch (error) {
    setText("#settings-status", error.message);
    showAlert(error.message, "设置保存失败");
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function refreshTask() {
  try {
    const result = await api("/api/task");
    const task = result.task || result.active_task || (result.id ? result : null);
    if (task) renderTask(task);
  } catch (error) {
    if (error.status !== 404) console.warn("Task refresh failed", error);
  }
}

function setConnectionState(status, label) {
  const element = $("#connection-state");
  element.dataset.state = status;
  setText(element.lastElementChild, label);
}

function connectEvents() {
  closeEvents();
  setConnectionState("connecting", "正在连接");
  const source = new EventSource("/api/events/stream", { withCredentials: true });
  state.eventSource = source;
  source.onopen = () => setConnectionState("connected", "后台已连接");
  source.onerror = () => setConnectionState("disconnected", "连接中断，自动重连");
  source.onmessage = (event) => handleServerEvent(event);
  [
    "task", "task_started", "state", "progress", "pdi_progress", "pdi_finished",
    "verification_progress", "log", "health", "config", "license", "receiver",
  ].forEach((type) => {
    source.addEventListener(type, handleServerEvent);
  });
}

function closeEvents() {
  if (state.eventSource) state.eventSource.close();
  state.eventSource = null;
}

function handleServerEvent(event) {
  let payload;
  try {
    payload = JSON.parse(event.data);
  } catch (_) {
    if (event.data) addLog({ level: "INFO", source: "后台", message: event.data });
    return;
  }
  const type = payload.type || event.type;
  const data = payload.payload ?? payload.data ?? payload;
  if ([
    "task", "task_started", "state", "progress", "task_state", "task_progress",
    "pdi_progress", "pdi_finished", "verification_progress",
  ].includes(type)) {
    if (data.task?.id || data.task?.status) renderTask(data.task);
    else scheduleTaskRefresh();
  }
  else if (type === "log") addLog(data);
  else if (type === "license") renderLicense(data);
  else if (type === "config") {
    state.config = data.config || data;
    populateSettings(state.config);
  } else if (type === "health") renderHealth(data);
  else if (type === "receiver" && data.message) addLog({ ...data, source: data.source || "storescp" });
}

function scheduleTaskRefresh() {
  window.clearTimeout(state.refreshTimer);
  state.refreshTimer = window.setTimeout(refreshTask, 120);
}

async function openDirectoryDialog(target, purpose) {
  state.directoryTarget = target;
  state.directoryPurpose = purpose;
  state.currentDirectory = "";
  $("#directory-select-button").disabled = true;
  const dialog = $("#directory-dialog");
  const titles = {
    destination: "选择 DICOM 保存目录",
    pdi: "选择 PDI 输出目录",
    dcmtk: "选择 DCMTK bin 目录",
  };
  setText("#directory-dialog-title", titles[purpose] || "选择主机目录");
  dialog.showModal();
  await loadDirectories(target.value.trim());
}

async function loadDirectories(path = "") {
  const requestId = ++state.directoryRequestId;
  const dialog = $("#directory-dialog");
  const selectButton = $("#directory-select-button");
  state.currentDirectory = "";
  selectButton.disabled = true;
  dialog.setAttribute("aria-busy", "true");
  $("#directory-error").hidden = true;
  const query = new URLSearchParams({ purpose: state.directoryPurpose });
  if (path) query.set("path", path);
  try {
    const result = await api(`/api/files/directories?${query.toString()}`);
    if (requestId !== state.directoryRequestId) return;
    state.currentDirectory = result.path || result.current || path || "";
    $("#directory-current-path").value = state.currentDirectory;
    $("#directory-up-button").dataset.path = result.parent || "";
    renderDirectories(result.directories || result.items || result.children || []);
    selectButton.disabled = !state.currentDirectory;
  } catch (error) {
    if (requestId !== state.directoryRequestId) return;
    const message = $("#directory-error");
    message.textContent = error.message;
    message.hidden = false;
    $("#directory-current-path").value = "";
    renderDirectories([]);
  } finally {
    if (requestId === state.directoryRequestId) dialog.removeAttribute("aria-busy");
  }
}

function renderDirectories(directories) {
  const list = $("#directory-list");
  list.replaceChildren();
  if (!directories.length) {
    const empty = document.createElement("li");
    empty.className = "directory-empty";
    empty.textContent = "此目录没有可显示的子目录";
    list.append(empty);
    return;
  }
  directories.forEach((directory) => {
    const path = typeof directory === "string" ? directory : directory.path;
    const name = typeof directory === "string" ? directory.split(/[\\/]/).filter(Boolean).at(-1) || directory : directory.name || path;
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.path = path;
    const symbol = document.createElement("span");
    symbol.textContent = "▱";
    symbol.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.textContent = name;
    button.append(symbol, label);
    button.addEventListener("dblclick", () => loadDirectories(path));
    button.addEventListener("click", () => {
      $$("button", list).forEach((entry) => entry.removeAttribute("aria-selected"));
      button.setAttribute("aria-selected", "true");
      state.currentDirectory = path;
      $("#directory-current-path").value = path;
      $("#directory-select-button").disabled = false;
    });
    item.append(button);
    list.append(item);
  });
}

async function importAccessions(file) {
  if (!file) return;
  state.preflightOk = false;
  state.preflightSignature = "";
  resetPreflightChecks();
  updateStartAvailability();
  const extension = file.name.split(".").pop().toLowerCase();
  if (extension === "xlsx") {
    try {
      const result = await api("/api/files/accessions", {
        method: "POST",
        headers: {
          "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "X-File-Name": encodeURIComponent(file.name),
        },
        body: await file.arrayBuffer(),
      });
      const values = result.accessions || result.values || [];
      $("#accession-input").value = values.join("\n");
      parseAccessions($("#accession-input").value);
      showToast(`已从 ${file.name} 导入 ${values.length} 个检查号`);
    } catch (error) {
      showAlert(error.message, "XLSX 导入失败");
    }
    return;
  }
  try {
    const text = await file.text();
    $("#accession-input").value = text;
    parseAccessions(text);
    showToast(`已读取 ${file.name}`);
  } catch (error) {
    showAlert(error.message, "文件读取失败");
  }
}

async function runOperation(name, body = {}) {
  try {
    const result = await api(`/api/operations/${name}`, { method: "POST", body });
    showToast(result.message || "操作已完成");
    if (result.download_url) window.location.assign(result.download_url);
    return result;
  } catch (error) {
    showAlert(error.message, "运维操作失败");
    return null;
  }
}

async function shutdownService() {
  const button = $("#shutdown-service-button");
  button.disabled = true;
  try {
    const result = await api("/api/operations/shutdown", {
      method: "POST",
      body: {},
    });
    state.initialized = false;
    window.clearTimeout(state.preflightTimer);
    closeEvents();
    setConnectionState("disconnected", "后台已关闭");
    showToast(result.message || "DcmGet 后台已安全关闭，可以关闭浏览器页面。", 6000);
  } catch (error) {
    button.disabled = false;
    showAlert(error.message, "关闭后台失败");
  }
}

async function refreshOperations() {
  if (state.managerMode) {
    await Promise.allSettled([refreshProfiles(), refreshWindowsServiceStatus(), refreshReleaseNotes()]);
    return;
  }
  const refreshes = [refreshHealth(), refreshLicense(), refreshReleaseNotes(), refreshWindowsServiceStatus()];
  if (state.localSession || state.managerMode) refreshes.push(refreshProfiles());
  await Promise.allSettled(refreshes);
}

async function profileOperation(name, body = {}) {
  return api(`/api/operations/profile-${name}`, { method: "POST", body });
}

function profileActionButton(label, action, profile, disabled = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "button button--quiet button--small";
  button.textContent = label;
  button.disabled = disabled;
  button.addEventListener("click", () => action(profile));
  return button;
}

function profileIssues(profile) {
  const raw = profile.issues || profile.validation_errors || profile.errors || [];
  if (Array.isArray(raw)) return raw.map((item) => String(item?.message || item)).filter(Boolean);
  if (raw && typeof raw === "object") return Object.values(raw).map((item) => String(item?.message || item)).filter(Boolean);
  if (profile.validation_error) return [String(profile.validation_error)];
  if (profile.last_error) return [String(profile.last_error)];
  return [];
}

function profilePageUrl(profile, page = "home") {
  const url = new URL(window.location.href);
  url.port = String(profile.web_port);
  url.pathname = "/";
  url.search = "";
  url.searchParams.set("page", page);
  url.hash = "";
  return url.toString();
}

function openProfilePage(profile, page = "home") {
  window.open(profilePageUrl(profile, page), `_dcmget_profile_${profile.number}_${page}`, "noopener");
}

function profilePageButton(label, page, profile, primary = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${primary ? "button--primary" : "button--secondary"} button--small`;
  const icon = document.createElement("svg");
  icon.setAttribute("class", "icon");
  icon.setAttribute("aria-hidden", "true");
  const use = document.createElement("use");
  use.setAttribute("href", "#icon-external");
  icon.append(use);
  const text = document.createElement("span");
  text.textContent = label;
  button.append(icon, text);
  button.addEventListener("click", () => openProfilePage(profile, page));
  return button;
}

function profileFact(label, value) {
  const wrapper = document.createElement("div");
  const term = document.createElement("dt");
  const detail = document.createElement("dd");
  term.textContent = label;
  detail.textContent = value || "—";
  detail.title = value || "";
  wrapper.append(term, detail);
  return wrapper;
}

function updateManagerOverview(profiles) {
  if (!state.managerMode) return;
  const running = profiles.filter((profile) => profile.is_running).length;
  const issues = profiles.filter((profile) => profileIssues(profile).length).length;
  setText("#manager-profile-count", profiles.length);
  setText("#manager-running-count", running);
  setText("#manager-issue-count", issues);
}

function renderProfiles(profiles) {
  state.profiles = Array.isArray(profiles) ? profiles : [];
  const grid = $("#profile-grid");
  grid.replaceChildren();
  updateManagerOverview(state.profiles);
  if (!state.profiles.length) {
    const empty = document.createElement("div");
    empty.className = "profile-empty";
    empty.textContent = "尚未创建 Profile。";
    grid.append(empty);
    return;
  }
  state.profiles.forEach((profile) => {
    const issues = profileIssues(profile);
    const card = document.createElement("article");
    card.className = "profile-card";
    card.dataset.state = issues.length ? "issue" : (profile.is_running ? "running" : "idle");

    const header = document.createElement("div");
    header.className = "profile-card__header";
    const identity = document.createElement("div");
    identity.className = "profile-card__identity";
    const name = document.createElement("h3");
    name.textContent = profile.display_name || `Profile ${profile.number}`;
    const number = document.createElement("p");
    number.textContent = `Profile ${profile.number}`;
    identity.append(name, number);
    const status = document.createElement("span");
    const tone = issues.length ? "error" : (profile.is_running ? "success" : "neutral");
    status.className = `status-badge status-badge--${tone}`;
    status.textContent = issues.length ? "需要处理" : (profile.is_running ? "运行中" : "未启动");
    header.append(identity, status);

    const facts = document.createElement("dl");
    facts.className = "profile-card__facts";
    facts.append(
      profileFact("PACS", `${profile.pacs_server_ip || "—"}:${profile.pacs_server_port || "—"}`),
      profileFact("调用 / PACS AE", `${profile.calling_ae_title || "—"} / ${profile.pacs_ae_title || "—"}`),
      profileFact("DICOM 接收", `${profile.storage_ae_title || "—"}:${profile.storage_port || "—"}`),
      profileFact("Web 入口", `${window.location.hostname}:${profile.web_port || "—"}`),
    );

    card.append(header, facts);
    if (issues.length) {
      const issue = document.createElement("p");
      issue.className = "profile-card__issue";
      issue.textContent = issues[0];
      issue.title = issues.join("\n");
      card.append(issue);
    }

    const actions = document.createElement("div");
    actions.className = "profile-card__actions";
    if (profile.is_running) {
      actions.append(
        profilePageButton("任务", "home", profile, true),
        profilePageButton("设置", "settings", profile),
        profilePageButton("运维", "operations", profile),
      );
    } else {
      actions.append(profileActionButton(issues.length ? "修复配置" : "配置并启动", launchProfile, profile));
    }
    card.append(actions);

    const footer = document.createElement("div");
    footer.className = "profile-card__footer";
    const path = document.createElement("span");
    path.className = "profile-card__path";
    path.textContent = profile.destination_directory || "尚未设置保存目录";
    path.title = profile.destination_directory || "";
    const more = document.createElement("details");
    more.className = "profile-more";
    const summary = document.createElement("summary");
    summary.textContent = "更多";
    const menu = document.createElement("div");
    menu.className = "profile-more__menu";
    menu.append(
      profileActionButton("配置", configureProfile, profile, profile.is_running),
      profileActionButton("复制", cloneProfile, profile),
      profileActionButton("创建快捷方式", createProfileShortcut, profile),
      profileActionButton("删除", deleteProfile, profile, profile.is_running || profile.has_recovery),
    );
    more.append(summary, menu);
    footer.append(path, more);
    card.append(footer);
    grid.append(card);
  });
}

async function refreshProfiles() {
  try {
    const result = await profileOperation("list");
    renderProfiles(result.profiles || []);
    setText("#profile-management-hint", state.managerMode
      ? "页面链接自动使用当前 Windows 主机地址；修复配置后，kayisoft-dcmget 会重新尝试启动对应 Profile。"
      : "启动前可修改 PACS、AE、目录和端口；保存时会检查当前 Profile、其他 Profile及本机端口占用。");
  } catch (error) {
    renderProfiles([]);
    setText("#profile-management-hint", error.message);
  }
}

async function cloneProfile(profile) {
  const name = window.prompt("新 Profile 显示名称", `${profile.display_name || `Profile ${profile.number}`} 副本`);
  if (name == null) return;
  try {
    const result = await profileOperation("clone", {
      source_profile_number: profile.number,
      display_name: name,
    });
    await refreshProfiles();
    showToast(`已创建 Profile ${result.profile.number}；请确认参数后启动。`);
    openProfileConfig(result.profile, true);
  } catch (error) {
    showAlert(error.message, "复制 Profile 失败");
  }
}

async function renameProfile(profile) {
  const name = window.prompt("Profile 显示名称", profile.display_name || `Profile ${profile.number}`);
  if (name == null) return;
  try {
    await profileOperation("rename", { profile_number: profile.number, display_name: name });
    await refreshProfiles();
    showToast("Profile 已重命名");
  } catch (error) {
    showAlert(error.message, "重命名 Profile 失败");
  }
}

async function deleteProfile(profile) {
  await confirmAction(
    "删除 Profile",
    `将删除“${profile.display_name}”的配置，但不会删除已下载影像、PDI 或日志。确认继续吗？`,
    async () => {
      try {
        await profileOperation("delete", { profile_number: profile.number });
        await refreshProfiles();
        showToast("Profile 配置已删除");
      } catch (error) {
        showAlert(error.message, "删除 Profile 失败");
      }
    },
  );
}

async function launchProfile(profile) {
  openProfileConfig(profile, true);
}

function configureProfile(profile) {
  openProfileConfig(profile, false);
}

function openProfileWeb(profile) {
  openProfilePage(profile, "home");
}

function openProfileConfig(profile, launchAfterSave) {
  const dialog = $("#profile-config-dialog");
  const form = $("#profile-config-form");
  const fields = {
    profile_number: profile.number,
    display_name: profile.display_name || `Profile ${profile.number}`,
    dicom_destination_folder: profile.destination_directory || "",
    pacs_server_ip: profile.pacs_server_ip || "",
    pacs_server_port: profile.pacs_server_port || 104,
    calling_ae_title: profile.calling_ae_title || "DCMGET",
    pacs_ae_title: profile.pacs_ae_title || "PACS",
    storage_ae_title: profile.storage_ae_title || "DCMGET",
    storage_port: profile.storage_port || 6666,
    web_port: profile.web_port || 8787,
  };
  for (const [name, value] of Object.entries(fields)) {
    const input = form.elements.namedItem(name);
    if (input) input.value = String(value ?? "");
  }
  form.dataset.launchAfterSave = launchAfterSave ? "true" : "false";
  $("#profile-config-error").hidden = true;
  $("#profile-save-launch-button").hidden = !launchAfterSave;
  setText("#profile-config-title", launchAfterSave ? "配置并启动 Profile" : "配置 Profile");
  dialog.showModal();
  $("#profile-config-name").focus();
}

function profileConfigPayload() {
  const form = $("#profile-config-form");
  const body = {};
  for (const element of form.elements) {
    if (!element.name) continue;
    body[element.name] = element.value.trim();
  }
  for (const field of ["profile_number", "pacs_server_port", "storage_port", "web_port"]) {
    body[field] = Number.parseInt(body[field], 10);
  }
  if (!Number.isInteger(body.storage_port) || !Number.isInteger(body.web_port)) {
    throw new Error("SCP 接收端口和 Web 端口必须是整数。");
  }
  if (body.storage_port === body.web_port) {
    throw new Error("SCP 接收端口不能与 Web 端口相同。");
  }
  return body;
}

async function saveProfileConfiguration(launchAfterSave) {
  const form = $("#profile-config-form");
  if (!form.reportValidity()) return;
  const buttons = [$("#profile-save-button"), $("#profile-save-launch-button")];
  if (buttons.some((button) => button.disabled)) return;
  buttons.forEach((button) => { button.disabled = true; });
  form.setAttribute("aria-busy", "true");
  const error = $("#profile-config-error");
  error.hidden = true;
  try {
    const payload = profileConfigPayload();
    const result = await profileOperation("update", payload);
    if (launchAfterSave) {
      await profileOperation("launch", { profile_number: payload.profile_number });
      showToast(`Profile ${payload.profile_number} 已保存并启动`);
    } else {
      showToast(`Profile ${payload.profile_number} 配置已保存`);
    }
    $("#profile-config-dialog").close();
    await refreshProfiles();
  } catch (exception) {
    error.textContent = exception.message;
    error.hidden = false;
  } finally {
    form.removeAttribute("aria-busy");
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function createProfileShortcut(profile) {
  try {
    const result = await profileOperation("shortcut", {
      profile_number: profile.number,
      overwrite: false,
    });
    showToast(`Web 页面快捷方式已创建：${result.shortcut.path}`);
  } catch (error) {
    showAlert(error.message, "创建快捷方式失败");
  }
}

async function startAllProfiles() {
  const button = $("#start-all-profiles-button");
  button.disabled = true;
  try {
    const service = state.windowsService || await refreshWindowsServiceStatus();
    if (service?.supported && !["running", "starting"].includes(service.status)) {
      const startedService = await api("/api/operations/windows-service-start", { method: "POST", body: {} });
      state.windowsService = startedService;
      showToast("kayisoft-dcmget 服务启动命令已提交，将自动启动全部 Profile");
      window.setTimeout(refreshWindowsServiceStatus, 1000);
      return;
    }
    const result = await profileOperation("launch-all");
    const started = Number(result.started_count ?? result.started?.length ?? 0);
    const skipped = Number(result.skipped_count ?? result.skipped?.length ?? 0);
    showToast(`已启动 ${started} 个 Profile${skipped ? `，跳过 ${skipped} 个` : ""}`);
    window.setTimeout(refreshProfiles, 900);
  } catch (error) {
    showAlert(error.message, "启动全部 Profile 失败");
  } finally {
    button.disabled = false;
  }
}

async function refreshWindowsServiceStatus() {
  const badge = $("#windows-service-state");
  const stopButton = $("#stop-all-services-button");
  try {
    const result = await api("/api/operations/windows-service-status", { method: "POST", body: {} });
    state.windowsService = result;
    badge.hidden = !result.supported;
    stopButton.hidden = !result.supported;
    if (result.supported) {
      const status = normalizeStatus(result.status);
      setStatusBadge(badge, status, `kayisoft-dcmget · ${result.status_label || result.status}`);
    }
    return result;
  } catch (error) {
    state.windowsService = null;
    badge.hidden = false;
    stopButton.hidden = false;
    setStatusBadge(badge, "failed", "kayisoft-dcmget · 状态异常");
    return null;
  }
}

async function stopAllServices() {
  await confirmAction(
    "停止全部 DcmGet 服务",
    "将停止 kayisoft-dcmget 服务，以及其启动的全部 Profile、storescp、movescu 和 PDI 子进程。已下载文件和恢复点会保留。",
    async () => {
      try {
        const result = await runOperation("windows-service-stop");
        if (result?.ok) setConnectionState("disconnected", "全部服务正在停止");
        else if (result) showAlert(result.message || "当前环境不支持 Windows 服务控制。", "停止全部服务失败");
      } catch (error) {
        showAlert(error.message, "停止全部服务失败");
      }
    },
    { confirmLabel: "确认停止全部", tone: "danger" },
  );
}

async function refreshHealth() {
  try {
    const result = await api("/api/operations/health", { method: "POST", body: {} });
    renderHealth(result);
  } catch (error) {
    renderHealth({ checks: [{ name: "后台服务", ok: false, message: error.message }] });
  }
}

function renderHealth(payload) {
  const list = $("#health-list");
  list.replaceChildren();
  const checks = payload.checks || payload.items || [];
  if (!checks.length) {
    const item = document.createElement("li");
    item.className = "health-placeholder";
    item.textContent = payload.message || "未返回健康检查结果";
    list.append(item);
    return;
  }
  checks.forEach((check) => {
    const item = document.createElement("li");
    const ok = Boolean(check.ok ?? check.success ?? check.ready);
    const warning = check.severity === "warning" || check.warning === true;
    item.dataset.state = warning ? "warning" : ok ? "ok" : "error";
    const symbol = document.createElement("span");
    symbol.className = "health-symbol";
    symbol.textContent = warning ? "!" : ok ? "✓" : "×";
    const name = document.createElement("span");
    name.textContent = check.label || check.name || check.key || "检查项";
    const detail = document.createElement("small");
    detail.textContent = check.message || check.detail || (ok ? "正常" : "异常");
    item.append(symbol, name, detail);
    list.append(item);
  });
}

async function refreshLicense() {
  try {
    const result = await api("/api/license");
    renderLicense({
      ...(result.license || result),
      machine_code: result.machine_code || result.license?.machine_code,
    });
  } catch (_) {
    // The header already carries the last known state.
  }
}

async function activateLicense() {
  const token = $("#license-token-input").value.trim();
  if (!token) {
    showAlert("请先粘贴注册码。", "无法激活");
    return;
  }
  const button = $("#activate-license-button");
  button.disabled = true;
  try {
    const result = await api("/api/license/activate", {
      method: "POST",
      body: { token },
    });
    $("#license-token-input").value = "";
    renderLicense({ ...(result.license || {}), machine_code: result.machine_code });
    await refreshLicense();
    showToast("软件授权已激活");
  } catch (error) {
    showAlert(error.message, "授权激活失败");
  } finally {
    button.disabled = false;
  }
}

async function refreshReleaseNotes() {
  try {
    const result = await api("/api/operations/release-notes");
    renderReleaseNotes(result.releases || result.notes || []);
  } catch (_) {
    // Version details are useful but not required for task operation.
  }
}

function renderReleaseNotes(releases) {
  const root = $("#release-notes");
  root.replaceChildren();
  if (!Array.isArray(releases) || !releases.length) {
    const message = document.createElement("p");
    message.className = "muted";
    message.textContent = "暂无版本说明。";
    root.append(message);
    return;
  }
  releases.forEach((release, index) => {
    const details = document.createElement("details");
    details.open = index === 0;
    const heading = document.createElement("summary");
    heading.textContent = [release.version, release.date].filter(Boolean).join(" · ");
    const list = document.createElement("ul");
    (release.items || release.changes || []).forEach((text) => {
      const item = document.createElement("li");
      item.textContent = String(text);
      list.append(item);
    });
    details.append(heading, list);
    root.append(details);
  });
}

function bindEvents() {
  $("#dismiss-alert").addEventListener("click", clearAlert);
  $$(".nav-item[data-page]").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page)));

  $("#accession-input").addEventListener("input", () => {
    window.clearTimeout(state.accessionInputTimer);
    state.accessionInputTimer = window.setTimeout(() => parseAccessions($("#accession-input").value), 120);
  });
  $("#destination-input").addEventListener("input", invalidatePreflight);
  $("#quick-pdi-enabled").addEventListener("change", (event) => {
    $("#quick-pdi-folder-row").hidden = !event.target.checked;
    invalidatePreflight();
  });
  $("#setting-pdi-enabled").addEventListener("change", syncPdiSettingsUi);
  $("#quick-pdi-folder").addEventListener("input", invalidatePreflight);
  $("#accession-file").addEventListener("change", (event) => importAccessions(event.target.files[0]));

  const dropzone = $("#accession-dropzone");
  ["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add("is-dragover");
  }));
  ["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-dragover");
  }));
  dropzone.addEventListener("drop", (event) => importAccessions(event.dataTransfer.files[0]));

  $("#run-preflight-button").addEventListener("click", () => runPreflight());
  $("#toggle-task-editor").addEventListener("click", () => setTaskEditorCollapsed(!state.taskEditorCollapsed));
  $("#start-task-button").addEventListener("click", startTask);
  $("#pause-task-button").addEventListener("click", () => taskAction("pause"));
  $("#resume-task-button").addEventListener("click", () => taskAction("resume"));
  $("#retry-task-button").addEventListener("click", () => taskAction("retry"));
  $("#accept-partial-button").addEventListener("click", () => confirmAction(
    "接受已有文件",
    "将保留已经接收的文件，并把当前任务作为部分结果结束；尚未完成的检查号不会继续下载。确认接受吗？",
    () => taskAction("accept-partial"),
    { confirmLabel: "接受已有文件", tone: "primary" },
  ));
  $("#cancel-task-button").addEventListener("click", () => confirmAction(
    "停止当前任务",
    "将停止当前 movescu，但已经收到的文件和恢复点会保留。确认停止吗？",
    () => taskAction("cancel"),
  ));
  $("#new-task-button").addEventListener("click", () => {
    $("#accession-input").value = "";
    $("#quick-pdi-enabled").checked = Boolean(state.config.pdi_export_enabled);
    $("#quick-pdi-folder-row").hidden = !$("#quick-pdi-enabled").checked;
    parseAccessions("");
    resetPreflightChecks();
    showTaskEditor();
  });
  $("#open-pdi-button").addEventListener("click", () => runPdiAction(async () => {
    try {
      const result = await api("/api/pdi/open", { method: "POST", body: { task_id: state.task?.id } });
      showToast(result.message || "已在 DcmGet 主机打开 PDI 目录");
    } catch (error) {
      showAlert(error.message, "无法打开 PDI");
    }
  }));
  $("#verify-pdi-button").addEventListener("click", () => runPdiAction(async () => {
    try {
      const result = await api("/api/pdi/verify", { method: "POST", body: { task_id: state.task?.id } });
      showToast(result.message || (result.ok ? "PDI 校验通过" : "PDI 校验未通过"));
      if (!result.ok) showAlert(result.message || "PDI 校验未通过，请查看任务日志。", "PDI 校验异常");
    } catch (error) {
      showAlert(error.message, "PDI 校验失败");
    }
  }));
  $("#retry-pdi-button").addEventListener("click", () => runPdiAction(async () => {
    try {
      const result = await api("/api/pdi/retry", { method: "POST", body: { task_id: state.task?.id } });
      renderTask(result.task || result);
      showToast("已重新加入 PDI 导出队列");
    } catch (error) {
      showAlert(error.message, "PDI 重试失败");
    }
  }));

  $("#detailed-log-toggle").addEventListener("change", (event) => {
    state.showDetailedLogs = event.target.checked;
    setText("#log-filter-copy", state.showDetailedLogs ? "正在显示全部实时日志。" : "默认仅显示错误日志。");
    renderLogs();
  });
  $("#clear-logs-button").addEventListener("click", () => {
    state.logs = [];
    renderLogs();
  });
  $("#copy-errors-button").addEventListener("click", async () => {
    const text = visibleLogs().map((entry) => `${entry.timestamp} [${entry.source}] ${entry.level} ${entry.message}`).join("\n");
    if (!text) return showToast("当前没有可复制的日志");
    try {
      await navigator.clipboard.writeText(text);
      showToast("日志已复制");
    } catch (_) {
      showAlert("浏览器拒绝了剪贴板权限，请从主机日志目录复制。", "无法复制");
    }
  });

  $("#browse-destination").addEventListener("click", () => openDirectoryDialog($("#destination-input"), "destination"));
  $("#browse-pdi-folder").addEventListener("click", () => openDirectoryDialog($("#quick-pdi-folder"), "pdi"));
  $("#browse-dcmtk").addEventListener("click", () => openDirectoryDialog($("#dcmtk-bin-input"), "dcmtk"));
  $("#directory-up-button").addEventListener("click", (event) => loadDirectories(event.currentTarget.dataset.path));
  $("#directory-go-button").addEventListener("click", () => loadDirectories($("#directory-current-path").value.trim()));
  $("#directory-current-path").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      loadDirectories(event.currentTarget.value.trim());
    }
  });
  $("#directory-select-button").addEventListener("click", () => {
    if ($("#directory-select-button").disabled || !state.currentDirectory || !state.directoryTarget) return;
    state.directoryTarget.value = state.currentDirectory;
    state.directoryTarget.dispatchEvent(new Event("input", { bubbles: true }));
    $("#directory-dialog").close();
  });

  $("#settings-form").addEventListener("submit", saveSettings);
  $("#refresh-health-button").addEventListener("click", refreshHealth);
  $("#refresh-profiles-button").addEventListener("click", refreshProfiles);
  $("#start-all-profiles-button").addEventListener("click", startAllProfiles);
  $("#stop-all-services-button").addEventListener("click", stopAllServices);
  $("#profile-save-button").addEventListener("click", () => saveProfileConfiguration(false));
  $("#profile-save-launch-button").addEventListener("click", () => saveProfileConfiguration(true));
  $("#profile-config-form").addEventListener("submit", (event) => {
    event.preventDefault();
    saveProfileConfiguration($("#profile-config-form").dataset.launchAfterSave === "true");
  });
  $("#profile-config-cancel").addEventListener("click", () => $("#profile-config-dialog").close("cancel"));
  $("#activate-license-button").addEventListener("click", activateLicense);
  $("#copy-machine-code-button").addEventListener("click", async () => {
    const code = $("#license-machine-code").textContent.trim();
    if (!code || code === "—") return showToast("机器码尚未加载");
    try {
      await navigator.clipboard.writeText(code);
      showToast("机器码已复制");
    } catch (_) {
      showAlert("浏览器拒绝了剪贴板权限。", "无法复制");
    }
  });
  $("#shutdown-service-button").addEventListener("click", () => confirmAction(
    "关闭 DcmGet 后台",
    "后台关闭后，当前浏览器将无法继续操作；正在运行的下载会安全停止并保留恢复点。",
    shutdownService,
    { confirmLabel: "确认关闭", tone: "danger" },
  ));
  $$('[data-operation]').forEach((button) => button.addEventListener("click", () => runOperation(button.dataset.operation)));
  $("#open-destination-button").addEventListener("click", () => runOperation("open-destination"));
  $("#open-log-directory-button").addEventListener("click", () => runOperation("open-log-directory"));
  $("#buy-license-button").addEventListener("click", async () => {
    await refreshLicense();
    showPage("operations");
    showToast("授权状态已刷新；离线激活请使用产品授权文件。");
  });
  $("#copy-lan-url").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#lan-url").textContent);
      showToast("局域网地址已复制");
    } catch (_) {
      showAlert("浏览器拒绝了剪贴板权限。", "无法复制");
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !$("#app-shell").hidden) {
      if (state.managerMode) refreshOperations();
      else refreshTask();
    }
  });
  window.setInterval(() => {
    if (document.hidden) return;
    if (state.managerMode) {
      refreshProfiles();
      refreshWindowsServiceStatus();
    } else if (state.task && !TERMINAL_STATUSES.has(normalizeStatus(state.task.status))) {
      refreshTask();
    }
  }, 15000);
}

bindEvents();
resetPreflightChecks();
parseAccessions("");
loadBootstrap();
