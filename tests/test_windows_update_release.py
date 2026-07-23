from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import zipfile
from pathlib import Path

import pytest

from dcmget.architecture import IMAGE_FILE_MACHINE_AMD64
from dcmget.windows_update import _validated_candidate
from scripts.build_windows_update import (
    COMPONENT_BASELINE_NAME,
    MAX_COMPONENT_BASELINES,
    ComponentBaseline,
    INSTALL_PATH_ALLOWLIST,
    PATCH_MANIFEST_NAME,
    UPDATE_MANIFEST_NAME,
    UPDATE_SIGNATURE_NAME,
    WindowsUpdateBuildError,
    _component_chain_anchors,
    build_windows_update_release,
)
from scripts.windows_release_gate import AuthenticodeConfig


def _write_pe(path: Path, *, suffix: bytes = b"") -> Path:
    content = bytearray(256)
    content[:2] = b"MZ"
    struct.pack_into("<I", content, 0x3C, 0x80)
    content[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", content, 0x84, IMAGE_FILE_MACHINE_AMD64)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(content) + suffix)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _install_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        content_sha256 = _sha256(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(path.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(content_sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _signed_release_fixture(
    root: Path,
    *,
    version: str,
) -> tuple[Path, Path, Path]:
    release = root / "release"
    release.mkdir(parents=True)
    installer = release / f"DcmGet-{version}-Setup-x64.exe"
    installer.write_bytes(f"signed installer {version}".encode())
    manifest = {
        "schema_version": 1,
        "product": "DcmGet",
        "version": version,
        "platform": "windows-x64",
        "signing": {
            "status": "SIGNED",
            "timestamped": True,
        },
        "artifacts": [
            {
                "name": installer.name,
                "relative_path": installer.name,
                "kind": "installer",
                "size": installer.stat().st_size,
                "sha256": _sha256(installer),
                "signature_status": "SIGNED",
                "amd64_verified": False,
            }
        ],
    }
    manifest_path = release / "RELEASE-MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return release, installer, manifest_path


def _install_root(root: Path, *, changed: bool = False) -> Path:
    install = root / "DcmGet"
    _write_pe(
        install / "DcmGet.exe",
        suffix=b"target" if changed else b"baseline",
    )
    internal = install / "_internal"
    internal.mkdir(parents=True)
    (internal / "unchanged.dat").write_bytes(b"same")
    return install


def _signing_fixture(root: Path) -> AuthenticodeConfig:
    signtool = root / "signtool.exe"
    signtool.write_bytes(b"signtool")
    return AuthenticodeConfig(
        signtool=signtool,
        certificate_sha1="A" * 40,
        timestamp_url="https://timestamp.example.test",
    )


def _successful_signtool_runner(commands: list[list[str]]):
    def runner(command, **kwargs):
        commands.append(list(command))
        if command[0] == "powershell.exe":
            environment = kwargs.get("env", {})
            if "DCMGET_BASE_UPDATE_CONTENT" in environment:
                Path(environment["DCMGET_BASE_UPDATE_CONTENT"]).write_bytes(
                    Path(environment["DCMGET_BASE_UPDATE_P7"])
                    .with_suffix("")
                    .read_bytes()
                )
                return subprocess.CompletedProcess(command, 0, "ok", "")
            return subprocess.CompletedProcess(command, 0, "A" * 40, "")
        if command[1] == "sign" and "/p7" in command:
            manifest = Path(command[-1])
            manifest.with_name(manifest.name + ".p7").write_bytes(
                b"signed-pkcs7"
            )
        return subprocess.CompletedProcess(command, 0, "ok", "")

    return runner


def _build_base_update(
    root: Path,
    *,
    version: str,
    compatibility_file: Path,
    signing: AuthenticodeConfig,
    runner,
) -> tuple[Path, Path, Path]:
    release, installer, release_manifest = _signed_release_fixture(
        root, version=version
    )
    install = _install_root(root)
    result = build_windows_update_release(
        release_directory=release,
        version=version,
        install_root=install,
        release_manifest_path=release_manifest,
        full_installer=installer,
        compatibility_files=[compatibility_file],
        compatibility_root=compatibility_file.parent,
        authenticode=signing,
        runner=runner,
    )
    return install, result.manifest_path, result.signature_path


