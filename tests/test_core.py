from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from dcmget.config import AppConfig
from dcmget import core
from dcmget.core import (
    AccessionResult,
    AccessionStatus,
    BatchSummary,
    DcmtkResolver,
    DownloadRunner,
    ToolPaths,
    build_movescu_command,
    build_storescp_command,
    preflight,
    safe_accession_dir,
)


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Windows", "AMD64", "windows-x86_64"),
        ("Darwin", "arm64", "macos-arm64"),
        ("Darwin", "x86_64", "macos-x86_64"),
        ("Linux", "x86_64", "linux-x86_64"),
    ],
)
def test_platform_key(monkeypatch, system, machine, expected):
    monkeypatch.setattr(core.platform, "system", lambda: system)
    monkeypatch.setattr(core.platform, "machine", lambda: machine)
    assert core.current_platform_key() == expected


@pytest.mark.parametrize("version", ["3.6.9", "3.7.0"])
def test_dcmtk_commands_use_storescp_move_destination_and_argument_arrays(tmp_path, version):
    config = AppConfig(
        pacs_server_ip="10.0.0.8",
        pacs_server_port=104,
        calling_ae_title="CALLING",
        pacs_ae_title="PACS",
        storage_ae_title="STORAGE",
        storage_port=11112,
    )
    tools = ToolPaths(
        Path("/tools/movescu"),
        Path("/tools/storescp"),
        Path("/tools"),
        version,
        "--fork fork mode",
    )

    store = build_storescp_command(config, tools, tmp_path)
    move = build_movescu_command(config, tools, "ACC 001")

    assert store[:10] == [
        str(tools.storescp),
        "-v",
        "-aet",
        "STORAGE",
        "+xa",
        "+uf",
        "-fe",
        ".dcm",
        "-od",
        str(tmp_path),
    ]
    assert store[-1] == "11112"
    assert "--fork" in store
    assert move == [
        str(tools.movescu),
        "-v",
        "--no-port",
        "-to",
        "30",
        "-td",
        "300",
        "-aet",
        "CALLING",
        "-aec",
        "PACS",
        "-aem",
        "STORAGE",
        "10.0.0.8",
        "104",
        "-S",
        "-k",
        "QueryRetrieveLevel=STUDY",
        "-k",
        "0008,0050=ACC 001",
    ]


def test_storescp_falls_back_to_single_process(tmp_path):
    config = AppConfig(storage_port=11112)
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0", "")
    assert "--single-process" in build_storescp_command(config, tools, tmp_path)


def test_windows_dcmtk_fork_capability_is_not_disabled():
    tools = ToolPaths(
        Path("movescu.exe"),
        Path("storescp.exe"),
        Path("."),
        "3.7.0",
        "--single-process\n--fork\n--max-associations",
    )

    assert tools.supports_fork


def test_resolver_only_uses_current_platform_runtime(tmp_path, monkeypatch):
    current = tmp_path / ".runtime" / "dcmtk" / "macos-arm64" / "dcmtk-current" / "bin"
    other = tmp_path / ".runtime" / "dcmtk" / "macos-x86_64" / "dcmtk-other" / "bin"
    suffix = ".exe" if core.os.name == "nt" else ""
    for directory in (current, other):
        directory.mkdir(parents=True)
        (directory / f"movescu{suffix}").touch()
        (directory / f"storescp{suffix}").touch()
    monkeypatch.setattr(core, "current_platform_key", lambda: "macos-arm64")
    resolver = DcmtkResolver(tmp_path)
    monkeypatch.setattr(
        resolver,
        "_probe",
        lambda move, store: ToolPaths(move, store, move.parent, "3.7.0"),
    )

    resolved = resolver.resolve()

    assert resolved.bin_dir == current


def test_resolver_discovers_validation_and_pdi_tools_next_to_dcmtk_binaries(
    tmp_path, monkeypatch
):
    suffix = ".exe" if core.os.name == "nt" else ""
    for name in ("movescu", "storescp", "dcmmkdir", "dcmdump"):
        (tmp_path / f"{name}{suffix}").touch()
    resolver = DcmtkResolver(tmp_path)
    monkeypatch.setattr(core, "_run_probe", lambda *_args, **_kwargs: "dcmtk v3.7.0")

    tools = resolver._probe(
        tmp_path / f"movescu{suffix}", tmp_path / f"storescp{suffix}"
    )

    assert tools.dcmmkdir == tmp_path / f"dcmmkdir{suffix}"
    assert tools.dcmdump == tmp_path / f"dcmdump{suffix}"


