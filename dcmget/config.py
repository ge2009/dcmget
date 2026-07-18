from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from filelock import FileLock


DEFAULT_DIRECTORY_TEMPLATE = "{PatientID}/{AccessionNumber}/{StudyInstanceUID}"
DIRECTORY_TEMPLATES = (
    DEFAULT_DIRECTORY_TEMPLATE,
    "{AccessionNumber}/{StudyInstanceUID}",
    "{PatientID}/{AccessionNumber}",
    "{AccessionNumber}",
    "{StudyInstanceUID}",
)
DIRECTORY_TEMPLATE_FIELDS = {"PatientID", "AccessionNumber", "StudyInstanceUID"}

AE_TITLE_LABELS = {
    "calling_ae_title": "本机调用 AE Title",
    "pacs_ae_title": "PACS AE Title",
    "storage_ae_title": "接收 AE Title",
}

DEFAULT_ANONYMIZATION_PROFILE = "research"
DEFAULT_MINIMUM_FREE_SPACE_BYTES = 2 * 1024**3
DEFAULT_AUTO_RETRY_ATTEMPTS = 2
DEFAULT_AUTO_RETRY_BACKOFF_SECONDS = 3
DEFAULT_CIRCUIT_BREAKER_FAILURES = 5
ANONYMIZATION_PROFILE_OPTIONS = (
    (
        "basic",
        "基础脱敏（院内）",
        "处理直接身份和检查号；保留检查日期、机构、描述和 DICOM UID。",
    ),
    (
        "research",
        "研究匿名（推荐）",
        "检查号与 UID 假名化、日期一致偏移，并清理机构、描述和私有标签。",
    ),
    (
        "strict",
        "严格元数据匿名",
        "在研究方案上继续清除日期、人口学和设备信息；仍不处理像素内容。",
    ),
)
ANONYMIZATION_PROFILE_IDS = {
    profile_id for profile_id, _label, _description in ANONYMIZATION_PROFILE_OPTIONS
}


def validate_ae_title(value: object, label: str = "AE Title") -> str:
    """Return a user-facing validation error for a DICOM AE Title."""
    title = ("" if value is None else str(value)).strip(" ")
    if not title:
        return f"请输入{label}"
    if len(title) > 16:
        return f"{label}最多 16 个字符"
    if "\\" in title or any(not 0x20 <= ord(char) <= 0x7E for char in title):
        return (
            f"{label}只能使用可打印 ASCII 字符，且不能包含反斜杠（\\）"
        )
    return ""


