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
  localSession: true,
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
    if (response.status === 401 && path !== "/api/login") showLogin(false);
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

function parseAccessions(raw) {
  const values = [];
  const seen = new Set();
  let blank = 0;
  let duplicate = 0;
  let invalid = 0;

  String(raw || "").split(/\r?\n/).forEach((line) => {
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
  invalidatePreflight();
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

function invalidatePreflight() {
  if (state.preflightSignature && state.preflightSignature !== draftSignature()) {
    state.preflightOk = false;
    state.preflightSignature = "";
    resetPreflightChecks();
  }
  updateStartAvailability();
}

function resetPreflightChecks() {
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

function renderPreflight(payload) {
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
  state.preflightSignature = allOk ? draftSignature() : "";
  setStatusBadge($("#task-readiness"), allOk ? "completed" : "failed", allOk ? "预检通过" : "预检未通过");
  updateStartAvailability();
}

async function runPreflight() {
  clearAlert();
  parseAccessions($("#accession-input").value);
  if (!state.parsedAccessions.length) {
    showAlert("请先输入至少一个有效检查号。", "无法预检");
    $("#accession-input").focus();
    return false;
  }
  if (!$("#destination-input").value.trim()) {
    showAlert("请选择运行 DcmGet 主机上的保存目录。", "无法预检");
    $("#destination-input").focus();
    return false;
  }

  const button = $("#run-preflight-button");
  button.disabled = true;
  button.textContent = "检查中…";
  try {
    const result = await api("/api/preflight", { method: "POST", body: taskDraft() });
    renderPreflight(result);
    if (!state.preflightOk) showAlert(result.message || "部分检查未通过，请根据红色项目修复后重试。", "预检未通过");
    return state.preflightOk;
  } catch (error) {
    renderPreflight({ ok: false });
    showAlert(error.message, "预检失败");
    return false;
  } finally {
    button.disabled = false;
    button.textContent = "重新检查";
  }
}

async function startTask() {
  if (!state.preflightOk || state.preflightSignature !== draftSignature()) {
    const ok = await runPreflight();
    if (!ok) return;
  }
  const button = $("#start-task-button");
  button.disabled = true;
  button.textContent = "正在创建…";
  try {
    const result = await api("/api/task/start", { method: "POST", body: taskDraft() });
    state.task = result.task || result;
    state.startedAt = Date.now();
    renderTask(state.task);
    showToast("任务已交给后台执行，关闭浏览器不会停止下载。");
  } catch (error) {
    showAlert(error.message, "任务启动失败");
    updateStartAvailability();
  } finally {
    button.textContent = "开始下载";
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
  try {
    const result = await api(`/api/task/${action}`, { method: "POST", body });
    state.task = result.task || result;
    renderTask(state.task);
  } catch (error) {
    showAlert(error.message, "任务操作失败");
  }
}

async function confirmAction(title, message, callback) {
  const dialog = $("#confirm-dialog");
  setText("#confirm-title", title);
  setText("#confirm-message", message);
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
  const logs = visibleLogs();
  const list = $("#log-list");
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
  if (list.lastElementChild) list.lastElementChild.scrollIntoView({ block: "nearest" });
}

function showPage(pageName) {
  $$("[data-page-panel]").forEach((panel) => {
    const active = panel.dataset.pagePanel === pageName;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  });
  $$(".nav-item[data-page]").forEach((button) => {
    const active = button.dataset.page === pageName;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  $("#main-content").focus({ preventScroll: true });
  if (pageName === "operations") refreshOperations();
}

function authState(payload) {
  return payload?.auth || payload?.authentication || payload || {};
}

function showLogin(firstRun = false, canSetupHere = true) {
  $("#app-shell").hidden = true;
  $("#login-screen").hidden = false;
  $("#login-error").hidden = true;
  const remoteSetupBlocked = firstRun && !canSetupHere;
  $("#login-form").hidden = remoteSetupBlocked;
  setText("#login-title", remoteSetupBlocked ? "等待本机初始化" : firstRun ? "设置首次访问密码" : "登录 DcmGet");
  setText("#login-hint", remoteSetupBlocked
    ? "首次密码只能在运行 DcmGet 的主机本机设置。请在主机打开此页面完成初始化，然后刷新本页。"
    : firstRun
      ? "这是首次启动。请设置仅用于当前 DcmGet Profile 的访问密码。"
      : "请输入管理员设置的访问密码。");
  $("#login-password").setAttribute("autocomplete", firstRun ? "new-password" : "current-password");
  $("#login-confirm-field").hidden = !firstRun;
  $("#login-password-confirm").required = Boolean(firstRun);
  $("#login-form").dataset.firstRun = firstRun ? "true" : "false";
  if (!remoteSetupBlocked) $("#login-password").focus();
}

function showApplication() {
  $("#login-screen").hidden = true;
  $("#app-shell").hidden = false;
}

async function login(event) {
  event.preventDefault();
  const password = $("#login-password").value;
  const firstRun = $("#login-form").dataset.firstRun === "true";
  const confirmPassword = $("#login-password-confirm").value;
  const error = $("#login-error");
  error.hidden = true;
  if (firstRun && password !== confirmPassword) {
    error.textContent = "两次输入的密码不一致。";
    error.hidden = false;
    $("#login-password-confirm").focus();
    return;
  }
  try {
    const result = await api(firstRun ? "/api/setup" : "/api/login", {
      method: "POST",
      body: { password, confirm_password: confirmPassword, setup: firstRun },
    });
    $("#login-password").value = "";
    $("#login-password-confirm").value = "";
    if (result.bootstrap) applyBootstrap(result.bootstrap);
    else await loadBootstrap();
  } catch (exception) {
    error.textContent = exception.message;
    error.hidden = false;
    $("#login-password").select();
  }
}

async function logout() {
  try {
    await api("/api/logout", { method: "POST", body: {} });
  } catch (_) {
    // A cleared or expired session has the same visible result.
  }
  closeEvents();
  showLogin(false);
}

function applyBootstrap(payload) {
  state.bootstrap = payload || {};
  state.csrfToken = payload.csrf_token || payload.csrfToken || state.csrfToken;
  const auth = authState(payload);
  const authenticated = auth.authenticated ?? payload.authenticated ?? false;
  const firstRun = auth.first_run || auth.requires_password_setup || auth.setup_required
    || payload.first_run || payload.setup_required;
  if (!authenticated) {
    const canSetupHere = auth.can_setup_here ?? payload.can_setup_here ?? true;
    showLogin(Boolean(firstRun), Boolean(canSetupHere));
    return;
  }

  showApplication();
  state.config = payload.config || {};
  populateSettings(state.config);
  $("#destination-input").value = state.config.dicom_destination_folder || "";
  renderEnvironment(payload);
  renderLicense(payload.license || {});
  const task = payload.task || payload.active_task;
  if (task) renderTask(task);
  else showTaskEditor();
  connectEvents();
}

async function loadBootstrap() {
  try {
    const payload = await api("/api/bootstrap");
    applyBootstrap(payload);
  } catch (error) {
    if (error.status === 401) return;
    showLogin(false);
    const loginError = $("#login-error");
    loginError.textContent = `无法连接 DcmGet 后台：${error.message}`;
    loginError.hidden = false;
  }
}

function renderEnvironment(payload) {
  const profile = payload.profile || {};
  const config = payload.config || {};
  const receiver = payload.receiver || {};
  const web = payload.web || {};
  state.localSession = web.local_session !== false;
  setText("#header-profile", `Profile ${profile.name || profile.id || payload.profile_name || "default"}`);
  setText("#header-pacs", `PACS ${config.pacs_server_ip || "—"}:${config.pacs_server_port || "—"} · ${config.pacs_ae_title || "—"}`);
  setText("#header-receiver", `接收 ${config.storage_ae_title || "—"}:${config.storage_port || "—"}`);
  setText("#operation-profile", profile.name || profile.id || payload.profile_name || "default");
  setText("#operation-data-dir", profile.data_dir || payload.data_dir);
  setText("#operation-version", payload.version || payload.app_version);
  setText("#operation-dcmtk-version", payload.dcmtk?.version || payload.dcmtk_version || receiver.dcmtk_version);

  const url = web.lan_url || web.url || payload.lan_url || window.location.origin;
  setText("#operation-web-url", url);
  setText("#lan-url", url);
  $("#lan-notice").hidden = !(web.lan_enabled ?? config.web_lan_enabled ?? payload.lan_enabled);
  $("#profile-management-card").hidden = !state.localSession;
  [
    "#open-destination-button",
    "#open-log-directory-button",
    '[data-operation="open-data-directory"]',
    '[data-operation="open-log-directory"]',
    '[data-operation="acceptance-report"]',
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
    web_auth_required: true,
    pdi_volume_size_gb: config.pdi_volume_size_gb ?? (Number(config.pdi_volume_size_bytes || 0) / 1024 ** 3),
    minimum_free_space_gb: config.minimum_free_space_gb ?? (Number(config.minimum_free_space_bytes || 0) / 1024 ** 3),
    max_log_file_size_mb: config.max_log_file_size_mb ?? (Number(config.max_log_file_size_bytes || 0) / 1024 ** 2),
  };
  for (const element of form.elements) {
    if (!element.name || !(element.name in derived)) continue;
    if (element.type === "checkbox") element.checked = Boolean(derived[element.name]);
    else element.value = derived[element.name] ?? "";
  }
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
  delete data.web_auth_required;
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
  const data = payload.data ?? payload.payload ?? payload;
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
  $("#directory-error").hidden = true;
  const query = new URLSearchParams({ purpose: state.directoryPurpose });
  if (path) query.set("path", path);
  try {
    const result = await api(`/api/files/directories?${query.toString()}`);
    state.currentDirectory = result.path || result.current || path || "";
    $("#directory-current-path").value = state.currentDirectory;
    $("#directory-up-button").dataset.path = result.parent || "";
    renderDirectories(result.directories || result.items || result.children || []);
  } catch (error) {
    const message = $("#directory-error");
    message.textContent = error.message;
    message.hidden = false;
    renderDirectories([]);
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
    });
    item.append(button);
    list.append(item);
  });
}

async function importAccessions(file) {
  if (!file) return;
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

async function refreshOperations() {
  const refreshes = [refreshHealth(), refreshLicense(), refreshReleaseNotes()];
  if (state.localSession) refreshes.push(refreshProfiles());
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

function renderProfiles(profiles) {
  const body = $("#profile-table-body");
  body.replaceChildren();
  if (!profiles.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.textContent = "尚未创建 Profile。";
    row.append(cell);
    body.append(row);
    return;
  }
  profiles.forEach((profile) => {
    const row = document.createElement("tr");
    const stateLabel = [profile.is_running ? "运行中" : "空闲", profile.has_recovery ? "有恢复任务" : ""].filter(Boolean).join(" · ");
    const values = [
      profile.display_name || `Profile ${profile.number}`,
      `${profile.storage_ae_title || "—"}:${profile.storage_port || "—"}`,
      profile.web_port || "—",
      profile.destination_directory || "—",
      stateLabel,
    ];
    values.forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = String(value);
      row.append(cell);
    });
    const actions = document.createElement("td");
    actions.className = "profile-actions";
    actions.append(
      profileActionButton("启动", launchProfile, profile),
      profileActionButton("复制", cloneProfile, profile),
      profileActionButton("重命名", renameProfile, profile),
      profileActionButton("快捷方式", createProfileShortcut, profile),
      profileActionButton("删除", deleteProfile, profile, profile.is_running || profile.has_recovery),
    );
    row.append(actions);
    body.append(row);
  });
}

async function refreshProfiles() {
  try {
    const result = await profileOperation("list");
    renderProfiles(result.profiles || []);
    setText("#profile-management-hint", "复制后请确认新接收 AE，并在 PACS 中同步 Move Destination 映射。");
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
    showToast(`已创建 Profile ${result.profile.number}；接收端口 ${result.recommended_port}，Web 端口 ${result.recommended_web_port}`);
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
  try {
    await profileOperation("launch", { profile_number: profile.number });
    showToast(`已在主机启动 Profile ${profile.number}`);
    window.setTimeout(refreshProfiles, 800);
  } catch (error) {
    showAlert(error.message, "启动 Profile 失败");
  }
}

async function createProfileShortcut(profile) {
  try {
    const result = await profileOperation("shortcut", {
      profile_number: profile.number,
      overwrite: false,
    });
    showToast(`快捷方式已创建：${result.shortcut.path}`);
  } catch (error) {
    showAlert(error.message, "创建快捷方式失败");
  }
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
  $("#login-form").addEventListener("submit", login);
  $("#logout-button").addEventListener("click", logout);
  $("#dismiss-alert").addEventListener("click", clearAlert);
  $$(".nav-item[data-page]").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page)));

  let inputTimer = 0;
  $("#accession-input").addEventListener("input", () => {
    window.clearTimeout(inputTimer);
    inputTimer = window.setTimeout(() => parseAccessions($("#accession-input").value), 120);
  });
  $("#destination-input").addEventListener("input", invalidatePreflight);
  $("#quick-pdi-enabled").addEventListener("change", (event) => {
    $("#quick-pdi-folder-row").hidden = !event.target.checked;
    invalidatePreflight();
  });
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

  $("#run-preflight-button").addEventListener("click", runPreflight);
  $("#start-task-button").addEventListener("click", startTask);
  $("#pause-task-button").addEventListener("click", () => taskAction("pause"));
  $("#resume-task-button").addEventListener("click", () => taskAction("resume"));
  $("#retry-task-button").addEventListener("click", () => taskAction("retry"));
  $("#accept-partial-button").addEventListener("click", () => taskAction("accept-partial"));
  $("#cancel-task-button").addEventListener("click", () => confirmAction(
    "停止当前任务",
    "将停止当前 movescu，但已经收到的文件和恢复点会保留。确认停止吗？",
    () => taskAction("cancel"),
  ));
  $("#new-task-button").addEventListener("click", () => {
    $("#accession-input").value = "";
    parseAccessions("");
    resetPreflightChecks();
    showTaskEditor();
  });
  $("#open-pdi-button").addEventListener("click", async () => {
    try {
      const result = await api("/api/pdi/open", { method: "POST", body: { task_id: state.task?.id } });
      showToast(result.message || "已在 DcmGet 主机打开 PDI 目录");
    } catch (error) {
      showAlert(error.message, "无法打开 PDI");
    }
  });
  $("#verify-pdi-button").addEventListener("click", async () => {
    try {
      const result = await api("/api/pdi/verify", { method: "POST", body: { task_id: state.task?.id } });
      showToast(result.message || (result.ok ? "PDI 校验通过" : "PDI 校验未通过"));
      if (!result.ok) showAlert(result.message || "PDI 校验未通过，请查看任务日志。", "PDI 校验异常");
    } catch (error) {
      showAlert(error.message, "PDI 校验失败");
    }
  });
  $("#retry-pdi-button").addEventListener("click", async () => {
    try {
      const result = await api("/api/pdi/retry", { method: "POST", body: { task_id: state.task?.id } });
      renderTask(result.task || result);
      showToast("已重新加入 PDI 导出队列");
    } catch (error) {
      showAlert(error.message, "PDI 重试失败");
    }
  });

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
    if (!state.currentDirectory || !state.directoryTarget) return;
    state.directoryTarget.value = state.currentDirectory;
    state.directoryTarget.dispatchEvent(new Event("input", { bubbles: true }));
    $("#directory-dialog").close();
  });

  $("#settings-form").addEventListener("submit", saveSettings);
  $("#refresh-health-button").addEventListener("click", refreshHealth);
  $("#refresh-profiles-button").addEventListener("click", refreshProfiles);
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
    if (!document.hidden && !$("#app-shell").hidden) refreshTask();
  });
  window.setInterval(() => {
    if (!document.hidden && state.task && !TERMINAL_STATUSES.has(normalizeStatus(state.task.status))) refreshTask();
  }, 15000);
}

bindEvents();
resetPreflightChecks();
parseAccessions("");
loadBootstrap();
