from __future__ import annotations

import hashlib
import io
import json
import subprocess
import threading
import time
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dcmget.update_signing import sign_manifest
from dcmget.windows_update import (
    ApplyRequest,
    ComponentFile,
    Ed25519SignedManifestVerifier,
    GitHubReleaseSource,
    MirrorFirstReleaseSource,
    SIGNED_MANIFEST_NAME,
    SecureStagingDirectory,
    STATIC_MIRROR_BASE_URL,
    StaticMirrorReleaseSource,
    TRUSTED_UPDATE_PUBLIC_KEYS,
    UpdateAsset,
    UpdateCandidate,
    UpdateNetworkError,
    UpdateSecurityError,
    WindowsAuthenticodeVerifier,
    WindowsScheduledTaskUpdater,
    WindowsSignedManifestVerifier,
    WindowsUpdateService,
    create_windows_update_service,
    parse_version,
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _tree_digest(records: dict[str, tuple[int, str]]) -> str:
    digest = hashlib.sha256()
    for path in sorted(records):
        size, sha256 = records[path]
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _asset(
    content: bytes,
    *,
    name: str = "DcmGet-3.6.0-from-3.5.2-component.zip",
    kind: str = "component_patch",
    base_version: str | None = "3.5.2",
    component_files: tuple[ComponentFile, ...] | None = None,
) -> UpdateAsset:
    resolved_component_files = (
        component_files
        if component_files is not None
        else (
            ComponentFile(
                "DcmGet.exe",
                len(content),
                _sha256(content),
                base_missing=True,
            ),
        )
        if kind == "component_patch"
        else ()
    )
    base_records = {
        item.path: (int(item.base_size), str(item.base_sha256))
        for item in resolved_component_files
        if not item.base_missing
    }
    target_records = {
        item.path: (item.size, item.sha256) for item in resolved_component_files
    }
    return UpdateAsset(
        name=name,
        kind=kind,
        size=len(content),
        sha256=_sha256(content),
        download_url=f"https://github.com/ge2009/dcmget/releases/download/v3.6.0/{name}",
        signature_status="SIGNED" if kind == "full_installer" else "NOT_APPLICABLE",
        base_version=base_version,
        preserves_user_data=kind == "component_patch",
        content_scope="application" if kind == "component_patch" else "",
        component_files=resolved_component_files,
        base_tree_sha256=_tree_digest(base_records) if kind == "component_patch" else "",
        target_tree_sha256=(
            _tree_digest(target_records) if kind == "component_patch" else ""
        ),
    )


def _component_package(
    payload: bytes = b"changed executable",
    base_payload: bytes = b"previous executable",
) -> tuple[UpdateAsset, bytes]:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        record = ComponentFile(
            "DcmGet.exe",
            len(payload),
            _sha256(payload),
            base_size=len(base_payload),
            base_sha256=_sha256(base_payload),
        )
        base_tree = _tree_digest(
            {record.path: (len(base_payload), _sha256(base_payload))}
        )
        target_tree = _tree_digest(
            {record.path: (record.size, record.sha256)}
        )
        patch_manifest = {
            "schema_version": 1,
            "product": "DcmGet",
            "platform": "windows-x64",
            "base_version": "3.5.2",
            "version": "3.6.0",
            "install_path_allowlist": ["DcmGet.exe", "_internal/**"],
            "files": [
                {
                    "path": record.path,
                    "size": record.size,
                    "sha256": record.sha256,
                    "base_missing": False,
                    "base_size": len(base_payload),
                    "base_sha256": _sha256(base_payload),
                }
            ],
            "base_tree_sha256": base_tree,
            "target_tree_sha256": target_tree,
            "removed_paths": [],
        }
        archive.writestr("PATCH-MANIFEST.json", json.dumps(patch_manifest))
        archive.writestr("DcmGet.exe", payload)
    package = stream.getvalue()
    return _asset(package, component_files=(record,)), package


def _signed_update_manifest(patch_content: bytes = b"patch payload") -> dict[str, object]:
    changed_executable = b"changed executable"
    return {
        "schema_version": 1,
        "product": "DcmGet",
        "platform": "windows-x64",
        "channel": "stable",
        "version": "3.6.0",
        "artifacts": [
            {
                "name": "DcmGet-3.6.0-from-3.5.2-component.zip",
                "kind": "component_patch",
                "size": len(patch_content),
                "sha256": _sha256(patch_content),
                "base_version": "3.5.2",
                "preserves_user_data": True,
                "content_scope": "application",
                "base_tree_sha256": _tree_digest({}),
                "target_tree_sha256": _tree_digest(
                    {
                        "DcmGet.exe": (
                            len(changed_executable),
                            _sha256(changed_executable),
                        )
                    }
                ),
                "files": [
                    {
                        "path": "DcmGet.exe",
                        "size": len(changed_executable),
                        "sha256": _sha256(changed_executable),
                        "base_missing": True,
                    }
                ],
            }
        ],
    }


class FakeSource:
    def __init__(
        self,
        candidate: UpdateCandidate,
        content_by_name: dict[str, bytes],
    ) -> None:
        self.candidate = candidate
        self.content_by_name = content_by_name
        self.fetch_count = 0
        self.download_count = 0

    def fetch_latest(self) -> UpdateCandidate:
        self.fetch_count += 1
        return self.candidate

    def download_asset(self, asset: UpdateAsset, destination: Path) -> None:
        self.download_count += 1
        destination.write_bytes(self.content_by_name[asset.name])


class FakeScheduler:
    def __init__(self, *, supports_component_patch: bool = True) -> None:
        self.supports_component_patch = supports_component_patch
        self.requests: list[ApplyRequest] = []

    def schedule(self, request: ApplyRequest) -> None:
        self.requests.append(request)


class BlockingSource(FakeSource):
    def __init__(self, candidate: UpdateCandidate, content_by_name: dict[str, bytes]):
        super().__init__(candidate, content_by_name)
        self.block_check = False
        self.block_download = False
        self.started = threading.Event()
        self.release = threading.Event()

    def fetch_latest(self) -> UpdateCandidate:
        if self.block_check:
            self.started.set()
            assert self.release.wait(2)
        return super().fetch_latest()

    def download_asset(self, asset: UpdateAsset, destination: Path) -> None:
        if self.block_download:
            self.started.set()
            assert self.release.wait(2)
        super().download_asset(asset, destination)


class FakeTimer:
    def __init__(self, delay: float, callback) -> None:
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        assert self.started and not self.cancelled
        self.callback()


def _service(
    tmp_path: Path,
    source: FakeSource,
    scheduler: FakeScheduler,
    verifier=lambda path, asset: None,
    *,
    policy: str = "automatic",
    platform_name: str = "win32",
    state_file: Path | None = None,
) -> WindowsUpdateService:
    return WindowsUpdateService(
        current_version="3.5.2",
        source=source,
        staging=SecureStagingDirectory(tmp_path / "updates"),
        scheduler=scheduler,
        authenticode_verifier=verifier,
        policy=policy,
        platform_name=platform_name,
        state_file=state_file,
    )


def test_semantic_versions_are_strict_and_numeric():
    assert parse_version("3.10.0") > parse_version("3.9.9")
    for invalid in ("v3.5.2", "3.5", "3.5.2.1", "3.5.2-beta", ""):
        with pytest.raises(UpdateSecurityError, match="X.Y.Z"):
            parse_version(invalid)


def test_disabled_policy_and_non_windows_never_touch_network(tmp_path: Path):
    candidate = UpdateCandidate("3.6.0", (_asset(b"patch"),))
    disabled_source = FakeSource(candidate, {})
    disabled = _service(
        tmp_path,
        disabled_source,
        FakeScheduler(),
        policy="disabled",
    )

    assert disabled.check(wait=True)["phase"] == "disabled"
    assert disabled.download(wait=True)["phase"] == "disabled"
    assert disabled.start_automatic_check(0) is False
    assert disabled_source.fetch_count == 0
    assert disabled_source.download_count == 0

    unsupported_source = FakeSource(candidate, {})
    unsupported = _service(
        tmp_path,
        unsupported_source,
        FakeScheduler(),
        platform_name="darwin",
    )
    assert unsupported.check(wait=True)["phase"] == "unsupported"
    assert unsupported.start_automatic_check(0) is False
    assert unsupported_source.fetch_count == 0


def test_automatic_check_repeats_daily_and_policy_controls_timers(
    tmp_path: Path,
):
    candidate = UpdateCandidate("3.6.0", (_asset(b"patch"),))
    source = FakeSource(candidate, {})
    timers: list[FakeTimer] = []

    def timer_factory(delay, callback):
        timer = FakeTimer(delay, callback)
        timers.append(timer)
        return timer

    service = WindowsUpdateService(
        current_version="3.5.2",
        source=source,
        staging=SecureStagingDirectory(tmp_path / "updates"),
        scheduler=FakeScheduler(),
        authenticode_verifier=lambda path, asset: None,
        policy="automatic",
        platform_name="win32",
        automatic_interval_seconds=24 * 60 * 60,
        timer_factory=timer_factory,
    )

    assert service.start_automatic_check(8) is True
    assert timers[0].delay == 8
    timers[0].fire()
    deadline = time.monotonic() + 2
    while source.fetch_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert source.fetch_count == 1
    assert timers[1].delay == 24 * 60 * 60

    periodic = timers[1]
    service.set_policy("disabled")
    assert periodic.cancelled is True
    assert service.status()["state"] == "disabled"
    service.set_policy("automatic")
    assert timers[2].delay == 0
    assert timers[2].started is True

    service.close()
    assert timers[2].cancelled is True
    assert service.start_automatic_check(0) is False


def test_public_network_operations_return_immediately_and_run_in_background(
    tmp_path: Path,
):
    patch, package = _component_package()
    source = BlockingSource(
        UpdateCandidate("3.6.0", (patch,)),
        {patch.name: package},
    )
    service = _service(tmp_path, source, FakeScheduler())
    source.block_check = True

    started_at = time.monotonic()
    status = service.check()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.2
    assert status["state"] == "checking"
    assert source.started.wait(1)
    source.release.set()
    deadline = time.monotonic() + 2
    while service.status()["state"] == "checking" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.status()["state"] == "available"

    source.started.clear()
    source.release.clear()
    source.block_check = False
    source.block_download = True
    started_at = time.monotonic()
    status = service.download()
    elapsed = time.monotonic() - started_at
    assert elapsed < 0.2
    assert status["state"] == "downloading"
    assert source.started.wait(1)
    source.release.set()
    deadline = time.monotonic() + 2
    while service.status()["state"] == "downloading" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.status()["state"] == "ready"


def test_component_patch_is_preferred_and_apply_preserves_user_roots(
    tmp_path: Path,
):
    patch, patch_content = _component_package()
    installer_content = b"full installer"
    installer = _asset(
        installer_content,
        name="DcmGet-3.6.0-Setup-x64.exe",
        kind="full_installer",
        base_version=None,
    )
    source = FakeSource(
        UpdateCandidate("3.6.0", (patch, installer)),
        {patch.name: patch_content, installer.name: installer_content},
    )
    scheduler = FakeScheduler(supports_component_patch=True)
    verifier_calls: list[Path] = []
    service = _service(
        tmp_path,
        source,
        scheduler,
        lambda path, asset: verifier_calls.append(path),
    )

    assert service.check(wait=True)["asset_kind"] == "component_patch"
    ready = service.download(wait=True)
    assert ready["phase"] == "ready"
    assert ready["state"] == "ready"
    assert ready["latest_version"] == "3.6.0"
    assert ready["package_kind"] == "patch"
    assert ready["download_size"] == len(patch_content)
    assert ready["downloaded"] is True
    assert ready["available"] is True
    assert service.apply()["phase"] == "applying"
    assert verifier_calls == []
    assert len(scheduler.requests) == 1
    request = scheduler.requests[0]
    assert request.asset_kind == "component_patch"
    assert request.target_version == "3.6.0"
    assert {"config", "state", "downloads", "license"}.issubset(
        request.protected_roots
    )


def test_production_style_scheduler_falls_back_to_full_installer(tmp_path: Path):
    patch = _asset(b"patch")
    installer = _asset(
        b"installer",
        name="DcmGet-3.6.0-Setup-x64.exe",
        kind="full_installer",
        base_version=None,
    )
    source = FakeSource(UpdateCandidate("3.6.0", (patch, installer)), {})
    scheduler = FakeScheduler(supports_component_patch=False)
    service = _service(tmp_path, source, scheduler)

    status = service.check(wait=True)

    assert status["phase"] == "available"
    assert status["asset_kind"] == "full_installer"
    assert status["asset_name"] == installer.name


def test_component_base_mismatch_automatically_selects_full_installer(
    tmp_path: Path,
):
    install = tmp_path / "install"
    install.mkdir()
    (install / "DcmGet.exe").write_bytes(b"unexpected local version")
    payload = b"new version"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("DcmGet.exe", payload)
    package = stream.getvalue()
    record = ComponentFile(
        "DcmGet.exe",
        len(payload),
        _sha256(payload),
        base_size=12,
        base_sha256=_sha256(b"expected old"),
    )
    patch = _asset(package, component_files=(record,))
    installer = _asset(
        b"installer",
        name="DcmGet-3.6.0-Setup-x64.exe",
        kind="full_installer",
        base_version=None,
    )
    scheduler = WindowsScheduledTaskUpdater(
        SecureStagingDirectory(tmp_path / "updates"),
        install_directory=install,
        platform_name="win32",
    )
    source = FakeSource(UpdateCandidate("3.6.0", (patch, installer)), {})
    service = _service(tmp_path, source, scheduler)

    status = service.check(wait=True)

    assert status["package_kind"] == "installer"
    assert status["asset_name"] == installer.name


def test_component_base_hash_reads_every_block(tmp_path: Path):
    install = tmp_path / "install"
    install.mkdir()
    base_content = b"A" * (1024 * 1024) + b"B" * (1024 * 1024 + 17)
    (install / "DcmGet.exe").write_bytes(base_content)
    record = ComponentFile(
        "DcmGet.exe",
        3,
        _sha256(b"new"),
        base_size=len(base_content),
        base_sha256=_sha256(base_content),
    )
    scheduler = WindowsScheduledTaskUpdater(
        SecureStagingDirectory(tmp_path / "updates"),
        install_directory=install,
        platform_name="win32",
    )

    base_tree = _tree_digest(
        {"DcmGet.exe": (len(base_content), _sha256(base_content))}
    )
    assert scheduler.can_apply_component((record,), base_tree) is True

    changed_tail = base_content[:-1] + b"C"
    (install / "DcmGet.exe").write_bytes(changed_tail)
    assert scheduler.can_apply_component((record,), base_tree) is False


def test_component_tree_ignores_installer_roots_but_detects_internal_drift(
    tmp_path: Path,
):
    install = tmp_path / "install"
    internal = install / "_internal"
    internal.mkdir(parents=True)
    old_app = b"old app"
    unchanged = b"runtime dependency"
    (install / "DcmGet.exe").write_bytes(old_app)
    (internal / "runtime.dat").write_bytes(unchanged)
    (install / "DcmGetService.exe").write_bytes(b"ignored WinSW wrapper")
    (install / "unins000.exe").write_bytes(b"ignored uninstaller")
    record = ComponentFile(
        "DcmGet.exe",
        len(b"new app"),
        _sha256(b"new app"),
        base_size=len(old_app),
        base_sha256=_sha256(old_app),
    )
    base_tree = _tree_digest(
        {
            "DcmGet.exe": (len(old_app), _sha256(old_app)),
            "_internal/runtime.dat": (len(unchanged), _sha256(unchanged)),
        }
    )
    scheduler = WindowsScheduledTaskUpdater(
        SecureStagingDirectory(tmp_path / "updates"),
        install_directory=install,
        platform_name="win32",
    )

    assert scheduler.can_apply_component((record,), base_tree) is True
    (install / "another-installer-helper.exe").write_bytes(b"also ignored")
    assert scheduler.can_apply_component((record,), base_tree) is True
    (internal / "unexpected.dat").write_bytes(b"drift")
    assert scheduler.can_apply_component((record,), base_tree) is False


def test_component_tree_rejects_internal_symlink(tmp_path: Path):
    install = tmp_path / "install"
    internal = install / "_internal"
    internal.mkdir(parents=True)
    old_app = b"old app"
    (install / "DcmGet.exe").write_bytes(old_app)
    external = tmp_path / "external.dat"
    external.write_bytes(b"external")
    try:
        (internal / "linked.dat").symlink_to(external)
    except OSError:
        pytest.skip("symlinks unavailable")
    record = ComponentFile(
        "DcmGet.exe",
        len(b"new app"),
        _sha256(b"new app"),
        base_size=len(old_app),
        base_sha256=_sha256(old_app),
    )
    scheduler = WindowsScheduledTaskUpdater(
        SecureStagingDirectory(tmp_path / "updates"),
        install_directory=install,
        platform_name="win32",
    )

    assert scheduler.can_apply_component((record,), "0" * 64) is False


def test_full_installer_is_reverified_before_independent_schedule(tmp_path: Path):
    content = b"signed installer"
    installer = _asset(
        content,
        name="DcmGet-3.6.0-Setup-x64.exe",
        kind="full_installer",
        base_version=None,
    )
    source = FakeSource(UpdateCandidate("3.6.0", (installer,)), {installer.name: content})
    scheduler = FakeScheduler(supports_component_patch=False)
    verifier_calls: list[tuple[Path, str]] = []
    service = _service(
        tmp_path,
        source,
        scheduler,
        lambda path, asset: verifier_calls.append((path, asset.kind)),
    )

    service.check(wait=True)
    assert service.download(wait=True)["phase"] == "ready"
    assert service.apply()["phase"] == "applying"

    assert [kind for _, kind in verifier_calls] == [
        "full_installer",
        "full_installer",
    ]
    assert len(scheduler.requests) == 1


def test_unsigned_full_installer_uses_signed_manifest_hash_without_authenticode(
    tmp_path: Path,
):
    content = b"unsigned installer authenticated by the signed manifest"
    installer = replace(
        _asset(
            content,
            name="DcmGet-3.6.0-Setup-x64.exe",
            kind="full_installer",
            base_version=None,
        ),
        signature_status="UNSIGNED",
    )
    source = FakeSource(
        UpdateCandidate("3.6.0", (installer,)),
        {installer.name: content},
    )
    scheduler = FakeScheduler(supports_component_patch=False)
    service = WindowsUpdateService(
        current_version="3.5.2",
        source=source,
        staging=SecureStagingDirectory(tmp_path / "updates"),
        scheduler=scheduler,
        platform_name="win32",
    )

    assert service.check(wait=True)["phase"] == "available"
    assert service.download(wait=True)["phase"] == "ready"
    assert service.apply()["phase"] == "applying"
    assert len(scheduler.requests) == 1
    assert scheduler.requests[0].asset_kind == "full_installer"


def test_size_or_hash_mismatch_never_reaches_scheduler(tmp_path: Path):
    patch, expected = _component_package()
    source = FakeSource(
        UpdateCandidate("3.6.0", (patch,)),
        {patch.name: b"tampered patch"},
    )
    scheduler = FakeScheduler()
    service = _service(tmp_path, source, scheduler)

    service.check(wait=True)
    status = service.download(wait=True)

    assert status["phase"] == "error"
    assert "大小不匹配" in str(status["error"]) or "SHA-256" in str(
        status["error"]
    )
    assert scheduler.requests == []


def test_policy_and_highest_seen_version_are_persisted(tmp_path: Path):
    state_file = tmp_path / "state" / "windows-update.json"
    newest = UpdateCandidate("3.7.0", (_asset(b"patch"),))
    source = FakeSource(newest, {})
    service = _service(
        tmp_path,
        source,
        FakeScheduler(),
        state_file=state_file,
    )
    assert service.check(wait=True)["phase"] == "available"
    assert service.set_policy("disabled")["phase"] == "disabled"

    older_source = FakeSource(
        UpdateCandidate("3.6.0", (_asset(b"patch"),)),
        {},
    )
    restored = _service(
        tmp_path,
        older_source,
        FakeScheduler(),
        policy="automatic",
        state_file=state_file,
    )
    assert restored.status()["policy"] == "disabled"
    restored.set_policy("automatic")
    status = restored.check(wait=True)
    assert status["phase"] == "error"
    assert "低于已见版本" in str(status["error"])


class FakeResponse:
    def __init__(self, content: bytes, url: str) -> None:
        self._content = content
        self._url = url
        self._position = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._content) - self._position
        result = self._content[self._position : self._position + size]
        self._position += len(result)
        return result

    def geturl(self) -> str:
        return self._url


