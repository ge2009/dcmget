#!/usr/bin/env python3
"""Build the authenticated Windows update feed and optional component patch.

The component patch is deliberately conservative.  It can only replace files
owned by the installed application (``DcmGet.exe`` and ``_internal/**``), and
it is refused whenever the previous layout contains a file that disappeared.
Configuration, task state, logs, licence/trial state and downloaded DICOM data
live outside this allowlist and can therefore never enter or be deleted by a
patch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping, Sequence

try:
    from scripts.windows_release_gate import (
        AuthenticodeConfig,
        DEFAULT_CHECKSUMS_NAME,
        DEFAULT_MANIFEST_NAME,
        PLATFORM,
        ReleaseGateError,
        SignatureStatus,
        file_sha256,
        verify_windows_files,
    )
except ModuleNotFoundError:  # direct execution from the scripts directory
    from windows_release_gate import (  # type: ignore[no-redef]
        AuthenticodeConfig,
        DEFAULT_CHECKSUMS_NAME,
        DEFAULT_MANIFEST_NAME,
        PLATFORM,
        ReleaseGateError,
        SignatureStatus,
        file_sha256,
        verify_windows_files,
    )

from dcmget.architecture import require_amd64_pe


UPDATE_SCHEMA_VERSION = 1
UPDATE_LAYOUT_VERSION = 1
UPDATE_MANIFEST_NAME = "UPDATE-MANIFEST.json"
UPDATE_SIGNATURE_NAME = f"{UPDATE_MANIFEST_NAME}.p7"
PATCH_MANIFEST_NAME = "PATCH-MANIFEST.json"
PRODUCT = "DcmGet"
CHANNEL = "stable"
INSTALL_PATH_ALLOWLIST = ("DcmGet.exe", "_internal/**")
_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_PROTECTED_NAMES = {
    "active-task.sqlite3",
    "config.json",
    "license.json",
    "task-ledger.sqlite3",
    "tasks.sqlite3",
    "trial.json",
}
_PROTECTED_PARTS = {"downloads", "logs", "quarantine", "tasks", "trial"}
_VERIFY_BASE_MANIFEST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$application = Get-AuthenticodeSignature -LiteralPath $env:DCMGET_CURRENT_EXE
if ($application.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    throw "Current DcmGet signature is $($application.Status)"
}
if ($null -eq $application.SignerCertificate) {
    throw 'Current DcmGet signer is missing'
}
$cms = New-Object System.Security.Cryptography.Pkcs.SignedCms
$cms.Decode([IO.File]::ReadAllBytes($env:DCMGET_BASE_UPDATE_P7))
$cms.CheckSignature($true)
if ($cms.SignerInfos.Count -ne 1) { throw 'Base manifest must have one signer' }
$signer = $cms.SignerInfos[0].Certificate
if ($null -eq $signer) { throw 'Base manifest signer is missing' }
if ($signer.Thumbprint -ne $application.SignerCertificate.Thumbprint) {
    throw 'Base manifest signer does not match current DcmGet'
}
[IO.File]::WriteAllBytes(
    $env:DCMGET_BASE_UPDATE_CONTENT,
    $cms.ContentInfo.Content
)
""".strip()


