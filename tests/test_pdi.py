from __future__ import annotations

import json
import shutil
import sys
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

from dcmget.config import AppConfig
from dcmget.core import ToolPaths
from dcmget import pdi
from dcmget.pdi import PdiExporter, PdiStage, PdiStatus


def _dicom(
    path: Path,
    *,
    sop_uid: str | None = None,
    patient_id: str = "PAT001",
    patient_name: str = "DcmGet^Patient",
    study_uid: str | None = None,
    series_uid: str | None = None,
    frames: int = 1,
    displayable: bool = True,
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
    dataset.Modality = "CT"
    dataset.StudyDescription = "PDI Test"
    dataset.SeriesDescription = "Series"
    if displayable:
        dataset.Rows = 1
        dataset.Columns = 1
        dataset.SamplesPerPixel = 1
        dataset.PhotometricInterpretation = "MONOCHROME2"
        dataset.BitsAllocated = 8
        dataset.BitsStored = 8
        dataset.HighBit = 7
        dataset.PixelRepresentation = 0
        if frames > 1:
            dataset.NumberOfFrames = str(frames)
        dataset.PixelData = b"\0" * frames
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
    dcmj2pnm = bin_dir / "dcmj2pnm"
    dcmmkdir.touch()
    dcmj2pnm.touch()
    return ToolPaths(
        movescu=bin_dir / "movescu",
        storescp=bin_dir / "storescp",
        bin_dir=bin_dir,
        version="3.7.0",
        dcmmkdir=dcmmkdir,
        dcmj2pnm=dcmj2pnm,
    )


def _config(tmp_path: Path, **overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "dicom_destination_folder": str(tmp_path / "download"),
        "pdi_output_folder": str(tmp_path / "portable"),
        "pdi_institution_name": "DcmGet Hospital",
        "pdi_include_html_preview": False,
        "pdi_include_weasis_windows": False,
    }
    values.update(overrides)
    return AppConfig(**values)


def _fake_dcmtk(
    exporter: PdiExporter,
    monkeypatch: pytest.MonkeyPatch,
    *,
    strict_fails: bool = False,
    preview_fails: bool = False,
    commands: list[list[str]] | None = None,
) -> None:
    def run(command: list[str], cwd: Path) -> pdi._CommandResult:
        if commands is not None:
            commands.append(command)
        if Path(command[0]).name.startswith("dcmmkdir"):
            if strict_fails and "-I" in command:
                return pdi._CommandResult(1, "strict profile rejected")
            _write_dicomdir(cwd)
            return pdi._CommandResult(0, "ok")
        if Path(command[0]).name.startswith("dcmj2pnm"):
            if preview_fails:
                return pdi._CommandResult(1, "unsupported image")
            destination = Path(command[-1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"JPEG")
            return pdi._CommandResult(0, "ok")
        raise AssertionError(command)

    monkeypatch.setattr(exporter, "_run_command", run)


def test_export_uses_only_exact_files_and_standard_extensionless_ids(
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
    assert result.source_count == result.exported_count == 2
    output = Path(result.output_directory)
    copied = sorted(path for path in (output / "DICOM").rglob("*") if path.is_file())
    assert len(copied) == 2
    assert all(path.suffix == "" for path in copied)
    assert all(
        len(part) <= 8 and set(part) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
        for path in copied
        for part in path.relative_to(output).parts
    )
    assert "HISTORY" not in (output / "INDEX.HTM").read_text(encoding="utf-8")
    index = (output / "INDEX.HTM").read_text(encoding="utf-8")
    assert "A^&lt;script&gt;" in index
    assert "http://" not in index.lower()
    assert "https://" not in index.lower()
    assert "fetch(" not in index.lower()
    assert all(pdi._sha256(path) == digest for path, digest in source_hashes.items())
    manifest = (output / "MANIFEST.SHA256").read_text(encoding="utf-8")
    assert "DICOMDIR" in manifest and "INDEX.HTM" in manifest


def test_crash_recovery_reuses_already_published_directory(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "download" / "source.dcm")
    task_id = "a" * 32
    first = PdiExporter(_config(tmp_path), tools, recovery_id=task_id)
    _fake_dcmtk(first, monkeypatch)

    original = first.export([source])

    assert original.status == PdiStatus.COMPLETED
    second = PdiExporter(
        _config(tmp_path),
        tools,
        recovery_id=task_id,
        reuse_published=True,
    )
    monkeypatch.setattr(
        second,
        "_prepare_items",
        Mock(side_effect=AssertionError("published PDI must not be rebuilt")),
    )
    restored = second.export([source])

    assert restored.status == PdiStatus.COMPLETED
    assert restored.output_directory == original.output_directory
    assert len(list((tmp_path / "portable").glob("DCMGET_PDI_*"))) == 1
    manifest = (
        Path(restored.output_directory) / "MANIFEST.SHA256"
    ).read_text(encoding="utf-8")
    assert pdi.RECOVERY_MARKER in manifest


def test_restart_removes_only_matching_interrupted_partial_directory(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "download" / "source.dcm")
    output_root = tmp_path / "portable"
    output_root.mkdir()
    matching = output_root / ".DCMGET_PDI_OLD.partial-deadbeef"
    other = output_root / ".DCMGET_PDI_OTHER.partial-deadbeef"
    matching.mkdir()
    other.mkdir()
    (matching / pdi.RECOVERY_MARKER).write_text(
        json.dumps({"version": 1, "attempt_id": "b" * 32}),
        encoding="utf-8",
    )
    (other / pdi.RECOVERY_MARKER).write_text(
        json.dumps({"version": 1, "attempt_id": "c" * 32}),
        encoding="utf-8",
    )
    exporter = PdiExporter(_config(tmp_path), tools, recovery_id="b" * 32)
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.status == PdiStatus.COMPLETED
    assert not matching.exists()
    assert other.exists()


def test_crashed_manual_retry_does_not_reuse_previous_attempt(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "download" / "source.dcm")
    first = PdiExporter(_config(tmp_path), tools, recovery_id="a" * 32)
    _fake_dcmtk(first, monkeypatch, strict_fails=True)
    previous = first.export([source])
    assert previous.status == PdiStatus.PARTIAL

    interrupted = tmp_path / "portable" / ".DCMGET_PDI_RETRY.partial-deadbeef"
    interrupted.mkdir()
    (interrupted / pdi.RECOVERY_MARKER).write_text(
        json.dumps(
            {
                "version": 1,
                "attempt_id": "b" * 32,
                "state": "building",
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    restarted = PdiExporter(
        _config(tmp_path),
        tools,
        recovery_id="b" * 32,
        reuse_published=True,
    )
    _fake_dcmtk(restarted, monkeypatch, commands=commands)

    recovered = restarted.export([source])

    assert recovered.status == PdiStatus.COMPLETED
    assert recovered.output_directory != previous.output_directory
    assert commands
    assert not interrupted.exists()


def test_identical_sop_uid_is_skipped_with_warning(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _dicom(tmp_path / "first.dcm")
    duplicate = tmp_path / "duplicate.dcm"
    shutil.copy2(first, duplicate)
    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([first, duplicate])

    assert result.status == PdiStatus.COMPLETED
    assert result.source_count == 2
    assert result.exported_count == 1
    assert result.duplicate_count == 1
    assert any("重复 DICOM" in warning for warning in result.warnings)


def test_conflicting_sop_uid_fails_without_publishing(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = generate_uid()
    first = _dicom(tmp_path / "first.dcm", sop_uid=uid, patient_id="ONE")
    second = _dicom(tmp_path / "second.dcm", sop_uid=uid, patient_id="TWO")
    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([first, second])

    assert result.status == PdiStatus.FAILED
    assert "内容不同" in result.message
    assert not list((tmp_path / "portable").glob("DCMGET_PDI_*"))
    assert not list((tmp_path / "portable").glob(".*.partial-*"))


def test_strict_failure_uses_compatibility_fallback(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    commands: list[list[str]] = []
    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch, strict_fails=True, commands=commands)

    result = exporter.export([source])

    assert result.status == PdiStatus.PARTIAL
    assert result.strict_profile is False
    assert len([command for command in commands if "dcmmkdir" in Path(command[0]).name]) == 2
    fallback = commands[1]
    assert all(option in fallback for option in ("+I", "-Nxc", "-Nec", "-Nrc"))
    readme = (Path(result.output_directory) / "README.TXT").read_text(encoding="utf-8")
    assert "不声称为严格 Profile" in readme


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("hybrid", 101), ("all", 121), ("series_cover", 1)],
)
def test_preview_modes_use_compact_jpeg_and_expected_frame_count(
    tmp_path: Path,
    tools: ToolPaths,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected: int,
) -> None:
    study_uid = generate_uid()
    series_uid = generate_uid()
    single = _dicom(
        tmp_path / "single.dcm", study_uid=study_uid, series_uid=series_uid
    )
    multi = _dicom(
        tmp_path / "multi.dcm",
        study_uid=study_uid,
        series_uid=series_uid,
        frames=120,
    )
    commands: list[list[str]] = []
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_html_preview=True, pdi_preview_mode=mode),
        tools,
    )
    _fake_dcmtk(exporter, monkeypatch, commands=commands)

    result = exporter.export([single, multi])

    assert result.status == PdiStatus.COMPLETED
    assert result.preview_count == expected
    preview_commands = [
        command for command in commands if "dcmj2pnm" in Path(command[0]).name
    ]
    assert len(preview_commands) == expected
    assert all("+oj" in command and command[command.index("+Jq") + 1] == "85" for command in preview_commands)
    assert all(Path(command[-1]).suffix == ".JPG" for command in preview_commands)


def test_non_displayable_and_conversion_failures_are_reported_partial(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = _dicom(tmp_path / "image.dcm")
    document = _dicom(tmp_path / "document.dcm", displayable=False)
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_html_preview=True), tools
    )
    _fake_dcmtk(exporter, monkeypatch, preview_fails=True)

    result = exporter.export([image, document])

    assert result.status == PdiStatus.PARTIAL
    assert result.unpreviewable_count == 2
    assert result.warnings and "2 个 DICOM" in result.warnings[-1]
    index = (Path(result.output_directory) / "INDEX.HTM").read_text(encoding="utf-8")
    assert "image.dcm" in index and "document.dcm" in index


def test_weasis_payload_and_official_ascii_launcher_are_copied(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    viewer = tmp_path / "Weasis"
    viewer.mkdir()
    (viewer / "Weasis.exe").write_bytes(b"exe")
    (viewer / "LICENSE.txt").write_text("Apache-2.0", encoding="ascii")
    (viewer / "THIRD_PARTY.txt").write_text("notices", encoding="ascii")
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_weasis_windows=True),
        tools,
        viewer_source=viewer,
    )
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.status == PdiStatus.COMPLETED
    output = Path(result.output_directory)
    assert (output / "VIEWER" / "WINDOWS" / "Weasis.exe").is_file()
    launcher = (output / "RUN.bat").read_bytes()
    assert not launcher.startswith(b"\xef\xbb\xbf")
    assert b'VIEWER\\WINDOWS\\Weasis.exe' in launcher
    assert b"weasis://%%24dicom%%3Aget" in launcher


def test_missing_viewer_publishes_partial_html_and_dicomdir(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_weasis_windows=True),
        tools,
        viewer_source=tmp_path / "missing",
    )
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.status == PdiStatus.PARTIAL
    assert Path(result.output_directory, "DICOMDIR").is_file()
    assert any("Weasis" in warning for warning in result.warnings)


def test_preview_start_failure_still_publishes_valid_dicomdir(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_html_preview=True), tools
    )
    _fake_dcmtk(exporter, monkeypatch)
    monkeypatch.setattr(
        exporter,
        "_create_previews",
        lambda *_args: (_ for _ in ()).throw(PermissionError("cannot execute")),
    )

    result = exporter.export([source])

    assert result.status == PdiStatus.PARTIAL
    assert Path(result.output_directory, "DICOMDIR").is_file()
    assert any("网页预览生成失败" in warning for warning in result.warnings)


