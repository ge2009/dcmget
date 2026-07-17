from __future__ import annotations

import hashlib
import html
import json
import locale
import os
import re
import shutil
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from filelock import FileLock
from pydicom import dcmread
from pydicom.charset import convert_encodings
from pydicom.datadict import dictionary_VR, keyword_for_tag
from pydicom.dataelem import RawDataElement
from pydicom.dataset import Dataset

from .config import AppConfig
from .core import ToolPaths
from .runtime import ensure_application_state_dir, resource_root


OHIF_VERSION = "3.12.6"
OHIF_PAYLOAD_CHECKSUMS = "DCMGET_PAYLOAD.SHA256"
# The viewer metadata is an implementation detail.  Keep it below the viewer
# data directory so users open the PDI root, not a JSON document.
STUDY_INDEX = "VIEWER/.dcmget/index"
# New exports keep implementation metadata away from the user-facing PDI root.
# RECOVERY_MARKER remains the legacy public name because older recovery/UI test
# fixtures still use it; all new writes use RECOVERY_MARKER_PATH.
RECOVERY_MARKER = ".DCMGET-EXPORT.JSON"
RECOVERY_MARKER_PATH = "VIEWER/.dcmget/recovery"
MAX_OHIF_INDEX_FRAMES = 100_000
MAX_OHIF_INDEX_ESTIMATED_BYTES = 64 * 1024 * 1024
_CHARSET_TEXT_VRS = {"LO", "LT", "PN", "SH", "ST", "UC", "UT"}
_CHARSET_SAMPLE_LIMIT = 32 * 1024
OHIF_INDEX_EXCLUDED_KEYWORDS = {
    "AdditionalPatientHistory",
    "AdmissionID",
    "CurrentPatientLocation",
    "InstitutionAddress",
    "InstitutionalDepartmentName",
    "MedicalRecordLocator",
    "Occupation",
    "OperatorsName",
    "OtherPatientIDs",
    "OtherPatientIDsSequence",
    "OtherPatientNames",
    "PatientAddress",
    "PatientBirthDate",
    "PatientBirthName",
    "PatientBirthTime",
    "PatientComments",
    "PatientInsurancePlanCodeSequence",
    "PatientMotherBirthName",
    "PatientTelephoneNumbers",
    "PerformingPhysicianName",
    "PhysiciansOfRecord",
    "ReferringPhysicianAddress",
    "ReferringPhysicianName",
    "ReferringPhysicianTelephoneNumbers",
    "RequestingPhysician",
    "ResponsibleOrganization",
    "ResponsiblePerson",
    "ScheduledPerformingPhysicianName",
}


class PdiStatus(str, Enum):
    WAITING = "等待"
    GENERATING = "生成中"
    COMPLETED = "完成"
    PARTIAL = "部分成功"
    FAILED = "失败"
    CANCELLED = "已取消"


class PdiStage(str, Enum):
    PREPARING = "整理文件"
    DICOMDIR = "生成 DICOMDIR"
    INDEXING = "整理阅片数据"
    VIEWER = "准备离线阅片器"
    VERIFYING = "校验导出目录"


@dataclass(slots=True)
class PdiExportResult:
    status: PdiStatus
    output_directory: str = ""
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    source_count: int = 0
    exported_count: int = 0
    duplicate_count: int = 0
    indexed_count: int = 0
    strict_profile: bool | None = None
    core_tool_failure: bool = False


ProgressCallback = Callable[[PdiStage, int, int, str], None]
LogCallback = Callable[[str, str, str], None]
ProcessCallback = Callable[[str, int, str, bool], None]


@dataclass(frozen=True, slots=True)
class _DicomItem:
    source: Path
    file_id: tuple[str, ...]
    digest: str
    sop_instance_uid: str
    transfer_syntax_uid: str
    patient_name: str
    patient_id: str
    study_instance_uid: str
    series_instance_uid: str
    accession_number: str
    study_date: str
    modality: str
    study_description: str
    series_description: str
    metadata: dict[str, object]

    @property
    def relative_path(self) -> Path:
        return Path(*self.file_id)


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int
    output: str


class _Cancelled(RuntimeError):
    pass


class PdiCoreToolError(RuntimeError):
    pass


