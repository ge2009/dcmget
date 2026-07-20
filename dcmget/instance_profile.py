from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from .config import AppConfig, load_config, save_config
from .core import AccessionStatus
from .profile_manager import RESERVED_PROFILE_PORTS
from .runtime import application_state_dir, default_config_path
from .task_state import (
    TaskCheckpoint,
    TaskCheckpointStore,
    TaskStateError,
    _cleanup_process_identity,
    _result_from_json,
)


PROFILE_DIRECTORY_NAME = "instances"
ALLOCATION_LOCK_NAME = ".allocate.lock"
MIGRATION_MARKER_NAME = ".tasks-migrated-v1.json"
LEGACY_MIGRATION_MARKER_NAME = ".active-task-migrated-v1.json"
MIGRATION_VERSION = 1
ALLOCATION_TIMEOUT_SECONDS = 10
MIGRATION_LOCK_TIMEOUT_SECONDS = 10

_SLOT_NAME = re.compile(r"i([1-9][0-9]*)")
_DIRECTLY_RESUMABLE_PHASES = {
    "queued",
    "running",
    "pause_pending",
    "paused",
}
_PDI_PHASES = {"pdi_pending", "pdi_running", "pdi_retryable"}
_PROCESS_KINDS = {"storescp", "movescu", "pdi"}


class InstanceProfileError(RuntimeError):
    pass


class ProfileInUseError(InstanceProfileError):
    pass


@dataclass
class InstanceProfile:
    number: int
    config_path: Path
    state_directory: Path
    task_state_path: Path
    log_directory: Path
    settings_name: str
    label: str
    _slot_lock: FileLock = field(repr=False)

    @property
    def slot_name(self) -> str:
        return f"i{self.number}"

    @property
    def profile_number(self) -> int:
        return self.number

    @property
    def state_dir(self) -> Path:
        return self.state_directory

    @property
    def lock_held(self) -> bool:
        return self._slot_lock.is_locked

    @property
    def slot_lock(self) -> FileLock:
        """Return the live lease used to prove ownership during online restore."""

        return self._slot_lock

    @property
    def activation_path(self) -> Path:
        return self.state_directory / "gui-instance.json"

    def close(self) -> None:
        if self._slot_lock.is_locked:
            self._slot_lock.release()

    def __enter__(self) -> InstanceProfile:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class MigratedTask:
    task_id: str
    profile_number: int


@dataclass(frozen=True, slots=True)
class TaskCatalogMigrationResult:
    marker_path: Path
    migrated: tuple[MigratedTask, ...] = ()
    already_migrated_task_ids: tuple[str, ...] = ()
    skipped_task_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LegacyCheckpointMigrationResult:
    marker_path: Path
    migrated: MigratedTask | None = None
    already_migrated: bool = False


@dataclass(frozen=True, slots=True)
class _ProfileRoots:
    state_root: Path
    config_root: Path
    template_config_path: Path

    @property
    def state_profiles(self) -> Path:
        return self.state_root / PROFILE_DIRECTORY_NAME

    @property
    def config_profiles(self) -> Path:
        return self.config_root / PROFILE_DIRECTORY_NAME

    @property
    def allocation_lock_path(self) -> Path:
        return self.state_profiles / ALLOCATION_LOCK_NAME

    @property
    def migration_marker_path(self) -> Path:
        return self.state_profiles / MIGRATION_MARKER_NAME

    @property
    def legacy_migration_marker_path(self) -> Path:
        return self.state_profiles / LEGACY_MIGRATION_MARKER_NAME


