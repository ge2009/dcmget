from __future__ import annotations

import os
import re
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


def profile_web_url(web_port: int) -> str:
    if isinstance(web_port, bool):
        raise InstanceShortcutError("Web 端口必须在 1 到 65535 之间")
    try:
        port = int(web_port)
    except (TypeError, ValueError) as exc:
        raise InstanceShortcutError("Web 端口必须在 1 到 65535 之间") from exc
    if not 1 <= port <= 65535:
        raise InstanceShortcutError("Web 端口必须在 1 到 65535 之间")
    return f"http://127.0.0.1:{port}/"


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
    open_profile_web: bool = False,
    no_open_browser: bool = False,
) -> InstanceLaunchCommand:
    normalized_profile = _normalize_profile_number(profile_number)
    root = Path(project_root or resource_root()).expanduser().resolve()
    target = Path(executable or sys.executable).expanduser().resolve()
    if not target.is_file():
        raise InstanceShortcutError(f"程序文件不存在：{target}")
    running_frozen = is_frozen() if frozen is None else bool(frozen)
    if open_profile_web and no_open_browser:
        raise InstanceShortcutError(
            "打开 Web 页面与禁止打开浏览器参数不能同时使用"
        )
    profile_arguments = ["--profile", str(normalized_profile)]
    if open_profile_web:
        profile_arguments.append("--open-profile-web")
    if no_open_browser:
        profile_arguments.append("--no-open-browser")
    if running_frozen:
        arguments = tuple(profile_arguments)
        working_directory = target.parent
    else:
        entrypoint = (root / "DICOM_download_ui.py").resolve()
        if not entrypoint.is_file():
            raise InstanceShortcutError(f"源码启动文件不存在：{entrypoint}")
        arguments = (str(entrypoint), *profile_arguments)
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
    web_port: int | None = None,
    url: str | None = None,
    platform: str | None = None,
    overwrite: bool = False,
) -> Path:
    platform_name = platform or sys.platform
    extension = _shortcut_extension(platform_name)
    normalized = normalize_shortcut_name(name)
    _normalize_profile_number(profile_number)
    shortcut_url = _shortcut_url(web_port=web_port, url=url)
    if normalized.lower().endswith(extension.lower()):
        normalized = normalize_shortcut_name(normalized[: -len(extension)])
    try:
        destination = Path(destination_directory).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
        shortcut_path = destination / f"{normalized}{extension}"
        if (shortcut_path.exists() or shortcut_path.is_symlink()) and not overwrite:
            raise ShortcutExistsError(shortcut_path)

        if platform_name == "win32":
            content = (
                "[InternetShortcut]\n"
                f"URL={shortcut_url}\n"
                "IconIndex=0\n"
            )
            _atomic_write_text(shortcut_path, content, executable=False)
        elif platform_name == "darwin":
            content = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict><key>URL</key>'
                f"<string>{shortcut_url}</string></dict></plist>\n"
            )
            _atomic_write_text(
                shortcut_path,
                content,
                executable=False,
            )
        else:
            content = (
                "[Desktop Entry]\n"
                "Type=Link\n"
                f"Name={normalized.replace(chr(10), ' ')}\n"
                f"URL={shortcut_url}\n"
            )
            _atomic_write_text(shortcut_path, content, executable=True)
        return shortcut_path
    except InstanceShortcutError:
        raise
    except OSError as exc:
        raise InstanceShortcutError(f"无法写入快捷方式：{exc}") from exc


def _shortcut_extension(platform_name: str) -> str:
    if platform_name == "win32":
        return ".url"
    if platform_name == "darwin":
        return ".webloc"
    return ".desktop"


def _shortcut_url(*, web_port: int | None, url: str | None) -> str:
    expected = profile_web_url(web_port) if web_port is not None else None
    if url is None:
        if expected is None:
            raise InstanceShortcutError("创建快捷方式时必须提供 Profile Web 端口")
        return expected
    normalized = str(url).strip()
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})/", normalized)
    if not match:
        raise InstanceShortcutError("快捷方式 URL 必须是本机 Profile Web 地址")
    checked = profile_web_url(int(match.group(1)))
    if expected is not None and checked != expected:
        raise InstanceShortcutError("快捷方式 URL 与 Profile Web 端口不一致")
    return checked


def _normalize_profile_number(profile_number: object) -> int:
    try:
        number = int(profile_number)
    except (TypeError, ValueError) as exc:
        raise InstanceShortcutError("实例编号必须在 1 到 9999 之间") from exc
    if isinstance(profile_number, bool) or not 1 <= number <= 9999:
        raise InstanceShortcutError("实例编号必须在 1 到 9999 之间")
    return number


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
