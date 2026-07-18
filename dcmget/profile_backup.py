from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from filelock import FileLock, Timeout

from . import __version__
from .config import AppConfig
from .profile_manager import (
    MAX_DISPLAY_NAME_LENGTH,
    MAX_METADATA_BYTES,
    PROFILE_METADATA_NAME,
    PROFILE_METADATA_SCHEMA,
    PROFILE_METADATA_VERSION,
)
from .runtime import application_state_dir, default_config_path


PROFILE_BACKUP_SCHEMA_VERSION = 2
SUPPORTED_PROFILE_BACKUP_SCHEMA_VERSIONS = frozenset({1, 2})
MAX_PROFILE_COUNT = 1000
MAX_CONFIG_BYTES = 1024 * 1024
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
_PROFILE_NAME = re.compile(r"i([1-9][0-9]{0,3})")
_FORBIDDEN_CONFIG_KEYS = {
    "license",
    "license_token",
    "registration_code",
    "trial",
    "trial_state",
}
_LEGACY_CONFIG_KEYS = {
    "application_entity_title",
    "called_ae_title",
    "movescu_executable_path",
    "network_port",
    "pdi_include_html_preview",
    "pdi_include_weasis_windows",
    "pdi_preview_mode",
}


class ProfileBackupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProfileBackupInfo:
    path: Path
    profile_numbers: tuple[int, ...]
    app_version: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ProfileRestoreResult:
    restored_paths: tuple[Path, ...]
    profile_numbers: tuple[int, ...]
    previous_backup: Path | None


def discover_profile_configs(
    config_root: str | Path | None = None,
) -> dict[int, Path]:
    root = Path(config_root or default_config_path().parent).expanduser().resolve()
    instances = root / "instances"
    result: dict[int, Path] = {}
    if not instances.is_dir():
        return result
    for directory in instances.iterdir():
        match = _PROFILE_NAME.fullmatch(directory.name)
        config_path = directory / "config.json"
        if (
            match
            and directory.is_dir()
            and not directory.is_symlink()
            and config_path.is_file()
            and not config_path.is_symlink()
        ):
            result[int(match.group(1))] = config_path
    return dict(sorted(result.items()))


def create_profile_backup(
    output_path: str | Path,
    profile_paths: Mapping[int | str, str | Path] | None = None,
    *,
    config_root: str | Path | None = None,
    now: datetime | None = None,
) -> ProfileBackupInfo:
    sources = (
        discover_profile_configs(config_root)
        if profile_paths is None
        else _normalize_profile_paths(profile_paths)
    )
    if not sources:
        raise ProfileBackupError("没有可备份的 Profile 配置")
    contents: dict[int, bytes] = {}
    metadata_contents: dict[int, bytes] = {}
    for number, path in sources.items():
        source = Path(path).expanduser()
        if source.is_symlink():
            raise ProfileBackupError(
                f"Profile {number} 配置文件不安全：{source}"
            )
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise ProfileBackupError(f"无法读取 Profile {number} 配置：{exc}") from exc
        _validated_config_bytes(data, number)
        contents[number] = data
        metadata_path = source.with_name(PROFILE_METADATA_NAME)
        if metadata_path.is_symlink():
            raise ProfileBackupError(
                f"Profile {number} 元数据文件不安全：{metadata_path}"
            )
        if metadata_path.exists():
            if not metadata_path.is_file():
                raise ProfileBackupError(
                    f"Profile {number} 元数据文件不安全：{metadata_path}"
                )
            try:
                metadata = metadata_path.read_bytes()
            except OSError as exc:
                raise ProfileBackupError(
                    f"无法读取 Profile {number} 元数据：{exc}"
                ) from exc
            _validated_metadata_bytes(metadata, number)
            metadata_contents[number] = metadata
    output = Path(output_path).expanduser()
    generated = now or datetime.now(timezone.utc)
    _write_backup_package(
        output,
        contents,
        generated,
        metadata_contents=metadata_contents,
    )
    return ProfileBackupInfo(
        output,
        tuple(sorted(contents)),
        __version__,
        generated.isoformat(),
    )


