from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    PYDICOM_IMPLEMENTATION_UID,
    generate_uid,
)

import dcmget.pdi as pdi
import dcmget.pdi_volume as pdi_volume
from dcmget.config import AppConfig
from dcmget.core import ToolPaths
from dcmget.pdi import PdiExportResult, PdiStatus, PdiVolumeExporter
from dcmget.pdi_volume import plan_pdi_volumes


def _dicom(
    path: Path,
    study_uid: str | None,
    *,
    payload_bytes: int = 0,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_meta = FileMetaDataset()
    file_meta.FileMetaInformationVersion = b"\x00\x01"
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID
    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    if study_uid is not None:
        dataset.StudyInstanceUID = study_uid
    if payload_bytes:
        dataset.Rows = 1
        dataset.Columns = payload_bytes
        dataset.SamplesPerPixel = 1
        dataset.PhotometricInterpretation = "MONOCHROME2"
        dataset.BitsAllocated = 8
        dataset.BitsStored = 8
        dataset.HighBit = 7
        dataset.PixelRepresentation = 0
        dataset.PixelData = b"x" * payload_bytes
    dataset.save_as(path, enforce_file_format=True)
    return path


def test_zero_capacity_keeps_all_studies_in_one_volume_and_stable_order(
    tmp_path: Path,
) -> None:
    study_a = generate_uid()
    study_b = generate_uid()
    a1 = _dicom(tmp_path / "a1.dcm", study_a)
    b1 = _dicom(tmp_path / "b1.dcm", study_b)
    a2 = _dicom(tmp_path / "a2.dcm", study_a)

    plan = plan_pdi_volumes([a1, b1, a2], capacity_bytes=0)

    assert plan.capacity_bytes == 0
    assert not plan.split and len(plan.volumes) == 1
    volume = plan.volumes[0]
    assert volume.files == (a1.resolve(), a2.resolve(), b1.resolve())
    assert volume.study_instance_uids == (study_a, study_b)
    assert volume.file_count == 3 and volume.study_count == 2
    assert volume.total_bytes == sum(path.stat().st_size for path in (a1, a2, b1))
    assert plan.total_files == 3 and plan.total_studies == 2
    assert not plan.warnings


def test_positive_capacity_splits_between_studies_never_inside_study(
    tmp_path: Path,
) -> None:
    study_a = generate_uid()
    study_b = generate_uid()
    a1 = _dicom(tmp_path / "a1.dcm", study_a, payload_bytes=1024)
    a2 = _dicom(tmp_path / "a2.dcm", study_a, payload_bytes=1024)
    b1 = _dicom(tmp_path / "b1.dcm", study_b, payload_bytes=512)
    study_a_bytes = a1.stat().st_size + a2.stat().st_size

    plan = plan_pdi_volumes([a1, b1, a2], capacity_bytes=study_a_bytes)

    assert plan.split and len(plan.volumes) == 2
    assert plan.volumes[0].files == (a1.resolve(), a2.resolve())
    assert plan.volumes[0].study_instance_uids == (study_a,)
    assert plan.volumes[0].total_bytes == study_a_bytes
    assert plan.volumes[1].files == (b1.resolve(),)
    assert plan.volumes[1].study_instance_uids == (study_b,)
    assert not plan.warnings


def test_planning_reserves_fixed_and_per_file_export_overhead(tmp_path: Path) -> None:
    first = _dicom(tmp_path / "first.dcm", generate_uid())
    second = _dicom(tmp_path / "second.dcm", generate_uid())
    raw_total = first.stat().st_size + second.stat().st_size

    without_reserve = plan_pdi_volumes([first, second], raw_total)
    with_reserve = plan_pdi_volumes(
        [first, second],
        raw_total,
        fixed_volume_overhead_bytes=1,
        per_file_overhead_bytes=1,
    )

    assert len(without_reserve.volumes) == 1
    assert len(with_reserve.volumes) == 2


def test_oversized_study_gets_dedicated_volume_and_warning(tmp_path: Path) -> None:
    large_uid = generate_uid()
    small_uid = generate_uid()
    large = _dicom(tmp_path / "large.dcm", large_uid, payload_bytes=16_384)
    small = _dicom(tmp_path / "small.dcm", small_uid, payload_bytes=64)
    capacity = large.stat().st_size - 1
    assert small.stat().st_size <= capacity

    plan = plan_pdi_volumes([large, small], capacity)

    assert len(plan.volumes) == 2
    assert plan.volumes[0].files == (large.resolve(),)
    assert plan.volumes[0].oversized
    assert plan.volumes[0].total_bytes > capacity
    assert plan.volumes[1].files == (small.resolve(),)
    assert not plan.volumes[1].oversized
    oversized = [warning for warning in plan.warnings if warning.code == "study_exceeds_capacity"]
    assert len(oversized) == 1
    assert oversized[0].study_instance_uid == large_uid
    assert "不会拆分" in oversized[0].message


def test_missing_study_uid_is_kept_as_independent_study(tmp_path: Path) -> None:
    first = _dicom(tmp_path / "first.dcm", None)
    second = _dicom(tmp_path / "second.dcm", None)

    plan = plan_pdi_volumes([first, second], capacity_bytes=0)

    assert plan.total_studies == 2
    assert plan.volumes[0].study_count == 2
    assert plan.volumes[0].study_instance_uids == (
        "MISSING-STUDY-000001",
        "MISSING-STUDY-000002",
    )
    assert [warning.code for warning in plan.warnings] == [
        "missing_study_instance_uid",
        "missing_study_instance_uid",
    ]


def test_rejects_missing_file_and_negative_capacity(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="DICOM 文件不存在"):
        plan_pdi_volumes([tmp_path / "missing.dcm"], 0)
    with pytest.raises(ValueError, match="不能为负数"):
        plan_pdi_volumes([], -1)


def test_rejects_unreadable_dicom(tmp_path: Path) -> None:
    source = tmp_path / "not-dicom.bin"
    source.write_bytes(b"not a DICOM file")

    with pytest.raises(ValueError, match="无法读取 DICOM 文件"):
        plan_pdi_volumes([source], 0)


def test_planning_reads_only_metadata_before_pixel_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "large.dcm", generate_uid(), payload_bytes=4096)
    calls = []
    real_dcmread = pdi_volume.dcmread

    def recording_dcmread(path, **kwargs):
        calls.append(kwargs)
        return real_dcmread(path, **kwargs)

    monkeypatch.setattr(pdi_volume, "dcmread", recording_dcmread)

    plan_pdi_volumes([source], 0)

    assert calls == [
        {
            "stop_before_pixels": True,
            "specific_tags": ["StudyInstanceUID"],
        }
    ]