def _build_full_baseline(
    root: Path,
    *,
    version: str,
    suffix: bytes,
    compatibility_file: Path,
    signing: AuthenticodeConfig,
    runner,
) -> ComponentBaseline:
    release, installer, release_manifest = _signed_release_fixture(
        root, version=version
    )
    install = _install_root(root)
    _write_pe(install / "DcmGet.exe", suffix=suffix)
    result = build_windows_update_release(
        release_directory=release,
        version=version,
        install_root=install,
        release_manifest_path=release_manifest,
        full_installer=installer,
        compatibility_files=[compatibility_file],
        compatibility_root=compatibility_file.parent,
        authenticode=signing,
        runner=runner,
    )
    assert result.baseline_snapshot_path is not None
    return ComponentBaseline(
        version=version,
        install_root=install,
        update_manifest=result.manifest_path,
        update_signature=result.signature_path,
    )


def test_update_manifest_requires_signed_timestamped_x64_release(tmp_path: Path):
    release, installer, release_manifest = _signed_release_fixture(
        tmp_path, version="3.6.0"
    )
    manifest = json.loads(release_manifest.read_text(encoding="utf-8"))
    manifest["signing"]["status"] = "UNSIGNED"
    release_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    install = _install_root(tmp_path)

    with pytest.raises(WindowsUpdateBuildError, match="已通过 Authenticode"):
        build_windows_update_release(
            release_directory=release,
            version="3.6.0",
            install_root=install,
            release_manifest_path=release_manifest,
            full_installer=installer,
            authenticode=_signing_fixture(tmp_path),
            runner=_successful_signtool_runner([]),
        )


def test_full_update_manifest_is_pkcs7_signed_and_lists_exact_installer(
    tmp_path: Path,
):
    release, installer, release_manifest = _signed_release_fixture(
        tmp_path, version="3.6.0"
    )
    install = _install_root(tmp_path)
    compatibility = tmp_path / "packaging" / "windows" / "dcmget.iss"
    compatibility.parent.mkdir(parents=True)
    compatibility.write_text("stable layout", encoding="utf-8")
    commands: list[list[str]] = []

    result = build_windows_update_release(
        release_directory=release,
        version="3.6.0",
        install_root=install,
        release_manifest_path=release_manifest,
        full_installer=installer,
        compatibility_files=[compatibility],
        compatibility_root=tmp_path,
        authenticode=_signing_fixture(tmp_path),
        runner=_successful_signtool_runner(commands),
    )

    assert result.manifest_path.name == UPDATE_MANIFEST_NAME
    assert result.signature_path.name == UPDATE_SIGNATURE_NAME
    assert result.signature_path.read_bytes() == b"signed-pkcs7"
    assert result.component_patch_path is None
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["product"] == "DcmGet"
    assert manifest["version"] == "3.6.0"
    assert manifest["platform"] == "windows-x64"
    assert manifest["layout_version"] == 1
    assert manifest["install_tree_sha256"] == _install_tree_sha256(install)
    assert manifest["component_patches"] == []
    assert manifest["full_installer"] == {
        "name": installer.name,
        "kind": "full_installer",
        "size": installer.stat().st_size,
        "sha256": _sha256(installer),
        "signature_status": "SIGNED",
        "preserves_user_data": True,
        "content_scope": "application",
        "source_release_manifest_kind": "installer",
    }
    assert manifest["artifacts"] == [manifest["full_installer"]]
    assert manifest["manifest_signature"] == {
        "name": UPDATE_SIGNATURE_NAME,
        "kind": "pkcs7_signed_data",
        "content_encoding": "Embedded",
        "digest_algorithm": "SHA256",
        "timestamped": True,
    }
    sign_command = next(
        command
        for command in commands
        if len(command) > 1 and command[1] == "sign"
    )
    assert sign_command[-7:] == [
        "/td",
        "SHA256",
        "/p7",
        str(release),
        "/p7ce",
        "Embedded",
        str(result.manifest_path),
    ]
    verify_command = commands[-1]
    assert verify_command[1:3] == ["verify", "/p7"]
    assert verify_command[-2:] == ["/v", str(result.signature_path)]
    checksum_lines = (release / "SHA256SUMS.txt").read_text(
        encoding="ascii"
    )
    assert UPDATE_MANIFEST_NAME in checksum_lines
    assert UPDATE_SIGNATURE_NAME in checksum_lines