def inspect_profile_backup(package_path: str | Path) -> ProfileBackupInfo:
    package = Path(package_path).expanduser()
    manifest, contents, _metadata_contents = _read_backup_package(package)
    return ProfileBackupInfo(
        package,
        tuple(sorted(contents)),
        str(manifest["app_version"]),
        str(manifest["created_at"]),
    )


def restore_profile_backup(
    package_path: str | Path,
    *,
    config_root: str | Path | None = None,
    state_root: str | Path | None = None,
    backup_directory: str | Path | None = None,
    lock_timeout: float = 10,
    now: datetime | None = None,
    owned_profile_lock: FileLock | None = None,
) -> ProfileRestoreResult:
    """Validate and transactionally restore Profile configs and display names.

    The pre-restore snapshot is atomically published before any target changes.
    Individual config replacements are atomic, and an error rolls already
    replaced files back to their original bytes.
    """

    package = Path(package_path).expanduser()
    manifest, imported, imported_metadata = _read_backup_package(package)
    canonical = {
        number: _canonical_config_bytes(data, number)
        for number, data in imported.items()
    }
    canonical_metadata = {
        number: _canonical_metadata_bytes(data, number)
        for number, data in imported_metadata.items()
    }
    restores_metadata = int(manifest["schema_version"]) >= 2
    root = Path(config_root or default_config_path().parent).expanduser().resolve()
    runtime_root = Path(state_root or application_state_dir()).expanduser().resolve()
    targets = {
        number: root / "instances" / f"i{number}" / "config.json"
        for number in sorted(canonical)
    }
    metadata_targets = {
        number: target.with_name(PROFILE_METADATA_NAME)
        for number, target in targets.items()
    }
    _prepare_restore_targets(root, targets)
    allocation_lock, profile_locks = _acquire_profile_locks(
        runtime_root,
        targets,
        lock_timeout,
        owned_profile_lock=owned_profile_lock,
    )
    try:
        locks = _acquire_config_locks(targets, lock_timeout)
    except Exception:
        for lock in reversed(profile_locks):
            if lock.is_locked:
                lock.release()
        if allocation_lock.is_locked:
            allocation_lock.release()
        raise
    previous: dict[int, bytes] = {}
    previous_metadata: dict[int, bytes] = {}
    previous_backup: Path | None = None
    replaced: list[int] = []
    generated = now or datetime.now(timezone.utc)
    try:
        for number, target in targets.items():
            if target.is_symlink():
                raise ProfileBackupError(
                    f"Profile {number} 配置文件不安全：{target}"
                )
            if target.is_file():
                try:
                    old_data = target.read_bytes()
                except OSError as exc:
                    raise ProfileBackupError(
                        f"无法读取 Profile {number} 的原配置：{exc}"
                    ) from exc
                _validated_config_bytes(old_data, number)
                previous[number] = old_data
            metadata_path = metadata_targets[number]
            if metadata_path.is_symlink():
                raise ProfileBackupError(
                    f"Profile {number} 元数据文件不安全：{metadata_path}"
                )
            if metadata_path.exists():
                if not metadata_path.is_file():
                    raise ProfileBackupError(
                        f"Profile {number} 元数据文件不安全：{metadata_path}"
                    )
                try:
                    old_metadata = metadata_path.read_bytes()
                except OSError as exc:
                    raise ProfileBackupError(
                        f"无法读取 Profile {number} 的原元数据：{exc}"
                    ) from exc
                _validated_metadata_bytes(old_metadata, number)
                if number not in previous:
                    raise ProfileBackupError(
                        f"Profile {number} 元数据没有对应的配置文件"
                    )
                previous_metadata[number] = old_metadata

        if previous:
            backup_root = Path(
                backup_directory or root / "profile-backups"
            ).expanduser()
            timestamp = generated.strftime("%Y%m%d-%H%M%S-%f")
            previous_backup = backup_root / f"pre-restore-{timestamp}.zip"
            _write_backup_package(
                previous_backup,
                previous,
                generated,
                metadata_contents=previous_metadata,
            )

        for number, target in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            replaced.append(number)
            _atomic_write(target, canonical[number])
            if restores_metadata:
                metadata_target = metadata_targets[number]
                if number in canonical_metadata:
                    _atomic_write(metadata_target, canonical_metadata[number])
                else:
                    metadata_target.unlink(missing_ok=True)
    except Exception as exc:
        rollback_errors: list[str] = []
        for number in reversed(replaced):
            target = targets[number]
            try:
                if number in previous:
                    _atomic_write(target, previous[number])
                else:
                    target.unlink(missing_ok=True)
                if restores_metadata:
                    metadata_target = metadata_targets[number]
                    if number in previous_metadata:
                        _atomic_write(metadata_target, previous_metadata[number])
                    else:
                        metadata_target.unlink(missing_ok=True)
            except Exception as rollback_exc:
                rollback_errors.append(f"Profile {number}: {rollback_exc}")
        if isinstance(exc, ProfileBackupError):
            message = str(exc)
        else:
            message = f"恢复 Profile 配置失败：{exc}"
        if rollback_errors:
            message += "；回滚失败：" + "；".join(rollback_errors)
        raise ProfileBackupError(message) from exc
    finally:
        for lock in reversed(locks):
            if lock.is_locked:
                lock.release()
        for lock in reversed(profile_locks):
            if lock.is_locked:
                lock.release()
        if allocation_lock.is_locked:
            allocation_lock.release()

    return ProfileRestoreResult(
        tuple(targets[number] for number in sorted(targets)),
        tuple(sorted(targets)),
        previous_backup,
    )


