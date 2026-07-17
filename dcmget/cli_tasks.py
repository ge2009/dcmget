from __future__ import annotations

from pathlib import Path

from .config import AppConfig
from .core import AccessionResult
from .pdi import PdiExportResult
from .task_manager import TERMINAL_PHASES, TaskCatalog, TaskSummary
from .task_state import TaskCheckpoint, TaskStateError


class MultipleTasksError(TaskStateError):
    def __init__(self, tasks: list[TaskSummary]):
        super().__init__(
            "存在多个未完成任务，请使用 --task-id 指定要恢复的任务"
        )
        self.tasks = tasks


def select_cli_task(
    catalog: TaskCatalog, requested_task_id: str | None
) -> TaskSummary | None:
    tasks = catalog.list_tasks()
    if requested_task_id:
        try:
            selected = catalog.get_summary(requested_task_id)
        except TaskStateError as exc:
            raise TaskStateError(f"找不到任务：{requested_task_id}") from exc
        if selected.phase == "completed":
            raise TaskStateError(
                f"任务 {selected.task_id} 已结束"
                f"（{selected.phase}），不能恢复"
            )
        return selected

    unfinished = [
        task
        for task in tasks
        if task.phase not in TERMINAL_PHASES | {"cancelling"}
    ]
    if len(unfinished) > 1:
        raise MultipleTasksError(unfinished)
    return unfinished[0] if unfinished else None


def format_task_list(tasks: list[TaskSummary]) -> list[str]:
    lines = ["未完成任务："]
    for task in sorted(tasks, key=lambda value: value.created_at):
        lines.append(
            f"  {task.task_id}  {task.phase:<18} "
            f"{task.processed_count}/{task.total_count}  {task.name}"
        )
    return lines


class CatalogCheckpointStore:
    """Compatibility layer for the existing single-task CLI workflow."""

    def __init__(
        self,
        catalog: TaskCatalog,
        selected_task_id: str | None,
    ):
        self.catalog = catalog
        self.path = catalog.path
        self._task_id = selected_task_id

    @property
    def has_checkpoint(self) -> bool:
        if not self._task_id:
            return False
        try:
            return self.catalog.get_summary(self._task_id).phase != "completed"
        except TaskStateError:
            return False

    @property
    def lease_held(self) -> bool:
        return self.catalog.foreground_lease_held

    def try_acquire_lease(self) -> bool:
        return self.catalog.try_acquire_foreground_lease()

    def release_lease(self) -> None:
        self.catalog.release_foreground_lease()

    def load(self, *, include_archived_files: bool = True) -> TaskCheckpoint | None:
        del include_archived_files
        if not self.has_checkpoint or self._task_id is None:
            return None
        record = self.catalog.get_task(self._task_id)
        return TaskCheckpoint(
            task_id=record.task_id,
            config=record.config,
            accessions=record.accessions,
            results=record.results,
            partial_results=record.partial_results,
            trial_required=record.trial_required,
            created_at=record.summary.created_at,
            phase=_checkpoint_phase(record.summary.phase),
            pdi_attempt_id=record.pdi_attempt_id,
        )

    def load_required(self) -> TaskCheckpoint:
        checkpoint = self.load()
        if checkpoint is None:
            raise TaskStateError("任务不存在或已经结束")
        return checkpoint

    def start(
        self,
        config: AppConfig,
        accessions: list[str],
        *,
        trial_required: bool,
    ) -> TaskCheckpoint:
        summary = self.catalog.create_task(
            config,
            accessions,
            trial_required=trial_required,
        )
        self._task_id = summary.task_id
        return self.load_required()

    def record_result(self, task_id: str, result: AccessionResult) -> AccessionResult:
        self._require_selected(task_id)
        return self.catalog.record_result(task_id, result)

    def set_phase(self, task_id: str, phase: str) -> None:
        self._require_selected(task_id)
        self.catalog.set_phase(task_id, _catalog_phase(phase))

    def prepare_download_retry(self, task_id: str) -> TaskCheckpoint:
        self._require_selected(task_id)
        self.catalog.retry_failed(task_id)
        return self.load_required()

    def prepare_selected_retry(self) -> None:
        if not self._task_id:
            return
        summary = self.catalog.get_summary(self._task_id)
        if summary.phase in {"failed", "cancelled"}:
            self.catalog.retry_failed(self._task_id)

    def clear(self, task_id: str | None = None) -> None:
        selected = task_id or self._task_id
        if not selected:
            return
        self._require_selected(selected)
        self.catalog.set_phase(
            selected,
            "completed" if task_id is not None else "cancelled",
        )

    def begin_pdi_attempt(
        self,
        task_id: str,
        *,
        reuse_existing: bool,
    ) -> tuple[str, bool]:
        self._require_selected(task_id)
        return self.catalog.begin_pdi_attempt(
            task_id,
            reuse_existing=reuse_existing,
        )

    def save_pdi_result(self, task_id: str, result: PdiExportResult) -> None:
        self._require_selected(task_id)
        self.catalog.save_pdi_result(task_id, result)

    def mark_trial_consumed(self, task_id: str) -> bool:
        self._require_selected(task_id)
        return self.catalog.mark_trial_consumed(task_id)

    def cleanup_recorded_processes(self, task_id: str) -> list[str]:
        self._require_selected(task_id)
        return self.catalog.cleanup_recorded_processes(task_id)

    def cleanup_startup_processes(self) -> list[str]:
        """Clear every stale app-level child before the CLI starts a receiver."""

        from .multitask_runtime import recover_orphaned_shared_staging

        messages: list[str] = []
        for summary in self.catalog.list_tasks():
            messages.extend(
                self.catalog.cleanup_recorded_processes(summary.task_id)
            )
        messages.extend(self.catalog.cleanup_receiver_sessions())
        unresolved = self.catalog.unresolved_process_records()
        if unresolved:
            raise TaskStateError(
                "上次运行的后台进程仍无法安全结束："
                + "、".join(unresolved)
            )
        for summary in self.catalog.list_tasks():
            if summary.phase == "cancelling":
                self.catalog.set_phase(summary.task_id, "cancelled")
        messages.extend(
            recover_orphaned_shared_staging(self.catalog.path.parent)
        )
        return messages

    def record_process(
        self,
        task_id: str,
        kind: str,
        pid: int,
        executable: str | Path,
        *,
        active: bool,
    ) -> None:
        self._require_selected(task_id)
        self.catalog.record_process(
            task_id,
            kind,
            pid,
            executable,
            active=active,
        )

    def _require_selected(self, task_id: str) -> None:
        if not self._task_id or task_id != self._task_id:
            raise TaskStateError("CLI 任务编号不匹配")


def _checkpoint_phase(phase: str) -> str:
    if phase in {"download_retryable", "pdi_pending", "pdi_running", "pdi_retryable"}:
        return phase
    if phase == "cancelling":
        return "cancelled"
    return "downloading"


def _catalog_phase(phase: str) -> str:
    if phase == "downloading":
        return "running"
    return phase
