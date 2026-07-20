#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO


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
MANIFEST_NAME = "DCMGET_DCMTK_MANIFEST.json"
REQUIRED_TOOLS = (
    "movescu",
    "storescp",
    "dcmmkdir",
    "dcmdump",
)


class IntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchivePayload:
    archive_sha256: str
    files: dict[str, str]
    symlinks: dict[str, str]
    hardlinks: dict[str, str]


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


def _stream_sha256(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return _stream_sha256(handle)


def tool_filename(name: str, key: str) -> str:
    return f"{name}.exe" if key.startswith("windows") else name


def _archive_member_path(value: str) -> PurePosixPath:
    pure = PurePosixPath(value.replace("\\", "/"))
    if (
        pure.is_absolute()
        or not pure.parts
        or ".." in pure.parts
        or pure.parts[0].endswith(":")
    ):
        raise IntegrityError(f"DCMTK 归档包含不安全路径：{value}")
    return pure


def _resolved_link_path(
    member: PurePosixPath, link_name: str, hardlink: bool
) -> PurePosixPath:
    link = PurePosixPath(link_name.replace("\\", "/"))
    if link.is_absolute() or (link.parts and link.parts[0].endswith(":")):
        raise IntegrityError(f"DCMTK 归档包含不安全链接：{member} -> {link_name}")
    combined = link if hardlink else member.parent / link
    parts: list[str] = []
    for part in combined.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise IntegrityError(
                    f"DCMTK 归档链接越界：{member} -> {link_name}"
                )
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise IntegrityError(f"DCMTK 归档链接目标无效：{member} -> {link_name}")
    return PurePosixPath(*parts)


def archive_payload(archive: Path, key: str) -> ArchivePayload:
    if key not in PACKAGES:
        raise IntegrityError(f"不支持的平台标识：{key}")
    try:
        archive_hash = sha256(archive)
    except OSError as exc:
        raise IntegrityError(
            f"DCMTK 官方归档缺失或无法读取：{archive}"
        ) from exc
    if archive_hash != EXPECTED_SHA256[key]:
        raise IntegrityError(
            f"DCMTK 官方归档 SHA-256 不匹配：期望 {EXPECTED_SHA256[key]}，"
            f"实际 {archive_hash}"
        )

    files: dict[str, str] = {}
    symlinks: dict[str, str] = {}
    hardlinks: dict[str, str] = {}
    paths: set[str] = set()

    def add_path(relative: str) -> None:
        if relative == MANIFEST_NAME or relative in paths:
            raise IntegrityError(f"DCMTK 归档包含重复或保留路径：{relative}")
        paths.add(relative)

    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as package:
                for member in package.infolist():
                    relative = _archive_member_path(member.filename).as_posix()
                    if member.is_dir():
                        continue
                    add_path(relative)
                    mode = (member.external_attr >> 16) & 0xFFFF
                    file_type = stat.S_IFMT(mode)
                    if file_type == stat.S_IFLNK:
                        target = package.read(member).decode(
                            "utf-8", errors="surrogateescape"
                        )
                        _resolved_link_path(PurePosixPath(relative), target, False)
                        symlinks[relative] = target
                    elif file_type in {0, stat.S_IFREG}:
                        with package.open(member) as handle:
                            files[relative] = _stream_sha256(handle)
                    else:
                        raise IntegrityError(
                            f"DCMTK 归档包含不支持的文件类型：{relative}"
                        )
        else:
            with tarfile.open(archive, "r:*") as package:
                for member in package.getmembers():
                    relative = _archive_member_path(member.name).as_posix()
                    if member.isdir():
                        continue
                    add_path(relative)
                    if member.isfile():
                        handle = package.extractfile(member)
                        if handle is None:
                            raise IntegrityError(
                                f"DCMTK 归档文件无法读取：{relative}"
                            )
                        with handle:
                            files[relative] = _stream_sha256(handle)
                    elif member.issym():
                        _resolved_link_path(
                            PurePosixPath(relative), member.linkname, False
                        )
                        symlinks[relative] = member.linkname
                    elif member.islnk():
                        resolved = _resolved_link_path(
                            PurePosixPath(relative), member.linkname, True
                        )
                        hardlinks[relative] = resolved.as_posix()
                    else:
                        raise IntegrityError(
                            f"DCMTK 归档包含不支持的文件类型：{relative}"
                        )
    except IntegrityError:
        raise
    except (OSError, RuntimeError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise IntegrityError(f"DCMTK 官方归档无法解析：{archive}") from exc

    for relative, target in hardlinks.items():
        seen = {relative}
        while target in hardlinks:
            if target in seen:
                raise IntegrityError(f"DCMTK 归档包含循环硬链接：{relative}")
            seen.add(target)
            target = hardlinks[target]
        if target not in files:
            raise IntegrityError(f"DCMTK 归档硬链接目标缺失：{relative}")
    if not files:
        raise IntegrityError("DCMTK 官方归档没有可验证文件")
    return ArchivePayload(archive_hash, files, symlinks, hardlinks)


def _payload_hash(payload: ArchivePayload, relative: str) -> str:
    seen: set[str] = set()
    while relative in payload.hardlinks:
        if relative in seen:
            raise IntegrityError(f"DCMTK 归档包含循环硬链接：{relative}")
        seen.add(relative)
        relative = payload.hardlinks[relative]
    try:
        return payload.files[relative]
    except KeyError as exc:
        raise IntegrityError(f"DCMTK 归档文件记录缺失：{relative}") from exc


def _expected_tool_records(
    payload: ArchivePayload, key: str
) -> tuple[dict[str, dict[str, str]], PurePosixPath]:
    records: dict[str, dict[str, str]] = {}
    bin_dir: PurePosixPath | None = None
    payload_files = set(payload.files) | set(payload.hardlinks)
    for name in REQUIRED_TOOLS:
        filename = tool_filename(name, key)
        matches = [
            relative
            for relative in payload_files
            if PurePosixPath(relative).name == filename
        ]
        if len(matches) != 1:
            raise IntegrityError(
                f"DCMTK 官方归档中的必需工具数量异常：{filename}"
            )
        relative = matches[0]
        parent = PurePosixPath(relative).parent
        if bin_dir is None:
            bin_dir = parent
        elif parent != bin_dir:
            raise IntegrityError("DCMTK 官方归档中的必需工具不在同一目录")
        records[name] = {
            "path": relative,
            "sha256": _payload_hash(payload, relative),
        }
    if bin_dir is None:
        raise IntegrityError("DCMTK 官方归档没有必需工具")
    return records, bin_dir


def _expected_manifest(key: str, payload: ArchivePayload) -> dict[str, object]:
    tools, _ = _expected_tool_records(payload, key)
    return {
        "platform": key,
        "version": VERSION,
        "archive_sha256": payload.archive_sha256,
        "tools": tools,
    }


def write_manifest(target: Path, key: str, payload: ArchivePayload) -> Path:
    manifest = _expected_manifest(key, payload)
    manifest_path = target / MANIFEST_NAME
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    return manifest_path


def _load_manifest(target: Path) -> dict[str, object]:
    manifest_path = target / MANIFEST_NAME
    if manifest_path.is_symlink():
        raise IntegrityError("DCMTK 完整性清单不能是符号链接")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(
            f"DCMTK 完整性清单缺失或损坏：{manifest_path}"
        ) from exc
    if not isinstance(manifest, dict):
        raise IntegrityError("DCMTK 完整性清单格式无效")
    return manifest


def _installed_entries(target: Path) -> dict[str, tuple[str, str | None]]:
    entries: dict[str, tuple[str, str | None]] = {}

    def record(path: Path) -> None:
        relative = path.relative_to(target).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            entries[relative] = ("symlink", os.readlink(path))
        elif stat.S_ISREG(mode):
            entries[relative] = ("file", None)
        else:
            entries[relative] = ("special", None)

    def walk_error(error: OSError) -> None:
        raise error

    try:
        for directory, dirnames, filenames in os.walk(
            target, topdown=True, onerror=walk_error, followlinks=False
        ):
            directory_path = Path(directory)
            for name in list(dirnames):
                path = directory_path / name
                if path.is_symlink():
                    record(path)
                    dirnames.remove(name)
            for name in filenames:
                record(directory_path / name)
    except OSError as exc:
        raise IntegrityError(f"DCMTK 安装目录无法读取：{target}") from exc
    return entries


def _validate_installed_tree(target: Path, payload: ArchivePayload) -> None:
    if target.is_symlink() or not target.is_dir():
        raise IntegrityError(f"DCMTK 安装目录缺失或不安全：{target}")
    entries = _installed_entries(target)
    expected = (
        set(payload.files)
        | set(payload.symlinks)
        | set(payload.hardlinks)
        | {MANIFEST_NAME}
    )
    actual = set(entries)
    missing = sorted(expected - actual)
    if missing:
        raise IntegrityError(f"DCMTK 归档载荷缺失：{missing[0]}")
    unexpected = sorted(actual - expected)
    if unexpected:
        raise IntegrityError(f"DCMTK 归档载荷包含未授权文件：{unexpected[0]}")
    if entries[MANIFEST_NAME][0] != "file":
        raise IntegrityError("DCMTK 完整性清单文件类型无效")

    for relative, expected_hash in payload.files.items():
        if entries[relative][0] != "file":
            raise IntegrityError(f"DCMTK 归档载荷文件类型不匹配：{relative}")
        try:
            actual_hash = sha256(target / Path(*PurePosixPath(relative).parts))
        except OSError as exc:
            raise IntegrityError(f"DCMTK 归档载荷无法读取：{relative}") from exc
        if actual_hash != expected_hash:
            raise IntegrityError(f"DCMTK 归档载荷哈希不匹配：{relative}")

    for relative, expected_target in payload.symlinks.items():
        kind, actual_target = entries[relative]
        if kind != "symlink":
            raise IntegrityError(f"DCMTK 归档载荷链接类型不匹配：{relative}")
        if actual_target != expected_target:
            raise IntegrityError(f"DCMTK 归档载荷链接目标不匹配：{relative}")

    for relative, link_target in payload.hardlinks.items():
        if entries[relative][0] != "file":
            raise IntegrityError(f"DCMTK 归档载荷硬链接类型不匹配：{relative}")
        path = target / Path(*PurePosixPath(relative).parts)
        linked = target / Path(*PurePosixPath(link_target).parts)
        try:
            same_file = os.path.samefile(path, linked)
        except OSError as exc:
            raise IntegrityError(f"DCMTK 归档载荷硬链接无法读取：{relative}") from exc
        if not same_file:
            raise IntegrityError(f"DCMTK 归档载荷硬链接目标不匹配：{relative}")


def _validate_installation(
    target: Path, key: str, payload: ArchivePayload
) -> Path:
    manifest = _load_manifest(target)
    expected_manifest = _expected_manifest(key, payload)
    expected_fields = set(expected_manifest)
    if set(manifest) != expected_fields:
        raise IntegrityError("DCMTK 完整性清单字段与官方归档不一致")
    for field in ("platform", "version", "archive_sha256"):
        if manifest.get(field) != expected_manifest[field]:
            raise IntegrityError(f"DCMTK 完整性清单的 {field} 与官方归档不一致")
    if manifest.get("tools") != expected_manifest["tools"]:
        raise IntegrityError("DCMTK 完整性清单工具记录与官方归档不一致")
    _validate_installed_tree(target, payload)
    _, bin_relative = _expected_tool_records(payload, key)
    return target.resolve() / Path(*bin_relative.parts)


def validate_installation(
    target: Path, key: str, archive: Path | None = None
) -> Path:
    if key not in PACKAGES:
        raise IntegrityError(f"不支持的平台标识：{key}")
    if archive is None:
        archive = target.parent.parent / "downloads" / PACKAGES[key]
    payload = archive_payload(archive, key)
    return _validate_installation(target, key, payload)


def download(url: str, destination: Path, attempts: int = 6) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, attempts + 1):
        size = destination.stat().st_size if destination.exists() else 0
        headers = {"User-Agent": "DcmGet/3.3.0"}
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
                mode = (member.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                if not member.is_dir() and file_type not in {0, stat.S_IFREG}:
                    raise RuntimeError(
                        f"压缩包包含不支持的文件类型：{member.filename}"
                    )
            package.extractall(destination)
        return
    with tarfile.open(archive, "r:*") as package:
        for member in package.getmembers():
            if not (
                member.isfile()
                or member.isdir()
                or member.issym()
                or member.islnk()
            ):
                raise RuntimeError(f"压缩包包含不支持的文件类型：{member.name}")
            _validate_member(
                destination,
                member.name,
                member.linkname if member.issym() or member.islnk() else "",
            )
        if hasattr(tarfile, "fully_trusted_filter"):
            package.extractall(destination, filter="fully_trusted")
        else:  # Python 3.10 before extraction filters were backported
            package.extractall(destination)


def find_bin(directory: Path, key: str) -> Path | None:
    executable = tool_filename("movescu", key)
    for move in directory.rglob(executable):
        if all(
            (move.parent / tool_filename(name, key)).is_file()
            for name in REQUIRED_TOOLS
        ):
            return move.parent
    return None


def acquire_archive(runtime: Path, key: str) -> tuple[Path, str]:
    filename = PACKAGES[key]
    archive = runtime / "downloads" / filename
    expected_hash = EXPECTED_SHA256[key]
    if archive.is_file():
        actual_hash = sha256(archive)
        if actual_hash == expected_hash:
            print(f"使用已校验的 DCMTK 归档：{archive}")
            return archive, actual_hash
        print(f"DCMTK 归档缓存校验失败，将重新下载：{archive}")
        archive.unlink()

    partial = archive.with_suffix(archive.suffix + ".part")
    print(f"正在从 OFFIS 下载 DCMTK {VERSION}：{filename}")
    download(f"{BASE_URL}/{filename}", partial)
    partial.replace(archive)
    actual_hash = sha256(archive)
    if actual_hash != expected_hash:
        archive.unlink(missing_ok=True)
        raise RuntimeError(
            f"DCMTK 归档校验失败：期望 {expected_hash}，实际 {actual_hash}"
        )
    print(f"下载完成，SHA-256：{actual_hash}")
    return archive, actual_hash


def install(project_root: Path, key: str, force: bool = False) -> Path:
    if key not in PACKAGES:
        raise SystemExit(f"不支持的平台标识：{key}")
    runtime = project_root / ".runtime"
    target = runtime / "dcmtk" / key
    archive, _ = acquire_archive(runtime, key)
    payload = archive_payload(archive, key)
    if target.exists() and not force:
        try:
            existing = _validate_installation(target, key, payload)
        except IntegrityError as exc:
            print(f"DCMTK 安装缓存校验失败，将重新安装：{exc}")
        else:
            print(f"DCMTK 已就绪：{existing}")
            return existing

    temporary = runtime / "dcmtk" / f".{key}.extracting"
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    try:
        extract(archive, temporary)
        tool_records, bin_relative = _expected_tool_records(payload, key)
        relative_bin = Path(*bin_relative.parts)
        if not key.startswith("windows"):
            for record in tool_records.values():
                path = temporary / Path(*PurePosixPath(record["path"]).parts)
                path.chmod(path.stat().st_mode | 0o111)
        write_manifest(temporary, key, payload)
        _validate_installation(temporary, key, payload)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    if target.exists():
        shutil.rmtree(target)
    temporary.replace(target)
    installed_bin = target / relative_bin
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
