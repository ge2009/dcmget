from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout
import psutil

from .config import AppConfig
from .core import AccessionResult, AccessionStatus, BatchSummary
from .runtime import ensure_application_state_dir


TASK_STATE_VERSION = 1
TASK_PHASES = {
    "downloading",
    "download_retryable",
    "pdi_pending",
    "pdi_running",
    "pdi_retryable",
}
FINAL_STATUSES = {
    AccessionStatus.COMPLETED,
    AccessionStatus.NO_DATA,
    AccessionStatus.PARTIAL,
    AccessionStatus.FAILED,
}


class TaskStateError(RuntimeError):
    pass


@dataclass(slots=True)
class TaskCheckpoint:
    task_id: str
    config: AppConfig
    accessions: list[str]
    results: list[AccessionResult]
    partial_results: dict[str, AccessionResult]
    trial_required: bool
    created_at: str
    phase: str
    pdi_attempt_id: str = ""

    @property
    def pending_accessions(self) -> list[str]:
        completed = {result.accession for result in self.results}
        return [accession for accession in self.accessions if accession not in completed]

    @property
    def result_by_accession(self) -> dict[str, AccessionResult]:
        return {result.accession: result for result in self.results}


def default_task_state_path() -> Path:
    return ensure_application_state_dir() / "active-task.sqlite3"


class TaskCheckpointStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path else default_task_state_path()
        self._lease = FileLock(str(self.path) + ".lock")

    @property
    def lease_held(self) -> bool:
        return self._lease.is_locked

    def try_acquire_lease(self) -> bool:
        if self._lease.is_locked:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._lease.acquire(timeout=0)
        except Timeout:
            return False
        return True

    def release_lease(self) -> None:
        if self._lease.is_locked:
            self._lease.release()

    def start(
        self,
        config: AppConfig,
        accessions: list[str],
        *,
        trial_required: bool,
    ) -> TaskCheckpoint:
        values = list(accessions)
        if not values:
            raise TaskStateError("活动任务至少需要一个检查号")
        if len(values) != len(set(values)):
            raise TaskStateError("活动任务检查号不能重复")

        checkpoint = TaskCheckpoint(
            task_id=uuid.uuid4().hex,
            config=config,
            accessions=values,
            results=[],
            partial_results={},
            trial_required=trial_required,
            created_at=datetime.now(timezone.utc).isoformat(),
            phase="downloading",
        )
        self._write_checkpoint(checkpoint)
        return self.load_required()

    def import_checkpoint(self, checkpoint: TaskCheckpoint) -> TaskCheckpoint:
        """Atomically import a checkpoint while preserving its task identity.

        This is intentionally stricter than :meth:`start`: an existing recovery
        point for another task is never replaced.  Re-importing the same task is
        idempotent so an interrupted catalog migration can safely be retried.
        """

        self._validate_checkpoint(checkpoint)
        acquired_here = False
        if not self.lease_held:
            if not self.try_acquire_lease():
                raise TaskStateError("任务恢复点正在被另一个 DcmGet 实例使用")
            acquired_here = True
        try:
            if self.path.is_file():
                existing = self.load_required()
                if existing.task_id == checkpoint.task_id:
                    return existing
                raise TaskStateError("任务恢复点已包含另一个未完成任务")
            self._write_checkpoint(checkpoint)
            return self.load_required()
        finally:
            if acquired_here:
                self.release_lease()

    @staticmethod
    def _validate_checkpoint(checkpoint: TaskCheckpoint) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", checkpoint.task_id):
            raise TaskStateError("导入任务编号格式不正确")
        values = list(checkpoint.accessions)
        if not values or any(not str(value) for value in values):
            raise TaskStateError("导入任务至少需要一个检查号")
        if len(values) != len(set(values)):
            raise TaskStateError("导入任务检查号不能重复")
        if checkpoint.phase not in TASK_PHASES:
            raise TaskStateError(f"不支持的任务阶段：{checkpoint.phase}")
        if not checkpoint.created_at:
            raise TaskStateError("导入任务缺少创建时间")
        if checkpoint.pdi_attempt_id and not re.fullmatch(
            r"[0-9a-f]{32}", checkpoint.pdi_attempt_id
        ):
            raise TaskStateError("导入任务的 PDI 恢复编号格式不正确")

        accession_set = set(values)
        result_accessions: set[str] = set()
        for result in checkpoint.results:
            if result.accession not in accession_set:
                raise TaskStateError(
                    f"导入结果不属于当前任务：{result.accession}"
                )
            if result.accession in result_accessions:
                raise TaskStateError(
                    f"导入任务包含重复结果：{result.accession}"
                )
            if result.status not in FINAL_STATUSES:
                raise TaskStateError(
                    f"导入结果状态不受支持：{result.status.value}"
                )
            result_accessions.add(result.accession)
        for accession, partial in checkpoint.partial_results.items():
            if accession not in accession_set or partial.accession != accession:
                raise TaskStateError(f"导入的部分结果不属于当前任务：{accession}")
            if partial.status != AccessionStatus.CANCELLED:
                raise TaskStateError(
                    f"导入的部分结果状态不受支持：{partial.status.value}"
                )

    def _write_checkpoint(self, checkpoint: TaskCheckpoint) -> None:
        self._validate_checkpoint(checkpoint)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with closing(sqlite3.connect(temporary)) as connection, connection:
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(
                    """
                    CREATE TABLE metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE accessions (
                        position INTEGER PRIMARY KEY,
                        accession TEXT NOT NULL UNIQUE,
                        result_json TEXT,
                        partial_json TEXT
                    );
                    """
                )
                connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)",
                    [
                        ("version", str(TASK_STATE_VERSION)),
                        ("task_id", checkpoint.task_id),
                        ("created_at", checkpoint.created_at),
                        (
                            "trial_required",
                            "1" if checkpoint.trial_required else "0",
                        ),
                        ("phase", checkpoint.phase),
                        (
                            "config",
                            json.dumps(
                                checkpoint.config.to_dict(),
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                        *(
                            [("pdi_attempt_id", checkpoint.pdi_attempt_id)]
                            if checkpoint.pdi_attempt_id
                            else []
                        ),
                    ],
                )
                results = checkpoint.result_by_accession
                connection.executemany(
                    """
                    INSERT INTO accessions(
                        position, accession, result_json, partial_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        (
                            position,
                            accession,
                            (
                                _result_to_json(results[accession])
                                if accession in results
                                else None
                            ),
                            (
                                _result_to_json(
                                    checkpoint.partial_results[accession]
                                )
                                if accession in checkpoint.partial_results
                                else None
                            ),
                        )
                        for position, accession in enumerate(checkpoint.accessions)
                    ),
                )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法保存任务恢复点：{exc}") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def load(self, *, include_archived_files: bool = True) -> TaskCheckpoint | None:
        if not self.path.is_file():
            return None
        try:
            with closing(sqlite3.connect(self.path)) as connection:
                metadata = dict(connection.execute("SELECT key, value FROM metadata"))
                rows = connection.execute(
                    """
                    SELECT accession, result_json, partial_json
                    FROM accessions
                    ORDER BY position
                    """
                )
                if int(metadata["version"]) != TASK_STATE_VERSION:
                    raise ValueError("unsupported version")
                raw_config = json.loads(metadata["config"])
                if not isinstance(raw_config, dict):
                    raise ValueError("invalid config")
                accessions: list[str] = []
                results: list[AccessionResult] = []
                partial_results: dict[str, AccessionResult] = {}
                for accession, result_json, partial_json in rows:
                    value = str(accession)
                    accessions.append(value)
                    if result_json:
                        result = _result_from_json(
                            str(result_json),
                            include_archived_files=include_archived_files,
                        )
                        if (
                            result.accession != value
                            or result.status not in FINAL_STATUSES
                        ):
                            raise ValueError("invalid final result")
                        results.append(result)
                    if partial_json:
                        partial = _result_from_json(
                            str(partial_json),
                            include_archived_files=include_archived_files,
                        )
                        if (
                            partial.accession != value
                            or partial.status != AccessionStatus.CANCELLED
                        ):
                            raise ValueError("invalid partial result")
                        partial_results[value] = partial
        except sqlite3.Error as exc:
            raise TaskStateError(f"任务恢复点已损坏：{exc}") from exc
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskStateError(
                "任务恢复点内容不完整或版本不受支持"
            ) from exc

        try:
            task_id = metadata["task_id"]
            if not re.fullmatch(r"[0-9a-f]{32}", task_id):
                raise ValueError("invalid task id")
            phase = metadata.get("phase", "downloading")
            if phase not in TASK_PHASES:
                raise ValueError("invalid task phase")
            pdi_attempt_id = metadata.get("pdi_attempt_id", "")
            if pdi_attempt_id and not re.fullmatch(
                r"[0-9a-f]{32}", pdi_attempt_id
            ):
                raise ValueError("invalid PDI attempt id")
            return TaskCheckpoint(
                task_id=task_id,
                config=AppConfig.from_dict(raw_config),
                accessions=accessions,
                results=results,
                partial_results=partial_results,
                trial_required=metadata.get("trial_required") == "1",
                created_at=metadata["created_at"],
                phase=phase,
                pdi_attempt_id=pdi_attempt_id,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskStateError("任务恢复点内容不完整或版本不受支持") from exc

    def load_required(
        self, *, include_archived_files: bool = True
    ) -> TaskCheckpoint:
        checkpoint = self.load(include_archived_files=include_archived_files)
        if checkpoint is None:
            raise TaskStateError("任务恢复点不存在")
        return checkpoint

    def record_result(
        self, task_id: str, result: AccessionResult
    ) -> AccessionResult:
        if result.status not in FINAL_STATUSES | {AccessionStatus.CANCELLED}:
            return result
        try:
            with closing(sqlite3.connect(self.path)) as connection, connection:
                stored_task_id = _metadata_value(connection, "task_id")
                if stored_task_id != task_id:
                    raise TaskStateError("活动任务已改变，拒绝写入旧任务结果")
                row = connection.execute(
                    "SELECT partial_json FROM accessions WHERE accession = ?",
                    (result.accession,),
                ).fetchone()
                if row is None:
                    raise TaskStateError(f"恢复点中不存在检查号 {result.accession}")
                prior_partial = _result_from_json(row[0]) if row[0] else None
                merged = _merge_partial_result(prior_partial, result)
                if merged.status == AccessionStatus.CANCELLED:
                    connection.execute(
                        "UPDATE accessions SET partial_json = ? WHERE accession = ?",
                        (_result_to_json(merged), merged.accession),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE accessions
                        SET result_json = ?, partial_json = NULL
                        WHERE accession = ?
                        """,
                        (_result_to_json(merged), merged.accession),
                    )
                return merged
        except TaskStateError:
            raise
        except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
            raise TaskStateError(f"无法更新任务恢复点：{exc}") from exc

    def clear(self, task_id: str | None = None) -> None:
        if not self.path.exists():
            return
        if task_id is not None:
            try:
                with closing(sqlite3.connect(self.path)) as connection:
                    if _metadata_value(connection, "task_id") != task_id:
                        return
            except TaskStateError:
                raise
            except sqlite3.Error as exc:
                raise TaskStateError(f"无法校验任务恢复点：{exc}") from exc
        try:
            self.path.unlink(missing_ok=True)
            self.path.with_name(self.path.name + "-journal").unlink(missing_ok=True)
        except OSError as exc:
            raise TaskStateError(f"无法清除已完成任务恢复点：{exc}") from exc

    def set_phase(self, task_id: str, phase: str) -> None:
        if phase not in TASK_PHASES:
            raise TaskStateError(f"不支持的任务阶段：{phase}")
        try:
            with closing(sqlite3.connect(self.path)) as connection, connection:
                if _metadata_value(connection, "task_id") != task_id:
                    raise TaskStateError("活动任务已改变，拒绝更新旧任务阶段")
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES ('phase', ?)",
                    (phase,),
                )
        except TaskStateError:
            raise
        except sqlite3.Error as exc:
            raise TaskStateError(f"无法更新任务阶段：{exc}") from exc

    def prepare_download_retry(
        self, task_id: str, *, include_archived_files: bool = True
    ) -> TaskCheckpoint:
        """Make failed/partial rows pending while retaining received files."""

        try:
            with closing(sqlite3.connect(self.path)) as connection, connection:
                if _metadata_value(connection, "task_id") != task_id:
                    raise TaskStateError("活动任务已改变，拒绝重试旧任务")
                if _metadata_value(connection, "phase") != "download_retryable":
                    raise TaskStateError("当前任务没有可重试的下载失败项")
                cursor = connection.execute(
                    "SELECT accession, result_json FROM accessions ORDER BY position"
                )
                retry_count = 0
                while batch := cursor.fetchmany(500):
                    for accession, result_json in batch:
                        if not result_json:
                            continue
                        result = _result_from_json(str(result_json))
                        if result.status not in {
                            AccessionStatus.FAILED,
                            AccessionStatus.PARTIAL,
                        }:
                            continue
                        has_retained_result = bool(
                            result.archived_files
                            or result.file_count
                            or result.new_file_count
                            or result.existing_skipped_count
                            or result.conflict_preserved_count
                        )
                        retained = (
                            _result_to_json(
                                replace(
                                    result,
                                    status=AccessionStatus.CANCELLED,
                                    message="重试前已保留收到的文件",
                                )
                            )
                            if has_retained_result
                            else None
                        )
                        connection.execute(
                            """
                            UPDATE accessions
                            SET result_json = NULL, partial_json = ?
                            WHERE accession = ?
                            """,
                            (retained, str(accession)),
                        )
                        retry_count += 1
                if retry_count == 0:
                    raise TaskStateError("当前任务没有可重试的下载失败项")
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES ('phase', 'downloading')"
                )
                connection.execute(
                    "DELETE FROM metadata WHERE key = 'pdi_attempt_id'"
                )
        except TaskStateError:
            raise
        except (sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
            raise TaskStateError(f"无法准备失败项重试：{exc}") from exc
        return self.load_required(include_archived_files=include_archived_files)

    def load_archived_files(self, task_id: str) -> list[str]:
        try:
            with closing(sqlite3.connect(self.path)) as connection:
                if _metadata_value(connection, "task_id") != task_id:
                    raise TaskStateError("活动任务已改变，拒绝读取旧任务文件")
                rows = connection.execute(
                    """
                    SELECT result_json, partial_json
                    FROM accessions
                    ORDER BY position
                    """
                )
                files: list[str] = []
                seen: set[str] = set()
                for result_json, partial_json in rows:
                    for payload in (result_json, partial_json):
                        if not payload:
                            continue
                        result = _result_from_json(str(payload))
                        for path in result.archived_files:
                            if path not in seen:
                                seen.add(path)
                                files.append(path)
                return files
        except TaskStateError:
            raise
        except (sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
            raise TaskStateError(f"无法读取任务归档文件：{exc}") from exc

    def begin_pdi_attempt(
        self,
        task_id: str,
        *,
        reuse_existing: bool,
    ) -> tuple[str, bool]:
        try:
            with closing(sqlite3.connect(self.path)) as connection, connection:
                if _metadata_value(connection, "task_id") != task_id:
                    raise TaskStateError("活动任务已改变，拒绝启动旧任务的 PDI")
                phase = _metadata_value(connection, "phase")
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'pdi_attempt_id'"
                ).fetchone()
                existing = str(row[0]) if row is not None else ""
                can_reuse = bool(
                    reuse_existing
                    and phase == "pdi_running"
                    and re.fullmatch(r"[0-9a-f]{32}", existing)
                )
                attempt_id = existing if can_reuse else uuid.uuid4().hex
                connection.executemany(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                    (
                        ("phase", "pdi_running"),
                        ("pdi_attempt_id", attempt_id),
                    ),
                )
                return attempt_id, can_reuse
        except TaskStateError:
            raise
        except sqlite3.Error as exc:
            raise TaskStateError(f"无法建立 PDI 恢复点：{exc}") from exc

    def update_config(self, task_id: str, config: AppConfig) -> None:
        payload = json.dumps(
            config.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            with closing(sqlite3.connect(self.path)) as connection, connection:
                if _metadata_value(connection, "task_id") != task_id:
                    raise TaskStateError("活动任务已改变，拒绝更新旧任务配置")
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES ('config', ?)",
                    (payload,),
                )
        except TaskStateError:
            raise
        except sqlite3.Error as exc:
            raise TaskStateError(f"无法更新任务恢复配置：{exc}") from exc

    def record_process(
        self,
        task_id: str,
        kind: str,
        pid: int,
        executable: str,
        *,
        active: bool,
    ) -> None:
        if kind not in {"storescp", "movescu", "pdi"}:
            raise TaskStateError(f"不支持的后台进程类型：{kind}")
        key = f"process:{kind}"
        try:
            with closing(sqlite3.connect(self.path)) as connection, connection:
                if _metadata_value(connection, "task_id") != task_id:
                    raise TaskStateError("活动任务已改变，拒绝更新旧任务进程")
                if not active:
                    connection.execute("DELETE FROM metadata WHERE key = ?", (key,))
                    return
                try:
                    process = psutil.Process(pid)
                    created_at = process.create_time()
                    command_line = process.cmdline()
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    return
                process_group_id = 0
                if os.name != "nt":
                    try:
                        candidate_group = os.getpgid(pid)
                    except OSError:
                        candidate_group = 0
                    if candidate_group == pid:
                        process_group_id = candidate_group
                payload = json.dumps(
                    {
                        "command_line": command_line,
                        "created_at": created_at,
                        "executable": str(Path(executable).expanduser().resolve()),
                        "pid": int(pid),
                        "process_group_id": process_group_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                    (key, payload),
                )
        except TaskStateError:
            raise
        except (OSError, psutil.Error, sqlite3.Error) as exc:
            raise TaskStateError(f"无法记录 {kind} 后台进程：{exc}") from exc

    def cleanup_recorded_processes(self, task_id: str) -> list[str]:
        try:
            with closing(sqlite3.connect(self.path)) as connection:
                if _metadata_value(connection, "task_id") != task_id:
                    raise TaskStateError("活动任务已改变，拒绝清理旧任务进程")
                records = dict(
                    connection.execute(
                        "SELECT key, value FROM metadata WHERE key LIKE 'process:%'"
                    )
                )
        except TaskStateError:
            raise
        except sqlite3.Error as exc:
            raise TaskStateError(f"无法读取后台进程恢复信息：{exc}") from exc

        messages: list[str] = []
        for kind in ("pdi", "movescu", "storescp"):
            raw = records.get(f"process:{kind}")
            if not raw:
                continue
            try:
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise TypeError("invalid process record")
                remove, message = _cleanup_process_identity(record, kind)
                if remove:
                    self.record_process(task_id, kind, 0, "", active=False)
                if message:
                    messages.append(message)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TaskStateError(f"{kind} 进程恢复记录已损坏") from exc
        return messages


def merge_checkpoint_summary(
    checkpoint: TaskCheckpoint, current: BatchSummary
) -> BatchSummary:
    final = checkpoint.result_by_accession
    current_by_accession = {result.accession: result for result in current.results}
    merged: list[AccessionResult] = []
    for accession in checkpoint.accessions:
        result = (
            final.get(accession)
            or checkpoint.partial_results.get(accession)
            or current_by_accession.get(accession)
        )
        if result is None:
            result = AccessionResult(
                accession,
                AccessionStatus.CANCELLED,
                message="任务尚未继续",
            )
        merged.append(result)
    return BatchSummary(
        results=merged,
        cancelled=current.cancelled,
        staging_directory=current.staging_directory,
    )


def _metadata_value(connection: sqlite3.Connection, key: str) -> str:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        raise TaskStateError("任务恢复点缺少任务标识")
    return str(row[0])


def _normalized_executable(value: object) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(value)))


