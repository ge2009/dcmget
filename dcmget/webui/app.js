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
  profileBootstrap: {},
  config: {},
  task: null,
  parsedAccessions: [],
  parseStats: { blank: 0, duplicate: 0, invalid: 0 },
  preflightOk: false,
  preflightSignature: "",
  csrfToken: "",
  eventSource: null,
  eventPollTimer: 0,
  eventCursor: "",
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
  profileGeneration: 0,
  profileAbortController: null,
  profileListRequestId: 0,
  activeProfileNumber: null,
  activeProfile: null,
  activeProfileRunning: false,
  openDrawer: "",
  lastFocusedBeforeDrawer: null,
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

class StaleProfileResponseError extends Error {
  constructor() {
    super("Profile 上下文已经切换");
    this.name = "StaleProfileResponseError";
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
    signal: options.signal,
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

function profileNumberOf(profile) {
  const value = profile?.number ?? profile?.profile_number ?? profile?.id;
  const number = Number.parseInt(value, 10);
  return Number.isInteger(number) ? number : null;
}

function profileDisplayName(profile) {
  const number = profileNumberOf(profile);
  return profile?.display_name || profile?.name || profile?.id || (number != null ? `Profile ${number}` : "当前 Profile");
}

function currentProfile() {
  return state.activeProfile || state.profileBootstrap.profile || state.bootstrap.profile || {};
}

function currentProfileNumber() {
  if (state.managerMode) return state.activeProfileNumber;
  return state.activeProfileNumber ?? profileNumberOf(currentProfile());
}

function currentProfileIssues(profile = currentProfile()) {
  const raw = profile?.issues || profile?.validation_errors || profile?.errors || [];
  if (Array.isArray(raw)) return raw.map((item) => String(item?.message || item)).filter(Boolean);
  if (raw && typeof raw === "object") return Object.values(raw).map((item) => String(item?.message || item)).filter(Boolean);
  if (profile?.validation_error) return [String(profile.validation_error)];
  if (profile?.last_error) return [String(profile.last_error)];
  return [];
}

function currentProfileCanRun() {
  return !currentProfileIssues().length;
}

function profileDesiredRunning(profile = currentProfile()) {
  return Boolean(profile?.is_running || profile?.desired_running);
}

function profileLifecycleState(profile = currentProfile()) {
  if (profile?.is_running) return "running";
  if (profile?.desired_running) return "starting";
  return "stopped";
}

function isManagedSelection() {
  return state.managerMode && currentProfileNumber() != null;
}

function managementProfilePath(profileNumber, suffix) {
  return `/api/management/profiles/${profileNumber}${suffix}`;
}

function isStaleProfileResponse(error) {
  return error?.name === "AbortError" || error instanceof StaleProfileResponseError;
}

function advanceProfileContext(profileNumber) {
  if (state.profileAbortController) state.profileAbortController.abort();
  state.profileGeneration += 1;
  state.profileAbortController = profileNumber == null ? null : new AbortController();
  state.eventCursor = "";
  window.clearTimeout(state.refreshTimer);
  window.clearTimeout(state.preflightTimer);
  window.clearTimeout(state.accessionInputTimer);
  state.preflightRequestId += 1;
  state.directoryRequestId += 1;
  state.preflightOk = false;
  state.preflightSignature = "";
  const preflightButton = $("#run-preflight-button");
  if (preflightButton) {
    preflightButton.disabled = false;
    setButtonLabel(preflightButton, "重新检查");
  }
  resetPreflightChecks();
  updateStartAvailability();
}

async function profileRequest(path, options = {}) {
  if (!state.managerMode) return api(path, options);
  if (!isManagedSelection()) throw new ApiError("请先选择一个 Profile。", 409);
  const profileNumber = currentProfileNumber();
  const generation = state.profileGeneration;
  const signal = state.profileAbortController?.signal;
  const apiPath = String(path || "").replace(/^\/?api\/?/, "").replace(/^\/+/, "");
  try {
    const result = await api(managementProfilePath(profileNumber, `/${apiPath}`), {
      ...options,
      signal: options.signal || signal,
    });
    if (generation !== state.profileGeneration || profileNumber !== currentProfileNumber()) {
      throw new StaleProfileResponseError();
    }
    return result;
  } catch (error) {
    if (
      isStaleProfileResponse(error)
      || generation !== state.profileGeneration
      || profileNumber !== currentProfileNumber()
    ) {
      throw new StaleProfileResponseError();
    }
    throw error;
  }
}

async function loadManagedProfileBootstrap(profileNumber) {
  if (profileNumber !== currentProfileNumber()) throw new StaleProfileResponseError();
  return profileRequest("/api/bootstrap");
}

async function loadManagedProfileSnapshot(profileNumber) {
  if (profileNumber !== currentProfileNumber()) throw new StaleProfileResponseError();
  return profileRequest("/api/snapshot");
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

const TOAST_ICONS = { success: "✓", error: "✕", warning: "!", info: "i" };

// options may be a type string ("success"|"error"|"warning"|"info"),
// a duration number, or an object { type, duration }.
function showToast(message, options = {}) {
  let opts = options;
  if (typeof options === "string") opts = { type: options };
  else if (typeof options === "number") opts = { duration: options };
  const type = TOAST_ICONS[opts.type] ? opts.type : "info";
  const duration = opts.duration ?? (type === "error" ? 5200 : 3200);
  const toast = document.createElement("div");
  toast.className = `toast toast--${type}`;
  toast.setAttribute("role", type === "error" ? "alert" : "status");
  const icon = document.createElement("span");
  icon.className = "toast__icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = TOAST_ICONS[type];
  const body = document.createElement("span");
  body.className = "toast__body";
  body.textContent = String(message);
  toast.append(icon, body);
  $("#toast-region").append(toast);
  window.setTimeout(() => toast.remove(), duration);
}

const THEME_STORAGE_KEY = "dcmget-theme";
const THEME_COLORS = { light: "#087481", dark: "#0e1518" };

function applyTheme(theme) {
  const resolved = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = resolved;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", THEME_COLORS[resolved]);
  const toggle = document.getElementById("theme-toggle");
  if (toggle) {
    const next = resolved === "dark" ? "浅色" : "深色";
    toggle.setAttribute("aria-label", `切换到${next}主题`);
    toggle.setAttribute("title", `切换到${next}主题`);
    toggle.setAttribute("aria-pressed", String(resolved === "dark"));
  }
}

function initTheme() {
  const bootstrap = document.documentElement.dataset.theme;
  applyTheme(bootstrap === "dark" ? "dark" : "light");
  const media = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
  if (media) {
    const onChange = (event) => {
      try {
        const forced = window.localStorage.getItem(THEME_STORAGE_KEY);
        if (forced === "light" || forced === "dark") return;
      } catch {
        // If localStorage is unavailable, continue to follow OS theme.
      }
      applyTheme(event.matches ? "dark" : "light");
    };
    if (media.addEventListener) media.addEventListener("change", onChange);
    else if (media.addListener) media.addListener(onChange);
  }
  const toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        window.localStorage.setItem(THEME_STORAGE_KEY, next);
      } catch {
        /* storage unavailable — theme still applies for this session */
      }
    });
  }
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
  return JSON.stringify({
    profile_number: state.managerMode ? currentProfileNumber() : null,
    task: taskDraft(),
  });
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
    const result = await profileRequest("/api/preflight", { method: "POST", body: taskDraft() });
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
  const signature = draftSignature();
  if (!state.preflightOk || state.preflightSignature !== signature) {
    showAlert("任务内容已经改变，请重新完成预检。", "无法开始下载");
    return;
  }
  const profile = currentProfile();
  const config = state.config || {};
  const profileName = profileDisplayName(profile);
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
    const result = await profileRequest("/api/task/start", {
      method: "POST",
      body: draft,
    });
    state.task = result.task || result;
    state.startedAt = Date.now();
    renderTask(state.task);
    showToast("任务已交给后台执行，关闭浏览器不会停止下载。", "success");
  } catch (error) {
    if (isStaleProfileResponse(error)) return;
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
  showTaskWorkspace();
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
  showTaskWorkspace();
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
    const result = await profileRequest(`/api/task/${action}`, { method: "POST", body });
    state.task = result.task || result;
    renderTask(state.task);
  } catch (error) {
    if (isStaleProfileResponse(error)) return;
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

function openDrawer(name) {
  const key = name === "settings" ? "settings" : "operations";
  const drawer = key === "settings" ? $("#settings-drawer") : $("#operations-drawer");
  if (!drawer) return;
  if (!state.openDrawer) state.lastFocusedBeforeDrawer = document.activeElement;
  state.openDrawer = key;
  $("#drawer-scrim").hidden = false;
  $("#settings-drawer").hidden = key !== "settings";
  $("#operations-drawer").hidden = key !== "operations";
  $("#app-shell").inert = true;
  document.body.classList.add("drawer-open");
  const target = key === "settings" ? $("#close-settings-drawer") : $("#close-operations-drawer");
  if (target) target.focus();
}

function closeDrawers() {
  state.openDrawer = "";
  $("#drawer-scrim").hidden = true;
  $("#settings-drawer").hidden = true;
  $("#operations-drawer").hidden = true;
  $("#app-shell").inert = false;
  document.body.classList.remove("drawer-open");
  if (state.lastFocusedBeforeDrawer instanceof HTMLElement) state.lastFocusedBeforeDrawer.focus();
  state.lastFocusedBeforeDrawer = null;
}

function showPage(pageName, { syncUrl = true } = {}) {
  const allowedPage = ["settings", "operations"].includes(pageName) ? pageName : "home";
  if (allowedPage === "settings") openDrawer("settings");
  else if (allowedPage === "operations") {
    openDrawer("operations");
    refreshOperations();
  } else {
    closeDrawers();
  }
  if (syncUrl) {
    const url = new URL(window.location.href);
    url.searchParams.set("page", allowedPage);
    window.history.replaceState({}, "", url);
  }
  if (allowedPage === "home") $("#main-content").focus({ preventScroll: true });
}

function trapDrawerFocus(event) {
  const drawer = state.openDrawer === "settings" ? $("#settings-drawer") : $("#operations-drawer");
  if (!drawer) return;
  const focusable = $$([
    "a[href]",
    "button:not([disabled])",
    "input:not([disabled])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    '[tabindex]:not([tabindex="-1"])',
  ].join(","), drawer).filter((element) => element.getClientRects().length > 0);
  if (!focusable.length) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && (document.activeElement === first || !drawer.contains(document.activeElement))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (document.activeElement === last || !drawer.contains(document.activeElement))) {
    event.preventDefault();
    first.focus();
  }
}

