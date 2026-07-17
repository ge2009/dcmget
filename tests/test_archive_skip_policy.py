from __future__ import annotations

import errno
from pathlib import Path

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import (
    CTImageStorage,
    EncapsulatedPDFStorage,
    ExplicitVRLittleEndian,
    JPEGBaseline8Bit,
)

from dcmget import core
from dcmget.config import AppConfig
from dcmget.core import (
    AccessionResult,
    AccessionStatus,
    ArchiveStats,
    BatchSummary,
    DownloadRunner,
    ToolPaths,
)
from dcmget.task_state import _merge_partial_result, _result_from_json, _result_to_json


def _write_dicom(
    path: Path,
    sop_instance_uid: str,
    *,
    accession: str = "ACC001",
    patient_name: str = "Patient^Original",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = FileMetaDataset()
    metadata.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(path, {}, file_meta=metadata, preamble=b"\0" * 128)
    dataset.SOPInstanceUID = sop_instance_uid
    dataset.AccessionNumber = accession
    dataset.PatientName = patient_name
    dataset.save_as(path)


def _write_pixel_dicom(
    path: Path,
    sop_instance_uid: str,
    *,
    pixel_data: bytes = b"\x00\x00\x01\x00\x02\x00\x03\x00",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = FileMetaDataset()
    metadata.TransferSyntaxUID = ExplicitVRLittleEndian
    metadata.MediaStorageSOPClassUID = CTImageStorage
    metadata.MediaStorageSOPInstanceUID = sop_instance_uid
    dataset = FileDataset(path, {}, file_meta=metadata, preamble=b"\0" * 128)
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = sop_instance_uid
    dataset.AccessionNumber = "ACC001"
    dataset.Rows = 2
    dataset.Columns = 2
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 16
    dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.PixelData = pixel_data
    dataset.save_as(path)


def _write_compressed_pixel_dicom(
    path: Path,
    sop_instance_uid: str,
    *,
    frame: bytes,
    patient_name: str = "Patient^Original",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = FileMetaDataset()
    metadata.TransferSyntaxUID = JPEGBaseline8Bit
    metadata.MediaStorageSOPClassUID = CTImageStorage
    metadata.MediaStorageSOPInstanceUID = sop_instance_uid
    dataset = FileDataset(path, {}, file_meta=metadata, preamble=b"\0" * 128)
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = sop_instance_uid
    dataset.AccessionNumber = "ACC001"
    dataset.PatientName = patient_name
    dataset.Rows = 2
    dataset.Columns = 2
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.PixelData = encapsulate([frame])
    dataset["PixelData"].is_undefined_length = True
    dataset.save_as(path)


def _write_encapsulated_pdf(
    path: Path,
    sop_instance_uid: str,
    *,
    content: bytes,
    patient_name: str = "Patient^Original",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = FileMetaDataset()
    metadata.TransferSyntaxUID = ExplicitVRLittleEndian
    metadata.MediaStorageSOPClassUID = EncapsulatedPDFStorage
    metadata.MediaStorageSOPInstanceUID = sop_instance_uid
    dataset = FileDataset(path, {}, file_meta=metadata, preamble=b"\0" * 128)
    dataset.SOPClassUID = EncapsulatedPDFStorage
    dataset.SOPInstanceUID = sop_instance_uid
    dataset.AccessionNumber = "ACC001"
    dataset.PatientName = patient_name
    dataset.MIMETypeOfEncapsulatedDocument = "application/pdf"
    dataset.EncapsulatedDocument = content
    dataset.save_as(path)


def test_readable_existing_sop_skips_different_incoming_bytes(tmp_path: Path) -> None:
    destination_root = tmp_path / "dicom"
    first = tmp_path / "staging" / "first.dcm"
    incoming = tmp_path / "staging" / "incoming.dcm"
    _write_dicom(first, "1.2.3.51", patient_name="Patient^Original")
    _write_dicom(incoming, "1.2.3.51", patient_name="Patient^Updated")
    original_bytes = first.read_bytes()

    first_stats = ArchiveStats()
    moved, rejected = core._archive_dicom_files(
        [first],
        destination_root,
        "{AccessionNumber}",
        "ACC001",
        stats=first_stats,
    )
    target = moved[0]
    retry_stats = ArchiveStats()
    retry_moved, retry_rejected = core._archive_dicom_files(
        [incoming],
        destination_root,
        "{AccessionNumber}",
        "ACC001",
        stats=retry_stats,
    )

    assert rejected == retry_rejected == []
    assert first_stats.new_file_count == 1
    assert retry_moved == [target]
    assert retry_stats.new_file_count == 0
    assert retry_stats.existing_skipped_count == 1
    assert retry_stats.conflict_preserved_count == 0
    assert target.read_bytes() == original_bytes
    assert not incoming.exists()
    assert not (destination_root / "_DcmGetConflicts").exists()


def test_corrupt_existing_target_preserves_incoming_in_conflicts(tmp_path: Path) -> None:
    destination_root = tmp_path / "dicom"
    target = destination_root / "ACC001" / "1.2.3.52.dcm"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"not a readable DICOM file")
    incoming = tmp_path / "staging" / "incoming.dcm"
    _write_dicom(incoming, "1.2.3.52")
    incoming_bytes = incoming.read_bytes()
    errors: list[str] = []
    stats = ArchiveStats()

    moved, rejected = core._archive_dicom_files(
        [incoming],
        destination_root,
        "{AccessionNumber}",
        "ACC001",
        error_callback=lambda _path, message: errors.append(message),
        stats=stats,
    )

    assert moved == []
    assert rejected == []
    assert errors == []
    assert stats.new_file_count == 0
    assert stats.existing_skipped_count == 0
    assert stats.conflict_preserved_count == 1
    assert target.read_bytes() == b"not a readable DICOM file"
    assert not incoming.exists()
    conflicts = list((destination_root / "_DcmGetConflicts").glob("*.dcm"))
    assert conflicts == stats.conflict_files
    assert len(conflicts) == 1
    assert conflicts[0].read_bytes() == incoming_bytes


def test_mismatched_existing_sop_preserves_incoming_in_conflicts(tmp_path: Path) -> None:
    destination_root = tmp_path / "dicom"
    target = destination_root / "ACC001" / "1.2.3.53.dcm"
    _write_dicom(target, "1.2.3.999")
    original_bytes = target.read_bytes()
    incoming = tmp_path / "staging" / "incoming.dcm"
    _write_dicom(incoming, "1.2.3.53")
    stats = ArchiveStats()

    moved, rejected = core._archive_dicom_files(
        [incoming],
        destination_root,
        "{AccessionNumber}",
        "ACC001",
        stats=stats,
    )

    assert moved == []
    assert rejected == []
    assert stats.conflict_preserved_count == 1
    assert target.read_bytes() == original_bytes
    assert not incoming.exists()
    assert len(stats.conflict_files) == 1
    assert stats.conflict_files[0].parent == destination_root / "_DcmGetConflicts"


def test_cross_device_conflict_copy_is_durable_before_source_removal(
    tmp_path: Path, monkeypatch
) -> None:
    destination_root = tmp_path / "dicom"
    target = destination_root / "ACC001" / "1.2.3.531.dcm"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"broken existing target")
    incoming = tmp_path / "private-state" / "incoming.dcm"
    _write_dicom(incoming, "1.2.3.531")
    incoming_bytes = incoming.read_bytes()
    real_replace = core.os.replace

    def replace_with_cross_device_error(candidate, destination):
        if Path(candidate) == incoming:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_replace(candidate, destination)

    monkeypatch.setattr(core.os, "replace", replace_with_cross_device_error)
    stats = ArchiveStats()

    moved, rejected = core._archive_dicom_files(
        [incoming],
        destination_root,
        "{AccessionNumber}",
        "ACC001",
        stats=stats,
    )

    assert moved == []
    assert rejected == []
    assert not incoming.exists()
    assert stats.conflict_files[0].read_bytes() == incoming_bytes
    conflict_directory = destination_root / "_DcmGetConflicts"
    assert list(conflict_directory.glob("*.part")) == []
    assert list(conflict_directory.glob("*.reserve")) == []


def test_truncated_existing_dicom_preserves_complete_incoming(tmp_path: Path) -> None:
    destination_root = tmp_path / "dicom"
    target = destination_root / "ACC001" / "1.2.3.54.dcm"
    _write_dicom(target, "1.2.3.54")
    truncated = target.read_bytes()[:-3]
    target.write_bytes(truncated)
    incoming = tmp_path / "staging" / "incoming.dcm"
    _write_dicom(incoming, "1.2.3.54", patient_name="Patient^Complete")
    complete = incoming.read_bytes()
    stats = ArchiveStats()

    moved, rejected = core._archive_dicom_files(
        [incoming],
        destination_root,
        "{AccessionNumber}",
        "ACC001",
        stats=stats,
    )

    assert moved == []
    assert rejected == []
    assert target.read_bytes() == truncated
    assert stats.conflict_preserved_count == 1
    assert stats.conflict_files[0].read_bytes() == complete


def test_existing_image_truncated_at_pixel_element_boundary_is_not_trusted(
    tmp_path: Path,
) -> None:
    destination_root = tmp_path / "dicom"
    target = destination_root / "ACC001" / "1.2.3.540.dcm"
    incoming = tmp_path / "staging" / "incoming.dcm"
    _write_pixel_dicom(target, "1.2.3.540")
    _write_pixel_dicom(incoming, "1.2.3.540")
    complete = incoming.read_bytes()
    pixel_tag = complete.find(b"\xe0\x7f\x10\x00")
    assert pixel_tag > 0
    target.write_bytes(complete[:pixel_tag])
    stats = ArchiveStats()

    moved, rejected = core._archive_dicom_files(
        [incoming],
        destination_root,
        "{AccessionNumber}",
        "ACC001",
        stats=stats,
    )

    assert moved == []
    assert rejected == []
    assert stats.existing_skipped_count == 0
    assert stats.conflict_preserved_count == 1
    assert stats.conflict_files[0].read_bytes() == complete


def test_existing_image_with_short_pixel_payload_is_not_trusted(
    tmp_path: Path,
) -> None:
    destination_root = tmp_path / "dicom"
    target = destination_root / "ACC001" / "1.2.3.541.dcm"
    incoming = tmp_path / "staging" / "incoming.dcm"
    _write_pixel_dicom(target, "1.2.3.541", pixel_data=b"\x00\x00")
    _write_pixel_dicom(incoming, "1.2.3.541")
    complete = incoming.read_bytes()
    stats = ArchiveStats()

    moved, rejected = core._archive_dicom_files(
        [incoming],
        destination_root,
        "{AccessionNumber}",
        "ACC001",
        stats=stats,
    )

    assert moved == []
    assert rejected == []
    assert stats.existing_skipped_count == 0
    assert stats.conflict_preserved_count == 1
    assert stats.conflict_files[0].read_bytes() == complete


def test_different_compressed_payload_is_preserved_for_review(tmp_path: Path) -> None:
    destination_root = tmp_path / "dicom"
    target = destination_root / "ACC001" / "1.2.3.542.dcm"
    incoming = tmp_path / "staging" / "incoming.dcm"
    _write_compressed_pixel_dicom(target, "1.2.3.542", frame=b"\xff\xd8")
    _write_compressed_pixel_dicom(
        incoming,
        "1.2.3.542",
        frame=b"\xff\xd8\x01\x02\xff\xd9",
    )
    complete = incoming.read_bytes()
    stats = ArchiveStats()

    moved, rejected = core._archive_dicom_files(
        [incoming],
        destination_root,
        "{AccessionNumber}",
        "ACC001",
        stats=stats,
    )

    assert moved == []
    assert rejected == []
    assert stats.existing_skipped_count == 0
    assert stats.conflict_preserved_count == 1
    assert stats.conflict_files[0].read_bytes() == complete


def test_identical_compressed_payload_can_skip_existing_target(tmp_path: Path) -> None:
    destination_root = tmp_path / "dicom"
    target = destination_root / "ACC001" / "1.2.3.543.dcm"
    incoming = tmp_path / "staging" / "incoming.dcm"
    frame = b"\xff\xd8\x01\x02\xff\xd9"
    _write_compressed_pixel_dicom(target, "1.2.3.543", frame=frame)
    _write_compressed_pixel_dicom(
        incoming,
        "1.2.3.543",
        frame=frame,
        patient_name="Patient^Updated",
    )
    original = target.read_bytes()
    stats = ArchiveStats()

    moved, rejected = core._archive_dicom_files(
        [incoming],
        destination_root,
        "{AccessionNumber}",
        "ACC001",
        stats=stats,
    )

    assert moved == [target]
    assert rejected == []
    assert stats.existing_skipped_count == 1
    assert stats.conflict_preserved_count == 0
    assert target.read_bytes() == original
    assert not incoming.exists()


def test_short_encapsulated_pdf_is_preserved_for_review(tmp_path: Path) -> None:
    destination_root = tmp_path / "dicom"
    target = destination_root / "ACC001" / "1.2.3.544.dcm"
    incoming = tmp_path / "staging" / "incoming.dcm"
    _write_encapsulated_pdf(target, "1.2.3.544", content=b"%P")
    _write_encapsulated_pdf(
        incoming,
        "1.2.3.544",
        content=b"%PDF-1.7\nDcmGet\n%%EOF\n",
    )
    complete = incoming.read_bytes()
    stats = ArchiveStats()

    moved, rejected = core._archive_dicom_files(
        [incoming],
        destination_root,
        "{AccessionNumber}",
        "ACC001",
        stats=stats,
    )

    assert moved == []
    assert rejected == []
    assert stats.existing_skipped_count == 0
    assert stats.conflict_preserved_count == 1
    assert stats.conflict_files[0].read_bytes() == complete


def test_identical_encapsulated_pdf_can_skip_existing_target(tmp_path: Path) -> None:
    destination_root = tmp_path / "dicom"
    target = destination_root / "ACC001" / "1.2.3.545.dcm"
    incoming = tmp_path / "staging" / "incoming.dcm"
    content = b"%PDF-1.7\nDcmGet\n%%EOF\n"
    _write_encapsulated_pdf(target, "1.2.3.545", content=content)
    _write_encapsulated_pdf(
        incoming,
        "1.2.3.545",
        content=content,
        patient_name="Patient^Updated",
    )
    original = target.read_bytes()
    stats = ArchiveStats()

    moved, rejected = core._archive_dicom_files(
        [incoming],
        destination_root,
        "{AccessionNumber}",
        "ACC001",
        stats=stats,
    )

    assert moved == [target]
    assert rejected == []
    assert stats.existing_skipped_count == 1
    assert stats.conflict_preserved_count == 0
    assert target.read_bytes() == original
    assert not incoming.exists()


def test_download_result_completes_when_valid_existing_sop_is_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    destination_root = tmp_path / "dicom"
    target = destination_root / "ACC001" / "1.2.3.55.dcm"
    _write_dicom(target, "1.2.3.55", patient_name="Patient^Original")
    original = target.read_bytes()
    staging = tmp_path / "staging"
    staging.mkdir()
    logs: list[tuple[str, str, str]] = []
    runner = DownloadRunner(
        AppConfig(
            dicom_destination_folder=str(destination_root),
            directory_template="{AccessionNumber}",
        ),
        ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0"),
        log_callback=lambda *entry: logs.append(entry),
    )

    class Process:
        stdout = iter(
            [
                "I: Received Final Move Response (Success)\n",
                "I: DIMSE Status: 0x0000: Success\n",
                "I: Number of Completed Suboperations : 1\n",
            ]
        )

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            _write_dicom(
                staging / "incoming.dcm",
                "1.2.3.55",
                patient_name="Patient^Updated",
            )
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())
    result = runner._download_one("ACC001", staging, 1, 1)
    runner._close_file_logger()

    assert result.status == AccessionStatus.COMPLETED
    assert result.file_count == 1
    assert result.new_file_count == 0
    assert result.existing_skipped_count == 1
    assert result.conflict_preserved_count == 0
    assert result.archived_files == [str(target)]
    assert "已存在跳过 1" in result.message
    assert target.read_bytes() == original
    assert not any(level == "error" for _source, _message, level in logs)


def test_download_result_is_partial_when_invalid_target_requires_conflict_review(
    tmp_path: Path, monkeypatch
) -> None:
    destination_root = tmp_path / "dicom"
    target = destination_root / "ACC001" / "1.2.3.56.dcm"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"broken existing target")
    staging = tmp_path / "staging"
    staging.mkdir()
    logs: list[tuple[str, str, str]] = []
    runner = DownloadRunner(
        AppConfig(
            dicom_destination_folder=str(destination_root),
            directory_template="{AccessionNumber}",
        ),
        ToolPaths(Path("movescu"), Path("storescp"), Path("."), "3.7.0"),
        log_callback=lambda *entry: logs.append(entry),
    )

    class Process:
        stdout = iter(
            [
                "I: Received Final Move Response (Success)\n",
                "I: DIMSE Status: 0x0000: Success\n",
                "I: Number of Completed Suboperations : 1\n",
            ]
        )

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            _write_dicom(staging / "incoming.dcm", "1.2.3.56")
            return 0

    monkeypatch.setattr(runner, "_popen", lambda _command: Process())
    result = runner._download_one("ACC001", staging, 1, 1)
    runner._close_file_logger()

    assert result.status == AccessionStatus.PARTIAL
    assert result.file_count == 1
    assert result.new_file_count == 0
    assert result.existing_skipped_count == 0
    assert result.conflict_preserved_count == 1
    assert result.archived_files == []
    assert "冲突保留 1" in result.message
    assert "需人工核对" in result.message
    assert target.read_bytes() == b"broken existing target"
    assert len(list((destination_root / "_DcmGetConflicts").glob("*.dcm"))) == 1
    assert not any("文件归档失败" in message for _source, message, _level in logs)
    assert sum("冲突保留 1" in message for _source, message, _level in logs) == 1


