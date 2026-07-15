from __future__ import annotations

from pathlib import Path

import pytest
from pydicom import dcmread
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ComprehensiveSRStorage, CTImageStorage, ExplicitVRLittleEndian

from dcmget import core
from dcmget.anonymization import AnonymizationError, DicomAnonymizer


SECRET = b"dcmget-test-anonymization-key-32b"


def test_basic_profile_removes_common_identity_and_private_tags():
    dataset = _dataset(None, "1.2.3.1", "1.2.3.10")

    DicomAnonymizer("basic", secret=SECRET).anonymize_dataset(dataset)

    assert str(dataset.PatientName) == "ANONYMOUS"
    assert str(dataset.PatientID).startswith("ANON-")
    assert str(dataset.AccessionNumber).startswith("ACC-")
    assert dataset.PatientBirthDate == ""
    assert dataset.StudyInstanceUID == "1.2.3.10"
    assert dataset.StudyDate == "20240115"
    assert dataset.InstitutionName == "Original Hospital"
    assert dataset.StudyDescription == "Patient named in free text"
    assert dataset.PatientSex == "F"
    assert (0x0011, 0x1010) not in dataset
    assert (
        str(dataset.RequestAttributesSequence[0].ReferringPhysicianName) == "ANONYMOUS"
    )
    assert dataset.RequestAttributesSequence[0].PatientID == dataset.PatientID
    assert (
        dataset.RequestAttributesSequence[0].AccessionNumber == dataset.AccessionNumber
    )
    assert (0x0013, 0x1010) not in dataset.RequestAttributesSequence[0]
    assert dataset.preamble == b"\0" * 128
    assert "SourceApplicationEntityTitle" not in dataset.file_meta
    assert "PrivateInformation" not in dataset.file_meta
    assert dataset.PatientIdentityRemoved == "NO"


def test_research_aliases_are_stable_across_anonymizer_instances():
    first = _dataset(None, "1.2.3.1", "1.2.3.10")
    second = _dataset(None, "1.2.3.1", "1.2.3.10")

    DicomAnonymizer("research", secret=SECRET).anonymize_dataset(first)
    DicomAnonymizer("research", secret=SECRET).anonymize_dataset(second)

    assert first.PatientID == second.PatientID
    assert first.AccessionNumber == second.AccessionNumber
    assert first.StudyInstanceUID == second.StudyInstanceUID


def test_research_profile_archives_with_consistent_aliases_uids_and_shifted_dates(
    tmp_path,
):
    staging = tmp_path / "staging"
    staging.mkdir()
    first = staging / "original-patient-one.dcm"
    second = staging / "original-patient-two.dcm"
    _dataset(first, "1.2.3.1", "1.2.3.10", referenced_uid="1.2.3.2", instance=1)
    _dataset(second, "1.2.3.2", "1.2.3.10", referenced_uid="1.2.3.1", instance=2)
    anonymizer = DicomAnonymizer("research", secret=SECRET)

    moved, rejected = core._archive_dicom_files(
        [first, second],
        tmp_path / "result",
        "{PatientID}/{AccessionNumber}/{StudyInstanceUID}",
        "ORIGINAL-ACCESSION",
        anonymizer=anonymizer,
    )

    assert rejected == []
    assert len(moved) == 2
    assert not first.exists() and not second.exists()
    assert all(path.suffix == ".dcm" for path in moved)
    assert all("ORIGINAL" not in str(path) for path in moved)

    datasets = {int(ds.InstanceNumber): ds for ds in map(dcmread, moved)}
    one, two = datasets[1], datasets[2]
    assert one.PatientID == two.PatientID
    assert one.AccessionNumber == two.AccessionNumber
    assert one.StudyInstanceUID == two.StudyInstanceUID
    assert one.StudyInstanceUID != "1.2.3.10"
    assert one.ReferencedImageSequence[0].ReferencedSOPInstanceUID == two.SOPInstanceUID
    assert two.ReferencedImageSequence[0].ReferencedSOPInstanceUID == one.SOPInstanceUID
    assert one.file_meta.MediaStorageSOPInstanceUID == one.SOPInstanceUID
    assert one.StudyDate == two.StudyDate != "20240115"
    assert one.StudyDescription == ""
    assert one.InstitutionName == ""
    assert one.ClinicalTrialSubjectID.startswith("ID-")
    assert one.PatientTelecomInformation == ""
    assert one.IssuerOfClinicalTrialSubjectID == ""
    assert "IssuerOfAccessionNumberSequence" not in one
    assert one.RetrieveAETitle == ""
    assert one.StorageMediaFileSetID == ""
    assert one.RequestingService == ""
    assert "DataSetTrailingPadding" not in one
    assert one.RequestAttributesSequence[0].ReferringPhysicianAddress == ""
    assert one.RequestAttributesSequence[0].ReferringPhysicianTelephoneNumbers == ""
    assert one.RequestAttributesSequence[0].PersonTelecomInformation == ""
    assert one.RequestAttributesSequence[0].PerformedStationAETitle == ""
    assert one.RequestAttributesSequence[0].TextValue == ""
    assert one.PatientSex == "F"
    assert one.PixelData == b"\x01\x02"
    assert one.preamble == b"\0" * 128
    assert "SourceApplicationEntityTitle" not in one.file_meta
    assert "PrivateInformation" not in one.file_meta
    assert not any(element.tag.group == 0x0004 for element in one)
    assert moved[0].name in {f"{one.SOPInstanceUID}.dcm", f"{two.SOPInstanceUID}.dcm"}


def test_strict_profile_clears_temporal_demographic_and_device_values():
    dataset = _dataset(None, "1.2.3.1", "1.2.3.10")

    DicomAnonymizer("strict", secret=SECRET).anonymize_dataset(dataset)

    assert dataset.StudyDate == ""
    assert dataset.StudyTime == ""
    assert dataset.PatientSex == ""
    assert dataset.PatientAge == ""
    assert dataset.PatientWeight == ""
    assert dataset.DeviceSerialNumber == ""
    assert dataset.StudyDescription == ""
    assert dataset.LongitudinalTemporalInformationModified == "REMOVED"
    assert dataset.PixelData == b"\x01\x02"


@pytest.mark.parametrize("profile", ["research", "strict"])
def test_anonymous_profiles_reject_known_burned_in_pixels(profile):
    dataset = _dataset(None, "1.2.3.1", "1.2.3.10")
    dataset.BurnedInAnnotation = "YES"

    with pytest.raises(AnonymizationError, match="烧录"):
        DicomAnonymizer(profile, secret=SECRET).anonymize_dataset(dataset)


@pytest.mark.parametrize("profile", ["research", "strict"])
def test_anonymous_profiles_reject_recognizable_visual_features(profile):
    dataset = _dataset(None, "1.2.3.1", "1.2.3.10")
    dataset.RecognizableVisualFeatures = "YES"

    with pytest.raises(AnonymizationError, match="视觉特征"):
        DicomAnonymizer(profile, secret=SECRET).anonymize_dataset(dataset)


@pytest.mark.parametrize(
    "embedded_type", ["pdf", "sr", "overlay", "icon", "curve"]
)
def test_research_and_strict_profiles_reject_unparsed_embedded_content(
    embedded_type,
):
    dataset = _dataset(None, "1.2.3.1", "1.2.3.10")
    if embedded_type == "pdf":
        dataset.EncapsulatedDocument = b"%PDF patient name"
    elif embedded_type == "sr":
        dataset.SOPClassUID = ComprehensiveSRStorage
    elif embedded_type == "overlay":
        dataset.add_new((0x6000, 0x0022), "LO", "patient overlay")
    elif embedded_type == "icon":
        icon = Dataset()
        icon.PixelData = b"patient thumbnail"
        dataset.IconImageSequence = Sequence([icon])
    else:
        dataset.add_new((0x5000, 0x3000), "OW", b"patient curve")

    for profile in ("research", "strict"):
        with pytest.raises(AnonymizationError, match="不支持"):
            DicomAnonymizer(profile, secret=SECRET).anonymize_dataset(dataset.copy())


def test_anonymization_failure_leaves_original_in_private_staging(tmp_path):
    staging = tmp_path / "private-staging"
    staging.mkdir()
    source = staging / "source.dcm"
    _dataset(source, "1.2.3.1", "1.2.3.10")

    class FailingAnonymizer:
        def anonymize_dataset(self, _dataset):
            raise AnonymizationError("test failure")

    errors = []
    moved, rejected = core._archive_dicom_files(
        [source],
        tmp_path / "result",
        "{PatientID}",
        "ACC",
        anonymizer=FailingAnonymizer(),  # type: ignore[arg-type]
        error_callback=lambda _path, message: errors.append(message),
    )

    assert moved == []
    assert rejected == [source]
    assert source.exists()
    assert not list((tmp_path / "result").rglob("*.dcm"))
    assert errors == ["test failure"]


