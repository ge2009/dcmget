from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from filelock import FileLock, Timeout
import psutil

from .config import AppConfig
from .core import AccessionResult, AccessionStatus
from .licensing import trial_task_consumed
from .pdi import PdiExportResult, PdiStatus
from .runtime import ensure_application_state_dir
from .task_state import (
    FINAL_STATUSES,
    TaskCheckpoint,
    TaskCheckpointStore,
    TaskStateError,
    _merge_partial_result,
    _cleanup_recorded_process_group,
    _normalized_executable,
    _process_executable,
    _result_from_json,
    _result_to_json,
    default_task_state_path,
)


CATALOG_VERSION = 1
RUNNABLE_PHASES = {"queued", "running", "pause_pending"}
TERMINAL_PHASES = {"cancelled", "completed", "failed"}
DELETABLE_TASK_PHASES = {
    *TERMINAL_PHASES,
    "download_retryable",
    "pdi_retryable",
}
RECEIVER_TASK_PHASES = {
    *RUNNABLE_PHASES,
    "paused",
    "cancelling",
    "download_retryable",
}
TASK_PHASES = {
    *RUNNABLE_PHASES,
    *TERMINAL_PHASES,
    "paused",
    "cancelling",
    "download_retryable",
    "pdi_pending",
    "pdi_running",
    "pdi_retryable",
}
SHARED_RECEIVER_CONFIG_FIELDS = (
    "dcmtk_bin_dir",
)
PROCESS_KINDS = {"storescp", "movescu", "pdi"}


def default_task_catalog_path() -> Path:
    return ensure_application_state_dir() / "tasks.sqlite3"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def shared_receiver_config(config: AppConfig) -> tuple[object, ...]:
    """Return persisted settings that must match across unfinished tasks."""

    return tuple(getattr(config, field) for field in SHARED_RECEIVER_CONFIG_FIELDS)


def receiver_key(config: AppConfig) -> tuple[str, int]:
    """Return the PACS move-destination mapping used by one receiver service."""

    return config.storage_ae_title.strip(), int(config.storage_port)


def _capture_process(pid: int, executable: str | Path) -> dict[str, object] | None:
    try:
        process = psutil.Process(int(pid))
        created_at = process.create_time()
        command_line = process.cmdline()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    process_group_id = 0
    if os.name != "nt":
        try:
            candidate_group = os.getpgid(int(pid))
        except OSError:
            candidate_group = 0
        if candidate_group == int(pid):
            process_group_id = candidate_group
    return {
        "command_line": command_line,
        "created_at": created_at,
        "executable": _normalized_executable(executable),
        "pid": int(pid),
        "process_group_id": process_group_id,
    }


def _cleanup_process_identity(
    record: dict[str, object], label: str
) -> tuple[bool, str]:
    """Terminate a recorded process only after PID identity verification."""

    try:
        pid = int(record["pid"])
        expected_created_at = float(record["created_at"])
        expected_executable = _normalized_executable(record["executable"])
        process = psutil.Process(pid)
        if abs(process.create_time() - expected_created_at) > 0.01:
            return True, f"未清理 PID {pid}：进程标识已经变化"
        if _process_executable(process) != expected_executable:
            return True, f"未清理 PID {pid}：可执行文件与恢复记录不一致"
        targets = [*process.children(recursive=True), process]
        for target in targets:
            try:
                target.terminate()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
        _gone, alive = psutil.wait_procs(targets, timeout=3)
        for target in alive:
            try:
                target.kill()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
        if alive:
            _gone, alive = psutil.wait_procs(alive, timeout=3)
        if alive:
            return False, f"未能清理上次的 {label} 进程 PID {pid}"
        return True, f"已清理上次异常退出遗留的 {label} 进程 PID {pid}"
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        group_result = _cleanup_recorded_process_group(
            record,
            expected_executable,
            expected_created_at,
        )
        if group_result == "cleaned":
            return True, f"已清理上次异常退出遗留的 {label} 进程组 PID {pid}"
        if group_result == "unsafe":
            return False, f"未清理 PID {pid} 的遗留进程组：进程身份无法安全确认"
        return True, ""
    except (OSError, psutil.Error) as exc:
        return False, f"未能清理上次的 {label} 进程：{exc}"


@dataclass(frozen=True, slots=True)
class TaskSummary:
    task_id: str
    name: str
    phase: str
    total_count: int
    processed_count: int
    pending_count: int
    completed_count: int
    failed_count: int
    file_count: int
    received_bytes: int
    speed_bytes_per_second: float
    queue_position: int | None
    current_accession: str
    error_message: str
    created_at: str
    updated_at: str
    no_data_count: int = 0
    partial_count: int = 0
    cancelled_count: int = 0

    @property
    def completed_only_count(self) -> int:
        """Completed rows excluding the separately reported no-data rows."""

        return max(0, self.completed_count - self.no_data_count)

    @property
    def failed_only_count(self) -> int:
        """Failed rows excluding the separately reported partial rows."""

        return max(0, self.failed_count - self.partial_count)

    @property
    def status(self) -> str:
        """Compatibility alias for views that call the task state ``status``."""

        return self.phase

    @property
    def accession_count(self) -> int:
        return self.total_count


@dataclass(slots=True)
class TaskRecord:
    summary: TaskSummary
    config: AppConfig
    accessions: list[str]
    results: list[AccessionResult]
    partial_results: dict[str, AccessionResult]
    trial_required: bool
    trial_consumed: bool
    pdi_attempt_id: str = ""

    @property
    def task_id(self) -> str:
        return self.summary.task_id

    @property
    def pending_accessions(self) -> list[str]:
        completed = {result.accession for result in self.results}
        return [value for value in self.accessions if value not in completed]


@dataclass(slots=True)
class TaskDetail:
    """Bounded task data for interactive views."""

    summary: TaskSummary
    config: AppConfig
    accessions: list[str]
    results: list[AccessionResult]
    partial_results: dict[str, AccessionResult]
    trial_required: bool
    trial_consumed: bool
    truncated: bool
    loaded_count: int
    pdi_attempt_id: str = ""

    @property
    def task_id(self) -> str:
        return self.summary.task_id


