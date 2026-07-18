#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

try:
    from scripts.build_windows import WINDOWS_DCMTK_PE_FILES
except ModuleNotFoundError:  # direct execution from the scripts directory
    from build_windows import WINDOWS_DCMTK_PE_FILES

from dcmget.architecture import (  # noqa: E402
    ensure_supported_runtime,
    require_amd64_pe,
)


SCHEMA_VERSION = 1
PLATFORM = "windows-x64"
DEFAULT_MANIFEST_NAME = "RELEASE-MANIFEST.json"
DEFAULT_CHECKSUMS_NAME = "SHA256SUMS.txt"
_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
_THUMBPRINT_PATTERN = re.compile(r"[0-9A-F]{40}")
_POWERSHELL = "powershell.exe"
_PFX_THUMBPRINT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
$arguments = @(
    $env:DCMGET_PFX_PATH,
    $env:DCMGET_PFX_PASSWORD,
    $flags
)
$parameters = @{
    TypeName = 'System.Security.Cryptography.X509Certificates.X509Certificate2'
    ArgumentList = $arguments
}
$cert = New-Object @parameters
[Console]::Out.WriteLine($cert.Thumbprint)
""".strip()
_FILE_THUMBPRINT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$signature = Get-AuthenticodeSignature -LiteralPath $env:DCMGET_SIGNED_FILE
if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    throw "Authenticode status is $($signature.Status)"
}
if ($null -eq $signature.SignerCertificate) {
    throw 'Signer certificate is missing'
}
[Console]::Out.WriteLine($signature.SignerCertificate.Thumbprint)
""".strip()


class ReleaseGateError(RuntimeError):
    pass


class SignatureStatus(str, Enum):
    SIGNED = "SIGNED"
    UNSIGNED = "UNSIGNED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class AuthenticodeConfig:
    signtool: Path | None
    certificate_path: Path | None = None
    certificate_password: str = ""
    certificate_sha1: str = ""
    timestamp_url: str = ""

    @property
    def configured(self) -> bool:
        return self.certificate_path is not None or bool(self.certificate_sha1)

    @property
    def method(self) -> str:
        if self.certificate_path is not None:
            return "pfx"
        if self.certificate_sha1:
            return "certificate_store"
        return "none"

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        which: Callable[[str], str | None] = shutil.which,
    ) -> "AuthenticodeConfig":
        env = os.environ if environment is None else environment
        tool_value = env.get("DCMGET_SIGNTOOL_PATH", "").strip()
        certificate_value = env.get("DCMGET_SIGN_CERTIFICATE_PATH", "").strip()
        password = env.get("DCMGET_SIGN_CERTIFICATE_PASSWORD", "")
        thumbprint = "".join(
            env.get("DCMGET_SIGN_CERTIFICATE_SHA1", "").split()
        ).upper()
        timestamp_url = env.get("DCMGET_SIGN_TIMESTAMP_URL", "").strip()

        if certificate_value and thumbprint:
            raise ReleaseGateError(
                "签名配置不能同时使用 PFX 文件和证书存储指纹"
            )
        if thumbprint and _THUMBPRINT_PATTERN.fullmatch(thumbprint) is None:
            raise ReleaseGateError("DCMGET_SIGN_CERTIFICATE_SHA1 必须是 40 位十六进制指纹")
        if password and not certificate_value:
            raise ReleaseGateError(
                "设置了证书密码，但未设置 DCMGET_SIGN_CERTIFICATE_PATH"
            )
        if timestamp_url and not (certificate_value or thumbprint):
            raise ReleaseGateError("设置了时间戳地址，但未配置签名证书")

        certificate = Path(certificate_value).expanduser() if certificate_value else None
        if certificate is not None and not certificate.is_file():
            raise ReleaseGateError(f"签名证书文件不存在：{certificate}")

        configured = certificate is not None or bool(thumbprint)
        tool_path: Path | None = None
        if tool_value:
            tool_path = Path(tool_value).expanduser()
            if not tool_path.is_file():
                raise ReleaseGateError(f"signtool 不存在：{tool_path}")
        elif configured:
            discovered = which("signtool.exe") or which("signtool")
            if not discovered:
                raise ReleaseGateError(
                    "已配置签名证书，但找不到 signtool；"
                    "请设置 DCMGET_SIGNTOOL_PATH"
                )
            tool_path = Path(discovered)

        return cls(
            signtool=tool_path,
            certificate_path=certificate,
            certificate_password=password,
            certificate_sha1=thumbprint,
            timestamp_url=timestamp_url,
        )


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    name: str
    relative_path: str
    kind: str
    size: int
    sha256: str
    signature_status: SignatureStatus
    amd64_verified: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
            "signature_status": self.signature_status.value,
            "amd64_verified": self.amd64_verified,
        }


