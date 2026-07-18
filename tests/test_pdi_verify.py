from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import (
    ExplicitVRLittleEndian,
    MediaStorageDirectoryStorage,
    PYDICOM_IMPLEMENTATION_UID,
    generate_uid,
)

from dcmget.pdi_verify import (
    MANIFEST_NAME,
    OHIF_MANIFEST_NAME,
    PdiVerificationStage,
    PdiVerificationStatus,
    PdiVerifier,
    STUDY_INDEX,
    discover_pdi_verification_roots,
    pdi_delivery_report_output_directory,
    verify_pdi_directory,
    write_pdi_delivery_reports,
)


DICOM_RELATIVE = "DICOM/P000001/S000001/I000001"


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    value.update(path.read_bytes())
    return value.hexdigest()


def _write_manifest(root: Path, name: str = MANIFEST_NAME) -> None:
    manifest = root / name
    lines = []
    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.as_posix()):
        if path.is_file() and path != manifest:
            lines.append(f"{_digest(path)}  {path.relative_to(root).as_posix()}")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_dicomdir(
    root: Path,
    reference: tuple[str, ...],
    *additional_references: tuple[str, ...],
) -> None:
    file_meta = FileMetaDataset()
    file_meta.FileMetaInformationVersion = b"\x00\x01"
    file_meta.MediaStorageSOPClassUID = MediaStorageDirectoryStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID
    dataset = FileDataset(
        str(root / "DICOMDIR"),
        {},
        file_meta=file_meta,
        preamble=b"\0" * 128,
    )
    dataset.SOPClassUID = file_meta.MediaStorageSOPClassUID
    dataset.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    dataset.FileSetID = "DCMGET"
    records = []
    for item in (reference, *additional_references):
        record = Dataset()
        record.DirectoryRecordType = "IMAGE"
        record.ReferencedFileID = list(item)
        records.append(record)
    dataset.DirectoryRecordSequence = Sequence(records)
    dataset.save_as(root / "DICOMDIR", enforce_file_format=True)


