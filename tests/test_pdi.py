from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydicom import dcmread
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
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
) -> Path:
    sop_uid = sop_uid or generate_uid()
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
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


def _viewer(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "index.html").write_text("<html><script src='/app.js'></script></html>")
    (path / "app-config.js").write_text("window.config={};")
    (path / "app.js").write_bytes(b"offline-ohif")
    (path / "LICENSE-OHIF.txt").write_text("MIT")
    (path / "THIRD_PARTY-OHIF.md").write_text("third party")
    return path


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
    payload_text = (output / pdi.STUDY_INDEX).read_text(encoding="utf-8")
    instances = _instances(output / pdi.STUDY_INDEX)
    assert [entry["metadata"]["InstanceNumber"] for entry in instances] == [1, 2]
    assert all(str(entry["url"]).startswith("dicomweb:/DICOM/") for entry in instances)
    assert "http://" not in payload_text and "https://" not in payload_text
    assert "SECRET-PRIVATE" not in payload_text
    assert all((output / str(entry["url"])[10:]).is_file() for entry in instances)
    assert instances[0]["metadata"]["PixelSpacing"] == [0.5, 0.5]
    assert "PixelData" not in instances[0]["metadata"]


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
    for name in ("OPEN_VIEWER.bat", "OPEN_VIEWER.command", "OPEN_VIEWER.sh"):
        assert (output / name).is_file()
    assert "127.0.0.1" in (output / "README.TXT").read_text(encoding="utf-8")
    assert "Weasis" not in (output / "README.TXT").read_text(encoding="utf-8")


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
    marker = json.loads(
        (Path(restored.output_directory) / pdi.RECOVERY_MARKER).read_text()
    )
    assert marker["version"] == 2 and marker["indexed_count"] == 1


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
    (matching / pdi.RECOVERY_MARKER).write_text(
        json.dumps({"version": 1, "attempt_id": "b" * 32}), encoding="utf-8"
    )
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
