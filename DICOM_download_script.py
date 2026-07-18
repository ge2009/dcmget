from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from dcmget import __version__
from dcmget.accession_import import (
    AccessionImportResult,
    ColumnSelectionError,
    import_accession_file,
)
from dcmget.architecture import ArchitectureError, ensure_supported_runtime
from dcmget.config import load_config
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
)
from dcmget.instance_profile import (
    InstanceProfileError,
    MIGRATION_MARKER_NAME,
    PROFILE_DIRECTORY_NAME,
    _read_migration_marker,
)
from dcmget.runtime import application_state_dir, ensure_default_config, resource_root
from dcmget.pdi import (
    PdiExporter,
    PdiStatus,
    PdiVolumeExporter,
    cleanup_interrupted_pdi,
)
from dcmget.task_state import (
    TaskCheckpointStore,
    TaskStateError,
    merge_checkpoint_summary,
)
from dcmget.task_ledger import TaskLedger, TaskLedgerError


PROJECT_ROOT = resource_root()


def default_legacy_catalog_was_migrated() -> bool:
    """Return whether the default 2.8 catalog was migrated into 2.9 profiles."""

    state_root = application_state_dir().expanduser().resolve()
    marker_path = (
        state_root / PROFILE_DIRECTORY_NAME / MIGRATION_MARKER_NAME
    )
    if not marker_path.is_file():
        return False
    _read_migration_marker(marker_path, state_root / "tasks.sqlite3")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"DcmGet {__version__} DICOM 批量下载工具"
    )
    parser.add_argument(
        "--config",
        default=str(ensure_default_config()),
        help="配置文件路径（默认：项目目录/config.json）",
    )
    parser.add_argument(
        "--accessions",
        help="覆盖配置中的检查号 TXT、CSV 或 XLSX 文件路径",
    )
    parser.add_argument(
        "--accession-column",
        metavar="NAME_OR_INDEX",
        help="CSV/XLSX 检查号列名或从 0 开始的列序号；多列表格必须指定",
    )
    parser.add_argument(
        "--task-id",
        help="恢复 tasks.sqlite3 中指定的未完成或可重试任务",
    )
    parser.add_argument("--password", help=argparse.SUPPRESS)
    parser.add_argument("--license", help="注册码文件路径")
    parser.add_argument(
        "--discard-checkpoint",
        action="store_true",
        help="放弃未完成任务恢复点并开始新任务（不删除已下载文件）",
    )
    parser.add_argument(
        "--accept-download-failures",
        action="store_true",
        help="接受恢复任务中的下载失败，保留现有文件并继续 PDI 或结束任务",
    )
    parser.add_argument("--task-state", help=argparse.SUPPRESS)
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument(
        "--health-check",
        action="store_true",
        help="运行本机环境、DCMTK、目录、端口和进程健康检查",
    )
    maintenance.add_argument(
        "--support-bundle",
        metavar="ZIP",
        help="生成默认脱敏的诊断支持包",
    )
    maintenance.add_argument(
        "--backup-profiles",
        metavar="ZIP",
        help="备份本机全部 Profile 配置与显示名（不含授权和试用信息）",
    )
    maintenance.add_argument(
        "--restore-profiles",
        metavar="ZIP",
        help="校验并恢复 Profile 配置，恢复前自动创建快照",
    )
    maintenance.add_argument(
        "--verify-pdi",
        metavar="DIRECTORY",
        help="校验已复制到 U 盘或其他介质的 PDI 完整性",
    )
    return parser


def load_cli_accessions(
    path: str | Path,
    column_argument: str | None,
) -> AccessionImportResult:
    """Load CLI accessions without guessing among multiple table columns."""

    column: str | int | None = column_argument
    if column_argument is not None:
        candidate = column_argument.strip()
        if not candidate:
            raise ColumnSelectionError("--accession-column 不能为空")
        column = int(candidate) if candidate.isdecimal() else candidate
    try:
        result = import_accession_file(path, column=column)
    except ColumnSelectionError as exc:
        if column_argument is None and exc.columns:
            choices = "、".join(
                f"{item.index}:{item.name}" for item in exc.columns
            )
            raise ColumnSelectionError(
                f"{exc}；可选列为 {choices}。请使用 --accession-column 明确指定",
                exc.columns,
            ) from exc
        raise
    if column_argument is None and len(result.available_columns) > 1:
        choices = "、".join(
            f"{item.index}:{item.name}" for item in result.available_columns
        )
        raise ColumnSelectionError(
            "表格包含多列，命令行不会自动选择检查号列；"
            f"可选列为 {choices}。请使用 --accession-column 明确指定",
            result.available_columns,
        )
    return result


