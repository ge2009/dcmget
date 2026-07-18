#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import uuid
import zipfile
from pathlib import Path

try:
    from scripts.download_dcmtk import IntegrityError, validate_installation
    from scripts.prepare_ohif import load_manifest, payload_is_current
except ModuleNotFoundError:  # direct execution from the scripts directory
    from download_dcmtk import IntegrityError, validate_installation
    from prepare_ohif import load_manifest, payload_is_current


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dcmget.windows_portable_runtime import (  # noqa: E402
    MANIFEST_NAME as PORTABLE_RUNTIME_MANIFEST_NAME,
    create_portable_runtime_manifest,
)
from dcmget.architecture import (  # noqa: E402
    ensure_supported_runtime,
    require_amd64_pe,
)


BUILD_ROOT = ROOT / "build" / "windows"
DIST_ROOT = BUILD_ROOT / "dist"
RELEASE_ROOT = ROOT / "release" / "windows"
PLATFORM_RUNTIME = ROOT / ".runtime" / "dcmtk" / "windows-x86_64"
OHIF_RUNTIME = ROOT / ".runtime" / "ohif" / "ohif-3.12.6"
MINIMAL_DCMTK_RUNTIME = BUILD_ROOT / "dcmtk-runtime"
DCMTK_PACKAGE_DIRECTORY = "dcmtk-3.7.0-win64-dynamic"
WINDOWS_DCMTK_PE_FILES = (
    "movescu.exe",
    "storescp.exe",
    "dcmmkdir.exe",
    "dcmdump.exe",
    "dcmdata.dll",
    "dcmimage.dll",
    "dcmimgle.dll",
    "dcmjpeg.dll",
    "dcmnet.dll",
    "dcmtls.dll",
    "ijg8.dll",
    "ijg12.dll",
    "ijg16.dll",
    "oficonv.dll",
    "oflog.dll",
    "ofstd.dll",
)
WINDOWS_DCMTK_DATA_DIRECTORIES = (
    "share/dcmtk-3.7.0/csmapper",
    "share/dcmtk-3.7.0/esdb",
)
WINDOWS_DCMTK_DATA_FILES = (
    "share/dcmtk-3.7.0/dicom.dic",
    "share/doc/dcmtk-3.7.0/COPYRIGHT",
    "share/doc/dcmtk-3.7.0/VERSION",
)


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


def find_dcmtk_bin(runtime: Path = PLATFORM_RUNTIME) -> Path:
    try:
        return validate_installation(runtime, "windows-x86_64")
    except IntegrityError as exc:
        raise FileNotFoundError(
            f"Windows DCMTK 完整性校验失败：{exc}。请先运行 "
            "scripts/download_dcmtk.py --platform windows-x86_64"
        ) from exc


def find_ohif_payload() -> Path:
    if not payload_is_current(OHIF_RUNTIME, load_manifest()):
        raise FileNotFoundError(
            "未找到通过逐文件 SHA-256 校验的 OHIF Viewer 3.12.6 离线资源。请先运行 "
            "scripts/prepare_ohif.py"
        )
    return OHIF_RUNTIME


