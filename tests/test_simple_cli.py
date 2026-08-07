from __future__ import annotations

from pathlib import Path

import pytest

import DICOM_download_cli as simple_cli
from dcmget.config import AppConfig, load_config, save_config


def _runtime_roots(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    config_root = tmp_path / "roaming" / "DcmGet"
    state_root = tmp_path / "local" / "DcmGet"
    monkeypatch.setattr(
        simple_cli,
        "default_config_path",
        lambda: config_root / "config.json",
    )
    monkeypatch.setattr(simple_cli, "application_state_dir", lambda: state_root)
    monkeypatch.setattr(
        simple_cli,
        "ensure_default_config",
        lambda: config_root / "config.json",
    )
    return config_root, state_root


def test_simple_cli_uses_profile_one_and_disables_pdi(tmp_path, monkeypatch):
    config_root, state_root = _runtime_roots(tmp_path, monkeypatch)
    source_config = config_root / "instances" / "i1" / "config.json"
    save_config(
        source_config,
        AppConfig(
            pacs_server_ip="192.0.2.10",
            pdi_export_enabled=True,
        ),
    )
    accessions = tmp_path / "access.txt"
    accessions.write_text("ACC001\n", encoding="utf-8")
    captured: list[str] = []
    monkeypatch.setattr(
        simple_cli.DICOM_download_script,
        "main",
        lambda arguments: captured.extend(arguments) or 0,
    )
    monkeypatch.setattr(simple_cli, "configure_packaged_dcmtk", lambda: None)

    assert simple_cli.main([str(accessions)]) == 0

    config_path = Path(captured[captured.index("--config") + 1])
    task_state = Path(captured[captured.index("--task-state") + 1])
    assert captured[captured.index("--accessions") + 1] == str(accessions)
    assert config_path.parent == task_state.parent
    assert config_path.is_relative_to(state_root / simple_cli.CLI_STATE_DIRECTORY)
    runtime_config = load_config(config_path)
    assert runtime_config.pacs_server_ip == "192.0.2.10"
    assert runtime_config.pdi_export_enabled is False
    assert load_config(source_config).pdi_export_enabled is True


def test_simple_cli_explicit_profile_must_exist(tmp_path, monkeypatch, capsys):
    _runtime_roots(tmp_path, monkeypatch)
    accessions = tmp_path / "access.txt"
    accessions.write_text("ACC001\n", encoding="utf-8")

    assert simple_cli.main([str(accessions), "--profile", "2"]) == 1
    assert "Profile 2 尚未配置" in capsys.readouterr().err


def test_simple_cli_explicit_config_and_options_are_forwarded(tmp_path, monkeypatch):
    _runtime_roots(tmp_path, monkeypatch)
    config_path = tmp_path / "custom.json"
    save_config(config_path, AppConfig())
    accessions = tmp_path / "list.xlsx"
    accessions.write_bytes(b"fixture")
    task_state = tmp_path / "custom-task.sqlite3"
    captured: list[str] = []
    monkeypatch.setattr(
        simple_cli.DICOM_download_script,
        "main",
        lambda arguments: captured.extend(arguments) or 2,
    )
    monkeypatch.setattr(simple_cli, "configure_packaged_dcmtk", lambda: None)

    result = simple_cli.main(
        [
            str(accessions),
            "--config",
            str(config_path),
            "--accession-column",
            "检查号",
            "--task-state",
            str(task_state),
            "--discard-checkpoint",
            "--accept-download-failures",
        ]
    )

    assert result == 2
    assert captured[captured.index("--task-state") + 1] == str(task_state)
    assert captured[captured.index("--accession-column") + 1] == "检查号"
    assert "--discard-checkpoint" in captured
    assert "--accept-download-failures" in captured


def test_frozen_windows_cli_reuses_sibling_dcmtk_runtime(tmp_path, monkeypatch):
    executable = tmp_path / "DcmGetCLI.exe"
    executable.write_bytes(b"cli")
    dcmtk_bin = executable.parent.joinpath(*simple_cli.PACKAGED_DCMTK_BIN)
    dcmtk_bin.mkdir(parents=True)
    (dcmtk_bin / "movescu.exe").write_bytes(b"move")
    (dcmtk_bin / "storescp.exe").write_bytes(b"store")
    selected: list[Path] = []
    monkeypatch.setattr(simple_cli.sys, "platform", "win32")
    monkeypatch.setattr(simple_cli.sys, "executable", str(executable))
    monkeypatch.setattr(simple_cli, "is_frozen", lambda: True)
    monkeypatch.setattr(
        simple_cli,
        "set_portable_dcmtk_bin",
        lambda path: selected.append(Path(path)),
    )

    assert simple_cli.configure_packaged_dcmtk() == dcmtk_bin.resolve()
    assert selected == [dcmtk_bin.resolve()]


@pytest.mark.parametrize("value", ["0", "10000", "invalid"])
def test_simple_cli_rejects_invalid_profile(value):
    with pytest.raises(SystemExit):
        simple_cli.build_parser().parse_args(["access.txt", "--profile", value])