def test_ed25519_manifest_verifier_rejects_tampering_and_unknown_keys():
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    manifest = b'{"version":"3.6.0"}'
    envelope = sign_manifest(
        manifest,
        private_key=private_key,
        key_id="test-release",
    )
    verifier = Ed25519SignedManifestVerifier({"test-release": public_key})

    assert verifier(envelope) == manifest

    tampered = json.loads(envelope)
    signature = str(tampered["signature"])
    tampered["signature"] = (
        ("A" if signature[0] != "A" else "B") + signature[1:]
    )
    with pytest.raises(UpdateSecurityError, match="签名无效"):
        verifier(json.dumps(tampered, separators=(",", ":")).encode("utf-8"))

    unknown_envelope = sign_manifest(
        manifest,
        private_key=private_key,
        key_id="unknown-release",
    )
    with pytest.raises(UpdateSecurityError, match="不受信任"):
        verifier(unknown_envelope)


def test_static_mirror_source_verifies_fixed_manifest_and_release_layout(
    tmp_path: Path,
):
    patch_content = b"patch payload"
    manifest = _signed_update_manifest(patch_content)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signed_payload = sign_manifest(
        json.dumps(manifest).encode("utf-8"),
        private_key=private_key,
        key_id="test-release",
    )
    asset_name = "DcmGet-3.6.0-from-3.5.2-component.zip"
    manifest_url = (
        f"{STATIC_MIRROR_BASE_URL}stable/{SIGNED_MANIFEST_NAME}"
    )
    asset_url = (
        f"{STATIC_MIRROR_BASE_URL}releases/3.6.0/{asset_name}"
    )
    requests: list[tuple[str, float]] = []
    def urlopen(request, **kwargs):
        url = request.full_url
        requests.append((url, kwargs["timeout"]))
        if url == manifest_url:
            return FakeResponse(signed_payload, url)
        if url == asset_url:
            return FakeResponse(patch_content, url)
        raise AssertionError(url)

    source = StaticMirrorReleaseSource(
        signed_manifest_verifier=Ed25519SignedManifestVerifier(
            {"test-release": public_key}
        ),
        urlopen=urlopen,
        timeout_seconds=1.25,
    )

    candidate = source.fetch_latest()
    destination = tmp_path / asset_name
    source.download_asset(candidate.assets[0], destination)

    assert candidate.version == "3.6.0"
    assert candidate.release_url == (
        f"{STATIC_MIRROR_BASE_URL}releases/3.6.0/"
    )
    assert candidate.assets[0].download_url == asset_url
    assert destination.read_bytes() == patch_content
    assert requests == [(manifest_url, 1.25), (asset_url, 1.25)]