def acquire_instance_profile(
    profile_number: int | str | None = None,
    *,
    state_root: str | Path | None = None,
    config_root: str | Path | None = None,
    template_config_path: str | Path | None = None,
) -> InstanceProfile:
    """Claim one persistent GUI slot until the returned profile is closed."""

    roots = _profile_roots(state_root, config_root, template_config_path)
    roots.state_profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
    allocation_lock = FileLock(str(roots.allocation_lock_path))
    try:
        allocation_lock.acquire(timeout=ALLOCATION_TIMEOUT_SECONDS)
    except Timeout as exc:
        raise InstanceProfileError("等待 DcmGet 实例分配锁超时") from exc
    try:
        requested = (
            _normalize_profile_number(profile_number)
            if profile_number is not None
            else None
        )
        if requested is not None:
            slot_lock = _try_acquire_slot_lock(roots, requested)
            if slot_lock is None:
                raise ProfileInUseError(f"实例 {requested} 已在运行")
            return _initialize_claimed_profile(roots, requested, slot_lock)

        known_numbers = _known_profile_numbers(roots)
        recovery_numbers = [
            number
            for number in known_numbers
            if _task_state_path(roots, number).is_file()
        ]
        for number in recovery_numbers:
            slot_lock = _try_acquire_slot_lock(roots, number)
            if slot_lock is not None:
                return _initialize_claimed_profile(roots, number, slot_lock)

        for number in _candidate_profile_numbers(known_numbers):
            slot_lock = _try_acquire_slot_lock(roots, number)
            if slot_lock is not None:
                return _initialize_claimed_profile(roots, number, slot_lock)
        raise InstanceProfileError("没有可用的 DcmGet 实例槽位")
    finally:
        allocation_lock.release()


def instance_activation_path(
    profile_number: int | str,
    *,
    state_root: str | Path | None = None,
) -> Path:
    number = _normalize_profile_number(profile_number)
    root = Path(state_root or application_state_dir()).expanduser().resolve()
    return root / PROFILE_DIRECTORY_NAME / f"i{number}" / "gui-instance.json"


def migrate_task_catalog_to_profiles(
    catalog_path: str | Path,
    *,
    state_root: str | Path | None = None,
    config_root: str | Path | None = None,
    template_config_path: str | Path | None = None,
) -> TaskCatalogMigrationResult:
    """Copy resumable 2.8 catalog tasks into independent single-task slots.

    The source catalog is opened read-only and is never renamed, deleted or
    updated.  A task-id marker prevents a completed imported task from being
    recreated by a later launch after its per-slot checkpoint has been cleared.
    """

    source = Path(catalog_path).expanduser().resolve()
    roots = _profile_roots(state_root, config_root, template_config_path)
    marker_path = roots.migration_marker_path
    if not source.is_file():
        return TaskCatalogMigrationResult(marker_path=marker_path)

    catalog_lease = FileLock(str(source) + ".foreground.lock")
    try:
        catalog_lease.acquire(timeout=MIGRATION_LOCK_TIMEOUT_SECONDS)
    except Timeout as exc:
        raise InstanceProfileError(
            "旧版多任务目录仍被另一个 DcmGet 调度器使用"
        ) from exc
    try:
        if marker_path.is_file():
            marker_ids = _read_migration_marker(marker_path, source)
            return TaskCatalogMigrationResult(
                marker_path=marker_path,
                already_migrated_task_ids=tuple(sorted(marker_ids)),
            )
        checkpoints, skipped, process_records = _read_migratable_catalog(source)
        _cleanup_legacy_processes(process_records)
        roots.state_profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
        allocation_lock = FileLock(str(roots.allocation_lock_path))
        try:
            allocation_lock.acquire(timeout=ALLOCATION_TIMEOUT_SECONDS)
        except Timeout as exc:
            raise InstanceProfileError("等待 DcmGet 实例分配锁超时") from exc
        try:
            existing = _existing_checkpoint_profiles(roots)
            migrated: list[MigratedTask] = []
            already: list[str] = []
            completed_ids: set[str] = set()

            for checkpoint in checkpoints:
                existing_number = existing.get(checkpoint.task_id)
                if existing_number is not None:
                    already.append(checkpoint.task_id)
                    completed_ids.add(checkpoint.task_id)
                    continue

                number, slot_lock = _claim_empty_migration_slot(roots)
                try:
                    config_path = _config_path(roots, number)
                    if not config_path.is_file():
                        save_config(config_path, checkpoint.config)
                        _make_private(config_path)
                    store = TaskCheckpointStore(_task_state_path(roots, number))
                    store.import_checkpoint(checkpoint)
                finally:
                    slot_lock.release()
                migrated.append(MigratedTask(checkpoint.task_id, number))
                existing[checkpoint.task_id] = number
                completed_ids.add(checkpoint.task_id)

            _write_migration_marker(marker_path, source, completed_ids)
            return TaskCatalogMigrationResult(
                marker_path=marker_path,
                migrated=tuple(migrated),
                already_migrated_task_ids=tuple(already),
                skipped_task_ids=tuple(skipped),
            )
        finally:
            allocation_lock.release()
    finally:
        catalog_lease.release()