def test_component_patch_only_contains_changed_allowlisted_files(tmp_path: Path):
    compatibility = tmp_path / "layout" / "dcmget.iss"
    compatibility.parent.mkdir(parents=True)
    compatibility.write_text("layout v1", encoding="utf-8")
    signing = _signing_fixture(tmp_path)
    commands: list[list[str]] = []
    runner = _successful_signtool_runner(commands)
    baseline_install, base_manifest, base_signature = _build_base_update(
        tmp_path / "base",
        version="3.5.2",
        compatibility_file=compatibility,
        signing=signing,
        runner=runner,
    )

    target_root = tmp_path / "target"
    release, installer, release_manifest = _signed_release_fixture(
        target_root, version="3.6.0"
    )
    target_install = _install_root(target_root, changed=True)
    (target_install / "_internal" / "new-resource.dat").write_bytes(b"new")
    result = build_windows_update_release(
        release_directory=release,
        version="3.6.0",
        install_root=target_install,
        release_manifest_path=release_manifest,
        full_installer=installer,
        compatibility_files=[compatibility],
        compatibility_root=compatibility.parent,
        enable_component_patch=True,
        baseline_install_root=baseline_install,
        base_version="3.5.2",
        base_update_manifest=base_manifest,
        base_update_signature=base_signature,
        authenticode=signing,
        runner=runner,
    )

    assert result.component_patch_path is not None
    assert [record.path for record in result.changed_files] == [
        "DcmGet.exe",
        "_internal/new-resource.dat",
    ]
    with zipfile.ZipFile(result.component_patch_path) as archive:
        assert archive.namelist() == [
            PATCH_MANIFEST_NAME,
            "DcmGet.exe",
            "_internal/new-resource.dat",
        ]
        patch_manifest = json.loads(archive.read(PATCH_MANIFEST_NAME))
        assert patch_manifest["base_version"] == "3.5.2"
        assert patch_manifest["version"] == "3.6.0"
        assert patch_manifest["install_path_allowlist"] == list(
            INSTALL_PATH_ALLOWLIST
        )
        assert patch_manifest["removed_paths"] == []
        assert [item["path"] for item in patch_manifest["files"]] == [
            "DcmGet.exe",
            "_internal/new-resource.dat",
        ]
        replaced, added = patch_manifest["files"]
        assert replaced["base_missing"] is False
        assert replaced["base_size"] == (baseline_install / "DcmGet.exe").stat().st_size
        assert replaced["base_sha256"] == _sha256(
            baseline_install / "DcmGet.exe"
        )
        assert added["base_missing"] is True
        assert "base_sha256" not in added
        assert "base_size" not in added
        assert archive.read("_internal/new-resource.dat") == b"new"
    update_manifest = json.loads(
        result.manifest_path.read_text(encoding="utf-8")
    )
    patch_record = update_manifest["component_patches"][0]
    assert patch_record["kind"] == "component_patch"
    assert patch_record["signature_status"] == "NOT_APPLICABLE"
    assert patch_record["preserves_user_data"] is True
    assert patch_record["content_scope"] == "application"
    assert patch_record["base_version"] == "3.5.2"
    assert patch_record["size"] == result.component_patch_path.stat().st_size
    assert patch_record["sha256"] == _sha256(result.component_patch_path)
    base_update = json.loads(base_manifest.read_text(encoding="utf-8"))
    assert patch_record["base_tree_sha256"] == base_update[
        "install_tree_sha256"
    ]
    assert patch_record["target_tree_sha256"] == update_manifest[
        "install_tree_sha256"
    ]
    candidate = _validated_candidate(
        update_manifest,
        {
            item["name"]: (
                "https://github.com/ge2009/dcmget/releases/"
                f"download/v3.6.0/{item['name']}"
            )
            for item in update_manifest["artifacts"]
        },
        release_url="https://github.com/ge2009/dcmget/releases/tag/v3.6.0",
    )
    assert candidate.preferred_asset("3.5.2").name == patch_record["name"]
    executable_verify = next(
        command
        for command in commands
        if len(command) > 1
        and command[1] == "verify"
        and command[-1].endswith("DcmGet.exe")
    )
    assert executable_verify[1:4] == ["verify", "/pa", "/all"]


