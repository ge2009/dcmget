from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .config import AppConfig
from .diagnostics import diagnostic_log_directory
from .health import HealthReport, run_health_check


SUPPORT_BUNDLE_SCHEMA_VERSION = 1
DEFAULT_MAX_LOG_FILES = 20
DEFAULT_MAX_LOG_BYTES = 2 * 1024 * 1024
_DIAGNOSTIC_PATTERNS = (
    "dcmget-diagnostics*.log*",
    "dcmget-crash*.log*",
)
_SENSITIVE_LABEL_TOKEN = (
    r"(?:Accession(?:\s*(?:Number|No\.?))?|"
    r"Patient(?:'s)?\s*(?:Name|ID)|"
    r"患者(?:姓名|名称|ID)|检查号)"
)
_SENSITIVE_LABEL_RE = re.compile(
    rf"(?im)({_SENSITIVE_LABEL_TOKEN}\s*[:=：]\s*)"
    rf"(.+?)(?=(?:\s+{_SENSITIVE_LABEL_TOKEN}\s*[:=：])|[,;|\r\n]|$)"
)
_SENSITIVE_BRACKET_LABEL_RE = re.compile(
    rf"(?im)({_SENSITIVE_LABEL_TOKEN}\s*\[\s*)([^\]\r\n]*)(\])"
)
_SENSITIVE_PROSE_LABEL_RE = re.compile(
    rf"(?im)({_SENSITIVE_LABEL_TOKEN}\s+(?:is|was|value(?:\s+is)?|为|是)\s+)"
    rf"(.+?)(?=(?:\s+{_SENSITIVE_LABEL_TOKEN}\b)|[,;|\r\n]|$)"
)
_ACCESSION_COMMAND_RE = re.compile(r"(?i)(0008,?0050\s*=\s*)([^\s,;]+)")
_START_ACCESSION_RE = re.compile(r"(?i)(开始检查号\s+)([^\s,;]+)")
_ACCESSION_LABEL_RE = re.compile(r"(?i)(检查号\s+)([^\s,;：]+)")
_LOG_ACCESSION_RE = re.compile(
    r"(?m)(\]\s*)([A-Za-z0-9][A-Za-z0-9._-]{3,})(?=\s*(?:：|发生))"
)
_LEADING_ACCESSION_RE = re.compile(
    r"(?m)^[A-Za-z0-9][A-Za-z0-9._-]{3,}(?=：(?:完成|失败|无数据|部分成功|已取消))"
)
_DICOM_SENSITIVE_TAG_RE = re.compile(
    r"(?i)((?:\(?0008\s*,?\s*0050\)?|"
    r"\(?0010\s*,?\s*(?:0010|0020)\)?)[^\r\n\[]*\[)([^\]]*)(\])"
)
_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6_CANDIDATE_RE = re.compile(
    r"(?<![0-9A-Za-z])(?:\[[0-9A-Fa-f:]+(?:%[A-Za-z0-9_.-]+)?\]|"
    r"[0-9A-Fa-f:]+(?:%[A-Za-z0-9_.-]+)?)(?![0-9A-Za-z])"
)
_WINDOWS_USER_PATH_RE = re.compile(r"(?i)([A-Z]:\\Users\\)([^\\/\s]+)")
_POSIX_USER_PATH_RE = re.compile(r"(?i)(/(?:Users|home)/)([^/\s]+)")
_UNC_PATH_RE = re.compile(
    r"(?i)(?<![:\\/\w])(?:\\\\|//)[^\\/\r\n\t]+[\\/]"
    r"[^\r\n\t,;|\"']+"
)
_URL_HOST_RE = re.compile(
    r"(?i)(\b(?:https?|dicom)://)(\[[^\]\s]+\]|[^/:\s?#]+)"
)
_HOST_LABEL_RE = re.compile(
    r"(?i)(\b(?:PACS(?:\s+(?:host|server|address))?|host(?:name)?|"
    r"server|endpoint)\s*[:=：]\s*)"
    r"(\[[^\]\s]+\]|[A-Za-z0-9][A-Za-z0-9._-]*)(:\d{1,5})?"
)
_PACS_BARE_HOST_RE = re.compile(
    r"(?i)(\bPACS\s+)((?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+)(:\d{1,5})?"
)
_INTERNAL_HOSTNAME_RE = re.compile(
    r"(?i)(?<![@A-Za-z0-9_-])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:local|internal|lan)(?![A-Za-z0-9_-])"
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/][^\r\n\t,;|\"']+"
)
_POSIX_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![:A-Za-z0-9])/(?:[^\s/,:;|\"']+/)*[^\s,:;|\"']*"
)
_SENSITIVE_HINT_RE = re.compile(
    rf"(?i){_SENSITIVE_LABEL_TOKEN}|"
    r"\(?0008\s*,?\s*0050\)?|"
    r"\(?0010\s*,?\s*(?:0010|0020)\)?"
)
_SAFE_REDACTED_VALUE_RE = re.compile(
    r"(?i)^\s*(?:(?:[:=：]|is|was|value(?:\s+is)?|为|是)\s*)?"
    r"(?:\[\s*)?<(?:REDACTED|ACCESSION)>(?:\s*\])?"
)
_SAFE_DIAGNOSTIC_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:(?:\d{4}-\d{2}-\d{2}[ T][0-9:.,+\-]+)\s+)?"
    r"(?:(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\b(?:\s+\[[^\]\r\n]+\])?\s*|"
    r"[IWEF]:\s*|[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning):\s*)"
)


class SupportBundleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SupportBundleResult:
    path: Path
    included_files: tuple[str, ...]
    omitted_log_count: int


def create_support_bundle(
    output_path: str | Path,
    config: AppConfig,
    *,
    project_root: str | Path | None = None,
    diagnostic_directory: str | Path | None = None,
    health_report: HealthReport | Mapping[str, Any] | None = None,
    max_log_files: int = DEFAULT_MAX_LOG_FILES,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    now: datetime | None = None,
) -> SupportBundleResult:
    """Create a local-only, PHI-redacted diagnostic ZIP.

    Only generated JSON and diagnostic/crash text logs are considered. DICOM,
    task databases, access-number input files, licenses and trial state are
    deliberately outside the allowlist.
    """

    if max_log_files < 0 or max_log_bytes < 1:
        raise SupportBundleError("日志文件数量和大小限制无效")
    output = Path(output_path).expanduser()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SupportBundleError(f"无法创建支持包目录：{exc}") from exc
    generated = now or datetime.now(timezone.utc)

    report = (
        run_health_check(config, project_root=project_root)
        if health_report is None
        else health_report
    )
    try:
        report_data = (
            report.to_dict() if isinstance(report, HealthReport) else dict(report)
        )
    except (TypeError, ValueError) as exc:
        raise SupportBundleError("健康检查结果格式无效") from exc
    entries: dict[str, bytes] = {
        "health.json": _json_bytes(redact_data(report_data)),
        "config-summary.json": _json_bytes(config_summary(config)),
    }

    log_directory = Path(
        diagnostic_directory
        if diagnostic_directory is not None
        else diagnostic_log_directory()
    ).expanduser()
    candidates = _diagnostic_files(log_directory)
    selected = candidates[:max_log_files]
    for index, source in enumerate(selected, 1):
        try:
            raw = _read_tail(source, max_log_bytes)
        except OSError:
            continue
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip(".-")
        safe_name = safe_name or f"diagnostic-{index}.log"
        archive_name = f"logs/{index:02d}-{safe_name}"
        entries[archive_name] = redact_text(
            raw.decode("utf-8", errors="replace")
        ).encode("utf-8")

    manifest_entries = [
        {
            "path": name,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, content in sorted(entries.items())
    ]
    manifest = {
        "schema": "dcmget-support-bundle",
        "schema_version": SUPPORT_BUNDLE_SCHEMA_VERSION,
        "app_version": __version__,
        "generated_at": generated.isoformat(),
        "privacy": {
            "redacted": True,
            "contains_dicom": False,
            "contains_license": False,
            "contains_trial_state": False,
        },
        "omitted_log_count": max(0, len(candidates) - len(selected)),
        "entries": manifest_entries,
    }
    entries["manifest.json"] = _json_bytes(manifest)

    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as package:
            for name, content in sorted(entries.items()):
                package.writestr(name, content)
        os.replace(temporary, output)
        try:
            output.chmod(0o600)
        except OSError:
            pass
    except (OSError, zipfile.BadZipFile) as exc:
        raise SupportBundleError(f"无法生成支持包：{exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)

    return SupportBundleResult(
        output,
        tuple(sorted(entries)),
        max(0, len(candidates) - len(selected)),
    )


def config_summary(config: AppConfig) -> dict[str, Any]:
    return {
        "schema": "dcmget-config-summary",
        "config_version": config.config_version,
        "dcmtk_bin_dir": _redact_path(config.dcmtk_bin_dir),
        "access_numbers_file_configured": bool(config.access_numbers_file_path.strip()),
        "dicom_destination_folder": _redact_path(config.dicom_destination_folder),
        "pacs_server_ip": "<IP>" if config.pacs_server_ip.strip() else "",
        "pacs_server_port": config.pacs_server_port,
        "calling_ae_title": _mask_identifier(config.calling_ae_title),
        "pacs_ae_title": _mask_identifier(config.pacs_ae_title),
        "storage_ae_title": _mask_identifier(config.storage_ae_title),
        "storage_port": config.storage_port,
        "directory_template": config.directory_template,
        "anonymization_enabled": config.anonymization_enabled,
        "anonymization_profile": config.anonymization_profile,
        "pdi_export_enabled": config.pdi_export_enabled,
        "pdi_output_folder": _redact_path(config.pdi_output_folder),
        "pdi_include_ohif_viewer": config.pdi_include_ohif_viewer,
    }


def redact_data(value: Any, key_hint: str = "") -> Any:
    if isinstance(value, str):
        lowered = key_hint.lower()
        if any(
            token in lowered for token in ("patient", "accession", "患者", "检查号")
        ):
            return "<REDACTED>" if value else ""
        if any(
            token in lowered for token in ("path", "directory", "executable", "bin_dir")
        ):
            return _redact_path(value)
        if any(
            token in lowered
            for token in ("host", "hostname", "server", "endpoint", "address")
        ):
            return "<HOST>" if value else ""
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(key): redact_data(item, str(key)) for key, item in value.items()}
    if isinstance(value, tuple):
        return [redact_data(item, key_hint) for item in value]
    if isinstance(value, list):
        return [redact_data(item, key_hint) for item in value]
    return value


def redact_text(value: str) -> str:
    text = str(value)
    home_candidates = {
        str(Path.home()),
        os.environ.get("USERPROFILE", ""),
        os.environ.get("HOME", ""),
    }
    for home in sorted(
        (item for item in home_candidates if item), key=len, reverse=True
    ):
        text = text.replace(home, "<HOME>")
        text = text.replace(home.replace("\\", "/"), "<HOME>")

    text = _WINDOWS_USER_PATH_RE.sub(r"\1<USER>", text)
    text = _POSIX_USER_PATH_RE.sub(r"\1<USER>", text)
    # Network share paths often contain patient names or accession numbers in
    # their directory components.  Remove the complete path before masking its
    # host so no component can survive in the support bundle.
    text = _UNC_PATH_RE.sub("<UNC_PATH>", text)
    text = _URL_HOST_RE.sub(r"\1<HOST>", text)
    text = _IPV4_RE.sub(_redact_ipv4, text)
    text = _IPV6_CANDIDATE_RE.sub(_redact_ipv6, text)
    text = _HOST_LABEL_RE.sub(r"\1<HOST>\3", text)
    text = _PACS_BARE_HOST_RE.sub(r"\1<HOST>\3", text)
    text = _INTERNAL_HOSTNAME_RE.sub("<HOST>", text)
    text = _SENSITIVE_LABEL_RE.sub(r"\1<REDACTED>", text)
    text = _SENSITIVE_BRACKET_LABEL_RE.sub(r"\1<REDACTED>\3", text)
    text = _SENSITIVE_PROSE_LABEL_RE.sub(r"\1<REDACTED>", text)
    text = _ACCESSION_COMMAND_RE.sub(r"\1<ACCESSION>", text)
    text = _START_ACCESSION_RE.sub(r"\1<ACCESSION>", text)
    text = _ACCESSION_LABEL_RE.sub(r"\1<ACCESSION>", text)
    text = _LOG_ACCESSION_RE.sub(r"\1<ACCESSION>", text)
    text = _LEADING_ACCESSION_RE.sub("<ACCESSION>", text)
    text = _DICOM_SENSITIVE_TAG_RE.sub(r"\1<REDACTED>\3", text)
    text = _WINDOWS_ABSOLUTE_PATH_RE.sub("<PATH>", text)
    text = _POSIX_ABSOLUTE_PATH_RE.sub("<PATH>", text)
    return _fail_closed_sensitive_lines(text)


def _redact_ipv4(match: re.Match[str]) -> str:
    parts = match.group(0).split(".")
    if all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
        return "<IP>"
    return match.group(0)


def _redact_ipv6(match: re.Match[str]) -> str:
    raw = match.group(0)
    bracketed = raw.startswith("[") and raw.endswith("]")
    candidate = raw[1:-1] if bracketed else raw
    if ":" not in candidate:
        return raw
    address = candidate.partition("%")[0]
    try:
        ipaddress.IPv6Address(address)
    except ipaddress.AddressValueError:
        return raw
    return "[<IPV6>]" if bracketed else "<IPV6>"


def _fail_closed_sensitive_lines(value: str) -> str:
    """Collapse an unrecognised sensitive-field format without leaking values.

    Exact redaction above preserves normal diagnostic messages.  This final
    pass handles unusual DCMTK/vendor formatting: if a known patient/accession
    marker is not immediately followed by one of our placeholders, only a safe
    log prefix and the field category are retained.
    """

    output: list[str] = []
    for line in value.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        newline = line[len(content) :]
        residual: re.Match[str] | None = None
        for match in _SENSITIVE_HINT_RE.finditer(content):
            if (
                match.start() > 0
                and content[match.start() - 1] == "<"
                and match.end() < len(content)
                and content[match.end()] == ">"
            ):
                continue
            if _SAFE_REDACTED_VALUE_RE.match(content[match.end() :]):
                continue
            residual = match
            break
        if residual is None:
            output.append(line)
            continue

        prefix_match = _SAFE_DIAGNOSTIC_PREFIX_RE.match(content)
        prefix = prefix_match.group(0) if prefix_match is not None else ""
        hint = residual.group(0).lower()
        if "accession" in hint or "0008" in hint or "检查号" in hint:
            category = "Accession"
        elif "id" in hint or "0020" in hint:
            category = "Patient ID"
        else:
            category = "Patient Name"
        output.append(f"{prefix}{category}: <REDACTED>{newline}")
    return "".join(output)


def _mask_identifier(value: str) -> str:
    text = str(value).strip()
    return "" if not text else f"<REDACTED:{len(text)}>"


def _redact_path(value: str) -> str:
    return "" if not str(value).strip() else "<PATH>"


def _diagnostic_files(directory: Path) -> list[Path]:
    if not directory.is_dir() or directory.is_symlink():
        return []
    found: dict[Path, Path] = {}
    for pattern in _DIAGNOSTIC_PATTERNS:
        for path in directory.glob(pattern):
            try:
                if path.is_file() and not path.is_symlink():
                    found[path.resolve()] = path
            except OSError:
                continue
    return sorted(
        found.values(),
        key=lambda path: (-_safe_mtime(path), path.name.casefold()),
    )


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _read_tail(path: Path, maximum: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - maximum))
        data = handle.read(maximum)
    prefix = b"[earlier log content omitted]\n" if size > maximum else b""
    return prefix + data


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
