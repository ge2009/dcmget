from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication, QMessageBox

from dcmget.config import AppConfig, save_config
from dcmget.core import AccessionResult, AccessionStatus, ToolPaths
from dcmget.pdi import PdiExportResult, PdiStatus
from dcmget.task_manager import TaskCatalog
from dcmget.ui import DcmGetWindow
import dcmget.task_controller as controller_module
import dcmget.ui as ui_module


class _Process:
    def poll(self):
        return None


class _BlockingRuntime:
    calls: list[tuple[str, str]] = []
    entered = threading.Event()
    release = threading.Event()
    starts = 0
    block_accession = "A001"
    block_accessions: set[str] | None = None

    def __init__(
        self,
        _config,
        tools,
        *,
        log_callback,
        progress_callback,
        process_callback,
    ):
        self.tools = tools
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.process_callback = process_callback

    def start(self):
        self.__class__.starts += 1
        return _Process()

    def validate_download_config(self, _config):
        return None

    def validate_pdi_config(self, _config):
        return None

    def stop(self, _handle):
        return None

    def run_accession(
        self,
        _handle,
        task_id,
        _config,
        accession,
        move_started=None,
        cancel_event=None,
    ):
        del cancel_event
        if move_started is not None:
            move_started()
        self.__class__.calls.append((task_id, accession))
        self.progress_callback(
            task_id,
            AccessionResult(
                accession,
                AccessionStatus.DOWNLOADING,
                file_count=1,
                speed_bytes_per_second=4096,
            ),
        )
        blocked = self.__class__.block_accessions
        should_block = (
            accession in blocked
            if blocked is not None
            else accession == self.__class__.block_accession
        )
        if should_block:
            self.__class__.entered.set()
            assert self.__class__.release.wait(5)
        return AccessionResult(
            accession,
            AccessionStatus.COMPLETED,
            file_count=1,
            received_bytes=128,
        )

    def cancel_accession(self, _task_id):
        self.__class__.release.set()


def _window(qtbot, tmp_path, monkeypatch):
    settings = tmp_path / "settings.ini"
    monkeypatch.setattr(
        ui_module,
        "QSettings",
        lambda *_args: QSettings(str(settings), QSettings.IniFormat),
    )
    monkeypatch.setattr(
        controller_module,
        "SharedDcmtkRuntime",
        _BlockingRuntime,
    )
    tools = ToolPaths(
        movescu=tmp_path / "movescu",
        storescp=tmp_path / "storescp",
        bin_dir=tmp_path,
        version="3.7.0",
    )
    monkeypatch.setattr(
        ui_module.DcmtkResolver,
        "resolve",
        lambda _self, _configured="": tools,
    )
    monkeypatch.setattr(
        ui_module,
        "prepare_download_entitlement",
        lambda _parent: (True, False, "已完成软件注册"),
    )
    config_path = tmp_path / "config.json"
    save_config(
        config_path,
        AppConfig(dicom_destination_folder=str(tmp_path / "dicom")),
    )
    window = DcmGetWindow(
        config_path,
        tmp_path,
        tmp_path / "active-task.sqlite3",
        offer_task_resume=False,
        enable_multi_task=True,
    )
    qtbot.addWidget(window)
    window.show()
    QApplication.processEvents()
    assert window.task_controller is not None
    return window


def test_running_task_starts_second_task_in_an_available_concurrency_slot(
    qtbot, tmp_path, monkeypatch
):
    _BlockingRuntime.calls = []
    _BlockingRuntime.starts = 0
    _BlockingRuntime.entered = threading.Event()
    _BlockingRuntime.release = threading.Event()
    _BlockingRuntime.block_accession = "A001"
    _BlockingRuntime.block_accessions = None
    window = _window(qtbot, tmp_path, monkeypatch)

    window.accession_edit.setPlainText("A001\nA002")
    window._start_download()
    assert _BlockingRuntime.entered.wait(2)
    QApplication.processEvents()
    first_id = window._selected_task_id

    window._show_new_task_editor()
    window.accession_edit.setPlainText("B001")
    QApplication.processEvents()
    assert window._multi_task_editor_active
    assert window.start_button.text() == "创建并并发开始"
    window._start_download()
    second_id = window._selected_task_id
    assert second_id != first_id

    try:
        qtbot.waitUntil(
            lambda: (second_id, "B001") in _BlockingRuntime.calls,
            timeout=2000,
        )
        assert not _BlockingRuntime.release.is_set()
    finally:
        _BlockingRuntime.release.set()
    qtbot.waitUntil(
        lambda: all(
            summary.phase == "completed"
            for summary in window.task_controller.list_tasks()
        ),
        timeout=5000,
    )

    assert _BlockingRuntime.calls == [
        (first_id, "A001"),
        (second_id, "B001"),
        (first_id, "A002"),
    ]
    assert _BlockingRuntime.starts == 1
    window.close()