class WindowsUpdateBuildError(ReleaseGateError):
    pass


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class WindowsUpdateBuildResult:
    manifest_path: Path
    signature_path: Path
    component_patch_path: Path | None
    changed_files: tuple[FileRecord, ...]


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def build_windows_update_release(
    *,
    release_directory: str | Path,
    version: str,
    install_root: str | Path,
    release_manifest_path: str | Path | None = None,
    full_installer: str | Path | None = None,
    compatibility_files: Iterable[str | Path] = (),
    compatibility_root: str | Path | None = None,
    enable_component_patch: bool = False,
    baseline_install_root: str | Path | None = None,
    base_version: str | None = None,
    base_update_manifest: str | Path | None = None,
    base_update_signature: str | Path | None = None,
    authenticode: AuthenticodeConfig | None = None,
    runner: CommandRunner = subprocess.run,
) -> WindowsUpdateBuildResult:
    """Create a signed update manifest and, when requested, a safe delta ZIP."""

    _validate_version(version, "目标版本")
    release_root = _require_directory(release_directory, "Windows 发布目录")
    payload_root = _require_directory(install_root, "Windows 安装负载目录")
    release_manifest_file = Path(
        release_manifest_path or release_root / DEFAULT_MANIFEST_NAME
    ).expanduser().resolve()
    release_manifest = _load_json_object(
        release_manifest_file, "Windows 发布清单"
    )
    installer_path, installer_record = _validate_signed_release(
        release_root,
        release_manifest,
        version=version,
        full_installer=full_installer,
    )

    signing = authenticode or AuthenticodeConfig.from_environment()
    if not signing.configured:
        raise WindowsUpdateBuildError(
            "自动更新发布必须配置 Authenticode 签名证书"
        )
    if not signing.timestamp_url:
        raise WindowsUpdateBuildError(
            "自动更新发布必须配置 RFC 3161 时间戳地址"
        )

    current_files = _inventory_install_root(payload_root)
    current_tree_sha256 = _tree_digest(current_files)
    application = payload_root / "DcmGet.exe"
    require_amd64_pe(application, "增量更新 DcmGet.exe")
    compatibility = _compatibility_metadata(
        compatibility_files,
        root=compatibility_root,
    )

    component_patch_path: Path | None = None
    changed_files: tuple[FileRecord, ...] = ()
    component_patch: dict[str, object] | None = None
    if enable_component_patch:
        if baseline_install_root is None or base_version is None:
            raise WindowsUpdateBuildError(
                "启用组件增量更新时必须提供上一稳定版目录和版本"
            )
        _validate_version(base_version, "基础版本")
        if _version_tuple(base_version) >= _version_tuple(version):
            raise WindowsUpdateBuildError("基础版本必须低于目标版本")
        if base_update_manifest is None:
            raise WindowsUpdateBuildError(
                "启用组件增量更新时必须提供上一稳定版 UPDATE-MANIFEST.json"
            )
        if base_update_signature is None:
            raise WindowsUpdateBuildError(
                "启用组件增量更新时必须提供上一稳定版 UPDATE-MANIFEST.json.p7"
            )
        base_manifest_path = Path(base_update_manifest).expanduser().resolve()
        verify_pkcs7_base_manifest(
            base_manifest_path,
            Path(base_update_signature).expanduser().resolve(),
            current_executable=application,
            working_directory=release_root,
            runner=runner,
        )
        base_manifest = _load_json_object(
            base_manifest_path,
            "上一稳定版更新清单",
        )
        expected_base_tree_sha256 = _validate_compatible_base_manifest(
            base_manifest,
            base_version=base_version,
            compatibility=compatibility,
        )
        baseline_root = _require_directory(
            baseline_install_root, "上一稳定版安装负载目录"
        )
        baseline_files = _inventory_install_root(baseline_root)
        actual_base_tree_sha256 = _tree_digest(baseline_files)
        if actual_base_tree_sha256 != expected_base_tree_sha256:
            raise WindowsUpdateBuildError(
                "上一稳定版 ZIP 安装树与签名更新清单不一致，"
                "请改用完整安装包"
            )
        removed_paths = sorted(set(baseline_files) - set(current_files))
        if removed_paths:
            raise WindowsUpdateBuildError(
                "组件增量更新不允许删除已安装文件，请改用完整安装包："
                + "、".join(removed_paths[:8])
            )
        changed_files = tuple(
            current_files[path]
            for path in sorted(current_files)
            if path not in baseline_files
            or current_files[path].sha256 != baseline_files[path].sha256
            or current_files[path].size != baseline_files[path].size
        )
        if not changed_files:
            raise WindowsUpdateBuildError("组件增量更新没有检测到任何文件变化")

        changed_executables = [
            payload_root / PurePosixPath(record.path)
            for record in changed_files
            if record.path.lower().endswith(".exe")
        ]
        if changed_executables:
            statuses = verify_windows_files(
                changed_executables, signing, runner=runner
            )
            unsigned = [
                path.name
                for path, status in statuses.items()
                if status is not SignatureStatus.SIGNED
            ]
            if unsigned:
                raise WindowsUpdateBuildError(
                    "组件增量包包含未签名可执行文件：" + "、".join(unsigned)
                )

        patch_file_records: list[dict[str, object]] = []
        for record in changed_files:
            item = record.to_dict()
            baseline = baseline_files.get(record.path)
            if baseline is None:
                item["base_missing"] = True
            else:
                item["base_missing"] = False
                item["base_size"] = baseline.size
                item["base_sha256"] = baseline.sha256
            patch_file_records.append(item)

        patch_name = (
            f"DcmGet-{version}-windows-x64-components-from-{base_version}.zip"
        )
        component_patch_path = release_root / patch_name
        patch_manifest = {
            "schema_version": UPDATE_SCHEMA_VERSION,
            "product": PRODUCT,
            "platform": PLATFORM,
            "layout_version": UPDATE_LAYOUT_VERSION,
            "base_version": base_version,
            "version": version,
            "install_path_allowlist": list(INSTALL_PATH_ALLOWLIST),
            "base_tree_sha256": actual_base_tree_sha256,
            "target_tree_sha256": current_tree_sha256,
            "files": patch_file_records,
            "removed_paths": [],
        }
        _write_component_patch(
            component_patch_path,
            payload_root=payload_root,
            changed_files=changed_files,
            patch_manifest=patch_manifest,
        )
        component_patch = {
            "name": component_patch_path.name,
            "kind": "component_patch",
            "size": component_patch_path.stat().st_size,
            "sha256": file_sha256(component_patch_path),
            "signature_status": "NOT_APPLICABLE",
            "base_version": base_version,
            "preserves_user_data": True,
            "content_scope": "application",
            "layout_version": UPDATE_LAYOUT_VERSION,
            "install_path_allowlist": list(INSTALL_PATH_ALLOWLIST),
            "base_tree_sha256": patch_manifest["base_tree_sha256"],
            "target_tree_sha256": patch_manifest["target_tree_sha256"],
            "files": patch_manifest["files"],
            "removed_paths": [],
        }

    full_installer_record = {
        "name": installer_path.name,
        "kind": "full_installer",
        "size": installer_path.stat().st_size,
        "sha256": file_sha256(installer_path),
        "signature_status": "SIGNED",
        "preserves_user_data": True,
        "content_scope": "application",
        "source_release_manifest_kind": installer_record.get("kind"),
    }
    update_manifest = {
        "schema_version": UPDATE_SCHEMA_VERSION,
        "product": PRODUCT,
        "version": version,
        "platform": PLATFORM,
        "channel": CHANNEL,
        "layout_version": UPDATE_LAYOUT_VERSION,
        "install_tree_sha256": current_tree_sha256,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "compatibility": compatibility,
        "artifacts": [
            full_installer_record,
            *([] if component_patch is None else [component_patch]),
        ],
        "full_installer": full_installer_record,
        "component_patches": [] if component_patch is None else [component_patch],
        "manifest_signature": {
            "name": UPDATE_SIGNATURE_NAME,
            "kind": "pkcs7_signed_data",
            "content_encoding": "Embedded",
            "digest_algorithm": "SHA256",
            "timestamped": True,
        },
    }
    manifest_path = release_root / UPDATE_MANIFEST_NAME
    _atomic_write_text(
        manifest_path,
        json.dumps(update_manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
    )
    signature_path = sign_pkcs7_manifest(
        manifest_path,
        signing,
        runner=runner,
    )
    _update_checksums(
        release_root,
        [manifest_path, signature_path, component_patch_path],
    )
    return WindowsUpdateBuildResult(
        manifest_path=manifest_path,
        signature_path=signature_path,
        component_patch_path=component_patch_path,
        changed_files=changed_files,
    )


def sign_pkcs7_manifest(
    manifest_path: str | Path,
    config: AuthenticodeConfig,
    *,
    runner: CommandRunner = subprocess.run,
) -> Path:
    """Create an embedded, timestamped PKCS#7 signature for update metadata."""

    path = Path(manifest_path).expanduser().resolve()
    if path.is_symlink() or not path.is_file():
        raise WindowsUpdateBuildError(f"更新清单不存在或类型无效：{path}")
    if not config.configured or config.signtool is None:
        raise WindowsUpdateBuildError("更新清单签名所需的 signtool 或证书未配置")
    if not config.timestamp_url:
        raise WindowsUpdateBuildError("更新清单签名必须包含 RFC 3161 时间戳")

    command = [str(config.signtool), "sign", "/fd", "SHA256"]
    if config.certificate_path is not None:
        command.extend(["/f", str(config.certificate_path)])
        if config.certificate_password:
            command.extend(["/p", config.certificate_password])
    else:
        command.extend(["/sha1", config.certificate_sha1])
    command.extend(
        [
            "/tr",
            config.timestamp_url,
            "/td",
            "SHA256",
            "/p7",
            str(path.parent),
            "/p7ce",
            "Embedded",
            str(path),
        ]
    )
    _run_command(command, f"无法签名更新清单 {path.name}", runner)
    signature_path = path.with_name(path.name + ".p7")
    if signature_path.is_symlink() or not signature_path.is_file():
        raise WindowsUpdateBuildError(
            f"signtool 未生成预期的 PKCS#7 文件：{signature_path}"
        )
    _run_command(
        [
            str(config.signtool),
            "verify",
            "/p7",
            "/v",
            str(signature_path),
        ],
        f"无法验证更新清单签名 {signature_path.name}",
        runner,
    )
    return signature_path


def verify_pkcs7_base_manifest(
    manifest_path: str | Path,
    signature_path: str | Path,
    *,
    current_executable: str | Path,
    working_directory: str | Path,
    runner: CommandRunner = subprocess.run,
) -> None:
    """Verify and bind the previous manifest to the current release signer."""

    manifest = Path(manifest_path).expanduser().resolve()
    signature = Path(signature_path).expanduser().resolve()
    executable = Path(current_executable).expanduser().resolve()
    working = _require_directory(working_directory, "更新清单验证目录")
    for path, label in (
        (manifest, "上一稳定版更新清单"),
        (signature, "上一稳定版更新清单签名"),
        (executable, "当前 DcmGet.exe"),
    ):
        if path.is_symlink() or not path.is_file():
            raise WindowsUpdateBuildError(f"{label}不存在或类型无效：{path}")
    extracted = working / f".base-update-content.{uuid.uuid4().hex}.tmp"
    environment = dict(os.environ)
    environment.update(
        {
            "DCMGET_CURRENT_EXE": str(executable),
            "DCMGET_BASE_UPDATE_P7": str(signature),
            "DCMGET_BASE_UPDATE_CONTENT": str(extracted),
        }
    )
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        _VERIFY_BASE_MANIFEST_SCRIPT,
    ]
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=environment,
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise WindowsUpdateBuildError(
                "上一稳定版更新清单 PKCS#7 验证失败"
                + (f"：{detail}" if detail else "")
            )
        if extracted.is_symlink() or not extracted.is_file():
            raise WindowsUpdateBuildError("PKCS#7 验证未输出已签名清单内容")
        if extracted.read_bytes() != manifest.read_bytes():
            raise WindowsUpdateBuildError(
                "上一稳定版 UPDATE-MANIFEST.json 与 PKCS#7 已签内容不一致"
            )
    except OSError as exc:
        raise WindowsUpdateBuildError(
            f"上一稳定版更新清单 PKCS#7 验证失败：{exc}"
        ) from exc
    finally:
        extracted.unlink(missing_ok=True)


