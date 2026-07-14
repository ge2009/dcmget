#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


VERSION = "3.7.0"
BASE_URL = "https://dicom.offis.de/download/dcmtk/release/bin"
PACKAGES = {
    "windows-x86_64": f"dcmtk-{VERSION}-win64-dynamic.zip",
    "macos-arm64": f"dcmtk-{VERSION}-macosx-arm64.tar.bz2",
    "macos-x86_64": f"dcmtk-{VERSION}-macosx-x86_64.tar.bz2",
    "linux-x86_64": f"dcmtk-{VERSION}-linux-x86_64.tar.bz2",
}
EXPECTED_SHA256 = {
    "windows-x86_64": "dcca45b2a7596e829f0eda885c746880385176830fe47253a6444330017ba191",
    "macos-arm64": "89336e0ea3390903693064f55eb359eebbc02d3a256d839502c25e3365b9a3c3",
    "macos-x86_64": "2c79741c60773db3d23eb667bfb79407c7dcc5ef4f7bcfba99a20389f561fbea",
    "linux-x86_64": "e350c3641ad84a88e40eea361ea81f33c9a2fb40ce0101c84a8a983c18dcf488",
}


def platform_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "windows-x86_64"
    if system == "darwin":
        return "macos-arm64" if machine in {"arm64", "aarch64"} else "macos-x86_64"
    if system == "linux" and machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    raise SystemExit(f"当前平台暂不支持自动下载：{system}/{machine}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, destination: Path, attempts: int = 6) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, attempts + 1):
        size = destination.stat().st_size if destination.exists() else 0
        headers = {"User-Agent": "DcmGet/2.1"}
        if size:
            headers["Range"] = f"bytes={size}-"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=90
            ) as response:
                resumed = size > 0 and getattr(response, "status", 200) == 206
                mode = "ab" if resumed else "wb"
                with destination.open(mode) as handle:
                    shutil.copyfileobj(response, handle, length=1024 * 1024)
            return
        except (OSError, urllib.error.URLError) as exc:
            if attempt == attempts:
                raise RuntimeError(f"DCMTK 下载失败：{exc}") from exc
            print(f"下载中断，第 {attempt}/{attempts} 次重试…")
            time.sleep(min(attempt * 2, 10))


def _validate_member(root: Path, name: str, link_name: str = "") -> None:
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts:
        raise RuntimeError(f"压缩包包含不安全路径：{name}")
    target = (root / Path(*pure.parts)).resolve()
    if os.path.commonpath((root.resolve(), target)) != str(root.resolve()):
        raise RuntimeError(f"压缩包路径越界：{name}")
    if link_name:
        link = PurePosixPath(link_name.replace("\\", "/"))
        link_target = (
            root / Path(*link.parts)
            if link.is_absolute()
            else target.parent / Path(*link.parts)
        ).resolve()
        if os.path.commonpath((root.resolve(), link_target)) != str(root.resolve()):
            raise RuntimeError(f"压缩包链接越界：{name} -> {link_name}")


def extract(archive: Path, destination: Path) -> None:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                _validate_member(destination, member.filename)
            package.extractall(destination)
        return
    with tarfile.open(archive, "r:*") as package:
        for member in package.getmembers():
            _validate_member(
                destination,
                member.name,
                member.linkname if member.issym() or member.islnk() else "",
            )
        package.extractall(destination)


def find_bin(directory: Path, key: str) -> Path | None:
    executable = "movescu.exe" if key.startswith("windows") else "movescu"
    storescp = "storescp.exe" if key.startswith("windows") else "storescp"
    for move in directory.rglob(executable):
        if (move.parent / storescp).is_file():
            return move.parent
    return None


def install(project_root: Path, key: str, force: bool = False) -> Path:
    if key not in PACKAGES:
        raise SystemExit(f"不支持的平台标识：{key}")
    runtime = project_root / ".runtime"
    target = runtime / "dcmtk" / key
    existing = find_bin(target, key) if target.exists() else None
    if existing and not force:
        print(f"DCMTK 已就绪：{existing}")
        return existing

    filename = PACKAGES[key]
    archive = runtime / "downloads" / filename
    partial = archive.with_suffix(archive.suffix + ".part")
    print(f"正在从 OFFIS 下载 DCMTK {VERSION}：{filename}")
    download(f"{BASE_URL}/{filename}", partial)
    partial.replace(archive)
    actual_hash = sha256(archive)
    if actual_hash != EXPECTED_SHA256[key]:
        archive.unlink(missing_ok=True)
        raise RuntimeError(
            f"DCMTK 归档校验失败：期望 {EXPECTED_SHA256[key]}，实际 {actual_hash}"
        )
    print(f"下载完成，SHA-256：{actual_hash}")

    temporary = runtime / "dcmtk" / f".{key}.extracting"
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    extract(archive, temporary)
    bin_dir = find_bin(temporary, key)
    if not bin_dir:
        shutil.rmtree(temporary)
        raise RuntimeError("DCMTK 压缩包中未找到 movescu/storescp")
    relative_bin = bin_dir.relative_to(temporary)
    if target.exists():
        shutil.rmtree(target)
    temporary.replace(target)
    installed_bin = target / relative_bin
    if not key.startswith("windows"):
        for name in ("movescu", "storescp"):
            (installed_bin / name).chmod((installed_bin / name).stat().st_mode | 0o111)
    print(f"DCMTK {VERSION} 已安装：{installed_bin}")
    return installed_bin


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="下载并安装 OFFIS DCMTK 3.7.0")
    parser.add_argument("--platform", choices=sorted(PACKAGES), default=platform_key())
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    install(args.project_root.resolve(), args.platform, args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
