from __future__ import annotations

import errno
import hashlib
import locale
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

from filelock import FileLock, Timeout

from .anonymization import DicomAnonymizer
from .architecture import ArchitectureError, require_amd64_pe
from .config import AppConfig
from .diagnostics import PrivateRotatingFileHandler
from .runtime import ensure_application_state_dir, portable_dcmtk_bin


_archive_publish_lock = threading.Lock()
_RECEIVER_BIND_ADDRESS = "0.0.0.0"


class AccessionStatus(str, Enum):
    WAITING = "等待"
    DOWNLOADING = "下载中"
    COMPLETED = "完成"
    NO_DATA = "无数据"
    PARTIAL = "部分成功"
    FAILED = "失败"
    CANCELLED = "已取消"


class ResultVerificationStatus(str, Enum):
    """Non-blocking ownership verification for permissively received files."""

    MATCHED = "核对通过"
    MISMATCH = "内容不匹配"
    UNVERIFIABLE = "无法核对"


@dataclass(frozen=True, slots=True)
class ToolPaths:
    movescu: Path
    storescp: Path
    bin_dir: Path
    version: str
    storescp_help: str = ""
    dcmmkdir: Path | None = None
    dcmdump: Path | None = None

    @property
    def supports_fork(self) -> bool:
        return "--fork" in self.storescp_help


@dataclass(slots=True)
class AccessionResult:
    accession: str
    status: AccessionStatus
    file_count: int = 0
    duration_seconds: float = 0.0
    message: str = ""
    output_directory: str = ""
    received_bytes: int = 0
    speed_bytes_per_second: float = 0.0
    archived_files: list[str] = field(default_factory=list)
    new_file_count: int = 0
    existing_skipped_count: int = 0
    conflict_preserved_count: int = 0
    verification_status: ResultVerificationStatus = (
        ResultVerificationStatus.UNVERIFIABLE
    )
    verification_message: str = ""
    actual_accessions: list[str] = field(default_factory=list)
    study_instance_uids: list[str] = field(default_factory=list)
    series_instance_count: int = 0
    sop_instance_count: int = 0
    local_verified_files: int = 0
    pacs_completed_suboperations: int | None = None
    move_return_code: int | None = None
    move_dimse_status: int | None = None
    attempt_count: int = 1
    transient_failure: bool = False
    safety_pause_reason: str = ""


@dataclass(frozen=True, slots=True)
class ReceivedDicomMetadata:
    file_path: str
    actual_accession_number: str = ""
    study_instance_uid: str = ""
    series_instance_uid: str = ""
    sop_instance_uid: str = ""
    size_bytes: int = 0
    metadata_error: str = ""


@dataclass(slots=True)
class ArchiveStats:
    """Per-archive outcome counts without changing the legacy tuple result."""

    new_file_count: int = 0
    existing_skipped_count: int = 0
    conflict_preserved_count: int = 0
    conflict_files: list[Path] = field(default_factory=list)
    source_accessions: set[str] = field(default_factory=set)
    missing_accession_count: int = 0
    study_instance_uids: set[str] = field(default_factory=set)
    series_instance_uids: set[str] = field(default_factory=set)
    sop_instance_uids: set[str] = field(default_factory=set)
    observations: list[ReceivedDicomMetadata] = field(default_factory=list)


class _ArchiveDisposition(str, Enum):
    PUBLISHED = "published"
    EXISTING_SKIPPED = "existing_skipped"
    CONFLICT_PRESERVED = "conflict_preserved"


@dataclass(frozen=True, slots=True)
class _ArchivePublication:
    disposition: _ArchiveDisposition
    path: Path


@dataclass(slots=True)
class BatchSummary:
    results: list[AccessionResult] = field(default_factory=list)
    cancelled: bool = False
    staging_directory: str = ""
    interrupted_reason: str = ""

    @property
    def exit_code(self) -> int:
        if self.cancelled:
            return 130
        if self.interrupted_reason:
            return 2
        if any(r.status in {AccessionStatus.FAILED, AccessionStatus.PARTIAL} for r in self.results):
            return 2
        return 0

    @property
    def failed_accessions(self) -> list[str]:
        return [
            result.accession
            for result in self.results
            if result.status in {AccessionStatus.FAILED, AccessionStatus.PARTIAL}
        ]

    @property
    def archived_files(self) -> list[str]:
        return [path for result in self.results for path in result.archived_files]

    @property
    def new_file_count(self) -> int:
        return sum(result.new_file_count for result in self.results)

    @property
    def existing_skipped_count(self) -> int:
        return sum(result.existing_skipped_count for result in self.results)

    @property
    def conflict_preserved_count(self) -> int:
        return sum(result.conflict_preserved_count for result in self.results)


@dataclass(slots=True)
class PreflightResult:
    tools: ToolPaths | None
    errors: dict[str, str]
    checks: list[tuple[str, bool, str]]

    @property
    def ok(self) -> bool:
        return not self.errors and self.tools is not None


LogCallback = Callable[[str, str, str], None]
StateCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, AccessionResult], None]
ReadyCallback = Callable[[], None]
ArchiveErrorCallback = Callable[[Path, str], None]
ProcessCallback = Callable[[str, int, str, bool], None]
AuditCallback = Callable[
    [AccessionResult, tuple[ReceivedDicomMetadata, ...]], None
]


@dataclass(slots=True)
class _MoveDiagnostics:
    association_accepted: bool = False
    association_request_failed: bool = False
    transport_timeout: bool = False
    pending_responses: int = 0
    final_response_status: str | None = None
    dimse_status_code: int | None = None
    dimse_status_text: str = ""
    remaining_suboperations: int | None = None
    completed_suboperations: int | None = None
    failed_suboperations: int | None = None
    warning_suboperations: int | None = None


_MOVE_RESPONSE_RE = re.compile(
    r"\bReceived\s+(?:Final\s+)?Move\s+Response(?:\s+\d+)?\s*"
    r"\((?P<status>[^)]*)\)",
    re.IGNORECASE,
)
_MOVE_DIMSE_STATUS_RE = re.compile(
    r"\bDIMSE\s+Status\b\s*(?::|=)?\s*(?:0x)?(?P<code>[0-9a-f]{4})\b"
    r"(?:\s*:\s*(?P<text>.*))?",
    re.IGNORECASE,
)
_MOVE_SUBOPERATION_RE = re.compile(
    r"\b(?:Number\s+of\s+)?"
    r"(?P<kind>Remaining|Completed|Failed|Warning)\s+"
    r"Sub[\s-]*Operations?\s*(?::|=)?\s*(?P<count>\d+)\b",
    re.IGNORECASE,
)
_PENDING_DIMSE_STATUSES = {0xFF00, 0xFF01}


def _record_move_diagnostic(diagnostics: _MoveDiagnostics, text: str) -> None:
    folded = text.casefold()
    if "association accepted" in folded:
        diagnostics.association_accepted = True
    if "association request failed" in folded:
        diagnostics.association_request_failed = True
    if "timeout" in folded and (
        diagnostics.association_request_failed or "tcp initialization error" in folded
    ):
        diagnostics.transport_timeout = True

    response = _MOVE_RESPONSE_RE.search(text)
    if response:
        response_status = response.group("status").strip()
        if re.match(r"pending\b", response_status, re.IGNORECASE):
            diagnostics.pending_responses += 1
        else:
            _reset_move_suboperation_counts(diagnostics)
            diagnostics.final_response_status = response_status

    dimse_status = _MOVE_DIMSE_STATUS_RE.search(text)
    if dimse_status:
        status_code = int(dimse_status.group("code"), 16)
        if (
            status_code not in _PENDING_DIMSE_STATUSES
            and diagnostics.dimse_status_code in _PENDING_DIMSE_STATUSES
            and diagnostics.final_response_status is None
        ):
            _reset_move_suboperation_counts(diagnostics)
        diagnostics.dimse_status_code = status_code
        diagnostics.dimse_status_text = (dimse_status.group("text") or "").strip()

    suboperation = _MOVE_SUBOPERATION_RE.search(text)
    if suboperation:
        kind = suboperation.group("kind").casefold()
        setattr(
            diagnostics,
            f"{kind}_suboperations",
            int(suboperation.group("count")),
        )


def _reset_move_suboperation_counts(diagnostics: _MoveDiagnostics) -> None:
    """Discard counts from the last pending response before parsing the final one."""

    diagnostics.remaining_suboperations = None
    diagnostics.completed_suboperations = None
    diagnostics.failed_suboperations = None
    diagnostics.warning_suboperations = None


def _move_has_final_response(diagnostics: _MoveDiagnostics) -> bool:
    if diagnostics.final_response_status is not None:
        return True
    return (
        diagnostics.dimse_status_code is not None
        and diagnostics.dimse_status_code not in _PENDING_DIMSE_STATUSES
    )


def _move_has_problem(diagnostics: _MoveDiagnostics) -> bool:
    final_response = diagnostics.final_response_status
    if final_response is not None and not re.match(
        r"success\b", final_response, re.IGNORECASE
    ):
        return True

    dimse_status = diagnostics.dimse_status_code
    if (
        dimse_status is not None
        and dimse_status not in _PENDING_DIMSE_STATUSES
        and dimse_status != 0x0000
    ):
        return True

    if (diagnostics.failed_suboperations or 0) > 0:
        return True
    if (diagnostics.warning_suboperations or 0) > 0:
        return True
    return (
        diagnostics.association_accepted or diagnostics.pending_responses > 0
    ) and not _move_has_final_response(diagnostics)


def _move_archive_mismatch(
    diagnostics: _MoveDiagnostics, archived_file_count: int
) -> str:
    """Describe a final C-MOVE count that cannot match the usable local archive."""

    if not _move_has_final_response(diagnostics):
        return ""
    details: list[str] = []
    remaining = diagnostics.remaining_suboperations
    if remaining is not None and remaining > 0:
        details.append(f"最终响应仍有 {remaining} 个子操作未完成")
    completed = diagnostics.completed_suboperations
    if completed is not None and completed != archived_file_count:
        details.append(
            f"PACS 报告完成 {completed} 个子操作，本机成功归档 {archived_file_count} 个文件"
        )
    return "；".join(details)


def _move_diagnostic_summary(diagnostics: _MoveDiagnostics) -> str:
    details: list[str] = []
    dimse_status = diagnostics.dimse_status_code
    if dimse_status is not None and dimse_status not in _PENDING_DIMSE_STATUSES:
        status = f"C-MOVE 最终状态 0x{dimse_status:04X}"
        status_text = (
            diagnostics.dimse_status_text or diagnostics.final_response_status or ""
        )
        if status_text:
            status += f"（{status_text}）"
        details.append(status)
    elif diagnostics.final_response_status is not None:
        details.append(f"C-MOVE 最终响应 {diagnostics.final_response_status}")

    if diagnostics.pending_responses and not _move_has_final_response(diagnostics):
        details.append(
            f"PACS 返回 {diagnostics.pending_responses} 次待处理响应后未返回最终响应"
        )

    counts = []
    for label, value in (
        ("剩余", diagnostics.remaining_suboperations),
        ("完成", diagnostics.completed_suboperations),
        ("失败", diagnostics.failed_suboperations),
        ("警告", diagnostics.warning_suboperations),
    ):
        if value is not None:
            counts.append(f"{label} {value}")
    if counts:
        details.append("子操作：" + "、".join(counts))
    return "；".join(details)


