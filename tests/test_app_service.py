from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from dcmget.app_service import AppServiceError, DcmGetAppService
from dcmget.config import AppConfig
from dcmget.core import AccessionResult, AccessionStatus, BatchSummary, ToolPaths
from dcmget.licensing import LicenseError, TrialInfo
from dcmget.pdi import PdiExportResult, PdiStatus
from dcmget.task_ledger import TaskLedger
from dcmget.task_state import TaskCheckpointStore


def _tools(tmp_path: Path) -> ToolPaths:
    return ToolPaths(
        tmp_path / "movescu",
        tmp_path / "storescp",
        tmp_path,
        "3.7.0",
        dcmmkdir=tmp_path / "dcmmkdir",
    )


def _service(tmp_path: Path, **kwargs) -> DcmGetAppService:
    return DcmGetAppService(
        task_store=TaskCheckpointStore(tmp_path / "active-task.sqlite3"),
        task_ledger=TaskLedger(tmp_path / "task-ledger.sqlite3"),
        project_root=tmp_path,
        load_license_fn=lambda: object(),
        **kwargs,
    )


class _CompletingRunner:
    instances: list["_CompletingRunner"] = []

    def __init__(self, _config, _tools, **callbacks):
        self.callbacks = callbacks
        self.cancelled = False
        self.paused = False
        self.__class__.instances.append(self)

    def request_cancel(self) -> None:
        self.cancelled = True

    def request_pause(self) -> None:
        self.paused = True
        self.callbacks["state_callback"]("pause_pending")

    def request_resume(self) -> None:
        self.paused = False
        self.callbacks["state_callback"]("downloading")

    def run(self, accessions):
        values = list(accessions)
        self.callbacks["state_callback"]("starting_receiver")
        self.callbacks["state_callback"]("downloading")
        self.callbacks["ready_callback"]()
        results = []
        for index, accession in enumerate(values, 1):
            result = AccessionResult(
                accession,
                AccessionStatus.COMPLETED,
                file_count=1,
                received_bytes=1024,
                archived_files=[f"/{accession}.dcm"],
            )
            results.append(result)
            self.callbacks["audit_callback"](result, ())
            self.callbacks["progress_callback"](index, len(values), result)
        return BatchSummary(results)


def test_service_events_are_monotonic_json_safe_and_task_survives_unsubscribe(
    tmp_path,
):
    release = threading.Event()

    class Runner(_CompletingRunner):
        def run(self, accessions):
            self.callbacks["state_callback"]("downloading")
            assert release.wait(2)
            return super().run(accessions)

    service = _service(tmp_path, runner_factory=Runner)
    observed = []
    unsubscribe = service.subscribe(observed.append)
    service.start_task(
        AppConfig(dicom_destination_folder=str(tmp_path / "dicom")),
        _tools(tmp_path),
        ["A001"],
    )
    unsubscribe()  # Equivalent to closing the browser/SSE connection.
    release.set()
    assert service.wait(2)

    events = service.events_since()
    assert [event["id"] for event in events] == list(range(1, len(events) + 1))
    json.dumps(events, ensure_ascii=False, allow_nan=False)
    snapshot = service.snapshot()
    assert snapshot["status"] == "completed"
    assert snapshot["progress"]["processed"] == 1
    assert observed