@pytest.mark.parametrize("signature_status", ["SIGNED", "UNSIGNED"])
def test_signed_mirror_manifest_accepts_full_installer_signature_status(
    signature_status: str,
):
    installer = b"installer authenticated by Ed25519 manifest"
    installer_name = "DcmGet-3.6.0-Setup-x64.exe"
    manifest = {
        "schema_version": 1,
        "product": "DcmGet",
        "platform": "windows-x64",
        "channel": "stable",
        "version": "3.6.0",
        "artifacts": [
            {
                "name": installer_name,
                "kind": "full_installer",
                "size": len(installer),
                "sha256": _sha256(installer),
                "signature_status": signature_status,
            }
        ],
    }
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    envelope = sign_manifest(
        json.dumps(manifest).encode("utf-8"),
        private_key=private_key,
        key_id="test-release",
    )
    manifest_url = f"{STATIC_MIRROR_BASE_URL}stable/{SIGNED_MANIFEST_NAME}"
    source = StaticMirrorReleaseSource(
        signed_manifest_verifier=Ed25519SignedManifestVerifier(
            {"test-release": public_key}
        ),
        urlopen=lambda request, **kwargs: FakeResponse(
            envelope,
            manifest_url,
        ),
    )

    candidate = source.fetch_latest()

    assert candidate.assets[0].signature_status == signature_status