function showApplication() {
  $("#app-shell").hidden = false;
  const loading = document.getElementById("app-loading");
  if (loading) loading.hidden = true;
}

function setWorkspaceTaskVisibility(visible) {
  $("#workspace-task-stack").hidden = !visible;
  $("#profile-idle-state").hidden = visible;
}

function showIdleState(profile = currentProfile()) {
  setWorkspaceTaskVisibility(false);
  const issues = currentProfileIssues(profile);
  const name = profileDisplayName(profile);
  const starting = profileLifecycleState(profile) === "starting";
  setText("#profile-idle-title", starting ? `${name} 正在启动` : `${name} 当前未启动`);
  setText(
    "#profile-idle-message",
    issues.length
      ? `该 Profile 存在待修复配置：${issues[0]}`
      : starting
        ? "已记录运行选择，正在等待后台服务启动或恢复当前 Profile。"
      : "先启动当前 Profile，再在右侧提交下载任务、查看进度与处理错误。",
  );
  $("#idle-start-button").hidden = currentProfileNumber() == null || starting;
  $("#idle-configure-button").hidden = currentProfileNumber() == null;
}

function showProfileLoadingState(profile) {
  setWorkspaceTaskVisibility(false);
  setText("#profile-idle-title", `${profileDisplayName(profile)} 正在载入`);
  setText("#profile-idle-message", "正在同步该 Profile 的配置、任务和实时日志。");
  $("#idle-start-button").hidden = true;
  $("#idle-configure-button").hidden = true;
}

