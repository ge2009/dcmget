from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

import pytest
from PyQt5.QtCore import QPoint, QSettings, Qt
from PyQt5.QtGui import QCloseEvent
from PyQt5.QtWidgets import QApplication, QMessageBox

from dcmget.config import AppConfig, load_config
from dcmget.core import (
    AccessionResult,
    AccessionStatus,
    BatchSummary,
    PreflightResult,
    ToolPaths,
)
import dcmget.pdi as pdi_module
import dcmget.ui as ui_module
from dcmget.ui import DcmGetWindow, PdiWorker, pdi_viewer_command


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    path = tmp_path / "pdi-ui-settings.ini"
    monkeypatch.setattr(
        ui_module,
        "QSettings",
        lambda *_args: QSettings(str(path), QSettings.IniFormat),
    )
    installed_resources = tmp_path / "installed-resources"
    make_installed_viewer_root(installed_resources)
    monkeypatch.setattr(ui_module, "resource_root", lambda: installed_resources)


def make_window(qtbot, tmp_path):
    window = DcmGetWindow(
        tmp_path / "config.json",
        tmp_path,
        tmp_path / "active-task.sqlite3",
        offer_task_resume=False,
        enable_multi_task=False,
    )
    qtbot.addWidget(window)
    window.show()
    return window


def make_pdi_viewer_root(output: Path) -> Path:
    viewer = output / "VIEWER" / "OHIF"
    viewer.mkdir(parents=True, exist_ok=True)
    (viewer / "index.html").write_text("OHIF", encoding="utf-8")
    private = output / "VIEWER" / ".dcmget"
    private.mkdir()
    (private / "index").write_text('{"studies": []}', encoding="utf-8")
    (output / "DICOM").mkdir()
    server_script = output / "VIEWER" / "pdi_server.py"
    server_script.write_text("# viewer", encoding="utf-8")
    return server_script


def make_installed_viewer_root(resource_root: Path) -> Path:
    viewer = (
        resource_root
        / ".runtime"
        / "ohif"
        / f"ohif-{ui_module.PDI_OHIF_VERSION}"
    )
    viewer.mkdir(parents=True, exist_ok=True)
    (viewer / "index.html").write_text("TRUSTED INSTALLED OHIF", encoding="utf-8")
    (viewer / "DCMGET_PAYLOAD.SHA256").write_text("checksums", encoding="utf-8")
    (viewer / "DCMGET_OHIF_PAYLOAD.json").write_text("{}", encoding="utf-8")
    return viewer


