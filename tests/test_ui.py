from __future__ import annotations

from pathlib import Path
import threading
import traceback

import pytest
from PyQt5.QtCore import QSettings, Qt, QThread
from PyQt5.QtGui import QCloseEvent
from PyQt5.QtWidgets import (
    QApplication,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextBrowser,
)

from dcmget.config import AppConfig
from dcmget.core import AccessionResult, AccessionStatus, BatchSummary, ToolPaths
from dcmget.task_state import TaskCheckpoint, TaskCheckpointStore
import dcmget.ui as ui_module
from dcmget.ui import DcmGetWindow, DownloadWorker, PdiWorker


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    path = tmp_path / "ui-settings.ini"
    monkeypatch.setattr(
        ui_module,
        "QSettings",
        lambda *_args: QSettings(str(path), QSettings.IniFormat),
    )


def make_window(qtbot, tmp_path):
    window = DcmGetWindow(
        tmp_path / "config.json",
        tmp_path,
        tmp_path / "active-task.sqlite3",
        offer_task_resume=False,
    )
    qtbot.addWidget(window)
    window.show()
    return window


def test_paste_preview_deduplicates_and_updates_table(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    window.accession_edit.setPlainText("A001\n\nA002\nA001")

    assert window.current_accessions == ["A001", "A002"]
    assert window.accession_summary.text() == "有效 2 · 空行 1 · 重复 1 · 无效 0"
    assert window.task_table.rowCount() == 2
    assert window.task_table.item(1, 0).text() == "A002"


def test_more_than_200_accessions_use_summary_mode_without_table_rows(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    accessions = [f"A{index:05d}" for index in range(40_000)]

    window.accession_edit.setPlainText("\n".join(accessions))

    assert window.current_accessions == accessions
    assert window.task_table.rowCount() == 0
    assert window.row_by_accession == {}
    assert window.task_table.isHidden()
    assert window.large_batch_summary_card.isVisible()
    assert "40,000" in window.large_batch_summary_label.text()
    assert "超过 200 条" in window.large_batch_summary_label.text()


def test_200_accessions_keep_details_and_201_hide_them(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    two_hundred = [f"A{index:03d}" for index in range(200)]

    window._populate_waiting_rows(two_hundred)
    assert window.task_table.rowCount() == 200
    assert not window._task_table_summary_mode

    window._populate_waiting_rows([*two_hundred, "A200"])
    assert window.task_table.rowCount() == 0
    assert window._task_table_summary_mode


def test_large_recovery_restores_processed_summary_and_collapsed_count(
    qtbot, tmp_path
):
    window = make_window(qtbot, tmp_path)
    accessions = [f"A{index:03d}" for index in range(201)]
    checkpoint = TaskCheckpoint(
        task_id="a" * 32,
        config=AppConfig(dicom_destination_folder=str(tmp_path / "dicom")),
        accessions=accessions,
        results=[
            AccessionResult("A000", AccessionStatus.COMPLETED, file_count=2),
            AccessionResult("A001", AccessionStatus.FAILED),
        ],
        partial_results={},
        trial_required=False,
        created_at="2026-07-16T00:00:00+00:00",
        phase="downloading",
    )

    window._hold_download_resume(checkpoint, "已保留恢复任务")
    window._set_task_form_expanded(False)

    assert window.current_accessions == []
    assert window.task_table.rowCount() == 0
    assert "已处理 2/201" in window.large_batch_summary_label.text()
    assert "完成 1" in window.large_batch_summary_label.text()
    assert "失败 1" in window.large_batch_summary_label.text()
    assert "201 个检查号" in window.task_form_summary.text()


def test_large_batch_progress_is_aggregated_once_and_small_retry_restores_table(
    qtbot, tmp_path
):
    window = make_window(qtbot, tmp_path)
    accessions = [f"A{index:03d}" for index in range(201)]
    window._display_total = len(accessions)
    window._populate_waiting_rows(accessions)
    downloading = AccessionResult(
        "A000", AccessionStatus.DOWNLOADING, file_count=2
    )

    window._on_worker_progress(1, len(accessions), downloading)
    window._on_worker_progress(1, len(accessions), downloading)
    window._on_worker_progress(
        1,
        len(accessions),
        AccessionResult(
            "A000",
            AccessionStatus.COMPLETED,
            file_count=3,
            archived_files=[str(tmp_path / f"image-{index}.dcm") for index in range(3)],
        ),
    )
    window._on_worker_progress(
        2,
        len(accessions),
        AccessionResult("A001", AccessionStatus.FAILED),
    )

    assert window._summary_processed == 2
    assert window._summary_files == 3
    assert window._summary_results["A000"].archived_files == []
    assert "失败 1" in window.large_batch_summary_label.text()
    assert window.task_table.rowCount() == 0

    window._populate_waiting_rows(["A001", "A002"])
    assert not window._task_table_summary_mode
    assert window.task_table.isVisible()
    assert window.task_table.rowCount() == 2
    assert window.row_by_accession == {"A001": 0, "A002": 1}


def test_large_batch_cancel_does_not_count_unstarted_items_as_processed(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    accessions = [f"A{index:03d}" for index in range(201)]
    checkpoint = window.task_store.start(
        AppConfig(), accessions, trial_required=False
    )
    window._active_task_id = checkpoint.task_id
    window._active_accessions = accessions
    window._display_total = len(accessions)
    window._populate_waiting_rows(accessions)
    monkeypatch.setattr(window, "_show_download_completion", lambda *_args: None)

    window._on_worker_finished(
        BatchSummary(
            [
                AccessionResult(
                    accession,
                    AccessionStatus.CANCELLED,
                    message="任务尚未开始",
                )
                for accession in accessions
            ],
            cancelled=True,
        )
    )

    assert window._summary_processed == 0
    assert "已处理 0/201" in window.large_batch_summary_label.text()
    assert len(window.task_store.load_required().pending_accessions) == 201


def test_large_batch_cancel_keeps_current_partial_file_count(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    accessions = [f"A{index:03d}" for index in range(201)]
    checkpoint = window.task_store.start(
        AppConfig(), accessions, trial_required=False
    )
    partial = AccessionResult(
        "A000",
        AccessionStatus.CANCELLED,
        file_count=2,
        message="用户已取消",
        archived_files=[str(tmp_path / "one.dcm"), str(tmp_path / "two.dcm")],
    )
    window.task_store.record_result(checkpoint.task_id, partial)
    window._active_task_id = checkpoint.task_id
    window._active_accessions = accessions
    window._display_total = len(accessions)
    window._populate_waiting_rows(accessions)
    monkeypatch.setattr(window, "_show_download_completion", lambda *_args: None)

    window._on_worker_finished(
        BatchSummary(
            [
                partial,
                *[
                    AccessionResult(
                        accession,
                        AccessionStatus.CANCELLED,
                        message="任务尚未开始",
                    )
                    for accession in accessions[1:]
                ],
            ],
            cancelled=True,
        )
    )

    assert window._summary_processed == 1
    assert window._summary_files == 2
    assert "已取消 1" in window.large_batch_summary_label.text()


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


def test_settings_ports_are_plain_text_fields_with_range_validation(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    page = window.settings_page
    page.set_config(AppConfig(pacs_server_port=104, storage_port=6666))

    assert isinstance(page.pacs_port_edit, QLineEdit)
    assert isinstance(page.storage_port_edit, QLineEdit)
    assert page.pacs_port_edit.text() == "104"
    assert page.storage_port_edit.text() == "6666"
    assert page.pacs_port_edit.hasAcceptableInput()
    assert page.storage_port_edit.hasAcceptableInput()

    page.pacs_port_edit.setText("65536")
    page.storage_port_edit.clear()
    errors = page.config().validate()

    assert "pacs_server_port" in errors
    assert "storage_port" in errors


def test_dcmtk_ready_status_only_appears_in_header_but_errors_remain_inline(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    tools = ToolPaths(
        movescu=tmp_path / "movescu",
        storescp=tmp_path / "storescp",
        bin_dir=tmp_path,
        version="3.7.0",
    )
    monkeypatch.setattr(window.resolver, "resolve", lambda *_args: tools)

    window._refresh_tool_status()

    assert window.tool_status.text() == "DCMTK 3.7.0 已就绪"
    assert window.settings_page.dcmtk_hint.text() == ""
    assert window.settings_page.dcmtk_status_label.isHidden()
    assert window.settings_page.dcmtk_hint.isHidden()

    def fail_resolve(*_args):
        raise RuntimeError("找不到 movescu")

    monkeypatch.setattr(window.resolver, "resolve", fail_resolve)
    window._refresh_tool_status()

    assert window.tool_status.text() == "DCMTK 未就绪"
    assert window.settings_page.dcmtk_hint.text() == "找不到 movescu"
    assert not window.settings_page.dcmtk_status_label.isHidden()
    assert not window.settings_page.dcmtk_hint.isHidden()


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


def test_worker_persists_final_result_before_emitting_progress(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    checkpoint = store.start(AppConfig(), ["A001"], trial_required=False)
    worker = DownloadWorker(
        AppConfig(),
        ToolPaths(tmp_path / "movescu", tmp_path / "storescp", tmp_path, "3.7.0"),
        ["A001"],
        task_store=store,
        task_id=checkpoint.task_id,
    )
    persisted_before_signal = []
    worker.progress.connect(
        lambda *_args: persisted_before_signal.append(
            [result.accession for result in store.load_required().results]
        )
    )

    worker._report_progress(
        1,
        1,
        AccessionResult("A001", AccessionStatus.COMPLETED),
    )

    assert persisted_before_signal == [["A001"]]


def test_download_worker_records_full_traceback_before_failure_signal(
    tmp_path, monkeypatch
):
    recorded = []

    def capture_exception(context, exc):
        recorded.append((context, exc, traceback.format_exc()))

    class Runner:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, _accessions):
            raise RuntimeError("download worker exploded")

    monkeypatch.setattr(ui_module, "record_exception", capture_exception)
    monkeypatch.setattr(ui_module, "DownloadRunner", Runner)
    worker = DownloadWorker(
        AppConfig(),
        ToolPaths(tmp_path / "movescu", tmp_path / "storescp", tmp_path, "3.7.0"),
        ["A001"],
    )
    failures = []
    worker.failed.connect(failures.append)

    worker.run()

    assert failures == ["download worker exploded"]
    assert recorded[0][0] == "DownloadWorker.run"
    assert isinstance(recorded[0][1], RuntimeError)
    assert "Traceback (most recent call last)" in recorded[0][2]
    assert "RuntimeError: download worker exploded" in recorded[0][2]


def test_pdi_worker_records_full_traceback_before_failure_signal(
    tmp_path, monkeypatch
):
    import dcmget.pdi as pdi_module

    recorded = []

    def capture_exception(context, exc):
        recorded.append((context, exc, traceback.format_exc()))

    class Exporter:
        def __init__(self, *_args, **_kwargs):
            pass

        def export(self, _files):
            raise RuntimeError("PDI worker exploded")

    monkeypatch.setattr(ui_module, "record_exception", capture_exception)
    monkeypatch.setattr(pdi_module, "PdiExporter", Exporter)
    worker = PdiWorker(
        AppConfig(),
        ToolPaths(tmp_path / "movescu", tmp_path / "storescp", tmp_path, "3.7.0"),
        [str(tmp_path / "image.dcm")],
        tmp_path,
    )
    failures = []
    worker.failed.connect(failures.append)

    worker.run()

    assert failures == ["PDI worker exploded"]
    assert recorded[0][0] == "PdiWorker.run"
    assert isinstance(recorded[0][1], RuntimeError)
    assert "Traceback (most recent call last)" in recorded[0][2]
    assert "RuntimeError: PDI worker exploded" in recorded[0][2]


def test_startup_offer_restores_snapshot_and_only_resumes_pending_items(
    qtbot, tmp_path, monkeypatch
):
    state_path = tmp_path / "active-task.sqlite3"
    store = TaskCheckpointStore(state_path)
    destination = tmp_path / "original-destination"
    checkpoint = store.start(
        AppConfig(dicom_destination_folder=str(destination)),
        ["A001", "A002", "A003"],
        trial_required=False,
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult("A001", AccessionStatus.COMPLETED),
    )
    window = make_window(qtbot, tmp_path)
    started = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )
    monkeypatch.setattr(
        window,
        "_start_download",
        lambda override=None, **kwargs: started.append((override, kwargs)),
    )

    window._offer_task_resume()

    assert window.current_accessions == ["A001", "A002", "A003"]
    assert window.destination_edit.text() == str(destination)
    resumed = started[0][1]["resume_checkpoint"]
    assert resumed.pending_accessions == ["A002", "A003"]
    assert resumed.config.dicom_destination_folder == str(destination)


def test_finished_resume_merges_history_and_clears_checkpoint(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    checkpoint = window.task_store.start(
        AppConfig(), ["A001", "A002"], trial_required=False
    )
    first = AccessionResult(
        "A001",
        AccessionStatus.COMPLETED,
        archived_files=[str(tmp_path / "a.dcm")],
    )
    second = AccessionResult(
        "A002",
        AccessionStatus.COMPLETED,
        archived_files=[str(tmp_path / "b.dcm")],
    )
    window.task_store.record_result(checkpoint.task_id, first)
    window.task_store.record_result(checkpoint.task_id, second)
    window._active_task_id = checkpoint.task_id
    window._display_total = 2
    window._active_accessions = ["A002"]
    monkeypatch.setattr(window, "_show_download_completion", lambda *_args, **_kwargs: None)

    window._on_worker_finished(BatchSummary([second]))

    assert [result.accession for result in window.last_summary.results] == [
        "A001",
        "A002",
    ]
    assert window.last_summary.archived_files == [
        str(tmp_path / "a.dcm"),
        str(tmp_path / "b.dcm"),
    ]
    assert window.task_store.load() is None


def test_failed_batch_remains_retryable_and_reuses_original_task(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    checkpoint = window.task_store.start(
        AppConfig(), ["OK", "FAILED"], trial_required=False
    )
    completed = AccessionResult("OK", AccessionStatus.COMPLETED)
    failed = AccessionResult("FAILED", AccessionStatus.FAILED, message="timeout")
    window.task_store.record_result(checkpoint.task_id, completed)
    window.task_store.record_result(checkpoint.task_id, failed)
    window._active_task_id = checkpoint.task_id
    window._display_total = 2
    monkeypatch.setattr(window, "_show_download_completion", lambda *_args, **_kwargs: None)

    window._on_worker_finished(BatchSummary([completed, failed]))

    restored = window.task_store.load_required()
    assert restored.phase == "download_retryable"
    assert window._resume_checkpoint is not None
    assert window._resume_checkpoint.task_id == checkpoint.task_id
    assert window.start_button.text() == "重试失败项"
    assert window.retry_button.isEnabled()

    started = []
    monkeypatch.setattr(
        window,
        "_start_download",
        lambda override=None, **kwargs: started.append((override, kwargs)),
    )
    window._retry_failed()

    assert started == [(None, {"resume_checkpoint": window._resume_checkpoint})]


def test_declining_download_resume_keeps_checkpoint_for_later(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    checkpoint = window.task_store.start(
        AppConfig(), ["A001", "A002"], trial_required=False
    )
    window.task_store.record_result(
        checkpoint.task_id,
        AccessionResult("A001", AccessionStatus.COMPLETED),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.No,
    )

    window._offer_task_resume()

    assert window.task_store.load_required().task_id == checkpoint.task_id
    assert window._resume_checkpoint is not None
    assert window._resume_checkpoint.task_id == checkpoint.task_id
    assert window.start_button.text() == "继续未完成任务"
    assert window.discard_resume_button.isVisible()


def test_pending_resume_locks_inputs_and_disables_failed_retry_until_discarded(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    checkpoint = window.task_store.start(
        AppConfig(), ["FAILED", "PENDING"], trial_required=False
    )
    window.task_store.record_result(
        checkpoint.task_id,
        AccessionResult("FAILED", AccessionStatus.FAILED),
    )
    window._resume_checkpoint = window.task_store.load_required()
    window.last_summary = BatchSummary(
        [
            AccessionResult("FAILED", AccessionStatus.FAILED),
            AccessionResult("PENDING", AccessionStatus.CANCELLED),
        ],
        cancelled=True,
    )

    window._set_running(False)

    assert window.start_button.text() == "继续未完成任务"
    assert window.accession_edit.isReadOnly()
    assert not window.destination_edit.isReadOnly()
    assert window.settings_button.isEnabled()
    assert not window.retry_button.isEnabled()
    assert window.discard_resume_button.isVisible()

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )
    qtbot.mouseClick(window.discard_resume_button, Qt.LeftButton)

    assert window.task_store.load() is None
    assert not window.accession_edit.isReadOnly()
    assert window.start_button.text() == "开始下载"
    assert window.last_summary is None
    assert not window._accepted_partial_results
    assert not window.retry_button.isEnabled()


def test_version_notes_dialog_lists_upgrade_history(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)

    qtbot.mouseClick(window.release_notes_button, Qt.LeftButton)

    assert window.release_notes_dialog is not None
    assert window.release_notes_dialog.isVisible()
    text = window.release_notes_dialog.findChild(QTextBrowser).toPlainText()
    for version in (
        "2.6.2",
        "2.6.1",
        "2.6.0",
        "2.5.2",
        "2.5.1",
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


def test_start_button_mouse_click_uses_current_accessions(
    qtbot, tmp_path, monkeypatch
):
    started = []

    def capture_start(window, override=None, *, resume_checkpoint=None):
        values = window.current_accessions if override is None else override
        started.append((list(values), resume_checkpoint))

    monkeypatch.setattr(DcmGetWindow, "_start_download", capture_start)
    window = make_window(qtbot, tmp_path)
    window.accession_edit.setPlainText("A001\nA002")

    qtbot.mouseClick(window.start_button, Qt.LeftButton)

    assert started == [(["A001", "A002"], None)]


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


def test_header_diagnostic_log_button_opens_private_log_directory(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    diagnostic_logs = tmp_path / "private-state" / "logs"
    opened = []
    monkeypatch.setattr(
        ui_module, "diagnostic_log_directory", lambda: diagnostic_logs
    )
    monkeypatch.setattr(
        ui_module.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toLocalFile()),
    )

    qtbot.mouseClick(window.diagnostic_log_button, Qt.LeftButton)

    assert diagnostic_logs.is_dir()
    assert len(opened) == 1
    assert Path(opened[0]).resolve() == diagnostic_logs.resolve()


def test_open_destination_button_opens_exact_target_directory(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    destination = tmp_path / "DICOM 结果"
    destination.mkdir()
    window.destination_edit.setText(str(destination))
    opened = []
    monkeypatch.setattr(
        ui_module.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toLocalFile()),
    )

    qtbot.mouseClick(window.open_destination_button, Qt.LeftButton)

    assert len(opened) == 1
    assert Path(opened[0]).resolve() == destination.resolve()


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


def test_download_result_keeps_thread_busy_until_thread_finished(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    thread = QThread(window)
    worker = object()
    completions = []
    window.worker = worker  # type: ignore[assignment]
    window.worker_thread = thread
    window._set_running(True)
    thread.finished.connect(window._on_worker_thread_finished)
    monkeypatch.setattr(
        window,
        "_show_download_completion",
        lambda *_args, **_kwargs: completions.append(True),
    )
    thread.start()
    qtbot.waitUntil(thread.isRunning)

    window._on_worker_finished(BatchSummary())

    assert window.worker is worker
    assert window.worker_thread is thread
    assert window._is_busy()
    assert not window.start_button.isEnabled()
    assert completions == []

    thread.quit()
    qtbot.waitUntil(lambda: window.worker_thread is None)

    assert window.worker is None
    assert not window._is_busy()
    assert window.start_button.isEnabled()
    assert completions == [True]


def test_download_failure_keeps_thread_busy_until_thread_finished(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    thread = QThread(window)
    worker = object()
    critical_messages = []
    window.worker = worker  # type: ignore[assignment]
    window.worker_thread = thread
    window._set_running(True)
    thread.finished.connect(window._on_worker_thread_finished)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, title, message: critical_messages.append((title, message)),
    )
    thread.start()
    qtbot.waitUntil(thread.isRunning)

    window._on_worker_failed("download failed")

    assert window.worker is worker
    assert window.worker_thread is thread
    assert window._is_busy()
    assert not window.start_button.isEnabled()
    assert critical_messages == []

    thread.quit()
    qtbot.waitUntil(lambda: window.worker_thread is None)

    assert window.worker is None
    assert not window._is_busy()
    assert window.start_button.isEnabled()
    assert critical_messages[0][0] == "下载中断"
    assert "download failed" in critical_messages[0][1]


def test_pdi_failure_keeps_thread_busy_and_retry_disabled_until_finished(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    thread = QThread(window)
    worker = object()
    completions = []
    window.pdi_worker = worker  # type: ignore[assignment]
    window.pdi_thread = thread
    window._pdi_source_files = [str(tmp_path / "image.dcm")]
    window.last_summary = BatchSummary()
    window._set_running(True, reset_summary=False, can_pause=False)
    thread.finished.connect(window._on_pdi_thread_finished)
    monkeypatch.setattr(
        window,
        "_show_download_completion",
        lambda message="", **kwargs: completions.append((message, kwargs)),
    )
    thread.start()
    qtbot.waitUntil(thread.isRunning)

    window._on_pdi_failed("PDI failed")

    assert window.pdi_worker is worker
    assert window.pdi_thread is thread
    assert window._is_busy()
    assert not window.pdi_retry_button.isEnabled()
    assert completions == []

    thread.quit()
    qtbot.waitUntil(lambda: window.pdi_thread is None)

    assert window.pdi_worker is None
    assert not window._is_busy()
    assert window.pdi_retry_button.isEnabled()
    assert completions == [("PDI 导出失败：PDI failed", {"pdi_problem": True})]


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


def test_close_waits_for_running_thread_before_window_is_destroyed(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)

    class Worker:
        cancelled = False

        def request_cancel(self):
            self.cancelled = True

    worker = Worker()
    thread = QThread(window)
    window.worker = worker  # type: ignore[assignment]
    window.worker_thread = thread
    thread.finished.connect(window._on_worker_thread_finished)
    thread.start()
    qtbot.waitUntil(thread.isRunning)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes
    )
    event = QCloseEvent()

    window.closeEvent(event)

    assert worker.cancelled
    assert thread.isRunning()
    assert window.worker_thread is thread
    assert window.isVisible()
    assert not event.isAccepted()

    thread.quit()
    qtbot.waitUntil(lambda: not thread.isRunning())
    qtbot.waitUntil(lambda: not window.isVisible())

    assert window.worker_thread is None
    assert window.worker is None
