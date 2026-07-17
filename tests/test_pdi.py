from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydicom import dcmread
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    MediaStorageDirectoryStorage,
    generate_uid,
)

from dcmget import pdi
from dcmget.config import AppConfig
from dcmget.core import ToolPaths
from dcmget.pdi import PdiExporter, PdiStage, PdiStatus


def _dicom(
    path: Path,
    *,
    sop_uid: str | None = None,
    patient_id: str = "PAT001",
    patient_name: str = "DcmGet^Patient",
    study_uid: str | None = None,
    series_uid: str | None = None,
    instance_number: int = 1,
    modality: str = "CT",
    private_value: str | None = None,
    frames: int = 1,
    transfer_syntax_uid: str = ExplicitVRLittleEndian,
) -> Path:
    sop_uid = sop_uid or generate_uid()
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = transfer_syntax_uid
    dataset = FileDataset(path, {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = sop_uid
    dataset.StudyInstanceUID = study_uid or generate_uid()
    dataset.SeriesInstanceUID = series_uid or generate_uid()
    dataset.PatientName = patient_name
    dataset.PatientID = patient_id
    dataset.AccessionNumber = "ACC001"
    dataset.StudyDate = "20260716"
    dataset.StudyTime = "135500"
    dataset.Modality = modality
    dataset.StudyDescription = "PDI Test"
    dataset.SeriesDescription = "Series"
    dataset.SeriesNumber = 2
    dataset.InstanceNumber = instance_number
    dataset.Rows = 2
    dataset.Columns = 2
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.PixelSpacing = [0.5, 0.5]
    dataset.WindowCenter = 40
    dataset.WindowWidth = 400
    if frames > 1:
        dataset.NumberOfFrames = frames
    dataset.PixelData = b"\0\1\2\3" * frames
    if private_value is not None:
        dataset.add_new((0x0011, 0x1010), "LO", private_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.save_as(path, enforce_file_format=True)
    return path


def _replace_specific_character_set(path: Path, replacement: bytes | None) -> None:
    """Patch test data without re-encoding its text elements."""

    content = path.read_bytes()
    tag = b"\x08\x00\x05\x00"
    offset = content.index(tag)
    explicit_vr = content[offset + 4 : offset + 6] == b"CS"
    length_offset = offset + (6 if explicit_vr else 4)
    length_size = 2 if explicit_vr else 4
    length = int.from_bytes(
        content[length_offset : length_offset + length_size], "little"
    )
    end = length_offset + length_size + length
    encoded_element = b""
    if replacement is not None:
        padded = replacement + (b" " if len(replacement) % 2 else b"")
        vr = b"CS" if explicit_vr else b""
        encoded_element = (
            tag + vr + len(padded).to_bytes(length_size, "little") + padded
        )
    path.write_bytes(content[:offset] + encoded_element + content[end:])


def _write_dicomdir(root: Path) -> None:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = MediaStorageDirectoryStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(
        root / "DICOMDIR", {}, file_meta=file_meta, preamble=b"\0" * 128
    )
    dataset.FileSetID = "DCMGET"
    records: list[Dataset] = []
    for path in sorted((root / "DICOM").rglob("*")):
        if not path.is_file():
            continue
        source = dcmread(path, stop_before_pixels=True)
        record = Dataset()
        record.DirectoryRecordType = "IMAGE"
        record.ReferencedFileID = list(path.relative_to(root).parts)
        record.ReferencedSOPClassUIDInFile = source.SOPClassUID
        record.ReferencedSOPInstanceUIDInFile = source.SOPInstanceUID
        record.ReferencedTransferSyntaxUIDInFile = source.file_meta.TransferSyntaxUID
        records.append(record)
    dataset.DirectoryRecordSequence = Sequence(records)
    dataset.save_as(root / "DICOMDIR", enforce_file_format=True)


@pytest.fixture
def tools(tmp_path: Path) -> ToolPaths:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dcmmkdir = bin_dir / "dcmmkdir"
    dcmmkdir.touch()
    return ToolPaths(
        movescu=bin_dir / "movescu",
        storescp=bin_dir / "storescp",
        bin_dir=bin_dir,
        version="3.7.0",
        dcmmkdir=dcmmkdir,
    )


def _config(tmp_path: Path, **overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "dicom_destination_folder": str(tmp_path / "download"),
        "pdi_output_folder": str(tmp_path / "portable"),
        "pdi_institution_name": "DcmGet Hospital",
        "pdi_include_ohif_viewer": False,
    }
    values.update(overrides)
    return AppConfig(**values)


def test_pdi_process_recovery_callback_failure_is_reported_as_error(
    tmp_path: Path, tools: ToolPaths
) -> None:
    messages = []

    def fail_process_update(*_event: object) -> None:
        raise OSError("recovery database is read-only")

    exporter = PdiExporter(
        _config(tmp_path),
        tools,
        log_callback=lambda source, message, level: messages.append(
            (source, message, level)
        ),
        process_callback=fail_process_update,
    )

    exporter._notify_process(123, "dcmmkdir", True)

    assert messages == [
        ("PDI", "无法更新 PDI 子进程恢复信息：recovery database is read-only", "error")
    ]


def _viewer(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "index.html").write_text("<html><script src='/app.js'></script></html>")
    (path / "app-config.js").write_text("window.config={};")
    (path / "app.js").write_bytes(b"offline-ohif")
    (path / "LICENSE-OHIF.txt").write_text("MIT")
    (path / "THIRD_PARTY-OHIF.md").write_text("third party")
    _write_viewer_checksums(path)
    return path


def _write_viewer_checksums(path: Path) -> None:
    checksum_path = path / pdi.OHIF_PAYLOAD_CHECKSUMS
    lines = [
        f"{pdi._sha256(candidate)}  {candidate.relative_to(path).as_posix()}"
        for candidate in sorted(path.rglob("*"))
        if candidate.is_file() and candidate != checksum_path
    ]
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fake_dcmtk(
    exporter: PdiExporter,
    monkeypatch: pytest.MonkeyPatch,
    *,
    strict_fails: bool = False,
    all_fail: bool = False,
    commands: list[list[str]] | None = None,
) -> None:
    def run(command: list[str], cwd: Path) -> pdi._CommandResult:
        if commands is not None:
            commands.append(command)
        if all_fail:
            return pdi._CommandResult(1, "dcmmkdir failed")
        if strict_fails and "-I" in command:
            return pdi._CommandResult(1, "strict profile rejected")
        _write_dicomdir(cwd)
        return pdi._CommandResult(0, "ok")

    monkeypatch.setattr(exporter, "_run_command", run)


def _instances(index_path: Path) -> list[dict[str, object]]:
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    return [
        instance
        for study in payload["studies"]
        for series in study["series"]
        for instance in series["instances"]
    ]


def test_export_uses_exact_files_and_preserves_sources(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _dicom(tmp_path / "download" / "first.dcm", patient_name="A^<script>")
    second = _dicom(tmp_path / "download" / "second.dcm", patient_id="PAT002")
    old = _dicom(tmp_path / "download" / "old.dcm", patient_id="HISTORY")
    source_hashes = {path: pdi._sha256(path) for path in (first, second, old)}
    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([first, second])

    assert result.status == PdiStatus.COMPLETED
    assert result.source_count == result.exported_count == result.indexed_count == 2
    output = Path(result.output_directory)
    copied = sorted(path for path in (output / "DICOM").rglob("*") if path.is_file())
    assert len(copied) == 2
    assert all(path.suffix == "" for path in copied)
    assert all(
        len(part) <= 8 and set(part) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
        for path in copied
        for part in path.relative_to(output).parts
    )
    index_html = (output / "INDEX.HTM").read_text(encoding="utf-8")
    assert "A^&lt;script&gt;" in index_html
    assert "HISTORY" not in index_html
    assert not list(output.rglob("*.jpg")) and not list(output.rglob("*.png"))
    assert all(pdi._sha256(path) == digest for path, digest in source_hashes.items())


def test_ohif_index_references_original_local_dicom_and_naturalized_metadata(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    study_uid = generate_uid()
    series_uid = generate_uid()
    first = _dicom(
        tmp_path / "one.dcm",
        study_uid=study_uid,
        series_uid=series_uid,
        instance_number=2,
        private_value="SECRET-PRIVATE",
    )
    second = _dicom(
        tmp_path / "two.dcm",
        study_uid=study_uid,
        series_uid=series_uid,
        instance_number=1,
    )
    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([first, second])

    output = Path(result.output_directory)
    assert not (output / "DCMGET_STUDIES.json").exists()
    assert (output / pdi.STUDY_INDEX).is_file()
    payload_text = (output / pdi.STUDY_INDEX).read_text(encoding="utf-8")
    instances = _instances(output / pdi.STUDY_INDEX)
    assert [entry["metadata"]["InstanceNumber"] for entry in instances] == [1, 2]
    assert all(str(entry["url"]).startswith("dicomweb:/DICOM/") for entry in instances)
    assert "http://" not in payload_text and "https://" not in payload_text
    assert "SECRET-PRIVATE" not in payload_text
    assert all((output / str(entry["url"])[10:]).is_file() for entry in instances)
    assert instances[0]["metadata"]["PixelSpacing"] == [0.5, 0.5]
    assert "PixelData" not in instances[0]["metadata"]


@pytest.mark.parametrize(
    ("declared_charset", "transfer_syntax_uid"),
    [
        (None, ExplicitVRLittleEndian),
        (b"ISO_IR 192", ExplicitVRLittleEndian),
        (b"ISO_IR 100", ExplicitVRLittleEndian),
        (b"ISO 2022 IR 100", ExplicitVRLittleEndian),
        (None, ImplicitVRLittleEndian),
    ],
)
def test_ohif_index_repairs_missing_or_wrong_gb18030_declaration(
    tmp_path: Path,
    tools: ToolPaths,
    monkeypatch: pytest.MonkeyPatch,
    declared_charset: bytes | None,
    transfer_syntax_uid: str,
) -> None:
    source = _dicom(
        tmp_path / "chinese.dcm", transfer_syntax_uid=transfer_syntax_uid
    )
    dataset = dcmread(source)
    dataset.SpecificCharacterSet = "GB18030"
    dataset.PatientName = "孙碎兰"
    dataset.StudyDescription = "胸部检查"
    dataset.SeriesDescription = "胸部平扫"
    dataset.ProtocolName = "常规胸部"
    dataset.save_as(source, enforce_file_format=True)
    _replace_specific_character_set(source, declared_charset)
    source_hash = pdi._sha256(source)

    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)
    result = exporter.export([source])

    output = Path(result.output_directory)
    payload_text = (output / pdi.STUDY_INDEX).read_text(encoding="utf-8")
    payload = json.loads(payload_text)
    study = payload["studies"][0]
    series = study["series"][0]
    metadata = series["instances"][0]["metadata"]
    assert result.status == PdiStatus.COMPLETED
    assert study["PatientName"] == "孙碎兰"
    assert study["StudyDescription"] == "胸部检查"
    assert series["SeriesDescription"] == "胸部平扫"
    assert series["ProtocolName"] == "常规胸部"
    assert metadata["PatientName"] == "孙碎兰"
    assert metadata["StudyDescription"] == "胸部检查"
    assert metadata["SpecificCharacterSet"] == "GB18030"
    assert metadata["TransferSyntaxUID"] == str(transfer_syntax_uid)
    assert "\ufffd" not in payload_text
    assert any("GB18030" in warning for warning in result.warnings)
    assert pdi._sha256(source) == source_hash


@pytest.mark.parametrize(
    "declared_charset", [b"GB18030", b"ISO_IR 100", b"ISO 2022 IR 100"]
)
def test_ohif_index_repairs_wrong_declaration_for_utf8_text(
    tmp_path: Path,
    tools: ToolPaths,
    monkeypatch: pytest.MonkeyPatch,
    declared_charset: bytes,
) -> None:
    source = _dicom(tmp_path / "utf8-declared-gb18030.dcm")
    dataset = dcmread(source)
    dataset.SpecificCharacterSet = "ISO_IR 192"
    dataset.PatientName = "测试中文"
    dataset.StudyDescription = "测试中文"
    dataset.SeriesDescription = "测试中文"
    dataset.save_as(source, enforce_file_format=True)
    _replace_specific_character_set(source, declared_charset)
    source_hash = pdi._sha256(source)

    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)
    result = exporter.export([source])

    output = Path(result.output_directory)
    payload_text = (output / pdi.STUDY_INDEX).read_text(encoding="utf-8")
    payload = json.loads(payload_text)
    study = payload["studies"][0]
    series = study["series"][0]
    metadata = series["instances"][0]["metadata"]
    assert result.status == PdiStatus.COMPLETED
    assert study["PatientName"] == "测试中文"
    assert study["StudyDescription"] == "测试中文"
    assert series["SeriesDescription"] == "测试中文"
    assert metadata["PatientName"] == "测试中文"
    assert metadata["StudyDescription"] == "测试中文"
    assert metadata["SpecificCharacterSet"] == "ISO_IR 192"
    assert "娴嬭瘯" not in payload_text
    assert "\ufffd" not in payload_text
    assert any("ISO_IR 192" in warning for warning in result.warnings)
    assert pdi._sha256(source) == source_hash


def test_ohif_index_preserves_valid_gb18030_when_utf8_decode_also_succeeds(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "valid-gb18030.dcm")
    dataset = dcmread(source)
    dataset.SpecificCharacterSet = "GB18030"
    # These GB18030 bytes also form valid UTF-8 ("һҵΪô"), so successful
    # UTF-8 decoding alone must not override the explicit declaration.
    dataset.PatientName = "一业为么"
    dataset.StudyDescription = "一业为么"
    dataset.save_as(source, enforce_file_format=True)
    source_hash = pdi._sha256(source)

    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)
    result = exporter.export([source])

    output = Path(result.output_directory)
    metadata = _instances(output / pdi.STUDY_INDEX)[0]["metadata"]
    assert result.status == PdiStatus.COMPLETED
    assert metadata["PatientName"] == "一业为么"
    assert metadata["StudyDescription"] == "一业为么"
    assert metadata["SpecificCharacterSet"] == "GB18030"
    assert not any("字符集缺失或声明异常" in warning for warning in result.warnings)
    assert pdi._sha256(source) == source_hash


@pytest.mark.parametrize("specific_character_set", ["ISO_IR 100", "ISO 2022 IR 100"])
def test_ohif_index_preserves_valid_latin_character_set(
    tmp_path: Path,
    tools: ToolPaths,
    monkeypatch: pytest.MonkeyPatch,
    specific_character_set: str,
) -> None:
    source = _dicom(tmp_path / f"{specific_character_set.replace(' ', '-')}.dcm")
    dataset = dcmread(source)
    dataset.SpecificCharacterSet = specific_character_set
    dataset.PatientName = "Jos\xe9^Patient^\xc0\xc9\xc8\xc7"
    dataset.StudyDescription = "Radiologie \xc0\xc9\xc8\xc7 g\xe9n\xe9rale"
    dataset.save_as(source, enforce_file_format=True)
    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    metadata = _instances(Path(result.output_directory) / pdi.STUDY_INDEX)[0][
        "metadata"
    ]
    assert metadata["PatientName"] == "Jos\xe9^Patient^\xc0\xc9\xc8\xc7"
    assert metadata["StudyDescription"] == "Radiologie \xc0\xc9\xc8\xc7 g\xe9n\xe9rale"
    assert metadata["SpecificCharacterSet"] == specific_character_set
    assert not any("字符集缺失或声明异常" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    ("specific_character_set", "patient_name", "study_description"),
    [
        ("ISO 2022 IR 149", "홍길동", "흉부 검사"),
        ("ISO_IR 144", "Иванов^Иван", "Обследование грудной клетки"),
    ],
    ids=["korean-iso2022-ir149", "cyrillic-iso-ir144"],
)
def test_ohif_index_preserves_valid_non_chinese_character_sets(
    tmp_path: Path,
    tools: ToolPaths,
    monkeypatch: pytest.MonkeyPatch,
    specific_character_set: str,
    patient_name: str,
    study_description: str,
) -> None:
    source = _dicom(tmp_path / f"{specific_character_set.replace(' ', '-')}.dcm")
    dataset = dcmread(source)
    dataset.SpecificCharacterSet = specific_character_set
    dataset.PatientName = patient_name
    dataset.StudyDescription = study_description
    dataset.SeriesDescription = study_description
    dataset.save_as(source, enforce_file_format=True)
    source_hash = pdi._sha256(source)
    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    output = Path(result.output_directory)
    payload = json.loads((output / pdi.STUDY_INDEX).read_text(encoding="utf-8"))
    study = payload["studies"][0]
    metadata = study["series"][0]["instances"][0]["metadata"]
    assert result.status == PdiStatus.COMPLETED
    assert study["PatientName"] == patient_name
    assert study["StudyDescription"] == study_description
    assert metadata["PatientName"] == patient_name
    assert metadata["StudyDescription"] == study_description
    assert metadata["SpecificCharacterSet"] == specific_character_set
    assert not any("字符集缺失或声明异常" in warning for warning in result.warnings)
    assert pdi._sha256(source) == source_hash


def test_ohif_index_ignores_malformed_private_text_when_repairing_charset(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "gb18030-with-bad-private-lo.dcm")
    dataset = dcmread(source)
    dataset.SpecificCharacterSet = "GB18030"
    dataset.PatientName = "孙碎兰"
    dataset.StudyDescription = "胸部"
    dataset.SeriesDescription = "胸部平扫"
    dataset.add_new((0x0011, 0x1010), "LO", b"\xff\xff")
    dataset.save_as(source, enforce_file_format=True)
    _replace_specific_character_set(source, b"ISO_IR 192")
    source_hash = pdi._sha256(source)

    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)
    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        result = exporter.export([source])
    assert caught_warnings == []

    output = Path(result.output_directory)
    payload_text = (output / pdi.STUDY_INDEX).read_text(encoding="utf-8")
    payload = json.loads(payload_text)
    study = payload["studies"][0]
    metadata = study["series"][0]["instances"][0]["metadata"]
    assert result.status == PdiStatus.COMPLETED
    assert study["PatientName"] == "孙碎兰"
    assert study["StudyDescription"] == "胸部"
    assert metadata["PatientName"] == "孙碎兰"
    assert metadata["StudyDescription"] == "胸部"
    assert metadata["SpecificCharacterSet"] == "GB18030"
    assert "\ufffd" not in payload_text
    assert pdi._sha256(source) == source_hash


def test_ohif_index_omits_non_rendering_identity_but_keeps_image_metadata(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    dataset = dcmread(source)
    dataset.PatientAddress = "SECRET PATIENT ADDRESS"
    dataset.PatientTelephoneNumbers = "555-0100"
    dataset.OtherPatientIDs = "LEGACY-SECRET-ID"
    dataset.ReferringPhysicianName = "Secret^Referrer"
    dataset.OperatorsName = "Secret^Operator"
    dataset.save_as(source, enforce_file_format=True)
    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    output = Path(result.output_directory)
    raw_index = (output / pdi.STUDY_INDEX).read_text(encoding="utf-8")
    metadata = _instances(output / pdi.STUDY_INDEX)[0]["metadata"]
    for secret in (
        "SECRET PATIENT ADDRESS",
        "555-0100",
        "LEGACY-SECRET-ID",
        "Secret^Referrer",
        "Secret^Operator",
    ):
        assert secret not in raw_index
    assert metadata["PixelSpacing"] == [0.5, 0.5]
    assert metadata["Rows"] == 2
    assert metadata["Columns"] == 2
    assert metadata["WindowCenter"] == 40.0


def test_multiframe_object_expands_manifest_frames_without_copying_pixels(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "multi.dcm", frames=3)
    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.exported_count == 1 and result.indexed_count == 3
    output = Path(result.output_directory)
    instances = _instances(output / pdi.STUDY_INDEX)
    assert [entry["url"] for entry in instances] == [
        "dicomweb:/DICOM/P000001/S000001/I000001?frame=1",
        "dicomweb:/DICOM/P000001/S000001/I000001?frame=2",
        "dicomweb:/DICOM/P000001/S000001/I000001?frame=3",
    ]
    assert len([path for path in (output / "DICOM").rglob("*") if path.is_file()]) == 1


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "frames"),
    [
        ("MAX_OHIF_INDEX_FRAMES", 2, 3),
        ("MAX_OHIF_INDEX_ESTIMATED_BYTES", 1, 1),
    ],
)
def test_ohif_index_limit_publishes_dicomdir_only_and_requests_split_batch(
    tmp_path: Path,
    tools: ToolPaths,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit_value: int,
    frames: int,
) -> None:
    source = _dicom(tmp_path / "source.dcm", frames=frames)
    viewer = _viewer(tmp_path / "ohif")
    monkeypatch.setattr(pdi, limit_name, limit_value)
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_ohif_viewer=True),
        tools,
        viewer_source=viewer,
    )
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.status == PdiStatus.PARTIAL
    assert result.indexed_count == 0
    assert any("请拆分批次" in warning for warning in result.warnings)
    output = Path(result.output_directory)
    assert (output / "DICOMDIR").is_file()
    assert len([path for path in (output / "DICOM").rglob("*") if path.is_file()]) == 1
    assert not (output / pdi.STUDY_INDEX).exists()
    assert not (output / "VIEWER" / "OHIF").exists()
    assert not any((output / name).exists() for name in ("OPEN_VIEWER.exe", "OPEN_VIEWER.bat", "OPEN_VIEWER.command", "OPEN_VIEWER.sh"))
    index_html = (output / "INDEX.HTM").read_text(encoding="utf-8")
    assert "此页仅显示检查清单，不能直接看图" in index_html


