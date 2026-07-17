from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from .runtime import is_frozen, resource_root


_INVALID_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_POWERSHELL_CREATE_SHORTCUT = """
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($env:DCMGET_SHORTCUT_TEMP)
$shortcut.TargetPath = $env:DCMGET_SHORTCUT_TARGET
$shortcut.Arguments = $env:DCMGET_SHORTCUT_ARGUMENTS
$shortcut.WorkingDirectory = $env:DCMGET_SHORTCUT_WORKDIR
$shortcut.Description = $env:DCMGET_SHORTCUT_DESCRIPTION
$shortcut.IconLocation = $env:DCMGET_SHORTCUT_ICON + ',0'
$shortcut.Save()
""".strip()


class InstanceShortcutError(RuntimeError):
    pass


class ShortcutExistsError(InstanceShortcutError):
    def __init__(self, path: Path):
        super().__init__(f"快捷方式已存在：{path}")
        self.path = path


@dataclass(frozen=True, slots=True)
class InstanceLaunchCommand:
    target: Path
    arguments: tuple[str, ...]
    working_directory: Path
    icon: Path


def default_instance_shortcut_name(storage_port: int, storage_ae_title: str) -> str:
    ae_title = storage_ae_title.strip() or "AE"
    return normalize_shortcut_name(f"dcmget-{int(storage_port)}-{ae_title}")


def normalize_shortcut_name(value: str) -> str:
    name = _INVALID_FILENAME_CHARACTERS.sub("-", str(value).strip())
    name = re.sub(r"-{2,}", "-", name).strip(" .-")
    if not name:
        raise InstanceShortcutError("请输入快捷方式名称")
    if name.split(".", 1)[0].upper() in _RESERVED_WINDOWS_NAMES:
        name = f"_{name}"
    name = name[:120].rstrip(" .")
    if not name:
        raise InstanceShortcutError("请输入快捷方式名称")
    return name


def build_instance_launch_command(
    profile_number: int,
    *,
    project_root: str | Path | None = None,
    executable: str | Path | None = None,
    frozen: bool | None = None,
) -> InstanceLaunchCommand:
    try:
        normalized_profile = int(profile_number)
    except (TypeError, ValueError) as exc:
        raise InstanceShortcutError("实例编号必须在 1 到 9999 之间") from exc
    if isinstance(profile_number, bool) or not 1 <= normalized_profile <= 9999:
        raise InstanceShortcutError("实例编号必须在 1 到 9999 之间")
    root = Path(project_root or resource_root()).expanduser().resolve()
    target = Path(executable or sys.executable).expanduser().resolve()
    if not target.is_file():
        raise InstanceShortcutError(f"程序文件不存在：{target}")
    running_frozen = is_frozen() if frozen is None else bool(frozen)
    if running_frozen:
        arguments = ("--profile", str(normalized_profile))
        working_directory = target.parent
    else:
        entrypoint = (root / "DICOM_download_ui.py").resolve()
        if not entrypoint.is_file():
            raise InstanceShortcutError(f"源码启动文件不存在：{entrypoint}")
        arguments = (str(entrypoint), "--profile", str(normalized_profile))
        working_directory = root
    return InstanceLaunchCommand(
        target=target,
        arguments=arguments,
        working_directory=working_directory,
        icon=target,
    )


def create_instance_shortcut(
    profile_number: int,
    name: str,
    destination_directory: str | Path,
    *,
    project_root: str | Path | None = None,
    executable: str | Path | None = None,
    frozen: bool | None = None,
    platform: str | None = None,
    overwrite: bool = False,
) -> Path:
    platform_name = platform or sys.platform
    extension = _shortcut_extension(platform_name)
    normalized = normalize_shortcut_name(name)
    if normalized.lower().endswith(extension.lower()):
        normalized = normalize_shortcut_name(normalized[: -len(extension)])
    try:
        destination = Path(destination_directory).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
        shortcut_path = destination / f"{normalized}{extension}"
        if (shortcut_path.exists() or shortcut_path.is_symlink()) and not overwrite:
            raise ShortcutExistsError(shortcut_path)

        launch = build_instance_launch_command(
            profile_number,
            project_root=project_root,
            executable=executable,
            frozen=frozen,
        )
        if platform_name == "win32":
            _create_windows_shortcut(shortcut_path, launch, profile_number)
        elif platform_name == "darwin":
            command = " ".join(
                shlex.quote(value) for value in (str(launch.target), *launch.arguments)
            )
            _atomic_write_text(
                shortcut_path,
                f"#!/bin/sh\nexec {command} \"$@\"\n",
                executable=True,
            )
        else:
            arguments = " ".join(
                _desktop_exec_quote(value)
                for value in (str(launch.target), *launch.arguments)
            )
            content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                f"Name={normalized.replace(chr(10), ' ')}\n"
                f"Exec={arguments}\n"
                f"Path={launch.working_directory}\n"
                f"Icon={launch.icon}\n"
                "Terminal=false\n"
            )
            _atomic_write_text(shortcut_path, content, executable=True)
        return shortcut_path
    except InstanceShortcutError:
        raise
    except OSError as exc:
        raise InstanceShortcutError(f"无法写入快捷方式：{exc}") from exc


def _shortcut_extension(platform_name: str) -> str:
    if platform_name == "win32":
        return ".lnk"
    if platform_name == "darwin":
        return ".command"
    return ".desktop"


def _create_windows_shortcut(
    path: Path,
    launch: InstanceLaunchCommand,
    profile_number: int,
) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise InstanceShortcutError("未找到 Windows PowerShell，无法创建 .lnk 快捷方式")
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.lnk")
    environment = os.environ.copy()
    environment.update(
        {
            "DCMGET_SHORTCUT_TEMP": str(temporary.resolve()),
            "DCMGET_SHORTCUT_TARGET": str(launch.target),
            "DCMGET_SHORTCUT_ARGUMENTS": subprocess.list2cmdline(
                list(launch.arguments)
            ),
            "DCMGET_SHORTCUT_WORKDIR": str(launch.working_directory),
            "DCMGET_SHORTCUT_DESCRIPTION": (
                f"固定打开 DcmGet 实例 {int(profile_number)}"
            ),
            "DCMGET_SHORTCUT_ICON": str(launch.icon),
        }
    )
    encoded = base64.b64encode(
        _POWERSHELL_CREATE_SHORTCUT.encode("utf-16-le")
    ).decode("ascii")
    try:
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            env=environment,
            capture_output=True,
            text=True,
            errors="replace",
            shell=False,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstanceShortcutError(f"创建 Windows 快捷方式失败：{exc}") from exc
    try:
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "未知错误").strip()
            raise InstanceShortcutError(f"创建 Windows 快捷方式失败：{detail}")
        if not temporary.is_file():
            raise InstanceShortcutError("Windows 未生成快捷方式文件")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_write_text(path: Path, content: str, *, executable: bool) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        if executable:
            temporary.chmod(0o755)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _desktop_exec_quote(value: str) -> str:
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
    )
    return f'"{escaped}"'