def run_maintenance_command(args: argparse.Namespace) -> int | None:
    if not any(
        (
            args.health_check,
            args.support_bundle,
            args.backup_profiles,
            args.restore_profiles,
            args.verify_pdi,
        )
    ):
        return None
    try:
        if args.backup_profiles:
            from dcmget.profile_backup import create_profile_backup

            result = create_profile_backup(args.backup_profiles)
            print(f"Profile 配置与显示名备份已生成：{result.path}")
            print("包含 Profile：" + "、".join(map(str, result.profile_numbers)))
            return 0
        if args.restore_profiles:
            from dcmget.profile_backup import restore_profile_backup

            result = restore_profile_backup(args.restore_profiles)
            print(
                "Profile 配置与显示名已恢复："
                + "、".join(map(str, result.profile_numbers))
            )
            if result.previous_backup:
                print(f"恢复前快照：{result.previous_backup}")
            return 0
        if args.verify_pdi:
            from dcmget.pdi_verify import (
                PdiVerificationStatus,
                discover_pdi_verification_roots,
                pdi_delivery_report_output_directory,
                verify_pdi_directory,
                write_pdi_delivery_reports,
            )

            roots = discover_pdi_verification_roots(args.verify_pdi)
            results = []
            for index, root in enumerate(roots, start=1):
                result = verify_pdi_directory(root)
                report_directory = pdi_delivery_report_output_directory(
                    args.verify_pdi,
                    root,
                    len(roots),
                )
                reports = write_pdi_delivery_reports(
                    result,
                    report_directory,
                )
                results.append(result)
                prefix = f"第 {index}/{len(roots)} 卷：" if len(roots) > 1 else ""
                print(prefix + result.message)
                print(f"PDI 验收报告：{reports.html_path}")
            if any(
                result.status == PdiVerificationStatus.CANCELLED
                for result in results
            ):
                return 130
            return (
                0
                if all(
                    result.status == PdiVerificationStatus.PASSED
                    for result in results
                )
                else 2
            )

        config = load_config(args.config)
        if args.health_check:
            from dcmget.health import run_health_check

            report = run_health_check(
                config,
                project_root=PROJECT_ROOT,
                minimum_free_bytes=config.minimum_free_space_bytes,
                check_pacs=True,
            )
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
            return 0 if report.status != "error" else 1
        if args.support_bundle:
            from dcmget.support_bundle import create_support_bundle

            result = create_support_bundle(
                args.support_bundle,
                config,
                project_root=PROJECT_ROOT,
            )
            print(f"脱敏支持包已生成：{result.path}")
            return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"维护操作失败：{exc}", file=sys.stderr)
        return 1
    return None


