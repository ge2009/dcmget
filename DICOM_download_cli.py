from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dcmget import __version__
from dcmget.architecture import ArchitectureError, ensure_supported_runtime
from dcmget.config import (
    DEFAULT_DIRECTORY_TEMPLATE,
    AppConfig,
    load_accessions,
)
from dcmget.core import (
    AccessionStatus,
    BatchSummary,
    DcmtkResolver,
    DownloadRunner,
    preflight,
)
from dcmget.runtime import is_frozen, set_portable_dcmtk_bin
from dcmget.task_state import (
    TaskCheckpoint,
    TaskCheckpointStore,
    TaskStateError,
    merge_checkpoint_summary,
)


DEFAULT_CONFIG: dict[str, object] = {
    "dcmtk_bin_dir": "",
    "dicom_destination_folder": "Dicom",
    "pacs_server_ip": "127.0.0.1",
    "pacs_server_port": 104,
    "calling_ae_title": "DCMGET",
    "pacs_ae_title": "ANY-SCP",
    "storage_ae_title": "DCMGET",
    "storage_port": 6666,
    "directory_template": DEFAULT_DIRECTORY_TEMPLATE,
    "auto_retry_attempts": 2,
    "auto_retry_backoff_seconds": 3,
    "circuit_breaker_failures": 5,
    "minimum_free_space_bytes": 2 * 1024**3,
}
CONFIG_FIELDS = frozenset(DEFAULT_CONFIG)
PACKAGED_DCMTK_RELATIVE = Path("dcmtk") / "bin"


@dataclass(frozen=True, slots=True)
class StandaloneConfig:
    path: Path
    app_config: AppConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            f"DcmGetCLI {__version__} 独立 DICOM 下载器；"
            "仅支持每行一个检查号的 TXT 文件"
        )
    )
    parser.add_argument("accession_file", metavar="ACCESS.TXT", help="检查号 TXT 文件")
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件；默认读取 DcmGetCLI.exe 同目录的 config.json",
    )
    parser.add_argument(
        "--reset",
        "--discard-checkpoint",
        dest="reset",
        action="store_true",
        help="放弃当前配置的未完成恢复点，从 ACCESS.TXT 重新开始",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="在控制台显示详细运行日志；默认只显示警告和错误",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"DcmGetCLI {__version__}",
    )
    return parser


def executable_directory() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def default_config_path() -> Path:
    return executable_directory() / "config.json"


def standalone_state_root() -> Path:
    if sys.platform == "win32":
        base = Path(
            os.environ.get(
                "LOCALAPPDATA",
                Path.home() / "AppData" / "Local",
            )
        )
        return base / "DcmGetCLI"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DcmGetCLI"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "dcmget-cli"


def task_state_path(config_path: Path) -> Path:
    identity = os.path.normcase(str(config_path.resolve()))
    slot = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return standalone_state_root() / "tasks" / slot / "active-task.sqlite3"


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise ValueError(f"配置项 {key} 必须是字符串")
    value = value.strip()
    if key != "dcmtk_bin_dir" and not value:
        raise ValueError(f"配置项 {key} 不能为空")
    return value


def _required_integer(raw: dict[str, Any], key: str) -> int:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"配置项 {key} 必须是整数")
    return value


def _resolved_path(value: str, base: Path) -> Path:
    expanded = Path(os.path.expandvars(value)).expanduser()
    return expanded.resolve() if expanded.is_absolute() else (base / expanded).resolve()


def load_standalone_config(path: str | Path) -> StandaloneConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(
            f"配置文件不存在：{config_path}；"
            "请确认压缩包内的 config.json 未被删除，"
            "或使用 --config 指定配置文件"
        )
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"配置 JSON 格式错误（第 {exc.lineno} 行，第 {exc.colno} 列）"
        ) from exc
    if not isinstance(raw, dict):
        raise ValueError("配置文件根节点必须是 JSON 对象")
    unknown = sorted(set(raw) - CONFIG_FIELDS)
    missing = sorted(CONFIG_FIELDS - set(raw))
    if unknown:
        raise ValueError("配置包含不支持的字段：" + "、".join(unknown))
    if missing:
        raise ValueError("配置缺少字段：" + "、".join(missing))

    strings = {
        key: _required_string(raw, key)
        for key in (
            "dcmtk_bin_dir",
            "dicom_destination_folder",
            "pacs_server_ip",
            "calling_ae_title",
            "pacs_ae_title",
            "storage_ae_title",
            "directory_template",
        )
    }
    integers = {
        key: _required_integer(raw, key)
        for key in (
            "pacs_server_port",
            "storage_port",
            "auto_retry_attempts",
            "auto_retry_backoff_seconds",
            "circuit_breaker_failures",
            "minimum_free_space_bytes",
        )
    }
    base = config_path.parent
    dcmtk = strings["dcmtk_bin_dir"]
    hidden_web_port = 65535 if integers["storage_port"] != 65535 else 65534
    config = AppConfig(
        dcmtk_bin_dir=(str(_resolved_path(dcmtk, base)) if dcmtk else ""),
        dicom_destination_folder=str(
            _resolved_path(strings["dicom_destination_folder"], base)
        ),
        pacs_server_ip=strings["pacs_server_ip"],
        pacs_server_port=integers["pacs_server_port"],
        calling_ae_title=strings["calling_ae_title"],
        pacs_ae_title=strings["pacs_ae_title"],
        storage_ae_title=strings["storage_ae_title"],
        storage_port=integers["storage_port"],
        directory_template=strings["directory_template"],
        auto_retry_attempts=integers["auto_retry_attempts"],
        auto_retry_backoff_seconds=integers["auto_retry_backoff_seconds"],
        circuit_breaker_failures=integers["circuit_breaker_failures"],
        minimum_free_space_bytes=integers["minimum_free_space_bytes"],
        web_bind_address="127.0.0.1",
        web_port=hidden_web_port,
        web_open_browser=False,
        anonymization_enabled=False,
        pdi_export_enabled=False,
    )
    errors = config.validate()
    if errors:
        detail = "；".join(f"{field}: {message}" for field, message in errors.items())
        raise ValueError(detail)
    return StandaloneConfig(config_path, config)


