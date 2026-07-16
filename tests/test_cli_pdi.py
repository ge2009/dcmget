from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import DICOM_download_script as cli
from dcmget.core import AccessionResult, AccessionStatus, BatchSummary, PreflightResult, ToolPaths
from dcmget.pdi import PdiExportResult, PdiStatus


def _run_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pdi_enabled: bool,
    pdi_status: PdiStatus = PdiStatus.COMPLETED,
    core_tool_failure: bool = False,
) -> tuple[int, Mock]:
    accessions = tmp_path / "access.txt"
    accessions.write_text("ACC001\n", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "config_version": 4,
                "access_numbers_file_path": str(accessions),
                "dicom_destination_folder": str(tmp_path / "dicom"),
                "pdi_export_enabled": pdi_enabled,
                "pdi_institution_name": "测试医院" if pdi_enabled else "",
            }
        ),
        encoding="utf-8",
    )
    archived = tmp_path / "dicom" / "ACC001.dcm"
    tools = ToolPaths(
        Path("movescu"),
        Path("storescp"),
        Path("."),
        "3.7.0",
        dcmmkdir=Path("dcmmkdir"),
        dcmj2pnm=Path("dcmj2pnm"),
    )
    summary = BatchSummary(
        [
            AccessionResult(
                "ACC001",
                AccessionStatus.COMPLETED,
                archived_files=[str(archived)],
            )
        ]
    )
    runner = Mock()
    runner.run.return_value = summary
    monkeypatch.setattr(cli, "authorize_cli", lambda *_args: "licensed")
    monkeypatch.setattr(
        cli,
        "preflight",
        lambda *_args: PreflightResult(
            tools,
            {},
            [("DCMTK 工具", True, "已就绪")],
        ),
    )
    monkeypatch.setattr(cli, "DownloadRunner", Mock(return_value=runner))
    exporter = Mock()
    exporter.export.return_value = PdiExportResult(
        pdi_status,
        output_directory=str(tmp_path / "PDI") if pdi_status != PdiStatus.FAILED else "",
        core_tool_failure=core_tool_failure,
    )
    exporter_factory = Mock(return_value=exporter)
    monkeypatch.setattr(cli, "PdiExporter", exporter_factory)

    exit_code = cli.main(["--config", str(config), "--password", "ignored"])
    return exit_code, exporter_factory


def test_cli_runs_pdi_after_download_without_consuming_another_trial(tmp_path, monkeypatch):
    exit_code, exporter_factory = _run_cli(
        tmp_path, monkeypatch, pdi_enabled=True
    )

    assert exit_code == 0
    exporter = exporter_factory.return_value
    exporter.export.assert_called_once_with([str(tmp_path / "dicom" / "ACC001.dcm")])


@pytest.mark.parametrize("status", [PdiStatus.PARTIAL, PdiStatus.FAILED])
def test_cli_returns_two_for_incomplete_pdi_export(tmp_path, monkeypatch, status):
    exit_code, _exporter_factory = _run_cli(
        tmp_path, monkeypatch, pdi_enabled=True, pdi_status=status
    )

    assert exit_code == 2


def test_cli_does_not_create_pdi_exporter_when_feature_is_disabled(tmp_path, monkeypatch):
    exit_code, exporter_factory = _run_cli(
        tmp_path, monkeypatch, pdi_enabled=False
    )

    assert exit_code == 0
    exporter_factory.assert_not_called()


def test_cli_returns_one_when_pdi_core_tool_cannot_start(tmp_path, monkeypatch):
    exit_code, _exporter_factory = _run_cli(
        tmp_path,
        monkeypatch,
        pdi_enabled=True,
        pdi_status=PdiStatus.FAILED,
        core_tool_failure=True,
    )

    assert exit_code == 1