def migrate_legacy_checkpoint_to_profile(
    legacy_path: str | Path,
    *,
    state_root: str | Path | None = None,
    config_root: str | Path | None = None,
    template_config_path: str | Path | None = None,
) -> LegacyCheckpointMigrationResult:
    """Copy one pre-2.8 active checkpoint into an independent profile slot."""

    source = Path(legacy_path).expanduser().resolve()
    roots = _profile_roots(state_root, config_root, template_config_path)
    marker_path = roots.legacy_migration_marker_path
    if not source.is_file():
        return LegacyCheckpointMigrationResult(marker_path=marker_path)

    source_lease = FileLock(str(source) + ".lock")
    try:
        source_lease.acquire(timeout=MIGRATION_LOCK_TIMEOUT_SECONDS)
    except Timeout as exc:
        raise InstanceProfileError(
            "旧版单任务恢复点仍被另一个 DcmGet 实例使用"
        ) from exc
    try:
        if marker_path.is_file():
            _read_source_marker(marker_path, source)
            return LegacyCheckpointMigrationResult(
                marker_path=marker_path,
                already_migrated=True,
            )
        try:
            checkpoint = TaskCheckpointStore(source).load_required()
        except TaskStateError as exc:
            raise InstanceProfileError(f"无法读取旧版单任务恢复点：{exc}") from exc
        process_records = _read_legacy_checkpoint_processes(source)
        _cleanup_legacy_processes(process_records)

        roots.state_profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
        allocation_lock = FileLock(str(roots.allocation_lock_path))
        try:
            allocation_lock.acquire(timeout=ALLOCATION_TIMEOUT_SECONDS)
        except Timeout as exc:
            raise InstanceProfileError("等待 DcmGet 实例分配锁超时") from exc
        try:
            existing = _existing_checkpoint_profiles(roots)
            existing_number = existing.get(checkpoint.task_id)
            if existing_number is not None:
                _write_source_marker(
                    marker_path,
                    source,
                    {checkpoint.task_id},
                )
                return LegacyCheckpointMigrationResult(
                    marker_path=marker_path,
                    already_migrated=True,
                )

            number, slot_lock = _claim_empty_migration_slot(roots)
            try:
                config_path = _config_path(roots, number)
                if not config_path.is_file():
                    save_config(config_path, checkpoint.config)
                    _make_private(config_path)
                TaskCheckpointStore(
                    _task_state_path(roots, number)
                ).import_checkpoint(checkpoint)
            finally:
                slot_lock.release()
            _write_source_marker(
                marker_path,
                source,
                {checkpoint.task_id},
            )
            return LegacyCheckpointMigrationResult(
                marker_path=marker_path,
                migrated=MigratedTask(checkpoint.task_id, number),
            )
        finally:
            allocation_lock.release()
    finally:
        source_lease.release()


def _profile_roots(
    state_root: str | Path | None,
    config_root: str | Path | None,
    template_config_path: str | Path | None,
) -> _ProfileRoots:
    template = Path(template_config_path or default_config_path()).expanduser()
    canonical_config_root = Path(config_root or default_config_path().parent)
    return _ProfileRoots(
        state_root=Path(state_root or application_state_dir()).expanduser().resolve(),
        config_root=canonical_config_root.expanduser().resolve(),
        template_config_path=template.resolve(),
    )