class TaskCatalog:
    """SQLite-backed catalog containing any number of resumable tasks."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        legacy_path: str | Path | None = None,
        auto_migrate: bool = True,
    ):
        self.path = Path(path).expanduser() if path else default_task_catalog_path()
        self.legacy_path = (
            Path(legacy_path).expanduser()
            if legacy_path is not None
            else (
                self.path.with_name("active-task.sqlite3")
                if path is not None
                else default_task_state_path()
            )
        )
        self._lock = threading.RLock()
        self._foreground_lease = FileLock(str(self.path) + ".foreground.lock")
        self._initialize()
        if auto_migrate and self.legacy_path != self.path:
            self.migrate_legacy()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @property
    def foreground_lease_held(self) -> bool:
        return self._foreground_lease.is_locked

    def try_acquire_foreground_lease(self) -> bool:
        """Ensure only one GUI or CLI scheduler can execute catalog tasks."""

        if self._foreground_lease.is_locked:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._foreground_lease.acquire(timeout=0)
        except Timeout:
            return False
        return True

    def release_foreground_lease(self) -> None:
        if self._foreground_lease.is_locked:
            self._foreground_lease.release()

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with (
                closing(sqlite3.connect(self.path, timeout=5)) as connection,
                connection,
            ):
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS catalog_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        config_json TEXT NOT NULL,
                        trial_required INTEGER NOT NULL,
                        trial_consumed INTEGER NOT NULL DEFAULT 0,
                        pdi_attempt_id TEXT NOT NULL DEFAULT '',
                        current_accession TEXT NOT NULL DEFAULT '',
                        speed_bytes_per_second REAL NOT NULL DEFAULT 0,
                        error_message TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS accessions (
                        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                        position INTEGER NOT NULL,
                        accession TEXT NOT NULL,
                        status TEXT,
                        file_count INTEGER NOT NULL DEFAULT 0,
                        received_bytes INTEGER NOT NULL DEFAULT 0,
                        speed_bytes_per_second REAL NOT NULL DEFAULT 0,
                        result_json TEXT,
                        partial_json TEXT,
                        PRIMARY KEY(task_id, position),
                        UNIQUE(task_id, accession)
                    );
                    CREATE INDEX IF NOT EXISTS accessions_pending
                    ON accessions(task_id, position) WHERE result_json IS NULL;
                    CREATE TABLE IF NOT EXISTS task_processes (
                        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        pid INTEGER NOT NULL,
                        process_created_at REAL NOT NULL,
                        executable TEXT NOT NULL,
                        command_line_json TEXT NOT NULL,
                        process_group_id INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(task_id, kind)
                    );
                    CREATE TABLE IF NOT EXISTS receiver_sessions (
                        session_id TEXT PRIMARY KEY,
                        pid INTEGER NOT NULL,
                        process_created_at REAL NOT NULL,
                        executable TEXT NOT NULL,
                        command_line_json TEXT NOT NULL,
                        process_group_id INTEGER NOT NULL DEFAULT 0,
                        started_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS pdi_results (
                        task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
                        status TEXT NOT NULL,
                        output_directory TEXT NOT NULL DEFAULT '',
                        message TEXT NOT NULL DEFAULT '',
                        warnings_json TEXT NOT NULL DEFAULT '[]',
                        source_count INTEGER NOT NULL DEFAULT 0,
                        exported_count INTEGER NOT NULL DEFAULT 0,
                        duplicate_count INTEGER NOT NULL DEFAULT 0,
                        indexed_count INTEGER NOT NULL DEFAULT 0,
                        strict_profile INTEGER,
                        core_tool_failure INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
                task_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(tasks)")
                }
                if "trial_consumed" not in task_columns:
                    connection.execute(
                        """
                        ALTER TABLE tasks
                        ADD COLUMN trial_consumed INTEGER NOT NULL DEFAULT 0
                        """
                    )
                if "pdi_attempt_id" not in task_columns:
                    connection.execute(
                        """
                        ALTER TABLE tasks
                        ADD COLUMN pdi_attempt_id TEXT NOT NULL DEFAULT ''
                        """
                    )
                version = connection.execute(
                    "SELECT value FROM catalog_metadata WHERE key = 'version'"
                ).fetchone()
                if version is not None and int(version[0]) != CATALOG_VERSION:
                    raise TaskStateError("多任务目录版本不受支持")
                connection.execute(
                    "INSERT OR REPLACE INTO catalog_metadata(key, value) VALUES ('version', ?)",
                    (str(CATALOG_VERSION),),
                )
            if os.name != "nt":
                self.path.chmod(0o600)
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise TaskStateError(f"无法初始化多任务目录：{exc}") from exc

    def migrate_legacy(self) -> TaskSummary | None:
        """Import the old single active task once without deleting its source."""

        legacy = TaskCheckpointStore(self.legacy_path)
        if not legacy.path.is_file():
            return None
        if not legacy.try_acquire_lease():
            return None
        try:
            checkpoint = legacy.load()
            if checkpoint is None:
                return None
            with self._lock:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    existing = connection.execute(
                        "SELECT 1 FROM tasks WHERE task_id = ?", (checkpoint.task_id,)
                    ).fetchone()
                    if existing is None:
                        conflicts = self._shared_config_conflicts(
                            connection,
                            checkpoint.config,
                        )
                        if conflicts:
                            fields = "、".join(conflicts)
                            raise TaskStateError(
                                "旧任务的应用全局运行配置与现有未完成任务不一致："
                                f"{fields}"
                            )
                        mapping_conflict = self._receiver_mapping_conflict(
                            connection,
                            checkpoint.config,
                        )
                        if mapping_conflict:
                            raise TaskStateError(
                                f"旧任务的接收映射冲突：{mapping_conflict}"
                            )
                        duplicates = self._active_duplicates(
                            connection,
                            checkpoint.accessions,
                        )
                        if duplicates:
                            examples = "、".join(duplicates[:3])
                            raise TaskStateError(
                                "旧任务检查号已存在于现有未完成任务中："
                                f"{examples}"
                            )
                        self._insert_checkpoint(connection, checkpoint)
                    connection.commit()
            backup = self._backup_legacy(checkpoint.task_id)
            with self._lock:
                with closing(self._connect()) as connection, connection:
                    connection.executemany(
                        "INSERT OR REPLACE INTO catalog_metadata(key, value) VALUES (?, ?)",
                        (
                            (f"legacy:{checkpoint.task_id}", str(self.legacy_path)),
                            (f"legacy_backup:{checkpoint.task_id}", str(backup)),
                        ),
                    )
            return self.get_summary(checkpoint.task_id)
        finally:
            legacy.release_lease()

    def _backup_legacy(self, task_id: str) -> Path:
        backup = self.legacy_path.with_name(
            f"{self.legacy_path.name}.pre-multitask.bak"
        )
        if backup.is_file():
            try:
                existing = TaskCheckpointStore(backup).load()
            except TaskStateError:
                existing = None
            if existing is not None and existing.task_id == task_id:
                return backup
            backup = self.legacy_path.with_name(
                f"{self.legacy_path.name}.pre-multitask-{task_id}.bak"
            )
            if backup.is_file():
                try:
                    existing = TaskCheckpointStore(backup).load()
                except TaskStateError as exc:
                    raise TaskStateError("旧任务备份文件已损坏") from exc
                if existing is None or existing.task_id != task_id:
                    raise TaskStateError("旧任务备份文件与当前任务不匹配")
                return backup
        temporary = backup.with_name(f".{backup.name}.{uuid.uuid4().hex}.tmp")
        try:
            with (
                closing(sqlite3.connect(self.legacy_path)) as source,
                closing(sqlite3.connect(temporary)) as destination,
            ):
                source.backup(destination)
            shutil.copystat(self.legacy_path, temporary)
            os.replace(temporary, backup)
            if os.name != "nt":
                backup.chmod(0o600)
            return backup
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法备份旧任务恢复点：{exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _insert_checkpoint(
        connection: sqlite3.Connection, checkpoint: TaskCheckpoint
    ) -> None:
        now = _utc_now()
        phase = {
            "downloading": "queued",
        }.get(checkpoint.phase, checkpoint.phase)
        if phase not in TASK_PHASES:
            phase = "queued"
        connection.execute(
            """
            INSERT INTO tasks(
                task_id, name, phase, config_json, trial_required, trial_consumed,
                pdi_attempt_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint.task_id,
                f"恢复任务 {checkpoint.task_id[:8]}",
                phase,
                json.dumps(
                    checkpoint.config.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                int(checkpoint.trial_required),
                int(
                    checkpoint.trial_required
                    and trial_task_consumed(checkpoint.task_id)
                ),
                checkpoint.pdi_attempt_id,
                checkpoint.created_at,
                now,
            ),
        )
        results = checkpoint.result_by_accession
        for position, accession in enumerate(checkpoint.accessions):
            result = results.get(accession)
            partial = checkpoint.partial_results.get(accession)
            displayed = result or partial
            connection.execute(
                """
                INSERT INTO accessions(
                    task_id, position, accession, status, file_count,
                    received_bytes, speed_bytes_per_second,
                    result_json, partial_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.task_id,
                    position,
                    accession,
                    result.status.value if result is not None else None,
                    displayed.file_count if displayed is not None else 0,
                    displayed.received_bytes if displayed is not None else 0,
                    (
                        displayed.speed_bytes_per_second
                        if displayed is not None
                        else 0.0
                    ),
                    _result_to_json(result) if result is not None else None,
                    _result_to_json(partial) if partial is not None else None,
                ),
            )

    def create_task(
        self,
        config: AppConfig,
        accessions: Iterable[str],
        *,
        trial_required: bool = False,
        name: str = "",
    ) -> TaskSummary:
        values = [str(value).strip() for value in accessions]
        if not values or any(not value for value in values):
            raise TaskStateError("任务至少需要一个有效检查号")
        if len(values) != len(set(values)):
            raise TaskStateError("同一任务中的检查号不能重复")
        task_id = uuid.uuid4().hex
        created_at = _utc_now()
        task_name = name.strip() or f"任务 {task_id[:8]}"
        payload = json.dumps(
            config.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            with self._lock:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        conflicts = self._shared_config_conflicts(connection, config)
                        if conflicts:
                            fields = "、".join(conflicts)
                            raise TaskStateError(
                                f"应用全局运行配置与未完成任务不一致：{fields}"
                            )
                        mapping_conflict = self._receiver_mapping_conflict(
                            connection,
                            config,
                        )
                        if mapping_conflict:
                            raise TaskStateError(
                                f"接收映射冲突：{mapping_conflict}"
                            )
                        duplicates = self._active_duplicates(connection, values)
                        if duplicates:
                            examples = "、".join(duplicates[:3])
                            raise TaskStateError(
                                f"检查号已存在于未完成任务中：{examples}"
                            )
                        connection.execute(
                            """
                            INSERT INTO tasks(
                                task_id, name, phase, config_json, trial_required,
                                created_at, updated_at
                            ) VALUES (?, ?, 'queued', ?, ?, ?, ?)
                            """,
                            (
                                task_id,
                                task_name,
                                payload,
                                int(trial_required),
                                created_at,
                                created_at,
                            ),
                        )
                        connection.executemany(
                            """
                            INSERT INTO accessions(task_id, position, accession)
                            VALUES (?, ?, ?)
                            """,
                            (
                                (task_id, position, accession)
                                for position, accession in enumerate(values)
                            ),
                        )
                    except Exception:
                        connection.rollback()
                        raise
                    connection.commit()
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法创建任务：{exc}") from exc
        return self.get_summary(task_id)

    @staticmethod
    def _shared_config_conflicts(
        connection: sqlite3.Connection,
        config: AppConfig,
        *,
        exclude_task_id: str = "",
    ) -> list[str]:
        placeholders = ", ".join("?" for _ in RECEIVER_TASK_PHASES)
        sql = f"SELECT task_id, config_json FROM tasks WHERE phase IN ({placeholders})"
        parameters: list[object] = [*sorted(RECEIVER_TASK_PHASES)]
        if exclude_task_id:
            sql += " AND task_id <> ?"
            parameters.append(exclude_task_id)
        expected = shared_receiver_config(config)
        for _task_id, raw_config in connection.execute(sql, parameters):
            try:
                existing = AppConfig.from_dict(json.loads(str(raw_config)))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TaskStateError("未完成任务的应用全局运行配置已损坏") from exc
            actual = shared_receiver_config(existing)
            if actual != expected:
                return [
                    field
                    for field, old, new in zip(
                        SHARED_RECEIVER_CONFIG_FIELDS, actual, expected
                    )
                    if old != new
                ]
        return []

    def validate_shared_config(
        self, config: AppConfig, *, exclude_task_id: str = ""
    ) -> None:
        try:
            with self._lock, closing(self._connect()) as connection:
                conflicts = self._shared_config_conflicts(
                    connection,
                    config,
                    exclude_task_id=exclude_task_id,
                )
                if conflicts:
                    raise TaskStateError(
                        "应用全局运行配置与未完成任务不一致：" + "、".join(conflicts)
                    )
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法校验应用全局运行配置：{exc}") from exc

    @staticmethod
    def _receiver_mapping_conflict(
        connection: sqlite3.Connection,
        config: AppConfig,
        *,
        exclude_task_id: str = "",
    ) -> str:
        placeholders = ", ".join("?" for _ in RECEIVER_TASK_PHASES)
        sql = f"SELECT task_id, config_json FROM tasks WHERE phase IN ({placeholders})"
        parameters: list[object] = [*sorted(RECEIVER_TASK_PHASES)]
        if exclude_task_id:
            sql += " AND task_id <> ?"
            parameters.append(exclude_task_id)
        expected_ae = config.storage_ae_title.strip()
        expected_port = int(config.storage_port)
        for _task_id, raw_config in connection.execute(sql, parameters):
            try:
                existing = AppConfig.from_dict(json.loads(str(raw_config)))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TaskStateError("未完成任务的接收映射配置已损坏") from exc
            existing_ae = existing.storage_ae_title
            existing_port = existing.storage_port
            if existing_port == expected_port and existing_ae != expected_ae:
                return (
                    f"端口 {expected_port} 已绑定接收 AE {existing_ae}，"
                    f"不能同时绑定 {expected_ae}"
                )
        return ""

    def validate_receiver_mappings(self) -> None:
        """Reject ambiguous AE-to-port mappings in restored download tasks."""

        try:
            with self._lock, closing(self._connect()) as connection:
                placeholders = ", ".join("?" for _ in RECEIVER_TASK_PHASES)
                rows = connection.execute(
                    f"SELECT config_json FROM tasks WHERE phase IN ({placeholders})",
                    [*sorted(RECEIVER_TASK_PHASES)],
                )
                port_to_ae: dict[int, str] = {}
                for (raw_config,) in rows:
                    try:
                        config = AppConfig.from_dict(json.loads(str(raw_config)))
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise TaskStateError(
                            "未完成任务的接收映射配置已损坏"
                        ) from exc
                    ae = config.storage_ae_title
                    port = config.storage_port
                    if port in port_to_ae and port_to_ae[port] != ae:
                        raise TaskStateError(
                            f"接收映射冲突：端口 {port} 同时绑定了接收 AE "
                            f"{port_to_ae[port]} 和 {ae}"
                        )
                    port_to_ae[port] = ae
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法校验接收映射：{exc}") from exc

    @staticmethod
    def _active_duplicates(
        connection: sqlite3.Connection,
        accessions: Iterable[str],
        *,
        exclude_task_id: str = "",
    ) -> list[str]:
        connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS candidate_accessions(accession TEXT PRIMARY KEY)"
        )
        connection.execute("DELETE FROM candidate_accessions")
        connection.executemany(
            "INSERT INTO candidate_accessions(accession) VALUES (?)",
            ((value,) for value in accessions),
        )
        conditions = ["t.phase NOT IN (?, ?, ?)"]
        parameters: list[object] = [*sorted(TERMINAL_PHASES)]
        if exclude_task_id:
            conditions.append("t.task_id <> ?")
            parameters.append(exclude_task_id)
        return [
            str(row[0])
            for row in connection.execute(
                f"""
                SELECT DISTINCT c.accession
                FROM candidate_accessions AS c
                JOIN accessions AS a ON a.accession = c.accession
                JOIN tasks AS t ON t.task_id = a.task_id
                WHERE {" AND ".join(conditions)}
                ORDER BY c.accession
                """,
                parameters,
            )
        ]

    def list_tasks(self) -> list[TaskSummary]:
        try:
            with self._lock, closing(self._connect()) as connection:
                task_ids = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT task_id FROM tasks ORDER BY created_at DESC"
                    )
                ]
                return [self._get_summary(connection, task_id) for task_id in task_ids]
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise TaskStateError(f"无法读取任务列表：{exc}") from exc

    def get_summary(self, task_id: str) -> TaskSummary:
        try:
            with self._lock, closing(self._connect()) as connection:
                return self._get_summary(connection, task_id)
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise TaskStateError(f"无法读取任务摘要：{exc}") from exc

    @staticmethod
    def _get_summary(connection: sqlite3.Connection, task_id: str) -> TaskSummary:
        task = connection.execute(
            """
            SELECT name, phase, current_accession, speed_bytes_per_second,
                   error_message, created_at, updated_at
            FROM tasks WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if task is None:
            raise TaskStateError("任务不存在")
        counts = connection.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(CASE WHEN status IS NOT NULL THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status IN (?, ?) THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status IN (?, ?) THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(file_count), 0),
                COALESCE(SUM(received_bytes), 0),
                COALESCE(SUM(CASE WHEN status = ? THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = ? THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = ? THEN 1 ELSE 0 END), 0)
            FROM accessions WHERE task_id = ?
            """,
            (
                AccessionStatus.COMPLETED.value,
                AccessionStatus.NO_DATA.value,
                AccessionStatus.FAILED.value,
                AccessionStatus.PARTIAL.value,
                AccessionStatus.NO_DATA.value,
                AccessionStatus.PARTIAL.value,
                AccessionStatus.CANCELLED.value,
                task_id,
            ),
        ).fetchone()
        if counts is None:
            raise TaskStateError("任务统计信息不存在")
        total = int(counts[0])
        processed = int(counts[1])
        return TaskSummary(
            task_id=task_id,
            name=str(task[0]),
            phase=str(task[1]),
            total_count=total,
            processed_count=processed,
            pending_count=max(0, total - processed),
            completed_count=int(counts[2]),
            failed_count=int(counts[3]),
            file_count=int(counts[4]),
            received_bytes=int(counts[5]),
            speed_bytes_per_second=float(task[3]),
            queue_position=None,
            current_accession=str(task[2]),
            error_message=str(task[4]),
            created_at=str(task[5]),
            updated_at=str(task[6]),
            no_data_count=int(counts[6]),
            partial_count=int(counts[7]),
            cancelled_count=int(counts[8]),
        )

    def get_task(self, task_id: str) -> TaskRecord:
        try:
            with self._lock, closing(self._connect()) as connection:
                task = connection.execute(
                    """
                    SELECT config_json, trial_required, trial_consumed, pdi_attempt_id
                    FROM tasks WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if task is None:
                    raise TaskStateError("任务不存在")
                raw_config = json.loads(str(task[0]))
                if not isinstance(raw_config, dict):
                    raise ValueError("invalid config")
                accessions: list[str] = []
                results: list[AccessionResult] = []
                partial_results: dict[str, AccessionResult] = {}
                for accession, result_json, partial_json in connection.execute(
                    """
                    SELECT accession, result_json, partial_json
                    FROM accessions WHERE task_id = ? ORDER BY position
                    """,
                    (task_id,),
                ):
                    value = str(accession)
                    accessions.append(value)
                    if result_json:
                        results.append(_result_from_json(str(result_json)))
                    if partial_json:
                        partial_results[value] = _result_from_json(str(partial_json))
                return TaskRecord(
                    summary=self._get_summary(connection, task_id),
                    config=AppConfig.from_dict(raw_config),
                    accessions=accessions,
                    results=results,
                    partial_results=partial_results,
                    trial_required=bool(task[1]),
                    trial_consumed=bool(task[2]),
                    pdi_attempt_id=str(task[3]),
                )
        except TaskStateError:
            raise
        except (
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise TaskStateError(f"无法读取任务：{exc}") from exc

    def get_task_detail(self, task_id: str, accession_limit: int = 201) -> TaskDetail:
        """Read a bounded task preview suitable for the UI detail panel."""

        if accession_limit < 1:
            raise TaskStateError("任务详情条数必须大于 0")
        try:
            with self._lock, closing(self._connect()) as connection:
                task = connection.execute(
                    """
                    SELECT config_json, trial_required, trial_consumed, pdi_attempt_id
                    FROM tasks WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if task is None:
                    raise TaskStateError("任务不存在")
                raw_config = json.loads(str(task[0]))
                if not isinstance(raw_config, dict):
                    raise ValueError("invalid config")
                accessions: list[str] = []
                results: list[AccessionResult] = []
                partial_results: dict[str, AccessionResult] = {}
                rows = connection.execute(
                    """
                    SELECT accession, result_json, partial_json
                    FROM accessions
                    WHERE task_id = ?
                    ORDER BY position
                    LIMIT ?
                    """,
                    (task_id, accession_limit),
                ).fetchall()
                for accession, result_json, partial_json in rows:
                    value = str(accession)
                    accessions.append(value)
                    if result_json:
                        results.append(_result_from_json(str(result_json)))
                    if partial_json:
                        partial_results[value] = _result_from_json(str(partial_json))
                summary = self._get_summary(connection, task_id)
                return TaskDetail(
                    summary=summary,
                    config=AppConfig.from_dict(raw_config),
                    accessions=accessions,
                    results=results,
                    partial_results=partial_results,
                    trial_required=bool(task[1]),
                    trial_consumed=bool(task[2]),
                    truncated=summary.total_count > len(accessions),
                    loaded_count=len(accessions),
                    pdi_attempt_id=str(task[3]),
                )
        except TaskStateError:
            raise
        except (
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise TaskStateError(f"无法读取任务详情：{exc}") from exc

    def list_accessions(
        self, task_id: str, *, limit: int = 201, offset: int = 0
    ) -> list[str]:
        """Return one bounded accession page without decoding result payloads."""

        if limit < 1 or offset < 0:
            raise TaskStateError("检查号分页参数无效")
        try:
            with self._lock, closing(self._connect()) as connection:
                if (
                    connection.execute(
                        "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
                    ).fetchone()
                    is None
                ):
                    raise TaskStateError("任务不存在")
                return [
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT accession FROM accessions
                        WHERE task_id = ? ORDER BY position LIMIT ? OFFSET ?
                        """,
                        (task_id, limit, offset),
                    )
                ]
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法读取检查号列表：{exc}") from exc

    def list_failed_accessions(self, task_id: str) -> list[str]:
        """Read failed/partial accession numbers without decoding result JSON."""

        try:
            with self._lock, closing(self._connect()) as connection:
                if (
                    connection.execute(
                        "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
                    ).fetchone()
                    is None
                ):
                    raise TaskStateError("任务不存在")
                return [
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT accession FROM accessions
                        WHERE task_id = ? AND status IN (?, ?)
                        ORDER BY position
                        """,
                        (
                            task_id,
                            AccessionStatus.FAILED.value,
                            AccessionStatus.PARTIAL.value,
                        ),
                    )
                ]
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法读取失败检查号：{exc}") from exc

    def get_config(self, task_id: str) -> AppConfig:
        """Read only the task snapshot needed to execute one scheduler turn."""

        try:
            with self._lock, closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT config_json FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise TaskStateError("任务不存在")
                raw = json.loads(str(row[0]))
                if not isinstance(raw, dict):
                    raise ValueError("invalid config")
                return AppConfig.from_dict(raw)
        except TaskStateError:
            raise
        except (
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise TaskStateError(f"无法读取任务配置：{exc}") from exc

    def trial_state(self, task_id: str) -> tuple[bool, bool]:
        try:
            with self._lock, closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT trial_required, trial_consumed
                    FROM tasks WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise TaskStateError("任务不存在")
                return bool(row[0]), bool(row[1])
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法读取任务试用状态：{exc}") from exc

    def mark_trial_consumed(self, task_id: str) -> bool:
        """Persist the first successful trial-consumption callback atomically."""

        try:
            with self._lock:
                with closing(self._connect()) as connection, connection:
                    cursor = connection.execute(
                        """
                        UPDATE tasks
                        SET trial_consumed = 1, updated_at = ?
                        WHERE task_id = ? AND trial_required = 1
                              AND trial_consumed = 0
                        """,
                        (_utc_now(), task_id),
                    )
                    if cursor.rowcount:
                        return True
                    if (
                        connection.execute(
                            "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
                        ).fetchone()
                        is None
                    ):
                        raise TaskStateError("任务不存在")
                    return False
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法更新任务试用状态：{exc}") from exc

    def begin_pdi_attempt(
        self,
        task_id: str,
        *,
        reuse_existing: bool,
    ) -> tuple[str, bool]:
        """Create or resume the persisted recovery identity for one PDI run."""

        try:
            with self._lock:
                with closing(self._connect()) as connection, connection:
                    row = connection.execute(
                        """
                        SELECT phase, pdi_attempt_id FROM tasks WHERE task_id = ?
                        """,
                        (task_id,),
                    ).fetchone()
                    if row is None:
                        raise TaskStateError("任务不存在")
                    existing = str(row[1])
                    can_reuse = bool(
                        reuse_existing
                        and str(row[0]) == "pdi_running"
                        and re.fullmatch(r"[0-9a-f]{32}", existing)
                    )
                    attempt_id = existing if can_reuse else uuid.uuid4().hex
                    connection.execute(
                        """
                        UPDATE tasks
                        SET phase = 'pdi_running', pdi_attempt_id = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (attempt_id, _utc_now(), task_id),
                    )
                    return attempt_id, can_reuse
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法建立 PDI 恢复点：{exc}") from exc

    def save_pdi_result(self, task_id: str, result: PdiExportResult) -> None:
        """Persist the latest PDI outcome independently from the task phase."""

        try:
            warnings_json = json.dumps(
                [str(warning) for warning in result.warnings],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            strict_profile = (
                None if result.strict_profile is None else int(result.strict_profile)
            )
            with self._lock:
                with closing(self._connect()) as connection, connection:
                    if (
                        connection.execute(
                            "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
                        ).fetchone()
                        is None
                    ):
                        raise TaskStateError("任务不存在")
                    connection.execute(
                        """
                        INSERT INTO pdi_results(
                            task_id, status, output_directory, message,
                            warnings_json, source_count, exported_count,
                            duplicate_count, indexed_count, strict_profile,
                            core_tool_failure, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(task_id) DO UPDATE SET
                            status = excluded.status,
                            output_directory = excluded.output_directory,
                            message = excluded.message,
                            warnings_json = excluded.warnings_json,
                            source_count = excluded.source_count,
                            exported_count = excluded.exported_count,
                            duplicate_count = excluded.duplicate_count,
                            indexed_count = excluded.indexed_count,
                            strict_profile = excluded.strict_profile,
                            core_tool_failure = excluded.core_tool_failure,
                            updated_at = excluded.updated_at
                        """,
                        (
                            task_id,
                            result.status.value,
                            str(result.output_directory),
                            str(result.message),
                            warnings_json,
                            int(result.source_count),
                            int(result.exported_count),
                            int(result.duplicate_count),
                            int(result.indexed_count),
                            strict_profile,
                            int(result.core_tool_failure),
                            _utc_now(),
                        ),
                    )
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise TaskStateError(f"无法保存 PDI 结果：{exc}") from exc

    def load_pdi_result(self, task_id: str) -> PdiExportResult | None:
        """Load the latest PDI outcome for task details after a restart."""

        try:
            with self._lock, closing(self._connect()) as connection:
                if (
                    connection.execute(
                        "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
                    ).fetchone()
                    is None
                ):
                    raise TaskStateError("任务不存在")
                row = connection.execute(
                    """
                    SELECT status, output_directory, message, warnings_json,
                           source_count, exported_count, duplicate_count,
                           indexed_count, strict_profile, core_tool_failure
                    FROM pdi_results WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if row is None:
                    return None
                warnings = json.loads(str(row[3]))
                if not isinstance(warnings, list) or not all(
                    isinstance(warning, str) for warning in warnings
                ):
                    raise ValueError("invalid PDI warnings")
                strict_profile = row[8]
                if strict_profile not in {None, 0, 1}:
                    raise ValueError("invalid PDI profile status")
                return PdiExportResult(
                    status=PdiStatus(str(row[0])),
                    output_directory=str(row[1]),
                    message=str(row[2]),
                    warnings=warnings,
                    source_count=int(row[4]),
                    exported_count=int(row[5]),
                    duplicate_count=int(row[6]),
                    indexed_count=int(row[7]),
                    strict_profile=(
                        None if strict_profile is None else bool(strict_profile)
                    ),
                    core_tool_failure=bool(row[9]),
                )
        except TaskStateError:
            raise
        except (
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise TaskStateError(f"无法读取 PDI 结果：{exc}") from exc

    def record_process(
        self,
        task_id: str,
        kind: str,
        pid: int,
        executable: str | Path,
        *,
        active: bool,
    ) -> None:
        """Persist one task-owned child process for crash-safe cleanup."""

        if kind not in PROCESS_KINDS:
            raise TaskStateError(f"不支持的后台进程类型：{kind}")
        try:
            with self._lock:
                with closing(self._connect()) as connection, connection:
                    if (
                        connection.execute(
                            "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
                        ).fetchone()
                        is None
                    ):
                        raise TaskStateError("任务不存在")
                    if not active:
                        connection.execute(
                            "DELETE FROM task_processes WHERE task_id = ? AND kind = ?",
                            (task_id, kind),
                        )
                        return
                    record = _capture_process(pid, executable)
                    if record is None:
                        return
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO task_processes(
                            task_id, kind, pid, process_created_at, executable,
                            command_line_json, process_group_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            task_id,
                            kind,
                            record["pid"],
                            record["created_at"],
                            record["executable"],
                            json.dumps(
                                record["command_line"],
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            record["process_group_id"],
                        ),
                    )
        except TaskStateError:
            raise
        except (OSError, psutil.Error, sqlite3.Error, TypeError, ValueError) as exc:
            raise TaskStateError(f"无法记录 {kind} 后台进程：{exc}") from exc

    def cleanup_recorded_processes(self, task_id: str) -> list[str]:
        """Clean task children left by a prior crash after identity checks."""

        try:
            with self._lock, closing(self._connect()) as connection:
                if (
                    connection.execute(
                        "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
                    ).fetchone()
                    is None
                ):
                    raise TaskStateError("任务不存在")
                rows = list(
                    connection.execute(
                        """
                        SELECT kind, pid, process_created_at, executable,
                               command_line_json, process_group_id
                        FROM task_processes WHERE task_id = ?
                        """,
                        (task_id,),
                    )
                )
        except TaskStateError:
            raise
        except sqlite3.Error as exc:
            raise TaskStateError(f"无法读取后台进程恢复信息：{exc}") from exc

        records = {str(row[0]): row for row in rows}
        messages: list[str] = []
        for kind in ("pdi", "movescu", "storescp"):
            row = records.get(kind)
            if row is None:
                continue
            try:
                record = self._process_record_from_row(row[1:])
                remove, message = _cleanup_process_identity(record, kind)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TaskStateError(f"{kind} 进程恢复记录已损坏") from exc
            if remove:
                self.record_process(task_id, kind, 0, "", active=False)
            if message:
                messages.append(message)
        return messages

    def begin_receiver_session(self, pid: int, executable: str | Path) -> str:
        """Persist the application-shared storescp process identity."""

        try:
            record = _capture_process(pid, executable)
            if record is None:
                raise TaskStateError("共享接收器进程已经退出")
            session_id = uuid.uuid4().hex
            with self._lock:
                with closing(self._connect()) as connection, connection:
                    connection.execute(
                        """
                        INSERT INTO receiver_sessions(
                            session_id, pid, process_created_at, executable,
                            command_line_json, process_group_id, started_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            record["pid"],
                            record["created_at"],
                            record["executable"],
                            json.dumps(
                                record["command_line"],
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            record["process_group_id"],
                            _utc_now(),
                        ),
                    )
            return session_id
        except TaskStateError:
            raise
        except (OSError, psutil.Error, sqlite3.Error, TypeError, ValueError) as exc:
            raise TaskStateError(f"无法记录共享接收器：{exc}") from exc

    def finish_receiver_session(self, session_id: str) -> None:
        try:
            with self._lock:
                with closing(self._connect()) as connection, connection:
                    connection.execute(
                        "DELETE FROM receiver_sessions WHERE session_id = ?",
                        (session_id,),
                    )
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法结束共享接收器记录：{exc}") from exc

    def cleanup_receiver_sessions(self) -> list[str]:
        """Clean any shared storescp sessions left by a prior foreground owner."""

        try:
            with self._lock, closing(self._connect()) as connection:
                rows = list(
                    connection.execute(
                        """
                        SELECT session_id, pid, process_created_at, executable,
                               command_line_json, process_group_id
                        FROM receiver_sessions ORDER BY started_at
                        """
                    )
                )
        except sqlite3.Error as exc:
            raise TaskStateError(f"无法读取共享接收器恢复信息：{exc}") from exc

        messages: list[str] = []
        for row in rows:
            session_id = str(row[0])
            try:
                record = self._process_record_from_row(row[1:])
                remove, message = _cleanup_process_identity(record, "storescp")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TaskStateError("共享接收器恢复记录已损坏") from exc
            if remove:
                self.finish_receiver_session(session_id)
            if message:
                messages.append(message)
        return messages

    def unresolved_process_records(self) -> list[str]:
        """Return process identities that could not be safely cleared at startup."""

        try:
            with self._lock, closing(self._connect()) as connection:
                task_rows = list(
                    connection.execute(
                        """
                        SELECT task_id, kind, pid
                        FROM task_processes
                        ORDER BY task_id, kind
                        """
                    )
                )
                receiver_rows = list(
                    connection.execute(
                        """
                        SELECT session_id, pid
                        FROM receiver_sessions
                        ORDER BY started_at
                        """
                    )
                )
        except sqlite3.Error as exc:
            raise TaskStateError(f"无法检查后台进程恢复信息：{exc}") from exc
        unresolved = [
            f"{str(kind)} PID {int(pid)}（任务 {str(task_id)[:8]}）"
            for task_id, kind, pid in task_rows
        ]
        unresolved.extend(
            f"storescp PID {int(pid)}（会话 {str(session_id)[:8]}）"
            for session_id, pid in receiver_rows
        )
        return unresolved

    @staticmethod
    def _process_record_from_row(row: tuple[object, ...]) -> dict[str, object]:
        command_line = json.loads(str(row[3]))
        if not isinstance(command_line, list):
            raise ValueError("invalid command line")
        return {
            "pid": int(row[0]),
            "created_at": float(row[1]),
            "executable": str(row[2]),
            "command_line": [str(value) for value in command_line],
            "process_group_id": int(row[4]),
        }

    def next_pending(self, task_id: str) -> str | None:
        try:
            with self._lock, closing(self._connect()) as connection:
                if (
                    connection.execute(
                        "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
                    ).fetchone()
                    is None
                ):
                    raise TaskStateError("任务不存在")
                row = connection.execute(
                    """
                    SELECT accession FROM accessions
                    WHERE task_id = ? AND result_json IS NULL
                    ORDER BY position LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
                return str(row[0]) if row is not None else None
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法读取下一个检查号：{exc}") from exc

    def record_result(self, task_id: str, result: AccessionResult) -> AccessionResult:
        if result.status == AccessionStatus.DOWNLOADING:
            try:
                with self._lock:
                    with closing(self._connect()) as connection, connection:
                        row = connection.execute(
                            """
                            SELECT partial_json, result_json FROM accessions
                            WHERE task_id = ? AND accession = ?
                            """,
                            (task_id, result.accession),
                        ).fetchone()
                        if row is None:
                            raise TaskStateError(
                                f"任务中不存在检查号 {result.accession}"
                            )
                        if row[1]:
                            return result
                        prior = _result_from_json(str(row[0])) if row[0] else None
                        retained_files = prior.file_count if prior is not None else 0
                        retained_bytes = (
                            prior.received_bytes if prior is not None else 0
                        )
                        connection.execute(
                            """
                            UPDATE accessions
                            SET file_count = ?, received_bytes = ?,
                                speed_bytes_per_second = ?
                            WHERE task_id = ? AND accession = ?
                            """,
                            (
                                retained_files + result.file_count,
                                retained_bytes + result.received_bytes,
                                result.speed_bytes_per_second,
                                task_id,
                                result.accession,
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE tasks
                            SET current_accession = ?, speed_bytes_per_second = ?,
                                updated_at = ?
                            WHERE task_id = ?
                            """,
                            (
                                result.accession,
                                result.speed_bytes_per_second,
                                _utc_now(),
                                task_id,
                            ),
                        )
                return result
            except TaskStateError:
                raise
            except (
                OSError,
                sqlite3.Error,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                raise TaskStateError(f"无法保存任务实时进度：{exc}") from exc
        if result.status not in FINAL_STATUSES | {AccessionStatus.CANCELLED}:
            return result
        try:
            with self._lock:
                with closing(self._connect()) as connection, connection:
                    row = connection.execute(
                        """
                        SELECT partial_json FROM accessions
                        WHERE task_id = ? AND accession = ?
                        """,
                        (task_id, result.accession),
                    ).fetchone()
                    if row is None:
                        raise TaskStateError(f"任务中不存在检查号 {result.accession}")
                    prior = _result_from_json(str(row[0])) if row[0] else None
                    merged = _merge_partial_result(prior, result)
                    if merged.status == AccessionStatus.CANCELLED:
                        connection.execute(
                            """
                            UPDATE accessions
                            SET status = NULL, file_count = ?, received_bytes = ?,
                                speed_bytes_per_second = ?, partial_json = ?
                            WHERE task_id = ? AND accession = ?
                            """,
                            (
                                merged.file_count,
                                merged.received_bytes,
                                merged.speed_bytes_per_second,
                                _result_to_json(merged),
                                task_id,
                                merged.accession,
                            ),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE accessions
                            SET status = ?, file_count = ?, received_bytes = ?,
                                speed_bytes_per_second = ?, result_json = ?,
                                partial_json = NULL
                            WHERE task_id = ? AND accession = ?
                            """,
                            (
                                merged.status.value,
                                merged.file_count,
                                merged.received_bytes,
                                merged.speed_bytes_per_second,
                                _result_to_json(merged),
                                task_id,
                                merged.accession,
                            ),
                        )
                    error = (
                        merged.message
                        if merged.status
                        in {AccessionStatus.FAILED, AccessionStatus.PARTIAL}
                        else ""
                    )
                    connection.execute(
                        """
                        UPDATE tasks
                        SET current_accession = ?, speed_bytes_per_second = ?,
                            error_message = CASE WHEN ? <> '' THEN ? ELSE error_message END,
                            updated_at = ?
                        WHERE task_id = ?
                        """,
                        (
                            merged.accession,
                            merged.speed_bytes_per_second,
                            error,
                            error,
                            _utc_now(),
                            task_id,
                        ),
                    )
                    return merged
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
            raise TaskStateError(f"无法保存任务结果：{exc}") from exc

    def set_phase(self, task_id: str, phase: str) -> None:
        if phase not in TASK_PHASES:
            raise TaskStateError(f"不支持的任务阶段：{phase}")
        try:
            with self._lock:
                with closing(self._connect()) as connection, connection:
                    cursor = connection.execute(
                        "UPDATE tasks SET phase = ?, updated_at = ? WHERE task_id = ?",
                        (phase, _utc_now(), task_id),
                    )
                    if cursor.rowcount != 1:
                        raise TaskStateError("任务不存在")
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法更新任务阶段：{exc}") from exc

    def update_runtime(
        self,
        task_id: str,
        *,
        current_accession: str | None = None,
        speed_bytes_per_second: float | None = None,
        error_message: str | None = None,
    ) -> None:
        assignments = ["updated_at = ?"]
        values: list[object] = [_utc_now()]
        if current_accession is not None:
            assignments.append("current_accession = ?")
            values.append(current_accession)
        if speed_bytes_per_second is not None:
            assignments.append("speed_bytes_per_second = ?")
            values.append(max(0.0, float(speed_bytes_per_second)))
        if error_message is not None:
            assignments.append("error_message = ?")
            values.append(error_message)
        values.append(task_id)
        try:
            with self._lock:
                with closing(self._connect()) as connection, connection:
                    cursor = connection.execute(
                        f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id = ?",
                        values,
                    )
                    if cursor.rowcount != 1:
                        raise TaskStateError("任务不存在")
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法更新任务运行信息：{exc}") from exc

    def retry_failed(self, task_id: str) -> int:
        retry_count = 0
        try:
            with self._lock:
                with closing(self._connect()) as connection, connection:
                    connection.execute("BEGIN IMMEDIATE")
                    task = connection.execute(
                        "SELECT config_json FROM tasks WHERE task_id = ?", (task_id,)
                    ).fetchone()
                    if task is None:
                        raise TaskStateError("任务不存在")
                    raw_config = json.loads(str(task[0]))
                    if not isinstance(raw_config, dict):
                        raise ValueError("invalid config")
                    config = AppConfig.from_dict(raw_config)
                    conflicts = self._shared_config_conflicts(
                        connection, config, exclude_task_id=task_id
                    )
                    if conflicts:
                        raise TaskStateError(
                            "应用全局运行配置与未完成任务不一致：" + "、".join(conflicts)
                        )
                    mapping_conflict = self._receiver_mapping_conflict(
                        connection,
                        config,
                        exclude_task_id=task_id,
                    )
                    if mapping_conflict:
                        raise TaskStateError(
                            f"接收映射冲突：{mapping_conflict}"
                        )
                    candidates = [
                        str(row[0])
                        for row in connection.execute(
                            """
                            SELECT accession FROM accessions
                            WHERE task_id = ? AND (
                                result_json IS NULL OR status IN (?, ?)
                            )
                            ORDER BY position
                            """,
                            (
                                task_id,
                                AccessionStatus.FAILED.value,
                                AccessionStatus.PARTIAL.value,
                            ),
                        )
                    ]
                    duplicates = self._active_duplicates(
                        connection, candidates, exclude_task_id=task_id
                    )
                    if duplicates:
                        examples = "、".join(duplicates[:3])
                        raise TaskStateError(f"检查号已存在于未完成任务中：{examples}")
                    rows = list(
                        connection.execute(
                            """
                            SELECT accession, result_json FROM accessions
                            WHERE task_id = ? AND status IN (?, ?)
                            ORDER BY position
                            """,
                            (
                                task_id,
                                AccessionStatus.FAILED.value,
                                AccessionStatus.PARTIAL.value,
                            ),
                        )
                    )
                    for accession, result_json in rows:
                        result = _result_from_json(str(result_json))
                        retained = (
                            replace(
                                result,
                                status=AccessionStatus.CANCELLED,
                                message="重试前已保留收到的文件",
                            )
                            if result.archived_files
                            else None
                        )
                        connection.execute(
                            """
                            UPDATE accessions
                            SET status = NULL, result_json = NULL, partial_json = ?,
                                file_count = ?, received_bytes = ?,
                                speed_bytes_per_second = 0
                            WHERE task_id = ? AND accession = ?
                            """,
                            (
                                _result_to_json(retained) if retained else None,
                                retained.file_count if retained else 0,
                                retained.received_bytes if retained else 0,
                                task_id,
                                str(accession),
                            ),
                        )
                        retry_count += 1
                    connection.execute(
                        """
                        UPDATE tasks
                        SET phase = 'queued', current_accession = '',
                            speed_bytes_per_second = 0, error_message = '', updated_at = ?
                        WHERE task_id = ?
                        """,
                        (_utc_now(), task_id),
                    )
        except TaskStateError:
            raise
        except (
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise TaskStateError(f"无法准备任务重试：{exc}") from exc
        return retry_count

    def delete_task(self, task_id: str) -> None:
        try:
            with self._lock:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        row = connection.execute(
                            "SELECT phase FROM tasks WHERE task_id = ?",
                            (task_id,),
                        ).fetchone()
                        if row is None:
                            raise TaskStateError("任务不存在")
                        phase = str(row[0])
                        if phase not in DELETABLE_TASK_PHASES:
                            raise TaskStateError(
                                "当前任务仍在运行、排队、暂停或生成 PDI，不能删除"
                            )
                        if connection.execute(
                            "SELECT 1 FROM task_processes WHERE task_id = ? LIMIT 1",
                            (task_id,),
                        ).fetchone() is not None:
                            raise TaskStateError(
                                "任务仍有后台进程记录，请先结束任务并重新启动程序完成清理"
                            )
                        cursor = connection.execute(
                            "DELETE FROM tasks WHERE task_id = ?", (task_id,)
                        )
                        if cursor.rowcount != 1:
                            raise TaskStateError("任务不存在")
                    except Exception:
                        connection.rollback()
                        raise
                    connection.commit()
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法删除任务：{exc}") from exc


class RoundRobinScheduler:
    """Small deterministic queue; one task receives one accession per turn."""

    def __init__(self):
        self._queue: deque[str] = deque()

    def add(self, task_id: str) -> None:
        if task_id not in self._queue:
            self._queue.append(task_id)

    def remove(self, task_id: str) -> None:
        self._queue = deque(value for value in self._queue if value != task_id)

    def pop_next(self) -> str | None:
        return self._queue.popleft() if self._queue else None

    def order(self) -> tuple[str, ...]:
        return tuple(self._queue)

    def __len__(self) -> int:
        return len(self._queue)


ReceiverStart = Callable[[], object | None]
ReceiverStop = Callable[[object | None], None]
MoveStarted = Callable[[], None]
ReceiverRun = Callable[
    [
        object | None,
        str,
        AppConfig,
        str,
        MoveStarted | None,
        threading.Event,
    ],
    AccessionResult,
]
ReceiverReady = Callable[[object | None], bool]
ExecuteAccession = Callable[[str, AppConfig, str], AccessionResult]
CancelAccession = Callable[[str], None]
BeforeFirstExecution = Callable[[str], None]
TaskStarted = Callable[[TaskSummary], None]


class ReceiverService:
    """Own one shared receiver and keep its lifecycle independent from tasks."""

    def __init__(
        self,
        start: ReceiverStart,
        stop: ReceiverStop,
        run_accession: ReceiverRun | None = None,
        ready: ReceiverReady | None = None,
        max_concurrent_moves: int = 2,
    ):
        self._start = start
        self._stop = stop
        self._run_accession = run_accession
        self._ready = ready
        self._lock = threading.RLock()
        self.max_concurrent_moves = max(1, min(8, int(max_concurrent_moves)))
        self._run_slots = threading.BoundedSemaphore(self.max_concurrent_moves)
        self._cancel_lock = threading.RLock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._handle: object | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def ensure_started(self) -> object | None:
        with self._lock:
            if self._running:
                ready = (
                    self._ready(self._handle)
                    if self._ready is not None
                    else self._is_ready(self._handle)
                )
                if ready:
                    return self._handle
                stale_handle = self._handle
                self._handle = None
                self._running = False
                if stale_handle is not None:
                    self._stop(stale_handle)
            handle = self._start()
            ready = (
                self._ready(handle)
                if self._ready is not None
                else self._is_ready(handle)
            )
            if not ready:
                if handle is not None:
                    self._stop(handle)
                raise RuntimeError("共享 DICOM 接收器启动后未就绪")
            self._handle = handle
            self._running = True
            return handle

    @staticmethod
    def _is_ready(handle: object | None) -> bool:
        if handle is None or handle is False:
            return False
        poll = getattr(handle, "poll", None)
        if callable(poll):
            return poll() is None
        process = getattr(handle, "process", None)
        process_poll = getattr(process, "poll", None)
        return not callable(process_poll) or process_poll() is None

    def run_accession(
        self,
        task_id: str,
        config: AppConfig,
        accession: str,
        move_started: MoveStarted | None = None,
    ) -> AccessionResult:
        if self._run_accession is None:
            raise RuntimeError("共享接收器没有配置检查号执行器")
        with self._cancel_lock:
            cancel_event = self._cancel_events.setdefault(
                task_id,
                threading.Event(),
            )
        try:
            with self._run_slots:
                handle = self.ensure_started()
                return self._run_accession(
                    handle,
                    task_id,
                    config,
                    accession,
                    move_started,
                    cancel_event,
                )
        finally:
            with self._cancel_lock:
                if self._cancel_events.get(task_id) is cancel_event:
                    self._cancel_events.pop(task_id, None)

    def request_cancel(self, task_id: str) -> None:
        """Remember cancellation even while the shared receiver is starting."""

        with self._cancel_lock:
            event = self._cancel_events.setdefault(task_id, threading.Event())
            event.set()

    def clear_cancel(self, task_id: str) -> None:
        """Discard a cancellation token after its in-flight future is reaped."""

        with self._cancel_lock:
            self._cancel_events.pop(task_id, None)

    def shutdown(self) -> None:
        with self._lock:
            if not self._running:
                return
            handle = self._handle
            self._handle = None
            self._running = False
        self._stop(handle)


ExecutePdi = Callable[[str, TaskRecord], bool]
CancelPdi = Callable[[str], None]
PdiStarting = Callable[[str], None]


class PdiQueue:
    """Persist and serialize PDI jobs independently from the download slot."""

    def __init__(
        self,
        catalog: TaskCatalog,
        execute: ExecutePdi,
        *,
        cancel: CancelPdi | None = None,
        starting: PdiStarting | None = None,
    ):
        self.catalog = catalog
        self._execute = execute
        self._cancel = cancel
        self._starting = starting
        self._queue: deque[str] = deque()
        self._lock = threading.RLock()
        self._slot = threading.Lock()
        self._active_task_id = ""
        self.last_error_task_id = ""
        self._cancel_requested: set[str] = set()
        self._resume_attempts: set[str] = set()
        self._shutting_down = False
        summaries = sorted(catalog.list_tasks(), key=lambda item: item.created_at)
        for summary in summaries:
            if summary.phase == "pdi_running":
                self._queue.append(summary.task_id)
                self._resume_attempts.add(summary.task_id)
            elif summary.phase == "pdi_pending":
                self._queue.append(summary.task_id)

    def _sync_pending(self) -> None:
        summaries = sorted(self.catalog.list_tasks(), key=lambda item: item.created_at)
        with self._lock:
            if self._shutting_down:
                return
            for summary in summaries:
                if (
                    summary.phase == "pdi_pending"
                    and summary.task_id not in self._queue
                    and summary.task_id != self._active_task_id
                ):
                    self._queue.append(summary.task_id)

    def enqueue(self, task_id: str) -> TaskSummary:
        with self._lock:
            if self._shutting_down:
                raise TaskStateError("PDI 队列正在关闭")
            summary = self.catalog.get_summary(task_id)
            if summary.phase not in {"completed", "pdi_retryable", "pdi_pending"}:
                raise TaskStateError("当前任务不能进入 PDI 队列")
            if summary.phase != "pdi_pending":
                self._resume_attempts.discard(task_id)
            self.catalog.set_phase(task_id, "pdi_pending")
            if task_id not in self._queue and task_id != self._active_task_id:
                self._queue.append(task_id)
            return self.catalog.get_summary(task_id)

    def retry(self, task_id: str) -> TaskSummary:
        if self.catalog.get_summary(task_id).phase != "pdi_retryable":
            raise TaskStateError("当前任务没有可重试的 PDI")
        return self.enqueue(task_id)

    def run_next(self) -> TaskSummary | None:
        if not self._slot.acquire(blocking=False):
            return None
        task_id = ""
        self.last_error_task_id = ""
        try:
            with self._lock:
                if self._shutting_down:
                    return None
            self._sync_pending()
            with self._lock:
                while self._queue:
                    candidate = self._queue.popleft()
                    phase = self.catalog.get_summary(candidate).phase
                    if phase == "pdi_pending" or (
                        phase == "pdi_running" and candidate in self._resume_attempts
                    ):
                        task_id = candidate
                        break
                if not task_id:
                    return None
                self._active_task_id = task_id
                reuse_attempt = task_id in self._resume_attempts
                self._resume_attempts.discard(task_id)
                self.catalog.set_phase(task_id, "pdi_running")
                self.catalog.begin_pdi_attempt(task_id, reuse_existing=reuse_attempt)
                if self._starting is not None:
                    self._starting(task_id)
            try:
                completed = bool(self._execute(task_id, self.catalog.get_task(task_id)))
            except Exception as exc:
                self.last_error_task_id = task_id
                self.catalog.update_runtime(
                    task_id,
                    error_message=str(exc).strip() or exc.__class__.__name__,
                )
                self.catalog.set_phase(task_id, "pdi_retryable")
                raise
            with self._lock:
                cancelled = task_id in self._cancel_requested or self._shutting_down
            self.catalog.set_phase(
                task_id,
                "completed" if completed and not cancelled else "pdi_retryable",
            )
            return self.catalog.get_summary(task_id)
        finally:
            if task_id:
                with self._lock:
                    self._cancel_requested.discard(task_id)
                    if self._active_task_id == task_id:
                        self._active_task_id = ""
            self._slot.release()

    def cancel(self, task_id: str) -> TaskSummary:
        should_cancel = False
        with self._lock:
            summary = self.catalog.get_summary(task_id)
            queued_resume = (
                summary.phase == "pdi_running"
                and task_id != self._active_task_id
                and task_id in self._resume_attempts
            )
            if summary.phase == "pdi_pending" or queued_resume:
                self._queue = deque(value for value in self._queue if value != task_id)
                self._resume_attempts.discard(task_id)
                self.catalog.set_phase(task_id, "pdi_retryable")
            elif summary.phase == "pdi_running" and task_id == self._active_task_id:
                self._cancel_requested.add(task_id)
                should_cancel = True
            elif summary.phase != "pdi_retryable":
                raise TaskStateError("当前 PDI 任务不能取消")
        if should_cancel and self._cancel is not None:
            self._cancel(task_id)
        return self.catalog.get_summary(task_id)

    def shutdown(self) -> None:
        active = ""
        with self._lock:
            self._shutting_down = True
            self._queue.clear()
            self._resume_attempts.clear()
            active = self._active_task_id
            if active:
                self._cancel_requested.add(active)
        if active and self._cancel is not None:
            self._cancel(active)


class TaskManager:
    """Coordinate fair task scheduling across bounded concurrent C-MOVE slots."""

    def __init__(
        self,
        catalog: TaskCatalog,
        execute_accession: ExecuteAccession | None = None,
        *,
        receiver: ReceiverService | None = None,
        cancel_accession: CancelAccession | None = None,
        before_first_execution: BeforeFirstExecution | None = None,
        max_concurrent_moves: int = 2,
        task_started: TaskStarted | None = None,
    ):
        if execute_accession is None and receiver is None:
            raise ValueError("必须提供检查号执行器或共享接收器")
        self.catalog = catalog
        self.receiver = receiver
        self._execute_accession = execute_accession
        self._cancel_accession = cancel_accession
        self._before_first_execution = before_first_execution
        self._task_started = task_started or (lambda _summary: None)
        self.max_concurrent_moves = max(1, min(8, int(max_concurrent_moves)))
        self._scheduler = RoundRobinScheduler()
        self._lock = threading.RLock()
        self._download_slot = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_concurrent_moves,
            thread_name_prefix="dcmget-move",
        )
        self._inflight: dict[
            Future[AccessionResult], tuple[str, str, AppConfig]
        ] = {}
        self._active_task_ids: set[str] = set()
        self._shutdown_requeue_ids: set[str] = set()
        self._cancel_requested: set[str] = set()
        self._shutting_down = False
        self._executor_stopped = False
        self.last_error_task_id = ""
        self._restore_queue()

    def _restore_queue(self) -> None:
        summaries = sorted(self.catalog.list_tasks(), key=lambda item: item.created_at)
        for summary in summaries:
            phase = summary.phase
            if phase in {"running", "pause_pending"}:
                self.catalog.set_phase(summary.task_id, "queued")
                phase = "queued"
            elif phase == "cancelling":
                self.catalog.set_phase(summary.task_id, "cancelled")
                phase = "cancelled"
            if phase == "queued":
                self._scheduler.add(summary.task_id)

    def create_task(
        self,
        config: AppConfig,
        accessions: Iterable[str],
        *,
        trial_required: bool = False,
        name: str = "",
    ) -> TaskSummary:
        summary = self.catalog.create_task(
            config,
            accessions,
            trial_required=trial_required,
            name=name,
        )
        with self._lock:
            self._scheduler.add(summary.task_id)
        return self._with_queue_position(summary)

    def list_tasks(self) -> list[TaskSummary]:
        return [self._with_queue_position(item) for item in self.catalog.list_tasks()]

    @property
    def has_inflight(self) -> bool:
        with self._lock:
            return bool(self._inflight)

    def get_task(self, task_id: str) -> TaskRecord:
        record = self.catalog.get_task(task_id)
        record.summary = self._with_queue_position(record.summary)
        return record

    def get_task_detail(self, task_id: str, accession_limit: int = 201) -> TaskDetail:
        detail = self.catalog.get_task_detail(task_id, accession_limit=accession_limit)
        detail.summary = self._with_queue_position(detail.summary)
        return detail

    def list_failed_accessions(self, task_id: str) -> list[str]:
        return self.catalog.list_failed_accessions(task_id)

    def pause_task(self, task_id: str) -> TaskSummary:
        with self._lock:
            summary = self.catalog.get_summary(task_id)
            if summary.phase == "queued":
                self.catalog.set_phase(task_id, "paused")
                self._scheduler.remove(task_id)
            elif summary.phase == "running":
                self.catalog.set_phase(task_id, "pause_pending")
            elif summary.phase not in {"paused", "pause_pending"}:
                raise TaskStateError("当前任务不能暂停")
        return self._with_queue_position(self.catalog.get_summary(task_id))

    def resume_task(self, task_id: str) -> TaskSummary:
        with self._lock:
            summary = self.catalog.get_summary(task_id)
            if summary.phase not in {"paused", "pause_pending"}:
                raise TaskStateError("当前任务不能继续")
            if summary.phase == "pause_pending" and task_id in self._active_task_ids:
                self.catalog.set_phase(task_id, "running")
            else:
                self.catalog.set_phase(task_id, "queued")
                self._scheduler.add(task_id)
        return self._with_queue_position(self.catalog.get_summary(task_id))

    def cancel_task(self, task_id: str) -> TaskSummary:
        should_cancel_process = False
        with self._lock:
            summary = self.catalog.get_summary(task_id)
            if summary.phase in TERMINAL_PHASES | {"download_retryable"}:
                return self._with_queue_position(summary)
            if task_id in self._active_task_ids:
                self.catalog.set_phase(task_id, "cancelling")
                self._cancel_requested.add(task_id)
                should_cancel_process = True
            else:
                self.catalog.set_phase(task_id, "cancelled")
                self._scheduler.remove(task_id)
        if should_cancel_process:
            if self.receiver is not None:
                self.receiver.request_cancel(task_id)
            if self._cancel_accession is not None:
                self._cancel_accession(task_id)
        return self._with_queue_position(self.catalog.get_summary(task_id))

    def retry_task(self, task_id: str) -> TaskSummary:
        with self._lock:
            summary = self.catalog.get_summary(task_id)
            if summary.phase not in {
                "cancelled",
                "failed",
                "download_retryable",
            }:
                raise TaskStateError("当前任务没有可重试内容")
            self.catalog.retry_failed(task_id)
            self._scheduler.add(task_id)
        return self._with_queue_position(self.catalog.get_summary(task_id))

    def delete_task(self, task_id: str) -> None:
        """Delete one inactive task record without touching exported files."""

        with self._lock:
            summary = self.catalog.get_summary(task_id)
            if (
                task_id in self._active_task_ids
                or any(
                    active_task_id == task_id
                    for active_task_id, _accession, _config in self._inflight.values()
                )
            ):
                raise TaskStateError("任务仍有后台下载活动，不能删除")
            if summary.phase not in DELETABLE_TASK_PHASES:
                raise TaskStateError(
                    "当前任务仍在运行、排队、暂停或生成 PDI，不能删除"
                )
            self.catalog.delete_task(task_id)
            self._scheduler.remove(task_id)
            self._cancel_requested.discard(task_id)
            self._shutdown_requeue_ids.discard(task_id)

    def run_next_round(self) -> TaskSummary | None:
        if not self._download_slot.acquire(blocking=False):
            return None
        self.last_error_task_id = ""
        try:
            self._submit_available()
            with self._lock:
                futures = list(self._inflight)
            if not futures:
                self._shutdown_receiver_if_idle()
                return None
            done, _pending = wait(
                futures,
                timeout=0.1,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                return None
            with self._lock:
                future = next(item for item in self._inflight if item in done)
            summary, error = self._complete_future(future)
            self._shutdown_receiver_if_idle()
            if error is not None:
                with self._lock:
                    shutting_down = self._shutting_down
                if not shutting_down:
                    raise error
            return self._with_queue_position(summary)
        finally:
            self._download_slot.release()

    def _submit_available(self) -> None:
        while True:
            with self._lock:
                if (
                    self._shutting_down
                    or len(self._inflight) >= self.max_concurrent_moves
                ):
                    return
            selected = self._select_next()
            if selected is None:
                return
            task_id, accession, config = selected
            try:
                future = self._executor.submit(
                    self._execute_selected,
                    task_id,
                    accession,
                    config,
                )
            except Exception:
                with self._lock:
                    self._active_task_ids.discard(task_id)
                    self.catalog.set_phase(task_id, "queued")
                    self._scheduler.add(task_id)
                raise
            with self._lock:
                self._inflight[future] = (task_id, accession, config)
            try:
                self._task_started(
                    self._with_queue_position(self.catalog.get_summary(task_id))
                )
            except Exception:
                # UI notification failures must not orphan an already-running move.
                pass

    def _execute_selected(
        self,
        task_id: str,
        accession: str,
        config: AppConfig,
    ) -> AccessionResult:
        trial_required, trial_consumed = self.catalog.trial_state(task_id)
        move_started: MoveStarted | None = None
        if trial_required and not trial_consumed:
            if self._before_first_execution is None:
                raise RuntimeError("试用任务没有配置首次执行计次器")

            def consume_trial_after_move_start() -> None:
                required, consumed = self.catalog.trial_state(task_id)
                if not required or consumed:
                    return
                self._before_first_execution(task_id)
                self.catalog.mark_trial_consumed(task_id)

            move_started = consume_trial_after_move_start
        if self._execute_accession is not None:
            if self.receiver is not None:
                self.receiver.ensure_started()
            with self._lock:
                cancelled = task_id in self._cancel_requested
            if cancelled:
                return AccessionResult(accession, AccessionStatus.CANCELLED)
            if move_started is not None:
                move_started()
            return self._execute_accession(task_id, config, accession)
        if self.receiver is not None:
            return self.receiver.run_accession(
                task_id,
                config,
                accession,
                move_started,
            )
        raise RuntimeError("检查号执行器不可用")

    def _complete_future(
        self,
        future: Future[AccessionResult],
    ) -> tuple[TaskSummary, Exception | None]:
        with self._lock:
            task_id, accession, config = self._inflight[future]
            shutting_down = self._shutting_down
            shutdown_requeue = (
                shutting_down and task_id in self._shutdown_requeue_ids
            )
        error: Exception | None = None
        try:
            result = future.result()
            if result.accession != accession:
                raise TaskStateError("执行器返回了不属于当前任务的检查号")
            self.catalog.record_result(task_id, result)
            self._finish_round(task_id, result)
        except Exception as exc:
            phase = self.catalog.get_summary(task_id).phase
            cancelled = phase == "cancelling"
            if cancelled or shutdown_requeue:
                self.catalog.update_runtime(
                    task_id,
                    current_accession="",
                    speed_bytes_per_second=0,
                    error_message="",
                )
                self.catalog.set_phase(
                    task_id,
                    "queued" if shutdown_requeue else "cancelled",
                )
            else:
                error = exc
                self.last_error_task_id = task_id
                self.catalog.update_runtime(
                    task_id,
                    current_accession=accession,
                    speed_bytes_per_second=0,
                    error_message=str(exc).strip() or exc.__class__.__name__,
                )
                self.catalog.set_phase(task_id, "failed")
            with self._lock:
                self._scheduler.remove(task_id)
        finally:
            if self.receiver is not None:
                self.receiver.clear_cancel(task_id)
            with self._lock:
                self._inflight.pop(future, None)
                self._active_task_ids.discard(task_id)
                self._cancel_requested.discard(task_id)
        return self.catalog.get_summary(task_id), error

    def _select_next(self) -> tuple[str, str, AppConfig] | None:
        with self._lock:
            if self._shutting_down:
                return None
            attempts = len(self._scheduler)
            for _ in range(attempts):
                task_id = self._scheduler.pop_next()
                if task_id is None:
                    return None
                summary = self.catalog.get_summary(task_id)
                if summary.phase != "queued":
                    continue
                accession = self.catalog.next_pending(task_id)
                if accession is None:
                    self._finalize_exhausted(summary)
                    continue
                config = self.catalog.get_config(task_id)
                self.catalog.set_phase(task_id, "running")
                self.catalog.update_runtime(
                    task_id,
                    current_accession=accession,
                    speed_bytes_per_second=0,
                )
                self._active_task_ids.add(task_id)
                return task_id, accession, config
        return None

    def _finish_round(self, task_id: str, result: AccessionResult) -> None:
        with self._lock:
            phase = self.catalog.get_summary(task_id).phase
            if phase == "cancelling":
                self.catalog.set_phase(task_id, "cancelled")
                self._scheduler.remove(task_id)
            elif result.status == AccessionStatus.CANCELLED:
                self.catalog.set_phase(
                    task_id,
                    "queued" if self._shutting_down else "cancelled",
                )
                self._scheduler.remove(task_id)
            elif phase == "pause_pending":
                self.catalog.set_phase(task_id, "paused")
                self._scheduler.remove(task_id)
            elif self.catalog.next_pending(task_id) is not None:
                self.catalog.set_phase(task_id, "queued")
                if not self._shutting_down:
                    self._scheduler.add(task_id)
            else:
                self._finalize_exhausted(self.catalog.get_summary(task_id))
            self.catalog.update_runtime(task_id, current_accession="")

    def _finalize_exhausted(self, summary: TaskSummary) -> None:
        phase = "download_retryable" if summary.failed_count else "completed"
        if phase == "completed" and summary.file_count:
            config = self.catalog.get_config(summary.task_id)
            if config.pdi_export_enabled:
                phase = "pdi_pending"
        self.catalog.set_phase(summary.task_id, phase)
        self._scheduler.remove(summary.task_id)

    def _with_queue_position(self, summary: TaskSummary) -> TaskSummary:
        with self._lock:
            try:
                position = self._scheduler.order().index(summary.task_id) + 1
            except ValueError:
                position = None
        return replace(summary, queue_position=position)

    def _shutdown_receiver_if_idle(self) -> None:
        if self.receiver is None:
            return
        with self._lock:
            idle = not self._active_task_ids and not self._scheduler.order()
        if idle:
            self.receiver.shutdown()

    def shutdown(self, timeout_seconds: float | None = None) -> bool:
        deadline = (
            None
            if timeout_seconds is None
            else time.monotonic() + max(0.0, float(timeout_seconds))
        )
        with self._lock:
            self._shutting_down = True
            active = tuple(self._active_task_ids)
            self._shutdown_requeue_ids = {
                task_id
                for task_id in active
                if self.catalog.get_summary(task_id).phase != "cancelling"
            }
            self._cancel_requested.update(active)
        for task_id in active:
            if self.receiver is not None:
                self.receiver.request_cancel(task_id)
            if self._cancel_accession is not None:
                self._cancel_accession(task_id)
        if deadline is None:
            acquired = self._download_slot.acquire()
        else:
            acquired = self._download_slot.acquire(
                timeout=max(0.0, deadline - time.monotonic())
            )
        if not acquired:
            return False
        try:
            while True:
                with self._lock:
                    futures = list(self._inflight)
                if not futures:
                    break
                remaining = (
                    None
                    if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                if remaining == 0:
                    return False
                done, not_done = wait(futures, timeout=remaining)
                for future in done:
                    with self._lock:
                        pending = future in self._inflight
                    if pending:
                        self._complete_future(future)
                if not_done:
                    return False
            if self.receiver is not None:
                self.receiver.shutdown()
            if not self._executor_stopped:
                self._executor.shutdown(wait=True, cancel_futures=True)
                self._executor_stopped = True
            return True
        finally:
            self._download_slot.release()