def test_offline_ohif_and_cross_platform_launchers_are_included(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    viewer = _viewer(tmp_path / "ohif")
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_ohif_viewer=True),
        tools,
        viewer_source=viewer,
    )
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.status == PdiStatus.COMPLETED
    output = Path(result.output_directory)
    assert (output / "VIEWER" / "OHIF" / "index.html").is_file()
    assert (output / "VIEWER" / "pdi_server.py").is_file()
    assert (output / "VIEWER" / "architecture.py").is_file()
    for name in ("OPEN_VIEWER.bat", "OPEN_VIEWER.command", "OPEN_VIEWER.sh"):
        assert (output / name).is_file()
    help_result = subprocess.run(
        [sys.executable, str(output / "VIEWER" / "pdi_server.py"), "--help"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "127.0.0.1" in (output / "README.TXT").read_text(encoding="utf-8")
    assert "Weasis" not in (output / "README.TXT").read_text(encoding="utf-8")


def test_corrupt_viewer_payload_is_rejected_but_dicomdir_is_published(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    viewer = _viewer(tmp_path / "ohif")
    (viewer / "app.js").write_bytes(b"corrupt-after-checksum")
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_ohif_viewer=True),
        tools,
        viewer_source=viewer,
    )
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.status == PdiStatus.PARTIAL
    assert any("校验失败" in warning for warning in result.warnings)
    output = Path(result.output_directory)
    assert (output / "DICOMDIR").is_file()
    assert not (output / "VIEWER" / "OHIF").exists()


@pytest.mark.parametrize(
    "lines",
    [
        ["0" * 64 + "  ../escape"],
        ["0" * 64 + "  app.js", "1" * 64 + "  app.js"],
    ],
)
def test_viewer_checksum_manifest_rejects_unsafe_paths_and_duplicates(
    tmp_path: Path, lines: list[str]
) -> None:
    viewer = _viewer(tmp_path / "ohif")
    (viewer / pdi.OHIF_PAYLOAD_CHECKSUMS).write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="清单格式无效"):
        pdi._verify_ohif_payload(viewer)


