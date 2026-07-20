from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

import dcmget.task_ledger as task_ledger_module
from dcmget.core import ReceivedDicomMetadata
from dcmget.task_ledger import (
    AttributionStatus,
    ObservedDicom,
    TaskLedger,
    TaskLedgerError,
    inspect_dicom_file,
)


def _dicom(
    path: Path,
    *,
    accession: str = "REQ-001",
    study_uid: str = "1.2.826.0.1.3680043.10.991.1",
    series_uid: str = "1.2.826.0.1.3680043.10.991.1.1",
    sop_uid: str = "1.2.826.0.1.3680043.10.991.1.1.1",
) -> Path:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = sop_uid
    dataset.StudyInstanceUID = study_uid
    dataset.SeriesInstanceUID = series_uid
    if accession:
        dataset.AccessionNumber = accession
    dataset.save_as(path, enforce_file_format=True)
    return path


def test_ledger_persists_transfer_metadata_and_uses_wal(tmp_path: Path):
    ledger_path = tmp_path / "state" / "task-ledger.sqlite3"
    ledger = TaskLedger(ledger_path)
    batch_id = ledger.create_batch(
        ["REQ-001", "REQ-002", "REQ-003"],
        batch_id="batch-a",
        profile_name="dcmget-6666-DCMGET",
        anonymization_requested=True,
        pdi_requested=True,
    )

    matched = _dicom(tmp_path / "matched.dcm")
    mismatched = _dicom(
        tmp_path / "mismatch.dcm",
        accession="OTHER-002",
        study_uid="1.2.826.0.1.3680043.10.991.2",
        series_uid="1.2.826.0.1.3680043.10.991.2.1",
        sop_uid="1.2.826.0.1.3680043.10.991.2.1.1",
    )
    unverifiable = _dicom(
        tmp_path / "missing-accession.dcm",
        accession="",
        study_uid="1.2.826.0.1.3680043.10.991.3",
        series_uid="1.2.826.0.1.3680043.10.991.3.1",
        sop_uid="1.2.826.0.1.3680043.10.991.3.1.1",
    )

    assert ledger.record_dicom_file(batch_id, "REQ-001", matched) == AttributionStatus.MATCHED
    assert ledger.record_dicom_file(batch_id, "REQ-002", mismatched) == AttributionStatus.MISMATCH
    assert (
        ledger.record_dicom_file(batch_id, "REQ-003", unverifiable)
        == AttributionStatus.UNVERIFIABLE
    )
    for accession in ("REQ-001", "REQ-002", "REQ-003"):
        ledger.record_accession_result(
            batch_id,
            accession,
            "完成",
            reported_file_count=1,
            reported_bytes=100,
            anonymization_status="完成",
        )
    ledger.record_anonymization_result(batch_id, "完成")
    ledger.record_pdi_result(
        batch_id,
        "完成",
        output_directory=tmp_path / "PDI" / "patient-secret",
    )
    ledger.complete_batch(batch_id, "完成")

    reopened = TaskLedger(ledger_path).load_batch(batch_id)
    assert reopened.status == "完成"
    assert reopened.anonymization_status == "完成"
    assert reopened.pdi_status == "完成"
    assert [item.attribution_status for item in reopened.requests] == [
        "matched",
        "mismatch",
        "unverifiable",
    ]
    assert reopened.requests[0].instances[0].study_instance_uid.endswith("991.1")
    assert reopened.requests[0].instances[0].series_instance_uid.endswith("991.1.1")
    assert reopened.requests[0].instances[0].sop_instance_uid.endswith("991.1.1.1")
    assert reopened.requests[0].instances[0].size_bytes == matched.stat().st_size
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_unreadable_dicom_is_recorded_as_unverifiable_without_blocking(tmp_path: Path):
    bad = tmp_path / "broken.dcm"
    bad.write_bytes(b"not a dicom")
    ledger = TaskLedger(tmp_path / "ledger.sqlite3")
    batch_id = ledger.create_batch(["REQ-BAD"])

    status = ledger.record_dicom_file(batch_id, "REQ-BAD", bad)

    assert status == AttributionStatus.UNVERIFIABLE
    request = ledger.load_batch(batch_id).requests[0]
    assert request.attribution_status == "unverifiable"
    assert len(request.instances) == 1
    assert request.instances[0].metadata_error


