from __future__ import annotations

import hashlib
import html
import json
import os
import re
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable

from pydicom import dcmread


MANIFEST_NAME = "MANIFEST.SHA256"
OHIF_MANIFEST_NAME = "DCMGET_PAYLOAD.SHA256"
STUDY_INDEX = "VIEWER/.dcmget/index"
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_ENTRIES = 1_000_000
MAX_REPORTED_ISSUES = 500
MAX_VOLUME_SET_BYTES = 2 * 1024 * 1024
_HASH_BLOCK_SIZE = 1024 * 1024
_MANIFEST_LINE = re.compile(r"^([0-9A-Fa-f]{64}) ([ *])(.+)$")


class PdiVerificationStatus(str, Enum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PdiVerificationSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class PdiVerificationStage(str, Enum):
    PREPARING = "preparing"
    MANIFEST = "manifest"
    DICOMDIR = "dicomdir"
    VIEWER = "viewer"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class PdiVerificationIssue:
    severity: PdiVerificationSeverity
    code: str
    message: str
    path: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class PdiVerificationProgress:
    stage: PdiVerificationStage
    current: int
    total: int
    message: str


@dataclass(frozen=True, slots=True)
class PdiVerificationResult:
    root_directory: str
    status: PdiVerificationStatus
    message: str
    started_at: str
    finished_at: str
    duration_seconds: float
    manifest_entries: int = 0
    verified_files: int = 0
    hashed_bytes: int = 0
    dicomdir_references: int = 0
    indexed_instances: int = 0
    viewer_included: bool = False
    launcher_files: tuple[str, ...] = ()
    issues: tuple[PdiVerificationIssue, ...] = ()
    suppressed_issue_count: int = 0

    @property
    def ok(self) -> bool:
        return self.status in {
            PdiVerificationStatus.PASSED,
            PdiVerificationStatus.WARNING,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "report_version": 1,
            "root_directory": self.root_directory,
            "status": self.status.value,
            "status_label": _status_label(self.status),
            "message": self.message,
            "verification_scope": (
                "验证文件完整性、DICOMDIR 内部引用和离线阅片资源；"
                "不代表影像适合诊断，也不证明患者信息已经完成匿名处理。"
            ),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "statistics": {
                "manifest_entries": self.manifest_entries,
                "verified_files": self.verified_files,
                "hashed_bytes": self.hashed_bytes,
                "dicomdir_references": self.dicomdir_references,
                "indexed_instances": self.indexed_instances,
            },
            "viewer": {
                "included": self.viewer_included,
                "launchers": list(self.launcher_files),
            },
            "issues": [issue.to_dict() for issue in self.issues],
            "suppressed_issue_count": self.suppressed_issue_count,
        }


@dataclass(frozen=True, slots=True)
class PdiDeliveryReportPaths:
    json_path: Path
    html_path: Path


ProgressCallback = Callable[[PdiVerificationProgress], None]


def discover_pdi_verification_roots(root: str | Path) -> tuple[Path, ...]:
    """Return one PDI root or every declared volume in a PDI volume set.

    The volume-set index is treated as untrusted portable-media input.  Its
    directory declarations must be sequential and must exactly match the
    ``VOLUME_###`` directories present on disk before any volume is verified.
    """

    selected = Path(root).expanduser()
    if selected.is_symlink():
        raise ValueError("PDI 根目录不能是符号链接")
    try:
        selected = selected.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"PDI 根目录无法访问：{exc}") from exc
    if not selected.is_dir():
        raise ValueError("选择的 PDI 根路径不是目录")
    index_path = selected / "VOLUME_SET.json"
    if not index_path.exists():
        return (selected,)
    if index_path.is_symlink() or not index_path.is_file():
        raise ValueError("PDI 分卷清单不是普通文件")
    try:
        if index_path.stat().st_size > MAX_VOLUME_SET_BYTES:
            raise ValueError("PDI 分卷清单超过大小限制")
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"PDI 分卷清单无法读取：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("PDI 分卷清单格式无效")
    if (
        payload.get("schema") != "dcmget-pdi-volume-set"
        or payload.get("version") != 1
    ):
        raise ValueError("PDI 分卷清单版本不受支持")
    declared = payload.get("volumes")
    if not isinstance(declared, list) or not declared:
        raise ValueError("PDI 分卷清单没有卷记录")

    expected_names: list[str] = []
    for position, item in enumerate(declared, start=1):
        if not isinstance(item, dict):
            raise ValueError("PDI 分卷清单包含无效卷记录")
        expected_name = f"VOLUME_{position:03d}"
        if item.get("number") != position or item.get("directory") != expected_name:
            raise ValueError("PDI 分卷编号或目录声明不连续")
        expected_names.append(expected_name)

    actual_names = sorted(
        path.name
        for path in selected.glob("VOLUME_[0-9][0-9][0-9]")
        if path.is_dir() and not path.is_symlink()
    )
    if actual_names != expected_names:
        raise ValueError("PDI 分卷目录与 VOLUME_SET.json 清单不一致")
    roots: list[Path] = []
    for name in expected_names:
        candidate = selected / name
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError(f"PDI 分卷目录无效：{name}")
        roots.append(candidate.resolve(strict=True))
    return tuple(roots)