def _normalize_profile_paths(
    values: Mapping[int | str, str | Path],
) -> dict[int, Path]:
    if len(values) > MAX_PROFILE_COUNT:
        raise ProfileBackupError("Profile 数量超过支持上限")
    result: dict[int, Path] = {}
    for raw_number, raw_path in values.items():
        number = _profile_number(raw_number)
        if number in result:
            raise ProfileBackupError(f"Profile {number} 重复")
        result[number] = Path(raw_path).expanduser()
    return dict(sorted(result.items()))


def _profile_number(value: int | str) -> int:
    if isinstance(value, bool) or not re.fullmatch(
        r"\+?[1-9][0-9]{0,3}", str(value).strip()
    ):
        raise ProfileBackupError("Profile 编号必须在 1 到 9999 之间")
    number = int(value)
    if not 1 <= number <= 9999:
        raise ProfileBackupError("Profile 编号必须在 1 到 9999 之间")
    return number


def _validated_config_bytes(data: bytes, number: int) -> AppConfig:
    if len(data) > MAX_CONFIG_BYTES:
        raise ProfileBackupError(f"Profile {number} 配置文件过大")
    try:
        raw = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileBackupError(f"Profile {number} 配置不是有效 JSON") from exc
    if not isinstance(raw, dict):
        raise ProfileBackupError(f"Profile {number} 配置根节点必须是对象")
    forbidden = sorted(
        str(key) for key in raw if str(key).strip().lower() in _FORBIDDEN_CONFIG_KEYS
    )
    if forbidden:
        raise ProfileBackupError(
            f"Profile {number} 配置包含禁止字段：{'、'.join(forbidden)}"
        )
    allowed = set(AppConfig().to_dict()) | _LEGACY_CONFIG_KEYS
    unknown = sorted(str(key) for key in raw if str(key) not in allowed)
    if unknown:
        raise ProfileBackupError(
            f"Profile {number} 配置包含未知字段：{'、'.join(unknown)}"
        )
    version = raw.get("config_version", 1)
    if isinstance(version, bool):
        raise ProfileBackupError(f"Profile {number} 配置版本无效")
    try:
        version_number = int(version)
    except (TypeError, ValueError) as exc:
        raise ProfileBackupError(f"Profile {number} 配置版本无效") from exc
    current_version = AppConfig().config_version
    if not 1 <= version_number <= current_version:
        raise ProfileBackupError(f"Profile {number} 配置版本 {version_number} 不受支持")
    try:
        config = AppConfig.from_dict(raw)
    except (TypeError, ValueError) as exc:
        raise ProfileBackupError(f"Profile {number} 配置结构无效：{exc}") from exc
    errors = config.validate()
    if errors:
        messages = "；".join(f"{key}: {value}" for key, value in sorted(errors.items()))
        raise ProfileBackupError(f"Profile {number} 配置校验失败：{messages}")
    return config


