from __future__ import annotations

import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import AppConfig
from .core import AccessionResult, DownloadRunner, ToolPaths
from .runtime import ensure_application_state_dir
from .storage_scp import PynetdicomStorageSCP, StorageRoute
from .task_state import TaskStateError
from .task_manager import (
    MoveStarted,
    ReceiverService,
    SHARED_RECEIVER_CONFIG_FIELDS,
    shared_receiver_config,
)


TaskLogCallback = Callable[[str, str, str, str], None]
TaskProgressCallback = Callable[[str, AccessionResult], None]
TaskProcessCallback = Callable[[str, str, int, str, bool], None]


def recover_orphaned_shared_staging(
    state_root: str | Path | None = None,
) -> list[str]:
    """Quarantine files left when the previous process died mid-receive."""

    root = (
        Path(state_root).expanduser()
        if state_root is not None
        else ensure_application_state_dir()
    )
    staging_root = root / "staging"
    if not staging_root.is_dir():
        return []
    messages: list[str] = []
    for staging in sorted(staging_root.glob("shared-*")):
        if not staging.is_dir():
            continue
        destination = _quarantine_or_cleanup_directory(staging, root)
        if destination is not None:
            messages.append(
                f"异常退出遗留的暂存文件已移入隔离目录：{destination}"
            )
    return messages


def _quarantine_or_cleanup_directory(
    staging: Path,
    state_root: Path,
) -> Path | None:
    try:
        has_files = any(path.is_file() for path in staging.rglob("*"))
    except OSError:
        has_files = True
    if not has_files:
        shutil.rmtree(staging, ignore_errors=True)
        return None
    root = state_root / "quarantine"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = root / staging.name
    suffix = 1
    while destination.exists():
        destination = root / f"{staging.name}-{suffix}"
        suffix += 1
    try:
        staging.replace(destination)
    except OSError:
        shutil.move(str(staging), str(destination))
    return destination


@dataclass(slots=True)
class SharedReceiverHandle:
    receiver: PynetdicomStorageSCP
    staging_directory: Path
    log_runner: DownloadRunner

    @property
    def process(self) -> PynetdicomStorageSCP:
        """Compatibility liveness handle used by ``ReceiverService``."""

        return self.receiver

    def poll(self) -> int | None:
        """Expose the in-process SCP liveness to ``ReceiverService``."""

        return self.receiver.poll()


