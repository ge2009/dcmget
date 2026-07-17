from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydicom import dcmread
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    JPEG2000Lossless,
    PYDICOM_IMPLEMENTATION_UID,
    generate_uid,
)
from pynetdicom import AE

from dcmget.storage_scp import PynetdicomStorageSCP


def _dataset(
    accession: str,
    study_uid: str,
    *,
    sop_uid: str | None = None,
    transfer_syntax: str = ExplicitVRLittleEndian,
) -> FileDataset:
    instance_uid = sop_uid or generate_uid()
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = instance_uid
    file_meta.TransferSyntaxUID = transfer_syntax
    file_meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID
    dataset = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = instance_uid
    dataset.StudyInstanceUID = study_uid
    dataset.AccessionNumber = accession
    dataset.PatientName = "Receiver^Test"
    if dataset.file_meta.TransferSyntaxUID.is_compressed:
        dataset.SamplesPerPixel = 1
        dataset.PhotometricInterpretation = "MONOCHROME2"
        dataset.Rows = 1
        dataset.Columns = 1
        dataset.BitsAllocated = 8
        dataset.BitsStored = 8
        dataset.HighBit = 7
        dataset.PixelRepresentation = 0
        dataset.PixelData = encapsulate([b"\xff\x4f\xff\xd9"])
        dataset["PixelData"].is_undefined_length = True
        dataset.set_original_encoding(False, True)
    return dataset


def _send(port: int, dataset: FileDataset) -> int:
    ae = AE(ae_title="TESTPACS")
    ae.add_requested_context(
        CTImageStorage,
        dataset.file_meta.TransferSyntaxUID,
    )
    association = ae.associate("127.0.0.1", port, ae_title="DCMGET")
    assert association.is_established
    try:
        response = association.send_c_store(dataset)
        return int(response.Status)
    finally:
        association.release()


def test_shared_scp_accepts_two_concurrent_associations_and_routes_exactly(
    tmp_path: Path,
):
    barrier = threading.Barrier(2)
    active_lock = threading.Lock()
    active = 0
    peak = 0

    class ConcurrentProbe(PynetdicomStorageSCP):
        def _handle_store(self, event: object) -> int:
            nonlocal active, peak
            with active_lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=5)
                return super()._handle_store(event)
            finally:
                with active_lock:
                    active -= 1

    receiver = ConcurrentProbe(
        "DCMGET",
        0,
        bind_address="127.0.0.1",
        quarantine_directory=tmp_path / "quarantine",
    )
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    receiver.register_route("A001", first_dir)
    receiver.register_route("A002", second_dir)
    receiver.start()
    first = _dataset("A001", generate_uid())
    second = _dataset("A002", generate_uid())
    try:
        assert receiver.poll() is None
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(
                executor.map(
                    lambda value: _send(receiver.listening_port, value),
                    (first, second),
                )
            )
    finally:
        receiver.stop()

    assert statuses == [0x0000, 0x0000]
    assert peak == 2
    assert (first_dir / f"{first.SOPInstanceUID}.dcm").is_file()
    assert (second_dir / f"{second.SOPInstanceUID}.dcm").is_file()
    assert not list(tmp_path.rglob("*.part"))
    assert receiver.poll() == 0


def test_bound_study_uid_routes_object_without_accession(tmp_path: Path):
    receiver = PynetdicomStorageSCP(
        "DCMGET",
        0,
        bind_address="127.0.0.1",
        quarantine_directory=tmp_path / "quarantine",
    )
    destination = tmp_path / "study"
    receiver.register_route("A001", destination)
    study_uid = generate_uid()
    first = _dataset("A001", study_uid)
    second = _dataset("", study_uid)
    receiver.start()
    try:
        assert _send(receiver.listening_port, first) == 0x0000
        assert _send(receiver.listening_port, second) == 0x0000
    finally:
        receiver.stop()

    assert (destination / f"{first.SOPInstanceUID}.dcm").is_file()
    assert (destination / f"{second.SOPInstanceUID}.dcm").is_file()
    assert not list((tmp_path / "quarantine").glob("*.dcm"))


def test_single_route_safely_binds_first_tagless_accession_by_study_uid(
    tmp_path: Path,
):
    receiver = PynetdicomStorageSCP(
        "DCMGET",
        0,
        bind_address="127.0.0.1",
        quarantine_directory=tmp_path / "quarantine",
        allow_single_route_fallback=True,
    )
    destination = tmp_path / "single"
    receiver.register_route("A001", destination)
    dataset = _dataset("", generate_uid())
    receiver.start()
    try:
        assert _send(receiver.listening_port, dataset) == 0x0000
    finally:
        receiver.stop()

    assert (destination / f"{dataset.SOPInstanceUID}.dcm").is_file()


def test_default_concurrent_mode_never_guesses_a_single_remaining_route(
    tmp_path: Path,
):
    quarantine = tmp_path / "quarantine"
    receiver = PynetdicomStorageSCP(
        "DCMGET",
        0,
        bind_address="127.0.0.1",
        quarantine_directory=quarantine,
    )
    destination = tmp_path / "only-route"
    receiver.register_route("A001", destination)
    dataset = _dataset("", generate_uid())
    receiver.start()
    try:
        assert _send(receiver.listening_port, dataset) == 0xC000
    finally:
        receiver.stop()

    assert not list(destination.glob("*.dcm"))
    assert len(list(quarantine.glob("*.dcm"))) == 1


def test_two_routes_never_guess_tagless_accession_by_unbound_study_uid(
    tmp_path: Path,
):
    quarantine = tmp_path / "quarantine"
    receiver = PynetdicomStorageSCP(
        "DCMGET",
        0,
        bind_address="127.0.0.1",
        quarantine_directory=quarantine,
        allow_single_route_fallback=True,
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    receiver.register_route("A001", first)
    receiver.register_route("A002", second)
    dataset = _dataset("", generate_uid())
    receiver.start()
    try:
        assert _send(receiver.listening_port, dataset) == 0xC000
    finally:
        receiver.stop()

    assert not list(first.glob("*.dcm"))
    assert not list(second.glob("*.dcm"))
    assert len(list(quarantine.glob("*.dcm"))) == 1


def test_unassigned_object_is_quarantined_and_reported_failed(tmp_path: Path):
    messages: list[tuple[str, str, str]] = []
    quarantine = tmp_path / "quarantine"
    receiver = PynetdicomStorageSCP(
        "DCMGET",
        0,
        bind_address="127.0.0.1",
        quarantine_directory=quarantine,
        log_callback=lambda source, message, level: messages.append(
            (source, message, level)
        ),
    )
    dataset = _dataset("UNKNOWN", generate_uid())
    receiver.start()
    try:
        assert _send(receiver.listening_port, dataset) == 0xC000
    finally:
        receiver.stop()

    quarantined = list(quarantine.glob("*.dcm"))
    assert len(quarantined) == 1
    assert dcmread(quarantined[0]).SOPInstanceUID == dataset.SOPInstanceUID
    assert any(
        level == "warning" and "无法归属" in message
        for _, message, level in messages
    )


def test_compressed_transfer_syntax_is_negotiated_and_preserved(tmp_path: Path):
    receiver = PynetdicomStorageSCP(
        "DCMGET",
        0,
        bind_address="127.0.0.1",
        quarantine_directory=tmp_path / "quarantine",
    )
    destination = tmp_path / "compressed"
    receiver.register_route("A001", destination)
    dataset = _dataset(
        "A001",
        generate_uid(),
        transfer_syntax=JPEG2000Lossless,
    )
    receiver.start()
    try:
        assert _send(receiver.listening_port, dataset) == 0x0000
    finally:
        receiver.stop()

    stored = dcmread(destination / f"{dataset.SOPInstanceUID}.dcm")
    assert stored.file_meta.TransferSyntaxUID == JPEG2000Lossless
    assert stored.PixelData == dataset.PixelData


def test_duplicate_accession_and_conflicting_study_bindings_are_rejected(
    tmp_path: Path,
):
    receiver = PynetdicomStorageSCP(
        "DCMGET",
        0,
        quarantine_directory=tmp_path / "quarantine",
    )
    first = receiver.register_route("A001", tmp_path / "first")
    second = receiver.register_route("A002", tmp_path / "second")
    study_uid = generate_uid()
    receiver.bind_study_uid(first, study_uid)

    try:
        receiver.register_route("A001", tmp_path / "duplicate")
    except ValueError as exc:
        assert "已注册" in str(exc)
    else:
        raise AssertionError("duplicate accession route was accepted")

    try:
        receiver.bind_study_uid(second, study_uid)
    except ValueError as exc:
        assert "另一个" in str(exc)
    else:
        raise AssertionError("conflicting Study Instance UID was accepted")