def test_repeated_planning_is_deterministic_and_serializable(tmp_path: Path) -> None:
    first_uid = generate_uid()
    second_uid = generate_uid()
    first = _dicom(tmp_path / "first.dcm", first_uid)
    second = _dicom(tmp_path / "second.dcm", second_uid)
    capacity = first.stat().st_size

    first_plan = plan_pdi_volumes([first, second], capacity)
    second_plan = plan_pdi_volumes([first, second], capacity)

    assert first_plan == second_plan
    payload = first_plan.to_dict()
    assert payload["total_files"] == 2
    assert payload["volumes"][0]["number"] == 1
    assert payload["volumes"][0]["file_count"] == 1


def test_empty_input_produces_empty_plan() -> None:
    plan = plan_pdi_volumes([], 0)

    assert plan.volumes == ()
    assert plan.total_files == plan.total_bytes == plan.total_studies == 0
    assert not plan.split and not plan.warnings


def test_volume_exporter_empty_input_returns_failed_result(tmp_path: Path) -> None:
    config = AppConfig(
        pdi_institution_name="Test Hospital",
        pdi_volume_size_bytes=700 * 1024 * 1024,
    )
    tools = ToolPaths(
        movescu=tmp_path / "movescu",
        storescp=tmp_path / "storescp",
        bin_dir=tmp_path,
        version="3.7.0",
    )

    result = PdiVolumeExporter(config, tools).export([])

    assert result.status == PdiStatus.FAILED
    assert result.volume_count == 0
    assert "没有可导出" in result.message


