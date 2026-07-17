from __future__ import annotations

import hashlib
import io
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

import pytest

from scripts.build_deploy_bundle import source_files
from scripts.build_windows import (
    PLATFORM_RUNTIME,
    pdi_server_pyinstaller_args,
    pyinstaller_args,
)
from scripts.prepare_ohif import (
    _PinnedHttpsRedirectHandler,
    PreparationError,
    acquire_asset,
    load_manifest,
    payload_path,
    payload_is_current,
    prepare_payload,
)


def _write_archive(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def _fixture_manifest(archive: Path, version: str = "fixture") -> dict[str, object]:
    return {
        "product": "OHIF Viewer",
        "package_name": "@ohif/app",
        "version": version,
        "asset_name": archive.name,
        "asset_url": archive.as_uri(),
        "asset_size": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "archive_root": "package/dist",
        "license": "MIT",
        "source_url": "https://github.com/OHIF/Viewers",
    }


def test_pinned_ohif_manifest_uses_official_3126_npm_asset():
    manifest = load_manifest()

    assert manifest["package_name"] == "@ohif/app"
    assert manifest["version"] == "3.12.6"
    assert manifest["asset_url"] == (
        "https://registry.npmjs.org/@ohif/app/-/app-3.12.6.tgz"
    )
    assert manifest["asset_size"] == 48508156
    assert manifest["sha256"] == (
        "930f3334c3a347d2b8e45fbef34c0f030da976af4fd18b8c62a1967afdc3674f"
    )


def test_ohif_download_is_verified_and_reuses_valid_cache(tmp_path: Path):
    source = tmp_path / "source.tgz"
    source.write_bytes(b"pinned-ohif-fixture")
    manifest = _fixture_manifest(source)

    cached = acquire_asset(manifest, tmp_path / "runtime")
    source.unlink()

    assert acquire_asset(manifest, tmp_path / "runtime", offline=True) == cached
    assert cached.read_bytes() == b"pinned-ohif-fixture"


def test_ohif_offline_mode_rejects_corrupt_cache(tmp_path: Path):
    source = tmp_path / "fixture.tgz"
    source.write_bytes(b"good")
    manifest = _fixture_manifest(source)
    source.unlink()
    cache = tmp_path / "runtime" / "cache"
    cache.mkdir(parents=True)
    (cache / "fixture.tgz").write_bytes(b"bad!")

    with pytest.raises(PreparationError, match="离线模式"):
        acquire_asset(manifest, tmp_path / "runtime", offline=True)


def test_ohif_paths_and_download_size_are_bounded(tmp_path: Path):
    source = tmp_path / "source.tgz"
    source.write_bytes(b"too-large")
    manifest = _fixture_manifest(source)
    manifest["asset_size"] = 2

    with pytest.raises(PreparationError, match="大小"):
        acquire_asset(manifest, tmp_path / "runtime")
    manifest["asset_name"] = "../escape.tgz"
    with pytest.raises(PreparationError, match="不安全"):
        acquire_asset(manifest, tmp_path / "runtime")
    manifest["version"] = "../../escape"
    with pytest.raises(PreparationError, match="不安全"):
        payload_path(tmp_path / "runtime", manifest)
    assert not (tmp_path / "escape.tgz").exists()


def test_ohif_payload_overlays_offline_config_and_detects_tampering(tmp_path: Path):
    archive = tmp_path / "fixture.tgz"
    _write_archive(
        archive,
        {
            "package/dist/index.html": b"<html>OHIF</html>",
            "package/dist/app-config.js": b"https://public.example/dicomweb",
            "package/dist/init-service-worker.js": b"https://public.example/workbox",
            "package/dist/sw.js": b"https://public.example/service-worker",
            "package/dist/google.js": b"https://public.example/analytics",
            "package/dist/oidc-client.min.js": b"https://public.example/oidc",
            "package/dist/silent-refresh.html": b"https://public.example/cdn",
            "package/dist/assets/viewer.js": b"viewer",
        },
    )
    manifest = _fixture_manifest(archive)
    payload = prepare_payload(archive, manifest, tmp_path / "runtime")

    config = (payload / "app-config.js").read_text(encoding="utf-8")
    assert "https://" not in config and "http://" not in config
    assert "dataSourcesModule.dicomjson" in config
    assert "defaultDataSourceName: 'directory'" in config
    assert "sourceName: 'directory'" in config
    assert "/dicomweb" not in config
    assert "installSingleClickSeriesLoading" in config
    assert 'data-cy="study-browser-thumbnail"' in config
    for quadrant in ("topLeft", "topRight", "bottomLeft", "bottomRight"):
        assert f"viewportOverlay.{quadrant}" in config
    assert "PatientName" in config and "AccessionNumber" in config
    service_worker = (payload / "init-service-worker.js").read_text(encoding="utf-8")
    assert "https://" not in service_worker and "http://" not in service_worker
    assert "unregister" in service_worker
    assert not (payload / "sw.js").exists()
    assert not (payload / "google.js").exists()
    assert not (payload / "oidc-client.min.js").exists()
    assert not (payload / "silent-refresh.html").exists()
    assert "DcmGet PDI" in config and "dcmget-logo.png" in config
    assert (payload / "assets" / "dcmget-logo.png").is_file()
    assert (payload / "LICENSE-OHIF.txt").is_file()
    assert (payload / "THIRD_PARTY-OHIF.md").is_file()
    assert payload_is_current(payload, manifest)

    (payload / "assets" / "viewer.js").write_bytes(b"tampered")
    assert not payload_is_current(payload, manifest)


def test_ohif_safe_extraction_rejects_path_traversal(tmp_path: Path):
    archive = tmp_path / "unsafe.tgz"
    _write_archive(
        archive,
        {
            "package/dist/index.html": b"<html>OHIF</html>",
            "package/dist/../../escape.txt": b"escape",
        },
    )
    manifest = _fixture_manifest(archive, "unsafe")

    with pytest.raises(PreparationError, match="不安全路径"):
        prepare_payload(archive, manifest, tmp_path / "runtime")

    assert not (tmp_path / "escape.txt").exists()


def test_ohif_rejects_unpinned_urls_and_cross_origin_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manifest = {
        "asset_name": "fixture.tgz",
        "asset_url": "http://127.0.0.1/private",
        "asset_size": 1,
        "sha256": "0" * 64,
    }
    opened = []
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_args: opened.append(True),
    )
    with pytest.raises(PreparationError, match="npm 官方"):
        acquire_asset(manifest, tmp_path / "runtime")
    assert not opened

    handler = _PinnedHttpsRedirectHandler("registry.npmjs.org", 443)
    request = urllib.request.Request("https://registry.npmjs.org/package.tgz")
    with pytest.raises(PreparationError, match="重定向"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://evil.example/package.tgz",
        )