function showTaskWorkspace() {
  setWorkspaceTaskVisibility(true);
}

function applyProfileBootstrap(payload, { connectStream = true } = {}) {
  const nextBootstrap = payload || {};
  const profile = nextBootstrap.profile || currentProfile();
  const profileNumber = profileNumberOf(profile);
  if (state.managerMode && profileNumber !== state.activeProfileNumber) return;
  state.profileBootstrap = nextBootstrap;
  state.csrfToken = nextBootstrap.csrf_token || nextBootstrap.csrfToken || state.csrfToken;
  state.activeProfile = { ...state.activeProfile, ...profile };
  state.activeProfileNumber = profileNumber ?? state.activeProfileNumber;
  state.activeProfileRunning = profile.is_running !== false;
  state.config = payload.config || {};
  populateSettings(state.config);
  $("#destination-input").value = state.config.dicom_destination_folder || "";
  renderEnvironment(payload);
  renderLicense(payload.license || {});
  const task = payload.task || payload.active_task;
  if (task) renderTask(task);
  else showTaskEditor();
  if (connectStream) connectEvents();
  if (!state.task && $("#destination-input").value.trim()) schedulePreflight(0);
}

function applyManagerBootstrap(payload) {
  state.profileBootstrap = {};
  state.activeProfile = null;
  state.activeProfileNumber = null;
  state.activeProfileRunning = false;
  advanceProfileContext(null);
  renderEnvironment(payload);
  renderLicense(payload.license || {});
  $("#manager-overview").hidden = false;
  setText("#operations-eyebrow", "Windows 服务管理");
  setText("#operations-title", "DcmGet 管理中心");
  setText("#operations-description", "查看这台 Windows 主机上的 Profile，并切换当前 Profile。");
  setConnectionState("connected", "管理中心已连接");
  showIdleState({ display_name: "请选择 Profile" });
}

async function loadBootstrap() {
  try {
    const payload = await api("/api/bootstrap");
    state.bootstrap = payload || {};
    state.csrfToken = payload.csrf_token || payload.csrfToken || state.csrfToken;
    state.managerMode = payload.profile?.mode === "manager";
    document.body.classList.toggle("manager-mode", state.managerMode);
    showApplication();
    if (state.managerMode) {
      document.title = "DcmGet · Profile 管理";
      applyManagerBootstrap(payload);
      state.initialized = true;
      await refreshOperations();
      showPage(requestedPage("home"), { syncUrl: false });
      return;
    }
    state.activeProfile = payload.profile || {};
    state.activeProfileNumber = profileNumberOf(state.activeProfile);
    applyProfileBootstrap(payload);
    state.initialized = true;
    showPage(requestedPage("home"), { syncUrl: false });
  } catch (error) {
    showApplication();
    setConnectionState("disconnected", "后台连接失败");
    showAlert(`无法连接 DcmGet 后台：${error.message}`, "连接失败");
  }
}