def _validate_signed_release(
    release_root: Path,
    manifest: Mapping[str, object],
    *,
    version: str,
    full_installer: str | Path | None,
) -> tuple[Path, Mapping[str, object]]:
    if manifest.get("product") != PRODUCT:
        raise WindowsUpdateBuildError("Windows 发布清单产品不匹配")
    if manifest.get("platform") != PLATFORM:
        raise WindowsUpdateBuildError("Windows 发布清单不是 windows-x64")
    if manifest.get("version") != version:
        raise WindowsUpdateBuildError("Windows 发布清单版本与目标版本不匹配")
    signing = manifest.get("signing")
    if not isinstance(signing, Mapping):
        raise WindowsUpdateBuildError("Windows 发布清单缺少签名状态")
    if signing.get("status") != "SIGNED" or signing.get("timestamped") is not True:
        raise WindowsUpdateBuildError(
            "只有已通过 Authenticode 签名并带时间戳的 x64 发布"
            "才能上线自动更新"
        )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise WindowsUpdateBuildError("Windows 发布清单缺少发布物列表")
    installers = [
        item
        for item in artifacts
        if isinstance(item, Mapping) and item.get("kind") == "installer"
    ]
    if len(installers) != 1:
        raise WindowsUpdateBuildError("Windows 发布清单必须且只能包含一个安装器")
    record = installers[0]
    if record.get("signature_status") != "SIGNED":
        raise WindowsUpdateBuildError("完整安装器未通过 Authenticode 签名校验")
    relative = record.get("relative_path")
    if not isinstance(relative, str) or not relative:
        raise WindowsUpdateBuildError("完整安装器缺少安全的相对路径")
    installer = (release_root / PurePosixPath(relative)).resolve()
    _require_inside(release_root, installer, "完整安装器")
    if full_installer is not None:
        requested = Path(full_installer).expanduser().resolve()
        if requested != installer:
            raise WindowsUpdateBuildError("指定安装器与发布清单记录不一致")
    if installer.is_symlink() or not installer.is_file():
        raise WindowsUpdateBuildError(f"完整安装器不存在或类型无效：{installer}")
    if record.get("size") != installer.stat().st_size:
        raise WindowsUpdateBuildError("完整安装器大小与发布清单不一致")
    if record.get("sha256") != file_sha256(installer):
        raise WindowsUpdateBuildError("完整安装器 SHA-256 与发布清单不一致")
    return installer, record


