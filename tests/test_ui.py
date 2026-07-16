from __future__ import annotations

import threading

import pytest
from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtGui import QCloseEvent
from PyQt5.QtWidgets import QApplication, QMessageBox, QPushButton, QTextBrowser

from dcmget.config import AppConfig
from dcmget.core import AccessionResult, AccessionStatus, BatchSummary, ToolPaths
import dcmget.ui as ui_module
from dcmget.ui import DcmGetWindow, DownloadWorker


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    path = tmp_path / "ui-settings.ini"
    monkeypatch.setattr(
        ui_module,
        "QSettings",
        lambda *_args: QSettings(str(path), QSettings.IniFormat),
    )


def make_window(qtbot, tmp_path):
    window = DcmGetWindow(tmp_path / "config.json", tmp_path)
    qtbot.addWidget(window)
    window.show()
    return window


def test_paste_preview_deduplicates_and_updates_table(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    window.accession_edit.setPlainText("A001\n\nA002\nA001")

    assert window.current_accessions == ["A001", "A002"]
    assert window.accession_summary.text() == "有效 2 · 空行 1 · 重复 1"
    assert window.task_table.rowCount() == 2
    assert window.task_table.item(1, 0).text() == "A002"


def test_accession_editor_tab_moves_focus_to_next_control(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    window.accession_edit.setFocus()

    qtbot.keyClick(window.accession_edit, Qt.Key_Tab)

    assert QApplication.focusWidget() is window.accession_button


def test_settings_validation_is_inline(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    page = window.settings_page
    window.pages.setCurrentIndex(1)
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    page.pacs_host_edit.clear()

    page._save()

    assert page.pacs_host_edit.property("invalid") is True
    assert "PACS" in page.pacs_host_edit.toolTip()
    assert "PACS" in page.pacs_host_edit.accessibleDescription()
    assert page.error_label.isVisible()
    assert page.layout().indexOf(page.error_label) < page.layout().indexOf(
        page.settings_scroll
    )
    assert page.pacs_host_edit.hasFocus()


def test_settings_validation_scrolls_to_first_invalid_field(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    window.resize(1024, 720)
    page = window.settings_page
    window.pages.setCurrentIndex(1)
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    page.directory_template_combo.clearEditText()
    page.settings_scroll.verticalScrollBar().setValue(0)

    page._save()

    assert page.directory_template_combo.hasFocus()
    assert page.settings_scroll.verticalScrollBar().value() > 0
    assert "目录模板" in page.directory_template_combo.accessibleDescription()


def test_cancel_settings_discards_unsaved_directory_template(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    original = window.config.directory_template
    window._show_settings()
    window.settings_page.directory_template_combo.setCurrentText(
        "{StudyInstanceUID}"
    )

    window._cancel_settings()

    assert window.config.directory_template == original
    assert window.settings_page.config().directory_template == original


def test_anonymization_settings_default_to_off_and_show_profile_warning(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    window._show_settings()
    page = window.settings_page

    assert not page.anonymization_enabled_checkbox.isChecked()
    assert not page.anonymization_profile_combo.isEnabled()
    assert page.anonymization_profile_combo.currentData() == "research"

    page.anonymization_enabled_checkbox.setChecked(True)

    assert page.anonymization_profile_combo.isEnabled()
    assert "像素" in page.anonymization_warning.text()
    assert "元数据" in page.anonymization_profile_hint.text()


def test_cancel_settings_discards_unsaved_anonymization_options(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    window._show_settings()
    page = window.settings_page
    page.anonymization_enabled_checkbox.setChecked(True)
    page.anonymization_profile_combo.setCurrentIndex(
        page.anonymization_profile_combo.findData("strict")
    )

    window._cancel_settings()

    restored = page.config()
    assert not restored.anonymization_enabled
    assert restored.anonymization_profile == "research"


def test_anonymization_settings_are_saved_and_reloaded(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    window._show_settings()
    page = window.settings_page
    page.anonymization_enabled_checkbox.setChecked(True)
    page.anonymization_profile_combo.setCurrentIndex(
        page.anonymization_profile_combo.findData("strict")
    )

    page._save()

    saved = ui_module.load_config(tmp_path / "config.json")
    assert saved.anonymization_enabled
    assert saved.anonymization_profile == "strict"


def test_running_state_locks_inputs_and_progress_updates(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    window.accession_edit.setPlainText("A001")
    window.last_summary = BatchSummary(
        [AccessionResult("OLD", AccessionStatus.FAILED)]
    )

    window._set_running(True)
    assert window.last_summary is None
    assert window.accession_edit.isReadOnly()
    assert window.destination_edit.isReadOnly()
    assert not window.start_button.isEnabled()
    assert window.stop_button.isEnabled()
    assert window.pause_button.isEnabled()

    result = AccessionResult(
        "A001",
        AccessionStatus.COMPLETED,
        3,
        1.25,
        "收到 3 个文件",
        received_bytes=3 * 1024 * 1024,
        speed_bytes_per_second=1.5 * 1024 * 1024,
    )
    window._on_worker_progress(1, 1, result)
    assert window.progress_bar.value() == 1
    assert window.task_table.item(0, 1).text() == "完成"
    assert window.task_table.item(0, 2).text() == "3"
    assert window.task_table.horizontalHeaderItem(3).text() == "速度"
    assert window.task_table.item(0, 3).text() == "1.5 MB/s"
    assert window.task_table.item(0, 4).text() == "1.2s"
    assert "1.5 MB/s" in window.progress_label.text()


@pytest.mark.parametrize(
    ("bytes_per_second", "expected"),
    [
        (0, "—"),
        (512, "512 B/s"),
        (1536, "1.5 KB/s"),
        (2.5 * 1024 * 1024, "2.5 MB/s"),
        (3 * 1024 * 1024 * 1024, "3.0 GB/s"),
    ],
)
def test_transfer_speed_formatting(bytes_per_second, expected):
    assert ui_module.format_transfer_rate(bytes_per_second) == expected


def test_pause_button_requests_pause_and_resume(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)

    class Worker:
        pauses = 0
        resumes = 0

        def request_pause(self):
            self.pauses += 1

        def request_resume(self):
            self.resumes += 1

    worker = Worker()
    window.worker = worker  # type: ignore[assignment]
    window._set_running(True)

    qtbot.mouseClick(window.pause_button, Qt.LeftButton)

    assert worker.pauses == 1
    assert window.pause_button.text() == "取消暂停"
    assert "当前检查号完成后暂停" in window.progress_label.text()

    window._on_worker_state("pause_pending")
    assert window.pause_button.text() == "取消暂停"
    window._on_worker_state("paused")
    assert "已暂停" in window.progress_label.text()
    assert window.pause_button.text() == "继续下载"
    qtbot.mouseClick(window.pause_button, Qt.LeftButton)

    assert worker.resumes == 1
    assert window.pause_button.text() == "暂停"
    window.worker = None
    window._set_running(False)


def test_worker_does_not_lose_resume_during_startup(monkeypatch, tmp_path):
    pause_entered = threading.Event()
    allow_pause = threading.Event()
    resumed = threading.Event()
    instance = []

    class Runner:
        def __init__(self, *_args, **_kwargs):
            self.paused = False
            instance.append(self)

        def request_pause(self):
            pause_entered.set()
            assert allow_pause.wait(2)
            self.paused = True

        def request_resume(self):
            self.paused = False
            resumed.set()

        def run(self, _accessions):
            assert resumed.wait(2)
            return BatchSummary()

    monkeypatch.setattr(ui_module, "DownloadRunner", Runner)
    worker = DownloadWorker(
        AppConfig(dicom_destination_folder=str(tmp_path)),
        ToolPaths(tmp_path / "movescu", tmp_path / "storescp", tmp_path, "3.7.0"),
        ["A001"],
    )
    worker.request_pause()
    run_thread = threading.Thread(target=worker.run, daemon=True)
    run_thread.start()
    assert pause_entered.wait(2)

    resume_thread = threading.Thread(target=worker.request_resume, daemon=True)
    resume_thread.start()
    allow_pause.set()
    resume_thread.join(2)
    run_thread.join(2)

    assert not run_thread.is_alive()
    assert resumed.is_set()
    assert not worker.pause_requested
    assert not instance[0].paused


def test_version_notes_dialog_lists_upgrade_history(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)

    qtbot.mouseClick(window.release_notes_button, Qt.LeftButton)

    assert window.release_notes_dialog is not None
    assert window.release_notes_dialog.isVisible()
    text = window.release_notes_dialog.findChild(QTextBrowser).toPlainText()
    for version in (
        "2.5.0",
        "2.4.0",
        "2.3.0",
        "2.2.0",
        "2.1.0",
        "2.0.0",
        "1.0.0",
    ):
        assert version in text
    close_button = next(
        button
        for button in window.release_notes_dialog.findChildren(QPushButton)
        if button.text() == "关闭"
    )
    qtbot.mouseClick(close_button, Qt.LeftButton)
    assert not window.release_notes_dialog.isVisible()


def test_retry_only_passes_failed_items(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    window.last_summary = BatchSummary(
        [
            AccessionResult("OK", AccessionStatus.COMPLETED),
            AccessionResult("FAILED", AccessionStatus.FAILED),
        ]
    )
    called = []
    monkeypatch.setattr(window, "_start_download", lambda values=None: called.append(values))

    window._retry_failed()

    assert called == [["FAILED"]]


def test_log_panel_can_be_collapsed_and_restored(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    initial = window._log_panel_expanded

    window._toggle_log_panel()

    assert window._log_panel_expanded is not initial
    assert window.log_panel.isHidden() is initial


def test_log_panel_defaults_collapsed_and_error_expands_it(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)

    assert not window._log_panel_expanded
    assert window.log_panel.isHidden()

    window._append_log("应用", "测试错误", "error")

    assert window._log_panel_expanded
    assert window.log_panel.isVisible()
    assert window.log_toggle_button.text() == "收起日志"


def test_new_task_form_can_collapse_without_losing_input(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    window.accession_edit.setPlainText("A001\nA002")
    window.destination_edit.setText(str(tmp_path / "dicom"))

    qtbot.mouseClick(window.task_form_toggle_button, Qt.LeftButton)

    assert not window._task_form_expanded
    assert window.task_form_body.isHidden()
    assert window.task_form_toggle_button.text() == "展开"
    assert window.task_form_toggle_button.arrowType() == Qt.RightArrow

    qtbot.mouseClick(window.task_form_toggle_button, Qt.LeftButton)

    assert window.accession_edit.toPlainText() == "A001\nA002"
    assert window.destination_edit.text() == str(tmp_path / "dicom")


def test_running_collapses_task_form_and_shows_summary(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    destination = str(tmp_path / "dicom")
    window.accession_edit.setPlainText("A001\nA002")
    window.destination_edit.setText(destination)

    window._set_running(True)

    assert window.task_form_body.isHidden()
    assert window.task_form_summary.isVisible()
    assert "2 个检查号" in window.task_form_summary.text()
    assert destination in window.task_form_summary.toolTip()


def test_finish_worker_uses_active_retry_batch_for_progress_range(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    window.current_accessions = ["A001", "A002", "A003", "A004"]
    window._active_accessions = ["A002", "A004"]

    window._finish_worker()

    assert window.progress_bar.maximum() == 2


def test_new_task_form_collapse_state_is_restored(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    window._set_task_form_expanded(False)
    window.close()

    restored = make_window(qtbot, tmp_path)

    assert not restored._task_form_expanded
    assert restored.task_form_body.isHidden()


def test_close_running_task_requests_cleanup(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)

    class Worker:
        cancelled = False

        def request_cancel(self):
            self.cancelled = True

    worker = Worker()
    window.worker = worker  # type: ignore[assignment]
    window.worker_thread = None
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    event = QCloseEvent()

    window.closeEvent(event)

    assert worker.cancelled
    assert window._closing_after_cancel
    assert not event.isAccepted()