def test_volume_exporter_publishes_independent_volumes_without_source_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _dicom(tmp_path / "patient-one" / "first.dcm", generate_uid())
    second = _dicom(tmp_path / "patient-two" / "second.dcm", generate_uid())
    capacity = max(first.stat().st_size, second.stat().st_size)
    output_root = tmp_path / "portable"
    config = AppConfig(
        dicom_destination_folder=str(tmp_path / "download"),
        pdi_output_folder=str(output_root),
        pdi_institution_name="Test Hospital",
        pdi_volume_size_bytes=capacity,
    )
    tools = ToolPaths(
        movescu=tmp_path / "bin" / "movescu",
        storescp=tmp_path / "bin" / "storescp",
        bin_dir=tmp_path / "bin",
        version="3.7.0",
    )
    exporter = PdiVolumeExporter(config, tools)
    sequence = iter((1, 2))

    class FakeExporter:
        def __init__(self, volume_config: AppConfig, number: int):
            self.volume_config = volume_config
            self.number = number

        def export(self, files: list[Path]) -> PdiExportResult:
            generated = (
                Path(self.volume_config.pdi_output_folder)
                / f"DCMGET_PDI_FAKE_{self.number}"
            )
            generated.mkdir()
            (generated / "DICOMDIR").write_bytes(b"fake")
            return PdiExportResult(
                status=PdiStatus.COMPLETED,
                output_directory=str(generated),
                source_count=len(files),
                exported_count=len(files),
                indexed_count=len(files),
                strict_profile=True,
            )

        def request_cancel(self) -> None:
            return None

    def make_exporter(
        volume_config: AppConfig, *, volume_number: int = 0, volume_total: int = 0
    ) -> FakeExporter:
        assert volume_total == 2
        number = next(sequence)
        assert volume_number == number
        return FakeExporter(volume_config, number)

    monkeypatch.setattr(exporter, "_make_exporter", make_exporter)

    result = exporter.export([first, second])

    assert result.status == PdiStatus.COMPLETED
    assert result.volume_count == 2
    root = Path(result.output_directory)
    assert [Path(path).name for path in result.output_directories] == [
        "VOLUME_001",
        "VOLUME_002",
    ]
    assert (root / "VOLUME_001" / "DICOMDIR").is_file()
    assert (root / "VOLUME_002" / "DICOMDIR").is_file()
    payload = json.loads((root / "VOLUME_SET.json").read_text(encoding="utf-8"))
    assert payload["plan"]["total_files"] == 2
    assert all("files" not in volume for volume in payload["plan"]["volumes"])
    assert str(tmp_path) not in json.dumps(payload, ensure_ascii=False)


