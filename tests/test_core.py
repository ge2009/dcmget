from __future__ import annotations

import socket
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
