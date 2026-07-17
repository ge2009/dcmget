from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import DICOM_download_script as cli
from dcmget.cli_tasks import (
    CatalogCheckpointStore,
    MultipleTasksError,
    format_task_list,
    select_cli_task,
)
from dcmget.config import AppConfig
from dcmget.core import (
    AccessionResult,
    AccessionStatus,
    BatchSummary,
    PreflightResult,
    ToolPaths,
)
from dcmget.task_manager import TaskCatalog
import dcmget.task_manager as task_manager_module
from dcmget.task_state import TaskStateError


def catalog(tmp_path) -> TaskCatalog:
    return TaskCatalog(
        tmp_path / "tasks.sqlite3",
        legacy_path=tmp_path / "missing-active-task.sqlite3",
    )


def write_catalog_migration_marker(state_root: Path) -> None:
    marker_path = state_root / "instances" / ".tasks-migrated-v1.json"
    marker_path.parent.mkdir(parents=True)
    marker_path.write_text(
        json.dumps(
            {
                "catalog": str((state_root / "tasks.sqlite3").resolve()),
                "migrated_at": "2026-07-17T00:00:00+00:00",
                "task_ids": ["a" * 32],
                "version": 1,
            }
        ),
        encoding="utf-8",
    )


def test_cli_rejects_default_catalog_after_profile_migration(
    tmp_path, monkeypatch, capsys
):
    state_root = tmp_path / "state"
    write_catalog_migration_marker(state_root)
    monkeypatch.setattr(cli, "application_state_dir", lambda: state_root)
    monkeypatch.setattr(
        task_manager_module,
        "TaskCatalog",
        lambda: pytest.fail("migrated catalog must not be opened"),
    )

    assert cli.main([]) == 1

    error = capsys.readouterr().err
    assert "已迁移到 DcmGet 2.9 实例恢复点" in error
    assert "--task-state" in error


def test_explicit_task_state_remains_usable_after_catalog_migration(
    tmp_path, monkeypatch
):
    state_root = tmp_path / "state"
    write_catalog_migration_marker(state_root)
    monkeypatch.setattr(cli, "application_state_dir", lambda: state_root)
    task_state = tmp_path / "independent" / "active-task.sqlite3"
    store = cli.TaskCheckpointStore(task_state)
    store.start(AppConfig(), ["A001"], trial_required=False)
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = Mock()
    runner.run.return_value = BatchSummary(
        [AccessionResult("A001", AccessionStatus.COMPLETED)]
    )
    monkeypatch.setattr(cli, "authorize_cli", lambda *_args: "licensed")
    monkeypatch.setattr(
        cli,
        "preflight",
        lambda *_args: PreflightResult(tools, {}, [("DCMTK", True, "就绪")]),
    )
    monkeypatch.setattr(cli, "DownloadRunner", Mock(return_value=runner))

    assert cli.main(["--task-state", str(task_state)]) == 0

    runner.run.assert_called_once_with(["A001"])


def test_selects_only_unfinished_task_without_explicit_id(tmp_path):
    tasks = catalog(tmp_path)
    completed = tasks.create_task(AppConfig(), ["DONE"], name="完成任务")
    tasks.set_phase(completed.task_id, "completed")
    pending = tasks.create_task(AppConfig(), ["WAIT"], name="待恢复任务")

    assert select_cli_task(tasks, None).task_id == pending.task_id


def test_multiple_unfinished_tasks_require_task_id_and_are_listed(tmp_path):
    tasks = catalog(tmp_path)
    first = tasks.create_task(AppConfig(), ["A001"], name="first")
    second = tasks.create_task(AppConfig(), ["B001"], name="second")

    with pytest.raises(MultipleTasksError) as raised:
        select_cli_task(tasks, None)

    lines = format_task_list(raised.value.tasks)
    text = "\n".join(lines)
    assert first.task_id in text
    assert second.task_id in text
    assert "0/1" in text