def test_volume_exporter_does_not_publish_partial_set_when_a_volume_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _dicom(tmp_path / "first.dcm", generate_uid())
    second = _dicom(tmp_path / "second.dcm", generate_uid())
    config = AppConfig(
        dicom_destination_folder=str(tmp_path / "download"),
        pdi_output_folder=str(tmp_path / "portable"),
        pdi_institution_name="Test Hospital",
        pdi_volume_size_bytes=max(first.stat().st_size, second.stat().st_size),
    )
    tools = ToolPaths(
        movescu=tmp_path / "bin" / "movescu",
        storescp=tmp_path / "bin" / "storescp",
        bin_dir=tmp_path / "bin",
        version="3.7.0",
    )
    exporter = PdiVolumeExporter(config, tools)
    calls = 0

    class FakeExporter:
        def __init__(self, volume_config: AppConfig, should_fail: bool):
            self.volume_config = volume_config
            self.should_fail = should_fail

        def export(self, files: list[Path]) -> PdiExportResult:
            if self.should_fail:
                return PdiExportResult(
                    status=PdiStatus.FAILED,
                    message="simulated failure",
                    source_count=len(files),
                )
            generated = Path(self.volume_config.pdi_output_folder) / "first-volume"
            generated.mkdir()
            return PdiExportResult(
                status=PdiStatus.COMPLETED,
                output_directory=str(generated),
                exported_count=len(files),
            )

        def request_cancel(self) -> None:
            return None

    def make_exporter(
        volume_config: AppConfig, *, volume_number: int = 0, volume_total: int = 0
    ) -> FakeExporter:
        nonlocal calls
        calls += 1
        return FakeExporter(volume_config, calls == 2)

    monkeypatch.setattr(exporter, "_make_exporter", make_exporter)

    result = exporter.export([first, second])

    assert result.status == PdiStatus.FAILED
    assert "第 2 卷生成失败" in result.message
    output_root = Path(config.pdi_output_folder)
    assert not list(output_root.glob("DCMGET_PDI_SET_*"))
    assert not list(output_root.glob(".*.partial-*"))


def test_volume_exporter_forwards_cancel_that_wins_exporter_creation_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _dicom(tmp_path / "source.dcm", generate_uid())
    config = AppConfig(
        dicom_destination_folder=str(tmp_path / "download"),
        pdi_output_folder=str(tmp_path / "portable"),
        pdi_institution_name="Test Hospital",
        pdi_volume_size_bytes=0,
    )
    tools = ToolPaths(
        movescu=tmp_path / "bin" / "movescu",
        storescp=tmp_path / "bin" / "storescp",
        bin_dir=tmp_path / "bin",
        version="3.7.0",
    )
    exporter = PdiVolumeExporter(config, tools)
    cancelled = []

    class FakeExporter:
        def request_cancel(self) -> None:
            cancelled.append(True)

        def export(self, _files) -> PdiExportResult:
            return PdiExportResult(status=PdiStatus.CANCELLED)

    monkeypatch.setattr(exporter, "_make_exporter", lambda *_args, **_kwargs: FakeExporter())
    exporter._cancel.set()

    result = exporter.export([source])

    assert result.status == PdiStatus.CANCELLED
    assert cancelled == [True]


def test_volume_planning_cancel_stops_before_reading_remaining_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _dicom(tmp_path / "first.dcm", generate_uid())
    second = _dicom(tmp_path / "second.dcm", generate_uid())
    config = AppConfig(
        dicom_destination_folder=str(tmp_path / "download"),
        pdi_output_folder=str(tmp_path / "portable"),
        pdi_institution_name="Test Hospital",
        pdi_volume_size_bytes=700 * 1024 * 1024,
    )
    tools = ToolPaths(
        movescu=tmp_path / "bin" / "movescu",
        storescp=tmp_path / "bin" / "storescp",
        bin_dir=tmp_path / "bin",
        version="3.7.0",
    )
    exporter = PdiVolumeExporter(config, tools)
    real_dcmread = pdi_volume.dcmread
    reads = []

    def cancel_after_first_read(path: Path, **kwargs):
        reads.append(Path(path))
        dataset = real_dcmread(path, **kwargs)
        exporter.request_cancel()
        return dataset

    monkeypatch.setattr(pdi_volume, "dcmread", cancel_after_first_read)

    result = exporter.export([first, second])

    assert result.status == PdiStatus.CANCELLED
    assert reads == [first.resolve()]
    assert not Path(config.pdi_output_folder).exists()


