from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from DICOM_download_ui import build_parser
from dcmget import __version__
from dcmget.release_notes import load_release_notes
from scripts.build_deploy_bundle import VERSION as DEPLOY_VERSION, source_files
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


def test_source_deploy_contains_transitive_requirement_files():
    root = Path(__file__).resolve().parents[1]
    bundled = {path.relative_to(root).as_posix() for path in source_files(root)}

    assert {"requirements.txt", "requirements-dev.txt", "requirements-build.txt"} <= bundled


def test_release_version_sources_and_ui_self_test_flag_stay_in_sync():
    root = Path(__file__).resolve().parents[1]
    windows_workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    assert DEPLOY_VERSION == __version__
    assert f"default: {__version__}" in windows_workflow
    assert build_parser().parse_args(["--ui-self-test"]).ui_self_test


def test_windows_release_artifacts_are_split_to_avoid_duplicate_runtime_downloads():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    for suffix in ("Setup-x64", "Portable-x64", "Windows-x64-ZIP"):
        assert f"DcmGet-${{{{ inputs.version }}}}-{suffix}" in workflow
    assert "name: DcmGet-${{ inputs.version }}-windows-x64\n" not in workflow
