from __future__ import annotations

import threading
import time

import pytest

from dcmget.config import AppConfig
from dcmget.core import AccessionResult, AccessionStatus, ToolPaths
from dcmget.pdi import PdiExportResult, PdiStatus
from dcmget.task_controller import TaskExecutionController


class _Process:
    def poll(self):
        return None


class _Runtime:
    calls: list[tuple[str, str]] = []
    starts = 0
    stops = 0

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
        self.__class__.stops += 1

    def run_accession(
        self,
        _handle,
        task_id,
        _config,
        accession,
        move_started=None,
        _cancel_event=None,
    ):
        if move_started is not None:
            move_started()
        self.__class__.calls.append((task_id, accession))
        self.progress_callback(
            task_id,
            AccessionResult(
                accession,
                AccessionStatus.DOWNLOADING,
                file_count=1,
                speed_bytes_per_second=2048,
            ),
        )
        return AccessionResult(
            accession,
            AccessionStatus.COMPLETED,
            file_count=1,
            received_bytes=128,
        )

    def cancel_accession(self, _task_id):
        return None


def _tools(tmp_path):
    return ToolPaths(
        movescu=tmp_path / "movescu",
        storescp=tmp_path / "storescp",
        bin_dir=tmp_path,
        version="3.7.0",
    )


def test_controller_applies_configured_move_concurrency(tmp_path, monkeypatch):
    from dcmget import task_controller

    monkeypatch.setattr(task_controller, "SharedDcmtkRuntime", _Runtime)
    controller = TaskExecutionController(
        AppConfig(max_concurrent_moves=5),
        _tools(tmp_path),
        catalog_path=tmp_path / "tasks.sqlite3",
        legacy_path=tmp_path / "active-task.sqlite3",
    )

    assert controller.manager.max_concurrent_moves == 5
    assert controller.manager.receiver is not None
    assert controller.manager.receiver.max_concurrent_moves == 5
    assert controller.shutdown()


def test_controller_deletes_completed_task_and_publishes_current_list(
    qtbot, tmp_path, monkeypatch
):
    from dcmget import task_controller

    monkeypatch.setattr(task_controller, "SharedDcmtkRuntime", _Runtime)
    controller = TaskExecutionController(
        AppConfig(),
        _tools(tmp_path),
        catalog_path=tmp_path / "tasks.sqlite3",
        legacy_path=tmp_path / "active-task.sqlite3",
    )
    task = controller.manager.create_task(AppConfig(), ["A001"])
    controller.catalog.set_phase(task.task_id, "completed")

    with qtbot.waitSignal(controller.tasks_updated) as updated:
        controller.delete_task(task.task_id)

    assert updated.args == [[]]
    assert controller.list_tasks() == []
    assert controller.shutdown()


def test_controller_runs_tasks_fairly_and_publishes_live_progress(
    qtbot, tmp_path, monkeypatch
):
    from dcmget import task_controller

    _Runtime.calls = []
    _Runtime.starts = 0
    _Runtime.stops = 0
    monkeypatch.setattr(task_controller, "SharedDcmtkRuntime", _Runtime)
    controller = TaskExecutionController(
        AppConfig(),
        _tools(tmp_path),
        catalog_path=tmp_path / "tasks.sqlite3",
        legacy_path=tmp_path / "active-task.sqlite3",
    )
    first = controller.manager.create_task(AppConfig(), ["A001", "A002"])
    second = controller.manager.create_task(AppConfig(), ["B001", "B002"])
    progress = []
    controller.progress.connect(lambda task_id, result: progress.append((task_id, result)))

    controller.start()
    qtbot.waitUntil(
        lambda: all(
            item.phase == "completed" for item in controller.list_tasks()
        ),
        timeout=5000,
    )

    assert _Runtime.calls == [
        (first.task_id, "A001"),
        (second.task_id, "B001"),
        (first.task_id, "A002"),
        (second.task_id, "B002"),
    ]
    assert {task_id for task_id, _result in progress} == {
        first.task_id,
        second.task_id,
    }
    assert _Runtime.starts == 1
    assert controller.shutdown()
    assert _Runtime.stops == 1


def test_controller_create_task_wakes_idle_scheduler(qtbot, tmp_path, monkeypatch):
    from dcmget import task_controller

    _Runtime.calls = []
    _Runtime.starts = 0
    _Runtime.stops = 0
    monkeypatch.setattr(task_controller, "SharedDcmtkRuntime", _Runtime)
    controller = TaskExecutionController(
        AppConfig(),
        _tools(tmp_path),
        catalog_path=tmp_path / "tasks.sqlite3",
        legacy_path=tmp_path / "active-task.sqlite3",
    )
    controller.start()

    created = controller.create_task(AppConfig(), ["C001"])
    qtbot.waitUntil(
        lambda: controller.get_task(created.task_id).summary.phase == "completed",
        timeout=5000,
    )

    assert _Runtime.calls == [(created.task_id, "C001")]
    assert controller.shutdown()


