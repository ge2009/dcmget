from __future__ import annotations

import argparse
import getpass
import os
import signal
import sys

from dcmget.config import load_accessions, load_config
from dcmget.core import DcmtkResolver, DownloadRunner, preflight
from dcmget.licensing import (
    LicenseError,
    consume_trial,
    default_license_path,
    load_license,
    machine_code,
    trial_status,
    validate_daily_password,
)
from dcmget.runtime import ensure_default_config, resource_root


PROJECT_ROOT = resource_root()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DcmGet 2.1 DICOM 批量下载工具")
    parser.add_argument(
        "--config",
        default=str(ensure_default_config()),
        help="配置文件路径（默认：项目目录/config.json）",
    )
    parser.add_argument("--accessions", help="覆盖配置中的检查号 TXT 文件路径")
    parser.add_argument("--password", help=argparse.SUPPRESS)
    parser.add_argument("--license", help="注册码文件路径")
    return parser


def authorize_cli(password: str | None, license_path: str | None) -> str | None:
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
    authorization = authorize_cli(args.password, args.license)
    if authorization is None:
        return 1
    try:
        config = load_config(args.config)
        accession_path = args.accessions or config.access_numbers_file_path
        parsed = load_accessions(accession_path)
    except (OSError, ValueError) as exc:
        print(f"配置或检查号文件错误：{exc}", file=sys.stderr)
        return 1

    if not parsed.values:
        print("检查号列表为空。", file=sys.stderr)
        return 1

    check = preflight(config, DcmtkResolver(PROJECT_ROOT))
    for name, ok, message in check.checks:
        marker = "通过" if ok else "失败"
        print(f"[{marker}] {name}：{message}")
    if not check.ok or check.tools is None:
        return 1

    def consume_trial_when_ready() -> None:
        trial = consume_trial()
        print(f"[授权] 本次使用免费试用，剩余 {trial.remaining} 次")

    runner = DownloadRunner(
        config,
        check.tools,
        log_callback=lambda source, message, _level: print(f"[{source}] {message}"),
        state_callback=lambda state: print(f"[状态] {state}"),
        progress_callback=lambda index, total, result: print(
            f"[{index}/{total}] {result.accession}：{result.status.value}，{result.file_count} 个文件"
        ),
        ready_callback=(consume_trial_when_ready if authorization == "trial" else None),
    )

    def cancel(_signum: int, _frame: object) -> None:
        runner.request_cancel()

    signal.signal(signal.SIGINT, cancel)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, cancel)

    try:
        return runner.run(parsed.values).exit_code
    except (OSError, LicenseError, RuntimeError, TimeoutError) as exc:
        print(f"下载启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