def test_expected_study_uid_can_verify_missing_accession(tmp_path: Path):
    source = _dicom(
        tmp_path / "missing-accession.dcm",
        accession="",
        study_uid="1.2.826.0.1.3680043.10.991.44",
    )
    ledger = TaskLedger(tmp_path / "ledger.sqlite3")
    batch_id = ledger.create_batch(["REQ-044"])

    status = ledger.record_dicom_file(
        batch_id,
        "REQ-044",
        source,
        expected_study_instance_uids=["1.2.826.0.1.3680043.10.991.44"],
    )

    assert status == AttributionStatus.MATCHED
    assert ledger.load_batch(batch_id).requests[0].attribution_status == "matched"


def test_mismatch_dominates_request_attribution(tmp_path: Path):
    ledger = TaskLedger(tmp_path / "ledger.sqlite3")
    batch_id = ledger.create_batch(["REQ-MIXED"])
    ledger.record_observed_dicom(
        batch_id,
        "REQ-MIXED",
        ObservedDicom("one.dcm", actual_accession_number="REQ-MIXED"),
    )
    ledger.record_observed_dicom(
        batch_id,
        "REQ-MIXED",
        ObservedDicom("two.dcm", actual_accession_number="OTHER"),
    )
    ledger.record_observed_dicom(
        batch_id,
        "REQ-MIXED",
        ObservedDicom("three.dcm"),
    )

    assert ledger.load_batch(batch_id).requests[0].attribution_status == "mismatch"


def test_bulk_instance_recording_uses_one_request_snapshot(tmp_path: Path):
    ledger = TaskLedger(tmp_path / "ledger.sqlite3")
    batch_id = ledger.create_batch(["BULK-001"])
    observations = [
        ObservedDicom(
            str(tmp_path / f"instance-{index}.dcm"),
            actual_accession_number="BULK-001",
            study_instance_uid="1.2.826.0.1.3680043.10.991.70",
            series_instance_uid="1.2.826.0.1.3680043.10.991.70.1",
            sop_instance_uid=f"1.2.826.0.1.3680043.10.991.70.1.{index + 1}",
            size_bytes=index + 1,
        )
        for index in range(250)
    ]

    statuses = ledger.record_observed_dicoms(
        batch_id, "BULK-001", observations
    )

    request = ledger.load_batch(batch_id).requests[0]
    assert len(statuses) == 250
    assert set(statuses) == {AttributionStatus.MATCHED}
    assert len(request.instances) == 250
    assert request.attribution_status == "matched"


class _Status(Enum):
    COMPLETED = "完成"


@dataclass
class _RunnerResult:
    accession: str
    status: object
    archived_files: list[str]
    message: str = ""
    duration_seconds: float = 1.25
    file_count: int = 1
    received_bytes: int = 512


def test_download_runner_received_metadata_is_accepted_by_ledger(tmp_path: Path):
    ledger = TaskLedger(tmp_path / "ledger.sqlite3")
    batch_id = ledger.create_batch(["REQ-RUNNER"])
    result = _RunnerResult(
        "REQ-RUNNER",
        _Status.COMPLETED,
        [str(tmp_path / "received.dcm")],
    )
    received = ReceivedDicomMetadata(
        file_path=str(tmp_path / "received.dcm"),
        actual_accession_number="REQ-RUNNER",
        study_instance_uid="1.2.826.0.1.3680043.10.991.80",
        series_instance_uid="1.2.826.0.1.3680043.10.991.80.1",
        sop_instance_uid="1.2.826.0.1.3680043.10.991.80.1.1",
        size_bytes=1024,
    )

    ledger.record_runner_result(
        batch_id,
        result,
        observed_instances=[received],
    )

    request = ledger.load_batch(batch_id).requests[0]
    assert request.attribution_status == "matched"
    assert request.instances[0].sop_instance_uid.endswith("991.80.1.1")