@pytest.mark.parametrize(
    "redirect_url",
    [
        "http://dcmget.v2ex.com.cn/updates/stable/UPDATE-MANIFEST.signed.json",
        "https://dcmget.v2ex.com.cn.evil.example/updates/stable/UPDATE-MANIFEST.signed.json",
        "https://user@dcmget.v2ex.com.cn/updates/stable/UPDATE-MANIFEST.signed.json",
        "https://dcmget.v2ex.com.cn/updates/releases/3.6.0/update.zip",
        "https://dcmget.v2ex.com.cn/updates/stable/../UPDATE-MANIFEST.signed.json",
    ],
)
def test_static_mirror_rejects_redirects_outside_exact_manifest_boundary(
    redirect_url: str,
):
    source = StaticMirrorReleaseSource(
        signed_manifest_verifier=lambda content: content,
        urlopen=lambda request, **kwargs: FakeResponse(b"signed", redirect_url),
    )

    with pytest.raises(UpdateSecurityError, match="越界重定向"):
        source.fetch_latest()


@pytest.mark.parametrize(
    "download_url",
    [
        "http://dcmget.v2ex.com.cn/updates/releases/3.6.0/update.zip",
        "https://dcmget.v2ex.com.cn.evil.example/updates/releases/3.6.0/update.zip",
        "https://user@dcmget.v2ex.com.cn/updates/releases/3.6.0/update.zip",
        "https://dcmget.v2ex.com.cn:443/updates/releases/3.6.0/update.zip",
        "https://dcmget.v2ex.com.cn/updates/releases/3.6.0/../update.zip",
        "https://dcmget.v2ex.com.cn/updates/releases/3.6.0/update.zip?token=x",
        "https://dcmget.v2ex.com.cn/updates/releases/v3.6.0/update.zip",
    ],
)
def test_static_mirror_rejects_untrusted_asset_url_boundaries(
    tmp_path: Path,
    download_url: str,
):
    source = StaticMirrorReleaseSource(
        signed_manifest_verifier=lambda content: content,
        urlopen=lambda request, **kwargs: pytest.fail("network must not be used"),
    )
    asset = replace(
        _asset(b"patch", name="update.zip"),
        download_url=download_url,
    )

    with pytest.raises(UpdateSecurityError, match="路径边界"):
        source.download_asset(asset, tmp_path / "update.zip")


@pytest.mark.parametrize(
    "download_url",
    [
        (
            f"{STATIC_MIRROR_BASE_URL}releases/3.7.0/"
            "DcmGet-3.6.0-from-3.5.2-component.zip"
        ),
        f"{STATIC_MIRROR_BASE_URL}releases/3.6.0/other.zip",
    ],
)
def test_service_rejects_mirror_url_not_bound_to_candidate(
    tmp_path: Path,
    download_url: str,
):
    asset = replace(_asset(b"patch"), download_url=download_url)
    source = FakeSource(UpdateCandidate("3.6.0", (asset,)), {})
    service = _service(tmp_path, source, FakeScheduler())

    status = service.check(wait=True)

    assert status["phase"] == "error"
    assert "路径与候选版本不一致" in str(status["error"])


@pytest.mark.parametrize("failure_kind", ["network", "not_found"])
def test_mirror_first_source_falls_back_only_when_mirror_is_unavailable(
    failure_kind: str,
):
    candidate = UpdateCandidate("3.6.0", (_asset(b"patch"),))
    github = FakeSource(candidate, {})

    def unavailable(request, **kwargs):
        if failure_kind == "not_found":
            raise HTTPError(request.full_url, 404, "Not Found", None, None)
        raise URLError("offline")

    mirror = StaticMirrorReleaseSource(
        signed_manifest_verifier=lambda content: content,
        urlopen=unavailable,
    )
    source = MirrorFirstReleaseSource(mirror, github)

    assert source.fetch_latest() is candidate
    assert github.fetch_count == 1


@pytest.mark.parametrize("failure_kind", ["signature", "manifest"])
def test_mirror_first_source_fails_closed_on_mirror_security_errors(
    failure_kind: str,
):
    candidate = UpdateCandidate("3.6.0", (_asset(b"patch"),))
    github = FakeSource(candidate, {})
    manifest_url = (
        f"{STATIC_MIRROR_BASE_URL}stable/{SIGNED_MANIFEST_NAME}"
    )

    def verifier(content: bytes) -> bytes:
        if failure_kind == "signature":
            raise UpdateSecurityError("bad signature")
        manifest = _signed_update_manifest()
        manifest["product"] = "OtherProduct"
        return json.dumps(manifest).encode("utf-8")

    mirror = StaticMirrorReleaseSource(
        signed_manifest_verifier=verifier,
        urlopen=lambda request, **kwargs: FakeResponse(b"signed", manifest_url),
    )
    source = MirrorFirstReleaseSource(mirror, github)

    with pytest.raises(UpdateSecurityError):
        source.fetch_latest()
    assert github.fetch_count == 0


def test_mirror_first_source_dispatches_download_by_trusted_asset_url(
    tmp_path: Path,
):
    content = b"patch"
    github_asset = _asset(content)
    mirror_asset = replace(
        github_asset,
        download_url=(
            f"{STATIC_MIRROR_BASE_URL}releases/3.6.0/{github_asset.name}"
        ),
    )
    candidate = UpdateCandidate("3.6.0", (github_asset,))
    mirror = FakeSource(candidate, {github_asset.name: content})
    github = FakeSource(candidate, {github_asset.name: content})
    source = MirrorFirstReleaseSource(mirror, github)

    source.download_asset(mirror_asset, tmp_path / "mirror.zip")
    source.download_asset(github_asset, tmp_path / "github.zip")

    assert mirror.download_count == 1
    assert github.download_count == 1
    untrusted = replace(
        github_asset,
        download_url="https://evil.example/update.zip",
    )
    with pytest.raises(UpdateSecurityError, match="可信更新源"):
        source.download_asset(untrusted, tmp_path / "evil.zip")


