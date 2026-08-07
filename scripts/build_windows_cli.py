#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

try:
    from scripts.download_dcmtk import IntegrityError, validate_installation
    from scripts.windows_release_gate import (
        AuthenticodeConfig,
        SignatureStatus,
        sign_windows_files,
    )
except ModuleNotFoundError:  # direct execution from scripts/
    from download_dcmtk import IntegrityError, validate_installation
    from windows_release_gate import (
        AuthenticodeConfig,
        SignatureStatus,
        sign_windows_files,
    )


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dcmget.architecture import ensure_supported_runtime, require_amd64_pe

BUILD_ROOT = ROOT / "build" / "windows-cli"
DIST_ROOT = BUILD_ROOT / "dist"
PACKAGE_ROOT = BUILD_ROOT / "package" / "DcmGetCLI"
RELEASE_ROOT = ROOT / "release" / "windows-cli"
PLATFORM_RUNTIME = ROOT / ".runtime" / "dcmtk" / "windows-x86_64"
PACKAGE_DIRECTORY = "DcmGetCLI"
DCMTK_BIN_FILES = (
    "movescu.exe",
    "storescp.exe",
    "dcmdump.exe",
    "dcmtls.dll",
    "dcmnet.dll",
    "dcmdata.dll",
    "oflog.dll",
    "ofstd.dll",
    "oficonv.dll",
)
DCMTK_DATA_FILES = (
    "share/dcmtk-3.7.0/dicom.dic",
    "share/doc/dcmtk-3.7.0/COPYRIGHT",
    "share/doc/dcmtk-3.7.0/VERSION",
)
PACKAGE_SOURCE_FILES = {
    "config.json": ROOT / "packaging" / "cli" / "config.json",
    "access.txt": ROOT / "packaging" / "cli" / "access.txt",
    "README.txt": ROOT / "packaging" / "cli" / "README.txt",
    "LICENSE": ROOT / "LICENSE",
    "THIRD_PARTY_NOTICES.md": ROOT / "packaging" / "cli" / "THIRD_PARTY_NOTICES.md",
}
PACKAGE_GENERATED_FILES = ("DcmGetCLI.exe", "FILES-SHA256.txt")


def source_version() -> str:
    text = (ROOT / "dcmget" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(
        r'^__version__\s*=\s*["\'](\d+\.\d+\.\d+)["\']',
        text,
        re.MULTILINE,
    )
    if not match:
        raise RuntimeError("无法读取 DcmGetCLI 源码版本")
    return match.group(1)


APP_VERSION = source_version()


def validate_release_version(value: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise argparse.ArgumentTypeError("版本必须采用 X.Y.Z 格式")
    if value != APP_VERSION:
        raise argparse.ArgumentTypeError(
            f"发布版本 {value} 与源码版本 {APP_VERSION} 不一致"
        )
    return value


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def find_dcmtk_bin() -> Path:
    try:
        return validate_installation(PLATFORM_RUNTIME, "windows-x86_64")
    except IntegrityError as exc:
        raise FileNotFoundError(
            f"Windows DCMTK 完整性校验失败：{exc}；请先运行 "
            "scripts/download_dcmtk.py --platform windows-x86_64"
        ) from exc


def make_icon() -> Path:
    from PIL import Image

    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    output = BUILD_ROOT / "dcmget-cli.ico"
    with Image.open(ROOT / "logo.png") as source:
        source.convert("RGBA").save(
            output,
            format="ICO",
            sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
    return output


def make_version_file(version: str) -> Path:
    major, minor, patch = (int(part) for part in version.split("."))
    output = BUILD_ROOT / "version_info.txt"
    output.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, 0),
    prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404b0', [
      StringStruct('CompanyName', 'DcmGet contributors'),
      StringStruct('FileDescription', 'DcmGetCLI 独立 DICOM 下载器'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'DcmGetCLI'),
      StringStruct('OriginalFilename', 'DcmGetCLI.exe'),
      StringStruct('ProductName', 'DcmGetCLI'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])])