def test_third_task_waits_for_a_concurrency_slot_and_shows_queue_copy(
    qtbot, tmp_path, monkeypatch
):
    _BlockingRuntime.calls = []
    _BlockingRuntime.starts = 0
    _BlockingRuntime.entered = threading.Event()
    _BlockingRuntime.release = threading.Event()
    _BlockingRuntime.block_accessions = {"A001", "B001"}
    window = _window(qtbot, tmp_path, monkeypatch)

    try:
        window.accession_edit.setPlainText("A001")
        window._start_download()
        window._show_new_task_editor()
        window.accession_edit.setPlainText("B001")
        window._start_download()
        qtbot.waitUntil(lambda: len(_BlockingRuntime.calls) == 2, timeout=2000)
        qtbot.waitUntil(
            lambda: sum(
                summary.phase == "running"
                for summary in window._workspace_task_summaries.values()
            )
            == 2,
            timeout=2000,
        )

        window._show_new_task_editor()
        window.accession_edit.setPlainText("C001")
        assert window.start_button.text() == "创建并等待并发槽"
        window._start_download()
        third_id = window._selected_task_id
        QApplication.processEvents()

        third = window.task_controller.get_task(third_id).summary
        assert third.phase == "queued"
        assert "等待可用并发槽" in window.progress_label.text()
        index = window.task_workspace.sidebar.model.index_for_task_id(third_id)
        assert index.data(
            window.task_workspace.sidebar.model.StatusTextRole
        ) == "等待并发槽"
    finally:
        _BlockingRuntime.release.set()
        _BlockingRuntime.block_accessions = None

    qtbot.waitUntil(
        lambda: all(
            summary.phase == "completed"
            for summary in window.task_controller.list_tasks()
        ),
        timeout=5000,
    )
    window.close()


def test_task_selection_loads_bounded_detail_and_independent_actions(
    qtbot, tmp_path, monkeypatch
):
    _BlockingRuntime.calls = []
    _BlockingRuntime.starts = 0
    _BlockingRuntime.entered = threading.Event()
    _BlockingRuntime.release = threading.Event()
    _BlockingRuntime.block_accession = "A00000"
    _BlockingRuntime.block_accessions = None
    window = _window(qtbot, tmp_path, monkeypatch)
    accessions = [f"A{index:05d}" for index in range(201)]

    window.accession_edit.setPlainText("\n".join(accessions))
    window._start_download()
    task_id = window._selected_task_id
    assert _BlockingRuntime.entered.wait(2)
    window.task_controller.pause_task(task_id)
    _BlockingRuntime.release.set()
    qtbot.waitUntil(
        lambda: window.task_controller.get_task(task_id).summary.phase == "paused",
        timeout=5000,
    )
    window.task_workspace.select_task(task_id)
    QApplication.processEvents()

    assert window._task_table_summary_mode
    assert window.task_table.rowCount() == 0
    assert "201" in window.large_batch_summary_label.text()
    assert window.pause_button.text() == "继续下载"
    assert window.stop_button.isVisible()
    window.task_controller.cancel_task(task_id)
    window.close()


