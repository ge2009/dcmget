from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import Mock

import pytest

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

    assert store[:7] == [
        str(tools.storescp),
        "-v",
        "-aet",
        "STORAGE",
        "+xa",
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


def test_file_archive_preserves_received_files(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    first = staging / "CT.1"
    second = staging / "MR.2"
    first.write_bytes(b"one")
    second.write_bytes(b"two")

    moved = core._move_files([first, second], tmp_path / "ACC001")

    assert [path.name for path in moved] == ["CT.1", "MR.2"]
    assert not first.exists()
    assert (tmp_path / "ACC001" / "CT.1").read_bytes() == b"one"


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


def test_safe_accession_directory_is_cross_platform():
    result = safe_accession_dir(' A/B:C*D?"E<F>G| ')
    assert not set('/\\:*?"<>|') & set(result)
    assert result.startswith("A_B_C_D__E_F_G_")