def stage_minimal_windows_dcmtk(
    source_runtime: str | Path,
    destination: str | Path = MINIMAL_DCMTK_RUNTIME,
) -> tuple[Path, Path]:
    """Copy the verified OFFIS runtime subset used by DcmGet releases."""

    source = Path(source_runtime).expanduser().resolve()
    package = source / DCMTK_PACKAGE_DIRECTORY
    selected: list[Path] = [
        Path(DCMTK_PACKAGE_DIRECTORY) / "bin" / name
        for name in WINDOWS_DCMTK_PE_FILES
    ]
    selected.extend(
        Path(DCMTK_PACKAGE_DIRECTORY) / relative
        for relative in WINDOWS_DCMTK_DATA_FILES
    )
    for relative in WINDOWS_DCMTK_DATA_DIRECTORIES:
        directory = package / relative
        if not directory.is_dir() or directory.is_symlink():
            raise FileNotFoundError(f"DCMTK 字符集目录缺失：{directory}")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise RuntimeError(f"DCMTK 最小运行时不允许符号链接：{path}")
            if path.is_file():
                selected.append(path.relative_to(source))
            elif not path.is_dir():
                raise RuntimeError(f"DCMTK 最小运行时包含特殊文件：{path}")

    selected = sorted(dict.fromkeys(selected))
    missing = [relative for relative in selected if not (source / relative).is_file()]
    if missing:
        raise FileNotFoundError(
            "DCMTK 最小运行时缺少文件：" + "、".join(str(path) for path in missing)
        )

    output = Path(destination).expanduser().resolve()
    temporary = output.with_name(f".{output.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        if temporary.exists():
            shutil.rmtree(temporary)
        for relative in selected:
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
        actual = sorted(
            path.relative_to(temporary)
            for path in temporary.rglob("*")
            if path.is_file()
        )
        if actual != selected:
            raise RuntimeError("DCMTK 最小运行时文件集合校验失败")
        if output.exists():
            shutil.rmtree(output)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return output, output / DCMTK_PACKAGE_DIRECTORY / "bin"


def make_icon() -> Path:
    from PIL import Image

    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    icon = BUILD_ROOT / "dcmget.ico"
    with Image.open(ROOT / "logo.png") as source:
        source.convert("RGBA").save(
            icon,
            format="ICO",
            sizes=[
                (16, 16),
                (24, 24),
                (32, 32),
                (48, 48),
                (64, 64),
                (128, 128),
                (256, 256),
            ],
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
    ohif_root: Path | None = None,
    pdi_server_executable: Path | None = None,
    portable_runtime_manifest: Path | None = None,
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
        "--collect-submodules",
        "pynetdicom",
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
        f"{ROOT / 'dcmget' / 'pdi_server.py'}:dcmget",
        "--add-data",
        f"{ROOT / 'dcmget' / 'architecture.py'}:dcmget",
        "--add-data",
        f"{runtime_root}:.runtime/dcmtk/windows-x86_64",
        "--noupx",
    ]
    if ohif_root is not None:
        arguments.extend(
            [
                "--add-data",
                f"{ohif_root}:.runtime/ohif/ohif-3.12.6",
            ]
        )
    if pdi_server_executable is not None:
        arguments.extend(
            [
                "--add-data",
                f"{pdi_server_executable}:.",
            ]
        )
    if portable_runtime_manifest is not None:
        arguments.extend(
            [
                "--add-data",
                f"{portable_runtime_manifest}:.",
            ]
        )
    return arguments


def pdi_server_pyinstaller_args(
    icon: Path,
    version_file: Path,
) -> list[str]:
    return [
        str(ROOT / "tools" / "dcmget_pdi_server.py"),
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onefile",
        "--name",
        "DcmGetPdiServer",
        "--icon",
        str(icon),
        "--version-file",
        str(version_file),
        "--distpath",
        str(DIST_ROOT),
        "--workpath",
        str(BUILD_ROOT / "work" / "DcmGetPdiServer"),
        "--specpath",
        str(BUILD_ROOT / "spec"),
        "--paths",
        str(ROOT),
        "--noupx",
    ]


def verify_built_architecture(version: str) -> tuple[Path, ...]:
    """Verify that every executable entry point and DCMTK transport is AMD64."""

    expected = [
        DIST_ROOT / "DcmGet" / "DcmGet.exe",
        DIST_ROOT / "DcmGetPdiServer.exe",
        RELEASE_ROOT / f"DcmGet-{version}-windows-x64-portable.exe",
    ]
    for root in (MINIMAL_DCMTK_RUNTIME, DIST_ROOT / "DcmGet"):
        for name in WINDOWS_DCMTK_PE_FILES:
            matches = sorted(root.rglob(name)) if root.is_dir() else []
            if not matches:
                expected.append(root / name)
            else:
                expected.extend(matches)

    missing = [path for path in expected if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Windows x64 架构校验缺少文件：" + "、".join(str(path) for path in missing)
        )

    unique = tuple(dict.fromkeys(path.resolve() for path in expected))
    for path in unique:
        require_amd64_pe(path, path.name)
    verify_packaged_dcmtk_tree(
        DIST_ROOT
        / "DcmGet"
        / "_internal"
        / ".runtime"
        / "dcmtk"
        / "windows-x86_64"
    )
    return unique


def verify_packaged_dcmtk_tree(
    packaged_runtime: str | Path,
    reference_runtime: str | Path = MINIMAL_DCMTK_RUNTIME,
) -> None:
    reference = Path(reference_runtime).resolve()
    packaged = Path(packaged_runtime).resolve()
    expected = {
        path.relative_to(reference): (path.stat().st_size, file_sha256(path))
        for path in reference.rglob("*")
        if path.is_file()
    }
    actual = {
        path.relative_to(packaged): (path.stat().st_size, file_sha256(path))
        for path in packaged.rglob("*")
        if path.is_file()
    }
    if not expected:
        raise FileNotFoundError(f"DCMTK 最小运行时为空：{reference}")
    if actual != expected:
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        changed = sorted(
            path
            for path in expected.keys() & actual.keys()
            if expected[path] != actual[path]
        )
        detail = missing[:1] or extra[:1] or changed[:1]
        raise RuntimeError(
            "Windows 发布包 DCMTK 文件集合或内容不匹配"
            + (f"：{detail[0]}" if detail else "")
        )


def sign_windows_payloads(paths: list[Path]) -> str:
    """Sign embedded Windows payloads before they are copied or zipped."""

    try:
        from scripts.windows_release_gate import (
            AuthenticodeConfig,
            SignatureStatus,
            sign_windows_files,
        )
    except ModuleNotFoundError:
        from windows_release_gate import (  # type: ignore[no-redef]
            AuthenticodeConfig,
            SignatureStatus,
            sign_windows_files,
        )

    config = AuthenticodeConfig.from_environment()
    statuses = sign_windows_files(paths, config)
    status = (
        SignatureStatus.SIGNED if config.configured else SignatureStatus.UNSIGNED
    )
    if set(statuses.values()) != {status}:
        raise RuntimeError("Windows 内嵌程序签名状态不一致")
    print(f"WINDOWS_PAYLOAD_SIGNING_STATUS={status.value}")
    return status.value


def build_payloads(version: str) -> None:
    if os.name != "nt":
        raise SystemExit("Windows 可执行文件必须在 Windows 上使用 PyInstaller 构建")
    ensure_supported_runtime()
    dcmtk_bin = find_dcmtk_bin()
    for name in WINDOWS_DCMTK_PE_FILES:
        require_amd64_pe(dcmtk_bin / name, f"DCMTK {name}")
    ohif = find_ohif_payload()
    from PyInstaller.__main__ import run as run_pyinstaller

    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    if RELEASE_ROOT.exists():
        shutil.rmtree(RELEASE_ROOT)
    RELEASE_ROOT.mkdir(parents=True)
    icon = make_icon()
    version_file = make_version_file(version)
    minimal_runtime, minimal_bin = stage_minimal_windows_dcmtk(PLATFORM_RUNTIME)
    portable_runtime_manifest = create_portable_runtime_manifest(
        minimal_runtime,
        minimal_bin,
        BUILD_ROOT / PORTABLE_RUNTIME_MANIFEST_NAME,
    )

    run_pyinstaller(pdi_server_pyinstaller_args(icon, version_file))
    pdi_server = DIST_ROOT / "DcmGetPdiServer.exe"
    if not pdi_server.is_file():
        raise FileNotFoundError("PDI 本地阅片服务 DcmGetPdiServer.exe 构建失败")
    sign_windows_payloads([pdi_server])

    run_pyinstaller(
        pyinstaller_args(
            "DcmGet",
            "--onedir",
            icon,
            version_file,
            minimal_runtime,
            ohif,
            pdi_server,
        )
    )
    run_pyinstaller(
        pyinstaller_args(
            "DcmGet-Portable",
            "--onefile",
            icon,
            version_file,
            minimal_runtime,
            ohif,
            pdi_server,
            portable_runtime_manifest,
        )
    )

    application = DIST_ROOT / "DcmGet" / "DcmGet.exe"
    portable_payload = DIST_ROOT / "DcmGet-Portable.exe"
    sign_windows_payloads([application, portable_payload])

    portable = RELEASE_ROOT / f"DcmGet-{version}-windows-x64-portable.exe"
    shutil.copy2(portable_payload, portable)
    archive = RELEASE_ROOT / f"DcmGet-{version}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        for path in sorted((DIST_ROOT / "DcmGet").rglob("*")):
            if path.is_file():
                package.write(path, Path("DcmGet") / path.relative_to(DIST_ROOT / "DcmGet"))
    verify_built_architecture(version)


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
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--checksums-only", action="store_true")
    actions.add_argument("--verify-architecture-only", action="store_true")
    args = parser.parse_args()
    if args.verify_architecture_only:
        verified = verify_built_architecture(args.version)
        print("\n".join(str(path) for path in verified))
        return 0
    if not args.checksums_only:
        build_payloads(args.version)
    output = write_checksums()
    print(output)
    print(output.read_text(encoding="ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