def test_controller_restores_tasks_with_different_receiver_snapshots(
    tmp_path, monkeypatch
):
    from dcmget import task_controller
    from dcmget.task_manager import TaskCatalog

    monkeypatch.setattr(task_controller, "SharedDcmtkRuntime", _Runtime)
    path = tmp_path / "tasks.sqlite3"
    catalog = TaskCatalog(path, auto_migrate=False)
    first = catalog.create_task(
        AppConfig(
            pacs_server_ip="192.0.2.10",
            calling_ae_title="CALLING_A",
            storage_ae_title="STORE_A",
            storage_port=7777,
        ),
        ["A001"],
    )
    second = catalog.create_task(
        AppConfig(
            pacs_server_ip="198.51.100.20",
            calling_ae_title="CALLING_B",
            storage_ae_title="STORE_B",
            storage_port=8888,
        ),
        ["B001"],
    )

    controller = TaskExecutionController(
        AppConfig(storage_port=6666, max_concurrent_moves=5),
        _tools(tmp_path),
        catalog_path=path,
        legacy_path=tmp_path / "unused.sqlite3",
    )

    assert {item.task_id for item in controller.list_tasks()} == {
        first.task_id,
        second.task_id,
    }
    assert controller.catalog.get_config(first.task_id).storage_port == 7777
    assert controller.catalog.get_config(second.task_id).storage_port == 8888
    assert controller.manager.max_concurrent_moves == 5
    assert controller.shutdown()


def test_controller_rejects_pdi_resume_with_different_dcmtk_snapshot(tmp_path):
    from dcmget.task_manager import TaskCatalog

    path = tmp_path / "tasks.sqlite3"
    catalog = TaskCatalog(path, auto_migrate=False)
    task = catalog.create_task(AppConfig(dcmtk_bin_dir="/old/dcmtk"), ["A001"])
    catalog.begin_pdi_attempt(task.task_id, reuse_existing=False)

    with pytest.raises(RuntimeError, match="PDI.*DCMTK"):
        TaskExecutionController(
            AppConfig(dcmtk_bin_dir="/new/dcmtk"),
            _tools(tmp_path),
            catalog_path=path,
            legacy_path=tmp_path / "unused.sqlite3",
        )

    reopened = TaskCatalog(path, auto_migrate=False)
    assert reopened.try_acquire_foreground_lease()
    reopened.release_foreground_lease()


def test_controller_blocks_when_previous_process_cannot_be_cleaned(
    tmp_path, monkeypatch
):
    from dcmget.task_manager import TaskCatalog

    path = tmp_path / "tasks.sqlite3"
    catalog = TaskCatalog(path, auto_migrate=False)
    catalog.create_task(AppConfig(), ["A001"])
    monkeypatch.setattr(
        TaskCatalog,
        "unresolved_process_records",
        lambda _self: ["movescu PID 123（任务 deadbeef）"],
    )

    with pytest.raises(RuntimeError, match="仍无法安全结束"):
        TaskExecutionController(
            AppConfig(),
            _tools(tmp_path),
            catalog_path=path,
            legacy_path=tmp_path / "unused.sqlite3",
        )

    reopened = TaskCatalog(path, auto_migrate=False)
    assert reopened.try_acquire_foreground_lease()
    reopened.release_foreground_lease()


def test_controller_routes_scheduler_error_to_the_failed_task(
    qtbot, tmp_path, monkeypatch
):
    from dcmget import task_controller

    class FailingRuntime(_Runtime):
        def run_accession(
            self,
            _handle,
            _task_id,
            _config,
            _accession,
            move_started=None,
            _cancel_event=None,
        ):
            if move_started is not None:
                move_started()
            raise RuntimeError("move failed")

    monkeypatch.setattr(task_controller, "SharedDcmtkRuntime", FailingRuntime)
    controller = TaskExecutionController(
        AppConfig(),
        _tools(tmp_path),
        catalog_path=tmp_path / "tasks.sqlite3",
        legacy_path=tmp_path / "active-task.sqlite3",
    )
    task = controller.manager.create_task(AppConfig(), ["A001"])
    errors = []
    controller.scheduler_error.connect(
        lambda task_id, message: errors.append((task_id, message))
    )

    controller.start()
    qtbot.waitUntil(lambda: bool(errors), timeout=5000)

    assert errors == [(task.task_id, "move failed")]
    assert controller.shutdown()


