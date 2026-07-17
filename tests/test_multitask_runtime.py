from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from dcmget.config import AppConfig
from dcmget.core import AccessionResult, AccessionStatus, ToolPaths
from dcmget.multitask_runtime import (
    SharedDcmtkRuntime,
    _ActiveMove,
    recover_orphaned_shared_staging,
)
from dcmget.storage_scp import StorageRoute
from dcmget.task_state import TaskStateError


class _StorageReceiver:
    instances = []

    def __init__(self, ae_title, port, **kwargs):
        self.ae_title = ae_title
        self.port = port
        self.returncode = None
        self.stopped = False
        self.routes: dict[str, StorageRoute] = {}
        self.route_directories: list[Path] = []
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    def start(self):
        return self

    def stop(self):
        self.stopped = True
        self.returncode = 0

    def poll(self):
        return self.returncode

    def register_route(self, accession, directory):
        route = StorageRoute(f"route-{len(self.routes)}", accession, Path(directory))
        self.routes[route.token] = route
        self.route_directories.append(Path(directory))
        return route

    def unregister_route(self, route):
        self.routes.pop(route.token, None)


class _Runner:
    instances = []

    def __init__(
        self,
        _config,
        _tools,
        *,
        log_callback=None,
        progress_callback=None,
        process_callback=None,
        log_file_name="dcmget.log",
        **_kwargs,
    ):
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.process_callback = process_callback
        self.log_file_name = log_file_name
        self.closed = False
        self.cancelled = False
        self.staging_directories: list[Path] = []
        self.__class__.instances.append(self)

    def _close_file_logger(self):
        self.closed = True

    def _emit(self, source, message, level):
        self.log_callback(source, message, level)

    def run_accession(self, accession, _staging, _process):
        self.staging_directories.append(Path(_staging))
        live = AccessionResult(
            accession,
            AccessionStatus.DOWNLOADING,
            file_count=2,
            speed_bytes_per_second=1024,
        )
        self.progress_callback(1, 1, live)
        return AccessionResult(
            accession,
            AccessionStatus.COMPLETED,
            file_count=2,
        )

    def request_cancel_current_move(self):
        self.cancelled = True


def _tools(tmp_path: Path) -> ToolPaths:
    return ToolPaths(
        movescu=tmp_path / "movescu",
        storescp=tmp_path / "storescp",
        bin_dir=tmp_path,
        version="3.7.0",
    )


def test_shared_runtime_reuses_one_receiver_and_separates_task_logs(
    tmp_path, monkeypatch
):
    from dcmget import multitask_runtime

    _Runner.instances = []
    _StorageReceiver.instances = []
    monkeypatch.setattr(multitask_runtime, "DownloadRunner", _Runner)
    monkeypatch.setattr(
        multitask_runtime,
        "PynetdicomStorageSCP",
        _StorageReceiver,
    )
    monkeypatch.setattr(multitask_runtime, "ensure_application_state_dir", lambda: tmp_path)
    progress = []
    runtime = SharedDcmtkRuntime(
        AppConfig(),
        _tools(tmp_path),
        progress_callback=lambda task_id, result: progress.append(
            (task_id, result.accession)
        ),
    )
    receiver = runtime.receiver_service()

    first_handle = receiver.ensure_started()
    assert receiver.ensure_started() is first_handle
    result = receiver.run_accession("task-a", AppConfig(), "A001")

    assert result.status == AccessionStatus.COMPLETED
    assert progress == [("task-a", "A001")]
    assert len(_Runner.instances) == 2
    assert any(
        item.log_file_name.startswith("receiver-DCMGET-6666-")
        for item in _Runner.instances
    )
    assert any(
        item.log_file_name == "task-task-a.log" for item in _Runner.instances
    )
    assert _StorageReceiver.instances[0].kwargs["maximum_associations"] == 16
    assert not _StorageReceiver.instances[0].kwargs["allow_single_route_fallback"]

    receiver.shutdown()
    assert all(item.closed for item in _Runner.instances)
    assert _StorageReceiver.instances[0].stopped
    assert not receiver.is_running


def test_serial_runtime_allows_single_route_fallback(tmp_path, monkeypatch):
    from dcmget import multitask_runtime

    _Runner.instances = []
    _StorageReceiver.instances = []
    monkeypatch.setattr(multitask_runtime, "DownloadRunner", _Runner)
    monkeypatch.setattr(
        multitask_runtime,
        "PynetdicomStorageSCP",
        _StorageReceiver,
    )
    monkeypatch.setattr(
        multitask_runtime,
        "ensure_application_state_dir",
        lambda: tmp_path,
    )
    runtime = SharedDcmtkRuntime(
        AppConfig(max_concurrent_moves=1),
        _tools(tmp_path),
    )
    receiver = runtime.receiver_service()

    receiver.ensure_started()
    receiver.run_accession("task-a", AppConfig(max_concurrent_moves=1), "A001")

    assert _StorageReceiver.instances[0].kwargs["allow_single_route_fallback"]
    receiver.shutdown()


