from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import psutil
import pytest

import dcmget.task_manager as task_manager_module
from dcmget.config import AppConfig
from dcmget.core import AccessionResult, AccessionStatus
from dcmget.pdi import PdiExportResult, PdiStatus
from dcmget.task_manager import PdiQueue, ReceiverService, TaskCatalog, TaskManager
from dcmget.task_state import TaskCheckpointStore, TaskStateError


def catalog(tmp_path, *, auto_migrate=False) -> TaskCatalog:
    return TaskCatalog(
        tmp_path / "tasks.sqlite3",
        legacy_path=tmp_path / "active-task.sqlite3",
        auto_migrate=auto_migrate,
    )


def completed(accession: str, *, files: int = 1) -> AccessionResult:
    return AccessionResult(
        accession,
        AccessionStatus.COMPLETED,
        file_count=files,
        received_bytes=files * 100,
        speed_bytes_per_second=50,
        archived_files=[f"/dicom/{accession}-{index}.dcm" for index in range(files)],
    )


def test_catalog_keeps_multiple_tasks_and_results_isolated(tmp_path):
    store = catalog(tmp_path)
    first = store.create_task(AppConfig(), ["A001", "A002"], name="first")
    second = store.create_task(AppConfig(), ["B001"], name="second")

    store.record_result(first.task_id, completed("A001", files=2))

    first_summary = store.get_summary(first.task_id)
    second_summary = store.get_summary(second.task_id)
    assert first_summary.name == "first"
    assert first_summary.total_count == 2
    assert first_summary.processed_count == 1
    assert first_summary.pending_count == 1
    assert first_summary.file_count == 2
    assert first_summary.received_bytes == 200
    assert first_summary.status == first_summary.phase
    assert first_summary.accession_count == 2
    assert second_summary.processed_count == 0
    assert store.next_pending(first.task_id) == "A002"
    assert store.next_pending(second.task_id) == "B001"
    assert {item.task_id for item in store.list_tasks()} == {
        first.task_id,
        second.task_id,
    }


def test_catalog_still_rejects_duplicate_accessions_within_one_task(tmp_path):
    with pytest.raises(TaskStateError, match="同一任务"):
        catalog(tmp_path).create_task(AppConfig(), ["A001", "A001"])


@pytest.mark.parametrize(
    "phase",
    ["completed", "failed", "cancelled", "download_retryable", "pdi_retryable"],
)
def test_catalog_deletes_only_inactive_terminal_or_retryable_tasks(tmp_path, phase):
    store = catalog(tmp_path)
    task = store.create_task(AppConfig(), [f"A-{phase}"])
    store.set_phase(task.task_id, phase)

    store.delete_task(task.task_id)

    with pytest.raises(TaskStateError, match="不存在"):
        store.get_summary(task.task_id)


@pytest.mark.parametrize(
    "phase",
    [
        "queued",
        "running",
        "pause_pending",
        "paused",
        "cancelling",
        "pdi_pending",
        "pdi_running",
    ],
)
def test_catalog_refuses_to_delete_task_with_background_or_resumable_state(
    tmp_path, phase
):
    store = catalog(tmp_path)
    task = store.create_task(AppConfig(), [f"A-{phase}"])
    store.set_phase(task.task_id, phase)

    with pytest.raises(TaskStateError, match="不能删除"):
        store.delete_task(task.task_id)

    assert store.get_summary(task.task_id).phase == phase


def test_catalog_delete_cascades_database_rows_but_preserves_all_output_files(
    tmp_path,
):
    store = catalog(tmp_path)
    task = store.create_task(AppConfig(), ["A001"])
    output_files = [
        tmp_path / "dicom" / "A001.dcm",
        tmp_path / "pdi" / "DICOMDIR",
        tmp_path / "logs" / "task.log",
        tmp_path / "quarantine" / "unresolved.dcm",
    ]
    for path in output_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"kept")
    store.record_result(
        task.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.COMPLETED,
            file_count=1,
            archived_files=[str(output_files[0])],
        ),
    )
    store.save_pdi_result(
        task.task_id,
        PdiExportResult(
            PdiStatus.COMPLETED,
            output_directory=str(output_files[1].parent),
        ),
    )
    store.set_phase(task.task_id, "completed")

    store.delete_task(task.task_id)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM accessions WHERE task_id = ?", (task.task_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM pdi_results WHERE task_id = ?", (task.task_id,)
        ).fetchone()[0] == 0
    assert all(path.read_bytes() == b"kept" for path in output_files)