def _association_failure_summary(
    diagnostics: _MoveDiagnostics, config: AppConfig
) -> str:
    if not diagnostics.association_request_failed:
        return ""
    endpoint = f"{config.pacs_server_ip}:{config.pacs_server_port}"
    if diagnostics.transport_timeout:
        return f"连接 PACS {endpoint} 超时，C-MOVE 尚未提交"
    return f"无法与 PACS {endpoint} 建立 DICOM 关联，C-MOVE 尚未提交"


class _LiveStagingTracker:
    """Approximate live transfer metrics without repeatedly stat'ing every file.

    ``storescp -od`` writes directly into the staging directory used here.  Live
    metrics therefore use a cheap top-level ``scandir`` plus a bounded recent-file
    cache.  The authoritative recursive snapshot still runs after C-MOVE exits.
    """

    _RECENT_FILE_LIMIT = 128

    def __init__(self, directory: Path, baseline: Iterable[Path]):
        self.directory = directory
        self._baseline = set(baseline)
        self._known: set[Path] = set()
        self._active: set[Path] = set()
        self._sizes: dict[Path, int] = {}
        self._recent: deque[Path] = deque(maxlen=self._RECENT_FILE_LIMIT)
        self._total_bytes = 0

    @property
    def sample_interval_seconds(self) -> float:
        count = len(self._known)
        if count >= 20_000:
            return 5.0
        if count >= 5_000:
            return 2.0
        if count >= 500:
            return 1.0
        return 0.5

    def sample(self) -> tuple[int, int]:
        self._discover_new_files()
        candidates = set(self._active)
        candidates.update(self._recent)
        for path in candidates:
            previous = self._sizes.get(path)
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                self._forget(path)
                continue
            except OSError:
                continue
            if previous is None:
                self._total_bytes += size
            else:
                self._total_bytes += size - previous
                if size == previous:
                    self._active.discard(path)
                else:
                    self._active.add(path)
            self._sizes[path] = size
        return len(self._known), max(0, self._total_bytes)

    def _discover_new_files(self) -> None:
        try:
            with os.scandir(self.directory) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    path = Path(entry.path)
                    if path in self._baseline or path in self._known:
                        continue
                    self._known.add(path)
                    self._active.add(path)
                    self._recent.append(path)
        except OSError:
            return

    def _forget(self, path: Path) -> None:
        self._known.discard(path)
        self._active.discard(path)
        previous = self._sizes.pop(path, None)
        if previous is not None:
            self._total_bytes -= previous


_RECEIVER_DRAIN_INITIAL_WAIT_SECONDS = 1.0
_RECEIVER_DRAIN_QUIET_SECONDS = 0.5
_RECEIVER_DRAIN_MAX_SECONDS = 30.0
_RECEIVER_DRAIN_POLL_SECONDS = 0.1


def _wait_for_late_store_writes(
    directory: Path,
    baseline: set[Path],
    cancel_event: threading.Event,
    *,
    dcmdump: Path | None = None,
    environment: dict[str, str] | None = None,
) -> bool | None:
    """Wait briefly for a receiver child that outlives a broken C-MOVE socket."""

    started = time.monotonic()
    first_file_deadline = started + _RECEIVER_DRAIN_INITIAL_WAIT_SECONDS
    hard_deadline = started + _RECEIVER_DRAIN_MAX_SECONDS
    last_snapshot: tuple[tuple[str, int], ...] = ()
    quiet_since: float | None = None
    observed_file = False
    next_validation_at = 0.0

    while not cancel_event.is_set():
        snapshot_items: list[tuple[str, int]] = []
        for path in sorted(_files_in(directory) - baseline):
            try:
                snapshot_items.append((str(path), path.stat().st_size))
            except OSError:
                continue
        snapshot = tuple(snapshot_items)
        now = time.monotonic()
        if snapshot:
            observed_file = True
            if snapshot != last_snapshot:
                last_snapshot = snapshot
                quiet_since = now
            elif (
                quiet_since is not None
                and now - quiet_since >= _RECEIVER_DRAIN_QUIET_SECONDS
                and now >= next_validation_at
            ):
                files = [Path(path) for path, _size in snapshot]
                validation_errors = _validate_dicom_files(
                    files,
                    dcmdump=dcmdump,
                    environment=environment,
                    cancel_event=cancel_event,
                )
                if not validation_errors:
                    return True
                next_validation_at = now + _RECEIVER_DRAIN_QUIET_SECONDS
        elif now >= first_file_deadline:
            return None

        if now >= hard_deadline:
            return False if observed_file else None
        cancel_event.wait(_RECEIVER_DRAIN_POLL_SECONDS)
    return None


class DcmtkResolver:
    def __init__(self, project_root: str | Path | None = None):
        self.project_root = Path(project_root or Path.cwd()).resolve()

    def resolve(self, configured_dir: str = "") -> ToolPaths:
        for bin_dir in self._candidate_directories(configured_dir):
            result = self._from_directory(bin_dir)
            if result:
                return result

        movescu = shutil.which(self._tool_name("movescu"))
        storescp = shutil.which(self._tool_name("storescp"))
        if movescu and storescp:
            return self._probe(Path(movescu), Path(storescp))

        raise FileNotFoundError(
            "未找到 movescu 和 storescp。请在设置中选择 DCMTK bin 目录，"
            "或先运行对应系统的部署脚本。"
        )

    def _candidate_directories(self, configured_dir: str) -> Iterable[Path]:
        seen: set[Path] = set()
        candidates: list[Path] = []
        if configured_dir:
            configured = Path(configured_dir).expanduser()
            candidates.append(configured.parent if configured.is_file() else configured)

        portable_bin = portable_dcmtk_bin()
        if portable_bin is not None:
            candidates.append(portable_bin)

        runtime = self.project_root / ".runtime" / "dcmtk"
        platform_key = current_platform_key()
        platform_runtime = runtime / platform_key
        candidates.extend(
            [
                platform_runtime / "bin",
                platform_runtime,
                runtime / "bin",
                self.project_root / "dcmtk" / "bin",
            ]
        )
        if platform_runtime.exists():
            candidates[2:2] = platform_runtime.glob("*/bin")

        for candidate in candidates:
            try:
                normalized = candidate.resolve()
            except OSError:
                normalized = candidate
            if normalized not in seen:
                seen.add(normalized)
                yield normalized

    def _from_directory(self, directory: Path) -> ToolPaths | None:
        movescu = directory / self._tool_name("movescu")
        storescp = directory / self._tool_name("storescp")
        if movescu.is_file() and storescp.is_file():
            try:
                return self._probe(movescu, storescp)
            except (OSError, subprocess.SubprocessError):
                return None
        return None

    def _probe(self, movescu: Path, storescp: Path) -> ToolPaths:
        if os.name == "nt":
            require_amd64_pe(movescu, "DCMTK movescu")
            require_amd64_pe(storescp, "DCMTK storescp")
        version_text = _run_probe([str(movescu), "--version"])
        storescp_version = _run_probe([str(storescp), "--version"])
        help_text = _run_probe([str(storescp), "--help"], allow_nonzero=True)
        version = _parse_version(version_text) or _parse_version(storescp_version) or "未知"
        return ToolPaths(
            movescu,
            storescp,
            movescu.parent,
            version,
            help_text,
            dcmmkdir=self._optional_tool(movescu.parent, "dcmmkdir"),
            dcmdump=self._optional_tool(movescu.parent, "dcmdump"),
        )

    def _optional_tool(self, directory: Path, name: str) -> Path | None:
        path = directory / self._tool_name(name)
        return path if path.is_file() else None

    @staticmethod
    def _tool_name(name: str) -> str:
        return f"{name}.exe" if os.name == "nt" else name


def current_platform_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "windows-x86_64"
    if system == "darwin":
        return "macos-arm64" if machine in {"arm64", "aarch64"} else "macos-x86_64"
    if system == "linux":
        return "linux-x86_64" if machine in {"x86_64", "amd64"} else f"linux-{machine}"
    return f"{system}-{machine}"


def build_storescp_command(config: AppConfig, tools: ToolPaths, staging: Path) -> list[str]:
    command = [
        str(tools.storescp),
        "-v",
        "-aet",
        config.storage_ae_title.strip(" "),
        "+xa",
        "+uf",
        "-fe",
        ".dcm",
        "-od",
        str(staging),
    ]
    if tools.supports_fork:
        command.append("--fork")
    else:
        command.append("--single-process")
    command.append(str(config.storage_port))
    return command


def build_movescu_command(config: AppConfig, tools: ToolPaths, accession: str) -> list[str]:
    return [
        str(tools.movescu),
        "-v",
        "--no-port",
        "-to",
        "30",
        "-td",
        "300",
        "-aet",
        config.calling_ae_title.strip(" "),
        "-aec",
        config.pacs_ae_title.strip(" "),
        "-aem",
        config.storage_ae_title.strip(" "),
        config.pacs_server_ip,
        str(config.pacs_server_port),
        "-S",
        "-k",
        "QueryRetrieveLevel=STUDY",
        "-k",
        f"0008,0050={accession}",
    ]


