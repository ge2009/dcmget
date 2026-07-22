from __future__ import annotations

import json
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from filelock import FileLock, Timeout

from .config import AppConfig, load_config, save_config
from .runtime import application_state_dir, default_config_path


PROFILE_METADATA_NAME = "profile-meta.json"
PROFILE_METADATA_SCHEMA = "dcmget-profile-meta"
PROFILE_METADATA_VERSION = 1
PROFILE_DIRECTORY_NAME = "instances"
ALLOCATION_LOCK_NAME = ".allocate.lock"
MIN_RECOMMENDED_PORT = 1024
MAX_PROFILE_NUMBER = 9999
MAX_DISPLAY_NAME_LENGTH = 80
MAX_METADATA_BYTES = 16 * 1024
WINDOWS_MANAGEMENT_PORT = 8786
RESERVED_PROFILE_PORTS = frozenset({WINDOWS_MANAGEMENT_PORT})

_PROFILE_DIRECTORY = re.compile(r"i([1-9][0-9]{0,3})")


class ProfileManagerError(RuntimeError):
    pass


class ProfileNotFoundError(ProfileManagerError):
    pass


class ProfileInUseError(ProfileManagerError):
    pass


class ProfileRecoveryExistsError(ProfileManagerError):
    pass


@dataclass(frozen=True, slots=True)
class ProfileInfo:
    number: int
    display_name: str
    config_path: Path
    pacs_server_ip: str
    pacs_server_port: int
    calling_ae_title: str
    pacs_ae_title: str
    storage_ae_title: str
    storage_port: int
    web_port: int
    destination_directory: str
    is_running: bool
    has_recovery: bool


@dataclass(frozen=True, slots=True)
class ProfileCloneResult:
    source_number: int
    recommended_port: int
    recommended_web_port: int
    profile: ProfileInfo