def _inventory_install_root(root: Path) -> dict[str, FileRecord]:
    records: dict[str, FileRecord] = {}
    invalid: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            invalid.append(path.relative_to(root).as_posix())
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if not _is_allowed_install_path(relative) or _is_protected_state_path(relative):
            invalid.append(relative)
            continue
        records[relative] = FileRecord(
            path=relative,
            size=path.stat().st_size,
            sha256=file_sha256(path),
        )
    if invalid:
        raise WindowsUpdateBuildError(
            "安装负载包含组件更新白名单之外的文件："
            + "、".join(invalid[:8])
        )
    if "DcmGet.exe" not in records:
        raise WindowsUpdateBuildError("安装负载缺少 DcmGet.exe")
    return records


def _is_allowed_install_path(relative: str) -> bool:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        return False
    return relative == "DcmGet.exe" or (
        len(pure.parts) >= 2 and pure.parts[0] == "_internal"
    )


def _is_protected_state_path(relative: str) -> bool:
    parts = tuple(part.lower() for part in PurePosixPath(relative).parts)
    name = parts[-1] if parts else ""
    if name in _PROTECTED_NAMES:
        return True
    return any(part in _PROTECTED_PARTS for part in parts)


def _compatibility_metadata(
    files: Iterable[str | Path],
    *,
    root: str | Path | None,
) -> dict[str, object]:
    compatibility_root = Path(root or Path.cwd()).expanduser().resolve()
    records: list[dict[str, object]] = []
    for value in files:
        path = Path(value).expanduser().resolve()
        _require_inside(compatibility_root, path, "完整安装兼容性输入")
        if path.is_symlink() or not path.is_file():
            raise WindowsUpdateBuildError(
                f"完整安装兼容性输入不存在或类型无效：{path}"
            )
        records.append(
            {
                "path": path.relative_to(compatibility_root).as_posix(),
                "sha256": file_sha256(path),
            }
        )
    records.sort(key=lambda item: str(item["path"]))
    digest = hashlib.sha256()
    for item in records:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return {
        "layout_version": UPDATE_LAYOUT_VERSION,
        "full_install_inputs_sha256": digest.hexdigest(),
        "full_install_inputs": records,
    }