def test_cli_lists_ambiguous_tasks_and_exits(tmp_path, monkeypatch, capsys):
    tasks = catalog(tmp_path)
    first = tasks.create_task(AppConfig(), ["A001"], name="first")
    second = tasks.create_task(AppConfig(), ["B001"], name="second")
    monkeypatch.setattr(task_manager_module, "TaskCatalog", lambda: tasks)

    assert cli.main([]) == 1

    error = capsys.readouterr().err
    assert "--task-id" in error
    assert first.task_id in error
    assert second.task_id in error


def test_explicit_task_id_selects_requested_unfinished_task(tmp_path):
    tasks = catalog(tmp_path)
    first = tasks.create_task(AppConfig(), ["A001"])
    second = tasks.create_task(AppConfig(), ["B001"])

    assert select_cli_task(tasks, second.task_id).task_id == second.task_id
    assert select_cli_task(tasks, first.task_id).task_id == first.task_id


def test_cli_task_id_runs_only_the_requested_catalog_task(tmp_path, monkeypatch):
    tasks = catalog(tmp_path)
    first = tasks.create_task(AppConfig(), ["A001"], name="first")
    second = tasks.create_task(AppConfig(), ["B001"], name="second")
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = Mock()
    runner.run.return_value = BatchSummary(
        [AccessionResult("B001", AccessionStatus.COMPLETED)]
    )
    monkeypatch.setattr(task_manager_module, "TaskCatalog", lambda: tasks)
    monkeypatch.setattr(cli, "authorize_cli", lambda *_args: "licensed")
    monkeypatch.setattr(
        cli,
        "preflight",
        lambda *_args: PreflightResult(tools, {}, [("DCMTK", True, "就绪")]),
    )
    monkeypatch.setattr(cli, "DownloadRunner", Mock(return_value=runner))

    assert cli.main(["--task-id", second.task_id]) == 0

    runner.run.assert_called_once_with(["B001"])
    assert tasks.get_summary(second.task_id).phase == "completed"
    assert tasks.get_summary(first.task_id).phase == "queued"


def test_explicit_unknown_or_completed_task_is_rejected(tmp_path):
    tasks = catalog(tmp_path)
    completed = tasks.create_task(AppConfig(), ["DONE"])
    tasks.set_phase(completed.task_id, "completed")

    with pytest.raises(TaskStateError, match="找不到"):
        select_cli_task(tasks, "f" * 32)
    with pytest.raises(TaskStateError, match="已结束"):
        select_cli_task(tasks, completed.task_id)


def test_cli_selection_finishes_interrupted_cancellation_without_redownload(
    tmp_path,
):
    tasks = catalog(tmp_path)
    summary = tasks.create_task(AppConfig(), ["A001"])
    tasks.set_phase(summary.task_id, "cancelling")

    assert select_cli_task(tasks, None) is None
    assert tasks.get_summary(summary.task_id).phase == "cancelling"
    assert select_cli_task(tasks, summary.task_id).phase == "cancelling"
    store = CatalogCheckpointStore(tasks, None)
    assert store.try_acquire_lease()
    try:
        store.cleanup_startup_processes()
    finally:
        store.release_lease()
    assert tasks.get_summary(summary.task_id).phase == "cancelled"


def test_cli_explicit_cancelled_task_is_prepared_for_retry(
    tmp_path, monkeypatch
):
    tasks = catalog(tmp_path)
    summary = tasks.create_task(AppConfig(), ["A001"])
    tasks.set_phase(summary.task_id, "cancelled")
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = Mock()
    runner.run.return_value = BatchSummary(
        [AccessionResult("A001", AccessionStatus.COMPLETED)]
    )
    monkeypatch.setattr(task_manager_module, "TaskCatalog", lambda: tasks)
    monkeypatch.setattr(cli, "authorize_cli", lambda *_args: "licensed")
    monkeypatch.setattr(
        cli,
        "preflight",
        lambda *_args: PreflightResult(tools, {}, [("DCMTK", True, "就绪")]),
    )
    monkeypatch.setattr(cli, "DownloadRunner", Mock(return_value=runner))

    assert cli.main(["--task-id", summary.task_id]) == 0
    runner.run.assert_called_once_with(["A001"])
    assert tasks.get_summary(summary.task_id).phase == "completed"