def preflight(
    config: AppConfig,
    resolver: DcmtkResolver,
    *,
    check_port: bool = True,
) -> PreflightResult:
    errors = config.validate()
    checks: list[tuple[str, bool, str]] = []

    tools: ToolPaths | None = None
    try:
        tools = resolver.resolve(config.dcmtk_bin_dir)
        checks.append(("DCMTK 工具", True, f"已就绪，版本 {tools.version}"))
    except (
        ArchitectureError,
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        errors["dcmtk_bin_dir"] = str(exc)
        checks.append(("DCMTK 工具", False, str(exc)))

    if config.pdi_export_enabled and tools is not None:
        if tools.dcmmkdir is None:
            message = "PDI 导出缺少核心 DCMTK 工具：dcmmkdir"
            errors["dcmtk_bin_dir"] = message
            checks.append(("PDI 导出工具", False, message))
        else:
            checks.append(("PDI 导出工具", True, "DICOMDIR 工具已就绪"))
        if config.pdi_include_ohif_viewer:
            checks.append(
                ("PDI 网页阅片", True, "将使用本地 OHIF 直接读取原始 DICOM")
            )

    destination = Path(config.dicom_destination_folder).expanduser()
    try:
        destination.mkdir(parents=True, exist_ok=True)
        descriptor, probe_name = tempfile.mkstemp(
            prefix=".dcmget-write-test-", dir=destination
        )
        os.close(descriptor)
        Path(probe_name).unlink()
        checks.append(("保存目录", True, "目录可写"))
        _append_disk_space_check(
            checks,
            errors,
            "dicom_destination_folder",
            "保存目录空间",
            destination,
            config.minimum_free_space_bytes,
        )
    except OSError as exc:
        message = f"保存目录不可写：{exc}"
        errors["dicom_destination_folder"] = message
        checks.append(("保存目录", False, message))

    if config.pdi_export_enabled:
        pdi_root = (
            Path(config.pdi_output_folder).expanduser()
            if config.pdi_output_folder.strip()
            else destination / "PDI"
        )
        try:
            pdi_root.mkdir(parents=True, exist_ok=True)
            descriptor, probe_name = tempfile.mkstemp(
                prefix=".dcmget-pdi-write-test-", dir=pdi_root
            )
            os.close(descriptor)
            Path(probe_name).unlink()
            checks.append(("PDI 输出目录", True, f"目录可写：{pdi_root}"))
            _append_disk_space_check(
                checks,
                errors,
                "pdi_output_folder",
                "PDI 输出空间",
                pdi_root,
                config.minimum_free_space_bytes,
            )
        except OSError as exc:
            message = f"PDI 输出目录不可写：{exc}"
            errors["pdi_output_folder"] = message
            checks.append(("PDI 输出目录", False, message))

    if check_port and "storage_port" not in errors:
        available = is_port_available(config.storage_port)
        message = "端口可用" if available else f"端口 {config.storage_port} 已被占用"
        checks.append(("接收端口", available, message))
        if not available:
            errors["storage_port"] = message
    elif "storage_port" not in errors:
        checks.append(("接收端口", True, "将在任务获得运行机会时检查"))

    checks.append(
        (
            "PACS 配置",
            not any(key in errors for key in ("pacs_server_ip", "pacs_server_port", "pacs_ae_title")),
            f"{config.pacs_server_ip}:{config.pacs_server_port} / {config.pacs_ae_title}",
        )
    )
    return PreflightResult(tools, errors, checks)


def _append_disk_space_check(
    checks: list[tuple[str, bool, str]],
    errors: dict[str, str],
    field: str,
    label: str,
    path: Path,
    minimum_free_space_bytes: int,
) -> None:
    if minimum_free_space_bytes <= 0:
        checks.append((label, True, "未启用最低可用空间限制"))
        return
    try:
        free = shutil.disk_usage(path).free
    except OSError as exc:
        message = f"无法读取磁盘可用空间：{exc}"
        errors.setdefault(field, message)
        checks.append((label, False, message))
        return
    ok = free >= minimum_free_space_bytes
    message = (
        f"可用空间 {_format_bytes(free)}，最低保留 {_format_bytes(minimum_free_space_bytes)}"
    )
    checks.append((label, ok, message))
    if not ok:
        errors.setdefault(field, message)


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def _combine_retry_attempts(attempts: list[AccessionResult]) -> AccessionResult:
    if not attempts:
        raise ValueError("至少需要一次下载尝试")
    final = attempts[-1]
    if len(attempts) == 1:
        return replace(final, attempt_count=max(1, final.attempt_count))

    archived_files = list(
        dict.fromkeys(path for result in attempts for path in result.archived_files)
    )
    actual_accessions = sorted(
        {value for result in attempts for value in result.actual_accessions}
    )
    study_uids = sorted(
        {value for result in attempts for value in result.study_instance_uids}
    )
    verification_status = final.verification_status
    verification_message = final.verification_message
    verified_attempts = [result for result in attempts if result.local_verified_files]
    if any(
        result.verification_status == ResultVerificationStatus.MISMATCH
        for result in verified_attempts
    ):
        verification_status = ResultVerificationStatus.MISMATCH
        mismatch = next(
            result
            for result in verified_attempts
            if result.verification_status == ResultVerificationStatus.MISMATCH
        )
        verification_message = mismatch.verification_message
    elif verified_attempts and any(
        result.verification_status == ResultVerificationStatus.UNVERIFIABLE
        for result in verified_attempts
    ):
        verification_status = ResultVerificationStatus.UNVERIFIABLE
        verification_message = next(
            result.verification_message
            for result in verified_attempts
            if result.verification_status == ResultVerificationStatus.UNVERIFIABLE
        )
    elif verified_attempts:
        verification_status = ResultVerificationStatus.MATCHED
        verification_message = next(
            (
                result.verification_message
                for result in reversed(verified_attempts)
                if result.verification_message
            ),
            "本次接收文件核对通过",
        )
    duration = sum(result.duration_seconds for result in attempts)
    received_bytes = sum(result.received_bytes for result in attempts)
    conflict_preserved_count = sum(
        result.conflict_preserved_count for result in attempts
    )
    retained_file_count = max(
        len(archived_files) + conflict_preserved_count,
        *(result.file_count for result in attempts),
    )
    output_directory = next(
        (
            result.output_directory
            for result in reversed(attempts)
            if result.output_directory
        ),
        "",
    )
    status = final.status
    message = f"第 {len(attempts)} 次尝试完成；{final.message}"
    if retained_file_count and final.status in {
        AccessionStatus.FAILED,
        AccessionStatus.NO_DATA,
    }:
        status = AccessionStatus.PARTIAL
        message = (
            f"共尝试 {len(attempts)} 次，累计已保留 {retained_file_count} 个文件；"
            f"最后一次：{final.message}"
        )
    if conflict_preserved_count and status != AccessionStatus.CANCELLED:
        status = AccessionStatus.PARTIAL
        message += f"；累计 {conflict_preserved_count} 个冲突文件需人工核对"
    return replace(
        final,
        status=status,
        file_count=retained_file_count,
        duration_seconds=duration,
        message=message,
        output_directory=output_directory,
        received_bytes=received_bytes,
        speed_bytes_per_second=(received_bytes / duration if duration > 0 else 0.0),
        archived_files=archived_files or list(final.archived_files),
        new_file_count=sum(result.new_file_count for result in attempts),
        existing_skipped_count=sum(
            result.existing_skipped_count for result in attempts
        ),
        conflict_preserved_count=conflict_preserved_count,
        verification_status=verification_status,
        verification_message=verification_message,
        actual_accessions=actual_accessions,
        study_instance_uids=study_uids,
        series_instance_count=max(
            result.series_instance_count for result in attempts
        ),
        sop_instance_count=max(result.sop_instance_count for result in attempts),
        local_verified_files=max(
            result.local_verified_files for result in attempts
        ),
        attempt_count=sum(max(1, result.attempt_count) for result in attempts),
    )


_UNCONFIRMED_MOVE_SAFETY_PAUSE = (
    "C-MOVE 未返回最终响应，已保留完整文件，但无法确认全部实例已到齐；"
    "任务已安全暂停，请重试当前检查号"
)


def _with_unconfirmed_move_safety_pause(
    result: AccessionResult,
    *,
    retained_by_earlier_unconfirmed_attempt: bool = False,
) -> AccessionResult:
    if (
        result.status != AccessionStatus.PARTIAL
        or result.file_count <= 0
        or not (
            result.transient_failure
            or retained_by_earlier_unconfirmed_attempt
        )
        or result.safety_pause_reason
    ):
        return result
    return replace(
        result,
        message=f"{result.message}；{_UNCONFIRMED_MOVE_SAFETY_PAUSE}",
        safety_pause_reason=_UNCONFIRMED_MOVE_SAFETY_PAUSE,
    )


def is_port_available(port: int, host: str = "0.0.0.0") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


class DownloadRunner:
    def __init__(
        self,
        config: AppConfig,
        tools: ToolPaths,
        log_callback: LogCallback | None = None,
        state_callback: StateCallback | None = None,
        progress_callback: ProgressCallback | None = None,
        ready_callback: ReadyCallback | None = None,
        process_callback: ProcessCallback | None = None,
        audit_callback: AuditCallback | None = None,
        log_file_name: str = "dcmget.log",
        log_directory: str | Path | None = None,
        fallback_log_directory: str | Path | None = None,
    ):
        self.config = config
        self.tools = tools
        self.log_callback = log_callback or (lambda _source, _message, _level: None)
        self.state_callback = state_callback or (lambda _state: None)
        self.progress_callback = progress_callback or (lambda _index, _total, _result: None)
        self.ready_callback = ready_callback or (lambda: None)
        self._move_started_notified = False
        self.process_callback = process_callback or (
            lambda _kind, _pid, _executable, _active: None
        )
        self.audit_callback = audit_callback or (
            lambda _result, _observations: None
        )
        if Path(log_file_name).name != log_file_name or not log_file_name:
            raise ValueError("日志文件名无效")
        self._log_file_name = log_file_name
        self._log_directory = (
            Path(log_directory).expanduser() if log_directory is not None else None
        )
        self._fallback_log_directory = (
            Path(fallback_log_directory).expanduser()
            if fallback_log_directory is not None
            else ensure_application_state_dir() / "logs"
        )
        self._active_log_directory: Path | None = None
        self._log_fallback_reason = ""
        self._cancel = threading.Event()
        self._pause_condition = threading.Condition()
        self._pause_requested = False
        self._process_lock = threading.Lock()
        self._termination_condition = threading.Condition()
        self._terminating_processes: set[int] = set()
        self._cancel_cleanup_lock = threading.Lock()
        self._cancel_cleanup_thread: threading.Thread | None = None
        self._diagnostic_lock = threading.Lock()
        self._current_process: subprocess.Popen[str] | None = None
        self._storescp_process: subprocess.Popen[str] | None = None
        self._receiver_lease_guard = threading.Lock()
        self._receiver_lease: FileLock | None = None
        self._storescp_abort_count = 0
        self._logger = self._build_file_logger()
        if self._log_fallback_reason:
            self._emit("应用", self._log_fallback_reason, "warning")
        self._anonymizer = (
            DicomAnonymizer(config.anonymization_profile)
            if config.anonymization_enabled
            else None
        )

    def request_cancel(self) -> None:
        self._request_cancel(include_receiver=True)

    def request_cancel_current_move(self) -> None:
        """Cancel only the active C-MOVE while keeping a shared receiver alive."""

        self._request_cancel(include_receiver=False)

    def _request_cancel(self, *, include_receiver: bool) -> None:
        self._cancel.set()
        with self._pause_condition:
            self._pause_condition.notify_all()
        self._emit("应用", "正在停止当前任务…", "warning")
        with self._cancel_cleanup_lock:
            if self._cancel_cleanup_thread is not None:
                return
            cleanup = threading.Thread(
                target=self._cancel_running_processes,
                kwargs={"include_receiver": include_receiver},
                name="dcmtk-cancel-cleanup",
                daemon=True,
            )
            self._cancel_cleanup_thread = cleanup
            cleanup.start()

    def _cancel_running_processes(self, *, include_receiver: bool = True) -> None:
        with self._process_lock:
            processes = (
                (self._current_process, self._storescp_process)
                if include_receiver
                else (self._current_process,)
            )
        seen: set[int] = set()
        for process in processes:
            if process is None or id(process) in seen:
                continue
            seen.add(id(process))
            self._terminate_process_safely(process)

    def run_accession(
        self,
        accession: str,
        staging: Path,
        receiver_process: object,
    ) -> AccessionResult:
        """Run one C-MOVE through an already-running shared receiver.

        The caller owns the receiver process and staging directory.  This
        runner only owns its ``movescu`` child, so cancelling one task cannot
        interrupt downloads queued for other tasks.
        """

        if not staging.is_dir():
            raise RuntimeError(f"共享暂存目录不存在：{staging}")
        receiver_poll = getattr(receiver_process, "poll", None)
        if not callable(receiver_poll):
            raise RuntimeError("共享 DICOM 接收器不支持存活检查")
        receiver_exit_code = receiver_poll()
        if receiver_exit_code is not None:
            raise RuntimeError(
                f"DICOM 接收器已退出，退出码 {receiver_exit_code}"
            )
        with self._process_lock:
            if self._storescp_process is not None:
                raise RuntimeError("当前执行器已连接到另一个 storescp")
            self._storescp_process = receiver_process
        try:
            result = self._download_one(accession, staging, 1, 1)
            original_safety_pause = result.safety_pause_reason
            result = _with_unconfirmed_move_safety_pause(result)
            if result.safety_pause_reason and not original_safety_pause:
                self._emit(
                    "movescu",
                    f"{accession}：{result.safety_pause_reason}",
                    "error",
                )
            elif result.status == AccessionStatus.FAILED and result.transient_failure:
                self._emit("movescu", f"{accession}：{result.message}", "error")
            return result
        finally:
            with self._process_lock:
                if self._storescp_process is receiver_process:
                    self._storescp_process = None
            self._close_file_logger()

    def _terminate_process_safely(self, process: subprocess.Popen[str]) -> None:
        """Terminate one child once when cancellation races worker cleanup."""

        identity = id(process)
        with self._termination_condition:
            if identity in self._terminating_processes:
                while identity in self._terminating_processes:
                    self._termination_condition.wait(timeout=0.1)
                return
            self._terminating_processes.add(identity)
        try:
            _terminate_process(process)
        finally:
            with self._termination_condition:
                self._terminating_processes.discard(identity)
                self._termination_condition.notify_all()

    def request_pause(self) -> None:
        with self._pause_condition:
            if self._cancel.is_set() or self._pause_requested:
                return
            self._pause_requested = True
        self.state_callback("pause_pending")
        self._emit("应用", "将在当前检查号完成后暂停", "warning")

    def request_resume(self) -> None:
        with self._pause_condition:
            if not self._pause_requested:
                return
            self._pause_requested = False
            self._pause_condition.notify_all()
        self.state_callback("downloading")
        self._emit("应用", "任务已继续", "info")

    def run(self, accessions: Iterable[str]) -> BatchSummary:
        values = list(accessions)
        staging = staging_directory_root(self.config) / datetime.now().strftime(
            "%Y%m%d-%H%M%S-%f"
        )
        staging.mkdir(parents=True, exist_ok=False)
        summary = BatchSummary(staging_directory=str(staging))

        try:
            self.state_callback("starting_receiver")
            if self._anonymizer:
                self._emit(
                    "匿名",
                    f"已启用 {self.config.anonymization_profile} 元数据匿名方案",
                    "info",
                )
            self._start_storescp(staging)
            self.state_callback("downloading")
            consecutive_transient_failures = 0
            for index, accession in enumerate(values, 1):
                if self._cancel.is_set():
                    summary.cancelled = True
                    self._append_cancelled(summary, values[index - 1 :])
                    break
                if not self._wait_if_paused():
                    summary.cancelled = True
                    self._append_cancelled(summary, values[index - 1 :])
                    break

                disk_issue = self._disk_space_issue(staging)
                if disk_issue:
                    summary.interrupted_reason = disk_issue
                    self.state_callback("safety_paused")
                    self._emit("磁盘", disk_issue, "error")
                    break

                result = self._download_accession_with_retry(
                    accession, staging, index, len(values)
                )
                summary.results.append(result)
                self.progress_callback(index, len(values), result)

                if result.status == AccessionStatus.CANCELLED:
                    summary.cancelled = True
                    self._append_cancelled(summary, values[index:])
                    break
                if result.safety_pause_reason:
                    summary.interrupted_reason = result.safety_pause_reason
                    self.state_callback("safety_paused")
                    break
                if result.transient_failure:
                    consecutive_transient_failures += 1
                else:
                    consecutive_transient_failures = 0
                if (
                    consecutive_transient_failures
                    >= self.config.circuit_breaker_failures
                ):
                    summary.interrupted_reason = (
                        f"连续 {consecutive_transient_failures} 个检查号发生网络或关联故障，"
                        "批次已安全暂停；请先检查 PACS、网络和 AE 配置后重试"
                    )
                    self.state_callback("safety_paused")
                    self._emit("PACS", summary.interrupted_reason, "error")
                    break
        finally:
            try:
                self.state_callback("stopping")
            finally:
                try:
                    self._stop_storescp()
                finally:
                    try:
                        self._cleanup_staging(staging)
                    finally:
                        self._close_file_logger()

        if self._cancel.is_set():
            summary.cancelled = True
        if summary.cancelled:
            self.state_callback("cancelled")
        elif summary.exit_code == 2:
            self.state_callback("partial")
        else:
            self.state_callback("completed")
        return summary

    def _download_accession_with_retry(
        self,
        accession: str,
        staging: Path,
        index: int,
        total: int,
    ) -> AccessionResult:
        attempts: list[AccessionResult] = []
        maximum_attempts = 1 + self.config.auto_retry_attempts
        for attempt in range(1, maximum_attempts + 1):
            result = self._download_one(accession, staging, index, total)
            attempts.append(result)
            if not result.transient_failure or self._cancel.is_set():
                break
            if attempt >= maximum_attempts:
                break
            delay = self.config.auto_retry_backoff_seconds * attempt
            self._emit(
                "重试",
                f"{accession} 发生瞬时网络或关联故障，{delay} 秒后进行第 {attempt + 1} 次尝试",
                "warning",
            )
            if self._cancel.wait(delay):
                break
        combined = _combine_retry_attempts(attempts)
        final = attempts[-1]
        original_safety_pause = combined.safety_pause_reason
        retained_by_earlier_unconfirmed_attempt = (
            final.status in {AccessionStatus.FAILED, AccessionStatus.NO_DATA}
            and any(
                result.file_count > 0 and result.transient_failure
                for result in attempts[:-1]
            )
        )
        combined = _with_unconfirmed_move_safety_pause(
            combined,
            retained_by_earlier_unconfirmed_attempt=(
                retained_by_earlier_unconfirmed_attempt
            ),
        )
        if combined.safety_pause_reason and not original_safety_pause:
            self._emit(
                "movescu",
                f"{accession}：{combined.safety_pause_reason}",
                "error",
            )
        elif combined.status == AccessionStatus.FAILED and final.transient_failure:
            self._emit("movescu", f"{accession}：{combined.message}", "error")
        elif len(attempts) > 1 and combined.status != final.status:
            self._emit("movescu", f"{accession}：{combined.message}", "warning")
        return combined

    def _disk_space_issue(self, staging: Path) -> str:
        minimum = self.config.minimum_free_space_bytes
        if minimum <= 0:
            return ""
        paths = {
            Path(self.config.dicom_destination_folder).expanduser(),
            staging,
        }
        checked_devices: set[int] = set()
        for path in paths:
            try:
                existing = path if path.exists() else path.parent
                device = existing.stat().st_dev
                if device in checked_devices:
                    continue
                checked_devices.add(device)
                free = shutil.disk_usage(existing).free
            except OSError as exc:
                return f"无法检查磁盘可用空间：{path}（{exc}）"
            if free < minimum:
                return (
                    f"磁盘可用空间仅 {_format_bytes(free)}，低于最低保留值 "
                    f"{_format_bytes(minimum)}；批次已安全暂停"
                )
        return ""

    def _wait_if_paused(self) -> bool:
        with self._pause_condition:
            if not self._pause_requested:
                if self._cancel.is_set():
                    return False
                return True

        self.state_callback("paused")
        self._emit("应用", "任务已暂停，DICOM 接收器保持监听", "warning")
        with self._pause_condition:
            while self._pause_requested and not self._cancel.is_set():
                receiver = self._storescp_process
                if receiver is not None and receiver.poll() is not None:
                    raise RuntimeError(
                        f"暂停期间 storescp 意外退出，退出码 {receiver.returncode}"
                    )
                self._pause_condition.wait(timeout=0.2)
            if self._cancel.is_set():
                return False
            return True

    def _start_storescp(self, staging: Path) -> None:
        self._acquire_receiver_lease()
        try:
            if _port_is_listening(self.config.storage_port):
                raise RuntimeError(
                    f"接收端口 {self.config.storage_port} 已被其他程序占用；"
                    "请关闭占用程序或为此 DcmGet 实例配置不同的监听端口"
                )
            command = build_storescp_command(self.config, self.tools, staging)
            mode = (
                "多进程并发（--fork）"
                if self.tools.supports_fork
                else "单进程兼容模式"
            )
            self._emit("storescp", f"接收模式：{mode}", "info")
            self._emit("storescp", f"启动接收器：{_display_command(command)}", "info")
            process = self._popen(command)
            with self._process_lock:
                self._storescp_process = process
            self._notify_process(
                "storescp", getattr(process, "pid", 0), command[0], True
            )
            self._start_reader(process, "storescp")

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self._cancel.is_set():
                    return
                if process.poll() is not None:
                    raise RuntimeError(
                        f"storescp 启动失败，退出码 {process.returncode}"
                    )
                if _port_is_listening(self.config.storage_port):
                    stable_until = min(deadline, time.monotonic() + 0.3)
                    while time.monotonic() < stable_until:
                        if self._cancel.is_set():
                            return
                        time.sleep(0.05)
                        if process.poll() is not None:
                            raise RuntimeError(
                                "storescp 端口就绪后意外退出，"
                                f"退出码 {process.returncode}"
                            )
                    if _port_is_listening(self.config.storage_port):
                        self._emit(
                            "storescp",
                            f"已监听端口 {self.config.storage_port}",
                            "success",
                        )
                        return
                time.sleep(0.1)
            raise TimeoutError(f"storescp 未能在端口 {self.config.storage_port} 就绪")
        except BaseException:
            self._stop_storescp()
            raise

    def _acquire_receiver_lease(self) -> None:
        with self._receiver_lease_guard:
            if self._receiver_lease is not None:
                raise RuntimeError("当前执行器已经持有 DICOM 接收端口")
            lease = FileLock(
                str(
                    _receiver_port_lock_path(
                        _RECEIVER_BIND_ADDRESS,
                        self.config.storage_port,
                    )
                )
            )
            try:
                lease.acquire(timeout=0)
            except Timeout as exc:
                raise RuntimeError(
                    f"接收端口 {self.config.storage_port} 已被另一个 DcmGet 实例占用；"
                    "请在设置中为每个实例配置不同的监听端口"
                ) from exc
            self._receiver_lease = lease

    def _release_receiver_lease(self) -> None:
        with self._receiver_lease_guard:
            lease = self._receiver_lease
            self._receiver_lease = None
        if lease is not None and lease.is_locked:
            lease.release()

    def _download_one(
        self, accession: str, staging: Path, index: int, total: int
    ) -> AccessionResult:
        before = _files_in(staging)
        live_files = _LiveStagingTracker(staging, before)
        with self._diagnostic_lock:
            aborts_before = self._storescp_abort_count
        command = build_movescu_command(self.config, self.tools, accession)

        started_process = self._start_movescu_process(command)
        if started_process is None:
            return AccessionResult(
                accession,
                AccessionStatus.CANCELLED,
                message="用户已取消",
            )
        process, started = started_process
        diagnostics = _MoveDiagnostics()
        reader: threading.Thread | None = None
        last_received_bytes = 0
        last_sample_at = started
        receiver_exit_code: int | None = None
        safety_pause_reason = ""
        try:
            self._emit("movescu", f"开始检查号 {accession}", "info")
            self._emit("movescu", _display_command(command), "debug")
            reader = self._start_reader(process, "movescu", diagnostics)
            while process.poll() is None:
                if self._cancel.is_set():
                    self._terminate_process_safely(process)
                    break
                receiver = self._storescp_process
                if receiver is not None:
                    receiver_exit_code = receiver.poll()
                    if receiver_exit_code is not None:
                        self._emit(
                            "接收器",
                            f"接收器意外退出，退出码 {receiver_exit_code}",
                            "error",
                        )
                        self._terminate_process_safely(process)
                        break
                now = time.monotonic()
                if now - last_sample_at >= live_files.sample_interval_seconds:
                    received, received_bytes = live_files.sample()
                    sample_seconds = now - last_sample_at
                    sample_speed = (
                        max(0, received_bytes - last_received_bytes) / sample_seconds
                        if sample_seconds > 0
                        else 0.0
                    )
                    last_received_bytes = received_bytes
                    last_sample_at = now
                    self.progress_callback(
                        index,
                        total,
                        AccessionResult(
                            accession,
                            AccessionStatus.DOWNLOADING,
                            file_count=received,
                            duration_seconds=now - started,
                            message="正在接收 DICOM 文件",
                            received_bytes=received_bytes,
                            speed_bytes_per_second=sample_speed,
                        ),
                    )
                    safety_pause_reason = self._disk_space_issue(staging)
                    if safety_pause_reason:
                        self._emit("磁盘", safety_pause_reason, "error")
                        self._terminate_process_safely(process)
                        break
                time.sleep(0.1)
            return_code = process.wait()
            transfer_finished = time.monotonic()
        finally:
            try:
                if process.poll() is None:
                    self._terminate_process_safely(process)
            finally:
                try:
                    if reader is not None:
                        reader.join()
                finally:
                    with self._process_lock:
                        if self._current_process is process:
                            self._current_process = None
                    self._notify_process(
                        "movescu", getattr(process, "pid", 0), command[0], False
                    )

        if (
            not self._cancel.is_set()
            and not _move_has_final_response(diagnostics)
            and (
                diagnostics.association_accepted
                or diagnostics.pending_responses
            )
        ):
            self._emit(
                "storescp",
                "C-MOVE 连接已结束，正在等待接收中的文件写入完成",
                "info",
            )
            drain_result = _wait_for_late_store_writes(
                staging,
                before,
                self._cancel,
                dcmdump=self.tools.dcmdump,
                environment=_dcmtk_environment(self.tools),
            )
            transfer_finished = time.monotonic()
            if drain_result is False and not safety_pause_reason:
                safety_pause_reason = (
                    "接收文件在等待期内仍未完整写入，任务已安全暂停；"
                    "请检查 PACS、网络和接收器日志后重试当前检查号"
                )
                self._emit("storescp", safety_pause_reason, "error")

        all_files = _files_in(staging)
        new_files = all_files - before
        # Each 2.9 instance owns one receiver and runs only one C-MOVE at a
        # time.  Therefore every file created in this move's receive window
        # belongs to the active request.  Do not reject useful PACS data just
        # because AccessionNumber is absent, malformed or different.
        candidate_files = sorted(new_files)
        received_bytes = _total_file_size(candidate_files)
        transfer_seconds = max(0.0, transfer_finished - started)
        average_speed = (
            received_bytes / transfer_seconds if transfer_seconds > 0 else 0.0
        )
        destination_root = Path(self.config.dicom_destination_folder).expanduser()

        def record_archive_error(_source: Path, message: str) -> None:
            reason = message.strip() or "未知错误"
            self._emit(
                "匿名" if self._anonymizer else "应用",
                f"文件归档失败：{reason}",
                "error",
            )

        archive_stats = ArchiveStats()
        moved, rejected = _archive_dicom_files(
            candidate_files,
            destination_root,
            self.config.directory_template,
            accession,
            anonymizer=self._anonymizer,
            error_callback=record_archive_error,
            dcmdump=self.tools.dcmdump,
            dcmtk_environment=_dcmtk_environment(self.tools),
            cancel_event=self._cancel,
            route_accession=accession,
            stats=archive_stats,
        )
        accepted_file_count = (
            archive_stats.new_file_count
            + archive_stats.existing_skipped_count
            + archive_stats.conflict_preserved_count
        )
        if accepted_file_count:
            verification_status, verification_message = reconcile_archive_stats(
                accession, archive_stats
            )
        else:
            verification_status = ResultVerificationStatus.UNVERIFIABLE
            verification_message = "本次未收到可核对的 DICOM 文件"
        rejected_detail = (
            f"{len(rejected)} 个匿名或归档失败文件留在私有暂存目录（原因见日志）"
            if self._anonymizer
            else f"{len(rejected)} 个异常文件留在暂存目录"
        )
        output_directory = _common_output_directory(
            [*moved, *archive_stats.conflict_files], destination_root
        )
        duration = time.monotonic() - started
        with self._diagnostic_lock:
            receiver_aborts = self._storescp_abort_count - aborts_before
        move_problem = _move_has_problem(diagnostics)
        move_detail = _move_diagnostic_summary(diagnostics)
        association_failure = _association_failure_summary(diagnostics, self.config)
        if association_failure:
            move_problem = True
            move_detail = "；".join(
                detail for detail in (association_failure, move_detail) if detail
            )
        archive_mismatch = _move_archive_mismatch(diagnostics, accepted_file_count)
        if archive_mismatch:
            move_problem = True
            move_detail = "；".join(
                detail for detail in (move_detail, archive_mismatch) if detail
            )
        if receiver_aborts:
            self._emit(
                "storescp",
                (
                    f"当前 C-MOVE 期间记录到 {receiver_aborts} 个未关联的接收连接中止；"
                    "该信息仅作为接收器警告，检查号结果以 C-MOVE 最终状态和文件完整性为准"
                ),
                "warning",
            )

        if self._cancel.is_set():
            status = AccessionStatus.CANCELLED
            message = "用户已取消"
        elif safety_pause_reason:
            status = (
                AccessionStatus.PARTIAL
                if accepted_file_count
                else AccessionStatus.FAILED
            )
            details = []
            if accepted_file_count:
                details.append(
                    _archive_result_message(accepted_file_count, archive_stats)
                )
            if move_detail:
                details.append(move_detail)
            if rejected:
                details.append(rejected_detail)
            details.append(safety_pause_reason)
            message = "；".join(details)
        elif receiver_exit_code is not None:
            status = (
                AccessionStatus.PARTIAL
                if accepted_file_count
                else AccessionStatus.FAILED
            )
            message = f"storescp 意外退出（退出码 {receiver_exit_code}）"
            if accepted_file_count:
                message += f"，已保留 {accepted_file_count} 个完整文件"
            if rejected:
                message += f"，{rejected_detail}"
        elif return_code == 0 and accepted_file_count and (
            move_problem or rejected or archive_stats.conflict_preserved_count
        ):
            status = AccessionStatus.PARTIAL
            details = []
            if move_problem:
                details.append(move_detail or "C-MOVE 未正常完成")
            if rejected:
                details.append(rejected_detail)
            if archive_stats.conflict_preserved_count:
                details.append(
                    f"{archive_stats.conflict_preserved_count} 个冲突文件需人工核对"
                )
            message = (
                _archive_result_message(accepted_file_count, archive_stats)
                + "，但"
                + "；".join(details)
            )
        elif return_code == 0 and accepted_file_count:
            status = AccessionStatus.COMPLETED
            message = _archive_result_message(accepted_file_count, archive_stats)
        elif return_code == 0 and (move_problem or rejected):
            status = AccessionStatus.FAILED
            details = []
            if move_problem:
                details.append(move_detail or "C-MOVE 未正常完成")
            if rejected:
                details.append(rejected_detail)
            message = "；".join(details) + "；本次未归档新文件"
        elif return_code == 0:
            status = AccessionStatus.NO_DATA
            message = "C-MOVE 完成，但未收到文件"
        elif accepted_file_count:
            status = AccessionStatus.PARTIAL
            message = (
                f"movescu 退出码 {return_code}，"
                f"已保留 {accepted_file_count} 个文件"
            )
            if move_detail:
                message += f"；{move_detail}"
            if rejected:
                message += f"，{rejected_detail}"
        else:
            status = AccessionStatus.FAILED
            message = f"movescu 退出码 {return_code}，本次未归档新文件"
            if move_detail:
                message += f"；{move_detail}"
            if rejected:
                message += f"，{rejected_detail}"

        if (
            accepted_file_count
            and verification_status != ResultVerificationStatus.MATCHED
        ):
            message += f"；归属核对：{verification_message}"

        transient_failure = bool(
            not safety_pause_reason
            and receiver_exit_code is None
            and not _move_has_final_response(diagnostics)
            and (
                return_code != 0
                or diagnostics.association_accepted
                or diagnostics.pending_responses > 0
            )
        )

        warning_statuses = {
            AccessionStatus.NO_DATA,
            AccessionStatus.PARTIAL,
            AccessionStatus.CANCELLED,
        }
        if status == AccessionStatus.COMPLETED:
            level = "success"
        elif transient_failure or status in warning_statuses:
            level = "warning"
        else:
            level = "error"
        self._emit("movescu", f"{accession}：{message}", level)
        result = AccessionResult(
            accession=accession,
            status=status,
            file_count=accepted_file_count,
            duration_seconds=duration,
            message=message,
            output_directory=str(output_directory) if accepted_file_count else "",
            received_bytes=received_bytes,
            speed_bytes_per_second=average_speed,
            archived_files=[str(path) for path in moved],
            new_file_count=archive_stats.new_file_count,
            existing_skipped_count=archive_stats.existing_skipped_count,
            conflict_preserved_count=archive_stats.conflict_preserved_count,
            verification_status=verification_status,
            verification_message=verification_message,
            actual_accessions=sorted(archive_stats.source_accessions),
            study_instance_uids=sorted(archive_stats.study_instance_uids),
            series_instance_count=len(archive_stats.series_instance_uids),
            sop_instance_count=len(archive_stats.sop_instance_uids),
            local_verified_files=accepted_file_count,
            pacs_completed_suboperations=diagnostics.completed_suboperations,
            move_return_code=return_code,
            move_dimse_status=diagnostics.dimse_status_code,
            transient_failure=transient_failure,
            safety_pause_reason=safety_pause_reason,
        )
        try:
            self.audit_callback(result, tuple(archive_stats.observations))
        except Exception as exc:
            self._emit("验收台账", f"无法写入检查号 {accession} 的验收记录：{exc}", "error")
        return result

    def _start_movescu_process(
        self, command: list[str]
    ) -> tuple[subprocess.Popen[str], float] | None:
        while True:
            with self._pause_condition:
                if self._cancel.is_set():
                    return None
                if not self._pause_requested:
                    started = time.monotonic()
                    process = self._popen(command)
                    with self._process_lock:
                        self._current_process = process
                    self._notify_process(
                        "movescu", getattr(process, "pid", 0), command[0], True
                    )
                    if not self._move_started_notified:
                        try:
                            self.ready_callback()
                        except Exception:
                            self._terminate_process_safely(process)
                            with self._process_lock:
                                if self._current_process is process:
                                    self._current_process = None
                            self._notify_process(
                                "movescu",
                                getattr(process, "pid", 0),
                                command[0],
                                False,
                            )
                            raise
                        self._move_started_notified = True
                    return process, started
            if not self._wait_if_paused():
                return None

    def _popen(self, command: list[str]) -> subprocess.Popen[str]:
        kwargs: dict[str, object] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": locale.getpreferredencoding(False) or "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "env": _dcmtk_environment(self.tools),
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]
        if os.name != "nt":
            # Every POSIX child above starts a new session, so its PID is also
            # the process-group ID.  Keep that identity even if the group
            # leader exits before its forked storescp children.
            setattr(process, "_dcmget_process_group", process.pid)
        return process

    def _start_reader(
        self,
        process: subprocess.Popen[str],
        source: str,
        diagnostics: _MoveDiagnostics | None = None,
    ) -> threading.Thread:
        def read_output() -> None:
            if process.stdout is None:
                return
            for line in process.stdout:
                text = line.rstrip()
                if text:
                    with self._diagnostic_lock:
                        if source == "storescp" and "Association Aborted" in text:
                            self._storescp_abort_count += 1
                        elif source == "movescu" and diagnostics is not None:
                            _record_move_diagnostic(diagnostics, text)
                    level = (
                        "warning"
                        if source == "movescu" and text.startswith(("E:", "F:"))
                        else "error"
                        if text.startswith(("E:", "F:"))
                        else "warning"
                        if "Association Aborted" in text
                        else "info"
                    )
                    self._emit(
                        source,
                        text,
                        level,
                        file_level=(
                            "error" if text.startswith(("E:", "F:")) else None
                        ),
                    )

        thread = threading.Thread(target=read_output, name=f"{source}-output", daemon=True)
        thread.start()
        return thread

    def _stop_storescp(self) -> None:
        with self._process_lock:
            process = self._storescp_process
            self._storescp_process = None
        try:
            if process:
                # ``storescp --fork`` children can outlive an exited group leader.
                # Always ask the process-group cleanup helper to drain the group.
                self._terminate_process_safely(process)
                self._emit("storescp", "接收器已停止", "info")
                self._notify_process(
                    "storescp",
                    getattr(process, "pid", 0),
                    str(self.tools.storescp),
                    False,
                )
        finally:
            self._release_receiver_lease()

    def _cleanup_staging(self, staging: Path) -> None:
        remaining = _files_in(staging)
        if remaining:
            self._emit("应用", f"暂存目录仍有 {len(remaining)} 个文件：{staging}", "warning")
            return
        try:
            staging.rmdir()
            staging.parent.rmdir()
        except OSError:
            pass

    def _append_cancelled(self, summary: BatchSummary, accessions: Iterable[str]) -> None:
        for accession in accessions:
            summary.results.append(
                AccessionResult(accession, AccessionStatus.CANCELLED, message="任务尚未开始")
            )

    def _notify_process(
        self,
        kind: str,
        pid: int,
        executable: str,
        active: bool,
    ) -> None:
        if not isinstance(pid, int) or pid <= 0:
            return
        try:
            self.process_callback(kind, pid, executable, active)
        except Exception as exc:
            self._emit("恢复", f"无法更新 {kind} 进程恢复信息：{exc}", "error")

    def _emit(
        self,
        source: str,
        message: str,
        level: str,
        *,
        file_level: str | None = None,
    ) -> None:
        self.log_callback(source, message, level)
        log_level = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "success": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
        }.get(file_level or level, logging.INFO)
        self._logger.log(log_level, "[%s] %s", source, message)

    def _build_file_logger(self) -> logging.Logger:
        primary = self._log_directory or log_directory(self.config)
        try:
            return self._build_file_logger_in(primary)
        except OSError as exc:
            fallback = self._fallback_log_directory
            if fallback == primary:
                raise
            logger = self._build_file_logger_in(fallback)
            self._log_fallback_reason = (
                f"任务日志目录不可写：{primary}（{exc}）；"
                f"已回退到实例日志目录：{fallback}"
            )
            return logger

    def _build_file_logger_in(self, log_dir: Path) -> logging.Logger:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"dcmget.runner.{id(self)}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = PrivateRotatingFileHandler(
            log_dir / self._log_file_name,
            maxBytes=self.config.max_log_file_size_bytes,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        self._active_log_directory = log_dir
        return logger

    @property
    def active_log_directory(self) -> Path:
        if self._active_log_directory is None:
            raise RuntimeError("任务日志尚未初始化")
        return self._active_log_directory

    @property
    def used_log_fallback(self) -> bool:
        return bool(self._log_fallback_reason)

    def _close_file_logger(self) -> None:
        for handler in list(self._logger.handlers):
            handler.close()
            self._logger.removeHandler(handler)