def test_component_patch_refuses_removed_installed_file(tmp_path: Path):
    compatibility = tmp_path / "layout.txt"
    compatibility.write_text("same", encoding="utf-8")
    signing = _signing_fixture(tmp_path)
    runner = _successful_signtool_runner([])
    baseline_install, base_manifest, base_signature = _build_base_update(
        tmp_path / "base",
        version="3.5.2",
        compatibility_file=compatibility,
        signing=signing,
        runner=runner,
    )
    release, installer, release_manifest = _signed_release_fixture(
        tmp_path / "target", version="3.6.0"
    )
    target_install = _install_root(tmp_path / "target", changed=True)
    (target_install / "_internal" / "unchanged.dat").unlink()

    with pytest.raises(WindowsUpdateBuildError, match="不允许删除"):
        build_windows_update_release(
            release_directory=release,
            version="3.6.0",
            install_root=target_install,
            release_manifest_path=release_manifest,
            full_installer=installer,
            compatibility_files=[compatibility],
            compatibility_root=compatibility.parent,
            enable_component_patch=True,
            baseline_install_root=baseline_install,
            base_version="3.5.2",
            base_update_manifest=base_manifest,
            base_update_signature=base_signature,
            authenticode=signing,
            runner=runner,
        )


def test_component_patch_refuses_layout_or_full_install_input_change(
    tmp_path: Path,
):
    compatibility = tmp_path / "layout.txt"
    compatibility.write_text("old layout", encoding="utf-8")
    signing = _signing_fixture(tmp_path)
    runner = _successful_signtool_runner([])
    baseline_install, base_manifest, base_signature = _build_base_update(
        tmp_path / "base",
        version="3.5.2",
        compatibility_file=compatibility,
        signing=signing,
        runner=runner,
    )
    compatibility.write_text("new layout", encoding="utf-8")
    release, installer, release_manifest = _signed_release_fixture(
        tmp_path / "target", version="3.6.0"
    )
    target_install = _install_root(tmp_path / "target", changed=True)

    with pytest.raises(WindowsUpdateBuildError, match="改用完整安装包"):
        build_windows_update_release(
            release_directory=release,
            version="3.6.0",
            install_root=target_install,
            release_manifest_path=release_manifest,
            full_installer=installer,
            compatibility_files=[compatibility],
            compatibility_root=compatibility.parent,
            enable_component_patch=True,
            baseline_install_root=baseline_install,
            base_version="3.5.2",
            base_update_manifest=base_manifest,
            base_update_signature=base_signature,
            authenticode=signing,
            runner=runner,
        )


def test_component_patch_rejects_base_json_that_does_not_match_pkcs7(
    tmp_path: Path,
):
    compatibility = tmp_path / "layout.txt"
    compatibility.write_text("same", encoding="utf-8")
    signing = _signing_fixture(tmp_path)
    normal_runner = _successful_signtool_runner([])
    baseline_install, base_manifest, base_signature = _build_base_update(
        tmp_path / "base",
        version="3.5.2",
        compatibility_file=compatibility,
        signing=signing,
        runner=normal_runner,
    )
    release, installer, release_manifest = _signed_release_fixture(
        tmp_path / "target", version="3.6.0"
    )
    target_install = _install_root(tmp_path / "target", changed=True)

    def mismatched_runner(command, **kwargs):
        environment = kwargs.get("env", {})
        if "DCMGET_BASE_UPDATE_CONTENT" in environment:
            Path(environment["DCMGET_BASE_UPDATE_CONTENT"]).write_bytes(
                b'{"tampered":true}'
            )
            return subprocess.CompletedProcess(command, 0, "ok", "")
        return normal_runner(command, **kwargs)

    with pytest.raises(WindowsUpdateBuildError, match="已签内容不一致"):
        build_windows_update_release(
            release_directory=release,
            version="3.6.0",
            install_root=target_install,
            release_manifest_path=release_manifest,
            full_installer=installer,
            compatibility_files=[compatibility],
            compatibility_root=compatibility.parent,
            enable_component_patch=True,
            baseline_install_root=baseline_install,
            base_version="3.5.2",
            base_update_manifest=base_manifest,
            base_update_signature=base_signature,
            authenticode=signing,
            runner=mismatched_runner,
        )