def authorize_cli(
    password: str | None,
    license_path: str | None,
    resume_task_id: str | None = None,
) -> str | None:
    del password  # 保留旧命令参数兼容性，但不再进行日期口令验证。
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
    try:
        ensure_supported_runtime()
    except ArchitectureError as exc:
        print(f"运行环境不受支持：{exc}", file=sys.stderr)
        return 1
    args = build_parser().parse_args(argv)
    maintenance_exit = run_maintenance_command(args)
    if maintenance_exit is not None:
        return maintenance_exit
    if args.task_state:
        if args.task_id:
            print(
                "--task-id 不能与旧版 --task-state 同时使用。",
                file=sys.stderr,
            )
            return 1
        store = TaskCheckpointStore(args.task_state)
    else:
        try:
            catalog_was_migrated = default_legacy_catalog_was_migrated()
        except InstanceProfileError as exc:
            print(f"无法校验旧任务迁移状态：{exc}", file=sys.stderr)
            return 1
        if catalog_was_migrated:
            print(
                "默认旧任务目录 tasks.sqlite3 已迁移到 DcmGet 2.9 实例恢复点。"
                "为避免重复下载，命令行不会再次打开该目录；请使用桌面 GUI 恢复，"
                "或显式指定 --task-state PATH 运行独立单任务。",
                file=sys.stderr,
            )
            return 1
        try:
            from dcmget.cli_tasks import (
                CatalogCheckpointStore,
                MultipleTasksError,
                format_task_list,
                select_cli_task,
            )
            from dcmget.task_manager import TaskCatalog
        except ImportError as exc:
            print(f"任务恢复组件不可用：{exc}", file=sys.stderr)
            return 1
        try:
            catalog = TaskCatalog()
            selected = select_cli_task(catalog, args.task_id)
            store = CatalogCheckpointStore(
                catalog,
                selected.task_id if selected is not None else None,
            )
        except MultipleTasksError as exc:
            print(str(exc), file=sys.stderr)
            for line in format_task_list(exc.tasks):
                print(line, file=sys.stderr)
            return 1
        except TaskStateError as exc:
            print(f"任务选择失败：{exc}", file=sys.stderr)
            return 1
    checkpoint = None
    startup_cleanup_done = False
    has_checkpoint = getattr(store, "has_checkpoint", store.path.is_file())
    if has_checkpoint:
        if not store.try_acquire_lease():
            print("已有 DcmGet 实例正在使用未完成任务。", file=sys.stderr)
            return 1
        try:
            cleanup_all = getattr(store, "cleanup_startup_processes", None)
            if callable(cleanup_all):
                for message in cleanup_all():
                    print(f"[恢复] {message}")
                startup_cleanup_done = True
            prepare_selected_retry = getattr(
                store,
                "prepare_selected_retry",
                None,
            )
            if callable(prepare_selected_retry) and not args.discard_checkpoint:
                prepare_selected_retry()
            if args.discard_checkpoint:
                try:
                    discarded = store.load()
                except TaskStateError as exc:
                    discarded = None
                    print(
                        f"[恢复] 恢复点损坏，无法定位其中的 PDI 暂存目录：{exc}",
                        file=sys.stderr,
                    )
                if discarded is not None and discarded.pdi_attempt_id:
                    try:
                        removed = cleanup_interrupted_pdi(
                            discarded.config,
                            discarded.pdi_attempt_id,
                        )
                    except (OSError, ValueError) as exc:
                        store.release_lease()
                        print(f"无法放弃 PDI 恢复：{exc}", file=sys.stderr)
                        return 1
                    for path in removed:
                        print(f"[PDI] 已删除中断的暂存目录：{path}")
                if discarded is not None and not startup_cleanup_done:
                    for message in store.cleanup_recorded_processes(
                        discarded.task_id
                    ):
                        print(f"[恢复] {message}")
                store.clear()
                print("已放弃旧任务恢复点；已下载文件保持不变。")
            else:
                checkpoint = store.load()
                if checkpoint is not None and not startup_cleanup_done:
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
    accepting_download_failures = False
    try:
        if args.accept_download_failures and (
            checkpoint is None or checkpoint.phase != "download_retryable"
        ):
            raise ValueError("当前没有可接受的下载失败恢复任务")
        if checkpoint is not None:
            if checkpoint.phase == "download_retryable":
                accepting_download_failures = bool(args.accept_download_failures)
                if accepting_download_failures:
                    if checkpoint.interrupted_reason or checkpoint.pending_accessions:
                        raise ValueError(
                            "安全暂停任务仍有未处理检查号，不能直接接受当前结果；"
                            "请先继续任务"
                        )
                    print("[恢复] 已接受当前下载结果，不再重试失败项")
                else:
                    interrupted_reason = checkpoint.interrupted_reason
                    checkpoint = store.prepare_download_retry(checkpoint.task_id)
                    if interrupted_reason:
                        print(f"[恢复] 安全暂停原因：{interrupted_reason}")
                        print("[恢复] 正在继续未处理项并重试已有失败项")
                    else:
                        print("[恢复] 正在重试上次失败和部分成功的检查号")
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
            parsed = load_cli_accessions(accession_path, args.accession_column)
            if parsed.invalid_values:
                examples = "、".join(parsed.invalid_values[:3])
                raise ValueError(
                    "检查号不能包含 DICOM 通配符 *、?、反斜杠或控制字符："
                    + examples
                )
            accessions = parsed.values
    except (OSError, ValueError, TaskStateError) as exc:
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
            cleanup_all = getattr(store, "cleanup_startup_processes", None)
            if callable(cleanup_all) and not startup_cleanup_done:
                for message in cleanup_all():
                    print(f"[恢复] {message}")
                startup_cleanup_done = True
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
    try:
        ledger = TaskLedger(Path(store.path).expanduser().with_name("task-ledger.sqlite3"))
        try:
            ledger.load_batch(task_id)
        except TaskLedgerError as exc:
            if "不存在当前批次" not in str(exc):
                raise
            ledger.create_batch(
                checkpoint.accessions,
                batch_id=task_id,
                profile_name=Path(args.config).stem,
                anonymization_requested=config.anonymization_enabled,
                pdi_requested=config.pdi_export_enabled,
            )
    except TaskLedgerError as exc:
        store.release_lease()
        print(f"无法建立任务台账：{exc}", file=sys.stderr)
        return 1

    def export_acceptance_report(*, complete_status: str | None = None) -> None:
        try:
            if complete_status:
                ledger.complete_batch(task_id, complete_status)
            report_root = (
                Path(config.dicom_destination_folder).expanduser()
                / "_DcmGetReports"
                / f"task-{task_id[:8]}"
            )
            report = ledger.export_reports(task_id, report_root)
            print(f"[验收] 脱敏验收报告：{report.html_path}")
        except (OSError, TaskLedgerError) as exc:
            print(f"[验收] 报告生成失败：{exc}", file=sys.stderr)

    def record_audit(result, observations) -> None:
        if not config.anonymization_enabled:
            anonymization_status = "not_requested"
        elif result.archived_files:
            anonymization_status = "completed"
        elif result.status == AccessionStatus.NO_DATA:
            anonymization_status = "no_data"
        else:
            anonymization_status = "failed"
        ledger.record_runner_result(
            task_id,
            result,
            observed_instances=observations,
            anonymization_status=anonymization_status,
        )

    def report_progress(index, total, result) -> None:
        persisted = store.record_result(task_id, result)
        print(
            f"[{offset + index}/{len(checkpoint.accessions)}] "
            f"{persisted.accession}：{persisted.status.value}，"
            f"{persisted.file_count} 个文件"
        )

    def consume_trial_when_ready() -> None:
        trial = consume_trial(task_id=task_id)
        mark_consumed = getattr(store, "mark_trial_consumed", None)
        if mark_consumed is not None:
            mark_consumed(task_id)
        print(f"[授权] 本次使用免费试用，剩余 {trial.remaining} 次")

    runner: DownloadRunner | None = None
    exporter: PdiExporter | PdiVolumeExporter | None = None
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
            store.set_phase(task_id, "downloading")
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
                audit_callback=record_audit,
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
            export_acceptance_report()
            return 130
        download_exit_code = summary.exit_code
        if download_exit_code == 2 and not accepting_download_failures:
            store.set_phase(
                task_id,
                "download_retryable",
                interrupted_reason=summary.interrupted_reason,
            )
            if summary.interrupted_reason:
                pending_count = len(store.load_required().pending_accessions)
                print(f"[安全暂停] {summary.interrupted_reason}")
                print(
                    f"[恢复] 尚有 {pending_count} 个检查号待处理；"
                    "修复问题后再次启动即可继续。"
                )
            else:
                print("[恢复] 失败项已保留，下次启动将只重试失败和部分成功项。")
            export_acceptance_report()
            return 2
        if not config.pdi_export_enabled:
            store.clear(task_id)
            export_acceptance_report(
                complete_status=(
                    "accepted_partial" if download_exit_code == 2 else "completed"
                )
            )
            return download_exit_code
        if not summary.archived_files:
            print("[PDI] 当前批次没有已归档 DICOM 文件，跳过便携目录导出。")
            store.clear(task_id)
            export_acceptance_report(
                complete_status=(
                    "accepted_partial" if download_exit_code == 2 else "completed"
                )
            )
            return download_exit_code

        pdi_attempt_id, reuse_published_pdi = store.begin_pdi_attempt(
            task_id,
            reuse_existing=checkpoint.phase == "pdi_running",
        )
        exporter_type = (
            PdiVolumeExporter
            if config.pdi_volume_size_bytes > 0
            else PdiExporter
        )
        exporter = exporter_type(
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
        save_pdi_result = getattr(store, "save_pdi_result", None)
        if callable(save_pdi_result):
            save_pdi_result(task_id, pdi_result)
        if pdi_result.output_directory:
            print(f"[PDI] 输出目录：{pdi_result.output_directory}")
        try:
            ledger.record_pdi_result(
                task_id,
                pdi_result.status,
                output_directory=pdi_result.output_directory,
                message=pdi_result.message,
            )
        except TaskLedgerError as exc:
            print(f"[验收] PDI 台账更新失败：{exc}", file=sys.stderr)
        if pdi_result.status == PdiStatus.CANCELLED:
            store.set_phase(task_id, "pdi_retryable")
            export_acceptance_report(complete_status="cancelled")
            return 130
        if pdi_result.core_tool_failure:
            store.set_phase(task_id, "pdi_retryable")
            export_acceptance_report(complete_status="failed")
            return 1
        if pdi_result.status in {PdiStatus.PARTIAL, PdiStatus.FAILED}:
            store.set_phase(task_id, "pdi_retryable")
            export_acceptance_report(
                complete_status=(
                    "partial"
                    if pdi_result.status == PdiStatus.PARTIAL
                    else "failed"
                )
            )
            return 2
        store.clear(task_id)
        export_acceptance_report(
            complete_status=(
                "accepted_partial" if download_exit_code == 2 else "completed"
            )
        )
        return download_exit_code
    except (OSError, LicenseError, RuntimeError, TaskStateError, TimeoutError) as exc:
        print(f"下载启动失败：{exc}", file=sys.stderr)
        return 1
    finally:
        store.release_lease()


if __name__ == "__main__":
    raise SystemExit(main())