@dataclass(slots=True)
class AppConfig:
    config_version: int = 7
    dcmtk_bin_dir: str = ""
    access_numbers_file_path: str = "access.txt"
    dicom_destination_folder: str = "Dicom"
    pacs_server_ip: str = "127.0.0.1"
    pacs_server_port: int = 8104
    calling_ae_title: str = "DCMGET"
    pacs_ae_title: str = "ANY-SCP"
    storage_ae_title: str = "DCMGET"
    storage_port: int = 6666
    max_concurrent_moves: int = 2
    directory_template: str = DEFAULT_DIRECTORY_TEMPLATE
    anonymization_enabled: bool = False
    anonymization_profile: str = DEFAULT_ANONYMIZATION_PROFILE
    pdi_export_enabled: bool = False
    pdi_institution_name: str = ""
    pdi_output_folder: str = ""
    pdi_include_ohif_viewer: bool = True
    pdi_volume_size_bytes: int = 0
    minimum_free_space_bytes: int = DEFAULT_MINIMUM_FREE_SPACE_BYTES
    auto_retry_attempts: int = DEFAULT_AUTO_RETRY_ATTEMPTS
    auto_retry_backoff_seconds: int = DEFAULT_AUTO_RETRY_BACKOFF_SECONDS
    circuit_breaker_failures: int = DEFAULT_CIRCUIT_BREAKER_FAILURES
    max_log_file_size_bytes: int = 104_857_600

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        data = dict(raw)
        source_version = int(data.get("config_version", 1) or 1)
        is_legacy = source_version < 2

        if is_legacy:
            legacy_move_destination = data.get("calling_ae_title", "DCMGET")
            data = {
                "config_version": 7,
                "dcmtk_bin_dir": _legacy_dcmtk_dir(data.get("movescu_executable_path", "")),
                "access_numbers_file_path": data.get("access_numbers_file_path", "access.txt"),
                "dicom_destination_folder": data.get("dicom_destination_folder", "Dicom"),
                "pacs_server_ip": data.get("pacs_server_ip", "127.0.0.1"),
                "pacs_server_port": data.get("pacs_server_port", 8104),
                "calling_ae_title": data.get("application_entity_title", "DCMGET"),
                "pacs_ae_title": data.get("called_ae_title", "ANY-SCP"),
                "storage_ae_title": legacy_move_destination,
                "storage_port": data.get("network_port", 6666),
                "max_concurrent_moves": 2,
                "directory_template": DEFAULT_DIRECTORY_TEMPLATE,
                "anonymization_enabled": False,
                "anonymization_profile": DEFAULT_ANONYMIZATION_PROFILE,
                "pdi_export_enabled": False,
                "pdi_institution_name": "",
                "pdi_output_folder": "",
                "pdi_include_ohif_viewer": True,
                "pdi_volume_size_bytes": 0,
                "minimum_free_space_bytes": DEFAULT_MINIMUM_FREE_SPACE_BYTES,
                "auto_retry_attempts": DEFAULT_AUTO_RETRY_ATTEMPTS,
                "auto_retry_backoff_seconds": DEFAULT_AUTO_RETRY_BACKOFF_SECONDS,
                "circuit_breaker_failures": DEFAULT_CIRCUIT_BREAKER_FAILURES,
                "max_log_file_size_bytes": data.get(
                    "max_log_file_size_bytes", 104_857_600
                ),
            }
        elif "pdi_include_ohif_viewer" not in data and source_version <= 4:
            data["pdi_include_ohif_viewer"] = _as_bool(
                data.get("pdi_include_html_preview"), True
            ) or _as_bool(data.get("pdi_include_weasis_windows"), True)

        defaults = cls()
        values = {
            field: data.get(field, getattr(defaults, field))
            for field in asdict(defaults)
        }
        values["config_version"] = 7
        values["pacs_server_port"] = _as_int(values["pacs_server_port"], 8104)
        values["storage_port"] = _as_int(values["storage_port"], 6666)
        values["max_concurrent_moves"] = _as_int(
            values["max_concurrent_moves"], 2
        )
        values["max_log_file_size_bytes"] = _as_int(
            values["max_log_file_size_bytes"], 104_857_600
        )
        values["pdi_volume_size_bytes"] = _as_int(
            values["pdi_volume_size_bytes"], 0
        )
        values["minimum_free_space_bytes"] = _as_int(
            values["minimum_free_space_bytes"], DEFAULT_MINIMUM_FREE_SPACE_BYTES
        )
        values["auto_retry_attempts"] = _as_int(
            values["auto_retry_attempts"], DEFAULT_AUTO_RETRY_ATTEMPTS
        )
        values["auto_retry_backoff_seconds"] = _as_int(
            values["auto_retry_backoff_seconds"], DEFAULT_AUTO_RETRY_BACKOFF_SECONDS
        )
        values["circuit_breaker_failures"] = _as_int(
            values["circuit_breaker_failures"], DEFAULT_CIRCUIT_BREAKER_FAILURES
        )
        for field in AE_TITLE_LABELS:
            value = values[field]
            values[field] = ("" if value is None else str(value)).strip(" ")
        values["anonymization_enabled"] = _as_bool(
            values["anonymization_enabled"], False
        )
        values["pdi_export_enabled"] = _as_bool(
            values["pdi_export_enabled"], False
        )
        values["pdi_include_ohif_viewer"] = _as_bool(
            values["pdi_include_ohif_viewer"], True
        )
        values["anonymization_profile"] = str(
            values["anonymization_profile"] or DEFAULT_ANONYMIZATION_PROFILE
        ).strip().lower()
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> dict[str, str]:
        errors: dict[str, str] = {}
        required = {
            "dicom_destination_folder": "请选择 DICOM 保存目录",
            "pacs_server_ip": "请输入 PACS 服务器地址",
        }
        for field, message in required.items():
            if not str(getattr(self, field)).strip():
                errors[field] = message

        for field, label in AE_TITLE_LABELS.items():
            message = validate_ae_title(getattr(self, field), label)
            if message:
                errors[field] = message

        if not 1 <= self.pacs_server_port <= 65535:
            errors["pacs_server_port"] = "端口必须在 1 到 65535 之间"
        if not 1 <= self.storage_port <= 65535:
            errors["storage_port"] = "端口必须在 1 到 65535 之间"
        if not 1 <= self.max_concurrent_moves <= 8:
            errors["max_concurrent_moves"] = "并发下载数必须在 1 到 8 之间"
        if self.max_log_file_size_bytes < 1024:
            errors["max_log_file_size_bytes"] = "日志大小至少为 1024 字节"
        if self.minimum_free_space_bytes < 0:
            errors["minimum_free_space_bytes"] = "磁盘保留空间不能为负数"
        if not 0 <= self.auto_retry_attempts <= 10:
            errors["auto_retry_attempts"] = "自动重试次数必须在 0 到 10 之间"
        if not 0 <= self.auto_retry_backoff_seconds <= 300:
            errors["auto_retry_backoff_seconds"] = "重试等待时间必须在 0 到 300 秒之间"
        if not 2 <= self.circuit_breaker_failures <= 100:
            errors["circuit_breaker_failures"] = "连续失败暂停阈值必须在 2 到 100 之间"
        if self.pdi_volume_size_bytes < 0:
            errors["pdi_volume_size_bytes"] = "PDI 分卷容量不能为负数"
        if (
            self.anonymization_enabled
            and self.anonymization_profile not in ANONYMIZATION_PROFILE_IDS
        ):
            errors["anonymization_profile"] = "请选择有效的匿名方案"
        if self.pdi_export_enabled:
            if not self.pdi_institution_name.strip():
                errors["pdi_institution_name"] = "启用 PDI 时请输入机构名称"
        template = self.directory_template.strip().replace("\\", "/")
        fields = set(re.findall(r"\{([^{}]+)\}", template))
        without_placeholders = re.sub(
            r"\{(?:PatientID|AccessionNumber|StudyInstanceUID)\}", "", template
        )
        if not template or not fields:
            errors["directory_template"] = "目录模板至少包含一个 DICOM 字段"
        elif fields - DIRECTORY_TEMPLATE_FIELDS:
            unknown = "、".join(sorted(fields - DIRECTORY_TEMPLATE_FIELDS))
            errors["directory_template"] = f"目录模板包含不支持的字段：{unknown}"
        elif "{" in without_placeholders or "}" in without_placeholders:
            errors["directory_template"] = "目录模板中的花括号不完整"
        elif template.startswith("/") or re.match(r"^[A-Za-z]:", template) or any(
            segment in {".", ".."} for segment in template.split("/")
        ):
            errors["directory_template"] = "目录模板不能使用绝对路径或上级目录"
        return errors