def test_service_persists_result_before_progress_event(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    persisted = []
    service = DcmGetAppService(
        task_store=store,
        task_ledger=TaskLedger(tmp_path / "task-ledger.sqlite3"),
        project_root=tmp_path,
        load_license_fn=lambda: object(),
        runner_factory=_CompletingRunner,
    )

    def observe(event):
        if event["type"] == "progress" and event["payload"]["final"]:
            persisted.append([item.accession for item in store.load_required().results])

    service.subscribe(observe)
    service.start_task(AppConfig(), _tools(tmp_path), ["A001"])
    assert service.wait(2)
    assert persisted == [["A001"]]


def test_pause_resume_cancel_are_forwarded_and_shutdown_drains_worker(tmp_path):
    started = threading.Event()
    release = threading.Event()

    class Runner(_CompletingRunner):
        def run(self, _accessions):
            started.set()
            release.wait(2)
            return BatchSummary(cancelled=self.cancelled)

        def request_cancel(self):
            super().request_cancel()
            release.set()

    service = _service(tmp_path, runner_factory=Runner)
    service.start_task(AppConfig(), _tools(tmp_path), ["A001"])
    assert started.wait(2)
    paused = service.pause()
    assert Runner.instances[-1].paused
    assert paused["status"] == "pause_pending"
    assert paused["message"] == "当前检查号完成后暂停"
    service.resume()
    assert not Runner.instances[-1].paused
    service.shutdown(timeout=2)
    assert Runner.instances[-1].cancelled
    assert service.snapshot()["status"] == "stopped"


def test_failed_task_can_retry_without_repeating_completed_accessions(tmp_path):
    runs = []

    class Runner(_CompletingRunner):
        def run(self, accessions):
            values = list(accessions)
            runs.append(values)
            results = []
            for index, accession in enumerate(values, 1):
                status = (
                    AccessionStatus.FAILED
                    if len(runs) == 1 and accession == "A002"
                    else AccessionStatus.COMPLETED
                )
                result = AccessionResult(accession, status)
                results.append(result)
                self.callbacks["progress_callback"](index, len(values), result)
            return BatchSummary(results)

    service = _service(tmp_path, runner_factory=Runner)
    service.start_task(AppConfig(), _tools(tmp_path), ["A001", "A002"])
    assert service.wait(2)
    assert service.snapshot()["status"] == "download_retryable"
    service.retry_failed(_tools(tmp_path))
    assert service.wait(2)
    assert runs == [["A001", "A002"], ["A002"]]
    assert service.snapshot()["status"] == "completed"


def test_restart_resumes_checkpoint_without_repeating_completed_accessions(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    checkpoint = store.start(
        config,
        ["DONE", "PENDING-1", "PENDING-2"],
        trial_required=False,
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "DONE",
            AccessionStatus.COMPLETED,
            file_count=1,
            received_bytes=1024,
            archived_files=[str(tmp_path / "DONE.dcm")],
        ),
    )
    resumed_accessions: list[list[str]] = []

    class Runner(_CompletingRunner):
        def run(self, accessions):
            resumed_accessions.append(list(accessions))
            return super().run(accessions)

    service = DcmGetAppService(
        task_store=store,
        task_ledger=TaskLedger(tmp_path / "task-ledger.sqlite3"),
        project_root=tmp_path,
        load_license_fn=lambda: object(),
        runner_factory=Runner,
    )

    restored = service.snapshot()
    assert restored["task"]["id"] == checkpoint.task_id
    assert restored["status"] == "interrupted"
    assert restored["progress"]["processed"] == 1

    service.resume_task(_tools(tmp_path))
    assert service.wait(2)

    assert resumed_accessions == [["PENDING-1", "PENDING-2"]]
    finished = service.snapshot()
    assert finished["status"] == "completed"
    assert finished["progress"]["processed"] == 3
    assert finished["progress"]["file_count"] == 3


def test_trial_is_consumed_only_when_runner_reports_ready(tmp_path):
    consumed = []
    service = DcmGetAppService(
        task_store=TaskCheckpointStore(tmp_path / "active-task.sqlite3"),
        task_ledger=TaskLedger(tmp_path / "task-ledger.sqlite3"),
        project_root=tmp_path,
        load_license_fn=lambda: (_ for _ in ()).throw(LicenseError("unlicensed")),
        trial_status_fn=lambda: TrialInfo(0, 30),
        consume_trial_fn=lambda **kwargs: consumed.append(kwargs["task_id"])
        or TrialInfo(1, 29),
        runner_factory=_CompletingRunner,
    )
    service.start_task(AppConfig(), _tools(tmp_path), ["A001"])
    assert service.wait(2)
    assert len(consumed) == 1
    assert consumed[0] == service.snapshot()["task"]["id"]


def test_pdi_runs_after_download_and_retry_keeps_checkpoint(tmp_path):
    exports = []

    class Exporter:
        def __init__(self, _config, _tools, **kwargs):
            self.kwargs = kwargs

        def request_cancel(self):
            pass

        def export(self, files):
            exports.append(list(files))
            status = PdiStatus.FAILED if len(exports) == 1 else PdiStatus.COMPLETED
            return PdiExportResult(
                status,
                output_directory=str(tmp_path / "PDI") if len(exports) > 1 else "",
                message="result",
            )

    service = _service(
        tmp_path,
        runner_factory=_CompletingRunner,
        pdi_exporter_factory=Exporter,
    )
    config = AppConfig(
        dicom_destination_folder=str(tmp_path / "dicom"),
        pdi_export_enabled=True,
        pdi_institution_name="测试医院",
    )
    service.start_task(config, _tools(tmp_path), ["A001"])
    assert service.wait(2)
    assert service.snapshot()["status"] == "pdi_retryable"
    assert service.snapshot()["actions"]["can_retry_pdi"]
    service.retry_pdi(_tools(tmp_path))
    assert service.wait(2)
    assert service.snapshot()["status"] == "completed"
    assert exports == [["/A001.dcm"], ["/A001.dcm"]]


def test_large_task_snapshot_is_aggregated_and_does_not_return_accession_list(
    tmp_path,
):
    blocker = threading.Event()

    class Runner(_CompletingRunner):
        def run(self, _accessions):
            blocker.wait(2)
            return BatchSummary(cancelled=True)

        def request_cancel(self):
            blocker.set()

    service = _service(tmp_path, runner_factory=Runner, detail_limit=200)
    service.start_task(
        AppConfig(),
        _tools(tmp_path),
        [f"A{value:05d}" for value in range(201)],
    )
    snapshot = service.snapshot()
    assert snapshot["task"]["large_batch"] is True
    assert snapshot["task"]["accessions"] is None
    assert snapshot["results"] is None
    assert snapshot["progress"]["total"] == 201
    service.shutdown(timeout=2)


def test_new_task_is_rejected_when_trial_is_exhausted(tmp_path):
    service = DcmGetAppService(
        task_store=TaskCheckpointStore(tmp_path / "active-task.sqlite3"),
        task_ledger=TaskLedger(tmp_path / "task-ledger.sqlite3"),
        load_license_fn=lambda: (_ for _ in ()).throw(LicenseError("unlicensed")),
        trial_status_fn=lambda: TrialInfo(30, 0),
    )
    with pytest.raises(AppServiceError, match="试用已用完"):
        service.start_task(AppConfig(), _tools(tmp_path), ["A001"])


def test_accept_partial_closes_retry_checkpoint_without_redownloading(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    config = AppConfig(dicom_destination_folder=str(tmp_path / "dicom"))
    checkpoint = store.start(config, ["A001", "A002"], trial_required=False)
    store.record_result(
        checkpoint.task_id,
        AccessionResult("A001", AccessionStatus.COMPLETED),
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult("A002", AccessionStatus.FAILED),
    )
    store.set_phase(checkpoint.task_id, "download_retryable")
    service = DcmGetAppService(
        task_store=store,
        task_ledger=TaskLedger(tmp_path / "task-ledger.sqlite3"),
        project_root=tmp_path,
        load_license_fn=lambda: object(),
    )

    assert service.snapshot()["actions"]["can_accept_partial"]
    service.accept_partial()

    assert not store.path.exists()
    assert service.snapshot()["status"] == "completed"


def test_verify_pdi_runs_without_qt_and_publishes_json_safe_result(
    tmp_path, monkeypatch
):
    import dcmget.pdi_verify as verify_module

    root = tmp_path / "PDI"
    root.mkdir()
    report = SimpleNamespace(json_path=tmp_path / "report.json", html_path=tmp_path / "report.html")
    monkeypatch.setattr(
        verify_module, "discover_pdi_verification_roots", lambda _root: (root,)
    )
    monkeypatch.setattr(
        verify_module,
        "pdi_delivery_report_output_directory",
        lambda *_args: tmp_path / "reports",
    )
    monkeypatch.setattr(
        verify_module, "write_pdi_delivery_reports", lambda *_args: report
    )

    class Verifier:
        def __init__(self, _root, *, progress_callback, cancel_event):
            self.progress_callback = progress_callback
            self.cancel_event = cancel_event

        def cancel(self):
            self.cancel_event.set()

        def verify(self):
            self.progress_callback(SimpleNamespace(current=1, total=1, message="ok"))
            return SimpleNamespace(status=SimpleNamespace(value="passed"), message="ok")

    service = _service(tmp_path, pdi_verifier_factory=Verifier)
    service.verify_pdi(root)
    assert service.wait(2)

    snapshot = service.snapshot()
    assert snapshot["status"] == "verification_completed"
    json.dumps(snapshot, ensure_ascii=False)
    assert any(
        event["type"] == "verification_progress"
        for event in service.events_since()
    )


def test_cancel_during_runner_construction_is_not_lost(tmp_path):
    constructor_entered = threading.Event()
    allow_constructor = threading.Event()

    class Runner(_CompletingRunner):
        def __init__(self, *args, **kwargs):
            constructor_entered.set()
            assert allow_constructor.wait(2)
            super().__init__(*args, **kwargs)

        def run(self, _accessions):
            return BatchSummary(cancelled=self.cancelled)

    service = _service(tmp_path, runner_factory=Runner)
    service.start_task(
        AppConfig(dicom_destination_folder=str(tmp_path / "dicom")),
        _tools(tmp_path),
        ["A001"],
    )
    assert constructor_entered.wait(2)
    service.cancel()
    allow_constructor.set()
    assert service.wait(2)
    assert Runner.instances[-1].cancelled
    assert service.snapshot()["status"] == "cancelled"
