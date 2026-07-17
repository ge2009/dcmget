from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from .config import AppConfig
from .core import AccessionResult, ToolPaths
from .multitask_runtime import (
    SharedDcmtkRuntime,
    recover_orphaned_shared_staging,
)
from .pdi import PdiExportResult
from .task_manager import (
    PdiQueue,
    ReceiverService,
    TaskCatalog,
    TaskManager,
    TaskRecord,
    TaskSummary,
)
from .task_state import TaskStateError


BeforeTaskMove = Callable[[str], None]


class _SchedulerWorker(QObject):
    task_updated = pyqtSignal(object)
    tasks_updated = pyqtSignal(object)
    scheduler_error = pyqtSignal(str, str)
    idle = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self, manager: TaskManager) -> None:
        super().__init__()
        self.manager = manager
        self._stop = threading.Event()
        self._wake = threading.Condition()

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    summary = self.manager.run_next_round()
                except Exception as exc:
                    task_id = self.manager.last_error_task_id
                    self.scheduler_error.emit(
                        task_id,
                        str(exc).strip() or exc.__class__.__name__,
                    )
                    self.tasks_updated.emit(self.manager.list_tasks())
                    continue
                if summary is not None:
                    self.task_updated.emit(summary)
                    self.tasks_updated.emit(self.manager.list_tasks())
                    continue
                if self.manager.has_inflight:
                    with self._wake:
                        if not self._stop.is_set():
                            self._wake.wait(timeout=0.05)
                    continue
                self.idle.emit()
                with self._wake:
                    if not self._stop.is_set():
                        self._wake.wait(timeout=0.5)
        finally:
            self.finished.emit()

    def wake(self) -> None:
        with self._wake:
            self._wake.notify_all()

    def stop(self) -> None:
        self._stop.set()
        self.wake()


class _PdiSchedulerWorker(QObject):
    task_updated = pyqtSignal(object)
    tasks_updated = pyqtSignal(object)
    scheduler_error = pyqtSignal(str, str)
    idle = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self, queue: PdiQueue, catalog: TaskCatalog) -> None:
        super().__init__()
        self.queue = queue
        self.catalog = catalog
        self._stop = threading.Event()
        self._wake = threading.Condition()

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    summary = self.queue.run_next()
                except Exception as exc:
                    self.scheduler_error.emit(
                        self.queue.last_error_task_id,
                        str(exc).strip() or exc.__class__.__name__,
                    )
                    self.tasks_updated.emit(self.catalog.list_tasks())
                    continue
                if summary is not None:
                    self.task_updated.emit(summary)
                    self.tasks_updated.emit(self.catalog.list_tasks())
                    continue
                self.idle.emit()
                with self._wake:
                    if not self._stop.is_set():
                        self._wake.wait(timeout=0.5)
        finally:
            self.finished.emit()

    def wake(self) -> None:
        with self._wake:
            self._wake.notify_all()

    def stop(self) -> None:
        self._stop.set()
        self.queue.shutdown()
        self.wake()