def _process_executable(process: psutil.Process) -> str:
    try:
        return _normalized_executable(process.exe())
    except (psutil.AccessDenied, psutil.ZombieProcess):
        command = process.cmdline()
        if not command:
            raise
        return _normalized_executable(command[0])


def _cleanup_process_identity(
    record: dict[str, object], label: str
) -> tuple[bool, str]:
    """Terminate a recorded process only after PID identity verification.

    The returned boolean indicates whether the recovery record is resolved and
    may be discarded.  A stale/reused PID is resolved without terminating the
    unrelated process, while an identity that cannot be safely inspected or
    stopped remains unresolved.
    """

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


def _cleanup_recorded_process_group(
    record: dict[str, object],
    expected_executable: str,
    expected_created_at: float,
) -> str:
    """Clean an orphaned POSIX session only when every member is identifiable."""

    if os.name == "nt":
        return "empty"
    try:
        leader_pid = int(record["pid"])
        process_group_id = int(record.get("process_group_id", 0))
        expected_command = [str(value) for value in record.get("command_line", [])]
    except (TypeError, ValueError):
        return "unsafe"
    if process_group_id != leader_pid or process_group_id <= 0 or not expected_command:
        return "empty"

    members: list[psutil.Process] = []
    for process in psutil.process_iter():
        try:
            member_group_id = os.getpgid(process.pid)
        except ProcessLookupError:
            continue
        except OSError:
            return "unsafe"
        if member_group_id != process_group_id:
            continue
        try:
            if (
                _process_executable(process) != expected_executable
                or process.cmdline() != expected_command
                or process.create_time() + 1.0 < expected_created_at
            ):
                return "unsafe"
            members.append(process)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (OSError, psutil.AccessDenied):
            return "unsafe"
    if not members:
        return "empty"

    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return "empty"
    except OSError:
        return "unsafe"
    _gone, alive = psutil.wait_procs(members, timeout=3)
    if alive:
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            alive = []
        except OSError:
            return "unsafe"
        if alive:
            _gone, alive = psutil.wait_procs(alive, timeout=3)
    return "cleaned" if not alive else "unsafe"