class PdiExporter:
    """Build a standards-based PDI directory with an offline OHIF viewer."""

    def __init__(
        self,
        config: AppConfig,
        tools: ToolPaths,
        project_root: str | Path | None = None,
        viewer_source: str | Path | None = None,
        ohif_payload_dir: str | Path | None = None,
        log_callback: LogCallback | None = None,
        progress_callback: ProgressCallback | None = None,
        process_callback: ProcessCallback | None = None,
        recovery_id: str = "",
        reuse_published: bool = False,
    ):
        self.config = config
        self.tools = tools
        self.project_root = Path(project_root or resource_root())
        if viewer_source is not None and ohif_payload_dir is not None:
            raise ValueError("viewer_source 与 ohif_payload_dir 不能同时指定")
        selected_viewer = ohif_payload_dir or viewer_source
        self.ohif_payload_dir = (
            Path(selected_viewer).expanduser() if selected_viewer else None
        )
        self.log_callback = log_callback or (lambda _source, _message, _level: None)
        self.progress_callback = progress_callback or (
            lambda _stage, _current, _total, _message: None
        )
        self.process_callback = process_callback or (
            lambda _kind, _pid, _executable, _active: None
        )
        normalized_recovery_id = recovery_id.strip().lower()
        if normalized_recovery_id and not re.fullmatch(
            r"[0-9a-f]{32}", normalized_recovery_id
        ):
            raise ValueError("PDI 恢复标识格式不正确")
        self.recovery_id = normalized_recovery_id
        self.reuse_published = bool(reuse_published and normalized_recovery_id)
        self._cancel = threading.Event()
        self._process_lock = threading.Lock()
        self._current_process: subprocess.Popen[str] | None = None
        self._termination_condition = threading.Condition()
        self._terminating_processes: dict[int, subprocess.Popen[str]] = {}
        self._terminated_processes: dict[int, subprocess.Popen[str]] = {}
        self._cancel_cleanup_lock = threading.Lock()
        self._cancel_cleanup_thread: threading.Thread | None = None

    def request_cancel(self) -> None:
        self._cancel.set()
        with self._cancel_cleanup_lock:
            if self._cancel_cleanup_thread is not None:
                return
            cleanup = threading.Thread(
                target=self._cancel_running_process,
                name="pdi-cancel-cleanup",
                daemon=True,
            )
            self._cancel_cleanup_thread = cleanup
            cleanup.start()

    def _cancel_running_process(self) -> None:
        with self._process_lock:
            process = self._current_process
        if process is not None:
            self._terminate_process_safely(process)

    def _terminate_process_safely(self, process: subprocess.Popen[str]) -> None:
        """Terminate a PDI child once when cancellation races worker cleanup."""

        identity = id(process)
        with self._termination_condition:
            if self._terminated_processes.get(identity) is process:
                return
            if self._terminating_processes.get(identity) is process:
                while self._terminating_processes.get(identity) is process:
                    self._termination_condition.wait(timeout=0.1)
                return
            self._terminating_processes[identity] = process
        try:
            self._terminate_process(process)
        finally:
            with self._termination_condition:
                self._terminating_processes.pop(identity, None)
                self._terminated_processes[identity] = process
                self._termination_condition.notify_all()

    def export(self, files: Iterable[str | Path]) -> PdiExportResult:
        source_paths = [Path(path).expanduser().resolve() for path in files]
        result = PdiExportResult(
            status=PdiStatus.GENERATING,
            source_count=len(source_paths),
        )
        temporary: Path | None = None
        try:
            self._check_cancelled()
            institution = str(self.config.pdi_institution_name).strip()
            if not institution:
                raise ValueError("请先在设置中填写 PDI 机构名称")
            if not source_paths:
                raise ValueError("当前批次没有可导出的 DICOM 文件")

            output_root = self._output_root()
            output_root.mkdir(parents=True, exist_ok=True)
            self._remove_recovery_partials()
            if self.reuse_published:
                published = self._find_reusable_export(output_root)
                if published is not None:
                    self._emit(
                        f"已恢复上次完成的 PDI 目录：{published.output_directory}",
                        "success" if published.status == PdiStatus.COMPLETED else "warning",
                    )
                    return published

            viewer_requested = bool(self.config.pdi_include_ohif_viewer)
            viewer_payload = self._resolve_ohif_payload() if viewer_requested else None
            viewer_allowed, space_warnings = self._check_free_space(
                output_root, source_paths, viewer_payload, viewer_requested
            )
            result.warnings.extend(space_warnings)

            proposed = self._next_output_directory(output_root)
            temporary = output_root / f".{proposed.name}.partial-{uuid.uuid4().hex[:8]}"
            temporary.mkdir(parents=False, exist_ok=False)
            self._write_recovery_marker(temporary, result, state="building")

            self._progress(PdiStage.PREPARING, 0, len(source_paths), "正在检查 DICOM 文件")
            items, duplicate_count, duplicate_warnings = self._prepare_items(source_paths)
            result.exported_count = len(items)
            result.duplicate_count = duplicate_count
            result.warnings.extend(duplicate_warnings)
            self._copy_dicom_files(temporary, items)

            self._progress(PdiStage.DICOMDIR, 0, 1, "正在生成 DICOMDIR")
            strict_profile, dicomdir_warnings = self._create_dicomdir(temporary, items)
            result.strict_profile = strict_profile
            result.warnings.extend(dicomdir_warnings)
            self._verify_dicomdir(temporary, len(items))
            self._progress(PdiStage.DICOMDIR, 1, 1, "DICOMDIR 已生成")

            index_problem = False
            try:
                self._progress(PdiStage.INDEXING, 0, len(items), "正在整理本地阅片数据")
                result.indexed_count = self._write_ohif_index(temporary, items)
                self._progress(
                    PdiStage.INDEXING,
                    len(items),
                    len(items),
                    f"阅片数据已就绪，共 {result.indexed_count} 个图像帧",
                )
            except _Cancelled:
                raise
            except Exception as exc:
                index_problem = True
                warning = f"阅片数据整理失败，DICOMDIR 仍可使用：{exc}"
                result.warnings.append(warning)
                self._emit(warning, "warning")

            viewer_problem = viewer_requested and (not viewer_allowed or index_problem)
            viewer_included = False
            if viewer_allowed and not index_problem:
                try:
                    viewer_included, viewer_warning = self._copy_ohif(
                        temporary, viewer_payload
                    )
                    if viewer_warning:
                        viewer_problem = True
                        result.warnings.append(viewer_warning)
                except _Cancelled:
                    raise
                except Exception as exc:
                    shutil.rmtree(temporary / "VIEWER" / "OHIF", ignore_errors=True)
                    (temporary / "VIEWER" / "pdi_server.py").unlink(missing_ok=True)
                    self._remove_launchers(temporary)
                    viewer_problem = True
                    warning = f"离线阅片器准备失败，DICOMDIR 仍可使用：{exc}"
                    result.warnings.append(warning)
                    self._emit(warning, "warning")

            self._write_index(temporary, institution, items, viewer_included)
            self._write_readme(
                temporary,
                institution,
                strict_profile,
                result.warnings,
                viewer_included,
            )

            is_partial = (not strict_profile) or index_problem or viewer_problem
            result.status = PdiStatus.PARTIAL if is_partial else PdiStatus.COMPLETED
            result.message = (
                f"PDI 便携目录已生成，包含 {len(items)} 个 DICOM 文件"
                if result.status == PdiStatus.COMPLETED
                else f"PDI 目录已生成，但有 {len(result.warnings)} 条警告"
            )
            self._write_recovery_marker(temporary, result, state="published")

            self._progress(PdiStage.VERIFYING, 0, 1, "正在生成 SHA-256 校验清单")
            self._write_manifest(temporary)
            self._verify_published_content(
                temporary,
                items,
                require_index=not index_problem,
                expected_index_count=result.indexed_count,
            )
            self._progress(PdiStage.VERIFYING, 1, 1, "导出目录校验完成")
            self._check_cancelled()
            final = self._publish_directory(temporary, output_root)
            temporary = None

            result.output_directory = str(final)
            self._emit(result.message, "warning" if is_partial else "success")
            return result
        except _Cancelled:
            result.status = PdiStatus.CANCELLED
            result.message = "PDI 导出已取消"
            self._emit(result.message, "warning")
            return result
        except PdiCoreToolError as exc:
            result.status = PdiStatus.FAILED
            result.core_tool_failure = True
            result.message = str(exc).strip() or exc.__class__.__name__
            self._emit(f"PDI 导出失败：{result.message}", "error")
            return result
        except Exception as exc:
            result.status = PdiStatus.FAILED
            result.message = str(exc).strip() or exc.__class__.__name__
            self._emit(f"PDI 导出失败：{result.message}", "error")
            return result
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)

    def _prepare_items(
        self, source_paths: list[Path]
    ) -> tuple[list[_DicomItem], int, list[str]]:
        items: list[_DicomItem] = []
        seen_uids: dict[str, _DicomItem] = {}
        warnings: list[str] = []
        patient_ids: dict[str, str] = {}
        study_ids: dict[tuple[str, str], str] = {}
        instance_index = 0

        for current, source in enumerate(source_paths, 1):
            self._check_cancelled()
            if not source.is_file():
                raise FileNotFoundError(f"DICOM 文件不存在：{source}")
            try:
                dataset = dcmread(source, stop_before_pixels=True)
            except Exception as exc:
                raise ValueError(f"无法读取 DICOM 文件 {source}：{exc}") from exc

            repaired_character_set = _repair_dataset_character_set(dataset)
            if repaired_character_set:
                warning = (
                    "检测到字符集缺失或声明异常，已按 "
                    f"{repaired_character_set} 整理 PDI 阅片文字（原始 DICOM 未修改）"
                )
                if warning not in warnings:
                    warnings.append(warning)

            sop_uid = str(
                dataset.get("SOPInstanceUID", "")
                or getattr(dataset.file_meta, "MediaStorageSOPInstanceUID", "")
            ).strip()
            if not sop_uid:
                raise ValueError(f"DICOM 文件缺少 SOP Instance UID：{source}")
            digest = self._hash_file(source)
            duplicate = seen_uids.get(sop_uid)
            if duplicate is not None:
                if duplicate.digest != digest:
                    raise ValueError(
                        "发现相同 SOP Instance UID 但内容不同的文件："
                        f"{duplicate.source} 与 {source}"
                    )
                warnings.append(f"已跳过重复 DICOM 实例：{source.name}")
                self._progress(
                    PdiStage.PREPARING,
                    current,
                    len(source_paths),
                    f"跳过重复文件 {source.name}",
                )
                continue

            patient_key = str(dataset.get("PatientID", "")).strip() or str(
                dataset.get("PatientName", "")
            ).strip() or f"UNKNOWN-{current}"
            study_uid = str(dataset.get("StudyInstanceUID", "")).strip()
            study_key = study_uid or f"MISSING-STUDY-{current}"
            patient_file_id = patient_ids.setdefault(
                patient_key, f"P{len(patient_ids) + 1:06d}"
            )
            study_pair = (patient_key, study_key)
            study_file_id = study_ids.setdefault(
                study_pair, f"S{len(study_ids) + 1:06d}"
            )
            instance_index += 1
            file_id = (
                "DICOM",
                patient_file_id,
                study_file_id,
                f"I{instance_index:06d}",
            )
            transfer_syntax = str(
                getattr(dataset.file_meta, "TransferSyntaxUID", "")
            ).strip()
            metadata = _naturalize_dataset(dataset)
            metadata.setdefault("SOPInstanceUID", sop_uid)
            metadata.setdefault("StudyInstanceUID", study_uid)
            metadata.setdefault("TransferSyntaxUID", transfer_syntax)
            item = _DicomItem(
                source=source,
                file_id=file_id,
                digest=digest,
                sop_instance_uid=sop_uid,
                transfer_syntax_uid=transfer_syntax,
                patient_name=str(dataset.get("PatientName", "")),
                patient_id=str(dataset.get("PatientID", "")),
                study_instance_uid=study_uid,
                series_instance_uid=str(dataset.get("SeriesInstanceUID", "")),
                accession_number=str(dataset.get("AccessionNumber", "")),
                study_date=str(dataset.get("StudyDate", "")),
                modality=str(dataset.get("Modality", "")),
                study_description=str(dataset.get("StudyDescription", "")),
                series_description=str(dataset.get("SeriesDescription", "")),
                metadata=metadata,
            )
            items.append(item)
            seen_uids[sop_uid] = item
            self._progress(
                PdiStage.PREPARING,
                current,
                len(source_paths),
                f"已检查 {source.name}",
            )

        if not items:
            raise ValueError("去重后没有可导出的 DICOM 文件")
        return items, len(source_paths) - len(items), warnings

    def _copy_dicom_files(self, root: Path, items: list[_DicomItem]) -> None:
        for current, item in enumerate(items, 1):
            self._check_cancelled()
            destination = root / item.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._copy_file(item.source, destination)
            if self._hash_file(destination) != item.digest:
                raise OSError(f"复制后文件校验失败：{item.source}")
            self._progress(
                PdiStage.PREPARING,
                current,
                len(items),
                f"已整理 {current}/{len(items)} 个 DICOM 文件",
            )

    def _create_dicomdir(
        self, root: Path, items: list[_DicomItem]
    ) -> tuple[bool, list[str]]:
        dcmmkdir = self._tool_path("dcmmkdir")
        if not dcmmkdir.is_file():
            raise PdiCoreToolError(f"未找到 DCMTK dcmmkdir：{dcmmkdir}")

        profile = _strict_profile(items)
        strict_command = [
            str(dcmmkdir),
            "-v",
            profile,
            "-a",
            "-I",
            "+F",
            "DCMGET",
            "+D",
            "DICOMDIR",
            "+id",
            str(root),
            "+r",
            "DICOM",
        ]
        strict = self._run_command(strict_command, root)
        if strict.returncode == 0 and (root / "DICOMDIR").is_file():
            self._emit(f"DICOMDIR 严格 Profile 校验通过（{profile}）", "success")
            return True, []

        (root / "DICOMDIR").unlink(missing_ok=True)
        warning = (
            "严格 DICOMDIR Profile 未通过，已使用兼容模式生成；"
            "该目录不应声称为严格 Profile 合规介质"
        )
        self._emit(warning, "warning")
        compatibility_command = [
            str(dcmmkdir),
            "-v",
            "-Pgp",
            "+I",
            "-Nxc",
            "-Nec",
            "-Nrc",
            "+W",
            "+F",
            "DCMGET",
            "+D",
            "DICOMDIR",
            "+id",
            str(root),
            "+r",
            "DICOM",
        ]
        compatible = self._run_command(compatibility_command, root)
        if compatible.returncode or not (root / "DICOMDIR").is_file():
            details = compatible.output.strip() or strict.output.strip() or "未生成 DICOMDIR"
            raise RuntimeError(f"dcmmkdir 生成 DICOMDIR 失败：{details}")
        return False, [warning]

    def _verify_dicomdir(self, root: Path, expected_count: int) -> None:
        try:
            dataset = dcmread(root / "DICOMDIR")
        except Exception as exc:
            raise RuntimeError(f"DICOMDIR 校验失败：{exc}") from exc
        references: set[tuple[str, ...]] = set()
        for record in dataset.get("DirectoryRecordSequence", []):
            value = record.get("ReferencedFileID")
            if not value:
                continue
            parts = (
                tuple(part for part in value.split("\\") if part)
                if isinstance(value, str)
                else tuple(str(part) for part in value)
            )
            if not parts:
                continue
            if any(
                not part
                or len(part) > 8
                or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for char in part)
                for part in parts
            ):
                raise RuntimeError(f"DICOMDIR 包含非标准文件 ID：{'/'.join(parts)}")
            target = root.joinpath(*parts)
            try:
                target.resolve().relative_to(root.resolve())
            except ValueError as exc:
                raise RuntimeError("DICOMDIR 引用超出导出目录") from exc
            if not target.is_file():
                raise RuntimeError(f"DICOMDIR 引用的文件不存在：{'/'.join(parts)}")
            references.add(parts)
        if len(references) != expected_count:
            raise RuntimeError(
                f"DICOMDIR 引用数量不一致：期望 {expected_count}，实际 {len(references)}"
            )

    def _write_ohif_index(self, root: Path, items: list[_DicomItem]) -> int:
        frame_counts: dict[Path, int] = {}
        estimated_bytes = 32
        indexed_count = 0
        for item in items:
            self._check_cancelled()
            try:
                frame_count = max(1, int(item.metadata.get("NumberOfFrames", 1)))
            except (TypeError, ValueError):
                frame_count = 1
            if indexed_count + frame_count > MAX_OHIF_INDEX_FRAMES:
                raise RuntimeError(
                    "本批次阅片索引超过 100000 帧，请拆分批次后重新导出 PDI"
                )
            metadata_bytes = len(
                json.dumps(
                    item.metadata,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            base_url_bytes = len(
                f"dicomweb:/{item.relative_path.as_posix()}".encode("utf-8")
            )
            # Each frame repeats its metadata in the DICOM JSON payload.  Add a
            # conservative allowance for URL, keys, separators and containers.
            estimated_bytes += frame_count * (metadata_bytes + base_url_bytes + 96)
            estimated_bytes += 512
            if estimated_bytes > MAX_OHIF_INDEX_ESTIMATED_BYTES:
                raise RuntimeError(
                    "本批次阅片索引预计超过 64 MiB，请拆分批次后重新导出 PDI"
                )
            frame_counts[item.relative_path] = frame_count
            indexed_count += frame_count

        studies: dict[str, dict[str, Any]] = {}
        series_maps: dict[str, dict[str, dict[str, Any]]] = {}

        for item in items:
            self._check_cancelled()
            study_key = item.study_instance_uid or f"MISSING-{item.file_id[2]}"
            if study_key not in studies:
                metadata = item.metadata
                study = {
                    "StudyInstanceUID": study_key,
                    "StudyDate": _metadata_value(metadata, "StudyDate", ""),
                    "StudyTime": _metadata_value(metadata, "StudyTime", ""),
                    "PatientName": _metadata_value(metadata, "PatientName", []),
                    "PatientID": _metadata_value(metadata, "PatientID", "")
                    or "UNKNOWN",
                    "AccessionNumber": _metadata_value(metadata, "AccessionNumber", ""),
                    "PatientSex": _metadata_value(metadata, "PatientSex", ""),
                    "PatientAge": _metadata_value(metadata, "PatientAge", ""),
                    "PatientWeight": _metadata_value(metadata, "PatientWeight", ""),
                    "StudyDescription": _metadata_value(
                        metadata, "StudyDescription", ""
                    ),
                    "InstitutionName": _metadata_value(
                        metadata, "InstitutionName", ""
                    ),
                    "series": [],
                }
                studies[study_key] = study
                series_maps[study_key] = {}

            series_key = item.series_instance_uid or f"MISSING-{item.file_id[3]}"
            series_map = series_maps[study_key]
            if series_key not in series_map:
                metadata = item.metadata
                series = {
                    "SeriesInstanceUID": series_key,
                    "SeriesNumber": _metadata_value(metadata, "SeriesNumber", 0),
                    "SeriesDate": _metadata_value(metadata, "SeriesDate", ""),
                    "SeriesTime": _metadata_value(metadata, "SeriesTime", ""),
                    "Modality": _metadata_value(metadata, "Modality", ""),
                    "SliceThickness": _metadata_value(
                        metadata, "SliceThickness", ""
                    ),
                    "SeriesDescription": _metadata_value(
                        metadata, "SeriesDescription", ""
                    ),
                    "ProtocolName": _metadata_value(metadata, "ProtocolName", ""),
                    "instances": [],
                }
                series_map[series_key] = series
                studies[study_key]["series"].append(series)

            base_url = f"dicomweb:/{item.relative_path.as_posix()}"
            frame_count = frame_counts[item.relative_path]
            for frame_number in range(1, frame_count + 1):
                series_map[series_key]["instances"].append(
                    {
                        "metadata": item.metadata,
                        "url": (
                            f"{base_url}?frame={frame_number}"
                            if frame_count > 1
                            else base_url
                        ),
                    }
                )

        for study_key, study in studies.items():
            modalities: list[str] = []
            instance_count = 0
            series_values = study["series"]
            for series in series_values:
                series["instances"].sort(
                    key=lambda entry: _sort_number(
                        entry["metadata"].get("InstanceNumber", 0)
                    )
                )
                instance_count += len(series["instances"])
                modality = str(series.get("Modality", "")).strip()
                if modality and modality not in modalities:
                    modalities.append(modality)
            series_values.sort(key=lambda value: _sort_number(value.get("SeriesNumber", 0)))
            study["NumInstances"] = instance_count
            study["Modalities"] = "\\".join(modalities)

        payload = {"studies": list(studies.values())}
        index_path = root / STUDY_INDEX
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return indexed_count

    def _copy_ohif(
        self, root: Path, payload: Path | None
    ) -> tuple[bool, str | None]:
        self._progress(PdiStage.VIEWER, 0, 1, "正在准备中文离线阅片器")
        if payload is None:
            return False, "未找到经过校验的 OHIF 资源，已保留 DICOMDIR 和原始 DICOM"
        _verify_ohif_payload(payload)
        missing = [
            name
            for name in ("index.html", "app-config.js")
            if not (payload / name).is_file()
        ]
        license_files = [path for path in payload.glob("LICENSE*") if path.is_file()]
        third_party_files = [
            path for path in payload.glob("THIRD_PARTY*") if path.is_file()
        ]
        if missing:
            return False, f"OHIF 资源不完整，缺少：{'、'.join(missing)}"
        if not license_files or not third_party_files:
            return False, "OHIF 资源缺少开源许可证或第三方声明"

        destination = root / "VIEWER" / "OHIF"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(payload, destination, copy_function=self._copy_file)
        _verify_ohif_payload(destination)

        server_script = Path(__file__).with_name("pdi_server.py")
        if not server_script.is_file():
            server_script = self.project_root / "dcmget" / "pdi_server.py"
        if not server_script.is_file():
            return False, "未找到 PDI 本地服务启动脚本"
        self._copy_file(server_script, root / "VIEWER" / "pdi_server.py")

        server_executable = self._resolve_server_executable()
        if server_executable is not None:
            self._copy_file(
                server_executable,
                root
                / (
                    "OPEN_VIEWER.exe"
                    if server_executable.suffix.lower() == ".exe"
                    else "OPEN_VIEWER"
                ),
            )
        self._write_launchers(root)
        self._progress(PdiStage.VIEWER, 1, 1, "中文离线阅片器已就绪")
        return True, None

    @staticmethod
    def _write_launchers(root: Path) -> None:
        (root / "OPEN_VIEWER.bat").write_text(
            "@echo off\r\n"
            "setlocal\r\n"
            "cd /d \"%~dp0\"\r\n"
            "if exist \"OPEN_VIEWER.exe\" (\r\n"
            "  start \"\" \"OPEN_VIEWER.exe\" --root \"%CD%\" --quiet\r\n"
            "  exit /b 0\r\n"
            ")\r\n"
            "where py >nul 2>nul && py -3 \"VIEWER\\pdi_server.py\" --root \"%CD%\" && exit /b 0\r\n"
            "where python >nul 2>nul && python \"VIEWER\\pdi_server.py\" --root \"%CD%\" && exit /b 0\r\n"
            "echo [DcmGet] Local viewer server or Python 3 was not found.\r\n"
            "pause\r\n",
            encoding="ascii",
            newline="",
        )
        unix_script = (
            "#!/bin/sh\n"
            "set -eu\n"
            "ROOT=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
            "exec python3 \"$ROOT/VIEWER/pdi_server.py\" --root \"$ROOT\"\n"
        )
        command = root / "OPEN_VIEWER.command"
        shell = root / "OPEN_VIEWER.sh"
        command.write_text(unix_script, encoding="utf-8", newline="\n")
        shell.write_text(unix_script, encoding="utf-8", newline="\n")
        command.chmod(0o755)
        shell.chmod(0o755)

    @staticmethod
    def _remove_launchers(root: Path) -> None:
        for name in (
            "OPEN_VIEWER.exe",
            "OPEN_VIEWER",
            "OPEN_VIEWER.bat",
            "OPEN_VIEWER.command",
            "OPEN_VIEWER.sh",
        ):
            (root / name).unlink(missing_ok=True)

    def _write_index(
        self,
        root: Path,
        institution: str,
        items: list[_DicomItem],
        viewer_included: bool,
    ) -> None:
        studies: dict[tuple[str, str], list[_DicomItem]] = {}
        for item in items:
            key = (item.patient_id or item.patient_name, item.study_instance_uid)
            studies.setdefault(key, []).append(item)
        cards: list[str] = []
        for study_items in studies.values():
            first = study_items[0]
            modalities = " / ".join(
                dict.fromkeys(item.modality for item in study_items if item.modality)
            ) or "-"
            cards.append(
                '<article class="study">'
                f"<h2>{html.escape(first.patient_name or first.patient_id or '未命名患者')}</h2>"
                f"<p>患者 ID：{html.escape(first.patient_id or '-')}</p>"
                f"<p>检查号：{html.escape(first.accession_number or '-')}</p>"
                f"<p>检查日期：{html.escape(first.study_date or '-')}　类型：{html.escape(modalities)}</p>"
                f"<p>{html.escape(first.study_description or '-')}</p>"
                f"<span>{len(study_items)} 个 DICOM 实例</span>"
                "</article>"
            )
        launch = (
            '<div class="launch"><strong>这是检查清单，不是阅片器</strong>'
            "<p>查看影像无需选择 JSON、DICOMDIR 或逐个文件，请返回当前目录运行："
            "Windows 双击 OPEN_VIEWER.exe（推荐）或 OPEN_VIEWER.bat；"
            "macOS 双击 OPEN_VIEWER.command；Linux 运行 OPEN_VIEWER.sh。</p></div>"
            if viewer_included
            else (
                '<div class="warning"><strong>此页仅显示检查清单，不能直接看图。</strong> '
                "本次导出未能加入离线阅片器，请查看 README.TXT 的警告说明，"
                "或使用外部 DICOM 查看器打开 DICOMDIR。</div>"
            )
        )
        contents = (
            "目录内保存标准 DICOMDIR、原始 DICOM 和中文离线阅片器。"
            if viewer_included
            else "目录内保存标准 DICOMDIR 和原始 DICOM；本次未加入离线阅片器。"
        )
        document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DcmGet PDI</title><style>
:root{{--bg:#07131f;--panel:#101e2d;--line:#263b4f;--text:#eaf4fb;--muted:#9bb0c1;--cyan:#5de2ef;--warn:#ffbd66}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(145deg,#07131f,#0a1825);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}}
main{{max-width:1120px;margin:auto;padding:32px}}header,.launch,.warning,.study{{background:var(--panel);border:1px solid var(--line);border-radius:10px}}
header{{padding:26px;margin-bottom:18px}}h1{{margin:0 0 8px;color:var(--cyan);font-size:28px}}header p,.study p{{color:var(--muted)}}
.notice{{border-left:4px solid var(--warn);padding:14px 18px;margin:18px 0;background:#2a2117;color:#ffd9a3}}
.launch,.warning{{padding:18px;margin:18px 0}}.launch strong{{color:var(--cyan)}}.launch p{{display:inline-block;margin:10px 24px 0 0;color:var(--muted)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px}}.study{{padding:18px}}.study h2{{margin:0 0 12px;font-size:18px}}
.study p{{margin:7px 0}}.study span{{display:inline-block;margin-top:10px;color:var(--cyan)}}a{{color:var(--cyan)}}
</style></head><body><main><header><h1>DcmGet PDI 便携影像</h1>
<p>机构：{html.escape(institution)}</p><p>{contents}</p>
<a href="README.TXT">查看使用说明</a></header>
<section class="notice"><strong>仅供查阅，不用于诊断。</strong> 请以原始 DICOM 和医疗机构正式报告为准。</section>
{launch}<section class="grid">{''.join(cards)}</section>
</main></body></html>"""
        (root / "INDEX.HTM").write_text(document, encoding="utf-8", newline="\n")

    @staticmethod
    def _write_readme(
        root: Path,
        institution: str,
        strict_profile: bool,
        warnings: list[str],
        viewer_included: bool,
    ) -> None:
        profile = (
            "DICOMDIR 已通过严格 DCMTK Profile 生成。"
            if strict_profile
            else "DICOMDIR 使用兼容模式生成，不声称为严格 Profile 合规介质。"
        )
        viewer = (
            "Windows 双击 OPEN_VIEWER.exe（推荐）或 OPEN_VIEWER.bat；"
            "macOS 双击 OPEN_VIEWER.command；"
            "Linux 运行 OPEN_VIEWER.sh。阅片服务只监听本机 127.0.0.1。"
            if viewer_included
            else (
                "本次导出未能加入离线阅片器，请查看下方警告；"
                "也可使用外部 DICOM 查看器打开 DICOMDIR。"
            )
        )
        warning_lines = "\n".join(f"- {warning}" for warning in warnings) or "- 无"
        content = f"""DcmGet PDI 便携影像目录

机构：{institution}
生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}

使用方法：
1. 将整个目录复制到 U 盘，不要只复制部分文件。
2. {viewer}
3. INDEX.HTM 只用于查看目录说明和检查清单，不能直接显示 DICOM 图像；
   无需选择 JSON、DICOMDIR 或逐个影像文件。

{profile}
离线阅片器直接读取目录中的原始 DICOM，不生成 JPEG 预览，也不会连接 PACS 或公网。
本目录仅供查阅，不用于诊断；DICOM 原始文件为准。
MANIFEST.SHA256 校验目录中除清单自身外的所有文件。

警告：
{warning_lines}
"""
        (root / "README.TXT").write_text(content, encoding="utf-8", newline="\n")

    def _write_manifest(self, root: Path) -> None:
        lines: list[str] = []
        manifest_path = root / "MANIFEST.SHA256"
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            self._check_cancelled()
            if not path.is_file() or path == manifest_path:
                continue
            relative = path.relative_to(root).as_posix()
            lines.append(f"{self._hash_file(path)}  {relative}")
        manifest_path.write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
        )

    def _verify_published_content(
        self,
        root: Path,
        items: list[_DicomItem],
        *,
        require_index: bool = True,
        expected_index_count: int | None = None,
    ) -> None:
        required = ["DICOMDIR", "INDEX.HTM", "README.TXT", "MANIFEST.SHA256"]
        if require_index:
            required.append(STUDY_INDEX)
        missing = [name for name in required if not (root / name).is_file()]
        if missing:
            raise RuntimeError(f"PDI 目录缺少文件：{'、'.join(missing)}")
        for item in items:
            destination = root / item.relative_path
            if not destination.is_file() or self._hash_file(destination) != item.digest:
                raise RuntimeError(f"PDI DICOM 文件校验失败：{destination}")
        if require_index:
            self._verify_ohif_index(
                root,
                len(items) if expected_index_count is None else expected_index_count,
            )

        manifest_path = root / "MANIFEST.SHA256"
        expected: dict[str, str] = {}
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            digest, separator, relative = line.partition("  ")
            if (
                not separator
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or not relative
            ):
                raise RuntimeError("PDI SHA-256 清单格式无效")
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or path.as_posix() in expected:
                raise RuntimeError("PDI SHA-256 清单包含无效路径")
            expected[path.as_posix()] = digest
        actual = {
            path.relative_to(root).as_posix(): self._hash_file(path)
            for path in root.rglob("*")
            if path.is_file() and path != manifest_path
        }
        if expected != actual:
            raise RuntimeError("PDI SHA-256 清单与导出文件不一致")

    @staticmethod
    def _verify_ohif_index(root: Path, expected_count: int) -> None:
        try:
            payload = json.loads((root / STUDY_INDEX).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"OHIF 索引校验失败：{exc}") from exc
        studies = payload.get("studies") if isinstance(payload, dict) else None
        if not isinstance(studies, list):
            raise RuntimeError("OHIF 索引缺少 studies 列表")
        count = 0
        for study in studies:
            if not isinstance(study, dict) or not isinstance(study.get("series"), list):
                raise RuntimeError("OHIF 索引中的检查结构无效")
            for series in study["series"]:
                if not isinstance(series, dict) or not isinstance(
                    series.get("instances"), list
                ):
                    raise RuntimeError("OHIF 索引中的序列结构无效")
                for instance in series["instances"]:
                    if not isinstance(instance, dict):
                        raise RuntimeError("OHIF 索引中的实例结构无效")
                    url = str(instance.get("url", ""))
                    if not url.startswith("dicomweb:/DICOM/"):
                        raise RuntimeError("OHIF 索引包含非本地 DICOM 地址")
                    relative = url.removeprefix("dicomweb:/").partition("?")[0]
                    target = root.joinpath(*relative.split("/"))
                    try:
                        target.resolve().relative_to((root / "DICOM").resolve())
                    except ValueError as exc:
                        raise RuntimeError("OHIF 索引引用超出 DICOM 目录") from exc
                    if not target.is_file():
                        raise RuntimeError(f"OHIF 索引引用的文件不存在：{relative}")
                    count += 1
        if count != expected_count:
            raise RuntimeError(
                f"OHIF 索引实例数不一致：期望 {expected_count}，实际 {count}"
            )

    def _run_command(self, command: list[str], cwd: Path) -> _CommandResult:
        self._check_cancelled()
        kwargs: dict[str, object] = {
            "cwd": cwd,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": locale.getpreferredencoding(False) or "utf-8",
            "errors": "replace",
            "env": self._dcmtk_environment(),
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]
        except OSError as exc:
            raise PdiCoreToolError(f"无法启动 {Path(command[0]).name}：{exc}") from exc
        with self._process_lock:
            self._current_process = process
        self._notify_process(process.pid, command[0], True)
        try:
            while True:
                if self._cancel.is_set():
                    self._terminate_process_safely(process)
                    raise _Cancelled()
                try:
                    output = process.communicate(timeout=0.1)[0] or ""
                    break
                except subprocess.TimeoutExpired:
                    continue
            self._check_cancelled()
            return _CommandResult(process.returncode or 0, output)
        finally:
            with self._process_lock:
                if self._current_process is process:
                    self._current_process = None
            self._notify_process(process.pid, command[0], False)

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=3,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()

    def _dcmtk_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = str(self.tools.bin_dir) + os.pathsep + env.get("PATH", "")
        share_root = self.tools.bin_dir.parent / "share"
        if share_root.exists():
            dictionaries = list(share_root.glob("dcmtk-*/dicom.dic")) + list(
                share_root.glob("dicom.dic")
            )
            if dictionaries:
                env.setdefault("DCMDICTPATH", str(dictionaries[0]))
        return env

    def _tool_path(self, name: str) -> Path:
        configured = getattr(self.tools, name, None)
        if configured is not None:
            return Path(configured)
        suffix = ".exe" if os.name == "nt" else ""
        return self.tools.bin_dir / f"{name}{suffix}"

    def _output_root(self) -> Path:
        return _pdi_output_root(self.config)

    def _write_recovery_marker(
        self,
        root: Path,
        result: PdiExportResult,
        *,
        state: str,
    ) -> None:
        if not self.recovery_id:
            return
        payload = {
            "attempt_id": self.recovery_id,
            "duplicate_count": result.duplicate_count,
            "exported_count": result.exported_count,
            "indexed_count": result.indexed_count,
            "message": result.message,
            "source_count": result.source_count,
            "state": state,
            "status": result.status.value,
            "strict_profile": result.strict_profile,
            "version": 2,
            "warnings": list(result.warnings),
        }
        marker_path = root / RECOVERY_MARKER_PATH
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _remove_recovery_partials(self) -> None:
        if not self.recovery_id:
            return
        for candidate in cleanup_interrupted_pdi(self.config, self.recovery_id):
            self._emit(f"已清理上次中断的 PDI 暂存目录：{candidate.name}", "warning")

    def _find_reusable_export(self, output_root: Path) -> PdiExportResult | None:
        for candidate in sorted(output_root.glob("DCMGET_PDI_*"), reverse=True):
            if not candidate.is_dir():
                continue
            marker = self._read_recovery_marker(candidate)
            if (
                marker is None
                or marker.get("attempt_id") != self.recovery_id
                or marker.get("state") != "published"
            ):
                continue
            try:
                status = PdiStatus(str(marker["status"]))
                if status not in {PdiStatus.COMPLETED, PdiStatus.PARTIAL}:
                    continue
                indexed_count = int(marker.get("indexed_count", 0))
                self._verify_published_content(
                    candidate,
                    [],
                    require_index=indexed_count > 0,
                    expected_index_count=indexed_count,
                )
                warnings = marker.get("warnings", [])
                if not isinstance(warnings, list):
                    raise ValueError("invalid warnings")
                strict_profile = marker.get("strict_profile")
                if strict_profile not in {True, False, None}:
                    raise ValueError("invalid profile status")
                return PdiExportResult(
                    status=status,
                    output_directory=str(candidate),
                    message=str(marker.get("message", "") or status.value),
                    warnings=[str(warning) for warning in warnings],
                    source_count=int(marker.get("source_count", 0)),
                    exported_count=int(marker.get("exported_count", 0)),
                    duplicate_count=int(marker.get("duplicate_count", 0)),
                    indexed_count=indexed_count,
                    strict_profile=strict_profile,
                )
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                self._emit(
                    f"忽略无法校验的 PDI 恢复目录 {candidate.name}：{exc}",
                    "warning",
                )
        return None

    @staticmethod
    def _read_recovery_marker(root: Path) -> dict[str, object] | None:
        return _read_recovery_marker(root)

    def _resolve_ohif_payload(self) -> Path | None:
        if not self.config.pdi_include_ohif_viewer:
            return None
        if self.ohif_payload_dir is not None:
            return self.ohif_payload_dir if self.ohif_payload_dir.is_dir() else None
        candidates = (
            resource_root() / ".runtime" / "ohif" / f"ohif-{OHIF_VERSION}",
            self.project_root / ".runtime" / "ohif" / f"ohif-{OHIF_VERSION}",
            self.project_root / "resources" / "ohif" / f"ohif-{OHIF_VERSION}",
        )
        return next((path for path in candidates if path.is_dir()), None)

    def _resolve_server_executable(self) -> Path | None:
        names = (
            "DcmGetPdiServer.exe",
            "DcmGetPdiServer",
        )
        roots = (
            resource_root(),
            self.project_root,
            self.project_root / "resources",
        )
        return next(
            (
                root / name
                for root in roots
                for name in names
                if (root / name).is_file()
            ),
            None,
        )

    @staticmethod
    def _check_free_space(
        output_root: Path,
        source_paths: list[Path],
        payload: Path | None,
        viewer_requested: bool,
    ) -> tuple[bool, list[str]]:
        source_size = sum(path.stat().st_size for path in source_paths if path.is_file())
        available = shutil.disk_usage(output_root).free
        core_reserve = max(10 * 1024 * 1024, len(source_paths) * 8192)
        core_required = source_size + core_reserve
        if available < core_required:
            raise OSError(
                f"PDI 导出空间不足：核心目录至少需要 {core_required} 字节，"
                f"当前可用 {available} 字节"
            )
        warnings: list[str] = []
        if not viewer_requested:
            return False, warnings
        if payload is None:
            warnings.append("未找到 OHIF 运行资源，PDI 将不包含网页阅片器")
            return False, warnings
        try:
            payload_size = sum(
                path.stat().st_size for path in payload.rglob("*") if path.is_file()
            )
        except OSError as exc:
            warnings.append(f"无法检查 OHIF 资源，已跳过可选查看器：{exc}")
            return False, warnings
        if available - core_required < payload_size:
            warnings.append("可用空间不足，已跳过可选 OHIF 查看器")
            return False, warnings
        return True, warnings

    @staticmethod
    def _next_output_directory(output_root: Path) -> Path:
        base = f"DCMGET_PDI_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        candidate = output_root / base
        suffix = 1
        while candidate.exists():
            candidate = output_root / f"{base}-{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _publish_directory(temporary: Path, output_root: Path) -> Path:
        """Atomically choose and publish a unique PDI directory across processes."""

        try:
            normalized = os.path.normcase(str(output_root.resolve(strict=False)))
        except OSError:
            normalized = os.path.normcase(os.path.abspath(os.fspath(output_root)))
        digest = hashlib.sha256(os.fsencode(normalized)).hexdigest()
        lock_directory = ensure_application_state_dir() / "pdi-publish-locks"
        lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            lock_directory.chmod(0o700)
        except OSError:
            pass
        with FileLock(str(lock_directory / f"{digest}.lock"), timeout=300):
            final = PdiExporter._next_output_directory(output_root)
            temporary.rename(final)
            return final

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled()

    def _hash_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                self._check_cancelled()
                digest.update(chunk)
        return digest.hexdigest()

    def _copy_file(self, source: str | Path, destination: str | Path) -> str:
        self._check_cancelled()
        source_path = Path(source)
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with source_path.open("rb") as input_file, destination_path.open("wb") as output_file:
            while chunk := input_file.read(1024 * 1024):
                self._check_cancelled()
                output_file.write(chunk)
        shutil.copystat(source_path, destination_path)
        self._check_cancelled()
        return str(destination_path)

    def _progress(
        self, stage: PdiStage, current: int, total: int, message: str
    ) -> None:
        self.progress_callback(stage, current, total, message)

    def _emit(self, message: str, level: str) -> None:
        self.log_callback("PDI", message, level)

    def _notify_process(self, pid: int, executable: str, active: bool) -> None:
        try:
            self.process_callback("pdi", pid, executable, active)
        except Exception as exc:
            self._emit(f"无法更新 PDI 子进程恢复信息：{exc}", "error")


def _naturalize_dataset(dataset: Dataset) -> dict[str, object]:
    metadata: dict[str, object] = {}
    binary_vrs = {"OB", "OD", "OF", "OL", "OV", "OW", "UN"}
    for tag in dataset.keys():
        # Avoid materializing private raw text: malformed private values are
        # excluded from OHIF metadata and must not trigger charset decoding.
        if tag.is_private:
            continue
        keyword = keyword_for_tag(tag)
        if (
            not keyword
            or keyword == "PixelData"
            or keyword in OHIF_INDEX_EXCLUDED_KEYWORDS
        ):
            continue
        element = dataset[tag]
        if element.VR in binary_vrs:
            continue
        try:
            value = _naturalize_value(element.value, element.VR)
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            continue
        metadata[keyword] = value
    return metadata


def _naturalize_value(value: object, vr: str = "") -> object:
    if vr == "SQ":
        return [_naturalize_dataset(item) for item in value]  # type: ignore[union-attr]
    if vr == "PN":
        if _is_sequence_value(value):
            return [str(item) for item in value]  # type: ignore[union-attr]
        return str(value)
    if isinstance(value, bytes):
        raise TypeError("binary value")
    if _is_sequence_value(value):
        return [_naturalize_value(item) for item in value]  # type: ignore[union-attr]
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    class_name = value.__class__.__name__
    if class_name.startswith("IS"):
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return str(value)
    if class_name.startswith("DS"):
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _repair_dataset_character_set(dataset: Dataset) -> str | None:
    """Repair common vendor charset omissions or errors for the OHIF index.

    DICOM defaults to ISO_IR 6 when (0008,0005) is absent, but some PACS export
    GBK/GB18030 or UTF-8 bytes without a matching declaration.  Pydicom keeps
    text elements raw until first access, so this conservative check runs
    immediately after ``dcmread`` and only changes the in-memory dataset used to
    build the viewer index.  The copied source object remains byte-for-byte
    unchanged.
    """

    samples = _raw_character_set_samples(dataset)
    if not samples:
        return None

    declared_value = dataset.get("SpecificCharacterSet", "")
    declared = (
        str(declared_value[0] if _is_sequence_value(declared_value) else declared_value)
        .strip()
        .upper()
    )
    try:
        declared_encoding = convert_encodings(declared_value or "")[0]
    except (LookupError, TypeError, ValueError):
        declared_encoding = "iso8859"

    declared_text = _decode_character_set_samples(samples, declared_encoding)
    utf8_text = _decode_character_set_samples(samples, "utf-8")
    gb18030_text = _decode_character_set_samples(samples, "gb18030")

    replacement: str | None = None
    if declared in {"ISO_IR 192", "UTF-8", "UTF8"}:
        if declared_text is None and _cjk_count(gb18030_text) >= 2:
            replacement = "GB18030"
    elif declared in {"GB18030", "GBK", "ISO 2022 GBK"}:
        if declared_text is None and utf8_text is not None:
            replacement = "ISO_IR 192"
        elif (
            utf8_text is not None
            and _cjk_count(utf8_text) >= 2
            and _text_damage_score(declared_text)
            >= _text_damage_score(utf8_text) + 8
        ):
            replacement = "ISO_IR 192"
    elif declared in {"", "ISO_IR 6", "ISO 2022 IR 6"}:
        if utf8_text is not None and _non_ascii_count(utf8_text) > 0:
            replacement = "ISO_IR 192"
        elif _cjk_count(gb18030_text) >= 2 and _has_consecutive_high_bytes(samples):
            replacement = "GB18030"
    elif declared in {"ISO_IR 100", "ISO 2022 IR 100"} and _cjk_count(
        declared_text
    ) == 0:
        # A few PACS declare a Latin character set while writing UTF-8 or
        # GB18030 bytes.  ISO-8859 decoders accept every byte, so decode
        # success/damage scores cannot identify this case (for example,
        # GB18030 ``胸部`` becomes the plausible-looking ``ÐØ²¿``).  Only
        # override the declaration when at least two independent metadata
        # values contain dense CJK text backed by a run of multibyte data.
        # This deliberately leaves normal Latin text, including adjacent
        # accents, under its declared character set.
        if _strong_cjk_encoding_evidence(samples, "utf-8"):
            replacement = "ISO_IR 192"
        elif _strong_cjk_encoding_evidence(samples, "gb18030"):
            replacement = "GB18030"

    if replacement is None or replacement == declared:
        return None
    dataset.SpecificCharacterSet = replacement
    is_implicit_vr, is_little_endian = dataset.original_encoding
    dataset.set_original_encoding(
        is_implicit_vr,
        is_little_endian,
        convert_encodings(replacement),
    )
    return replacement


def _raw_character_set_samples(dataset: Dataset) -> list[bytes]:
    samples: list[bytes] = []
    total = 0
    for raw in dataset._dict.values():
        if not isinstance(raw, RawDataElement):
            continue
        if raw.tag.is_private:
            continue
        keyword = keyword_for_tag(raw.tag)
        if not keyword or keyword in OHIF_INDEX_EXCLUDED_KEYWORDS:
            continue
        try:
            value_representation = str(raw.VR or dictionary_VR(raw.tag))
        except KeyError:
            continue
        if value_representation not in _CHARSET_TEXT_VRS:
            continue
        if not isinstance(raw.value, bytes) or not any(
            byte >= 0x80 for byte in raw.value
        ):
            continue
        remaining = _CHARSET_SAMPLE_LIMIT - total
        if remaining <= 0:
            break
        sample = raw.value[:remaining]
        samples.append(sample)
        total += len(sample)
    return samples


def _decode_character_set_samples(
    samples: list[bytes], encoding: str
) -> str | None:
    try:
        return "\n".join(
            sample.rstrip(b"\x00 ").decode(encoding, errors="strict")
            for sample in samples
        )
    except (LookupError, UnicodeDecodeError):
        return None


def _cjk_count(value: str | None) -> int:
    if not value:
        return 0
    return sum(
        "\u3400" <= char <= "\u4dbf"
        or "\u4e00" <= char <= "\u9fff"
        or "\uf900" <= char <= "\ufaff"
        for char in value
    )


def _non_ascii_count(value: str | None) -> int:
    return sum(ord(char) > 0x7F for char in value or "")


def _text_damage_score(value: str | None) -> int:
    """Score strong corruption signals without guessing from valid text alone."""

    score = 0
    for char in value or "":
        codepoint = ord(char)
        is_private_use = (
            0xE000 <= codepoint <= 0xF8FF
            or 0xF0000 <= codepoint <= 0xFFFFD
            or 0x100000 <= codepoint <= 0x10FFFD
        )
        if char == "\ufffd":
            score += 16
        elif is_private_use or (char not in "\n\r\t" and not char.isprintable()):
            score += 8
    return score


def _has_consecutive_high_bytes(samples: list[bytes]) -> bool:
    return any(
        any(first >= 0x80 and second >= 0x80 for first, second in zip(sample, sample[1:]))
        for sample in samples
    )


def _strong_cjk_encoding_evidence(samples: list[bytes], encoding: str) -> bool:
    """Return true only when multiple text values strongly support encoding.

    Requiring two dense CJK values and four consecutive high bytes avoids
    treating isolated or adjacent Latin-1 accents as Chinese multibyte text.
    Each sample is decoded independently so one malformed, unrelated value
    cannot invalidate otherwise consistent evidence.
    """

    votes = 0
    for sample in samples:
        raw = sample.rstrip(b"\x00 ")
        try:
            decoded = raw.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
        cjk_count = _cjk_count(decoded)
        visible_count = sum(
            not char.isspace() and char not in "^=\\" for char in decoded
        )
        if (
            _max_consecutive_high_bytes(raw) >= 4
            and cjk_count >= 2
            and cjk_count * 2 >= max(1, visible_count)
        ):
            votes += 1
            if votes >= 2:
                return True
    return False


def _max_consecutive_high_bytes(value: bytes) -> int:
    longest = 0
    current = 0
    for byte in value:
        if byte >= 0x80:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _is_sequence_value(value: object) -> bool:
    return isinstance(value, (list, tuple)) or value.__class__.__name__ in {
        "MultiValue",
        "Sequence",
    }


def _metadata_value(
    metadata: dict[str, object], keyword: str, default: object
) -> object:
    value = metadata.get(keyword, default)
    return default if value is None else value


def _sort_number(value: object) -> tuple[int, float | str]:
    try:
        return 0, float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1, str(value)


def _read_recovery_marker(root: Path) -> dict[str, object] | None:
    for relative in (RECOVERY_MARKER_PATH, RECOVERY_MARKER):
        try:
            payload = json.loads((root / relative).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("version") in {1, 2}:
            return payload
    return None


def _pdi_output_root(config: AppConfig) -> Path:
    configured = str(config.pdi_output_folder).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    destination = Path(config.dicom_destination_folder).expanduser().resolve()
    return destination / "PDI"


def cleanup_interrupted_pdi(config: AppConfig, attempt_id: str) -> list[Path]:
    normalized_attempt_id = attempt_id.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", normalized_attempt_id):
        raise ValueError("PDI 恢复标识格式不正确")
    output_root = _pdi_output_root(config)
    if not output_root.is_dir():
        return []
    removed: list[Path] = []
    for candidate in output_root.glob(".DCMGET_PDI_*.partial-*"):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        marker = _read_recovery_marker(candidate)
        if marker is None or marker.get("attempt_id") != normalized_attempt_id:
            continue
        try:
            shutil.rmtree(candidate)
        except OSError as exc:
            raise OSError(f"无法删除 PDI 暂存目录 {candidate}：{exc}") from exc
        if candidate.exists():
            raise OSError(f"PDI 暂存目录删除后仍然存在：{candidate}")
        removed.append(candidate)
    return removed


def _strict_profile(items: list[_DicomItem]) -> str:
    syntaxes = {item.transfer_syntax_uid for item in items}
    explicit_vr_little_endian = {"1.2.840.10008.1.2.1"}
    jpeg = {
        "1.2.840.10008.1.2.4.50",
        "1.2.840.10008.1.2.4.51",
        "1.2.840.10008.1.2.4.53",
        "1.2.840.10008.1.2.4.55",
        "1.2.840.10008.1.2.4.57",
        "1.2.840.10008.1.2.4.70",
    }
    jpeg_2000 = {"1.2.840.10008.1.2.4.90", "1.2.840.10008.1.2.4.91"}
    if syntaxes & jpeg and syntaxes <= jpeg | explicit_vr_little_endian:
        return "-Pfl"
    if syntaxes & jpeg_2000 and syntaxes <= jpeg_2000 | explicit_vr_little_endian:
        return "-Pf2"
    return "-Pgp"


def _verify_ohif_payload(root: Path) -> None:
    checksum_path = root / OHIF_PAYLOAD_CHECKSUMS
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError("离线阅片器缺少资源校验清单") from exc

    expected: dict[str, str] = {}
    for line in lines:
        digest, separator, relative = line.partition("  ")
        candidate = PurePosixPath(relative)
        if (
            not separator
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not relative
            or candidate.is_absolute()
            or ".." in candidate.parts
            or candidate.as_posix() in expected
        ):
            raise RuntimeError("离线阅片器资源校验清单格式无效")
        expected[candidate.as_posix()] = digest

    actual: dict[str, str] = {}
    try:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise RuntimeError("离线阅片器资源包含不允许的符号链接")
            if not path.is_file() or path == checksum_path:
                continue
            relative = path.relative_to(root).as_posix()
            actual[relative] = _sha256(path)
    except OSError as exc:
        raise RuntimeError(f"离线阅片器资源无法读取：{exc}") from exc
    if not actual or actual != expected:
        raise RuntimeError("离线阅片器资源校验失败，文件可能缺失或已损坏")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
