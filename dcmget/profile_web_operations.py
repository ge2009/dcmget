from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from .instance_shortcut import (
    InstanceLaunchCommand,
    build_instance_launch_command,
    create_instance_shortcut,
    default_instance_shortcut_name,
)
from .profile_manager import ProfileCloneResult, ProfileInfo, ProfileManager
from .runtime import is_frozen, resource_root


JsonDict = dict[str, object]


class ProfileWebOperations:
    """Thin JSON-safe adapter around :class:`ProfileManager`."""

    def __init__(
        self,
        *,
        manager: ProfileManager | None = None,
        project_root: str | Path | None = None,
        executable: str | Path | None = None,
        frozen: bool | None = None,
        desktop_directory: str | Path | None = None,
        popen: Callable[..., subprocess.Popen[Any]] | None = None,
    ) -> None:
        self.manager = manager or ProfileManager()
        self.project_root = (
            Path(project_root).expanduser().resolve()
            if project_root is not None
            else resource_root()
        )
        self.executable = (
            Path(executable).expanduser().resolve()
            if executable is not None
            else Path(sys_executable()).resolve()
        )
        self.frozen = is_frozen() if frozen is None else bool(frozen)
        self.desktop_directory = (
            Path(desktop_directory).expanduser().resolve()
            if desktop_directory is not None
            else _default_desktop_directory()
        )
        self._popen = popen or subprocess.Popen

    def handlers(self) -> dict[str, Callable[[object], JsonDict]]:
        return {
            "profile-list": self.list_profiles,
            "profile-clone": self.clone_profile,
            "profile-rename": self.rename_profile,
            "profile-delete": self.delete_profile,
            "profile-launch": self.launch_profile,
            "profile-shortcut": self.create_shortcut,
        }

    def list_profiles(self, _payload: object = None) -> JsonDict:
        profiles = [self._serialize_profile(item) for item in self.manager.list_profiles()]
        return {"profiles": profiles, "count": len(profiles)}

    def clone_profile(self, payload: object) -> JsonDict:
        body = _require_mapping(payload)
        source_profile_number = _require_profile_number(body, "source_profile_number")
        display_name = _optional_display_name(body, "display_name")
        result = self.manager.clone_profile(
            source_profile_number,
            display_name=display_name,
        )
        return self._serialize_clone_result(result)

    def rename_profile(self, payload: object) -> JsonDict:
        body = _require_mapping(payload)
        profile_number = _require_profile_number(body, "profile_number")
        display_name = _require_display_name(body, "display_name")
        profile = self.manager.rename_profile(profile_number, display_name)
        return {"ok": True, "profile": self._serialize_profile(profile)}

    def delete_profile(self, payload: object) -> JsonDict:
        body = _require_mapping(payload)
        profile_number = _require_profile_number(body, "profile_number")
        self.manager.delete_profile(profile_number)
        return {"ok": True, "deleted_profile_number": profile_number}

    def launch_profile(self, payload: object) -> JsonDict:
        body = _require_mapping(payload)
        profile_number = _require_profile_number(body, "profile_number")
        profile = self.manager.get_profile(profile_number)
        launch = build_instance_launch_command(
            profile.number,
            project_root=self.project_root,
            executable=self.executable,
            frozen=self.frozen,
        )
        process = self._popen(
            [str(launch.target), *launch.arguments],
            cwd=str(launch.working_directory),
            shell=False,
            close_fds=(os.name != "nt"),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {
            "ok": True,
            "profile": self._serialize_profile(profile),
            "pid": int(process.pid),
            "launch": self._serialize_launch(launch),
        }

    def create_shortcut(self, payload: object) -> JsonDict:
        body = _require_mapping(payload)
        profile_number = _require_profile_number(body, "profile_number")
        profile = self.manager.get_profile(profile_number)
        overwrite = _optional_bool(body, "overwrite", default=False)
        destination = _optional_path(
            body,
            "destination_directory",
            default=self.desktop_directory,
        )
        shortcut_name = _optional_shortcut_name(
            body,
            "name",
            default=default_instance_shortcut_name(
                profile.storage_port,
                profile.storage_ae_title,
            ),
        )
        shortcut_path = create_instance_shortcut(
            profile.number,
            shortcut_name,
            destination,
            project_root=self.project_root,
            executable=self.executable,
            frozen=self.frozen,
            overwrite=overwrite,
        )
        return {
            "ok": True,
            "profile": self._serialize_profile(profile),
            "shortcut": {
                "path": str(shortcut_path),
                "name": shortcut_path.name,
                "destination_directory": str(shortcut_path.parent),
                "overwrite": overwrite,
            },
        }

    @staticmethod
    def _serialize_profile(profile: ProfileInfo) -> JsonDict:
        return {
            "number": profile.number,
            "display_name": profile.display_name,
            "config_path": str(profile.config_path),
            "calling_ae_title": profile.calling_ae_title,
            "pacs_ae_title": profile.pacs_ae_title,
            "storage_ae_title": profile.storage_ae_title,
            "storage_port": profile.storage_port,
            "web_port": profile.web_port,
            "destination_directory": profile.destination_directory,
            "is_running": profile.is_running,
            "has_recovery": profile.has_recovery,
        }

    def _serialize_clone_result(self, result: ProfileCloneResult) -> JsonDict:
        return {
            "ok": True,
            "source_profile_number": result.source_number,
            "recommended_port": result.recommended_port,
            "recommended_web_port": result.recommended_web_port,
            "profile": self._serialize_profile(result.profile),
        }

    @staticmethod
    def _serialize_launch(launch: InstanceLaunchCommand) -> JsonDict:
        return {
            "target": str(launch.target),
            "arguments": list(launch.arguments),
            "working_directory": str(launch.working_directory),
            "icon": str(launch.icon),
        }


def _require_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("请求必须是 JSON 对象")
    return value


def _require_profile_number(
    payload: Mapping[str, object],
    field_name: str,
) -> int:
    if field_name not in payload:
        raise ValueError(f"缺少 {field_name}")
    value = payload[field_name]
    if isinstance(value, bool):
        raise ValueError("实例编号必须在 1 到 9999 之间")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("实例编号必须在 1 到 9999 之间") from exc
    if not 1 <= number <= 9999:
        raise ValueError("实例编号必须在 1 到 9999 之间")
    return number


def _require_display_name(
    payload: Mapping[str, object],
    field_name: str,
) -> str:
    if field_name not in payload:
        raise ValueError(f"缺少 {field_name}")
    value = payload[field_name]
    if not isinstance(value, str):
        raise ValueError("Profile 显示名必须是字符串")
    normalized = value.strip()
    if not normalized:
        raise ValueError("Profile 显示名不能为空")
    return normalized


def _optional_display_name(
    payload: Mapping[str, object],
    field_name: str,
) -> str | None:
    if field_name not in payload or payload[field_name] is None:
        return None
    return _require_display_name(payload, field_name)


def _optional_shortcut_name(
    payload: Mapping[str, object],
    field_name: str,
    *,
    default: str,
) -> str:
    if field_name not in payload or payload[field_name] is None:
        return default
    value = payload[field_name]
    if not isinstance(value, str):
        raise ValueError("快捷方式名称必须是字符串")
    normalized = value.strip()
    if not normalized:
        raise ValueError("快捷方式名称不能为空")
    return normalized


def _optional_bool(
    payload: Mapping[str, object],
    field_name: str,
    *,
    default: bool,
) -> bool:
    if field_name not in payload:
        return default
    value = payload[field_name]
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是布尔值")
    return value


def _optional_path(
    payload: Mapping[str, object],
    field_name: str,
    *,
    default: Path,
) -> Path:
    if field_name not in payload or payload[field_name] is None:
        return default
    value = payload[field_name]
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串路径")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} 不能为空")
    return Path(normalized).expanduser().resolve()


def _default_desktop_directory() -> Path:
    return (Path.home() / "Desktop").expanduser().resolve()


def sys_executable() -> str:
    import sys

    return sys.executable
