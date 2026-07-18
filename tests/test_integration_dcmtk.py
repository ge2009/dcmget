from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import socket
import threading
import time
from pathlib import Path

import pytest
from pydicom import dcmread
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid
from pynetdicom import AE, evt
from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelMove

from dcmget.config import AppConfig
from dcmget import core
from dcmget.core import (
    AccessionStatus,
    DcmtkResolver,
    DownloadRunner,
    build_storescp_command,
    is_port_available,
)
from dcmget.pdi import PdiExporter, PdiStatus, PdiVolumeExporter
from dcmget.multitask_runtime import SharedDcmtkRuntime
from dcmget.task_manager import ReceiverService, TaskCatalog, TaskManager


def sample_dataset(accession: str, instance_uid: str | None = None) -> FileDataset:
    instance_uid = instance_uid or generate_uid()
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(
        None,
        {},
        file_meta=file_meta,
        preamble=b"\0" * 128,
        is_implicit_VR=False,
        is_little_endian=True,
    )
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = instance_uid
    dataset.StudyInstanceUID = generate_uid()
    dataset.SeriesInstanceUID = generate_uid()
    dataset.PatientName = "DcmGet^Integration"
    dataset.PatientID = "DGM001"
    dataset.AccessionNumber = accession
    dataset.Modality = "CT"
    dataset.StudyDate = "20260716"
    dataset.StudyTime = "090000"
    dataset.StudyID = "STUDY001"
    dataset.SeriesNumber = 1
    dataset.InstanceNumber = 1
    dataset.Rows = 1
    dataset.Columns = 1
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.PixelData = b"\0"
    return dataset


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.mark.integration
def test_real_storescp_movescu_cstore_round_trip(tmp_path):
    try:
        tools = DcmtkResolver(Path(__file__).resolve().parents[1]).resolve()
    except FileNotFoundError:
        pytest.skip("本机未安装 movescu/storescp")

    pacs_port = unused_port()
    storage_port = unused_port()
    accession = "INTEGRATION001"
    dataset = sample_dataset(accession)
    pacs = AE(ae_title="PACS")
    pacs.add_supported_context(StudyRootQueryRetrieveInformationModelMove)
    pacs.add_requested_context(CTImageStorage, ExplicitVRLittleEndian)

    def handle_move(_event):
        yield "127.0.0.1", storage_port
        yield 1
        yield 0xFF00, dataset

    server = pacs.start_server(
        ("127.0.0.1", pacs_port),
        block=False,
        evt_handlers=[(evt.EVT_C_MOVE, handle_move)],
    )
    config = AppConfig(
        dicom_destination_folder=str(tmp_path / "dicom"),
        pacs_server_ip="127.0.0.1",
        pacs_server_port=pacs_port,
        calling_ae_title="MOVESCU",
        pacs_ae_title="PACS",
        storage_ae_title="STORESCP",
        storage_port=storage_port,
    )
    try:
        time.sleep(0.1)
        summary = DownloadRunner(config, tools).run([accession])
    finally:
        server.shutdown()

    assert summary.exit_code == 0
    assert summary.results[0].status == AccessionStatus.COMPLETED
    received = list((tmp_path / "dicom").rglob("*.dcm"))
    assert received
    assert "DGM001" in received[0].parts
    assert accession in received[0].parts
    assert str(dataset.StudyInstanceUID) in received[0].parts
    assert received[0].read_bytes()[128:132] == b"DICM"


@pytest.mark.integration
def test_real_multitask_shares_one_scp_and_runs_multiple_scps_concurrently(
    tmp_path, monkeypatch
):
    try:
        tools = DcmtkResolver(Path(__file__).resolve().parents[1]).resolve()
    except FileNotFoundError:
        pytest.skip("本机未安装 movescu/storescp")

    from dcmget import multitask_runtime

    monkeypatch.setattr(
        multitask_runtime,
        "ensure_application_state_dir",
        lambda: tmp_path / "state",
    )
    pacs_port = unused_port()
    storage_port_a = unused_port()
    storage_port_b = unused_port()
    receiver_ports = {
        "STORE_A": storage_port_a,
        "STORE_B": storage_port_b,
    }
    pacs = AE(ae_title="PACS")
    pacs.add_supported_context(StudyRootQueryRetrieveInformationModelMove)
    pacs.add_requested_context(CTImageStorage, ExplicitVRLittleEndian)
    move_lock = threading.Lock()
    all_moves_ready = threading.Event()
    release_first_a001 = threading.Event()
    release_other_moves = threading.Event()
    duplicate_a001_ready = threading.Event()
    release_duplicate_a001 = threading.Event()
    a001_calls = 0
    active_handlers = 0
    peak_handlers = 0
    active_by_destination = {"STORE_A": 0, "STORE_B": 0}
    peak_by_destination = {"STORE_A": 0, "STORE_B": 0}

    def handle_move(event):
        nonlocal a001_calls, active_handlers, peak_handlers
        accession = str(event.identifier.AccessionNumber)
        destination = str(event.move_destination)
        yield "127.0.0.1", receiver_ports[destination]
        yield 1
        with move_lock:
            a001_call_number = 0
            if accession == "A001":
                a001_calls += 1
                a001_call_number = a001_calls
            active_handlers += 1
            peak_handlers = max(peak_handlers, active_handlers)
            active_by_destination[destination] += 1
            peak_by_destination[destination] = max(
                peak_by_destination[destination],
                active_by_destination[destination],
            )
            if active_handlers >= 3:
                all_moves_ready.set()
        try:
            if accession == "A001" and a001_call_number == 1:
                assert release_first_a001.wait(5)
            elif accession != "A001":
                assert release_other_moves.wait(5)
            else:
                assert a001_call_number == 2
                duplicate_a001_ready.set()
                assert release_duplicate_a001.wait(5)
            yield 0xFF00, sample_dataset(accession)
        finally:
            with move_lock:
                active_handlers -= 1
                active_by_destination[destination] -= 1

    server = pacs.start_server(
        ("127.0.0.1", pacs_port),
        block=False,
        evt_handlers=[(evt.EVT_C_MOVE, handle_move)],
    )
    config_a = AppConfig(
        dicom_destination_folder=str(tmp_path / "dicom-a"),
        pacs_server_ip="127.0.0.1",
        pacs_server_port=pacs_port,
        calling_ae_title="MOVESCU",
        pacs_ae_title="PACS",
        storage_ae_title="STORE_A",
        storage_port=storage_port_a,
    )
    config_a_second = AppConfig.from_dict(config_a.to_dict())
    config_a_second.dicom_destination_folder = str(tmp_path / "dicom-a-second")
    config_a_duplicate = AppConfig.from_dict(config_a.to_dict())
    config_a_duplicate.dicom_destination_folder = str(
        tmp_path / "dicom-a-duplicate"
    )
    config_b = AppConfig(
        dicom_destination_folder=str(tmp_path / "dicom-b"),
        pacs_server_ip="127.0.0.1",
        pacs_server_port=pacs_port,
        calling_ae_title="MOVESCU",
        pacs_ae_title="PACS",
        storage_ae_title="STORE_B",
        storage_port=storage_port_b,
    )
    active = {"movescu": 0}
    peak = {"movescu": 0}
    starts = {"movescu": 0}
    process_lock = threading.Lock()

    def process_event(_task_id, kind, _pid, _executable, is_active):
        if kind not in active:
            return
        with process_lock:
            active[kind] += 1 if is_active else -1
            if is_active:
                starts[kind] += 1
                peak[kind] = max(peak[kind], active[kind])

    runtime = SharedDcmtkRuntime(
        config_a,
        tools,
        process_callback=process_event,
    )
    order = []
    handle_ids: set[int] = set()
    staging_roots: dict[tuple[str, int], Path] = {}
    event_lock = threading.Lock()
    route_directories: list[tuple[str, Path]] = []
    instrumented_receivers: set[int] = set()
    original_ensure_receiver = runtime._ensure_receiver

    def ensure_receiver(task_config):
        receiver_handle = original_ensure_receiver(task_config)
        receiver_id = id(receiver_handle.receiver)
        with event_lock:
            handle_ids.add(receiver_id)
            staging_roots[receiver_handle.key] = receiver_handle.staging_directory
            if receiver_id in instrumented_receivers:
                return receiver_handle
            instrumented_receivers.add(receiver_id)
            original_register_route = receiver_handle.receiver.register_route

            def register_route(accession, directory, **kwargs):
                with event_lock:
                    route_directories.append((accession, Path(directory)))
                return original_register_route(accession, directory, **kwargs)

            receiver_handle.receiver.register_route = register_route
        return receiver_handle

    runtime._ensure_receiver = ensure_receiver

    def execute(
        handle,
        task_id,
        task_config,
        accession,
        move_started,
        cancel_event,
    ):
        with event_lock:
            order.append((task_id, accession))
        return runtime.run_accession(
            handle,
            task_id,
            task_config,
            accession,
            move_started,
            cancel_event,
        )

    receiver = ReceiverService(
        runtime.start,
        runtime.stop,
        execute,
        max_concurrent_moves=4,
    )
    receiver.ensure_started()
    catalog = TaskCatalog(
        tmp_path / "tasks.sqlite3",
        legacy_path=tmp_path / "unused.sqlite3",
        auto_migrate=False,
    )
    manager = TaskManager(catalog, receiver=receiver, max_concurrent_moves=4)
    first = manager.create_task(config_a, ["A001"])
    same_key = manager.create_task(config_a_second, ["A002"])
    second_receiver = manager.create_task(config_b, ["B001"])
    duplicate_a001 = manager.create_task(config_a_duplicate, ["A001"])
    worker_errors: list[Exception] = []

    def schedule_all_tasks():
        while any(
            item.phase in {"queued", "running"} for item in manager.list_tasks()
        ):
            try:
                manager.run_next_round()
            except Exception as exc:
                worker_errors.append(exc)
                return

    worker = threading.Thread(target=schedule_all_tasks)
    try:
        worker.start()
        assert all_moves_ready.wait(5)
        with process_lock:
            assert active["movescu"] == 3
            assert peak["movescu"] == 3
        assert not duplicate_a001_ready.is_set()
        assert manager.get_task(duplicate_a001.task_id).summary.phase == "queued"

        release_first_a001.set()
        assert duplicate_a001_ready.wait(10)
        assert not release_other_moves.is_set()
        with process_lock:
            assert active["movescu"] == 3
            assert peak["movescu"] == 3

        release_other_moves.set()
        release_duplicate_a001.set()
        worker.join(10)
        assert not worker.is_alive()
        assert worker_errors == []
        deadline = time.monotonic() + 15
        while any(
            item.phase in {"queued", "running"} for item in manager.list_tasks()
        ):
            assert time.monotonic() < deadline
            manager.run_next_round()
    finally:
        release_first_a001.set()
        release_other_moves.set()
        release_duplicate_a001.set()
        manager.shutdown()
        server.shutdown()

    assert set(order[:3]) == {
        (first.task_id, "A001"),
        (same_key.task_id, "A002"),
        (second_receiver.task_id, "B001"),
    }
    assert order[-1] == (duplicate_a001.task_id, "A001")
    assert starts == {"movescu": 4}
    assert peak == {"movescu": 3}
    assert active == {"movescu": 0}
    assert peak_handlers == 3
    assert peak_by_destination == {"STORE_A": 2, "STORE_B": 1}
    assert len(handle_ids) == 2
    assert set(staging_roots) == {
        ("STORE_A", storage_port_a),
        ("STORE_B", storage_port_b),
    }
    assert len(set(staging_roots.values())) == 2
    assert all(not path.exists() for path in staging_roots.values())
    assert sorted(accession for accession, _directory in route_directories) == [
        "A001",
        "A001",
        "A002",
        "B001",
    ]
    assert len({directory for _accession, directory in route_directories}) == 4
    task_details = [
        manager.get_task_detail(task.task_id)
        for task in (first, same_key, second_receiver, duplicate_a001)
    ]
    task_diagnostics = [
        {
            "task_id": detail.task_id,
            "phase": detail.summary.phase,
            "error": detail.summary.error_message,
            "results": [
                {
                    "accession": result.accession,
                    "status": result.status.value,
                    "message": result.message,
                    "files": result.file_count,
                }
                for result in detail.results
            ],
        }
        for detail in task_details
    ]
    assert [detail.summary.phase for detail in task_details] == [
        "completed",
        "completed",
        "completed",
        "completed",
    ], task_diagnostics
    received = [
        *list((tmp_path / "dicom-a").rglob("*.dcm")),
        *list((tmp_path / "dicom-a-second").rglob("*.dcm")),
        *list((tmp_path / "dicom-b").rglob("*.dcm")),
        *list((tmp_path / "dicom-a-duplicate").rglob("*.dcm")),
    ]
    assert len(received) == 4
    assert len(list((tmp_path / "dicom-a").rglob("*.dcm"))) == 1
    assert len(list((tmp_path / "dicom-a-second").rglob("*.dcm"))) == 1
    assert len(list((tmp_path / "dicom-b").rglob("*.dcm"))) == 1
    assert len(list((tmp_path / "dicom-a-duplicate").rglob("*.dcm"))) == 1
    assert {path.parent.parent.name for path in received} == {
        "A001",
        "A002",
        "B001",
    }
    assert all(path.read_bytes()[128:132] == b"DICM" for path in received)


@pytest.mark.integration
def test_real_storescp_accepts_parallel_associations(tmp_path):
    try:
        tools = DcmtkResolver(Path(__file__).resolve().parents[1]).resolve()
    except FileNotFoundError:
        pytest.skip("本机未安装 movescu/storescp")
    if not tools.supports_fork:
        pytest.skip("当前 storescp 不支持多进程 association 接收")

    storage_port = unused_port()
    config = AppConfig(
        dicom_destination_folder=str(tmp_path),
        storage_ae_title="STORESCP",
        storage_port=storage_port,
    )
    runner = DownloadRunner(config, tools)
    process = runner._popen(build_storescp_command(config, tools, tmp_path))
    reader = runner._start_reader(process, "storescp")

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and is_port_available(storage_port):
        if process.poll() is not None:
            pytest.fail(f"storescp exited with code {process.returncode}")
        time.sleep(0.05)

    shared_instance_uid = generate_uid()

    def send(index: int) -> int | None:
        ae = AE(ae_title=f"SCU{index}")
        ae.add_requested_context(CTImageStorage, ExplicitVRLittleEndian)
        association = ae.associate("127.0.0.1", storage_port, ae_title="STORESCP")
        if not association.is_established:
            return None
        status = association.send_c_store(
            sample_dataset(f"PARALLEL{index:03d}", shared_instance_uid)
        )
        association.release()
        return getattr(status, "Status", None)

    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(send, range(12)))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(list(tmp_path.glob("*.*"))) < 12:
            time.sleep(0.05)
    finally:
        core._terminate_process(process)
        reader.join(timeout=2)
        runner._close_file_logger()

    received = [path for path in tmp_path.glob("*.*") if path.is_file()]
    assert statuses == [0x0000] * 12
    assert len(received) == 12
    assert all(path.suffix == ".dcm" for path in received)
    assert all(path.read_bytes()[128:132] == b"DICM" for path in received)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not is_port_available(storage_port):
        time.sleep(0.05)
    assert is_port_available(storage_port)


@pytest.mark.integration
def test_real_dcmtk_builds_valid_pdi_dicomdir_and_ohif_index(tmp_path):
    try:
        tools = DcmtkResolver(Path(__file__).resolve().parents[1]).resolve()
    except FileNotFoundError:
        pytest.skip("本机未安装 DCMTK")
    if tools.dcmmkdir is None:
        pytest.skip("当前 DCMTK 缺少 dcmmkdir")

    source = tmp_path / "download" / "source.dcm"
    source.parent.mkdir()
    dataset = sample_dataset("PDI-INTEGRATION")
    dataset.save_as(source, enforce_file_format=True)
    source_digest = source.read_bytes()
    config = AppConfig(
        dicom_destination_folder=str(source.parent),
        pdi_export_enabled=True,
        pdi_institution_name="DcmGet Integration Hospital",
        pdi_output_folder=str(tmp_path / "portable"),
        pdi_include_ohif_viewer=False,
    )

    result = PdiExporter(config, tools).export([source])

    assert result.status == PdiStatus.COMPLETED
    output = Path(result.output_directory)
    assert source.read_bytes() == source_digest
    copied = [path for path in (output / "DICOM").rglob("*") if path.is_file()]
    assert len(copied) == 1 and copied[0].suffix == ""
    index = json.loads(
        (output / "VIEWER" / ".dcmget" / "index").read_text(encoding="utf-8")
    )
    instances = index["studies"][0]["series"][0]["instances"]
    assert len(instances) == 1
    assert instances[0]["url"].startswith("dicomweb:/DICOM/")
    assert not list(output.rglob("*.JPG"))
    directory = dcmread(output / "DICOMDIR")
    references = [
        record.ReferencedFileID
        for record in directory.DirectoryRecordSequence
        if "ReferencedFileID" in record
    ]
    assert len(references) == 1


@pytest.mark.integration
def test_real_dcmtk_builds_independent_complete_study_pdi_volumes(tmp_path):
    try:
        tools = DcmtkResolver(Path(__file__).resolve().parents[1]).resolve()
    except FileNotFoundError:
        pytest.skip("本机未安装 DCMTK")
    if tools.dcmmkdir is None:
        pytest.skip("当前 DCMTK 缺少 dcmmkdir")

    source_root = tmp_path / "download"
    source_root.mkdir()
    sources = []
    for number in (1, 2):
        source = source_root / f"study-{number}.dcm"
        dataset = sample_dataset(f"PDI-VOLUME-{number}")
        dataset.save_as(source, enforce_file_format=True)
        sources.append(source)
    # The configured limit applies to the complete portable volume, not only
    # to its source DICOM payload.  Match the exporter's safety reserves so
    # one Study fits while a second Study still starts a new volume.
    capacity = (
        16 * 1024 * 1024
        + 64 * 1024
        + max(path.stat().st_size for path in sources)
    )
    config = AppConfig(
        dicom_destination_folder=str(source_root),
        pdi_export_enabled=True,
        pdi_institution_name="DcmGet Integration Hospital",
        pdi_output_folder=str(tmp_path / "portable"),
        pdi_include_ohif_viewer=False,
        pdi_volume_size_bytes=capacity,
    )

    result = PdiVolumeExporter(config, tools).export(sources)

    assert result.status == PdiStatus.COMPLETED
    assert result.volume_count == 2
    output = Path(result.output_directory)
    assert (output / "VOLUME_SET.json").is_file()
    for number in (1, 2):
        volume = output / f"VOLUME_{number:03d}"
        assert (volume / "DICOMDIR").is_file()
        assert (volume / "MANIFEST.SHA256").is_file()
        references = [
            record.ReferencedFileID
            for record in dcmread(volume / "DICOMDIR").DirectoryRecordSequence
            if "ReferencedFileID" in record
        ]
        assert len(references) == 1
    metadata = (output / "VOLUME_SET.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in metadata