def test_download_runner_adapter_accepts_pre_anonymization_metadata(tmp_path: Path):
    anonymous_file = _dicom(
        tmp_path / "anonymous.dcm",
        accession="ACC-PSEUDONYM",
    )
    ledger = TaskLedger(tmp_path / "ledger.sqlite3")
    batch_id = ledger.create_batch(
        ["REQ-ORIGINAL"], anonymization_requested=True
    )
    result = _RunnerResult(
        "REQ-ORIGINAL",
        _Status.COMPLETED,
        [str(anonymous_file)],
    )
    original_metadata = ObservedDicom(
        str(anonymous_file),
        actual_accession_number="REQ-ORIGINAL",
        study_instance_uid="1.2.826.0.1.3680043.10.991.1",
        series_instance_uid="1.2.826.0.1.3680043.10.991.1.1",
        sop_instance_uid="1.2.826.0.1.3680043.10.991.1.1.1",
        size_bytes=anonymous_file.stat().st_size,
    )

    ledger.record_runner_result(
        batch_id,
        result,
        observed_instances=[original_metadata],
        anonymization_status="完成",
        anonymized_output=True,
    )

    request = ledger.load_batch(batch_id).requests[0]
    assert request.transfer_status == "完成"
    assert request.anonymization_status == "完成"
    assert request.attribution_status == "matched"


def test_anonymous_file_without_original_metadata_is_not_false_mismatch(tmp_path: Path):
    anonymous_file = _dicom(
        tmp_path / "anonymous.dcm",
        accession="ACC-PSEUDONYM",
    )
    ledger = TaskLedger(tmp_path / "ledger.sqlite3")
    batch_id = ledger.create_batch(
        ["REQ-ORIGINAL"], anonymization_requested=True
    )
    result = _RunnerResult(
        "REQ-ORIGINAL",
        _Status.COMPLETED,
        [str(anonymous_file)],
    )

    ledger.record_runner_result(
        batch_id,
        result,
        anonymization_status="完成",
        anonymized_output=True,
    )

    assert ledger.load_batch(batch_id).requests[0].attribution_status == "unverifiable"


def test_default_reports_redact_identifiers_paths_and_messages(tmp_path: Path):
    raw_accession = "SECRET-ACC-009"
    raw_study = "1.2.826.0.1.3680043.10.991.900"
    raw_series = f"{raw_study}.1"
    raw_sop = f"{raw_series}.1"
    source = tmp_path / "patient-secret" / f"{raw_sop}.dcm"
    source.parent.mkdir(parents=True, exist_ok=True)
    _dicom(
        source,
        accession=raw_accession,
        study_uid=raw_study,
        series_uid=raw_series,
        sop_uid=raw_sop,
    )
    ledger = TaskLedger(tmp_path / "ledger.sqlite3")
    batch_id = ledger.create_batch(
        [raw_accession], batch_id=raw_accession, pdi_requested=True
    )
    ledger.record_dicom_file(batch_id, raw_accession, source)
    ledger.record_accession_result(
        batch_id,
        raw_accession,
        "完成",
        message=(
            f"=HYPERLINK(\"https://invalid.example\") "
            f"<script>alert(1)</script> {raw_accession} {source}"
        ),
        reported_file_count=1,
    )
    ledger.record_pdi_result(
        batch_id,
        "完成",
        output_directory=tmp_path / "PDI" / raw_accession,
        message=f"PDI for {raw_accession}",
    )
    ledger.complete_batch(batch_id, "完成")

    paths = ledger.export_reports(batch_id, tmp_path / "reports")
    json_text = paths.json_path.read_text(encoding="utf-8")
    csv_bytes = paths.csv_path.read_bytes()
    html_text = paths.html_path.read_text(encoding="utf-8")
    combined = json_text + csv_bytes.decode("utf-8-sig") + html_text

    assert raw_accession not in combined
    assert raw_study not in combined
    assert raw_series not in combined
    assert raw_sop not in combined
    assert str(source) not in combined
    assert raw_accession not in paths.json_path.name
    assert "REQ-" in combined
    assert "STUDY-" in combined
    assert "[已脱敏路径]" in json_text
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
    assert "'=HYPERLINK" in csv_bytes.decode("utf-8-sig")
    assert "<script>" not in html_text
    assert "&lt;script&gt;" in html_text
    payload = json.loads(json_text)
    assert payload["redacted"] is True
    assert payload["batch"]["attribution_counts"]["matched"] == 1


