#!/usr/bin/env python3
"""Black-box Windows gate for the native desktop application backend.

The gate deliberately uses an unreachable loopback PACS. A successful gate
means that the real backend migrated a legacy Profile, exercised the expected
download failure path, shut down cleanly, and released its Storage SCP port.
It does not claim PACS interoperability or a successful C-MOVE.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

PROFILE_ID = "i7"
PROFILE_NAME = "CI 旧版 Profile"
ACCESSION = "CI-NO-PACS-0001"
REPORT_SCHEMA_VERSION = 1
EXPECTED_FAILURE_PHASES = {"download_retryable", "failed"}


class GateError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reserve_unreachable_loopback_port() -> tuple[socket.socket, int]:
    """Hold a bound, non-listening port so a PACS connection is refused."""

    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        guard.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    guard.bind(("127.0.0.1", 0))
    return guard, int(guard.getsockname()[1])


def find_available_storage_port(excluded: set[int]) -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("0.0.0.0", 0))
            port = int(probe.getsockname()[1])
        if port not in excluded:
            return port


def assert_port_released(port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("0.0.0.0", port))
    except OSError as exc:
        raise GateError(f"Storage SCP 端口 {port} 在 desktop 退出后仍未释放：{exc}") from exc


def write_legacy_profile(
    appdata: Path,
    destination: Path,
    *,
    pacs_port: int,
    storage_port: int,
) -> Path:
    profile_root = appdata / "DcmGet" / "instances" / PROFILE_ID
    profile_root.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    config_path = profile_root / "config.json"
    config = {
        "config_version": 8,
        "dicom_destination_folder": str(destination),
        "pacs_server_ip": "127.0.0.1",
        "pacs_server_port": pacs_port,
        "calling_ae_title": "DCMGET_CI",
        "pacs_ae_title": "PACS_CI",
        "storage_ae_title": "DCMGET_CI",
        "storage_port": storage_port,
        "directory_template": "{AccessionNumber}/{StudyInstanceUID}",
        "pdi_export_enabled": False,
        "minimum_free_space_bytes": 0,
        "auto_retry_attempts": 0,
        "auto_retry_backoff_seconds": 0,
        "circuit_breaker_failures": 2,
        "max_log_file_size_bytes": 1_048_576,
    }
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (profile_root / "profile-meta.json").write_text(
        json.dumps(
            {
                "schema": "dcmget-profile-meta",
                "version": 1,
                "display_name": PROFILE_NAME,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


def read_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise GateError(f"desktop 未生成 backend smoke 报告：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"backend smoke 报告不是有效 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise GateError("backend smoke 报告根节点必须是对象")
    return value


def require_report_contract(report: dict[str, Any], storage_port: int) -> None:
    expected_values = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "profile_id": PROFILE_ID,
        "legacy_source_loaded": True,
        "failure_observed": True,
        "receiver_started": True,
        "receiver_stopped": True,
        "port_released": True,
        "shutdown_complete": True,
        "storage_port": storage_port,
    }
    mismatches = [
        f"{key}={report.get(key)!r}，预期 {expected!r}"
        for key, expected in expected_values.items()
        if type(report.get(key)) is not type(expected) or report.get(key) != expected
    ]
    profile_count = report.get("profile_count")
    if type(profile_count) is not int or profile_count < 1:
        mismatches.append(f"profile_count={profile_count!r}，预期至少为 1")
    phase = report.get("task_phase")
    if not isinstance(phase, str) or phase not in EXPECTED_FAILURE_PHASES:
        mismatches.append(
            f"task_phase={phase!r}，预期属于 {sorted(EXPECTED_FAILURE_PHASES)!r}"
        )
    if mismatches:
        raise GateError("backend smoke 报告不满足契约：\n- " + "\n- ".join(mismatches))


def require_persisted_state(localappdata: Path, storage_port: int) -> Path:
    database = localappdata / "DcmGet" / "native" / "state.sqlite3"
    if not database.is_file():
        raise GateError(f"未生成原生状态库：{database}")
    with sqlite3.connect(database) as connection:
        profile = connection.execute(
            "SELECT display_name,config_json,source_config_path "
            "FROM profiles WHERE profile_id=?",
            (PROFILE_ID,),
        ).fetchone()
        if profile is None:
            raise GateError(f"状态库未迁移 Profile {PROFILE_ID}")
        display_name, raw_config, source_config_path = profile
        if display_name != PROFILE_NAME:
            raise GateError(f"迁移后的 Profile 名称错误：{display_name!r}")
        config = json.loads(raw_config)
        if config.get("storage_port") != storage_port:
            raise GateError("状态库中的 storage_port 与旧配置 fixture 不一致")
        if not source_config_path:
            raise GateError("迁移后的 Profile 未保留只读旧配置来源路径")

        task = connection.execute(
            "SELECT task_id,phase FROM tasks WHERE profile_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (PROFILE_ID,),
        ).fetchone()
        if task is None:
            raise GateError("backend smoke 没有通过 ApplicationService 持久化任务")
        task_id, phase = task
        if phase not in EXPECTED_FAILURE_PHASES:
            raise GateError(f"任务 {task_id} 的失败阶段不正确：{phase!r}")
        accession = connection.execute(
            "SELECT status,result_json FROM accessions "
            "WHERE task_id=? AND accession=?",
            (task_id, ACCESSION),
        ).fetchone()
        if accession is None or not accession[0] or not accession[1]:
            raise GateError("无 PACS 失败路径没有持久化检查号结果")
    return database


def require_migration_backup(localappdata: Path) -> None:
    backup_root = localappdata / "DcmGet" / "native" / "backups"
    copied_configs = list(backup_root.glob("legacy-*/*-config.json"))
    if not copied_configs:
        raise GateError(f"旧配置迁移前未生成不可变备份：{backup_root}")


def require_diagnostic_log(localappdata: Path) -> Path:
    log_path = (
        localappdata / "DcmGet" / "native" / "logs" / "dcmget-native.log"
    )
    if not log_path.is_file():
        raise GateError(f"未生成原生诊断日志：{log_path}")
    content = log_path.read_text(encoding="utf-8", errors="replace")
    missing = [
        marker
        for marker in ("SESSION START", "SESSION BACKEND STOPPED", "SESSION NORMAL EXIT")
        if marker not in content
    ]
    if missing:
        raise GateError("原生诊断日志缺少生命周期标记：" + ", ".join(missing))
    return log_path


def run_gate(desktop: Path, work_root: Path, timeout_seconds: int) -> dict[str, Any]:
    if not desktop.is_file():
        raise GateError(f"desktop 文件不存在：{desktop}")
    if work_root.exists():
        if not work_root.is_dir():
            raise GateError(f"--work-root 不是目录：{work_root}")
        if any(work_root.iterdir()):
            raise GateError(f"--work-root 必须为空，避免复用旧 smoke 状态：{work_root}")
    work_root.mkdir(parents=True, exist_ok=True)
    appdata = work_root / "Roaming"
    localappdata = work_root / "Local"
    destination = work_root / "DICOM 输出"
    report_path = work_root / "backend-smoke-report.json"
    appdata.mkdir(parents=True, exist_ok=True)
    localappdata.mkdir(parents=True, exist_ok=True)

    pacs_guard, pacs_port = reserve_unreachable_loopback_port()
    storage_port = find_available_storage_port({pacs_port})
    config_path = write_legacy_profile(
        appdata,
        destination,
        pacs_port=pacs_port,
        storage_port=storage_port,
    )
    original_config_hash = sha256_file(config_path)
    environment = os.environ.copy()
    environment.update(
        {
            "APPDATA": str(appdata),
            "LOCALAPPDATA": str(localappdata),
            "DCMGET_BACKEND_SMOKE": "1",
            "RUST_LOG": "dcmget=debug",
        }
    )
    command = [
        str(desktop),
        "--backend-smoke",
        "--exit-after-ready",
        "--backend-smoke-report",
        str(report_path),
        "--backend-smoke-accession",
        ACCESSION,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=desktop.parent,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GateError(f"backend smoke 超过 {timeout_seconds} 秒仍未退出") from exc
    finally:
        pacs_guard.close()

    (work_root / "desktop-stdout.log").write_text(
        completed.stdout or "", encoding="utf-8"
    )
    (work_root / "desktop-stderr.log").write_text(
        completed.stderr or "", encoding="utf-8"
    )
    if completed.returncode != 0:
        raise GateError(
            "backend smoke 进程失败："
            f"exit={completed.returncode}; stdout={completed.stdout[-2000:]!r}; "
            f"stderr={completed.stderr[-2000:]!r}"
        )

    report = read_report(report_path)
    require_report_contract(report, storage_port)
    if sha256_file(config_path) != original_config_hash:
        raise GateError("原生迁移修改了旧版 config.json")
    database = require_persisted_state(localappdata, storage_port)
    require_migration_backup(localappdata)
    diagnostic_log = require_diagnostic_log(localappdata)
    assert_port_released(storage_port)
    unexpected_payloads = [
        path
        for path in destination.rglob("*")
        if path.is_file() and path.suffix.lower() in {".dcm", ".part"}
    ]
    if unexpected_payloads:
        raise GateError(
            "无 PACS smoke 不应生成影像数据："
            + ", ".join(str(path) for path in unexpected_payloads)
        )
    return {
        "profile_id": PROFILE_ID,
        "storage_port": storage_port,
        "task_phase": report["task_phase"],
        "state_database": str(database),
        "diagnostic_log": str(diagnostic_log),
        "legacy_config_unchanged": True,
        "port_released_out_of_process": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--desktop", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    arguments = parser.parse_args()
    if arguments.timeout_seconds < 10:
        raise GateError("--timeout-seconds 至少为 10")
    summary = run_gate(
        arguments.desktop.resolve(),
        arguments.work_root.resolve(),
        arguments.timeout_seconds,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateError as exc:
        print(f"Native backend gate failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