class SharedDcmtkRuntime:
    """Bridge concurrent C-MOVEs to one routed, application-level Storage SCP."""

    def __init__(
        self,
        config: AppConfig,
        tools: ToolPaths,
        *,
        log_callback: TaskLogCallback | None = None,
        progress_callback: TaskProgressCallback | None = None,
        process_callback: TaskProcessCallback | None = None,
    ) -> None:
        self.config = AppConfig.from_dict(config.to_dict())
        self.tools = tools
        self.log_callback = log_callback or (
            lambda _task_id, _source, _message, _level: None
        )
        self.progress_callback = progress_callback or (
            lambda _task_id, _result: None
        )
        self.process_callback = process_callback or (
            lambda _task_id, _kind, _pid, _executable, _active: None
        )
        self._lock = threading.RLock()
        self._active_condition = threading.Condition(self._lock)
        self._handle: SharedReceiverHandle | None = None
        self._active_runners: dict[str, DownloadRunner] = {}

    def receiver_service(self) -> ReceiverService:
        return ReceiverService(
            self.start,
            self.stop,
            self.run_accession,
            max_concurrent_moves=self.config.max_concurrent_moves,
        )

    def validate_download_config(self, config: AppConfig) -> None:
        expected = shared_receiver_config(self.config)
        actual = shared_receiver_config(config)
        if actual == expected:
            return
        conflicts = [
            field
            for field, current, task_value in zip(
                SHARED_RECEIVER_CONFIG_FIELDS,
                expected,
                actual,
            )
            if current != task_value
        ]
        raise TaskStateError(
            "任务共享接收配置与应用当前设置不一致：" + "、".join(conflicts)
        )

    def validate_pdi_config(self, config: AppConfig) -> None:
        if config.dcmtk_bin_dir != self.config.dcmtk_bin_dir:
            raise TaskStateError("任务 DCMTK 路径与应用当前设置不一致")

    def start(self) -> SharedReceiverHandle:
        with self._lock:
            if self._handle is not None:
                return self._handle
        staging = (
            ensure_application_state_dir()
            / "staging"
            / (
                datetime.now().strftime("shared-%Y%m%d-%H%M%S-%f-")
                + uuid.uuid4().hex[:8]
            )
        )
        staging.mkdir(parents=True, exist_ok=False, mode=0o700)
        receiver_logger = DownloadRunner(
            self.config,
            self.tools,
            log_callback=lambda source, message, level: self.log_callback(
                "", source, message, level
            ),
            log_file_name="receiver.log",
        )
        receiver: PynetdicomStorageSCP | None = None
        try:
            receiver = PynetdicomStorageSCP(
                self.config.storage_ae_title,
                self.config.storage_port,
                quarantine_directory=(
                    ensure_application_state_dir() / "quarantine" / "receiver"
                ),
                log_callback=receiver_logger._emit,
                maximum_associations=max(
                    16,
                    self.config.max_concurrent_moves * 4,
                ),
                allow_single_route_fallback=(
                    self.config.max_concurrent_moves == 1
                ),
            )
            receiver.start()
            handle = SharedReceiverHandle(receiver, staging, receiver_logger)
            with self._lock:
                if self._handle is not None:
                    receiver.stop()
                    receiver_logger._close_file_logger()
                    self._quarantine_or_cleanup(staging)
                    return self._handle
                self._handle = handle
            return handle
        except Exception:
            if receiver is not None:
                receiver.stop()
            receiver_logger._close_file_logger()
            self._quarantine_or_cleanup(staging)
            raise

    def stop(self, raw_handle: object | None) -> None:
        handle = raw_handle if isinstance(raw_handle, SharedReceiverHandle) else None
        with self._active_condition:
            while self._active_runners:
                self._active_condition.wait(timeout=0.1)
            current = self._handle
            if current is None:
                return
            if handle is not None and handle is not current:
                return
            self._handle = None
        current.receiver.stop()
        current.log_runner._close_file_logger()
        self._quarantine_or_cleanup(current.staging_directory)

    def run_accession(
        self,
        raw_handle: object | None,
        task_id: str,
        config: AppConfig,
        accession: str,
        move_started: MoveStarted | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AccessionResult:
        if not isinstance(raw_handle, SharedReceiverHandle):
            raise RuntimeError("共享 DICOM 接收器句柄无效")
        self.validate_download_config(config)
        route_directory = raw_handle.staging_directory / (
            f"route-{task_id[:12]}-{uuid.uuid4().hex[:12]}"
        )
        route_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        runner = DownloadRunner(
            config,
            self.tools,
            log_callback=lambda source, message, level: self.log_callback(
                task_id, source, message, level
            ),
            progress_callback=lambda _index, _total, result: (
                self.progress_callback(task_id, result)
            ),
            process_callback=lambda kind, pid, executable, active: (
                self.process_callback(task_id, kind, pid, executable, active)
            ),
            ready_callback=move_started,
            log_file_name=f"task-{task_id}.log",
        )
        route: StorageRoute | None = None
        with self._lock:
            if task_id in self._active_runners:
                runner._close_file_logger()
                self._quarantine_or_cleanup(route_directory)
                raise RuntimeError("同一任务已有 C-MOVE 正在运行")
            self._active_runners[task_id] = runner
            cancel_before_move = (
                cancel_event is not None and cancel_event.is_set()
            )
        try:
            if cancel_before_move:
                runner.request_cancel_current_move()
            route = raw_handle.receiver.register_route(
                accession,
                route_directory,
            )
            if cancel_event is not None and cancel_event.is_set():
                runner.request_cancel_current_move()
            return runner.run_accession(
                accession,
                route_directory,
                raw_handle.receiver,
            )
        finally:
            try:
                if route is not None:
                    raw_handle.receiver.unregister_route(route)
            finally:
                with self._active_condition:
                    self._active_runners.pop(task_id, None)
                    self._active_condition.notify_all()
                runner._close_file_logger()
                self._quarantine_or_cleanup(route_directory)

    def cancel_accession(self, task_id: str) -> None:
        with self._lock:
            runner = self._active_runners.get(task_id)
        if runner is not None:
            runner.request_cancel_current_move()

    def shutdown(self) -> None:
        with self._lock:
            active = list(self._active_runners.values())
            handle = self._handle
        for runner in active:
            runner.request_cancel_current_move()
        if handle is not None:
            self.stop(handle)

    def _quarantine_or_cleanup(self, staging: Path) -> None:
        if not staging.exists():
            return
        destination = _quarantine_or_cleanup_directory(
            staging,
            ensure_application_state_dir(),
        )
        if destination is None:
            return
        self.log_callback(
            "",
            "接收器",
            f"无法归属的暂存文件已移入隔离目录：{destination}",
            "warning",
        )
