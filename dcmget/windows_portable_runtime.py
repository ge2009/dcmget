from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from filelock import FileLock, Timeout

from .runtime import (
    application_state_dir,
    is_frozen,
    set_portable_dcmtk_bin,
)


MANIFEST_NAME = "DCMGET_PORTABLE_RUNTIME.json"
SCHEMA_VERSION = 1
PLATFORM = "windows-x86_64"
DCMTK_VERSION = "3.7.0"
SOURCE_ROOT = ".runtime/dcmtk/windows-x86_64"
LOCK_TIMEOUT_SECONDS = 180
DIRECTORY_HASH_LENGTH = 16
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_MANIFEST_FIELDS = {
    "schema_version",
    "platform",
    "dcmtk_version",
    "source_root",
    "bin_relative",
    "files",
    "payload_sha256",
}


class PortableRuntimeError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative(value: object, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PortableRuntimeError(f"便携运行时清单的 {field} 路径无效")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(":" in part for part in path.parts)
    ):
        raise PortableRuntimeError(f"便携运行时清单的 {field} 路径不安全：{value}")
    return path


def _tree_files(root: Path) -> dict[str, Path]:
    if root.is_symlink() or not root.is_dir():
        raise PortableRuntimeError(f"DCMTK 运行时目录缺失或类型无效：{root}")
    files: dict[str, Path] = {}
    casefolded: set[str] = set()
    try:
        for directory, directory_names, file_names in os.walk(
            root, topdown=True, followlinks=False
        ):
            directory_path = Path(directory)
            for name in directory_names:
                path = directory_path / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    raise PortableRuntimeError(
                        f"DCMTK 运行时包含不支持的目录项：{path.relative_to(root)}"
                    )
            for name in file_names:
                path = directory_path / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    raise PortableRuntimeError(
                        f"DCMTK 运行时包含不支持的文件项：{path.relative_to(root)}"
                    )
                relative = path.relative_to(root).as_posix()
                _safe_relative(relative, "files.path")
                folded = relative.casefold()
                if folded in casefolded:
                    raise PortableRuntimeError(
                        f"DCMTK 运行时包含 Windows 下重名的文件：{relative}"
                    )
                casefolded.add(folded)
                files[relative] = path
    except OSError as exc:
        raise PortableRuntimeError(f"无法读取 DCMTK 运行时目录：{root}") from exc
    return files


def _payload_sha256(manifest: dict[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "payload_sha256"}
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_portable_runtime_manifest(
    runtime_root: str | Path,
    bin_directory: str | Path,
    output_path: str | Path,
) -> Path:
    root = Path(runtime_root).resolve()
    bin_dir = Path(bin_directory).resolve()
    try:
        bin_relative = bin_dir.relative_to(root).as_posix()
    except ValueError as exc:
        raise PortableRuntimeError("DCMTK bin 目录不在便携运行时目录中") from exc
    _safe_relative(bin_relative, "bin_relative")

    tree = _tree_files(root)
    records = [
        {
            "path": relative,
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for relative, path in sorted(tree.items())
    ]
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "platform": PLATFORM,
        "dcmtk_version": DCMTK_VERSION,
        "source_root": SOURCE_ROOT,
        "bin_relative": bin_relative,
        "files": records,
    }
    manifest["payload_sha256"] = _payload_sha256(manifest)
    _validate_manifest(manifest)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def _load_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise PortableRuntimeError("便携运行时清单不能是符号链接")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PortableRuntimeError(f"便携运行时清单缺失或损坏：{path}") from exc
    if not isinstance(value, dict):
        raise PortableRuntimeError("便携运行时清单格式无效")
    return value


def _validate_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if set(manifest) != _MANIFEST_FIELDS:
        raise PortableRuntimeError("便携运行时清单字段不完整")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise PortableRuntimeError("便携运行时清单版本不受支持")
    if manifest.get("platform") != PLATFORM:
        raise PortableRuntimeError("便携运行时清单平台不匹配")
    if manifest.get("dcmtk_version") != DCMTK_VERSION:
        raise PortableRuntimeError("便携运行时清单 DCMTK 版本不匹配")
    if manifest.get("source_root") != SOURCE_ROOT:
        raise PortableRuntimeError("便携运行时清单源目录不匹配")
    bin_relative = _safe_relative(manifest.get("bin_relative"), "bin_relative")

    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise PortableRuntimeError("便携运行时清单没有文件记录")
    records: dict[str, dict[str, Any]] = {}
    folded_paths: set[str] = set()
    previous = ""
    for record in raw_files:
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            raise PortableRuntimeError("便携运行时清单文件记录无效")
        relative = _safe_relative(record.get("path"), "files.path").as_posix()
        if previous and relative <= previous:
            raise PortableRuntimeError("便携运行时清单文件记录未按路径排序")
        previous = relative
        folded = relative.casefold()
        if folded in folded_paths:
            raise PortableRuntimeError(f"便携运行时清单包含重复文件：{relative}")
        folded_paths.add(folded)
        size = record.get("size")
        digest = record.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PortableRuntimeError(f"便携运行时清单文件大小无效：{relative}")
        if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
            raise PortableRuntimeError(f"便携运行时清单文件哈希无效：{relative}")
        records[relative] = record

    required = {
        (bin_relative / "movescu.exe").as_posix(),
        (bin_relative / "storescp.exe").as_posix(),
    }
    if not required <= records.keys():
        raise PortableRuntimeError("便携运行时清单缺少 movescu.exe 或 storescp.exe")
    expected_payload_hash = _payload_sha256(manifest)
    if manifest.get("payload_sha256") != expected_payload_hash:
        raise PortableRuntimeError("便携运行时清单整体哈希不匹配")
    return records


def _validate_tree(root: Path, records: dict[str, dict[str, Any]]) -> bool:
    try:
        tree = _tree_files(root)
        if tree.keys() != records.keys():
            return False
        for relative, record in records.items():
            path = tree[relative]
            if path.stat().st_size != record["size"] or _sha256(path) != record["sha256"]:
                return False
    except (OSError, PortableRuntimeError):
        return False
    return True


def _copy_verified_tree(
    source: Path,
    destination: Path,
    records: dict[str, dict[str, Any]],
) -> None:
    source_tree = _tree_files(source)
    if source_tree.keys() != records.keys():
        raise PortableRuntimeError("便携包内 DCMTK 文件集合与清单不一致")
    for relative, record in records.items():
        source_path = source_tree[relative]
        if source_path.stat().st_size != record["size"]:
            raise PortableRuntimeError(f"便携包内 DCMTK 文件大小不匹配：{relative}")
        output = destination / Path(*PurePosixPath(relative).parts)
        output.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        try:
            with source_path.open("rb") as source_handle, output.open("xb") as output_handle:
                for block in iter(lambda: source_handle.read(1024 * 1024), b""):
                    digest.update(block)
                    output_handle.write(block)
                output_handle.flush()
                os.fsync(output_handle.fileno())
        except OSError as exc:
            raise PortableRuntimeError(f"无法复制便携包内 DCMTK 文件：{relative}") from exc
        if digest.hexdigest() != record["sha256"]:
            raise PortableRuntimeError(f"便携包内 DCMTK 文件哈希不匹配：{relative}")


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)


