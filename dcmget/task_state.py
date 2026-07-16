from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout
import psutil

from .config import AppConfig
from .core import AccessionResult, AccessionStatus, BatchSummary
from .runtime import ensure_application_state_dir


TASK_STATE_VERSION = 1
TASK_PHASES = {"downloading", "pdi_pending", "pdi_running", "pdi_retryable"}
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

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        task_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with sqlite3.connect(temporary) as connection:
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
                    (
                        ("version", str(TASK_STATE_VERSION)),
                        ("task_id", task_id),
                        ("created_at", created_at),
                        ("trial_required", "1" if trial_required else "0"),
                        ("phase", "downloading"),
                        (
                            "config",
                            json.dumps(
                                config.to_dict(),
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    ),
                )
                connection.executemany(
                    "INSERT INTO accessions(position, accession) VALUES (?, ?)",
                    enumerate(values),
                )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        except (OSError, sqlite3.Error) as exc:
            raise TaskStateError(f"无法保存任务恢复点：{exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return self.load_required()

    def load(self) -> TaskCheckpoint | None:
        if not self.path.is_file():
            return None
        try:
            with sqlite3.connect(self.path) as connection:
                metadata = dict(connection.execute("SELECT key, value FROM metadata"))
                rows = list(
                    connection.execute(
                        """
                        SELECT accession, result_json, partial_json
                        FROM accessions
                        ORDER BY position
                        """
                    )
                )
        except sqlite3.Error as exc:
            raise TaskStateError(f"任务恢复点已损坏：{exc}") from exc

        try:
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
                    result = _result_from_json(str(result_json))
                    if result.accession != value or result.status not in FINAL_STATUSES:
                        raise ValueError("invalid final result")
                    results.append(result)
                if partial_json:
                    partial = _result_from_json(str(partial_json))
                    if partial.accession != value or partial.status != AccessionStatus.CANCELLED:
                        raise ValueError("invalid partial result")
                    partial_results[value] = partial
            task_id = metadata["task_id"]
            if not re.fullmatch(r"[0-9a-f]{32}", task_id):
                raise ValueError("invalid task id")
            phase = metadata.get("phase", "downloading")
            if phase not in TASK_PHASES:
                raise ValueError("invalid task phase")
            return TaskCheckpoint(
                task_id=task_id,
                config=AppConfig.from_dict(raw_config),
                accessions=accessions,
                results=results,
                partial_results=partial_results,
                trial_required=metadata.get("trial_required") == "1",
                created_at=metadata["created_at"],
                phase=phase,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskStateError("任务恢复点内容不完整或版本不受支持") from exc

    def load_required(self) -> TaskCheckpoint:
        checkpoint = self.load()
        if checkpoint is None:
            raise TaskStateError("任务恢复点不存在")
        return checkpoint

    def record_result(
        self, task_id: str, result: AccessionResult
    ) -> AccessionResult:
        if result.status not in FINAL_STATUSES | {AccessionStatus.CANCELLED}:
            return result
        try:
            with sqlite3.connect(self.path) as connection:
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
                with sqlite3.connect(self.path) as connection:
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
            with sqlite3.connect(self.path) as connection:
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

    def begin_pdi_attempt(
        self,
        task_id: str,
        *,
        reuse_existing: bool,
    ) -> tuple[str, bool]:
        try:
            with sqlite3.connect(self.path) as connection:
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
            with sqlite3.connect(self.path) as connection:
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
            with sqlite3.connect(self.path) as connection:
                if _metadata_value(connection, "task_id") != task_id:
                    raise TaskStateError("活动任务已改变，拒绝更新旧任务进程")
                if not active:
                    connection.execute("DELETE FROM metadata WHERE key = ?", (key,))
                    return
                try:
                    created_at = psutil.Process(pid).create_time()
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    return
                payload = json.dumps(
                    {
                        "created_at": created_at,
                        "executable": str(Path(executable).expanduser().resolve()),
                        "pid": int(pid),
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
            with sqlite3.connect(self.path) as connection:
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
                pid = int(record["pid"])
                expected_created_at = float(record["created_at"])
                expected_executable = _normalized_executable(record["executable"])
                process = psutil.Process(pid)
                if abs(process.create_time() - expected_created_at) > 0.01:
                    messages.append(f"未清理 PID {pid}：进程标识已经变化")
                    self.record_process(task_id, kind, 0, "", active=False)
                    continue
                actual_executable = _process_executable(process)
                if actual_executable != expected_executable:
                    messages.append(f"未清理 PID {pid}：可执行文件与恢复记录不一致")
                    self.record_process(task_id, kind, 0, "", active=False)
                    continue
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
                    messages.append(f"未能清理上次的 {kind} 进程 PID {pid}")
                    continue
                self.record_process(
                    task_id,
                    kind,
                    pid,
                    expected_executable,
                    active=False,
                )
                messages.append(f"已清理上次异常退出遗留的 {kind} 进程 PID {pid}")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TaskStateError(f"{kind} 进程恢复记录已损坏") from exc
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                self.record_process(
                    task_id,
                    kind,
                    0,
                    "",
                    active=False,
                )
            except (OSError, psutil.Error) as exc:
                messages.append(f"未能清理上次的 {kind} 进程：{exc}")
        return messages


def merge_checkpoint_summary(
    checkpoint: TaskCheckpoint, current: BatchSummary
) -> BatchSummary:
    final = checkpoint.result_by_accession
    current_by_accession = {result.accession: result for result in current.results}
    merged: list[AccessionResult] = []
    for accession in checkpoint.accessions:
        result = final.get(accession) or current_by_accession.get(accession)
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


def _merge_partial_result(
    prior: AccessionResult | None, current: AccessionResult
) -> AccessionResult:
    if prior is None or not prior.archived_files:
        return current
    archived_files = list(dict.fromkeys([*prior.archived_files, *current.archived_files]))
    status = current.status
    if status in {AccessionStatus.NO_DATA, AccessionStatus.FAILED}:
        status = AccessionStatus.PARTIAL
    duration = prior.duration_seconds + current.duration_seconds
    received_bytes = prior.received_bytes + current.received_bytes
    message = current.message
    retained = len(prior.archived_files)
    if retained:
        message = f"{message}；含上次中断保留的 {retained} 个文件".strip("；")
    return replace(
        current,
        status=status,
        file_count=len(archived_files),
        duration_seconds=duration,
        message=message,
        output_directory=current.output_directory or prior.output_directory,
        received_bytes=received_bytes,
        speed_bytes_per_second=(received_bytes / duration if duration > 0 else 0.0),
        archived_files=archived_files,
    )


def _result_to_json(result: AccessionResult) -> str:
    return json.dumps(
        {
            "accession": result.accession,
            "archived_files": list(result.archived_files),
            "duration_seconds": result.duration_seconds,
            "file_count": result.file_count,
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


def _result_from_json(value: str) -> AccessionResult:
    raw = json.loads(value)
    if not isinstance(raw, dict):
        raise ValueError("invalid result")
    return AccessionResult(
        accession=str(raw["accession"]),
        status=AccessionStatus(str(raw["status"])),
        file_count=int(raw.get("file_count", 0)),
        duration_seconds=float(raw.get("duration_seconds", 0.0)),
        message=str(raw.get("message", "")),
        output_directory=str(raw.get("output_directory", "")),
        received_bytes=int(raw.get("received_bytes", 0)),
        speed_bytes_per_second=float(raw.get("speed_bytes_per_second", 0.0)),
        archived_files=[str(path) for path in raw.get("archived_files", [])],
    )