@pytest.mark.parametrize("failure_kind", ["timeout", "not_found"])
def test_mirror_asset_unavailable_falls_back_to_matching_github_asset(
    tmp_path: Path,
    failure_kind: str,
):
    content = b"patch"
    github_asset = _asset(content)
    mirror_asset = replace(
        github_asset,
        download_url=(
            f"{STATIC_MIRROR_BASE_URL}releases/3.6.0/{github_asset.name}"
        ),
    )

    def unavailable(request, **kwargs):
        if failure_kind == "not_found":
            raise HTTPError(request.full_url, 404, "Not Found", None, None)
        raise TimeoutError("mirror timed out")

    mirror = StaticMirrorReleaseSource(
        signed_manifest_verifier=lambda content: content,
        urlopen=unavailable,
    )
    github = FakeSource(
        UpdateCandidate("3.6.0", (github_asset,)),
        {github_asset.name: content},
    )
    source = MirrorFirstReleaseSource(mirror, github)
    destination = tmp_path / "update.zip"

    source.download_asset(mirror_asset, destination)

    assert destination.read_bytes() == content
    assert github.fetch_count == 1
    assert github.download_count == 1


def test_mirror_asset_fallback_removes_partial_file_and_requires_same_manifest(
    tmp_path: Path,
):
    content = b"patch"
    github_asset = _asset(content)
    mirror_asset = replace(
        github_asset,
        download_url=(
            f"{STATIC_MIRROR_BASE_URL}releases/3.6.0/{github_asset.name}"
        ),
    )

    class PartialMirror(FakeSource):
        def download_asset(self, asset: UpdateAsset, destination: Path) -> None:
            self.download_count += 1
            destination.write_bytes(b"partial")
            raise UpdateNetworkError("mirror interrupted")

    candidate = UpdateCandidate("3.6.0", (github_asset,))
    mirror = PartialMirror(candidate, {})
    github = FakeSource(candidate, {github_asset.name: content})
    source = MirrorFirstReleaseSource(mirror, github)
    destination = tmp_path / "update.zip"

    source.download_asset(mirror_asset, destination)

    assert destination.read_bytes() == content
    assert mirror.download_count == 1
    assert github.download_count == 1

    component = github_asset.component_files[0]
    mismatches = {
        "kind": replace(github_asset, kind="full_installer"),
        "size": replace(github_asset, size=github_asset.size + 1),
        "sha256": replace(github_asset, sha256="0" * 64),
        "signature_status": replace(github_asset, signature_status="SIGNED"),
        "base_version": replace(github_asset, base_version="3.5.1"),
        "preserves_user_data": replace(
            github_asset,
            preserves_user_data=False,
        ),
        "content_scope": replace(github_asset, content_scope=""),
        "base_tree": replace(github_asset, base_tree_sha256="1" * 64),
        "target_tree": replace(github_asset, target_tree_sha256="2" * 64),
        "files": replace(
            github_asset,
            component_files=(replace(component, size=component.size + 1),),
        ),
    }
    for label, mismatched in mismatches.items():
        rejecting_github = FakeSource(
            UpdateCandidate("3.6.0", (mismatched,)),
            {mismatched.name: content},
        )
        rejecting_source = MirrorFirstReleaseSource(mirror, rejecting_github)
        rejected_destination = tmp_path / f"rejected-{label}.zip"

        with pytest.raises(UpdateSecurityError, match="签名清单不一致"):
            rejecting_source.download_asset(mirror_asset, rejected_destination)
        assert not rejected_destination.exists()
        assert rejecting_github.fetch_count == 1
        assert rejecting_github.download_count == 0

    renamed = replace(github_asset, name="other.zip")
    renamed_github = FakeSource(
        UpdateCandidate("3.6.0", (renamed,)),
        {renamed.name: content},
    )
    with pytest.raises(UpdateSecurityError, match="唯一的同名"):
        MirrorFirstReleaseSource(mirror, renamed_github).download_asset(
            mirror_asset,
            tmp_path / "renamed.zip",
        )
    assert renamed_github.download_count == 0

    newer_github = FakeSource(
        UpdateCandidate("3.7.0", (github_asset,)),
        {github_asset.name: content},
    )
    with pytest.raises(UpdateSecurityError, match="版本与镜像版本不一致"):
        MirrorFirstReleaseSource(mirror, newer_github).download_asset(
            mirror_asset,
            tmp_path / "newer.zip",
        )
    assert newer_github.download_count == 0


def test_mirror_asset_security_error_never_falls_back_to_github(tmp_path: Path):
    content = b"patch"
    github_asset = _asset(content)
    mirror_asset = replace(
        github_asset,
        download_url=(
            f"{STATIC_MIRROR_BASE_URL}releases/3.6.0/{github_asset.name}"
        ),
    )
    candidate = UpdateCandidate("3.6.0", (github_asset,))

    class UnsafeMirror(FakeSource):
        def download_asset(self, asset: UpdateAsset, destination: Path) -> None:
            self.download_count += 1
            raise UpdateSecurityError("mirror redirect escaped boundary")

    mirror = UnsafeMirror(candidate, {})
    github = FakeSource(candidate, {github_asset.name: content})
    source = MirrorFirstReleaseSource(mirror, github)

    with pytest.raises(UpdateSecurityError, match="escaped boundary"):
        source.download_asset(mirror_asset, tmp_path / "update.zip")
    assert mirror.download_count == 1
    assert github.fetch_count == 0
    assert github.download_count == 0