def test_archive_statistics_round_trip_and_merge() -> None:
    prior = AccessionResult(
        "ACC001",
        AccessionStatus.CANCELLED,
        file_count=2,
        new_file_count=1,
        existing_skipped_count=1,
        conflict_preserved_count=0,
        archived_files=["/dicom/one.dcm", "/dicom/two.dcm"],
    )
    restored = _result_from_json(_result_to_json(prior))

    assert restored.new_file_count == 1
    assert restored.existing_skipped_count == 1
    assert restored.conflict_preserved_count == 0

    current = AccessionResult(
        "ACC001",
        AccessionStatus.COMPLETED,
        file_count=1,
        conflict_preserved_count=1,
    )
    merged = _merge_partial_result(restored, current)

    assert merged.new_file_count == 1
    assert merged.existing_skipped_count == 1
    assert merged.conflict_preserved_count == 1
    assert merged.file_count == 3
    assert merged.status == AccessionStatus.PARTIAL


def test_batch_summary_aggregates_archive_statistics() -> None:
    summary = BatchSummary(
        [
            AccessionResult(
                "ACC001",
                AccessionStatus.COMPLETED,
                new_file_count=3,
                existing_skipped_count=2,
            ),
            AccessionResult(
                "ACC002",
                AccessionStatus.COMPLETED,
                new_file_count=4,
                conflict_preserved_count=1,
            ),
        ]
    )

    assert summary.new_file_count == 7
    assert summary.existing_skipped_count == 2
    assert summary.conflict_preserved_count == 1


def test_legacy_result_without_archive_statistics_counts_files_as_new() -> None:
    restored = _result_from_json(
        '{"accession":"ACC001","status":"完成","file_count":3,'
        '"archived_files":[]}'
    )

    assert restored.new_file_count == 3
    assert restored.existing_skipped_count == 0
    assert restored.conflict_preserved_count == 0
