from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from filelock import FileLock, Timeout


PROFILE_RUNTIME_SCHEMA = "dcmget-profile-runtime"
PROFILE_RUNTIME_VERSION = 1
PROFILE_RUNTIME_FILE_NAME = "profile-runtime.json"
MAX_RUNTIME_STATE_BYTES = 64 * 1024


class ProfileRuntimeStateError(RuntimeError):
    """Raised when the management runtime state cannot be trusted or saved."""


class ProfileRuntimeState:
    """Persist the Profiles the Windows service should keep running.

    Configuration files describe *how* a Profile runs.  This separate state
    file describes whether the operator wants it running, so newly created
    Profiles remain stopped while an explicitly started Profile survives a
    service or Windows restart.
    """

    def __init__(self, path: str | Path, *, lock_timeout: float = 10.0) -> None:
        source = Path(path).expanduser()
        self.path = source if source.is_absolute() else Path.cwd() / source
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.lock_timeout = float(lock_timeout)

    def desired_profiles(self) -> tuple[int, ...]:
        lock = self._acquire_lock()
        try:
            return self._read_unlocked()
        finally:
            lock.release()

    def is_desired(self, profile_number: int | str) -> bool:
        number = _profile_number(profile_number)
        return number in self.desired_profiles()

    def set_desired(
        self,
        profile_number: int | str,
        desired: bool,
    ) -> tuple[int, ...]:
        number = _profile_number(profile_number)
        if not isinstance(desired, bool):
            raise TypeError("desired 必须是布尔值")
        lock = self._acquire_lock()
        try:
            values = set(self._read_unlocked())
            if desired:
                values.add(number)
            else:
                values.discard(number)
            result = tuple(sorted(values))
            self._write_unlocked(result)
            return result
        finally:
            lock.release()

    def remove(self, profile_number: int | str) -> tuple[int, ...]:
        return self.set_desired(profile_number, False)

    def _acquire_lock(self) -> FileLock:
        self._ensure_parent()
        if self.lock_path.is_symlink():
            raise ProfileRuntimeStateError(
                f"Profile 运行状态锁文件不安全：{self.lock_path}"
            )
        lock = FileLock(str(self.lock_path))
        try:
            lock.acquire(timeout=self.lock_timeout)
        except Timeout as exc:
            raise ProfileRuntimeStateError(
                "Profile 运行状态正在被使用，请稍后重试"
            ) from exc
        return lock

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            raise ProfileRuntimeStateError(
                f"Profile 运行状态目录不安全：{self.path.parent}"
            )
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass

    def _read_unlocked(self) -> tuple[int, ...]:
        if not self.path.exists():
            return ()
        if self.path.is_symlink() or not self.path.is_file():
            raise ProfileRuntimeStateError(
                f"Profile 运行状态文件不安全：{self.path}"
            )
        try:
            size = self.path.stat().st_size
            if size > MAX_RUNTIME_STATE_BYTES:
                raise ProfileRuntimeStateError("Profile 运行状态文件过大")
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except ProfileRuntimeStateError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProfileRuntimeStateError(f"无法读取 Profile 运行状态：{exc}") from exc
        if not isinstance(raw, dict):
            raise ProfileRuntimeStateError("Profile 运行状态格式无效")
        if (
            raw.get("schema") != PROFILE_RUNTIME_SCHEMA
            or raw.get("version") != PROFILE_RUNTIME_VERSION
        ):
            raise ProfileRuntimeStateError("Profile 运行状态版本不受支持")
        values = raw.get("desired_running_profiles")
        if not isinstance(values, list):
            raise ProfileRuntimeStateError("Profile 运行状态列表无效")
        try:
            normalized = tuple(sorted({_profile_number(value) for value in values}))
        except (TypeError, ValueError) as exc:
            raise ProfileRuntimeStateError("Profile 运行状态列表无效") from exc
        return normalized

    def _write_unlocked(self, values: tuple[int, ...]) -> None:
        if self.path.is_symlink():
            raise ProfileRuntimeStateError(
                f"拒绝写入符号链接：{self.path}"
            )
        payload = {
            "schema": PROFILE_RUNTIME_SCHEMA,
            "version": PROFILE_RUNTIME_VERSION,
            "desired_running_profiles": list(values),
        }
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOINHERIT", 0)
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            raise ProfileRuntimeStateError(f"无法保存 Profile 运行状态：{exc}") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _profile_number(value: int | str) -> int:
    if isinstance(value, bool):
        raise ValueError("Profile 编号必须在 1 到 9999 之间")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Profile 编号必须在 1 到 9999 之间") from exc
    if not 1 <= number <= 9999:
        raise ValueError("Profile 编号必须在 1 到 9999 之间")
    return number
