from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from filelock import FileLock
from pydicom.dataset import Dataset
from pydicom.multival import MultiValue
from pydicom.uid import ImplicitVRLittleEndian, UID

from .config import ANONYMIZATION_PROFILE_IDS
from .runtime import ensure_application_state_dir


class AnonymizationError(RuntimeError):
    pass


_COMMON_CLEAR = {
    "AdditionalPatientHistory",
    "CountryOfResidence",
    "CurrentPatientLocation",
    "IssuerOfPatientID",
    "IssuerOfClinicalTrialSubjectID",
    "MedicalRecordLocator",
    "MilitaryRank",
    "Occupation",
    "OtherPatientIDs",
    "OtherPatientNames",
    "PatientAddress",
    "PatientBirthDate",
    "PatientBirthName",
    "PatientBirthTime",
    "PatientComments",
    "PatientMotherBirthName",
    "PatientReligiousPreference",
    "PatientTelephoneNumbers",
    "PatientTelecomInformation",
    "PersonAddress",
    "PersonTelephoneNumbers",
    "PersonTelecomInformation",
    "ReferringPhysicianAddress",
    "ReferringPhysicianTelephoneNumbers",
    "RegionOfResidence",
    "ResponsibleOrganization",
    "ResponsiblePerson",
}

_REMOVE_ELEMENTS = {
    "DeidentificationMethodCodeSequence",
    "DigitalSignaturesSequence",
    "DataSetTrailingPadding",
    "EncryptedAttributesSequence",
    "IssuerOfPatientIDQualifiersSequence",
    "IssuerOfAccessionNumberSequence",
    "MACParametersSequence",
    "OriginalAttributesSequence",
    "OtherPatientIDsSequence",
    "OperatorIdentificationSequence",
    "PerformingPhysicianIdentificationSequence",
    "PersonIdentificationCodeSequence",
    "PhysicianReadingStudyIdentificationSequence",
    "PhysiciansOfRecordIdentificationSequence",
    "RequestingPhysicianIdentificationSequence",
}

_IDENTIFIER_FIELDS = {
    "AdmissionID",
    "ClinicalTrialSubjectID",
    "ClinicalTrialSubjectReadingID",
    "FillerOrderNumberImagingServiceRequest",
    "PerformedProcedureStepID",
    "PlacerOrderNumberImagingServiceRequest",
    "RequestedProcedureID",
    "ScheduledProcedureStepID",
    "ServiceEpisodeID",
    "StudyID",
}

_RESEARCH_CLEAR = {
    "AcquisitionComments",
    "AdmittingDiagnosesDescription",
    "ClinicalTrialProtocolName",
    "ClinicalTrialSiteID",
    "ClinicalTrialSiteName",
    "ClinicalTrialSponsorName",
    "DerivationDescription",
    "FrameComments",
    "ImageComments",
    "Impressions",
    "InstitutionAddress",
    "InstitutionCodeSequence",
    "InstitutionName",
    "InstitutionalDepartmentName",
    "InstitutionalDepartmentTypeCodeSequence",
    "InterpretationText",
    "ImagingServiceRequestComments",
    "LocalNamespaceEntityID",
    "PerformedLocation",
    "PerformedStationAETitle",
    "PerformedProcedureStepDescription",
    "ProtocolName",
    "ReasonForTheRequestedProcedure",
    "RequestingService",
    "RequestingServiceCodeSequence",
    "RequestedProcedureDescription",
    "ResultsComments",
    "ScheduledStationAETitle",
    "ScheduledStationName",
    "ScheduledProcedureStepDescription",
    "SeriesDescription",
    "StorageMediaFileSetID",
    "StudyDescription",
    "TextValue",
    "UnformattedTextValue",
    "UniversalEntityID",
    "UniversalEntityIDType",
    "RetrieveAETitle",
}

# LT/ST/UT/UC are free-form text VRs.  They are not required to
# decode Pixel Data and may contain identifying text in newly added Standard
# Attributes that are not yet present in a fixed keyword list.  LO/SH are also
# used for technical acquisition metadata, so only the high-risk Standard
# patient/workflow fields below are cleared by keyword.
_HIGH_RISK_FREE_TEXT_VRS = {"LT", "ST", "UC", "UT"}

