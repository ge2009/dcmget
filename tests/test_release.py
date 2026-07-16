from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from DICOM_download_ui import build_parser, validate_frozen_pdi_resources
from dcmget import __version__
from dcmget.pdi_server import PdiRequestHandler
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
    assert PdiRequestHandler.server_version == f"DcmGetPDI/{__version__}"
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


def test_windows_pdi_smoke_uses_authenticated_directory_entry():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    assert "secrets.token_urlsafe(32)" in workflow
    assert '"--session-token", $token' in workflow
    assert '"http://127.0.0.1:$port/ready/$token"' in workflow
    assert '"http://127.0.0.1:$port/open/$token" -WebSession $session' in workflow
    assert '"dicomweb:/DICOM/I000001"' in workflow
    assert "/viewer/dicomjson/" not in workflow


def test_windows_upgrade_uses_a_pinned_real_previous_release_build():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    assert "ref: c01c83a1963a55457bef15917ddd4cfdbab81fd1" in workflow
    assert "path: upgrade-baseline" in workflow
    assert 'Copy-Item -LiteralPath ".runtime\\ohif\\cache"' in workflow
    assert "python scripts/prepare_ohif.py --offline" in workflow
    assert 'Copy-Item -LiteralPath ".runtime\\ohif" -Destination' not in workflow
    assert "python scripts/build_windows.py --version 2.6.1" in workflow
    assert 'Join-Path $baselineRoot "packaging\\windows\\dcmget.iss"' in workflow
    assert "DcmGet-2.6.1-Setup-x64.exe" in workflow
    assert '$baselineRecords[0].DisplayVersion -ne "2.6.1"' in workflow
    assert "/DAppVersion=2.0.0" not in workflow


def test_windows_firewall_is_limited_to_storescp_and_private_networks():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "packaging/windows/dcmget.iss").read_text(encoding="utf-8")
    bootstrap = (root / "scripts/bootstrap_windows.ps1").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    assert 'program=""{app}\\_internal\\.runtime\\dcmtk' in installer
    assert "profile=domain,private" in installer
    assert "-Program $Storescp.FullName" in bootstrap
    assert "-Profile Domain,Private" in bootstrap
    assert "$firewallRules.Count -ne 1" in workflow
    assert "$applicationFilters.Count -ne 1" in workflow
    assert (
        ".runtime\\dcmtk\\windows-x86_64\\dcmtk-3.7.0-win64-dynamic"
        "\\bin\\storescp.exe"
    ) in workflow
    assert "[StringComparison]::OrdinalIgnoreCase" in workflow
    assert "$profileNames.Count -ne 2" in workflow
    assert '$profileNames -notcontains "Domain"' in workflow
    assert '$profileNames -notcontains "Private"' in workflow
    assert "DCMGET_PAYLOAD.SHA256" in workflow


def test_frozen_self_test_requires_offline_ohif_and_local_server(
    tmp_path: Path, monkeypatch
):
    import DICOM_download_ui as entry

    monkeypatch.setattr(entry, "is_frozen", lambda: True)
    with pytest.raises(RuntimeError, match="PDI 离线资源缺失"):
        validate_frozen_pdi_resources(tmp_path)

    ohif = tmp_path / ".runtime" / "ohif" / "ohif-3.12.6"
    ohif.mkdir(parents=True)
    (tmp_path / "DcmGetPdiServer.exe").write_bytes(b"server")
    server_script = tmp_path / "dcmget" / "pdi_server.py"
    server_script.parent.mkdir()
    server_script.write_text("# offline server\n", encoding="utf-8")
    for name in (
        "index.html",
        "app-config.js",
        "init-service-worker.js",
        "LICENSE-OHIF.txt",
        "THIRD_PARTY-OHIF.md",
        "DCMGET_OHIF_PAYLOAD.json",
        "DCMGET_PAYLOAD.SHA256",
    ):
        (ohif / name).write_text("offline", encoding="utf-8")

    validate_frozen_pdi_resources(tmp_path)
    (ohif / "app-config.js").write_text("https://remote.invalid", encoding="utf-8")
    with pytest.raises(RuntimeError, match="外部地址"):
        validate_frozen_pdi_resources(tmp_path)
