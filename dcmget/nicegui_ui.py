"""NiceGUI workspace for the DcmGet FastAPI application.

The UI intentionally talks to the existing JSON API from the browser.  This
keeps the API's cookie, Origin and CSRF checks authoritative and avoids adding
a second application state machine inside NiceGUI.
"""

from __future__ import annotations

import inspect
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from fastapi import Request
from nicegui import ui

from .accession_import import (
    AccessionImportError,
    ColumnSelectionError,
    ImportColumn,
    import_accession_file,
)
from .config import parse_accessions


DETAIL_LIMIT = 200
ACTIVE_STATUSES = {
    "queued",
    "preflight",
    "starting",
    "starting_receiver",
    "running",
    "downloading",
}
RESUMABLE_STATUSES = {
    "pause_pending",
    "paused",
    "interrupted",
    "download_retryable",
}
TERMINAL_STATUSES = {
    "completed",
    "failed",
    "partial",
    "partial_success",
    "cancelled",
    "pdi_completed",
}

STATUS_LABELS = {
    "idle": "等待任务",
    "queued": "已排队",
    "preflight": "正在预检",
    "starting": "正在启动",
    "starting_receiver": "正在启动接收器",
    "running": "正在运行",
    "downloading": "正在下载",
    "pause_pending": "正在暂停",
    "paused": "已暂停",
    "interrupted": "任务中断",
    "download_retryable": "可重试",
    "completed": "已完成",
    "failed": "失败",
    "partial": "部分完成",
    "partial_success": "部分完成",
    "cancelled": "已取消",
    "pdi_pending": "PDI 等待生成",
    "pdi_running": "PDI 生成中",
    "pdi_retryable": "PDI 可重试",
    "pdi_completed": "PDI 已完成",
}

STATUS_MESSAGES = {
    "starting_receiver": "正在启动 DICOM 接收器",
    "downloading": "正在接收影像",
    "pause_pending": "当前检查号完成后暂停",
    "paused": "下载已暂停，可以随时继续",
    "interrupted": "下载已安全中断，可以继续",
    "download_retryable": "部分检查号未完成，可以重试失败项",
}


THEME_BOOTSTRAP = r"""
<meta name="color-scheme" content="light dark">
<script data-dcmget-theme-bootstrap>
(() => {
  const key = 'dcmget-theme';
  const media = window.matchMedia?.('(prefers-color-scheme: dark)');
  const apply = (theme) => {
    const resolved = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.dataset.theme = resolved;
    document.documentElement.style.colorScheme = resolved;
    document.querySelector('meta[name="theme-color"]')
      ?.setAttribute('content', resolved === 'dark' ? '#0e1518' : '#147da6');
  };
  const preferred = () => {
    try {
      const stored = window.localStorage.getItem(key);
      if (stored === 'light' || stored === 'dark') return stored;
    } catch (_) {}
    return media?.matches ? 'dark' : 'light';
  };
  apply(preferred());
  media?.addEventListener?.('change', () => {
    try {
      if (window.localStorage.getItem(key)) return;
    } catch (_) {}
    apply(preferred());
  });
})();
</script>
"""

THEME_TOGGLE_HANDLER = r"""
() => {
  const root = document.documentElement;
  const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
  root.dataset.theme = next;
  root.style.colorScheme = next;
  try { window.localStorage.setItem('dcmget-theme', next); } catch (_) {}
  document.querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', next === 'dark' ? '#0e1518' : '#147da6');
}
"""