def test_pdi_settings_are_collapsed_and_disabled_by_default(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    page = window.settings_page

    assert not page.pdi_enabled_checkbox.isChecked()
    assert page.pdi_card_body.isHidden()
    assert not page.pdi_institution_edit.isEnabled()
    assert page.pdi_ohif_checkbox.isChecked()
    assert not page.pdi_ohif_checkbox.isEnabled()
    assert page.pdi_ohif_checkbox.text() == "在目录内附带离线阅片器（推荐）"
    assert "DCMGET_PDI_…" in page.pdi_output_hint.text()
    assert "不生成 JPG" in page.pdi_ohif_hint.text()
    assert "无需选择 JSON、DICOMDIR 或逐个影像文件" in page.pdi_ohif_hint.text()
    assert not window.pdi_status_card.isVisible()

    qtbot.mouseClick(page.pdi_card_toggle, Qt.LeftButton)

    assert not page.pdi_card_body.isHidden()
    assert not page.pdi_institution_edit.isEnabled()


def test_enabling_pdi_expands_options_and_validates_institution(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    page = window.settings_page
    window.pages.setCurrentIndex(1)

    page.pdi_enabled_checkbox.setChecked(True)
    page._save()

    assert page.pdi_card_body.isVisible()
    assert page.pdi_institution_edit.isEnabled()
    assert page.pdi_institution_edit.property("invalid") is True
    assert "机构名称" in page.pdi_institution_edit.toolTip()
    assert "患者隐私" in page.pdi_privacy_warning.text()


def test_pdi_settings_round_trip_ohif_option(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    page = window.settings_page
    output = tmp_path / "portable"

    page.set_config(
        AppConfig(
            pdi_export_enabled=True,
            pdi_institution_name="海市中心医院",
            pdi_output_folder=str(output),
            pdi_include_ohif_viewer=False,
        )
    )
    result = page.config()

    assert result.pdi_export_enabled
    assert result.pdi_institution_name == "海市中心医院"
    assert result.pdi_output_folder == str(output)
    assert not result.pdi_include_ohif_viewer
    assert page.pdi_ohif_checkbox.isEnabled()
    assert "无需选择 JSON、DICOMDIR 或逐个影像文件" in page.pdi_ohif_hint.text()


def test_task_page_pdi_quick_controls_persist_and_sync_settings(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    output = tmp_path / "便携阅片"
    output.mkdir()
    original_ae = window.config.calling_ae_title

    assert not window.quick_pdi_checkbox.isChecked()
    assert not window.quick_pdi_output_button.isEnabled()

    window.quick_pdi_checkbox.click()

    persisted = load_config(tmp_path / "config.json")
    assert persisted.pdi_export_enabled
    assert persisted.calling_ae_title == original_ae
    assert window.settings_page.pdi_enabled_checkbox.isChecked()
    assert "pdi_institution_name" in persisted.validate()
    assert window.pdi_status_card.isVisible()

    monkeypatch.setattr(
        ui_module.QFileDialog,
        "getExistingDirectory",
        lambda *_args: str(output),
    )
    window.quick_pdi_output_button.click()

    persisted = load_config(tmp_path / "config.json")
    assert persisted.pdi_output_folder == str(output)
    assert window.settings_page.pdi_output_edit.text() == str(output)
    assert window.quick_pdi_output_label.toolTip() == str(output)

    window.quick_pdi_checkbox.click()
    assert not load_config(tmp_path / "config.json").pdi_export_enabled
    assert not window.settings_page.pdi_enabled_checkbox.isChecked()
    assert not window.pdi_status_card.isVisible()


def test_pdi_settings_save_syncs_task_page_quick_controls(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    page = window.settings_page
    output = tmp_path / "pdi-output"

    window._show_settings()
    page.pdi_enabled_checkbox.setChecked(True)
    page.pdi_institution_edit.setText("测试医院")
    page.pdi_output_edit.setText(str(output))
    page._save()

    assert window.pages.currentWidget() is window.task_page
    assert window.quick_pdi_checkbox.isChecked()
    assert window.quick_pdi_output_button.isEnabled()
    assert window.quick_pdi_output_label.toolTip() == str(output)
    assert load_config(tmp_path / "config.json").pdi_output_folder == str(output)


def test_task_page_pdi_quick_controls_fit_high_dpi_and_lock_while_running(
    qtbot, tmp_path
):
    window = make_window(qtbot, tmp_path)
    window.setMinimumSize(1, 1)
    window.resize(683, 480)
    window._set_task_form_expanded(True)
    window.quick_pdi_checkbox.click()
    QApplication.processEvents()

    viewport = window.task_scroll.viewport()
    assert window.task_scroll.horizontalScrollBar().maximum() == 0
    for widget in (window.quick_pdi_checkbox, window.quick_pdi_output_button):
        left = widget.mapTo(viewport, QPoint(0, 0)).x()
        right = widget.mapTo(
            viewport, QPoint(widget.width() - 1, widget.height() - 1)
        ).x()
        assert 0 <= left <= right < viewport.width()

    window.quick_pdi_checkbox.setFocus(Qt.OtherFocusReason)
    qtbot.keyClick(window.quick_pdi_checkbox, Qt.Key_Tab)
    assert QApplication.focusWidget() is window.quick_pdi_output_button

    window.worker = object()
    window._set_running(True)
    assert not window.quick_pdi_checkbox.isEnabled()
    assert not window.quick_pdi_output_button.isEnabled()

    window.worker = None
    window._set_running(False)
    assert window.quick_pdi_checkbox.isEnabled()
    assert window.quick_pdi_output_button.isEnabled()


def test_download_completion_starts_pdi_with_exact_batch_files(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    archived = tmp_path / "study" / "image.dcm"
    window.config.pdi_export_enabled = True
    called = []
    monkeypatch.setattr(window, "_start_pdi_export", lambda files=None: called.append(files))
    summary = BatchSummary(
        [
            AccessionResult(
                "A001",
                AccessionStatus.COMPLETED,
                archived_files=[str(archived)],
            )
        ]
    )

    window._on_worker_finished(summary)

    assert called == [[str(archived)]]
    assert window.last_summary is summary


def test_download_checkpoint_is_kept_until_pdi_finishes(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    archived = tmp_path / "study" / "image.dcm"
    config = AppConfig(
        pdi_export_enabled=True,
        pdi_institution_name="测试医院",
    )
    checkpoint = window.task_store.start(config, ["A001"], trial_required=False)
    result = AccessionResult(
        "A001",
        AccessionStatus.COMPLETED,
        archived_files=[str(archived)],
    )
    window.task_store.record_result(checkpoint.task_id, result)
    window.task_store.try_acquire_lease()
    window.config = config
    window._active_task_id = checkpoint.task_id
    called = []
    monkeypatch.setattr(window, "_start_pdi_export", lambda files=None: called.append(files))

    window._on_worker_finished(BatchSummary([result]))

    restored = window.task_store.load_required()
    assert restored.phase == "pdi_pending"
    assert window._pdi_task_id == checkpoint.task_id
    assert called == [[str(archived)]]
    window.task_store.release_lease()


def test_large_download_completion_keeps_archived_paths_lazy_until_pdi(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    config = AppConfig(
        pdi_export_enabled=True,
        pdi_institution_name="测试医院",
    )
    accessions = [f"A{index:03d}" for index in range(201)]
    checkpoint = window.task_store.start(config, accessions, trial_required=False)
    results = []
    for index, accession in enumerate(accessions):
        result = AccessionResult(
            accession,
            AccessionStatus.COMPLETED,
            file_count=1,
            archived_files=[str(tmp_path / f"{index}.dcm")],
        )
        window.task_store.record_result(checkpoint.task_id, result)
        results.append(result)
    window.task_store.try_acquire_lease()
    window.config = config
    window._active_task_id = checkpoint.task_id
    window._display_total = len(accessions)
    window._populate_waiting_rows(accessions)
    started = []
    monkeypatch.setattr(
        window, "_start_pdi_export", lambda files=None: started.append(files)
    )

    window._on_worker_finished(BatchSummary(results))

    assert window.last_summary.archived_files == []
    assert window._pdi_source_files == []
    assert window._pdi_task_id == checkpoint.task_id
    assert window.task_store.load_required().phase == "pdi_pending"
    assert started == [None]
    window.task_store.release_lease()


def test_download_failures_are_retried_before_pdi_starts(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    config = AppConfig(
        pdi_export_enabled=True,
        pdi_institution_name="测试医院",
    )
    checkpoint = window.task_store.start(
        config, ["OK", "FAILED"], trial_required=False
    )
    completed = AccessionResult(
        "OK",
        AccessionStatus.COMPLETED,
        archived_files=[str(tmp_path / "ok.dcm")],
    )
    failed = AccessionResult("FAILED", AccessionStatus.FAILED, message="timeout")
    window.task_store.record_result(checkpoint.task_id, completed)
    window.task_store.record_result(checkpoint.task_id, failed)
    window.config = config
    window._active_task_id = checkpoint.task_id
    started = []
    monkeypatch.setattr(
        window, "_start_pdi_export", lambda files=None: started.append(files)
    )
    monkeypatch.setattr(window, "_show_download_completion", lambda *_args, **_kwargs: None)

    window._on_worker_finished(BatchSummary([completed, failed]))

    assert started == []
    assert window.task_store.load_required().phase == "download_retryable"
    assert window._pdi_task_id == ""
    assert window._resume_checkpoint is not None


def test_startup_can_resume_pdi_without_redownloading(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    archived = tmp_path / "study" / "image.dcm"
    config = AppConfig(
        pdi_export_enabled=True,
        pdi_institution_name="测试医院",
    )
    checkpoint = window.task_store.start(config, ["A001"], trial_required=False)
    window.task_store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.COMPLETED,
            archived_files=[str(archived)],
        ),
    )
    window.task_store.set_phase(checkpoint.task_id, "pdi_retryable")
    started = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )
    monkeypatch.setattr(
        window,
        "_start_pdi_export",
        lambda files=None: started.append(files),
    )

    window._offer_task_resume()

    assert started == [[str(archived)]]
    assert window._pdi_task_id == checkpoint.task_id
    # Large recovery points keep file paths on disk and load them only when
    # PDI actually starts.
    assert window.last_summary.archived_files == []
    assert window.last_summary.results[0].file_count == 1
    window.task_store.release_lease()


def test_startup_converts_finished_download_with_failures_to_retryable_before_pdi(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    config = AppConfig(
        pdi_export_enabled=True,
        pdi_institution_name="测试医院",
    )
    checkpoint = window.task_store.start(config, ["FAILED"], trial_required=False)
    window.task_store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "FAILED",
            AccessionStatus.PARTIAL,
            file_count=1,
            archived_files=[str(tmp_path / "partial.dcm")],
        ),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.No,
    )

    window._offer_task_resume()

    restored = window.task_store.load_required(include_archived_files=False)
    assert restored.phase == "download_retryable"
    assert window._resume_checkpoint is not None
    assert window._resume_checkpoint.phase == "download_retryable"
    assert window._pdi_task_id == ""
    assert window.start_button.text() == "重试失败项"
    assert not window.pdi_retry_button.isEnabled()


def test_pdi_recovery_restores_accepted_partial_result_semantics(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    config = AppConfig(
        pdi_export_enabled=True,
        pdi_institution_name="测试医院",
    )
    checkpoint = window.task_store.start(config, ["PARTIAL"], trial_required=False)
    window.task_store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "PARTIAL",
            AccessionStatus.PARTIAL,
            file_count=1,
            archived_files=[str(tmp_path / "partial.dcm")],
        ),
    )
    window.task_store.set_phase(checkpoint.task_id, "pdi_retryable")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.No,
    )

    window._offer_task_resume()

    assert window._accepted_partial_results
    assert window._pdi_task_id == checkpoint.task_id
    window._save_pdi_checkpoint_status(completed=True)
    window._set_running(False)

    assert window.task_store.load() is None
    assert not window.retry_button.isEnabled()


def test_pdi_success_clears_checkpoint_and_failure_keeps_retryable_phase(
    qtbot, tmp_path
):
    window = make_window(qtbot, tmp_path)
    checkpoint = window.task_store.start(AppConfig(), ["A001"], trial_required=False)
    window._pdi_task_id = checkpoint.task_id
    window.task_store.try_acquire_lease()

    window._save_pdi_checkpoint_status(completed=False)

    assert window.task_store.load_required().phase == "pdi_retryable"
    assert window._pdi_task_id == checkpoint.task_id

    window.task_store.try_acquire_lease()
    window._save_pdi_checkpoint_status(completed=True)

    assert window.task_store.load() is None
    assert window._pdi_task_id == ""


def test_preflight_area_expands_for_pdi_checks(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    checks = [
        ("DCMTK 工具", True, "已就绪"),
        ("PDI 导出工具", True, "已就绪"),
        ("保存目录", True, "目录可写"),
        ("PDI 输出目录", True, "目录可写"),
        ("接收端口", True, "端口可用"),
        ("PACS 配置", True, "127.0.0.1:8104"),
    ]

    window._show_preflight(PreflightResult(None, {}, checks))

    assert len(window.preflight_labels) == 6
    assert all(label.isVisible() for label in window.preflight_labels)
    assert window.preflight_labels[1].text().startswith("PDI 导出工具")


def test_pdi_progress_and_success_enable_viewer_and_open_directory(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    output = tmp_path / "PDI" / "DCMGET_PDI_20260716_120000"
    make_pdi_viewer_root(output)
    window.config.pdi_export_enabled = True
    window._pdi_source_files = [str(tmp_path / "image.dcm")]
    window.last_summary = BatchSummary()
    messages = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, message: messages.append((title, message)),
    )

    class Stage:
        value = "生成 DICOMDIR"

    window._on_pdi_progress(Stage(), 1, 2, "正在写入目录")

    assert window.pdi_progress_bar.maximum() == 2
    assert window.pdi_progress_bar.value() == 1
    assert "生成 DICOMDIR" in window.pdi_status_label.text()

    @dataclass
    class Result:
        status: object
        output_directory: str
        message: str = "PDI 便携目录已生成"
        warnings: list[str] = field(default_factory=list)

    class Status:
        value = "完成"

    window._on_pdi_finished(Result(Status(), str(output)))

    assert window.pdi_view_button.isEnabled()
    assert window.pdi_open_button.isEnabled()
    assert not window.pdi_retry_button.isEnabled()
    assert window.pdi_view_button.text() == "打开影像"
    assert window.pdi_open_button.text() == "打开导出目录"
    assert "离线阅片器" in window.pdi_view_button.toolTip()
    assert "PDI 导出目录" in window.pdi_open_button.toolTip()
    assert "已生成" in window.pdi_status_label.text()
    assert output.name in window.pdi_status_label.text()
    assert str(output) in messages[0][1]


def test_pdi_immediate_viewer_uses_pdi_data_and_installed_viewer(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    output = tmp_path / "PDI 目录"
    make_pdi_viewer_root(output)
    (output / "VIEWER" / "OHIF" / "index.html").write_text(
        "TAMPERED MEDIA VIEWER", encoding="utf-8"
    )
    installed_resources = tmp_path / "installed-resources"
    trusted_viewer = make_installed_viewer_root(installed_resources)
    monkeypatch.setattr(ui_module, "resource_root", lambda: installed_resources)
    trusted_server_script = Path(ui_module.__file__).with_name("pdi_server.py")

    @dataclass
    class Result:
        output_directory: str

    window.last_pdi_result = Result(str(output))
    window.pdi_view_button.setEnabled(True)
    window.open_existing_pdi_button.setEnabled(True)
    launched = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, *args):
            for callback in list(self.callbacks):
                callback(*args)

    class FakeProcess:
        NotRunning = 0
        Running = 2
        instances = []

        def __init__(self, _parent=None):
            self._state = self.NotRunning
            self.cwd = ""
            self.terminated = False
            self.killed = False
            self.deleted = False
            self.readyReadStandardOutput = FakeSignal()
            self.finished = FakeSignal()
            self.errorOccurred = FakeSignal()
            self.instances.append(self)

        def setWorkingDirectory(self, cwd):
            self.cwd = cwd

        def start(self, program, arguments):
            launched.append((program, list(arguments), self.cwd))
            self._state = self.Running

        def waitForStarted(self, _timeout):
            return True

        def state(self):
            return self._state

        def readAllStandardOutput(self):
            return b""

        def terminate(self):
            self.terminated = True
            self._state = self.NotRunning

        def waitForFinished(self, _timeout):
            return self._state == self.NotRunning

        def kill(self):
            self.killed = True
            self._state = self.NotRunning

        def deleteLater(self):
            self.deleted = True

        def errorString(self):
            return "fake error"

    monkeypatch.setattr(ui_module, "QProcess", FakeProcess)
    opened = []
    monkeypatch.setattr(
        ui_module.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toString()) or True,
    )
    monkeypatch.setattr(window, "_probe_pdi_viewer_ready", lambda: None)

    window._open_pdi_viewer()
    window._pdi_viewer_probe_timer.stop()

    assert window.pdi_view_button.text() == "正在启动…"
    assert not window.pdi_view_button.isEnabled()
    assert window.open_existing_pdi_button.text() == "阅片器启动中…"
    assert not window.open_existing_pdi_button.isEnabled()
    assert "正在启动本地离线阅片服务" in window.progress_label.text()
    assert len(launched) == 1
    program, arguments, cwd = launched[0]
    assert Path(program).resolve() == Path(ui_module.sys.executable).resolve()
    assert arguments[:6] == [
        str(trusted_server_script),
        "--root",
        str(output.resolve()),
        "--viewer-root",
        str(trusted_viewer.resolve()),
        "--quiet",
    ]
    assert str(output / "VIEWER" / "OHIF") not in arguments
    assert arguments[6] == "--session-token"
    session_token = arguments[7]
    assert len(session_token) >= 43
    assert arguments[8] == "--port"
    port = int(arguments[9])
    assert arguments[10] == "--no-browser"
    assert cwd == str(output.resolve())
    from dcmget.pdi_server import viewer_url

    assert opened == []
    assert window._pdi_viewer_probe_url == (
        f"http://127.0.0.1:{port}/ready/{session_token}"
    )

    class FakeReply:
        def __init__(self):
            self.deleted = False

        def attribute(self, _attribute):
            return 200

        def deleteLater(self):
            self.deleted = True

    reply = FakeReply()
    window._pdi_viewer_probe_reply = reply
    window._on_pdi_viewer_probe_finished(reply)

    assert opened == [viewer_url(port, session_token)]
    assert window.pdi_view_button.text() == "打开影像"
    assert window.pdi_view_button.isEnabled()
    assert window.open_existing_pdi_button.text() == "打开已有 PDI 目录"
    assert window.open_existing_pdi_button.isEnabled()
    assert "已就绪" in window.progress_label.text()
    window._open_pdi_viewer()
    assert opened == [
        viewer_url(port, session_token),
        viewer_url(port, session_token),
    ]

    process = FakeProcess.instances[0]
    window._on_pdi_viewer_finished(process, 0, None)
    assert window.pdi_view_button.text() == "打开影像"
    assert window.pdi_view_button.isEnabled()
    assert window.open_existing_pdi_button.text() == "打开已有 PDI 目录"
    assert window.open_existing_pdi_button.isEnabled()
    assert "已退出" in window.progress_label.text()
    event = QCloseEvent()
    window.closeEvent(event)

    assert process.terminated
    assert process.deleted
    assert window._pdi_viewer_process is None


def test_open_existing_pdi_directory_remembers_chinese_path(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    output = tmp_path / "既往 PDI 目录 with spaces"
    make_pdi_viewer_root(output)
    (output / "OPEN_VIEWER.exe").write_bytes(b"untrusted media executable")
    dialog_calls = []

    def choose_directory(_parent, title, initial):
        dialog_calls.append((title, initial))
        return str(output)

    monkeypatch.setattr(
        ui_module.QFileDialog,
        "getExistingDirectory",
        choose_directory,
    )
    launched = []
    monkeypatch.setattr(
        window,
        "_launch_pdi_viewer",
        lambda root: launched.append(root),
    )

    window._choose_existing_pdi_directory()

    assert window.open_existing_pdi_button.text() == "打开已有 PDI 目录"
    assert dialog_calls[0][0] == "打开已有 PDI 目录"
    assert launched == [output.resolve()]
    assert window.settings_store.value(
        ui_module.PDI_LAST_OPEN_DIRECTORY_KEY,
        "",
        type=str,
    ) == str(output.resolve())

    remembered = []

    def cancel_directory(_parent, title, initial):
        remembered.append((title, initial))
        return ""

    monkeypatch.setattr(
        ui_module.QFileDialog,
        "getExistingDirectory",
        cancel_directory,
    )
    window._choose_existing_pdi_directory()

    assert remembered == [("打开已有 PDI 目录", str(output.resolve()))]
    assert launched == [output.resolve()]


def test_open_existing_pdi_directory_rejects_incomplete_root_without_json_wording(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    incomplete = tmp_path / "不完整 PDI 目录"
    incomplete.mkdir()
    monkeypatch.setattr(
        ui_module.QFileDialog,
        "getExistingDirectory",
        lambda *_args, **_kwargs: str(incomplete),
    )
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    launched = []
    monkeypatch.setattr(
        window,
        "_launch_pdi_viewer",
        lambda root: launched.append(root),
    )

    window._choose_existing_pdi_directory()

    assert launched == []
    assert warnings
    assert warnings[0][0] == "无法打开 PDI 目录"
    assert "PDI 根目录" in warnings[0][1]
    assert "JSON" not in " ".join(warnings[0])
    assert window.settings_store.value(
        ui_module.PDI_LAST_OPEN_DIRECTORY_KEY,
        "",
        type=str,
    ) == ""


def test_frozen_ui_never_executes_a_viewer_from_the_pdi_directory(
    tmp_path, monkeypatch
):
    output = tmp_path / "external PDI"
    make_pdi_viewer_root(output)
    (output / "OPEN_VIEWER.exe").write_bytes(b"untrusted")
    empty_resources = tmp_path / "empty-installed-resources"
    empty_resources.mkdir()
    monkeypatch.setattr(ui_module, "is_frozen", lambda: True)
    monkeypatch.setattr(ui_module, "resource_root", lambda: empty_resources)

    assert pdi_viewer_command(output) is None


def test_app_command_opens_legacy_index_with_installed_viewer(
    tmp_path, monkeypatch
):
    output = tmp_path / "legacy PDI"
    make_pdi_viewer_root(output)
    (output / "VIEWER" / ".dcmget" / "index").unlink()
    (output / "DCMGET_STUDIES.json").write_text(
        '{"studies": []}', encoding="utf-8"
    )
    (output / "VIEWER" / "OHIF" / "media-only.js").write_text(
        "malicious media javascript", encoding="utf-8"
    )
    installed_resources = tmp_path / "installed-resources"
    trusted_viewer = make_installed_viewer_root(installed_resources)
    monkeypatch.setattr(ui_module, "resource_root", lambda: installed_resources)

    command = pdi_viewer_command(output)

    assert command is not None
    program, arguments = command
    assert Path(program).resolve() == Path(ui_module.sys.executable).resolve()
    assert arguments[:6] == [
        str(Path(ui_module.__file__).with_name("pdi_server.py")),
        "--root",
        str(output.resolve()),
        "--viewer-root",
        str(trusted_viewer.resolve()),
        "--quiet",
    ]
    assert str(output / "VIEWER" / "OHIF") not in arguments


def test_retry_pdi_does_not_restart_download(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    files = [str(tmp_path / "image.dcm")]
    window._pdi_source_files = files
    called = []
    monkeypatch.setattr(window, "_start_pdi_export", lambda values=None: called.append(values))

    window._retry_pdi()

    assert called == [None]


def test_pdi_viewer_reports_early_exit_and_readiness_timeout(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    messages = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, title, message: messages.append((title, message)),
    )

    class FakeProcess:
        def __init__(self, state):
            self._state = state
            self.terminated = False
            self.deleted = False

        def state(self):
            return self._state

        def readAllStandardOutput(self):
            return b""

        def terminate(self):
            self.terminated = True
            self._state = ui_module.QProcess.NotRunning

        def waitForFinished(self, _timeout):
            return self._state == ui_module.QProcess.NotRunning

        def kill(self):
            self._state = ui_module.QProcess.NotRunning

        def deleteLater(self):
            self.deleted = True

    exited = FakeProcess(ui_module.QProcess.NotRunning)
    window.pdi_view_button.setEnabled(True)
    window.open_existing_pdi_button.setEnabled(True)
    window._set_pdi_viewer_starting_feedback()
    window._pdi_viewer_process = exited
    window._pdi_viewer_root = tmp_path
    window._on_pdi_viewer_finished(exited, 1, None)

    assert window._pdi_viewer_process is None
    assert exited.deleted
    assert "就绪前退出" in messages[-1][1]
    assert "诊断日志" in messages[-1][1]
    assert window.pdi_view_button.text() == "打开影像"
    assert window.pdi_view_button.isEnabled()
    assert window.open_existing_pdi_button.text() == "打开已有 PDI 目录"
    assert window.open_existing_pdi_button.isEnabled()
    assert "启动失败" in window.progress_label.text()

    timed_out = FakeProcess(ui_module.QProcess.Running)
    window._set_pdi_viewer_starting_feedback()
    window._pdi_viewer_process = timed_out
    window._pdi_viewer_root = tmp_path
    window._on_pdi_viewer_start_timeout()

    assert window._pdi_viewer_process is None
    assert timed_out.terminated
    assert timed_out.deleted
    assert "30 秒内未就绪" in messages[-1][1]
    assert window.pdi_view_button.text() == "打开影像"
    assert window.pdi_view_button.isEnabled()
    assert window.open_existing_pdi_button.text() == "打开已有 PDI 目录"
    assert window.open_existing_pdi_button.isEnabled()
    assert "启动失败" in window.progress_label.text()


def test_accept_partial_results_continues_pdi_without_redownload(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    archived = str(tmp_path / "completed.dcm")
    config = AppConfig(
        pdi_export_enabled=True,
        pdi_institution_name="测试医院",
    )
    checkpoint = window.task_store.start(
        config, ["A001", "A002"], trial_required=False
    )
    window.task_store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.COMPLETED,
            file_count=1,
            archived_files=[archived],
        ),
    )
    window.task_store.record_result(
        checkpoint.task_id,
        AccessionResult("A002", AccessionStatus.FAILED, message="PACS failed"),
    )
    window.task_store.set_phase(checkpoint.task_id, "download_retryable")
    window._resume_checkpoint = window.task_store.load_required(
        include_archived_files=False
    )
    window.last_summary = BatchSummary(window._resume_checkpoint.results)
    window._set_running(False)
    assert not window.accept_partial_button.isHidden()
    assert window.accept_partial_button.isEnabled()

    started: list[list[str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )
    monkeypatch.setattr(
        window,
        "_start_pdi_export",
        lambda files=None: started.append(list(files or [])),
    )

    window._accept_partial_results()

    assert started == [[archived]]
    assert window._resume_checkpoint is None
    assert window._pdi_task_id == checkpoint.task_id
    assert window.task_store.load_required().phase == "pdi_pending"
    window.task_store.release_lease()


def test_accept_partial_results_without_pdi_ends_recovery(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    checkpoint = window.task_store.start(
        AppConfig(pdi_export_enabled=False), ["A001"], trial_required=False
    )
    window.task_store.record_result(
        checkpoint.task_id,
        AccessionResult("A001", AccessionStatus.FAILED),
    )
    window.task_store.set_phase(checkpoint.task_id, "download_retryable")
    window._resume_checkpoint = window.task_store.load_required(
        include_archived_files=False
    )
    window.last_summary = BatchSummary(list(window._resume_checkpoint.results))
    window._set_running(False)
    assert window.start_button.text() == "重试失败项"
    assert window.start_button.isEnabled()
    assert window.retry_button.isHidden()
    assert not window.retry_button.isEnabled()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )
    messages = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, message: messages.append((title, message)),
    )

    window._accept_partial_results()
    window._show_download_completion()

    assert window.task_store.load() is None
    assert window._resume_checkpoint is None
    assert window.start_button.isEnabled()
    assert not window.retry_button.isEnabled()
    assert all("重试" not in message for _title, message in messages)


def test_saving_corrected_settings_preserves_failed_pdi_retry(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    archived = str(tmp_path / "image.dcm")
    window._pdi_source_files = [archived]
    checkpoint = window.task_store.start(
        AppConfig(pdi_export_enabled=True, pdi_institution_name="旧机构"),
        ["A001"],
        trial_required=False,
    )
    window.task_store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.COMPLETED,
            archived_files=[archived],
        ),
    )
    window.task_store.set_phase(checkpoint.task_id, "pdi_retryable")
    window._pdi_task_id = checkpoint.task_id

    class Status:
        value = "失败"

    @dataclass
    class Result:
        status: object = field(default_factory=Status)
        output_directory: str = ""

    window.last_pdi_result = Result()
    corrected = AppConfig(
        pdi_export_enabled=True,
        pdi_institution_name="海市中心医院",
        pdi_include_ohif_viewer=False,
    )

    window._save_settings(corrected)

    assert window.pdi_retry_button.isEnabled()
    assert "可以重试" in window.pdi_status_label.text()
    restored = window.task_store.load_required()
    assert restored.config.pdi_institution_name == "海市中心医院"
    assert not restored.config.pdi_include_ohif_viewer

    restarted = make_window(qtbot, tmp_path)
    started_with = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )
    monkeypatch.setattr(
        restarted,
        "_start_pdi_export",
        lambda _files=None: started_with.append(
            (
                restarted.config.pdi_institution_name,
                restarted.config.pdi_include_ohif_viewer,
            )
        ),
    )
    restarted._offer_task_resume()

    assert started_with == [("海市中心医院", False)]
    restarted.task_store.release_lease()


def test_saving_settings_preserves_completed_pdi_actions(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    output = tmp_path / "portable"
    (output / "VIEWER").mkdir(parents=True)
    (output / "VIEWER" / "pdi_server.py").write_text(
        "# viewer", encoding="utf-8"
    )

    class Status:
        value = "完成"

    @dataclass
    class Result:
        status: object = field(default_factory=Status)
        output_directory: str = str(output)

    window.config.pdi_export_enabled = True
    window.last_pdi_result = Result()
    window._set_pdi_status("PDI 便携目录已生成", "ok")
    window.pdi_open_button.setEnabled(True)
    window.pdi_view_button.setEnabled(True)
    window.pdi_progress_bar.setValue(100)

    window._save_settings(
        AppConfig(pdi_export_enabled=True, pdi_institution_name="测试医院")
    )

    assert window.pdi_status_label.text() == "PDI 便携目录已生成"
    assert window.pdi_open_button.isEnabled()
    assert window.pdi_view_button.isEnabled()
    assert window.pdi_progress_bar.value() == 100


def test_saving_settings_preserves_lazy_pdi_recovery_retry(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    checkpoint = window.task_store.start(
        AppConfig(pdi_export_enabled=True, pdi_institution_name="旧机构"),
        ["A001"],
        trial_required=False,
    )
    window.task_store.set_phase(checkpoint.task_id, "pdi_retryable")
    window._pdi_task_id = checkpoint.task_id
    window._pdi_source_files = []
    window.pdi_retry_button.setEnabled(True)

    window._save_settings(
        AppConfig(pdi_export_enabled=True, pdi_institution_name="新机构")
    )

    assert window.pdi_retry_button.isEnabled()
    assert "可以重试" in window.pdi_status_label.text()


def test_failed_pdi_checkpoint_blocks_new_download_until_discarded(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    checkpoint = window.task_store.start(
        AppConfig(pdi_export_enabled=True, pdi_institution_name="测试医院"),
        ["A001"],
        trial_required=False,
    )
    window._pdi_task_id = checkpoint.task_id
    window._pdi_source_files = [str(tmp_path / "image.dcm")]
    window.task_store.set_phase(checkpoint.task_id, "pdi_retryable")

    window._set_running(False)

    assert not window.start_button.isEnabled()
    assert window.start_button.text() == "PDI 待重试"
    assert window.discard_resume_button.isVisible()
    assert window.settings_button.isEnabled()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )
    qtbot.mouseClick(window.discard_resume_button, Qt.LeftButton)

    assert window.task_store.load() is None
    assert window._pdi_task_id == ""
    assert window.start_button.isEnabled()


def test_pdi_checkpoint_guard_rejects_direct_start_and_failed_retry(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    checkpoint = window.task_store.start(
        AppConfig(pdi_export_enabled=True, pdi_institution_name="测试医院"),
        ["A001"],
        trial_required=False,
    )
    window._pdi_task_id = checkpoint.task_id
    window.current_accessions = ["NEW001"]
    window.last_summary = BatchSummary(
        [AccessionResult("FAILED001", AccessionStatus.FAILED)]
    )
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args, **_kwargs: warnings.append(_args[2]),
    )

    window._start_download()
    window._retry_failed()

    assert len(warnings) == 2
    assert all("PDI" in warning for warning in warnings)
    assert window.task_store.load_required().task_id == checkpoint.task_id


def test_discard_keeps_checkpoint_when_pdi_partial_cannot_be_deleted(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    checkpoint = window.task_store.start(
        AppConfig(
            dicom_destination_folder=str(tmp_path / "dicom"),
            pdi_export_enabled=True,
            pdi_institution_name="测试医院",
            pdi_output_folder=str(tmp_path / "portable"),
        ),
        ["A001"],
        trial_required=False,
    )
    window.task_store.begin_pdi_attempt(
        checkpoint.task_id,
        reuse_existing=False,
    )
    window._pdi_task_id = checkpoint.task_id
    window._set_running(False)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pdi_module,
        "cleanup_interrupted_pdi",
        lambda *_args: (_ for _ in ()).throw(OSError("目录正在使用")),
    )

    window._discard_resume_task()

    assert window.task_store.load_required().task_id == checkpoint.task_id
    assert window._pdi_task_id == checkpoint.task_id


def test_declining_pdi_resume_preserves_partial_and_checkpoint_for_later(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    output_root = tmp_path / "portable"
    config = AppConfig(
        dicom_destination_folder=str(tmp_path / "dicom"),
        pdi_export_enabled=True,
        pdi_institution_name="测试医院",
        pdi_output_folder=str(output_root),
    )
    checkpoint = window.task_store.start(config, ["A001"], trial_required=False)
    window.task_store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.COMPLETED,
            archived_files=[str(tmp_path / "image.dcm")],
        ),
    )
    attempt_id, _reused = window.task_store.begin_pdi_attempt(
        checkpoint.task_id,
        reuse_existing=False,
    )
    partial = output_root / ".DCMGET_PDI_OLD.partial-deadbeef"
    partial.mkdir(parents=True)
    (partial / pdi_module.RECOVERY_MARKER).write_text(
        json.dumps({"version": 1, "attempt_id": attempt_id}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.No,
    )

    window._offer_task_resume()

    assert partial.exists()
    assert window.task_store.load_required().task_id == checkpoint.task_id
    assert window._pdi_task_id == checkpoint.task_id
    assert window.pdi_retry_button.isEnabled()


def test_pdi_thread_completion_releases_busy_state(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    window.config.pdi_export_enabled = True
    window.config.pdi_institution_name = "海市中心医院"
    window.last_summary = BatchSummary()
    window.tools = ToolPaths(
        tmp_path / "movescu",
        tmp_path / "storescp",
        tmp_path,
        "3.7.0",
    )

    class Status:
        value = "完成"

    @dataclass
    class Result:
        status: object = field(default_factory=Status)
        output_directory: str = ""
        message: str = "PDI 便携目录已生成"
        warnings: list[str] = field(default_factory=list)

    class Exporter:
        def __init__(self, *_args, **_kwargs):
            pass

        def export(self, _files):
            return Result()

        def request_cancel(self):
            pass

    monkeypatch.setattr(pdi_module, "PdiExporter", Exporter)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: None)

    window._start_pdi_export([str(tmp_path / "image.dcm")])
    qtbot.waitUntil(lambda: window.pdi_thread is None)

    assert window.pdi_worker is None
    assert not window._is_busy()
    assert window.start_button.isEnabled()
    assert window.settings_button.isEnabled()


def test_pdi_worker_uses_exporter_callbacks_and_honors_early_cancel(
    qtbot, tmp_path, monkeypatch
):
    instances = []
    result = object()

    class Exporter:
        def __init__(self, config, tools, **kwargs):
            self.config = config
            self.tools = tools
            self.kwargs = kwargs
            self.cancel_calls = 0
            instances.append(self)

        def request_cancel(self):
            self.cancel_calls += 1

        def export(self, files):
            assert files == [str(tmp_path / "image.dcm")]
            return result

    monkeypatch.setattr(pdi_module, "PdiExporter", Exporter)
    tools = ToolPaths(
        tmp_path / "movescu",
        tmp_path / "storescp",
        tmp_path,
        "3.7.0",
    )
    worker = PdiWorker(
        AppConfig(), tools, [str(tmp_path / "image.dcm")], tmp_path
    )
    finished = []
    worker.finished.connect(finished.append)
    worker.request_cancel()
    worker.request_cancel()

    worker.run()

    assert instances[0].config == worker.config
    assert instances[0].tools is tools
    assert instances[0].cancel_calls == 1
    assert callable(instances[0].kwargs["log_callback"])
    assert callable(instances[0].kwargs["progress_callback"])
    assert finished == [result]


def test_close_during_pdi_requests_export_cancel(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)

    class Worker:
        cancelled = False

        def request_cancel(self):
            self.cancelled = True

    worker = Worker()
    window.pdi_worker = worker  # type: ignore[assignment]
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes
    )
    event = QCloseEvent()

    window.closeEvent(event)

    assert worker.cancelled
    assert window._closing_after_cancel
    assert not event.isAccepted()
