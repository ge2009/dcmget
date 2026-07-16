from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_deploy_bundle import source_files
from scripts.build_windows import PLATFORM_RUNTIME, pyinstaller_args
from scripts.prepare_weasis import (
    PreparationError,
    acquire_asset,
    load_manifest,
    payload_is_current,
    preparation_inputs_sha256,
    write_payload_checksums,
)


def test_pinned_weasis_manifest_uses_official_471_asset():
    manifest = load_manifest()

    assert manifest["version"] == "4.7.1"
    assert manifest["platform"] == "windows-x86_64"
    assert manifest["asset_url"].startswith(
        "https://github.com/nroduit/Weasis/releases/download/v4.7.1/"
    )
    assert manifest["sha256"] == (
        "e2af585492e4f6954cc8cbc6938d841fc8824ace7fa138add3f2fdd134e9609a"
    )


def test_weasis_download_is_verified_and_reuses_valid_cache(tmp_path: Path):
    source = tmp_path / "source.msi"
    source.write_bytes(b"pinned-weasis-fixture")
    content_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = {
        "platform": "windows-x86_64",
        "asset_name": "fixture.msi",
        "asset_url": source.as_uri(),
        "asset_size": source.stat().st_size,
        "sha256": content_sha256,
    }

    cached = acquire_asset(manifest, tmp_path / "runtime")
    source.unlink()

    assert acquire_asset(manifest, tmp_path / "runtime", offline=True) == cached
    assert cached.read_bytes() == b"pinned-weasis-fixture"


def test_weasis_offline_mode_rejects_corrupt_cache(tmp_path: Path):
    manifest = {
        "platform": "windows-x86_64",
        "asset_name": "fixture.msi",
        "asset_url": "https://invalid.example/fixture.msi",
        "asset_size": 4,
        "sha256": hashlib.sha256(b"good").hexdigest(),
    }
    cache = tmp_path / "runtime" / "windows-x86_64" / "cache"
    cache.mkdir(parents=True)
    (cache / "fixture.msi").write_bytes(b"bad!")

    with pytest.raises(PreparationError, match="离线模式"):
        acquire_asset(manifest, tmp_path / "runtime", offline=True)


def test_weasis_payload_requires_runtime_and_matching_provenance(tmp_path: Path):
    payload = tmp_path / "Weasis"
    (payload / "app").mkdir(parents=True)
    (payload / "runtime").mkdir()
    (payload / "app" / "viewer.jar").write_bytes(b"app")
    (payload / "runtime" / "java.exe").write_bytes(b"runtime")
    (payload / "Weasis.exe").write_bytes(b"executable")
    (payload / "LICENSE-Weasis.txt").write_text("EPL-2.0", encoding="utf-8")
    (payload / "THIRD_PARTY-Weasis.md").write_text(
        "third party licenses", encoding="utf-8"
    )
    manifest = {"version": "4.7.1", "sha256": "a" * 64}
    (payload / "DCMGET_WEASIS_PAYLOAD.json").write_text(
        json.dumps(
            {
                "version": "4.7.1",
                "source_sha256": "a" * 64,
                "preparation_inputs_sha256": preparation_inputs_sha256(),
            }
        ),
        encoding="utf-8",
    )
    write_payload_checksums(payload)

    assert payload_is_current(payload, manifest)
    (payload / "runtime" / "java.exe").write_bytes(b"tampered")
    assert not payload_is_current(payload, manifest)


def test_windows_onedir_embeds_weasis_but_single_file_can_stay_small(tmp_path: Path):
    icon = tmp_path / "icon.ico"
    version_file = tmp_path / "version.txt"
    viewer = tmp_path / "Weasis"

    onedir = pyinstaller_args(
        "DcmGet", "--onedir", icon, version_file, PLATFORM_RUNTIME, viewer
    )
    onefile = pyinstaller_args(
        "DcmGet-Portable", "--onefile", icon, version_file, PLATFORM_RUNTIME
    )

    assert any(".runtime/weasis/windows-x86_64/Weasis" in value for value in onedir)
    assert not any(".runtime/weasis" in value for value in onefile)


def test_source_deploy_includes_weasis_manifest_helper_and_license():
    root = Path(__file__).resolve().parents[1]
    bundled = {path.relative_to(root).as_posix() for path in source_files(root)}

    assert {
        "scripts/prepare_weasis.py",
        "packaging/weasis/windows-x86_64.json",
        "packaging/weasis/LICENSE-Weasis.txt",
        "packaging/weasis/THIRD_PARTY-Weasis.md",
    } <= bundled
