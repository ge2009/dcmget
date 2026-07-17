from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import sys

import pytest

from dcmget.config import (
    AE_TITLE_LABELS,
    DEFAULT_ANONYMIZATION_PROFILE,
    DEFAULT_DIRECTORY_TEMPLATE,
    AppConfig,
    load_config,
    parse_accessions,
    save_config,
    validate_ae_title,
)
from dcmget import runtime


def _save_config_process(
    path: str,
    storage_port: int,
    start_event,
    result_queue,
) -> None:
    start_event.wait(10)
    try:
        for index in range(12):
            save_config(
                path,
                AppConfig(
                    storage_port=storage_port,
                    storage_ae_title=f"STORE{storage_port}",
                    dicom_destination_folder=f"Dicom-{storage_port}-{index}",
                ),
            )
        result_queue.put("")
    except Exception as exc:  # pragma: no cover - reported in parent process
        result_queue.put(repr(exc))


def test_migrates_legacy_configuration(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "movescu_executable_path": "C:/dcmtk/bin/movescu.exe",
                "application_entity_title": "CALLING",
                "called_ae_title": "PACS",
                "calling_ae_title": "STORAGE",
                "network_port": 11112,
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.config_version == 6
    assert Path(config.dcmtk_bin_dir) == Path("C:/dcmtk/bin")
    assert config.calling_ae_title == "CALLING"
    assert config.pacs_ae_title == "PACS"
    assert config.storage_ae_title == "STORAGE"
    assert config.storage_port == 11112
    assert not config.anonymization_enabled
    assert config.anonymization_profile == DEFAULT_ANONYMIZATION_PROFILE
    assert not config.pdi_export_enabled
    assert config.pdi_include_ohif_viewer
    assert config.max_concurrent_moves == 2


def test_accession_parser_ignores_blanks_and_deduplicates_in_order():
    result = parse_accessions(" A001\n\nA002\nA001\n A003 \n")

    assert result.values == ["A001", "A002", "A003"]
    assert result.blank_count == 1
    assert result.duplicate_count == 1
    assert result.invalid_values == ()


def test_accession_parser_rejects_dicom_wildcards_and_controls():
    result = parse_accessions("SAFE001\n*\nA?\nA\\B\nBAD\x07VALUE\n")

    assert result.values == ["SAFE001"]
    assert result.invalid_values == ("*", "A?", "A\\B", "BAD\x07VALUE")


def test_configuration_round_trip(tmp_path):
    path = tmp_path / "nested" / "config.json"
    expected = AppConfig(storage_port=12345, storage_ae_title="STORE")

    save_config(path, expected)

    assert load_config(path) == expected
    assert not path.with_suffix(".json.tmp").exists()


def test_concurrent_processes_save_complete_atomic_configuration(tmp_path):
    path = tmp_path / "config.json"
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    ports = (16661, 16662, 16663, 16664)
    processes = [
        context.Process(
            target=_save_config_process,
            args=(str(path), port, start_event, result_queue),
        )
        for port in ports
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(20)

    assert [process.exitcode for process in processes] == [0] * len(processes)
    assert [result_queue.get(timeout=2) for _process in processes] == [""] * len(
        processes
    )
    saved = load_config(path)
    assert saved.storage_port in ports
    assert saved.storage_ae_title == f"STORE{saved.storage_port}"
    assert saved.dicom_destination_folder.startswith(f"Dicom-{saved.storage_port}-")
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_new_configuration_uses_dcmget_receiver_and_metadata_layout():
    config = AppConfig()

    assert config.calling_ae_title == "DCMGET"
    assert config.storage_ae_title == "DCMGET"
    assert config.storage_port == 6666
    assert config.max_concurrent_moves == 2
    assert config.directory_template == DEFAULT_DIRECTORY_TEMPLATE
    assert not config.anonymization_enabled
    assert config.anonymization_profile == "research"
    assert not config.pdi_export_enabled
    assert config.pdi_include_ohif_viewer


def test_version_two_configuration_keeps_existing_values_and_adds_current_defaults():
    config = AppConfig.from_dict(
        {
            "config_version": 2,
            "pacs_server_ip": "10.1.2.3",
            "storage_port": 16666,
            "directory_template": "{StudyInstanceUID}",
        }
    )

    assert config.config_version == 6
    assert config.pacs_server_ip == "10.1.2.3"
    assert config.storage_port == 16666
    assert config.directory_template == "{StudyInstanceUID}"
    assert not config.anonymization_enabled
    assert config.anonymization_profile == DEFAULT_ANONYMIZATION_PROFILE
    assert not config.pdi_export_enabled
    assert config.pdi_include_ohif_viewer


def test_anonymization_profile_is_validated_only_when_enabled():
    disabled = AppConfig(anonymization_enabled=False, anonymization_profile="unknown")
    enabled = AppConfig(anonymization_enabled=True, anonymization_profile="unknown")

    assert "anonymization_profile" not in disabled.validate()
    assert "anonymization_profile" in enabled.validate()


def test_configuration_parses_string_boolean_without_enabling_anonymization():
    config = AppConfig.from_dict(
        {"config_version": 3, "anonymization_enabled": "false"}
    )

    assert not config.anonymization_enabled


def test_version_four_configuration_migrates_to_ohif_without_overwriting_values():
    config = AppConfig.from_dict(
        {
            "config_version": 4,
            "pacs_server_ip": "10.1.2.3",
            "pdi_export_enabled": "true",
            "pdi_institution_name": "测试医院",
            "pdi_output_folder": "/tmp/pdi",
            "pdi_include_html_preview": "false",
            "pdi_preview_mode": "all",
            "pdi_include_weasis_windows": "false",
        }
    )

    assert config.config_version == 6
    assert config.pacs_server_ip == "10.1.2.3"
    assert config.pdi_export_enabled
    assert config.pdi_institution_name == "测试医院"
    assert config.pdi_output_folder == "/tmp/pdi"
    assert not config.pdi_include_ohif_viewer
    assert "pdi_include_html_preview" not in config.to_dict()
    assert "pdi_preview_mode" not in config.to_dict()
    assert "pdi_include_weasis_windows" not in config.to_dict()


def test_version_five_configuration_parses_ohif_boolean():
    config = AppConfig.from_dict(
        {"config_version": 5, "pdi_include_ohif_viewer": "false"}
    )

    assert not config.pdi_include_ohif_viewer
    assert config.config_version == 6
    assert config.max_concurrent_moves == 2


def test_version_five_configuration_adds_default_concurrency_without_overwriting():
    migrated = AppConfig.from_dict(
        {"config_version": 5, "pacs_server_ip": "10.1.2.3"}
    )
    configured = AppConfig.from_dict(
        {"config_version": 6, "max_concurrent_moves": "4"}
    )

    assert migrated.config_version == 6
    assert migrated.max_concurrent_moves == 2
    assert migrated.pacs_server_ip == "10.1.2.3"
    assert configured.max_concurrent_moves == 4


@pytest.mark.parametrize(
    ("html_preview", "weasis", "expected"),
    [
        (False, False, False),
        (False, True, True),
        (True, False, True),
        (True, True, True),
    ],
)
def test_version_four_viewer_options_merge_into_ohif(
    html_preview: bool, weasis: bool, expected: bool
):
    config = AppConfig.from_dict(
        {
            "config_version": 4,
            "pdi_include_html_preview": html_preview,
            "pdi_include_weasis_windows": weasis,
        }
    )

    assert config.pdi_include_ohif_viewer is expected


def test_example_configuration_matches_current_schema():
    config = load_config(Path(__file__).parents[1] / "config.example.json")

    assert config.config_version == 6
    assert config.max_concurrent_moves == 2
    assert not config.anonymization_enabled
    assert config.anonymization_profile == DEFAULT_ANONYMIZATION_PROFILE
    assert config.pdi_include_ohif_viewer


def test_validation_reports_required_and_invalid_values():
    config = AppConfig(
        pacs_server_ip="",
        calling_ae_title="A" * 17,
        pacs_server_port=0,
        storage_port=70000,
        max_concurrent_moves=9,
        max_log_file_size_bytes=100,
    )

    errors = config.validate()

    assert set(errors) >= {
        "pacs_server_ip",
        "calling_ae_title",
        "pacs_server_port",
        "storage_port",
        "max_concurrent_moves",
        "max_log_file_size_bytes",
    }


@pytest.mark.parametrize(
    ("value", "message_part"),
    [
        ("   ", "请输入"),
        ("A" * 17, "最多 16 个字符"),
        ("中文AE", "可打印 ASCII 字符"),
        ("BAD\\AE", "不能包含反斜杠"),
        ("BAD\tAE", "可打印 ASCII 字符"),
        ("BAD\x7fAE", "可打印 ASCII 字符"),
    ],
)
@pytest.mark.parametrize(("field", "label"), AE_TITLE_LABELS.items())
def test_ae_title_validation_is_field_specific(field, label, value, message_part):
    config = AppConfig()
    setattr(config, field, value)

    message = config.validate()[field]

    assert label in message
    assert message_part in message


@pytest.mark.parametrize(
    "value",
    ["A", "DcmGet 01", "AE-1_2.3", "!#$%&'()*+,-./:;<=>?@[]^_`{|}~"[:16]],
)
def test_ae_title_validation_accepts_dicom_printable_ascii(value):
    assert validate_ae_title(value) == ""


def test_configuration_load_trims_ae_title_padding_but_not_controls():
    config = AppConfig.from_dict(
        {
            "config_version": 6,
            "calling_ae_title": " DCMGET ",
            "pacs_ae_title": "PACS\t",
            "storage_ae_title": " STORE ",
        }
    )

    assert config.calling_ae_title == "DCMGET"
    assert config.storage_ae_title == "STORE"
    assert "pacs_ae_title" in config.validate()


def test_pdi_institution_validation_is_only_required_when_export_is_enabled():
    disabled = AppConfig(
        pdi_export_enabled=False,
        pdi_institution_name="",
    )
    enabled = AppConfig(
        pdi_export_enabled=True,
        pdi_institution_name="",
    )

    assert "pdi_institution_name" not in disabled.validate()
    assert enabled.validate()["pdi_institution_name"]


def test_directory_template_rejects_unknown_fields_and_parent_paths():
    unknown = AppConfig(directory_template="{PatientName}/{AccessionNumber}")
    parent = AppConfig(directory_template="../{AccessionNumber}")
    unmatched = AppConfig(directory_template="{PatientID}/{")
    windows_absolute = AppConfig(directory_template="C:/data/{AccessionNumber}")

    assert "directory_template" in unknown.validate()
    assert "directory_template" in parent.validate()
    assert "directory_template" in unmatched.validate()
    assert "directory_template" in windows_absolute.validate()


def test_frozen_runtime_uses_appdata_for_persistent_config(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    appdata = tmp_path / "appdata"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setenv("APPDATA", str(appdata))

    assert runtime.resource_root() == bundle
    assert runtime.default_config_path() == appdata / "DcmGet" / "config.json"
    config_path = runtime.ensure_default_config()
    config = load_config(config_path)
    assert config_path.exists()
    assert config.dicom_destination_folder == str(
        Path.home() / "Documents" / "DcmGet" / "Dicom"
    )


def test_application_state_directory_is_platform_specific(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert runtime.application_state_dir() == tmp_path / "local" / "DcmGet"

    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    assert runtime.application_state_dir() == (
        Path.home() / "Library" / "Application Support" / "DcmGet"
    )

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert runtime.application_state_dir() == tmp_path / "state" / "dcmget"
