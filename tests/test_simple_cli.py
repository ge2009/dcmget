from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import DICOM_download_cli as cli
from dcmget.config import AppConfig
from dcmget.core import (
    AccessionResult,
    AccessionStatus,
    BatchSummary,
    ToolPaths,
)


def _write_config(path: Path, **changes: object) -> Path:
    values = dict(cli.DEFAULT_CONFIG)
    values.update(changes)
    path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    return path


def test_missing_config_message_matches_standalone_zip(tmp_path):
    with pytest.raises(FileNotFoundError) as error:
        cli.load_standalone_config(tmp_path / "config.json")

    message = str(error.value)
    assert "压缩包内的 config.json" in message
    assert "config.example.json" not in message


def test_standalone_config_is_strict_and_resolves_relative_paths(tmp_path):
    path = _write_config(
        tmp_path / "config.json",
        dcmtk_bin_dir="tools",
        dicom_destination_folder="output",
        storage_port=65535,
    )

    loaded = cli.load_standalone_config(path)

    assert loaded.path == path.resolve()
    assert loaded.app_config.dcmtk_bin_dir == str((tmp_path / "tools").resolve())
    assert loaded.app_config.dicom_destination_folder == str(
        (tmp_path / "output").resolve()
    )
    assert loaded.app_config.storage_port == 65535
    assert loaded.app_config.web_port == 65534
    assert loaded.app_config.pdi_export_enabled is False
    assert loaded.app_config.anonymization_enabled is False


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda raw: raw.update({"pdi_export_enabled": True}), "不支持的字段"),
        (lambda raw: raw.pop("pacs_server_ip"), "缺少字段"),
        (lambda raw: raw.update({"storage_port": "6666"}), "必须是整数"),
    ],
)
def test_standalone_config_rejects_non_download_schema(
    tmp_path,
    mutate,
    message,
):
    raw = dict(cli.DEFAULT_CONFIG)
    mutate(raw)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        cli.load_standalone_config(path)


def test_accession_file_is_txt_only_and_deduplicated(tmp_path, capsys):
    source = tmp_path / "access.txt"
    source.write_text("\ufeffACC001\n\nACC001\nACC002\n", encoding="utf-8")

    assert cli._load_accession_file(source) == ["ACC001", "ACC002"]
    output = capsys.readouterr().out
    assert "忽略空行 1" in output
    assert "去重 1" in output

    csv = tmp_path / "access.csv"
    csv.write_text("ACC001\n", encoding="utf-8")
    with pytest.raises(ValueError, match="只接受 TXT"):
        cli._load_accession_file(csv)


def test_packaged_cli_uses_its_own_dcmtk_directory(tmp_path, monkeypatch):
    executable = tmp_path / "DcmGetCLI.exe"
    executable.write_bytes(b"cli")
    dcmtk = tmp_path / "dcmtk" / "bin"
    dcmtk.mkdir(parents=True)
    (dcmtk / "movescu.exe").write_bytes(b"move")
    (dcmtk / "storescp.exe").write_bytes(b"store")
    selected: list[Path] = []
    monkeypatch.setattr(cli, "is_frozen", lambda: True)
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli.sys, "executable", str(executable))
    monkeypatch.setattr(
        cli,
        "set_portable_dcmtk_bin",
        lambda value: selected.append(Path(value)),
    )

    assert cli.configure_packaged_dcmtk() == dcmtk
    assert selected == [dcmtk]


def test_standalone_main_downloads_without_profile_pdi_or_license(
    tmp_path,
    monkeypatch,
):
    config_path = _write_config(tmp_path / "config.json")
    accessions = tmp_path / "access.txt"
    accessions.write_text("ACC001\nACC002\n", encoding="utf-8")
    destination = tmp_path / "Dicom"
    tools = ToolPaths(
        tmp_path / "movescu",
        tmp_path / "storescp",
        tmp_path,
        "3.7.0",
    )
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, config, received_tools, **callbacks):
            captured["config"] = config
            captured["tools"] = received_tools
            captured["callbacks"] = callbacks

        def run(self, values):
            results = [
                AccessionResult(
                    value,
                    AccessionStatus.COMPLETED,
                    file_count=index,
                )
                for index, value in enumerate(values, 1)
            ]
            progress = captured["callbacks"]["progress_callback"]
            for index, result in enumerate(results, 1):
                progress(index, len(results), result)
            return BatchSummary(results)

        def request_cancel(self):
            return None

    monkeypatch.setattr(cli, "ensure_supported_runtime", lambda: None)
    monkeypatch.setattr(cli, "configure_packaged_dcmtk", lambda: None)
    monkeypatch.setattr(cli, "standalone_state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(
        cli,
        "preflight",
        lambda config, resolver: SimpleNamespace(
            ok=True,
            tools=tools,
            checks=[("DCMTK 工具", True, "已就绪")],
        ),
    )
    monkeypatch.setattr(cli, "DownloadRunner", FakeRunner)

    assert cli.main([str(accessions), "--config", str(config_path)]) == 0

    config = captured["config"]
    assert isinstance(config, AppConfig)
    assert config.pdi_export_enabled is False
    assert config.anonymization_enabled is False
    assert captured["tools"] is tools
    assert not cli.task_state_path(config_path).exists()
    assert destination == Path(config.dicom_destination_folder)