@pytest.mark.parametrize("release_tag", ["v3.6.0", "component-v3.6.0"])
def test_github_source_only_parses_manifest_after_injected_pkcs7_verification(
    release_tag: str,
):
    patch_content = b"patch payload"
    manifest = {
        "schema_version": 1,
        "product": "DcmGet",
        "platform": "windows-x64",
        "channel": "stable",
        "version": "3.6.0",
        "artifacts": [
            {
                "name": "DcmGet-3.6.0-from-3.5.2-component.zip",
                "kind": "component_patch",
                "size": len(patch_content),
                "sha256": _sha256(patch_content),
                "base_version": "3.5.2",
                "preserves_user_data": True,
                "content_scope": "application",
                "base_tree_sha256": _tree_digest({}),
                "target_tree_sha256": _tree_digest(
                    {
                        "DcmGet.exe": (
                            len(b"changed executable"),
                            _sha256(b"changed executable"),
                        )
                    }
                ),
                "files": [
                    {
                        "path": "DcmGet.exe",
                        "size": len(b"changed executable"),
                        "sha256": _sha256(b"changed executable"),
                        "base_missing": True,
                    }
                ],
            }
        ],
    }
    manifest_url = (
        "https://github.com/ge2009/dcmget/releases/download/"
        f"{release_tag}/{SIGNED_MANIFEST_NAME}"
    )
    patch_url = (
        f"https://github.com/ge2009/dcmget/releases/download/{release_tag}/"
        "DcmGet-3.6.0-from-3.5.2-component.zip"
    )
    release = {
        "tag_name": release_tag,
        "draft": False,
        "prerelease": False,
        "html_url": f"https://github.com/ge2009/dcmget/releases/tag/{release_tag}",
        "assets": [
            {
                "name": SIGNED_MANIFEST_NAME,
                "browser_download_url": manifest_url,
            },
            {
                "name": "DcmGet-3.6.0-from-3.5.2-component.zip",
                "browser_download_url": patch_url,
            },
        ],
    }
    verified_inputs: list[bytes] = []

    def verifier(content: bytes) -> bytes:
        verified_inputs.append(content)
        return json.dumps(manifest).encode("utf-8")

    def urlopen(request, **kwargs):
        url = request.full_url
        if url.endswith("/releases/latest"):
            return FakeResponse(json.dumps(release).encode("utf-8"), url)
        if url == manifest_url:
            return FakeResponse(b"pkcs7 envelope, not json", url)
        raise AssertionError(url)

    source = GitHubReleaseSource(
        signed_manifest_verifier=verifier,
        urlopen=urlopen,
    )

    candidate = source.fetch_latest()

    assert verified_inputs == [b"pkcs7 envelope, not json"]
    assert candidate.version == "3.6.0"
    assert candidate.assets[0].kind == "component_patch"


@pytest.mark.parametrize(
    "release_tag",
    [
        "3.6.0",
        "v3.6",
        "v3.6.0-extra",
        "component-component-v3.6.0",
        "component-v3.6.1",
    ],
)
def test_github_source_rejects_unsafe_or_manifest_mismatched_release_tags(
    release_tag: str,
):
    installer = b"signed installer"
    manifest = {
        "schema_version": 1,
        "product": "DcmGet",
        "platform": "windows-x64",
        "channel": "stable",
        "version": "3.6.0",
        "artifacts": [
            {
                "name": "DcmGet-3.6.0-Setup-x64.exe",
                "kind": "full_installer",
                "size": len(installer),
                "sha256": _sha256(installer),
                "signature_status": "SIGNED",
            }
        ],
    }
    manifest_url = (
        "https://github.com/ge2009/dcmget/releases/download/"
        f"v3.6.0/{SIGNED_MANIFEST_NAME}"
    )
    installer_url = (
        "https://github.com/ge2009/dcmget/releases/download/"
        "v3.6.0/DcmGet-3.6.0-Setup-x64.exe"
    )
    release = {
        "tag_name": release_tag,
        "draft": False,
        "prerelease": False,
        "html_url": "",
        "assets": [
            {
                "name": SIGNED_MANIFEST_NAME,
                "browser_download_url": manifest_url,
            },
            {
                "name": "DcmGet-3.6.0-Setup-x64.exe",
                "browser_download_url": installer_url,
            },
        ],
    }

    def urlopen(request, **kwargs):
        url = request.full_url
        if url.endswith("/releases/latest"):
            return FakeResponse(json.dumps(release).encode("utf-8"), url)
        if url == manifest_url:
            return FakeResponse(b"pkcs7 envelope", url)
        raise AssertionError(url)

    source = GitHubReleaseSource(
        signed_manifest_verifier=lambda _: json.dumps(manifest).encode("utf-8"),
        urlopen=urlopen,
    )

    with pytest.raises(UpdateSecurityError, match="标签与签名清单版本不一致"):
        source.fetch_latest()


def test_github_source_rejects_unsigned_json_and_non_github_redirects():
    release_url = "https://api.github.com/repos/ge2009/dcmget/releases/latest"

    def offline(request, **kwargs):
        raise URLError("private network")

    source = GitHubReleaseSource(
        signed_manifest_verifier=lambda value: value,
        urlopen=offline,
    )
    with pytest.raises(UpdateNetworkError, match="连接失败"):
        source.fetch_latest()

    response = FakeResponse(b"payload", "https://evil.example/update")
    with pytest.raises(UpdateSecurityError, match="重定向"):
        source._validate_final_url(response)
    assert release_url.startswith("https://api.github.com/")


def test_windows_pkcs7_verifier_checks_same_signer_and_extracts_content(
    tmp_path: Path,
):
    executable = tmp_path / "DcmGet.exe"
    executable.write_bytes(b"exe")
    commands: list[list[str]] = []

    def runner(command, **kwargs):
        commands.append(command)
        Path(kwargs["env"]["DCMGET_UPDATE_JSON"]).write_bytes(b'{"version":"3.6.0"}')
        return subprocess.CompletedProcess(command, 0, "", "")

    verifier = WindowsSignedManifestVerifier(
        executable,
        tmp_path / "verify",
        platform_name="win32",
        runner=runner,
    )

    assert verifier(b"signed") == b'{"version":"3.6.0"}'
    script = commands[0][-1]
    assert "SignedCms" in script
    assert "CheckSignature($true)" in script
    assert "Thumbprint" in script
    assert "Get-AuthenticodeSignature" in script