def safe_accession_dir(accession: str) -> str:
    return _safe_path_component(accession, "accession")[:120]


def staging_directory_root(config: AppConfig) -> Path:
    if config.anonymization_enabled:
        return ensure_application_state_dir() / "staging"
    return (
        Path(config.dicom_destination_folder).expanduser().resolve()
        / ".dcmget-staging"
    )


def log_directory(config: AppConfig) -> Path:
    if config.anonymization_enabled:
        return ensure_application_state_dir() / "logs"
    return (
        Path(config.dicom_destination_folder).expanduser().resolve()
        / "_DcmGetLogs"
    )


def _run_probe(command: list[str], allow_nonzero: bool = False) -> str:
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": locale.getpreferredencoding(False) or "utf-8",
        "errors": "replace",
        "timeout": 10,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(command, **kwargs)  # type: ignore[arg-type]
    if result.returncode and not allow_nonzero:
        raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


def _parse_version(text: str) -> str:
    match = re.search(r"\bv(\d+\.\d+(?:\.\d+)?)\b", text)
    return match.group(1) if match else ""


def _port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _receiver_port_lock_path(bind_address: str, port: int) -> Path:
    identity = f"{bind_address.strip().casefold()}:{int(port)}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    directory = ensure_application_state_dir() / "receiver-port-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory / f"{digest}.lock"