def test_preflight_reports_port_conflict(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    config = AppConfig(dicom_destination_folder=str(tmp_path), storage_port=port)
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    resolver = Mock(spec=DcmtkResolver)
    resolver.resolve.return_value = tools
    try:
        result = preflight(config, resolver)
    finally:
        listener.close()

    assert not result.ok
    assert "storage_port" in result.errors


def test_static_preflight_defers_receiver_port_check(tmp_path, monkeypatch):
    config = AppConfig(dicom_destination_folder=str(tmp_path), storage_port=6666)
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    resolver = Mock(spec=DcmtkResolver)
    resolver.resolve.return_value = tools
    monkeypatch.setattr(core, "is_port_available", lambda _port: False)

    result = preflight(config, resolver, check_port=False)

    assert result.ok
    assert "storage_port" not in result.errors
    assert ("接收端口", True, "将在任务获得运行机会时检查") in result.checks


def test_preflight_requires_pdi_dcmtk_tools_only_when_pdi_is_enabled(tmp_path):
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    resolver = Mock(spec=DcmtkResolver)
    resolver.resolve.return_value = tools

    disabled = preflight(
        AppConfig(dicom_destination_folder=str(tmp_path), pdi_export_enabled=False),
        resolver,
    )
    enabled = preflight(
        AppConfig(
            dicom_destination_folder=str(tmp_path),
            pdi_export_enabled=True,
            pdi_institution_name="测试医院",
        ),
        resolver,
    )

    assert "dcmtk_bin_dir" not in disabled.errors
    assert "dcmtk_bin_dir" in enabled.errors


def test_preflight_ohif_does_not_require_image_conversion_tool(tmp_path):
    tools = ToolPaths(
        Path("movescu"),
        Path("storescp"),
        Path("."),
        "3.7.0",
        dcmmkdir=Path("dcmmkdir"),
    )
    resolver = Mock(spec=DcmtkResolver)
    resolver.resolve.return_value = tools

    result = preflight(
        AppConfig(
            dicom_destination_folder=str(tmp_path / "dicom"),
            pdi_export_enabled=True,
            pdi_institution_name="测试医院",
        ),
        resolver,
    )

    assert result.ok
    assert any(
        name == "PDI 网页阅片" and ok and "原始 DICOM" in message
        for name, ok, message in result.checks
    )
    assert all(name != "PDI 网页预览" for name, _ok, _message in result.checks)


def test_preflight_uses_configured_pdi_output_folder(tmp_path):
    pdi_output = tmp_path / "portable media"
    tools = ToolPaths(
        Path("movescu"),
        Path("storescp"),
        Path("."),
        "3.7.0",
        dcmmkdir=Path("dcmmkdir"),
    )
    resolver = Mock(spec=DcmtkResolver)
    resolver.resolve.return_value = tools
    result = preflight(
        AppConfig(
            dicom_destination_folder=str(tmp_path / "dicom"),
            pdi_export_enabled=True,
            pdi_institution_name="测试医院",
            pdi_output_folder=str(pdi_output),
        ),
        resolver,
    )

    assert result.ok
    assert pdi_output.is_dir()
    assert any(name == "PDI 输出目录" and ok for name, ok, _message in result.checks)


def test_file_archive_adds_dcm_suffix_and_uses_metadata_directory(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    first = staging / "CT.1"
    metadata = FileMetaDataset()
    metadata.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(first, {}, file_meta=metadata, preamble=b"\0" * 128)
    dataset.PatientID = "PAT001"
    dataset.AccessionNumber = "ACC001"
    dataset.StudyInstanceUID = "1.2.3.4"
    dataset.SOPInstanceUID = "1.2.3.4.5"
    dataset.save_as(first)

    moved, rejected = core._archive_dicom_files(
        [first],
        tmp_path / "dicom",
        "{PatientID}/{AccessionNumber}/{StudyInstanceUID}",
        "FALLBACK",
    )

    assert [path.name for path in moved] == ["1.2.3.4.5.dcm"]
    assert rejected == []
    assert not first.exists()
    target = (
        tmp_path
        / "dicom"
        / "PAT001"
        / "ACC001"
        / "1.2.3.4"
        / "1.2.3.4.5.dcm"
    )
    assert target.exists()
    assert target.read_bytes()[128:132] == b"DICM"


def test_concurrent_publish_never_overwrites_conflicting_sop_instance(tmp_path):
    first = tmp_path / "first.dcm"
    second = tmp_path / "second.dcm"
    target = tmp_path / "archive" / "1.2.3.dcm"
    target.parent.mkdir()
    first.write_bytes(b"first-content")
    second.write_bytes(b"second-content")
    barrier = threading.Barrier(2)
    errors: list[Exception] = []
    errors_lock = threading.Lock()

    def publish(source: Path) -> None:
        barrier.wait()
        try:
            core._publish_or_deduplicate(source, target)
        except Exception as exc:
            with errors_lock:
                errors.append(exc)

    workers = [
        threading.Thread(target=publish, args=(source,))
        for source in (first, second)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(2)

    assert all(not worker.is_alive() for worker in workers)
    assert len(errors) == 1
    assert "SOP Instance UID 内容冲突" in str(errors[0])
    assert target.read_bytes() in {b"first-content", b"second-content"}
    remaining = [source for source in (first, second) if source.exists()]
    assert len(remaining) == 1
    assert remaining[0].read_bytes() != target.read_bytes()


def test_archive_cancel_stops_before_subsequent_files_and_preserves_staging(
    tmp_path, monkeypatch
):
    staging = tmp_path / "staging"
    staging.mkdir()
    first = staging / "first.dcm"
    second = staging / "second.dcm"
    _write_minimal_dicom(first, "1.2.3.101", accession="ACC101")
    _write_minimal_dicom(second, "1.2.3.102", accession="ACC101")
    cancel = threading.Event()
    publish = core._publish_or_deduplicate

    def publish_then_cancel(source, target):
        publish(source, target)
        cancel.set()

    monkeypatch.setattr(core, "_publish_or_deduplicate", publish_then_cancel)

    moved, rejected = core._archive_dicom_files(
        [first, second],
        tmp_path / "dicom",
        "{AccessionNumber}",
        "ACC101",
        cancel_event=cancel,
    )

    assert len(moved) == 1
    assert rejected == []
    assert not first.exists()
    assert second.exists()
    assert len(list((tmp_path / "dicom").rglob("*.dcm"))) == 1


def test_anonymized_runtime_files_use_private_application_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(core, "ensure_application_state_dir", lambda: state)
    anonymous = AppConfig(
        dicom_destination_folder=str(tmp_path / "dicom"),
        anonymization_enabled=True,
    )
    regular = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))

    assert core.staging_directory_root(anonymous) == state / "staging"
    assert core.log_directory(anonymous) == state / "logs"
    assert core.staging_directory_root(regular) == tmp_path / "dicom" / ".dcmget-staging"
    assert core.log_directory(regular) == state / "logs"


def test_task_log_is_private_and_not_created_beside_dicom(tmp_path, monkeypatch):
    state = tmp_path / "private-state"
    destination = tmp_path / "dicom"
    monkeypatch.setattr(core, "ensure_application_state_dir", lambda: state)
    runner = DownloadRunner(
        AppConfig(dicom_destination_folder=str(destination)),
        ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0"),
    )
    runner._emit("应用", "private log test", "info")
    runner._close_file_logger()

    log = state / "logs" / "dcmget.log"
    assert log.is_file()
    assert not (destination / "logs").exists()
    if os.name != "nt":
        assert log.stat().st_mode & 0o777 == 0o600


def test_file_archive_uses_safe_fallbacks_and_does_not_overwrite(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    first = staging / "MR.1.dcm"
    second = staging / "MR.1"
    _write_minimal_dicom(first, "1.2.3.1")
    _write_minimal_dicom(second, "1.2.3.2")

    moved, rejected = core._archive_dicom_files(
        [first, second],
        tmp_path / "dicom",
        "{PatientID}/{AccessionNumber}",
        "ACC/002",
    )

    assert [path.name for path in moved] == ["1.2.3.1.dcm", "1.2.3.2.dcm"]
    assert rejected == []
    assert all(path.suffix == ".dcm" for path in moved)
    assert all("ACC_002" in str(path) for path in moved)


def test_truncated_pixel_data_is_rejected_and_kept_in_staging(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "truncated.dcm"
    metadata = FileMetaDataset()
    metadata.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(source, {}, file_meta=metadata, preamble=b"\0" * 128)
    dataset.SOPInstanceUID = "1.2.3.99"
    dataset.Rows = 64
    dataset.Columns = 64
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.PixelData = b"\0" * (64 * 64)
    dataset.save_as(source)
    source.write_bytes(source.read_bytes()[:-512])

    moved, rejected = core._archive_dicom_files(
        [source],
        tmp_path / "dicom",
        "{AccessionNumber}",
        "ACC-TRUNCATED",
    )

    assert moved == []
    assert rejected == [source]
    assert source.exists()


def test_dcmdump_validation_terminates_running_process_when_cancelled(
    tmp_path, monkeypatch
):
    started = threading.Event()
    terminated = threading.Event()
    cancel = threading.Event()
    outcomes: list[bool | None] = []

    class Process:
        pid = 4321
        stopped = False

        @classmethod
        def poll(cls):
            started.set()
            return -15 if cls.stopped else None

    process = Process()

    def terminate(target):
        assert target is process
        Process.stopped = True
        terminated.set()

    monkeypatch.setattr(core.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(core, "_terminate_process", terminate)
    worker = threading.Thread(
        target=lambda: outcomes.append(
            core._run_dcmdump_validation(
                tmp_path / "dcmdump",
                [tmp_path / "image.dcm"],
                None,
                cancel_event=cancel,
            )
        ),
        daemon=True,
    )
    worker.start()
    assert started.wait(0.5)

    cancel.set()
    worker.join(1)

    assert not worker.is_alive()
    assert terminated.is_set()
    assert outcomes == [None]


def test_retry_same_sop_and_content_is_idempotent(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    first = staging / "first.dcm"
    duplicate = staging / "duplicate.dcm"
    _write_minimal_dicom(first, "1.2.3.50", accession="ACC050")
    duplicate.write_bytes(first.read_bytes())

    first_moved, first_rejected = core._archive_dicom_files(
        [first], tmp_path / "dicom", "{AccessionNumber}", "ACC050"
    )
    retry_moved, retry_rejected = core._archive_dicom_files(
        [duplicate], tmp_path / "dicom", "{AccessionNumber}", "ACC050"
    )

    assert first_rejected == retry_rejected == []
    assert retry_moved == first_moved
    assert len(list((tmp_path / "dicom").rglob("*.dcm"))) == 1
    assert not duplicate.exists()


def test_same_sop_with_different_content_is_rejected_as_conflict(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    first = staging / "first.dcm"
    conflict = staging / "conflict.dcm"
    _write_minimal_dicom(first, "1.2.3.51", accession="ACC051")
    different = bytearray(first.read_bytes())
    different[0] = 1
    conflict.write_bytes(different)

    moved, rejected = core._archive_dicom_files(
        [first], tmp_path / "dicom", "{AccessionNumber}", "ACC051"
    )
    conflict_moved, conflict_rejected = core._archive_dicom_files(
        [conflict], tmp_path / "dicom", "{AccessionNumber}", "ACC051"
    )

    assert len(moved) == 1 and rejected == []
    assert conflict_moved == []
    assert conflict_rejected == [conflict]
    assert conflict.exists()
    assert len(list((tmp_path / "dicom").rglob("*.dcm"))) == 1


def test_non_dicom_file_stays_in_staging_and_is_not_counted(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    invalid = staging / "not-dicom.bin"
    invalid.write_bytes(b"plain text, not a DICOM data set")

    moved, rejected = core._archive_dicom_files(
        [invalid],
        tmp_path / "dicom",
        "{AccessionNumber}",
        "ACC003",
    )

    assert moved == []
    assert rejected == [invalid]
    assert invalid.exists()
    assert not list((tmp_path / "dicom").rglob("*.dcm"))


def test_failed_and_partial_items_are_retryable():
    summary = BatchSummary(
        [
            AccessionResult("OK", AccessionStatus.COMPLETED),
            AccessionResult("EMPTY", AccessionStatus.NO_DATA),
            AccessionResult("PART", AccessionStatus.PARTIAL),
            AccessionResult("FAIL", AccessionStatus.FAILED),
        ]
    )
    assert summary.failed_accessions == ["PART", "FAIL"]
    assert summary.exit_code == 2


def test_cancel_terminates_current_movescu(tmp_path, monkeypatch):
    config = AppConfig(dicom_destination_folder=str(tmp_path))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = DownloadRunner(config, tools)
    process = Mock()
    runner._current_process = process
    terminate = Mock()
    monkeypatch.setattr(core, "_terminate_process", terminate)

    runner.request_cancel()

    terminate.assert_called_once_with(process)


def test_pause_waits_between_accessions_and_resume_continues(tmp_path, monkeypatch):
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    states: list[str] = []
    first_started = threading.Event()
    finish_first = threading.Event()
    second_started = threading.Event()
    paused = threading.Event()
    receiver = Mock()
    receiver.poll.return_value = None
    receiver_starts = 0

    def record_state(state: str) -> None:
        states.append(state)
        if state == "paused":
            paused.set()

    runner = DownloadRunner(config, tools, state_callback=record_state)

    def start_receiver(_staging):
        nonlocal receiver_starts
        receiver_starts += 1
        runner._storescp_process = receiver

    monkeypatch.setattr(runner, "_start_storescp", start_receiver)
    monkeypatch.setattr(runner, "_stop_storescp", lambda: None)

    def download_one(accession, _staging, _index, _total):
        assert runner._storescp_process is receiver
        if accession == "A001":
            first_started.set()
            assert finish_first.wait(2)
        else:
            second_started.set()
        return AccessionResult(accession, AccessionStatus.COMPLETED)

    monkeypatch.setattr(runner, "_download_one", download_one)
    result: list[BatchSummary] = []
    thread = threading.Thread(
        target=lambda: result.append(runner.run(["A001", "A002"])), daemon=True
    )
    thread.start()
    assert first_started.wait(2)

    runner.request_pause()
    finish_first.set()

    assert paused.wait(2)
    assert not second_started.wait(0.1)
    runner.request_resume()
    assert second_started.wait(2)
    thread.join(2)

    assert not thread.is_alive()
    assert [item.accession for item in result[0].results] == ["A001", "A002"]
    assert receiver_starts == 1
    assert "pause_pending" in states
    assert "paused" in states


def test_pause_in_start_boundary_does_not_launch_movescu(tmp_path, monkeypatch):
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    first_gate_passed = threading.Event()
    release_boundary = threading.Event()
    paused = threading.Event()
    process_started = threading.Event()

    def record_state(state: str) -> None:
        if state == "paused":
            paused.set()

    runner = DownloadRunner(config, tools, state_callback=record_state)
    monkeypatch.setattr(runner, "_start_storescp", lambda _staging: None)
    original_wait = runner._wait_if_paused
    gate_calls = 0

    def wait_at_boundary():
        nonlocal gate_calls
        result = original_wait()
        gate_calls += 1
        if gate_calls == 1:
            first_gate_passed.set()
            assert release_boundary.wait(2)
        return result

    class Process:
        stdout = iter(())

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            return 0

    def start_process(_command):
        process_started.set()
        return Process()

    monkeypatch.setattr(runner, "_wait_if_paused", wait_at_boundary)
    monkeypatch.setattr(runner, "_popen", start_process)
    result: list[BatchSummary] = []
    thread = threading.Thread(
        target=lambda: result.append(runner.run(["A001"])), daemon=True
    )
    thread.start()
    assert first_gate_passed.wait(2)

    runner.request_pause()
    release_boundary.set()

    assert paused.wait(2)
    assert not process_started.wait(0.1)
    runner.request_resume()
    assert process_started.wait(2)
    thread.join(2)
    assert not thread.is_alive()
    assert result[0].results[0].status == AccessionStatus.NO_DATA


def test_cancel_wakes_a_paused_runner(tmp_path, monkeypatch):
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    paused = threading.Event()
    runner = DownloadRunner(
        config,
        tools,
        state_callback=lambda state: paused.set() if state == "paused" else None,
    )
    monkeypatch.setattr(runner, "_start_storescp", lambda _staging: None)
    runner.request_pause()
    result: list[BatchSummary] = []
    thread = threading.Thread(
        target=lambda: result.append(runner.run(["A001"])), daemon=True
    )
    thread.start()
    assert paused.wait(2)

    runner.request_cancel()
    thread.join(2)

    assert not thread.is_alive()
    assert result[0].cancelled
    assert result[0].results[0].status == AccessionStatus.CANCELLED


def test_request_cancel_returns_before_background_process_cleanup_finishes(
    tmp_path, monkeypatch
):
    runner = DownloadRunner(
        AppConfig(dicom_destination_folder=str(tmp_path)),
        ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0"),
    )
    current = Mock(pid=101)
    receiver = Mock(pid=102)
    runner._current_process = current
    runner._storescp_process = receiver
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()
    returned = threading.Event()
    terminated: list[object] = []

    def terminate(process):
        terminated.append(process)
        cleanup_entered.set()
        assert allow_cleanup.wait(2)

    monkeypatch.setattr(core, "_terminate_process", terminate)

    caller = threading.Thread(
        target=lambda: (runner.request_cancel(), returned.set()),
        daemon=True,
    )
    caller.start()

    assert returned.wait(0.5), "request_cancel blocked on process termination"
    assert cleanup_entered.wait(0.5)
    concurrent_cleanup = threading.Thread(
        target=runner._terminate_process_safely,
        args=(current,),
        daemon=True,
    )
    concurrent_cleanup.start()
    assert len(terminated) == 1

    allow_cleanup.set()
    caller.join(1)
    concurrent_cleanup.join(1)
    assert runner._cancel_cleanup_thread is not None
    runner._cancel_cleanup_thread.join(1)
    runner._close_file_logger()

    assert not caller.is_alive()
    assert not concurrent_cleanup.is_alive()
    assert terminated == [current, receiver]


def test_cancel_during_receiver_cleanup_marks_batch_cancelled(tmp_path, monkeypatch):
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = DownloadRunner(config, tools)
    monkeypatch.setattr(runner, "_start_storescp", lambda _staging: None)
    monkeypatch.setattr(
        runner,
        "_download_one",
        lambda accession, *_args: AccessionResult(
            accession, AccessionStatus.COMPLETED
        ),
    )
    monkeypatch.setattr(runner, "_stop_storescp", runner.request_cancel)

    summary = runner.run(["A001"])

    assert summary.cancelled


def test_paused_runner_fails_if_storescp_exits(tmp_path):
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = DownloadRunner(config, tools)

    class Receiver:
        returncode = 9

        @staticmethod
        def poll():
            return 9

    runner._storescp_process = Receiver()  # type: ignore[assignment]
    runner.request_pause()

    with pytest.raises(RuntimeError, match="暂停期间 storescp 意外退出"):
        runner._wait_if_paused()
    runner._close_file_logger()


def test_ready_callback_is_not_called_when_storescp_fails(tmp_path, monkeypatch):
    config = AppConfig(dicom_destination_folder=str(tmp_path))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    ready = Mock()
    runner = DownloadRunner(config, tools, ready_callback=ready)
    monkeypatch.setattr(
        runner,
        "_start_storescp",
        Mock(side_effect=RuntimeError("receiver failed")),
    )

    with pytest.raises(RuntimeError, match="receiver failed"):
        runner.run(["ACC001"])

    ready.assert_not_called()


def test_ready_callback_runs_once_after_first_movescu_process_starts(
    tmp_path, monkeypatch
):
    config = AppConfig(dicom_destination_folder=str(tmp_path))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    ready = Mock()
    runner = DownloadRunner(config, tools, ready_callback=ready)

    class Process:
        pid = 123

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())
    monkeypatch.setattr(runner, "_notify_process", lambda *_args: None)

    assert runner._start_movescu_process(["movescu"]) is not None
    runner._current_process = None
    assert runner._start_movescu_process(["movescu"]) is not None

    ready.assert_called_once_with()
    runner._current_process = None
    runner._close_file_logger()


def test_pending_move_with_aborted_store_is_failed_and_retryable(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    logs: list[tuple[str, str, str]] = []
    runner = DownloadRunner(config, tools, log_callback=lambda *entry: logs.append(entry))

    class Process:
        stdout = iter(["I: Received Move Response 1 (Pending)\n"])

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            with runner._diagnostic_lock:
                runner._storescp_abort_count += 1
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())
    result = runner._download_one("FAILED001", staging, 1, 1)
    runner._close_file_logger()

    assert result.status == AccessionStatus.FAILED
    assert "待处理响应" in result.message
    assert "接收连接中止" not in result.message
    assert any("仅作为接收器警告" in message for _source, message, _level in logs)
    assert BatchSummary([result]).failed_accessions == ["FAILED001"]


@pytest.mark.parametrize(
    ("response_status", "has_problem"),
    [
        ("Success", False),
        ("Warning: SubOperationsCompleteOneOrMoreFailures", True),
        ("Failure", True),
    ],
)
def test_final_move_response_labels_are_classified(response_status, has_problem):
    diagnostics = core._MoveDiagnostics()

    core._record_move_diagnostic(
        diagnostics,
        f"I: Received Final Move Response ({response_status})",
    )

    assert diagnostics.final_response_status == response_status
    assert core._move_has_problem(diagnostics) is has_problem


def test_move_warning_with_failed_suboperations_is_partial(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = DownloadRunner(config, tools)

    class Process:
        stdout = iter(
            [
                "I: Received Move Response 1 (Pending)\n",
                "I: DIMSE Status : 0xFF00: Pending\n",
                "I: Number of Remaining SubOperations : 1\n",
                "I: Received Final Move Response "
                "(Warning: SubOperationsCompleteOneOrMoreFailures)\n",
                "I: DIMSE Status : 0xB000: "
                "SubOperationsCompleteOneOrMoreFailures\n",
                "I: Number of Remaining Sub-Operations : 0\n",
                "I: Number of Completed Sub-Operations : 1\n",
                "I: Number of Failed Sub-Operations : 2\n",
                "I: Number of Warning Sub-Operations : 0\n",
            ]
        )

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            _write_minimal_dicom(
                staging / "warning.dcm",
                "1.2.3.70",
                accession="WARN001",
            )
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())
    result = runner._download_one("WARN001", staging, 1, 1)
    runner._close_file_logger()

    assert result.status == AccessionStatus.PARTIAL
    assert result.file_count == 1
    assert "0xB000" in result.message
    assert "完成 1" in result.message
    assert "失败 2" in result.message
    assert "警告 0" in result.message


def test_move_failure_status_without_files_is_failed(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.6.9")
    runner = DownloadRunner(config, tools)

    class Process:
        stdout = iter(
            [
                "I: Received Move Response 1 (Failure)\n",
                "I: DIMSE STATUS = 0xa702: UnableToPerformSubOperations\n",
                "I: number of completed suboperations = 0\n",
                "I: number of failed suboperations = 3\n",
            ]
        )

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())
    result = runner._download_one("FAIL001", staging, 1, 1)
    runner._close_file_logger()

    assert result.status == AccessionStatus.FAILED
    assert "0xA702" in result.message
    assert "完成 0" in result.message
    assert "失败 3" in result.message
    assert "未收到文件" in result.message


def test_success_status_with_zero_failed_suboperations_remains_completed(
    tmp_path, monkeypatch
):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = DownloadRunner(config, tools)

    class Process:
        stdout = iter(
            [
                "I: Received Final Move Response (Success)\n",
                "I: DIMSE Status: 0x0000: Success\n",
                "I: Number of Completed Suboperations : 1\n",
                "I: Number of Failed Suboperations : 0\n",
                "I: Number of Warning Suboperations : 0\n",
            ]
        )

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            _write_minimal_dicom(
                staging / "success.dcm",
                "1.2.3.71",
                accession="SUCCESS001",
            )
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())
    result = runner._download_one("SUCCESS001", staging, 1, 1)
    runner._close_file_logger()

    assert result.status == AccessionStatus.COMPLETED
    assert result.file_count == 1


def test_success_with_completed_suboperations_but_no_archived_files_is_failed(
    tmp_path, monkeypatch
):
    staging = tmp_path / "staging"
    staging.mkdir()
    runner = DownloadRunner(
        AppConfig(dicom_destination_folder=str(tmp_path / "dicom")),
        ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0"),
    )

    class Process:
        stdout = iter(
            [
                "I: Received Final Move Response (Success)\n",
                "I: DIMSE Status: 0x0000: Success\n",
                "I: Number of Remaining Suboperations : 0\n",
                "I: Number of Completed Suboperations : 2\n",
                "I: Number of Failed Suboperations : 0\n",
            ]
        )

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())

    result = runner._download_one("MISSING001", staging, 1, 1)
    runner._close_file_logger()

    assert result.status == AccessionStatus.FAILED
    assert result.file_count == 0
    assert "PACS 报告完成 2 个子操作" in result.message
    assert "成功归档 0 个文件" in result.message


def test_success_with_fewer_archived_files_than_completed_suboperations_is_partial(
    tmp_path, monkeypatch
):
    staging = tmp_path / "staging"
    staging.mkdir()
    runner = DownloadRunner(
        AppConfig(dicom_destination_folder=str(tmp_path / "dicom")),
        ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0"),
    )

    class Process:
        stdout = iter(
            [
                "I: Received Final Move Response (Success)\n",
                "I: DIMSE Status: 0x0000: Success\n",
                "I: Number of Remaining Suboperations : 0\n",
                "I: Number of Completed Suboperations : 2\n",
                "I: Number of Failed Suboperations : 0\n",
            ]
        )

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            _write_minimal_dicom(
                staging / "one.dcm", "1.2.3.73", accession="PARTIAL002"
            )
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())

    result = runner._download_one("PARTIAL002", staging, 1, 1)
    runner._close_file_logger()

    assert result.status == AccessionStatus.PARTIAL
    assert result.file_count == 1
    assert "PACS 报告完成 2 个子操作" in result.message
    assert "成功归档 1 个文件" in result.message


def test_success_with_remaining_suboperations_is_partial(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    runner = DownloadRunner(
        AppConfig(dicom_destination_folder=str(tmp_path / "dicom")),
        ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0"),
    )

    class Process:
        stdout = iter(
            [
                "I: Received Final Move Response (Success)\n",
                "I: DIMSE Status: 0x0000: Success\n",
                "I: Number of Remaining Suboperations : 1\n",
                "I: Number of Completed Suboperations : 1\n",
                "I: Number of Failed Suboperations : 0\n",
            ]
        )

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            _write_minimal_dicom(
                staging / "one.dcm", "1.2.3.74", accession="REMAINING001"
            )
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())

    result = runner._download_one("REMAINING001", staging, 1, 1)
    runner._close_file_logger()

    assert result.status == AccessionStatus.PARTIAL
    assert result.file_count == 1
    assert "最终响应仍有 1 个子操作未完成" in result.message


def test_pending_without_final_response_with_files_is_partial(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.6.9")
    runner = DownloadRunner(config, tools)

    class Process:
        stdout = iter(
            [
                "I: Received Move Response 1 (Pending)\n",
                "I: DIMSE Status: 0xFF00: Pending\n",
            ]
        )

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            _write_minimal_dicom(
                staging / "pending.dcm",
                "1.2.3.72",
                accession="PENDING001",
            )
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())
    result = runner._download_one("PENDING001", staging, 1, 1)
    runner._close_file_logger()

    assert result.status == AccessionStatus.PARTIAL
    assert result.file_count == 1
    assert "1 次待处理响应后未返回最终响应" in result.message


def test_invalid_received_file_is_failed_and_left_in_staging(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = DownloadRunner(config, tools)

    class Process:
        stdout = iter(())

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            (staging / "broken.dcm").write_bytes(b"not dicom")
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())
    result = runner._download_one("BROKEN001", staging, 1, 1)
    runner._close_file_logger()

    assert result.status == AccessionStatus.FAILED
    assert "异常文件留在暂存目录" in result.message
    assert (staging / "broken.dcm").exists()


def test_received_accession_is_not_assigned_by_timing_and_is_recovered_later(
    tmp_path, monkeypatch
):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = DownloadRunner(config, tools)
    calls = 0

    class Process:
        stdout = iter(())

        @staticmethod
        def poll():
            return 0

        def wait(self):
            nonlocal calls
            calls += 1
            if calls == 1:
                _write_minimal_dicom(
                    staging / "foreign.dcm",
                    "1.2.3.60",
                    accession="A001",
                )
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())

    wrong_window = runner._download_one("B001", staging, 1, 2)
    recovered = runner._download_one("A001", staging, 2, 2)
    runner._close_file_logger()

    assert wrong_window.status == AccessionStatus.NO_DATA
    assert recovered.status == AccessionStatus.COMPLETED
    assert recovered.file_count == 1
    assert not list(staging.glob("*.dcm"))
    assert len(list((tmp_path / "dicom").rglob("*.dcm"))) == 1


def test_unchanged_staging_file_metadata_is_cached_between_accessions(
    tmp_path, monkeypatch
):
    import pydicom

    staging = tmp_path / "staging"
    staging.mkdir()
    foreign = staging / "foreign.dcm"
    _write_minimal_dicom(foreign, "1.2.3.61", accession="LATER")
    real_dcmread = pydicom.dcmread
    reads = 0

    def counted_dcmread(*args, **kwargs):
        nonlocal reads
        reads += 1
        return real_dcmread(*args, **kwargs)

    monkeypatch.setattr(pydicom, "dcmread", counted_dcmread)
    cache: dict[Path, tuple[int, int, int, str | None]] = {}

    selected, mismatched = core._select_files_for_accession(
        {foreign}, {foreign}, "EARLY", cache=cache
    )
    assert selected == []
    assert mismatched == [foreign]

    for index in range(100):
        selected, mismatched = core._select_files_for_accession(
            {foreign}, set(), f"OTHER-{index}", cache=cache
        )
        assert selected == []
        assert mismatched == []

    selected, mismatched = core._select_files_for_accession(
        {foreign}, set(), "LATER", cache=cache
    )

    assert selected == [foreign]
    assert mismatched == []
    assert reads == 1


def test_receiver_death_aborts_current_move_and_marks_failure(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = DownloadRunner(config, tools)

    class Receiver:
        @staticmethod
        def poll():
            return 9

    class MoveProcess:
        stdout = iter(())
        terminated = False

        def poll(self):
            return -15 if self.terminated else None

        @staticmethod
        def wait():
            return -15

    process = MoveProcess()
    runner._storescp_process = Receiver()  # type: ignore[assignment]
    monkeypatch.setattr(runner, "_popen", lambda _command: process)
    monkeypatch.setattr(
        core,
        "_terminate_process",
        lambda target: setattr(target, "terminated", True),
    )

    result = runner._download_one("A001", staging, 1, 1)
    runner._close_file_logger()

    assert process.terminated
    assert result.status == AccessionStatus.FAILED
    assert "storescp 意外退出" in result.message


def test_unrelated_store_abort_does_not_downgrade_successful_move(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    logs: list[tuple[str, str, str]] = []
    runner = DownloadRunner(config, tools, log_callback=lambda *entry: logs.append(entry))

    class Process:
        stdout = iter(
            [
                "I: Received Final Move Response (Success)\n",
                "I: DIMSE Status: 0x0000: Success\n",
                "I: Number of Remaining Suboperations : 0\n",
                "I: Number of Completed Suboperations : 1\n",
                "I: Number of Failed Suboperations : 0\n",
            ]
        )

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            _write_minimal_dicom(
                staging / "CT.1", "1.2.3.10", accession="PARTIAL001"
            )
            with runner._diagnostic_lock:
                runner._storescp_abort_count += 1
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())
    result = runner._download_one("PARTIAL001", staging, 1, 1)
    runner._close_file_logger()

    assert result.status == AccessionStatus.COMPLETED
    assert result.file_count == 1
    assert "接收连接中止" not in result.message
    assert any("仅作为接收器警告" in message for _source, message, _level in logs)
    assert result.archived_files == [
        str(next((tmp_path / "dicom").rglob("*.dcm")))
    ]


def test_batch_summary_exposes_only_exact_archived_files():
    summary = BatchSummary(
        [
            AccessionResult(
                "A001",
                AccessionStatus.COMPLETED,
                archived_files=["/data/a.dcm", "/data/b.dcm"],
            ),
            AccessionResult("A002", AccessionStatus.NO_DATA),
            AccessionResult(
                "A003",
                AccessionStatus.PARTIAL,
                archived_files=["/data/c.dcm"],
            ),
        ]
    )

    assert summary.archived_files == ["/data/a.dcm", "/data/b.dcm", "/data/c.dcm"]


def test_download_result_uses_received_bytes_and_transfer_time_for_speed(
    tmp_path, monkeypatch
):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = DownloadRunner(config, tools)

    class Process:
        stdout = iter(())

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            _write_minimal_dicom(staging / "CT.1", "1.2.3.20")
            return 0

    class Clock:
        monotonic = Mock(side_effect=[10.0, 12.0, 13.0])

        @staticmethod
        def sleep(_seconds):
            return None

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())
    monkeypatch.setattr(core, "time", Clock())

    result = runner._download_one("SPEED001", staging, 1, 1)
    runner._close_file_logger()

    archived = next((tmp_path / "dicom").rglob("*.dcm"))
    assert result.received_bytes == archived.stat().st_size
    assert result.speed_bytes_per_second == pytest.approx(result.received_bytes / 2)
    assert result.duration_seconds == pytest.approx(3.0)


def test_live_speed_updates_when_one_received_file_keeps_growing(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    progress: list[AccessionResult] = []
    runner = DownloadRunner(
        config,
        tools,
        progress_callback=lambda _index, _total, result: progress.append(result),
    )

    class Process:
        stdout = iter(())
        polls = 0

        @classmethod
        def poll(cls):
            cls.polls += 1
            if cls.polls == 1:
                _write_minimal_dicom(staging / "CT.1", "1.2.3.21")
                return None
            if cls.polls == 2:
                return None
            if cls.polls == 3:
                with (staging / "CT.1").open("ab") as handle:
                    handle.write(b"\0" * 1024)
                return None
            return 0

        @staticmethod
        def wait():
            return 0

    class Clock:
        monotonic = Mock(side_effect=[10.0, 10.1, 10.6, 11.2, 11.4, 11.5])

        @staticmethod
        def sleep(_seconds):
            return None

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())
    monkeypatch.setattr(core, "time", Clock())

    result = runner._download_one("GROWING001", staging, 1, 1)
    runner._close_file_logger()

    downloading = [item for item in progress if item.status == AccessionStatus.DOWNLOADING]
    assert len(downloading) == 2
    assert downloading[0].file_count == downloading[1].file_count == 1
    assert downloading[1].received_bytes > downloading[0].received_bytes
    assert downloading[1].speed_bytes_per_second > 0
    assert result.status == AccessionStatus.COMPLETED


def test_live_speed_uses_incremental_tracker_and_only_full_scans_at_boundaries(
    tmp_path, monkeypatch
):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    progress: list[AccessionResult] = []
    runner = DownloadRunner(
        config,
        tools,
        progress_callback=lambda _index, _total, result: progress.append(result),
    )
    files_in = Mock(return_value=set())

    class Process:
        stdout = iter(())
        polls = 0

        @classmethod
        def poll(cls):
            cls.polls += 1
            return None if cls.polls <= 6 else 0

        @staticmethod
        def wait():
            return 0

    class Clock:
        monotonic = Mock(
            side_effect=[10.0, 10.1, 10.2, 10.3, 10.4, 10.49, 10.5, 10.6, 10.7]
        )

        @staticmethod
        def sleep(_seconds):
            return None

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())
    monkeypatch.setattr(core, "_files_in", files_in)
    monkeypatch.setattr(core, "time", Clock())

    result = runner._download_one("SAMPLED001", staging, 1, 1)
    runner._close_file_logger()

    assert files_in.call_count == 2  # initial baseline and authoritative final snapshot
    assert len(progress) == 1
    assert result.status == AccessionStatus.NO_DATA


def test_live_tracker_stops_statting_all_stable_files_and_adapts_interval(
    tmp_path, monkeypatch
):
    staging = tmp_path / "staging"
    staging.mkdir()
    for index in range(1_000):
        (staging / f"{index:04d}.dcm").write_bytes(b"x")

    real_stat = Path.stat
    stat_calls = 0

    def counted_stat(path, *args, **kwargs):
        nonlocal stat_calls
        if path.parent == staging:
            stat_calls += 1
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counted_stat)
    tracker = core._LiveStagingTracker(staging, set())

    assert tracker.sample() == (1_000, 1_000)
    first_sample_calls = stat_calls
    assert first_sample_calls == 1_000
    assert tracker.sample_interval_seconds == 1.0

    tracker.sample()
    second_sample_calls = stat_calls - first_sample_calls
    assert second_sample_calls == 1_000

    before_third = stat_calls
    tracker.sample()
    assert stat_calls - before_third <= tracker._RECENT_FILE_LIMIT


