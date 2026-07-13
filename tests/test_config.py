from __future__ import annotations

import json
from pathlib import Path

from dcmget.config import AppConfig, load_config, parse_accessions, save_config


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

    assert config.config_version == 2
    assert Path(config.dcmtk_bin_dir) == Path("C:/dcmtk/bin")
    assert config.calling_ae_title == "CALLING"
    assert config.pacs_ae_title == "PACS"
    assert config.storage_ae_title == "STORAGE"
    assert config.storage_port == 11112


def test_accession_parser_ignores_blanks_and_deduplicates_in_order():
    result = parse_accessions(" A001\n\nA002\nA001\n A003 \n")

    assert result.values == ["A001", "A002", "A003"]
    assert result.blank_count == 1
    assert result.duplicate_count == 1


def test_configuration_round_trip(tmp_path):
    path = tmp_path / "nested" / "config.json"
    expected = AppConfig(storage_port=12345, storage_ae_title="STORE")

    save_config(path, expected)

    assert load_config(path) == expected
    assert not path.with_suffix(".json.tmp").exists()


def test_validation_reports_required_and_invalid_values():
    config = AppConfig(
        pacs_server_ip="",
        calling_ae_title="A" * 17,
        pacs_server_port=0,
        storage_port=70000,
        max_log_file_size_bytes=100,
    )

    errors = config.validate()

    assert set(errors) >= {
        "pacs_server_ip",
        "calling_ae_title",
        "pacs_server_port",
        "storage_port",
        "max_log_file_size_bytes",
    }