@dataclass(frozen=True, slots=True)
class ReleaseGateResult:
    manifest_path: Path
    checksums_path: Path
    signing_status: SignatureStatus
    artifacts: tuple[ReleaseArtifact, ...]
    verified_amd64_files: tuple[Path, ...]
    verified_dcmtk_directories: tuple[Path, ...]


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def validate_windows_release_runtime(
    *,
    platform_name: str | None = None,
    executable: str | Path | None = None,
    pointer_bits: int | None = None,
) -> None:
    """Require AMD64 Python, including under Windows ARM64 x64 emulation."""

    current_platform = sys.platform if platform_name is None else platform_name
    if current_platform != "win32":
        raise ReleaseGateError("Windows 发布前校验只能在 Windows 上执行")
    ensure_supported_runtime(
        platform_name="win32",
        executable=sys.executable if executable is None else executable,
        pointer_bits=pointer_bits,
    )


def verify_amd64_files(paths: Iterable[str | Path]) -> tuple[Path, ...]:
    verified: list[Path] = []
    seen: set[Path] = set()
    for value in paths:
        path = Path(value).expanduser().resolve()
        if path in seen:
            continue
        if not path.is_file() or path.is_symlink():
            raise ReleaseGateError(f"AMD64 校验文件不存在或类型无效：{path}")
        require_amd64_pe(path, path.name)
        seen.add(path)
        verified.append(path)
    if not verified:
        raise ReleaseGateError("没有提供需要校验的 AMD64 Windows 构件")
    return tuple(verified)


def verify_dcmtk_allowlist(bin_directory: str | Path) -> tuple[Path, ...]:
    root = Path(bin_directory).expanduser().resolve()
    if root.is_symlink() or not root.is_dir():
        raise ReleaseGateError(f"DCMTK bin 目录不存在或类型无效：{root}")
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise ReleaseGateError(f"无法读取 DCMTK bin 目录：{root}") from exc
    invalid = [path.name for path in entries if path.is_symlink() or not path.is_file()]
    if invalid:
        raise ReleaseGateError(
            "DCMTK bin 包含不允许的目录项：" + "、".join(sorted(invalid))
        )
    actual = {path.name for path in entries}
    expected = set(WINDOWS_DCMTK_PE_FILES)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append("缺少 " + "、".join(missing))
        if extra:
            details.append("多出 " + "、".join(extra))
        raise ReleaseGateError("DCMTK 精简 allowlist 不匹配：" + "；".join(details))
    ordered = tuple(root / name for name in WINDOWS_DCMTK_PE_FILES)
    for path in ordered:
        require_amd64_pe(path, f"DCMTK {path.name}")
    return ordered