""",
        encoding="utf-8",
    )
    return output


def pyinstaller_args(icon: Path, version_file: Path) -> list[str]:
    return [
        str(ROOT / "DICOM_download_cli.py"),
        "--noconfirm",
        "--clean",
        "--console",
        "--onefile",
        "--name",
        "DcmGetCLI",
        "--icon",
        str(icon),
        "--version-file",
        str(version_file),
        "--distpath",
        str(DIST_ROOT),
        "--workpath",
        str(BUILD_ROOT / "work"),
        "--specpath",
        str(BUILD_ROOT / "spec"),
        "--paths",
        str(ROOT),
        "--exclude-module",
        "DICOM_download_script",
        "--exclude-module",
        "DICOM_download_ui",
        "--exclude-module",
        "cryptography",
        "--exclude-module",
        "dcmget.anonymization",
        "--exclude-module",
        "dcmget.licensing",
        "--exclude-module",
        "dcmget.pdi",
        "--exclude-module",
        "dcmget.pdi_server",
        "--exclude-module",
        "dcmget.web_server",
        "--exclude-module",
        "dcmget.windows_update",
        "--exclude-module",
        "fastapi",
        "--exclude-module",
        "pynetdicom",
        "--exclude-module",
        "uvicorn",
        "--exclude-module",
        "webview",
        "--noupx",
    ]


def stage_dcmtk(source_bin: Path, destination: Path) -> None:
    package_root = source_bin.parent
    selected = [Path("bin") / name for name in DCMTK_BIN_FILES]
    selected.extend(Path(value) for value in DCMTK_DATA_FILES)
    missing = [
        relative
        for relative in selected
        if not (package_root / relative).is_file()
    ]
    if missing:
        raise FileNotFoundError(
            "独立 CLI 的 DCMTK 运行时缺少文件：" + "、".join(map(str, missing))
        )
    for relative in selected:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(package_root / relative, target)


def expected_package_files() -> tuple[str, ...]:
    """Return the exact standalone ZIP payload allowlist."""

    files = [*PACKAGE_GENERATED_FILES, *PACKAGE_SOURCE_FILES]
    files.extend(f"dcmtk/bin/{name}" for name in DCMTK_BIN_FILES)
    files.extend(f"dcmtk/{name}" for name in DCMTK_DATA_FILES)
    return tuple(sorted(files))


def validate_package_tree(package_root: Path) -> tuple[Path, ...]:
    """Reject links, special files, and anything outside the CLI allowlist."""

    actual: dict[str, Path] = {}
    for path in sorted(package_root.rglob("*")):
        relative = path.relative_to(package_root).as_posix()
        if path.is_symlink():
            raise RuntimeError(f"CLI 发布目录不允许符号链接：{relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimeError(f"CLI 发布目录包含特殊文件：{relative}")
        key = relative.casefold()
        if key in actual:
            raise RuntimeError(f"CLI 发布目录包含大小写冲突路径：{relative}")
        actual[key] = path

    expected = {name.casefold(): name for name in expected_package_files()}
    missing = sorted(expected[key] for key in expected.keys() - actual.keys())
    extra = sorted(
        path.relative_to(package_root).as_posix()
        for key, path in actual.items()
        if key not in expected
    )
    if missing or extra:
        details = []
        if missing:
            details.append("缺少 " + "、".join(missing))
        if extra:
            details.append("多出 " + "、".join(extra))
        raise RuntimeError("CLI 发布目录不符合白名单：" + "；".join(details))
    return tuple(actual[name.casefold()] for name in expected_package_files())


def write_package_checksums(package_root: Path) -> Path:
    records = [
        path
        for path in sorted(package_root.rglob("*"))
        if path.is_file() and path.name != "FILES-SHA256.txt"
    ]
    output = package_root / "FILES-SHA256.txt"
    output.write_text(
        "".join(
            f"{file_sha256(path)}  {path.relative_to(package_root).as_posix()}\n"
            for path in records
        ),
        encoding="ascii",
        newline="\n",
    )
    return output


def write_package_archive(package_root: Path, archive: Path) -> Path:
    files = validate_package_tree(package_root)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        archive,
        "w",
        zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as package:
        for path in files:
            relative = path.relative_to(package_root).as_posix()
            package.write(path, f"{PACKAGE_DIRECTORY}/{relative}")
    verify_package_archive(archive)
    return archive


def verify_package_archive(archive: Path) -> None:
    """Verify the archive itself, not just the directory used to create it."""

    expected = {}
    for relative in expected_package_files():
        packaged = f"{PACKAGE_DIRECTORY}/{relative}"
        expected[packaged.casefold()] = packaged
    actual: dict[str, str] = {}
    with zipfile.ZipFile(archive) as package:
        for info in package.infolist():
            if info.is_dir():
                raise RuntimeError(f"CLI ZIP 不应包含目录记录：{info.filename}")
            pure = PurePosixPath(info.filename.replace("\\", "/"))
            if pure.is_absolute() or ".." in pure.parts or not pure.parts:
                raise RuntimeError(f"CLI ZIP 包含不安全路径：{info.filename}")
            unix_mode = info.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise RuntimeError(f"CLI ZIP 不允许符号链接：{info.filename}")
            normalized = pure.as_posix()
            key = normalized.casefold()
            if key in actual:
                raise RuntimeError(f"CLI ZIP 包含重复路径：{normalized}")
            actual[key] = normalized

    missing = sorted(expected[key] for key in expected.keys() - actual.keys())
    extra = sorted(actual[key] for key in actual.keys() - expected.keys())
    if missing or extra:
        details = []
        if missing:
            details.append("缺少 " + "、".join(missing))
        if extra:
            details.append("多出 " + "、".join(extra))
        raise RuntimeError("CLI ZIP 不符合白名单：" + "；".join(details))


def build(version: str) -> tuple[Path, Path]:
    if os.name != "nt":
        raise SystemExit("Windows x64 独立 CLI 必须在 Windows 上构建")
    ensure_supported_runtime()
    from PyInstaller.__main__ import run as run_pyinstaller

    source_bin = find_dcmtk_bin()
    for name in DCMTK_BIN_FILES:
        require_amd64_pe(source_bin / name, f"DCMTK {name}")

    shutil.rmtree(BUILD_ROOT, ignore_errors=True)
    shutil.rmtree(RELEASE_ROOT, ignore_errors=True)
    RELEASE_ROOT.mkdir(parents=True)
    run_pyinstaller(pyinstaller_args(make_icon(), make_version_file(version)))
    executable = DIST_ROOT / "DcmGetCLI.exe"
    if not executable.is_file():
        raise FileNotFoundError("DcmGetCLI.exe 构建失败")
    require_amd64_pe(executable, "DcmGetCLI.exe")

    signing = AuthenticodeConfig.from_environment()
    statuses = sign_windows_files([executable], signing)
    expected = SignatureStatus.SIGNED if signing.configured else SignatureStatus.UNSIGNED
    if set(statuses.values()) != {expected}:
        raise RuntimeError("DcmGetCLI 签名状态不一致")
    print(f"WINDOWS_CLI_SIGNING_STATUS={expected.value}")

    PACKAGE_ROOT.mkdir(parents=True)
    shutil.copy2(executable, PACKAGE_ROOT / executable.name)
    for name, source in PACKAGE_SOURCE_FILES.items():
        if not source.is_file():
            raise FileNotFoundError(f"CLI 发布文件缺失：{source}")
        shutil.copy2(source, PACKAGE_ROOT / name)
    stage_dcmtk(source_bin, PACKAGE_ROOT / "dcmtk")
    write_package_checksums(PACKAGE_ROOT)

    archive = RELEASE_ROOT / f"DcmGetCLI-{version}-windows-x64.zip"
    write_package_archive(PACKAGE_ROOT, archive)
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    checksum.write_text(
        f"{file_sha256(archive)}  {archive.name}\n",
        encoding="ascii",
        newline="\n",
    )
    return archive, checksum


def main() -> int:
    parser = argparse.ArgumentParser(description="构建独立 DcmGetCLI Windows x64 ZIP")
    parser.add_argument("--version", default=APP_VERSION, type=validate_release_version)
    args = parser.parse_args()
    archive, checksum = build(args.version)
    print(archive)
    print(checksum.read_text(encoding="ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