function renderEnvironment(payload) {
  const profile = payload.profile || currentProfile();
  const baseConfig = payload.config || state.config || {};
  const config = state.managerMode && profileNumberOf(profile) != null
    ? {
        ...baseConfig,
        pacs_server_ip: profile.pacs_server_ip,
        pacs_server_port: profile.pacs_server_port,
        calling_ae_title: profile.calling_ae_title,
        pacs_ae_title: profile.pacs_ae_title,
        storage_ae_title: profile.storage_ae_title,
        storage_port: profile.storage_port,
        web_port: profile.web_port,
        dicom_destination_folder: profile.destination_directory,
      }
    : baseConfig;
  const receiver = payload.receiver || {};
  const web = payload.web || {};
  state.localSession = web.local_session !== false;
  const running = state.managerMode ? state.activeProfileRunning : profile.is_running !== false;
  const starting = state.managerMode && !running && profileDesiredRunning(profile);
  const issues = currentProfileIssues(profile);
  const displayName = profileDisplayName(profile);
  const version = payload.version || payload.app_version;
  const dcmtkVersion = payload.dcmtk?.version || payload.dcmtk_version || receiver.dcmtk_version;
  if (state.managerMode && !state.activeProfileNumber) {
    setText("#header-profile", "8786 Profile 管理");
    setText("#header-pacs", "查看并控制各个 Profile");
    setText("#header-receiver", `管理端口 ${profile.manager_port || window.location.port || "8786"}`);
  } else {
    setText("#header-profile", displayName);
    setText("#header-pacs", `PACS ${config.pacs_server_ip || "—"}:${config.pacs_server_port || "—"} · ${config.pacs_ae_title || "—"}`);
    setText("#header-receiver", `接收 ${config.storage_ae_title || "—"}:${config.storage_port || "—"}`);
  }
  setText("#workspace-profile-title", state.managerMode && !state.activeProfileNumber ? "请选择左侧 Profile" : displayName);
  setText(
    "#workspace-profile-subtitle",
    state.managerMode && !state.activeProfileNumber
      ? "先在左侧选中一个 Profile，任务、设置、PDI 和错误日志都会切换到当前上下文。"
      : running
        ? "当前 Profile 已就绪；任务、进度、PDI 与日志都在本页完成。"
        : starting
          ? "已记录运行选择，正在等待后台服务启动或恢复当前 Profile。"
        : "当前 Profile 默认不启动；先启动或修复配置，再开始下载任务。",
  );
  setStatusBadge(
    $("#workspace-profile-status"),
    running ? "completed" : starting ? "working" : issues.length ? "failed" : "stopped",
    running ? "已启动" : starting ? "启动中" : issues.length ? "待修复" : "未启动",
  );
  setText("#operation-profile", displayName);
  setText("#operation-data-dir", profile.data_dir || profile.destination_directory || payload.data_dir || config.dicom_destination_folder);
  setText("#operation-version", version);
  setText("#operation-version-copy", version);
  setText("#operation-dcmtk-version", dcmtkVersion);
  setText("#operation-dcmtk-version-copy", dcmtkVersion);

  const url = web.lan_url || web.url || payload.lan_url || window.location.origin;
  setText("#workspace-directory", profile.destination_directory || config.dicom_destination_folder);
  setText("#workspace-web-url", url);
  setText("#operation-web-url", url);
  setText("#lan-url", url);
  $("#lan-notice").hidden = !(web.lan_enabled ?? config.web_lan_enabled ?? payload.lan_enabled);
  $("#workspace-sidebar").hidden = !state.managerMode;
  $("#current-profile-start-button").hidden = !state.managerMode || currentProfileNumber() == null || running || starting;
  $("#current-profile-stop-button").hidden = !state.managerMode || currentProfileNumber() == null || (!running && !starting);
  $("#workspace-config-button").disabled = state.managerMode && currentProfileNumber() == null;
  $("#workspace-operations-button").disabled = false;
  $("#open-destination-button").disabled = state.managerMode && currentProfileNumber() == null;
  $("#open-log-directory-button").disabled = state.managerMode && currentProfileNumber() == null;
  $$('[data-operation]').forEach((control) => {
    control.disabled = state.managerMode && currentProfileNumber() == null;
  });
  [
    "#open-destination-button",
    "#open-log-directory-button",
    '[data-operation="open-data-directory"]',
    '[data-operation="open-log-directory"]',
    '[data-operation="acceptance-report"]',
  ].forEach((selector) => {
    const control = $(selector);
    if (control) control.hidden = !(state.localSession || state.managerMode);
  });
  const shutdownButton = $("#shutdown-service-button");
  if (shutdownButton) shutdownButton.hidden = state.managerMode || !state.localSession;
  if (!$("#destination-input").value) $("#destination-input").value = config.dicom_destination_folder || "";
  $("#quick-pdi-enabled").checked = Boolean(config.pdi_export_enabled);
  $("#quick-pdi-folder").value = config.pdi_output_folder || "";
  $("#quick-pdi-folder-row").hidden = !$("#quick-pdi-enabled").checked;
  if (state.managerMode && currentProfileNumber() != null && !running) showIdleState(profile);
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
    const result = await profileRequest("/api/config", { method: "PUT", body: settingsPayload() });
    state.config = result.config || result;
    populateSettings(state.config);
    renderEnvironment({
      ...state.profileBootstrap,
      config: state.config,
      web: result.web || state.profileBootstrap.web || state.bootstrap.web,
      profile: currentProfile(),
    });
    setText("#settings-status", `${result.message || "设置已保存"}。正在运行的任务继续使用原配置快照。`);
    showToast("设置已保存", "success");
  } catch (error) {
    if (isStaleProfileResponse(error)) return;
    setText("#settings-status", error.message);
    showAlert(error.message, "设置保存失败");
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function refreshTask() {
  if (state.managerMode && currentProfileNumber() == null) return;
  try {
    if (isManagedSelection()) {
      const snapshot = await loadManagedProfileSnapshot(currentProfileNumber());
      if (snapshot.config) {
        state.config = snapshot.config;
        populateSettings(state.config);
      }
      if (snapshot.profile || snapshot.web || snapshot.receiver || snapshot.license || snapshot.version) {
        renderEnvironment({
          ...state.profileBootstrap,
          ...snapshot,
          profile: { ...currentProfile(), ...(snapshot.profile || {}) },
        });
      }
      if (snapshot.license) renderLicense(snapshot.license);
      if (snapshot.health) renderHealth(snapshot.health);
      const task = snapshot.task || snapshot.active_task || (snapshot.id ? snapshot : null);
      if (task) renderTask(task);
      else showTaskEditor();
      return;
    }
    const result = await api("/api/task");
    const task = result.task || result.active_task || (result.id ? result : null);
    if (task) renderTask(task);
    else showTaskEditor();
  } catch (error) {
    if (isStaleProfileResponse(error)) return;
    if (error.status !== 404) console.warn("Task refresh failed", error);
  }
}

function setConnectionState(status, label) {
  const element = $("#connection-state");
  element.dataset.state = status;
  setText(element.lastElementChild, label);
}

function consumeServerPayload(type, data) {
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
    renderEnvironment({ ...state.profileBootstrap, config: state.config });
  } else if (type === "health") renderHealth(data);
  else if (type === "receiver" && data.message) addLog({ ...data, source: data.source || "storescp" });
}