def test_shared_runtime_restarts_receiver_after_storescp_exits(tmp_path, monkeypatch):
    from dcmget import multitask_runtime

    _Runner.instances = []
    _StorageReceiver.instances = []
    monkeypatch.setattr(multitask_runtime, "DownloadRunner", _Runner)
    monkeypatch.setattr(
        multitask_runtime,
        "PynetdicomStorageSCP",
        _StorageReceiver,
    )
    monkeypatch.setattr(multitask_runtime, "ensure_application_state_dir", lambda: tmp_path)
    runtime = SharedDcmtkRuntime(AppConfig(), _tools(tmp_path))
    receiver = runtime.receiver_service()

    pool = receiver.ensure_started()
    first_handle = runtime._ensure_receiver(AppConfig())
    first_receiver = first_handle.receiver
    first_receiver.returncode = 7
    assert receiver.ensure_started() is pool
    replacement = runtime._ensure_receiver(AppConfig())

    assert replacement is not first_handle
    assert replacement.receiver is not first_receiver
    assert first_receiver.stopped
    assert len(_StorageReceiver.instances) == 2
    receiver.shutdown()


def test_shared_runtime_moves_unassigned_files_to_quarantine(tmp_path, monkeypatch):
    from dcmget import multitask_runtime

    _Runner.instances = []
    _StorageReceiver.instances = []
    monkeypatch.setattr(multitask_runtime, "DownloadRunner", _Runner)
    monkeypatch.setattr(
        multitask_runtime,
        "PynetdicomStorageSCP",
        _StorageReceiver,
    )
    monkeypatch.setattr(multitask_runtime, "ensure_application_state_dir", lambda: tmp_path)
    logs = []
    runtime = SharedDcmtkRuntime(
        AppConfig(),
        _tools(tmp_path),
        log_callback=lambda *event: logs.append(event),
    )
    receiver = runtime.receiver_service()
    handle = receiver.ensure_started()
    assert handle is not None
    receiver_handle = runtime._ensure_receiver(AppConfig())
    (receiver_handle.staging_directory / "unknown.dcm").write_bytes(b"DICM")

    receiver.shutdown()

    quarantined = list((tmp_path / "quarantine").glob("*/unknown.dcm"))
    assert len(quarantined) == 1
    assert "隔离目录" in logs[-1][2]


def test_startup_quarantines_orphaned_shared_staging(tmp_path, monkeypatch):
    from dcmget import multitask_runtime

    monkeypatch.setattr(multitask_runtime, "ensure_application_state_dir", lambda: tmp_path)
    orphan = tmp_path / "staging" / "shared-crashed"
    empty = tmp_path / "staging" / "shared-empty"
    unrelated = tmp_path / "staging" / "other-task"
    orphan.mkdir(parents=True)
    empty.mkdir()
    unrelated.mkdir()
    (orphan / "received.dcm").write_bytes(b"DICM")
    (unrelated / "keep.dcm").write_bytes(b"DICM")

    messages = recover_orphaned_shared_staging()

    assert len(messages) == 1
    assert (tmp_path / "quarantine" / "shared-crashed" / "received.dcm").is_file()
    assert not empty.exists()
    assert (unrelated / "keep.dcm").is_file()


def test_cancel_accession_does_not_stop_shared_receiver(tmp_path, monkeypatch):
    from dcmget import multitask_runtime

    _Runner.instances = []
    _StorageReceiver.instances = []
    monkeypatch.setattr(multitask_runtime, "DownloadRunner", _Runner)
    monkeypatch.setattr(
        multitask_runtime,
        "PynetdicomStorageSCP",
        _StorageReceiver,
    )
    monkeypatch.setattr(multitask_runtime, "ensure_application_state_dir", lambda: tmp_path)
    runtime = SharedDcmtkRuntime(AppConfig(), _tools(tmp_path))
    receiver = runtime.receiver_service()
    receiver.ensure_started()
    active = _Runner(AppConfig(), _tools(tmp_path))
    receiver_handle = runtime._ensure_receiver(AppConfig())
    runtime._active_runners["task-a"] = _ActiveMove(active, receiver_handle)

    runtime.cancel_accession("task-a")

    assert active.cancelled
    assert receiver.is_running
    runtime._active_runners.pop("task-a")
    receiver.shutdown()