def test_viewer_copy_failure_still_publishes_valid_dicomdir(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    viewer = tmp_path / "Weasis"
    viewer.mkdir()
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_weasis_windows=True),
        tools,
        viewer_source=viewer,
    )
    _fake_dcmtk(exporter, monkeypatch)
    monkeypatch.setattr(
        exporter,
        "_copy_weasis",
        lambda *_args: (_ for _ in ()).throw(OSError("copy failed")),
    )

    result = exporter.export([source])

    assert result.status == PdiStatus.PARTIAL
    assert Path(result.output_directory, "DICOMDIR").is_file()
    assert any("查看器加入失败" in warning for warning in result.warnings)


def test_cancel_removes_partial_directory(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    exporter: PdiExporter

    def progress(stage: PdiStage, current: int, _total: int, _message: str) -> None:
        if stage == PdiStage.PREPARING and current == 0:
            exporter.request_cancel()

    exporter = PdiExporter(_config(tmp_path), tools, progress_callback=progress)
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.status == PdiStatus.CANCELLED
    assert not list((tmp_path / "portable").iterdir())


def test_cancel_terminates_current_dcmtk_process(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    exporter = PdiExporter(_config(tmp_path), tools)
    process = Mock()
    exporter._current_process = process
    terminate = Mock()
    monkeypatch.setattr(exporter, "_terminate_process", terminate)

    exporter.request_cancel()

    terminate.assert_called_once_with(process)


def test_dcmtk_command_drains_large_output_without_deadlock(
    tmp_path: Path, tools: ToolPaths
) -> None:
    process_events = []
    exporter = PdiExporter(
        _config(tmp_path),
        tools,
        process_callback=lambda *event: process_events.append(event),
    )

    result = exporter._run_command(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 2_000_000)"],
        tmp_path,
    )

    assert result.returncode == 0
    assert len(result.output) == 2_000_000
    assert [event[3] for event in process_events] == [True, False]
    assert all(event[0] == "pdi" for event in process_events)
    assert all(event[2] == sys.executable for event in process_events)


def test_core_tool_start_failure_is_classified_for_cli_exit_code(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    exporter = PdiExporter(_config(tmp_path), tools)
    monkeypatch.setattr(
        exporter,
        "_create_dicomdir",
        lambda *_args: (_ for _ in ()).throw(pdi.PdiCoreToolError("cannot start")),
    )

    result = exporter.export([source])

    assert result.status == PdiStatus.FAILED
    assert result.core_tool_failure
    assert not list((tmp_path / "portable").glob("DCMGET_PDI_*"))


@pytest.mark.parametrize(
    ("compressed", "expected"),
    [
        ("1.2.840.10008.1.2.4.50", "-Pfl"),
        ("1.2.840.10008.1.2.4.90", "-Pf2"),
    ],
)
def test_strict_usb_profile_accepts_mixed_explicit_vr_and_compressed(
    compressed: str, expected: str
) -> None:
    items = [
        Mock(transfer_syntax_uid="1.2.840.10008.1.2.1"),
        Mock(transfer_syntax_uid=compressed),
    ]

    assert pdi._strict_profile(items) == expected


def test_invalid_dicomdir_is_not_published(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    exporter = PdiExporter(_config(tmp_path), tools)

    def bad_dcmtk(_command: list[str], cwd: Path) -> pdi._CommandResult:
        _write_dicomdir(cwd)
        dataset = dcmread(cwd / "DICOMDIR")
        dataset.DirectoryRecordSequence[0].ReferencedFileID = ["DICOM", "MISSING"]
        dataset.save_as(cwd / "DICOMDIR", enforce_file_format=True)
        return pdi._CommandResult(0, "ok")

    monkeypatch.setattr(exporter, "_run_command", bad_dcmtk)

    result = exporter.export([source])

    assert result.status == PdiStatus.FAILED
    assert "不存在" in result.message
    assert not list((tmp_path / "portable").glob("DCMGET_PDI_*"))


def test_incomplete_sha256_manifest_is_not_published(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    exporter = PdiExporter(_config(tmp_path), tools)
    _fake_dcmtk(exporter, monkeypatch)
    original = exporter._write_manifest

    def write_incomplete(root: Path) -> None:
        original(root)
        manifest = root / "MANIFEST.SHA256"
        lines = [
            line
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if not line.endswith("  INDEX.HTM")
        ]
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    monkeypatch.setattr(exporter, "_write_manifest", write_incomplete)

    result = exporter.export([source])

    assert result.status == PdiStatus.FAILED
    assert "SHA-256 清单" in result.message
    assert not list((tmp_path / "portable").glob("DCMGET_PDI_*"))


def test_optional_preview_is_skipped_when_only_core_disk_space_is_available(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    available = source.stat().st_size + 10 * 1024 * 1024 + 1
    monkeypatch.setattr(pdi.shutil, "disk_usage", lambda _path: Mock(free=available))
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_html_preview=True), tools
    )
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.status == PdiStatus.PARTIAL
    assert Path(result.output_directory, "DICOMDIR").is_file()
    assert any("空间不足" in warning and "网页" in warning for warning in result.warnings)


def test_disabled_preview_does_not_reserve_optional_disk_space(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    available = source.stat().st_size + 10 * 1024 * 1024
    monkeypatch.setattr(pdi.shutil, "disk_usage", lambda _path: Mock(free=available))
    exporter = PdiExporter(
        _config(tmp_path, pdi_include_html_preview=False), tools
    )
    _fake_dcmtk(exporter, monkeypatch)

    result = exporter.export([source])

    assert result.status == PdiStatus.COMPLETED


def test_insufficient_disk_space_fails_before_partial_directory_is_created(
    tmp_path: Path, tools: ToolPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm")
    exporter = PdiExporter(_config(tmp_path), tools)
    monkeypatch.setattr(
        exporter,
        "_check_free_space",
        lambda *_args: (_ for _ in ()).throw(OSError("PDI 导出空间不足")),
    )

    result = exporter.export([source])

    assert result.status == PdiStatus.FAILED
    assert "空间不足" in result.message
    assert not list((tmp_path / "portable").glob(".*.partial-*"))


def test_output_directory_name_never_overwrites_existing_export(
    tmp_path: Path, tools: ToolPaths
) -> None:
    exporter = PdiExporter(_config(tmp_path), tools)
    output_root = tmp_path / "portable"
    output_root.mkdir()
    first = exporter._next_output_directory(output_root)
    first.mkdir()

    second = exporter._next_output_directory(output_root)

    assert second != first
    assert second.name.startswith(first.name + "-")