def read_profile_display_name(
    config_path: str | Path,
    profile_number: int | str,
) -> str:
    """Read a profile label without acquiring its running-instance lock."""

    number = _profile_number(profile_number)
    path = Path(config_path).expanduser().with_name(PROFILE_METADATA_NAME)
    if not path.exists():
        return f"实例 {number}"
    if path.is_symlink() or not path.is_file():
        raise ProfileManagerError(f"Profile 元数据文件不安全：{path}")
    try:
        data = path.read_bytes()
        if len(data) > MAX_METADATA_BYTES:
            raise ProfileManagerError(f"实例 {number} 的 Profile 元数据文件过大")
        raw = json.loads(data.decode("utf-8-sig"))
    except ProfileManagerError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileManagerError(
            f"无法读取实例 {number} 的 Profile 元数据：{exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ProfileManagerError(f"实例 {number} 的 Profile 元数据格式无效")
    if (
        raw.get("schema") != PROFILE_METADATA_SCHEMA
        or raw.get("version") != PROFILE_METADATA_VERSION
    ):
        raise ProfileManagerError(f"实例 {number} 的 Profile 元数据版本不受支持")
    return _display_name(raw.get("display_name"))


class ProfileManager:
    """Manage persistent, independently launchable DcmGet profiles."""

    def __init__(
        self,
        *,
        config_root: str | Path | None = None,
        state_root: str | Path | None = None,
        bind_host: str = "0.0.0.0",
        lock_timeout: float = 10,
        port_probe: Callable[[str, int], bool] | None = None,
    ) -> None:
        self.config_root = Path(
            config_root or default_config_path().parent
        ).expanduser().resolve()
        self.state_root = Path(
            state_root or application_state_dir()
        ).expanduser().resolve()
        self.config_profiles = self.config_root / PROFILE_DIRECTORY_NAME
        self.state_profiles = self.state_root / PROFILE_DIRECTORY_NAME
        self.bind_host = str(bind_host).strip() or "0.0.0.0"
        self.lock_timeout = float(lock_timeout)
        self._port_probe = port_probe or _port_is_available
        self._validate_root_child(self.config_profiles, self.config_root)
        self._validate_root_child(self.state_profiles, self.state_root)

    def list_profiles(self) -> tuple[ProfileInfo, ...]:
        if not self.config_profiles.exists():
            return ()
        self._validate_directory(self.config_profiles, self.config_root)
        profiles: list[ProfileInfo] = []
        for directory in sorted(
            self.config_profiles.iterdir(), key=_profile_sort_key
        ):
            match = _PROFILE_DIRECTORY.fullmatch(directory.name)
            if not match:
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise ProfileManagerError(
                    f"Profile 目录不安全：{directory}"
                )
            number = int(match.group(1))
            config_path = self._config_path(number)
            if config_path.is_file():
                profiles.append(self._profile_info(number))
        return tuple(profiles)

    def get_profile(self, profile_number: int | str) -> ProfileInfo:
        return self._profile_info(_profile_number(profile_number))

    def recommend_available_port(
        self,
        starting_port: int = 6666,
        *,
        excluded_ports: tuple[int, ...] = (),
    ) -> int:
        try:
            start = int(starting_port)
        except (TypeError, ValueError) as exc:
            raise ProfileManagerError("起始端口必须在 1024 到 65535 之间") from exc
        if (
            isinstance(starting_port, bool)
            or not MIN_RECOMMENDED_PORT <= start <= 65535
        ):
            raise ProfileManagerError("起始端口必须在 1024 到 65535 之间")

        used_ports = {
            port
            for profile in self.list_profiles()
            for port in (profile.storage_port, profile.web_port)
        }
        used_ports.update(RESERVED_PROFILE_PORTS)
        used_ports.update(int(port) for port in excluded_ports)
        candidates = range(start, 65536)
        wrapped = range(MIN_RECOMMENDED_PORT, start)
        for candidate in (*candidates, *wrapped):
            if candidate in used_ports:
                continue
            try:
                available = bool(self._port_probe(self.bind_host, candidate))
            except OSError:
                available = False
            if available:
                return candidate
        raise ProfileManagerError("没有可用的接收端口")

    def create_profile(
        self,
        *,
        display_name: str | None = None,
    ) -> ProfileInfo:
        """Create one stopped Profile with non-conflicting default ports."""

        allocation_lock = self._acquire_allocation_lock()
        target_lock: FileLock | None = None
        target_number: int | None = None
        completed = False
        try:
            target_number = self._next_profile_number()
            target_lock = self._acquire_profile_lock(target_number)
            storage_port = self.recommend_available_port(6666)
            web_port = self.recommend_available_port(
                8787,
                excluded_ports=(storage_port,),
            )
            config = replace(
                AppConfig(),
                storage_port=storage_port,
                web_port=web_port,
            )
            name = _display_name(
                display_name if display_name is not None else f"实例 {target_number}"
            )
            target_config = self._config_path(target_number)
            target_metadata = self._metadata_path(target_number)
            if target_config.exists() or target_metadata.exists():
                raise ProfileManagerError(
                    f"实例 {target_number} 的配置已存在，无法创建"
                )
            target_config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._validate_directory(target_config.parent, self.config_root)
            save_config(target_config, config)
            _make_private(target_config)
            self._write_metadata(target_number, name)
            completed = True
        except Exception as exc:
            if target_number is not None:
                self._remove_failed_clone(target_number)
            if isinstance(exc, ProfileManagerError):
                raise
            raise ProfileManagerError(f"创建 Profile 失败：{exc}") from exc
        finally:
            if target_lock is not None and target_lock.is_locked:
                target_lock.release()
            if target_number is not None and not completed and target_lock is not None:
                self._remove_unused_lock_file(target_number)
            allocation_lock.release()

        assert target_number is not None
        return self.get_profile(target_number)

    def clone_profile(
        self,
        source_profile_number: int | str,
        *,
        display_name: str | None = None,
    ) -> ProfileCloneResult:
        source_number = _profile_number(source_profile_number)
        allocation_lock = self._acquire_allocation_lock()
        target_lock: FileLock | None = None
        target_number: int | None = None
        completed = False
        try:
            source_config = self._load_profile_config(source_number)
            target_number = self._next_profile_number()
            target_lock = self._acquire_profile_lock(target_number)
            starting_port = (
                source_config.storage_port + 1
                if source_config.storage_port < 65535
                else MIN_RECOMMENDED_PORT
            )
            recommended_port = self.recommend_available_port(starting_port)
            starting_web_port = (
                source_config.web_port + 1
                if source_config.web_port < 65535
                else MIN_RECOMMENDED_PORT
            )
            recommended_web_port = self.recommend_available_port(
                starting_web_port,
                excluded_ports=(recommended_port,),
            )
            cloned_config = replace(
                source_config,
                storage_port=recommended_port,
                web_port=recommended_web_port,
            )
            name = _display_name(
                display_name if display_name is not None else f"实例 {target_number}"
            )
            target_config = self._config_path(target_number)
            target_metadata = self._metadata_path(target_number)
            if target_config.exists() or target_metadata.exists():
                raise ProfileManagerError(
                    f"实例 {target_number} 的配置已存在，无法克隆"
                )
            target_config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._validate_directory(target_config.parent, self.config_root)
            save_config(target_config, cloned_config)
            _make_private(target_config)
            self._write_metadata(target_number, name)
            completed = True
        except Exception as exc:
            if target_number is not None:
                self._remove_failed_clone(target_number)
            if isinstance(exc, ProfileManagerError):
                raise
            raise ProfileManagerError(f"克隆 Profile 失败：{exc}") from exc
        finally:
            if target_lock is not None and target_lock.is_locked:
                target_lock.release()
            if target_number is not None and not completed and target_lock is not None:
                self._remove_unused_lock_file(target_number)
            allocation_lock.release()

        assert target_number is not None
        profile = self.get_profile(target_number)
        return ProfileCloneResult(
            source_number=source_number,
            recommended_port=profile.storage_port,
            recommended_web_port=profile.web_port,
            profile=profile,
        )

    def rename_profile(
        self,
        profile_number: int | str,
        display_name: str,
    ) -> ProfileInfo:
        number = _profile_number(profile_number)
        normalized = _display_name(display_name)
        allocation_lock = self._acquire_allocation_lock()
        try:
            self._require_config_path(number)
            self._write_metadata(number, normalized)
        finally:
            allocation_lock.release()
        return self.get_profile(number)

    def update_profile(
        self,
        profile_number: int | str,
        *,
        display_name: str | None = None,
        pacs_server_ip: str | None = None,
        pacs_server_port: int | None = None,
        calling_ae_title: str | None = None,
        pacs_ae_title: str | None = None,
        storage_ae_title: str | None = None,
        storage_port: int | None = None,
        web_port: int | None = None,
        dicom_destination_folder: str | None = None,
    ) -> ProfileInfo:
        """Update one stopped profile after validating all reserved ports."""

        number = _profile_number(profile_number)
        normalized_name = (
            _display_name(display_name) if display_name is not None else None
        )
        changes: dict[str, object] = {}
        for field, value in (
            ("pacs_server_ip", pacs_server_ip),
            ("calling_ae_title", calling_ae_title),
            ("pacs_ae_title", pacs_ae_title),
            ("storage_ae_title", storage_ae_title),
            ("dicom_destination_folder", dicom_destination_folder),
        ):
            if value is not None:
                if not isinstance(value, str):
                    raise ProfileManagerError(f"{field} 必须是文本")
                changes[field] = value.strip()
        for field, value in (
            ("pacs_server_port", pacs_server_port),
            ("storage_port", storage_port),
            ("web_port", web_port),
        ):
            if value is not None:
                changes[field] = _port_number(value, field)

        allocation_lock = self._acquire_allocation_lock()
        profile_lock: FileLock | None = None
        previous: AppConfig | None = None
        previous_name = ""
        metadata_existed = False
        config_saved = False
        try:
            try:
                profile_lock = self._acquire_profile_lock(number)
            except ProfileInUseError:
                raise ProfileInUseError(
                    f"实例 {number} 正在运行，请先停止后再修改配置"
                ) from None
            previous = self._load_profile_config(number)
            updated = replace(previous, **changes)
            errors = updated.validate()
            if errors:
                detail = "；".join(dict.fromkeys(errors.values()))
                raise ProfileManagerError(f"实例 {number} 配置校验失败：{detail}")
            self._validate_config_ports(
                number,
                updated,
                check_system_ports=True,
            )

            metadata_path = self._metadata_path(number)
            metadata_existed = metadata_path.is_file()
            previous_name = self._read_display_name(number)
            save_config(self._config_path(number), updated)
            _make_private(self._config_path(number))
            config_saved = True
            if normalized_name is not None:
                self._write_metadata(number, normalized_name)
        except Exception as exc:
            rollback_errors: list[str] = []
            if config_saved and previous is not None:
                try:
                    save_config(self._config_path(number), previous)
                    _make_private(self._config_path(number))
                except Exception as rollback_exc:
                    rollback_errors.append(f"配置回滚失败：{rollback_exc}")
                if normalized_name is not None:
                    try:
                        if metadata_existed:
                            self._write_metadata(number, previous_name)
                        else:
                            self._metadata_path(number).unlink(missing_ok=True)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"名称回滚失败：{rollback_exc}")
            if isinstance(exc, ProfileManagerError) and not rollback_errors:
                raise
            detail = f"保存实例 {number} 配置失败：{exc}"
            if rollback_errors:
                detail += "；" + "；".join(rollback_errors)
            raise ProfileManagerError(detail) from exc
        finally:
            if profile_lock is not None and profile_lock.is_locked:
                profile_lock.release()
            allocation_lock.release()
        return self.get_profile(number)

    def validate_profile_ports(
        self,
        profile_number: int | str,
        *,
        check_system_ports: bool = False,
    ) -> None:
        """Raise a Chinese actionable error for duplicate or occupied ports."""

        number = _profile_number(profile_number)
        allocation_lock = self._acquire_allocation_lock()
        try:
            config = self._load_profile_config(number)
            self._validate_config_ports(
                number,
                config,
                check_system_ports=check_system_ports,
            )
        finally:
            allocation_lock.release()

    def profile_stop_blockers(self, profile_number: int | str) -> tuple[str, ...]:
        """Return processes or ports that prove a Profile is not fully stopped."""

        profile = self.get_profile(profile_number)
        blockers: list[str] = []
        if profile.is_running:
            blockers.append("Profile 后台进程")
        for label, port in (
            ("DICOM 接收端口", profile.storage_port),
            ("Web 端口", profile.web_port),
        ):
            try:
                available = bool(self._port_probe(self.bind_host, port))
            except OSError:
                available = False
            if not available:
                blockers.append(f"{label} {port}")
        return tuple(blockers)

    def wait_for_profile_stopped(
        self,
        profile_number: int | str,
        *,
        timeout: float = 45.0,
        poll_interval: float = 0.1,
    ) -> tuple[bool, tuple[str, ...]]:
        """Wait until the Profile lock and both local listening ports are free."""

        deadline = time.monotonic() + max(0.0, float(timeout))
        blockers: tuple[str, ...] = ()
        while True:
            blockers = self.profile_stop_blockers(profile_number)
            if not blockers:
                return True, ()
            if time.monotonic() >= deadline:
                return False, blockers
            time.sleep(min(max(0.0, poll_interval), max(0.0, deadline - time.monotonic())))

    def delete_profile(self, profile_number: int | str) -> None:
        number = _profile_number(profile_number)
        allocation_lock = self._acquire_allocation_lock()
        profile_lock: FileLock | None = None
        config_lock: FileLock | None = None
        try:
            config_path = self._require_config_path(number)
            try:
                profile_lock = self._acquire_profile_lock(number)
            except ProfileInUseError:
                raise ProfileInUseError(f"实例 {number} 正在运行，不能删除") from None
            recovery_path = self._recovery_path(number)
            if recovery_path.is_file() or any(
                recovery_path.parent.glob(f"{recovery_path.name}-*")
            ):
                raise ProfileRecoveryExistsError(
                    f"实例 {number} 存在未完成任务恢复点，不能删除"
                )

            config_lock = FileLock(str(config_path.with_name("config.json.lock")))
            try:
                config_lock.acquire(timeout=self.lock_timeout)
            except Timeout as exc:
                raise ProfileManagerError(
                    f"实例 {number} 的配置正在被使用，请稍后重试"
                ) from exc
            metadata_path = self._metadata_path(number)
            self._transactional_remove((metadata_path, config_path))
        finally:
            if config_lock is not None and config_lock.is_locked:
                config_lock.release()
            if profile_lock is not None and profile_lock.is_locked:
                profile_lock.release()
            allocation_lock.release()

    def _profile_info(self, number: int) -> ProfileInfo:
        config = self._load_profile_config(number)
        config_path = self._config_path(number)
        return ProfileInfo(
            number=number,
            display_name=self._read_display_name(number),
            config_path=config_path,
            pacs_server_ip=config.pacs_server_ip,
            pacs_server_port=config.pacs_server_port,
            calling_ae_title=config.calling_ae_title,
            pacs_ae_title=config.pacs_ae_title,
            storage_ae_title=config.storage_ae_title,
            storage_port=config.storage_port,
            web_port=config.web_port,
            destination_directory=config.dicom_destination_folder,
            is_running=self._profile_is_running(number),
            has_recovery=self._recovery_path(number).is_file(),
        )

    def _validate_config_ports(
        self,
        number: int,
        config: AppConfig,
        *,
        check_system_ports: bool,
    ) -> None:
        ports = (
            ("DICOM 接收端口", config.storage_port),
            ("Web 端口", config.web_port),
        )
        for label, port in ports:
            if isinstance(port, bool) or not 1 <= int(port) <= 65535:
                raise ProfileManagerError(
                    f"实例 {number} 的{label}必须在 1 到 65535 之间"
                )
            if int(port) in RESERVED_PROFILE_PORTS:
                raise ProfileManagerError(
                    f"实例 {number} 的{label} {port} 是 Windows 管理中心保留端口，"
                    "请改用其他端口"
                )
        if config.storage_port == config.web_port:
            raise ProfileManagerError(
                f"实例 {number} 的 DICOM 接收端口 {config.storage_port} "
                "不能与 Web 端口相同"
            )
        for profile in self.list_profiles():
            if profile.number == number:
                continue
            other_ports = (
                ("DICOM 接收端口", profile.storage_port),
                ("Web 端口", profile.web_port),
            )
            for label, port in ports:
                for other_label, other_port in other_ports:
                    if port == other_port:
                        raise ProfileManagerError(
                            f"实例 {number} 的{label} {port} 与实例 "
                            f"{profile.number} 的{other_label}冲突，请修改后再启动"
                        )
        if not check_system_ports:
            return
        for label, port in ports:
            try:
                available = bool(self._port_probe(self.bind_host, port))
            except OSError:
                available = False
            if not available:
                raise ProfileManagerError(
                    f"实例 {number} 的{label} {port} 已被其他程序占用，"
                    "请修改后再启动"
                )

    def _load_profile_config(self, number: int) -> AppConfig:
        config_path = self._require_config_path(number)
        config_lock = FileLock(str(config_path.with_name("config.json.lock")))
        try:
            config_lock.acquire(timeout=self.lock_timeout)
            return load_config(config_path)
        except Timeout as exc:
            raise ProfileManagerError(
                f"实例 {number} 的配置正在被使用，请稍后重试"
            ) from exc
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ProfileManagerError(
                f"无法读取实例 {number} 的配置：{exc}"
            ) from exc
        finally:
            if config_lock.is_locked:
                config_lock.release()

    def _read_display_name(self, number: int) -> str:
        return read_profile_display_name(self._config_path(number), number)

    def _write_metadata(self, number: int, display_name: str) -> None:
        path = self._metadata_path(number)
        if path.is_symlink():
            raise ProfileManagerError(f"Profile 元数据文件不安全：{path}")
        payload = {
            "schema": PROFILE_METADATA_SCHEMA,
            "version": PROFILE_METADATA_VERSION,
            "display_name": _display_name(display_name),
        }
        _atomic_write_json(path, payload)

    def _profile_is_running(self, number: int) -> bool:
        self.state_profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._validate_directory(self.state_profiles, self.state_root)
        lock = FileLock(str(self._profile_lock_path(number)))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            return True
        else:
            lock.release()
            return False

    def _acquire_allocation_lock(self) -> FileLock:
        self.state_profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._validate_directory(self.state_profiles, self.state_root)
        lock = FileLock(str(self.state_profiles / ALLOCATION_LOCK_NAME))
        try:
            lock.acquire(timeout=self.lock_timeout)
        except Timeout as exc:
            raise ProfileManagerError("等待 Profile 管理锁超时") from exc
        return lock

    def _acquire_profile_lock(self, number: int) -> FileLock:
        lock = FileLock(str(self._profile_lock_path(number)))
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise ProfileInUseError(f"实例 {number} 正在运行") from exc
        return lock

    def _next_profile_number(self) -> int:
        known: set[int] = set()
        for root in (self.config_profiles, self.state_profiles):
            if not root.exists():
                continue
            self._validate_directory(
                root,
                self.config_root if root == self.config_profiles else self.state_root,
            )
            for path in root.iterdir():
                name = path.name[:-5] if path.name.endswith(".lock") else path.name
                match = _PROFILE_DIRECTORY.fullmatch(name)
                if match:
                    known.add(int(match.group(1)))
        for number in range(1, MAX_PROFILE_NUMBER + 1):
            if number not in known:
                return number
        raise ProfileManagerError("Profile 数量已达到上限")

    def _require_config_path(self, number: int) -> Path:
        path = self._config_path(number)
        if path.is_symlink():
            raise ProfileManagerError(f"Profile 配置文件不安全：{path}")
        if not path.is_file():
            raise ProfileNotFoundError(f"实例 {number} 不存在")
        return path

    def _config_path(self, number: int) -> Path:
        path = self.config_profiles / f"i{number}" / "config.json"
        self._validate_root_child(path, self.config_root)
        return path

    def _metadata_path(self, number: int) -> Path:
        path = self.config_profiles / f"i{number}" / PROFILE_METADATA_NAME
        self._validate_root_child(path, self.config_root)
        return path

    def _profile_lock_path(self, number: int) -> Path:
        path = self.state_profiles / f"i{number}.lock"
        self._validate_root_child(path, self.state_root)
        return path

    def _recovery_path(self, number: int) -> Path:
        path = self.state_profiles / f"i{number}" / "active-task.sqlite3"
        self._validate_root_child(path, self.state_root)
        return path

    def _remove_failed_clone(self, number: int) -> None:
        for path in (self._metadata_path(number), self._config_path(number)):
            try:
                if not path.is_symlink():
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    def _remove_unused_lock_file(self, number: int) -> None:
        try:
            self._profile_lock_path(number).unlink(missing_ok=True)
        except OSError:
            pass

    def _transactional_remove(self, paths: tuple[Path, ...]) -> None:
        renamed: list[tuple[Path, Path]] = []
        try:
            for path in paths:
                if not path.exists():
                    continue
                if path.is_symlink():
                    raise ProfileManagerError(f"Profile 文件不安全：{path}")
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.delete")
                os.replace(path, temporary)
                renamed.append((path, temporary))
        except Exception as exc:
            rollback_errors: list[str] = []
            for original, temporary in reversed(renamed):
                try:
                    os.replace(temporary, original)
                except OSError as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            detail = f"删除 Profile 配置失败：{exc}"
            if rollback_errors:
                detail += "；回滚失败：" + "；".join(rollback_errors)
            raise ProfileManagerError(detail) from exc
        for _original, temporary in renamed:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                raise ProfileManagerError(f"清理已删除 Profile 失败：{exc}") from exc

    @staticmethod
    def _validate_root_child(path: Path, root: Path) -> None:
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
        except ValueError as exc:
            raise ProfileManagerError(f"Profile 路径超出允许目录：{path}") from exc

    @classmethod
    def _validate_directory(cls, path: Path, root: Path) -> None:
        cls._validate_root_child(path, root)
        if path.is_symlink() or not path.is_dir():
            raise ProfileManagerError(f"Profile 目录不安全：{path}")