def test_component_patch_rejects_baseline_zip_tree_drift(tmp_path: Path):
    compatibility = tmp_path / "layout.txt"
    compatibility.write_text("same", encoding="utf-8")
    signing = _signing_fixture(tmp_path)
    runner = _successful_signtool_runner([])
    baseline_install, base_manifest, base_signature = _build_base_update(
        tmp_path / "base",
        version="3.5.2",
        compatibility_file=compatibility,
        signing=signing,
        runner=runner,
    )
    (baseline_install / "_internal" / "unchanged.dat").write_bytes(
        b"drifted after signed baseline"
    )
    release, installer, release_manifest = _signed_release_fixture(
        tmp_path / "target", version="3.6.0"
    )
    target_install = _install_root(tmp_path / "target", changed=True)

    with pytest.raises(WindowsUpdateBuildError, match="ZIP 安装树"):
        build_windows_update_release(
            release_directory=release,
            version="3.6.0",
            install_root=target_install,
            release_manifest_path=release_manifest,
            full_installer=installer,
            compatibility_files=[compatibility],
            compatibility_root=compatibility.parent,
            enable_component_patch=True,
            baseline_install_root=baseline_install,
            base_version="3.5.2",
            base_update_manifest=base_manifest,
            base_update_signature=base_signature,
            authenticode=signing,
            runner=runner,
        )


@pytest.mark.parametrize(
    "relative",
    [
        "config.json",
        "tasks.sqlite3",
        "logs/dcmget.log",
        "downloads/image.dcm",
        "license.json",
        "_internal/config.json",
        "_internal/tasks.sqlite3",
    ],
)
def test_component_inventory_rejects_user_state_and_non_install_paths(
    tmp_path: Path,
    relative: str,
):
    release, installer, release_manifest = _signed_release_fixture(
        tmp_path, version="3.6.0"
    )
    install = _install_root(tmp_path)
    forbidden = install / relative
    forbidden.parent.mkdir(parents=True, exist_ok=True)
    forbidden.write_bytes(b"must never ship")

    with pytest.raises(WindowsUpdateBuildError, match="白名单之外"):
        build_windows_update_release(
            release_directory=release,
            version="3.6.0",
            install_root=install,
            release_manifest_path=release_manifest,
            full_installer=installer,
            authenticode=_signing_fixture(tmp_path),
            runner=_successful_signtool_runner([]),
        )


def test_patch_only_release_builds_direct_patches_from_recent_baselines(
    tmp_path: Path,
):
    compatibility = tmp_path / "layout.txt"
    compatibility.write_text("stable layout", encoding="utf-8")
    signing = _signing_fixture(tmp_path)
    runner = _successful_signtool_runner([])
    older = _build_full_baseline(
        tmp_path / "base-older",
        version="3.5.0",
        suffix=b"3.5.0",
        compatibility_file=compatibility,
        signing=signing,
        runner=runner,
    )
    newer = _build_full_baseline(
        tmp_path / "base-newer",
        version="3.5.9",
        suffix=b"3.5.9",
        compatibility_file=compatibility,
        signing=signing,
        runner=runner,
    )
    release = tmp_path / "target" / "release"
    release.mkdir(parents=True)
    install = _install_root(tmp_path / "target")
    _write_pe(install / "DcmGet.exe", suffix=b"3.6.0")

    result = build_windows_update_release(
        release_directory=release,
        version="3.6.0",
        install_root=install,
        compatibility_files=[compatibility],
        compatibility_root=compatibility.parent,
        baselines=[older, newer],
        patch_only=True,
        authenticode=signing,
        runner=runner,
    )

    assert [path.name for path in result.component_patch_paths] == [
        "DcmGet-3.6.0-windows-x64-components-from-3.5.9.zip",
        "DcmGet-3.6.0-windows-x64-components-from-3.5.0.zip",
    ]
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["full_installer"] is None
    assert manifest["artifacts"] == manifest["component_patches"]
    assert [item["base_version"] for item in manifest["component_patches"]] == [
        "3.5.9",
        "3.5.0",
    ]
    for base_version in ("3.5.9", "3.5.0"):
        candidate = _validated_candidate(
            manifest,
            {
                item["name"]: f"https://example.test/{item['name']}"
                for item in manifest["artifacts"]
            },
            release_url="https://example.test/release",
            allowed_asset_url=lambda _url: True,
        )
        assert candidate.preferred_asset(base_version).base_version == base_version

    assert result.baseline_snapshot_path == release / COMPONENT_BASELINE_NAME
    assert result.baseline_snapshot_path.is_file()
    with zipfile.ZipFile(result.baseline_snapshot_path) as archive:
        assert PATCH_MANIFEST_NAME not in archive.namelist()
        assert "DcmGet.exe" in archive.namelist()
        assert all(
            name == "DcmGet.exe" or name.startswith("_internal/")
            for name in archive.namelist()
        )
    checksums = (release / "SHA256SUMS.txt").read_text(encoding="ascii")
    assert COMPONENT_BASELINE_NAME not in checksums