def test_viewer_payload_is_verified_again_after_copy(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    viewer = _viewer(tmp_path / "ohif")
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_ohif_viewer=True),
        tools,
        viewer_source=viewer,
    )
    _fake_dcmtk(exporter, monkeypatch)
    original_copy = exporter._copy_file

    def corrupt_copy(source_path: str | Path, destination_path: str | Path) -> str:
        copied = original_copy(source_path, destination_path)
        if Path(destination_path).name == "app.js":
            Path(destination_path).write_bytes(b"corrupt-during-copy")
        return copied

    monkeypatch.setattr(exporter, "_copy_file", corrupt_copy)

    result = exporter.export([source])

    assert result.status == PdiStatus.PARTIAL
    assert any("校验失败" in warning for warning in result.warnings)
    assert not (Path(result.output_directory) / "VIEWER" / "OHIF").exists()


def test_frozen_windows_export_uses_bundled_physical_server_script(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    viewer = _viewer(tmp_path / "ohif")
    frozen_root = tmp_path / "frozen"
    server_script = frozen_root / "dcmget" / "pdi_server.py"
    server_script.parent.mkdir(parents=True)
    server_script.write_text("# portable server\n", encoding="utf-8")
    (frozen_root / "dcmget" / "architecture.py").write_text(
        "# portable architecture\n", encoding="utf-8"
    )
    (frozen_root / "DcmGetPdiServer.exe").write_bytes(b"server")
    project_root = tmp_path / "empty-project"
    project_root.mkdir()
    monkeypatch.setattr(pdi, "__file__", str(frozen_root / "dcmget" / "pdi.py"))
    monkeypatch.setattr(pdi, "resource_root", lambda: frozen_root)
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_ohif_viewer=True),
        tools,
        project_root=project_root,
        viewer_source=viewer,
    )
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.status == PdiStatus.COMPLETED
    assert result.warnings == []
    output = Path(result.output_directory)
    assert (output / "OPEN_VIEWER.exe").read_bytes() == b"server"
    assert (output / "VIEWER" / "OHIF" / "index.html").is_file()
    assert (output / "VIEWER" / "pdi_server.py").read_text(encoding="utf-8") == (
        "# portable server\n"
    )
    assert (output / "VIEWER" / "architecture.py").read_text(encoding="utf-8") == (
        "# portable architecture\n"
    )
    assert (output / "OPEN_VIEWER.bat").is_file()
    assert (output / "MANIFEST.SHA256").is_file()
    index_html = (output / "INDEX.HTM").read_text(encoding="utf-8")
    assert "这是检查清单，不是阅片器" in index_html
    assert "无需选择 JSON、DICOMDIR 或逐个文件" in index_html
    assert "OPEN_VIEWER.exe（推荐）" in index_html
    assert "本次导出未能加入离线阅片器" not in index_html


