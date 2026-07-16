from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import DICOM_download_script as cli
from dcmget.config import AppConfig
from dcmget.core import AccessionResult, AccessionStatus, BatchSummary, PreflightResult, ToolPaths
from dcmget.licensing import LicenseError, TrialInfo
from dcmget.pdi import PdiExportResult, PdiStatus
from dcmget.task_state import TaskCheckpointStore


def test_cli_legacy_password_is_ignored_for_remaining_trial(monkeypatch):
    monkeypatch.setattr(
        cli,
        "load_license",
        Mock(side_effect=LicenseError("尚未注册")),
    )
    monkeypatch.setattr(
        cli,
        "trial_status",
        lambda: TrialInfo(used=1, remaining=29),
    )

    assert cli.authorize_cli("不是当天日期", None) == "trial"


def test_cli_resume_remains_authorized_after_last_trial(monkeypatch):
    task_id = "a" * 32
    monkeypatch.setattr(
        cli,
        "load_license",
        Mock(side_effect=LicenseError("尚未注册")),
    )
    monkeypatch.setattr(
        cli,
        "trial_status",
        lambda: TrialInfo(used=30, remaining=0),
    )
    monkeypatch.setattr(cli, "trial_task_consumed", lambda value: value == task_id)

    assert cli.authorize_cli("任意旧口令", None, task_id) == "trial"


def _run_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pdi_enabled: bool,
    pdi_status: PdiStatus = PdiStatus.COMPLETED,
    core_tool_failure: bool = False,
    download_status: AccessionStatus = AccessionStatus.COMPLETED,
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
    )
    summary = BatchSummary(
        [
            AccessionResult(
                "ACC001",
                download_status,
                archived_files=(
                    [str(archived)]
                    if download_status
                    in {AccessionStatus.COMPLETED, AccessionStatus.PARTIAL}
                    else []
                ),
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

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--password",
            "ignored",
            "--task-state",
            str(tmp_path / "active-task.sqlite3"),
        ]
    )
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


@pytest.mark.parametrize("pdi_enabled", [False, True])
def test_cli_download_failure_keeps_retry_checkpoint_and_skips_pdi(
    tmp_path, monkeypatch, pdi_enabled
):
    exit_code, exporter_factory = _run_cli(
        tmp_path,
        monkeypatch,
        pdi_enabled=pdi_enabled,
        download_status=AccessionStatus.FAILED,
    )

    assert exit_code == 2
    exporter_factory.assert_not_called()
    checkpoint = TaskCheckpointStore(
        tmp_path / "active-task.sqlite3"
    ).load_required()
    assert checkpoint.phase == "download_retryable"
    assert [result.accession for result in checkpoint.results] == ["ACC001"]


def test_cli_returns_one_when_pdi_core_tool_cannot_start(tmp_path, monkeypatch):
    exit_code, _exporter_factory = _run_cli(
        tmp_path,
        monkeypatch,
        pdi_enabled=True,
        pdi_status=PdiStatus.FAILED,
        core_tool_failure=True,
    )

    assert exit_code == 1


def test_cli_restart_only_runs_pending_accessions(tmp_path, monkeypatch):
    state_path = tmp_path / "active-task.sqlite3"
    store = TaskCheckpointStore(state_path)
    checkpoint = store.start(
        AppConfig(dicom_destination_folder=str(tmp_path / "dicom")),
        ["A001", "A002"],
        trial_required=False,
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult("A001", AccessionStatus.COMPLETED),
    )
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = Mock()
    runner.run.return_value = BatchSummary(
        [AccessionResult("A002", AccessionStatus.COMPLETED)]
    )
    monkeypatch.setattr(cli, "authorize_cli", lambda *_args: "licensed")
    monkeypatch.setattr(
        cli,
        "preflight",
        lambda *_args: PreflightResult(tools, {}, [("DCMTK", True, "就绪")]),
    )
    monkeypatch.setattr(cli, "DownloadRunner", Mock(return_value=runner))

    exit_code = cli.main(
        ["--password", "ignored", "--task-state", str(state_path)]
    )

    assert exit_code == 0
    runner.run.assert_called_once_with(["A002"])
    assert store.load() is None


def test_cli_restart_retries_only_failed_and_partial_results(tmp_path, monkeypatch):
    state_path = tmp_path / "active-task.sqlite3"
    store = TaskCheckpointStore(state_path)
    checkpoint = store.start(
        AppConfig(dicom_destination_folder=str(tmp_path / "dicom")),
        ["DONE", "FAILED", "PARTIAL"],
        trial_required=False,
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult("DONE", AccessionStatus.COMPLETED),
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult("FAILED", AccessionStatus.FAILED),
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "PARTIAL",
            AccessionStatus.PARTIAL,
            archived_files=[str(tmp_path / "dicom" / "kept.dcm")],
        ),
    )
    store.set_phase(checkpoint.task_id, "download_retryable")
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = Mock()
    runner.run.return_value = BatchSummary(
        [
            AccessionResult("FAILED", AccessionStatus.COMPLETED),
            AccessionResult("PARTIAL", AccessionStatus.COMPLETED),
        ]
    )
    monkeypatch.setattr(cli, "authorize_cli", lambda *_args: "licensed")
    monkeypatch.setattr(
        cli,
        "preflight",
        lambda *_args: PreflightResult(tools, {}, [("DCMTK", True, "就绪")]),
    )
    monkeypatch.setattr(cli, "DownloadRunner", Mock(return_value=runner))

    exit_code = cli.main(
        ["--password", "ignored", "--task-state", str(state_path)]
    )

    assert exit_code == 0
    runner.run.assert_called_once_with(["FAILED", "PARTIAL"])
    assert store.load() is None


def test_cli_can_accept_download_failures_and_continue_pdi(tmp_path, monkeypatch):
    state_path = tmp_path / "active-task.sqlite3"
    archived = tmp_path / "dicom" / "DONE.dcm"
    store = TaskCheckpointStore(state_path)
    checkpoint = store.start(
        AppConfig(
            dicom_destination_folder=str(tmp_path / "dicom"),
            pdi_export_enabled=True,
            pdi_institution_name="测试医院",
        ),
        ["DONE", "FAILED"],
        trial_required=False,
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "DONE",
            AccessionStatus.COMPLETED,
            file_count=1,
            archived_files=[str(archived)],
        ),
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult("FAILED", AccessionStatus.FAILED),
    )
    store.set_phase(checkpoint.task_id, "download_retryable")
    tools = ToolPaths(
        Path("movescu"),
        Path("storescp"),
        Path("."),
        "3.7.0",
        dcmmkdir=Path("dcmmkdir"),
    )
    resolver = Mock()
    resolver.resolve.return_value = tools
    exporter = Mock()
    exporter.export.return_value = PdiExportResult(PdiStatus.COMPLETED)
    exporter_factory = Mock(return_value=exporter)
    monkeypatch.setattr(cli, "authorize_cli", lambda *_args: "licensed")
    monkeypatch.setattr(cli, "DcmtkResolver", Mock(return_value=resolver))
    runner_factory = Mock()
    monkeypatch.setattr(cli, "DownloadRunner", runner_factory)
    monkeypatch.setattr(cli, "PdiExporter", exporter_factory)

    exit_code = cli.main(
        [
            "--password",
            "ignored",
            "--task-state",
            str(state_path),
            "--accept-download-failures",
        ]
    )

    assert exit_code == 2
    runner_factory.assert_not_called()
    exporter.export.assert_called_once_with([str(archived)])
    assert store.load() is None


def test_cli_restart_can_retry_pdi_without_running_download(tmp_path, monkeypatch):
    state_path = tmp_path / "active-task.sqlite3"
    archived = tmp_path / "dicom" / "A001.dcm"
    store = TaskCheckpointStore(state_path)
    checkpoint = store.start(
        AppConfig(
            dicom_destination_folder=str(tmp_path / "dicom"),
            pdi_export_enabled=True,
            pdi_institution_name="测试医院",
        ),
        ["A001"],
        trial_required=False,
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.COMPLETED,
            archived_files=[str(archived)],
        ),
    )
    store.set_phase(checkpoint.task_id, "pdi_retryable")
    tools = ToolPaths(
        Path("movescu"),
        Path("storescp"),
        Path("."),
        "3.7.0",
        dcmmkdir=Path("dcmmkdir"),
    )
    resolver = Mock()
    resolver.resolve.return_value = tools
    monkeypatch.setattr(cli, "authorize_cli", lambda *_args: "licensed")
    monkeypatch.setattr(cli, "DcmtkResolver", Mock(return_value=resolver))
    runner_factory = Mock()
    monkeypatch.setattr(cli, "DownloadRunner", runner_factory)
    exporter = Mock()
    exporter.export.return_value = PdiExportResult(PdiStatus.COMPLETED)
    exporter_factory = Mock(return_value=exporter)
    monkeypatch.setattr(cli, "PdiExporter", exporter_factory)

    exit_code = cli.main(
        ["--password", "ignored", "--task-state", str(state_path)]
    )

    assert exit_code == 0
    runner_factory.assert_not_called()
    exporter.export.assert_called_once_with([str(archived)])
    assert store.load() is None


def test_cli_cancel_does_not_persist_unstarted_large_batch_placeholders(
    tmp_path, monkeypatch
):
    values = [f"A{index:05d}" for index in range(40_000)]
    accessions = tmp_path / "access.txt"
    accessions.write_text("\n".join(values), encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "config_version": 4,
                "access_numbers_file_path": str(accessions),
                "dicom_destination_folder": str(tmp_path / "dicom"),
            }
        ),
        encoding="utf-8",
    )
    tools = ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0")
    runner = Mock()
    runner.run.return_value = BatchSummary(
        [
            AccessionResult(value, AccessionStatus.CANCELLED)
            for value in values
        ],
        cancelled=True,
    )
    record_result = Mock(wraps=TaskCheckpointStore.record_result)
    monkeypatch.setattr(TaskCheckpointStore, "record_result", record_result)
    monkeypatch.setattr(cli, "authorize_cli", lambda *_args: "licensed")
    monkeypatch.setattr(
        cli,
        "preflight",
        lambda *_args: PreflightResult(tools, {}, [("DCMTK", True, "就绪")]),
    )
    monkeypatch.setattr(cli, "DownloadRunner", Mock(return_value=runner))

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--password",
            "ignored",
            "--task-state",
            str(tmp_path / "active-task.sqlite3"),
        ]
    )

    assert exit_code == 130
    record_result.assert_not_called()


def test_cli_explicit_discard_can_replace_a_corrupt_checkpoint(tmp_path, monkeypatch):
    accessions = tmp_path / "access.txt"
    accessions.write_text("A001\n", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "config_version": 4,
                "access_numbers_file_path": str(accessions),
                "dicom_destination_folder": str(tmp_path / "dicom"),
            }
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "active-task.sqlite3"
    state_path.write_bytes(b"not a sqlite database")
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

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--password",
            "ignored",
            "--discard-checkpoint",
            "--task-state",
            str(state_path),
        ]
    )

    assert exit_code == 0
    assert not state_path.exists()