def _files_in(directory: Path) -> set[Path]:
    if not directory.exists():
        return set()
    return {path for path in directory.rglob("*") if path.is_file()}


def _total_file_size(files: Iterable[Path]) -> int:
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _archive_result_message(file_count: int, stats: ArchiveStats) -> str:
    message = f"收到 {file_count} 个文件"
    if not (stats.existing_skipped_count or stats.conflict_preserved_count):
        return message
    return (
        f"{message}（新增 {stats.new_file_count}、"
        f"已存在跳过 {stats.existing_skipped_count}、"
        f"冲突保留 {stats.conflict_preserved_count}）"
    )


def reconcile_archive_stats(
    requested_accession: str,
    stats: ArchiveStats,
) -> tuple[ResultVerificationStatus, str]:
    """Classify returned metadata without rejecting otherwise valid DICOM."""

    requested = requested_accession.strip()
    observed = sorted(value for value in stats.source_accessions if value)
    mismatched = [value for value in observed if value != requested]
    if mismatched:
        preview = "、".join(mismatched[:3])
        suffix = "…" if len(mismatched) > 3 else ""
        return (
            ResultVerificationStatus.MISMATCH,
            f"返回检查号与请求不一致：{preview}{suffix}；文件已保留，需人工核对",
        )
    if stats.missing_accession_count or not observed:
        return (
            ResultVerificationStatus.UNVERIFIABLE,
            f"{stats.missing_accession_count or len(stats.sop_instance_uids)} 个文件缺少可核对的检查号；文件已保留",
        )
    return ResultVerificationStatus.MATCHED, "返回检查号与请求一致"


