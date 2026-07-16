from __future__ import annotations

import argparse
import getpass
import os
import signal
import sys
import threading

from dcmget.config import load_accessions, load_config
from dcmget.core import (
    AccessionStatus,
    BatchSummary,
    DcmtkResolver,
    DownloadRunner,
    preflight,
)
from dcmget.licensing import (
    LicenseError,
    consume_trial,
    default_license_path,
    load_license,
    machine_code,
    trial_status,
    trial_task_consumed,
    validate_daily_password,
)
from dcmget.runtime import ensure_default_config, resource_root
from dcmget.pdi import PdiExporter, PdiStatus
from dcmget.task_state import (
    TaskCheckpointStore,
    TaskStateError,
    merge_checkpoint_summary,
)


PROJECT_ROOT = resource_root()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DcmGet 2.5 DICOM 批量下载工具")
    parser.add_argument(
        "--config",
        default=str(ensure_default_config()),
        help="配置文件路径（默认：项目目录/config.json）",
    )
    parser.add_argument("--accessions", help="覆盖配置中的检查号 TXT 文件路径")
    parser.add_argument("--password", help=argparse.SUPPRESS)
    parser.add_argument("--license", help="注册码文件路径")
    parser.add_argument(
        "--discard-checkpoint",
        action="store_true",
        help="放弃未完成任务恢复点并开始新任务（不删除已下载文件）",
    )
    parser.add_argument("--task-state", help=argparse.SUPPRESS)
    return parser