_HIGH_RISK_TEXT_KEYWORDS = {
    "Allergies",
    "DischargeDiagnosisDescription",
    "MedicalAlerts",
    "PatientInstitutionResidence",
    "PatientState",
    "PatientTransportArrangements",
    "PreMedication",
    "ReasonForTheImagingServiceRequest",
    "RequestedProcedureLocation",
    "RouteOfAdmissions",
    "ScheduledPatientInstitutionResidence",
    "ScheduledProcedureStepLocation",
    "ServiceEpisodeDescription",
    "SpecialNeeds",
}

_STRICT_CLEAR = {
    "DeviceSerialNumber",
    "DeviceUID",
    "EthnicGroup",
    "ManufacturerModelName",
    "PatientAge",
    "PatientSex",
    "PatientSize",
    "PatientWeight",
    "PregnancyStatus",
    "SmokingStatus",
    "SoftwareVersions",
    "StationName",
}

_PRESERVED_UID_KEYWORDS = {
    "CodingSchemeUID",
    "ContextGroupExtensionCreatorUID",
    "ImplementationClassUID",
    "MappingResourceUID",
    "TransferSyntaxUID",
}


class DicomAnonymizer:
    """Apply one of DcmGet's metadata-only de-identification profiles."""

    def __init__(self, profile: str, secret: bytes | None = None):
        normalized = str(profile).strip().lower()
        if normalized not in ANONYMIZATION_PROFILE_IDS:
            raise AnonymizationError(f"未知匿名方案：{profile}")
        self.profile = normalized
        self._secret = secret if secret is not None else _load_or_create_secret()
        if len(self._secret) < 16:
            raise AnonymizationError("匿名密钥长度不足")

    def anonymize_dataset(self, dataset: Dataset) -> Dataset:
        burned_in = (
            str(getattr(dataset, "BurnedInAnnotation", "") or "").strip().upper()
        )
        if self.profile in {"research", "strict"}:
            if burned_in == "YES":
                raise AnonymizationError("检测到像素烧录标记，匿名处理拒绝归档")
            recognizable = str(
                getattr(dataset, "RecognizableVisualFeatures", "") or ""
            ).strip().upper()
            if recognizable == "YES":
                raise AnonymizationError("检测到可识别视觉特征，匿名处理拒绝归档")
            self._reject_unsupported_embedded_content(dataset)

        identity = self._identity_key(dataset)
        self._remove_private_and_security_data(dataset)
        self._replace_person_names(dataset)
        self._clear_keywords(dataset, _COMMON_CLEAR)

        self._pseudonymize_patient_and_accession(dataset, identity)
        self._pseudonymize_identifiers(dataset, identity)

        if self.profile in {"research", "strict"}:
            self._clear_keywords(dataset, _RESEARCH_CLEAR)
            self._clear_high_risk_text(dataset)
            self._remap_uids(dataset)

        if self.profile == "research":
            self._modify_temporal_values(dataset, identity, remove=False)
            dataset.LongitudinalTemporalInformationModified = "MODIFIED"
        elif self.profile == "strict":
            self._clear_keywords(dataset, _STRICT_CLEAR)
            self._modify_temporal_values(dataset, identity, remove=True)
            dataset.LongitudinalTemporalInformationModified = "REMOVED"

        dataset.PatientIdentityRemoved = "NO" if self.profile == "basic" else "YES"
        dataset.DeidentificationMethod = f"DcmGet metadata profile: {self.profile}"
        self._sanitize_file_structure(dataset)
        return dataset

    @staticmethod
    def _reject_unsupported_embedded_content(dataset: Dataset) -> None:
        for current in _datasets(dataset):
            for element in current:
                if element.keyword == "EncapsulatedDocument":
                    raise AnonymizationError("当前匿名方案不支持 PDF 等内嵌文档")
                is_graphic_group = (element.tag.group & 0xFF00) in {
                    0x5000,
                    0x6000,
                }
                if (
                    element.keyword in {"GraphicAnnotationSequence", "IconImageSequence"}
                    or is_graphic_group
                ):
                    raise AnonymizationError("当前匿名方案不支持图形标注或叠加层")
        sop_class_uid = str(getattr(dataset, "SOPClassUID", "") or "")
        if sop_class_uid:
            name = UID(sop_class_uid).name
            if "SR Storage" in name or "Structured Report" in name:
                raise AnonymizationError("当前匿名方案不支持 SR 结构化报告内容")
            if "Presentation State Storage" in name:
                raise AnonymizationError("当前匿名方案不支持图形展示状态内容")

    def _identity_key(self, dataset: Dataset) -> str:
        patient_id = str(getattr(dataset, "PatientID", "") or "").strip()
        if patient_id:
            issuer = str(getattr(dataset, "IssuerOfPatientID", "") or "").strip()
            return f"patient:{issuer}|{patient_id}"
        for namespace, keyword in (
            ("accession", "AccessionNumber"),
            ("study", "StudyInstanceUID"),
            ("instance", "SOPInstanceUID"),
        ):
            value = str(getattr(dataset, keyword, "") or "").strip()
            if value:
                return f"{namespace}:{value}"
        return "DICOM"

    def _alias(self, prefix: str, value: str, namespace: str) -> str:
        digest = (
            hmac.new(
                self._secret,
                f"{namespace}:{value}".encode("utf-8", errors="replace"),
                hashlib.sha256,
            )
            .hexdigest()[:12]
            .upper()
        )
        return f"{prefix}-{digest}"

    def _uid(self, value: str) -> str:
        digest = hmac.new(
            self._secret,
            f"uid:{value}".encode("ascii", errors="replace"),
            hashlib.sha256,
        ).digest()[:16]
        return f"2.25.{int.from_bytes(digest, 'big')}"

    def _pseudonymize_patient_and_accession(
        self, dataset: Dataset, identity: str
    ) -> None:
        root_has_patient_id = "PatientID" in dataset
        root_has_accession = "AccessionNumber" in dataset
        for current in _datasets(dataset):
            if "PatientID" in current:
                original = str(current.PatientID or identity)
                current.PatientID = self._alias(
                    "ANON", f"{identity}|{original}", "patient"
                )
            if "AccessionNumber" in current:
                original = str(current.AccessionNumber or identity)
                current.AccessionNumber = self._alias(
                    "ACC", f"{identity}|{original}", "accession"
                )
        if not root_has_patient_id:
            dataset.PatientID = self._alias("ANON", identity, "patient")
        if not root_has_accession:
            dataset.AccessionNumber = self._alias("ACC", identity, "accession")

    def _remove_private_and_security_data(self, dataset: Dataset) -> None:
        for current in _datasets(dataset):
            for element in list(current):
                if (
                    element.tag.is_private
                    or element.tag.group == 0x0004
                    or element.keyword in _REMOVE_ELEMENTS
                ):
                    del current[element.tag]

    def _replace_person_names(self, dataset: Dataset) -> None:
        for current in _datasets(dataset):
            for element in current:
                if element.VR == "PN":
                    element.value = _mapped_value(
                        element.value, lambda _value: "ANONYMOUS"
                    )

    def _clear_keywords(self, dataset: Dataset, keywords: set[str]) -> None:
        for current in _datasets(dataset):
            for element in current:
                if element.keyword in keywords:
                    element.value = [] if element.VR == "SQ" else ""

    @staticmethod
    def _clear_high_risk_text(dataset: Dataset) -> None:
        """Clear free-form and known patient/workflow text at every depth."""

        for current in _datasets(dataset):
            for element in current:
                if (
                    element.VR in _HIGH_RISK_FREE_TEXT_VRS
                    or element.keyword in _HIGH_RISK_TEXT_KEYWORDS
                ):
                    element.value = ""

    def _pseudonymize_identifiers(self, dataset: Dataset, identity: str) -> None:
        for current in _datasets(dataset):
            for element in current:
                if element.keyword in _IDENTIFIER_FIELDS:
                    original = str(element.value or identity)
                    element.value = self._alias(
                        "ID", f"{identity}|{original}", element.keyword
                    )

    def _remap_uids(self, dataset: Dataset) -> None:
        for current in _datasets(dataset):
            for element in current:
                if element.VR != "UI" or self._preserve_uid(element.keyword):
                    continue
                element.value = _mapped_value(
                    element.value,
                    lambda value: self._uid(str(value)) if str(value).strip() else "",
                )

    @staticmethod
    def _preserve_uid(keyword: str) -> bool:
        return keyword in _PRESERVED_UID_KEYWORDS or keyword.endswith("ClassUID")

    @staticmethod
    def _sanitize_file_structure(dataset: Dataset) -> None:
        from pydicom.dataset import FileMetaDataset

        original = getattr(dataset, "file_meta", None)
        transfer_syntax = getattr(original, "TransferSyntaxUID", ImplicitVRLittleEndian)
        media_storage_class = str(
            getattr(dataset, "SOPClassUID", "")
            or getattr(original, "MediaStorageSOPClassUID", "")
        )
        media_storage_instance = str(
            getattr(dataset, "SOPInstanceUID", "")
            or getattr(original, "MediaStorageSOPInstanceUID", "")
        )
        clean = FileMetaDataset()
        clean.TransferSyntaxUID = transfer_syntax
        if media_storage_class:
            clean.MediaStorageSOPClassUID = media_storage_class
        if media_storage_instance:
            clean.MediaStorageSOPInstanceUID = media_storage_instance
        dataset.file_meta = clean
        dataset.preamble = b"\0" * 128

    def _modify_temporal_values(
        self, dataset: Dataset, identity: str, *, remove: bool
    ) -> None:
        offset = self._date_offset(identity)
        for current in _datasets(dataset):
            for element in current:
                if element.VR in {"DA", "DT", "TM"}:
                    if remove or element.VR == "TM":
                        element.value = ""
                    elif element.VR == "DA":
                        element.value = _mapped_value(
                            element.value, lambda value: _shift_date(str(value), offset)
                        )
                    else:
                        element.value = _mapped_value(
                            element.value,
                            lambda value: _shift_datetime(str(value), offset),
                        )

    def _date_offset(self, identity: str) -> int:
        digest = hmac.new(
            self._secret,
            f"date:{identity}".encode("utf-8", errors="replace"),
            hashlib.sha256,
        ).digest()
        offset = int.from_bytes(digest[:2], "big") % 7301 - 3650
        return offset or 1