def test_anonymized_write_failure_removes_temporary_output_and_keeps_source(
    tmp_path, monkeypatch
):
    staging = tmp_path / "private-staging"
    staging.mkdir()
    source = staging / "source.dcm"
    _dataset(source, "1.2.3.1", "1.2.3.10")

    def fail_save(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(FileDataset, "save_as", fail_save)

    moved, rejected = core._archive_dicom_files(
        [source],
        tmp_path / "result",
        "{PatientID}",
        "ACC",
        anonymizer=DicomAnonymizer("research", secret=SECRET),
    )

    assert moved == []
    assert rejected == [source]
    assert source.exists()
    assert not list((tmp_path / "result").rglob(".dcmget-anonymous-*.tmp"))


def _dataset(
    path: Path | None,
    sop_uid: str,
    study_uid: str,
    *,
    referenced_uid: str = "1.2.3.99",
    instance: int = 1,
) -> FileDataset:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.SourceApplicationEntityTitle = "SOURCE-PHI"
    file_meta.PrivateInformationCreatorUID = "1.2.3.999"
    file_meta.PrivateInformation = b"SOURCE-PATIENT"
    preamble = b"PATIENT-PREAMBLE".ljust(128, b"!")
    dataset = FileDataset(path, {}, file_meta=file_meta, preamble=preamble)
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = sop_uid
    dataset.StudyInstanceUID = study_uid
    dataset.SeriesInstanceUID = f"{study_uid}.1"
    dataset.PatientName = "Original^Patient"
    dataset.PatientID = "ORIGINAL-PATIENT"
    dataset.PatientBirthDate = "19801231"
    dataset.PatientAddress = "Original Address"
    dataset.PatientTelephoneNumbers = "123456789"
    dataset.PatientTelecomInformation = "patient@example.invalid"
    dataset.PatientSex = "F"
    dataset.PatientAge = "043Y"
    dataset.PatientWeight = "60"
    dataset.AccessionNumber = "ORIG-ACCESSION"
    dataset.StudyID = "ORIG-STUDY"
    dataset.StudyDate = "20240115"
    dataset.StudyTime = "093000"
    dataset.StudyDescription = "Patient named in free text"
    dataset.InstitutionName = "Original Hospital"
    dataset.DeviceSerialNumber = "DEVICE-SECRET"
    dataset.ClinicalTrialSubjectID = "TRIAL-SUBJECT"
    dataset.IssuerOfClinicalTrialSubjectID = "TRIAL-ISSUER"
    dataset.RetrieveAETitle = "RETRIEVE-AE"
    dataset.StorageMediaFileSetID = "ORIG-MEDIA"
    dataset.RequestingService = "Original Service"
    dataset.DataSetTrailingPadding = b"ORIGINAL-PATIENT-PADDING"
    dataset.add_new((0x0004, 0x1130), "CS", "ORIGINAL_FILESET")
    dataset.BurnedInAnnotation = "NO"
    dataset.Modality = "CT"
    dataset.InstanceNumber = instance
    dataset.Rows = 1
    dataset.Columns = 2
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.PixelData = b"\x01\x02"
    dataset.add_new((0x0011, 0x1010), "LO", "PRIVATE-PATIENT-DATA")
    request = Dataset()
    request.ReferringPhysicianName = "Original^Doctor"
    request.ReferringPhysicianAddress = "Original Doctor Address"
    request.ReferringPhysicianTelephoneNumbers = "123456789"
    request.PersonTelecomInformation = "doctor@example.invalid"
    request.PatientID = "ORIGINAL-PATIENT"
    request.AccessionNumber = "ORIG-ACCESSION"
    request.PerformedStationAETitle = "ORIGINAL-AE"
    request.ScheduledProcedureStepID = "ORIGINAL-STEP"
    request.TextValue = "Original patient in nested free text"
    request.add_new((0x0013, 0x1010), "LO", "NESTED-PRIVATE-DATA")
    dataset.RequestAttributesSequence = Sequence([request])
    issuer = Dataset()
    issuer.LocalNamespaceEntityID = "ORIGINAL-NAMESPACE"
    dataset.IssuerOfAccessionNumberSequence = Sequence([issuer])
    reference = Dataset()
    reference.ReferencedSOPClassUID = CTImageStorage
    reference.ReferencedSOPInstanceUID = referenced_uid
    dataset.ReferencedImageSequence = Sequence([reference])
    if path is not None:
        dataset.save_as(path, enforce_file_format=True)
    return dataset