def _canonical_config_bytes(data: bytes, number: int) -> bytes:
    config = _validated_config_bytes(data, number)
    return (json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )


def _validated_metadata_bytes(data: bytes, number: int) -> str:
    if len(data) > MAX_METADATA_BYTES:
        raise ProfileBackupError(f"Profile {number} 元数据文件过大")
    try:
        raw = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileBackupError(
            f"Profile {number} 元数据不是有效 JSON"
        ) from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "version",
        "display_name",
    }:
        raise ProfileBackupError(f"Profile {number} 元数据结构无效")
    if raw.get("schema") != PROFILE_METADATA_SCHEMA:
        raise ProfileBackupError(f"Profile {number} 元数据类型无效")
    version = raw.get("version")
    if type(version) is not int or version != PROFILE_METADATA_VERSION:
        raise ProfileBackupError(f"Profile {number} 元数据版本不受支持")
    display_name = raw.get("display_name")
    if not isinstance(display_name, str):
        raise ProfileBackupError(f"Profile {number} 显示名无效")
    normalized = display_name.strip()
    if not normalized:
        raise ProfileBackupError(f"Profile {number} 显示名不能为空")
    if len(normalized) > MAX_DISPLAY_NAME_LENGTH:
        raise ProfileBackupError(
            f"Profile {number} 显示名不能超过 "
            f"{MAX_DISPLAY_NAME_LENGTH} 个字符"
        )
    if any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in normalized
    ):
        raise ProfileBackupError(f"Profile {number} 显示名不能包含控制字符")
    return normalized