def _datasets(dataset: Dataset) -> Iterable[Dataset]:
    yield dataset
    for element in list(dataset):
        if element.VR == "SQ" and element.value:
            for item in element.value:
                yield from _datasets(item)


def _mapped_value(value: Any, transform):
    if isinstance(value, (list, tuple, MultiValue)):
        return [transform(item) for item in value]
    return transform(value)


def _shift_date(value: str, days: int) -> str:
    text = value.strip()
    if len(text) != 8 or not text.isdigit():
        return ""
    try:
        return (
            date.fromisoformat(f"{text[:4]}-{text[4:6]}-{text[6:8]}")
            + timedelta(days=days)
        ).strftime("%Y%m%d")
    except (OverflowError, ValueError):
        return ""


def _shift_datetime(value: str, days: int) -> str:
    text = value.strip()
    if len(text) < 8 or not text[:8].isdigit():
        return ""
    shifted = _shift_date(text[:8], days)
    return shifted + text[8:] if shifted else ""


def _load_or_create_secret(path: Path | None = None) -> bytes:
    secret_path = path or ensure_application_state_dir() / "anonymization.key"
    lock_path = secret_path.with_name(f".{secret_path.name}.lock")
    try:
        secret_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(str(lock_path)):
            if secret_path.exists():
                secret = secret_path.read_bytes()
            else:
                secret = secrets.token_bytes(32)
                descriptor = -1
                temporary_path: Path | None = None
                try:
                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix=f".{secret_path.name}.",
                        suffix=".tmp",
                        dir=secret_path.parent,
                    )
                    temporary_path = Path(temporary_name)
                    handle = os.fdopen(descriptor, "wb")
                    descriptor = -1
                    with handle:
                        handle.write(secret)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary_path, secret_path)
                    temporary_path = None
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                    if temporary_path is not None:
                        temporary_path.unlink(missing_ok=True)
    except OSError as exc:
        raise AnonymizationError(f"无法读取或创建匿名密钥：{exc}") from exc
    if len(secret) < 32:
        raise AnonymizationError("匿名密钥文件无效")
    return secret
