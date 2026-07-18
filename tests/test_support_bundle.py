from __future__ import annotations

import json
import zipfile

from dcmget.config import AppConfig
from dcmget.support_bundle import create_support_bundle, redact_text


def _assert_no_secrets(text: str, *secrets: str) -> None:
    leaked = [secret for secret in secrets if secret and secret in text]
    assert leaked == []


def test_redact_text_masks_healthcare_identifiers_ip_and_user_paths(monkeypatch):
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Administrator")
    source = (
        "PACS 192.168.0.147 PatientName=孙碎兰 PatientID: 6906368\n"
        "开始检查号 202601261643\n"
        "0008,0050=CT202108130339\n"
        "CT202106300149：失败\n"
        "2026-07-18 INFO [movescu] MRI202607180001：完成\n"
        "2026-07-18 ERROR [验收台账] 无法写入检查号 XA20260718002 的验收记录\n"
        r"C:\Users\Administrator\AppData\Local\DcmGet\logs"
        "\n"
        "/Users/alice/Documents/patient\n"
        "(0010,0010) PN [王小明]\n"
    )

    redacted = redact_text(source)

    _assert_no_secrets(
        redacted,
        "192.168.0.147",
        "孙碎兰",
        "6906368",
        "202601261643",
        "CT202108130339",
        "CT202106300149",
        "MRI202607180001",
        "XA20260718002",
        "Administrator",
        "alice",
        "王小明",
    )
    assert "<IP>" in redacted
    assert "<ACCESSION>" in redacted
    assert "<REDACTED>" in redacted
    assert "<HOME>" in redacted or "<USER>" in redacted


def test_redact_text_fail_closed_for_unc_ipv6_hosts_and_dcmtk_labels():
    source = (
        "2026-07-18 ERROR [dcmtk] PermissionError: cannot open "
        r"\\pacs-share\Patients\王小明\202601261643"
        "\n"
        "PACS host=pacs-internal.hospital.local:104\n"
        "PACS endpoint=[fd00::147]:104\n"
        "connect fe80::1%12 failed\n"
        "viewer URL=https://viewer.internal/studies/202601261643\n"
        "E: (0008,0050) SH [CT202108130339]\n"
        "I: (0010,0010) PN [孙碎兰]\n"
        "I: (0010,0020) LO [6906368]\n"
        "ValueError: Patient's Name was 赵敏\n"
        "Patient ID: P-991 Accession No.: XA20260718002\n"
        "2026-07-18 12:00:00 CRITICAL [vendor] "
        "Patient's Name reported as 未知格式姓名\n"
    )

    redacted = redact_text(source)

    _assert_no_secrets(
        redacted,
        "pacs-share",
        "Patients",
        "王小明",
        "202601261643",
        "pacs-internal.hospital.local",
        "fd00::147",
        "fe80::1",
        "viewer.internal",
        "CT202108130339",
        "孙碎兰",
        "6906368",
        "赵敏",
        "P-991",
        "XA20260718002",
        "未知格式姓名",
    )
    assert "ERROR [dcmtk] PermissionError:" in redacted
    assert "CRITICAL [vendor] Patient Name: <REDACTED>" in redacted
    assert "ValueError: Patient's Name was <REDACTED>" in redacted
    assert "<UNC_PATH>" in redacted
    assert "<HOST>:104" in redacted
    assert "<IPV6>" in redacted
    assert "viewer URL=https:/<PATH>" in redacted
    assert "E: Accession: <REDACTED>" in redacted
    assert "I: Patient Name: <REDACTED>" in redacted
    assert "I: Patient ID: <REDACTED>" in redacted


def test_support_bundle_has_allowlisted_redacted_files_and_no_dicom(tmp_path):
    logs = tmp_path / "diagnostics"
    logs.mkdir()
    (logs / "dcmget-diagnostics-123.log").write_text(
        "开始检查号 202601261643；PACS=172.16.0.163\n"
        r"error at C:\Users\Administrator\AppData\Local\DcmGet"
        "\n",
        encoding="utf-8",
    )
    (logs / "dcmget-crash-123.log").write_text(
        "PatientName: 孙碎兰\n"
        r"ERROR cannot open \\pacs-share\Patients\孙碎兰\202601261643"
        "\n"
        "E: (0010,0020) LO [6906368]\n",
        encoding="utf-8",
    )
    (logs / "image.dcm").write_bytes(b"DICM PATIENT SECRET")
    (logs / "unrelated.txt").write_text("202601261643", encoding="utf-8")
    output = tmp_path / "support.zip"
    config = AppConfig(
        dcmtk_bin_dir=r"C:\Users\Administrator\dcmtk\bin",
        access_numbers_file_path=r"C:\Users\Administrator\access.txt",
        dicom_destination_folder=r"D:\Patients\孙碎兰",
        pacs_server_ip="172.16.0.163",
        calling_ae_title="MACGET",
        pacs_ae_title="PACS01",
        storage_ae_title="MACGET",
        storage_port=6666,
    )
    health = {
        "schema": "dcmget-health",
        "status": "error",
        "checks": [
            {
                "id": "destination",
                "summary": "PatientID: 6906368",
                "details": {
                    "path": r"C:\Users\Administrator\DICOM",
                    "server": "172.16.0.163",
                    "host": "pacs-internal.hospital.local",
                    "endpoint": "fd00::147",
                    "patient_name": "孙碎兰",
                },
            }
        ],
    }

    result = create_support_bundle(
        output,
        config,
        diagnostic_directory=logs,
        health_report=health,
    )

    assert result.path == output
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert names == {
            "manifest.json",
            "health.json",
            "config-summary.json",
            "logs/01-dcmget-crash-123.log",
            "logs/02-dcmget-diagnostics-123.log",
        }
        combined = b"\n".join(archive.read(name) for name in names).decode("utf-8")
        manifest = json.loads(archive.read("manifest.json"))
        summary = json.loads(archive.read("config-summary.json"))
        health_data = json.loads(archive.read("health.json"))

    _assert_no_secrets(
        combined,
        "202601261643",
        "172.16.0.163",
        "Administrator",
        "孙碎兰",
        "6906368",
        "MACGET",
        "PACS01",
        "DICM PATIENT SECRET",
        "pacs-share",
        "Patients",
        "pacs-internal.hospital.local",
        "fd00::147",
    )
    assert manifest["privacy"] == {
        "contains_dicom": False,
        "contains_license": False,
        "contains_trial_state": False,
        "redacted": True,
    }
    assert summary["pacs_server_ip"] == "<IP>"
    assert summary["calling_ae_title"] == "<REDACTED:6>"
    assert summary["access_numbers_file_configured"] is True

    details = health_data["checks"][0]["details"]
    assert details["host"] == "<HOST>"
    assert details["endpoint"] == "<HOST>"


def test_support_bundle_limits_logs_and_ignores_symlinks(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    for index in range(3):
        (logs / f"dcmget-diagnostics-{index}.log").write_text(
            f"log {index}", encoding="utf-8"
        )
    outside = tmp_path / "outside.log"
    outside.write_text("PatientName: secret", encoding="utf-8")
    symlink = logs / "dcmget-crash-link.log"
    try:
        symlink.symlink_to(outside)
    except OSError:
        pass

    result = create_support_bundle(
        tmp_path / "support.zip",
        AppConfig(),
        diagnostic_directory=logs,
        health_report={"status": "ok"},
        max_log_files=1,
    )

    assert result.omitted_log_count == 2
    assert sum(name.startswith("logs/") for name in result.included_files) == 1