def _validate_compatible_base_manifest(
    manifest: Mapping[str, object],
    *,
    base_version: str,
    compatibility: Mapping[str, object],
) -> str:
    expected = {
        "schema_version": UPDATE_SCHEMA_VERSION,
        "product": PRODUCT,
        "version": base_version,
        "platform": PLATFORM,
        "channel": CHANNEL,
        "layout_version": UPDATE_LAYOUT_VERSION,
    }
    mismatches = [
        key for key, value in expected.items() if manifest.get(key) != value
    ]
    if mismatches:
        raise WindowsUpdateBuildError(
            "上一稳定版更新清单不兼容：" + "、".join(mismatches)
        )
    manifest_signature = manifest.get("manifest_signature")
    if (
        not isinstance(manifest_signature, Mapping)
        or manifest_signature.get("kind") != "pkcs7_signed_data"
        or manifest_signature.get("content_encoding") != "Embedded"
        or manifest_signature.get("timestamped") is not True
    ):
        raise WindowsUpdateBuildError(
            "上一稳定版更新清单没有有效的带时间戳 PKCS#7 声明"
        )
    artifacts = manifest.get("artifacts")
    signed_installers = (
        [
            item
            for item in artifacts
            if isinstance(item, Mapping)
            and item.get("kind") == "full_installer"
            and item.get("signature_status") == "SIGNED"
        ]
        if isinstance(artifacts, list)
        else []
    )
    if len(signed_installers) != 1:
        raise WindowsUpdateBuildError(
            "上一稳定版更新清单必须包含一个已签名完整安装包"
        )
    install_tree_sha256 = str(manifest.get("install_tree_sha256", "")).lower()
    if _SHA256_PATTERN.fullmatch(install_tree_sha256) is None:
        raise WindowsUpdateBuildError(
            "上一稳定版更新清单缺少有效的安装树 SHA-256"
        )
    base_compatibility = manifest.get("compatibility")
    if not isinstance(base_compatibility, Mapping):
        raise WindowsUpdateBuildError("上一稳定版更新清单缺少兼容性信息")
    if (
        base_compatibility.get("layout_version") != UPDATE_LAYOUT_VERSION
        or base_compatibility.get("full_install_inputs_sha256")
        != compatibility.get("full_install_inputs_sha256")
    ):
        raise WindowsUpdateBuildError(
            "安装布局、Windows 服务、DCMTK 或依赖发生变化，"
            "请改用完整安装包"
        )
    return install_tree_sha256


