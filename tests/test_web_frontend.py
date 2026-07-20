from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "dcmget" / "webui"
INDEX = WEB_ROOT / "index.html"
CSS = WEB_ROOT / "app.css"
JAVASCRIPT = WEB_ROOT / "app.js"


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.references: list[tuple[str, str]] = []
        self.scripts: list[dict[str, str]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = {key: value or "" for key, value in attrs}
        if values.get("id"):
            self.ids.append(values["id"])
        for attribute in ("src", "href"):
            if values.get(attribute):
                self.references.append((attribute, values[attribute]))
        if tag == "script":
            self.scripts.append(values)


def _document() -> tuple[str, _DocumentParser]:
    source = INDEX.read_text(encoding="utf-8")
    parser = _DocumentParser()
    parser.feed(source)
    return source, parser


def _javascript() -> str:
    return JAVASCRIPT.read_text(encoding="utf-8")


def test_web_frontend_is_a_self_contained_es_module_application() -> None:
    source, document = _document()

    assert INDEX.is_file()
    assert CSS.is_file()
    assert JAVASCRIPT.is_file()
    assert len(document.ids) == len(set(document.ids))
    assert {"login-screen", "app-shell", "page-home", "page-settings", "page-operations"} <= set(document.ids)
    assert any(
        script.get("type") == "module" and script.get("src") == "/assets/app.js"
        for script in document.scripts
    )
    assert 'href="/assets/app.css"' in source
    assert all(
        value.startswith(("/", "#"))
        for _attribute, value in document.references
    )


def test_web_frontend_never_depends_on_network_assets_or_node_runtime() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in (INDEX, CSS, JAVASCRIPT)
    ).lower()

    assert "https://" not in combined
    assert "http://" not in combined
    assert "@import" not in CSS.read_text(encoding="utf-8").lower()
    assert "node_modules" not in combined
    assert "unpkg" not in combined
    assert "jsdelivr" not in combined
    assert "googleapis" not in combined


def test_every_static_javascript_id_selector_exists_in_html() -> None:
    _source, document = _document()
    javascript = _javascript()
    referenced_ids = set(
        re.findall(r"(?:querySelector|\$)\(\s*[`\"]#([A-Za-z][\w-]*)", javascript)
    )

    assert referenced_ids
    assert referenced_ids <= set(document.ids)


def test_all_state_changing_requests_use_the_csrf_api_wrapper() -> None:
    javascript = _javascript()

    assert 'headers.set("X-CSRF-Token", state.csrfToken || "")' in javascript


def test_remote_sessions_hide_host_only_controls_and_skip_profile_refresh() -> None:
    javascript = _javascript()

    assert "web.local_session !== false" in javascript
    assert '$("#profile-management-card").hidden = !state.localSession' in javascript
    assert 'if (state.localSession) refreshes.push(refreshProfiles())' in javascript
    for operation in ("open-data-directory", "open-log-directory", "acceptance-report"):
        assert f'[data-operation="{operation}"]' in javascript


def test_bootstrap_overwrites_browser_restored_destination_with_profile_config() -> None:
    javascript = _javascript()

    assert '$("#destination-input").value = state.config.dicom_destination_folder || ""' in javascript


def test_health_ui_distinguishes_intentional_http_warning_from_failure() -> None:
    javascript = _javascript()
    css = CSS.read_text(encoding="utf-8")

    assert 'check.severity === "warning"' in javascript
    assert 'item.dataset.state = warning ? "warning"' in javascript
    assert '.health-list li[data-state="warning"]' in css
    assert javascript.count("fetch(") == 1
    assert 'method: "POST"' in javascript
    assert 'method: "PUT"' in javascript
    assert 'firstRun ? "/api/setup" : "/api/login"' in javascript
    assert 'api("/api/logout"' in javascript
    assert 'api("/api/preflight"' in javascript
    assert 'api("/api/task/start"' in javascript
    assert 'api("/api/pdi/open"' in javascript
    assert 'api("/api/pdi/retry"' in javascript
    assert 'api("/api/pdi/verify"' in javascript


def test_task_control_and_live_updates_cover_background_operation() -> None:
    source, _document_parser = _document()
    javascript = _javascript()

    for action in ("pause", "resume", "cancel", "retry", "accept-partial"):
        assert f'taskAction("{action}"' in javascript
    assert 'new EventSource("/api/events/stream"' in javascript
    assert "visibilitychange" in javascript
    assert "关闭浏览器不会停止" in source
    assert "beforeunload" not in javascript


def test_large_tasks_use_aggregate_rendering_after_200_accessions() -> None:
    source, _document_parser = _document()
    javascript = _javascript()

    assert "const DETAIL_LIMIT = 200" in javascript
    assert "total > DETAIL_LIMIT" in javascript
    assert "items.slice(0, DETAIL_LIMIT)" in javascript
    assert 'id="large-task-summary"' in source
    assert "超过 200 条检查号" in source


def test_logs_default_to_errors_until_user_opts_in() -> None:
    source, _document_parser = _document()
    javascript = _javascript()

    assert 'showDetailedLogs: false' in javascript
    assert 'new Set(["ERROR", "CRITICAL"])' in javascript
    assert 'id="detailed-log-toggle" type="checkbox"' in source
    assert "默认仅显示错误日志" in source


def test_recovery_large_batch_and_sse_contracts_are_visible() -> None:
    javascript = _javascript()

    for status in ("interrupted", "download_retryable", "pdi_retryable", "recovery_error"):
        assert f"{status}:" in javascript
    assert "task?.status_counts?.[key]" in javascript
    assert "payload.payload ?? payload.data ?? payload" in javascript
    assert "scheduleTaskRefresh()" in javascript
    assert "can_setup_here" in javascript
    assert "首次密码只能在运行 DcmGet 的主机本机设置" in javascript