def test_receiver_pool_reuses_same_ae_and_port_across_pacs_configs(
    tmp_path, monkeypatch
):
    from dcmget import multitask_runtime

    _Runner.instances = []
    _StorageReceiver.instances = []
    monkeypatch.setattr(multitask_runtime, "DownloadRunner", _Runner)
    monkeypatch.setattr(
        multitask_runtime,
        "PynetdicomStorageSCP",
        _StorageReceiver,
    )
    monkeypatch.setattr(multitask_runtime, "ensure_application_state_dir", lambda: tmp_path)
    runtime = SharedDcmtkRuntime(AppConfig(), _tools(tmp_path))
    receiver = runtime.receiver_service()
    receiver.ensure_started()
    first = AppConfig(
        pacs_server_ip="10.0.0.1",
        pacs_ae_title="PACS_A",
        calling_ae_title="CALLING_A",
        storage_ae_title="DCMGET",
        storage_port=6666,
    )
    second = AppConfig(
        pacs_server_ip="10.0.0.2",
        pacs_ae_title="PACS_B",
        calling_ae_title="CALLING_B",
        storage_ae_title="DCMGET",
        storage_port=6666,
    )

    receiver.run_accession("task-a", first, "A001")
    receiver.run_accession("task-b", second, "B001")

    assert len(_StorageReceiver.instances) == 1
    assert len(_StorageReceiver.instances[0].route_directories) == 2
    assert len(set(_StorageReceiver.instances[0].route_directories)) == 2
    receiver.shutdown()


def test_receiver_pool_starts_distinct_receivers_staging_and_logs(
    tmp_path, monkeypatch
):
    from dcmget import multitask_runtime

    _Runner.instances = []
    _StorageReceiver.instances = []
    monkeypatch.setattr(multitask_runtime, "DownloadRunner", _Runner)
    monkeypatch.setattr(
        multitask_runtime,
        "PynetdicomStorageSCP",
        _StorageReceiver,
    )
    monkeypatch.setattr(multitask_runtime, "ensure_application_state_dir", lambda: tmp_path)
    runtime = SharedDcmtkRuntime(AppConfig(), _tools(tmp_path))
    receiver = runtime.receiver_service()
    receiver.ensure_started()
    first = AppConfig(storage_ae_title="DCMGET_A", storage_port=6666)
    second = AppConfig(storage_ae_title="DCMGET_B", storage_port=6667)

    receiver.run_accession("task-a", first, "A001")
    receiver.run_accession("task-b", second, "B001")

    assert {(item.ae_title, item.port) for item in _StorageReceiver.instances} == {
        ("DCMGET_A", 6666),
        ("DCMGET_B", 6667),
    }
    receiver_handles = list(runtime._receivers.values())
    staging_roots = {item.staging_directory for item in receiver_handles}
    receiver_logs = {item.log_runner.log_file_name for item in receiver_handles}
    assert len(staging_roots) == 2
    assert all(path.is_dir() for path in staging_roots)
    assert len(receiver_logs) == 2
    assert all(name.startswith("receiver-") for name in receiver_logs)
    assert all(
        route.parent in staging_roots
        for instance in _StorageReceiver.instances
        for route in instance.route_directories
    )

    receiver.shutdown()

    assert all(item.stopped for item in _StorageReceiver.instances)
    assert all(not path.exists() for path in staging_roots)
    assert all(item.log_runner.closed for item in receiver_handles)


def test_receiver_pool_rejects_active_same_port_with_different_ae(
    tmp_path, monkeypatch
):
    from dcmget import multitask_runtime

    _Runner.instances = []
    _StorageReceiver.instances = []
    monkeypatch.setattr(multitask_runtime, "DownloadRunner", _Runner)
    monkeypatch.setattr(
        multitask_runtime,
        "PynetdicomStorageSCP",
        _StorageReceiver,
    )
    monkeypatch.setattr(multitask_runtime, "ensure_application_state_dir", lambda: tmp_path)
    runtime = SharedDcmtkRuntime(AppConfig(), _tools(tmp_path))
    receiver = runtime.receiver_service()
    receiver.ensure_started()
    first_config = AppConfig(storage_ae_title="DCMGET_A", storage_port=6666)
    first_handle = runtime._ensure_receiver(first_config)
    active = _Runner(first_config, _tools(tmp_path))
    runtime._active_runners["task-a"] = _ActiveMove(active, first_handle)

    with pytest.raises(TaskStateError, match="端口 6666.*AE DCMGET_A.*AE DCMGET_B"):
        receiver.run_accession(
            "task-b",
            AppConfig(storage_ae_title="DCMGET_B", storage_port=6666),
            "B001",
        )

    runtime._active_runners.pop("task-a")
    assert len(_StorageReceiver.instances) == 1
    task_b_runner = next(
        item for item in _Runner.instances if item.log_file_name == "task-task-b.log"
    )
    assert task_b_runner.closed
    receiver.shutdown()


def test_receiver_pool_starts_active_same_ae_on_different_ports(
    tmp_path, monkeypatch
):
    from dcmget import multitask_runtime

    _Runner.instances = []
    _StorageReceiver.instances = []
    monkeypatch.setattr(multitask_runtime, "DownloadRunner", _Runner)
    monkeypatch.setattr(
        multitask_runtime,
        "PynetdicomStorageSCP",
        _StorageReceiver,
    )
    monkeypatch.setattr(multitask_runtime, "ensure_application_state_dir", lambda: tmp_path)
    runtime = SharedDcmtkRuntime(AppConfig(), _tools(tmp_path))
    receiver = runtime.receiver_service()
    receiver.ensure_started()
    first_config = AppConfig(storage_ae_title="DCMGET", storage_port=6666)
    first_handle = runtime._ensure_receiver(first_config)
    active = _Runner(first_config, _tools(tmp_path))
    runtime._active_runners["task-a"] = _ActiveMove(active, first_handle)

    result = receiver.run_accession(
        "task-b",
        AppConfig(storage_ae_title="DCMGET", storage_port=6667),
        "B001",
    )

    assert result.status == AccessionStatus.COMPLETED
    assert {(item.ae_title, item.port) for item in _StorageReceiver.instances} == {
        ("DCMGET", 6666),
        ("DCMGET", 6667),
    }
    assert not first_handle.receiver.stopped
    runtime._active_runners.pop("task-a")
    receiver.shutdown()


def test_receiver_pool_rebinds_idle_port_to_different_ae(tmp_path, monkeypatch):
    from dcmget import multitask_runtime

    _Runner.instances = []
    _StorageReceiver.instances = []
    monkeypatch.setattr(multitask_runtime, "DownloadRunner", _Runner)
    monkeypatch.setattr(
        multitask_runtime,
        "PynetdicomStorageSCP",
        _StorageReceiver,
    )
    monkeypatch.setattr(multitask_runtime, "ensure_application_state_dir", lambda: tmp_path)
    runtime = SharedDcmtkRuntime(AppConfig(), _tools(tmp_path))
    receiver = runtime.receiver_service()
    receiver.ensure_started()
    first_config = AppConfig(storage_ae_title="DCMGET_A", storage_port=6666)
    second_config = AppConfig(storage_ae_title="DCMGET_B", storage_port=6666)

    receiver.run_accession("task-a", first_config, "A001")
    first_handle = runtime._receivers[
        (first_config.storage_ae_title, first_config.storage_port)
    ]
    first_staging = first_handle.staging_directory
    receiver.run_accession("task-b", second_config, "B001")

    assert first_handle.receiver.stopped
    assert first_handle.log_runner.closed
    assert not first_staging.exists()
    assert set(runtime._receivers) == {
        (second_config.storage_ae_title, second_config.storage_port)
    }
    assert len(_StorageReceiver.instances) == 2
    receiver.shutdown()


def test_receiver_pool_shutdown_cancels_and_drains_shared_receiver_runners(
    tmp_path, monkeypatch
):
    from dcmget import multitask_runtime

    started = threading.Barrier(3)

    class _BlockingRunner(_Runner):
        def run_accession(self, accession, staging, _process):
            self.staging_directories.append(Path(staging))
            started.wait(timeout=2)
            deadline = time.monotonic() + 5
            while not self.cancelled and time.monotonic() < deadline:
                time.sleep(0.01)
            return AccessionResult(accession, AccessionStatus.CANCELLED)

    _BlockingRunner.instances = []
    _StorageReceiver.instances = []
    monkeypatch.setattr(multitask_runtime, "DownloadRunner", _BlockingRunner)
    monkeypatch.setattr(
        multitask_runtime,
        "PynetdicomStorageSCP",
        _StorageReceiver,
    )
    monkeypatch.setattr(multitask_runtime, "ensure_application_state_dir", lambda: tmp_path)
    runtime = SharedDcmtkRuntime(AppConfig(), _tools(tmp_path))
    receiver = runtime.receiver_service()
    receiver.ensure_started()
    results: list[AccessionResult] = []

    def run(task_id, accession):
        results.append(receiver.run_accession(task_id, AppConfig(), accession))

    workers = [
        threading.Thread(target=run, args=("task-a", "A001")),
        threading.Thread(target=run, args=("task-b", "B001")),
    ]
    for worker in workers:
        worker.start()
    started.wait(timeout=2)

    receiver.shutdown()
    for worker in workers:
        worker.join(2)

    assert all(not worker.is_alive() for worker in workers)
    assert {result.accession for result in results} == {"A001", "B001"}
    assert all(result.status == AccessionStatus.CANCELLED for result in results)
    assert not receiver.is_running
    assert len(_StorageReceiver.instances) == 1
    assert len(_StorageReceiver.instances[0].route_directories) == 2
    assert _StorageReceiver.instances[0].stopped