def test_progress_callback_failure_terminates_movescu_and_joins_reader(
    tmp_path, monkeypatch
):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")

    def fail_progress(_index, _total, _result):
        raise RuntimeError("progress failed")

    runner = DownloadRunner(config, tools, progress_callback=fail_progress)
    reader = Mock()

    class Process:
        stdout = iter(())
        alive = True

        @classmethod
        def poll(cls):
            return None if cls.alive else 0

    process = Process()

    def terminate(target):
        assert target is process
        Process.alive = False

    class Clock:
        monotonic = Mock(side_effect=[10.0, 10.5])

        @staticmethod
        def sleep(_seconds):
            return None

    monkeypatch.setattr(runner, "_popen", lambda _command: process)
    monkeypatch.setattr(runner, "_start_reader", Mock(return_value=reader))
    terminate_process = Mock(side_effect=terminate)
    monkeypatch.setattr(core, "_terminate_process", terminate_process)
    monkeypatch.setattr(core, "time", Clock())

    with pytest.raises(RuntimeError, match="progress failed"):
        runner._download_one("CALLBACK001", staging, 1, 1)
    runner._close_file_logger()

    terminate_process.assert_called_once_with(process)
    reader.join.assert_called_once_with()
    assert runner._current_process is None


def test_movescu_pending_diagnostics_are_isolated_per_process(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = DownloadRunner(config, tools)

    class Process:
        stdout = iter(())

        def __init__(self, has_pending: bool):
            self.has_pending = has_pending

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            return 0

    processes = [Process(True), Process(False)]
    monkeypatch.setattr(runner, "_popen", Mock(side_effect=processes))

    def start_reader(process, source, diagnostics):
        assert source == "movescu"
        reader = Mock()

        def finish_reading():
            if process.has_pending:
                diagnostics.pending_responses += 1

        reader.join.side_effect = finish_reading
        return reader

    monkeypatch.setattr(runner, "_start_reader", start_reader)

    first = runner._download_one("PENDING001", staging, 1, 2)
    second = runner._download_one("EMPTY002", staging, 2, 2)
    runner._close_file_logger()

    assert first.status == AccessionStatus.FAILED
    assert "1 次待处理响应" in first.message
    assert second.status == AccessionStatus.NO_DATA


def test_movescu_process_callback_tracks_start_and_stop(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    events = []
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = DownloadRunner(
        config,
        tools,
        process_callback=lambda *event: events.append(event),
    )

    class Process:
        pid = 123
        stdout = iter(())

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())

    result = runner._download_one("A001", staging, 1, 1)

    assert result.status == AccessionStatus.NO_DATA
    assert events == [
        ("movescu", 123, "movescu", True),
        ("movescu", 123, "movescu", False),
    ]


def test_windows_process_cleanup_kills_the_full_process_tree(monkeypatch):
    process = Mock(pid=4321)
    process.poll.return_value = None
    run = Mock()
    monkeypatch.setattr(core.os, "name", "nt")
    monkeypatch.setattr(core.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(core.subprocess, "run", run)

    core._terminate_process(process)

    assert run.call_args.args[0] == ["taskkill", "/PID", "4321", "/T", "/F"]
    assert run.call_args.kwargs["timeout"] == 3
    process.wait.assert_called_once_with(timeout=3)


def test_windows_taskkill_timeout_falls_back_to_direct_kill(monkeypatch):
    process = Mock(pid=4321)
    process.poll.return_value = None
    run = Mock(side_effect=subprocess.TimeoutExpired("taskkill", 3))
    monkeypatch.setattr(core.os, "name", "nt")
    monkeypatch.setattr(core.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(core.subprocess, "run", run)

    core._terminate_process(process)

    assert run.call_args.kwargs["timeout"] == 3
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=3)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups only")
def test_posix_cleanup_kills_fork_child_after_group_leader_exits(tmp_path):
    import psutil

    child_pid_file = tmp_path / "child.pid"
    leader_code = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)']); "
        "open(sys.argv[1], 'w').write(str(child.pid))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", leader_code, str(child_pid_file)],
        start_new_session=True,
    )
    setattr(process, "_dcmget_process_group", process.pid)
    process.wait(timeout=5)
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))

    try:
        os.kill(child_pid, 0)
        child_was_running = True
    except OSError:
        child_was_running = False

    try:
        core._terminate_process(process)
        assert not psutil.pid_exists(child_pid) or (
            psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
        )
    finally:
        if child_was_running:
            try:
                os.kill(child_pid, 9)
            except OSError:
                pass

    assert child_was_running