def _archive_dicom_files(
    files: Iterable[Path],
    destination_root: Path,
    directory_template: str,
    fallback_accession: str,
    anonymizer: DicomAnonymizer | None = None,
    error_callback: ArchiveErrorCallback | None = None,
    dcmdump: Path | None = None,
    dcmtk_environment: dict[str, str] | None = None,
    cancel_event: threading.Event | None = None,
    route_accession: str | None = None,
    stats: ArchiveStats | None = None,
) -> tuple[list[Path], list[Path]]:
    from pydicom import dcmread
    from pydicom.uid import UID

    moved: list[Path] = []
    rejected: list[Path] = []
    archive_stats = stats if stats is not None else ArchiveStats()
    routing_accession = str(route_accession or "").strip()
    values = list(files)
    if not values:
        return moved, rejected
    validation_errors = _validate_dicom_files(
        values,
        dcmdump=dcmdump,
        environment=dcmtk_environment,
        cancel_event=cancel_event,
    )
    for source in values:
        if cancel_event is not None and cancel_event.is_set():
            break
        temporary: Path | None = None
        source_accession = ""
        source_study_uid = ""
        source_series_uid = ""
        source_sop_uid = ""
        metadata = {
            "PatientID": "UNKNOWN_PATIENT",
            "AccessionNumber": fallback_accession or "UNKNOWN_ACCESSION",
            "StudyInstanceUID": "UNKNOWN_STUDY",
            "SeriesInstanceUID": "",
            "SOPInstanceUID": "",
        }
        try:
            validation_error = validation_errors.get(source)
            if validation_error:
                raise ValueError(validation_error)
            if anonymizer:
                dataset = dcmread(source, force=True)
                source_accession = str(
                    getattr(dataset, "AccessionNumber", "") or ""
                ).strip()
                source_study_uid = str(
                    getattr(dataset, "StudyInstanceUID", "") or ""
                ).strip()
                source_series_uid = str(
                    getattr(dataset, "SeriesInstanceUID", "") or ""
                ).strip()
                source_sop_uid = str(
                    getattr(dataset, "SOPInstanceUID", "") or ""
                ).strip()
                if not str(getattr(dataset, "PatientID", "") or "").strip():
                    dataset.PatientID = str(
                        getattr(dataset, "StudyInstanceUID", "") or "UNKNOWN_PATIENT"
                    )
                if routing_accession:
                    dataset.AccessionNumber = routing_accession
                elif not str(
                    getattr(dataset, "AccessionNumber", "") or ""
                ).strip():
                    dataset.AccessionNumber = fallback_accession or "UNKNOWN_ACCESSION"
                anonymizer.anonymize_dataset(dataset)
            else:
                dataset = dcmread(
                    source,
                    stop_before_pixels=True,
                    force=True,
                    specific_tags=list(metadata),
                )
                source_accession = str(
                    getattr(dataset, "AccessionNumber", "") or ""
                ).strip()
                source_study_uid = str(
                    getattr(dataset, "StudyInstanceUID", "") or ""
                ).strip()
                source_series_uid = str(
                    getattr(dataset, "SeriesInstanceUID", "") or ""
                ).strip()
                source_sop_uid = str(
                    getattr(dataset, "SOPInstanceUID", "") or ""
                ).strip()
            for field in metadata:
                value = str(getattr(dataset, field, "") or "").strip()
                if value:
                    metadata[field] = value
            if not anonymizer and routing_accession:
                # Route the accepted object under the request that opened this
                # receive window.  Keep the original dataset tag untouched so
                # clinical metadata is never silently rewritten.
                metadata["AccessionNumber"] = routing_accession
            sop_instance_uid = UID(metadata["SOPInstanceUID"])
            if not sop_instance_uid.is_valid:
                raise ValueError("invalid SOP Instance UID")
        except Exception as exc:
            if error_callback:
                error_callback(source, str(exc))
            rejected.append(source)
            continue

        if cancel_event is not None and cancel_event.is_set():
            break

        destination = _render_directory_template(
            destination_root,
            directory_template,
            {
                field: value
                for field, value in metadata.items()
                if field not in {"SeriesInstanceUID", "SOPInstanceUID"}
            },
        )
        try:
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / f"{metadata['SOPInstanceUID']}.dcm"
            if anonymizer:
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".dcmget-anonymous-", suffix=".tmp", dir=destination
                )
                os.close(descriptor)
                temporary = Path(temporary_name)
                dataset.save_as(temporary, enforce_file_format=True)
                _validate_anonymized_file(temporary, metadata["SOPInstanceUID"])
                if cancel_event is not None and cancel_event.is_set():
                    temporary.unlink(missing_ok=True)
                    temporary = None
                    break
                publication = _publish_or_deduplicate(
                    temporary,
                    target,
                    expected_sop_instance_uid=metadata["SOPInstanceUID"],
                    conflict_root=destination_root,
                )
                temporary = None
                source.unlink()
            else:
                if cancel_event is not None and cancel_event.is_set():
                    break
                publication = _publish_or_deduplicate(
                    source,
                    target,
                    expected_sop_instance_uid=metadata["SOPInstanceUID"],
                    conflict_root=destination_root,
                )
        except Exception as exc:
            if temporary:
                temporary.unlink(missing_ok=True)
            if error_callback:
                error_callback(source, str(exc))
            rejected.append(source)
            continue
        if source_accession:
            archive_stats.source_accessions.add(source_accession)
        else:
            archive_stats.missing_accession_count += 1
        if source_study_uid:
            archive_stats.study_instance_uids.add(source_study_uid)
        if source_series_uid:
            archive_stats.series_instance_uids.add(source_series_uid)
        if source_sop_uid:
            archive_stats.sop_instance_uids.add(source_sop_uid)
        try:
            published_size = publication.path.stat().st_size
        except OSError:
            published_size = 0
        archive_stats.observations.append(
            ReceivedDicomMetadata(
                file_path=str(publication.path),
                actual_accession_number=source_accession,
                study_instance_uid=source_study_uid,
                series_instance_uid=source_series_uid,
                sop_instance_uid=source_sop_uid,
                size_bytes=published_size,
            )
        )
        if publication.disposition == _ArchiveDisposition.PUBLISHED:
            archive_stats.new_file_count += 1
        elif publication.disposition == _ArchiveDisposition.EXISTING_SKIPPED:
            archive_stats.existing_skipped_count += 1
        else:
            archive_stats.conflict_preserved_count += 1
            archive_stats.conflict_files.append(publication.path)
            continue
        if target not in moved:
            moved.append(target)
    return moved, rejected


