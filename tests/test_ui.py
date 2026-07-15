from __future__ import annotations

import pytest
from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtGui import QCloseEvent
from PyQt5.QtWidgets import QMessageBox

from dcmget.core import AccessionResult, AccessionStatus, BatchSummary
import dcmget.ui as ui_module
from dcmget.ui import DcmGetWindow


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


def test_settings_validation_is_inline(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    page = window.settings_page
    window.pages.setCurrentIndex(1)
    page.pacs_host_edit.clear()

    page._save()

    assert page.pacs_host_edit.property("invalid") is True
    assert "PACS" in page.pacs_host_edit.toolTip()
    assert page.error_label.isVisible()


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

    window._set_running(True)
    assert window.accession_edit.isReadOnly()
    assert window.destination_edit.isReadOnly()
    assert not window.start_button.isEnabled()
    assert window.stop_button.isEnabled()

    result = AccessionResult("A001", AccessionStatus.COMPLETED, 3, 1.25, "收到 3 个文件")
    window._on_worker_progress(1, 1, result)
    assert window.progress_bar.value() == 1
    assert window.task_table.item(0, 1).text() == "完成"
    assert window.task_table.item(0, 2).text() == "3"


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
