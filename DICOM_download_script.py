from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from dcmget.config import load_accessions, load_config
from dcmget.core import DcmtkResolver, DownloadRunner, preflight


PROJECT_ROOT = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DcmGet 2.0 DICOM 批量下载工具")
    parser.add_argument(
        "--config",
        default=str(PROJECT_ROOT / "config.json"),
        help="配置文件路径（默认：项目目录/config.json）",
    )
    parser.add_argument("--accessions", help="覆盖配置中的检查号 TXT 文件路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
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

    runner = DownloadRunner(
        config,
        check.tools,
        log_callback=lambda source, message, _level: print(f"[{source}] {message}"),
        state_callback=lambda state: print(f"[状态] {state}"),
        progress_callback=lambda index, total, result: print(
            f"[{index}/{total}] {result.accession}：{result.status.value}，{result.file_count} 个文件"
        ),
    )

    def cancel(_signum: int, _frame: object) -> None:
        runner.request_cancel()

    signal.signal(signal.SIGINT, cancel)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, cancel)

    try:
        return runner.run(parsed.values).exit_code
    except (OSError, RuntimeError, TimeoutError) as exc:
        print(f"下载启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