def _canonical_metadata_bytes(data: bytes, number: int) -> bytes:
    display_name = _validated_metadata_bytes(data, number)
    payload = {
        "schema": PROFILE_METADATA_SCHEMA,
        "version": PROFILE_METADATA_VERSION,
        "display_name": display_name,
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_backup_package(
    output: Path,
    contents: Mapping[int, bytes],
    generated: datetime,
    *,
    metadata_contents: Mapping[int, bytes] | None = None,
) -> None:
    if not contents or len(contents) > MAX_PROFILE_COUNT:
        raise ProfileBackupError("备份包中的 Profile 数量无效")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProfileBackupError(f"无法创建 Profile 备份目录：{exc}") from exc
    metadata_contents = metadata_contents or {}
    unknown_metadata = sorted(set(metadata_contents) - set(contents))
    if unknown_metadata:
        raise ProfileBackupError(
            "Profile 元数据缺少对应配置："
            + "、".join(str(number) for number in unknown_metadata)
        )
    profiles: list[dict[str, Any]] = []
    entries: dict[str, bytes] = {}
    for number in sorted(contents):
        data = contents[number]
        _validated_config_bytes(data, number)
        name = f"profiles/i{number}/config.json"
        entries[name] = data
        row: dict[str, Any] = {
            "profile_number": number,
            "path": name,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if number in metadata_contents:
            metadata = metadata_contents[number]
            _validated_metadata_bytes(metadata, number)
            metadata_name = f"profiles/i{number}/{PROFILE_METADATA_NAME}"
            entries[metadata_name] = metadata
            row["metadata"] = {
                "path": metadata_name,
                "bytes": len(metadata),
                "sha256": hashlib.sha256(metadata).hexdigest(),
            }
        profiles.append(row)
    manifest = {
        "schema": "dcmget-profile-backup",
        "schema_version": PROFILE_BACKUP_SCHEMA_VERSION,
        "app_version": __version__,
        "created_at": generated.isoformat(),
        "contains_trial_state": False,
        "profiles": profiles,
    }
    entries["manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, data in sorted(entries.items()):
                archive.writestr(name, data)
        os.replace(temporary, output)
        try:
            output.chmod(0o600)
        except OSError:
            pass
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProfileBackupError(f"无法写入 Profile 备份包：{exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _read_backup_package(
    package: Path,
) -> tuple[dict[str, Any], dict[int, bytes], dict[int, bytes]]:
    try:
        with zipfile.ZipFile(package) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ProfileBackupError("Profile 备份包包含重复文件")
            if sum(info.file_size for info in infos) > MAX_PACKAGE_BYTES:
                raise ProfileBackupError("Profile 备份包解压后过大")
            if "manifest.json" not in names:
                raise ProfileBackupError("Profile 备份包缺少 manifest.json")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8-sig"))
            expected_names, profile_rows = _validate_manifest(manifest)
            if set(names) != expected_names:
                extras = sorted(set(names) - expected_names)
                missing = sorted(expected_names - set(names))
                detail = []
                if extras:
                    detail.append("多余文件：" + "、".join(extras))
                if missing:
                    detail.append("缺少文件：" + "、".join(missing))
                raise ProfileBackupError(
                    "Profile 备份包文件清单不一致：" + "；".join(detail)
                )
            contents: dict[int, bytes] = {}
            metadata_contents: dict[int, bytes] = {}
            for row in profile_rows:
                number = row["profile_number"]
                info = archive.getinfo(row["path"])
                if info.file_size > MAX_CONFIG_BYTES:
                    raise ProfileBackupError(f"Profile {number} 配置文件过大")
                data = archive.read(info)
                if len(data) != row["bytes"] or not _same_digest(data, row["sha256"]):
                    raise ProfileBackupError(f"Profile {number} 配置摘要不匹配")
                _validated_config_bytes(data, number)
                contents[number] = data
                metadata_row = row.get("metadata")
                if metadata_row is not None:
                    metadata_info = archive.getinfo(metadata_row["path"])
                    if metadata_info.file_size > MAX_METADATA_BYTES:
                        raise ProfileBackupError(
                            f"Profile {number} 元数据文件过大"
                        )
                    metadata = archive.read(metadata_info)
                    if len(metadata) != metadata_row["bytes"] or not _same_digest(
                        metadata,
                        metadata_row["sha256"],
                    ):
                        raise ProfileBackupError(
                            f"Profile {number} 元数据摘要不匹配"
                        )
                    _validated_metadata_bytes(metadata, number)
                    metadata_contents[number] = metadata
    except ProfileBackupError:
        raise
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ) as exc:
        raise ProfileBackupError(f"无法读取 Profile 备份包：{exc}") from exc
    return manifest, contents, metadata_contents


def _validate_manifest(
    manifest: Any,
) -> tuple[set[str], list[dict[str, Any]]]:
    if not isinstance(manifest, dict):
        raise ProfileBackupError("Profile 备份清单根节点必须是对象")
    if manifest.get("schema") != "dcmget-profile-backup":
        raise ProfileBackupError("不是 DcmGet Profile 备份包")
    schema_version = manifest.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version not in SUPPORTED_PROFILE_BACKUP_SCHEMA_VERSIONS
    ):
        raise ProfileBackupError("Profile 备份包版本不受支持")
    if manifest.get("contains_trial_state") is not False:
        raise ProfileBackupError("Profile 备份包不得包含试用状态")
    if (
        not isinstance(manifest.get("app_version"), str)
        or not str(manifest.get("app_version")).strip()
    ):
        raise ProfileBackupError("Profile 备份包缺少应用版本")
    if (
        not isinstance(manifest.get("created_at"), str)
        or not str(manifest.get("created_at")).strip()
    ):
        raise ProfileBackupError("Profile 备份包缺少创建时间")
    rows = manifest.get("profiles")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_PROFILE_COUNT:
        raise ProfileBackupError("Profile 备份清单数量无效")
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ProfileBackupError("Profile 备份清单条目无效")
        number = _profile_number(raw.get("profile_number"))
        if number in seen:
            raise ProfileBackupError(f"Profile {number} 在清单中重复")
        seen.add(number)
        expected_path = f"profiles/i{number}/config.json"
        if raw.get("path") != expected_path:
            raise ProfileBackupError(f"Profile {number} 配置路径无效")
        try:
            size = int(raw.get("bytes"))
        except (TypeError, ValueError) as exc:
            raise ProfileBackupError(f"Profile {number} 配置大小无效") from exc
        digest = str(raw.get("sha256") or "").lower()
        if not 0 <= size <= MAX_CONFIG_BYTES or not re.fullmatch(
            r"[0-9a-f]{64}", digest
        ):
            raise ProfileBackupError(f"Profile {number} 配置摘要无效")
        normalized_row: dict[str, Any] = {
            "profile_number": number,
            "path": expected_path,
            "bytes": size,
            "sha256": digest,
        }
        has_metadata = "metadata" in raw
        metadata = raw.get("metadata")
        if schema_version == 1:
            if has_metadata:
                raise ProfileBackupError(
                    f"Profile {number} 的 v1 清单不得包含元数据"
                )
        elif has_metadata:
            if not isinstance(metadata, dict) or set(metadata) != {
                "path",
                "bytes",
                "sha256",
            }:
                raise ProfileBackupError(f"Profile {number} 元数据清单无效")
            expected_metadata_path = f"profiles/i{number}/{PROFILE_METADATA_NAME}"
            if metadata.get("path") != expected_metadata_path:
                raise ProfileBackupError(f"Profile {number} 元数据路径无效")
            metadata_size = metadata.get("bytes")
            if type(metadata_size) is not int or not (
                0 <= metadata_size <= MAX_METADATA_BYTES
            ):
                raise ProfileBackupError(f"Profile {number} 元数据大小无效")
            metadata_digest = metadata.get("sha256")
            if not isinstance(metadata_digest, str) or not re.fullmatch(
                r"[0-9a-fA-F]{64}",
                metadata_digest,
            ):
                raise ProfileBackupError(f"Profile {number} 元数据摘要无效")
            normalized_row["metadata"] = {
                "path": expected_metadata_path,
                "bytes": metadata_size,
                "sha256": metadata_digest.lower(),
            }
        normalized.append(normalized_row)
    normalized.sort(key=lambda item: item["profile_number"])
    expected_names = {"manifest.json", *(row["path"] for row in normalized)}
    expected_names.update(
        row["metadata"]["path"]
        for row in normalized
        if "metadata" in row
    )
    return expected_names, normalized


def _same_digest(data: bytes, expected: str) -> bool:
    import hmac

    return hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected)