def test_authenticode_verifier_requires_valid_same_signer(tmp_path: Path):
    current = tmp_path / "DcmGet.exe"
    update = tmp_path / "DcmGet-3.6.0-Setup-x64.exe"
    current.write_bytes(b"current")
    update.write_bytes(b"update")
    commands: list[list[str]] = []

    def runner(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    verifier = WindowsAuthenticodeVerifier(
        current,
        platform_name="win32",
        runner=runner,
    )
    verifier(update, _asset(b"update", name=update.name, kind="full_installer", base_version=None))

    assert commands[0][0] == "powershell.exe"
    assert "Get-AuthenticodeSignature" in commands[0][-1]
    assert "Thumbprint" in commands[0][-1]


def test_scheduled_task_runs_installer_as_system_without_direct_launch(
    tmp_path: Path,
):
    staging = SecureStagingDirectory(tmp_path / "updates")
    package_root = staging.prepare("3.6.0")
    package = package_root / "DcmGet-3.6.0-Setup-x64.exe"
    package.write_bytes(b"installer")
    commands: list[list[str]] = []

    def runner(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    scheduler = WindowsScheduledTaskUpdater(
        staging,
        platform_name="win32",
        runner=runner,
        now=lambda: datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc),
    )
    scheduler.schedule(
        ApplyRequest(package, "3.5.2", "3.6.0", "full_installer")
    )

    assert len(commands) == 1
    assert commands[0][0] == "schtasks.exe"
    assert commands[0][1] == "/Create"
    xml_path = Path(commands[0][commands[0].index("/XML") + 1])
    xml = xml_path.read_text(encoding="utf-16")
    assert "S-1-5-18" in xml
    assert str(package) in xml
    assert "/VERYSILENT" in xml
    assert "/NORESTART" in xml
    assert "DeleteExpiredTaskAfter" in xml


def test_scheduled_task_can_apply_allowlisted_component_patch_with_rollback(
    tmp_path: Path,
):
    patch, package_content = _component_package(b"new signed app")
    staging = SecureStagingDirectory(tmp_path / "updates")
    package_root = staging.prepare("3.6.0")
    package = package_root / patch.name
    package.write_bytes(package_content)
    install_directory = tmp_path / "Program Files" / "DcmGet"
    install_directory.mkdir(parents=True)
    (install_directory / "DcmGet.exe").write_bytes(b"previous executable")
    commands: list[list[str]] = []

    def runner(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    scheduler = WindowsScheduledTaskUpdater(
        staging,
        install_directory=install_directory,
        platform_name="win32",
        runner=runner,
        now=lambda: datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc),
    )
    assert scheduler.supports_component_patch is True
    scheduler.schedule(
        ApplyRequest(
            package,
            "3.5.2",
            "3.6.0",
            "component_patch",
            component_files=patch.component_files,
            base_tree_sha256=patch.base_tree_sha256,
            target_tree_sha256=patch.target_tree_sha256,
        )
    )

    assert len(commands) == 1
    xml_path = Path(commands[0][commands[0].index("/XML") + 1])
    xml = xml_path.read_text(encoding="utf-16")
    assert "powershell.exe" in xml
    scripts = list(package_root.glob("apply-*.ps1"))
    assert len(scripts) == 1
    script = scripts[0].read_text(encoding="utf-8-sig")
    assert "kayisoft-dcmget" in script
    assert "$restartService" in script
    assert "Get-FileHash" in script
    assert "Get-DcmGetApplicationTreeDigest" in script
    assert "base_tree_sha256" in script
    assert "target_tree_sha256" in script
    assert "rollback application tree mismatch" in script
    assert "$backupRoot" in script
    payloads = list(package_root.glob("payload-*/DcmGet.exe"))
    assert len(payloads) == 1
    assert payloads[0].read_bytes() == b"new signed app"


def test_component_patch_rejects_extra_or_config_files(tmp_path: Path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("app/DcmGet.exe", b"app")
        archive.writestr("app/config.json", b"do not overwrite")
    package_content = stream.getvalue()
    asset = _asset(
        package_content,
        component_files=(
            ComponentFile(
                "DcmGet.exe", 3, _sha256(b"app"), base_missing=True
            ),
        ),
    )
    source = FakeSource(
        UpdateCandidate("3.6.0", (asset,)),
        {asset.name: package_content},
    )
    service = _service(tmp_path, source, FakeScheduler())

    service.check(wait=True)
    status = service.download(wait=True)

    assert status["phase"] == "error"
    assert "额外文件" in str(status["error"])


def test_component_patch_rejects_embedded_tree_digest_mismatch(tmp_path: Path):
    patch, original = _component_package()
    source_archive = zipfile.ZipFile(io.BytesIO(original))
    manifest = json.loads(source_archive.read("PATCH-MANIFEST.json"))
    manifest["target_tree_sha256"] = "0" * 64
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("PATCH-MANIFEST.json", json.dumps(manifest))
        archive.writestr("DcmGet.exe", source_archive.read("DcmGet.exe"))
    source_archive.close()
    tampered_package = stream.getvalue()
    outer_asset = _asset(
        tampered_package,
        component_files=patch.component_files,
    )
    source = FakeSource(
        UpdateCandidate("3.6.0", (outer_asset,)),
        {outer_asset.name: tampered_package},
    )
    service = _service(tmp_path, source, FakeScheduler())

    service.check(wait=True)
    status = service.download(wait=True)

    assert status["phase"] == "error"
    assert "应用树指纹" in str(status["error"])


def test_secure_staging_rejects_symbolic_links(tmp_path: Path):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "updates"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(UpdateSecurityError, match="不安全|重解析点"):
        SecureStagingDirectory(link).prepare("3.6.0")


def test_production_factory_is_quietly_unsupported_off_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "program-data"))
    service = create_windows_update_service(
        tmp_path / "state",
        tmp_path / "install",
        "3.5.2",
    )
    try:
        assert isinstance(service._source, StaticMirrorReleaseSource)
        assert service._source._timeout == 3.0
        assert isinstance(
            service._source._verifier,
            Ed25519SignedManifestVerifier,
        )
        assert (
            service._source._verifier._trusted_public_keys
            == TRUSTED_UPDATE_PUBLIC_KEYS
        )
        assert service._authenticode_verifier is None
        assert service.status()["state"] == "unsupported"
        assert service.check()["state"] == "unsupported"
        assert not (tmp_path / "program-data").exists()
    finally:
        service.close()