def test_volume_cancel_after_metadata_does_not_publish_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _dicom(tmp_path / "first.dcm", generate_uid())
    second = _dicom(tmp_path / "second.dcm", generate_uid())
    config = AppConfig(
        dicom_destination_folder=str(tmp_path / "download"),
        pdi_output_folder=str(tmp_path / "portable"),
        pdi_institution_name="Test Hospital",
        pdi_volume_size_bytes=1,
    )
    tools = ToolPaths(
        movescu=tmp_path / "bin" / "movescu",
        storescp=tmp_path / "bin" / "storescp",
        bin_dir=tmp_path / "bin",
        version="3.7.0",
    )
    exporter = PdiVolumeExporter(config, tools)

    class FakeExporter:
        def __init__(self, volume_config: AppConfig, number: int):
            self.volume_config = volume_config
            self.number = number

        def export(self, files: list[Path]) -> PdiExportResult:
            generated = Path(self.volume_config.pdi_output_folder) / f"generated-{self.number}"
            generated.mkdir()
            (generated / "DICOMDIR").write_bytes(b"")
            return PdiExportResult(
                status=PdiStatus.COMPLETED,
                output_directory=str(generated),
                exported_count=len(files),
                strict_profile=True,
            )

        def request_cancel(self) -> None:
            return None

    monkeypatch.setattr(
        exporter,
        "_make_exporter",
        lambda volume_config, *, volume_number=0, volume_total=0: FakeExporter(
            volume_config, volume_number
        ),
    )
    write_metadata = pdi._write_volume_set_metadata

    def cancel_after_metadata(root, plan, results) -> None:
        write_metadata(root, plan, results)
        exporter.request_cancel()

    monkeypatch.setattr(pdi, "_write_volume_set_metadata", cancel_after_metadata)

    result = exporter.export([first, second])

    output_root = Path(config.pdi_output_folder)
    assert result.status == PdiStatus.CANCELLED
    assert not list(output_root.glob("DCMGET_PDI_SET_*"))
    assert not list(output_root.glob(".*.partial-*"))


def test_volume_outer_partial_marker_is_recoverable_after_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _dicom(tmp_path / "first.dcm", generate_uid())
    second = _dicom(tmp_path / "second.dcm", generate_uid())
    attempt_id = "e" * 32
    config = AppConfig(
        dicom_destination_folder=str(tmp_path / "download"),
        pdi_output_folder=str(tmp_path / "portable"),
        pdi_institution_name="Test Hospital",
        pdi_volume_size_bytes=1,
    )
    tools = ToolPaths(
        movescu=tmp_path / "bin" / "movescu",
        storescp=tmp_path / "bin" / "storescp",
        bin_dir=tmp_path / "bin",
        version="3.7.0",
    )
    exporter = PdiVolumeExporter(config, tools, recovery_id=attempt_id)

    class FailedExporter:
        def export(self, _files) -> PdiExportResult:
            return PdiExportResult(status=PdiStatus.FAILED, message="failed")

        def request_cancel(self) -> None:
            return None

    monkeypatch.setattr(
        exporter,
        "_make_exporter",
        lambda *_args, **_kwargs: FailedExporter(),
    )
    real_rmtree = pdi.shutil.rmtree
    monkeypatch.setattr(
        pdi.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("in use")),
    )

    result = exporter.export([first, second])

    partials = list(Path(config.pdi_output_folder).glob(".*.partial-*"))
    assert result.status == PdiStatus.FAILED
    assert len(partials) == 1
    marker = json.loads(
        (partials[0] / pdi.RECOVERY_MARKER_PATH).read_text(encoding="utf-8")
    )
    assert marker["attempt_id"] == attempt_id

    monkeypatch.setattr(pdi.shutil, "rmtree", real_rmtree)
    assert pdi.cleanup_interrupted_pdi(config, attempt_id) == partials
    assert not partials[0].exists()


