from __future__ import annotations

import socket
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

    assert [path.name for path in moved] == ["CT.1.dcm"]
    assert rejected == []
    assert not first.exists()
    target = tmp_path / "dicom" / "PAT001" / "ACC001" / "1.2.3.4" / "CT.1.dcm"
    assert target.exists()
    assert target.read_bytes()[128:132] == b"DICM"


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
    assert core.log_directory(regular) == tmp_path / "dicom" / "logs"


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

    assert [path.name for path in moved] == ["MR.1.dcm", "MR.1-1.dcm"]
    assert rejected == []
    assert all(path.suffix == ".dcm" for path in moved)
    assert all("ACC_002" in str(path) for path in moved)


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


def test_pending_move_with_aborted_store_is_failed_and_retryable(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = DownloadRunner(config, tools)

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
    assert "接收连接中止" in result.message
    assert BatchSummary([result]).failed_accessions == ["FAILED001"]


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


def test_received_files_with_aborted_store_are_partial(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = DownloadRunner(config, tools)

    class Process:
        stdout = iter(["I: Received Move Response 1 (Pending)\n"])

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            _write_minimal_dicom(staging / "CT.1", "1.2.3.10")
            with runner._diagnostic_lock:
                runner._storescp_abort_count += 1
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())
    result = runner._download_one("PARTIAL001", staging, 1, 1)
    runner._close_file_logger()

    assert result.status == AccessionStatus.PARTIAL
    assert result.file_count == 1
    assert "接收连接中止" in result.message


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
                with (staging / "CT.1").open("ab") as handle:
                    handle.write(b"\0" * 1024)
                return None
            return 0

        @staticmethod
        def wait():
            return 0

    class Clock:
        monotonic = Mock(side_effect=[10.0, 10.1, 10.7, 11.0, 11.2])

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


def test_windows_process_cleanup_kills_the_full_process_tree(monkeypatch):
    process = Mock(pid=4321)
    process.poll.return_value = None
    run = Mock()
    monkeypatch.setattr(core.os, "name", "nt")
    monkeypatch.setattr(core.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(core.subprocess, "run", run)

    core._terminate_process(process)

    assert run.call_args.args[0] == ["taskkill", "/PID", "4321", "/T", "/F"]
    process.wait.assert_called_once_with(timeout=3)


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


def _write_minimal_dicom(path: Path, sop_instance_uid: str) -> None:
    metadata = FileMetaDataset()
    metadata.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(path, {}, file_meta=metadata, preamble=b"\0" * 128)
    dataset.SOPInstanceUID = sop_instance_uid
    dataset.save_as(path)