def _pdi(root: Path, *, viewer: bool = True, reference: tuple[str, ...] | None = None) -> Path:
    root.mkdir()
    target = root / DICOM_RELATIVE
    target.parent.mkdir(parents=True)
    target.write_bytes(b"dicom payload")
    _write_dicomdir(root, reference or tuple(DICOM_RELATIVE.split("/")))
    (root / "INDEX.HTM").write_text("<html>检查清单</html>\n", encoding="utf-8")
    (root / "README.TXT").write_text("DcmGet PDI\n", encoding="utf-8")

    if viewer:
        ohif = root / "VIEWER" / "OHIF"
        ohif.mkdir(parents=True)
        (ohif / "index.html").write_text("<html>OHIF</html>\n", encoding="utf-8")
        (ohif / "app-config.js").write_text("window.config = {};\n", encoding="utf-8")
        (ohif / "LICENSE-OHIF.txt").write_text("license\n", encoding="utf-8")
        (ohif / "THIRD_PARTY-OHIF.md").write_text("notices\n", encoding="utf-8")
        _write_manifest(ohif, OHIF_MANIFEST_NAME)
        (root / "VIEWER" / "pdi_server.py").write_text("# server\n", encoding="utf-8")
        (root / "VIEWER" / "architecture.py").write_text(
            "# architecture\n", encoding="utf-8"
        )
        index = root / STUDY_INDEX
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(
            json.dumps(
                {
                    "studies": [
                        {
                            "series": [
                                {
                                    "instances": [
                                        {"url": f"dicomweb:/{DICOM_RELATIVE}"}
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        for name in ("OPEN_VIEWER.bat", "OPEN_VIEWER.command", "OPEN_VIEWER.sh"):
            (root / name).write_text("launcher\n", encoding="utf-8")

    _write_manifest(root)
    return root


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_complete_pdi_passes_and_writes_reports_outside_media(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI")
    progress = []

    result = verify_pdi_directory(root, progress_callback=progress.append)
    reports = write_pdi_delivery_reports(result)

    assert result.status == PdiVerificationStatus.PASSED
    assert result.ok
    assert result.manifest_entries == result.verified_files
    assert result.dicomdir_references == 1
    assert result.indexed_instances == 1
    assert result.viewer_included
    assert {item.stage for item in progress} >= {
        PdiVerificationStage.MANIFEST,
        PdiVerificationStage.DICOMDIR,
        PdiVerificationStage.VIEWER,
        PdiVerificationStage.COMPLETE,
    }
    payload = json.loads(reports.json_path.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["status_label"] == "通过"
    assert "不证明患者信息已经完成匿名处理" in payload["verification_scope"]
    assert payload["statistics"]["dicomdir_references"] == 1
    html = reports.html_path.read_text(encoding="utf-8")
    assert "PDI 交付验收报告" in html and "验收结果：通过" in html
    assert reports.json_path.parent.parent == root.parent
    assert not reports.json_path.is_relative_to(root)
    assert PdiVerifier(root).verify().status == PdiVerificationStatus.PASSED


def test_manifest_accepts_sha256sum_binary_marker(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI")
    manifest = root / MANIFEST_NAME
    lines = manifest.read_text(encoding="utf-8").splitlines()
    manifest.write_text(
        "\n".join(
            line[:65] + "*" + line[66:] if line.endswith("  README.TXT") else line
            for line in lines
        )
        + "\n",
        encoding="utf-8",
    )

    result = PdiVerifier(root).verify()

    assert result.status == PdiVerificationStatus.PASSED


def test_manifest_detects_tampered_and_unlisted_files(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI")
    (root / DICOM_RELATIVE).write_bytes(b"tampered")
    (root / "unexpected.txt").write_text("extra", encoding="utf-8")

    result = PdiVerifier(root).verify()

    assert result.status == PdiVerificationStatus.FAILED
    assert {"manifest_digest_mismatch", "manifest_file_unlisted"} <= _codes(result)


def test_manifest_rejects_parent_traversal(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI")
    manifest = root / MANIFEST_NAME
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"{'0' * 64}  ../outside.dcm\n",
        encoding="utf-8",
    )

    result = PdiVerifier(root).verify()

    assert result.status == PdiVerificationStatus.FAILED
    assert "manifest_path_invalid" in _codes(result)


def test_blank_manifest_never_passes(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI")
    (root / MANIFEST_NAME).write_text("\n", encoding="utf-8")

    result = PdiVerifier(root).verify()

    assert result.status == PdiVerificationStatus.FAILED
    assert "manifest_entry_count_invalid" in _codes(result)


def test_dicomdir_missing_reference_is_reported(tmp_path: Path) -> None:
    root = _pdi(
        tmp_path / "DCMGET_PDI",
        viewer=False,
        reference=("DICOM", "P999999", "S999999", "I999999"),
    )

    result = PdiVerifier(root).verify()

    assert result.status == PdiVerificationStatus.FAILED
    assert "dicomdir_reference_missing" in _codes(result)


def test_dicomdir_must_reference_every_manifest_dicom_file(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI", viewer=False)
    omitted = root / "DICOM/P000001/S000001/I000002"
    omitted.write_bytes(b"second dicom payload")
    _write_manifest(root)

    result = PdiVerifier(root).verify()

    assert result.status == PdiVerificationStatus.FAILED
    assert "dicomdir_reference_omitted" in _codes(result)


def test_dicomdir_rejects_duplicate_references(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI", viewer=False)
    reference = tuple(DICOM_RELATIVE.split("/"))
    _write_dicomdir(root, reference, reference)
    _write_manifest(root)

    result = PdiVerifier(root).verify()

    assert result.status == PdiVerificationStatus.FAILED
    assert "dicomdir_reference_duplicate" in _codes(result)


def test_dicomdir_parent_reference_is_rejected(tmp_path: Path) -> None:
    with pytest.warns(UserWarning, match="Invalid value for VR CS"):
        root = _pdi(
            tmp_path / "DCMGET_PDI",
            viewer=False,
            reference=("DICOM", "..", "OUTSIDE"),
        )

    result = PdiVerifier(root).verify()

    assert result.status == PdiVerificationStatus.FAILED
    assert "dicomdir_invalid_reference" in _codes(result)


def test_viewer_completeness_is_checked_beyond_root_manifest(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI")
    (root / "VIEWER" / "OHIF" / "app-config.js").unlink()
    _write_manifest(root)

    result = PdiVerifier(root).verify()

    assert result.status == PdiVerificationStatus.FAILED
    assert {
        "viewer_resource_missing",
        "viewer_manifest_file_set_mismatch",
    } <= _codes(result)


def test_viewer_index_rejects_encoded_escape(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI")
    index = root / STUDY_INDEX
    payload = json.loads(index.read_text(encoding="utf-8"))
    payload["studies"][0]["series"][0]["instances"][0]["url"] = (
        "dicomweb:/DICOM/%2e%2e/README.TXT"
    )
    index.write_text(json.dumps(payload), encoding="utf-8")
    _write_manifest(root)

    result = PdiVerifier(root).verify()

    assert result.status == PdiVerificationStatus.FAILED
    assert "viewer_index_external_url" in _codes(result)


def test_viewer_index_must_reference_every_manifest_dicom_file(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI")
    second_relative = "DICOM/P000001/S000001/I000002"
    (root / second_relative).write_bytes(b"second dicom payload")
    first_reference = tuple(DICOM_RELATIVE.split("/"))
    second_reference = tuple(second_relative.split("/"))
    _write_dicomdir(root, first_reference, second_reference)
    _write_manifest(root)

    result = PdiVerifier(root).verify()

    assert result.status == PdiVerificationStatus.FAILED
    assert "viewer_index_reference_omitted" in _codes(result)


def test_viewer_index_rejects_duplicate_references(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI")
    index = root / STUDY_INDEX
    payload = json.loads(index.read_text(encoding="utf-8"))
    instances = payload["studies"][0]["series"][0]["instances"]
    instances.append(dict(instances[0]))
    index.write_text(json.dumps(payload), encoding="utf-8")
    _write_manifest(root)

    result = PdiVerifier(root).verify()

    assert result.status == PdiVerificationStatus.FAILED
    assert "viewer_index_reference_duplicate" in _codes(result)


def test_pdi_without_viewer_passes_with_explicit_warning(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI", viewer=False)

    result = PdiVerifier(root).verify()

    assert result.status == PdiVerificationStatus.WARNING
    assert result.ok and not result.viewer_included
    assert "viewer_not_included" in _codes(result)


def test_progress_callback_can_cancel_hashing(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI")
    verifier: PdiVerifier

    def on_progress(progress) -> None:
        if progress.stage == PdiVerificationStage.MANIFEST:
            verifier.cancel()

    verifier = PdiVerifier(root, progress_callback=on_progress)
    result = verifier.verify()

    assert result.status == PdiVerificationStatus.CANCELLED
    assert result.message == "PDI 交付验证已取消"


def test_report_refuses_to_modify_verified_pdi(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI")
    result = PdiVerifier(root).verify()

    with pytest.raises(OSError, match="不能写入 PDI 目录"):
        write_pdi_delivery_reports(result, root / "REPORT")


def test_volume_set_discovers_exact_declared_volumes(tmp_path: Path) -> None:
    root = tmp_path / "DCMGET_PDI_SET"
    root.mkdir()
    first = _pdi(root / "VOLUME_001")
    second = _pdi(root / "VOLUME_002")
    (root / "VOLUME_SET.json").write_text(
        json.dumps(
            {
                "schema": "dcmget-pdi-volume-set",
                "version": 1,
                "volumes": [
                    {"number": 1, "directory": "VOLUME_001"},
                    {"number": 2, "directory": "VOLUME_002"},
                ],
            }
        ),
        encoding="utf-8",
    )

    assert discover_pdi_verification_roots(root) == (
        first.resolve(),
        second.resolve(),
    )
    first_reports = pdi_delivery_report_output_directory(root, first, 2)
    second_reports = pdi_delivery_report_output_directory(root, second, 2)
    expected_root = tmp_path / "DCMGET_PDI_SET-验收报告"
    assert first_reports == expected_root / "VOLUME_001"
    assert second_reports == expected_root / "VOLUME_002"
    assert not first_reports.is_relative_to(root)


def test_single_pdi_report_output_keeps_existing_default(tmp_path: Path) -> None:
    root = _pdi(tmp_path / "DCMGET_PDI")

    assert pdi_delivery_report_output_directory(root, root, 1) is None


@pytest.mark.parametrize("mutation", ["missing", "extra", "renumbered"])
def test_volume_set_rejects_directory_or_index_mismatch(
    tmp_path: Path, mutation: str
) -> None:
    root = tmp_path / "DCMGET_PDI_SET"
    root.mkdir()
    (root / "VOLUME_001").mkdir()
    volumes = [{"number": 1, "directory": "VOLUME_001"}]
    if mutation == "missing":
        volumes.append({"number": 2, "directory": "VOLUME_002"})
    elif mutation == "extra":
        (root / "VOLUME_002").mkdir()
    else:
        volumes[0] = {"number": 2, "directory": "VOLUME_002"}
    (root / "VOLUME_SET.json").write_text(
        json.dumps(
            {
                "schema": "dcmget-pdi-volume-set",
                "version": 1,
                "volumes": volumes,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="分卷"):
        discover_pdi_verification_roots(root)