def test_missing_ohif_is_partial_but_keeps_valid_dicomdir(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_ohif_viewer=True),
        tools,
        viewer_source=tmp_path / "missing",
    )
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.status == PdiStatus.PARTIAL
    assert any("OHIF" in warning for warning in result.warnings)
    output = Path(result.output_directory)
    assert (output / "DICOMDIR").is_file()
    assert (output / pdi.STUDY_INDEX).is_file()
    assert not (output / "VIEWER" / "OHIF").exists()
    index_html = (output / "INDEX.HTM").read_text(encoding="utf-8")
    assert "此页仅显示检查清单，不能直接看图" in index_html
    assert "本次导出未能加入离线阅片器" in index_html
    assert "原始 DICOM 和中文离线阅片器" not in index_html


def test_duplicate_uid_with_same_content_is_deduplicated(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _dicom(tmp_path / "first.dcm")
    duplicate = tmp_path / "duplicate.dcm"
    shutil.copy2(first, duplicate)
    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([first, duplicate])

    assert result.status == PdiStatus.COMPLETED
    assert result.exported_count == 1 and result.duplicate_count == 1
    assert len(_instances(Path(result.output_directory) / pdi.STUDY_INDEX)) == 1


def test_duplicate_uid_with_different_content_stops_export(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = generate_uid()
    first = _dicom(tmp_path / "first.dcm", sop_uid=uid)
    second = _dicom(tmp_path / "second.dcm", sop_uid=uid, patient_id="OTHER")
    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([first, second])

    assert result.status == PdiStatus.FAILED
    assert "内容不同" in result.message
    assert not list((tmp_path / "portable").glob("DCMGET_PDI_*"))


def test_strict_profile_falls_back_to_compatibility_and_marks_partial(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    commands: list[list[str]] = []
    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch, strict_fails=True, commands=commands)

    result = exporter.export([source])

    assert result.status == PdiStatus.PARTIAL
    assert result.strict_profile is False
    assert "-I" in commands[0]
    assert {"+I", "-Nxc", "-Nec", "-Nrc"} <= set(commands[1])


def test_missing_dcmmkdir_is_core_tool_failure(tmp_path: Path, tools: ToolPaths) -> None:
    source = _dicom(tmp_path / "source.dcm")
    Path(tools.dcmmkdir).unlink()

    result = PdiExporter(_config(tmp_path), tools).export([source])

    assert result.status == PdiStatus.FAILED
    assert result.core_tool_failure
    assert "dcmmkdir" in result.message


def test_dicomdir_generation_failure_does_not_publish(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch, all_fail=True)

    result = exporter.export([source])

    assert result.status == PdiStatus.FAILED
    assert "DICOMDIR" in result.message
    assert not list((tmp_path / "portable").glob("DCMGET_PDI_*"))


def test_cancel_removes_partial_and_keeps_source(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    original_hash = pdi._sha256(source)

    def progress(stage: PdiStage, current: int, _total: int, _message: str) -> None:
        if stage == PdiStage.PREPARING and current == 0:
            exporter.request_cancel()

    exporter = PdiExporter(_config(tmp_path), tools, progress_callback=progress)
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.status == PdiStatus.CANCELLED
    assert pdi._sha256(source) == original_hash
    assert not list((tmp_path / "portable").glob(".*.partial-*"))


def test_request_cancel_returns_before_process_cleanup_and_terminates_once(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    exporter = PdiExporter(_config(tmp_path), tools)
    process = Mock()
    exporter._current_process = process
    entered = threading.Event()
    release = threading.Event()
    calls: list[object] = []

    def terminate(target: object) -> None:
        calls.append(target)
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(exporter, "_terminate_process", terminate)
    fail_safe = threading.Timer(1, release.set)
    fail_safe.start()
    started = time.monotonic()
    try:
        exporter.request_cancel()
        elapsed = time.monotonic() - started
        assert elapsed < 0.5
        assert entered.wait(1)

        competing_cleanup = threading.Thread(
            target=exporter._terminate_process_safely,
            args=(process,),
        )
        competing_cleanup.start()
        exporter.request_cancel()
        release.set()
        competing_cleanup.join(timeout=2)
        cleanup = exporter._cancel_cleanup_thread
        assert cleanup is not None
        cleanup.join(timeout=2)
        assert not competing_cleanup.is_alive()
        assert not cleanup.is_alive()
    finally:
        release.set()
        fail_safe.cancel()

    assert calls == [process]


@pytest.mark.parametrize(
    "taskkill_error",
    [
        pdi.subprocess.TimeoutExpired(["taskkill"], 3),
        OSError("taskkill unavailable"),
    ],
    ids=["timeout", "os-error"],
)
def test_windows_taskkill_failure_has_timeout_and_falls_back_to_direct_kill(
    tmp_path: Path,
    tools: ToolPaths,
    monkeypatch: pytest.MonkeyPatch,
    taskkill_error: BaseException,
) -> None:
    exporter = PdiExporter(_config(tmp_path), tools)
    process = Mock()
    process.pid = 4321
    process.poll.return_value = None
    process.wait.return_value = 0
    taskkill = Mock(side_effect=taskkill_error)
    monkeypatch.setattr(pdi.os, "name", "nt")
    monkeypatch.setattr(pdi.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(pdi.subprocess, "run", taskkill)

    exporter._terminate_process(process)

    taskkill.assert_called_once()
    assert taskkill.call_args.kwargs["timeout"] == 3
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=3)


def test_crash_recovery_reuses_published_directory(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    task_id = "a" * 32
    first = PdiExporter(_config(tmp_path), tools, recovery_id=task_id)
    _fake_dcmtk(first, monkeypatch)
    original = first.export([source])

    second = PdiExporter(
        _config(tmp_path), tools, recovery_id=task_id, reuse_published=True
    )
    monkeypatch.setattr(
        second,
        "_prepare_items",
        Mock(side_effect=AssertionError("published PDI must not be rebuilt")),
    )
    restored = second.export([source])

    assert restored.status == PdiStatus.COMPLETED
    assert restored.output_directory == original.output_directory
    output = Path(restored.output_directory)
    marker = json.loads(
        (output / pdi.RECOVERY_MARKER_PATH).read_text(
            encoding="utf-8"
        )
    )
    assert marker["version"] == 2 and marker["indexed_count"] == 1
    assert not (output / pdi.RECOVERY_MARKER).exists()
    manifest = (output / "MANIFEST.SHA256").read_text(encoding="utf-8")
    assert pdi.RECOVERY_MARKER_PATH in manifest
    assert pdi.RECOVERY_MARKER not in manifest


def test_concurrent_pdi_publication_uses_unique_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "portable"
    output_root.mkdir()
    temporary_directories = []
    for number in (1, 2):
        temporary = output_root / f".export-{number}.partial"
        temporary.mkdir()
        (temporary / "source.txt").write_text(str(number), encoding="utf-8")
        temporary_directories.append(temporary)
    monkeypatch.setattr(
        pdi,
        "ensure_application_state_dir",
        lambda: tmp_path / "state",
    )

    barrier = threading.Barrier(2)
    published: list[Path] = []
    failures: list[BaseException] = []

    def publish(temporary: Path) -> None:
        try:
            barrier.wait(timeout=2)
            published.append(PdiExporter._publish_directory(temporary, output_root))
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    workers = [
        threading.Thread(target=publish, args=(temporary,))
        for temporary in temporary_directories
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert failures == []
    assert len(published) == 2
    assert len({path.name for path in published}) == 2
    assert all(path.is_dir() for path in published)
    assert {path.joinpath("source.txt").read_text(encoding="utf-8") for path in published} == {
        "1",
        "2",
    }


def test_restart_removes_only_matching_interrupted_partial(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    output_root = tmp_path / "portable"
    output_root.mkdir()
    matching = output_root / ".DCMGET_PDI_OLD.partial-deadbeef"
    other = output_root / ".DCMGET_PDI_OTHER.partial-deadbeef"
    matching.mkdir()
    other.mkdir()
    matching_marker = matching / pdi.RECOVERY_MARKER_PATH
    matching_marker.parent.mkdir(parents=True)
    matching_marker.write_text(
        json.dumps({"version": 1, "attempt_id": "b" * 32}), encoding="utf-8"
    )
    # Old exports stored the marker at the root; keep that recovery path valid.
    (other / pdi.RECOVERY_MARKER).write_text(
        json.dumps({"version": 2, "attempt_id": "c" * 32}), encoding="utf-8"
    )
    exporter = PdiExporter(_config(tmp_path), tools, recovery_id="b" * 32)
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.status == PdiStatus.COMPLETED
    assert not matching.exists() and other.exists()


def test_partial_cleanup_failure_is_reported_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partial = tmp_path / "portable" / ".DCMGET_PDI_OLD.partial-deadbeef"
    partial.mkdir(parents=True)
    (partial / pdi.RECOVERY_MARKER).write_text(
        json.dumps({"version": 2, "attempt_id": "d" * 32}), encoding="utf-8"
    )
    monkeypatch.setattr(pdi.shutil, "rmtree", Mock(side_effect=OSError("in use")))

    with pytest.raises(OSError, match="无法删除 PDI 暂存目录"):
        pdi.cleanup_interrupted_pdi(_config(tmp_path), "d" * 32)
    assert partial.exists()


def test_manifest_covers_index_viewer_launchers_and_dicom(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    viewer = _viewer(tmp_path / "ohif")
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_ohif_viewer=True), tools, viewer_source=viewer
    )
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    output = Path(result.output_directory)
    manifest = (output / "MANIFEST.SHA256").read_text(encoding="utf-8")
    for value in (
        "DICOMDIR",
        pdi.STUDY_INDEX,
        "VIEWER/OHIF/index.html",
        "VIEWER/pdi_server.py",
        "VIEWER/architecture.py",
        "OPEN_VIEWER.bat",
        "INDEX.HTM",
    ):
        assert value in manifest
    assert "MANIFEST.SHA256" not in manifest


def test_core_disk_shortage_fails_without_publishing(
    tmp_path: Path,
    tools: ToolPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    monkeypatch.setattr(
        pdi.shutil, "disk_usage", lambda _path: shutil._ntuple_diskusage(1, 1, 1)
    )

    result = PdiExporter(_config(tmp_path), tools).export([source])

    assert result.status == PdiStatus.FAILED
    assert "空间不足" in result.message


def test_optional_viewer_disk_shortage_publishes_partial(
    tmp_path: Path,
    tools: ToolPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    viewer = _viewer(tmp_path / "ohif")
    (viewer / "large.bin").write_bytes(b"x" * 1024)
    available = source.stat().st_size + 10 * 1024 * 1024 + 100
    monkeypatch.setattr(
        pdi.shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(available, 0, available),
    )
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_ohif_viewer=True), tools, viewer_source=viewer
    )
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.status == PdiStatus.PARTIAL
    assert any("空间不足" in warning for warning in result.warnings)
    assert (Path(result.output_directory) / "DICOMDIR").is_file()


def test_invalid_recovery_identifier_is_rejected(tmp_path: Path, tools: ToolPaths) -> None:
    with pytest.raises(ValueError, match="恢复标识"):
        PdiExporter(_config(tmp_path), tools, recovery_id="../escape")


@pytest.mark.parametrize(
    ("transfer_syntax", "expected"),
    [
        ("1.2.840.10008.1.2.1", "-Pgp"),
        ("1.2.840.10008.1.2.4.50", "-Pfl"),
        ("1.2.840.10008.1.2.4.90", "-Pf2"),
    ],
)
def test_strict_profile_selection(transfer_syntax: str, expected: str) -> None:
    item = pdi._DicomItem(
        source=Path("source.dcm"),
        file_id=("DICOM", "P000001", "S000001", "I000001"),
        digest="0" * 64,
        sop_instance_uid="1.2.3",
        transfer_syntax_uid=transfer_syntax,
        patient_name="",
        patient_id="",
        study_instance_uid="1.2.3.4",
        series_instance_uid="1.2.3.4.5",
        accession_number="",
        study_date="",
        modality="CT",
        study_description="",
        series_description="",
        metadata={},
    )
    assert pdi._strict_profile([item]) == expected