def configure_packaged_dcmtk() -> Path | None:
    candidate = executable_directory() / PACKAGED_DCMTK_RELATIVE
    suffix = ".exe" if sys.platform == "win32" else ""
    if not all((candidate / f"{name}{suffix}").is_file() for name in ("movescu", "storescp")):
        return None
    set_portable_dcmtk_bin(candidate)
    return candidate


def _load_accession_file(path: str | Path) -> list[str]:
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() != ".txt":
        raise ValueError("独立版只接受 TXT 检查号文件")
    parsed = load_accessions(source)
    if parsed.invalid_values:
        examples = "、".join(parsed.invalid_values[:3])
        raise ValueError(
            "检查号不能包含通配符、反斜杠或控制字符：" + examples
        )
    if not parsed.values:
        raise ValueError("检查号列表为空")
    if parsed.blank_count or parsed.duplicate_count:
        print(
            f"[检查号] 有效 {len(parsed.values)}，忽略空行 {parsed.blank_count}，"
            f"去重 {parsed.duplicate_count}"
        )
    return parsed.values


def _same_task_input(
    checkpoint: TaskCheckpoint,
    config: AppConfig,
    accessions: list[str],
) -> bool:
    return checkpoint.config.to_dict() == config.to_dict() and checkpoint.accessions == accessions


def _format_totals(summary: BatchSummary) -> str:
    counts = {status: 0 for status in AccessionStatus}
    for result in summary.results:
        counts[result.status] += 1
    return (
        f"完成 {counts[AccessionStatus.COMPLETED]}，"
        f"无数据 {counts[AccessionStatus.NO_DATA]}，"
        f"部分成功 {counts[AccessionStatus.PARTIAL]}，"
        f"失败 {counts[AccessionStatus.FAILED]}，"
        f"取消 {counts[AccessionStatus.CANCELLED]}，"
        f"文件 {sum(result.file_count for result in summary.results)}"
    )


