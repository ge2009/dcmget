"""Qt-free application service for the local DcmGet web application.

The service owns the long-running download/PDI threads.  Browser connections are
only event subscribers, so closing a page never stops an active operation.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import AppConfig
from .core import (
    AccessionResult,
    AccessionStatus,
    BatchSummary,
    DownloadRunner,
    ToolPaths,
    log_directory,
)
from .diagnostics import record_exception
from .licensing import (
    LicenseError,
    consume_trial,
    load_license,
    trial_status,
    trial_task_consumed,
)
from .runtime import resource_root
from .task_ledger import TaskLedger, TaskLedgerError
from .task_state import (
    TaskCheckpoint,
    TaskCheckpointStore,
    TaskStateError,
    merge_checkpoint_summary,
)


EventCallback = Callable[[dict[str, object]], None]
RunnerFactory = Callable[..., object]
ExporterFactory = Callable[..., object]
VerifierFactory = Callable[..., object]

_RUNNER_STATE_MESSAGES = {
    "starting_receiver": "正在启动 DICOM 接收器",
    "downloading": "正在接收影像",
    "pause_pending": "当前检查号完成后暂停",
    "paused": "下载已暂停，可以随时继续",
    "stopping": "正在停止当前任务",
    "ending": "正在结束当前任务",
    "cancelled": "任务已取消",
    "ended": "任务已结束",
    "download_retryable": "部分检查号未完成，可以重试失败项",
    "interrupted": "下载已安全中断，可以继续",
    "completed": "下载已完成",
}


class AppServiceError(RuntimeError):
    """A safe, user-facing application service error."""


@dataclass(frozen=True, slots=True)
class AppEvent:
    id: int
    type: str
    timestamp: str
    payload: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        payload = _json_safe(self.payload)
        assert isinstance(payload, dict)
        return {
            "id": self.id,
            "type": self.type,
            "timestamp": self.timestamp,
            "payload": payload,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if is_dataclass(value):
        return {
            field.name: _json_safe(getattr(value, field.name))
            for field in fields(value)
        }
    return str(value)


def _result_payload(result: AccessionResult) -> dict[str, object]:
    payload = _json_safe(result)
    assert isinstance(payload, dict)
    payload["archived_file_count"] = len(result.archived_files)
    # The browser only needs progress metadata. Keeping every local path in the
    # SSE replay buffer would make large studies consume unbounded memory.
    payload.pop("archived_files", None)
    return payload


class DcmGetAppService:
    """One-profile, one-task lifecycle independent from any UI connection."""

    def __init__(
        self,
        *,
        task_store: TaskCheckpointStore,
        task_ledger: TaskLedger | None = None,
        task_ledger_path: str | Path | None = None,
        project_root: str | Path | None = None,
        profile_name: str = "",
        fallback_log_directory: str | Path | None = None,
        detail_limit: int = 200,
        event_limit: int = 5000,
        runner_factory: RunnerFactory = DownloadRunner,
        pdi_exporter_factory: ExporterFactory | None = None,
        pdi_verifier_factory: VerifierFactory | None = None,
        load_license_fn: Callable[..., object] = load_license,
        trial_status_fn: Callable[..., object] = trial_status,
        consume_trial_fn: Callable[..., object] = consume_trial,
        trial_task_consumed_fn: Callable[..., bool] = trial_task_consumed,
    ) -> None:
        if detail_limit < 0:
            raise ValueError("detail_limit 不能为负数")
        if event_limit < 100:
            raise ValueError("event_limit 不能小于 100")
        self.task_store = task_store
        self.task_ledger = task_ledger or TaskLedger(
            task_ledger_path
            or task_store.path.expanduser().with_name("task-ledger.sqlite3")
        )
        self.project_root = Path(project_root or resource_root()).expanduser()
        self.profile_name = str(profile_name)
        self.fallback_log_directory = (
            Path(fallback_log_directory).expanduser()
            if fallback_log_directory is not None
            else task_store.path.parent / "logs"
        )
        self.detail_limit = detail_limit
        self._runner_factory = runner_factory
        self._pdi_exporter_factory = pdi_exporter_factory
        self._pdi_verifier_factory = pdi_verifier_factory
        self._load_license = load_license_fn
        self._trial_status = trial_status_fn
        self._consume_trial = consume_trial_fn
        self._trial_task_consumed = trial_task_consumed_fn

        self._lock = threading.RLock()
        self._events: deque[AppEvent] = deque(maxlen=event_limit)
        self._event_id = 0
        self._subscribers: dict[int, EventCallback] = {}
        self._next_subscriber_id = 0
        self._error_logs: deque[dict[str, object]] = deque(maxlen=200)
        self._thread: threading.Thread | None = None
        self._worker: object | None = None
        self._operation = ""
        self._status = "idle"
        self._status_message = ""
        self._task_id = ""
        self._config: AppConfig | None = None
        self._accessions: list[str] = []
        self._results: dict[str, AccessionResult] = {}
        self._partial_results: dict[str, AccessionResult] = {}
        self._current_accession = ""
        self._current_file_count = 0
        self._current_speed = 0.0
        self._last_summary: BatchSummary | None = None
        self._last_pdi_result: object | None = None
        self._verification_result: object | None = None
        self._verification_cancel = threading.Event()
        self._last_tools: ToolPaths | None = None
        self._trial_required = False
        self._trial_consumed = False
        self._cancel_requested = False
        self._end_requested = False
        self._pause_requested = False
        self._shutting_down = False
        self._restore_checkpoint()

    # ------------------------------------------------------------------ events
    def subscribe(self, callback: EventCallback) -> Callable[[], None]:
        if not callable(callback):
            raise TypeError("事件订阅者必须可调用")
        with self._lock:
            self._next_subscriber_id += 1
            subscriber_id = self._next_subscriber_id
            self._subscribers[subscriber_id] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(subscriber_id, None)

        return unsubscribe

    def events_since(
        self, after_id: int = 0, *, limit: int = 1000
    ) -> list[dict[str, object]]:
        bounded = max(1, min(int(limit), 5000))
        with self._lock:
            selected = [event for event in self._events if event.id > int(after_id)]
            return [event.to_dict() for event in selected[:bounded]]

    def _emit_event(self, event_type: str, **payload: object) -> dict[str, object]:
        safe = _json_safe(payload)
        assert isinstance(safe, dict)
        with self._lock:
            self._event_id += 1
            event = AppEvent(self._event_id, str(event_type), _utc_now(), safe)
            self._events.append(event)
            callbacks = tuple(self._subscribers.values())
            serialized = event.to_dict()
        for callback in callbacks:
            try:
                callback(serialized)
            except Exception:
                # A disconnected browser or faulty observer must never affect a task.
                continue
        return serialized

    def _log(self, source: str, message: str, level: str = "info") -> None:
        normalized = str(level or "info").lower()
        payload = {
            "source": str(source),
            "message": str(message),
            "level": normalized,
        }
        if normalized == "error":
            with self._lock:
                self._error_logs.append({"timestamp": _utc_now(), **payload})
        self._emit_event("log", **payload)

    # --------------------------------------------------------------- snapshots
    def snapshot(self) -> dict[str, object]:
        with self._lock:
            total = len(self._accessions)
            results = list(self._results.values())
            partials = list(self._partial_results.values())
            final_results = [*results, *partials]
            processed = len(results)
            large_batch = total > self.detail_limit
            status_counts = {
                status.value: sum(item.status == status for item in final_results)
                for status in AccessionStatus
            }
            failed_count = sum(
                item.status in {AccessionStatus.FAILED, AccessionStatus.PARTIAL}
                for item in results
            )
            file_count = sum(item.file_count for item in final_results)
            received_bytes = sum(item.received_bytes for item in final_results)
            actions = self._action_snapshot_locked()
            task = {
                "id": self._task_id,
                "profile": self.profile_name,
                "total": total,
                "large_batch": large_batch,
                "accessions": None if large_batch else list(self._accessions),
                "destination": (
                    self._config.dicom_destination_folder if self._config else ""
                ),
            }
            result_payloads = None
            if not large_batch:
                ordered = {
                    item.accession: item for item in [*results, *partials]
                }
                result_payloads = [
                    _result_payload(ordered[accession])
                    for accession in self._accessions
                    if accession in ordered
                ]
            authorization = self._authorization_snapshot_locked()
            pdi = (
                _json_safe(self._last_pdi_result)
                if self._last_pdi_result is not None
                else None
            )
            verification = (
                _json_safe(self._verification_result)
                if self._verification_result is not None
                else None
            )
            return {
                "event_id": self._event_id,
                "status": self._status,
                "message": self._status_message,
                "operation": self._operation,
                "task": task,
                "progress": {
                    "processed": processed,
                    "total": total,
                    "pending": max(0, total - processed),
                    "failed": failed_count,
                    "file_count": file_count,
                    "received_bytes": received_bytes,
                    "current_accession": self._current_accession,
                    "current_file_count": self._current_file_count,
                    "speed_bytes_per_second": self._current_speed,
                    "status_counts": status_counts,
                },
                "results": result_payloads,
                "pdi": pdi,
                "verification": verification,
                "actions": actions,
                "authorization": authorization,
                "error_logs": list(self._error_logs),
            }

    def _action_snapshot_locked(self) -> dict[str, bool]:
        busy = bool(self._thread and self._thread.is_alive())
        has_checkpoint = self.task_store.path.is_file()
        pdi_retryable = self._status == "pdi_retryable"
        download_retryable = self._status in {
            "download_retryable",
            "interrupted",
            "cancelled",
        }
        can_accept = False
        if download_retryable and not busy:
            try:
                checkpoint = self.task_store.load(include_archived_files=False)
                can_accept = bool(
                    checkpoint
                    and checkpoint.phase == "download_retryable"
                    and not checkpoint.interrupted_reason
                    and not checkpoint.pending_accessions
                )
            except TaskStateError:
                pass
        return {
            "can_start": not busy
            and not self._shutting_down
            and not self.task_store.path.is_file(),
            "can_pause": busy
            and self._operation == "download"
            and self._status in {"starting_receiver", "downloading"},
            "can_resume": (
                busy
                and self._operation == "download"
                and self._status in {"pause_pending", "paused"}
            )
            or (not busy and download_retryable),
            "can_cancel": busy
            and not self._end_requested
            and not self._shutting_down,
            "can_end": has_checkpoint
            and not self._end_requested
            and not self._shutting_down
            and self._status not in {"locked", "recovery_error", "stopped"},
            "can_retry_failed": not busy and download_retryable,
            "can_accept_partial": can_accept,
            "can_retry_pdi": not busy and pdi_retryable,
            "can_verify_pdi": not busy and not self._shutting_down,
            "can_shutdown": not self._shutting_down and self._status != "stopped",
        }

    def _authorization_snapshot_locked(self) -> dict[str, object]:
        try:
            info = self._load_license()
        except (OSError, LicenseError):
            try:
                trial = self._trial_status()
                return {
                    "registered": False,
                    "trial_used": int(getattr(trial, "used", 0)),
                    "trial_remaining": int(getattr(trial, "remaining", 0)),
                }
            except Exception as exc:
                return {"registered": False, "error": str(exc)}
        return {
            "registered": True,
            "customer": str(getattr(info, "customer", "")),
            "expires_on": str(getattr(info, "expires_on", "") or ""),
        }

    # --------------------------------------------------------------- lifecycle
    def start_task(
        self,
        config: AppConfig,
        tools: ToolPaths,
        accessions: Iterable[str],
    ) -> dict[str, object]:
        values = [str(value).strip() for value in accessions]
        if not values or any(not value for value in values):
            raise AppServiceError("至少需要一个有效检查号")
        if len(values) != len(set(values)):
            raise AppServiceError("检查号不能重复")
        with self._lock:
            self._ensure_available_locked()
            if self.task_store.path.is_file():
                raise AppServiceError("存在未完成任务，请先继续或放弃恢复任务")
        trial_required = self._authorize_new_task()
        task_config = AppConfig.from_dict(config.to_dict())
        if not self.task_store.try_acquire_lease():
            raise AppServiceError("该 Profile 正在另一个 DcmGet 进程中运行")
        try:
            checkpoint = self.task_store.start(
                task_config, values, trial_required=trial_required
            )
            self._ensure_ledger_batch(checkpoint)
        except Exception:
            try:
                if "checkpoint" in locals():
                    self.task_store.clear(checkpoint.task_id)
            finally:
                self.task_store.release_lease()
            raise
        self._load_checkpoint_state(checkpoint)
        self._last_tools = tools
        self._trial_required = trial_required
        self._trial_consumed = False
        self._launch_download(checkpoint, tools, values, trial_required)
        return self.snapshot()

    def resume_task(self, tools: ToolPaths | None = None) -> dict[str, object]:
        with self._lock:
            self._ensure_available_locked()
        selected_tools = tools or self._last_tools
        if selected_tools is None:
            raise AppServiceError("继续任务需要可用的 DCMTK 工具")
        if not self.task_store.try_acquire_lease():
            raise AppServiceError("恢复任务正在另一个 DcmGet 进程中运行")
        try:
            checkpoint = self.task_store.load_required(include_archived_files=False)
            self._ensure_ledger_batch(checkpoint)
            self._authorize_resume(checkpoint)
            if checkpoint.phase in {"pdi_pending", "pdi_running", "pdi_retryable"}:
                self._load_checkpoint_state(checkpoint)
                self._last_tools = selected_tools
                self._launch_pdi(checkpoint, selected_tools)
                return self.snapshot()
            if checkpoint.phase == "download_retryable":
                checkpoint = self.task_store.prepare_download_retry(
                    checkpoint.task_id, include_archived_files=False
                )
            pending = checkpoint.pending_accessions
            if not pending:
                raise AppServiceError("恢复任务没有未处理或失败项")
            self._load_checkpoint_state(checkpoint)
            self._last_tools = selected_tools
            consume_on_ready = bool(
                checkpoint.trial_required
                and not self._trial_task_consumed(checkpoint.task_id)
            )
            self._launch_download(
                checkpoint, selected_tools, pending, consume_on_ready
            )
            return self.snapshot()
        except Exception:
            if not self._thread or not self._thread.is_alive():
                self.task_store.release_lease()
            raise

    def retry_failed(self, tools: ToolPaths | None = None) -> dict[str, object]:
        checkpoint = self.task_store.load(include_archived_files=False)
        if checkpoint is None or checkpoint.phase != "download_retryable":
            raise AppServiceError("当前任务没有可重试的失败项")
        return self.resume_task(tools)

    def pause(self) -> dict[str, object]:
        with self._lock:
            worker = self._worker
            if self._operation != "download" or not (
                self._thread and self._thread.is_alive()
            ):
                raise AppServiceError("当前没有可暂停的下载任务")
            self._pause_requested = True
            callback = getattr(worker, "request_pause", None)
            self._status = "pause_pending"
            self._status_message = "当前检查号完成后暂停"
        if callable(callback):
            callback()
        self._emit_event("state", status="pause_pending")
        return self.snapshot()

    def resume(self) -> dict[str, object]:
        with self._lock:
            worker = self._worker
            if self._operation != "download" or not (
                self._thread and self._thread.is_alive()
            ):
                raise AppServiceError("当前没有已暂停的下载任务")
            self._pause_requested = False
            callback = getattr(worker, "request_resume", None)
        if callable(callback):
            callback()
        return self.snapshot()

    def cancel(self) -> dict[str, object]:
        with self._lock:
            worker = self._worker
            if not (self._thread and self._thread.is_alive()):
                raise AppServiceError("当前没有运行中的任务")
            callback = getattr(worker, "request_cancel", None) or getattr(
                worker, "cancel", None
            )
            self._cancel_requested = True
            self._status = "stopping"
            self._status_message = "正在停止后台任务"
            if self._operation == "verification":
                self._verification_cancel.set()
        if callable(callback):
            callback()
        self._emit_event("state", status="stopping")
        return self.snapshot()

    def end_task(self) -> dict[str, object]:
        """Permanently end the current task while preserving received files.

        Unlike :meth:`cancel`, this operation removes the recovery checkpoint.
        An active worker is cancelled first; the checkpoint is only removed
        after the worker has returned and can no longer write progress.
        """

        with self._lock:
            if self._shutting_down or self._status == "stopped":
                raise AppServiceError("DcmGet 服务正在停止或已经停止")
            if self._end_requested:
                raise AppServiceError("当前任务正在结束")
            busy = bool(self._thread and self._thread.is_alive())
            if busy and self._operation not in {"download", "pdi"}:
                raise AppServiceError("当前后台操作不属于可结束的任务")
            worker = self._worker
            task_id = self._task_id

        if not task_id or not self.task_store.path.is_file():
            raise AppServiceError("当前没有可结束的任务")

        if busy:
            with self._lock:
                self._end_requested = True
                self._cancel_requested = True
                self._status = "ending"
                self._status_message = "正在停止后台进程并结束任务"
                callback = getattr(worker, "request_cancel", None) or getattr(
                    worker, "cancel", None
                )
            if callable(callback):
                callback()
            self._emit_event("state", status="ending")
            return self.snapshot()

        if not self.task_store.try_acquire_lease():
            raise AppServiceError("当前任务正在另一个 DcmGet 进程中运行")
        try:
            checkpoint = self.task_store.load_required(
                include_archived_files=False
            )
            self._ensure_ledger_batch(checkpoint)
            self._load_checkpoint_state(checkpoint)
            with self._lock:
                self._end_requested = True
                self._status = "ending"
                self._status_message = "正在清理恢复点并结束任务"
            self._emit_event("state", status="ending")
            self._finalize_ended_task(checkpoint)
            return self.snapshot()
        except Exception:
            with self._lock:
                self._end_requested = False
            self.task_store.release_lease()
            raise

    def accept_partial(
        self, tools: ToolPaths | None = None
    ) -> dict[str, object]:
        with self._lock:
            self._ensure_available_locked()
        if not self.task_store.try_acquire_lease():
            raise AppServiceError("恢复任务正在另一个 DcmGet 进程中运行")
        try:
            checkpoint = self.task_store.load_required(include_archived_files=False)
            if checkpoint.phase != "download_retryable":
                raise AppServiceError("当前任务不能接受部分结果")
            if checkpoint.interrupted_reason or checkpoint.pending_accessions:
                raise AppServiceError("任务仍有待处理项，不能接受当前结果")
            source_files = (
                self.task_store.load_archived_files(checkpoint.task_id)
                if checkpoint.config.pdi_export_enabled
                else []
            )
            if source_files:
                selected_tools = tools or self._last_tools
                if selected_tools is None:
                    raise AppServiceError("生成 PDI 需要可用的 DCMTK 工具")
                self.task_store.set_phase(checkpoint.task_id, "pdi_pending")
                checkpoint.phase = "pdi_pending"
                self._load_checkpoint_state(checkpoint)
                self._last_tools = selected_tools
                self._launch_pdi(checkpoint, selected_tools, source_files)
                return self.snapshot()
            self.task_store.clear(checkpoint.task_id)
            self._complete_ledger(checkpoint.task_id, "accepted_partial")
            self.task_store.release_lease()
            self._set_status("completed", "已接受当前下载结果")
            return self.snapshot()
        except Exception:
            if not self._thread or not self._thread.is_alive():
                self.task_store.release_lease()
            raise

    def retry_pdi(self, tools: ToolPaths | None = None) -> dict[str, object]:
        checkpoint = self.task_store.load(include_archived_files=False)
        if checkpoint is None or checkpoint.phase not in {
            "pdi_pending",
            "pdi_running",
            "pdi_retryable",
        }:
            raise AppServiceError("当前没有可重试的 PDI 任务")
        return self.resume_task(tools)

    def verify_pdi(self, root: str | Path) -> dict[str, object]:
        with self._lock:
            self._ensure_available_locked()
            selected = Path(root).expanduser()
            self._operation = "verification"
            self._status = "verifying"
            self._status_message = "正在校验 PDI 目录"
            self._verification_result = None
            self._verification_cancel.clear()
            thread = threading.Thread(
                target=self._verification_main,
                args=(selected,),
                name="dcmget-pdi-verification",
                daemon=False,
            )
            self._thread = thread
            thread.start()
        self._emit_event("state", status="verifying", root=str(selected))
        return self.snapshot()

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                thread = self._thread
            if thread is None:
                return True
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(remaining)
            if thread.is_alive():
                return False
            with self._lock:
                if self._thread is thread:
                    return True
            if deadline is not None and time.monotonic() >= deadline:
                return False

    def shutdown(self, *, timeout: float = 15.0) -> bool:
        with self._lock:
            if self._status == "stopped":
                return True
            self._shutting_down = True
            self._cancel_requested = True
            self._verification_cancel.set()
            worker = self._worker
            thread = self._thread
            self._status = "shutting_down"
            self._status_message = "正在停止后台进程"
        self._emit_event("state", status="shutting_down")
        if worker is not None:
            callback = getattr(worker, "request_cancel", None) or getattr(
                worker, "cancel", None
            )
            if callable(callback):
                callback()
        stopped = self.wait(timeout) if thread is not None else True
        if self._task_id and self.task_store.path.is_file():
            try:
                for message in self.task_store.cleanup_recorded_processes(
                    self._task_id
                ):
                    self._log("恢复", message, "warning")
            except TaskStateError as exc:
                self._log("恢复", str(exc), "error")
                stopped = False
        if not stopped:
            # Process cleanup can unblock a runner that was waiting on DCMTK.
            stopped = self.wait(min(2.0, max(0.0, timeout)))
        if stopped:
            self.task_store.release_lease()
        with self._lock:
            if stopped:
                self._thread = None
                self._worker = None
                self._operation = ""
                self._status = "stopped"
                self._status_message = "服务已停止"
            else:
                self._status = "shutdown_failed"
                self._status_message = "后台进程未能在限定时间内停止"
            self._shutting_down = False
        self._emit_event("state", status=self._status)
        return stopped

    # --------------------------------------------------------- download worker
    def _launch_download(
        self,
        checkpoint: TaskCheckpoint,
        tools: ToolPaths,
        accessions: list[str],
        consume_trial_on_ready: bool,
    ) -> None:
        with self._lock:
            self._operation = "download"
            self._cancel_requested = False
            self._pause_requested = False
            self._status = "starting_receiver"
            self._status_message = "正在启动 DICOM 接收器"
            thread = threading.Thread(
                target=self._download_main,
                args=(checkpoint, tools, list(accessions), consume_trial_on_ready),
                name=f"dcmget-download-{checkpoint.task_id[:8]}",
                daemon=False,
            )
            self._thread = thread
            thread.start()
        self._emit_event(
            "task_started",
            task_id=checkpoint.task_id,
            total=len(checkpoint.accessions),
            pending=len(accessions),
        )

    def _download_main(
        self,
        checkpoint: TaskCheckpoint,
        tools: ToolPaths,
        accessions: list[str],
        consume_trial_on_ready: bool,
    ) -> None:
        current_thread = threading.current_thread()
        ready_lock = threading.Lock()
        ready_consumed = False

        def on_ready() -> None:
            nonlocal ready_consumed
            with ready_lock:
                if ready_consumed or not consume_trial_on_ready:
                    return
                trial = self._consume_trial(task_id=checkpoint.task_id)
                ready_consumed = True
            with self._lock:
                self._trial_consumed = True
            self._log(
                "授权",
                f"本次使用免费试用，剩余 {getattr(trial, 'remaining', 0)} 次",
                "info",
            )

        try:
            runner = self._runner_factory(
                checkpoint.config,
                tools,
                log_callback=self._log,
                state_callback=self._on_runner_state,
                progress_callback=lambda index, total, result: self._on_progress(
                    checkpoint.task_id, index, total, result
                ),
                ready_callback=on_ready,
                process_callback=lambda kind, pid, executable, active: (
                    self.task_store.record_process(
                        checkpoint.task_id,
                        kind,
                        pid,
                        executable,
                        active=active,
                    )
                ),
                audit_callback=lambda result, observations: self._record_audit(
                    checkpoint, result, observations
                ),
                log_file_name=f"task-{checkpoint.task_id}.log",
                log_directory=log_directory(checkpoint.config),
                fallback_log_directory=self.fallback_log_directory,
            )
            with self._lock:
                self._worker = runner
                cancel_requested = self._cancel_requested
                pause_requested = self._pause_requested
            if cancel_requested:
                runner.request_cancel()
            elif pause_requested:
                runner.request_pause()
            summary = runner.run(accessions)
            self._finish_download(checkpoint, tools, summary)
        except Exception as exc:
            record_exception("DcmGetAppService.download", exc)
            self._handle_download_failure(checkpoint, exc)
        finally:
            with self._lock:
                if self._worker is not None and self._operation == "download":
                    self._worker = None
                if self._thread is current_thread:
                    self._thread = None
                    self._operation = ""

    def _on_runner_state(self, state: str) -> None:
        with self._lock:
            if self._end_requested:
                return
        normalized = {
            "partial": "download_retryable",
            "safety_paused": "interrupted",
        }.get(str(state), str(state))
        self._set_status(
            normalized,
            _RUNNER_STATE_MESSAGES.get(normalized, str(state)),
        )

    def _on_progress(
        self,
        task_id: str,
        index: int,
        total: int,
        result: AccessionResult,
    ) -> None:
        final = result.status not in {
            AccessionStatus.WAITING,
            AccessionStatus.DOWNLOADING,
        }
        stored = result
        if final:
            stored = self.task_store.record_result(task_id, result)
        with self._lock:
            self._current_accession = stored.accession
            self._current_file_count = stored.file_count
            self._current_speed = stored.speed_bytes_per_second
            if final:
                if stored.status == AccessionStatus.CANCELLED:
                    self._partial_results[stored.accession] = stored
                else:
                    self._results[stored.accession] = stored
                    self._partial_results.pop(stored.accession, None)
        self._emit_event(
            "progress",
            task_id=task_id,
            index=index,
            total=total,
            final=final,
            result=_result_payload(stored),
        )

    def _record_audit(
        self,
        checkpoint: TaskCheckpoint,
        result: AccessionResult,
        observations: tuple[object, ...],
    ) -> None:
        if not checkpoint.config.anonymization_enabled:
            anonymization_status = "not_requested"
        elif result.archived_files:
            anonymization_status = "completed"
        elif result.status == AccessionStatus.NO_DATA:
            anonymization_status = "no_data"
        else:
            anonymization_status = "failed"
        self.task_ledger.record_runner_result(
            checkpoint.task_id,
            result,
            observed_instances=observations,
            anonymization_status=anonymization_status,
        )

    def _finish_download(
        self,
        checkpoint: TaskCheckpoint,
        tools: ToolPaths,
        current: BatchSummary,
    ) -> None:
        stored = self.task_store.load(include_archived_files=False)
        if stored is None or stored.task_id != checkpoint.task_id:
            raise TaskStateError("下载结束时任务恢复点不存在或已经改变")
        merged = merge_checkpoint_summary(stored, current)
        with self._lock:
            self._last_summary = merged
            self._results = stored.result_by_accession
            self._partial_results = dict(stored.partial_results)
        if self._is_end_requested(checkpoint.task_id):
            self._finalize_ended_task(checkpoint)
            return
        if merged.cancelled:
            self.task_store.release_lease()
            self._set_status("cancelled", "任务已取消，恢复点已保留")
            self._export_acceptance_report(checkpoint.task_id, checkpoint.config)
            return
        if merged.exit_code == 2:
            self.task_store.set_phase(
                checkpoint.task_id,
                "download_retryable",
                interrupted_reason=merged.interrupted_reason,
            )
            self.task_store.release_lease()
            status = "interrupted" if merged.interrupted_reason else "download_retryable"
            self._set_status(
                status,
                merged.interrupted_reason or "存在失败或部分成功的检查号",
            )
            self._export_acceptance_report(checkpoint.task_id, checkpoint.config)
            return
        archived_files = self.task_store.load_archived_files(checkpoint.task_id)
        if checkpoint.config.pdi_export_enabled and archived_files:
            self.task_store.set_phase(checkpoint.task_id, "pdi_pending")
            stored.phase = "pdi_pending"
            self._launch_pdi(stored, tools, archived_files)
            return
        self.task_store.clear(checkpoint.task_id)
        self._complete_ledger(checkpoint.task_id, "completed")
        self.task_store.release_lease()
        self._set_status("completed", "所有检查号均已处理完成")

    def _handle_download_failure(
        self, checkpoint: TaskCheckpoint, exc: BaseException
    ) -> None:
        message = str(exc).strip() or exc.__class__.__name__
        if self._is_end_requested(checkpoint.task_id):
            self._log("应用", f"结束任务时后台退出：{message}", "warning")
            self._finalize_ended_task(checkpoint)
            return
        try:
            self.task_store.set_phase(
                checkpoint.task_id,
                "download_retryable",
                interrupted_reason=message,
            )
        except TaskStateError as state_exc:
            self._log("恢复", str(state_exc), "error")
        self.task_store.release_lease()
        self._log("应用", message, "error")
        self._set_status("interrupted", message)

    # -------------------------------------------------------------- PDI worker
    def _launch_pdi(
        self,
        checkpoint: TaskCheckpoint,
        tools: ToolPaths,
        files: list[str] | None = None,
    ) -> None:
        source_files = list(files or self.task_store.load_archived_files(checkpoint.task_id))
        if not source_files:
            raise AppServiceError("当前任务没有可导出的 DICOM 文件")
        attempt_id, reuse_existing = self.task_store.begin_pdi_attempt(
            checkpoint.task_id,
            reuse_existing=checkpoint.phase == "pdi_running",
        )
        with self._lock:
            self._operation = "pdi"
            self._cancel_requested = False
            self._status = "pdi_running"
            self._status_message = "正在生成 PDI 便携目录"
            thread = threading.Thread(
                target=self._pdi_main,
                args=(checkpoint, tools, source_files, attempt_id, reuse_existing),
                name=f"dcmget-pdi-{checkpoint.task_id[:8]}",
                daemon=False,
            )
            self._thread = thread
            thread.start()
        self._emit_event(
            "state", status="pdi_running", source_count=len(source_files)
        )

    def _make_exporter(
        self,
        checkpoint: TaskCheckpoint,
        tools: ToolPaths,
        attempt_id: str,
        reuse_existing: bool,
    ) -> object:
        if self._pdi_exporter_factory is not None:
            exporter_type = self._pdi_exporter_factory
        else:
            from .pdi import PdiExporter, PdiVolumeExporter

            exporter_type = (
                PdiVolumeExporter
                if checkpoint.config.pdi_volume_size_bytes > 0
                else PdiExporter
            )
        return exporter_type(
            checkpoint.config,
            tools,
            project_root=self.project_root,
            log_callback=self._log,
            progress_callback=lambda stage, current, total, message: (
                self._emit_event(
                    "pdi_progress",
                    stage=getattr(stage, "value", stage),
                    current=current,
                    total=total,
                    message=message,
                )
            ),
            process_callback=lambda kind, pid, executable, active: (
                self.task_store.record_process(
                    checkpoint.task_id,
                    kind,
                    pid,
                    executable,
                    active=active,
                )
            ),
            recovery_id=attempt_id,
            reuse_published=reuse_existing,
        )

    def _pdi_main(
        self,
        checkpoint: TaskCheckpoint,
        tools: ToolPaths,
        files: list[str],
        attempt_id: str,
        reuse_existing: bool,
    ) -> None:
        current_thread = threading.current_thread()
        try:
            exporter = self._make_exporter(
                checkpoint, tools, attempt_id, reuse_existing
            )
            with self._lock:
                self._worker = exporter
                cancel_requested = self._cancel_requested
            if cancel_requested:
                exporter.request_cancel()
            result = exporter.export(files)
            self._finish_pdi(checkpoint, result)
        except Exception as exc:
            record_exception("DcmGetAppService.pdi", exc)
            message = str(exc).strip() or exc.__class__.__name__
            if self._is_end_requested(checkpoint.task_id):
                self._log("PDI", f"结束任务时后台退出：{message}", "warning")
                self._finalize_ended_task(checkpoint)
                return
            try:
                self.task_store.set_phase(checkpoint.task_id, "pdi_retryable")
            except TaskStateError as state_exc:
                self._log("恢复", str(state_exc), "error")
            self.task_store.release_lease()
            self._log("PDI", message, "error")
            self._set_status("pdi_retryable", message)
        finally:
            with self._lock:
                if self._operation == "pdi":
                    self._worker = None
                if self._thread is current_thread:
                    self._thread = None
                    self._operation = ""

    def _finish_pdi(self, checkpoint: TaskCheckpoint, result: object) -> None:
        from .pdi import PdiStatus

        status = getattr(result, "status", PdiStatus.FAILED)
        with self._lock:
            self._last_pdi_result = result
        self.task_ledger.record_pdi_result(
            checkpoint.task_id,
            status,
            output_directory=str(getattr(result, "output_directory", "") or ""),
            message=str(getattr(result, "message", "") or ""),
        )
        if self._is_end_requested(checkpoint.task_id):
            self._finalize_ended_task(checkpoint)
            self._emit_event("pdi_finished", result=result)
            return
        if status == PdiStatus.COMPLETED:
            self.task_store.clear(checkpoint.task_id)
            self._complete_ledger(checkpoint.task_id, "completed")
            self.task_store.release_lease()
            self._set_status("completed", "下载与 PDI 导出已完成")
        else:
            self.task_store.set_phase(checkpoint.task_id, "pdi_retryable")
            self.task_ledger.complete_batch(
                checkpoint.task_id,
                {
                    PdiStatus.PARTIAL: "partial",
                    PdiStatus.FAILED: "failed",
                    PdiStatus.CANCELLED: "cancelled",
                }.get(status, "failed"),
            )
            self._export_acceptance_report(checkpoint.task_id, checkpoint.config)
            self.task_store.release_lease()
            self._set_status(
                "pdi_retryable",
                str(getattr(result, "message", "") or "PDI 导出未完成"),
            )
        self._emit_event("pdi_finished", result=result)

    # --------------------------------------------------------- PDI verification
    def _verification_main(self, root: Path) -> None:
        current_thread = threading.current_thread()
        completed: list[dict[str, object]] = []
        try:
            from .pdi_verify import (
                PdiVerifier,
                discover_pdi_verification_roots,
                pdi_delivery_report_output_directory,
                write_pdi_delivery_reports,
            )

            roots = discover_pdi_verification_roots(root)
            cancelled = False
            for index, volume_root in enumerate(roots, 1):
                if self._verification_cancel.is_set():
                    cancelled = True
                    break
                verifier_type = self._pdi_verifier_factory or PdiVerifier
                verifier = verifier_type(
                    volume_root,
                    progress_callback=lambda progress, _index=index: (
                        self._emit_event(
                            "verification_progress",
                            volume=_index,
                            volume_count=len(roots),
                            progress=progress,
                        )
                    ),
                    cancel_event=self._verification_cancel,
                )
                with self._lock:
                    self._worker = verifier
                result = verifier.verify()
                result_status = str(
                    getattr(getattr(result, "status", ""), "value", "")
                )
                if self._verification_cancel.is_set() or result_status == "cancelled":
                    cancelled = True
                report_directory = pdi_delivery_report_output_directory(
                    root, volume_root, len(roots)
                )
                reports = write_pdi_delivery_reports(result, report_directory)
                completed.append(
                    {
                        "result": _json_safe(result),
                        "reports": _json_safe(reports),
                    }
                )
                if cancelled:
                    break
            with self._lock:
                self._verification_result = {
                    "cancelled": cancelled,
                    "items": completed,
                }
            self._set_status(
                "verification_cancelled" if cancelled else "verification_completed",
                "PDI 校验已取消" if cancelled else "PDI 校验已完成",
            )
            self._emit_event(
                "verification_finished", cancelled=cancelled, items=completed
            )
        except Exception as exc:
            record_exception("DcmGetAppService.verify_pdi", exc)
            message = str(exc).strip() or exc.__class__.__name__
            self._log("PDI 校验", message, "error")
            self._set_status("verification_failed", message)
        finally:
            with self._lock:
                self._worker = None
                if self._thread is current_thread:
                    self._thread = None
                    self._operation = ""

    # --------------------------------------------------------------- internals
    def _restore_checkpoint(self) -> None:
        if not self.task_store.path.is_file():
            return
        if not self.task_store.try_acquire_lease():
            self._status = "locked"
            self._status_message = "该 Profile 正由另一个 DcmGet 进程使用"
            return
        try:
            checkpoint = self.task_store.load(include_archived_files=False)
            if checkpoint is None:
                return
            for message in self.task_store.cleanup_recorded_processes(
                checkpoint.task_id
            ):
                self._log("恢复", message, "warning")
            self._ensure_ledger_batch(checkpoint)
            if checkpoint.phase == "downloading" and not checkpoint.pending_accessions:
                failed = any(
                    result.status in {AccessionStatus.FAILED, AccessionStatus.PARTIAL}
                    for result in checkpoint.results
                )
                if failed:
                    self.task_store.set_phase(
                        checkpoint.task_id, "download_retryable"
                    )
                    checkpoint.phase = "download_retryable"
                elif checkpoint.config.pdi_export_enabled and any(
                    result.file_count for result in checkpoint.results
                ):
                    self.task_store.set_phase(checkpoint.task_id, "pdi_pending")
                    checkpoint.phase = "pdi_pending"
                else:
                    self.task_store.clear(checkpoint.task_id)
                    return
            self._load_checkpoint_state(checkpoint)
            self._status = {
                "downloading": "interrupted",
                "download_retryable": "download_retryable",
                "pdi_pending": "pdi_retryable",
                "pdi_running": "pdi_retryable",
                "pdi_retryable": "pdi_retryable",
            }[checkpoint.phase]
            self._status_message = "发现可继续的未完成任务"
        except (TaskStateError, TaskLedgerError) as exc:
            self._status = "recovery_error"
            self._status_message = str(exc)
            self._log("恢复", str(exc), "error")
        finally:
            self.task_store.release_lease()

    def _load_checkpoint_state(self, checkpoint: TaskCheckpoint) -> None:
        with self._lock:
            self._task_id = checkpoint.task_id
            self._config = AppConfig.from_dict(checkpoint.config.to_dict())
            self._accessions = list(checkpoint.accessions)
            self._results = checkpoint.result_by_accession
            self._partial_results = dict(checkpoint.partial_results)
            self._trial_required = checkpoint.trial_required
            self._current_accession = ""
            self._current_file_count = 0
            self._current_speed = 0.0

    def _authorize_new_task(self) -> bool:
        try:
            self._load_license()
            return False
        except (OSError, LicenseError):
            trial = self._trial_status()
            if int(getattr(trial, "remaining", 0)) <= 0:
                raise AppServiceError("30 次免费试用已用完，请输入注册码")
            return True

    def _authorize_resume(self, checkpoint: TaskCheckpoint) -> None:
        try:
            self._load_license()
            return
        except (OSError, LicenseError):
            if checkpoint.trial_required and self._trial_task_consumed(
                checkpoint.task_id
            ):
                return
            trial = self._trial_status()
            if int(getattr(trial, "remaining", 0)) <= 0:
                raise AppServiceError("30 次免费试用已用完，请输入注册码")

    def _ensure_ledger_batch(self, checkpoint: TaskCheckpoint) -> None:
        try:
            self.task_ledger.load_batch(checkpoint.task_id)
            return
        except TaskLedgerError as exc:
            if "不存在当前批次" not in str(exc):
                raise
        self.task_ledger.create_batch(
            checkpoint.accessions,
            batch_id=checkpoint.task_id,
            profile_name=self.profile_name,
            anonymization_requested=checkpoint.config.anonymization_enabled,
            pdi_requested=checkpoint.config.pdi_export_enabled,
        )

    def _is_end_requested(self, task_id: str) -> bool:
        with self._lock:
            return self._end_requested and self._task_id == task_id

    def _finalize_ended_task(self, checkpoint: TaskCheckpoint) -> None:
        """Clean runtime state and irreversibly remove one task checkpoint."""

        try:
            cleanup_messages = self.task_store.cleanup_recorded_processes(
                checkpoint.task_id
            )
            for message in cleanup_messages:
                self._log("结束任务", message, "warning")
            if cleanup_messages:
                unresolved = self.task_store.cleanup_recorded_processes(
                    checkpoint.task_id
                )
                if unresolved:
                    raise TaskStateError(
                        "后台进程未能完全停止：" + "；".join(unresolved)
                    )
            self.task_ledger.complete_batch(checkpoint.task_id, "ended")
            if self._config is not None:
                self._export_acceptance_report(
                    checkpoint.task_id, self._config
                )
            self.task_store.clear(checkpoint.task_id)
        except (TaskStateError, TaskLedgerError, OSError) as exc:
            self.task_store.release_lease()
            with self._lock:
                self._end_requested = False
            self._log("结束任务", str(exc), "error")
            self._set_status("end_failed", f"任务结束失败：{exc}")
            return
        self.task_store.release_lease()
        with self._lock:
            self._end_requested = False
            self._cancel_requested = False
        self._set_status("ended", "任务已结束；已下载文件和日志已保留")
        self._emit_event("task_ended", task_id=checkpoint.task_id)

    def _complete_ledger(self, task_id: str, status: object) -> None:
        try:
            self.task_ledger.complete_batch(task_id, status)
        except TaskLedgerError as exc:
            self._log("验收", f"任务台账更新失败：{exc}", "error")
        config = self._config
        if config is not None:
            self._export_acceptance_report(task_id, config)

    def _export_acceptance_report(
        self, task_id: str, config: AppConfig
    ) -> None:
        if not task_id:
            return
        destination = Path(config.dicom_destination_folder).expanduser()
        report_dir = destination / "_DcmGetReports" / f"task-{task_id[:8]}"
        try:
            paths = self.task_ledger.export_reports(task_id, report_dir)
        except (OSError, TaskLedgerError) as exc:
            self._log("验收", f"验收报告生成失败：{exc}", "error")
            return
        self._emit_event("acceptance_report", paths=paths)

    def _ensure_available_locked(self) -> None:
        if self._shutting_down or self._status == "stopped":
            raise AppServiceError("DcmGet 服务正在停止或已经停止")
        if self._thread is not None and self._thread.is_alive():
            raise AppServiceError("当前已有后台任务正在运行")

    def _set_status(self, status: str, message: str = "") -> None:
        with self._lock:
            self._status = str(status)
            self._status_message = str(message)
        self._emit_event("state", status=status, message=message)


__all__ = ["AppEvent", "AppServiceError", "DcmGetAppService"]
