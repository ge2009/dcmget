from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[1]


def default_config_path() -> Path:
    if not is_frozen():
        return resource_root() / "config.json"
    app_data = os.environ.get("APPDATA")
    base = Path(app_data) if app_data else Path.home() / "AppData" / "Roaming"
    return base / "DcmGet" / "config.json"


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