CSS = r"""
:root {
  --font-ui: "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI",
    "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
  --font-display: "Segoe UI Variable Display", "Segoe UI", "Microsoft YaHei UI",
    "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
  --font-mono: "Cascadia Mono", "Cascadia Code", Consolas, "SFMono-Regular",
    Menlo, "Liberation Mono", monospace;
  --ink: #1b2a33;
  --panel: #ffffff;
  --panel-hi: #f8fbfc;
  --line: #dce5e9;
  --muted: #647985;
  --paper: #f3f7f9;
  --signal: #147da6;
  --signal-2: #36a4c6;
  --good: #248663;
  --bad: #c54848;
  --cyan: #4d8f98;
  --on-signal: #ffffff;
  --panel-glass: rgba(255,255,255,.86);
  --page-top: #f7fafb;
  --page-bottom: #f1f5f7;
  --quiet-bg: #ffffff;
  --neutral-soft: #edf2f4;
  --progress-bg: #eaf0f3;
  --error-soft: #fff4f3;
  --error-copy: #8c4a48;
  --summary-soft: #f1f8fb;
  --summary-copy: #3a687a;
  --log-info: #49636f;
  --shadow: 0 12px 34px rgba(37,64,78,.07);
}
:root[data-theme="dark"] {
  --ink: #e7eef1;
  --panel: #17242a;
  --panel-hi: #1b2b32;
  --line: #2c3e46;
  --muted: #a2b4bc;
  --paper: #0e1518;
  --signal: #45bdce;
  --signal-2: #6ccddd;
  --good: #55c58f;
  --bad: #f07b82;
  --cyan: #74b9c1;
  --on-signal: #05262c;
  --panel-glass: rgba(23,36,42,.90);
  --page-top: #111b20;
  --page-bottom: #0e1518;
  --quiet-bg: #1b2b32;
  --neutral-soft: #233239;
  --progress-bg: #23343c;
  --error-soft: #2c171a;
  --error-copy: #f0a2a6;
  --summary-soft: #142a33;
  --summary-copy: #a9d5e3;
  --log-info: #a8bac2;
  --shadow: 0 14px 38px rgba(0,0,0,.34);
}
html, body, #q-app {
  background: var(--paper); color: var(--ink); font-family: var(--font-ui);
  line-height: 1.55; text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
}
html { font-size:16px; }
body {
  font-size:14px;
  background-image:
    radial-gradient(circle at 88% 0%, color-mix(in srgb,var(--signal) 9%,transparent), transparent 32rem),
    linear-gradient(180deg, var(--page-top) 0, var(--page-bottom) 100%);
}
button, input, textarea, select, .q-btn, .q-field, .q-item, .q-table, .q-dialog {
  font-family: var(--font-ui) !important;
}
.q-btn__content { font-size: 13px; font-weight: 600; letter-spacing: .01em; }
.q-field__native, .q-field__input { font-size: 14px; line-height: 1.5; }
.q-field__label { font-size: 13px; }
.font-mono { font-family: var(--font-mono) !important; }
.text-xs { font-size: 12px !important; line-height: 1.55; }
.text-sm { font-size: 13px !important; line-height: 1.55; }
.text-lg { font-size: 17px !important; line-height: 1.4; font-weight: 600; }
.text-xl { font-size: 20px !important; line-height: 1.35; font-weight: 600; }
.q-page { min-height: 100vh !important; }
.workspace { width: min(1480px, 100%); margin: 0 auto; padding: 20px 24px 48px; }
.topbar {
  display: flex; align-items: center; justify-content: space-between; gap: 20px;
  padding: 8px 2px 18px; border-bottom: 1px solid var(--line); margin-bottom: 22px;
}
.topbar-stack { display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
.brand-lockup { display: flex; align-items: center; gap: 13px; }
.brand-mark {
  width: 38px; height: 38px; border: 1px solid color-mix(in srgb,var(--signal) 35%,transparent);
  display: grid; place-items: center; color: var(--signal); position: relative;
  border-radius: 10px; background: color-mix(in srgb,var(--signal) 8%,transparent);
}
.brand-mark::before, .brand-mark::after { content: ""; position:absolute; background: var(--signal); opacity:.55; }
.brand-mark::before { width: 16px; height: 1px; }
.brand-mark::after { width: 1px; height: 16px; }
.brand-title { font-family:var(--font-display); font-size:20px; font-weight:700; letter-spacing:.055em; }
.brand-sub { color:var(--muted); font:11px var(--font-mono); letter-spacing:.16em; }
.topbar-chip {
  display:flex; align-items:center; gap:8px; padding:7px 11px; border-radius:999px;
  border:1px solid color-mix(in srgb,var(--signal) 14%,var(--line));
  background:color-mix(in srgb,var(--panel) 84%,transparent);
  color:var(--muted); font:600 12px var(--font-ui);
}
.connection {
  font:600 12px var(--font-ui); font-variant-numeric:tabular-nums; color:var(--good);
  display:flex; align-items:center; gap:8px; padding:7px 11px; border-radius:999px;
  border:1px solid color-mix(in srgb,var(--good) 18%,var(--line));
  background:color-mix(in srgb,var(--good) 10%,transparent);
}
.connection.is-pending { color:var(--signal); }
.connection.is-error { color:var(--bad); }
.connection::before { content:""; width:7px; height:7px; border-radius:50%; background:currentColor; box-shadow:0 0 12px currentColor; }
.hero { display:grid; grid-template-columns:minmax(0, 1.35fr) minmax(310px, .65fr); gap:22px; }
.eyebrow { color:var(--signal); font:600 11px var(--font-mono); letter-spacing:.18em; text-transform:uppercase; }
.headline { font-family:var(--font-display); font-size:clamp(28px,2.3vw,36px); font-weight:700; line-height:1.25; letter-spacing:-.015em; margin:8px 0 12px; }
.lede { color:var(--muted); max-width:680px; line-height:1.72; font-size:15px; }
.hero-panel { display:grid; gap:14px; }
.summary-card { padding:20px; background:linear-gradient(180deg, color-mix(in srgb,var(--panel) 98%,transparent), color-mix(in srgb,var(--summary-soft) 78%,var(--panel))); }
.summary-head { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }
.summary-copy { display:grid; gap:4px; }
.summary-copy p { margin:0; color:var(--muted); font-size:13px; line-height:1.6; }
.summary-badge {
  display:inline-flex; align-items:center; gap:8px; padding:7px 11px; border-radius:999px;
  border:1px solid color-mix(in srgb,var(--signal) 20%,var(--line));
  background:color-mix(in srgb,var(--signal) 10%,transparent);
  color:var(--signal); font:600 12px var(--font-ui); white-space:nowrap;
}
.hero-facts { display:grid; grid-template-columns:repeat(2,1fr); border:1px solid var(--line); border-radius:14px; background:var(--panel-glass); box-shadow:var(--shadow); overflow:hidden; }
.hero-fact { padding:17px; border-right:1px solid var(--line); border-bottom:1px solid var(--line); min-width:0; }
.hero-fact:nth-child(2n) { border-right:0; }
.hero-fact:nth-last-child(-n+2) { border-bottom:0; }
.fact-label { display:block; color:var(--muted); font-size:12px; margin-bottom:7px; }
.fact-value { display:block; font:600 13px var(--font-mono); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.hero-actions { display:flex; flex-wrap:wrap; gap:10px; }
.hero-actions .q-btn { min-width:0; }
.launch-bar {
  display:flex; align-items:center; justify-content:space-between; gap:18px;
  margin-top:20px; padding:15px 18px; border:1px solid color-mix(in srgb,var(--signal) 24%,var(--line));
  border-radius:15px; background:var(--panel-glass);
  box-shadow:var(--shadow); position:sticky; top:10px; z-index:20;
}
.launch-copy { min-width:0; }
.launch-title { display:block; font-size:16px; font-weight:700; }
.launch-state { display:block; color:var(--muted); font-size:13px; margin-top:2px; }
.launch-actions { display:flex; align-items:center; gap:9px; flex:0 0 auto; }
.grid-main { display:grid; grid-template-columns:minmax(0,1.5fr) minmax(330px,.5fr); gap:22px; margin-top:25px; align-items:start; }
.stack { display:grid; gap:18px; }
.surface { border:1px solid var(--line); border-radius:15px; background:var(--panel); box-shadow:var(--shadow); overflow:hidden; }
.surface-head { display:flex; justify-content:space-between; align-items:center; gap:16px; padding:18px 20px; border-bottom:1px solid var(--line); }
.surface-head h2 { font-size:20px; margin:0; }
.surface-head p { color:var(--muted); font-size:12px; margin:3px 0 0; }
.surface-body { padding:20px; }
.step-index { color:var(--signal); font:600 12px var(--font-ui); font-variant-numeric:tabular-nums; letter-spacing:.03em; }
.section-copy { display:grid; gap:2px; }
.section-note { color:var(--muted); font-size:12px; line-height:1.55; }
.q-field--outlined .q-field__control:before { border-color:var(--line) !important; }
.q-field--outlined .q-field__control:hover:before { border-color:color-mix(in srgb,var(--signal) 55%,var(--line)) !important; }
.q-field__native, .q-field__input, .q-field__label { color:var(--ink) !important; }
.q-field__bottom { color:var(--muted); }
.q-field__control, .q-menu, .q-card { background:var(--panel); color:var(--ink); }
.accession-input textarea { min-height:170px !important; font:13px/1.62 var(--font-mono) !important; }
.inline-stats { display:flex; flex-wrap:wrap; gap:8px; margin-top:11px; }
.stat-pill { border:1px solid var(--line); padding:5px 9px; color:var(--muted); font:12px var(--font-ui); font-variant-numeric:tabular-nums; }
.stat-pill strong { color:var(--ink); }
.quick-grid { display:grid; grid-template-columns:1fr auto; gap:10px; align-items:center; }
.button-primary { background:var(--signal) !important; color:var(--on-signal) !important; font-weight:700; letter-spacing:.02em; }
.button-primary .q-btn__content, .button-primary .q-icon { color:var(--on-signal) !important; opacity:1 !important; }
.button-primary.q-btn--disabled { opacity:.52 !important; }
.button-danger { color:var(--bad) !important; border-color:color-mix(in srgb,var(--bad) 35%,transparent) !important; }
.button-quiet { color:var(--ink) !important; border:1px solid var(--line); background:var(--quiet-bg) !important; }
html body .q-btn.button-danger { color:var(--bad) !important; }
html body .q-btn.button-quiet { color:var(--ink) !important; }
.button-danger .q-btn__content, .button-danger .q-icon { color:var(--bad) !important; }
.button-quiet .q-btn__content, .button-quiet .q-icon { color:var(--ink) !important; }
.drop-upload { width:100%; border:1px dashed color-mix(in srgb,var(--cyan) 42%,transparent); background:color-mix(in srgb,var(--cyan) 4%,transparent); }
.preflight-list { display:grid; gap:10px; }
.check-row { display:grid; grid-template-columns:24px 1fr; gap:10px; align-items:start; padding:9px 0; border-bottom:1px solid var(--line); }
.check-dot { width:20px;height:20px;border-radius:50%;display:grid;place-items:center;background:var(--neutral-soft);color:var(--muted);font-size:11px; }
.check-dot.ok { color:var(--good); background:color-mix(in srgb,var(--good) 12%,transparent); }
.check-dot.bad { color:var(--bad); background:color-mix(in srgb,var(--bad) 12%,transparent); }
.check-copy strong { display:block;font-size:13px; }.check-copy small{color:var(--muted);font-size:12px;}
.progress-rail { height:9px; background:var(--progress-bg); border:1px solid var(--line); border-radius:999px; overflow:hidden; }
.progress-fill { height:100%; width:0; background:linear-gradient(90deg,var(--signal),var(--signal-2)); transition:width .4s ease; }
.progress-label { color:var(--signal); font:600 12px var(--font-mono); }
.metric-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:1px; background:var(--line); border:1px solid var(--line); margin-top:18px; }
.metric { background:var(--panel-hi); padding:15px; min-width:0; }.metric span{display:block;color:var(--muted);font-size:12px;letter-spacing:.05em}.metric strong{display:block;margin-top:8px;font:600 15px var(--font-mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.action-row { display:flex;flex-wrap:wrap;gap:9px;margin-top:18px; }
.error-block { border-left:3px solid var(--bad); background:var(--error-soft); padding:14px; margin-bottom:14px; }
.error-block strong { color:var(--bad); }.error-block p{color:var(--error-copy);margin:5px 0 0;font-size:12px;}
.result-table { width:100%; }
.result-table .q-table { background:transparent; color:var(--ink); }
.result-table th { color:var(--muted); font-size:12px; letter-spacing:.05em; }
.large-summary { border:1px solid color-mix(in srgb,var(--signal) 22%,var(--line)); border-radius:10px; background:var(--summary-soft); padding:16px; color:var(--summary-copy); line-height:1.65; }
.log-line { display:grid;grid-template-columns:70px 1fr;gap:10px;padding:9px 0;border-bottom:1px solid var(--line);font:12px/1.55 var(--font-mono); }
.log-time { color:var(--muted); }.log-error{color:var(--bad)}.log-info{color:var(--log-info)}
.profile-grid { display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:16px;margin-top:22px; }
.profile-card { border:1px solid var(--line); border-radius:15px; background:var(--panel);padding:19px;position:relative;overflow:hidden;box-shadow:var(--shadow); }
.profile-card::after { content:"";position:absolute;width:80px;height:80px;border:1px solid color-mix(in srgb,var(--cyan) 8%,transparent);border-radius:50%;right:-30px;top:-30px; }
.profile-card.running { border-color:color-mix(in srgb,var(--good) 32%,var(--line)); }.profile-card.issue{border-color:color-mix(in srgb,var(--bad) 35%,var(--line))}
.profile-name { font-family:var(--font-display);font-size:20px;font-weight:700; }.profile-no{color:var(--signal);font:11px var(--font-mono);}
.profile-facts { display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:18px 0; }.profile-fact span{display:block;color:var(--muted);font-size:12px}.profile-fact strong{font:12px var(--font-mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block;margin-top:4px}
.profile-status { position:absolute;right:15px;top:15px;font:500 12px var(--font-ui);color:var(--muted); }.profile-status.on{color:var(--good)}
.manager-summary { display:flex;gap:25px;margin-top:16px; }.manager-summary strong{font-family:var(--font-display);font-size:30px;font-weight:700}.manager-summary span{display:block;color:var(--muted);font-size:12px}
.manager-cta { display:flex; flex-wrap:wrap; gap:10px; margin-top:18px; }
.settings-dialog { width:min(850px,94vw);max-width:850px;background:var(--panel) !important;color:var(--ink);border:1px solid var(--line); }
.settings-grid { display:grid;grid-template-columns:1fr 1fr;gap:13px; }.settings-section{padding:4px 0 12px}.settings-section-title{color:var(--signal);font:600 12px var(--font-ui);letter-spacing:.05em;margin-bottom:12px}
.dialog-host { display:contents; }
.q-expansion-item { border-bottom:1px solid var(--line); }.q-expansion-item__container > .q-item{color:var(--ink)}
.directory-dialog { width:min(720px,94vw);background:var(--panel) !important;color:var(--ink);border:1px solid var(--line); }
.directory-row { width:100%;justify-content:flex-start;color:var(--ink)!important;border-bottom:1px solid var(--line); }
.loading-card { min-height:220px; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:14px; color:var(--muted); }
.theme-toggle { color:var(--ink) !important; border:1px solid var(--line); background:var(--quiet-bg) !important; }
.text-slate-400, .text-slate-500 { color:var(--muted) !important; }
.q-notification { border:1px solid color-mix(in srgb,currentColor 20%,transparent); box-shadow:var(--shadow); }
.surface, .profile-card, .launch-bar { transition:border-color .16s ease, box-shadow .16s ease, transform .16s ease; }
.profile-card:hover { border-color:color-mix(in srgb,var(--signal) 38%,var(--line)); transform:translateY(-1px); }
.q-btn:active { transform:translateY(1px); }
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition-duration:.01ms!important;animation-duration:.01ms!important;animation-iteration-count:1!important}}
@media(forced-colors:active){.surface,.profile-card,.launch-bar,.q-btn,.q-field__control{border:1px solid ButtonBorder!important}.button-primary{border-color:ButtonText!important}.connection::before{forced-color-adjust:none}}
@media(max-width:900px){.hero,.grid-main{grid-template-columns:1fr}.hero-facts{margin-top:4px}.metric-grid{grid-template-columns:1fr 1fr}.workspace{padding:14px 14px 36px}.settings-grid{grid-template-columns:1fr}.launch-bar{top:6px}.topbar{align-items:flex-start;flex-direction:column}.topbar-stack{justify-content:flex-start}}
@media(max-width:520px){.headline{font-size:34px}.hero-facts{grid-template-columns:1fr}.hero-fact{border-right:0!important;border-bottom:1px solid var(--line)}.hero-fact:last-child{border-bottom:0}.metric-grid{grid-template-columns:1fr}.connection{display:none}.topbar-chip{width:100%;justify-content:center}.launch-bar{align-items:stretch;flex-direction:column}.launch-actions{display:grid;grid-template-columns:1fr 1.25fr}.launch-actions .q-btn{width:100%}}
"""


def _normal_status(value: object) -> str:
    return str(value or "idle").strip().lower().replace("-", "_").replace(" ", "_")


def _format_speed(value: object) -> str:
    try:
        speed = max(0.0, float(value or 0))
    except (TypeError, ValueError):
        speed = 0.0
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    index = 0
    while speed >= 1024 and index < len(units) - 1:
        speed /= 1024
        index += 1
    return f"{speed:.0f} {units[index]}" if index == 0 or speed >= 100 else f"{speed:.1f} {units[index]}"