function scheduleManagedEventPoll(delay = 1500) {
  window.clearTimeout(state.eventPollTimer);
  state.eventPollTimer = window.setTimeout(pollManagedEvents, delay);
}

async function pollManagedEvents() {
  if (!isManagedSelection() || !state.activeProfileRunning) return;
  const query = new URLSearchParams();
  if (state.eventCursor) query.set("after_id", state.eventCursor);
  try {
    const result = await profileRequest(`/api/events${query.toString() ? `?${query.toString()}` : ""}`);
    const events = result.events || result.items || result.records || [];
    events.forEach((entry) => {
      const type = entry.type || entry.event_type || entry.name || "message";
      const data = entry.payload ?? entry.data ?? entry;
      if (entry.id != null) state.eventCursor = String(entry.id);
      consumeServerPayload(type, data);
    });
    if (result.last_id != null) state.eventCursor = String(result.last_id);
    setConnectionState("connected", "当前 Profile 已同步");
    scheduleManagedEventPoll(events.length ? 350 : 1500);
  } catch (error) {
    if (isStaleProfileResponse(error)) return;
    setConnectionState("disconnected", "同步中断，自动重试");
    scheduleManagedEventPoll(2500);
  }
}

function connectEvents() {
  closeEvents();
  if (state.managerMode && (!state.activeProfileRunning || currentProfileNumber() == null)) {
    setConnectionState("connected", "等待选择已启动的 Profile");
    return;
  }
  setConnectionState("connecting", "正在连接");
  if (isManagedSelection()) {
    state.eventCursor = "";
    scheduleManagedEventPoll(0);
    return;
  }
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
  window.clearTimeout(state.eventPollTimer);
  state.eventPollTimer = 0;
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
  consumeServerPayload(type, data);
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
    const result = await profileRequest(`/api/files/directories?${query.toString()}`);
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
      const result = await profileRequest("/api/files/accessions", {
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
      showToast(`已从 ${file.name} 导入 ${values.length} 个检查号`, "success");
    } catch (error) {
      if (isStaleProfileResponse(error)) return;
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
    showToast(result.message || "操作已完成", "success");
    if (result.download_url) window.location.assign(result.download_url);
    return result;
  } catch (error) {
    showAlert(error.message, "运维操作失败");
    return null;
  }
}

async function runScopedOperation(name, body = {}) {
  try {
    const result = await profileRequest(`/api/operations/${name}`, { method: "POST", body });
    showToast(result.message || "操作已完成", "success");
    if (result.download_url) window.location.assign(result.download_url);
    return result;
  } catch (error) {
    if (isStaleProfileResponse(error)) return null;
    showAlert(error.message, "当前 Profile 操作失败");
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
    showToast(result.message || "DcmGet 后台已安全关闭，可以关闭浏览器页面。", { type: "success", duration: 6000 });
  } catch (error) {
    button.disabled = false;
    showAlert(error.message, "关闭后台失败");
  }
}

async function refreshOperations() {
  if (state.managerMode) {
    await Promise.allSettled([refreshProfiles(), refreshHealth(), refreshLicense(), refreshWindowsServiceStatus(), refreshReleaseNotes()]);
    return;
  }
  const refreshes = [refreshHealth(), refreshLicense(), refreshReleaseNotes(), refreshWindowsServiceStatus()];
  await Promise.allSettled(refreshes);
}

async function profileOperation(name, body = {}) {
  if (state.managerMode && name === "list") return api("/api/management/profiles");
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
  return currentProfileIssues(profile);
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

async function selectManagedProfile(profile, { preserveLogs = false } = {}) {
  if (!profile) return;
  const profileNumber = profileNumberOf(profile);
  if (profileNumber == null) return;
  if (state.openDrawer) closeDrawers();
  state.activeProfile = profile;
  state.activeProfileNumber = profileNumber;
  state.activeProfileRunning = Boolean(profile.is_running);
  state.task = null;
  advanceProfileContext(profileNumber);
  renderProfiles(state.profiles);
  clearAlert();
  if (!preserveLogs) {
    state.logs = [];
    renderLogs();
  }
  closeEvents();
  if (!state.activeProfileRunning) {
    state.profileBootstrap = { profile };
    state.task = null;
    renderEnvironment({ ...state.bootstrap, profile, web: {} });
    setConnectionState(
      "connected",
      profileDesiredRunning(profile) ? "Profile 正在启动" : "Profile 未启动",
    );
    showIdleState(profile);
    return;
  }
  showProfileLoadingState(profile);
  try {
    const payload = await loadManagedProfileBootstrap(profileNumber);
    applyProfileBootstrap({
      ...payload,
      profile: { ...profile, ...(payload.profile || {}) },
    });
  } catch (error) {
    if (isStaleProfileResponse(error)) return;
    state.activeProfileRunning = false;
    renderEnvironment({ ...state.bootstrap, profile, web: {} });
    setConnectionState("disconnected", "Profile 载入失败");
    showIdleState(profile);
    showAlert(error.message, "Profile 加载失败");
  }
}

function clearManagedProfileSelection() {
  closeEvents();
  state.activeProfile = null;
  state.activeProfileNumber = null;
  state.activeProfileRunning = false;
  state.profileBootstrap = {};
  state.config = {};
  state.task = null;
  state.startedAt = 0;
  state.logs = [];
  advanceProfileContext(null);
  renderLogs();
  $("#accession-input").value = "";
  $("#destination-input").value = "";
  $("#quick-pdi-enabled").checked = false;
  $("#quick-pdi-folder").value = "";
  $("#quick-pdi-folder-row").hidden = true;
  parseAccessions("", { schedule: false });
  ["#directory-dialog", "#confirm-dialog", "#profile-config-dialog"].forEach((selector) => {
    const dialog = $(selector);
    if (dialog.open) dialog.close("cancel");
  });
  if (state.openDrawer) closeDrawers();
  renderEnvironment({ ...state.bootstrap, profile: state.bootstrap.profile || {}, web: state.bootstrap.web || {} });
  showIdleState({ display_name: "尚无可用 Profile" });
  setConnectionState("connected", "等待创建 Profile");
}

async function startManagedProfile(profile = currentProfile()) {
  const profileNumber = profileNumberOf(profile);
  if (profileNumber == null) return;
  if (profileIssues(profile).length) {
    openProfileConfig(profile, true);
    return;
  }
  try {
    profile.desired_running = true;
    if (profileNumber === state.activeProfileNumber) state.activeProfile = profile;
    renderProfiles(state.profiles);
    renderEnvironment({ ...state.bootstrap, profile, web: {} });
    const result = await api(managementProfilePath(profileNumber, "/start"), { method: "POST", body: {} });
    showToast(result.message || `${profileDisplayName(profile)} 启动命令已提交`, "success");
    await refreshProfiles();
    window.setTimeout(() => refreshProfiles(), 900);
  } catch (error) {
    await refreshProfiles();
    showAlert(error.message, "启动 Profile 失败");
  }
}

async function stopManagedProfileNow(profile = currentProfile()) {
  const profileNumber = profileNumberOf(profile);
  if (profileNumber == null) return null;
  profile.desired_running = false;
  if (profileNumber === state.activeProfileNumber) state.activeProfile = profile;
  const result = await api(managementProfilePath(profileNumber, "/stop"), { method: "POST", body: {} });
  await refreshProfiles();
  return result;
}

async function stopManagedProfile(profile = currentProfile()) {
  const profileNumber = profileNumberOf(profile);
  if (profileNumber == null) return;
  await confirmAction(
    "停止当前 Profile",
    `将停止 ${profileDisplayName(profile)} 的接收端、下载进程与 PDI 子任务，但保留已下载文件和恢复点。确认停止吗？`,
    async () => {
      try {
        const result = await stopManagedProfileNow(profile);
        showToast(result?.message || `${profileDisplayName(profile)} 已停止`, "success");
      } catch (error) {
        await refreshProfiles();
        showAlert(error.message, "停止 Profile 失败");
      }
    },
  );
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
    const lifecycle = profileLifecycleState(profile);
    const selected = profileNumberOf(profile) === state.activeProfileNumber;
    const card = document.createElement("article");
    card.className = "profile-card";
    card.dataset.state = issues.length ? "issue" : lifecycle;
    card.classList.toggle("is-selected", selected);

    const header = document.createElement("div");
    header.className = "profile-card__header";
    const identity = document.createElement("div");
    identity.className = "profile-card__identity";
    const name = document.createElement("h3");
    name.textContent = profile.display_name || `Profile ${profile.number}`;
    const number = document.createElement("p");
    number.textContent = `Profile ${profile.number}`;
    identity.append(name, number);
    const status = document.createElement("button");
    const tone = issues.length ? "error" : lifecycle === "running" ? "success" : lifecycle === "starting" ? "working" : "neutral";
    status.type = "button";
    status.className = `status-badge status-badge--${tone} profile-card__switch`;
    status.textContent = selected
      ? "当前上下文"
      : lifecycle === "running"
        ? "切换到此"
        : lifecycle === "starting"
          ? "启动中"
          : "未启动";
    status.addEventListener("click", () => selectManagedProfile(profile, { preserveLogs: selected }));
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
    if (lifecycle === "running") {
      actions.append(
        profileActionButton(selected ? "当前 Profile" : "切换", selectManagedProfile, profile, selected),
        profileActionButton("停止", stopManagedProfile, profile),
        profileActionButton("配置", requestManagedProfileConfiguration, profile),
      );
    } else if (lifecycle === "starting") {
      actions.append(
        profileActionButton(selected ? "当前 Profile" : "切换", selectManagedProfile, profile, selected),
        profileActionButton("取消启动", stopManagedProfile, profile),
      );
    } else {
      actions.append(
        profileActionButton(issues.length ? "修复配置" : "启动", launchProfile, profile),
        profileActionButton("配置", requestManagedProfileConfiguration, profile),
      );
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
      profileActionButton("配置", requestManagedProfileConfiguration, profile),
      profileActionButton("复制", cloneProfile, profile),
      profileActionButton("创建快捷方式", createProfileShortcut, profile),
      profileActionButton("删除", deleteProfile, profile, profileDesiredRunning(profile) || profile.has_recovery),
    );
    more.append(summary, menu);
    footer.append(path, more);
    card.append(footer);
    grid.append(card);
  });
}

