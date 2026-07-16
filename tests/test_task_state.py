from __future__ import annotations

import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
from unittest.mock import Mock

import psutil
import pytest

import dcmget.task_state as task_state_module
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


def test_lightweight_load_omits_paths_but_keeps_status_and_counts(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    checkpoint = store.start(AppConfig(), ["A001", "A002"], trial_required=False)
    store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.COMPLETED,
            file_count=2,
            archived_files=["/dicom/one.dcm", "/dicom/two.dcm"],
        ),
    )

    restored = store.load_required(include_archived_files=False)

    assert restored.results[0].status == AccessionStatus.COMPLETED
    assert restored.results[0].file_count == 2
    assert restored.results[0].archived_files == []
    assert restored.pending_accessions == ["A002"]
    assert store.load_archived_files(checkpoint.task_id) == [
        "/dicom/one.dcm",
        "/dicom/two.dcm",
    ]


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


def test_checkpoint_summary_uses_persisted_partial_result(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    checkpoint = store.start(AppConfig(), ["A001"], trial_required=False)
    store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.CANCELLED,
            file_count=1,
            archived_files=["/dicom/retained.dcm"],
        ),
    )

    summary = merge_checkpoint_summary(
        store.load_required(), BatchSummary(cancelled=True)
    )

    assert summary.results[0].status == AccessionStatus.CANCELLED
    assert summary.results[0].archived_files == ["/dicom/retained.dcm"]


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


def test_failed_and_partial_results_can_be_retried_after_restart(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    checkpoint = store.start(
        AppConfig(), ["DONE", "FAILED", "PARTIAL"], trial_required=False
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult("DONE", AccessionStatus.COMPLETED),
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult("FAILED", AccessionStatus.FAILED, message="timeout"),
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "PARTIAL",
            AccessionStatus.PARTIAL,
            file_count=1,
            duration_seconds=2,
            received_bytes=100,
            archived_files=["/dicom/kept.dcm"],
        ),
    )
    store.set_phase(checkpoint.task_id, "download_retryable")

    retry = store.prepare_download_retry(checkpoint.task_id)

    assert retry.phase == "downloading"
    assert [result.accession for result in retry.results] == ["DONE"]
    assert retry.pending_accessions == ["FAILED", "PARTIAL"]
    assert retry.partial_results["PARTIAL"].archived_files == [
        "/dicom/kept.dcm"
    ]

    merged = store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "PARTIAL",
            AccessionStatus.COMPLETED,
            file_count=1,
            duration_seconds=3,
            received_bytes=50,
            archived_files=["/dicom/new.dcm"],
        ),
    )
    assert merged.status == AccessionStatus.COMPLETED
    assert merged.archived_files == ["/dicom/kept.dcm", "/dicom/new.dcm"]


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
    process_executable = (
        sys.executable if os.name == "nt" else shutil.which("sleep")
    )
    assert process_executable is not None
    command = (
        [process_executable, "-c", "import time; time.sleep(60)"]
        if os.name == "nt"
        else [process_executable, "60"]
    )
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        expected_executable = os.path.realpath(process_executable)
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
            process_executable,
            active=True,
        )

        messages = store.cleanup_recorded_processes(checkpoint.task_id)

        assert process.wait(timeout=5) is not None
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups only")
def test_group_cleanup_refuses_foreign_command_in_same_group(monkeypatch):
    process_group_id = 4321
    expected_executable = "/tools/storescp"
    expected_command = [expected_executable, "--fork", "6666"]
    trusted_process = Mock(pid=4322)
    trusted_process.cmdline.return_value = expected_command
    trusted_process.create_time.return_value = 101.0
    foreign_process = Mock(pid=4323)
    foreign_process.cmdline.return_value = [expected_executable, "--foreign"]
    foreign_process.create_time.return_value = 101.0
    killpg = Mock()
    monkeypatch.setattr(
        task_state_module.psutil,
        "process_iter",
        lambda: [trusted_process, foreign_process],
    )
    monkeypatch.setattr(
        task_state_module.os, "getpgid", lambda _pid: process_group_id
    )
    monkeypatch.setattr(
        task_state_module, "_process_executable", lambda _process: expected_executable
    )
    monkeypatch.setattr(task_state_module.os, "killpg", killpg)

    result = task_state_module._cleanup_recorded_process_group(
        {
            "pid": process_group_id,
            "process_group_id": process_group_id,
            "command_line": expected_command,
        },
        expected_executable,
        100.0,
    )

    assert result == "unsafe"
    killpg.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups only")
