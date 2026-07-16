#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

try:
    from scripts.prepare_weasis import load_manifest, payload_is_current
except ModuleNotFoundError:  # direct execution from the scripts directory
    from prepare_weasis import load_manifest, payload_is_current


ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build" / "windows"
DIST_ROOT = BUILD_ROOT / "dist"
RELEASE_ROOT = ROOT / "release" / "windows"
PLATFORM_RUNTIME = ROOT / ".runtime" / "dcmtk" / "windows-x86_64"
WEASIS_RUNTIME = ROOT / ".runtime" / "weasis" / "windows-x86_64" / "Weasis"


def source_version() -> str:
    text = (ROOT / "dcmget" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\'](\d+\.\d+\.\d+)["\']', text, re.MULTILINE)
    if not match:
        raise RuntimeError("无法从 dcmget/__init__.py 读取版本号")
    return match.group(1)


APP_VERSION = source_version()


def validate_version(value: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise argparse.ArgumentTypeError("版本必须采用 X.Y.Z 格式")
    return value


def validate_release_version(value: str) -> str:
    version = validate_version(value)
    if version != APP_VERSION:
        raise argparse.ArgumentTypeError(
            f"发布版本 {version} 与源码版本 {APP_VERSION} 不一致"
        )
    return version


def version_tuple(version: str) -> tuple[int, int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch, 0


def find_dcmtk_bin() -> Path:
    for movescu in PLATFORM_RUNTIME.rglob("movescu.exe"):
        required = (
            "storescp.exe",
            "dcmmkdir.exe",
            "dcmj2pnm.exe",
            "dcmdjpeg.exe",
            "dcmdump.exe",
        )
        if all((movescu.parent / name).is_file() for name in required):
            return movescu.parent
    raise FileNotFoundError(
        "未找到完整的 Windows DCMTK/PDI 工具。请先运行 "
        "scripts/download_dcmtk.py --platform windows-x86_64"
    )


def find_weasis_payload() -> Path:
    if not payload_is_current(WEASIS_RUNTIME, load_manifest()):
        raise FileNotFoundError(
            "未找到通过逐文件 SHA-256 校验的 Weasis 4.7.1 Windows 便携查看器。请先运行 "
            "scripts/prepare_weasis.py --platform windows-x86_64"
        )
    return WEASIS_RUNTIME


def make_icon() -> Path:
    from PIL import Image

    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    icon = BUILD_ROOT / "dcmget.ico"
    with Image.open(ROOT / "logo.png") as source:
        source.convert("RGBA").save(
            icon,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)],
        )
    return icon


def make_version_file(version: str) -> Path:
    numbers = version_tuple(version)
    path = BUILD_ROOT / "version_info.txt"
    path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0,
    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404b0', [
      StringStruct('CompanyName', 'DcmGet contributors'),
      StringStruct('FileDescription', 'DcmGet DICOM 下载工作台'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'DcmGet'),
      StringStruct('OriginalFilename', 'DcmGet.exe'),
      StringStruct('ProductName', 'DcmGet'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])])
""",
        encoding="utf-8",
    )
    return path


def pyinstaller_args(
    name: str,
    mode: str,
    icon: Path,
    version_file: Path,
    runtime_root: Path,
    weasis_root: Path | None = None,
) -> list[str]:
    arguments = [
        str(ROOT / "DICOM_download_ui.py"),
        "--noconfirm",
        "--clean",
        "--windowed",
        mode,
        "--name",
        name,
        "--icon",
        str(icon),
        "--version-file",
        str(version_file),
        "--distpath",
        str(DIST_ROOT),
        "--workpath",
        str(BUILD_ROOT / "work" / name),
        "--specpath",
        str(BUILD_ROOT / "spec"),
        "--paths",
        str(ROOT),
        "--add-data",
        f"{ROOT / 'logo.png'}:.",
        "--add-data",
        f"{ROOT / 'config.example.json'}:.",
        "--add-data",
        f"{ROOT / 'README.md'}:.",
        "--add-data",
        f"{ROOT / 'CHANGELOG.md'}:.",
        "--add-data",
        f"{ROOT / 'LICENSE'}:.",
        "--add-data",
        f"{ROOT / 'THIRD_PARTY_NOTICES.md'}:.",
        "--add-data",
        f"{runtime_root}:.runtime/dcmtk/windows-x86_64",
        "--noupx",
    ]
    if weasis_root is not None:
        arguments.extend(
            [
                "--add-data",
                f"{weasis_root}:.runtime/weasis/windows-x86_64/Weasis",
            ]
        )
    return arguments


def build_payloads(version: str) -> None:
    if os.name != "nt":
        raise SystemExit("Windows 可执行文件必须在 Windows 上使用 PyInstaller 构建")
    find_dcmtk_bin()
    weasis = find_weasis_payload()
    from PyInstaller.__main__ import run as run_pyinstaller

    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    if RELEASE_ROOT.exists():
        shutil.rmtree(RELEASE_ROOT)
    RELEASE_ROOT.mkdir(parents=True)
    icon = make_icon()
    version_file = make_version_file(version)

    run_pyinstaller(
        pyinstaller_args(
            "DcmGet", "--onedir", icon, version_file, PLATFORM_RUNTIME, weasis
        )
    )
    run_pyinstaller(
        pyinstaller_args(
            "DcmGet-Portable", "--onefile", icon, version_file, PLATFORM_RUNTIME
        )
    )

    portable = RELEASE_ROOT / f"DcmGet-{version}-windows-x64-portable.exe"
    shutil.copy2(DIST_ROOT / "DcmGet-Portable.exe", portable)
    archive = RELEASE_ROOT / f"DcmGet-{version}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        for path in sorted((DIST_ROOT / "DcmGet").rglob("*")):
            if path.is_file():
                package.write(path, Path("DcmGet") / path.relative_to(DIST_ROOT / "DcmGet"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_checksums() -> Path:
    artifacts = sorted(
        path
        for path in RELEASE_ROOT.iterdir()
        if path.is_file() and path.suffix.lower() in {".exe", ".zip"}
    )
    if not artifacts:
        raise FileNotFoundError("release/windows 中没有可校验的 Windows 发布物")
    output = RELEASE_ROOT / "SHA256SUMS.txt"
    with output.open("w", encoding="ascii", newline="\n") as handle:
        handle.write(
            "".join(f"{file_sha256(path)}  {path.name}\n" for path in artifacts)
        )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 DcmGet Windows EXE 发布物")
    parser.add_argument(
        "--version", default=APP_VERSION, type=validate_release_version
    )
    parser.add_argument("--checksums-only", action="store_true")
    args = parser.parse_args()
    if not args.checksums_only:
        build_payloads(args.version)
    output = write_checksums()
    print(output)
    print(output.read_text(encoding="ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