def pdi_delivery_report_output_directory(
    selected_root: str | Path,
    volume_root: str | Path,
    volume_count: int,
) -> Path | None:
    """Return a safe per-volume report directory for a volume set.

    A single PDI keeps the historical default next to that PDI.  Reports for a
    declared volume set live in a sibling directory of the whole set so report
    generation never mutates the directory users copy to removable media.
    """

    if volume_count <= 1:
        return None
    selected = Path(selected_root).expanduser().resolve(strict=False)
    volume = Path(volume_root).expanduser().resolve(strict=False)
    try:
        relative = volume.relative_to(selected)
    except ValueError as exc:
        raise ValueError("PDI 分卷不在所选分卷集目录内") from exc
    if len(relative.parts) != 1 or not re.fullmatch(r"VOLUME_[0-9]{3}", volume.name):
        raise ValueError("PDI 分卷目录名称无效")
    return selected.parent / f"{selected.name}-验收报告" / volume.name


class _VerificationCancelled(RuntimeError):
    pass


@dataclass(slots=True)
class _VerificationState:
    manifest_entries: int = 0
    verified_files: int = 0
    hashed_bytes: int = 0
    dicomdir_references: int = 0
    indexed_instances: int = 0
    viewer_included: bool = False
    launcher_files: list[str] = field(default_factory=list)
    issues: list[PdiVerificationIssue] = field(default_factory=list)
    suppressed_issue_count: int = 0
    hashes: dict[str, str] = field(default_factory=dict)
    manifest_dicom_files: set[str] = field(default_factory=set)