def _task_count(task: Mapping[str, Any], *keys: str) -> int:
    for container in (task, task.get("summary", {}), task.get("status_counts", {})):
        if not isinstance(container, Mapping):
            continue
        for key in keys:
            if key in container:
                try:
                    return int(container[key] or 0)
                except (TypeError, ValueError):
                    return 0
    return 0


def _payload_error(exc: Exception) -> str:
    message = str(exc).strip()
    if "JavaScriptError" in message and ":" in message:
        message = message.rsplit(":", 1)[-1].strip()
    return message or "请求失败，请稍后重试"


async def _browser_api(
    path: str,
    *,
    method: str = "GET",
    body: Mapping[str, Any] | None = None,
    timeout: float = 30.0,
) -> Any:
    """Execute an API call in the connected browser, including its cookies."""

    request = {"path": path, "method": method.upper(), "body": body}
    script = f"""
    (async () => {{
      try {{
        const request = {json.dumps(request, ensure_ascii=False)};
        const mutation = !['GET', 'HEAD'].includes(request.method);
        const headers = {{'Accept': 'application/json'}};
        if (mutation) {{
          headers['Content-Type'] = 'application/json';
          if (window.__dcmgetCsrf) headers['X-CSRF-Token'] = window.__dcmgetCsrf;
        }}
        const response = await fetch(request.path, {{
          method: request.method,
          credentials: 'same-origin',
          headers,
          body: mutation ? JSON.stringify(request.body || {{}}) : undefined,
        }});
        const text = await response.text();
        let payload = {{}};
        if (text) {{
          try {{ payload = JSON.parse(text); }} catch (_) {{ payload = {{detail: text}}; }}
        }}
        if (!response.ok) {{
          const detail = payload?.detail;
          const message = typeof detail === 'string'
            ? detail
            : detail?.message || payload?.message || `请求失败 (${{response.status}})`;
          const fields = detail?.fields;
          return {{
            __dcmget_api_result__: true,
            ok: false,
            error: fields ? `${{message}}：${{Object.values(fields).join('；')}}` : message,
          }};
        }}
        if (payload?.csrf_token) window.__dcmgetCsrf = payload.csrf_token;
        return {{__dcmget_api_result__: true, ok: true, payload}};
      }} catch (error) {{
        return {{
          __dcmget_api_result__: true,
          ok: false,
          error: error?.message || String(error) || '网络请求失败',
        }};
      }}
    }})()
    """
    result = await ui.run_javascript(script, timeout=timeout)
    if isinstance(result, Mapping) and result.get("__dcmget_api_result__"):
        if not bool(result.get("ok")):
            raise RuntimeError(str(result.get("error") or "请求失败"))
        return result.get("payload", {})
    raise RuntimeError("浏览器返回了无效的 API 响应")


async def _read_upload(event: Any) -> tuple[str, bytes]:
    file_object = getattr(event, "file", None)
    if file_object is not None:
        name = str(getattr(file_object, "name", "accessions.txt"))
        content = file_object.read()
        if inspect.isawaitable(content):
            content = await content
        return name, bytes(content)
    name = str(getattr(event, "name", "accessions.txt"))
    content = getattr(event, "content", b"")
    if hasattr(content, "read"):
        content = content.read()
        if inspect.isawaitable(content):
            content = await content
    return name, bytes(content)


