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
    INSTALL_PATH_ALLOWLIST,
    PATCH_MANIFEST_NAME,
    UPDATE_MANIFEST_NAME,
    UPDATE_SIGNATURE_NAME,
    WindowsUpdateBuildError,
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