class TaskExecutionController(QObject):
    """Qt-facing controller for the persistent multi-task scheduler."""

    task_updated = pyqtSignal(object)
    tasks_updated = pyqtSignal(object)
    progress = pyqtSignal(str, object)
    log = pyqtSignal(str, str, str, str)
    process_changed = pyqtSignal(str, str, int, str, bool)
    scheduler_error = pyqtSignal(str, str)
    idle = pyqtSignal()
    pdi_progress = pyqtSignal(str, object, int, int, str)
    pdi_finished = pyqtSignal(str, object)

    def __init__(
        self,
        config: AppConfig,
        tools: ToolPaths,
        *,
        catalog_path: str | Path | None = None,
        legacy_path: str | Path | None = None,
        before_task_move: BeforeTaskMove | None = None,
        project_root: str | Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.catalog = TaskCatalog(catalog_path, legacy_path=legacy_path)
        if not self.catalog.try_acquire_foreground_lease():
            raise RuntimeError("另一个 DcmGet 前台任务调度器正在运行")
        try:
            self.catalog.validate_shared_config(config)
            self.catalog.validate_receiver_mappings()
            for summary in self.catalog.list_tasks():
                if summary.phase not in {"pdi_pending", "pdi_running"}:
                    continue
                task_config = self.catalog.get_config(summary.task_id)
                if task_config.dcmtk_bin_dir != config.dcmtk_bin_dir:
                    raise RuntimeError(
                        "待恢复 PDI 任务的 DCMTK 路径与应用当前设置不一致"
                    )
            self.startup_messages = self.catalog.cleanup_receiver_sessions()
            for summary in self.catalog.list_tasks():
                self.startup_messages.extend(
                    self.catalog.cleanup_recorded_processes(summary.task_id)
                )
            unresolved = self.catalog.unresolved_process_records()
            if unresolved:
                raise RuntimeError(
                    "上次运行的后台进程仍无法安全结束："
                    + "、".join(unresolved)
                    + "。请结束这些进程后重新启动 DcmGet。"
                )
            self.startup_messages.extend(
                recover_orphaned_shared_staging(self.catalog.path.parent)
            )
        except Exception:
            self.catalog.release_foreground_lease()
            raise
        self.before_task_move = before_task_move
        self.project_root = Path(project_root or Path.cwd())
        self._active_exporters: dict[str, object] = {}
        self._pdi_starting: set[str] = set()
        self._pending_pdi_cancellations: set[str] = set()
        self._pdi_lock = threading.RLock()
        self._receiver_session_id = ""
        self.runtime = SharedDcmtkRuntime(
            config,
            tools,
            log_callback=lambda task_id, source, message, level: self.log.emit(
                task_id, source, message, level
            ),
            progress_callback=self._record_live_progress,
            process_callback=self._record_process,
        )
        receiver = ReceiverService(
            self.runtime.start,
            self.runtime.stop,
            self.runtime.run_accession,
            max_concurrent_moves=config.max_concurrent_moves,
        )
        self.manager = TaskManager(
            self.catalog,
            receiver=receiver,
            cancel_accession=self.runtime.cancel_accession,
            before_first_execution=before_task_move,
            max_concurrent_moves=config.max_concurrent_moves,
            task_started=self._on_download_task_started,
        )
        self.pdi_queue = PdiQueue(
            self.catalog,
            self._execute_pdi,
            cancel=self._cancel_pdi,
            starting=self._mark_pdi_starting,
        )
        self._thread = QThread(self)
        self._worker = _SchedulerWorker(self.manager)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.task_updated.connect(self._on_download_task_updated)
        self._worker.tasks_updated.connect(self.tasks_updated)
        self._worker.scheduler_error.connect(self.scheduler_error)
        self._worker.idle.connect(self.idle)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._pdi_thread = QThread(self)
        self._pdi_worker = _PdiSchedulerWorker(self.pdi_queue, self.catalog)
        self._pdi_worker.moveToThread(self._pdi_thread)
        self._pdi_thread.started.connect(self._pdi_worker.run)
        self._pdi_worker.task_updated.connect(self.task_updated)
        self._pdi_worker.tasks_updated.connect(self.tasks_updated)
        self._pdi_worker.scheduler_error.connect(self.scheduler_error)
        self._pdi_worker.finished.connect(self._pdi_thread.quit)
        self._pdi_thread.finished.connect(self._pdi_worker.deleteLater)
        self._started = False
        self._workers_stop_requested = False

    def start(self) -> None:
        if self._started:
            self._worker.wake()
            return
        self._started = True
        self._thread.start()
        self._pdi_thread.start()
        self.tasks_updated.emit(self.manager.list_tasks())

    def create_task(
        self,
        config: AppConfig,
        accessions: Iterable[str],
        *,
        trial_required: bool = False,
        name: str = "",
    ) -> TaskSummary:
        self.runtime.validate_download_config(config)
        summary = self.manager.create_task(
            config,
            accessions,
            trial_required=trial_required,
            name=name,
        )
        self.task_updated.emit(summary)
        self.tasks_updated.emit(self.manager.list_tasks())
        self.start()
        self._worker.wake()
        return summary

    def list_tasks(self) -> list[TaskSummary]:
        return self.manager.list_tasks()

    def get_task(self, task_id: str) -> TaskRecord:
        return self.manager.get_task(task_id)

    def load_pdi_result(self, task_id: str) -> PdiExportResult | None:
        """Reload the latest persisted PDI result for a task."""

        return self.catalog.load_pdi_result(task_id)

    def pause_task(self, task_id: str) -> TaskSummary:
        summary = self.manager.pause_task(task_id)
        self._publish(summary)
        return summary

    def resume_task(self, task_id: str) -> TaskSummary:
        summary = self.manager.resume_task(task_id)
        self._publish(summary)
        self.start()
        self._worker.wake()
        return summary

    def cancel_task(self, task_id: str) -> TaskSummary:
        summary = self.manager.cancel_task(task_id)
        self._publish(summary)
        self._worker.wake()
        return summary

    def delete_task(self, task_id: str) -> None:
        """Remove an inactive task record while preserving all output files."""

        with self._pdi_lock:
            if task_id in self._active_exporters or task_id in self._pdi_starting:
                raise TaskStateError("任务仍有 PDI 后台活动，不能删除")
        self.manager.delete_task(task_id)
        with self._pdi_lock:
            self._pending_pdi_cancellations.discard(task_id)
        self.tasks_updated.emit(self.manager.list_tasks())

    def retry_task(self, task_id: str) -> TaskSummary:
        self.runtime.validate_download_config(self.catalog.get_config(task_id))
        summary = self.manager.retry_task(task_id)
        self._publish(summary)
        self.start()
        self._worker.wake()
        return summary

    def retry_pdi(self, task_id: str) -> TaskSummary:
        self.runtime.validate_pdi_config(self.catalog.get_config(task_id))
        summary = self.pdi_queue.retry(task_id)
        self._publish(summary)
        self.start()
        self._pdi_worker.wake()
        return summary

    def cancel_pdi(self, task_id: str) -> TaskSummary:
        summary = self.pdi_queue.cancel(task_id)
        self._publish(summary)
        return summary

    def shutdown(self, timeout_ms: int = 15_000) -> bool:
        deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000

        def remaining_seconds() -> float:
            return max(0.0, deadline - time.monotonic())

        downloads_stopped = self.manager.shutdown(remaining_seconds())
        if not downloads_stopped:
            return False
        if not self._started:
            self.pdi_queue.shutdown()
            self.catalog.release_foreground_lease()
            return True
        if not self._workers_stop_requested:
            self._worker.stop()
            self._pdi_worker.stop()
            self._workers_stop_requested = True
        self._thread.quit()
        self._pdi_thread.quit()
        stopped = self._thread.wait(int(remaining_seconds() * 1000))
        pdi_stopped = self._pdi_thread.wait(int(remaining_seconds() * 1000))
        all_stopped = bool(downloads_stopped and stopped and pdi_stopped)
        if all_stopped:
            self._started = False
            self.catalog.release_foreground_lease()
        return all_stopped

    def _on_download_task_updated(self, summary: TaskSummary) -> None:
        try:
            current = self.catalog.get_summary(summary.task_id)
        except TaskStateError:
            # A queued signal may outlive a task record deleted on the UI thread.
            return
        self.task_updated.emit(current)
        if current.phase == "pdi_pending":
            # PdiQueue discovers persisted pending jobs itself.  Do not enqueue
            # from this potentially stale Qt signal: the PDI worker may already
            # have completed and moved the task back to ``completed``.
            self._pdi_worker.wake()

    def _on_download_task_started(self, summary: TaskSummary) -> None:
        """Publish occupied slots before the scheduler waits for completion."""

        self.task_updated.emit(summary)
        self.tasks_updated.emit(self.manager.list_tasks())

    def _execute_pdi(self, task_id: str, record: TaskRecord) -> bool:
        from .pdi import PdiExporter, PdiStatus

        self.runtime.validate_pdi_config(record.config)
        files = [
            path
            for result in [*record.results, *record.partial_results.values()]
            for path in result.archived_files
        ]
        if not files:
            with self._pdi_lock:
                self._pdi_starting.discard(task_id)
                self._pending_pdi_cancellations.discard(task_id)
            return True
        try:
            recovery_id, reuse_published = self.catalog.begin_pdi_attempt(
                task_id,
                reuse_existing=True,
            )
            exporter = PdiExporter(
                record.config,
                self.runtime.tools,
                project_root=self.project_root,
                log_callback=lambda source, message, level: self.log.emit(
                    task_id, source, message, level
                ),
                progress_callback=(
                    lambda stage, current, total, message: self.pdi_progress.emit(
                        task_id, stage, current, total, message
                    )
                ),
                process_callback=(
                    lambda kind, pid, executable, active: self._record_process(
                        task_id, kind, pid, executable, active
                    )
                ),
                recovery_id=recovery_id,
                reuse_published=reuse_published,
            )
        except Exception:
            with self._pdi_lock:
                self._pdi_starting.discard(task_id)
                self._pending_pdi_cancellations.discard(task_id)
            raise
        with self._pdi_lock:
            self._active_exporters[task_id] = exporter
            self._pdi_starting.discard(task_id)
            cancel_pending = task_id in self._pending_pdi_cancellations
            self._pending_pdi_cancellations.discard(task_id)
        if cancel_pending:
            exporter.request_cancel()
        try:
            result = exporter.export(files)
        finally:
            with self._pdi_lock:
                self._active_exporters.pop(task_id, None)
                self._pdi_starting.discard(task_id)
                self._pending_pdi_cancellations.discard(task_id)
        self.catalog.save_pdi_result(task_id, result)
        self.pdi_finished.emit(task_id, result)
        return result.status == PdiStatus.COMPLETED

    def _cancel_pdi(self, task_id: str) -> None:
        with self._pdi_lock:
            exporter = self._active_exporters.get(task_id)
            if exporter is None and task_id in self._pdi_starting:
                self._pending_pdi_cancellations.add(task_id)
        if exporter is not None:
            exporter.request_cancel()

    def _mark_pdi_starting(self, task_id: str) -> None:
        with self._pdi_lock:
            self._pdi_starting.add(task_id)

    def _record_process(
        self,
        task_id: str,
        kind: str,
        pid: int,
        executable: str,
        active: bool,
    ) -> None:
        try:
            if task_id:
                self.catalog.record_process(
                    task_id,
                    kind,
                    pid,
                    executable,
                    active=active,
                )
            elif kind == "storescp":
                if active:
                    self._receiver_session_id = self.catalog.begin_receiver_session(
                        pid,
                        executable,
                    )
                elif self._receiver_session_id:
                    self.catalog.finish_receiver_session(
                        self._receiver_session_id
                    )
                    self._receiver_session_id = ""
        except Exception as exc:
            self.log.emit(
                task_id,
                "恢复",
                f"无法保存后台进程恢复信息：{exc}",
                "error",
            )
        self.process_changed.emit(task_id, kind, pid, executable, active)

    def _record_live_progress(
        self,
        task_id: str,
        result: AccessionResult,
    ) -> None:
        try:
            self.catalog.record_result(task_id, result)
        except Exception as exc:
            self.log.emit(
                task_id,
                "恢复",
                f"无法保存实时任务进度：{exc}",
                "error",
            )
        self.progress.emit(task_id, result)
        try:
            self.task_updated.emit(self.catalog.get_summary(task_id))
        except Exception:
            pass

    def _publish(self, summary: TaskSummary) -> None:
        self.task_updated.emit(summary)
        self.tasks_updated.emit(self.manager.list_tasks())