def test_unredacted_report_is_explicit_and_contains_original_values(tmp_path: Path):
    source = _dicom(tmp_path / "raw.dcm", accession="RAW-001")
    ledger = TaskLedger(tmp_path / "ledger.sqlite3")
    batch_id = ledger.create_batch(["RAW-001"])
    ledger.record_dicom_file(batch_id, "RAW-001", source)

    report = ledger.report_data(batch_id, redact=False)

    assert report["redacted"] is False
    assert report["requests"][0]["requested_accession"] == "RAW-001"
    assert report["requests"][0]["instances"][0]["file_path"] == str(source)


def test_large_report_export_streams_cursor_batches_without_materializing_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ledger = TaskLedger(tmp_path / "ledger.sqlite3")
    batch_id = ledger.create_batch(["STREAM-001"])
    observations = (
        ObservedDicom(
            str(tmp_path / f"image-{index:05d}.dcm"),
            actual_accession_number="STREAM-001",
            study_instance_uid="1.2.826.0.1.3680043.10.991.80",
            series_instance_uid="1.2.826.0.1.3680043.10.991.80.1",
            sop_instance_uid=f"1.2.826.0.1.3680043.10.991.80.1.{index + 1}",
            size_bytes=index + 1,
        )
        for index in range(1_300)
    )
    ledger.record_observed_dicoms(batch_id, "STREAM-001", observations)

    def no_materialized_batch(*_args, **_kwargs):
        raise AssertionError("报告导出不得调用全量 load_batch/report_data")

    monkeypatch.setattr(TaskLedger, "load_batch", no_materialized_batch)
    monkeypatch.setattr(TaskLedger, "report_data", no_materialized_batch)
    chunk_sizes: list[int] = []

    def tracked_cursor_rows(cursor, batch_size=task_ledger_module._REPORT_FETCH_SIZE):
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            chunk_sizes.append(len(rows))
            yield from rows

    monkeypatch.setattr(task_ledger_module, "_iter_cursor_rows", tracked_cursor_rows)

    paths = ledger.export_reports(batch_id, tmp_path / "reports")

    assert chunk_sizes
    assert max(chunk_sizes) <= task_ledger_module._REPORT_FETCH_SIZE
    assert chunk_sizes.count(task_ledger_module._REPORT_FETCH_SIZE) >= 4
    assert sum(1 for _line in paths.csv_path.open(encoding="utf-8-sig")) == 1_301
    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    assert payload["batch"]["instance_count"] == 1_300
    assert len(payload["requests"][0]["instances"]) == 1_300


def test_duplicate_accession_validation_does_not_create_partial_batch(tmp_path: Path):
    ledger = TaskLedger(tmp_path / "ledger.sqlite3")

    with pytest.raises(TaskLedgerError, match="不能重复"):
        ledger.create_batch(["DUP", "DUP"], batch_id="should-not-exist")

    with pytest.raises(TaskLedgerError, match="不存在"):
        ledger.load_batch("should-not-exist")


def test_inspect_missing_file_returns_unverifiable_observation(tmp_path: Path):
    observation = inspect_dicom_file(tmp_path / "missing.dcm")

    assert observation.size_bytes == 0
    assert observation.metadata_error
    assert observation.attribution_status == AttributionStatus.UNVERIFIABLE