def publish_portable_dcmtk(
    resource_directory: str | Path,
    state_directory: str | Path,
) -> Path:
    resource_root = Path(resource_directory).resolve()
    manifest = _load_manifest(resource_root / MANIFEST_NAME)
    records = _validate_manifest(manifest)
    source = resource_root / Path(*PurePosixPath(SOURCE_ROOT).parts)
    runtime_root = Path(state_directory).resolve() / "runtime" / "dcmtk"
    runtime_root.mkdir(parents=True, exist_ok=True)
    target = runtime_root / (
        f"dcmtk-{DCMTK_VERSION}-{PLATFORM}-"
        f"{manifest['payload_sha256'][:DIRECTORY_HASH_LENGTH]}"
    )
    temporary = runtime_root / f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    quarantine: Path | None = None

    try:
        lock = FileLock(str(runtime_root / ".publish.lock"), timeout=LOCK_TIMEOUT_SECONDS)
        with lock:
            if _validate_tree(target, records):
                return target / Path(*PurePosixPath(str(manifest["bin_relative"])).parts)

            if target.exists() or target.is_symlink():
                quarantine = runtime_root / (
                    f".{target.name}.invalid-{os.getpid()}-{uuid.uuid4().hex}"
                )
                os.replace(target, quarantine)

            try:
                temporary.mkdir()
                _copy_verified_tree(source, temporary, records)
                if not _validate_tree(temporary, records):
                    raise PortableRuntimeError("便携 DCMTK 临时目录完整性校验失败")
                os.replace(temporary, target)
                if not _validate_tree(target, records):
                    raise PortableRuntimeError("便携 DCMTK 发布后完整性校验失败")
            finally:
                if temporary.exists() or temporary.is_symlink():
                    _remove_path(temporary)
                if quarantine is not None and (
                    quarantine.exists() or quarantine.is_symlink()
                ):
                    _remove_path(quarantine)
    except Timeout as exc:
        raise PortableRuntimeError("等待其他 DcmGet 进程准备 DCMTK 超时") from exc
    except PortableRuntimeError:
        raise
    except OSError as exc:
        raise PortableRuntimeError("无法在用户目录准备便携 DCMTK 运行时") from exc

    return target / Path(*PurePosixPath(str(manifest["bin_relative"])).parts)


def prepare_windows_portable_dcmtk(
    resource_directory: str | Path,
) -> Path | None:
    resource_root = Path(resource_directory)
    if (
        sys.platform != "win32"
        or not is_frozen()
        or not (resource_root / MANIFEST_NAME).is_file()
    ):
        return None
    bin_directory = publish_portable_dcmtk(resource_root, application_state_dir())
    set_portable_dcmtk_bin(bin_directory)
    return bin_directory