def test_login_and_lan_settings_explain_plain_http_risk() -> None:
    source, _document_parser = _document()

    assert source.count("仅限可信内网") >= 1
    assert source.count("HTTP") >= 2
    assert "流量未加密" in source
    assert 'id="setting-lan-enabled"' in source
    assert 'id="setting-auth-required"' in source
    assert "不要将端口映射到公网" in source


def test_browser_directory_picker_explicitly_targets_the_server() -> None:
    source, _document_parser = _document()
    javascript = _javascript()

    assert source.count("运行 DcmGet 的主机") >= 3
    assert "/api/files/directories?" in javascript
    assert 'purpose: state.directoryPurpose' in javascript
    assert 'id="directory-dialog"' in source


def test_accession_import_is_local_for_text_and_binary_for_xlsx() -> None:
    source, _document_parser = _document()
    javascript = _javascript()

    assert ".txt,.csv,.xlsx" in source
    assert "await file.text()" in javascript
    assert 'api("/api/files/accessions"' in javascript
    assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in javascript
    assert '"X-File-Name": encodeURIComponent(file.name)' in javascript


def test_settings_cover_clinical_network_reliability_privacy_and_pdi() -> None:
    source, _document_parser = _document()

    expected_names = {
        "dcmtk_bin_dir",
        "pacs_server_ip",
        "pacs_server_port",
        "calling_ae_title",
        "pacs_ae_title",
        "storage_ae_title",
        "storage_port",
        "directory_template",
        "auto_retry_attempts",
        "auto_retry_backoff_seconds",
        "circuit_breaker_failures",
        "anonymization_enabled",
        "anonymization_profile",
        "pdi_export_enabled",
        "pdi_institution_name",
        "pdi_include_ohif_viewer",
        "pdi_volume_size_gb",
        "web_open_browser",
    }
    present_names = set(re.findall(r'\bname="([^"]+)"', source))
    assert expected_names <= present_names
    assert 'data.web_bind_address = data.web_lan_enabled ? "0.0.0.0" : "127.0.0.1"' in _javascript()
    assert 'id="setting-auth-required" name="web_auth_required" type="checkbox" checked disabled' in source
    assert 'name="max_concurrent_moves"' not in source


def test_pacs_and_local_receiver_settings_are_separate_groups() -> None:
    source, _document_parser = _document()

    pacs_start = source.index('id="pacs-settings-title"')
    receiver_start = source.index('id="receiver-settings-title"')
    storage_start = source.index('id="storage-settings-title"')
    assert pacs_start < receiver_start < storage_start
    assert source.index('name="pacs_server_ip"', pacs_start, receiver_start) > pacs_start
    assert source.index('name="pacs_ae_title"', pacs_start, receiver_start) > pacs_start
    assert source.index('name="calling_ae_title"', receiver_start, storage_start) > receiver_start
    assert source.index('name="storage_ae_title"', receiver_start, storage_start) > receiver_start
    assert source.index('name="storage_port"', receiver_start, storage_start) > receiver_start


def test_pdi_is_off_by_default_but_preserves_an_explicit_saved_preference() -> None:
    source, _document_parser = _document()
    javascript = _javascript()

    assert '默认关闭' in source
    assert 'id="quick-pdi-enabled" type="checkbox"' in source
    assert 'id="setting-pdi-enabled" name="pdi_export_enabled" type="checkbox"' in source
    assert 'id="quick-pdi-enabled" type="checkbox" checked' not in source
    assert '$("#quick-pdi-enabled").checked = Boolean(config.pdi_export_enabled)' in javascript
    assert 'id="setting-pdi-options"' in source
    assert 'syncPdiSettingsUi()' in javascript


def test_preflight_runs_automatically_and_discards_stale_responses() -> None:
    javascript = _javascript()

    assert "schedulePreflight(0)" in javascript
    assert "runPreflight({ requireAccessions: false, silent: true })" in javascript
    assert "const requestId = ++state.preflightRequestId" in javascript
    assert "requestId !== state.preflightRequestId || signature !== draftSignature()" in javascript
    assert '$("#run-preflight-button").addEventListener("click", () => runPreflight())' in javascript


def test_start_and_local_shutdown_require_current_password_confirmation() -> None:
    source, _document_parser = _document()
    javascript = _javascript()

    assert 'id="confirm-password" type="password" autocomplete="current-password"' in source
    assert 'id="shutdown-service-button"' in source
    assert 'body: { ...taskDraft(), password }' in javascript
    assert 'api("/api/operations/shutdown"' in javascript
    assert "requirePassword: true" in javascript
    assert '"#shutdown-service-button"' in javascript


def test_offline_license_activation_exposes_machine_code_and_token_input() -> None:
    source, _document_parser = _document()
    javascript = _javascript()

    assert 'id="license-machine-code"' in source
    assert 'id="license-token-input"' in source
    assert 'api("/api/license/activate"' in javascript
    assert 'api("/api/license"' in javascript
    assert "license.registered" in javascript


def test_ui_has_accessibility_and_responsive_baselines() -> None:
    source, _document_parser = _document()
    css = CSS.read_text(encoding="utf-8")
    javascript = _javascript()

    assert '<html lang="zh-CN">' in source
    assert 'class="skip-link"' in source
    assert 'aria-live="polite"' in source
    assert 'role="progressbar"' in source
    assert "@media (max-width: 1080px)" in css
    assert "@media (max-width: 840px)" in css
    assert "@media (max-width: 620px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "innerHTML" not in javascript
    assert ".textContent" in javascript
