from __future__ import annotations

import csv
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import tempfile
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Protocol, TextIO

from filelock import FileLock
from pydicom import dcmread
from pydicom.tag import Tag


LEDGER_SCHEMA_VERSION = 1
_REPORT_FETCH_SIZE = 512
_ACCESSION_NUMBER = Tag(0x0008, 0x0050)
_SOP_INSTANCE_UID = Tag(0x0008, 0x0018)
_STUDY_INSTANCE_UID = Tag(0x0020, 0x000D)
_SERIES_INSTANCE_UID = Tag(0x0020, 0x000E)
_DICOM_METADATA_TAGS = (
    _ACCESSION_NUMBER,
    _SOP_INSTANCE_UID,
    _STUDY_INSTANCE_UID,
    _SERIES_INSTANCE_UID,
)


class TaskLedgerError(RuntimeError):
    pass


class AttributionStatus(str, Enum):
    MATCHED = "matched"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class ObservedDicom:
    """Metadata observed before optional anonymization and final publication."""

    file_path: str
    actual_accession_number: str = ""
    study_instance_uid: str = ""
    series_instance_uid: str = ""
    sop_instance_uid: str = ""
    size_bytes: int | None = None
    metadata_error: str = ""
    attribution_status: AttributionStatus | str | None = None


@dataclass(frozen=True, slots=True)
class LedgerInstance:
    file_path: str
    actual_accession_number: str
    study_instance_uid: str
    series_instance_uid: str
    sop_instance_uid: str
    size_bytes: int
    attribution_status: str
    metadata_error: str


@dataclass(frozen=True, slots=True)
class LedgerRequest:
    position: int
    requested_accession: str
    transfer_status: str
    message: str
    duration_seconds: float
    reported_file_count: int
    reported_bytes: int
    attribution_status: str
    anonymization_status: str
    instances: tuple[LedgerInstance, ...] = ()


@dataclass(frozen=True, slots=True)
class LedgerBatch:
    batch_id: str
    profile_name: str
    status: str
    created_at: str
    completed_at: str
    anonymization_requested: bool
    anonymization_status: str
    anonymization_message: str
    pdi_requested: bool
    pdi_status: str
    pdi_output_directory: str
    pdi_message: str
    requests: tuple[LedgerRequest, ...] = ()


@dataclass(frozen=True, slots=True)
class LedgerReportPaths:
    csv_path: Path
    json_path: Path
    html_path: Path


class RunnerResult(Protocol):
    accession: str
    status: object
    message: str
    duration_seconds: float
    file_count: int
    received_bytes: int
    archived_files: Iterable[str]