@dataclass(frozen=True, slots=True)
class AccessionParseResult:
    values: list[str]
    blank_count: int
    duplicate_count: int
    invalid_values: tuple[str, ...] = ()


def parse_accessions(lines: str | Iterable[str]) -> AccessionParseResult:
    source = lines.splitlines() if isinstance(lines, str) else list(lines)
    values: list[str] = []
    seen: set[str] = set()
    blank_count = 0
    duplicate_count = 0
    invalid_values: list[str] = []

    for raw in source:
        value = str(raw).strip()
        if not value:
            blank_count += 1
            continue
        if re.search(r"[\\*?\x00-\x1f\x7f]", value):
            invalid_values.append(value)
            continue
        if value in seen:
            duplicate_count += 1
            continue
        seen.add(value)
        values.append(value)

    return AccessionParseResult(
        values,
        blank_count,
        duplicate_count,
        tuple(invalid_values),
    )


def load_accessions(path: str | Path) -> AccessionParseResult:
    with Path(path).expanduser().open("r", encoding="utf-8-sig") as handle:
        return parse_accessions(handle.readlines())


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser()
    if not config_path.exists():
        return AppConfig()
    with config_path.open("r", encoding="utf-8-sig") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("配置文件根节点必须是 JSON 对象")
    return AppConfig.from_dict(raw)


def save_config(path: str | Path, config: AppConfig) -> None:
    config_path = Path(path).expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = config_path.with_name(f"{config_path.name}.lock")
    temporary = config_path.with_name(
        f".{config_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    with FileLock(str(lock_path), timeout=10):
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(config.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, config_path)
        finally:
            temporary.unlink(missing_ok=True)


def _legacy_dcmtk_dir(value: Any) -> str:
    if not value:
        return ""
    path = Path(str(value))
    return str(path.parent if path.suffix.lower() == ".exe" or path.name == "movescu" else path)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default