def test_retry_uses_terminal_task_receiver_snapshot(qtbot, tmp_path, monkeypatch):
    from dcmget import task_controller
    from dcmget.task_manager import TaskCatalog

    monkeypatch.setattr(task_controller, "SharedDcmtkRuntime", _Runtime)
    path = tmp_path / "tasks.sqlite3"
    catalog = TaskCatalog(path, auto_migrate=False)
    task = catalog.create_task(AppConfig(storage_port=7777), ["A001"])
    catalog.set_phase(task.task_id, "failed")
    controller = TaskExecutionController(
        AppConfig(storage_port=6666),
        _tools(tmp_path),
        catalog_path=path,
        legacy_path=tmp_path / "unused.sqlite3",
    )

    controller.retry_task(task.task_id)
    qtbot.waitUntil(
        lambda: controller.get_task(task.task_id).summary.phase == "completed",
        timeout=5000,
    )

    assert controller.catalog.get_config(task.task_id).storage_port == 7777
    assert controller.shutdown()


def test_pdi_export_runs_in_parallel_with_next_download(qtbot, tmp_path, monkeypatch):
    from dcmget import pdi, task_controller

    exporter_entered = threading.Event()
    second_download_entered = threading.Event()

    class OverlapRuntime(_Runtime):
        def run_accession(
            self,
            _handle,
            task_id,
            _config,
            accession,
            move_started=None,
            _cancel_event=None,
        ):
            if move_started is not None:
                move_started()
            self.__class__.calls.append((task_id, accession))
            if accession == "B001":
                assert exporter_entered.wait(2)
                second_download_entered.set()
            return AccessionResult(
                accession,
                AccessionStatus.COMPLETED,
                file_count=1,
                received_bytes=128,
                archived_files=[str(tmp_path / f"{accession}.dcm")],
            )

    class BlockingExporter:
        def __init__(self, *_args, **_kwargs):
            pass

        def export(self, _files):
            exporter_entered.set()
            assert second_download_entered.wait(2)
            return PdiExportResult(
                PdiStatus.COMPLETED,
                output_directory=str(tmp_path / "pdi"),
            )

        def request_cancel(self):
            pass

    OverlapRuntime.calls = []
    OverlapRuntime.starts = 0
    OverlapRuntime.stops = 0
    monkeypatch.setattr(task_controller, "SharedDcmtkRuntime", OverlapRuntime)
    monkeypatch.setattr(pdi, "PdiExporter", BlockingExporter)
    controller = TaskExecutionController(
        AppConfig(),
        _tools(tmp_path),
        catalog_path=tmp_path / "tasks.sqlite3",
        legacy_path=tmp_path / "active-task.sqlite3",
    )
    first_config = AppConfig(
        pdi_export_enabled=True,
        pdi_institution_name="测试医院",
        pdi_output_folder=str(tmp_path / "pdi"),
    )
    first = controller.manager.create_task(first_config, ["A001"])
    second = controller.manager.create_task(AppConfig(), ["B001"])

    controller.start()
    qtbot.waitUntil(
        lambda: controller.get_task(first.task_id).summary.phase == "completed"
        and controller.get_task(second.task_id).summary.phase == "completed",
        timeout=5000,
    )

    assert exporter_entered.is_set()
    assert second_download_entered.is_set()
    assert [accession for _task_id, accession in OverlapRuntime.calls] == [
        "A001",
        "B001",
    ]
    assert controller.shutdown()


def test_late_pdi_pending_signal_does_not_requeue_completed_export(
    tmp_path, monkeypatch
):
    from dcmget import task_controller

    monkeypatch.setattr(task_controller, "SharedDcmtkRuntime", _Runtime)
    controller = TaskExecutionController(
        AppConfig(),
        _tools(tmp_path),
        catalog_path=tmp_path / "tasks.sqlite3",
        legacy_path=tmp_path / "active-task.sqlite3",
    )
    task = controller.manager.create_task(
        AppConfig(
            pdi_export_enabled=True,
            pdi_institution_name="测试医院",
        ),
        ["A001"],
    )
    controller.catalog.set_phase(task.task_id, "pdi_pending")
    stale = controller.catalog.get_summary(task.task_id)
    # The independent PDI worker can finish before the queued Qt signal is
    # delivered to this controller slot.
    controller.catalog.set_phase(task.task_id, "completed")
    published = []
    controller.task_updated.connect(published.append)

    controller._on_download_task_updated(stale)

    assert controller.catalog.get_summary(task.task_id).phase == "completed"
    assert published[-1].phase == "completed"
    assert controller.shutdown()


