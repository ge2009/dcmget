from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time

import psutil
import pytest

from dcmget.config import AppConfig
from dcmget.core import AccessionResult, AccessionStatus, BatchSummary
from dcmget.task_state import (
    TaskCheckpointStore,
    TaskStateError,
    merge_checkpoint_summary,
)


def test_40k_accessions_are_initialized_once_and_keep_order(tmp_path):
    path = tmp_path / "active-task.sqlite3"
    store = TaskCheckpointStore(path)
    accessions = [f"A{index:05d}" for index in range(40_000)]

    checkpoint = store.start(
        AppConfig(dicom_destination_folder=str(tmp_path / "dicom")),
        accessions,
        trial_required=True,
    )

    assert checkpoint.accessions == accessions
    assert checkpoint.pending_accessions == accessions
    assert checkpoint.results == []
    assert checkpoint.trial_required
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_cancelled_item_stays_pending_and_retained_files_merge_on_resume(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    checkpoint = store.start(AppConfig(), ["A001", "A002", "A003"], trial_required=False)
    store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.COMPLETED,
            file_count=1,
            archived_files=["/dicom/a.dcm"],
        ),
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "A002",
            AccessionStatus.CANCELLED,
            file_count=1,
            duration_seconds=2,
            received_bytes=100,
            archived_files=["/dicom/partial.dcm"],
        ),
    )

    interrupted = store.load_required()
    assert interrupted.pending_accessions == ["A002", "A003"]
    assert interrupted.partial_results["A002"].archived_files == [
        "/dicom/partial.dcm"
    ]

    merged = store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "A002",
            AccessionStatus.FAILED,
            duration_seconds=3,
            message="未收到新文件",
            received_bytes=50,
            archived_files=["/dicom/new.dcm"],
        ),
    )

    assert merged.status == AccessionStatus.PARTIAL
    assert merged.file_count == 2
    assert merged.archived_files == ["/dicom/partial.dcm", "/dicom/new.dcm"]
    assert merged.received_bytes == 150
    restored = store.load_required()
    assert [result.accession for result in restored.results] == ["A001", "A002"]
    assert restored.pending_accessions == ["A003"]


def test_checkpoint_summary_merges_old_and_resumed_results_in_original_order(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    checkpoint = store.start(AppConfig(), ["A001", "A002", "A003"], trial_required=False)
    store.record_result(
        checkpoint.task_id,
        AccessionResult("A001", AccessionStatus.COMPLETED),
    )
    restored = store.load_required()

    summary = merge_checkpoint_summary(
        restored,
        BatchSummary(
            [
                AccessionResult("A002", AccessionStatus.COMPLETED),
                AccessionResult("A003", AccessionStatus.CANCELLED),
            ],
            cancelled=True,
        ),
    )

    assert [result.accession for result in summary.results] == ["A001", "A002", "A003"]
    assert summary.cancelled


def test_corrupt_checkpoint_is_reported_without_deleting_it(tmp_path):
    path = tmp_path / "active-task.sqlite3"
    path.write_bytes(b"not-a-sqlite-database")
    store = TaskCheckpointStore(path)

    with pytest.raises(TaskStateError, match="损坏"):
        store.load()

    assert path.read_bytes() == b"not-a-sqlite-database"


def test_task_lease_prevents_a_second_instance_from_modifying_progress(tmp_path):
    path = tmp_path / "active-task.sqlite3"
    first = TaskCheckpointStore(path)
    second = TaskCheckpointStore(path)

    assert first.try_acquire_lease()
    assert not second.try_acquire_lease()

    first.release_lease()
    assert second.try_acquire_lease()
    second.release_lease()


def test_pdi_phase_is_persisted_for_restart(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    checkpoint = store.start(AppConfig(), ["A001"], trial_required=False)

    store.set_phase(checkpoint.task_id, "pdi_pending")

    assert store.load_required().phase == "pdi_pending"


def test_each_manual_pdi_retry_gets_a_new_attempt_id(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    checkpoint = store.start(AppConfig(), ["A001"], trial_required=False)

    first, first_reused = store.begin_pdi_attempt(
        checkpoint.task_id,
        reuse_existing=False,
    )
    store.set_phase(checkpoint.task_id, "pdi_retryable")
    second, second_reused = store.begin_pdi_attempt(
        checkpoint.task_id,
        reuse_existing=False,
    )
    resumed, resumed_existing = store.begin_pdi_attempt(
        checkpoint.task_id,
        reuse_existing=True,
    )

    assert not first_reused
    assert not second_reused
    assert first != second
    assert resumed_existing
    assert resumed == second
    assert store.load_required().pdi_attempt_id == second


def test_recovery_config_updates_without_losing_progress(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    checkpoint = store.start(AppConfig(), ["A001", "A002"], trial_required=False)
    store.record_result(
        checkpoint.task_id,
        AccessionResult("A001", AccessionStatus.COMPLETED),
    )
    updated = AppConfig(
        dicom_destination_folder=str(tmp_path / "new-destination"),
        pdi_export_enabled=True,
        pdi_institution_name="测试医院",
    )

    store.update_config(checkpoint.task_id, updated)

    restored = store.load_required()
    assert restored.config.dicom_destination_folder == str(
        tmp_path / "new-destination"
    )
    assert restored.config.pdi_institution_name == "测试医院"
    assert [result.accession for result in restored.results] == ["A001"]
    assert restored.pending_accessions == ["A002"]


@pytest.mark.parametrize("kind", ["movescu", "pdi"])
def test_recorded_orphan_process_is_identity_checked_and_terminated(tmp_path, kind):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    checkpoint = store.start(AppConfig(), ["A001"], trial_required=False)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        expected_executable = os.path.realpath(sys.executable)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                actual_executable = os.path.realpath(
                    psutil.Process(process.pid).exe()
                )
                if actual_executable == expected_executable:
                    break
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
            time.sleep(0.01)
        else:
            pytest.fail("测试子进程未在 5 秒内完成启动")

        store.record_process(
            checkpoint.task_id,
            kind,
            process.pid,
            sys.executable,
            active=True,
        )

        messages = store.cleanup_recorded_processes(checkpoint.task_id)

        assert not psutil.pid_exists(process.pid)
        assert any(kind in message and "已清理" in message for message in messages)
    finally:
        if psutil.pid_exists(process.pid):
            try:
                leftover = psutil.Process(process.pid)
                leftover.kill()
                leftover.wait(timeout=5)
            except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                pass


def test_mismatched_process_identity_is_not_killed_and_record_is_discarded(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    checkpoint = store.start(AppConfig(), ["A001"], trial_required=False)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        store.record_process(
            checkpoint.task_id,
            "movescu",
            process.pid,
            sys.executable,
            active=True,
        )
        with sqlite3.connect(store.path) as connection:
            raw = connection.execute(
                "SELECT value FROM metadata WHERE key = 'process:movescu'"
            ).fetchone()[0]
            payload = json.loads(raw)
            payload["executable"] = str(tmp_path / "different-program")
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'process:movescu'",
                (json.dumps(payload),),
            )

        messages = store.cleanup_recorded_processes(checkpoint.task_id)

        assert process.poll() is None
        assert any("可执行文件" in message for message in messages)
        assert store.cleanup_recorded_processes(checkpoint.task_id) == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