def test_large_task_detail_uses_full_sql_counts_and_copies_all_failed_items(
    qtbot, tmp_path, monkeypatch
):
    window = _window(qtbot, tmp_path, monkeypatch)
    controller = window.task_controller
    assert controller is not None
    task = controller.catalog.create_task(
        AppConfig(dicom_destination_folder=str(tmp_path / "dicom")),
        (f"A{index:05d}" for index in range(205)),
    )
    controller.catalog.set_phase(task.task_id, "paused")
    controller.catalog.record_result(
        task.task_id,
        AccessionResult("A00000", AccessionStatus.COMPLETED, file_count=1),
    )
    controller.catalog.record_result(
        task.task_id,
        AccessionResult("A00001", AccessionStatus.NO_DATA),
    )
    controller.catalog.record_result(
        task.task_id,
        AccessionResult("A00002", AccessionStatus.PARTIAL),
    )
    controller.catalog.record_result(
        task.task_id,
        AccessionResult("A00003", AccessionStatus.FAILED),
    )
    controller.catalog.record_result(
        task.task_id,
        AccessionResult("A00204", AccessionStatus.FAILED),
    )
    summary = controller.catalog.get_summary(task.task_id)
    original = controller.manager.list_failed_accessions
    queried: list[str] = []

    def list_failed(task_id: str) -> list[str]:
        queried.append(task_id)
        return original(task_id)

    monkeypatch.setattr(controller.manager, "list_failed_accessions", list_failed)
    window._on_multi_tasks_updated([summary])
    window._on_workspace_task_selected(task.task_id)
    QApplication.processEvents()

    assert window._task_table_summary_mode
    assert window.task_table.rowCount() == 0
    assert window._summary_results == {}
    assert queried == []
    assert (
        "已处理 5/205 · 完成 1 · 无数据 1 · 部分成功 1 · "
        "失败 2 · 已取消 0 · 文件 1"
        in window.large_batch_summary_label.text()
    )
    assert window.copy_failed_button.isVisible()
    assert "3 个失败或部分成功" in window.copy_failed_button.toolTip()

    window._copy_failed_accessions()

    assert queried == [task.task_id]
    assert QApplication.clipboard().text() == "A00002\nA00003\nA00204"
    controller.catalog.set_phase(task.task_id, "completed")
    window.close()


def test_pdi_pending_and_running_tasks_expose_stop_action(
    qtbot, tmp_path, monkeypatch
):
    window = _window(qtbot, tmp_path, monkeypatch)
    controller = window.task_controller
    assert controller is not None
    summary = controller.catalog.create_task(AppConfig(), ["A001"])
    phase_holder = {"value": "pdi_pending"}
    cancelled: list[str] = []
    window._selected_task_id = summary.task_id
    monkeypatch.setattr(
        controller.manager,
        "get_task_detail",
        lambda *_args, **_kwargs: SimpleNamespace(
            summary=replace(summary, phase=phase_holder["value"])
        ),
    )
    monkeypatch.setattr(
        controller,
        "cancel_pdi",
        lambda task_id: cancelled.append(task_id),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )

    for phase in ("pdi_pending", "pdi_running"):
        phase_holder["value"] = phase
        window._render_multi_summary(replace(summary, phase=phase))
        QApplication.processEvents()

        assert window.stop_button.isVisible()
        assert window.stop_button.isEnabled()
        window.stop_button.click()
        assert cancelled[-1] == summary.task_id

    assert cancelled == [summary.task_id, summary.task_id]
    controller.catalog.set_phase(summary.task_id, "completed")
    window.close()


def test_task_selection_restores_persisted_pdi_output_after_restart(
    qtbot, tmp_path, monkeypatch
):
    window = _window(qtbot, tmp_path, monkeypatch)
    controller = window.task_controller
    assert controller is not None
    output = tmp_path / "pdi" / "DCMGET_PDI_TEST"
    output.mkdir(parents=True)
    task = controller.catalog.create_task(
        AppConfig(
            pdi_export_enabled=True,
            pdi_institution_name="测试医院",
            pdi_output_folder=str(tmp_path / "pdi"),
        ),
        ["A001"],
    )
    controller.catalog.set_phase(task.task_id, "pdi_retryable")
    persisted = PdiExportResult(
        PdiStatus.PARTIAL,
        output_directory=str(output),
        message="PDI 已生成，存在警告",
        warnings=["viewer warning"],
    )
    controller.catalog.save_pdi_result(task.task_id, persisted)

    summary = controller.catalog.get_summary(task.task_id)
    window._on_multi_tasks_updated([summary])
    window._on_workspace_task_selected(task.task_id)
    QApplication.processEvents()

    assert window.last_pdi_result == persisted
    assert "存在警告" in window.pdi_status_label.text()
    assert window.pdi_open_button.isEnabled()
    assert window.pdi_retry_button.isEnabled()
    controller.catalog.set_phase(task.task_id, "completed")
    window.close()