def test_group_cleanup_refuses_access_denied_member(monkeypatch):
    process_group_id = 4321
    expected_executable = "/tools/storescp"
    expected_command = [expected_executable, "--fork", "6666"]
    trusted_process = Mock(pid=4322)
    trusted_process.cmdline.return_value = expected_command
    trusted_process.create_time.return_value = 101.0
    denied_process = Mock(pid=4323)
    denied_process.cmdline.side_effect = psutil.AccessDenied(pid=denied_process.pid)
    killpg = Mock()
    monkeypatch.setattr(
        task_state_module.psutil,
        "process_iter",
        lambda: [trusted_process, denied_process],
    )
    monkeypatch.setattr(
        task_state_module.os, "getpgid", lambda _pid: process_group_id
    )
    monkeypatch.setattr(
        task_state_module, "_process_executable", lambda _process: expected_executable
    )
    monkeypatch.setattr(task_state_module.os, "killpg", killpg)

    result = task_state_module._cleanup_recorded_process_group(
        {
            "pid": process_group_id,
            "process_group_id": process_group_id,
            "command_line": expected_command,
        },
        expected_executable,
        100.0,
    )

    assert result == "unsafe"
    killpg.assert_not_called()


def test_group_cleanup_windows_does_not_call_posix_process_apis(monkeypatch):
    process_iter = Mock(side_effect=AssertionError("process_iter must not be called"))
    getpgid = Mock(side_effect=AssertionError("getpgid must not be called"))
    killpg = Mock(side_effect=AssertionError("killpg must not be called"))
    monkeypatch.setattr(task_state_module.os, "name", "nt")
    monkeypatch.setattr(task_state_module.psutil, "process_iter", process_iter)
    monkeypatch.setattr(task_state_module.os, "getpgid", getpgid)
    monkeypatch.setattr(task_state_module.os, "killpg", killpg)

    result = task_state_module._cleanup_recorded_process_group(
        {
            "pid": 4321,
            "process_group_id": 4321,
            "command_line": ["storescp.exe", "6666"],
        },
        "storescp.exe",
        100.0,
    )

    assert result == "empty"
    process_iter.assert_not_called()
    getpgid.assert_not_called()
    killpg.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups only")
def test_cleanup_recovers_fork_child_after_recorded_leader_disappears(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    checkpoint = store.start(AppConfig(), ["A001"], trial_required=False)
    child_pid_file = tmp_path / "child.pid"
    leader_code = (
        "import os, pathlib, sys, time; "
        "child = os.fork(); "
        "path = pathlib.Path(sys.argv[1]); "
        "path.write_text(str(child) if child else str(os.getpid())); "
        "time.sleep(30) if child == 0 else time.sleep(1)"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code, str(child_pid_file)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pid = 0
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if child_pid_file.is_file():
                child_pid = int(child_pid_file.read_text(encoding="utf-8"))
                if child_pid and child_pid != leader.pid:
                    break
            time.sleep(0.02)
        else:
            pytest.fail("fork child did not start")

        store.record_process(
            checkpoint.task_id,
            "storescp",
            leader.pid,
            psutil.Process(leader.pid).exe(),
            active=True,
        )
        leader.wait(timeout=5)
        assert psutil.pid_exists(child_pid)

        messages = store.cleanup_recorded_processes(checkpoint.task_id)

        assert any(
            "进程组" in message and "已清理" in message for message in messages
        ), messages
        assert not psutil.pid_exists(child_pid) or (
            psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
        )
        assert store.cleanup_recorded_processes(checkpoint.task_id) == []
    finally:
        if leader.poll() is None:
            leader.kill()
            leader.wait()
        if child_pid and psutil.pid_exists(child_pid):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass
