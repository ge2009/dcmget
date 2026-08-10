#!/usr/bin/env python3
"""End-to-end synthetic PACS gate for the native Rust CLI.

The gate starts a real pynetdicom Study Root C-MOVE SCP.  The release CLI must
open its native Storage SCP, request one accession, receive two CT objects, and
publish them using the configured directory template.  No dcmget internal API
is mocked and all identifiers are synthetic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pydicom
import pynetdicom
from pydicom import dcmread
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, PYDICOM_IMPLEMENTATION_UID
from pynetdicom import AE, evt
from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelMove

PYDICOM_VERSION = "3.0.2"
PYNETDICOM_VERSION = "3.0.4"
PATIENT_ID = "SYNTHPAT001"
ACCESSION = "SYNTHACC001"
STUDY_UID = "1.2.826.0.1.3680043.10.543.20260810.1"
SERIES_UID = "1.2.826.0.1.3680043.10.543.20260810.2"
SOP_UIDS = (
    "1.2.826.0.1.3680043.10.543.20260810.3.1",
    "1.2.826.0.1.3680043.10.543.20260810.3.2",
)


class GateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExpectedObject:
    sop_uid: str
    pixel_sha256: str


def reserve_tcp_port() -> socket.socket:
    """Keep a loopback port reserved until the native receiver is launched."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener


def port_bind_race(stderr: str) -> bool:
    normalized = stderr.casefold()
    return any(
        marker in normalized
        for marker in (
            "address already in use",
            "os error 48",
            "os error 98",
            "os error 10048",
            "端口已被占用",
            "端口被占用",
        )
    )


def make_ct(index: int, sop_uid: str) -> tuple[FileDataset, ExpectedObject]:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID

    pixels = bytes((index * 17 + offset) % 256 for offset in range(64))
    dataset = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SpecificCharacterSet = "ISO_IR 192"
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = sop_uid
    dataset.PatientID = PATIENT_ID
    dataset.PatientName = "Synthetic^Patient"
    dataset.AccessionNumber = ACCESSION
    dataset.StudyInstanceUID = STUDY_UID
    dataset.SeriesInstanceUID = SERIES_UID
    dataset.Modality = "CT"
    dataset.InstanceNumber = index
    dataset.Rows = 8
    dataset.Columns = 8
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.PixelData = pixels
    return dataset, ExpectedObject(sop_uid, hashlib.sha256(pixels).hexdigest())


def start_pacs(
    pacs_port: int,
    storage_endpoint: dict[str, int],
    datasets: list[FileDataset],
    observed: list[dict[str, str]],
):
    ae = AE(ae_title="PACS_MOCK")
    ae.add_supported_context(StudyRootQueryRetrieveInformationModelMove)
    ae.add_requested_context(CTImageStorage, ExplicitVRLittleEndian)

    def handle_move(event) -> Iterator[object]:
        identifier = event.identifier
        requested = str(getattr(identifier, "AccessionNumber", "")).strip()
        destination = str(event.move_destination).strip()
        observed.append({"accession": requested, "destination_ae": destination})
        if requested != ACCESSION or destination != "DCMGET":
            yield (None, None)
            return
        yield ("127.0.0.1", storage_endpoint["port"])
        yield len(datasets)
        for dataset in datasets:
            yield (0xFF00, dataset)

    return ae.start_server(
        ("127.0.0.1", pacs_port),
        block=False,
        evt_handlers=[(evt.EVT_C_MOVE, handle_move)],
    )