def test_stop_storescp_cleans_group_even_when_leader_already_exited(
    tmp_path, monkeypatch
):
    runner = DownloadRunner(
        AppConfig(dicom_destination_folder=str(tmp_path)),
        ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0"),
    )
    process = Mock(pid=1234)
    process.poll.return_value = 0
    runner._storescp_process = process
    terminate = Mock()
    monkeypatch.setattr(core, "_terminate_process", terminate)

    runner._stop_storescp()
    runner._close_file_logger()

    terminate.assert_called_once_with(process)
    assert runner._storescp_process is None


def test_safe_accession_directory_is_cross_platform():
    result = safe_accession_dir(' A/B:C*D?"E<F>G| ')
    assert not set('/\\:*?"<>|') & set(result)
    assert result.startswith("A_B_C_D__E_F_G_")


@pytest.mark.parametrize("reserved", ["CON", "nul.txt", "COM1", "lpt9.dcm"])
def test_path_components_avoid_windows_reserved_device_names(reserved):
    result = core._safe_path_component(reserved, "fallback")

    assert result.startswith("_")
    assert result.split(".", 1)[0].upper() not in {
        "CON",
        "NUL",
        "COM1",
        "LPT9",
    }


def test_directory_template_does_not_reinterpret_metadata_placeholders(tmp_path):
    rendered = core._render_directory_template(
        tmp_path,
        "{PatientID}/{AccessionNumber}/{StudyInstanceUID}",
        {
            "PatientID": "{AccessionNumber}",
            "AccessionNumber": "ACC9",
            "StudyInstanceUID": "1.2.3",
        },
    )

    assert rendered.relative_to(tmp_path).parts == (
        "{AccessionNumber}",
        "ACC9",
        "1.2.3",
    )


def test_common_output_directory_covers_multiple_studies(tmp_path):
    root = tmp_path / "dicom"
    files = [
        root / "PAT1" / "ACC1" / "1.2.3" / "one.dcm",
        root / "PAT1" / "ACC1" / "1.2.4" / "two.dcm",
    ]

    assert core._common_output_directory(files, root) == root / "PAT1" / "ACC1"


def _write_minimal_dicom(
    path: Path,
    sop_instance_uid: str,
    *,
    accession: str = "",
) -> None:
    metadata = FileMetaDataset()
    metadata.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(path, {}, file_meta=metadata, preamble=b"\0" * 128)
    dataset.SOPInstanceUID = sop_instance_uid
    if accession:
        dataset.AccessionNumber = accession
    dataset.save_as(path)