def test_resume_refuses_changed_config_or_accessions(tmp_path):
    path = _write_config(tmp_path / "config.json")
    config = cli.load_standalone_config(path).app_config
    store = cli.TaskCheckpointStore(tmp_path / "state.sqlite3")
    assert store.try_acquire_lease()
    checkpoint = store.start(config, ["ACC001"], trial_required=False)

    with pytest.raises(cli.TaskStateError, match="不一致"):
        cli._prepare_checkpoint(
            store,
            config,
            ["ACC002"],
            reset=False,
        )
    assert store.load_required().task_id == checkpoint.task_id
    store.release_lease()


def test_completed_checkpoint_exits_without_preflight_or_receiver(
    tmp_path,
    monkeypatch,
    capsys,
):
    config_path = _write_config(tmp_path / "config.json")
    accessions = tmp_path / "access.txt"
    accessions.write_text("ACC001\n", encoding="utf-8")
    monkeypatch.setattr(cli, "ensure_supported_runtime", lambda: None)
    monkeypatch.setattr(cli, "configure_packaged_dcmtk", lambda: None)
    monkeypatch.setattr(cli, "standalone_state_root", lambda: tmp_path / "state")

    config = cli.load_standalone_config(config_path).app_config
    state_path = cli.task_state_path(config_path)
    store = cli.TaskCheckpointStore(state_path)
    assert store.try_acquire_lease()
    checkpoint = store.start(config, ["ACC001"], trial_required=False)
    store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "ACC001",
            AccessionStatus.COMPLETED,
            file_count=3,
        ),
    )
    store.release_lease()

    def unexpected_preflight(*_args, **_kwargs):
        raise AssertionError("已完成恢复点不应再执行端口预检")

    class UnexpectedRunner:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("已完成恢复点不应启动接收器")

    monkeypatch.setattr(cli, "preflight", unexpected_preflight)
    monkeypatch.setattr(cli, "DownloadRunner", UnexpectedRunner)

    assert cli.main([str(accessions), "--config", str(config_path)]) == 0
    assert not state_path.exists()
    output = capsys.readouterr().out
    assert "无需重新启动接收器" in output
    assert "完成 1" in output


def test_standalone_entry_does_not_import_full_product_features():
    source = Path(cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "DICOM_download_script" not in imported
    assert "dcmget.licensing" not in imported
    assert "dcmget.pdi" not in imported
    assert "dcmget.instance_profile" not in imported
    assert "dcmget.profile_manager" not in imported


def test_standalone_refuses_pdi_checkpoint_phase(tmp_path):
    path = _write_config(tmp_path / "config.json")
    config = cli.load_standalone_config(path).app_config
    store = cli.TaskCheckpointStore(tmp_path / "state.sqlite3")
    assert store.try_acquire_lease()
    checkpoint = store.start(config, ["ACC001"], trial_required=False)
    store.set_phase(checkpoint.task_id, "pdi_pending")

    with pytest.raises(cli.TaskStateError, match="不能恢复任务阶段"):
        cli._prepare_checkpoint(
            store,
            config,
            ["ACC001"],
            reset=False,
        )
    store.release_lease()


@pytest.mark.parametrize(
    "arguments",
    [
        ["access.txt", "--profile", "1"],
        ["access.txt", "--license", "license.lic"],
        ["access.txt", "--accept-download-failures"],
        ["access.txt", "--verify-pdi", "PDI"],
    ],
)
def test_full_product_options_are_unreachable(arguments):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(arguments)


def test_cli_has_no_profile_pdi_or_license_options():
    help_text = cli.build_parser().format_help()
    assert "--profile" not in help_text
    assert "--license" not in help_text
    assert "PDI" not in help_text
