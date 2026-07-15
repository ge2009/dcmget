from __future__ import annotations

from pathlib import Path

from . import __version__


def load_release_notes(project_root: str | Path) -> str:
    candidates = (
        Path(project_root) / "CHANGELOG.md",
        Path(__file__).with_name("CHANGELOG.md"),
    )
    for path in candidates:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return f"# DcmGet {__version__}\n\n当前安装包未包含版本说明文件。"