def test_cli_rejects_task_id_with_legacy_task_state(tmp_path, capsys):
    assert (
        cli.main(
            [
                "--task-id",
                "a" * 32,
                "--task-state",
                str(tmp_path / "active-task.sqlite3"),
            ]
        )
        == 1
    )
    assert "不能与" in capsys.readouterr().err


def test_catalog_checkpoint_adapter_preserves_history_after_completion(tmp_path):
    tasks = catalog(tmp_path)
    summary = tasks.create_task(AppConfig(), ["A001"], trial_required=True)
    store = CatalogCheckpointStore(tasks, summary.task_id)
    assert store.try_acquire_lease()
    try:
        checkpoint = store.load_required()
        assert checkpoint.pending_accessions == ["A001"]
        assert checkpoint.trial_required

        store.set_phase(summary.task_id, "downloading")
        store.record_result(
            summary.task_id,
            AccessionResult("A001", AccessionStatus.COMPLETED, file_count=2),
        )
        store.clear(summary.task_id)
    finally:
        store.release_lease()

    persisted = tasks.get_summary(summary.task_id)
    assert persisted.phase == "completed"
    assert persisted.processed_count == 1
    assert persisted.file_count == 2


def test_catalog_checkpoint_adapter_marks_explicit_discard_cancelled(tmp_path):
    tasks = catalog(tmp_path)
    summary = tasks.create_task(AppConfig(), ["A001"])
    store = CatalogCheckpointStore(tasks, summary.task_id)

    store.clear()

    assert tasks.get_summary(summary.task_id).phase == "cancelled"


def test_catalog_cli_startup_quarantines_orphaned_shared_staging(tmp_path):
    tasks = catalog(tmp_path)
    orphan = tmp_path / "staging" / "shared-crashed"
    orphan.mkdir(parents=True)
    (orphan / "received.dcm").write_bytes(b"DICM")
    store = CatalogCheckpointStore(tasks, None)

    messages = store.cleanup_startup_processes()

    assert len(messages) == 1
    assert (tmp_path / "quarantine" / "shared-crashed" / "received.dcm").is_file()


def test_catalog_checkpoint_adapter_retries_failed_items_only(tmp_path):
    tasks = catalog(tmp_path)
    summary = tasks.create_task(AppConfig(), ["DONE", "FAILED"])
    tasks.record_result(
        summary.task_id,
        AccessionResult("DONE", AccessionStatus.COMPLETED),
    )
    tasks.record_result(
        summary.task_id,
        AccessionResult("FAILED", AccessionStatus.FAILED),
    )
    tasks.set_phase(summary.task_id, "download_retryable")
    store = CatalogCheckpointStore(tasks, summary.task_id)

    checkpoint = store.prepare_download_retry(summary.task_id)

    assert checkpoint.pending_accessions == ["FAILED"]
    assert [result.accession for result in checkpoint.results] == ["DONE"]


def test_catalog_checkpoint_adapter_reuses_interrupted_pdi_attempt(tmp_path):
    tasks = catalog(tmp_path)
    summary = tasks.create_task(AppConfig(), ["A001"])
    tasks.record_result(
        summary.task_id,
        AccessionResult("A001", AccessionStatus.COMPLETED),
    )
    store = CatalogCheckpointStore(tasks, summary.task_id)

    attempt_id, reused = store.begin_pdi_attempt(
        summary.task_id,
        reuse_existing=False,
    )
    assert not reused
    checkpoint = store.load_required()
    assert checkpoint.phase == "pdi_running"
    assert checkpoint.pdi_attempt_id == attempt_id

    restored_id, restored = store.begin_pdi_attempt(
        summary.task_id,
        reuse_existing=True,
    )
    assert restored
    assert restored_id == attempt_id