def _write_component_patch(
    output: Path,
    *,
    payload_root: Path,
    changed_files: Sequence[FileRecord],
    patch_manifest: Mapping[str, object],
) -> None:
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            _write_deterministic_zip_bytes(
                archive,
                PATCH_MANIFEST_NAME,
                (
                    json.dumps(
                        patch_manifest,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            for record in changed_files:
                source = payload_root / PurePosixPath(record.path)
                _write_deterministic_zip_bytes(
                    archive,
                    record.path,
                    source.read_bytes(),
                )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def _write_deterministic_zip_bytes(
    archive: zipfile.ZipFile,
    name: str,
    content: bytes,
) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    info.create_system = 3
    archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def _tree_digest(records: Mapping[str, FileRecord]) -> str:
    digest = hashlib.sha256()
    for path in sorted(records):
        record = records[path]
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(record.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _update_checksums(release_root: Path, values: Iterable[Path | None]) -> None:
    checksums_path = release_root / DEFAULT_CHECKSUMS_NAME
    checksums: dict[str, str] = {}
    if checksums_path.is_file():
        for line in checksums_path.read_text(encoding="ascii").splitlines():
            if "  " not in line:
                raise WindowsUpdateBuildError("SHA256SUMS.txt 格式无效")
            digest, relative = line.split("  ", 1)
            checksums[relative] = digest
    for path in values:
        if path is None:
            continue
        resolved = path.resolve()
        _require_inside(release_root, resolved, "更新发布物")
        checksums[resolved.relative_to(release_root).as_posix()] = file_sha256(
            resolved
        )
    _atomic_write_text(
        checksums_path,
        "".join(
            f"{checksums[relative]}  {relative}\n"
            for relative in sorted(checksums)
        ),
        encoding="ascii",
    )


def _run_command(
    command: Sequence[str],
    error_prefix: str,
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
        raise WindowsUpdateBuildError(f"{error_prefix}：{exc}") from exc
    if completed.returncode:
        detail = "\n".join(
            value.strip()
            for value in (completed.stdout or "", completed.stderr or "")
            if value.strip()
        )
        raise WindowsUpdateBuildError(
            error_prefix + (f"：{detail}" if detail else "")
        )


def _load_json_object(path: Path, label: str) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise WindowsUpdateBuildError(f"{label}不存在或类型无效：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WindowsUpdateBuildError(f"{label}无法读取：{path}") from exc
    if not isinstance(value, Mapping):
        raise WindowsUpdateBuildError(f"{label}根节点必须是对象")
    return value


def _require_directory(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if path.is_symlink() or not path.is_dir():
        raise WindowsUpdateBuildError(f"{label}不存在或类型无效：{path}")
    return path


def _require_inside(root: Path, path: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise WindowsUpdateBuildError(f"{label}不在允许目录内：{path}") from exc


def _validate_version(value: str, label: str) -> None:
    if _VERSION_PATTERN.fullmatch(value) is None:
        raise WindowsUpdateBuildError(f"{label}必须采用 X.Y.Z 格式")


def _version_tuple(value: str) -> tuple[int, int, int]:
    _validate_version(value, "版本")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


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
        description="生成 DcmGet Windows x64 自动更新清单和可选组件增量包"
    )
    parser.add_argument("--release-dir", type=_path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--install-root", type=_path, required=True)
    parser.add_argument("--release-manifest", type=_path)
    parser.add_argument("--full-installer", type=_path)
    parser.add_argument("--compatibility-root", type=_path, default=Path.cwd())
    parser.add_argument(
        "--compatibility-file",
        type=_path,
        action="append",
        default=[],
    )
    parser.add_argument("--enable-component-patch", action="store_true")
    parser.add_argument("--baseline-install-root", type=_path)
    parser.add_argument("--base-version")
    parser.add_argument("--base-update-manifest", type=_path)
    parser.add_argument("--base-update-signature", type=_path)
    args = parser.parse_args(argv)
    result = build_windows_update_release(
        release_directory=args.release_dir,
        version=args.version,
        install_root=args.install_root,
        release_manifest_path=args.release_manifest,
        full_installer=args.full_installer,
        compatibility_files=args.compatibility_file,
        compatibility_root=args.compatibility_root,
        enable_component_patch=args.enable_component_patch,
        baseline_install_root=args.baseline_install_root,
        base_version=args.base_version,
        base_update_manifest=args.base_update_manifest,
        base_update_signature=args.base_update_signature,
    )
    print(result.manifest_path)
    print(result.signature_path)
    if result.component_patch_path is not None:
        print(result.component_patch_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
