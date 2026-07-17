from __future__ import annotations

import os
import sys
from pathlib import Path


_portable_dcmtk_bin: Path | None = None


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[1]


def set_portable_dcmtk_bin(path: str | Path | None) -> None:
    global _portable_dcmtk_bin
    _portable_dcmtk_bin = None if path is None else Path(path).resolve()


def portable_dcmtk_bin() -> Path | None:
    return _portable_dcmtk_bin


def default_config_path() -> Path:
    if not is_frozen():
        return resource_root() / "config.json"
    app_data = os.environ.get("APPDATA")
    base = Path(app_data) if app_data else Path.home() / "AppData" / "Roaming"
    return base / "DcmGet" / "config.json"


def application_state_dir() -> Path:
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "DcmGet"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DcmGet"
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state_home) if xdg_state_home else Path.home() / ".local" / "state"
    return base / "dcmget"


def ensure_application_state_dir() -> Path:
    path = application_state_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def application_log_dir() -> Path:
    return application_state_dir() / "logs"


def ensure_application_log_dir() -> Path:
    path = application_log_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def ensure_default_config() -> Path:
    path = default_config_path()
    if is_frozen() and not path.exists():
        from .config import AppConfig, save_config

        save_config(
            path,
            AppConfig(
                access_numbers_file_path=str(path.parent / "access.txt"),
                dicom_destination_folder=str(Path.home() / "Documents" / "DcmGet" / "Dicom"),
            ),
        )
    return path