def _validate_dicom_files(
    files: list[Path],
    *,
    dcmdump: Path | None = None,
    environment: dict[str, str] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[Path, str]:
    if dcmdump is not None and dcmdump.is_file():
        return _validate_dicom_files_with_dcmdump(
            files,
            dcmdump,
            environment,
            cancel_event,
        )

    errors: dict[Path, str] = {}
    for path in files:
        if cancel_event is not None and cancel_event.is_set():
            break
        try:
            _validate_dicom_stream(path, cancel_event=cancel_event)
        except Exception as exc:
            errors[path] = f"DICOM 文件不完整或损坏：{exc}"
    return errors


def _validate_dicom_files_with_dcmdump(
    files: list[Path],
    dcmdump: Path,
    environment: dict[str, str] | None,
    cancel_event: threading.Event | None = None,
) -> dict[Path, str]:
    errors: dict[Path, str] = {}
    chunks: list[list[Path]] = []
    current: list[Path] = []
    current_length = 0
    for path in files:
        if cancel_event is not None and cancel_event.is_set():
            return errors
        argument_length = len(os.fsencode(path)) + 3
        if current and current_length + argument_length > 24_000:
            chunks.append(current)
            current = []
            current_length = 0
        current.append(path)
        current_length += argument_length
    if current:
        chunks.append(current)

    for chunk in chunks:
        valid = _run_dcmdump_validation(
            dcmdump,
            chunk,
            environment,
            cancel_event=cancel_event,
        )
        if valid is None:
            return errors
        if valid:
            continue
        for path in chunk:
            if cancel_event is not None and cancel_event.is_set():
                return errors
            valid = _run_dcmdump_validation(
                dcmdump,
                [path],
                environment,
                cancel_event=cancel_event,
            )
            if valid is None:
                return errors
            if not valid:
                errors[path] = "DICOM 文件未通过 dcmdump 完整性校验"
    return errors


def _run_dcmdump_validation(
    dcmdump: Path,
    files: list[Path],
    environment: dict[str, str] | None,
    *,
    cancel_event: threading.Event | None = None,
) -> bool | None:
    command = [str(dcmdump), "-q", "-M", "-E", *(str(path) for path in files)]
    timeout_seconds = max(30, min(300, len(files) * 3))
    kwargs: dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": environment,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]
    except OSError:
        return False
    if os.name != "nt":
        setattr(process, "_dcmget_process_group", process.pid)

    deadline = time.monotonic() + timeout_seconds
    while True:
        return_code = process.poll()
        if return_code is not None:
            return return_code == 0
        if cancel_event is not None and cancel_event.is_set():
            _terminate_process(process)
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process(process)
            return False
        if cancel_event is not None:
            cancel_event.wait(timeout=min(0.05, remaining))
        else:
            time.sleep(min(0.05, remaining))


def _validate_dicom_stream(
    path: Path,
    *,
    cancel_event: threading.Event | None = None,
) -> None:
    from pydicom.filereader import (
        _read_file_meta_info,
        data_element_generator,
        read_preamble,
    )
    from pydicom.uid import UID

    file_size = path.stat().st_size
    with path.open("rb") as handle:
        read_preamble(handle, force=False)
        file_meta = _read_file_meta_info(handle)
        transfer_syntax = UID(str(getattr(file_meta, "TransferSyntaxUID", "") or ""))
        if not transfer_syntax.is_valid:
            raise ValueError("缺少有效的 Transfer Syntax UID")
        if transfer_syntax.is_deflated:
            raise ValueError("压缩数据集需要 dcmdump 完整性校验")
        for raw in data_element_generator(
            handle,
            transfer_syntax.is_implicit_VR,
            transfer_syntax.is_little_endian,
            defer_size=1,
        ):
            if cancel_event is not None and cancel_event.is_set():
                return
            raw_length = getattr(raw, "length", None)
            if raw_length is None or raw_length == 0xFFFFFFFF:
                continue
            if raw.value_tell + raw_length > file_size:
                raise EOFError(
                    f"元素 {raw.tag} 声明长度超过文件末尾"
                )


def _publish_or_deduplicate(
    source: Path,
    target: Path,
    *,
    expected_sop_instance_uid: str | None = None,
    conflict_root: Path | None = None,
) -> _ArchivePublication:
    # Separate DownloadRunner instances can archive into the same destination.
    # Keep existence checking, conflict verification and publication atomic.
    with _locked_archive_target(target):
        if target.exists():
            return _resolve_existing_archive_target(
                source,
                target,
                expected_sop_instance_uid=expected_sop_instance_uid,
                conflict_root=conflict_root,
            )
        try:
            os.replace(source, target)
            return _ArchivePublication(_ArchiveDisposition.PUBLISHED, target)
        except OSError as exc:
            if not _is_cross_device_error(exc):
                raise

    # Multi-task staging lives in the private application-state directory,
    # which is commonly on C: while users save DICOM to D: or removable media.
    # Copy into a temporary file beside the target, make the bytes durable, then
    # perform the final same-volume rename under the publication lock.  The
    # source is deliberately retained until a complete target is published or
    # an identical target has been verified.
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".dcmget-publish-",
        suffix=".part",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as writer, source.open("rb") as reader:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        with _locked_archive_target(target):
            if target.exists():
                publication = _resolve_existing_archive_target(
                    temporary,
                    target,
                    expected_sop_instance_uid=expected_sop_instance_uid,
                    conflict_root=conflict_root,
                )
            else:
                os.replace(temporary, target)
                publication = _ArchivePublication(
                    _ArchiveDisposition.PUBLISHED, target
                )
        source.unlink()
        return publication
    finally:
        temporary.unlink(missing_ok=True)


def _resolve_existing_archive_target(
    source: Path,
    target: Path,
    *,
    expected_sop_instance_uid: str | None,
    conflict_root: Path | None,
) -> _ArchivePublication:
    if expected_sop_instance_uid is None:
        if _file_sha256(source) != _file_sha256(target):
            raise ValueError(f"SOP Instance UID 内容冲突：{target.stem}")
        source.unlink()
        return _ArchivePublication(_ArchiveDisposition.EXISTING_SKIPPED, target)

    if _existing_target_has_sop_instance_uid(
        target,
        expected_sop_instance_uid,
        source,
    ):
        source.unlink()
        return _ArchivePublication(_ArchiveDisposition.EXISTING_SKIPPED, target)

    if conflict_root is None:
        raise ValueError(f"SOP Instance UID 内容冲突：{target.stem}")
    conflict = _preserve_conflicting_source(
        source,
        conflict_root,
        expected_sop_instance_uid,
    )
    return _ArchivePublication(_ArchiveDisposition.CONFLICT_PRESERVED, conflict)


def _existing_target_has_sop_instance_uid(
    target: Path,
    expected_sop_instance_uid: str,
    source: Path | None = None,
) -> bool:
    from pydicom import dcmread
    from pydicom.uid import UID

    if target.stem != expected_sop_instance_uid:
        return False
    try:
        _validate_dicom_stream(target)
        dataset = dcmread(
            target,
            defer_size=1,
        )
        actual_sop_instance_uid = str(
            getattr(dataset, "SOPInstanceUID", "") or ""
        ).strip()
        if (
            actual_sop_instance_uid != expected_sop_instance_uid
            or not UID(actual_sop_instance_uid).is_valid
        ):
            return False
        if source is None:
            return True

        # The incoming object has already passed the stream validator.  A
        # syntactically readable old file can still be truncated exactly at an
        # element boundary (for example immediately before Pixel Data).  Only
        # discard the incoming copy when the old object contains every
        # top-level data element carried by the new object and identifies the
        # same SOP Class.  Otherwise preserve the new file for manual review.
        incoming = dcmread(source, defer_size=1)
        incoming_sop_class = str(
            getattr(incoming, "SOPClassUID", "") or ""
        ).strip()
        existing_sop_class = str(
            getattr(dataset, "SOPClassUID", "") or ""
        ).strip()
        if incoming_sop_class and incoming_sop_class != existing_sop_class:
            return False
        return (
            set(incoming.keys()).issubset(dataset.keys())
            and _dataset_pixel_payload_is_complete(dataset, incoming)
            and _binary_payloads_are_safe_to_deduplicate(dataset, incoming)
        )
    except Exception:
        return False


def _dataset_pixel_payload_is_complete(
    dataset: object,
    incoming: object | None = None,
) -> bool:
    """Reject image objects whose pixel element is present but incomplete."""

    from pydicom.pixels.utils import get_expected_length, get_nr_frames
    from pydicom.tag import Tag
    from pydicom.uid import UID

    pixel_data = Tag(0x7FE0, 0x0010)
    float_pixel_data = Tag(0x7FE0, 0x0008)
    double_float_pixel_data = Tag(0x7FE0, 0x0009)
    pixel_tags = [
        tag
        for tag in (pixel_data, float_pixel_data, double_float_pixel_data)
        if tag in dataset  # type: ignore[operator]
    ]
    if not pixel_tags:
        return True
    if len(pixel_tags) != 1:
        return False

    tag = pixel_tags[0]
    element = dataset[tag]  # type: ignore[index]
    value = element.value
    if not isinstance(value, bytes | bytearray) or not value:
        return False

    transfer_syntax = UID(
        str(
            getattr(
                getattr(dataset, "file_meta", None),
                "TransferSyntaxUID",
                "",
            )
            or ""
        )
    )
    if tag == pixel_data and transfer_syntax.is_compressed:
        if not element.is_undefined_length:
            return False
        if not _encapsulated_pixel_data_is_structurally_complete(value):
            return False
        if incoming is None or pixel_data not in incoming:  # type: ignore[operator]
            return False
        incoming_value = incoming[pixel_data].value  # type: ignore[index]
        # Without a decoder for every negotiated transfer syntax, differently
        # encoded compressed payloads cannot be proven equivalent.  Preserve
        # the incoming object for review instead of deleting potentially
        # better image data.  Byte-identical payloads are safe to de-duplicate.
        return isinstance(incoming_value, bytes | bytearray) and bytes(
            incoming_value
        ) == bytes(value)
    if element.is_undefined_length:
        return False

    if tag == pixel_data:
        expected_length = get_expected_length(dataset, unit="bytes")
    else:
        rows = int(getattr(dataset, "Rows"))
        columns = int(getattr(dataset, "Columns"))
        samples = int(getattr(dataset, "SamplesPerPixel", 1))
        bytes_per_sample = 4 if tag == float_pixel_data else 8
        expected_length = (
            rows
            * columns
            * samples
            * int(get_nr_frames(dataset))
            * bytes_per_sample
        )
    padded_length = expected_length + (expected_length % 2)
    return len(value) == padded_length


def _encapsulated_pixel_data_is_structurally_complete(
    value: bytes | bytearray,
) -> bool:
    """Validate the BOT and fragment item boundaries without decoding pixels."""

    position = 0
    item_index = 0
    fragment_count = 0
    size = len(value)
    while position < size:
        if size - position < 8:
            return False
        group, element, length = struct.unpack_from("<HHI", value, position)
        position += 8
        tag = (group, element)
        if tag == (0xFFFE, 0xE0DD):
            return length == 0 and position == size and fragment_count > 0
        if tag != (0xFFFE, 0xE000) or length == 0xFFFFFFFF:
            return False
        if length % 2 or position + length > size:
            return False
        if item_index == 0:
            if length % 4:
                return False
        else:
            if length == 0:
                return False
            fragment_count += 1
        position += length
        item_index += 1
    return item_index >= 2 and fragment_count > 0


def _binary_payloads_are_safe_to_deduplicate(
    existing: object,
    incoming: object,
) -> bool:
    """Require non-pixel binary payloads to match before deleting incoming."""

    binary_vrs = {"OB", "OD", "OF", "OL", "OV", "OW", "UN"}
    pixel_tags = {0x7FE00008, 0x7FE00009, 0x7FE00010}
    for incoming_element in incoming:  # type: ignore[union-attr]
        if int(incoming_element.tag) in pixel_tags:
            continue
        if incoming_element.VR == "SQ":
            incoming_items = incoming_element.value
            if not any(
                _dataset_contains_non_pixel_binary_payload(item)
                for item in incoming_items
            ):
                continue
            if incoming_element.tag not in existing:  # type: ignore[operator]
                return False
            existing_element = existing[incoming_element.tag]  # type: ignore[index]
            if existing_element.VR != "SQ":
                return False
            existing_items = existing_element.value
            if len(existing_items) < len(incoming_items):
                return False
            if any(
                not _binary_payloads_are_safe_to_deduplicate(
                    existing_items[index],
                    incoming_item,
                )
                for index, incoming_item in enumerate(incoming_items)
            ):
                return False
            continue
        if incoming_element.VR not in binary_vrs:
            continue
        if incoming_element.tag not in existing:  # type: ignore[operator]
            return False
        existing_element = existing[incoming_element.tag]  # type: ignore[index]
        if existing_element.VR not in binary_vrs:
            return False
        incoming_value = incoming_element.value
        existing_value = existing_element.value
        if not isinstance(incoming_value, bytes | bytearray) or not isinstance(
            existing_value,
            bytes | bytearray,
        ):
            return False
        if bytes(existing_value) != bytes(incoming_value):
            return False
    return True


def _dataset_contains_non_pixel_binary_payload(dataset: object) -> bool:
    binary_vrs = {"OB", "OD", "OF", "OL", "OV", "OW", "UN"}
    pixel_tags = {0x7FE00008, 0x7FE00009, 0x7FE00010}
    for element in dataset:  # type: ignore[union-attr]
        if int(element.tag) in pixel_tags:
            continue
        if element.VR in binary_vrs:
            return True
        if element.VR == "SQ" and any(
            _dataset_contains_non_pixel_binary_payload(item)
            for item in element.value
        ):
            return True
    return False


def _preserve_conflicting_source(
    source: Path,
    destination_root: Path,
    sop_instance_uid: str,
) -> Path:
    conflict_directory = destination_root / "_DcmGetConflicts"
    conflict_directory.mkdir(parents=True, exist_ok=True)
    descriptor, reservation_name = tempfile.mkstemp(
        prefix=f"{sop_instance_uid}-",
        suffix=".reserve",
        dir=conflict_directory,
    )
    os.close(descriptor)
    reservation = Path(reservation_name)
    conflict = reservation.with_suffix(".dcm")
    preserved = False
    temporary: Path | None = None
    try:
        try:
            os.replace(source, conflict)
            preserved = True
            return conflict
        except OSError as exc:
            if not _is_cross_device_error(exc):
                raise

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".dcmget-conflict-",
            suffix=".part",
            dir=conflict_directory,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as writer, source.open("rb") as reader:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, conflict)
        temporary = None
        source.unlink()
        preserved = True
        return conflict
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        reservation.unlink(missing_ok=True)
        if not preserved:
            conflict.unlink(missing_ok=True)