def _merge_partial_result(
    prior: AccessionResult | None, current: AccessionResult
) -> AccessionResult:
    if prior is None or not (
        prior.archived_files
        or prior.file_count
        or prior.new_file_count
        or prior.existing_skipped_count
        or prior.conflict_preserved_count
    ):
        return current
    archived_files = list(dict.fromkeys([*prior.archived_files, *current.archived_files]))
    status = current.status
    if status in {AccessionStatus.NO_DATA, AccessionStatus.FAILED}:
        status = AccessionStatus.PARTIAL
    duration = prior.duration_seconds + current.duration_seconds
    received_bytes = prior.received_bytes + current.received_bytes
    new_file_count = min(
        len(archived_files),
        prior.new_file_count + current.new_file_count,
    )
    existing_skipped_count = max(0, len(archived_files) - new_file_count)
    conflict_preserved_count = (
        prior.conflict_preserved_count + current.conflict_preserved_count
    )
    if conflict_preserved_count:
        status = AccessionStatus.PARTIAL
    message = current.message
    retained = len(prior.archived_files)
    if retained:
        message = f"{message}；含上次中断保留的 {retained} 个文件".strip("；")
    if conflict_preserved_count:
        message = (
            f"{message}；仍有 {conflict_preserved_count} 个冲突文件需人工核对"
        ).strip("；")
    return replace(
        current,
        status=status,
        file_count=max(
            len(archived_files) + conflict_preserved_count,
            prior.file_count,
            current.file_count,
        ),
        duration_seconds=duration,
        message=message,
        output_directory=current.output_directory or prior.output_directory,
        received_bytes=received_bytes,
        speed_bytes_per_second=(received_bytes / duration if duration > 0 else 0.0),
        archived_files=archived_files,
        new_file_count=new_file_count,
        existing_skipped_count=existing_skipped_count,
        conflict_preserved_count=conflict_preserved_count,
    )