def _profile_number(value: int | str) -> int:
    if isinstance(value, bool) or not re.fullmatch(
        r"\+?[1-9][0-9]{0,3}", str(value).strip()
    ):
        raise ProfileManagerError("实例编号必须在 1 到 9999 之间")
    number = int(value)
    if not 1 <= number <= MAX_PROFILE_NUMBER:
        raise ProfileManagerError("实例编号必须在 1 到 9999 之间")
    return number


def _display_name(value: object) -> str:
    name = str(value if value is not None else "").strip()
    if not name:
        raise ProfileManagerError("Profile 显示名不能为空")
    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        raise ProfileManagerError(
            f"Profile 显示名不能超过 {MAX_DISPLAY_NAME_LENGTH} 个字符"
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in name):
        raise ProfileManagerError("Profile 显示名不能包含控制字符")
    return name


def _port_number(value: object, field: str) -> int:
    labels = {
        "pacs_server_port": "PACS 端口",
        "storage_port": "DICOM 接收端口",
        "web_port": "Web 端口",
    }
    label = labels.get(field, "端口")
    if isinstance(value, bool):
        raise ProfileManagerError(f"{label}必须在 1 到 65535 之间")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ProfileManagerError(f"{label}必须在 1 到 65535 之间") from exc
    if not 1 <= port <= 65535:
        raise ProfileManagerError(f"{label}必须在 1 到 65535 之间")
    return port


def _profile_sort_key(path: Path) -> tuple[int, str]:
    match = _PROFILE_DIRECTORY.fullmatch(path.name)
    return (int(match.group(1)) if match else MAX_PROFILE_NUMBER + 1, path.name)


def _port_is_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind((host, int(port)))
            probe.listen(1)
        except OSError:
            return False
    return True


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _make_private(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _make_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass
