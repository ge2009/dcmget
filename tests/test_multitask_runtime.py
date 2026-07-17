from __future__ import annotations

from pathlib import Path

from dcmget.config import AppConfig
from dcmget.core import AccessionResult, AccessionStatus, ToolPaths
from dcmget.multitask_runtime import (
    SharedDcmtkRuntime,
    recover_orphaned_shared_staging,
)
from dcmget.storage_scp import StorageRoute


class _StorageReceiver:
    instances = []

    def __init__(self, _ae_title, _port, **kwargs):
        self.returncode = None
        self.stopped = False
        self.routes: dict[str, StorageRoute] = {}
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
        self.__class__.instances.append(self)

    def _close_file_logger(self):
        self.closed = True

    def _emit(self, source, message, level):
        self.log_callback(source, message, level)

    def run_accession(self, accession, _staging, _process):
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
    assert _Runner.instances[0].log_file_name == "receiver.log"
    assert _Runner.instances[1].log_file_name == "task-task-a.log"
    assert _StorageReceiver.instances[0].kwargs["maximum_associations"] == 16
    assert not _StorageReceiver.instances[0].kwargs["allow_single_route_fallback"]

    receiver.shutdown()
    assert _Runner.instances[0].closed
    assert _Runner.instances[1].closed
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

    first = receiver.ensure_started()
    first.receiver.returncode = 7
    replacement = receiver.ensure_started()

    assert replacement is not first
    assert first.receiver.stopped
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
    (handle.staging_directory / "unknown.dcm").write_bytes(b"DICM")

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
    runtime._active_runners["task-a"] = active

    runtime.cancel_accession("task-a")

    assert active.cancelled
    assert receiver.is_running
    runtime._active_runners.pop("task-a")
    receiver.shutdown()
