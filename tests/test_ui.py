from __future__ import annotations

from PyQt5.QtGui import QCloseEvent
from PyQt5.QtWidgets import QMessageBox

from dcmget.core import AccessionResult, AccessionStatus, BatchSummary
from dcmget.ui import DcmGetWindow


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