def verify_received(destination: Path, expected: list[ExpectedObject]) -> list[Path]:
    files = sorted(destination.rglob("*.dcm"))
    expected_uids = {item.sop_uid for item in expected}
    if len(files) != len(expected):
        raise GateError(f"expected {len(expected)} DICOM files, received {len(files)}")
    if list(destination.rglob("*.part")):
        raise GateError("temporary .part files remained after the CLI exited")
    if list(destination.rglob("*.dcm.quarantine")):
        raise GateError("synthetic valid objects were unexpectedly quarantined")

    expected_by_uid = {item.sop_uid: item for item in expected}
    received_uids: set[str] = set()
    for path in files:
        raw = path.read_bytes()
        if len(raw) < 132 or raw[128:132] != b"DICM":
            raise GateError(f"missing Part 10 DICM marker: {path.name}")
        dataset = dcmread(path)
        sop_uid = str(dataset.SOPInstanceUID)
        item = expected_by_uid.get(sop_uid)
        if item is None:
            raise GateError(f"unexpected SOP Instance UID: {sop_uid}")
        expected_parent = Path(PATIENT_ID) / ACCESSION / STUDY_UID
        if path.parent.relative_to(destination) != expected_parent:
            raise GateError(f"directory template mismatch: {path.relative_to(destination)}")
        if path.name != f"{sop_uid}.dcm":
            raise GateError(f"SOP filename mismatch: {path.name}")
        if str(dataset.PatientID) != PATIENT_ID:
            raise GateError("PatientID changed during receive")
        if str(dataset.AccessionNumber) != ACCESSION:
            raise GateError("AccessionNumber changed during receive")
        if str(dataset.StudyInstanceUID) != STUDY_UID:
            raise GateError("StudyInstanceUID changed during receive")
        if hashlib.sha256(bytes(dataset.PixelData)).hexdigest() != item.pixel_sha256:
            raise GateError(f"PixelData changed for SOP {sop_uid}")
        received_uids.add(sop_uid)
    if received_uids != expected_uids:
        raise GateError("received SOP Instance UID set is incomplete")
    return files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True, type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if sys.version_info[:2] != (3, 12):
        raise GateError(f"Python 3.12 is required, found {sys.version.split()[0]}")
    if pydicom.__version__ != PYDICOM_VERSION:
        raise GateError(f"pydicom {PYDICOM_VERSION} is required")
    if pynetdicom.__version__ != PYNETDICOM_VERSION:
        raise GateError(f"pynetdicom {PYNETDICOM_VERSION} is required")
    cli = args.cli.resolve()
    if not cli.is_file():
        raise GateError(f"native CLI not found: {cli}")

    work_root = (args.work_root or Path(tempfile.mkdtemp(prefix="dcmget-native-pacs-"))).resolve()
    destination = work_root / "download"
    logs = work_root / "logs"
    destination.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    pairs = [make_ct(index, uid) for index, uid in enumerate(SOP_UIDS, start=1)]
    datasets = [pair[0] for pair in pairs]
    expected = [pair[1] for pair in pairs]
    config_path = work_root / "config.json"
    accessions_path = work_root / "accessions.txt"
    accessions_path.write_text(f"{ACCESSION}\n", encoding="utf-8")

    observed: list[dict[str, str]] = []
    storage_endpoint = {"port": 0}
    server = start_pacs(0, storage_endpoint, datasets, observed)
    pacs_port = int(server.server_address[1])
    stdout_path = logs / "cli.stdout.log"
    stderr_path = logs / "cli.stderr.log"
    report_path = logs / "gate-report.json"
    report: dict[str, object] = {"ok": False, "received": 0, "observed": observed}
    try:
        time.sleep(0.2)
        completed: subprocess.CompletedProcess[str] | None = None
        for attempt in range(1, 4):
            reservation = reserve_tcp_port()
            storage_port = int(reservation.getsockname()[1])
            storage_endpoint["port"] = storage_port
            config = {
                "config_version": 8,
                "dicom_destination_folder": str(destination),
                "pacs_server_ip": "127.0.0.1",
                "pacs_server_port": pacs_port,
                "calling_ae_title": "DCMGET",
                "pacs_ae_title": "PACS_MOCK",
                "storage_ae_title": "DCMGET",
                "storage_port": storage_port,
                "directory_template": "{PatientID}/{AccessionNumber}/{StudyInstanceUID}",
                "minimum_free_space_bytes": 0,
                "pdi_export_enabled": False,
                "anonymization_enabled": False,
            }
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
            observed_before = len(observed)
            reservation.close()
            completed = subprocess.run(
                [
                    str(cli),
                    "download",
                    "--config",
                    str(config_path),
                    "--accessions",
                    str(accessions_path),
                    "--destination",
                    str(destination),
                ],
                cwd=work_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=args.timeout_seconds,
            )
            stdout_path.write_text(completed.stdout, encoding="utf-8")
            stderr_path.write_text(completed.stderr, encoding="utf-8")
            if completed.returncode == 0:
                break
            if (
                attempt == 3
                or len(observed) != observed_before
                or not port_bind_race(completed.stderr)
            ):
                break
            time.sleep(0.1 * attempt)
        if completed is None:
            raise GateError("native CLI was not launched")
        if completed.returncode != 0:
            raise GateError(f"native CLI exited with code {completed.returncode}")
        files = verify_received(destination, expected)
        if observed != [{"accession": ACCESSION, "destination_ae": "DCMGET"}]:
            raise GateError(f"unexpected C-MOVE request sequence: {observed!r}")
        report.update(
            {
                "ok": True,
                "received": len(files),
                "sop_instance_uids": sorted(item.sop_uid for item in expected),
            }
        )
        print(f"native PACS gate passed: {len(files)} synthetic CT objects")
        return 0
    finally:
        server.shutdown()
        server.server_close()
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GateError, subprocess.TimeoutExpired) as error:
        print(f"native PACS gate failed: {error}", file=sys.stderr)
        raise SystemExit(1)
