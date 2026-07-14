#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import platform
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build" / "license-generator"
RELEASE_ROOT = ROOT / "release" / "license-generator"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    if sys.platform != "darwin":
        raise SystemExit("注册机必须在 macOS 上构建")
    architecture = {"arm64": "arm64", "x86_64": "x86_64"}.get(platform.machine())
    if not architecture:
        raise SystemExit(f"不支持的 macOS 架构：{platform.machine()}")

    from PyInstaller.__main__ import run as run_pyinstaller

    name = f"DcmGet-License-Generator-macos-{architecture}"
    shutil.rmtree(BUILD_ROOT, ignore_errors=True)
    RELEASE_ROOT.mkdir(parents=True, exist_ok=True)
    for existing in RELEASE_ROOT.glob("DcmGet-License-Generator-macos-*"):
        existing.unlink()

    run_pyinstaller(
        [
            str(ROOT / "tools" / "dcmget_license_generator.py"),
            "--noconfirm",
            "--clean",
            "--onefile",
            "--console",
            "--name",
            name,
            "--distpath",
            str(RELEASE_ROOT),
            "--workpath",
            str(BUILD_ROOT / "work"),
            "--specpath",
            str(BUILD_ROOT / "spec"),
            "--paths",
            str(ROOT),
            "--noupx",
        ]
    )
    output = RELEASE_ROOT / name
    checksum = output.with_name(output.name + ".sha256")
    checksum.write_text(f"{sha256(output)}  {output.name}\n", encoding="ascii")
    print(output)
    print(checksum.read_text(encoding="ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
