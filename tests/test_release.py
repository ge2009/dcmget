from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from dcmget import __version__
from dcmget.release_notes import load_release_notes
from scripts.build_windows import validate_release_version


def test_root_and_packaged_release_notes_stay_in_sync():
    root = Path(__file__).resolve().parents[1]

    assert (root / "CHANGELOG.md").read_bytes() == (
        root / "dcmget" / "CHANGELOG.md"
    ).read_bytes()
    assert f"## {__version__}" in load_release_notes(root)


def test_windows_build_rejects_a_version_different_from_source():
    assert validate_release_version(__version__) == __version__

    with pytest.raises(argparse.ArgumentTypeError, match="与源码版本"):
        validate_release_version("9.9.9")
