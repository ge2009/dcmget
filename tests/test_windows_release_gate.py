from __future__ import annotations

import hashlib
import json
import struct
import subprocess
from pathlib import Path

import pytest

from dcmget.architecture import (
    ArchitectureError,
    IMAGE_FILE_MACHINE_AMD64,
    IMAGE_FILE_MACHINE_ARM64,
    IMAGE_FILE_MACHINE_I386,
)
from scripts.build_windows import WINDOWS_DCMTK_PE_FILES
from scripts.windows_release_gate import (
    AuthenticodeConfig,
    ReleaseGateError,
    SignatureStatus,
    run_release_gate,
    sign_windows_files,
    validate_windows_release_runtime,
    verify_amd64_files,
    verify_dcmtk_allowlist,
    verify_windows_files,
)


def _write_pe(path: Path, machine: int = IMAGE_FILE_MACHINE_AMD64) -> Path:
    content = bytearray(256)
    content[:2] = b"MZ"
    struct.pack_into("<I", content, 0x3C, 0x80)
    content[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", content, 0x84, machine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _write_dcmtk_allowlist(root: Path) -> Path:
    root.mkdir(parents=True)
    for name in WINDOWS_DCMTK_PE_FILES:
        _write_pe(root / name)
    return root


def test_release_runtime_requires_amd64_python_but_allows_x64_emulation(
    tmp_path: Path,
):
    amd64_python = _write_pe(tmp_path / "python-amd64.exe")
    arm64_python = _write_pe(
        tmp_path / "python-arm64.exe", IMAGE_FILE_MACHINE_ARM64
    )
    x86_python = _write_pe(tmp_path / "python-x86.exe", IMAGE_FILE_MACHINE_I386)

    validate_windows_release_runtime(
        platform_name="win32", executable=amd64_python, pointer_bits=64
    )
    with pytest.raises(ArchitectureError, match="64 位"):
        validate_windows_release_runtime(
            platform_name="win32", executable=amd64_python, pointer_bits=32
        )
    with pytest.raises(ArchitectureError, match="ARM64"):
        validate_windows_release_runtime(
            platform_name="win32", executable=arm64_python, pointer_bits=64
        )
    with pytest.raises(ArchitectureError, match="x86/32-bit"):
        validate_windows_release_runtime(
            platform_name="win32", executable=x86_python, pointer_bits=64
        )
    with pytest.raises(ReleaseGateError, match="只能在 Windows"):
        validate_windows_release_runtime(
            platform_name="darwin", executable=amd64_python, pointer_bits=64
        )


def test_amd64_file_gate_rejects_x86_and_arm64(tmp_path: Path):
    amd64 = _write_pe(tmp_path / "app-amd64.exe")
    x86 = _write_pe(tmp_path / "app-x86.exe", IMAGE_FILE_MACHINE_I386)
    arm64 = _write_pe(tmp_path / "app-arm64.exe", IMAGE_FILE_MACHINE_ARM64)

    assert verify_amd64_files([amd64, amd64]) == (amd64.resolve(),)
    with pytest.raises(ArchitectureError, match="x86/32-bit"):
        verify_amd64_files([x86])
    with pytest.raises(ArchitectureError, match="ARM64"):
        verify_amd64_files([arm64])


def test_dcmtk_gate_requires_exact_amd64_allowlist(tmp_path: Path):
    valid = _write_dcmtk_allowlist(tmp_path / "valid")
    verified = verify_dcmtk_allowlist(valid)

    assert [path.name for path in verified] == list(WINDOWS_DCMTK_PE_FILES)

    extra = _write_dcmtk_allowlist(tmp_path / "extra")
    _write_pe(extra / "dcmj2pnm.exe")
    with pytest.raises(ReleaseGateError, match="多出 dcmj2pnm.exe"):
        verify_dcmtk_allowlist(extra)

    missing = _write_dcmtk_allowlist(tmp_path / "missing")
    (missing / "storescp.exe").unlink()
    with pytest.raises(ReleaseGateError, match="缺少 storescp.exe"):
        verify_dcmtk_allowlist(missing)

    wrong_architecture = _write_dcmtk_allowlist(tmp_path / "wrong-architecture")
    _write_pe(wrong_architecture / "dcmnet.dll", IMAGE_FILE_MACHINE_I386)
    with pytest.raises(ArchitectureError, match="x86/32-bit"):
        verify_dcmtk_allowlist(wrong_architecture)


def test_unsigned_mode_is_explicit_and_never_invokes_signtool(tmp_path: Path):
    executable = _write_pe(tmp_path / "DcmGet.exe")

    def unexpected_runner(*args, **kwargs):
        raise AssertionError("unsigned mode must not invoke signtool")

    statuses = sign_windows_files(
        [executable],
        AuthenticodeConfig(signtool=None),
        runner=unexpected_runner,
    )

    assert statuses == {executable.resolve(): SignatureStatus.UNSIGNED}


def test_pfx_signing_uses_sha256_timestamp_and_verifies(tmp_path: Path):
    executable = _write_pe(tmp_path / "DcmGet.exe")
    signtool = tmp_path / "signtool.exe"
    signtool.write_bytes(b"tool")
    certificate = tmp_path / "certificate.pfx"
    certificate.write_bytes(b"certificate")
    commands: list[list[str]] = []
    powershell_environments: list[dict[str, str]] = []

    def successful_runner(command, **kwargs):
        commands.append(command)
        if command[0] == "powershell.exe":
            powershell_environments.append(kwargs["env"])
            output = "A" * 40
        else:
            output = "ok"
        return subprocess.CompletedProcess(command, 0, output, "")

    config = AuthenticodeConfig(
        signtool=signtool,
        certificate_path=certificate,
        certificate_password="secret",
        timestamp_url="https://timestamp.example.test",
    )
    statuses = sign_windows_files([executable], config, runner=successful_runner)

    assert statuses[executable.resolve()] is SignatureStatus.SIGNED
    assert commands[0][0] == "powershell.exe"
    pfx_script = commands[0][-1]
    assert (
        "TypeName = 'System.Security.Cryptography.X509Certificates.X509Certificate2'"
        in pfx_script
    )
    assert "$arguments = @(" in pfx_script
    assert "ArgumentList = $arguments" in pfx_script
    assert "New-Object @parameters" in pfx_script
    assert "X509Certificate2(" not in pfx_script
    assert commands[1:3] == [
        [
            str(signtool),
            "sign",
            "/fd",
            "SHA256",
            "/f",
            str(certificate),
            "/p",
            "secret",
            "/tr",
            "https://timestamp.example.test",
            "/td",
            "SHA256",
            str(executable.resolve()),
        ],
        [
            str(signtool),
            "verify",
            "/pa",
            "/all",
            "/v",
            str(executable.resolve()),
        ],
    ]
    assert commands[3][0] == "powershell.exe"
    assert "Get-AuthenticodeSignature" in commands[3][-1]
    assert powershell_environments[0]["DCMGET_PFX_PATH"] == str(
        certificate.resolve()
    )
    assert powershell_environments[0]["DCMGET_PFX_PASSWORD"] == "secret"
    assert powershell_environments[1]["DCMGET_SIGNED_FILE"] == str(
        executable.resolve()
    )


def test_existing_signature_mode_only_verifies_exact_artifact(tmp_path: Path):
    executable = _write_pe(tmp_path / "DcmGet.exe")
    signtool = tmp_path / "signtool.exe"
    signtool.write_bytes(b"tool")
    commands: list[list[str]] = []

    def successful_runner(command, **kwargs):
        commands.append(command)
        output = "A" * 40 if command[0] == "powershell.exe" else "ok"
        return subprocess.CompletedProcess(command, 0, output, "")

    config = AuthenticodeConfig(
        signtool=signtool,
        certificate_sha1="A" * 40,
    )

    statuses = verify_windows_files(
        [executable], config, runner=successful_runner
    )

    assert statuses == {executable.resolve(): SignatureStatus.SIGNED}
    assert commands[0:1] == [
        [
            str(signtool),
            "verify",
            "/pa",
            "/all",
            "/v",
            str(executable.resolve()),
        ]
    ]
    assert commands[1][0] == "powershell.exe"
    assert "Get-AuthenticodeSignature" in commands[1][-1]


def test_existing_signature_rejects_different_trusted_signer(tmp_path: Path):
    executable = _write_pe(tmp_path / "DcmGet.exe")
    signtool = tmp_path / "signtool.exe"
    signtool.write_bytes(b"tool")

    def wrong_signer_runner(command, **kwargs):
        output = "B" * 40 if command[0] == "powershell.exe" else "verified"
        return subprocess.CompletedProcess(command, 0, output, "")

    with pytest.raises(ReleaseGateError, match="签名者证书不匹配"):
        verify_windows_files(
            [executable],
            AuthenticodeConfig(
                signtool=signtool,
                certificate_sha1="A" * 40,
            ),
            runner=wrong_signer_runner,
        )


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"DCMGET_SIGN_CERTIFICATE_PASSWORD": "secret"}, "未设置"),
        ({"DCMGET_SIGN_TIMESTAMP_URL": "https://timestamp.test"}, "未配置"),
        (
            {
                "DCMGET_SIGN_CERTIFICATE_PATH": "certificate.pfx",
                "DCMGET_SIGN_CERTIFICATE_SHA1": "A" * 40,
            },
            "不能同时",
        ),
        ({"DCMGET_SIGN_CERTIFICATE_SHA1": "not-a-thumbprint"}, "40 位"),
    ],
)
def test_partial_signing_environment_is_rejected(
    tmp_path: Path,
    environment: dict[str, str],
    message: str,
):
    if "DCMGET_SIGN_CERTIFICATE_PATH" in environment:
        certificate = tmp_path / environment["DCMGET_SIGN_CERTIFICATE_PATH"]
        certificate.write_bytes(b"certificate")
        environment = {
            **environment,
            "DCMGET_SIGN_CERTIFICATE_PATH": str(certificate),
        }

    with pytest.raises(ReleaseGateError, match=message):
        AuthenticodeConfig.from_environment(environment, which=lambda name: None)