def sign_windows_files(
    paths: Iterable[str | Path],
    config: AuthenticodeConfig,
    *,
    runner: CommandRunner = subprocess.run,
) -> dict[Path, SignatureStatus]:
    """Optionally sign PE files, always verifying a configured signature."""

    targets = tuple(
        dict.fromkeys(Path(value).expanduser().resolve() for value in paths)
    )
    for path in targets:
        if path.is_symlink() or not path.is_file():
            raise ReleaseGateError(f"待签名文件不存在或类型无效：{path}")
    if not config.configured:
        return {path: SignatureStatus.UNSIGNED for path in targets}
    if config.signtool is None:
        raise ReleaseGateError("签名证书已配置，但 signtool 未就绪")

    expected_thumbprint = _expected_signer_thumbprint(config, runner)
    statuses: dict[Path, SignatureStatus] = {}
    for path in targets:
        command = [str(config.signtool), "sign", "/fd", "SHA256"]
        if config.certificate_path is not None:
            command.extend(["/f", str(config.certificate_path)])
            if config.certificate_password:
                command.extend(["/p", config.certificate_password])
        else:
            command.extend(["/sha1", config.certificate_sha1])
        if config.timestamp_url:
            command.extend(["/tr", config.timestamp_url, "/td", "SHA256"])
        command.append(str(path))
        _run_signtool(command, path, "签名", runner)
        _run_signtool(
            [str(config.signtool), "verify", "/pa", "/all", "/v", str(path)],
            path,
            "验证签名",
            runner,
        )
        _verify_signer_identity(path, expected_thumbprint, runner)
        statuses[path] = SignatureStatus.SIGNED
    return statuses


def verify_windows_files(
    paths: Iterable[str | Path],
    config: AuthenticodeConfig,
    *,
    runner: CommandRunner = subprocess.run,
) -> dict[Path, SignatureStatus]:
    """Verify signatures already applied to the exact tested artifacts."""

    targets = tuple(
        dict.fromkeys(Path(value).expanduser().resolve() for value in paths)
    )
    for path in targets:
        if path.is_symlink() or not path.is_file():
            raise ReleaseGateError(f"待验证文件不存在或类型无效：{path}")
    if not config.configured:
        return {path: SignatureStatus.UNSIGNED for path in targets}
    if config.signtool is None:
        raise ReleaseGateError("签名证书已配置，但 signtool 未就绪")
    expected_thumbprint = _expected_signer_thumbprint(config, runner)
    statuses: dict[Path, SignatureStatus] = {}
    for path in targets:
        _run_signtool(
            [str(config.signtool), "verify", "/pa", "/all", "/v", str(path)],
            path,
            "验证签名",
            runner,
        )
        _verify_signer_identity(path, expected_thumbprint, runner)
        statuses[path] = SignatureStatus.SIGNED
    return statuses