async function refreshProfiles() {
  if (!state.managerMode) return;
  const requestId = ++state.profileListRequestId;
  try {
    const result = await profileOperation("list");
    if (requestId !== state.profileListRequestId) return;
    const profiles = result.profiles || result.items || result;
    renderProfiles(profiles || []);
    setText("#profile-management-hint", "通过 8786 页面切换 Profile；右侧显示所选 Profile 的任务、设置与维护操作。");
    if (!state.profiles.length) {
      clearManagedProfileSelection();
      return;
    }
    const nextProfile = state.profiles.find((profile) => profileNumberOf(profile) === state.activeProfileNumber)
      || state.profiles.find((profile) => profile.is_running)
      || state.profiles.find((profile) => profile.desired_running)
      || state.profiles[0];
    if (!state.activeProfileNumber || profileNumberOf(nextProfile) !== state.activeProfileNumber) {
      await selectManagedProfile(nextProfile);
      return;
    }
    const wasRunning = state.activeProfileRunning;
    state.activeProfile = nextProfile;
    state.activeProfileRunning = Boolean(nextProfile.is_running);
    if (wasRunning !== state.activeProfileRunning) {
      await selectManagedProfile(nextProfile, { preserveLogs: true });
      return;
    }
    renderProfiles(state.profiles);
    if (!state.activeProfileRunning) {
      renderEnvironment({ ...state.bootstrap, profile: nextProfile, web: {} });
      showIdleState(nextProfile);
    }
  } catch (error) {
    if (requestId !== state.profileListRequestId) return;
    if (!state.profiles.length) {
      const errorState = document.createElement("div");
      errorState.className = "profile-empty profile-empty--error";
      errorState.textContent = `Profile 列表读取失败：${error.message}`;
      $("#profile-grid").replaceChildren(errorState);
    }
    setText("#profile-management-hint", `刷新失败，已保留上次状态：${error.message}`);
  }
}