def _prepare_checkpoint(
    store: TaskCheckpointStore,
    config: AppConfig,
    accessions: list[str],
    *,
    reset: bool,
) -> TaskCheckpoint | None:
    if not store.path.is_file():
        return None
    if not store.try_acquire_lease():
        raise TaskStateError("相同配置的另一个 DcmGetCLI 正在运行")
    checkpoint = store.load_required()
    for message in store.cleanup_recorded_processes(checkpoint.task_id):
        print(f"[恢复] {message}")
    if reset:
        store.clear(checkpoint.task_id)
        print("[恢复] 已放弃旧恢复点；已落盘文件保持不变")
        return None
    if not _same_task_input(checkpoint, config, accessions):
        raise TaskStateError(
            "当前配置或 access.txt 与未完成任务不一致；"
            "请恢复原文件继续，或增加 --reset 明确开始新任务"
        )
    if checkpoint.phase == "download_retryable":
        checkpoint = store.prepare_download_retry(checkpoint.task_id)
        print("[恢复] 正在继续未处理项并重试失败或部分成功项")
    elif checkpoint.phase != "downloading":
        raise TaskStateError(f"独立版不能恢复任务阶段：{checkpoint.phase}")
    print(
        f"[恢复] 任务 {checkpoint.task_id[:8]}："
        f"已完成 {len(checkpoint.results)}/{len(checkpoint.accessions)}，"
        f"待处理 {len(checkpoint.pending_accessions)}"
    )
    return checkpoint


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        ensure_supported_runtime()
    except ArchitectureError as exc:
        print(f"运行环境不受支持：{exc}", file=sys.stderr)
        return 1

    args = build_parser().parse_args(argv)
    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    try:
        standalone = load_standalone_config(config_path)
        accessions = _load_accession_file(args.accession_file)
    except (OSError, ValueError) as exc:
        print(f"配置或检查号错误：{exc}", file=sys.stderr)
        return 1

    configure_packaged_dcmtk()
    store = TaskCheckpointStore(task_state_path(standalone.path))
    checkpoint: TaskCheckpoint | None = None
    try:
        checkpoint = _prepare_checkpoint(
            store,
            standalone.app_config,
            accessions,
            reset=args.reset,
        )
    except (OSError, TaskStateError) as exc:
        store.release_lease()
        print(f"任务恢复失败：{exc}", file=sys.stderr)
        return 1

    config = checkpoint.config if checkpoint is not None else standalone.app_config
    if checkpoint is not None and not checkpoint.pending_accessions:
        recovered_summary = BatchSummary(list(checkpoint.results))
        if recovered_summary.exit_code == 0:
            print("[恢复] 任务结果已全部写入，无需重新启动接收器")
            print("[结果] " + _format_totals(recovered_summary))
            try:
                store.clear(checkpoint.task_id)
            except TaskStateError as exc:
                print(f"无法清理已完成恢复点：{exc}", file=sys.stderr)
                store.release_lease()
                return 1
            store.release_lease()
            return 0

        try:
            store.set_phase(checkpoint.task_id, "download_retryable")
            checkpoint = store.prepare_download_retry(checkpoint.task_id)
        except TaskStateError as exc:
            store.release_lease()
            print(f"无法准备失败项重试：{exc}", file=sys.stderr)
            return 1
        config = checkpoint.config
        print("[恢复] 已重新加入失败或部分成功项")

    resolver = DcmtkResolver(executable_directory())
    check = preflight(config, resolver)
    for name, ok, message in check.checks:
        print(f"[{'通过' if ok else '失败'}] {name}：{message}")
    if not check.ok or check.tools is None:
        store.release_lease()
        return 1

    if checkpoint is None:
        if not store.lease_held and not store.try_acquire_lease():
            print("相同配置的另一个 DcmGetCLI 正在启动", file=sys.stderr)
            return 1
        try:
            checkpoint = store.start(
                config,
                accessions,
                trial_required=False,
            )
        except TaskStateError as exc:
            store.release_lease()
            print(f"无法建立任务恢复点：{exc}", file=sys.stderr)
            return 1

    task_id = checkpoint.task_id
    pending = checkpoint.pending_accessions
    offset = len(checkpoint.results)
    total = len(checkpoint.accessions)
    runner: DownloadRunner | None = None

    def request_cancel(_signum: int, _frame: object) -> None:
        if runner is not None:
            runner.request_cancel()

    signal.signal(signal.SIGINT, request_cancel)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_cancel)

    def progress(index: int, _count: int, result: Any) -> None:
        persisted = store.record_result(task_id, result)
        print(
            f"[{offset + index}/{total}] {persisted.accession}："
            f"{persisted.status.value}，{persisted.file_count} 个文件，"
            f"{persisted.duration_seconds:.1f} 秒"
        )

    def log_message(source: str, message: str, level: str) -> None:
        if args.verbose or level in {"warning", "error"}:
            stream = sys.stderr if level == "error" else sys.stdout
            print(f"[{source}] {message}", file=stream)

    try:
        runner = DownloadRunner(
            config,
            check.tools,
            log_callback=log_message,
            state_callback=(
                (lambda state: print(f"[状态] {state}"))
                if args.verbose
                else None
            ),
            progress_callback=progress,
            process_callback=lambda kind, pid, executable, active: store.record_process(
                task_id,
                kind,
                pid,
                executable,
                active=active,
            ),
            log_file_name=f"task-{task_id[:8]}.log",
            log_directory=Path(config.dicom_destination_folder) / "_DcmGetLogs",
            fallback_log_directory=standalone_state_root() / "logs",
            recover_legacy_staging=False,
        )
        current = runner.run(pending)
        persisted = store.load_required()
        final_accessions = {result.accession for result in persisted.results}
        partial_accessions = set(persisted.partial_results)
        for result in current.results:
            if (
                result.status == AccessionStatus.CANCELLED
                and result.archived_files
                and result.accession not in partial_accessions
            ) or (
                result.status != AccessionStatus.CANCELLED
                and result.accession not in final_accessions
            ):
                store.record_result(task_id, result)
        checkpoint = store.load_required()
        summary = merge_checkpoint_summary(checkpoint, current)
        print("[结果] " + _format_totals(summary))
        if summary.cancelled:
            print("[恢复] 已保留进度；再次运行相同命令即可继续")
            return 130
        if summary.exit_code == 2:
            store.set_phase(
                task_id,
                "download_retryable",
                interrupted_reason=summary.interrupted_reason,
            )
            print("[恢复] 已保留失败项；再次运行相同命令将只继续未完成项")
            return 2
        store.clear(task_id)
        return 0
    except (OSError, RuntimeError, TaskStateError, ValueError) as exc:
        try:
            store.set_phase(task_id, "download_retryable", interrupted_reason=str(exc))
        except TaskStateError:
            pass
        print(f"下载失败：{exc}", file=sys.stderr)
        print("[恢复] 已保留进度；修复问题后再次运行相同命令", file=sys.stderr)
        return 1
    finally:
        store.release_lease()


if __name__ == "__main__":
    raise SystemExit(main())