def _normalize_profile_number(value: int | str) -> int:
    if isinstance(value, bool):
        raise InstanceProfileError("实例编号必须是正整数")
    if isinstance(value, str) and not re.fullmatch(r"\+?[1-9][0-9]*", value.strip()):
        raise InstanceProfileError("实例编号必须是正整数")
    if not isinstance(value, (int, str)):
        raise InstanceProfileError("实例编号必须是正整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InstanceProfileError("实例编号必须是正整数") from exc
    if number < 1 or number > 9999:
        raise InstanceProfileError("实例编号必须在 1 到 9999 之间")
    return number


def _known_profile_numbers(roots: _ProfileRoots) -> list[int]:
    numbers = {1}
    for directory in (roots.state_profiles, roots.config_profiles):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            name = path.name[:-5] if path.name.endswith(".lock") else path.name
            match = _SLOT_NAME.fullmatch(name)
            if match:
                numbers.add(int(match.group(1)))
    return sorted(numbers)


def _candidate_profile_numbers(known_numbers: list[int]) -> range:
    return range(1, max(known_numbers, default=0) + 2)


def _state_directory(roots: _ProfileRoots, number: int) -> Path:
    return roots.state_profiles / f"i{number}"


def _config_path(roots: _ProfileRoots, number: int) -> Path:
    return roots.config_profiles / f"i{number}" / "config.json"


def _task_state_path(roots: _ProfileRoots, number: int) -> Path:
    return _state_directory(roots, number) / "active-task.sqlite3"


def _slot_lock_path(roots: _ProfileRoots, number: int) -> Path:
    return roots.state_profiles / f"i{number}.lock"


def _try_acquire_slot_lock(roots: _ProfileRoots, number: int) -> FileLock | None:
    slot_lock = FileLock(str(_slot_lock_path(roots, number)))
    try:
        slot_lock.acquire(timeout=0)
    except Timeout:
        return None
    return slot_lock


def _initialize_claimed_profile(
    roots: _ProfileRoots,
    number: int,
    slot_lock: FileLock,
) -> InstanceProfile:
    try:
        state_directory = _state_directory(roots, number)
        log_directory = state_directory / "logs"
        state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        config_path = _config_path(roots, number)
        _ensure_profile_config(roots, number, config_path)
        label = f"实例 {number}"
        try:
            from .profile_manager import read_profile_display_name

            label = read_profile_display_name(config_path, number)
        except Exception:
            # A cosmetic metadata problem must never prevent the downloader
            # from opening; Profile 管理 will surface and repair it separately.
            pass
        return InstanceProfile(
            number=number,
            config_path=config_path,
            state_directory=state_directory,
            task_state_path=state_directory / "active-task.sqlite3",
            log_directory=log_directory,
            settings_name=f"DcmGet2-i{number}",
            label=label,
            _slot_lock=slot_lock,
        )
    except Exception:
        slot_lock.release()
        raise


def _ensure_profile_config(
    roots: _ProfileRoots,
    number: int,
    target: Path,
) -> None:
    if target.is_file():
        return
    legacy_custom_profile = (
        roots.template_config_path.parent
        / PROFILE_DIRECTORY_NAME
        / f"i{number}"
        / "config.json"
    )
    if (
        legacy_custom_profile.is_file()
        and legacy_custom_profile.resolve() != target.resolve()
    ):
        source = legacy_custom_profile
        preserve_source_ports = True
    elif number > 1 and _config_path(roots, 1).is_file():
        source = _config_path(roots, 1)
        preserve_source_ports = False
    else:
        source = roots.template_config_path
        preserve_source_ports = False
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not source.is_file():
        config = AppConfig()
        _move_reserved_profile_ports(roots, config)
        if number > 1 and not preserve_source_ports:
            config.storage_port = _next_profile_port(roots, config.storage_port)
            config.web_port = _next_profile_port(
                roots, config.web_port, reserved={config.storage_port}
            )
        save_config(target, config)
        _make_private(target)
        return
    try:
        config = load_config(source)
        _move_reserved_profile_ports(roots, config)
        if number > 1 and not preserve_source_ports:
            config.storage_port = _next_profile_port(roots, config.storage_port)
            config.web_port = _next_profile_port(
                roots, config.web_port, reserved={config.storage_port}
            )
        save_config(target, config)
        _make_private(target)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise InstanceProfileError(f"无法初始化实例 {number} 配置：{exc}") from exc


def _next_profile_port(
    roots: _ProfileRoots,
    starting_port: int,
    *,
    reserved: set[int] | None = None,
) -> int:
    used_ports: set[int] = set(reserved or ())
    used_ports.update(RESERVED_PROFILE_PORTS)
    if roots.config_profiles.is_dir():
        for path in roots.config_profiles.glob("i*/config.json"):
            try:
                config = load_config(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            used_ports.update((config.storage_port, config.web_port))
    start = starting_port + 1 if starting_port < 65535 else 1024
    for candidate in (*range(start, 65536), *range(1024, start)):
        if candidate not in used_ports:
            return candidate
    raise InstanceProfileError("没有可用的 Profile 服务端口")


def _move_reserved_profile_ports(roots: _ProfileRoots, config: AppConfig) -> None:
    if config.storage_port in RESERVED_PROFILE_PORTS:
        config.storage_port = _next_profile_port(
            roots,
            config.storage_port,
            reserved={config.web_port},
        )
    if config.web_port in RESERVED_PROFILE_PORTS:
        config.web_port = _next_profile_port(
            roots,
            config.web_port,
            reserved={config.storage_port},
        )


def _read_migratable_catalog(
    catalog_path: Path,
) -> tuple[
    list[TaskCheckpoint],
    list[str],
    list[tuple[str, dict[str, object]]],
]:
    uri = catalog_path.as_uri() + "?mode=ro"
    checkpoints: list[TaskCheckpoint] = []
    skipped: list[str] = []
    process_records: list[tuple[str, dict[str, object]]] = []
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            rows = connection.execute(
                """
                SELECT task_id, phase, config_json, trial_required,
                       pdi_attempt_id, created_at
                FROM tasks
                ORDER BY created_at, task_id
                """
            ).fetchall()
            for task_id, phase, config_json, trial_required, pdi_attempt_id, created_at in rows:
                raw_config = json.loads(str(config_json))
                if not isinstance(raw_config, dict):
                    raise ValueError("invalid config")
                accessions: list[str] = []
                results = []
                partial_results = {}
                for accession, result_json, partial_json in connection.execute(
                    """
                    SELECT accession, result_json, partial_json
                    FROM accessions
                    WHERE task_id = ?
                    ORDER BY position
                    """,
                    (str(task_id),),
                ):
                    value = str(accession)
                    accessions.append(value)
                    if result_json:
                        results.append(_result_from_json(str(result_json)))
                    if partial_json:
                        partial_results[value] = _result_from_json(
                            str(partial_json)
                        )
                checkpoint_phase = _migration_phase(
                    str(phase), accessions, results
                )
                if checkpoint_phase is None:
                    skipped.append(str(task_id))
                    continue
                checkpoint = TaskCheckpoint(
                    task_id=str(task_id),
                    config=AppConfig.from_dict(raw_config),
                    accessions=accessions,
                    results=results,
                    partial_results=partial_results,
                    trial_required=bool(trial_required),
                    created_at=str(created_at),
                    phase=checkpoint_phase,
                    pdi_attempt_id=str(pdi_attempt_id or ""),
                )
                TaskCheckpointStore._validate_checkpoint(checkpoint)
                checkpoints.append(checkpoint)
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "task_processes" in tables:
                for row in connection.execute(
                    """
                    SELECT task_id, kind, pid, process_created_at, executable,
                           command_line_json, process_group_id
                    FROM task_processes
                    ORDER BY task_id, kind
                    """
                ):
                    task_id, kind = str(row[0]), str(row[1])
                    if kind not in _PROCESS_KINDS:
                        raise ValueError("invalid process kind")
                    process_records.append(
                        (
                            f"{kind}（任务 {task_id[:8]}）",
                            _catalog_process_identity(row[2:]),
                        )
                    )
            if "receiver_sessions" in tables:
                for row in connection.execute(
                    """
                    SELECT session_id, pid, process_created_at, executable,
                           command_line_json, process_group_id
                    FROM receiver_sessions
                    ORDER BY started_at, session_id
                    """
                ):
                    session_id = str(row[0])
                    process_records.append(
                        (
                            f"storescp（接收会话 {session_id[:8]}）",
                            _catalog_process_identity(row[1:]),
                        )
                    )
    except TaskStateError as exc:
        raise InstanceProfileError(f"旧版多任务目录内容无效：{exc}") from exc
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InstanceProfileError(f"无法读取旧版多任务目录：{exc}") from exc
    return checkpoints, skipped, process_records


def _read_legacy_checkpoint_processes(
    checkpoint_path: Path,
) -> list[tuple[str, dict[str, object]]]:
    uri = checkpoint_path.as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            rows = list(
                connection.execute(
                    """
                    SELECT key, value FROM metadata
                    WHERE key LIKE 'process:%'
                    ORDER BY key
                    """
                )
            )
        records: list[tuple[str, dict[str, object]]] = []
        for key, raw in rows:
            kind = str(key).partition(":")[2]
            if kind not in _PROCESS_KINDS:
                raise ValueError("invalid process kind")
            identity = json.loads(str(raw))
            if not isinstance(identity, dict):
                raise ValueError("invalid process identity")
            records.append((kind, _validated_process_identity(identity)))
        return records
    except (
        KeyError,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise InstanceProfileError(
            f"无法读取旧版单任务后台进程记录：{exc}"
        ) from exc


def _catalog_process_identity(row: tuple[object, ...]) -> dict[str, object]:
    command_line = json.loads(str(row[3]))
    return _validated_process_identity(
        {
            "pid": row[0],
            "created_at": row[1],
            "executable": row[2],
            "command_line": command_line,
            "process_group_id": row[4],
        }
    )


def _validated_process_identity(
    identity: dict[str, object],
) -> dict[str, object]:
    command_line = identity.get("command_line")
    if not isinstance(command_line, list):
        raise ValueError("invalid command line")
    pid = int(identity["pid"])
    created_at = float(identity["created_at"])
    executable = str(identity["executable"])
    process_group_id = int(identity.get("process_group_id", 0))
    if (
        pid <= 0
        or not math.isfinite(created_at)
        or created_at < 0
        or not executable
        or process_group_id < 0
    ):
        raise ValueError("invalid process identity")
    return {
        "pid": pid,
        "created_at": created_at,
        "executable": executable,
        "command_line": [str(value) for value in command_line],
        "process_group_id": process_group_id,
    }


def _cleanup_legacy_processes(
    records: list[tuple[str, dict[str, object]]],
) -> None:
    seen: set[tuple[object, ...]] = set()
    for label, identity in records:
        key = (
            identity["pid"],
            identity["created_at"],
            identity["executable"],
            tuple(identity["command_line"]),
            identity["process_group_id"],
        )
        if key in seen:
            continue
        seen.add(key)
        resolved, message = _cleanup_process_identity(identity, label)
        if not resolved:
            raise InstanceProfileError(
                message or f"无法安全清理旧版后台进程 {label}"
            )


def _migration_phase(
    phase: str,
    accessions: list[str],
    results: list[object],
) -> str | None:
    if phase in _DIRECTLY_RESUMABLE_PHASES:
        return "downloading"
    if phase == "download_retryable" or phase in _PDI_PHASES:
        return phase
    if phase != "failed":
        return None
    retryable = any(
        getattr(result, "status", None)
        in {AccessionStatus.FAILED, AccessionStatus.PARTIAL}
        for result in results
    )
    if retryable:
        return "download_retryable"
    completed = {str(getattr(result, "accession", "")) for result in results}
    return "downloading" if any(value not in completed for value in accessions) else None


def _existing_checkpoint_profiles(roots: _ProfileRoots) -> dict[str, int]:
    existing: dict[str, int] = {}
    for number in _known_profile_numbers(roots):
        path = _task_state_path(roots, number)
        if not path.is_file():
            continue
        try:
            checkpoint = TaskCheckpointStore(path).load_required(
                include_archived_files=False
            )
        except TaskStateError as exc:
            raise InstanceProfileError(
                f"实例 {number} 的任务恢复点无法读取：{exc}"
            ) from exc
        prior = existing.get(checkpoint.task_id)
        if prior is not None and prior != number:
            raise InstanceProfileError(
                f"任务 {checkpoint.task_id} 同时存在于实例 {prior} 和 {number}"
            )
        existing[checkpoint.task_id] = number
    return existing


def _claim_empty_migration_slot(roots: _ProfileRoots) -> tuple[int, FileLock]:
    for number in _candidate_profile_numbers(_known_profile_numbers(roots)):
        if _task_state_path(roots, number).exists():
            continue
        slot_lock = _try_acquire_slot_lock(roots, number)
        if slot_lock is not None:
            return number, slot_lock
    raise InstanceProfileError("没有可用于迁移旧任务的实例槽位")


def _read_migration_marker(marker_path: Path, catalog_path: Path) -> set[str]:
    return _read_task_id_marker(marker_path, catalog_path, source_key="catalog")


def _read_source_marker(marker_path: Path, source_path: Path) -> set[str]:
    return _read_task_id_marker(marker_path, source_path, source_key="source")


def _read_task_id_marker(
    marker_path: Path,
    source_path: Path,
    *,
    source_key: str,
) -> set[str]:
    if not marker_path.is_file():
        return set()
    try:
        raw = json.loads(marker_path.read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or raw.get("version") != MIGRATION_VERSION
            or raw.get(source_key) != str(source_path)
            or not isinstance(raw.get("task_ids"), list)
        ):
            raise ValueError("invalid marker")
        task_ids = {str(task_id) for task_id in raw["task_ids"]}
        if any(not re.fullmatch(r"[0-9a-f]{32}", task_id) for task_id in task_ids):
            raise ValueError("invalid task id")
        return task_ids
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise InstanceProfileError(f"旧任务迁移标记已损坏：{exc}") from exc


def _write_migration_marker(
    marker_path: Path,
    catalog_path: Path,
    task_ids: set[str],
) -> None:
    _write_task_id_marker(
        marker_path,
        catalog_path,
        task_ids,
        source_key="catalog",
    )


def _write_source_marker(
    marker_path: Path,
    source_path: Path,
    task_ids: set[str],
) -> None:
    _write_task_id_marker(
        marker_path,
        source_path,
        task_ids,
        source_key="source",
    )


def _write_task_id_marker(
    marker_path: Path,
    source_path: Path,
    task_ids: set[str],
    *,
    source_key: str,
) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = marker_path.with_name(
        f".{marker_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    payload = {
        source_key: str(source_path),
        "migrated_at": datetime.now(timezone.utc).isoformat(),
        "task_ids": sorted(task_ids),
        "version": MIGRATION_VERSION,
    }
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _make_private(temporary)
        os.replace(temporary, marker_path)
    except OSError as exc:
        raise InstanceProfileError(f"无法保存旧任务迁移标记：{exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _make_private(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        path.chmod(0o600)
    except OSError:
        pass