def test_volume_cleanup_failure_is_visible_and_partial_is_not_silently_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _dicom(tmp_path / "first.dcm", generate_uid())
    second = _dicom(tmp_path / "second.dcm", generate_uid())
    config = AppConfig(
        dicom_destination_folder=str(tmp_path / "download"),
        pdi_output_folder=str(tmp_path / "portable"),
        pdi_institution_name="Test Hospital",
        pdi_volume_size_bytes=max(first.stat().st_size, second.stat().st_size),
    )
    tools = ToolPaths(
        movescu=tmp_path / "bin" / "movescu",
        storescp=tmp_path / "bin" / "storescp",
        bin_dir=tmp_path / "bin",
        version="3.7.0",
    )
    exporter = PdiVolumeExporter(config, tools)

    class FailedExporter:
        def export(self, _files) -> PdiExportResult:
            return PdiExportResult(status=PdiStatus.FAILED, message="failed")

        def request_cancel(self) -> None:
            return None

    monkeypatch.setattr(
        exporter,
        "_make_exporter",
        lambda *_args, **_kwargs: FailedExporter(),
    )
    monkeypatch.setattr(
        "dcmget.pdi.shutil.rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("in use")),
    )

    result = exporter.export([first, second])

    assert result.status == PdiStatus.FAILED
    assert "可能仍包含患者影像" in result.message
    assert any("人工删除" in warning for warning in result.warnings)


def test_multi_study_single_volume_is_not_kept_when_final_size_exceeds_capacity(
    tmp_path: Path,
) -> None:
    config = AppConfig(pdi_institution_name="Test Hospital")
    tools = ToolPaths(
        movescu=tmp_path / "movescu",
        storescp=tmp_path / "storescp",
        bin_dir=tmp_path,
        version="3.7.0",
    )
    exporter = PdiVolumeExporter(config, tools)
    output = tmp_path / "published"
    output.mkdir()
    (output / "payload.bin").write_bytes(b"x" * 32)
    result = PdiExportResult(
        status=PdiStatus.COMPLETED,
        output_directory=str(output),
        output_directories=[str(output)],
    )

    exporter._apply_single_volume_capacity_result(
        result,
        SimpleNamespace(study_count=2),
        16,
    )

    assert result.status == PdiStatus.FAILED
    assert not output.exists()
    assert result.output_directory == ""


def test_multi_study_over_capacity_cleanup_failure_keeps_valid_partial_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig(pdi_institution_name="Test Hospital")
    tools = ToolPaths(
        movescu=tmp_path / "movescu",
        storescp=tmp_path / "storescp",
        bin_dir=tmp_path,
        version="3.7.0",
    )
    logs = []
    exporter = PdiVolumeExporter(
        config,
        tools,
        log_callback=lambda source, message, level: logs.append(
            (source, message, level)
        ),
    )
    output = tmp_path / "published"
    output.mkdir()
    (output / "DICOMDIR").write_bytes(b"x" * 32)
    result = PdiExportResult(
        status=PdiStatus.COMPLETED,
        output_directory=str(output),
        output_directories=[str(output)],
    )
    monkeypatch.setattr(
        pdi.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("in use")),
    )

    exporter._apply_single_volume_capacity_result(
        result,
        SimpleNamespace(study_count=2),
        16,
    )

    assert result.status == PdiStatus.PARTIAL
    assert result.output_directory == str(output)
    assert result.output_directories == [str(output)]
    assert output.is_dir()
    assert "目录保持完整" in result.message
    assert "需人工处理" in result.message
    assert "仍包含患者影像" in result.warnings[-1]
    assert logs[-1][2] == "error"
    assert "已停止发布" not in result.message


def test_single_study_over_capacity_is_kept_with_explicit_warning(tmp_path: Path) -> None:
    config = AppConfig(pdi_institution_name="Test Hospital")
    tools = ToolPaths(
        movescu=tmp_path / "movescu",
        storescp=tmp_path / "storescp",
        bin_dir=tmp_path,
        version="3.7.0",
    )
    exporter = PdiVolumeExporter(config, tools)
    output = tmp_path / "published"
    output.mkdir()
    (output / "payload.bin").write_bytes(b"x" * 32)
    result = PdiExportResult(
        status=PdiStatus.COMPLETED,
        output_directory=str(output),
    )

    exporter._apply_single_volume_capacity_result(
        result,
        SimpleNamespace(study_count=1),
        16,
    )

    assert result.status == PdiStatus.PARTIAL
    assert output.is_dir()
    assert "单个 Study" in result.warnings[-1]
