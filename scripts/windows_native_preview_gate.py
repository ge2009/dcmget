#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

IMAGE_FILE_MACHINE_AMD64 = 0x8664


FORBIDDEN_NAME_PATTERN = re.compile(r"python|dcmtk|movescu|storescp|tauri", re.IGNORECASE)
FORBIDDEN_EXTENSIONS = {".py", ".pyc"}


class GateError(RuntimeError):
    pass


class ArchitectureError(RuntimeError):
    pass


def ensure_file(path: Path, *, reason: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise GateError(f"{reason} 文件不存在或类型无效：{path}")


def check_forbidden_content(root: Path) -> list[Path]:
    forbidden: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in FORBIDDEN_EXTENSIONS:
            forbidden.append(path)
            continue
        if FORBIDDEN_NAME_PATTERN.search(path.name):
            forbidden.append(path)
    return forbidden


def check_portable_root(portable_root: Path, *, require_binary_names: tuple[str, ...]) -> None:
    if not portable_root.is_dir() or portable_root.is_symlink():
        raise GateError(f"portable 目录不可用：{portable_root}")

    missing = [name for name in require_binary_names if not (portable_root / name).is_file()]
    if missing:
        raise GateError(f"portable 缺少预期产物：{', '.join(sorted(missing))}")

    forbidden = check_forbidden_content(portable_root)
    if forbidden:
        raise GateError(
            "发现禁用内容："
            + "\n- "
            + "\n- ".join(str(path) for path in sorted(forbidden))
        )

    for name in require_binary_names:
        try:
            require_amd64_pe(portable_root / name, f"portable 文件 {name}")
        except ArchitectureError as exc:
            raise GateError(f"{exc}") from exc


def require_amd64_pe(path: Path, description: str) -> None:
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ArchitectureError(f"{description} 不是有效的 Windows PE 文件：{path}")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset < 0x40 or len(data) < pe_offset + 6:
        raise ArchitectureError(f"{description} 的 PE 头偏移无效：{path}")
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ArchitectureError(f"{description} 的 PE 签名无效：{path}")
    machine = struct.unpack_from("<H", data, pe_offset + 4)[0]
    if machine != IMAGE_FILE_MACHINE_AMD64:
        raise ArchitectureError(f"{description} 必须是 AMD64/x64，当前为 0x{machine:04X}：{path}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(
    output_root: Path,
    version: str,
    bundle: str,
    artifact_paths: list[Path],
) -> Path:
    artifacts = []
    for path in sorted(artifact_paths):
        artifacts.append(
            {
                "name": path.name,
                "size": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )

    manifest = {
        "schema_version": 1,
        "product": "DcmGet",
        "version": version,
        "platform": "windows-x64",
        "bundle": bundle,
        "artifacts": artifacts,
    }

    manifest_path = output_root / "RELEASE-MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def check_installer(
    installer_path: Path,
    *,
    version: str,
) -> None:
    ensure_file(installer_path, reason="installer")
    if not installer_path.name.lower().endswith("-setup-preview-x64.exe"):
        raise GateError(f"installer 文件名应为 -Setup-preview-x64.exe：{installer_path.name}")
    if version and version not in installer_path.name:
        raise GateError(f"installer 版本号不匹配：{installer_path.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--portable-dir", required=True)
    parser.add_argument("--installer", required=True)
    args = parser.parse_args()

    portable_root = Path(args.portable_dir).resolve()
    installer_path = Path(args.installer).resolve()

    # mac 下无需运行 Inno Setup；此脚本只做源目录内容与签名前门禁。
    check_portable_root(portable_root, require_binary_names=("dcmget-desktop.exe", "dcmget-cli.exe"))
    check_installer(installer_path, version=args.version)

    portable_manifest = write_manifest(
        portable_root,
        args.version,
        "portable",
        [path for path in portable_root.iterdir() if path.is_file()],
    )
    installer_manifest = write_manifest(
        installer_path.parent,
        args.version,
        "installer",
        [installer_path],
    )
    print(
        "native preview gate passed; manifests at "
        f"{portable_manifest} and {installer_manifest}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateError as exc:
        print(f"Native preview gate failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