def run_release_gate(
    *,
    release_directory: str | Path,
    version: str,
    amd64_files: Iterable[str | Path],
    dcmtk_bin_directories: Iterable[str | Path],
    artifacts: Iterable[str | Path] | None = None,
    authenticode: AuthenticodeConfig | None = None,
    runner: CommandRunner = subprocess.run,
    validate_host: bool = True,
    sign_artifacts: bool = True,
) -> ReleaseGateResult:
    if _VERSION_PATTERN.fullmatch(version) is None:
        raise ReleaseGateError("发布版本必须采用 X.Y.Z 格式")
    if validate_host:
        validate_windows_release_runtime()

    release_root = Path(release_directory).expanduser().resolve()
    if release_root.is_symlink() or not release_root.is_dir():
        raise ReleaseGateError(f"Windows 发布目录不存在或类型无效：{release_root}")
    verified_amd64 = verify_amd64_files(amd64_files)
    verified_set = set(verified_amd64)

    dcmtk_roots: list[Path] = []
    for value in dcmtk_bin_directories:
        root = Path(value).expanduser().resolve()
        verify_dcmtk_allowlist(root)
        dcmtk_roots.append(root)
    if not dcmtk_roots:
        raise ReleaseGateError("没有提供需要校验的精简 DCMTK bin 目录")

    artifact_paths = _resolve_artifacts(release_root, artifacts)
    # The Inno Setup launcher is an x86-compatible bootstrap by design.  Every
    # other released executable must itself be AMD64, including the portable EXE.
    for path in artifact_paths:
        if path.suffix.lower() == ".exe" and not _is_inno_setup_bootstrap(path):
            require_amd64_pe(path, f"Windows 发布物 {path.name}")
            verified_set.add(path)

    signing = authenticode or AuthenticodeConfig.from_environment()
    executable_artifacts = [
        path for path in artifact_paths if path.suffix.lower() == ".exe"
    ]
    signature_statuses = (
        sign_windows_files(executable_artifacts, signing, runner=runner)
        if sign_artifacts
        else verify_windows_files(executable_artifacts, signing, runner=runner)
    )
    overall_signing = (
        SignatureStatus.SIGNED if signing.configured else SignatureStatus.UNSIGNED
    )

    records = tuple(
        _artifact_record(
            release_root,
            path,
            signature_statuses.get(path, SignatureStatus.NOT_APPLICABLE),
            path in verified_set,
        )
        for path in artifact_paths
    )
    manifest_path = release_root / DEFAULT_MANIFEST_NAME
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "product": "DcmGet",
        "version": version,
        "platform": PLATFORM,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "build_runtime": {
            "python_version": ".".join(str(value) for value in sys.version_info[:3]),
            "pointer_bits": struct.calcsize("P") * 8,
            "required_pe_machine": "AMD64/x64",
            "windows_arm64_x64_compatibility": True,
        },
        "signing": {
            "status": overall_signing.value,
            "method": signing.method,
            "timestamped": bool(signing.timestamp_url),
            "gate_action": "sign_and_verify" if sign_artifacts else "verify_existing",
        },
        "dcmtk_bin_allowlist": list(WINDOWS_DCMTK_PE_FILES),
        "verified_amd64_files": sorted(path.name for path in verified_set),
        "artifacts": [record.to_dict() for record in records],
    }
    _atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    checksums_path = release_root / DEFAULT_CHECKSUMS_NAME
    checksum_paths = [*artifact_paths, manifest_path]
    _atomic_write_text(
        checksums_path,
        "".join(
            f"{file_sha256(path)}  {path.relative_to(release_root).as_posix()}\n"
            for path in checksum_paths
        ),
        encoding="ascii",
    )
    return ReleaseGateResult(
        manifest_path=manifest_path,
        checksums_path=checksums_path,
        signing_status=overall_signing,
        artifacts=records,
        verified_amd64_files=tuple(sorted(verified_set)),
        verified_dcmtk_directories=tuple(dcmtk_roots),
    )


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_artifacts(
    release_root: Path,
    values: Iterable[str | Path] | None,
) -> tuple[Path, ...]:
    if values is None:
        candidates = [
            path
            for path in release_root.iterdir()
            if path.is_file()
            and path.name not in {DEFAULT_MANIFEST_NAME, DEFAULT_CHECKSUMS_NAME}
            and path.suffix.lower() in {".exe", ".zip"}
        ]
    else:
        candidates = [Path(value).expanduser() for value in values]
    resolved: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate.resolve()
        try:
            path.relative_to(release_root)
        except ValueError as exc:
            raise ReleaseGateError(f"发布物不在发布目录内：{path}") from exc
        if path in seen:
            continue
        if path.is_symlink() or not path.is_file():
            raise ReleaseGateError(f"发布物不存在或类型无效：{path}")
        seen.add(path)
        resolved.append(path)
    if not resolved:
        raise ReleaseGateError("Windows 发布目录中没有可校验的发布物")
    return tuple(sorted(resolved, key=lambda path: path.relative_to(release_root).as_posix()))


def _artifact_record(
    release_root: Path,
    path: Path,
    signature_status: SignatureStatus,
    amd64_verified: bool,
) -> ReleaseArtifact:
    kind = {
        ".exe": "installer" if _is_inno_setup_bootstrap(path) else "executable",
        ".zip": "archive",
    }.get(path.suffix.lower(), "file")
    return ReleaseArtifact(
        name=path.name,
        relative_path=path.relative_to(release_root).as_posix(),
        kind=kind,
        size=path.stat().st_size,
        sha256=file_sha256(path),
        signature_status=signature_status,
        amd64_verified=amd64_verified,
    )


