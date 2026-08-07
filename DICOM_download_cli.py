from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import replace
from pathlib import Path

import DICOM_download_script
from dcmget import __version__
from dcmget.config import load_config, save_config
from dcmget.runtime import (
    application_state_dir,
    default_config_path,
    ensure_default_config,
    is_frozen,
    set_portable_dcmtk_bin,
)


DEFAULT_PROFILE_NUMBER = 1
CLI_STATE_DIRECTORY = "cli-tasks"
PACKAGED_DCMTK_BIN = (
    "_internal",
    ".runtime",
    "dcmtk",
    "windows-x86_64",
    "dcmtk-3.7.0-win64-dynamic",
    "bin",
)


def _profile_number(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Profile 编号必须是正整数") from exc
    if not 1 <= number <= 9999:
        raise argparse.ArgumentTypeError("Profile 编号必须在 1 到 9999 之间")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            f"DcmGet {__version__} 纯命令行下载器；"
            "默认读取实例 1 配置且不生成 PDI"
        )
    )
    parser.add_argument(
        "accession_file",
        metavar="ACCESS.TXT",
        help="检查号 TXT、CSV 或 XLSX 文件",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--profile",
        type=_profile_number,
        help="读取指定 Profile 配置（默认：1）",
    )
    source.add_argument(
        "--config",
        help="改用指定 config.json，不读取 Profile 配置",
    )
    parser.add_argument(
        "--accession-column",
        metavar="NAME_OR_INDEX",
        help="CSV/XLSX 检查号列名或从 0 开始的列序号",
    )
    parser.add_argument("--license", help="注册码文件路径")
    parser.add_argument(
        "--task-state",
        help="覆盖默认的独立 CLI 恢复点路径",
    )
    parser.add_argument(
        "--discard-checkpoint",
        action="store_true",
        help="放弃未完成 CLI 任务并从当前检查号文件重新开始",
    )
    parser.add_argument(
        "--accept-download-failures",
        action="store_true",
        help="接受恢复任务中的下载失败并结束任务",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"DcmGetCLI {__version__}",
    )
    return parser


def resolve_source_config(
    *,
    config_path: str | None,
    profile_number: int | None,
) -> tuple[Path, str]:
    if config_path:
        selected = Path(config_path).expanduser()
        if not selected.is_file():
            raise FileNotFoundError(f"配置文件不存在：{selected}")
        return selected.resolve(), f"config:{os.path.normcase(str(selected.resolve()))}"

    number = profile_number or DEFAULT_PROFILE_NUMBER
    profile_config = (
        default_config_path().parent
        / "instances"
        / f"i{number}"
        / "config.json"
    )
    if profile_config.is_file():
        return profile_config.resolve(), f"profile:{number}"
    if profile_number is not None:
        raise FileNotFoundError(
            f"Profile {number} 尚未配置；请先在 DcmGet 工作台创建并保存该 Profile"
        )
    return ensure_default_config().resolve(), "default-config"


def _state_slot(identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"task-{digest}"


def prepare_cli_config(
    source_config: Path,
    identity: str,
) -> tuple[Path, Path]:
    state_directory = application_state_dir() / CLI_STATE_DIRECTORY / _state_slot(identity)
    state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_config = state_directory / "config.json"
    task_state = state_directory / "active-task.sqlite3"
    config = replace(load_config(source_config), pdi_export_enabled=False)
    save_config(runtime_config, config)
    return runtime_config, task_state


def configure_packaged_dcmtk() -> Path | None:
    if sys.platform != "win32" or not is_frozen():
        return None
    candidate = Path(sys.executable).resolve().parent.joinpath(*PACKAGED_DCMTK_BIN)
    if not (candidate / "movescu.exe").is_file() or not (
        candidate / "storescp.exe"
    ).is_file():
        return None
    set_portable_dcmtk_bin(candidate)
    return candidate


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source_config, identity = resolve_source_config(
            config_path=args.config,
            profile_number=args.profile,
        )
        runtime_config, default_task_state = prepare_cli_config(
            source_config,
            identity,
        )
    except (OSError, ValueError) as exc:
        print(f"CLI 配置错误：{exc}", file=sys.stderr)
        return 1

    configure_packaged_dcmtk()
    task_state = (
        Path(args.task_state).expanduser()
        if args.task_state
        else default_task_state
    )
    forwarded = [
        "--config",
        str(runtime_config),
        "--accessions",
        args.accession_file,
        "--task-state",
        str(task_state),
    ]
    if args.accession_column:
        forwarded.extend(["--accession-column", args.accession_column])
    if args.license:
        forwarded.extend(["--license", args.license])
    if args.discard_checkpoint:
        forwarded.append("--discard-checkpoint")
    if args.accept_download_failures:
        forwarded.append("--accept-download-failures")

    print(f"DcmGetCLI {__version__}")
    print(f"配置：{source_config}")
    print(f"检查号文件：{Path(args.accession_file).expanduser()}")
    print("PDI：已关闭（纯下载模式）")
    return DICOM_download_script.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