class PdiVerifier:
    """Verify an exported PDI without modifying the delivery directory."""

    def __init__(
        self,
        root: str | Path,
        *,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.root = Path(root).expanduser()
        self.progress_callback = progress_callback or (lambda _progress: None)
        self._external_cancel = cancel_event
        self._cancel = threading.Event()
        self._state = _VerificationState()

    def cancel(self) -> None:
        self._cancel.set()

    def verify(self) -> PdiVerificationResult:
        started_clock = time.monotonic()
        started_at = _now()
        self._state = _VerificationState()
        self._progress(
            PdiVerificationStage.PREPARING,
            0,
            1,
            "正在检查 PDI 目录",
        )
        cancelled = False
        try:
            self._check_cancelled()
            root = self._prepare_root()
            if root is not None:
                self._verify_required_files(root)
                self._state.hashes = self._verify_manifest(root, MANIFEST_NAME)
                self._verify_dicomdir(root)
                self._verify_viewer(root)
        except _VerificationCancelled:
            cancelled = True
        except Exception as exc:
            self._issue(
                PdiVerificationSeverity.ERROR,
                "verification_error",
                f"PDI 验证过程中发生异常：{exc}",
            )

        status = self._status(cancelled)
        message = {
            PdiVerificationStatus.PASSED: "PDI 交付验证通过",
            PdiVerificationStatus.WARNING: "PDI 核心文件有效，但存在需要确认的警告",
            PdiVerificationStatus.FAILED: "PDI 交付验证失败",
            PdiVerificationStatus.CANCELLED: "PDI 交付验证已取消",
        }[status]
        self._progress(
            PdiVerificationStage.COMPLETE,
            1,
            1,
            message,
            check_cancel=False,
        )
        finished_at = _now()
        return PdiVerificationResult(
            root_directory=str(self.root.absolute()),
            status=status,
            message=message,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=max(0.0, time.monotonic() - started_clock),
            manifest_entries=self._state.manifest_entries,
            verified_files=self._state.verified_files,
            hashed_bytes=self._state.hashed_bytes,
            dicomdir_references=self._state.dicomdir_references,
            indexed_instances=self._state.indexed_instances,
            viewer_included=self._state.viewer_included,
            launcher_files=tuple(self._state.launcher_files),
            issues=tuple(self._state.issues),
            suppressed_issue_count=self._state.suppressed_issue_count,
        )

    def _prepare_root(self) -> Path | None:
        if self.root.is_symlink():
            self._issue(
                PdiVerificationSeverity.ERROR,
                "root_symlink",
                "PDI 根目录不能是符号链接",
                str(self.root),
            )
            return None
        try:
            root = self.root.resolve(strict=True)
        except OSError as exc:
            self._issue(
                PdiVerificationSeverity.ERROR,
                "root_unavailable",
                f"PDI 根目录无法访问：{exc}",
                str(self.root),
            )
            return None
        if not root.is_dir():
            self._issue(
                PdiVerificationSeverity.ERROR,
                "root_not_directory",
                "选择的 PDI 根路径不是目录",
                str(root),
            )
            return None
        self._progress(
            PdiVerificationStage.PREPARING,
            1,
            1,
            "PDI 根目录已就绪",
        )
        return root

    def _verify_required_files(self, root: Path) -> None:
        for relative in ("DICOMDIR", "INDEX.HTM", "README.TXT", MANIFEST_NAME):
            target = root / relative
            if target.is_symlink() or not target.is_file():
                self._issue(
                    PdiVerificationSeverity.ERROR,
                    "required_file_missing",
                    f"PDI 缺少必需文件：{relative}",
                    relative,
                )

    def _verify_manifest(self, root: Path, relative_manifest: str) -> dict[str, str]:
        manifest_path = root / relative_manifest
        expected = self._read_manifest(
            root,
            manifest_path,
            relative_manifest,
            error_prefix="PDI SHA-256 清单",
        )
        if relative_manifest == MANIFEST_NAME:
            self._state.manifest_dicom_files = {
                relative for relative in expected if relative.startswith("DICOM/")
            }
        self._state.manifest_entries = len(expected)
        if not expected:
            return {}

        actual_files = self._collect_files(root, excluded={relative_manifest})
        expected_files = set(expected)
        for relative in sorted(expected_files - actual_files):
            self._issue(
                PdiVerificationSeverity.ERROR,
                "manifest_file_missing",
                f"清单中的文件不存在：{relative}",
                relative,
            )
        for relative in sorted(actual_files - expected_files):
            self._issue(
                PdiVerificationSeverity.ERROR,
                "manifest_file_unlisted",
                f"目录中存在未写入清单的文件：{relative}",
                relative,
            )

        hashes: dict[str, str] = {}
        total = len(expected)
        for index, (relative, expected_digest) in enumerate(expected.items(), start=1):
            self._check_cancelled()
            target = self._safe_target(root, relative)
            if target is None or not target.is_file():
                continue
            self._progress(
                PdiVerificationStage.MANIFEST,
                index - 1,
                total,
                f"正在校验 {relative}",
            )
            try:
                digest, size = self._hash_file(target)
            except OSError as exc:
                self._issue(
                    PdiVerificationSeverity.ERROR,
                    "manifest_file_unreadable",
                    f"无法读取清单文件：{exc}",
                    relative,
                )
                continue
            hashes[relative] = digest
            self._state.hashed_bytes += size
            if digest != expected_digest:
                self._issue(
                    PdiVerificationSeverity.ERROR,
                    "manifest_digest_mismatch",
                    f"文件 SHA-256 与清单不一致：{relative}",
                    relative,
                )
            else:
                self._state.verified_files += 1
            self._progress(
                PdiVerificationStage.MANIFEST,
                index,
                total,
                f"已校验 {index}/{total} 个文件",
            )
        return hashes

    def _verify_dicomdir(self, root: Path) -> None:
        path = root / "DICOMDIR"
        self._progress(
            PdiVerificationStage.DICOMDIR,
            0,
            1,
            "正在检查 DICOMDIR 引用",
        )
        if path.is_symlink() or not path.is_file():
            return
        try:
            dataset = dcmread(path)
            records = list(dataset.get("DirectoryRecordSequence", []))
        except Exception as exc:
            self._issue(
                PdiVerificationSeverity.ERROR,
                "dicomdir_unreadable",
                f"DICOMDIR 无法读取：{exc}",
                "DICOMDIR",
            )
            return

        references: set[str] = set()
        total = max(1, len(records))
        for index, record in enumerate(records, start=1):
            self._check_cancelled()
            value = record.get("ReferencedFileID")
            if value:
                parts = _referenced_file_parts(value)
                if not parts or any(not _valid_file_id_part(part) for part in parts):
                    display = "/".join(parts) if parts else str(value)
                    self._issue(
                        PdiVerificationSeverity.ERROR,
                        "dicomdir_invalid_reference",
                        f"DICOMDIR 包含无效文件引用：{display}",
                        "DICOMDIR",
                    )
                else:
                    relative = "/".join(parts)
                    target = self._safe_target(root, relative)
                    if target is None or not target.is_file():
                        self._issue(
                            PdiVerificationSeverity.ERROR,
                            "dicomdir_reference_missing",
                            f"DICOMDIR 引用的文件不存在：{relative}",
                            relative,
                        )
                    if relative in references:
                        self._issue(
                            PdiVerificationSeverity.ERROR,
                            "dicomdir_reference_duplicate",
                            f"DICOMDIR 重复引用同一影像文件：{relative}",
                            relative,
                        )
                    references.add(relative)
            self._progress(
                PdiVerificationStage.DICOMDIR,
                index,
                total,
                f"已检查 {index}/{len(records)} 条目录记录",
            )
        self._state.dicomdir_references = len(references)
        if not references:
            self._issue(
                PdiVerificationSeverity.ERROR,
                "dicomdir_no_references",
                "DICOMDIR 中没有可交付的影像文件引用",
                "DICOMDIR",
            )
        expected = self._state.manifest_dicom_files
        for relative in sorted(expected - references):
            self._issue(
                PdiVerificationSeverity.ERROR,
                "dicomdir_reference_omitted",
                f"DICOMDIR 遗漏主清单中的影像文件：{relative}",
                relative,
            )
        for relative in sorted(references - expected):
            self._issue(
                PdiVerificationSeverity.ERROR,
                "dicomdir_reference_unlisted",
                f"DICOMDIR 引用了主清单 DICOM 集合之外的文件：{relative}",
                relative,
            )

    def _verify_viewer(self, root: Path) -> None:
        ohif_root = root / "VIEWER" / "OHIF"
        viewer_markers = (
            ohif_root.exists(),
            (root / "VIEWER" / "pdi_server.py").exists(),
            any((root / name).exists() for name in _launcher_names()),
        )
        self._state.viewer_included = any(viewer_markers)
        self._progress(
            PdiVerificationStage.VIEWER,
            0,
            1,
            "正在检查离线阅片器",
        )
        if not self._state.viewer_included:
            self._issue(
                PdiVerificationSeverity.WARNING,
                "viewer_not_included",
                "本 PDI 未包含离线 OHIF 阅片器，可使用外部 DICOM 查看器",
            )
            return

        required = (
            "VIEWER/OHIF/index.html",
            "VIEWER/OHIF/app-config.js",
            f"VIEWER/OHIF/{OHIF_MANIFEST_NAME}",
            "VIEWER/pdi_server.py",
            "VIEWER/architecture.py",
            STUDY_INDEX,
            "OPEN_VIEWER.bat",
            "OPEN_VIEWER.command",
            "OPEN_VIEWER.sh",
        )
        for relative in required:
            target = self._safe_target(root, relative)
            if target is None or not target.is_file():
                self._issue(
                    PdiVerificationSeverity.ERROR,
                    "viewer_resource_missing",
                    f"离线阅片器缺少资源：{relative}",
                    relative,
                )

        license_files = list(ohif_root.glob("LICENSE*")) if ohif_root.is_dir() else []
        third_party_files = (
            list(ohif_root.glob("THIRD_PARTY*")) if ohif_root.is_dir() else []
        )
        if not any(path.is_file() and not path.is_symlink() for path in license_files):
            self._issue(
                PdiVerificationSeverity.ERROR,
                "viewer_license_missing",
                "离线 OHIF 阅片器缺少开源许可证",
                "VIEWER/OHIF",
            )
        if not any(path.is_file() and not path.is_symlink() for path in third_party_files):
            self._issue(
                PdiVerificationSeverity.ERROR,
                "viewer_notice_missing",
                "离线 OHIF 阅片器缺少第三方声明",
                "VIEWER/OHIF",
            )

        self._state.launcher_files = [
            name
            for name in _launcher_names()
            if (root / name).is_file() and not (root / name).is_symlink()
        ]
        if ohif_root.is_dir() and not ohif_root.is_symlink():
            self._verify_ohif_manifest(root, ohif_root)
        self._verify_study_index(root)
        self._progress(
            PdiVerificationStage.VIEWER,
            1,
            1,
            "离线阅片器检查完成",
        )

    def _verify_ohif_manifest(self, root: Path, ohif_root: Path) -> None:
        relative_manifest = f"VIEWER/OHIF/{OHIF_MANIFEST_NAME}"
        expected = self._read_manifest(
            ohif_root,
            ohif_root / OHIF_MANIFEST_NAME,
            OHIF_MANIFEST_NAME,
            error_prefix="离线阅片器资源清单",
        )
        if not expected:
            return
        actual_files = self._collect_files(ohif_root, excluded={OHIF_MANIFEST_NAME})
        if set(expected) != actual_files:
            self._issue(
                PdiVerificationSeverity.ERROR,
                "viewer_manifest_file_set_mismatch",
                "离线阅片器资源清单与实际文件集合不一致",
                relative_manifest,
            )
        total = len(expected)
        for index, (relative, expected_digest) in enumerate(expected.items(), start=1):
            self._check_cancelled()
            full_relative = f"VIEWER/OHIF/{relative}"
            digest = self._state.hashes.get(full_relative)
            if digest is None:
                target = self._safe_target(ohif_root, relative)
                if target is None or not target.is_file():
                    continue
                try:
                    digest, size = self._hash_file(target)
                except OSError as exc:
                    self._issue(
                        PdiVerificationSeverity.ERROR,
                        "viewer_resource_unreadable",
                        f"离线阅片器资源无法读取：{exc}",
                        full_relative,
                    )
                    continue
                self._state.hashed_bytes += size
            if digest != expected_digest:
                self._issue(
                    PdiVerificationSeverity.ERROR,
                    "viewer_digest_mismatch",
                    f"离线阅片器资源校验失败：{relative}",
                    full_relative,
                )
            self._progress(
                PdiVerificationStage.VIEWER,
                index,
                total,
                f"已检查 {index}/{total} 个 OHIF 资源",
            )

    def _verify_study_index(self, root: Path) -> None:
        index_path = root / STUDY_INDEX
        if index_path.is_symlink() or not index_path.is_file():
            return
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self._issue(
                PdiVerificationSeverity.ERROR,
                "viewer_index_unreadable",
                f"OHIF 检查索引无法读取：{exc}",
                STUDY_INDEX,
            )
            return
        studies = payload.get("studies") if isinstance(payload, dict) else None
        if not isinstance(studies, list):
            self._issue(
                PdiVerificationSeverity.ERROR,
                "viewer_index_invalid",
                "OHIF 检查索引缺少 studies 列表",
                STUDY_INDEX,
            )
            return
        references: set[str] = set()
        for study in studies:
            series_values = study.get("series") if isinstance(study, dict) else None
            if not isinstance(series_values, list):
                self._issue(
                    PdiVerificationSeverity.ERROR,
                    "viewer_index_invalid",
                    "OHIF 检查索引包含无效检查结构",
                    STUDY_INDEX,
                )
                continue
            for series in series_values:
                instances = series.get("instances") if isinstance(series, dict) else None
                if not isinstance(instances, list):
                    self._issue(
                        PdiVerificationSeverity.ERROR,
                        "viewer_index_invalid",
                        "OHIF 检查索引包含无效序列结构",
                        STUDY_INDEX,
                    )
                    continue
                for instance in instances:
                    self._check_cancelled()
                    url = str(instance.get("url", "")) if isinstance(instance, dict) else ""
                    relative = _local_dicom_url(url)
                    if relative is None:
                        self._issue(
                            PdiVerificationSeverity.ERROR,
                            "viewer_index_external_url",
                            "OHIF 检查索引包含非本地或越界 DICOM 地址",
                            STUDY_INDEX,
                        )
                        continue
                    target = self._safe_target(root, relative)
                    if target is None or not target.is_file():
                        self._issue(
                            PdiVerificationSeverity.ERROR,
                            "viewer_index_file_missing",
                            f"OHIF 检查索引引用的文件不存在：{relative}",
                            relative,
                        )
                    if relative in references:
                        self._issue(
                            PdiVerificationSeverity.ERROR,
                            "viewer_index_reference_duplicate",
                            f"OHIF 检查索引重复引用同一影像文件：{relative}",
                            relative,
                        )
                    references.add(relative)
        self._state.indexed_instances = len(references)
        expected = self._state.manifest_dicom_files
        for relative in sorted(expected - references):
            self._issue(
                PdiVerificationSeverity.ERROR,
                "viewer_index_reference_omitted",
                f"OHIF 检查索引遗漏主清单中的影像文件：{relative}",
                relative,
            )
        for relative in sorted(references - expected):
            self._issue(
                PdiVerificationSeverity.ERROR,
                "viewer_index_reference_unlisted",
                f"OHIF 检查索引引用了主清单 DICOM 集合之外的文件：{relative}",
                relative,
            )

    def _read_manifest(
        self,
        root: Path,
        manifest_path: Path,
        relative_manifest: str,
        *,
        error_prefix: str,
    ) -> dict[str, str]:
        if manifest_path.is_symlink():
            self._issue(
                PdiVerificationSeverity.ERROR,
                "manifest_symlink",
                f"{error_prefix}不能是符号链接",
                relative_manifest,
            )
            return {}
        try:
            if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
                raise ValueError("文件超过 64 MiB 上限")
            lines = manifest_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError, ValueError) as exc:
            self._issue(
                PdiVerificationSeverity.ERROR,
                "manifest_unreadable",
                f"{error_prefix}无法读取：{exc}",
                relative_manifest,
            )
            return {}
        if not lines or len(lines) > MAX_MANIFEST_ENTRIES:
            self._issue(
                PdiVerificationSeverity.ERROR,
                "manifest_entry_count_invalid",
                f"{error_prefix}条目数量无效",
                relative_manifest,
            )
            return {}

        expected: dict[str, str] = {}
        casefold_paths: set[str] = set()
        for line_number, line in enumerate(lines, start=1):
            self._check_cancelled()
            if not line:
                continue
            match = _MANIFEST_LINE.fullmatch(line)
            if match is None:
                self._issue(
                    PdiVerificationSeverity.ERROR,
                    "manifest_format_invalid",
                    f"{error_prefix}第 {line_number} 行格式无效",
                    relative_manifest,
                )
                continue
            digest = match.group(1).lower()
            relative = match.group(3)
            normalized = _safe_manifest_relative(relative)
            if normalized is None or normalized == relative_manifest:
                self._issue(
                    PdiVerificationSeverity.ERROR,
                    "manifest_path_invalid",
                    f"{error_prefix}第 {line_number} 行包含无效路径",
                    relative_manifest,
                )
                continue
            folded = normalized.casefold()
            if normalized in expected or folded in casefold_paths:
                self._issue(
                    PdiVerificationSeverity.ERROR,
                    "manifest_path_duplicate",
                    f"{error_prefix}包含重复路径：{normalized}",
                    normalized,
                )
                continue
            expected[normalized] = digest
            casefold_paths.add(folded)
        if not expected:
            self._issue(
                PdiVerificationSeverity.ERROR,
                "manifest_entry_count_invalid",
                f"{error_prefix}没有有效条目",
                relative_manifest,
            )
        return expected

    def _collect_files(self, root: Path, *, excluded: set[str]) -> set[str]:
        files: set[str] = set()
        try:
            for path in root.rglob("*"):
                self._check_cancelled()
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    self._issue(
                        PdiVerificationSeverity.ERROR,
                        "symlink_not_allowed",
                        f"PDI 包含不允许的符号链接：{relative}",
                        relative,
                    )
                    continue
                if path.is_file() and relative not in excluded:
                    files.add(relative)
        except OSError as exc:
            self._issue(
                PdiVerificationSeverity.ERROR,
                "directory_scan_failed",
                f"无法扫描 PDI 文件：{exc}",
                str(root),
            )
        return files

    def _safe_target(self, root: Path, relative: str) -> Path | None:
        normalized = _safe_manifest_relative(relative)
        if normalized is None:
            self._issue(
                PdiVerificationSeverity.ERROR,
                "path_outside_root",
                f"PDI 包含越界路径：{relative}",
                relative,
            )
            return None
        target = root.joinpath(*PurePosixPath(normalized).parts)
        current = root
        try:
            for part in PurePosixPath(normalized).parts:
                current = current / part
                if current.is_symlink():
                    self._issue(
                        PdiVerificationSeverity.ERROR,
                        "symlink_not_allowed",
                        f"PDI 路径经过符号链接：{normalized}",
                        normalized,
                    )
                    return None
            target.resolve(strict=False).relative_to(root.resolve())
        except (OSError, ValueError):
            self._issue(
                PdiVerificationSeverity.ERROR,
                "path_outside_root",
                f"PDI 包含越界路径：{relative}",
                relative,
            )
            return None
        return target

    def _hash_file(self, path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while True:
                self._check_cancelled()
                block = source.read(_HASH_BLOCK_SIZE)
                if not block:
                    break
                digest.update(block)
                size += len(block)
        return digest.hexdigest(), size

    def _issue(
        self,
        severity: PdiVerificationSeverity,
        code: str,
        message: str,
        path: str = "",
    ) -> None:
        if len(self._state.issues) < MAX_REPORTED_ISSUES:
            self._state.issues.append(
                PdiVerificationIssue(severity, code, message, path)
            )
        else:
            self._state.suppressed_issue_count += 1

    def _status(self, cancelled: bool) -> PdiVerificationStatus:
        if cancelled:
            return PdiVerificationStatus.CANCELLED
        if any(
            issue.severity == PdiVerificationSeverity.ERROR
            for issue in self._state.issues
        ) or self._state.suppressed_issue_count:
            return PdiVerificationStatus.FAILED
        if self._state.issues:
            return PdiVerificationStatus.WARNING
        return PdiVerificationStatus.PASSED

    def _check_cancelled(self) -> None:
        if self._cancel.is_set() or (
            self._external_cancel is not None and self._external_cancel.is_set()
        ):
            raise _VerificationCancelled("PDI verification cancelled")

    def _progress(
        self,
        stage: PdiVerificationStage,
        current: int,
        total: int,
        message: str,
        *,
        check_cancel: bool = True,
    ) -> None:
        if check_cancel:
            self._check_cancelled()
        self.progress_callback(
            PdiVerificationProgress(
                stage=stage,
                current=max(0, int(current)),
                total=max(0, int(total)),
                message=message,
            )
        )


def write_pdi_delivery_reports(
    result: PdiVerificationResult,
    output_directory: str | Path | None = None,
) -> PdiDeliveryReportPaths:
    """Write UTF-8 JSON and Chinese HTML reports outside the verified PDI."""

    pdi_root = Path(result.root_directory).expanduser().absolute()
    report_root = (
        Path(output_directory).expanduser()
        if output_directory is not None
        else pdi_root.parent / f"{pdi_root.name}-验收报告"
    )
    try:
        report_root_resolved = report_root.resolve(strict=False)
        pdi_root_resolved = pdi_root.resolve(strict=False)
        if report_root_resolved == pdi_root_resolved or report_root_resolved.is_relative_to(
            pdi_root_resolved
        ):
            raise ValueError("验收报告不能写入 PDI 目录，否则会破坏原始 SHA-256 清单")
        report_root.mkdir(parents=True, exist_ok=True)
        json_path = report_root / "PDI交付验收报告.json"
        html_path = report_root / "PDI交付验收报告.html"
        _atomic_write_text(
            json_path,
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )
        _atomic_write_text(html_path, _render_html_report(result))
    except (OSError, ValueError) as exc:
        raise OSError(f"无法生成 PDI 交付验收报告：{exc}") from exc
    return PdiDeliveryReportPaths(json_path=json_path, html_path=html_path)


def verify_pdi_directory(
    root: str | Path,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> PdiVerificationResult:
    """Convenience entry point for command-line and future UI integration."""

    return PdiVerifier(
        root,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    ).verify()


def _render_html_report(result: PdiVerificationResult) -> str:
    status_label = _status_label(result.status)
    issue_rows = "".join(
        "<tr>"
        f"<td>{'错误' if issue.severity == PdiVerificationSeverity.ERROR else '警告'}</td>"
        f"<td>{html.escape(issue.code)}</td>"
        f"<td>{html.escape(issue.message)}</td>"
        f"<td>{html.escape(issue.path)}</td>"
        "</tr>"
        for issue in result.issues
    )
    if not issue_rows:
        issue_rows = '<tr><td colspan="4">未发现异常</td></tr>'
    if result.suppressed_issue_count:
        issue_rows += (
            '<tr><td colspan="4">'
            f"另有 {result.suppressed_issue_count} 条异常未展开显示"
            "</td></tr>"
        )
    launchers = "、".join(result.launcher_files) or "未包含"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDI 交付验收报告</title>
<style>
body {{ margin: 32px auto; max-width: 1100px; padding: 0 24px; color: #18324a;
       background: #f4f7fa; font: 15px/1.6 system-ui, sans-serif; }}
main {{ background: white; border: 1px solid #dbe5ee; border-radius: 12px; padding: 28px; }}
h1 {{ margin-top: 0; }} .status {{ font-size: 20px; font-weight: 700; }}
table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
th, td {{ border: 1px solid #dbe5ee; padding: 8px 10px; text-align: left; vertical-align: top; }}
th {{ background: #edf5fb; }} .note {{ padding: 12px; background: #fff7e7; border-left: 4px solid #df8d18; }}
</style>
</head>
<body><main>
<h1>DcmGet PDI 交付验收报告</h1>
<p class="status">验收结果：{html.escape(status_label)}</p>
<table>
<tr><th>项目</th><th>结果</th></tr>
<tr><td>PDI 根目录</td><td>{html.escape(result.root_directory)}</td></tr>
<tr><td>开始时间</td><td>{html.escape(result.started_at)}</td></tr>
<tr><td>完成时间</td><td>{html.escape(result.finished_at)}</td></tr>
<tr><td>清单条目 / 校验通过</td><td>{result.manifest_entries} / {result.verified_files}</td></tr>
<tr><td>已哈希字节数</td><td>{result.hashed_bytes}</td></tr>
<tr><td>DICOMDIR 引用数</td><td>{result.dicomdir_references}</td></tr>
<tr><td>OHIF 索引实例数</td><td>{result.indexed_instances}</td></tr>
<tr><td>离线阅片器</td><td>{'已包含' if result.viewer_included else '未包含'}；启动器：{html.escape(launchers)}</td></tr>
</table>
<h2>异常与警告</h2>
<table><tr><th>级别</th><th>代码</th><th>说明</th><th>路径</th></tr>{issue_rows}</table>
<p class="note">本报告验证文件完整性、DICOMDIR 内部引用和离线阅片资源，
不代表影像适合诊断，也不证明患者信息已经完成匿名处理。</p>
</main></body></html>
"""


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_manifest_relative(value: str) -> str | None:
    if (
        not value
        or value != value.strip()
        or "\\" in value
        or "\x00" in value
        or re.match(r"^[A-Za-z]:", value)
    ):
        return None
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        return None
    candidate = PurePosixPath(value)
    if candidate.is_absolute():
        return None
    return candidate.as_posix()


def _referenced_file_parts(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part for part in value.split("\\") if part)
    try:
        return tuple(str(part) for part in value)  # type: ignore[arg-type]
    except TypeError:
        return ()


def _valid_file_id_part(value: str) -> bool:
    return bool(
        value
        and len(value) <= 8
        and all(character in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in value)
    )


def _local_dicom_url(value: str) -> str | None:
    prefix = "dicomweb:/"
    if not value.startswith(prefix):
        return None
    raw = urllib.parse.unquote(value.removeprefix(prefix).partition("?")[0])
    normalized = _safe_manifest_relative(raw)
    if normalized is None or not normalized.startswith("DICOM/"):
        return None
    return normalized


def _launcher_names() -> tuple[str, ...]:
    return (
        "OPEN_VIEWER.exe",
        "OPEN_VIEWER",
        "OPEN_VIEWER.bat",
        "OPEN_VIEWER.command",
        "OPEN_VIEWER.sh",
    )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _status_label(status: PdiVerificationStatus) -> str:
    return {
        PdiVerificationStatus.PASSED: "通过",
        PdiVerificationStatus.WARNING: "通过，但有警告",
        PdiVerificationStatus.FAILED: "失败",
        PdiVerificationStatus.CANCELLED: "已取消",
    }[status]