def test_all_windows_variants_embed_ohif_and_local_server(tmp_path: Path):
    icon = tmp_path / "icon.ico"
    version_file = tmp_path / "version.txt"
    viewer = tmp_path / "ohif-3.12.6"
    server = tmp_path / "DcmGetPdiServer.exe"

    onedir = pyinstaller_args(
        "DcmGet", "--onedir", icon, version_file, PLATFORM_RUNTIME, viewer, server
    )
    onefile = pyinstaller_args(
        "DcmGet-Portable",
        "--onefile",
        icon,
        version_file,
        PLATFORM_RUNTIME,
        viewer,
        server,
    )

    assert any(".runtime/ohif/ohif-3.12.6" in value for value in onedir)
    assert any(".runtime/ohif/ohif-3.12.6" in value for value in onefile)
    assert any("DcmGetPdiServer.exe" in value for value in onedir)
    assert any("DcmGetPdiServer.exe" in value for value in onefile)
    assert any(
        value.replace("\\", "/").endswith("dcmget/pdi_server.py:dcmget")
        for value in onedir
    )
    assert any(
        value.replace("\\", "/").endswith("dcmget/pdi_server.py:dcmget")
        for value in onefile
    )
    assert any(
        value.replace("\\", "/").endswith("dcmget/architecture.py:dcmget")
        for value in onedir
    )
    server_args = pdi_server_pyinstaller_args(icon, version_file)
    assert "--onefile" in server_args and "--windowed" in server_args


def test_pdi_server_tool_entry_runs_directly_outside_repo_cwd(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "tools" / "dcmget_pdi_server.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )

    assert result.returncode == 0, result.stderr


def test_source_deploy_includes_ohif_manifest_helper_config_and_license():
    root = Path(__file__).resolve().parents[1]
    bundled = {path.relative_to(root).as_posix() for path in source_files(root)}

    assert {
        "scripts/prepare_ohif.py",
        "tools/dcmget_pdi_server.py",
        "dcmget/pdi_server.py",
        "packaging/ohif/ohif-3.12.6.json",
        "packaging/ohif/app-config.js",
        "packaging/ohif/init-service-worker.js",
        "packaging/ohif/LICENSE-OHIF.txt",
        "packaging/ohif/THIRD_PARTY-OHIF.md",
    } <= bundled
