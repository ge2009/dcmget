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


def test_web_frontend_is_a_self_contained_single_workspace_application() -> None:
    source, document = _document()

    assert INDEX.is_file()
    assert CSS.is_file()
    assert JAVASCRIPT.is_file()
    assert len(document.ids) == len(set(document.ids))
    assert {
        "app-shell",
        "workspace-sidebar",
        "workspace-summary",
        "page-home",
        "page-settings",
        "page-operations",
        "settings-drawer",
        "operations-drawer",
    } <= set(document.ids)
    assert "login-screen" not in document.ids
    assert "window.open(" not in _javascript()
    assert any(
        script.get("type") == "module" and script.get("src") == "/assets/app.js"
        for script in document.scripts
    )
    assert 'href="/assets/app.css"' in source
    assert "影像下载工作台" in source
    assert all(value.startswith(("/", "#")) for _attribute, value in document.references)


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


def test_manager_mode_uses_single_workbench_profile_contract_and_no_new_windows() -> None:
    source, _document_parser = _document()
    javascript = _javascript()

    assert "/api/management/profiles" in javascript
    assert 'profileRequest("/api/bootstrap")' in javascript
    assert 'profileRequest("/api/snapshot")' in javascript
    assert 'query.set("after_id", state.eventCursor)' in javascript
    assert 'api("/api/management/profiles", { method: "POST", body: {} })' in javascript
    assert 'managementProfilePath(profileNumber, "/start")' in javascript
    assert 'managementProfilePath(profileNumber, "/stop")' in javascript
    assert 'new EventSource("/api/events/stream"' in javascript
    assert 'id="create-profile-button"' in source
    assert 'id="workspace-sidebar"' in source
    assert "当前 Profile 默认不启动" in javascript
    assert "window.open(" not in javascript


def test_drawer_navigation_replaces_the_old_three_page_jump_flow() -> None:
    source, _document_parser = _document()
    javascript = _javascript()

    assert 'id="drawer-scrim"' in source
    assert 'id="settings-drawer"' in source
    assert 'id="operations-drawer"' in source
    assert "function openDrawer(name)" in javascript
    assert "function closeDrawers()" in javascript
    assert 'showPage("operations")' in javascript
    assert 'showPage("settings")' in javascript
    assert 'event.key === "Escape" && state.openDrawer' in javascript
    assert source.count('role="dialog" aria-modal="true"') == 2
    assert '$("#app-shell").inert = true' in javascript
    assert '$("#app-shell").inert = false' in javascript
    assert 'event.key === "Tab" && state.openDrawer' in javascript
    assert "function trapDrawerFocus(event)" in javascript
    assert 'if (allowedPage === "home") $("#main-content").focus' in javascript


def test_task_control_and_live_updates_cover_background_operation() -> None:
    source, _document_parser = _document()
    javascript = _javascript()

    for action in ("pause", "resume", "cancel", "retry", "accept-partial"):
        assert f'taskAction("{action}"' in javascript
    assert "visibilitychange" in javascript
    assert "关闭浏览器不会停止" in source
    assert "beforeunload" not in javascript


def test_task_start_freezes_the_preflighted_draft_and_confirms_targets() -> None:
    javascript = _javascript()

    assert 'parseAccessions($("#accession-input").value, { schedule: false })' in javascript
    assert "const draft = taskDraft();" in javascript
    assert "const signature = draftSignature();" in javascript
    assert "() => submitStartTask(draft, signature)" in javascript
    assert "signature !== draftSignature()" in javascript
    assert "body: draft" in javascript
    for label in ("Profile：", "PACS：", "保存目录：", "PDI："):
        assert label in javascript
    assert "const profile = currentProfile();" in javascript
    assert "const profileName = profileDisplayName(profile);" in javascript


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
    assert "scrollIntoView" not in javascript
    assert "list.scrollTop = list.scrollHeight" in javascript


def test_recovery_large_batch_and_polling_contracts_are_visible() -> None:
    javascript = _javascript()

    for status in ("interrupted", "download_retryable", "pdi_retryable", "recovery_error"):
        assert f"{status}:" in javascript
    assert "task?.status_counts?.[key]" in javascript
    assert "payload.payload ?? payload.data ?? payload" in javascript
    assert "scheduleTaskRefresh()" in javascript
    assert "scheduleManagedEventPoll" in javascript
    assert "showLogin" not in javascript
    assert 'type="password"' not in INDEX.read_text(encoding="utf-8")


def test_passwordless_lan_settings_explain_plain_http_risk() -> None:
    source, _document_parser = _document()

    assert source.count("仅限可信内网") >= 1
    assert source.count("HTTP") >= 1
    assert "流量未加密" in source
    assert 'id="setting-lan-enabled"' in source
    assert "访问密码" not in source
    assert "登录 DcmGet" not in source
    assert "打开页面即可直接使用" in source
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
    assert 'profileRequest("/api/files/accessions"' in javascript
    assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in javascript
    assert '"X-File-Name": encodeURIComponent(file.name)' in javascript


def test_management_workspace_scopes_profile_actions_and_keeps_manager_shutdown_hidden() -> None:
    javascript = _javascript()

    assert "state.managerMode ? runScopedOperation : runOperation" in javascript
    assert "shutdownButton.hidden = state.managerMode || !state.localSession" in javascript
    assert "state.managerMode && !state.activeProfileRunning" in javascript
    assert "pacs_server_ip: profile.pacs_server_ip" in javascript
    assert "storage_port: profile.storage_port" in javascript


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
    assert 'name="web_auth_required"' not in source
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

    assert "默认关闭" in source
    assert 'id="quick-pdi-enabled" type="checkbox"' in source
    assert 'id="setting-pdi-enabled" name="pdi_export_enabled" type="checkbox"' in source
    assert 'id="quick-pdi-enabled" type="checkbox" checked' not in source
    assert '$("#quick-pdi-enabled").checked = Boolean(config.pdi_export_enabled)' in javascript
    assert 'id="setting-pdi-options"' in source
    assert "syncPdiSettingsUi()" in javascript


def test_preflight_runs_automatically_and_discards_stale_responses() -> None:
    javascript = _javascript()

    assert "schedulePreflight(0)" in javascript
    assert "runPreflight({ requireAccessions: false, silent: true })" in javascript
    assert "const requestId = ++state.preflightRequestId" in javascript
    assert "requestId !== state.preflightRequestId || signature !== draftSignature()" in javascript
    assert "profile_number: state.managerMode ? currentProfileNumber() : null" in javascript
    assert "state.preflightRequestId += 1" in javascript
    assert '$("#run-preflight-button").addEventListener("click", () => runPreflight())' in javascript


def test_profile_switch_aborts_and_discards_all_stale_profile_responses() -> None:
    javascript = _javascript()

    assert "profileGeneration: 0" in javascript
    assert "profileAbortController: null" in javascript
    assert "function advanceProfileContext(profileNumber)" in javascript
    assert "state.profileAbortController.abort()" in javascript
    assert "state.profileGeneration += 1" in javascript
    assert "signal: options.signal || signal" in javascript
    assert "generation !== state.profileGeneration" in javascript
    assert "profileNumber !== currentProfileNumber()" in javascript
    assert 'profileRequest(`/api/events${' in javascript
    assert "if (isStaleProfileResponse(error)) return" in javascript
    assert "advanceProfileContext(profileNumber);" in javascript


def test_legacy_manager_requires_verified_stop_before_profile_configuration() -> None:
    javascript = _javascript()

    assert "async function stopManagedProfileNow" in javascript
    assert "async function requestManagedProfileConfiguration" in javascript
    assert 'confirmLabel: "停止并修改"' in javascript
    assert "await stopManagedProfileNow(profile)" in javascript
    assert "requestManagedProfileConfiguration(currentProfile())" in javascript
    assert 'profileActionButton("配置", configureProfile' not in javascript
    assert 'profileActionButton("配置", requestManagedProfileConfiguration' in javascript


def test_empty_profile_list_fully_clears_the_managed_context() -> None:
    javascript = _javascript()

    assert "function clearManagedProfileSelection()" in javascript
    for reset in (
        "state.activeProfile = null",
        "state.activeProfileNumber = null",
        "state.activeProfileRunning = false",
        "state.profileBootstrap = {}",
        "state.config = {}",
        "state.task = null",
        "state.logs = []",
        "advanceProfileContext(null)",
    ):
        assert reset in javascript
    assert "if (state.managerMode) return state.activeProfileNumber" in javascript
    assert "clearManagedProfileSelection();" in javascript
    assert 'throw new ApiError("请先选择一个 Profile。", 409)' in javascript


def test_manager_workspace_exposes_profile_start_stop_idle_and_settings_controls() -> None:
    source, _document_parser = _document()
    javascript = _javascript()

    for element_id in (
        "workspace-profile-title",
        "profile-idle-title",
        "current-profile-start-button",
        "current-profile-stop-button",
        "idle-start-button",
        "idle-configure-button",
        "create-profile-button",
    ):
        assert f'id="{element_id}"' in source
    assert "showIdleState(" in javascript
    assert "setWorkspaceTaskVisibility(false)" in javascript
    assert 'profileActionButton(selected ? "当前 Profile" : "切换"' in javascript
    assert 'profileActionButton("停止", stopManagedProfile' in javascript
    assert 'profileActionButton(issues.length ? "修复配置" : "启动"' in javascript


def test_start_and_local_shutdown_are_passwordless_but_keep_confirmation() -> None:
    source, _document_parser = _document()
    javascript = _javascript()

    assert 'type="password"' not in source
    assert 'id="shutdown-service-button"' in source
    assert "shutdownService" in javascript
    assert "requirePassword" not in javascript
    assert "confirmAction(" in javascript


def test_profile_management_configures_before_launch_and_exposes_service_controls() -> None:
    source, _document_parser = _document()
    javascript = _javascript()

    assert 'id="profile-config-dialog"' in source
    assert 'id="profile-save-launch-button"' in source
    for name in (
        "pacs_server_ip",
        "pacs_server_port",
        "calling_ae_title",
        "pacs_ae_title",
        "storage_ae_title",
        "storage_port",
        "web_port",
        "dicom_destination_folder",
    ):
        assert f'name="{name}"' in source
    assert "SCP 接收端口不能与 Web 端口相同" in javascript
    assert 'profileOperation("update"' in javascript
    assert 'id="start-all-profiles-button"' in source
    assert 'id="stop-all-services-button"' in source
    assert 'id="windows-service-state"' in source
    assert 'api("/api/operations/windows-service-status"' in javascript
    assert 'api("/api/operations/windows-service-start"' in javascript
    assert 'runOperation("windows-service-stop")' in javascript
    assert "stopButton.hidden = !result.supported" in javascript


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
    responsive_1080 = css.split("@media (max-width: 1080px)", 1)[1].split(
        "@media (max-width: 840px)", 1
    )[0]
    assert ".editor-grid" in responsive_1080
    assert "grid-template-columns: 1fr" in responsive_1080
    assert "innerHTML" not in javascript
    assert ".textContent" in javascript