def authorize_cli(
    password: str | None,
    license_path: str | None,
    resume_task_id: str | None = None,
) -> str | None:
    value = password or os.environ.get("DCMGET_DAILY_PASSWORD", "")
    if not value and sys.stdin.isatty():
        value = getpass.getpass("当天口令：")
    if not validate_daily_password(value):
        print(
            "当天口令不正确；非交互运行请设置 DCMGET_DAILY_PASSWORD。",
            file=sys.stderr,
        )
        return None
    try:
        load_license(license_path)
        return "licensed"
    except (OSError, LicenseError) as exc:
        trial = trial_status()
        if trial.remaining > 0:
            return "trial"
        if resume_task_id and trial_task_consumed(resume_task_id):
            return "trial"
        path = license_path or str(default_license_path())
        print(f"授权失败：{exc}", file=sys.stderr)
        print("30 次免费试用已用完。", file=sys.stderr)
        print(f"本机机器码：{machine_code()}", file=sys.stderr)
        print(f"请将有效注册码保存到：{path}", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    store = TaskCheckpointStore(args.task_state)
    checkpoint = None
    if store.path.is_file():
        if not store.try_acquire_lease():
            print("已有 DcmGet 实例正在使用未完成任务。", file=sys.stderr)
            return 1
        try:
            if args.discard_checkpoint:
                store.clear()
                print("已放弃旧任务恢复点；已下载文件保持不变。")
            else:
                checkpoint = store.load()
                if checkpoint is not None:
                    for message in store.cleanup_recorded_processes(
                        checkpoint.task_id
                    ):
                        print(f"[恢复] {message}")
        except TaskStateError as exc:
            store.release_lease()
            print(f"任务恢复点错误：{exc}", file=sys.stderr)
            return 1

    authorization = authorize_cli(
        args.password,
        args.license,
        checkpoint.task_id if checkpoint is not None else None,
    )
    if authorization is None:
        store.release_lease()
        return 1
    try:
        if checkpoint is not None:
            config = checkpoint.config
            accessions = checkpoint.pending_accessions
            print(
                f"[恢复] 任务 {checkpoint.task_id[:8]}："
                f"已处理 {len(checkpoint.results)}/{len(checkpoint.accessions)}，"
                f"剩余 {len(accessions)}"
            )
        else:
            config = load_config(args.config)
            accession_path = args.accessions or config.access_numbers_file_path
            parsed = load_accessions(accession_path)
            accessions = parsed.values
    except (OSError, ValueError) as exc:
        store.release_lease()
        print(f"配置或检查号文件错误：{exc}", file=sys.stderr)
        return 1

    if checkpoint is None and not accessions:
        store.release_lease()
        print("检查号列表为空。", file=sys.stderr)
        return 1
    if (
        checkpoint is not None
        and checkpoint.phase != "downloading"
        and accessions
    ):
        store.release_lease()
        print("任务恢复点阶段与未完成检查号不一致。", file=sys.stderr)
        return 1

    resolver = DcmtkResolver(PROJECT_ROOT)
    download_needed = checkpoint is None or bool(accessions)
    if download_needed:
        check = preflight(config, resolver)
        for name, ok, message in check.checks:
            marker = "通过" if ok else "失败"
            print(f"[{marker}] {name}：{message}")
        if not check.ok or check.tools is None:
            store.release_lease()
            return 1
        tools = check.tools
    else:
        try:
            tools = resolver.resolve(config.dcmtk_bin_dir)
        except (OSError, RuntimeError) as exc:
            store.release_lease()
            print(f"DCMTK 检测失败：{exc}", file=sys.stderr)
            return 1

    if checkpoint is None:
        if not store.lease_held and not store.try_acquire_lease():
            print("另一个 DcmGet 实例正在启动任务。", file=sys.stderr)
            return 1
        try:
            checkpoint = store.start(
                config,
                accessions,
                trial_required=authorization == "trial",
            )
        except TaskStateError as exc:
            store.release_lease()
            print(f"无法建立任务恢复点：{exc}", file=sys.stderr)
            return 1

    task_id = checkpoint.task_id
    offset = len(checkpoint.results)

    def report_progress(index, total, result) -> None:
        persisted = store.record_result(task_id, result)
        print(
            f"[{offset + index}/{len(checkpoint.accessions)}] "
            f"{persisted.accession}：{persisted.status.value}，"
            f"{persisted.file_count} 个文件"
        )

    def consume_trial_when_ready() -> None:
        trial = consume_trial(task_id=task_id)
        print(f"[授权] 本次使用免费试用，剩余 {trial.remaining} 次")

    runner: DownloadRunner | None = None
    exporter: PdiExporter | None = None
    cancel_requested = threading.Event()

    def cancel(_signum: int, _frame: object) -> None:
        cancel_requested.set()
        if runner is not None:
            runner.request_cancel()
        if exporter is not None:
            exporter.request_cancel()

    signal.signal(signal.SIGINT, cancel)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, cancel)

    try:
        if accessions:
            runner = DownloadRunner(
                config,
                tools,
                log_callback=lambda source, message, _level: print(
                    f"[{source}] {message}"
                ),
                state_callback=lambda state: print(f"[状态] {state}"),
                progress_callback=report_progress,
                ready_callback=(
                    consume_trial_when_ready if authorization == "trial" else None
                ),
                process_callback=lambda kind, pid, executable, active: store.record_process(
                    task_id,
                    kind,
                    pid,
                    executable,
                    active=active,
                ),
            )
            current_summary = runner.run(accessions)
            persisted = store.load_required()
            final_accessions = {result.accession for result in persisted.results}
            partial_accessions = set(persisted.partial_results)
            for result in current_summary.results:
                if (
                    result.status == AccessionStatus.CANCELLED
                    and bool(result.archived_files)
                    and result.accession not in partial_accessions
                ) or (
                    result.status != AccessionStatus.CANCELLED
                    and result.accession not in final_accessions
                ):
                    store.record_result(task_id, result)
            checkpoint = store.load_required()
            summary = merge_checkpoint_summary(checkpoint, current_summary)
        else:
            summary = BatchSummary(list(checkpoint.results))
        if summary.cancelled or cancel_requested.is_set():
            return 130
        download_exit_code = summary.exit_code
        if not config.pdi_export_enabled:
            store.clear(task_id)
            return download_exit_code
        if not summary.archived_files:
            print("[PDI] 当前批次没有已归档 DICOM 文件，跳过便携目录导出。")
            store.clear(task_id)
            return download_exit_code

        pdi_attempt_id, reuse_published_pdi = store.begin_pdi_attempt(
            task_id,
            reuse_existing=checkpoint.phase == "pdi_running",
        )
        exporter = PdiExporter(
            config,
            tools,
            project_root=PROJECT_ROOT,
            log_callback=lambda source, message, _level: print(
                f"[{source}] {message}"
            ),
            progress_callback=lambda stage, current, total, message: print(
                f"[PDI {stage.value}] {current}/{total} {message}"
            ),
            process_callback=lambda kind, pid, executable, active: store.record_process(
                task_id,
                kind,
                pid,
                executable,
                active=active,
            ),
            recovery_id=pdi_attempt_id,
            reuse_published=reuse_published_pdi,
        )
        if cancel_requested.is_set():
            exporter.request_cancel()
        pdi_result = exporter.export(summary.archived_files)
        if pdi_result.output_directory:
            print(f"[PDI] 输出目录：{pdi_result.output_directory}")
        if pdi_result.status == PdiStatus.CANCELLED:
            store.set_phase(task_id, "pdi_retryable")
            return 130
        if pdi_result.core_tool_failure:
            store.set_phase(task_id, "pdi_retryable")
            return 1
        if pdi_result.status in {PdiStatus.PARTIAL, PdiStatus.FAILED}:
            store.set_phase(task_id, "pdi_retryable")
            return 2
        store.clear(task_id)
        return download_exit_code
    except (OSError, LicenseError, RuntimeError, TaskStateError, TimeoutError) as exc:
        print(f"下载启动失败：{exc}", file=sys.stderr)
        return 1
    finally:
        store.release_lease()


if __name__ == "__main__":
    raise SystemExit(main())