def test_patch_only_manifest_can_be_a_verified_baseline_for_next_release(
    tmp_path: Path,
):
    compatibility = tmp_path / "layout.txt"
    compatibility.write_text("stable layout", encoding="utf-8")
    signing = _signing_fixture(tmp_path)
    runner = _successful_signtool_runner([])
    full_base = _build_full_baseline(
        tmp_path / "full-base",
        version="3.5.0",
        suffix=b"3.5.0",
        compatibility_file=compatibility,
        signing=signing,
        runner=runner,
    )

    middle_release = tmp_path / "middle" / "release"
    middle_release.mkdir(parents=True)
    middle_install = _install_root(tmp_path / "middle")
    _write_pe(middle_install / "DcmGet.exe", suffix=b"3.5.1")
    middle = build_windows_update_release(
        release_directory=middle_release,
        version="3.5.1",
        install_root=middle_install,
        compatibility_files=[compatibility],
        compatibility_root=compatibility.parent,
        baselines=[full_base],
        patch_only=True,
        authenticode=signing,
        runner=runner,
    )
    assert middle.baseline_snapshot_path is not None
    expanded = tmp_path / "middle-expanded"
    with zipfile.ZipFile(middle.baseline_snapshot_path) as archive:
        archive.extractall(expanded)
    patch_only_base = ComponentBaseline(
        version="3.5.1",
        install_root=expanded,
        update_manifest=middle.manifest_path,
        update_signature=middle.signature_path,
    )

    target_release = tmp_path / "target" / "release"
    target_release.mkdir(parents=True)
    target_install = _install_root(tmp_path / "target")
    _write_pe(target_install / "DcmGet.exe", suffix=b"3.6.0")
    result = build_windows_update_release(
        release_directory=target_release,
        version="3.6.0",
        install_root=target_install,
        compatibility_files=[compatibility],
        compatibility_root=compatibility.parent,
        baselines=[patch_only_base],
        patch_only=True,
        authenticode=signing,
        runner=runner,
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["full_installer"] is None
    assert [item["base_version"] for item in manifest["component_patches"]] == [
        "3.5.1"
    ]
    assert [
        item["version"]
        for item in manifest["component_chain"]["root_full_releases"]
    ] == ["3.5.0"]


def test_component_release_keeps_only_five_most_recent_direct_baselines(
    tmp_path: Path,
):
    compatibility = tmp_path / "layout.txt"
    compatibility.write_text("stable layout", encoding="utf-8")
    signing = _signing_fixture(tmp_path)
    runner = _successful_signtool_runner([])
    baselines = [
        _build_full_baseline(
            tmp_path / f"base-{patch}",
            version=f"3.5.{patch}",
            suffix=f"3.5.{patch}".encode(),
            compatibility_file=compatibility,
            signing=signing,
            runner=runner,
        )
        for patch in range(6)
    ]
    release = tmp_path / "target" / "release"
    release.mkdir(parents=True)
    install = _install_root(tmp_path / "target")
    _write_pe(install / "DcmGet.exe", suffix=b"3.6.0")

    result = build_windows_update_release(
        release_directory=release,
        version="3.6.0",
        install_root=install,
        compatibility_files=[compatibility],
        compatibility_root=compatibility.parent,
        baselines=list(reversed(baselines)),
        patch_only=True,
        authenticode=signing,
        runner=runner,
    )

    assert len(result.component_patch_paths) == MAX_COMPONENT_BASELINES
    assert [version for version, _files in result.changed_files_by_base] == [
        "3.5.5",
        "3.5.4",
        "3.5.3",
        "3.5.2",
        "3.5.1",
    ]


def test_patch_only_release_requires_a_signed_full_release_chain_anchor(
    tmp_path: Path,
):
    release = tmp_path / "release"
    release.mkdir()
    install = _install_root(tmp_path)

    with pytest.raises(WindowsUpdateBuildError, match="首个组件更新基线"):
        build_windows_update_release(
            release_directory=release,
            version="3.6.0",
            install_root=install,
            patch_only=True,
            authenticode=_signing_fixture(tmp_path),
            runner=_successful_signtool_runner([]),
        )


def test_patch_only_chain_anchor_must_precede_the_baseline_version():
    tree = hashlib.sha256(b"tree").hexdigest()
    manifest = {
        "artifacts": [{"kind": "component_patch"}],
        "component_chain": {
            "schema_version": 1,
            "root_full_releases": [
                {"version": "3.5.1", "install_tree_sha256": tree}
            ],
        },
    }

    with pytest.raises(WindowsUpdateBuildError, match="必须低于基线版本"):
        _component_chain_anchors(
            manifest,
            version="3.5.1",
            install_tree_sha256=tree,
        )


def test_windows_workflow_publishes_only_explicit_authenticated_release():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    assert "publish_release:" in workflow
    assert "component_update:" in workflow
    assert workflow.count("default: false") >= 2
    assert "if: ${{ inputs.publish_release }}" in workflow
    assert "if: ${{ inputs.publish_release && inputs.component_update }}" in workflow
    assert "scripts/build_windows_update.py" in workflow
    assert "UPDATE-MANIFEST.json.p7" in workflow
    assert "--base-update-signature" in workflow
    assert "name: Publish signed Windows x64 Release" in workflow
    assert "runs-on: windows-2025" in workflow
    assert "contents: write" in workflow
    assert "only timestamped SIGNED releases may be published" in workflow
    assert "Verify PKCS#7 manifest and signer before publication" in workflow
    assert "does not match the signed PKCS#7 content" in workflow
    assert "update manifest has no valid install tree SHA-256" in workflow
    assert "standalone archive tree does not match signed update manifest" in workflow
    assert "component patch target tree does not match release tree" in workflow
    assert "refusing to replace published update assets" in workflow


def test_component_workflow_is_manual_patch_only_and_skips_full_build_stages():
    root = Path(__file__).resolve().parents[1]
    workflow = (
        root / ".github/workflows/windows-component-update.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "publish_update:" in workflow
    assert "default: false" in workflow
    assert "if: ${{ inputs.publish_update }}" in workflow
    assert "DCMGET_SIGN_CERTIFICATE_BASE64 is required" in workflow
    assert "DCMGET_SIGN_CERTIFICATE_PASSWORD is required" in workflow
    assert "--update-payload-only" in workflow
    assert "--patch-only" in workflow
    assert '"--baseline"' in workflow
    assert "component-baseline.zip" in workflow
    assert "if ($baselines.Count -ge 5)" in workflow
    assert "Publish one signed full release first" in workflow
    assert 'tag="component-v${{ inputs.version }}"' in workflow
    assert 'tag="v${{ inputs.version }}"' not in workflow
    assert "Build current one-click installer" not in workflow
    assert "Build standalone and portable EXE" not in workflow
    assert "Prepare Chinese installer language" not in workflow
    assert "Download VC++ Runtime" not in workflow
    assert "Smoke test portable application" not in workflow
    assert "Upgrade installer" not in workflow
    assert "Uninstaller" not in workflow


def test_release_workflows_accept_only_signed_version_matched_baselines():
    root = Path(__file__).resolve().parents[1]
    component_workflow = (
        root / ".github/workflows/windows-component-update.yml"
    ).read_text(encoding="utf-8")
    full_workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    for workflow in (component_workflow, full_workflow):
        assert "component-baseline.zip" in workflow
        assert "System.Security.Cryptography.Pkcs.SignedCms" in workflow
        assert "$cms.CheckSignature($true)" in workflow
        assert "(?:component-)?v(?<version>\\d+\\.\\d+\\.\\d+)" in workflow

    assert "Baseline release tag and signed manifest version mismatch" in (
        component_workflow
    )
    assert "Baseline $baseVersion signed manifest content mismatch" in (
        component_workflow
    )
    assert "Previous release tag and signed manifest version mismatch" in (
        full_workflow
    )
    assert "Previous signed manifest content mismatch" in full_workflow
    assert '"DcmGet-$tagVersion-windows-x64.zip"' in full_workflow
    assert "Previous signed baseline version must be lower than target version" in (
        full_workflow
    )
