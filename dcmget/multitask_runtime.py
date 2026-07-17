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
    receiver_key,
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
    candidates = {
        *staging_root.glob("shared-*"),
        *staging_root.glob("receiver-*"),
    }
    for staging in sorted(candidates):
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
    key: tuple[str, int]

    def poll(self) -> int | None:
        """Expose the in-process SCP liveness to the receiver pool."""

        return self.receiver.poll()


@dataclass(slots=True)
class ReceiverPoolHandle:
    """Application-level handle that lazily owns one SCP per AE/port pair."""

    token: str
    closed: bool = False

    def poll(self) -> int | None:
        return 0 if self.closed else None


@dataclass(slots=True)
class _ActiveMove:
    runner: DownloadRunner
    receiver_handle: SharedReceiverHandle


class SharedDcmtkRuntime:
    """Bridge concurrent C-MOVEs to a routed pool of Storage SCP listeners."""

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
        self._pool_handle: ReceiverPoolHandle | None = None
        self._receivers: dict[tuple[str, int], SharedReceiverHandle] = {}
        self._port_owners: dict[int, tuple[str, int]] = {}
        self._active_runners: dict[str, _ActiveMove] = {}
        self._stopping = False

    def receiver_service(self) -> ReceiverService:
        return ReceiverService(
            self.start,
            self.stop,
            self.run_accession,
            max_concurrent_moves=self.config.max_concurrent_moves,
        )

    def validate_download_config(self, config: AppConfig) -> None:
        """Validate only the toolchain that is genuinely process-global.

        PACS, calling AE and receiving AE/port are task snapshots.  A task with
        a different receiving AE/port is served by another SCP in this pool.
        """

        if config.dcmtk_bin_dir != self.config.dcmtk_bin_dir:
            raise TaskStateError("任务 DCMTK 路径与应用当前设置不一致")

    def validate_pdi_config(self, config: AppConfig) -> None:
        if config.dcmtk_bin_dir != self.config.dcmtk_bin_dir:
            raise TaskStateError("任务 DCMTK 路径与应用当前设置不一致")

    def start(self) -> ReceiverPoolHandle:
        with self._lock:
            if self._pool_handle is not None and not self._pool_handle.closed:
                return self._pool_handle
            self._stopping = False
            handle = ReceiverPoolHandle(uuid.uuid4().hex)
            self._pool_handle = handle
            return handle

    def _ensure_receiver(self, config: AppConfig) -> SharedReceiverHandle:
        key = receiver_key(config)
        with self._active_condition:
            pool = self._pool_handle
            if pool is None or pool.closed or self._stopping:
                raise RuntimeError("DICOM 接收器池未运行")
            existing = self._receivers.get(key)
            if existing is not None and existing.poll() is None:
                return existing
            if existing is not None:
                if any(
                    active.receiver_handle is existing
                    for active in self._active_runners.values()
                ):
                    raise RuntimeError(
                        f"DICOM 接收器 {key[0]}:{key[1]} 已意外退出"
                    )
                self._remove_receiver_locked(existing)

            port_owner = self._port_owners.get(key[1])
            conflicts: list[SharedReceiverHandle] = []
            if port_owner is not None and port_owner != key:
                conflict = self._receivers.get(port_owner)
                if conflict is not None:
                    conflicts.append(conflict)
            active_conflicts = [
                conflict
                for conflict in conflicts
                if self._receiver_is_active_locked(conflict)
            ]
            if port_owner is not None and port_owner != key and any(
                conflict.key == port_owner for conflict in active_conflicts
            ):
                raise TaskStateError(
                    f"接收端口 {key[1]} 正由 AE {port_owner[0]} 使用，"
                    f"不能同时绑定 AE {key[0]}；请为任务设置不同接收端口"
                )
            for conflict in conflicts:
                self._remove_receiver_locked(conflict)
            handle = self._start_receiver_locked(config, key)
            self._receivers[key] = handle
            self._port_owners[key[1]] = key
            return handle

    def _start_receiver_locked(
        self,
        config: AppConfig,
        key: tuple[str, int],
    ) -> SharedReceiverHandle:
        session = uuid.uuid4().hex[:8]
        safe_ae = "".join(
            character if character.isalnum() else "-" for character in key[0]
        ).strip("-") or "AE"
        staging = (
            ensure_application_state_dir()
            / "staging"
            / (
                datetime.now().strftime("receiver-%Y%m%d-%H%M%S-%f-")
                + f"{safe_ae}-{key[1]}-{session}"
            )
        )
        staging.mkdir(parents=True, exist_ok=False, mode=0o700)
        receiver_logger = DownloadRunner(
            config,
            self.tools,
            log_callback=lambda source, message, level: self.log_callback(
                "", source, message, level
            ),
            log_file_name=f"receiver-{safe_ae}-{key[1]}-{session}.log",
        )
        receiver: PynetdicomStorageSCP | None = None
        try:
            receiver = PynetdicomStorageSCP(
                key[0],
                key[1],
                quarantine_directory=(
                    ensure_application_state_dir()
                    / "quarantine"
                    / f"receiver-{safe_ae}-{key[1]}-{session}"
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
            try:
                receiver.start()
            except OSError as exc:
                raise RuntimeError(
                    f"接收器 AE {key[0]} 无法监听端口 {key[1]}：{exc}"
                ) from exc
            return SharedReceiverHandle(
                receiver,
                staging,
                receiver_logger,
                key,
            )
        except Exception:
            if receiver is not None:
                receiver.stop()
            receiver_logger._close_file_logger()
            self._quarantine_or_cleanup(staging)
            raise

    def stop(self, raw_handle: object | None) -> None:
        handle = raw_handle if isinstance(raw_handle, ReceiverPoolHandle) else None
        with self._active_condition:
            current = self._pool_handle
            if current is None:
                return
            if handle is not None and handle is not current:
                return
            self._stopping = True
            for active in self._active_runners.values():
                active.runner.request_cancel_current_move()
            while self._active_runners:
                self._active_condition.wait(timeout=0.1)
            receivers = list(self._receivers.values())
            self._receivers.clear()
            self._port_owners.clear()
            self._pool_handle = None
            current.closed = True
        for receiver_handle in receivers:
            self._stop_receiver(receiver_handle)

    def run_accession(
        self,
        raw_handle: object | None,
        task_id: str,
        config: AppConfig,
        accession: str,
        move_started: MoveStarted | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AccessionResult:
        if not isinstance(raw_handle, ReceiverPoolHandle):
            raise RuntimeError("DICOM 接收器池句柄无效")
        self.validate_download_config(config)
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
        route_directory: Path | None = None
        receiver_handle: SharedReceiverHandle | None = None
        try:
            with self._active_condition:
                if raw_handle is not self._pool_handle or raw_handle.closed:
                    raise RuntimeError("DICOM 接收器池句柄已经失效")
                receiver_handle = self._ensure_receiver(config)
                route_directory = receiver_handle.staging_directory / (
                    f"route-{task_id[:12]}-{uuid.uuid4().hex[:12]}"
                )
                route_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
                if task_id in self._active_runners:
                    raise RuntimeError("同一任务已有 C-MOVE 正在运行")
                self._active_runners[task_id] = _ActiveMove(
                    runner,
                    receiver_handle,
                )
                cancel_before_move = (
                    cancel_event is not None and cancel_event.is_set()
                )
        except Exception:
            runner._close_file_logger()
            if route_directory is not None:
                self._quarantine_or_cleanup(route_directory)
            raise
        try:
            if cancel_before_move:
                runner.request_cancel_current_move()
            route = receiver_handle.receiver.register_route(
                accession,
                route_directory,
            )
            if cancel_event is not None and cancel_event.is_set():
                runner.request_cancel_current_move()
            return runner.run_accession(
                accession,
                route_directory,
                receiver_handle.receiver,
            )
        finally:
            try:
                if route is not None:
                    receiver_handle.receiver.unregister_route(route)
            finally:
                try:
                    runner._close_file_logger()
                    if route_directory is not None:
                        self._quarantine_or_cleanup(route_directory)
                finally:
                    with self._active_condition:
                        self._active_runners.pop(task_id, None)
                        self._active_condition.notify_all()

    def cancel_accession(self, task_id: str) -> None:
        with self._lock:
            active = self._active_runners.get(task_id)
        if active is not None:
            active.runner.request_cancel_current_move()

    def shutdown(self) -> None:
        with self._lock:
            active = [item.runner for item in self._active_runners.values()]
            handle = self._pool_handle
        for runner in active:
            runner.request_cancel_current_move()
        if handle is not None:
            self.stop(handle)

    def _remove_receiver_locked(self, handle: SharedReceiverHandle) -> None:
        self._receivers.pop(handle.key, None)
        if self._port_owners.get(handle.key[1]) == handle.key:
            self._port_owners.pop(handle.key[1], None)
        self._stop_receiver(handle)

    def _receiver_is_active_locked(self, handle: SharedReceiverHandle) -> bool:
        return any(
            active.receiver_handle is handle
            for active in self._active_runners.values()
        )

    def _stop_receiver(self, handle: SharedReceiverHandle) -> None:
        handle.receiver.stop()
        handle.log_runner._close_file_logger()
        self._quarantine_or_cleanup(handle.staging_directory)

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
            "error",
        )