def _result_to_json(result: AccessionResult) -> str:
    return json.dumps(
        {
            "accession": result.accession,
            "archived_files": list(result.archived_files),
            "duration_seconds": result.duration_seconds,
            "file_count": result.file_count,
            "new_file_count": result.new_file_count,
            "existing_skipped_count": result.existing_skipped_count,
            "conflict_preserved_count": result.conflict_preserved_count,
            "message": result.message,
            "output_directory": result.output_directory,
            "received_bytes": result.received_bytes,
            "speed_bytes_per_second": result.speed_bytes_per_second,
            "status": result.status.value,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _result_from_json(
    value: str, *, include_archived_files: bool = True
) -> AccessionResult:
    raw = json.loads(value)
    if not isinstance(raw, dict):
        raise ValueError("invalid result")
    archived_values = raw.get("archived_files", [])
    if not isinstance(archived_values, list):
        raise ValueError("invalid archived files")
    file_count = max(int(raw.get("file_count", 0)), len(archived_values))
    archive_stats_known = any(
        key in raw
        for key in (
            "new_file_count",
            "existing_skipped_count",
            "conflict_preserved_count",
        )
    )
    return AccessionResult(
        accession=str(raw["accession"]),
        status=AccessionStatus(str(raw["status"])),
        file_count=file_count,
        duration_seconds=float(raw.get("duration_seconds", 0.0)),
        message=str(raw.get("message", "")),
        output_directory=str(raw.get("output_directory", "")),
        received_bytes=int(raw.get("received_bytes", 0)),
        speed_bytes_per_second=float(raw.get("speed_bytes_per_second", 0.0)),
        archived_files=(
            [str(path) for path in archived_values]
            if include_archived_files
            else []
        ),
        new_file_count=(
            max(0, int(raw.get("new_file_count", 0)))
            if archive_stats_known
            else file_count
        ),
        existing_skipped_count=max(
            0, int(raw.get("existing_skipped_count", 0))
        ),
        conflict_preserved_count=max(
            0, int(raw.get("conflict_preserved_count", 0))
        ),
    )