@contextmanager
def _locked_archive_target(target: Path):
    """Serialize one final SOP path across threads and DcmGet processes."""

    try:
        normalized = os.path.normcase(str(target.resolve(strict=False)))
    except OSError:
        normalized = os.path.normcase(os.path.abspath(os.fspath(target)))
    digest = hashlib.sha256(os.fsencode(normalized)).hexdigest()
    lock_directory = ensure_application_state_dir() / "archive-publish-locks"
    lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        lock_directory.chmod(0o700)
    except OSError:
        pass
    lock_path = lock_directory / f"{digest}.lock"
    with _archive_publish_lock, FileLock(str(lock_path), timeout=300):
        yield


def _is_cross_device_error(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_anonymized_file(path: Path, expected_sop_instance_uid: str) -> None:
    from pydicom import dcmread
    from pydicom.uid import UID

    with path.open("rb") as handle:
        handle.seek(128)
        if handle.read(4) != b"DICM":
            raise ValueError("anonymous output is missing DICM prefix")
    dataset = dcmread(path, stop_before_pixels=True)
    sop_instance_uid = str(getattr(dataset, "SOPInstanceUID", "") or "")
    if sop_instance_uid != expected_sop_instance_uid or not UID(sop_instance_uid).is_valid:
        raise ValueError("anonymous output has invalid SOP Instance UID")


def _render_directory_template(
    destination_root: Path,
    template: str,
    metadata: dict[str, str],
) -> Path:
    rendered = template.strip().replace("\\", "/")
    rendered = re.sub(
        r"\{(PatientID|AccessionNumber|StudyInstanceUID)\}",
        lambda match: _safe_path_component(
            metadata[match.group(1)], f"UNKNOWN_{match.group(1).upper()}"
        ),
        rendered,
    )
    components = [
        _safe_path_component(component, "UNKNOWN")
        for component in rendered.split("/")
        if component.strip()
    ]
    return destination_root.joinpath(*(components or ["UNKNOWN"]))


def _safe_path_component(value: str, fallback: str) -> str:
    source = str(value).strip()
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", source).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = fallback
    reserved_stem = cleaned.split(".", 1)[0].upper()
    if reserved_stem in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }:
        cleaned = f"_{cleaned}"
    if cleaned != source:
        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned}-{digest}"
    return cleaned[:180]


def _common_output_directory(files: list[Path], fallback: Path) -> Path:
    if not files:
        return fallback
    return Path(os.path.commonpath([str(path.parent) for path in files]))


def _dcmtk_environment(tools: ToolPaths) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = str(tools.bin_dir) + os.pathsep + env.get("PATH", "")
    share_root = tools.bin_dir.parent / "share"
    if share_root.exists():
        dictionaries = list(share_root.glob("dcmtk-*/dicom.dic")) + list(share_root.glob("dicom.dic"))
        if dictionaries:
            env.setdefault("DCMDICTPATH", str(dictionaries[0]))
    return env


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        if process.poll() is not None:
            return
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
    process_group = getattr(process, "_dcmget_process_group", None)
    if not isinstance(process_group, int) or process_group <= 0:
        if process.poll() is not None:
            return
        try:
            process_group = os.getpgid(process.pid)
        except OSError:
            return
        if process_group != process.pid:
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
            return

    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        if process.poll() is None:
            process.terminate()

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            break
        except OSError:
            break
        time.sleep(0.05)
    else:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if process.poll() is None:
                process.kill()
        # Signal delivery and process-state updates are asynchronous on macOS.
        # Give the kernel a short window to retire the orphaned group before
        # reporting cleanup complete to the worker thread.
        kill_deadline = time.monotonic() + 1
        while time.monotonic() < kill_deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                break
            except OSError:
                break
            time.sleep(0.05)

    if process.poll() is None:
        try:
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()


def _display_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