@pytest.mark.parametrize(
    ("status", "completed"),
    [
        (PdiStatus.COMPLETED, True),
        (PdiStatus.PARTIAL, False),
        (PdiStatus.FAILED, False),
        (PdiStatus.CANCELLED, False),
    ],
)
def test_controller_persists_every_pdi_outcome_before_emitting_and_reloads(
    tmp_path, monkeypatch, status, completed
):
    from dcmget import pdi

    expected = PdiExportResult(
        status=status,
        output_directory=str(tmp_path / "pdi"),
        message=f"result: {status.value}",
        warnings=["warning"],
        source_count=4,
        exported_count=3,
        duplicate_count=1,
        indexed_count=2,
        strict_profile=False,
        core_tool_failure=status == PdiStatus.FAILED,
    )

    class ResultExporter:
        def __init__(self, *_args, **_kwargs):
            pass

        def export(self, _files):
            return expected

        def request_cancel(self):
            pass

    monkeypatch.setattr(pdi, "PdiExporter", ResultExporter)
    catalog_path = tmp_path / "tasks.sqlite3"
    controller = TaskExecutionController(
        AppConfig(),
        _tools(tmp_path),
        catalog_path=catalog_path,
        legacy_path=tmp_path / "active-task.sqlite3",
    )
    task = controller.manager.create_task(AppConfig(), ["A001"])
    controller.catalog.record_result(
        task.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.COMPLETED,
            archived_files=[str(tmp_path / "A001.dcm")],
        ),
    )
    emitted = []
    controller.pdi_finished.connect(
        lambda task_id, result: emitted.append(
            (task_id, result, controller.load_pdi_result(task_id))
        )
    )

    assert (
        controller._execute_pdi(task.task_id, controller.get_task(task.task_id))
        is completed
    )
    assert emitted == [(task.task_id, expected, expected)]
    assert controller.shutdown()

    restarted = TaskExecutionController(
        AppConfig(),
        _tools(tmp_path),
        catalog_path=catalog_path,
        legacy_path=tmp_path / "active-task.sqlite3",
    )
    assert restarted.load_pdi_result(task.task_id) == expected
    assert restarted.shutdown()


def test_controller_shutdown_timeout_keeps_foreground_lease(
    qtbot, tmp_path, monkeypatch
):
    from dcmget import task_controller

    entered = threading.Event()
    release = threading.Event()

    class IgnoringCancelRuntime(_Runtime):
        def run_accession(
            self,
            _handle,
            _task_id,
            _config,
            accession,
            move_started=None,
            _cancel_event=None,
        ):
            if move_started is not None:
                move_started()
            entered.set()
            assert release.wait(2)
            return AccessionResult(accession, AccessionStatus.COMPLETED)

    monkeypatch.setattr(
        task_controller,
        "SharedDcmtkRuntime",
        IgnoringCancelRuntime,
    )
    controller = TaskExecutionController(
        AppConfig(),
        _tools(tmp_path),
        catalog_path=tmp_path / "tasks.sqlite3",
        legacy_path=tmp_path / "active-task.sqlite3",
    )
    controller.manager.create_task(AppConfig(), ["A001"])
    controller.start()
    qtbot.waitUntil(entered.is_set, timeout=2000)

    started = time.monotonic()
    assert not controller.shutdown(timeout_ms=50)
    assert time.monotonic() - started < 0.5
    assert controller.catalog.foreground_lease_held

    release.set()
    assert controller.shutdown(timeout_ms=2000)
    assert not controller.catalog.foreground_lease_held


def test_pdi_cancel_during_exporter_registration_is_not_lost(
    tmp_path, monkeypatch
):
    from dcmget import pdi

    constructor_entered = threading.Event()
    release_constructor = threading.Event()
    cancel_received = threading.Event()

    class SlowConstructorExporter:
        def __init__(self, *_args, **_kwargs):
            constructor_entered.set()
            assert release_constructor.wait(2)

        def request_cancel(self):
            cancel_received.set()

        def export(self, _files):
            assert cancel_received.is_set()
            return PdiExportResult(PdiStatus.CANCELLED, message="用户取消")

    monkeypatch.setattr(pdi, "PdiExporter", SlowConstructorExporter)
    controller = TaskExecutionController(
        AppConfig(),
        _tools(tmp_path),
        catalog_path=tmp_path / "tasks.sqlite3",
        legacy_path=tmp_path / "active-task.sqlite3",
    )
    task = controller.manager.create_task(AppConfig(), ["A001"])
    controller.catalog.record_result(
        task.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.COMPLETED,
            archived_files=[str(tmp_path / "A001.dcm")],
        ),
    )
    controller.catalog.set_phase(task.task_id, "completed")
    controller.pdi_queue.enqueue(task.task_id)
    worker = threading.Thread(target=controller.pdi_queue.run_next)
    worker.start()
    assert constructor_entered.wait(2)

    controller.pdi_queue.cancel(task.task_id)
    release_constructor.set()
    worker.join(2)

    assert not worker.is_alive()
    assert cancel_received.is_set()
    assert controller.catalog.get_summary(task.task_id).phase == "pdi_retryable"
    assert controller.load_pdi_result(task.task_id).status == PdiStatus.CANCELLED
    assert controller.shutdown()