def test_catalog_refuses_delete_while_task_process_record_remains(tmp_path):
    store = catalog(tmp_path)
    task = store.create_task(AppConfig(), ["A001"])
    store.set_phase(task.task_id, "completed")
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO task_processes(
                task_id, kind, pid, process_created_at, executable,
                command_line_json, process_group_id
            ) VALUES (?, 'movescu', 123, 1.0, '/missing/movescu', '[]', 0)
            """,
            (task.task_id,),
        )

    with pytest.raises(TaskStateError, match="后台进程记录"):
        store.delete_task(task.task_id)

    assert store.get_summary(task.task_id).phase == "completed"


def test_catalog_persists_complete_pdi_result_and_overwrites_latest(tmp_path):
    store = catalog(tmp_path)
    task = store.create_task(AppConfig(), ["A001"])
    partial = PdiExportResult(
        status=PdiStatus.PARTIAL,
        output_directory=str(tmp_path / "PDI"),
        message="预览器缺失",
        warnings=["未包含离线阅片器", "DICOMDIR 已生成"],
        source_count=12,
        exported_count=10,
        duplicate_count=2,
        indexed_count=9,
        strict_profile=False,
        core_tool_failure=True,
    )

    store.save_pdi_result(task.task_id, partial)

    reopened = catalog(tmp_path)
    assert reopened.load_pdi_result(task.task_id) == partial

    failed = PdiExportResult(
        status=PdiStatus.FAILED,
        message="DICOMDIR 生成失败",
        strict_profile=None,
    )
    reopened.save_pdi_result(task.task_id, failed)
    assert store.load_pdi_result(task.task_id) == failed


def test_catalog_adds_pdi_results_table_to_existing_catalog(tmp_path):
    store = catalog(tmp_path)
    task = store.create_task(AppConfig(), ["A001"])
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE pdi_results")

    reopened = catalog(tmp_path)

    assert reopened.load_pdi_result(task.task_id) is None
    reopened.save_pdi_result(
        task.task_id,
        PdiExportResult(PdiStatus.CANCELLED, message="用户取消"),
    )
    assert reopened.load_pdi_result(task.task_id).status == PdiStatus.CANCELLED


def test_live_progress_updates_summary_without_completing_or_double_counting(
    tmp_path,
):
    store = catalog(tmp_path)
    task = store.create_task(AppConfig(), ["A001"])
    store.record_result(
        task.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.DOWNLOADING,
            file_count=3,
            received_bytes=300,
            speed_bytes_per_second=75,
        ),
    )

    live = store.get_summary(task.task_id)
    assert live.processed_count == 0
    assert live.file_count == 3
    assert live.received_bytes == 300
    assert live.speed_bytes_per_second == 75
    assert store.next_pending(task.task_id) == "A001"
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute(
                "SELECT result_json FROM accessions WHERE task_id = ?",
                (task.task_id,),
            ).fetchone()[0]
            is None
        )

    store.record_result(task.task_id, completed("A001", files=3))
    final = store.get_summary(task.task_id)
    assert final.processed_count == 1
    assert final.file_count == 3
    assert final.received_bytes == 300

    store.record_result(
        task.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.DOWNLOADING,
            file_count=99,
            received_bytes=9_900,
        ),
    )
    assert store.get_summary(task.task_id).file_count == 3


def test_failed_accessions_are_listed_without_pending_or_successful_items(tmp_path):
    store = catalog(tmp_path)
    task = store.create_task(AppConfig(), ["DONE", "FAILED", "PARTIAL", "PENDING"])
    store.record_result(task.task_id, completed("DONE"))
    store.record_result(
        task.task_id,
        AccessionResult("FAILED", AccessionStatus.FAILED, message="failed"),
    )
    store.record_result(
        task.task_id,
        AccessionResult(
            "PARTIAL",
            AccessionStatus.PARTIAL,
            file_count=1,
            archived_files=["/dicom/partial.dcm"],
        ),
    )

    assert store.list_failed_accessions(task.task_id) == ["FAILED", "PARTIAL"]


def test_catalog_fetches_next_pending_without_materializing_40k_results(tmp_path):
    store = catalog(tmp_path)
    summary = store.create_task(
        AppConfig(),
        (f"A{index:05d}" for index in range(40_000)),
    )

    for index in range(3):
        accession = f"A{index:05d}"
        assert store.next_pending(summary.task_id) == accession
        store.record_result(summary.task_id, completed(accession))

    restored = store.get_summary(summary.task_id)
    assert restored.total_count == 40_000
    assert restored.processed_count == 3
    assert restored.pending_count == 39_997
    assert store.next_pending(summary.task_id) == "A00003"

    detail = store.get_task_detail(summary.task_id)
    assert detail.loaded_count == 201
    assert detail.truncated
    assert detail.summary.total_count == 40_000
    assert detail.accessions[0] == "A00000"
    assert detail.accessions[-1] == "A00200"
    assert [result.accession for result in detail.results] == [
        "A00000",
        "A00001",
        "A00002",
    ]
    assert store.list_accessions(summary.task_id, limit=2, offset=200) == [
        "A00200",
        "A00201",
    ]


def test_large_task_summary_keeps_exact_status_counts_beyond_loaded_page(tmp_path):
    store = catalog(tmp_path)
    task = store.create_task(
        AppConfig(),
        (f"A{index:05d}" for index in range(205)),
    )
    store.record_result(task.task_id, completed("A00000"))
    store.record_result(
        task.task_id,
        AccessionResult("A00001", AccessionStatus.NO_DATA),
    )
    store.record_result(
        task.task_id,
        AccessionResult("A00002", AccessionStatus.PARTIAL),
    )
    store.record_result(
        task.task_id,
        AccessionResult("A00003", AccessionStatus.FAILED),
    )
    store.record_result(
        task.task_id,
        AccessionResult("A00204", AccessionStatus.FAILED),
    )

    summary = store.get_summary(task.task_id)
    detail = store.get_task_detail(task.task_id, accession_limit=201)

    assert detail.loaded_count == 201
    assert detail.truncated
    assert all(result.accession != "A00204" for result in detail.results)
    assert summary.processed_count == 5
    assert summary.completed_count == 2
    assert summary.completed_only_count == 1
    assert summary.no_data_count == 1
    assert summary.failed_count == 3
    assert summary.failed_only_count == 2
    assert summary.partial_count == 1
    assert store.list_failed_accessions(task.task_id) == [
        "A00002",
        "A00003",
        "A00204",
    ]


def test_legacy_active_task_is_imported_once_without_deleting_source(tmp_path):
    legacy_path = tmp_path / "active-task.sqlite3"
    legacy = TaskCheckpointStore(legacy_path)
    checkpoint = legacy.start(AppConfig(), ["DONE", "RETRY"], trial_required=True)
    legacy.record_result(checkpoint.task_id, completed("DONE"))
    legacy.record_result(
        checkpoint.task_id,
        AccessionResult(
            "RETRY",
            AccessionStatus.CANCELLED,
            file_count=1,
            archived_files=["/dicom/partial.dcm"],
        ),
    )

    store = catalog(tmp_path, auto_migrate=True)
    migrated = store.get_task(checkpoint.task_id)

    assert legacy_path.is_file()
    backup = tmp_path / "active-task.sqlite3.pre-multitask.bak"
    assert backup.is_file()
    assert TaskCheckpointStore(backup).load_required().task_id == checkpoint.task_id
    assert migrated.task_id == checkpoint.task_id
    assert migrated.trial_required
    assert migrated.summary.phase == "queued"
    assert [result.accession for result in migrated.results] == ["DONE"]
    assert migrated.partial_results["RETRY"].archived_files == ["/dicom/partial.dcm"]
    assert store.next_pending(checkpoint.task_id) == "RETRY"

    reopened = catalog(tmp_path, auto_migrate=True)
    assert [item.task_id for item in reopened.list_tasks()] == [checkpoint.task_id]
    assert backup.is_file()


def test_locked_legacy_task_is_not_imported(tmp_path):
    legacy_path = tmp_path / "active-task.sqlite3"
    first = TaskCheckpointStore(legacy_path)
    first.start(AppConfig(), ["A001"], trial_required=False)
    assert first.try_acquire_lease()
    try:
        store = catalog(tmp_path, auto_migrate=True)
        assert store.list_tasks() == []
    finally:
        first.release_lease()


def test_new_legacy_checkpoint_gets_its_own_matching_backup(tmp_path):
    legacy_path = tmp_path / "active-task.sqlite3"
    legacy = TaskCheckpointStore(legacy_path)
    first = legacy.start(AppConfig(), ["A001"], trial_required=False)
    store = catalog(tmp_path, auto_migrate=True)
    assert store.get_summary(first.task_id).task_id == first.task_id

    second = legacy.start(AppConfig(), ["B001"], trial_required=False)
    reopened = catalog(tmp_path, auto_migrate=True)
    assert reopened.get_summary(second.task_id).task_id == second.task_id
    second_backup = tmp_path / (
        f"active-task.sqlite3.pre-multitask-{second.task_id}.bak"
    )
    assert second_backup.is_file()
    assert TaskCheckpointStore(second_backup).load_required().task_id == second.task_id


def test_delayed_legacy_migration_allows_heterogeneous_receiver_config(tmp_path):
    legacy = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    checkpoint = legacy.start(
        AppConfig(pacs_server_port=11112, storage_port=6666),
        ["LEGACY"],
        trial_required=False,
    )
    assert legacy.try_acquire_lease()
    try:
        store = catalog(tmp_path, auto_migrate=True)
        current = store.create_task(
            AppConfig(
                pacs_server_port=104,
                storage_ae_title="DCMGET_ALT",
                storage_port=7777,
            ),
            ["CURRENT"],
        )
    finally:
        legacy.release_lease()

    migrated = store.migrate_legacy()

    assert migrated is not None
    assert migrated.task_id == checkpoint.task_id
    assert len(store.list_tasks()) == 2
    assert store.get_config(current.task_id).storage_port == 7777
    assert store.get_config(checkpoint.task_id).storage_port == 6666
    assert list(tmp_path.glob("active-task.sqlite3.pre-multitask*.bak"))


def test_delayed_legacy_migration_allows_duplicate_accession(tmp_path):
    legacy = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    checkpoint = legacy.start(AppConfig(), ["DUP"], trial_required=False)
    assert legacy.try_acquire_lease()
    try:
        store = catalog(tmp_path, auto_migrate=True)
        store.create_task(AppConfig(), ["DUP"])
    finally:
        legacy.release_lease()

    migrated = store.migrate_legacy()

    assert migrated is not None
    assert migrated.task_id == checkpoint.task_id
    assert len(store.list_tasks()) == 2
    assert store.get_task(checkpoint.task_id).accessions == ["DUP"]
    assert list(tmp_path.glob("active-task.sqlite3.pre-multitask*.bak"))


def test_round_robin_runs_one_accession_per_task_and_shares_receiver(tmp_path):
    starts: list[str] = []
    stops: list[object | None] = []
    calls: list[tuple[str, str]] = []
    receiver = ReceiverService(
        lambda: starts.append("start") or object(),
        lambda handle: stops.append(handle),
    )

    def execute(task_id, _config, accession):
        calls.append((task_id, accession))
        return completed(accession)

    manager = TaskManager(catalog(tmp_path), execute, receiver=receiver)
    first = manager.create_task(AppConfig(), ["A001", "A002"], name="A")
    second = manager.create_task(AppConfig(), ["B001", "B002"], name="B")

    assert manager.list_tasks()[1].queue_position in {1, 2}
    for _ in range(4):
        assert manager.run_next_round() is not None

    assert calls == [
        (first.task_id, "A001"),
        (second.task_id, "B001"),
        (first.task_id, "A002"),
        (second.task_id, "B002"),
    ]
    assert starts == ["start"]
    assert len(stops) == 1
    assert not receiver.is_running
    assert manager.get_task(first.task_id).summary.phase == "completed"
    assert manager.get_task(second.task_id).summary.phase == "completed"


def test_two_tasks_run_concurrently_but_one_task_uses_only_one_slot(tmp_path):
    entered: list[tuple[str, str]] = []
    active_by_task: dict[str, int] = {}
    peak_total = 0
    peak_same_task = 0
    lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()

    def execute(task_id, _config, accession):
        nonlocal peak_total, peak_same_task
        with lock:
            entered.append((task_id, accession))
            active_by_task[task_id] = active_by_task.get(task_id, 0) + 1
            peak_total = max(peak_total, sum(active_by_task.values()))
            peak_same_task = max(peak_same_task, active_by_task[task_id])
            if sum(active_by_task.values()) == 2:
                both_started.set()
        assert release.wait(2)
        with lock:
            active_by_task[task_id] -= 1
        return completed(accession)

    manager = TaskManager(catalog(tmp_path), execute, max_concurrent_moves=2)
    first = manager.create_task(AppConfig(), ["A001", "A002"])
    second = manager.create_task(AppConfig(), ["B001"])
    worker = threading.Thread(target=manager.run_next_round)
    worker.start()

    assert both_started.wait(2)
    assert {task_id for task_id, _accession in entered} == {
        first.task_id,
        second.task_id,
    }
    release.set()
    worker.join(2)
    while any(item.phase in {"queued", "running"} for item in manager.list_tasks()):
        assert manager.run_next_round() is not None

    assert peak_total == 2
    assert peak_same_task == 1
    assert manager.get_task(first.task_id).summary.phase == "completed"
    assert manager.get_task(second.task_id).summary.phase == "completed"
    manager.shutdown()


def test_same_receiver_key_and_different_key_can_fill_concurrent_slots(tmp_path):
    entered: list[tuple[str, tuple[str, int]]] = []
    active_by_key: dict[tuple[str, int], int] = {}
    peak_by_key: dict[tuple[str, int], int] = {}
    lock = threading.Lock()
    all_started = threading.Event()
    release = threading.Event()

    def execute(task_id, config, accession):
        key = (config.storage_ae_title, config.storage_port)
        with lock:
            entered.append((task_id, key))
            active_by_key[key] = active_by_key.get(key, 0) + 1
            peak_by_key[key] = max(
                peak_by_key.get(key, 0),
                active_by_key[key],
            )
            if len(entered) == 3:
                all_started.set()
        assert release.wait(2)
        with lock:
            active_by_key[key] -= 1
        return completed(accession)

    manager = TaskManager(catalog(tmp_path), execute, max_concurrent_moves=3)
    first = manager.create_task(
        AppConfig(storage_ae_title="STORE_A", storage_port=6666),
        ["A001"],
    )
    same_receiver = manager.create_task(
        AppConfig(storage_ae_title="STORE_A", storage_port=6666),
        ["B001"],
    )
    different_receiver = manager.create_task(
        AppConfig(storage_ae_title="STORE_C", storage_port=7777),
        ["C001"],
    )
    worker = threading.Thread(target=manager.run_next_round)
    worker.start()

    assert all_started.wait(2)
    with lock:
        started_ids = {task_id for task_id, _key in entered}
    assert started_ids == {
        first.task_id,
        same_receiver.task_id,
        different_receiver.task_id,
    }

    release.set()
    worker.join(2)
    deadline = time.monotonic() + 2
    while any(item.phase in {"queued", "running"} for item in manager.list_tasks()):
        assert time.monotonic() < deadline
        manager.run_next_round()

    assert peak_by_key[("STORE_A", 6666)] == 2
    assert {task_id for task_id, _key in entered} == {
        first.task_id,
        same_receiver.task_id,
        different_receiver.task_id,
    }
    manager.shutdown()


def test_duplicate_accession_is_serialized_per_receiver_but_not_across_receivers(
    tmp_path,
):
    entered: list[tuple[str, tuple[str, int], str]] = []
    active_by_route: dict[tuple[tuple[str, int], str], int] = {}
    peak_by_route: dict[tuple[tuple[str, int], str], int] = {}
    lock = threading.Lock()
    two_receivers_started = threading.Event()
    release = threading.Event()

    def execute(task_id, config, accession):
        key = (config.storage_ae_title, config.storage_port)
        route_key = (key, accession)
        with lock:
            entered.append((task_id, key, accession))
            active_by_route[route_key] = active_by_route.get(route_key, 0) + 1
            peak_by_route[route_key] = max(
                peak_by_route.get(route_key, 0),
                active_by_route[route_key],
            )
            if len(entered) == 2:
                two_receivers_started.set()
        assert release.wait(2)
        with lock:
            active_by_route[route_key] -= 1
        return completed(accession)

    manager = TaskManager(catalog(tmp_path), execute, max_concurrent_moves=3)
    first = manager.create_task(
        AppConfig(storage_ae_title="STORE_A", storage_port=6666),
        ["SHARED"],
    )
    same_receiver = manager.create_task(
        AppConfig(storage_ae_title="STORE_A", storage_port=6666),
        ["SHARED"],
    )
    different_receiver = manager.create_task(
        AppConfig(storage_ae_title="STORE_B", storage_port=7777),
        ["SHARED"],
    )
    worker = threading.Thread(target=manager.run_next_round)
    worker.start()

    try:
        assert two_receivers_started.wait(2)
        with lock:
            initially_started = {task_id for task_id, _key, _accession in entered}
        assert initially_started == {first.task_id, different_receiver.task_id}
    finally:
        release.set()
    worker.join(2)

    deadline = time.monotonic() + 2
    while any(item.phase in {"queued", "running"} for item in manager.list_tasks()):
        assert time.monotonic() < deadline
        manager.run_next_round()

    assert {
        task_id for task_id, _key, _accession in entered[:2]
    } == {first.task_id, different_receiver.task_id}
    assert entered[-1][0] == same_receiver.task_id
    assert peak_by_route[(("STORE_A", 6666), "SHARED")] == 1
    assert peak_by_route[(("STORE_B", 7777), "SHARED")] == 1
    assert all(item.phase == "completed" for item in manager.list_tasks())
    manager.shutdown()


def test_new_task_fills_free_slot_while_existing_move_is_still_running(tmp_path):
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    stop_scheduler = threading.Event()

    def execute(_task_id, _config, accession):
        if accession == "A001":
            first_started.set()
            assert release_first.wait(2)
        else:
            second_started.set()
        return completed(accession)

    manager = TaskManager(catalog(tmp_path), execute, max_concurrent_moves=2)
    manager.create_task(AppConfig(), ["A001"])

    def schedule() -> None:
        while not stop_scheduler.is_set():
            if manager.run_next_round() is None:
                time.sleep(0.01)

    worker = threading.Thread(target=schedule)
    worker.start()
    assert first_started.wait(2)
    manager.create_task(
        AppConfig(storage_ae_title="STORE_B", storage_port=7777),
        ["B001"],
    )

    assert second_started.wait(1)
    release_first.set()
    deadline = time.monotonic() + 2
    while any(item.phase in {"queued", "running"} for item in manager.list_tasks()):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    stop_scheduler.set()
    worker.join(2)
    manager.shutdown()


def test_scheduler_turn_reads_config_without_materializing_all_accessions(
    tmp_path, monkeypatch
):
    store = catalog(tmp_path)
    manager = TaskManager(
        store,
        lambda _task_id, _config, accession: completed(accession),
    )
    task = manager.create_task(
        AppConfig(),
        (f"A{index:05d}" for index in range(40_000)),
    )
    monkeypatch.setattr(
        store,
        "get_task",
        lambda _task_id: (_ for _ in ()).throw(
            AssertionError("scheduler materialized the full task")
        ),
    )

    result = manager.run_next_round()

    assert result is not None
    assert result.task_id == task.task_id
    assert result.processed_count == 1


def test_pause_after_current_accession_then_resume(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def execute(_task_id, _config, accession):
        entered.set()
        assert release.wait(2)
        return completed(accession)

    manager = TaskManager(catalog(tmp_path), execute)
    task = manager.create_task(AppConfig(), ["A001", "A002"])
    worker = threading.Thread(target=manager.run_next_round)
    worker.start()
    assert entered.wait(2)

    pending = manager.pause_task(task.task_id)
    assert pending.phase == "pause_pending"
    release.set()
    worker.join(2)

    paused = manager.get_task(task.task_id).summary
    assert paused.phase == "paused"
    assert paused.processed_count == 1
    assert paused.current_accession == ""
    resumed = manager.resume_task(task.task_id)
    assert resumed.phase == "queued"
    assert resumed.queue_position == 1


def test_resuming_pause_pending_does_not_schedule_same_task_twice(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def execute(_task_id, _config, accession):
        calls.append(accession)
        entered.set()
        assert release.wait(2)
        return completed(accession)

    manager = TaskManager(catalog(tmp_path), execute, max_concurrent_moves=2)
    task = manager.create_task(AppConfig(), ["A001", "A002"])
    worker = threading.Thread(target=manager.run_next_round)
    worker.start()
    assert entered.wait(2)

    assert manager.pause_task(task.task_id).phase == "pause_pending"
    resumed = manager.resume_task(task.task_id)
    assert resumed.phase == "running"
    assert resumed.queue_position is None
    assert calls == ["A001"]

    release.set()
    worker.join(2)
    assert calls == ["A001"]
    assert manager.run_next_round() is not None
    assert calls == ["A001", "A002"]
    manager.shutdown()


def test_cancel_active_task_calls_only_its_canceller(tmp_path):
    entered = threading.Event()
    cancelled = threading.Event()
    cancelled_ids: list[str] = []

    def execute(_task_id, _config, accession):
        if accession == "A001":
            entered.set()
            assert cancelled.wait(2)
            return AccessionResult(accession, AccessionStatus.CANCELLED)
        return completed(accession)

    def cancel(task_id):
        cancelled_ids.append(task_id)
        cancelled.set()

    manager = TaskManager(
        catalog(tmp_path),
        execute,
        cancel_accession=cancel,
        max_concurrent_moves=1,
    )
    first = manager.create_task(AppConfig(), ["A001", "A002"])
    second = manager.create_task(
        AppConfig(storage_ae_title="STORE_B", storage_port=7777),
        ["B001"],
    )
    worker = threading.Thread(target=manager.run_next_round)
    worker.start()
    assert entered.wait(2)

    assert manager.cancel_task(first.task_id).phase == "cancelling"
    worker.join(2)

    assert cancelled_ids == [first.task_id]
    assert manager.get_task(first.task_id).summary.phase == "cancelled"
    assert manager.get_task(second.task_id).summary.phase == "queued"
    assert manager.run_next_round() is not None
    assert manager.get_task(second.task_id).summary.phase == "completed"


def test_shutdown_requeues_all_inflight_before_stopping_shared_receiver(tmp_path):
    entered: dict[str, threading.Event] = {}
    cancelled: dict[str, threading.Event] = {}
    active = 0
    active_lock = threading.Lock()
    stopped_with_active: list[int] = []

    class Handle:
        @staticmethod
        def poll():
            return None

    def execute(task_id, _config, accession):
        nonlocal active
        with active_lock:
            active += 1
        entered.setdefault(task_id, threading.Event()).set()
        assert cancelled.setdefault(task_id, threading.Event()).wait(2)
        with active_lock:
            active -= 1
        # A process can win the completion race after shutdown requested cancel;
        # completed work must be committed so recovery does not download it twice.
        return completed(accession)

    def cancel(task_id):
        cancelled.setdefault(task_id, threading.Event()).set()

    def stop(_handle):
        with active_lock:
            stopped_with_active.append(active)

    receiver = ReceiverService(
        Handle,
        stop,
        max_concurrent_moves=2,
    )
    store = catalog(tmp_path)
    manager = TaskManager(
        store,
        execute,
        receiver=receiver,
        cancel_accession=cancel,
        max_concurrent_moves=2,
    )
    first = manager.create_task(
        AppConfig(storage_ae_title="DCMGET_A", storage_port=6666),
        ["A001", "A002"],
    )
    second = manager.create_task(
        AppConfig(storage_ae_title="DCMGET_B", storage_port=6667),
        ["B001"],
    )
    worker = threading.Thread(target=manager.run_next_round)
    worker.start()

    deadline = time.monotonic() + 2
    while len(entered) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert set(entered) == {first.task_id, second.task_id}

    manager.shutdown()
    worker.join(2)

    assert stopped_with_active == [0]
    assert store.get_summary(first.task_id).phase == "queued"
    assert store.get_summary(second.task_id).phase == "completed"
    assert store.get_summary(first.task_id).processed_count == 1
    assert store.get_summary(second.task_id).processed_count == 1
    reopened = TaskManager(
        store,
        lambda _task_id, _config, accession: completed(accession),
        max_concurrent_moves=2,
    )
    assert [
        item.task_id for item in reopened.list_tasks() if item.phase == "queued"
    ] == [first.task_id]
    reopened.shutdown()


def test_shutdown_records_cancelled_partial_files_before_requeue(tmp_path):
    entered = threading.Event()
    cancelled = threading.Event()
    archived = tmp_path / "already-archived.dcm"

    def execute(_task_id, _config, accession):
        entered.set()
        assert cancelled.wait(2)
        return AccessionResult(
            accession,
            AccessionStatus.CANCELLED,
            file_count=1,
            received_bytes=321,
            archived_files=[str(archived)],
        )

    store = catalog(tmp_path)
    manager = TaskManager(
        store,
        execute,
        cancel_accession=lambda _task_id: cancelled.set(),
    )
    task = manager.create_task(AppConfig(), ["A001"])
    worker = threading.Thread(target=manager.run_next_round)
    worker.start()
    assert entered.wait(2)

    assert manager.shutdown()
    worker.join(2)

    summary = store.get_summary(task.task_id)
    assert summary.phase == "queued"
    assert summary.processed_count == 0
    assert summary.file_count == 1
    partial = store.get_task(task.task_id).partial_results["A001"]
    assert partial.archived_files == [str(archived)]
    assert partial.received_bytes == 321

    reopened = TaskManager(
        store,
        lambda _task_id, _config, accession: completed(accession),
    )
    assert reopened.get_task(task.task_id).partial_results[
        "A001"
    ].archived_files == [str(archived)]
    reopened.shutdown()


def test_download_slot_rejects_a_second_parallel_round(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def execute(_task_id, _config, accession):
        entered.set()
        assert release.wait(2)
        return completed(accession)

    manager = TaskManager(catalog(tmp_path), execute)
    manager.create_task(AppConfig(), ["A001"])
    first = threading.Thread(target=manager.run_next_round)
    first.start()
    assert entered.wait(2)

    started = time.monotonic()
    assert manager.run_next_round() is None
    assert time.monotonic() - started < 0.5
    release.set()
    first.join(2)


def test_failed_and_partial_results_retry_with_retained_files(tmp_path):
    calls = 0

    def execute(_task_id, _config, accession):
        nonlocal calls
        calls += 1
        if calls == 1:
            return AccessionResult(
                accession,
                AccessionStatus.PARTIAL,
                file_count=1,
                archived_files=["/dicom/kept.dcm"],
                message="one failed",
            )
        return completed(accession)

    manager = TaskManager(catalog(tmp_path), execute)
    task = manager.create_task(AppConfig(), ["A001"])

    result = manager.run_next_round()
    assert result is not None
    assert result.phase == "download_retryable"
    retried = manager.retry_task(task.task_id)
    assert retried.phase == "queued"
    assert retried.pending_count == 1
    assert manager.get_task(task.task_id).partial_results["A001"].archived_files == [
        "/dicom/kept.dcm"
    ]

    completed_summary = manager.run_next_round()
    assert completed_summary is not None
    assert completed_summary.phase == "completed"
    assert completed_summary.file_count == 2


def test_manager_deletes_cancelled_task_and_rejects_queued_task(tmp_path):
    store = catalog(tmp_path)
    manager = TaskManager(
        store,
        lambda _task_id, _config, accession: completed(accession),
    )
    cancelled = manager.create_task(AppConfig(), ["A001"])
    queued = manager.create_task(AppConfig(), ["B001"])
    manager.cancel_task(cancelled.task_id)

    manager.delete_task(cancelled.task_id)

    assert [item.task_id for item in manager.list_tasks()] == [queued.task_id]
    with pytest.raises(TaskStateError, match="不能删除"):
        manager.delete_task(queued.task_id)
    assert store.get_summary(queued.task_id).phase == "queued"
    assert manager.shutdown()


def test_executor_failure_marks_only_that_task_failed_and_stops_idle_receiver(
    tmp_path,
):
    stops = []
    receiver = ReceiverService(lambda: object(), lambda handle: stops.append(handle))
    manager = TaskManager(
        catalog(tmp_path),
        lambda _task_id, _config, _accession: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
        receiver=receiver,
    )
    task = manager.create_task(AppConfig(), ["A001"])

    with pytest.raises(RuntimeError, match="boom"):
        manager.run_next_round()

    summary = manager.get_task(task.task_id).summary
    assert summary.phase == "failed"
    assert summary.error_message == "boom"
    assert not receiver.is_running
    assert len(stops) == 1


def test_catalog_rejects_unknown_tasks_and_invalid_transitions(tmp_path):
    store = catalog(tmp_path)
    with pytest.raises(TaskStateError, match="不存在"):
        store.next_pending("missing")
    with pytest.raises(TaskStateError, match="不支持"):
        store.set_phase("missing", "mystery")


def test_active_tasks_allow_duplicate_accessions_across_tasks(tmp_path):
    store = catalog(tmp_path)
    first = store.create_task(AppConfig(), ["A001"])
    second = store.create_task(AppConfig(), ["A001", "B001"])

    assert first.task_id != second.task_id
    assert store.next_pending(first.task_id) == "A001"
    assert store.next_pending(second.task_id) == "A001"


def test_concurrent_task_creation_allows_duplicate_accessions(tmp_path):
    first = catalog(tmp_path)
    second = catalog(tmp_path)
    barrier = threading.Barrier(2)
    created = []
    errors = []

    def create(store):
        barrier.wait()
        try:
            created.append(store.create_task(AppConfig(), ["SHARED"]))
        except TaskStateError as exc:
            errors.append(str(exc))

    threads = [
        threading.Thread(target=create, args=(first,)),
        threading.Thread(target=create, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)

    assert len(created) == 2
    assert errors == []
    assert len({item.task_id for item in created}) == 2


def test_retry_can_reactivate_accession_owned_by_another_active_task(tmp_path):
    store = catalog(tmp_path)
    failed = store.create_task(AppConfig(), ["SHARED"])
    store.record_result(
        failed.task_id,
        AccessionResult("SHARED", AccessionStatus.FAILED, message="failed"),
    )
    store.set_phase(failed.task_id, "failed")
    store.create_task(AppConfig(), ["SHARED"])
    manager = TaskManager(
        store,
        lambda _task_id, _config, accession: completed(accession),
    )

    retried = manager.retry_task(failed.task_id)

    assert retried.phase == "queued"
    assert store.next_pending(failed.task_id) == "SHARED"


def test_task_receiver_configuration_is_snapshotted_per_active_task(tmp_path):
    store = catalog(tmp_path)
    first_config = AppConfig(
        dicom_destination_folder=str(tmp_path / "one"),
        pacs_server_ip="192.0.2.10",
        pacs_server_port=104,
        calling_ae_title="CALLING_A",
        pacs_ae_title="PACS_A",
        storage_ae_title="STORE_A",
        storage_port=6666,
    )
    first = store.create_task(first_config, ["A001"])

    other_receiver = AppConfig(
        dicom_destination_folder=str(tmp_path / "two"),
        pacs_server_ip="198.51.100.20",
        pacs_server_port=11112,
        calling_ae_title="CALLING_B",
        pacs_ae_title="PACS_B",
        storage_ae_title="STORE_B",
        storage_port=7777,
        anonymization_enabled=True,
        pdi_export_enabled=True,
        pdi_institution_name="测试医院",
        pdi_output_folder=str(tmp_path / "pdi"),
    )
    second = store.create_task(other_receiver, ["B001"])
    store.validate_shared_config(other_receiver)

    same_receiver = AppConfig(
        dicom_destination_folder=str(tmp_path / "same-receiver"),
        pacs_server_ip="203.0.113.30",
        pacs_server_port=4242,
        calling_ae_title="CALLING_C",
        pacs_ae_title="PACS_C",
        storage_ae_title="STORE_A",
        storage_port=6666,
    )
    third = store.create_task(same_receiver, ["C001"])

    reopened = TaskCatalog(store.path, auto_migrate=False)
    assert reopened.get_config(first.task_id).to_dict() == first_config.to_dict()
    assert reopened.get_config(second.task_id).to_dict() == other_receiver.to_dict()
    assert reopened.get_config(third.task_id).to_dict() == same_receiver.to_dict()

    same_port_different_ae = AppConfig(
        storage_ae_title="STORE_OTHER",
        storage_port=6666,
    )
    with pytest.raises(TaskStateError, match="端口 6666.*STORE_A.*STORE_OTHER"):
        store.create_task(same_port_different_ae, ["D001"])

    same_ae_different_port = AppConfig(
        storage_ae_title="STORE_A",
        storage_port=9999,
    )
    additional_receiver = store.create_task(same_ae_different_port, ["E001"])
    assert store.get_config(additional_receiver.task_id).storage_port == 9999

    different_concurrency = AppConfig(
        dicom_destination_folder=str(tmp_path / "three"),
        max_concurrent_moves=first_config.max_concurrent_moves + 1,
        storage_ae_title="STORE_A",
        storage_port=6666,
    )
    fourth = store.create_task(different_concurrency, ["F001"])
    assert store.get_config(fourth.task_id).max_concurrent_moves == 3

    different_dcmtk = AppConfig(
        dicom_destination_folder=str(tmp_path / "four"),
        dcmtk_bin_dir=str(tmp_path / "other-dcmtk"),
        storage_ae_title="STORE_A",
        storage_port=6666,
    )
    with pytest.raises(TaskStateError, match="dcmtk_bin_dir"):
        store.create_task(different_dcmtk, ["G001"])


def test_restored_tasks_reject_ambiguous_receiver_mapping(tmp_path):
    store = catalog(tmp_path)
    store.create_task(
        AppConfig(storage_ae_title="STORE_A", storage_port=6666),
        ["A001"],
    )
    second = store.create_task(
        AppConfig(storage_ae_title="STORE_B", storage_port=7777),
        ["B001"],
    )
    invalid = AppConfig(storage_ae_title="STORE_B", storage_port=6666)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE tasks SET config_json = ? WHERE task_id = ?",
            (
                json.dumps(invalid.to_dict(), ensure_ascii=False),
                second.task_id,
            ),
        )

    with pytest.raises(TaskStateError, match="端口 6666.*STORE_A.*STORE_B"):
        TaskCatalog(store.path, auto_migrate=False).validate_receiver_mappings()


def test_trial_hook_runs_once_for_task_resume_and_retry(tmp_path):
    store = catalog(tmp_path)
    consumed = []

    def consume(task_id):
        consumed.append(task_id)

    first_manager = TaskManager(
        store,
        lambda _task_id, _config, accession: completed(accession),
        before_first_execution=consume,
    )
    task = first_manager.create_task(AppConfig(), ["A001", "A002"], trial_required=True)
    first_manager.run_next_round()
    assert store.trial_state(task.task_id) == (True, True)

    resumed = TaskManager(
        store,
        lambda _task_id, _config, accession: completed(accession),
        before_first_execution=consume,
    )
    resumed.run_next_round()

    assert consumed == [task.task_id]
    assert resumed.get_task(task.task_id).trial_consumed


def test_receiver_is_not_marked_running_until_start_is_ready():
    stopped = []
    missing = ReceiverService(lambda: None, lambda handle: stopped.append(handle))
    with pytest.raises(RuntimeError, match="未就绪"):
        missing.ensure_started()
    assert not missing.is_running
    assert stopped == []

    class Exited:
        @staticmethod
        def poll():
            return 9

    exited = Exited()
    dead = ReceiverService(lambda: exited, lambda handle: stopped.append(handle))
    with pytest.raises(RuntimeError, match="未就绪"):
        dead.ensure_started()
    assert not dead.is_running
    assert stopped == [exited]


def test_receiver_restarts_when_previously_running_handle_has_exited():
    class Handle:
        def __init__(self):
            self.exit_code = None

        def poll(self):
            return self.exit_code

    first = Handle()
    second = Handle()
    handles = iter((first, second))
    stopped = []
    receiver = ReceiverService(lambda: next(handles), stopped.append)

    assert receiver.ensure_started() is first
    first.exit_code = 1
    assert receiver.ensure_started() is second
    assert stopped == [first]
    assert receiver.is_running


def test_receiver_restarts_when_shared_handle_process_has_exited():
    class Process:
        def __init__(self):
            self.exit_code = None

        def poll(self):
            return self.exit_code

    class SharedHandle:
        def __init__(self):
            self.process = Process()

    first = SharedHandle()
    second = SharedHandle()
    handles = iter((first, second))
    stopped = []
    receiver = ReceiverService(lambda: next(handles), stopped.append)

    assert receiver.ensure_started() is first
    first.process.exit_code = 7
    assert receiver.ensure_started() is second
    assert stopped == [first]


def test_receiver_start_failure_does_not_consume_trial(tmp_path):
    store = catalog(tmp_path)
    consumed = []
    receiver = ReceiverService(
        lambda: None,
        lambda _handle: None,
        lambda *_args: pytest.fail("move must not run when receiver startup fails"),
    )
    manager = TaskManager(
        store,
        receiver=receiver,
        before_first_execution=consumed.append,
    )
    task = manager.create_task(AppConfig(), ["A001"], trial_required=True)

    with pytest.raises(RuntimeError, match="未就绪"):
        manager.run_next_round()

    assert consumed == []
    assert store.trial_state(task.task_id) == (True, False)
    assert manager.last_error_task_id == task.task_id


def test_cancel_while_receiver_starts_never_launches_move_or_consumes_trial(
    tmp_path,
):
    start_entered = threading.Event()
    release_start = threading.Event()
    move_calls: list[str] = []
    consumed: list[str] = []

    def start():
        start_entered.set()
        assert release_start.wait(2)
        return object()

    def run_move(
        _handle,
        _task_id,
        _config,
        accession,
        move_started,
        cancel_event,
    ):
        assert cancel_event.is_set()
        assert move_started is not None
        if not cancel_event.is_set():
            move_calls.append(accession)
            move_started()
        return AccessionResult(accession, AccessionStatus.CANCELLED)

    receiver = ReceiverService(start, lambda _handle: None, run_move)
    store = catalog(tmp_path)
    manager = TaskManager(
        store,
        receiver=receiver,
        cancel_accession=lambda _task_id: None,
        before_first_execution=consumed.append,
    )
    task = manager.create_task(AppConfig(), ["A001"], trial_required=True)
    worker = threading.Thread(target=manager.run_next_round)
    worker.start()
    assert start_entered.wait(2)

    assert manager.cancel_task(task.task_id).phase == "cancelling"
    release_start.set()
    worker.join(2)

    assert not worker.is_alive()
    assert move_calls == []
    assert consumed == []
    assert store.trial_state(task.task_id) == (True, False)
    assert store.get_summary(task.task_id).phase == "cancelled"


def test_shutdown_timeout_during_receiver_start_requeues_without_starting_move(
    tmp_path,
):
    start_entered = threading.Event()
    release_start = threading.Event()
    move_calls: list[str] = []

    def start():
        start_entered.set()
        assert release_start.wait(2)
        return object()

    def run_move(
        _handle,
        _task_id,
        _config,
        accession,
        _move_started,
        cancel_event,
    ):
        assert cancel_event.is_set()
        if not cancel_event.is_set():
            move_calls.append(accession)
        return AccessionResult(accession, AccessionStatus.CANCELLED)

    receiver = ReceiverService(start, lambda _handle: None, run_move)
    store = catalog(tmp_path)
    manager = TaskManager(store, receiver=receiver)
    task = manager.create_task(AppConfig(), ["A001"])
    worker = threading.Thread(target=manager.run_next_round)
    worker.start()
    assert start_entered.wait(2)

    started = time.monotonic()
    assert not manager.shutdown(timeout_seconds=0.05)
    assert time.monotonic() - started < 0.5
    release_start.set()
    assert manager.shutdown(timeout_seconds=2)
    worker.join(2)

    assert not worker.is_alive()
    assert move_calls == []
    assert store.get_summary(task.task_id).phase == "queued"


def test_cancelled_receiver_start_exception_finishes_cancelled(tmp_path):
    start_entered = threading.Event()
    release_start = threading.Event()

    def start():
        start_entered.set()
        assert release_start.wait(2)
        raise RuntimeError("receiver failed while cancellation was pending")

    receiver = ReceiverService(
        start,
        lambda _handle: None,
        lambda *_args: pytest.fail("move must not run after startup failure"),
    )
    store = catalog(tmp_path)
    manager = TaskManager(store, receiver=receiver)
    task = manager.create_task(AppConfig(), ["A001"])
    worker = threading.Thread(target=manager.run_next_round)
    worker.start()
    assert start_entered.wait(2)

    assert manager.cancel_task(task.task_id).phase == "cancelling"
    release_start.set()
    worker.join(2)

    assert not worker.is_alive()
    assert store.get_summary(task.task_id).phase == "cancelled"


def test_movescu_spawn_failure_does_not_consume_trial(tmp_path):
    store = catalog(tmp_path)
    consumed = []

    def fail_before_move(
        _handle,
        _task_id,
        _config,
        _accession,
        _move_started,
        _cancel_event,
    ):
        raise OSError("movescu spawn failed")

    receiver = ReceiverService(
        lambda: object(),
        lambda _handle: None,
        fail_before_move,
    )
    manager = TaskManager(
        store,
        receiver=receiver,
        before_first_execution=consumed.append,
    )
    task = manager.create_task(AppConfig(), ["A001"], trial_required=True)

    with pytest.raises(OSError, match="movescu spawn failed"):
        manager.run_next_round()

    assert consumed == []
    assert store.trial_state(task.task_id) == (True, False)


def test_trial_is_consumed_when_movescu_has_started(tmp_path):
    store = catalog(tmp_path)
    consumed = []

    def complete_after_start(
        _handle,
        _task_id,
        _config,
        accession,
        move_started,
        _cancel_event,
    ):
        assert move_started is not None
        move_started()
        return completed(accession)

    receiver = ReceiverService(
        lambda: object(),
        lambda _handle: None,
        complete_after_start,
    )
    manager = TaskManager(
        store,
        receiver=receiver,
        before_first_execution=consumed.append,
    )
    task = manager.create_task(AppConfig(), ["A001"], trial_required=True)

    manager.run_next_round()

    assert consumed == [task.task_id]
    assert store.trial_state(task.task_id) == (True, True)


def test_pdi_queue_persists_restart_and_runs_separately_from_download(tmp_path):
    store = catalog(tmp_path)
    download_entered = threading.Event()
    pdi_entered = threading.Event()
    release = threading.Event()

    manager = TaskManager(
        store,
        lambda _task_id, _config, accession: (
            download_entered.set(),
            release.wait(2),
            completed(accession),
        )[-1],
    )
    download = manager.create_task(AppConfig(), ["B001"])
    pdi = store.create_task(AppConfig(), ["A001"])
    store.record_result(pdi.task_id, completed("A001"))
    recovery_id, _reused = store.begin_pdi_attempt(pdi.task_id, reuse_existing=False)
    observed_attempts = []

    def execute_pdi(_task_id, record):
        observed_attempts.append(record.pdi_attempt_id)
        pdi_entered.set()
        release.wait(2)
        return True

    abandoned_queue = PdiQueue(store, execute_pdi)
    assert abandoned_queue is not None
    assert store.get_summary(pdi.task_id).phase == "pdi_running"

    queue = PdiQueue(store, execute_pdi)
    assert store.get_summary(pdi.task_id).phase == "pdi_running"

    download_thread = threading.Thread(target=manager.run_next_round)
    pdi_thread = threading.Thread(target=queue.run_next)
    download_thread.start()
    pdi_thread.start()
    assert download_entered.wait(2)
    assert pdi_entered.wait(2)
    release.set()
    download_thread.join(2)
    pdi_thread.join(2)

    assert store.get_summary(download.task_id).phase == "completed"
    assert store.get_summary(pdi.task_id).phase == "completed"
    assert observed_attempts == [recovery_id]


def test_pdi_shutdown_cancels_active_job_and_keeps_it_retryable(tmp_path):
    store = catalog(tmp_path)
    task = store.create_task(AppConfig(), ["A001"])
    store.record_result(task.task_id, completed("A001"))
    store.set_phase(task.task_id, "completed")
    entered = threading.Event()
    cancelled = threading.Event()
    cancelled_ids = []

    def execute(_task_id, _record):
        entered.set()
        assert cancelled.wait(2)
        return False

    def cancel(task_id):
        cancelled_ids.append(task_id)
        cancelled.set()

    queue = PdiQueue(store, execute, cancel=cancel)
    queue.enqueue(task.task_id)
    worker = threading.Thread(target=queue.run_next)
    worker.start()
    assert entered.wait(2)

    queue.shutdown()
    worker.join(2)

    assert cancelled_ids == [task.task_id]
    assert store.get_summary(task.task_id).phase == "pdi_retryable"


def test_pdi_queue_preserves_failed_task_id_for_log_routing(tmp_path):
    store = catalog(tmp_path)
    task = store.create_task(AppConfig(), ["A001"])
    store.record_result(task.task_id, completed("A001"))
    store.set_phase(task.task_id, "completed")
    queue = PdiQueue(
        store,
        lambda _task_id, _record: (_ for _ in ()).throw(
            RuntimeError("PDI failed")
        ),
    )
    queue.enqueue(task.task_id)

    with pytest.raises(RuntimeError, match="PDI failed"):
        queue.run_next()

    assert queue.last_error_task_id == task.task_id


def test_download_persists_pdi_pending_without_completed_crash_window(tmp_path):
    store = catalog(tmp_path)
    exported = []
    queue = PdiQueue(
        store,
        lambda task_id, _record: exported.append(task_id) or True,
    )
    manager = TaskManager(
        store,
        lambda _task_id, _config, accession: completed(accession),
    )
    task = manager.create_task(
        AppConfig(
            pdi_export_enabled=True,
            pdi_institution_name="测试医院",
            pdi_output_folder=str(tmp_path / "pdi"),
        ),
        ["A001"],
    )

    download_result = manager.run_next_round()

    assert download_result is not None
    assert download_result.phase == "pdi_pending"
    assert (
        TaskCatalog(
            store.path,
            legacy_path=tmp_path / "unused.sqlite3",
            auto_migrate=False,
        )
        .get_summary(task.task_id)
        .phase
        == "pdi_pending"
    )
    pdi_result = queue.run_next()
    assert pdi_result is not None
    assert pdi_result.phase == "completed"
    assert exported == [task.task_id]


def test_pdi_attempt_id_is_persisted_and_reused_only_for_resume(tmp_path):
    store = catalog(tmp_path)
    task = store.create_task(AppConfig(), ["A001"])
    store.record_result(task.task_id, completed("A001"))
    store.set_phase(task.task_id, "completed")

    first, first_reused = store.begin_pdi_attempt(task.task_id, reuse_existing=False)
    resumed, resumed_existing = store.begin_pdi_attempt(
        task.task_id, reuse_existing=True
    )
    retry, retry_reused = store.begin_pdi_attempt(task.task_id, reuse_existing=False)

    assert len(first) == 32
    assert first_reused is False
    assert resumed == first
    assert resumed_existing is True
    assert retry != first
    assert retry_reused is False
    assert store.get_task(task.task_id).pdi_attempt_id == retry
    assert store.get_task_detail(task.task_id).pdi_attempt_id == retry


def test_foreground_lease_prevents_gui_and_cli_schedulers_running_together(
    tmp_path,
):
    first = catalog(tmp_path)
    second = catalog(tmp_path)

    assert first.try_acquire_foreground_lease()
    assert first.foreground_lease_held
    assert not second.try_acquire_foreground_lease()
    first.release_foreground_lease()
    assert second.try_acquire_foreground_lease()
    second.release_foreground_lease()


def _sleeping_process() -> subprocess.Popen:
    if os.name != "nt":
        executable = shutil.which("sleep")
        assert executable is not None
        return subprocess.Popen(
            [executable, "60"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_catalog_keeps_unresolved_process_identity_for_startup_block(
    tmp_path, monkeypatch
):
    store = catalog(tmp_path)
    task = store.create_task(AppConfig(), ["A001"])
    process = _sleeping_process()
    try:
        store.record_process(
            task.task_id,
            "movescu",
            process.pid,
            psutil.Process(process.pid).exe(),
            active=True,
        )
        monkeypatch.setattr(
            task_manager_module,
            "_cleanup_process_identity",
            lambda _record, _label: (False, "未能清理测试进程"),
        )

        assert store.cleanup_recorded_processes(task.task_id) == [
            "未能清理测试进程"
        ]
        unresolved = store.unresolved_process_records()
        assert len(unresolved) == 1
        assert "movescu" in unresolved[0]
        assert str(process.pid) in unresolved[0]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_catalog_cleans_identity_checked_task_and_receiver_processes(tmp_path):
    store = catalog(tmp_path)
    task = store.create_task(AppConfig(), ["A001"])
    task_process = _sleeping_process()
    receiver_process = _sleeping_process()
    try:
        store.record_process(
            task.task_id,
            "movescu",
            task_process.pid,
            psutil.Process(task_process.pid).exe(),
            active=True,
        )
        session_id = store.begin_receiver_session(
            receiver_process.pid, psutil.Process(receiver_process.pid).exe()
        )

        task_messages = store.cleanup_recorded_processes(task.task_id)
        receiver_messages = store.cleanup_receiver_sessions()

        task_process.wait(timeout=5)
        receiver_process.wait(timeout=5)
        assert any(
            "movescu" in message and "已清理" in message for message in task_messages
        )
        assert any(
            "storescp" in message and "已清理" in message
            for message in receiver_messages
        )
        assert store.cleanup_recorded_processes(task.task_id) == []
        assert store.cleanup_receiver_sessions() == []
        store.finish_receiver_session(session_id)
    finally:
        for process in (task_process, receiver_process):
            if process.poll() is None:
                process.kill()
                process.wait()


def test_catalog_refuses_to_kill_process_when_executable_identity_changed(
    tmp_path,
):
    store = catalog(tmp_path)
    task = store.create_task(AppConfig(), ["A001"])
    process = _sleeping_process()
    try:
        store.record_process(
            task.task_id,
            "movescu",
            process.pid,
            psutil.Process(process.pid).exe(),
            active=True,
        )
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                """
                UPDATE task_processes SET executable = ?
                WHERE task_id = ? AND kind = 'movescu'
                """,
                (str(tmp_path / "not-the-process"), task.task_id),
            )

        messages = store.cleanup_recorded_processes(task.task_id)

        assert process.poll() is None
        assert any("可执行文件" in message for message in messages)
        assert store.cleanup_recorded_processes(task.task_id) == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