async function createProfile() {
  if (!state.managerMode) return;
  try {
    const result = await api("/api/management/profiles", { method: "POST", body: {} });
    await refreshProfiles();
    if (result.profile) {
      showToast(`已创建 ${profileDisplayName(result.profile)}；当前默认处于停止状态。`, "success");
      await selectManagedProfile(result.profile);
      openProfileConfig(result.profile, false);
    } else {
      showToast("新 Profile 已创建；请补充配置后再启动。", "success");
    }
  } catch (error) {
    showAlert(error.message, "新建 Profile 失败");
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
    showToast(`已创建 Profile ${result.profile.number}；请确认参数后启动。`, "success");
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
    showToast("Profile 已重命名", "success");
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
        showToast("Profile 配置已删除", "success");
      } catch (error) {
        showAlert(error.message, "删除 Profile 失败");
      }
    },
  );
}

async function launchProfile(profile) {
  if (state.managerMode) {
    await startManagedProfile(profile);
    return;
  }
  openProfileConfig(profile, true);
}

function configureProfile(profile) {
  openProfileConfig(profile, false);
}

async function requestManagedProfileConfiguration(profile = currentProfile()) {
  if (!state.managerMode || !profileDesiredRunning(profile)) {
    configureProfile(profile);
    return;
  }
  await confirmAction(
    "需要先停止 Profile",
    "修改 AE、端口或保存目录前，需要停止当前接收服务。未完成任务会保留恢复点；确认后将等待 Web 与 DICOM 接收端口全部释放。",
    async () => {
      try {
        const profileNumber = profileNumberOf(profile);
        const result = await stopManagedProfileNow(profile);
        const latest = state.profiles.find(
          (item) => profileNumberOf(item) === profileNumber,
        ) || profile;
        showToast(result?.message || `${profileDisplayName(profile)} 已停止`, "success");
        configureProfile(latest);
      } catch (error) {
        await refreshProfiles();
        showAlert(error.message, "停止 Profile 失败");
      }
    },
    { confirmLabel: "停止并修改", tone: "primary" },
  );
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
      showToast(`Profile ${payload.profile_number} 已保存并启动`, "success");
    } else {
      showToast(`Profile ${payload.profile_number} 配置已保存`, "success");
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
    showToast(`Web 页面快捷方式已创建：${result.shortcut.path}`, "success");
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
      showToast("kayisoft-dcmget 服务启动命令已提交，将自动启动全部 Profile", "success");
      window.setTimeout(refreshWindowsServiceStatus, 1000);
      return;
    }
    const result = await profileOperation("launch-all");
    const started = Number(result.started_count ?? result.started?.length ?? 0);
    const skipped = Number(result.skipped_count ?? result.skipped?.length ?? 0);
    showToast(`已启动 ${started} 个 Profile${skipped ? `，跳过 ${skipped} 个` : ""}`, "success");
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
  if (state.managerMode && !state.activeProfileRunning) return;
  try {
    const result = await profileRequest("/api/license");
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
    if (state.managerMode && !state.activeProfileRunning) {
      throw new Error("请先启动一个 Profile，再进行软件授权。");
    }
    const result = await profileRequest("/api/license/activate", {
      method: "POST",
      body: { token },
    });
    $("#license-token-input").value = "";
    renderLicense({ ...(result.license || {}), machine_code: result.machine_code });
    await refreshLicense();
    showToast("软件授权已激活", "success");
  } catch (error) {
    if (isStaleProfileResponse(error)) return;
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
  $("#workspace-config-button").addEventListener("click", () => {
    if (state.managerMode && currentProfileNumber() == null) return;
    if (state.managerMode) {
      requestManagedProfileConfiguration(currentProfile());
      return;
    }
    showPage("settings");
  });
  $("#workspace-operations-button").addEventListener("click", () => showPage("operations"));
  $("#close-settings-drawer").addEventListener("click", closeDrawers);
  $("#close-operations-drawer").addEventListener("click", closeDrawers);
  $("#drawer-scrim").addEventListener("click", closeDrawers);
  $("#current-profile-start-button").addEventListener("click", () => startManagedProfile());
  $("#current-profile-stop-button").addEventListener("click", () => stopManagedProfile());
  $("#idle-start-button").addEventListener("click", () => startManagedProfile());
  $("#idle-configure-button").addEventListener("click", () => configureProfile(currentProfile()));
  $("#create-profile-button").addEventListener("click", createProfile);

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
      const result = await profileRequest("/api/pdi/open", { method: "POST", body: { task_id: state.task?.id } });
      showToast(result.message || "已在 DcmGet 主机打开 PDI 目录", "success");
    } catch (error) {
      if (isStaleProfileResponse(error)) return;
      showAlert(error.message, "无法打开 PDI");
    }
  }));
  $("#verify-pdi-button").addEventListener("click", () => runPdiAction(async () => {
    try {
      const result = await profileRequest("/api/pdi/verify", { method: "POST", body: { task_id: state.task?.id } });
      showToast(result.message || (result.ok ? "PDI 校验通过" : "PDI 校验未通过"), result.ok ? "success" : "warning");
      if (!result.ok) showAlert(result.message || "PDI 校验未通过，请查看任务日志。", "PDI 校验异常");
    } catch (error) {
      if (isStaleProfileResponse(error)) return;
      showAlert(error.message, "PDI 校验失败");
    }
  }));
  $("#retry-pdi-button").addEventListener("click", () => runPdiAction(async () => {
    try {
      const result = await profileRequest("/api/pdi/retry", { method: "POST", body: { task_id: state.task?.id } });
      renderTask(result.task || result);
      showToast("已重新加入 PDI 导出队列", "success");
    } catch (error) {
      if (isStaleProfileResponse(error)) return;
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
      showToast("日志已复制", "success");
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
    if (!code || code === "—") return showToast("机器码尚未加载", "warning");
    try {
      await navigator.clipboard.writeText(code);
      showToast("机器码已复制", "success");
    } catch (_) {
      showAlert("浏览器拒绝了剪贴板权限。", "无法复制");
    }
  });
  $("#shutdown-service-button").addEventListener("click", () => {
    if (state.managerMode) {
      stopManagedProfile();
      return;
    }
    confirmAction(
      "关闭 DcmGet 后台",
      "后台关闭后，当前浏览器将无法继续操作；正在运行的下载会安全停止并保留恢复点。",
      shutdownService,
      { confirmLabel: "确认关闭", tone: "danger" },
    );
  });
  $$('[data-operation]').forEach((button) => button.addEventListener("click", () => {
    const operation = state.managerMode ? runScopedOperation : runOperation;
    operation(button.dataset.operation);
  }));
  $("#open-destination-button").addEventListener("click", () => {
    if (state.managerMode) {
      if (currentProfileNumber() != null) runScopedOperation("open-destination");
      return;
    }
    runOperation("open-destination");
  });
  $("#open-log-directory-button").addEventListener("click", () => {
    if (state.managerMode) {
      if (currentProfileNumber() != null) runScopedOperation("open-log-directory");
      return;
    }
    runOperation("open-log-directory");
  });
  $("#buy-license-button").addEventListener("click", async () => {
    await refreshLicense();
    showPage("operations");
    showToast("授权状态已刷新；离线激活请使用产品授权文件。", "success");
  });
  $("#copy-lan-url").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#lan-url").textContent);
      showToast("局域网地址已复制", "success");
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
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.openDrawer) closeDrawers();
    else if (event.key === "Tab" && state.openDrawer) trapDrawerFocus(event);
  });
  window.setInterval(() => {
    if (document.hidden) return;
    if (state.managerMode) {
      refreshProfiles();
      refreshWindowsServiceStatus();
      if (state.activeProfileRunning) refreshTask();
    } else if (state.task && !TERMINAL_STATUSES.has(normalizeStatus(state.task.status))) {
      refreshTask();
    }
  }, 15000);
}

initTheme();
bindEvents();
resetPreflightChecks();
parseAccessions("");
loadBootstrap();