class TaskLedger:
    """Crash-safe transfer ledger and privacy-safe acceptance report exporter.

    Attribution findings are evidence only.  A mismatch or unreadable metadata
    record is persisted as ``mismatch`` or ``unverifiable`` and is never raised
    as a transfer failure, preserving DcmGet's permissive receive policy.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        if self.path.is_symlink():
            raise TaskLedgerError("任务台账不能使用符号链接")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _make_private(self.path.parent, directory=True)
        self._initialize()

    def _initialize(self) -> None:
        lock = FileLock(str(self.path) + ".init.lock")
        with lock:
            try:
                with closing(sqlite3.connect(self.path, timeout=10)) as connection:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("PRAGMA synchronous=FULL")
                    connection.execute("PRAGMA foreign_keys=ON")
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS ledger_metadata (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        );
                        CREATE TABLE IF NOT EXISTS batches (
                            batch_id TEXT PRIMARY KEY,
                            profile_name TEXT NOT NULL,
                            status TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            completed_at TEXT NOT NULL DEFAULT '',
                            anonymization_requested INTEGER NOT NULL,
                            anonymization_status TEXT NOT NULL,
                            anonymization_message TEXT NOT NULL DEFAULT '',
                            pdi_requested INTEGER NOT NULL,
                            pdi_status TEXT NOT NULL,
                            pdi_output_directory TEXT NOT NULL DEFAULT '',
                            pdi_message TEXT NOT NULL DEFAULT ''
                        );
                        CREATE TABLE IF NOT EXISTS requests (
                            batch_id TEXT NOT NULL,
                            position INTEGER NOT NULL,
                            requested_accession TEXT NOT NULL,
                            transfer_status TEXT NOT NULL DEFAULT 'waiting',
                            message TEXT NOT NULL DEFAULT '',
                            duration_seconds REAL NOT NULL DEFAULT 0,
                            reported_file_count INTEGER NOT NULL DEFAULT 0,
                            reported_bytes INTEGER NOT NULL DEFAULT 0,
                            attribution_status TEXT NOT NULL DEFAULT 'unverifiable',
                            anonymization_status TEXT NOT NULL DEFAULT 'not_requested',
                            updated_at TEXT NOT NULL,
                            PRIMARY KEY (batch_id, requested_accession),
                            UNIQUE (batch_id, position),
                            FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
                                ON DELETE CASCADE
                        );
                        CREATE TABLE IF NOT EXISTS instances (
                            instance_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            batch_id TEXT NOT NULL,
                            requested_accession TEXT NOT NULL,
                            file_path TEXT NOT NULL,
                            actual_accession_number TEXT NOT NULL DEFAULT '',
                            study_instance_uid TEXT NOT NULL DEFAULT '',
                            series_instance_uid TEXT NOT NULL DEFAULT '',
                            sop_instance_uid TEXT NOT NULL DEFAULT '',
                            size_bytes INTEGER NOT NULL DEFAULT 0,
                            attribution_status TEXT NOT NULL,
                            metadata_error TEXT NOT NULL DEFAULT '',
                            created_at TEXT NOT NULL,
                            UNIQUE (batch_id, requested_accession, file_path),
                            FOREIGN KEY (batch_id, requested_accession)
                                REFERENCES requests(batch_id, requested_accession)
                                ON DELETE CASCADE
                        );
                        CREATE INDEX IF NOT EXISTS instances_batch_request
                            ON instances(batch_id, requested_accession);
                        CREATE INDEX IF NOT EXISTS instances_study_uid
                            ON instances(batch_id, study_instance_uid);
                        CREATE INDEX IF NOT EXISTS instances_sop_uid
                            ON instances(batch_id, sop_instance_uid);
                        """
                    )
                    version = connection.execute(
                        "SELECT value FROM ledger_metadata WHERE key = 'schema_version'"
                    ).fetchone()
                    if version is None:
                        connection.execute(
                            "INSERT INTO ledger_metadata(key, value) VALUES (?, ?)",
                            ("schema_version", str(LEDGER_SCHEMA_VERSION)),
                        )
                    elif int(version[0]) != LEDGER_SCHEMA_VERSION:
                        raise TaskLedgerError("任务台账版本不受支持")
                    secret = connection.execute(
                        "SELECT value FROM ledger_metadata WHERE key = 'report_secret'"
                    ).fetchone()
                    if secret is None:
                        connection.execute(
                            "INSERT INTO ledger_metadata(key, value) VALUES (?, ?)",
                            ("report_secret", secrets.token_hex(32)),
                        )
                    connection.commit()
                _make_private(self.path)
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise TaskLedgerError(f"无法初始化任务台账：{exc}") from exc

    def create_batch(
        self,
        requested_accessions: Iterable[str],
        *,
        batch_id: str | None = None,
        profile_name: str = "",
        anonymization_requested: bool = False,
        pdi_requested: bool = False,
    ) -> str:
        accessions = [str(value).strip() for value in requested_accessions]
        if not accessions or any(not value for value in accessions):
            raise TaskLedgerError("批次至少需要一个有效检查号")
        if len(accessions) != len(set(accessions)):
            raise TaskLedgerError("同一批次的请求检查号不能重复")
        identifier = str(batch_id or uuid.uuid4().hex).strip().lower()
        if not identifier or len(identifier) > 128:
            raise TaskLedgerError("批次编号格式不正确")
        now = _utc_now()
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO batches(
                        batch_id, profile_name, status, created_at, updated_at,
                        anonymization_requested, anonymization_status,
                        pdi_requested, pdi_status
                    ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        str(profile_name),
                        now,
                        now,
                        int(bool(anonymization_requested)),
                        "pending" if anonymization_requested else "not_requested",
                        int(bool(pdi_requested)),
                        "pending" if pdi_requested else "not_requested",
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO requests(
                        batch_id, position, requested_accession,
                        anonymization_status, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            identifier,
                            position,
                            accession,
                            "pending"
                            if anonymization_requested
                            else "not_requested",
                            now,
                        )
                        for position, accession in enumerate(accessions)
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise TaskLedgerError(f"批次编号已存在：{identifier}") from exc
        return identifier

    def record_accession_result(
        self,
        batch_id: str,
        requested_accession: str,
        transfer_status: object,
        *,
        message: str = "",
        duration_seconds: float = 0,
        reported_file_count: int = 0,
        reported_bytes: int = 0,
        anonymization_status: str | None = None,
    ) -> None:
        status = _status_text(transfer_status)
        now = _utc_now()
        assignments = [
            "transfer_status = ?",
            "message = ?",
            "duration_seconds = ?",
            "reported_file_count = ?",
            "reported_bytes = ?",
            "updated_at = ?",
        ]
        values: list[object] = [
            status,
            str(message),
            max(0.0, float(duration_seconds or 0)),
            max(0, int(reported_file_count or 0)),
            max(0, int(reported_bytes or 0)),
            now,
        ]
        if anonymization_status is not None:
            assignments.append("anonymization_status = ?")
            values.append(_nonempty_status(anonymization_status))
        values.extend((str(batch_id), str(requested_accession)))
        with self._transaction() as connection:
            cursor = connection.execute(
                f"UPDATE requests SET {', '.join(assignments)} "
                "WHERE batch_id = ? AND requested_accession = ?",
                values,
            )
            if cursor.rowcount != 1:
                raise TaskLedgerError("任务台账中不存在当前请求检查号")
            connection.execute(
                "UPDATE batches SET updated_at = ? WHERE batch_id = ?",
                (now, str(batch_id)),
            )

    def record_observed_dicom(
        self,
        batch_id: str,
        requested_accession: str,
        observed: ObservedDicom,
        *,
        expected_study_instance_uids: Iterable[str] = (),
        anonymized_output: bool = False,
    ) -> AttributionStatus:
        return self.record_observed_dicoms(
            batch_id,
            requested_accession,
            [observed],
            expected_study_instance_uids=expected_study_instance_uids,
            anonymized_output=anonymized_output,
        )[0]

    def record_observed_dicoms(
        self,
        batch_id: str,
        requested_accession: str,
        observations: Iterable[ObservedDicom],
        *,
        expected_study_instance_uids: Iterable[str] = (),
        anonymized_output: bool = False,
    ) -> tuple[AttributionStatus, ...]:
        """Record all instances for one request in a single transaction."""

        expected_studies = {
            str(value).strip()
            for value in expected_study_instance_uids
            if str(value).strip()
        }
        rows: list[tuple[ObservedDicom, AttributionStatus, str, int]] = []
        for observed in observations:
            status = _attribution_status(
                requested_accession,
                observed,
                expected_studies,
                anonymized_output=anonymized_output,
            )
            path = str(Path(observed.file_path).expanduser())
            size = observed.size_bytes
            if size is None:
                try:
                    size = Path(path).stat().st_size
                except OSError:
                    size = 0
            rows.append((observed, status, path, max(0, int(size or 0))))
        if not rows:
            return ()
        now = _utc_now()
        with self._transaction() as connection:
            exists = connection.execute(
                """
                SELECT 1 FROM requests
                WHERE batch_id = ? AND requested_accession = ?
                """,
                (str(batch_id), str(requested_accession)),
            ).fetchone()
            if exists is None:
                raise TaskLedgerError("任务台账中不存在当前请求检查号")
            connection.executemany(
                """
                INSERT INTO instances(
                    batch_id, requested_accession, file_path,
                    actual_accession_number, study_instance_uid,
                    series_instance_uid, sop_instance_uid, size_bytes,
                    attribution_status, metadata_error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id, requested_accession, file_path) DO UPDATE SET
                    actual_accession_number = excluded.actual_accession_number,
                    study_instance_uid = excluded.study_instance_uid,
                    series_instance_uid = excluded.series_instance_uid,
                    sop_instance_uid = excluded.sop_instance_uid,
                    size_bytes = excluded.size_bytes,
                    attribution_status = excluded.attribution_status,
                    metadata_error = excluded.metadata_error
                """,
                (
                    (
                        str(batch_id),
                        str(requested_accession),
                        path,
                        str(observed.actual_accession_number).strip(),
                        str(observed.study_instance_uid).strip(),
                        str(observed.series_instance_uid).strip(),
                        str(observed.sop_instance_uid).strip(),
                        size,
                        status.value,
                        str(observed.metadata_error),
                        now,
                    )
                    for observed, status, path, size in rows
                ),
            )
            self._refresh_request_attribution(
                connection,
                str(batch_id),
                str(requested_accession),
                now,
            )
        return tuple(status for _observed, status, _path, _size in rows)

    def record_dicom_file(
        self,
        batch_id: str,
        requested_accession: str,
        file_path: str | Path,
        *,
        expected_study_instance_uids: Iterable[str] = (),
        anonymized_output: bool = False,
    ) -> AttributionStatus:
        observed = inspect_dicom_file(file_path)
        return self.record_observed_dicom(
            batch_id,
            requested_accession,
            observed,
            expected_study_instance_uids=expected_study_instance_uids,
            anonymized_output=anonymized_output,
        )

    def record_runner_result(
        self,
        batch_id: str,
        result: RunnerResult,
        *,
        observed_instances: Iterable[ObservedDicom] | None = None,
        anonymization_status: str | None = None,
        expected_study_instance_uids: Iterable[str] = (),
        anonymized_output: bool = False,
    ) -> None:
        """Persist one DownloadRunner result without changing its outcome.

        Integrations that anonymize files should pass metadata captured before
        anonymization in ``observed_instances``.  If only final anonymous files
        are available, ``anonymized_output=True`` records them as unverifiable
        instead of reporting a false accession mismatch.
        """

        accession = str(result.accession)
        self.record_accession_result(
            batch_id,
            accession,
            result.status,
            message=str(getattr(result, "message", "")),
            duration_seconds=float(getattr(result, "duration_seconds", 0) or 0),
            reported_file_count=int(getattr(result, "file_count", 0) or 0),
            reported_bytes=int(getattr(result, "received_bytes", 0) or 0),
            anonymization_status=anonymization_status,
        )
        observations = observed_instances
        metadata_from_anonymous_files = bool(
            anonymized_output and observations is None
        )
        if observations is None:
            observations = (
                inspect_dicom_file(path)
                for path in getattr(result, "archived_files", ())
            )
        self.record_observed_dicoms(
            batch_id,
            accession,
            observations,
            expected_study_instance_uids=expected_study_instance_uids,
            anonymized_output=metadata_from_anonymous_files,
        )

    def record_anonymization_result(
        self,
        batch_id: str,
        status: object,
        *,
        message: str = "",
    ) -> None:
        self._update_batch_fields(
            batch_id,
            anonymization_status=_status_text(status),
            anonymization_message=str(message),
        )

    def record_pdi_result(
        self,
        batch_id: str,
        status: object,
        *,
        output_directory: str | Path = "",
        message: str = "",
    ) -> None:
        self._update_batch_fields(
            batch_id,
            pdi_status=_status_text(status),
            pdi_output_directory=str(output_directory),
            pdi_message=str(message),
        )

    def complete_batch(self, batch_id: str, status: object) -> None:
        now = _utc_now()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE batches
                SET status = ?, completed_at = ?, updated_at = ?
                WHERE batch_id = ?
                """,
                (_status_text(status), now, now, str(batch_id)),
            )
            if cursor.rowcount != 1:
                raise TaskLedgerError("任务台账中不存在当前批次")

    def load_batch(self, batch_id: str) -> LedgerBatch:
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                batch_row = connection.execute(
                    "SELECT * FROM batches WHERE batch_id = ?", (str(batch_id),)
                ).fetchone()
                if batch_row is None:
                    raise TaskLedgerError("任务台账中不存在当前批次")
                grouped: dict[str, list[LedgerInstance]] = {}
                instance_cursor = connection.execute(
                    """
                    SELECT * FROM instances
                    WHERE batch_id = ? ORDER BY instance_id
                    """,
                    (str(batch_id),),
                )
                for row in _iter_cursor_rows(instance_cursor):
                    grouped.setdefault(
                        str(row["requested_accession"]), []
                    ).append(
                        LedgerInstance(
                            file_path=str(row["file_path"]),
                            actual_accession_number=str(
                                row["actual_accession_number"]
                            ),
                            study_instance_uid=str(row["study_instance_uid"]),
                            series_instance_uid=str(row["series_instance_uid"]),
                            sop_instance_uid=str(row["sop_instance_uid"]),
                            size_bytes=int(row["size_bytes"]),
                            attribution_status=str(row["attribution_status"]),
                            metadata_error=str(row["metadata_error"]),
                        )
                    )
                request_cursor = connection.execute(
                    """
                    SELECT * FROM requests
                    WHERE batch_id = ? ORDER BY position
                    """,
                    (str(batch_id),),
                )
                requests = tuple(
                    LedgerRequest(
                        position=int(row["position"]),
                        requested_accession=str(row["requested_accession"]),
                        transfer_status=str(row["transfer_status"]),
                        message=str(row["message"]),
                        duration_seconds=float(row["duration_seconds"]),
                        reported_file_count=int(row["reported_file_count"]),
                        reported_bytes=int(row["reported_bytes"]),
                        attribution_status=str(row["attribution_status"]),
                        anonymization_status=str(row["anonymization_status"]),
                        instances=tuple(
                            grouped.get(str(row["requested_accession"]), ())
                        ),
                    )
                    for row in _iter_cursor_rows(request_cursor)
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise TaskLedgerError(f"无法读取任务台账：{exc}") from exc
        return LedgerBatch(
            batch_id=str(batch_row["batch_id"]),
            profile_name=str(batch_row["profile_name"]),
            status=str(batch_row["status"]),
            created_at=str(batch_row["created_at"]),
            completed_at=str(batch_row["completed_at"]),
            anonymization_requested=bool(batch_row["anonymization_requested"]),
            anonymization_status=str(batch_row["anonymization_status"]),
            anonymization_message=str(batch_row["anonymization_message"]),
            pdi_requested=bool(batch_row["pdi_requested"]),
            pdi_status=str(batch_row["pdi_status"]),
            pdi_output_directory=str(batch_row["pdi_output_directory"]),
            pdi_message=str(batch_row["pdi_message"]),
            requests=requests,
        )

    def export_reports(
        self,
        batch_id: str,
        output_directory: str | Path,
        *,
        redact: bool = True,
    ) -> LedgerReportPaths:
        root = Path(output_directory).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        secret = self._report_secret()
        safe_batch = (
            _pseudonym(secret, "BATCH", str(batch_id))
            if redact
            else _safe_filename(str(batch_id))
        )
        paths = LedgerReportPaths(
            csv_path=root / f"dcmget-acceptance-{safe_batch}.csv",
            json_path=root / f"dcmget-acceptance-{safe_batch}.json",
            html_path=root / f"dcmget-acceptance-{safe_batch}.html",
        )
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                summary = self._report_summary(
                    connection, str(batch_id), secret=secret, redact=redact
                )
                self._write_json_stream(
                    paths.json_path,
                    connection,
                    str(batch_id),
                    summary,
                    secret=secret,
                    redact=redact,
                )
                self._write_csv_stream(
                    paths.csv_path,
                    connection,
                    str(batch_id),
                    summary,
                    secret=secret,
                    redact=redact,
                )
                self._write_html_stream(
                    paths.html_path,
                    connection,
                    str(batch_id),
                    summary,
                    secret=secret,
                    redact=redact,
                )
                connection.commit()
        except TaskLedgerError:
            raise
        except sqlite3.Error as exc:
            raise TaskLedgerError(f"无法导出任务验收报告：{exc}") from exc
        return paths

    def report_data(self, batch_id: str, *, redact: bool = True) -> dict[str, object]:
        secret = self._report_secret()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                report = self._report_summary(
                    connection, str(batch_id), secret=secret, redact=redact
                )
                requests: list[dict[str, object]] = []
                current: dict[str, object] | None = None
                current_key: tuple[int, str] | None = None
                for row in self._iter_detailed_report_rows(connection, str(batch_id)):
                    key = (int(row["position"]), str(row["requested_accession"]))
                    if key != current_key:
                        current = self._request_payload(
                            row, secret=secret, redact=redact
                        )
                        current["instances"] = []
                        requests.append(current)
                        current_key = key
                    if row["instance_id"] is not None and current is not None:
                        instances = current["instances"]
                        assert isinstance(instances, list)
                        instances.append(
                            self._instance_payload(
                                row,
                                secret=secret,
                                redact=redact,
                                requested_accession=str(row["requested_accession"]),
                            )
                        )
                connection.commit()
        except TaskLedgerError:
            raise
        except sqlite3.Error as exc:
            raise TaskLedgerError(f"无法读取任务验收报告：{exc}") from exc
        report["requests"] = requests
        return report

    def _report_summary(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        *,
        secret: bytes,
        redact: bool,
    ) -> dict[str, object]:
        batch = connection.execute(
            "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise TaskLedgerError("任务台账中不存在当前批次")
        request_totals = connection.execute(
            """
            SELECT COUNT(*) AS request_count,
                   COALESCE(SUM(attribution_status = 'matched'), 0) AS matched,
                   COALESCE(SUM(attribution_status = 'mismatch'), 0) AS mismatch,
                   COALESCE(SUM(attribution_status = 'unverifiable'), 0) AS unverifiable
            FROM requests WHERE batch_id = ?
            """,
            (batch_id,),
        ).fetchone()
        instance_totals = connection.execute(
            """
            SELECT COUNT(*) AS instance_count,
                   COALESCE(SUM(size_bytes), 0) AS total_bytes
            FROM instances WHERE batch_id = ?
            """,
            (batch_id,),
        ).fetchone()
        assert request_totals is not None and instance_totals is not None
        pdi_directory = str(batch["pdi_output_directory"])
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "generated_at": _utc_now(),
            "redacted": bool(redact),
            "batch": {
                "batch_id": _report_identifier(
                    secret, "BATCH", str(batch["batch_id"]), redact=redact
                ),
                "profile_name": (
                    "实例配置"
                    if redact and str(batch["profile_name"])
                    else str(batch["profile_name"])
                ),
                "status": str(batch["status"]),
                "created_at": str(batch["created_at"]),
                "completed_at": str(batch["completed_at"]),
                "request_count": int(request_totals["request_count"]),
                "instance_count": int(instance_totals["instance_count"]),
                "total_bytes": int(instance_totals["total_bytes"]),
                "attribution_counts": {
                    "matched": int(request_totals["matched"]),
                    "mismatch": int(request_totals["mismatch"]),
                    "unverifiable": int(request_totals["unverifiable"]),
                },
                "anonymization": {
                    "requested": bool(batch["anonymization_requested"]),
                    "status": str(batch["anonymization_status"]),
                    "message": _redacted_batch_message(
                        batch["anonymization_message"], redact=redact
                    ),
                },
                "pdi": {
                    "requested": bool(batch["pdi_requested"]),
                    "status": str(batch["pdi_status"]),
                    "output_directory": (
                        "[已脱敏路径]"
                        if redact and pdi_directory
                        else pdi_directory
                    ),
                    "message": _redacted_batch_message(
                        batch["pdi_message"], redact=redact
                    ),
                },
            },
        }

    @staticmethod
    def _iter_detailed_report_rows(
        connection: sqlite3.Connection, batch_id: str
    ) -> Iterator[sqlite3.Row]:
        cursor = connection.execute(
            """
            SELECT r.position, r.requested_accession, r.transfer_status,
                   r.message, r.duration_seconds, r.reported_file_count,
                   r.reported_bytes, r.attribution_status,
                   r.anonymization_status,
                   COALESCE(a.actual_file_count, 0) AS actual_file_count,
                   COALESCE(a.actual_bytes, 0) AS actual_bytes,
                   i.instance_id, i.file_path, i.actual_accession_number,
                   i.study_instance_uid, i.series_instance_uid,
                   i.sop_instance_uid, i.size_bytes AS instance_size_bytes,
                   i.attribution_status AS instance_attribution_status,
                   i.metadata_error
            FROM requests AS r
            LEFT JOIN (
                SELECT batch_id, requested_accession,
                       COUNT(*) AS actual_file_count,
                       COALESCE(SUM(size_bytes), 0) AS actual_bytes
                FROM instances WHERE batch_id = ?
                GROUP BY batch_id, requested_accession
            ) AS a
              ON a.batch_id = r.batch_id
             AND a.requested_accession = r.requested_accession
            LEFT JOIN instances AS i
              ON i.batch_id = r.batch_id
             AND i.requested_accession = r.requested_accession
            WHERE r.batch_id = ?
            ORDER BY r.position, i.instance_id
            """,
            (batch_id, batch_id),
        )
        yield from _iter_cursor_rows(cursor)

    @staticmethod
    def _iter_request_report_rows(
        connection: sqlite3.Connection, batch_id: str
    ) -> Iterator[sqlite3.Row]:
        cursor = connection.execute(
            """
            SELECT r.position, r.requested_accession, r.transfer_status,
                   r.message, r.duration_seconds, r.reported_file_count,
                   r.reported_bytes, r.attribution_status,
                   r.anonymization_status,
                   COUNT(i.instance_id) AS actual_file_count,
                   COALESCE(SUM(i.size_bytes), 0) AS actual_bytes
            FROM requests AS r
            LEFT JOIN instances AS i
              ON i.batch_id = r.batch_id
             AND i.requested_accession = r.requested_accession
            WHERE r.batch_id = ?
            GROUP BY r.batch_id, r.position, r.requested_accession
            ORDER BY r.position
            """,
            (batch_id,),
        )
        yield from _iter_cursor_rows(cursor)

    @staticmethod
    def _request_payload(
        row: sqlite3.Row, *, secret: bytes, redact: bool
    ) -> dict[str, object]:
        requested_accession = str(row["requested_accession"])
        return {
            "position": int(row["position"]) + 1,
            "requested_accession": _report_identifier(
                secret, "REQ", requested_accession, redact=redact
            ),
            "transfer_status": str(row["transfer_status"]),
            "message": _stream_redact_text(
                row["message"],
                secret,
                redact=redact,
                requested_accession=requested_accession,
            ),
            "duration_seconds": float(row["duration_seconds"]),
            "reported_file_count": int(row["reported_file_count"]),
            "reported_bytes": int(row["reported_bytes"]),
            "actual_file_count": int(row["actual_file_count"]),
            "actual_bytes": int(row["actual_bytes"]),
            "attribution_status": str(row["attribution_status"]),
            "anonymization_status": str(row["anonymization_status"]),
        }

    @staticmethod
    def _instance_payload(
        row: sqlite3.Row,
        *,
        secret: bytes,
        redact: bool,
        requested_accession: str,
    ) -> dict[str, object]:
        values = {
            "file_path": str(row["file_path"] or ""),
            "actual_accession_number": str(
                row["actual_accession_number"] or ""
            ),
            "study_instance_uid": str(row["study_instance_uid"] or ""),
            "series_instance_uid": str(row["series_instance_uid"] or ""),
            "sop_instance_uid": str(row["sop_instance_uid"] or ""),
        }
        return {
            "file_path": _report_identifier(
                secret, "FILE", values["file_path"], redact=redact
            ),
            "actual_accession_number": _report_identifier(
                secret,
                "ACC",
                values["actual_accession_number"],
                redact=redact,
            ),
            "study_instance_uid": _report_identifier(
                secret, "STUDY", values["study_instance_uid"], redact=redact
            ),
            "series_instance_uid": _report_identifier(
                secret, "SERIES", values["series_instance_uid"], redact=redact
            ),
            "sop_instance_uid": _report_identifier(
                secret, "SOP", values["sop_instance_uid"], redact=redact
            ),
            "size_bytes": int(row["instance_size_bytes"] or 0),
            "attribution_status": str(
                row["instance_attribution_status"] or ""
            ),
            "metadata_error": _stream_redact_text(
                row["metadata_error"],
                secret,
                redact=redact,
                requested_accession=requested_accession,
                extra_values=(
                    ("FILE", values["file_path"]),
                    ("ACC", values["actual_accession_number"]),
                    ("STUDY", values["study_instance_uid"]),
                    ("SERIES", values["series_instance_uid"]),
                    ("SOP", values["sop_instance_uid"]),
                ),
            ),
        }

    def _report_secret(self) -> bytes:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT value FROM ledger_metadata WHERE key = 'report_secret'"
                ).fetchone()
        except sqlite3.Error as exc:
            raise TaskLedgerError(f"无法读取报告脱敏密钥：{exc}") from exc
        if row is None:
            raise TaskLedgerError("任务台账缺少报告脱敏密钥")
        try:
            value = bytes.fromhex(str(row[0]))
        except ValueError as exc:
            raise TaskLedgerError("任务台账报告脱敏密钥已损坏") from exc
        if len(value) != 32:
            raise TaskLedgerError("任务台账报告脱敏密钥已损坏")
        return value

    def _update_batch_fields(self, batch_id: str, **fields: object) -> None:
        allowed = {
            "anonymization_status",
            "anonymization_message",
            "pdi_status",
            "pdi_output_directory",
            "pdi_message",
        }
        if not fields or set(fields) - allowed:
            raise TaskLedgerError("任务台账批次字段无效")
        now = _utc_now()
        assignments = [f"{key} = ?" for key in fields]
        values = [str(value) for value in fields.values()]
        values.extend((now, str(batch_id)))
        with self._transaction() as connection:
            cursor = connection.execute(
                f"UPDATE batches SET {', '.join(assignments)}, updated_at = ? "
                "WHERE batch_id = ?",
                values,
            )
            if cursor.rowcount != 1:
                raise TaskLedgerError("任务台账中不存在当前批次")

    @staticmethod
    def _refresh_request_attribution(
        connection: sqlite3.Connection,
        batch_id: str,
        requested_accession: str,
        now: str,
    ) -> None:
        totals = connection.execute(
            """
            SELECT COUNT(*) AS instance_count,
                   COALESCE(SUM(attribution_status = 'matched'), 0) AS matched,
                   COALESCE(SUM(attribution_status = 'mismatch'), 0) AS mismatch
            FROM instances
            WHERE batch_id = ? AND requested_accession = ?
            """,
            (batch_id, requested_accession),
        ).fetchone()
        assert totals is not None
        instance_count = int(totals["instance_count"])
        if int(totals["mismatch"]):
            aggregate = AttributionStatus.MISMATCH.value
        elif instance_count and int(totals["matched"]) == instance_count:
            aggregate = AttributionStatus.MATCHED.value
        else:
            aggregate = AttributionStatus.UNVERIFIABLE.value
        connection.execute(
            """
            UPDATE requests
            SET attribution_status = ?, updated_at = ?
            WHERE batch_id = ? AND requested_accession = ?
            """,
            (aggregate, now, batch_id, requested_accession),
        )
        connection.execute(
            "UPDATE batches SET updated_at = ? WHERE batch_id = ?",
            (now, batch_id),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                except Exception:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
        except TaskLedgerError:
            raise
        except sqlite3.Error as exc:
            raise TaskLedgerError(f"无法更新任务台账：{exc}") from exc

    def _write_json_stream(
        self,
        path: Path,
        connection: sqlite3.Connection,
        batch_id: str,
        summary: Mapping[str, object],
        *,
        secret: bytes,
        redact: bool,
    ) -> None:
        batch_json = json.dumps(
            summary["batch"], ensure_ascii=False, indent=2
        ).replace("\n", "\n  ")
        with _atomic_text_handle(path, encoding="utf-8", newline="\n") as handle:
            handle.write("{\n")
            handle.write(
                f'  "schema_version": {int(summary["schema_version"])},\n'
            )
            handle.write(
                '  "generated_at": '
                + json.dumps(summary["generated_at"], ensure_ascii=False)
                + ",\n"
            )
            handle.write(
                f'  "redacted": {json.dumps(bool(summary["redacted"]))},\n'
            )
            handle.write(f'  "batch": {batch_json},\n')
            handle.write('  "requests": [\n')
            first_request = True
            current_key: tuple[int, str] | None = None
            instance_open = False
            first_instance = True
            for row in self._iter_detailed_report_rows(connection, batch_id):
                key = (int(row["position"]), str(row["requested_accession"]))
                if key != current_key:
                    if instance_open:
                        handle.write("]}")
                    if not first_request:
                        handle.write(",\n")
                    request = self._request_payload(
                        row, secret=secret, redact=redact
                    )
                    encoded = json.dumps(
                        request, ensure_ascii=False, separators=(",", ":")
                    )
                    handle.write("    " + encoded[:-1] + ',"instances":[')
                    first_request = False
                    first_instance = True
                    instance_open = True
                    current_key = key
                if row["instance_id"] is not None:
                    if not first_instance:
                        handle.write(",")
                    handle.write(
                        json.dumps(
                            self._instance_payload(
                                row,
                                secret=secret,
                                redact=redact,
                                requested_accession=str(
                                    row["requested_accession"]
                                ),
                            ),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    first_instance = False
            if instance_open:
                handle.write("]}")
            if not first_request:
                handle.write("\n")
            handle.write("  ]\n}\n")

    def _write_csv_stream(
        self,
        path: Path,
        connection: sqlite3.Connection,
        batch_id: str,
        summary: Mapping[str, object],
        *,
        secret: bytes,
        redact: bool,
    ) -> None:
        headers = (
            "序号",
            "请求检查号",
            "传输状态",
            "请求归属核对",
            "文件归属核对",
            "匿名状态",
            "实际文件数",
            "实际字节数",
            "实际检查号",
            "StudyInstanceUID",
            "SeriesInstanceUID",
            "SOPInstanceUID",
            "文件路径",
            "元数据错误",
            "传输消息",
        )
        del summary
        with _atomic_text_handle(
            path, encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            for source in self._iter_detailed_report_rows(connection, batch_id):
                request = self._request_payload(
                    source, secret=secret, redact=redact
                )
                instance = (
                    self._instance_payload(
                        source,
                        secret=secret,
                        redact=redact,
                        requested_accession=str(source["requested_accession"]),
                    )
                    if source["instance_id"] is not None
                    else {}
                )
                row = {
                    "序号": request["position"],
                    "请求检查号": request["requested_accession"],
                    "传输状态": request["transfer_status"],
                    "请求归属核对": _attribution_label(
                        request["attribution_status"]
                    ),
                    "文件归属核对": _attribution_label(
                        instance.get("attribution_status", "")
                    ),
                    "匿名状态": request["anonymization_status"],
                    "实际文件数": request["actual_file_count"],
                    "实际字节数": request["actual_bytes"],
                    "实际检查号": instance.get("actual_accession_number", ""),
                    "StudyInstanceUID": instance.get("study_instance_uid", ""),
                    "SeriesInstanceUID": instance.get("series_instance_uid", ""),
                    "SOPInstanceUID": instance.get("sop_instance_uid", ""),
                    "文件路径": instance.get("file_path", ""),
                    "元数据错误": instance.get("metadata_error", ""),
                    "传输消息": request["message"],
                }
                writer.writerow(
                    {key: _csv_safe(value) for key, value in row.items()}
                )

    def _write_html_stream(
        self,
        path: Path,
        connection: sqlite3.Connection,
        batch_id: str,
        report: Mapping[str, object],
        *,
        secret: bytes,
        redact: bool,
    ) -> None:
        batch = report["batch"]  # type: ignore[index]
        privacy_notice = (
            "本报告已对检查号、UID 和文件路径进行脱敏。"
            if report["redacted"]
            else "本报告包含原始标识符，请按医疗数据管理要求保存。"
        )
        prefix = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DcmGet 任务验收报告</title>
<style>
body{{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:32px;color:#172033;background:#f4f7fb}}
main{{max-width:1200px;margin:auto;background:#fff;padding:28px;border-radius:12px}}
h1{{color:#075985}} .notice{{padding:12px;background:#eef8ff;border-left:4px solid #0284c7}}
.summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:18px 0}}
.card{{padding:12px;border:1px solid #dbe4ef;border-radius:8px}} table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #dbe4ef;padding:8px;text-align:left;vertical-align:top}} th{{background:#edf5fb}}
</style></head><body><main>
<h1>DcmGet 任务台账与验收报告</h1><p class="notice">{_h(privacy_notice)} 仅用于传输验收，不替代临床诊断。</p>
<div class="summary">
<div class="card">批次：{_h(batch['batch_id'])}</div>
<div class="card">批次状态：{_h(batch['status'])}</div>
<div class="card">请求数：{_h(batch['request_count'])}</div>
<div class="card">DICOM 文件：{_h(batch['instance_count'])}</div>
<div class="card">总数据量：{_h(_format_bytes(int(batch['total_bytes'])))}</div>
<div class="card">核对一致：{_h(batch['attribution_counts']['matched'])}</div>
<div class="card">内容不匹配：{_h(batch['attribution_counts']['mismatch'])}</div>
<div class="card">无法核对：{_h(batch['attribution_counts']['unverifiable'])}</div>
<div class="card">匿名：{_h(batch['anonymization']['status'])}</div>
<div class="card">PDI：{_h(batch['pdi']['status'])}</div>
</div>
<table><thead><tr><th>序号</th><th>请求检查号</th><th>传输状态</th><th>归属核对</th>
<th>文件数</th><th>数据量</th><th>匿名</th><th>说明</th></tr></thead>
<tbody>"""
        suffix = f"""</tbody></table>
<p>生成时间：{_h(report['generated_at'])}</p>
</main></body></html>"""
        with _atomic_text_handle(path, encoding="utf-8", newline="\n") as handle:
            handle.write(prefix)
            for source in self._iter_request_report_rows(connection, batch_id):
                request = self._request_payload(
                    source, secret=secret, redact=redact
                )
                handle.write(
                    "<tr>"
                    f"<td>{_h(request['position'])}</td>"
                    f"<td>{_h(request['requested_accession'])}</td>"
                    f"<td>{_h(request['transfer_status'])}</td>"
                    f"<td>{_h(_attribution_label(request['attribution_status']))}</td>"
                    f"<td>{_h(request['actual_file_count'])}</td>"
                    f"<td>{_h(_format_bytes(int(request['actual_bytes'])))}</td>"
                    f"<td>{_h(request['anonymization_status'])}</td>"
                    f"<td>{_h(request['message'])}</td>"
                    "</tr>"
                )
            handle.write(suffix)


def inspect_dicom_file(path: str | Path) -> ObservedDicom:
    source = Path(path).expanduser()
    try:
        size = source.stat().st_size
    except OSError:
        size = 0
    try:
        dataset = dcmread(
            source,
            stop_before_pixels=True,
            force=True,
            specific_tags=list(_DICOM_METADATA_TAGS),
        )
        file_meta = getattr(dataset, "file_meta", None)
        sop_uid = str(getattr(dataset, "SOPInstanceUID", "") or "").strip()
        if not sop_uid:
            sop_uid = str(
                getattr(file_meta, "MediaStorageSOPInstanceUID", "") or ""
            ).strip()
        metadata_error = ""
        if not any(
            (
                sop_uid,
                str(getattr(dataset, "StudyInstanceUID", "") or "").strip(),
                str(getattr(dataset, "SeriesInstanceUID", "") or "").strip(),
            )
        ):
            metadata_error = "未读取到可识别的 DICOM UID 元数据"
        return ObservedDicom(
            file_path=str(source),
            actual_accession_number=str(
                getattr(dataset, "AccessionNumber", "") or ""
            ).strip(),
            study_instance_uid=str(
                getattr(dataset, "StudyInstanceUID", "") or ""
            ).strip(),
            series_instance_uid=str(
                getattr(dataset, "SeriesInstanceUID", "") or ""
            ).strip(),
            sop_instance_uid=sop_uid,
            size_bytes=size,
            metadata_error=metadata_error,
        )
    except Exception as exc:
        return ObservedDicom(
            file_path=str(source),
            size_bytes=size,
            metadata_error=str(exc).strip() or exc.__class__.__name__,
            attribution_status=AttributionStatus.UNVERIFIABLE,
        )


def _attribution_status(
    requested_accession: str,
    observed: ObservedDicom,
    expected_studies: set[str],
    *,
    anonymized_output: bool,
) -> AttributionStatus:
    explicit = getattr(observed, "attribution_status", None)
    if explicit is not None:
        try:
            return AttributionStatus(str(getattr(explicit, "value", explicit)))
        except ValueError:
            return AttributionStatus.UNVERIFIABLE
    if observed.metadata_error or anonymized_output:
        return AttributionStatus.UNVERIFIABLE
    actual_study = str(observed.study_instance_uid).strip()
    if expected_studies and actual_study:
        return (
            AttributionStatus.MATCHED
            if actual_study in expected_studies
            else AttributionStatus.MISMATCH
        )
    actual_accession = str(observed.actual_accession_number).strip()
    if not actual_accession:
        return AttributionStatus.UNVERIFIABLE
    return (
        AttributionStatus.MATCHED
        if actual_accession == str(requested_accession).strip()
        else AttributionStatus.MISMATCH
    )


def _status_text(value: object) -> str:
    text = str(getattr(value, "value", value) or "").strip()
    return text or "unknown"


def _nonempty_status(value: object) -> str:
    text = _status_text(value)
    if len(text) > 80:
        raise TaskLedgerError("任务台账状态文本过长")
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_private(path: Path, *, directory: bool = False) -> None:
    if not path.exists():
        return
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError:
        pass


def _safe_filename(value: str) -> str:
    safe = "".join(character if character.isalnum() else "-" for character in value)
    return safe.strip("-")[:64] or "batch"


def _pseudonym(secret: bytes, prefix: str, value: str) -> str:
    digest = hmac.new(
        secret,
        f"{prefix}:{value}".encode("utf-8", errors="replace"),
        hashlib.sha256,
    ).hexdigest()[:16]
    return f"{prefix}-{digest.upper()}"


def _report_identifier(
    secret: bytes, prefix: str, value: str, *, redact: bool
) -> str:
    if not value or not redact:
        return value
    return _pseudonym(secret, prefix, value)


_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![\w:])(?:[A-Za-z]:[\\/]|\\\\)[^\s<>\"']+"
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![:/\w<])/(?:[^\s<>\"']+)")
_DICOM_UID_TEXT = re.compile(r"(?<![\d.])(?:\d+\.){2,}\d+(?![\d.])")
_LABELED_ACCESSION = re.compile(
    r"(?i)(accession(?:\s*number)?|检查号)(\s*[:=：]\s*)([^\s,;，；]+)"
)


def _stream_redact_text(
    value: object,
    secret: bytes,
    *,
    redact: bool,
    requested_accession: str = "",
    pdi_output_directory: str = "",
    extra_values: Iterable[tuple[str, str]] = (),
) -> str:
    """Redact one row without building a batch-wide identifier map.

    Dedicated report identifier columns are pseudonymized separately.  Free
    text is processed with the identifiers available in the current row and
    conservative path/UID/accession-label patterns, keeping memory bounded for
    batches containing millions of instances.
    """

    text = str(value or "")
    if not redact or not text:
        return text
    replacements: dict[str, str] = {}
    if requested_accession:
        replacements[requested_accession] = _pseudonym(
            secret, "REQ", requested_accession
        )
    if pdi_output_directory:
        replacements[pdi_output_directory] = "[已脱敏路径]"
    for prefix, item in extra_values:
        if item:
            replacements[item] = _pseudonym(secret, prefix, item)
    text = _redact_text(text, replacements)
    text = _WINDOWS_ABSOLUTE_PATH.sub("[已脱敏路径]", text)
    text = _POSIX_ABSOLUTE_PATH.sub("[已脱敏路径]", text)
    text = _DICOM_UID_TEXT.sub(
        lambda match: _pseudonym(secret, "UID", match.group(0)), text
    )
    text = _LABELED_ACCESSION.sub(
        lambda match: match.group(1) + match.group(2) + "[已脱敏]", text
    )
    return text


def _redacted_batch_message(value: object, *, redact: bool) -> str:
    text = str(value or "")
    if redact and text:
        # Batch-level messages are unstructured and cannot be safely matched to
        # one request while streaming.  Keep the field/status but never risk
        # exposing an identifier from any of millions of request rows.
        return "[已脱敏内容]"
    return text


def _redact_text(value: object, replacements: Mapping[str, str]) -> str:
    text = str(value or "")
    for original in sorted(replacements, key=len, reverse=True):
        if original:
            text = text.replace(original, replacements[original])
    return text


def _iter_cursor_rows(
    cursor: sqlite3.Cursor, batch_size: int = _REPORT_FETCH_SIZE
) -> Iterator[sqlite3.Row]:
    """Yield rows in bounded batches; never materialize an entire result set."""

    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            return
        yield from rows


@contextmanager
def _atomic_text_handle(
    path: Path, *, encoding: str, newline: str
) -> Iterator[TextIO]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline=newline) as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _make_private(path)
    finally:
        temporary.unlink(missing_ok=True)


def _h(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _csv_safe(value: object) -> object:
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _attribution_label(value: object) -> str:
    return {
        AttributionStatus.MATCHED.value: "核对一致",
        AttributionStatus.MISMATCH.value: "内容不匹配",
        AttributionStatus.UNVERIFIABLE.value: "无法核对",
    }.get(str(value), str(value or ""))


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024
    return f"{amount:.2f} TiB"