def _acquire_config_locks(
    targets: Mapping[int, Path], timeout: float
) -> list[FileLock]:
    locks: list[FileLock] = []
    try:
        for target in targets.values():
            if target.parent.is_symlink() or not target.parent.is_dir():
                raise ProfileBackupError(
                    f"Profile 配置目录不安全：{target.parent}"
                )
            lock_path = target.with_name(f"{target.name}.lock")
            _validate_lock_path(lock_path, target.parent, "Profile 配置锁")
            lock = FileLock(str(lock_path))
            lock.acquire(timeout=timeout)
            locks.append(lock)
    except (OSError, Timeout, ProfileBackupError) as exc:
        for lock in reversed(locks):
            if lock.is_locked:
                lock.release()
        if isinstance(exc, ProfileBackupError):
            raise
        if isinstance(exc, Timeout):
            message = "Profile 配置正在被其他进程使用，请稍后重试"
        else:
            message = f"无法准备 Profile 配置目录：{exc}"
        raise ProfileBackupError(message) from exc
    return locks


def _prepare_restore_targets(root: Path, targets: Mapping[int, Path]) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise ProfileBackupError(f"无法创建 Profile 配置根目录：{exc}") from exc
    instances = root / "instances"
    if instances.is_symlink():
        raise ProfileBackupError(f"Profile 配置目录不安全：{instances}")
    try:
        instances.mkdir(parents=False, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise ProfileBackupError(f"无法创建 Profile 配置目录：{exc}") from exc
    if not instances.is_dir():
        raise ProfileBackupError(f"Profile 配置目录不安全：{instances}")
    for number, target in targets.items():
        parent = target.parent
        if parent.is_symlink():
            raise ProfileBackupError(f"Profile {number} 配置目录不安全：{parent}")
        try:
            parent.mkdir(parents=False, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ProfileBackupError(
                f"无法创建 Profile {number} 配置目录：{exc}"
            ) from exc
        if not parent.is_dir():
            raise ProfileBackupError(f"Profile {number} 配置目录不安全：{parent}")
        try:
            parent.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise ProfileBackupError(
                f"Profile {number} 配置目录越过配置根目录"
            ) from exc


def _acquire_profile_locks(
    state_root: Path,
    targets: Mapping[int, Path],
    timeout: float,
    *,
    owned_profile_lock: FileLock | None,
) -> tuple[FileLock, list[FileLock]]:
    state_profiles = state_root / "instances"
    if state_profiles.is_symlink():
        raise ProfileBackupError(f"Profile 状态目录不安全：{state_profiles}")
    try:
        state_profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise ProfileBackupError(f"无法准备 Profile 状态目录：{exc}") from exc
    if not state_profiles.is_dir():
        raise ProfileBackupError(f"Profile 状态目录不安全：{state_profiles}")
    try:
        state_profiles.resolve(strict=True).relative_to(state_root)
    except (OSError, ValueError) as exc:
        raise ProfileBackupError("Profile 状态目录越过状态根目录") from exc

    owned = _owned_profile_number(
        state_profiles,
        targets,
        owned_profile_lock,
    )
    allocation_path = state_profiles / ".allocate.lock"
    _validate_lock_path(allocation_path, state_profiles, "Profile 管理锁")
    allocation_lock = FileLock(str(allocation_path))
    profile_locks: list[FileLock] = []
    try:
        allocation_lock.acquire(timeout=timeout)
        for number in sorted(targets):
            if number == owned:
                continue
            lock_path = state_profiles / f"i{number}.lock"
            _validate_lock_path(lock_path, state_profiles, f"Profile {number} 运行锁")
            lock = FileLock(str(lock_path))
            try:
                lock.acquire(timeout=0)
            except Timeout as exc:
                raise ProfileBackupError(
                    f"Profile {number} 正在运行，不能恢复；请先关闭对应 DcmGet 窗口"
                ) from exc
            profile_locks.append(lock)
    except (OSError, Timeout, ProfileBackupError) as exc:
        for lock in reversed(profile_locks):
            if lock.is_locked:
                lock.release()
        if allocation_lock.is_locked:
            allocation_lock.release()
        if isinstance(exc, ProfileBackupError):
            raise
        if isinstance(exc, Timeout):
            message = "等待 Profile 管理锁超时"
        else:
            message = f"无法准备 Profile 运行锁：{exc}"
        raise ProfileBackupError(message) from exc
    return allocation_lock, profile_locks


def _owned_profile_number(
    state_profiles: Path,
    targets: Mapping[int, Path],
    lock: FileLock | None,
) -> int | None:
    if lock is None:
        return None
    if not isinstance(lock, FileLock):
        raise ProfileBackupError("当前 Profile 运行锁无效，不能在线恢复")
    if not lock.is_locked:
        raise ProfileBackupError("当前 Profile 运行锁未持有，不能在线恢复")
    try:
        actual = Path(lock.lock_file).expanduser()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProfileBackupError("当前 Profile 运行锁无效，不能在线恢复") from exc
    for number in sorted(targets):
        expected = state_profiles / f"i{number}.lock"
        _validate_lock_path(expected, state_profiles, f"Profile {number} 运行锁")
        try:
            if os.path.normcase(str(actual.resolve(strict=True))) == os.path.normcase(
                str(expected.resolve(strict=True))
            ):
                return number
        except OSError as exc:
            raise ProfileBackupError(
                f"无法验证 Profile {number} 运行锁：{exc}"
            ) from exc
    # The current GUI may restore a package that only contains other profiles.
    # In that case the held lease is irrelevant and every target is still
    # checked by acquiring its own running lock below.
    return None


def _validate_lock_path(path: Path, root: Path, label: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ProfileBackupError(f"{label}路径不安全：{path}")
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ProfileBackupError(f"{label}越过允许目录：{path}") from exc


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
