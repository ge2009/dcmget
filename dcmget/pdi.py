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
from pathlib import Path
from typing import Callable, Iterable

from pydicom import dcmread

from .config import AppConfig
from .core import ToolPaths
from .runtime import resource_root


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
    PREVIEWS = "生成网页预览"
    VIEWER = "加入查看器"
    VERIFYING = "校验导出目录"


class PreviewMode(str, Enum):
    HYBRID = "hybrid"
    ALL = "all"
    SERIES_COVER = "series_cover"


@dataclass(slots=True)
class PdiExportResult:
    status: PdiStatus
    output_directory: str = ""
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    source_count: int = 0
    exported_count: int = 0
    duplicate_count: int = 0
    preview_count: int = 0
    unpreviewable_count: int = 0
    strict_profile: bool | None = None
    core_tool_failure: bool = False


ProgressCallback = Callable[[PdiStage, int, int, str], None]
LogCallback = Callable[[str, str, str], None]
ProcessCallback = Callable[[str, int, str, bool], None]
RECOVERY_MARKER = ".DCMGET-EXPORT.JSON"


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
    frame_count: int
    displayable: bool

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
    """Build a portable DICOM file-set from an exact list of archived files."""

    def __init__(
        self,
        config: AppConfig,
        tools: ToolPaths,
        project_root: str | Path | None = None,
        viewer_source: str | Path | None = None,
        weasis_payload_dir: str | Path | None = None,
        log_callback: LogCallback | None = None,
        progress_callback: ProgressCallback | None = None,
        process_callback: ProcessCallback | None = None,
        recovery_id: str = "",
        reuse_published: bool = False,
    ):
        self.config = config
        self.tools = tools
        self.project_root = Path(project_root or resource_root())
        if viewer_source is not None and weasis_payload_dir is not None:
            raise ValueError("viewer_source 与 weasis_payload_dir 不能同时指定")
        selected_viewer = viewer_source or weasis_payload_dir
        self.weasis_payload_dir = (
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

    def request_cancel(self) -> None:
        self._cancel.set()
        with self._process_lock:
            process = self._current_process
        if process is not None:
            self._terminate_process(process)

    def export(self, files: Iterable[str | Path]) -> PdiExportResult:
        source_paths = [Path(path).expanduser().resolve() for path in files]
        result = PdiExportResult(
            status=PdiStatus.GENERATING,
            source_count=len(source_paths),
        )
        temporary: Path | None = None
        final: Path | None = None
        try:
            self._check_cancelled()
            institution = str(getattr(self.config, "pdi_institution_name", "")).strip()
            if not institution:
                raise ValueError("请先在设置中填写 PDI 机构名称")
            if not source_paths:
                raise ValueError("当前批次没有可导出的 DICOM 文件")

            mode = self._preview_mode()
            output_root = self._output_root()
            output_root.mkdir(parents=True, exist_ok=True)
            self._remove_recovery_partials(output_root)
            if self.reuse_published:
                published = self._find_reusable_export(output_root)
                if published is not None:
                    self._emit(
                        f"已恢复上次完成的 PDI 目录：{published.output_directory}",
                        "success" if published.status == PdiStatus.COMPLETED else "warning",
                    )
                    return published
            payload = self._resolve_weasis_payload()
            preview_requested = bool(
                getattr(self.config, "pdi_include_html_preview", True)
            )
            viewer_requested = bool(
                getattr(self.config, "pdi_include_weasis_windows", True)
            )
            preview_allowed, viewer_allowed, space_warnings = self._check_free_space(
                output_root,
                source_paths,
                payload,
                preview_requested,
                viewer_requested,
            )
            result.warnings.extend(space_warnings)
            final = self._next_output_directory(output_root)
            temporary = output_root / f".{final.name}.partial-{uuid.uuid4().hex[:8]}"
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

            previews: dict[tuple[str, ...], list[Path]] = {}
            unpreviewable: list[_DicomItem] = []
            preview_problem = preview_requested and not preview_allowed
            if preview_problem:
                unpreviewable = list(items)
                result.unpreviewable_count = len(items)
            if preview_allowed:
                try:
                    previews, unpreviewable = self._create_previews(
                        temporary, items, mode
                    )
                    result.preview_count = sum(
                        len(paths) for paths in previews.values()
                    )
                    result.unpreviewable_count = len(unpreviewable)
                    preview_problem = bool(unpreviewable)
                    if unpreviewable:
                        result.warnings.append(
                            f"{len(unpreviewable)} 个 DICOM 对象无法生成完整网页预览"
                        )
                except _Cancelled:
                    raise
                except Exception as exc:
                    shutil.rmtree(temporary / "IHE_PDI", ignore_errors=True)
                    previews = {}
                    unpreviewable = list(items)
                    result.unpreviewable_count = len(items)
                    preview_problem = True
                    warning = f"网页预览生成失败，DICOMDIR 仍可使用：{exc}"
                    result.warnings.append(warning)
                    self._emit(warning, "warning")

            viewer_problem = viewer_requested and not viewer_allowed
            viewer_included = False
            if viewer_allowed:
                try:
                    viewer_included, viewer_warning = self._copy_weasis(
                        temporary, payload
                    )
                    if viewer_warning:
                        viewer_problem = True
                        result.warnings.append(viewer_warning)
                except _Cancelled:
                    raise
                except Exception as exc:
                    shutil.rmtree(temporary / "VIEWER", ignore_errors=True)
                    (temporary / "RUN.bat").unlink(missing_ok=True)
                    viewer_problem = True
                    warning = f"Weasis 查看器加入失败，DICOMDIR 仍可使用：{exc}"
                    result.warnings.append(warning)
                    self._emit(warning, "warning")

            self._write_index(
                temporary,
                institution,
                items,
                previews,
                unpreviewable,
                viewer_included,
            )
            self._write_readme(
                temporary,
                institution,
                strict_profile,
                result.warnings,
                viewer_included,
            )

            is_partial = (not strict_profile) or preview_problem or viewer_problem
            result.status = PdiStatus.PARTIAL if is_partial else PdiStatus.COMPLETED
            result.message = (
                f"PDI 便携目录已生成，包含 {len(items)} 个 DICOM 文件"
                if result.status == PdiStatus.COMPLETED
                else f"PDI 目录已生成，但有 {len(result.warnings)} 条警告"
            )
            self._write_recovery_marker(temporary, result, state="published")

            self._progress(PdiStage.VERIFYING, 0, 1, "正在生成 SHA-256 校验清单")
            self._write_manifest(temporary)
            self._verify_published_content(temporary, items)
            self._progress(PdiStage.VERIFYING, 1, 1, "导出目录校验完成")
            self._check_cancelled()
            temporary.rename(final)
            temporary = None

            result.output_directory = str(final)
            self._emit(result.message, "success" if not is_partial else "warning")
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
            try:
                frame_count = max(1, int(dataset.get("NumberOfFrames", 1) or 1))
            except (TypeError, ValueError):
                frame_count = 1
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
                frame_count=frame_count,
                displayable=bool(dataset.get("Rows", 0) and dataset.get("Columns", 0)),
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
            if isinstance(value, str):
                parts = tuple(part for part in value.split("\\") if part)
            else:
                parts = tuple(str(part) for part in value)
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

    def _create_previews(
        self, root: Path, items: list[_DicomItem], mode: PreviewMode
    ) -> tuple[dict[tuple[str, ...], list[Path]], list[_DicomItem]]:
        tool = self._tool_path("dcmj2pnm")
        candidates = self._preview_candidates(items, mode)
        previews: dict[tuple[str, ...], list[Path]] = {}
        unpreviewable: list[_DicomItem] = [item for item in items if not item.displayable]
        self._progress(PdiStage.PREVIEWS, 0, len(candidates), "正在生成静态预览")
        if not tool.is_file():
            warning = f"未找到 DCMTK dcmj2pnm：{tool}"
            self._emit(warning, "warning")
            return {}, unpreviewable + list(candidates)

        for current, item in enumerate(candidates, 1):
            self._check_cancelled()
            frame_total = self._preview_frame_count(item, mode)
            generated: list[Path] = []
            failed = False
            for frame in range(1, frame_total + 1):
                self._check_cancelled()
                relative = Path(
                    "IHE_PDI",
                    "HTML",
                    "PREVIEW",
                    f"IMG{current:04d}",
                    f"F{frame:04d}.JPG",
                )
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                command = [
                    str(tool),
                    "+oj",
                    "+Jq",
                    "85",
                    "+Wm",
                    "+F",
                    str(frame),
                    str(root / item.relative_path),
                    str(destination),
                ]
                converted = self._run_command(command, root)
                if converted.returncode or not destination.is_file():
                    destination.unlink(missing_ok=True)
                    failed = True
                    self._emit(
                        f"无法生成预览：{item.source.name} 第 {frame} 帧",
                        "warning",
                    )
                    break
                generated.append(relative)
            if generated:
                previews[item.file_id] = generated
            if failed or not generated:
                unpreviewable.append(item)
            self._progress(
                PdiStage.PREVIEWS,
                current,
                len(candidates),
                f"已处理预览 {current}/{len(candidates)}",
            )
        return previews, unpreviewable

    @staticmethod
    def _preview_candidates(
        items: list[_DicomItem], mode: PreviewMode
    ) -> list[_DicomItem]:
        displayable = [item for item in items if item.displayable]
        if mode != PreviewMode.SERIES_COVER:
            return displayable
        selected: list[_DicomItem] = []
        seen_series: set[str] = set()
        for item in displayable:
            series_key = item.series_instance_uid or "\\".join(item.file_id[:-1])
            if series_key in seen_series:
                continue
            seen_series.add(series_key)
            selected.append(item)
        return selected

    @staticmethod
    def _preview_frame_count(item: _DicomItem, mode: PreviewMode) -> int:
        if mode == PreviewMode.SERIES_COVER:
            return 1
        if mode == PreviewMode.HYBRID:
            return min(item.frame_count, 100)
        return item.frame_count

    def _copy_weasis(
        self, root: Path, payload: Path | None
    ) -> tuple[bool, str | None]:
        self._progress(PdiStage.VIEWER, 0, 1, "正在加入 Windows Weasis")
        if payload is None:
            return False, "未找到 Windows Weasis 便携资源，已保留 HTML 和 DICOMDIR"
        executable = payload / "Weasis.exe"
        licenses = [path for path in payload.rglob("LICENSE*") if path.is_file()]
        third_party = [
            path for path in payload.rglob("THIRD_PARTY*") if path.is_file()
        ]
        if not executable.is_file():
            return False, f"Weasis 资源中未找到 Weasis.exe：{payload}"
        if not licenses or not third_party:
            return False, f"Weasis 资源缺少开源许可证或第三方声明：{payload}"

        destination = root / "VIEWER" / "WINDOWS"
        shutil.copytree(payload, destination, copy_function=self._copy_file)
        (root / "RUN.bat").write_text(
            "@echo off\r\n"
            "cd /d \"%~dp0\"\r\n"
            'start "" "VIEWER\\WINDOWS\\Weasis.exe" '
            '"weasis://%%24dicom%%3Aget%%20-p%%20%%24weasis%%3Aconfig%%20pro%%3D%%22weasis.portable.dir%%20.%%22"\r\n',
            encoding="ascii",
            newline="",
        )
        self._progress(PdiStage.VIEWER, 1, 1, "Windows Weasis 已加入")
        return True, None

    def _write_index(
        self,
        root: Path,
        institution: str,
        items: list[_DicomItem],
        previews: dict[tuple[str, ...], list[Path]],
        unpreviewable: list[_DicomItem],
        viewer_included: bool,
    ) -> None:
        cards: list[str] = []
        for item in items:
            images = "".join(
                f'<img loading="lazy" src="{_escape_path(path)}" alt="DICOM 预览">'
                for path in previews.get(item.file_id, [])
            )
            if not images:
                images = '<p class="muted">该实例无静态预览，请使用 DICOM 查看器。</p>'
            dicom_path = "/".join(item.file_id)
            cards.append(
                '<article class="card">'
                f"<h2>{html.escape(item.patient_name or item.patient_id or '未命名患者')}</h2>"
                f"<p>患者 ID：{html.escape(item.patient_id or '-')} &nbsp; "
                f"检查号：{html.escape(item.accession_number or '-')} &nbsp; "
                f"日期：{html.escape(item.study_date or '-')} &nbsp; "
                f"类型：{html.escape(item.modality or '-')}</p>"
                f"<p>{html.escape(item.study_description or item.series_description or '')}</p>"
                f'<p><a href="{html.escape(dicom_path, quote=True)}">DICOM 原始文件</a></p>'
                f'<div class="images">{images}</div>'
                "</article>"
            )
        unsupported = "".join(
            f"<li>{html.escape(item.source.name)} ({html.escape(item.modality or '-')})</li>"
            for item in unpreviewable
        ) or "<li>无</li>"
        viewer = (
            '<p><a class="button" href="RUN.bat">使用 Windows Weasis 打开</a></p>'
            if viewer_included
            else "<p>Windows Weasis 未包含在本目录中。</p>"
        )
        document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DcmGet PDI</title><style>
body{{margin:0;background:#f3f6f9;color:#172235;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:1180px;margin:auto;padding:28px}}header,.card,.notice{{background:#fff;border:1px solid #dce4ec;border-radius:12px;padding:20px;margin-bottom:16px}}
h1{{color:#075ea8;margin-top:0}}.warning{{color:#8a4b00;background:#fff5df}}.muted{{color:#526173}}
.images{{display:flex;gap:10px;overflow:auto}}img{{max-width:280px;max-height:280px;object-fit:contain;background:#000}}
a{{color:#075ea8}}.button{{display:inline-block;background:#075ea8;color:white;padding:10px 16px;border-radius:6px;text-decoration:none}}
</style></head><body><main><header><h1>DcmGet PDI 便携影像</h1>
<p>机构：{html.escape(institution)}</p>{viewer}<p><a href="README.TXT">查看使用说明</a></p></header>
<section class="notice warning"><strong>仅供查阅，不用于诊断。</strong> DICOM 原始文件为准。</section>
{''.join(cards)}
<section class="card"><h2>无法生成网页预览的对象</h2><ul>{unsupported}</ul></section>
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
            "Windows 用户可双击 RUN.bat 启动 Weasis。"
            if viewer_included
            else "本目录未包含 Windows Weasis，可使用 INDEX.HTM 或外部 DICOM 查看器。"
        )
        warning_lines = "\n".join(f"- {warning}" for warning in warnings) or "- 无"
        content = f"""DcmGet PDI 便携影像目录

机构：{institution}
生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}

使用方法：
1. 将整个目录复制到 U 盘，不要只复制部分文件。
2. 双击 INDEX.HTM 查看静态预览。
3. {viewer}

{profile}
静态网页仅供查阅，不用于诊断；DICOM 原始文件为准。
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
        (root / "MANIFEST.SHA256").write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
        )

    def _verify_published_content(self, root: Path, items: list[_DicomItem]) -> None:
        required = ("DICOMDIR", "INDEX.HTM", "README.TXT", "MANIFEST.SHA256")
        missing = [name for name in required if not (root / name).is_file()]
        if missing:
            raise RuntimeError(f"PDI 目录缺少文件：{'、'.join(missing)}")
        for item in items:
            destination = root / item.relative_path
            if not destination.is_file() or self._hash_file(destination) != item.digest:
                raise RuntimeError(f"PDI DICOM 文件校验失败：{destination}")
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
                    self._terminate_process(process)
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
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
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

    def _preview_mode(self) -> PreviewMode:
        value = str(getattr(self.config, "pdi_preview_mode", PreviewMode.HYBRID.value))
        try:
            return PreviewMode(value)
        except ValueError as exc:
            raise ValueError(f"不支持的 PDI 预览模式：{value}") from exc

    def _output_root(self) -> Path:
        configured = str(getattr(self.config, "pdi_output_folder", "")).strip()
        if configured:
            return Path(configured).expanduser().resolve()
        destination = Path(self.config.dicom_destination_folder).expanduser().resolve()
        return destination / "PDI"

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
            "duplicate_count": result.duplicate_count,
            "exported_count": result.exported_count,
            "message": result.message,
            "preview_count": result.preview_count,
            "source_count": result.source_count,
            "state": state,
            "status": result.status.value,
            "strict_profile": result.strict_profile,
            "attempt_id": self.recovery_id,
            "unpreviewable_count": result.unpreviewable_count,
            "version": 1,
            "warnings": list(result.warnings),
        }
        (root / RECOVERY_MARKER).write_text(
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

    def _remove_recovery_partials(self, output_root: Path) -> None:
        if not self.recovery_id:
            return
        for candidate in output_root.glob(".DCMGET_PDI_*.partial-*"):
            if not candidate.is_dir():
                continue
            marker = self._read_recovery_marker(candidate)
            if marker is None or marker.get("attempt_id") != self.recovery_id:
                continue
            shutil.rmtree(candidate, ignore_errors=True)
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
                self._verify_published_content(candidate, [])
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
                    preview_count=int(marker.get("preview_count", 0)),
                    unpreviewable_count=int(marker.get("unpreviewable_count", 0)),
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
        try:
            payload = json.loads((root / RECOVERY_MARKER).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return None
        return payload

    def _resolve_weasis_payload(self) -> Path | None:
        if not bool(getattr(self.config, "pdi_include_weasis_windows", True)):
            return None
        if self.weasis_payload_dir is not None:
            return self.weasis_payload_dir if self.weasis_payload_dir.is_dir() else None
        candidates = (
            resource_root() / ".runtime" / "weasis" / "windows-x86_64" / "Weasis",
            self.project_root / ".runtime" / "weasis" / "windows-x86_64" / "Weasis",
            self.project_root / "resources" / "weasis" / "windows-x86_64" / "Weasis",
        )
        return next((path for path in candidates if path.is_dir()), None)

    @staticmethod
    def _check_free_space(
        output_root: Path,
        source_paths: list[Path],
        payload: Path | None,
        preview_requested: bool,
        viewer_requested: bool,
    ) -> tuple[bool, bool, list[str]]:
        source_size = sum(path.stat().st_size for path in source_paths if path.is_file())
        available = shutil.disk_usage(output_root).free
        core_reserve = max(10 * 1024 * 1024, len(source_paths) * 4096)
        core_required = source_size + core_reserve
        if available < core_required:
            raise OSError(
                f"PDI 导出空间不足：核心目录至少需要 {core_required} 字节，"
                f"当前可用 {available} 字节"
            )
        remaining = available - core_required
        warnings: list[str] = []
        preview_allowed = preview_requested
        if preview_requested:
            preview_reserve = max(
                10 * 1024 * 1024, min(source_size, 512 * 1024 * 1024)
            )
            if remaining < preview_reserve:
                preview_allowed = False
                warnings.append("可用空间不足，已跳过可选网页图像预览")
            else:
                remaining -= preview_reserve

        viewer_allowed = viewer_requested
        if viewer_requested and payload is not None:
            try:
                payload_size = sum(
                    path.stat().st_size
                    for path in payload.rglob("*")
                    if path.is_file()
                )
            except OSError as exc:
                viewer_allowed = False
                warnings.append(f"无法检查 Weasis 资源，已跳过可选查看器：{exc}")
            else:
                if remaining < payload_size:
                    viewer_allowed = False
                    warnings.append("可用空间不足，已跳过可选 Windows Weasis 查看器")
        return preview_allowed, viewer_allowed, warnings

    @staticmethod
    def _next_output_directory(output_root: Path) -> Path:
        base = f"DCMGET_PDI_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        candidate = output_root / base
        suffix = 1
        while candidate.exists():
            candidate = output_root / f"{base}-{suffix}"
            suffix += 1
        return candidate

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
            self._emit(f"无法更新 PDI 子进程恢复信息：{exc}", "warning")


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _escape_path(path: Path) -> str:
    return html.escape(path.as_posix(), quote=True)
