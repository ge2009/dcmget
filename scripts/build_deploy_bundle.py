#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path


VERSION = "2.6.3"
ARCHIVE_NAME = f"dcmget-{VERSION}-source-deploy.zip"
ROOT_FILES = (
    "DICOM_download_script.py",
    "DICOM_download_ui.py",
    "CHANGELOG.md",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "access.example.txt",
    "config.example.json",
    "logo.png",
    "pyproject.toml",
    "requirements-build.txt",
    "requirements-dev.txt",
    "requirements.txt",
)
TREE_ROOTS = ("dcmget", "scripts", "packaging", "tools")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def source_files(root: Path) -> list[Path]:
    files = [root / name for name in ROOT_FILES]
    for tree in TREE_ROOTS:
        files.extend(
            path
            for path in (root / tree).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        )
    missing = [path.name for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"部署包缺少文件：{', '.join(missing)}")
    return sorted(set(files))


def build(root: Path) -> tuple[Path, Path]:
    release = root / "release"
    release.mkdir(parents=True, exist_ok=True)
    archive = release / ARCHIVE_NAME
    prefix = f"dcmget-{VERSION}"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        for path in source_files(root):
            package.write(path, f"{prefix}/{path.relative_to(root).as_posix()}")
    checksum = release / f"{ARCHIVE_NAME}.sha256"
    checksum.write_text(f"{digest(archive)}  {ARCHIVE_NAME}\n", encoding="ascii")
    return archive, checksum


def main() -> int:
    archive, checksum = build(Path(__file__).resolve().parents[1])
    print(archive)
    print(checksum.read_text(encoding="ascii").strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