def _is_inno_setup_bootstrap(path: Path) -> bool:
    return bool(re.search(r"-Setup-x64\.exe$", path.name, re.IGNORECASE))


def _run_signtool(
    command: Sequence[str],
    path: Path,
    action: str,
    runner: CommandRunner,
) -> None:
    try:
        completed = runner(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise ReleaseGateError(f"signtool 无法{action} {path.name}：{exc}") from exc
    if completed.returncode:
        detail = "\n".join(
            value.strip()
            for value in (completed.stdout or "", completed.stderr or "")
            if value.strip()
        )
        raise ReleaseGateError(
            f"signtool {action}失败：{path.name}"
            + (f"：{detail}" if detail else "")
        )


def _expected_signer_thumbprint(
    config: AuthenticodeConfig,
    runner: CommandRunner,
) -> str:
    if config.certificate_sha1:
        return config.certificate_sha1.upper()
    if config.certificate_path is None:
        raise ReleaseGateError("无法确定签名证书指纹")
    environment = dict(os.environ)
    environment["DCMGET_PFX_PATH"] = str(config.certificate_path.resolve())
    environment["DCMGET_PFX_PASSWORD"] = config.certificate_password
    return _run_thumbprint_query(
        _PFX_THUMBPRINT_SCRIPT,
        environment,
        "无法读取 PFX 签名证书指纹",
        runner,
    )


def _verify_signer_identity(
    path: Path,
    expected_thumbprint: str,
    runner: CommandRunner,
) -> None:
    environment = dict(os.environ)
    environment["DCMGET_SIGNED_FILE"] = str(path)
    actual = _run_thumbprint_query(
        _FILE_THUMBPRINT_SCRIPT,
        environment,
        f"无法读取 {path.name} 的签名者指纹",
        runner,
    )
    if actual != expected_thumbprint:
        raise ReleaseGateError(
            f"签名者证书不匹配：{path.name}；"
            f"期望 {expected_thumbprint}，实际 {actual}"
        )


def _run_thumbprint_query(
    script: str,
    environment: Mapping[str, str],
    error_prefix: str,
    runner: CommandRunner,
) -> str:
    command = [
        _POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=dict(environment),
        )
    except OSError as exc:
        raise ReleaseGateError(f"{error_prefix}：{exc}") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ReleaseGateError(
            error_prefix + (f"：{detail}" if detail else "")
        )
    matches = _THUMBPRINT_PATTERN.findall((completed.stdout or "").upper())
    if len(matches) != 1:
        raise ReleaseGateError(f"{error_prefix}：PowerShell 未返回唯一 SHA-1 指纹")
    return matches[0]


def _atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding=encoding, newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _path(value: str) -> Path:
    return Path(value).expanduser()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验、可选签名并生成 DcmGet Windows x64 发布清单"
    )
    parser.add_argument("--release-dir", type=_path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--amd64-pe",
        type=_path,
        action="append",
        required=True,
        help="必须是 AMD64 的应用或运行时 PE，可重复指定",
    )
    parser.add_argument(
        "--dcmtk-bin",
        type=_path,
        action="append",
        required=True,
        help="必须精确匹配最小 allowlist 的 DCMTK bin，可重复指定",
    )
    parser.add_argument(
        "--artifact",
        type=_path,
        action="append",
        help="发布物；省略时自动选择发布目录中的 EXE 和 ZIP",
    )
    parser.add_argument(
        "--verify-existing-signatures",
        action="store_true",
        help="不再次签名，只复核已经测试过的发布物签名",
    )
    args = parser.parse_args(argv)
    result = run_release_gate(
        release_directory=args.release_dir,
        version=args.version,
        amd64_files=args.amd64_pe,
        dcmtk_bin_directories=args.dcmtk_bin,
        artifacts=args.artifact,
        sign_artifacts=not args.verify_existing_signatures,
    )
    print(f"SIGNING_STATUS={result.signing_status.value}")
    print(result.manifest_path)
    print(result.checksums_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
