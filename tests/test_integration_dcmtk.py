from __future__ import annotations

import time
import socket

import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid
from pynetdicom import AE, evt
from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelMove

from dcmget.config import AppConfig
from dcmget.core import AccessionStatus, DcmtkResolver, DownloadRunner


def sample_dataset(accession: str) -> FileDataset:
    instance_uid = generate_uid()
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = instance_uid
    dataset.StudyInstanceUID = generate_uid()
    dataset.SeriesInstanceUID = generate_uid()
    dataset.PatientName = "DcmGet^Integration"
    dataset.PatientID = "DGM001"
    dataset.AccessionNumber = accession
    dataset.Modality = "CT"
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
        tools = DcmtkResolver(tmp_path).resolve()
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
    received = list((tmp_path / "dicom" / accession).iterdir())
    assert received
    assert received[0].read_bytes()[128:132] == b"DICM"
