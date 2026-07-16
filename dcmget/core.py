from __future__ import annotations

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
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

from .config import AppConfig
from .anonymization import DicomAnonymizer
from .diagnostics import PrivateRotatingFileHandler
from .runtime import ensure_application_state_dir


class AccessionStatus(str, Enum):
    WAITING = "等待"
    DOWNLOADING = "下载中"
    COMPLETED = "完成"
    NO_DATA = "无数据"
    PARTIAL = "部分成功"
    FAILED = "失败"
    CANCELLED = "已取消"


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


@dataclass(slots=True)
class BatchSummary:
    results: list[AccessionResult] = field(default_factory=list)
    cancelled: bool = False
    staging_directory: str = ""

    @property
    def exit_code(self) -> int:
        if self.cancelled:
            return 130
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


@dataclass(slots=True)
class _MoveDiagnostics:
    pending_responses: int = 0


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
        config.storage_ae_title,
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
        config.calling_ae_title,
        "-aec",
        config.pacs_ae_title,
        "-aem",
        config.storage_ae_title,
        config.pacs_server_ip,
        str(config.pacs_server_port),
        "-S",
        "-k",
        "QueryRetrieveLevel=STUDY",
        "-k",
        f"0008,0050={accession}",
    ]


def preflight(config: AppConfig, resolver: DcmtkResolver) -> PreflightResult:
    errors = config.validate()
    checks: list[tuple[str, bool, str]] = []

    tools: ToolPaths | None = None
    try:
        tools = resolver.resolve(config.dcmtk_bin_dir)
        checks.append(("DCMTK 工具", True, f"已就绪，版本 {tools.version}"))
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
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
        except OSError as exc:
            message = f"PDI 输出目录不可写：{exc}"
            errors["pdi_output_folder"] = message
            checks.append(("PDI 输出目录", False, message))

    if "storage_port" not in errors:
        available = is_port_available(config.storage_port)
        message = "端口可用" if available else f"端口 {config.storage_port} 已被占用"
        checks.append(("接收端口", available, message))
        if not available:
            errors["storage_port"] = message

    checks.append(
        (
            "PACS 配置",
            not any(key in errors for key in ("pacs_server_ip", "pacs_server_port", "pacs_ae_title")),
            f"{config.pacs_server_ip}:{config.pacs_server_port} / {config.pacs_ae_title}",
        )
    )
    return PreflightResult(tools, errors, checks)


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
    ):
        self.config = config
        self.tools = tools
        self.log_callback = log_callback or (lambda _source, _message, _level: None)
        self.state_callback = state_callback or (lambda _state: None)
        self.progress_callback = progress_callback or (lambda _index, _total, _result: None)
        self.ready_callback = ready_callback or (lambda: None)
        self.process_callback = process_callback or (
            lambda _kind, _pid, _executable, _active: None
        )
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
        self._storescp_abort_count = 0
        self._staging_accession_cache: dict[
            Path, tuple[int, int, int, str | None]
        ] = {}
        self._logger = self._build_file_logger()
        self._anonymizer = (
            DicomAnonymizer(config.anonymization_profile)
            if config.anonymization_enabled
            else None
        )

    def request_cancel(self) -> None:
        self._cancel.set()
        with self._pause_condition:
            self._pause_condition.notify_all()
        self._emit("应用", "正在停止当前任务…", "warning")
        with self._cancel_cleanup_lock:
            if self._cancel_cleanup_thread is not None:
                return
            cleanup = threading.Thread(
                target=self._cancel_running_processes,
                name="dcmtk-cancel-cleanup",
                daemon=True,
            )
            self._cancel_cleanup_thread = cleanup
            cleanup.start()

    def _cancel_running_processes(self) -> None:
        with self._process_lock:
            processes = (self._current_process, self._storescp_process)
        seen: set[int] = set()
        for process in processes:
            if process is None or id(process) in seen:
                continue
            seen.add(id(process))
            self._terminate_process_safely(process)

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
        self._staging_accession_cache.clear()
        staging = staging_directory_root(self.config) / datetime.now().strftime(
            "%Y%m%d-%H%M%S-%f"
        )
        staging.mkdir(parents=True, exist_ok=False)
        summary = BatchSummary(staging_directory=str(staging))

        self.state_callback("starting_receiver")
        try:
            if self._anonymizer:
                self._emit(
                    "匿名",
                    f"已启用 {self.config.anonymization_profile} 元数据匿名方案",
                    "info",
                )
            self._start_storescp(staging)
            if not self._cancel.is_set():
                self.ready_callback()
            self.state_callback("downloading")
            for index, accession in enumerate(values, 1):
                if self._cancel.is_set():
                    summary.cancelled = True
                    self._append_cancelled(summary, values[index - 1 :])
                    break
                if not self._wait_if_paused():
                    summary.cancelled = True
                    self._append_cancelled(summary, values[index - 1 :])
                    break

                result = self._download_one(accession, staging, index, len(values))
                summary.results.append(result)
                self.progress_callback(index, len(values), result)

                if result.status == AccessionStatus.CANCELLED:
                    summary.cancelled = True
                    self._append_cancelled(summary, values[index:])
                    break
        finally:
            self.state_callback("stopping")
            self._stop_storescp()
            self._cleanup_staging(staging)
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
        command = build_storescp_command(self.config, self.tools, staging)
        mode = "多进程并发（--fork）" if self.tools.supports_fork else "单进程兼容模式"
        self._emit("storescp", f"接收模式：{mode}", "info")
        self._emit("storescp", f"启动接收器：{_display_command(command)}", "info")
        process = self._popen(command)
        with self._process_lock:
            self._storescp_process = process
        self._notify_process("storescp", getattr(process, "pid", 0), command[0], True)
        self._start_reader(process, "storescp")

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._cancel.is_set():
                return
            if process.poll() is not None:
                raise RuntimeError(f"storescp 启动失败，退出码 {process.returncode}")
            if _port_is_listening(self.config.storage_port):
                self._emit("storescp", f"已监听端口 {self.config.storage_port}", "success")
                return
            time.sleep(0.1)
        self._terminate_process_safely(process)
        raise TimeoutError(f"storescp 未能在端口 {self.config.storage_port} 就绪")

    def _download_one(
        self, accession: str, staging: Path, index: int, total: int
    ) -> AccessionResult:
        before = _files_in(staging)
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
                            "storescp",
                            f"接收器意外退出，退出码 {receiver_exit_code}",
                            "error",
                        )
                        self._terminate_process_safely(process)
                        break
                now = time.monotonic()
                if now - last_sample_at >= 0.5:
                    new_files = _files_in(staging) - before
                    received = len(new_files)
                    received_bytes = _total_file_size(new_files)
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

        all_files = _files_in(staging)
        new_files = all_files - before
        candidate_files, mismatched_files = _select_files_for_accession(
            all_files,
            new_files,
            accession,
            cache=self._staging_accession_cache,
        )
        if mismatched_files:
            self._emit(
                "storescp",
                (
                    f"收到 {len(mismatched_files)} 个检查号与当前任务不匹配的文件，"
                    f"已保留在暂存目录：{staging}"
                ),
                "warning",
            )
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
        )
        rejected_detail = (
            f"{len(rejected)} 个匿名或归档失败文件留在私有暂存目录（原因见日志）"
            if self._anonymizer
            else f"{len(rejected)} 个异常文件留在暂存目录"
        )
        output_directory = _common_output_directory(moved, destination_root)
        duration = time.monotonic() - started
        with self._diagnostic_lock:
            receiver_aborts = self._storescp_abort_count - aborts_before
        pending_responses = diagnostics.pending_responses

        if self._cancel.is_set():
            status = AccessionStatus.CANCELLED
            message = "用户已取消"
        elif receiver_exit_code is not None:
            status = AccessionStatus.PARTIAL if moved else AccessionStatus.FAILED
            message = f"storescp 意外退出（退出码 {receiver_exit_code}）"
            if moved:
                message += f"，已保留 {len(moved)} 个完整文件"
            if rejected:
                message += f"，{rejected_detail}"
        elif return_code == 0 and moved and (receiver_aborts or rejected):
            status = AccessionStatus.PARTIAL
            details = []
            if receiver_aborts:
                details.append(f"{receiver_aborts} 个接收连接中止")
            if rejected:
                details.append(rejected_detail)
            message = f"收到 {len(moved)} 个文件，但" + "，".join(details)
        elif return_code == 0 and moved:
            status = AccessionStatus.COMPLETED
            message = f"收到 {len(moved)} 个文件"
        elif return_code == 0 and (pending_responses or receiver_aborts or rejected):
            status = AccessionStatus.FAILED
            details = []
            if pending_responses:
                details.append(f"PACS 返回 {pending_responses} 次待处理响应")
            if receiver_aborts:
                details.append(f"{receiver_aborts} 个接收连接中止")
            if rejected:
                details.append(rejected_detail)
            message = "，".join(details) + "，未收到文件"
        elif return_code == 0:
            status = AccessionStatus.NO_DATA
            message = "C-MOVE 完成，但未收到文件"
        elif moved:
            status = AccessionStatus.PARTIAL
            message = f"movescu 退出码 {return_code}，已保留 {len(moved)} 个文件"
            if rejected:
                message += f"，{rejected_detail}"
        else:
            status = AccessionStatus.FAILED
            message = f"movescu 退出码 {return_code}，未收到文件"
            if rejected:
                message += f"，{rejected_detail}"

        level = "success" if status == AccessionStatus.COMPLETED else (
            "warning" if status in {AccessionStatus.NO_DATA, AccessionStatus.PARTIAL, AccessionStatus.CANCELLED} else "error"
        )
        self._emit("movescu", f"{accession}：{message}", level)
        return AccessionResult(
            accession=accession,
            status=status,
            file_count=len(moved),
            duration_seconds=duration,
            message=message,
            output_directory=str(output_directory) if moved else "",
            received_bytes=received_bytes,
            speed_bytes_per_second=average_speed,
            archived_files=[str(path) for path in moved],
        )

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
                        elif (
                            source == "movescu"
                            and diagnostics is not None
                            and "Received Move Response" in text
                            and "(Pending)" in text
                        ):
                            diagnostics.pending_responses += 1
                    level = (
                        "error"
                        if text.startswith(("E:", "F:"))
                        else "warning"
                        if "Association Aborted" in text
                        else "info"
                    )
                    self._emit(source, text, level)

        thread = threading.Thread(target=read_output, name=f"{source}-output", daemon=True)
        thread.start()
        return thread

    def _stop_storescp(self) -> None:
        with self._process_lock:
            process = self._storescp_process
            self._storescp_process = None
        if process:
            # ``storescp --fork`` children can outlive an exited group leader.
            # Always ask the process-group cleanup helper to drain the group.
            self._terminate_process_safely(process)
            self._emit("storescp", "接收器已停止", "info")
            self._notify_process(
                "storescp", getattr(process, "pid", 0), str(self.tools.storescp), False
            )

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
            self._emit("恢复", f"无法更新 {kind} 进程恢复信息：{exc}", "warning")

    def _emit(self, source: str, message: str, level: str) -> None:
        self.log_callback(source, message, level)
        log_level = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "success": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
        }.get(level, logging.INFO)
        self._logger.log(log_level, "[%s] %s", source, message)

    def _build_file_logger(self) -> logging.Logger:
        log_dir = log_directory(self.config)
        log_dir.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"dcmget.runner.{id(self)}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = PrivateRotatingFileHandler(
            log_dir / "dcmget.log",
            maxBytes=self.config.max_log_file_size_bytes,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        return logger

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
    del config
    return ensure_application_state_dir() / "logs"


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


