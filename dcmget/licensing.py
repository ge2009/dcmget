from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from filelock import FileLock, Timeout


PRODUCT = "DcmGet"
TOKEN_PREFIX = "DGM1"
TRIAL_LIMIT = 30
TRIAL_STATE_VERSION = 3
LEGACY_TRIAL_STATE_VERSION = 1
TASK_ID_TRIAL_STATE_VERSION = 2
PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAS7ATBrKJ0C3xqN+ZrKDYr3QpKniXj/smdL5AkwyRHn4=
-----END PUBLIC KEY-----
"""


class LicenseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LicenseInfo:
    customer: str
    machine_code: str
    issued_on: date
    expires_on: date | None


@dataclass(frozen=True, slots=True)
class TrialInfo:
    used: int
    remaining: int


@dataclass(frozen=True, slots=True)
class _TrialState:
    used: int
    task_ids: frozenset[str]


def user_data_directory() -> Path:
    app_data = os.environ.get("APPDATA")
    if app_data:
        return Path(app_data) / PRODUCT
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / PRODUCT
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg_config) if xdg_config else Path.home() / ".config") / PRODUCT


def default_license_path() -> Path:
    return user_data_directory() / "license.lic"


def default_trial_path() -> Path:
    return user_data_directory() / "trial.json"


def default_trial_anchor_path() -> Path:
    if sys.platform == "win32":
        program_data = os.environ.get("PROGRAMDATA")
        if program_data:
            shared = Path(program_data) / PRODUCT
            if shared.is_dir():
                return shared / ".trial-anchor"
        base = Path(
            os.environ.get("LOCALAPPDATA", os.environ.get("APPDATA", Path.home()))
        )
        return base / PRODUCT / ".trial-anchor"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences" / ".com.dcmget.trial"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "dcmget" / "trial.json"


def machine_code() -> str:
    digest = hashlib.sha256(
        f"{PRODUCT}|{_machine_identifier()}".encode("utf-8", errors="replace")
    ).hexdigest()[:24]
    return _format_machine_code(digest)


def issue_license(
    private_key_path: str | Path,
    target_machine_code: str,
    customer: str,
    expires_on: date | None = None,
    issued_on: date | None = None,
) -> str:
    normalized_machine = normalize_machine_code(target_machine_code)
    customer_name = customer.strip()
    if not customer_name:
        raise LicenseError("客户名称不能为空")
    payload = {
        "customer": customer_name,
        "expires": expires_on.isoformat() if expires_on else "",
        "issued": (issued_on or date.today()).isoformat(),
        "machine": normalized_machine,
        "product": PRODUCT,
        "version": 1,
    }
    encoded_payload = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    key_data = Path(private_key_path).expanduser().read_bytes()
    private_key = serialization.load_pem_private_key(key_data, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise LicenseError("私钥不是 Ed25519 格式")
    signature = private_key.sign(encoded_payload)
    return f"{TOKEN_PREFIX}.{_b64encode(encoded_payload)}.{_b64encode(signature)}"


def validate_license(
    token: str,
    expected_machine_code: str | None = None,
    today: date | None = None,
    public_key_pem: bytes | None = None,
) -> LicenseInfo:
    compact = "".join(token.split())
    parts = compact.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise LicenseError("注册码格式不正确")
    try:
        payload_bytes = _b64decode(parts[1])
        signature = _b64decode(parts[2])
        public_key = serialization.load_pem_public_key(public_key_pem or PUBLIC_KEY_PEM)
        if not isinstance(public_key, Ed25519PublicKey):
            raise LicenseError("授权公钥格式不正确")
        public_key.verify(signature, payload_bytes)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (InvalidSignature, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise LicenseError("注册码签名无效") from exc

    if not isinstance(payload, dict) or payload.get("product") != PRODUCT:
        raise LicenseError("注册码不适用于 DcmGet")
    if payload.get("version") != 1:
        raise LicenseError("注册码版本不受支持")

    try:
        licensed_machine = normalize_machine_code(str(payload["machine"]))
        issued = date.fromisoformat(str(payload["issued"]))
        expires_value = str(payload.get("expires", ""))
        expires = date.fromisoformat(expires_value) if expires_value else None
    except (KeyError, ValueError) as exc:
        raise LicenseError("注册码内容不完整") from exc

    expected = normalize_machine_code(expected_machine_code or machine_code())
    if not hmac.compare_digest(licensed_machine, expected):
        raise LicenseError("注册码属于其他电脑")
    if expires and (today or date.today()) > expires:
        raise LicenseError(f"注册码已于 {expires.isoformat()} 到期")

    customer = str(payload.get("customer", "")).strip()
    if not customer:
        raise LicenseError("注册码缺少客户名称")
    return LicenseInfo(customer, licensed_machine, issued, expires)


def load_license(
    path: str | Path | None = None,
    expected_machine_code: str | None = None,
    today: date | None = None,
    public_key_pem: bytes | None = None,
) -> LicenseInfo:
    license_path = Path(path or default_license_path()).expanduser()
    if not license_path.is_file():
        raise LicenseError("软件尚未注册")
    return validate_license(
        license_path.read_text(encoding="utf-8-sig"),
        expected_machine_code,
        today,
        public_key_pem,
    )


def save_license(
    token: str,
    path: str | Path | None = None,
    expected_machine_code: str | None = None,
    public_key_pem: bytes | None = None,
) -> LicenseInfo:
    info = validate_license(
        token,
        expected_machine_code=expected_machine_code,
        public_key_pem=public_key_pem,
    )
    license_path = Path(path or default_license_path()).expanduser()
    license_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = license_path.with_suffix(license_path.suffix + ".tmp")
    temporary.write_text("".join(token.split()) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(license_path)
    try:
        license_path.chmod(0o600)
    except OSError:
        pass
    return info


def trial_status(
    path: str | Path | None = None,
    expected_machine_code: str | None = None,
) -> TrialInfo:
    expected = normalize_machine_code(expected_machine_code or machine_code())
    return _read_trial_status(_trial_paths(path), expected)


def consume_trial(
    path: str | Path | None = None,
    expected_machine_code: str | None = None,
    task_id: str | None = None,
) -> TrialInfo:
    expected = normalize_machine_code(expected_machine_code or machine_code())
    normalized_task_id = _normalize_trial_task_id(task_id)
    paths = _trial_paths(path)
    lock_path = paths[0].with_suffix(paths[0].suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(lock_path), timeout=10):
            current, consumed_task_ids = _read_trial_details(paths, expected)
            if normalized_task_id and normalized_task_id in consumed_task_ids:
                state = _trial_state(expected, current.used, consumed_task_ids)
                for state_path in paths:
                    _write_trial_state(state_path, state)
                return current
            if current.remaining <= 0:
                raise LicenseError("30 次免费试用已用完，请输入注册码")
            used = current.used + 1
            if normalized_task_id:
                consumed_task_ids.add(normalized_task_id)
            state = _trial_state(expected, used, consumed_task_ids)
            for state_path in paths:
                _write_trial_state(state_path, state)
    except Timeout as exc:
        raise LicenseError("试用计数正被其他进程使用，请稍后重试") from exc
    return TrialInfo(used, TRIAL_LIMIT - used)


def trial_task_consumed(
    task_id: str,
    path: str | Path | None = None,
    expected_machine_code: str | None = None,
) -> bool:
    expected = normalize_machine_code(expected_machine_code or machine_code())
    normalized_task_id = _normalize_trial_task_id(task_id)
    if not normalized_task_id:
        return False
    _current, consumed_task_ids = _read_trial_details(_trial_paths(path), expected)
    return normalized_task_id in consumed_task_ids


def _trial_paths(path: str | Path | None) -> list[Path]:
    if path is not None:
        return [Path(path).expanduser()]
    primary = default_trial_path().expanduser()
    anchor = default_trial_anchor_path().expanduser()
    return list(dict.fromkeys((primary, anchor)))


def _read_trial_status(paths: list[Path], expected_machine: str) -> TrialInfo:
    return _read_trial_details(paths, expected_machine)[0]


def _read_trial_details(
    paths: list[Path], expected_machine: str
) -> tuple[TrialInfo, set[str]]:
    states: list[_TrialState] = []
    for state_path in paths:
        if not state_path.is_file():
            continue
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8-sig"))
            used = int(raw["used"])
            stored_machine = normalize_machine_code(str(raw["machine"]))
            checksum = str(raw["checksum"])
            version = int(raw["version"])
            if version not in {
                LEGACY_TRIAL_STATE_VERSION,
                TASK_ID_TRIAL_STATE_VERSION,
                TRIAL_STATE_VERSION,
            }:
                raise ValueError("unsupported trial state")
            if version == TRIAL_STATE_VERSION:
                raw_task_ids = raw.get("task_ids", [])
                if not isinstance(raw_task_ids, list):
                    raise ValueError("invalid task ledger")
                task_ids = frozenset(
                    _normalize_trial_task_id(str(task_id))
                    for task_id in raw_task_ids
                )
                if "" in task_ids or len(task_ids) > TRIAL_LIMIT:
                    raise ValueError("invalid task ledger")
            elif version == TASK_ID_TRIAL_STATE_VERSION:
                task_id = _normalize_trial_task_id(str(raw.get("task_id", "")))
                task_ids = frozenset({task_id} if task_id else set())
            else:
                task_ids = frozenset()
            if not hmac.compare_digest(stored_machine, expected_machine):
                raise ValueError("trial state belongs to another machine")
            if not hmac.compare_digest(
                checksum, _trial_checksum(stored_machine, used, task_ids, version)
            ):
                raise ValueError("trial state checksum mismatch")
            if not 0 <= used <= TRIAL_LIMIT:
                raise ValueError("invalid trial count")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return TrialInfo(TRIAL_LIMIT, 0), set()
        states.append(_TrialState(used, task_ids))
    used = max((state.used for state in states), default=0)
    consumed_task_ids = {
        task_id
        for state in states
        if state.used == used
        for task_id in state.task_ids
    }
    return TrialInfo(used, TRIAL_LIMIT - used), consumed_task_ids


def _write_trial_state(state_path: Path, state: dict[str, object]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_name(
        f".{state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(
                state,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(state_path)
        try:
            state_path.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def normalize_machine_code(value: str) -> str:
    compact = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(compact) != 24:
        raise LicenseError("机器码应包含 24 个十六进制字符")
    return _format_machine_code(compact)


def _format_machine_code(value: str) -> str:
    upper = value.upper()
    return "-".join(upper[index : index + 6] for index in range(0, 24, 6))


def _trial_checksum(
    target_machine: str,
    used: int,
    task_ids: str | Iterable[str] = (),
    version: int = TRIAL_STATE_VERSION,
) -> str:
    canonical_task_ids = _canonical_trial_task_ids(task_ids)
    if version == LEGACY_TRIAL_STATE_VERSION:
        value = f"{PRODUCT}|trial|1|{target_machine}|{used}|DGM-30"
    elif version == TASK_ID_TRIAL_STATE_VERSION:
        task_id = canonical_task_ids[-1] if canonical_task_ids else ""
        value = f"{PRODUCT}|trial|2|{target_machine}|{used}|{task_id}|DGM-30"
    else:
        ledger = ",".join(canonical_task_ids)
        value = (
            f"{PRODUCT}|trial|{TRIAL_STATE_VERSION}|{target_machine}|{used}|"
            f"{ledger}|DGM-30"
        )
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _trial_state(
    target_machine: str,
    used: int,
    task_ids: Iterable[str],
) -> dict[str, object]:
    canonical_task_ids = _canonical_trial_task_ids(task_ids)
    return {
        "checksum": _trial_checksum(
            target_machine,
            used,
            canonical_task_ids,
            TRIAL_STATE_VERSION,
        ),
        "machine": target_machine,
        "task_ids": list(canonical_task_ids),
        "used": used,
        "version": TRIAL_STATE_VERSION,
    }


def _canonical_trial_task_ids(
    task_ids: str | Iterable[str],
) -> tuple[str, ...]:
    values = (task_ids,) if isinstance(task_ids, str) else task_ids
    return tuple(
        sorted(
            {
                normalized
                for value in values
                if (normalized := _normalize_trial_task_id(str(value)))
            }
        )
    )


def _normalize_trial_task_id(task_id: str | None) -> str:
    value = str(task_id or "").strip().lower()
    if not value:
        return ""
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise LicenseError("试用任务标识格式不正确")
    return value


def _machine_identifier() -> str:
    system = platform.system().lower()
    if system == "windows":
        try:
            import winreg

            access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                access,
            ) as key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except (ImportError, OSError):
            pass
    elif system == "darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', result.stdout)
            if match:
                return match.group(1)
        except (OSError, subprocess.SubprocessError):
            pass
    elif system == "linux":
        for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                value = path.read_text(encoding="ascii").strip()
                if value:
                    return value
            except OSError:
                continue
    return f"{platform.node()}|{uuid.getnode():012x}"


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
