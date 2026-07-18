from __future__ import annotations

import os
import socket
from pathlib import Path

from dcmget.config import AppConfig
from dcmget.core import ToolPaths
from dcmget.health import run_health_check


class _Resolver:
    def __init__(self, tools: ToolPaths | None = None, error: Exception | None = None):
        self.tools = tools
        self.error = error

    def resolve(self, _configured: str) -> ToolPaths:
        if self.error is not None:
            raise self.error
        assert self.tools is not None
        return self.tools


class _Process:
    def __init__(self, pid: int, name: str, status: str = "running"):
        self.info = {"pid": pid, "name": name, "status": status}


def _tools(tmp_path: Path, *, include_pdi: bool = True) -> ToolPaths:
    tmp_path.mkdir(parents=True, exist_ok=True)
    names = ["movescu", "storescp"]
    if include_pdi:
        names.extend(("dcmmkdir", "dcmdump"))
    paths = {}
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"tool")
        paths[name] = path
    return ToolPaths(
        paths["movescu"],
        paths["storescp"],
        tmp_path,
        "3.7.0",
        dcmmkdir=paths.get("dcmmkdir"),
        dcmdump=paths.get("dcmdump"),
    )


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_health_report_covers_runtime_tools_destination_port_and_processes(tmp_path):
    port = _free_port()
    config = AppConfig(
        dicom_destination_folder=str(tmp_path / "dicom"),
        storage_port=port,
    )
    current = _Process(os.getpid(), "python")
    receiver = _Process(4567, "storescp")

    report = run_health_check(
        config,
        resolver=_Resolver(_tools(tmp_path / "tools")),
        minimum_free_bytes=0,
        process_iter=lambda _attrs: [current, receiver, _Process(8, "unrelated")],
    )
    payload = report.to_dict()

    assert payload["schema"] == "dcmget-health"
    assert payload["schema_version"] == 1
    assert payload["status"] == "ok"
    checks = {item["id"]: item for item in payload["checks"]}
    assert set(checks) == {
        "runtime",
        "dcmtk",
        "destination",
        "receiver_port",
        "processes",
    }
    assert checks["runtime"]["details"]["pointer_bits"] == 64
    assert checks["dcmtk"]["details"]["version"] == "3.7.0"
    assert checks["destination"]["details"]["writable"] is True
    assert checks["destination"]["details"]["disk_free_bytes"] > 0
    assert checks["receiver_port"]["details"]["available"] is True
    assert [item["name"] for item in checks["processes"]["details"]["processes"]] == [
        "python",
        "storescp",
    ]


def test_health_requires_pdi_tools_only_when_pdi_is_enabled(tmp_path):
    resolver = _Resolver(_tools(tmp_path / "tools", include_pdi=False))
    config = AppConfig(
        dicom_destination_folder=str(tmp_path / "dicom"),
        storage_port=_free_port(),
        pdi_export_enabled=True,
        pdi_institution_name="医院",
    )

    report = run_health_check(
        config,
        resolver=resolver,
        minimum_free_bytes=0,
        process_iter=lambda _attrs: [],
    )
    dcmtk = next(check for check in report.checks if check.check_id == "dcmtk")

    assert report.status == "error"
    assert dcmtk.status == "error"
    assert dcmtk.details["missing"] == ["dcmdump", "dcmmkdir"]


def test_health_reports_occupied_port_and_low_disk_as_structured_results(tmp_path):
    listener = socket.socket()
    listener.bind(("0.0.0.0", 0))
    port = listener.getsockname()[1]
    try:
        report = run_health_check(
            AppConfig(
                dicom_destination_folder=str(tmp_path / "dicom"),
                storage_port=port,
            ),
            resolver=_Resolver(_tools(tmp_path / "tools")),
            minimum_free_bytes=2**63,
            process_iter=lambda _attrs: [],
        )
    finally:
        listener.close()

    checks = {check.check_id: check for check in report.checks}
    assert report.status == "error"
    assert checks["receiver_port"].status == "error"
    assert checks["destination"].status == "warning"


def test_health_converts_resolver_failure_to_error_check(tmp_path):
    report = run_health_check(
        AppConfig(
            dicom_destination_folder=str(tmp_path / "dicom"),
            storage_port=_free_port(),
        ),
        resolver=_Resolver(error=FileNotFoundError("missing tools")),
        minimum_free_bytes=0,
        process_iter=lambda _attrs: [],
    )

    dcmtk = next(check for check in report.checks if check.check_id == "dcmtk")
    assert dcmtk.status == "error"
    assert "missing tools" in dcmtk.summary


def test_health_optionally_checks_pacs_tcp_reachability(tmp_path):
    calls = []

    class Connection:
        closed = False

        def close(self):
            self.closed = True

    connection = Connection()

    def connect(address, *, timeout):
        calls.append((address, timeout))
        return connection

    report = run_health_check(
        AppConfig(
            dicom_destination_folder=str(tmp_path / "dicom"),
            storage_port=_free_port(),
            pacs_server_ip="192.0.2.10",
            pacs_server_port=104,
        ),
        resolver=_Resolver(_tools(tmp_path / "tools")),
        minimum_free_bytes=0,
        process_iter=lambda _attrs: [],
        check_pacs=True,
        pacs_timeout_seconds=1.5,
        connection_factory=connect,
    )

    checks = {check.check_id: check for check in report.checks}
    assert checks["pacs_tcp"].status == "ok"
    assert checks["pacs_tcp"].details["scope"] == "tcp_only"
    assert calls == [(('192.0.2.10', 104), 1.5)]
    assert connection.closed