def _select_files_for_accession(
    all_files: Iterable[Path],
    new_files: Iterable[Path],
    accession: str,
    *,
    cache: dict[Path, tuple[int, int, int, str | None]] | None = None,
) -> tuple[list[Path], list[Path]]:
    """Select received files without assigning another accession by timing alone."""

    from pydicom import dcmread

    new = set(new_files)
    current = set(all_files)
    metadata_cache = cache if cache is not None else {}
    for stale in metadata_cache.keys() - current:
        metadata_cache.pop(stale, None)
    selected: list[Path] = []
    mismatched: list[Path] = []
    expected = accession.strip()
    for path in sorted(current):
        try:
            stat = path.stat()
        except OSError:
            metadata_cache.pop(path, None)
            continue
        fingerprint = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
        cached = metadata_cache.get(path)
        if cached is not None and cached[:3] == fingerprint:
            received_accession = cached[3]
        else:
            try:
                dataset = dcmread(
                    path,
                    stop_before_pixels=True,
                    force=True,
                    specific_tags=["AccessionNumber"],
                )
                received_accession = str(
                    getattr(dataset, "AccessionNumber", "") or ""
                ).strip()
            except Exception:
                received_accession = None
            metadata_cache[path] = (*fingerprint, received_accession)

        if received_accession is None:
            # Newly written malformed files still have to reach validation so
            # the active accession is reported as failed instead of no-data.
            if path in new:
                selected.append(path)
            continue

        if received_accession == expected:
            selected.append(path)
        elif not received_accession and path in new:
            # Some legacy PACS omit AccessionNumber in returned instances.  A
            # file created during this strictly sequential C-MOVE is the only
            # safe compatibility fallback.
            selected.append(path)
        elif path in new:
            mismatched.append(path)
    return selected, mismatched


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
) -> tuple[list[Path], list[Path]]:
    from pydicom import dcmread
    from pydicom.uid import UID

    moved: list[Path] = []
    rejected: list[Path] = []
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
        metadata = {
            "PatientID": "UNKNOWN_PATIENT",
            "AccessionNumber": fallback_accession or "UNKNOWN_ACCESSION",
            "StudyInstanceUID": "UNKNOWN_STUDY",
            "SOPInstanceUID": "",
        }
        try:
            validation_error = validation_errors.get(source)
            if validation_error:
                raise ValueError(validation_error)
            if anonymizer:
                dataset = dcmread(source, force=True)
                if not str(getattr(dataset, "PatientID", "") or "").strip():
                    dataset.PatientID = str(
                        getattr(dataset, "StudyInstanceUID", "") or "UNKNOWN_PATIENT"
                    )
                if not str(getattr(dataset, "AccessionNumber", "") or "").strip():
                    dataset.AccessionNumber = fallback_accession or "UNKNOWN_ACCESSION"
                anonymizer.anonymize_dataset(dataset)
            else:
                dataset = dcmread(
                    source,
                    stop_before_pixels=True,
                    force=True,
                    specific_tags=list(metadata),
                )
            for field in metadata:
                value = str(getattr(dataset, field, "") or "").strip()
                if value:
                    metadata[field] = value
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
                if field != "SOPInstanceUID"
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
                _publish_or_deduplicate(temporary, target)
                temporary = None
                source.unlink()
            else:
                if cancel_event is not None and cancel_event.is_set():
                    break
                _publish_or_deduplicate(source, target)
        except Exception as exc:
            if temporary:
                temporary.unlink(missing_ok=True)
            if error_callback:
                error_callback(source, str(exc))
            rejected.append(source)
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
            if raw.length == 0xFFFFFFFF:
                continue
            if raw.value_tell + raw.length > file_size:
                raise EOFError(
                    f"元素 {raw.tag} 声明长度超过文件末尾"
                )


def _publish_or_deduplicate(source: Path, target: Path) -> None:
    if not target.exists():
        os.replace(source, target)
        return
    if _file_sha256(source) != _file_sha256(target):
        raise ValueError(f"SOP Instance UID 内容冲突：{target.stem}")
    source.unlink()


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