@dataclass
class _WorkspaceState:
    bootstrap: dict[str, Any]
    config: dict[str, Any]
    task: dict[str, Any] = field(default_factory=dict)
    accessions: list[str] = field(default_factory=list)
    import_stats: dict[str, int] = field(
        default_factory=lambda: {"blank": 0, "duplicate": 0, "invalid": 0}
    )
    preflight_ok: bool = False
    preflight_signature: str = ""
    last_preflight_attempt: str = ""
    event_cursor: int = 0
    logs: list[dict[str, Any]] = field(default_factory=list)
    large_import: bool = False
    accession_signature: str = ""
    show_detailed_logs: bool = False

    def signature(self, destination: str, pdi_enabled: bool, pdi_folder: str) -> str:
        return json.dumps(
            [
                self.accession_signature,
                len(self.accessions),
                destination.strip(),
                pdi_enabled,
                pdi_folder.strip(),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )


def _fact(label: str, value: str) -> None:
    with ui.element("div").classes("hero-fact"):
        ui.label(label).classes("fact-label")
        ui.label(value or "—").classes("fact-value")


def _notify_error(exc: Exception, prefix: str = "操作失败") -> None:
    ui.notify(f"{prefix}：{_payload_error(exc)}", type="negative", close_button=True)


async def _build_manager(bootstrap: dict[str, Any]) -> None:
    profiles: list[dict[str, Any]] = []
    grid: Any = None
    dialog_host: Any = None
    active_profile_dialog: Any = None
    count_label: Any = None
    running_label: Any = None

    async def profile_action(
        profile: Mapping[str, Any],
        action: str,
        *,
        notify: bool = True,
        refresh: bool = True,
        raise_errors: bool = False,
    ) -> Mapping[str, Any] | None:
        number = int(profile.get("number", 0))
        try:
            result = await _browser_api(
                f"/api/management/profiles/{number}/{action}",
                method="POST",
                body={},
                timeout=90 if action == "stop" else 30,
            )
            if notify:
                default = "启动命令已提交" if action == "start" else "Profile 已停止"
                ui.notify(str(result.get("message") or default), type="positive")
            if refresh:
                await refresh_profiles()
            return result
        except Exception as exc:  # browser errors are user-facing
            if raise_errors:
                raise
            _notify_error(exc)
            return None

    async def create_profile() -> None:
        try:
            await _browser_api("/api/management/profiles", method="POST", body={})
            ui.notify("新 Profile 已创建，请补充配置后启动", type="positive")
            await refresh_profiles()
        except Exception as exc:
            _notify_error(exc, "创建失败")

    async def open_profile(profile: Mapping[str, Any]) -> None:
        number = int(profile.get("number", 0) or 0)
        if not number:
            ui.notify("Profile 编号无效", type="warning")
            return
        ui.navigate.to(f"./?profile={number}")

    def edit_profile(profile: Mapping[str, Any]) -> None:
        nonlocal active_profile_dialog
        if dialog_host is None:
            ui.notify("配置面板尚未就绪，请稍后重试", type="warning")
            return
        if active_profile_dialog is not None:
            active_profile_dialog.delete()
        # Profile cards are replaced by the four-second status refresh. Keep
        # dialogs in a stable sibling so clearing the card grid cannot close one.
        with dialog_host:
            with ui.dialog() as dialog, ui.card().classes("settings-dialog p-0"):
                with ui.element("div").classes("surface-head"):
                    with ui.element("div"):
                        ui.label(f"配置 Profile {profile.get('number', '')}").classes("text-xl")
                        ui.label("保存后再启动；接收端口和 Web 端口不能重复")
                    ui.button(icon="close", on_click=dialog.close).props("flat round")
                fields: dict[str, Any] = {}
                with ui.element("div").classes("surface-body"):
                    with ui.element("div").classes("settings-grid"):
                        fields["display_name"] = ui.input(
                            "显示名称", value=profile.get("display_name", "")
                        ).props("outlined dense")
                        fields["pacs_server_ip"] = ui.input(
                            "PACS 地址", value=profile.get("pacs_server_ip", "")
                        ).props("outlined dense")
                        for key, label in (
                            ("pacs_server_port", "PACS 端口"),
                            ("storage_port", "DICOM 接收端口"),
                            ("web_port", "Web 端口"),
                        ):
                            fields[key] = ui.input(
                                label, value=str(profile.get(key, ""))
                            ).props("outlined dense inputmode=numeric pattern=[0-9]*")
                        fields["calling_ae_title"] = ui.input(
                            "本机调用 AE", value=profile.get("calling_ae_title", "")
                        ).props("outlined dense")
                        fields["pacs_ae_title"] = ui.input(
                            "PACS AE", value=profile.get("pacs_ae_title", "")
                        ).props("outlined dense")
                        fields["storage_ae_title"] = ui.input(
                            "接收 AE", value=profile.get("storage_ae_title", "")
                        ).props("outlined dense")
                        fields["dicom_destination_folder"] = ui.input(
                            "保存目录", value=profile.get("dicom_destination_folder", "")
                        ).props("outlined dense")

                    async def save_profile() -> None:
                        body: dict[str, Any] = {
                            "profile_number": int(profile.get("number", 0))
                        }
                        try:
                            for key, control in fields.items():
                                value = control.value
                                body[key] = int(value) if key.endswith("_port") else value
                            result = await _browser_api(
                                "/api/operations/profile-update", method="POST", body=body
                            )
                            dialog.close()
                            ui.notify(
                                str(result.get("message") or "Profile 配置已保存"),
                                type="positive",
                            )
                            await refresh_profiles()
                        except (TypeError, ValueError) as exc:
                            _notify_error(exc, "端口必须是 1 到 65535 的整数")
                        except Exception as exc:
                            _notify_error(exc, "保存失败")

                    with ui.row().classes("justify-end w-full pt-5"):
                        ui.button("取消", on_click=dialog.close).props("flat")
                        ui.button("保存配置", icon="save", on_click=save_profile).props(
                            "unelevated"
                        ).classes("button-primary")
        active_profile_dialog = dialog
        dialog.open()

    async def request_edit_profile(profile: Mapping[str, Any]) -> None:
        if not bool(profile.get("is_running")):
            edit_profile(profile)
            return
        if dialog_host is None:
            ui.notify("配置面板尚未就绪，请稍后重试", type="warning")
            return
        with dialog_host:
            with ui.dialog() as confirm, ui.card().classes("settings-dialog p-0"):
                with ui.element("div").classes("surface-head"):
                    with ui.element("div"):
                        ui.label("需要先停止 Profile").classes("text-xl")
                        ui.label("修改 AE、端口或保存目录前，需要释放当前接收服务")
                with ui.element("div").classes("surface-body"):
                    ui.label(
                        "未完成任务会保留恢复点。确认后将等待后台进程退出，"
                        "并确认 Web 与 DICOM 接收端口均已释放，再打开配置。"
                    ).classes("text-sm")

                    async def stop_then_edit() -> None:
                        stop_button.disable()
                        stop_button.set_text("正在停止并检查端口…")
                        try:
                            result = await profile_action(
                                profile,
                                "stop",
                                notify=False,
                                refresh=False,
                                raise_errors=True,
                            )
                            confirm.close()
                            await refresh_profiles()
                            number = int(profile.get("number", 0))
                            latest = next(
                                (item for item in profiles if int(item.get("number", 0)) == number),
                                profile,
                            )
                            ui.notify(
                                str((result or {}).get("message") or "Profile 已停止"),
                                type="positive",
                            )
                            edit_profile(latest)
                        except Exception as exc:
                            stop_button.enable()
                            stop_button.set_text("停止并修改")
                            _notify_error(exc, "停止失败")

                    with ui.row().classes("justify-end w-full pt-5"):
                        ui.button("取消", on_click=confirm.close).props("flat")
                        stop_button = ui.button(
                            "停止并修改",
                            icon="stop_circle",
                            on_click=stop_then_edit,
                        ).props("unelevated no-caps").classes("button-primary")
        confirm.open()

    def render_profiles() -> None:
        grid.clear()
        count_label.set_text(str(len(profiles)))
        running_label.set_text(str(sum(bool(item.get("is_running")) for item in profiles)))
        with grid:
            if not profiles:
                ui.label("尚未创建 Profile。点击右上角按钮建立第一个工作空间。").classes("text-slate-400")
            for profile in profiles:
                running = bool(profile.get("is_running"))
                issue = bool(profile.get("issues")) or not profile.get("pacs_server_ip") or not profile.get("destination_directory")
                classes = "profile-card " + ("running" if running else "") + (" issue" if issue else "")
                with ui.element("article").classes(classes):
                    ui.label(f"PROFILE {profile.get('number', '—')}").classes("profile-no")
                    ui.label(str(profile.get("display_name") or f"实例 {profile.get('number', '')}")).classes("profile-name")
                    ui.label("运行中" if running else "已停止").classes("profile-status " + ("on" if running else ""))
                    with ui.element("div").classes("profile-facts"):
                        for label, value in (
                            ("PACS", f"{profile.get('pacs_server_ip', '—')}:{profile.get('pacs_server_port', '—')}"),
                            ("调用 / PACS AE", f"{profile.get('calling_ae_title', '—')} / {profile.get('pacs_ae_title', '—')}"),
                            ("本机接收", f"{profile.get('storage_ae_title', '—')}:{profile.get('storage_port', '—')}"),
                            ("WEB", str(profile.get("web_port", "—"))),
                        ):
                            with ui.element("div").classes("profile-fact"):
                                ui.label(label)
                                ui.label(value).classes("font-semibold")
                    ui.label(str(profile.get("destination_directory") or "尚未设置保存目录")).classes("text-xs text-slate-400 truncate w-full")
                    with ui.row().classes("action-row"):
                        if running:
                            ui.button("进入工作台", icon="arrow_forward", on_click=lambda p=profile: open_profile(p)).props("unelevated").classes("button-primary")
                            ui.button("配置", icon="settings", on_click=lambda p=profile: request_edit_profile(p)).props("flat").classes("button-quiet")
                            ui.button("停止", icon="stop", on_click=lambda p=profile: profile_action(p, "stop")).props("flat").classes("button-danger")
                        else:
                            ui.button("启动", icon="play_arrow", on_click=lambda p=profile: profile_action(p, "start")).props("unelevated").classes("button-primary")
                            ui.button("配置", icon="settings", on_click=lambda p=profile: request_edit_profile(p)).props("flat").classes("button-quiet")
                            if any(
                                "已被其他程序占用" in str(message)
                                for message in profile.get("issues", [])
                            ):
                                ui.button(
                                    "检查并清理占用",
                                    icon="cleaning_services",
                                    on_click=lambda p=profile: profile_action(p, "stop"),
                                ).props("flat").classes("button-danger")

    async def refresh_profiles() -> None:
        nonlocal profiles
        try:
            payload = await _browser_api("/api/management/profiles")
            source = payload.get("profiles", payload.get("items", payload)) if isinstance(payload, dict) else payload
            profiles = list(source or []) if isinstance(source, list) else []
            render_profiles()
        except Exception as exc:
            _notify_error(exc, "读取 Profile 失败")

    with ui.element("main").classes("workspace"):
        _topbar("Windows 管理中心")
        with ui.element("section").classes("hero"):
            with ui.element("div"):
                ui.label("PROFILE ORCHESTRATION / 8786").classes("eyebrow")
                ui.label("让每一条影像链路，清楚地运行。 ").classes("headline")
                ui.label("统一查看、启动和停止独立 Profile。每个 Profile 保持自己的 PACS、接收端口、保存目录与任务状态。").classes("lede")
            with ui.element("div").classes("surface surface-body summary-card"):
                with ui.element("div").classes("summary-head"):
                    with ui.element("div").classes("summary-copy"):
                        ui.label("工作台概况").classes("eyebrow")
                        ui.label("统一入口，按需启动；不再把细碎配置堆在首屏。").classes("text-sm")
                    ui.label("当前 Profile 默认不启动").classes("summary-badge")
                with ui.element("div").classes("manager-summary"):
                    with ui.element("div"):
                        count_label = ui.label("0")
                        ui.label("全部 Profile")
                    with ui.element("div"):
                        running_label = ui.label("0")
                        ui.label("正在运行")
                with ui.element("div").classes("manager-cta"):
                    ui.button("新建 Profile", icon="add", on_click=create_profile).props("unelevated").classes("button-primary")
        grid = ui.element("section").classes("profile-grid")
        dialog_host = ui.element("div").classes("dialog-host")
    await refresh_profiles()
    ui.timer(4.0, refresh_profiles)


def _topbar(context: str, *, connection_state: str = "connected") -> None:
    with ui.element("header").classes("topbar"):
        with ui.element("div").classes("brand-lockup"):
            ui.element("div").classes("brand-mark")
            with ui.element("div"):
                ui.label("DCMGET").classes("brand-title")
                ui.label(context.upper()).classes("brand-sub")
        with ui.element("div").classes("topbar-stack"):
            ui.label("浏览器安全会话").classes("topbar-chip")
            theme_button = ui.button(icon="contrast").props(
                'flat round dense aria-label="切换浅色或深色主题"'
            ).classes("theme-toggle")
            theme_button.on("click", js_handler=THEME_TOGGLE_HANDLER)
            theme_button.tooltip("切换浅色 / 深色主题")
            connection_labels = {
                "connected": "API 已连接",
                "connecting": "正在连接",
                "error": "连接失败",
            }
            connection_classes = {
                "connecting": " is-pending",
                "error": " is-error",
            }
            ui.label(connection_labels.get(connection_state, "API 已连接")).classes(
                "connection" + connection_classes.get(connection_state, "")
            )


async def _build_profile(
    bootstrap: dict[str, Any],
    *,
    api_prefix: str = "",
    managed_profiles: list[dict[str, Any]] | None = None,
    managed_profile_number: int | None = None,
) -> None:
    state = _WorkspaceState(
        bootstrap=bootstrap,
        config=dict(bootstrap.get("config") or {}),
        task=dict(bootstrap.get("task") or {}),
    )
    refs: dict[str, Any] = {}

    def api(path: str) -> str:
        if not api_prefix:
            return path
        if not path.startswith("/api"):
            raise ValueError(f"无效的 API 路径：{path}")
        return f"{api_prefix}{path[4:]}"

    def draft() -> dict[str, Any]:
        return {
            "accessions": list(state.accessions),
            "destination": str(refs["destination"].value or "").strip(),
            "pdi": {
                "enabled": bool(refs["pdi_enabled"].value),
                "output_folder": str(refs["pdi_folder"].value or "").strip(),
            },
        }

    def signature() -> str:
        payload = draft()
        return state.signature(
            payload["destination"], payload["pdi"]["enabled"], payload["pdi"]["output_folder"]
        )

    def invalidate_preflight() -> None:
        state.preflight_ok = False
        state.preflight_signature = ""
        refs["start"].disable()
        refs["readiness"].set_text("内容有变化，请重新预检")

    def can_start_new_task() -> bool:
        actions = state.task.get("actions")
        if isinstance(actions, Mapping) and "can_start" in actions:
            return bool(actions.get("can_start"))
        return _normal_status(state.task.get("status")) in TERMINAL_STATUSES | {"idle"}

    def task_block_message() -> str:
        status = _normal_status(state.task.get("status"))
        operation = str(state.task.get("operation") or "").strip().lower()
        port = state.config.get("storage_port", "—")
        if operation == "download" and status in {
            "starting_receiver", "downloading", "pause_pending", "paused", "stopping"
        }:
            return f"当前任务正在使用接收端口 {port}；请先继续或结束当前任务"
        return "当前 Profile 存在未完成任务；请先继续、重试或结束当前任务"

    def update_accessions(values: list[str], stats: dict[str, int]) -> bool:
        if values == state.accessions and stats == state.import_stats:
            return False
        state.accessions = values
        state.import_stats = stats
        digest = hashlib.sha256()
        for value in values:
            digest.update(value.encode("utf-8", errors="replace"))
            digest.update(b"\0")
        state.accession_signature = digest.hexdigest()
        return True

    def render_import() -> None:
        refs["valid"].set_text(str(len(state.accessions)))
        refs["duplicate"].set_text(str(state.import_stats["duplicate"]))
        refs["blank"].set_text(str(state.import_stats["blank"]))
        refs["invalid"].set_text(str(state.import_stats["invalid"]))
        state.large_import = len(state.accessions) > DETAIL_LIMIT
        if state.large_import:
            refs["input"].set_value("")
            refs["input"].props("readonly")
            refs["large_import"].set_text(
                f"已载入 {len(state.accessions):,} 个检查号。超过 {DETAIL_LIMIT} 条，页面不展示明细以保持流畅。"
            )
            refs["large_import"].set_visibility(True)
        else:
            refs["input"].props(remove="readonly")
            refs["large_import"].set_visibility(False)
        invalidate_preflight()

    def parse_pasted() -> None:
        parsed = parse_accessions(str(refs["input"].value or ""))
        changed = update_accessions(list(parsed.values), {
            "blank": parsed.blank_count,
            "duplicate": parsed.duplicate_count,
            "invalid": len(parsed.invalid_values),
        })
        if changed:
            render_import()

    def clear_accessions() -> None:
        update_accessions([], {"blank": 0, "duplicate": 0, "invalid": 0})
        refs["input"].props(remove="readonly")
        refs["input"].set_value("")
        render_import()

    async def import_upload(event: Any) -> None:
        temporary: Path | None = None
        try:
            name, content = await _read_upload(event)
            suffix = Path(name).suffix.casefold()
            if suffix not in {".txt", ".csv", ".xlsx"}:
                raise AccessionImportError("仅支持 TXT、CSV 和 XLSX 文件")
            descriptor, temp_name = tempfile.mkstemp(prefix="dcmget-nicegui-", suffix=suffix)
            temporary = Path(temp_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            result = import_accession_file(temporary)
            apply_import_result(result)
            ui.notify(f"已从 {name} 导入 {result.valid_count:,} 个检查号", type="positive")
        except ColumnSelectionError as exc:
            await choose_import_column(name, content, exc.columns)
        except (AccessionImportError, OSError, ValueError) as exc:
            _notify_error(exc, "导入失败")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def apply_import_result(result: Any) -> None:
        update_accessions(list(result.values), {
            "blank": int(result.blank_count),
            "duplicate": int(result.duplicate_count),
            "invalid": int(result.invalid_count),
        })
        if len(state.accessions) <= DETAIL_LIMIT:
            refs["input"].set_value("\n".join(state.accessions))
        render_import()

    async def choose_import_column(
        name: str,
        content: bytes,
        columns: tuple[ImportColumn, ...],
    ) -> None:
        if not columns:
            ui.notify("未找到可导入的检查号列", type="negative")
            return
        options = {column.index: f"{column.name}（第 {column.index + 1} 列）" for column in columns}
        with ui.dialog() as dialog, ui.card().classes("settings-dialog p-5"):
            ui.label("选择检查号列").classes("text-xl")
            ui.label("文件中有多个可用列，请指定哪一列是检查号。").classes("text-sm text-slate-400")
            selected = ui.select(options, value=columns[0].index, label="检查号列").props("outlined").classes("w-full")

            async def retry() -> None:
                temporary: Path | None = None
                try:
                    descriptor, temp_name = tempfile.mkstemp(prefix="dcmget-nicegui-", suffix=Path(name).suffix.casefold())
                    temporary = Path(temp_name)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(content)
                    result = import_accession_file(temporary, column=int(selected.value))
                    apply_import_result(result)
                    dialog.close()
                    ui.notify(f"已导入 {result.valid_count:,} 个检查号", type="positive")
                except (AccessionImportError, OSError, ValueError) as exc:
                    _notify_error(exc, "导入失败")
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)

            with ui.row().classes("justify-end w-full"):
                ui.button("取消", on_click=dialog.close).props("flat")
                ui.button("导入", on_click=retry).props("unelevated").classes("button-primary")
        dialog.open()

    async def browse_directory(target: Any, purpose: str) -> None:
        current = str(target.value or "")
        with ui.dialog() as dialog, ui.card().classes("directory-dialog p-0"):
            with ui.element("div").classes("surface-head w-full"):
                with ui.element("div"):
                    ui.label("选择服务器目录").classes("text-lg")
                    path_label = ui.label(current or "默认目录").classes("text-xs text-slate-400")
                ui.button(icon="close", on_click=dialog.close).props("flat round")
            rows = ui.column().classes("w-full gap-0 px-4 pb-4")

            async def load(path: str) -> None:
                try:
                    query = json.dumps(path)
                    encoded = await ui.run_javascript(f"encodeURIComponent({query})")
                    payload = await _browser_api(api(f"/api/files/directories?purpose={purpose}&path={encoded}"))
                    path_label.set_text(str(payload.get("path") or "默认目录"))
                    rows.clear()
                    with rows:
                        if payload.get("parent"):
                            ui.button("↑ 上一级", on_click=lambda p=payload["parent"]: load(p)).props("flat no-caps").classes("directory-row")
                        for item in payload.get("directories", []):
                            ui.button(str(item.get("name") or item.get("path")), icon="folder", on_click=lambda p=item.get("path", ""): load(p)).props("flat no-caps").classes("directory-row")
                        with ui.row().classes("justify-end w-full pt-3"):
                            ui.button("选择当前目录", on_click=lambda p=payload.get("path", ""): select(p)).props("unelevated").classes("button-primary")
                except Exception as exc:
                    _notify_error(exc, "目录读取失败")

            def select(path: str) -> None:
                target.set_value(path)
                dialog.close()
                invalidate_preflight()

        dialog.open()
        await load(current)

    async def run_preflight(*, silent: bool = False) -> bool:
        if not can_start_new_task():
            message = task_block_message()
            state.preflight_ok = False
            state.preflight_signature = ""
            render_checks(
                [{"key": "task", "name": "当前任务", "ok": False, "message": message}]
            )
            refs["readiness"].set_text(message)
            refs["start"].disable()
            if not silent:
                ui.notify(message, type="warning")
            return False
        if not state.large_import:
            parse_pasted()
        payload = draft()
        if not payload["accessions"]:
            if not silent:
                ui.notify("请先输入至少一个有效检查号", type="warning")
            return False
        if not payload["destination"]:
            if not silent:
                ui.notify("请先选择保存目录", type="warning")
            return False
        state.last_preflight_attempt = signature()
        refs["preflight"].disable()
        refs["readiness"].set_text("正在检查配置、工具、目录和端口…")
        try:
            result = await _browser_api(api("/api/preflight"), method="POST", body=payload)
            checks = result.get("checks", []) if isinstance(result, dict) else []
            render_checks(checks)
            state.preflight_ok = bool(result.get("ok"))
            state.preflight_signature = signature() if state.preflight_ok else ""
            refs["readiness"].set_text("预检通过，可以开始下载" if state.preflight_ok else "预检未通过，请修复红色项目")
            refs["start"].set_enabled(state.preflight_ok)
            if not state.preflight_ok and not silent:
                ui.notify("预检未通过", type="negative")
            return state.preflight_ok
        except Exception as exc:
            state.preflight_ok = False
            render_checks([])
            refs["readiness"].set_text(f"预检失败：{_payload_error(exc)}")
            _notify_error(exc, "预检失败")
            return False
        finally:
            refs["preflight"].enable()

    async def automatic_preflight() -> None:
        if not state.accessions or not str(refs["destination"].value or "").strip():
            return
        current = signature()
        if current == state.last_preflight_attempt or state.preflight_signature == current:
            return
        await run_preflight(silent=True)

    def render_checks(checks: list[dict[str, Any]]) -> None:
        refs["checks"].clear()
        with refs["checks"]:
            if not checks:
                ui.label("尚未完成预检").classes("text-sm text-slate-400")
            for check in checks:
                ok = bool(check.get("ok"))
                with ui.element("div").classes("check-row"):
                    ui.label("✓" if ok else "×").classes("check-dot " + ("ok" if ok else "bad"))
                    with ui.element("div").classes("check-copy"):
                        ui.label(str(check.get("name") or check.get("label") or "检查项")).classes("font-semibold")
                        ui.label(str(check.get("message") or check.get("detail") or ("已就绪" if ok else "未通过")))

    async def start_task() -> None:
        if not state.preflight_ok or state.preflight_signature != signature():
            if not await run_preflight():
                return
        refs["start"].disable()
        try:
            result = await _browser_api(api("/api/tasks/start"), method="POST", body=draft(), timeout=45)
            state.task = dict(result.get("task") or result)
            state.preflight_ok = False
            state.preflight_signature = ""
            state.last_preflight_attempt = ""
            render_task()
            ui.notify("任务已交给后台执行；关闭浏览器不会停止下载", type="positive")
        except Exception as exc:
            _notify_error(exc, "启动失败")
            refs["start"].enable()

    async def task_action(action: str) -> None:
        labels = {
            "pause": "暂停",
            "resume": "继续",
            "cancel": "取消",
            "retry-failed": "重试",
            "accept-partial": "接受已有文件",
        }
        try:
            result = await _browser_api(api(f"/api/tasks/{action}"), method="POST", body={})
            state.task = dict(result.get("task") or result)
            render_task()
            ui.notify(f"{labels.get(action, '操作')}命令已提交", type="positive")
        except Exception as exc:
            _notify_error(exc)

    async def pdi_action(action: str) -> None:
        labels = {"open": "打开 PDI", "verify": "校验 PDI", "retry": "重试 PDI"}
        try:
            result = await _browser_api(
                api(f"/api/pdi/{action}"),
                method="POST",
                body={"task_id": state.task.get("id")},
                timeout=45,
            )
            updated = result.get("task") if isinstance(result, Mapping) else None
            if isinstance(updated, dict):
                state.task = updated
                render_task()
            ui.notify(
                str(
                    (result.get("message") if isinstance(result, Mapping) else None)
                    or f"{labels.get(action, 'PDI 操作')}已提交"
                ),
                type="positive",
            )
        except Exception as exc:
            _notify_error(exc, labels.get(action, "PDI 操作失败"))

    async def operation(name: str) -> None:
        try:
            result = await _browser_api(api(f"/api/operations/{name}"), method="POST", body={})
            message = result.get("message") if isinstance(result, Mapping) else None
            ui.notify(str(message or "操作完成"), type="positive")
        except Exception as exc:
            _notify_error(exc)

    def license_label(value: Mapping[str, Any]) -> str:
        licensed = bool(value.get("licensed", value.get("registered", False)))
        if licensed:
            customer = str(value.get("customer") or value.get("edition") or "").strip()
            return f"已授权 · {customer}" if customer else "产品已授权"
        remaining = value.get("trial_remaining")
        return f"试用剩余 {remaining} 次" if remaining is not None else "等待授权"

    def update_license_summary(value: Mapping[str, Any]) -> None:
        state.bootstrap["license"] = dict(value)
        summary = refs.get("license_summary")
        if summary is not None:
            summary.set_text(license_label(value))
        badge = refs.get("license_badge")
        if badge is not None:
            badge.set_text(license_label(value))

    def open_license_dialog() -> None:
        with ui.dialog() as dialog, ui.card().classes("settings-dialog p-0"):
            with ui.element("div").classes("surface-head"):
                with ui.element("div"):
                    ui.label("软件授权").classes("text-xl")
                    ui.label("未注册设备默认可免费启动 30 个下载任务")
                ui.button(icon="close", on_click=dialog.close).props("flat round")
            with ui.element("div").classes("surface-body stack"):
                status_label = ui.label("正在读取授权状态…").classes("font-semibold")
                machine_label = ui.label("机器码：—").classes(
                    "text-sm font-mono break-all"
                )
                token_input = ui.textarea(
                    "注册码",
                    placeholder="粘贴由 DcmGet 注册机生成的注册码",
                ).props("outlined autogrow")

                async def refresh_license() -> None:
                    try:
                        result = await _browser_api(api("/api/license"))
                        value = result.get("license", result)
                        if isinstance(value, Mapping):
                            merged = dict(value)
                            merged["machine_code"] = result.get(
                                "machine_code", merged.get("machine_code", "")
                            )
                            update_license_summary(merged)
                            status_label.set_text(license_label(merged))
                            machine_label.set_text(
                                f"机器码：{merged.get('machine_code') or '—'}"
                            )
                    except Exception as exc:
                        status_label.set_text(f"授权状态读取失败：{_payload_error(exc)}")

                async def activate() -> None:
                    token = str(token_input.value or "").strip()
                    if not token:
                        ui.notify("请先粘贴注册码", type="warning")
                        return
                    try:
                        result = await _browser_api(
                            api("/api/license/activate"),
                            method="POST",
                            body={"token": token},
                        )
                        value = result.get("license", result)
                        if isinstance(value, Mapping):
                            merged = dict(value)
                            merged["machine_code"] = result.get(
                                "machine_code", merged.get("machine_code", "")
                            )
                            update_license_summary(merged)
                        token_input.set_value("")
                        ui.notify("软件授权已激活", type="positive")
                        await refresh_license()
                    except Exception as exc:
                        _notify_error(exc, "授权激活失败")

                with ui.row().classes("justify-end w-full"):
                    ui.button("取消", on_click=dialog.close).props("flat")
                    ui.button("激活授权", icon="verified_user", on_click=activate).props(
                        "unelevated"
                    ).classes("button-primary")
                ui.timer(0.05, refresh_license, once=True)
        dialog.open()

    def open_release_notes() -> None:
        with ui.dialog() as dialog, ui.card().classes("settings-dialog p-0"):
            with ui.element("div").classes("surface-head"):
                with ui.element("div"):
                    ui.label("版本说明").classes("text-xl")
                    ui.label("最近版本新增功能与修复")
                ui.button(icon="close", on_click=dialog.close).props("flat round")
            content = ui.element("div").classes("px-5 pb-5")

            async def load_notes() -> None:
                content.clear()
                try:
                    result = await _browser_api("/api/operations/release-notes")
                    releases = result.get("releases", [])
                    with content:
                        if not releases:
                            ui.label("暂无版本说明").classes("text-slate-500")
                        for index, release in enumerate(releases[:8]):
                            heading = " · ".join(
                                part
                                for part in (
                                    str(release.get("version") or ""),
                                    str(release.get("date") or ""),
                                )
                                if part
                            )
                            with ui.expansion(heading, value=index == 0).classes("w-full"):
                                for item in release.get("items", []):
                                    ui.label(f"• {item}").classes("text-sm leading-relaxed")
                except Exception as exc:
                    with content:
                        ui.label(f"版本说明读取失败：{_payload_error(exc)}").classes(
                            "text-negative"
                        )

            ui.timer(0.05, load_notes, once=True)
        dialog.open()

    def render_task() -> None:
        task = state.task
        status = _normal_status(task.get("status"))
        total = _task_count(task, "total", "total_count", "accession_count")
        processed = _task_count(task, "processed", "processed_count", "finished_count")
        percent = min(100, round(processed / total * 100)) if total else 0
        refs["runtime"].set_visibility(bool(task.get("id")) or status != "idle")
        refs["runtime_status"].set_text(STATUS_LABELS.get(status, status or "未知状态"))
        runtime_message = str(task.get("message") or "").strip()
        if _normal_status(runtime_message) == status and status in STATUS_LABELS:
            runtime_message = STATUS_MESSAGES.get(status, STATUS_LABELS[status])
        refs["runtime_message"].set_text(
            runtime_message or "后台任务状态会自动同步。"
        )
        refs["progress_text"].set_text(f"{processed:,} / {total:,} · {percent}%")
        refs["progress_fill"].style(f"width:{percent}%")
        refs["current"].set_text(str(task.get("current_accession") or "—"))
        refs["files"].set_text(f"{_task_count(task, 'file_count', 'received_files', 'files'):,}")
        refs["speed"].set_text(_format_speed(task.get("speed_bytes_per_second", task.get("speed_bps", 0))))
        refs["failed"].set_text(f"{_task_count(task, 'failed', 'failed_count', '失败'):,}")
        actions = task.get("actions") if isinstance(task.get("actions"), Mapping) else {}
        if bool(actions.get("can_start", status in TERMINAL_STATUSES | {"idle"})):
            refs["start"].set_enabled(state.preflight_ok)
        else:
            state.preflight_ok = False
            state.preflight_signature = ""
            refs["start"].disable()
            refs["readiness"].set_text(task_block_message())
        refs["pause"].set_visibility(bool(actions.get("can_pause", status in ACTIVE_STATUSES)))
        refs["resume"].set_visibility(bool(actions.get("can_resume", status in RESUMABLE_STATUSES)))
        refs["cancel"].set_visibility(bool(actions.get("can_cancel", status not in TERMINAL_STATUSES | {"idle"})))
        refs["retry"].set_visibility(bool(actions.get("can_retry_failed", _task_count(task, "failed", "failed_count") > 0)))
        refs["accept"].set_visibility(bool(actions.get("can_accept_partial")))
        pdi = task.get("pdi")
        has_pdi = isinstance(pdi, Mapping)
        refs["pdi_runtime"].set_visibility(has_pdi)
        if has_pdi:
            pdi_status = _normal_status(pdi.get("status"))
            pdi_finished = pdi_status in {"completed", "partial", "partial_success"}
            refs["pdi_status"].set_text(STATUS_LABELS.get(pdi_status, pdi_status or "PDI"))
            refs["pdi_message"].set_text(
                str(
                    pdi.get("message")
                    or pdi.get("detail")
                    or pdi.get("output_directory")
                    or "PDI 状态已更新"
                )
            )
            refs["pdi_open"].set_visibility(pdi_finished and local_session)
            refs["pdi_verify"].set_visibility(
                pdi_finished and bool(actions.get("can_verify_pdi", True))
            )
            refs["pdi_retry"].set_visibility(
                bool(actions.get("can_retry_pdi"))
                or pdi_status in {"failed", "partial", "partial_success"}
            )
        render_errors(task.get("error_logs") or [])
        render_results(task, total)

    def render_errors(errors: list[dict[str, Any]]) -> None:
        refs["errors"].clear()
        with refs["errors"]:
            if errors:
                latest = errors[-1]
                with ui.element("div").classes("error-block"):
                    ui.label(f"{len(errors)} 条错误需要关注").classes("font-semibold")
                    ui.label(str(latest.get("message") or latest.get("detail") or "任务报告了错误"))

    def render_results(task: Mapping[str, Any], total: int) -> None:
        refs["results"].clear()
        with refs["results"]:
            if total > DETAIL_LIMIT or bool(task.get("large_batch")):
                with ui.element("div").classes("large-summary"):
                    ui.label(f"大型任务 · 共 {total:,} 个检查号").classes("font-semibold")
                    ui.label("超过 200 条，已隐藏逐项列表。任务进度和完成统计仍会持续更新。")
                    ui.label(
                        f"完成 {_task_count(task, 'completed', 'completed_count'):,} · "
                        f"部分 {_task_count(task, 'partial', 'partial_count'):,} · "
                        f"失败 {_task_count(task, 'failed', 'failed_count'):,}"
                    ).classes("text-sm mt-2")
                return
            items = task.get("results") or []
            if not isinstance(items, list) or not items:
                ui.label("任务结果将在这里出现").classes("text-sm text-slate-400")
                return
            rows = []
            for item in items[:DETAIL_LIMIT]:
                if not isinstance(item, Mapping):
                    continue
                rows.append({
                    "accession": item.get("accession") or item.get("accession_number") or "—",
                    "status": STATUS_LABELS.get(_normal_status(item.get("status")), str(item.get("status") or "—")),
                    "files": item.get("file_count", item.get("files", 0)),
                    "message": item.get("error_summary") or item.get("message") or "—",
                })
            ui.table(
                columns=[
                    {"name": "accession", "label": "检查号", "field": "accession", "align": "left"},
                    {"name": "status", "label": "状态", "field": "status", "align": "left"},
                    {"name": "files", "label": "文件", "field": "files", "align": "right"},
                    {"name": "message", "label": "说明", "field": "message", "align": "left"},
                ],
                rows=rows,
                row_key="accession",
                pagination={"rowsPerPage": 10},
            ).classes("result-table w-full").props("flat dense")

    async def poll() -> None:
        try:
            payload = await _browser_api(api("/api/task"))
            task = payload.get("task") if isinstance(payload, dict) else None
            if isinstance(task, dict):
                state.task = task
                render_task()
            events = await _browser_api(api(f"/api/events?after_id={state.event_cursor}&limit=100"))
            for entry in events.get("events", []):
                try:
                    state.event_cursor = max(state.event_cursor, int(entry.get("id", 0)))
                except (TypeError, ValueError):
                    pass
                payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else entry
                if entry.get("type") == "log" or payload.get("message"):
                    state.logs.append(dict(payload))
            state.logs = state.logs[-200:]
            render_logs()
        except Exception:
            # Polling is best-effort; direct actions still surface API errors.
            return

    def render_logs() -> None:
        refs["logs"].clear()
        visible = (
            state.logs
            if state.show_detailed_logs
            else [
                item
                for item in state.logs
                if str(item.get("level", "")).lower() in {"error", "critical"}
            ]
        )
        ordered = sorted(
            visible,
            key=lambda item: str(item.get("level", "")).lower()
            not in {"error", "critical"},
        )
        with refs["logs"]:
            if not ordered:
                ui.label(
                    "暂无错误日志" if not state.show_detailed_logs else "暂无日志"
                ).classes("text-xs text-slate-500")
            for item in ordered[-60:]:
                level = str(item.get("level", "info")).lower()
                timestamp = str(item.get("timestamp", ""))[11:19] or "--:--:--"
                with ui.element("div").classes("log-line"):
                    ui.label(timestamp).classes("log-time")
                    ui.label(str(item.get("message") or item.get("detail") or item)).classes("log-error" if level == "error" else "log-info")

    def setting_input(label: str, key: str, *, kind: str = "text") -> Any:
        value = state.config.get(key, "")
        if kind == "number":
            return ui.input(label, value=str(value)).props(
                "outlined dense inputmode=numeric pattern=[0-9]*"
            )
        return ui.input(label, value=value).props("outlined dense")

    def open_settings() -> None:
        with ui.dialog() as dialog, ui.card().classes("settings-dialog p-0"):
            with ui.element("div").classes("surface-head"):
                with ui.element("div"):
                    ui.label("DcmGet 设置").classes("text-xl")
                    ui.label("运行中的任务继续使用启动时的配置快照")
                ui.button(icon="close", on_click=dialog.close).props("flat round")
            fields: dict[str, Any] = {}
            with ui.element("div").classes("px-5 pb-4"):
                with ui.expansion("PACS 连接", icon="hub", value=True).classes("w-full"):
                    with ui.element("div").classes("settings-grid settings-section"):
                        fields["pacs_server_ip"] = setting_input("PACS 地址", "pacs_server_ip")
                        fields["pacs_server_port"] = setting_input("PACS 端口", "pacs_server_port", kind="number")
                        fields["calling_ae_title"] = setting_input("本机调用 AE Title", "calling_ae_title")
                        fields["pacs_ae_title"] = setting_input("PACS AE Title", "pacs_ae_title")
                with ui.expansion("本机接收", icon="download").classes("w-full"):
                    with ui.element("div").classes("settings-grid settings-section"):
                        fields["storage_ae_title"] = setting_input("接收 AE Title", "storage_ae_title")
                        fields["storage_port"] = setting_input("接收端口", "storage_port", kind="number")
                        fields["dcmtk_bin_dir"] = setting_input("DCMTK 目录", "dcmtk_bin_dir")
                with ui.expansion("路径与整理", icon="folder").classes("w-full"):
                    with ui.element("div").classes("settings-grid settings-section"):
                        fields["dicom_destination_folder"] = setting_input("DICOM 保存目录", "dicom_destination_folder")
                        fields["directory_template"] = setting_input("目录模板", "directory_template")
                        fields["pdi_output_folder"] = setting_input("PDI 输出目录", "pdi_output_folder")
                        fields["pdi_institution_name"] = setting_input("PDI 机构名称", "pdi_institution_name")
                with ui.expansion("匿名化与 PDI", icon="shield").classes("w-full"):
                    with ui.element("div").classes("settings-grid settings-section"):
                        fields["anonymization_enabled"] = ui.switch("启用匿名化", value=bool(state.config.get("anonymization_enabled")))
                        fields["anonymization_profile"] = ui.select(
                            {"basic": "基础脱敏", "research": "研究匿名（推荐）", "strict": "严格元数据匿名"},
                            value=state.config.get("anonymization_profile", "research"), label="匿名方案"
                        ).props("outlined dense")
                        fields["pdi_export_enabled"] = ui.switch("默认导出 PDI", value=bool(state.config.get("pdi_export_enabled")))
                        fields["pdi_include_ohif_viewer"] = ui.switch("包含 OHIF 查看器", value=bool(state.config.get("pdi_include_ohif_viewer", True)))
                with ui.expansion("高级", icon="tune").classes("w-full"):
                    with ui.element("div").classes("settings-grid settings-section"):
                        fields["minimum_free_space_bytes"] = setting_input("磁盘保留空间（字节）", "minimum_free_space_bytes", kind="number")
                        fields["auto_retry_attempts"] = setting_input("自动重试次数", "auto_retry_attempts", kind="number")
                        fields["auto_retry_backoff_seconds"] = setting_input("重试等待秒数", "auto_retry_backoff_seconds", kind="number")
                        fields["circuit_breaker_failures"] = setting_input("连续失败暂停阈值", "circuit_breaker_failures", kind="number")

                async def save_settings() -> None:
                    body: dict[str, Any] = {}
                    numeric = {
                        "pacs_server_port", "storage_port",
                        "minimum_free_space_bytes", "auto_retry_attempts",
                        "auto_retry_backoff_seconds", "circuit_breaker_failures",
                    }
                    for key, control in fields.items():
                        value = control.value
                        body[key] = int(value or 0) if key in numeric else value
                    try:
                        result = await _browser_api(api("/api/config"), method="PUT", body=body)
                        state.config = dict(result.get("config") or result)
                        refs["destination"].set_value(state.config.get("dicom_destination_folder", ""))
                        refs["pdi_enabled"].set_value(bool(state.config.get("pdi_export_enabled")))
                        refs["pdi_row"].set_visibility(
                            bool(state.config.get("pdi_export_enabled"))
                        )
                        refs["pdi_folder"].set_value(state.config.get("pdi_output_folder", ""))
                        dialog.close()
                        invalidate_preflight()
                        ui.notify(str(result.get("message") or "设置已保存"), type="positive")
                    except Exception as exc:
                        _notify_error(exc, "保存失败")

                with ui.row().classes("justify-end w-full pt-4"):
                    ui.button("取消", on_click=dialog.close).props("flat")
                    ui.button("保存设置", icon="save", on_click=save_settings).props("unelevated").classes("button-primary")
        dialog.open()

    profile_name = str(bootstrap.get("profile_name") or (bootstrap.get("profile") or {}).get("name") or "默认 Profile")
    config = state.config
    web = bootstrap.get("web") if isinstance(bootstrap.get("web"), Mapping) else {}
    local_session = bool(web.get("local_session", bootstrap.get("local_session", False)))
    license_data = (
        dict(bootstrap.get("license"))
        if isinstance(bootstrap.get("license"), Mapping)
        else {}
    )

    def quick_pdi_changed(event: Any) -> None:
        invalidate_preflight()
        row = refs.get("pdi_row")
        if row is not None:
            row.set_visibility(bool(getattr(event, "value", False)))

    with ui.element("main").classes("workspace"):
        _topbar(profile_name)
        if managed_profiles and managed_profile_number is not None:
            options = {
                int(profile.get("number", 0)): str(
                    profile.get("display_name") or f"Profile {profile.get('number', '')}"
                )
                for profile in managed_profiles
                if int(profile.get("number", 0) or 0) > 0
            }

            def switch_profile(event: Any) -> None:
                try:
                    selected = int(getattr(event, "value", 0) or 0)
                except (TypeError, ValueError):
                    return
                if selected and selected != managed_profile_number:
                    ui.navigate.to(f"./?profile={selected}")

            with ui.row().classes("items-center justify-between w-full mb-5 gap-3"):
                ui.button(
                    "Profile 管理",
                    icon="arrow_back",
                    on_click=lambda: ui.navigate.to("./"),
                ).props("flat no-caps").classes("button-quiet")
                ui.select(
                    options,
                    value=managed_profile_number,
                    label="快速切换 Profile",
                    on_change=switch_profile,
                ).props("outlined dense options-dense").classes("w-64 max-w-full")
        with ui.element("section").classes("hero"):
            with ui.element("div"):
                ui.label("DICOM RETRIEVAL WORKSPACE").classes("eyebrow")
                ui.label("把影像取回这件事，变得简单而确定。 ").classes("headline")
                ui.label("粘贴检查号，确认保存位置，然后开始。预检、接收、下载、失败重试与 PDI 导出均由后台持续执行。").classes("lede")
            with ui.element("div").classes("hero-panel"):
                with ui.element("div").classes("surface summary-card"):
                    with ui.element("div").classes("summary-head"):
                        with ui.element("div").classes("summary-copy"):
                            ui.label("当前工作上下文").classes("eyebrow")
                            ui.label("主流程只保留任务输入、预检与下载控制。").classes("text-sm")
                        refs["license_badge"] = ui.label(
                            license_label(license_data)
                        ).classes("summary-badge")
                    with ui.element("div").classes("hero-facts"):
                        _fact("当前 PROFILE", profile_name)
                        _fact("PACS", f"{config.get('pacs_server_ip', '—')}:{config.get('pacs_server_port', '—')}")
                        _fact("接收端", f"{config.get('storage_ae_title', '—')}:{config.get('storage_port', '—')}")
                        _fact("WEB", str(web.get("lan_url") or web.get("url") or "—"))
                with ui.element("div").classes("hero-actions"):
                    ui.button("当前设置", icon="settings", on_click=open_settings).props("flat no-caps").classes("button-quiet")
                    ui.button("版本说明", icon="history", on_click=open_release_notes).props("flat no-caps").classes("button-quiet")
                    if local_session:
                        ui.button("打开保存目录", icon="folder_open", on_click=lambda: operation("open-destination")).props("flat no-caps").classes("button-quiet")

        with ui.element("section").classes("launch-bar"):
            with ui.element("div").classes("launch-copy"):
                ui.label("准备好后即可开始").classes("launch-title")
                refs["readiness"] = ui.label(
                    "输入检查号和保存目录后将自动预检"
                ).classes("launch-state")
            with ui.element("div").classes("launch-actions"):
                refs["preflight"] = ui.button(
                    "立即预检",
                    icon="fact_check",
                    on_click=run_preflight,
                ).props("flat no-caps").classes("button-quiet")
                refs["start"] = ui.button(
                    "开始下载",
                    icon="play_arrow",
                    on_click=start_task,
                ).props("unelevated size=lg no-caps").classes("button-primary")
                refs["start"].disable()

        with ui.element("section").classes("grid-main"):
            with ui.element("div").classes("stack"):
                with ui.element("article").classes("surface"):
                    with ui.element("div").classes("surface-head"):
                        with ui.element("div").classes("section-copy"):
                            ui.label("01 / 输入检查号").classes("step-index")
                            ui.label("新建下载任务").classes("text-xl")
                            ui.label("支持粘贴和导入文件；大批量任务会自动切换为聚合视图。").classes("section-note")
                        with ui.row().classes("items-center gap-2"):
                            ui.button("清空", icon="delete_sweep", on_click=clear_accessions).props("flat").classes("button-quiet")
                            ui.upload(
                                label="导入 TXT / CSV / XLSX",
                                auto_upload=True,
                                max_file_size=32 * 1024 * 1024,
                                on_upload=import_upload,
                            ).props("accept=.txt,.csv,.xlsx flat color=secondary").classes("drop-upload max-w-xs")
                    with ui.element("div").classes("surface-body"):
                        refs["input"] = ui.textarea(
                            label="每行一个检查号",
                            placeholder="例如：\n202607210001\n202607210002",
                            on_change=lambda: parse_pasted(),
                        ).props("outlined autogrow debounce=400").classes("accession-input w-full")
                        refs["large_import"] = ui.label("").classes("large-summary").set_visibility(False)
                        with ui.element("div").classes("inline-stats"):
                            for label, key in (("有效", "valid"), ("重复", "duplicate"), ("空行", "blank"), ("无效", "invalid")):
                                with ui.element("span").classes("stat-pill"):
                                    ui.label(label)
                                    refs[key] = ui.label("0").classes("font-semibold")

                with ui.element("article").classes("surface"):
                    with ui.element("div").classes("surface-head"):
                        with ui.element("div").classes("section-copy"):
                            ui.label("02 / 保存与交付").classes("step-index")
                            ui.label("快速选项").classes("text-xl")
                            ui.label("常用参数留在当前页，低频参数进入完整设置。").classes("section-note")
                        ui.button("完整设置", icon="settings", on_click=open_settings).props("flat").classes("button-quiet")
                    with ui.element("div").classes("surface-body stack"):
                        with ui.element("div").classes("quick-grid"):
                            refs["destination"] = ui.input("DICOM 保存目录", value=config.get("dicom_destination_folder", ""), on_change=invalidate_preflight).props("outlined")
                            ui.button("浏览", icon="folder_open", on_click=lambda: browse_directory(refs["destination"], "destination")).props("flat").classes("button-quiet")
                        refs["pdi_enabled"] = ui.switch(
                            "下载完成后生成 PDI",
                            value=bool(config.get("pdi_export_enabled")),
                            on_change=quick_pdi_changed,
                        )
                        refs["pdi_row"] = ui.element("div").classes("quick-grid")
                        with refs["pdi_row"]:
                            refs["pdi_folder"] = ui.input("PDI 输出目录（可选）", value=config.get("pdi_output_folder", ""), on_change=invalidate_preflight).props("outlined")
                            ui.button("浏览", icon="folder_open", on_click=lambda: browse_directory(refs["pdi_folder"], "pdi")).props("flat").classes("button-quiet")
                        refs["pdi_row"].set_visibility(bool(config.get("pdi_export_enabled")))

                with ui.element("article").classes("surface"):
                    with ui.element("div").classes("surface-head"):
                        with ui.element("div").classes("section-copy"):
                            ui.label("03 / 启动前确认").classes("step-index")
                            ui.label("自动预检").classes("text-xl")
                            ui.label("会检查配置、DCMTK、保存目录以及当前任务是否仍占用接收端口。").classes("section-note")
                    with ui.element("div").classes("surface-body"):
                        refs["checks"] = ui.element("div").classes("preflight-list")
                        render_checks([])

                refs["runtime"] = ui.element("article").classes("surface")
                with refs["runtime"]:
                    with ui.element("div").classes("surface-head"):
                        with ui.element("div"):
                            refs["runtime_status"] = ui.label("等待任务").classes("text-xl")
                            refs["runtime_message"] = ui.label("后台任务状态会自动同步")
                        refs["progress_text"] = ui.label("0 / 0 · 0%").classes("progress-label")
                    with ui.element("div").classes("surface-body"):
                        refs["errors"] = ui.element("div")
                        with ui.element("div").classes("progress-rail"):
                            refs["progress_fill"] = ui.element("div").classes("progress-fill")
                        with ui.element("div").classes("metric-grid"):
                            for label, key in (("当前检查号", "current"), ("接收文件", "files"), ("实时速度", "speed"), ("失败", "failed")):
                                with ui.element("div").classes("metric"):
                                    ui.label(label)
                                    refs[key] = ui.label("—" if key == "current" else "0")
                        with ui.element("div").classes("action-row"):
                            refs["pause"] = ui.button("暂停", icon="pause", on_click=lambda: task_action("pause")).props("flat").classes("button-quiet")
                            refs["resume"] = ui.button("继续", icon="play_arrow", on_click=lambda: task_action("resume")).props("unelevated no-caps").classes("button-primary")
                            refs["cancel"] = ui.button("取消任务", icon="close", on_click=lambda: task_action("cancel")).props("flat").classes("button-danger")
                            refs["retry"] = ui.button("重试失败项", icon="refresh", on_click=lambda: task_action("retry-failed")).props("flat").classes("button-quiet")
                            refs["accept"] = ui.button("接受已有文件", icon="done_all", on_click=lambda: task_action("accept-partial")).props("flat").classes("button-quiet")
                        refs["pdi_runtime"] = ui.element("div").classes("large-summary mt-5")
                        with refs["pdi_runtime"]:
                            refs["pdi_status"] = ui.label("PDI").classes("font-semibold")
                            refs["pdi_message"] = ui.label("PDI 状态已更新").classes("text-sm")
                            with ui.row().classes("action-row"):
                                refs["pdi_open"] = ui.button("打开 PDI", icon="folder_open", on_click=lambda: pdi_action("open")).props("flat").classes("button-quiet")
                                refs["pdi_verify"] = ui.button("校验 PDI", icon="verified", on_click=lambda: pdi_action("verify")).props("flat").classes("button-quiet")
                                refs["pdi_retry"] = ui.button("重试 PDI", icon="refresh", on_click=lambda: pdi_action("retry")).props("flat").classes("button-quiet")
                        ui.separator().classes("my-5 opacity-20")
                        refs["results"] = ui.element("div")

            with ui.element("aside").classes("stack"):
                with ui.element("article").classes("surface"):
                    with ui.element("div").classes("surface-head"):
                        with ui.element("div"):
                            ui.label("软件授权").classes("text-lg")
                            refs["license_summary"] = ui.label(
                                license_label(license_data)
                            ).classes("text-sm text-slate-500")
                    with ui.element("div").classes("surface-body"):
                        ui.button(
                            "查看机器码 / 激活",
                            icon="verified_user",
                            on_click=open_license_dialog,
                        ).props("flat no-caps").classes("button-quiet w-full")
                with ui.element("article").classes("surface"):
                    with ui.element("div").classes("surface-head"):
                        with ui.element("div"):
                            ui.label("工具与版本").classes("text-lg")
                            ui.label("低频功能集中放置，保持主流程简洁")
                    with ui.element("div").classes("surface-body"):
                        with ui.expansion("展开工具", icon="build").classes("w-full"):
                            with ui.element("div").classes("stack pt-2"):
                                ui.button(
                                    "生成验收报告",
                                    icon="assignment_turned_in",
                                    on_click=lambda: operation("acceptance-report"),
                                ).props("flat no-caps").classes("button-quiet w-full")
                                ui.button(
                                    "生成脱敏支持包",
                                    icon="support_agent",
                                    on_click=lambda: operation("support-bundle"),
                                ).props("flat no-caps").classes("button-quiet w-full")
                                ui.button(
                                    "备份 Profile",
                                    icon="archive",
                                    on_click=lambda: operation("profile-backup"),
                                ).props("flat no-caps").classes("button-quiet w-full")
                                ui.button(
                                    "版本说明",
                                    icon="history",
                                    on_click=open_release_notes,
                                ).props("flat no-caps").classes("button-quiet w-full")
                if local_session:
                    with ui.element("article").classes("surface"):
                        with ui.element("div").classes("surface-head"):
                            with ui.element("div"):
                                ui.label("本机操作").classes("text-lg")
                                ui.label("仅服务器本机会执行打开目录")
                        with ui.element("div").classes("surface-body stack"):
                            ui.button("打开保存目录", icon="folder_open", on_click=lambda: operation("open-destination")).props("flat no-caps").classes("button-quiet w-full")
                            ui.button("打开日志目录", icon="description", on_click=lambda: operation("open-log-directory")).props("flat no-caps").classes("button-quiet w-full")
                            ui.button("运行环境检查", icon="health_and_safety", on_click=lambda: operation("health")).props("flat no-caps").classes("button-quiet w-full")
                with ui.element("article").classes("surface"):
                    with ui.element("div").classes("surface-head"):
                        with ui.element("div"):
                            ui.label("错误优先日志").classes("text-lg")
                            ui.label("错误置顶；展开查看最近事件")
                    with ui.element("div").classes("surface-body"):
                        with ui.expansion("查看日志详情", icon="terminal").classes("w-full"):
                            def toggle_detailed_logs(event: Any) -> None:
                                state.show_detailed_logs = bool(getattr(event, "value", False))
                                render_logs()

                            ui.switch(
                                "显示详细日志",
                                value=False,
                                on_change=toggle_detailed_logs,
                            ).props("dense")
                            refs["logs"] = ui.element("div")

    render_import()
    render_task()
    render_logs()
    ui.timer(0.8, automatic_preflight)
    ui.timer(1.5, poll)


def install_nicegui(app: Any, mount_path: str = "/workspace") -> None:
    """Mount the DcmGet NiceGUI workspace on an existing FastAPI app."""

    normalized_mount = "/" + str(mount_path).strip("/")
    state = getattr(app, "state", None)
    marker = "_dcmget_nicegui_mount_path"
    if state is not None and getattr(state, marker, None) is not None:
        if getattr(state, marker) != normalized_mount:
            raise RuntimeError("NiceGUI 已挂载到其他路径")
        return

    @ui.page("/")
    async def dcmget_workspace(request: Request) -> None:
        ui.add_head_html(THEME_BOOTSTRAP)
        ui.add_css(CSS)
        ui.colors(
            primary="#147da6",
            secondary="#4d8f98",
            positive="#248663",
            negative="#c54848",
        )
        root = ui.element("div").classes("w-full")
        with root:
            with ui.element("main").classes("workspace"):
                _topbar("正在连接", connection_state="connecting")
                with ui.element("div").classes("surface surface-body loading-card"):
                    ui.spinner("dots", size="2.5rem", color="primary")
                    ui.label("正在读取 Profile 与任务状态…")

        async def initialize() -> None:
            try:
                bootstrap = await _browser_api("/api/bootstrap")
            except Exception as exc:
                root.clear()
                with root:
                    with ui.element("main").classes("workspace"):
                        _topbar("连接失败", connection_state="error")
                        with ui.element("div").classes("error-block mt-8"):
                            ui.label("无法连接 DcmGet API").classes("font-semibold")
                            ui.label(_payload_error(exc))
                return
            root.clear()
            with root:
                if str(bootstrap.get("mode", "profile")).lower() == "manager":
                    raw_profile = request.query_params.get("profile", "").strip()
                    if raw_profile.isdigit() and 1 <= int(raw_profile) <= 9999:
                        profile_number = int(raw_profile)
                        try:
                            profile_payload = await _browser_api("/api/management/profiles")
                            source = (
                                profile_payload.get(
                                    "profiles",
                                    profile_payload.get("items", profile_payload),
                                )
                                if isinstance(profile_payload, dict)
                                else profile_payload
                            )
                            profiles = (
                                [dict(item) for item in source if isinstance(item, Mapping)]
                                if isinstance(source, list)
                                else []
                            )
                            selected = next(
                                (
                                    item
                                    for item in profiles
                                    if int(item.get("number", 0) or 0) == profile_number
                                ),
                                None,
                            )
                            if selected is None:
                                raise RuntimeError(f"Profile {profile_number} 不存在")
                            if not bool(selected.get("is_running")):
                                raise RuntimeError(
                                    f"Profile {profile_number} 尚未启动，请先返回管理页启动"
                                )
                            profile_bootstrap = await _browser_api(
                                f"/api/management/profiles/{profile_number}/bootstrap"
                            )
                            await _build_profile(
                                dict(profile_bootstrap),
                                api_prefix=f"/api/management/profiles/{profile_number}",
                                managed_profiles=profiles,
                                managed_profile_number=profile_number,
                            )
                            return
                        except Exception as exc:
                            _notify_error(exc, "无法打开 Profile")
                    await _build_manager(dict(bootstrap))
                else:
                    await _build_profile(dict(bootstrap))

        ui.timer(0.05, initialize, once=True)

    if state is not None:
        setattr(state, marker, normalized_mount)
    favicon = Path(__file__).resolve().parents[1] / "logo.png"
    ui.run_with(
        app,
        mount_path=normalized_mount,
        title="DcmGet 影像下载工作台",
        favicon=favicon if favicon.is_file() else None,
        language="zh-CN",
        dark=False,
        reconnect_timeout=15.0,
        show_welcome_message=False,
    )


__all__ = ["install_nicegui"]