def test_configured_certificate_requires_signtool(tmp_path: Path):
    certificate = tmp_path / "certificate.pfx"
    certificate.write_bytes(b"certificate")

    with pytest.raises(ReleaseGateError, match="找不到 signtool"):
        AuthenticodeConfig.from_environment(
            {"DCMGET_SIGN_CERTIFICATE_PATH": str(certificate)},
            which=lambda name: None,
        )


def test_signtool_failure_stops_release(tmp_path: Path):
    executable = _write_pe(tmp_path / "DcmGet.exe")
    signtool = tmp_path / "signtool.exe"
    signtool.write_bytes(b"tool")

    def failed_runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "certificate error")

    with pytest.raises(ReleaseGateError, match="certificate error"):
        sign_windows_files(
            [executable],
            AuthenticodeConfig(signtool=signtool, certificate_sha1="A" * 40),
            runner=failed_runner,
        )


def test_release_gate_writes_post_sign_manifest_and_sha256(tmp_path: Path):
    release = tmp_path / "release"
    release.mkdir()
    portable = _write_pe(release / "DcmGet-3.0.0-portable-x64.exe")
    # Inno Setup's installer bootstrap is x86-compatible by design.
    installer = _write_pe(
        release / "DcmGet-3.0.0-Setup-x64.exe", IMAGE_FILE_MACHINE_I386
    )
    archive = release / "DcmGet-3.0.0-Windows-x64.zip"
    archive.write_bytes(b"archive")
    app = _write_pe(tmp_path / "onedir" / "DcmGet.exe")
    dcmtk_bin = _write_dcmtk_allowlist(tmp_path / "dcmtk-bin")

    result = run_release_gate(
        release_directory=release,
        version="3.0.0",
        amd64_files=[app],
        dcmtk_bin_directories=[dcmtk_bin],
        authenticode=AuthenticodeConfig(signtool=None),
        validate_host=False,
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    records = {record["name"]: record for record in manifest["artifacts"]}
    assert result.signing_status is SignatureStatus.UNSIGNED
    assert manifest["signing"] == {
        "gate_action": "sign_and_verify",
        "method": "none",
        "status": "UNSIGNED",
        "timestamped": False,
    }
    assert records[portable.name]["amd64_verified"] is True
    assert records[portable.name]["signature_status"] == "UNSIGNED"
    assert records[installer.name]["kind"] == "installer"
    assert records[installer.name]["amd64_verified"] is False
    assert records[installer.name]["signature_status"] == "UNSIGNED"
    assert records[archive.name]["signature_status"] == "NOT_APPLICABLE"
    assert manifest["dcmtk_bin_allowlist"] == list(WINDOWS_DCMTK_PE_FILES)

    checksum_lines = result.checksums_path.read_text(encoding="ascii").splitlines()
    checksums = {
        relative: digest
        for digest, relative in (line.split("  ", 1) for line in checksum_lines)
    }
    assert set(checksums) == {
        portable.name,
        installer.name,
        archive.name,
        "RELEASE-MANIFEST.json",
    }
    for relative, digest in checksums.items():
        assert digest == hashlib.sha256((release / relative).read_bytes()).hexdigest()


def test_release_gate_rejects_unlisted_x86_executable(tmp_path: Path):
    release = tmp_path / "release"
    release.mkdir()
    _write_pe(release / "DcmGet-portable-x64.exe", IMAGE_FILE_MACHINE_I386)
    app = _write_pe(tmp_path / "onedir" / "DcmGet.exe")
    dcmtk_bin = _write_dcmtk_allowlist(tmp_path / "dcmtk-bin")

    with pytest.raises(ArchitectureError, match="x86/32-bit"):
        run_release_gate(
            release_directory=release,
            version="3.0.0",
            amd64_files=[app],
            dcmtk_bin_directories=[dcmtk_bin],
            authenticode=AuthenticodeConfig(signtool=None),
            validate_host=False,
        )


def test_release_artifact_must_stay_inside_release_directory(tmp_path: Path):
    release = tmp_path / "release"
    release.mkdir()
    outside = _write_pe(tmp_path / "outside.exe")
    app = _write_pe(tmp_path / "onedir" / "DcmGet.exe")
    dcmtk_bin = _write_dcmtk_allowlist(tmp_path / "dcmtk-bin")

    with pytest.raises(ReleaseGateError, match="不在发布目录"):
        run_release_gate(
            release_directory=release,
            version="3.0.0",
            amd64_files=[app],
            dcmtk_bin_directories=[dcmtk_bin],
            artifacts=[outside],
            authenticode=AuthenticodeConfig(signtool=None),
            validate_host=False,
        )