def test_close_preserves_completed_accession_and_next_window_resumes_remaining(
    qtbot, tmp_path, monkeypatch
):
    _BlockingRuntime.calls = []
    _BlockingRuntime.starts = 0
    _BlockingRuntime.entered = threading.Event()
    _BlockingRuntime.release = threading.Event()
    _BlockingRuntime.block_accession = "A001"
    _BlockingRuntime.block_accessions = None
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )
    window = _window(qtbot, tmp_path, monkeypatch)
    window.accession_edit.setPlainText("A001\nA002")
    window._start_download()
    task_id = window._selected_task_id
    assert _BlockingRuntime.entered.wait(2)

    assert window.close()
    QApplication.processEvents()

    catalog = TaskCatalog(tmp_path / "tasks.sqlite3", auto_migrate=False)
    closed_summary = catalog.get_summary(task_id)
    assert closed_summary.phase == "queued"
    assert closed_summary.processed_count == 1
    assert closed_summary.pending_count == 1

    _BlockingRuntime.entered = threading.Event()
    _BlockingRuntime.release = threading.Event()
    _BlockingRuntime.block_accession = "A001"
    restored = _window(qtbot, tmp_path, monkeypatch)

    qtbot.waitUntil(
        lambda: restored.task_controller.get_task(task_id).summary.phase
        == "completed",
        timeout=5000,
    )
    assert _BlockingRuntime.calls == [(task_id, "A001"), (task_id, "A002")]
    restored.close()


def test_close_records_two_concurrent_tasks_that_finish_during_shutdown(
    qtbot, tmp_path, monkeypatch
):
    _BlockingRuntime.calls = []
    _BlockingRuntime.starts = 0
    _BlockingRuntime.entered = threading.Event()
    _BlockingRuntime.release = threading.Event()
    _BlockingRuntime.block_accessions = {"A001", "B001"}
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )
    window = _window(qtbot, tmp_path, monkeypatch)

    try:
        window.accession_edit.setPlainText("A001")
        window._start_download()
        first_id = window._selected_task_id

        window._show_new_task_editor()
        window.accession_edit.setPlainText("B001")
        window._start_download()
        second_id = window._selected_task_id

        qtbot.waitUntil(lambda: len(_BlockingRuntime.calls) == 2, timeout=2000)
        qtbot.waitUntil(
            lambda: sum(
                summary.phase == "running"
                for summary in window.task_controller.list_tasks()
            )
            == 2,
            timeout=2000,
        )
        assert window.close()
        QApplication.processEvents()

        catalog = TaskCatalog(tmp_path / "tasks.sqlite3", auto_migrate=False)
        first = catalog.get_summary(first_id)
        second = catalog.get_summary(second_id)
        assert first.phase == "completed"
        assert second.phase == "completed"
        assert first.processed_count == 1
        assert second.processed_count == 1
    finally:
        _BlockingRuntime.release.set()
        _BlockingRuntime.block_accessions = None


def test_unfinished_tasks_lock_concurrency_but_keep_future_task_defaults_editable(
    qtbot, tmp_path, monkeypatch
):
    window = _window(qtbot, tmp_path, monkeypatch)
    controller = window.task_controller
    assert controller is not None
    task = controller.manager.create_task(AppConfig(), ["A001"])
    controller.pause_task(task.task_id)

    window._show_settings()

    assert not window.settings_page.max_concurrent_moves_spin.isEnabled()
    assert not window.settings_page.dcmtk_browse_button.isEnabled()
    assert window.settings_page.directory_template_combo.isEnabled()
    assert "全局锁定" in window.settings_page.max_concurrent_moves_spin.toolTip()

    controller.cancel_task(task.task_id)
    window.pages.setCurrentWidget(window.task_page)
    window._show_settings()

    assert window.settings_page.max_concurrent_moves_spin.isEnabled()
    window.close()
