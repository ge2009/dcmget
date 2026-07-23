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
import base64
import binascii
import hashlib
import json
import os
import re
import stat
import sys
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

try:
    from scripts.windows_release_gate import (
        DEFAULT_CHECKSUMS_NAME,
        DEFAULT_MANIFEST_NAME,
        PLATFORM,
        ReleaseGateError,
        file_sha256,
    )
except ModuleNotFoundError:  # direct execution from the scripts directory
    from windows_release_gate import (  # type: ignore[no-redef]
        DEFAULT_CHECKSUMS_NAME,
        DEFAULT_MANIFEST_NAME,
        PLATFORM,
        ReleaseGateError,
        file_sha256,
    )

from dcmget.architecture import require_amd64_pe
from dcmget.update_signing import (
    UpdateSigningError,
    load_private_key,
    sign_manifest,
    verify_manifest,
)
from dcmget.update_trust import (
    DEFAULT_UPDATE_KEY_ID,
    TRUSTED_UPDATE_PUBLIC_KEYS,
)


UPDATE_SCHEMA_VERSION = 1
UPDATE_LAYOUT_VERSION = 1
UPDATE_MANIFEST_NAME = "UPDATE-MANIFEST.json"
UPDATE_SIGNATURE_NAME = "UPDATE-MANIFEST.signed.json"
PATCH_MANIFEST_NAME = "PATCH-MANIFEST.json"
COMPONENT_BASELINE_NAME = "component-baseline.zip"
MAX_COMPONENT_BASELINES = 5
PRODUCT = "DcmGet"
CHANNEL = "stable"
INSTALL_PATH_ALLOWLIST = ("DcmGet.exe", "_internal/**")
_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_SAFE_FILE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
_PROTECTED_NAMES = {
    "active-task.sqlite3",
    "config.json",
    "license.json",
    "task-ledger.sqlite3",
    "tasks.sqlite3",
    "trial.json",
}
_PROTECTED_PARTS = {"downloads", "logs", "quarantine", "tasks", "trial"}
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
class ComponentBaseline:
    version: str
    install_root: Path
    update_manifest: Path
    update_signature: Path


@dataclass(frozen=True, slots=True)
class WindowsUpdateBuildResult:
    manifest_path: Path
    signature_path: Path
    component_patch_path: Path | None
    changed_files: tuple[FileRecord, ...]
    component_patch_paths: tuple[Path, ...] = ()
    changed_files_by_base: tuple[tuple[str, tuple[FileRecord, ...]], ...] = ()
    baseline_snapshot_path: Path | None = None


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
    baselines: Iterable[ComponentBaseline] = (),
    patch_only: bool = False,
    update_private_key: Ed25519PrivateKey | None = None,
    update_key_id: str | None = None,
    trusted_update_public_keys: Mapping[
        str, bytes | Ed25519PublicKey
    ] = TRUSTED_UPDATE_PUBLIC_KEYS,
) -> WindowsUpdateBuildResult:
    """Create an Ed25519-signed manifest and safe direct component patches.

    The legacy single-baseline arguments remain supported.  ``patch_only``
    omits the full installer and is intended for fast releases produced from a
    onedir payload. A patch-only release must have at least one trusted baseline,
    so the update chain always starts from a complete release.
    """

    _validate_version(version, "目标版本")
    release_root = _require_directory(release_directory, "Windows 发布目录")
    payload_root = _require_directory(install_root, "Windows 安装负载目录")

    signing_key = update_private_key or _load_update_private_key()
    signing_key_id = update_key_id or os.environ.get(
        "DCMGET_UPDATE_SIGNING_KEY_ID", DEFAULT_UPDATE_KEY_ID
    )

    current_files = _inventory_install_root(payload_root)
    current_tree_sha256 = _tree_digest(current_files)
    application = payload_root / "DcmGet.exe"
    require_amd64_pe(application, "增量更新 DcmGet.exe")
    installer_path: Path | None = None
    installer_record: Mapping[str, object] | None = None
    if patch_only:
        if release_manifest_path is not None or full_installer is not None:
            raise WindowsUpdateBuildError(
                "patch-only 发布不能同时指定完整安装包或 Windows 发布清单"
            )
    else:
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

    compatibility = _compatibility_metadata(
        compatibility_files,
        root=compatibility_root,
    )

    requested_baselines = list(baselines)
    legacy_values = (
        baseline_install_root,
        base_version,
        base_update_manifest,
        base_update_signature,
    )
    if any(value is not None for value in legacy_values):
        if any(value is None for value in legacy_values):
            raise WindowsUpdateBuildError(
                "旧版单基线参数必须同时提供目录、版本、更新清单和签名"
            )
        requested_baselines.append(
            ComponentBaseline(
                version=str(base_version),
                install_root=Path(baseline_install_root),  # type: ignore[arg-type]
                update_manifest=Path(base_update_manifest),  # type: ignore[arg-type]
                update_signature=Path(base_update_signature),  # type: ignore[arg-type]
            )
        )
    elif enable_component_patch and not requested_baselines:
        raise WindowsUpdateBuildError(
            "启用组件增量更新时必须提供至少一个可信基线"
        )
    if patch_only and not requested_baselines:
        raise WindowsUpdateBuildError(
            "首个组件更新基线必须来自可信完整发布；patch-only 至少需要一个可信基线"
        )

    ordered_baselines = _normalise_baselines(requested_baselines, version=version)
    component_patches: list[dict[str, object]] = []
    component_patch_paths: list[Path] = []
    changed_files_by_base: list[tuple[str, tuple[FileRecord, ...]]] = []
    chain_anchors: dict[str, str] = {}
    for baseline in ordered_baselines:
        base_manifest_path = baseline.update_manifest.expanduser().resolve()
        verify_ed25519_base_manifest(
            base_manifest_path,
            baseline.update_signature.expanduser().resolve(),
            trusted_public_keys=trusted_update_public_keys,
        )
        base_manifest = _load_json_object(
            base_manifest_path,
            f"{baseline.version} 更新清单",
        )
        expected_base_tree_sha256 = _validate_compatible_base_manifest(
            base_manifest,
            base_version=baseline.version,
            compatibility=compatibility,
        )
        for anchor_version, anchor_tree in _component_chain_anchors(
            base_manifest,
            version=baseline.version,
            install_tree_sha256=expected_base_tree_sha256,
        ):
            existing_tree = chain_anchors.get(anchor_version)
            if existing_tree is not None and existing_tree != anchor_tree:
                raise WindowsUpdateBuildError(
                    f"组件更新链的完整发布锚点冲突：{anchor_version}"
                )
            chain_anchors[anchor_version] = anchor_tree
        baseline_root = _require_directory(
            baseline.install_root,
            f"{baseline.version} 安装负载目录",
        )
        baseline_files = _inventory_install_root(baseline_root)
        actual_base_tree_sha256 = _tree_digest(baseline_files)
        if actual_base_tree_sha256 != expected_base_tree_sha256:
            raise WindowsUpdateBuildError(
                f"{baseline.version} 基线 ZIP 安装树与签名更新清单不一致，"
                "请改用可信 component-baseline.zip 或完整发布 ZIP"
            )
        removed_paths = sorted(set(baseline_files) - set(current_files))
        if removed_paths:
            raise WindowsUpdateBuildError(
                f"从 {baseline.version} 更新时不允许删除已安装文件，"
                "请改用完整安装包："
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
            raise WindowsUpdateBuildError(
                f"从 {baseline.version} 更新没有检测到任何文件变化"
            )

        patch_file_records: list[dict[str, object]] = []
        for record in changed_files:
            item = record.to_dict()
            base_record = baseline_files.get(record.path)
            if base_record is None:
                item["base_missing"] = True
            else:
                item["base_missing"] = False
                item["base_size"] = base_record.size
                item["base_sha256"] = base_record.sha256
            patch_file_records.append(item)

        patch_name = (
            f"DcmGet-{version}-windows-x64-components-from-{baseline.version}.zip"
        )
        component_patch_path = release_root / patch_name
        patch_manifest = {
            "schema_version": UPDATE_SCHEMA_VERSION,
            "product": PRODUCT,
            "platform": PLATFORM,
            "layout_version": UPDATE_LAYOUT_VERSION,
            "base_version": baseline.version,
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
            "base_version": baseline.version,
            "preserves_user_data": True,
            "content_scope": "application",
            "layout_version": UPDATE_LAYOUT_VERSION,
            "install_path_allowlist": list(INSTALL_PATH_ALLOWLIST),
            "base_tree_sha256": patch_manifest["base_tree_sha256"],
            "target_tree_sha256": patch_manifest["target_tree_sha256"],
            "files": patch_manifest["files"],
            "removed_paths": [],
        }
        component_patch_paths.append(component_patch_path)
        component_patches.append(component_patch)
        changed_files_by_base.append((baseline.version, changed_files))

    full_installer_record: dict[str, object] | None = None
    if installer_path is not None and installer_record is not None:
        full_installer_record = {
            "name": installer_path.name,
            "kind": "full_installer",
            "size": installer_path.stat().st_size,
            "sha256": file_sha256(installer_path),
            "signature_status": installer_record.get("signature_status"),
            "preserves_user_data": True,
            "content_scope": "application",
            "source_release_manifest_kind": installer_record.get("kind"),
        }
    update_artifacts = [
        *([] if full_installer_record is None else [full_installer_record]),
        *component_patches,
    ]
    if not update_artifacts:
        raise WindowsUpdateBuildError("更新清单至少需要一个完整安装包或组件增量包")
    if full_installer_record is not None:
        chain_anchors = {version: current_tree_sha256}
    component_chain = {
        "schema_version": 1,
        "root_full_releases": [
            {
                "version": anchor_version,
                "install_tree_sha256": chain_anchors[anchor_version],
            }
            for anchor_version in sorted(
                chain_anchors,
                key=_version_tuple,
                reverse=True,
            )
        ],
    }
    if not component_chain["root_full_releases"]:
        raise WindowsUpdateBuildError(
            "组件更新链缺少可信完整发布锚点"
        )
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
        "component_chain": component_chain,
        "artifacts": update_artifacts,
        "full_installer": full_installer_record,
        "component_patches": component_patches,
        "manifest_signature": {
            "name": UPDATE_SIGNATURE_NAME,
            "kind": "ed25519_signed_envelope",
            "algorithm": "Ed25519",
            "key_id": signing_key_id,
            "content_encoding": "base64",
        },
    }
    manifest_path = release_root / UPDATE_MANIFEST_NAME
    _atomic_write_text(
        manifest_path,
        json.dumps(update_manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
    )
    try:
        signed_envelope = sign_manifest(
            manifest_path.read_bytes(),
            private_key=signing_key,
            key_id=signing_key_id,
        )
    except (OSError, UpdateSigningError) as exc:
        raise WindowsUpdateBuildError(f"无法签名更新清单：{exc}") from exc
    try:
        verified_manifest = verify_manifest(
            signed_envelope,
            trusted_update_public_keys,
        )
    except UpdateSigningError as exc:
        raise WindowsUpdateBuildError(
            f"更新签名密钥与内置受信公钥不匹配：{exc}"
        ) from exc
    if verified_manifest != manifest_path.read_bytes():
        raise WindowsUpdateBuildError("更新签名信封与更新清单内容不一致")
    signature_path = release_root / UPDATE_SIGNATURE_NAME
    _atomic_write_bytes(signature_path, signed_envelope)
    _update_checksums(
        release_root,
        [manifest_path, signature_path, *component_patch_paths],
    )
    baseline_snapshot_path = release_root / COMPONENT_BASELINE_NAME
    _write_component_baseline(
        baseline_snapshot_path,
        payload_root=payload_root,
        files=current_files,
    )
    first_changed = changed_files_by_base[0][1] if changed_files_by_base else ()
    return WindowsUpdateBuildResult(
        manifest_path=manifest_path,
        signature_path=signature_path,
        component_patch_path=(
            component_patch_paths[0] if component_patch_paths else None
        ),
        changed_files=first_changed,
        component_patch_paths=tuple(component_patch_paths),
        changed_files_by_base=tuple(changed_files_by_base),
        baseline_snapshot_path=baseline_snapshot_path,
    )


def verify_ed25519_base_manifest(
    manifest_path: str | Path,
    signature_path: str | Path,
    *,
    trusted_public_keys: Mapping[str, bytes | Ed25519PublicKey],
) -> None:
    """Verify that a baseline manifest exactly matches its signed envelope."""

    manifest = Path(manifest_path).expanduser().resolve()
    signature = Path(signature_path).expanduser().resolve()
    for path, label in (
        (manifest, "上一稳定版更新清单"),
        (signature, "上一稳定版更新清单签名"),
    ):
        if path.is_symlink() or not path.is_file():
            raise WindowsUpdateBuildError(f"{label}不存在或类型无效：{path}")
    try:
        verified = verify_manifest(
            signature.read_bytes(),
            trusted_public_keys,
        )
        if verified != manifest.read_bytes():
            raise WindowsUpdateBuildError(
                "上一稳定版 UPDATE-MANIFEST.json 与 Ed25519 已签内容不一致"
            )
    except (OSError, UpdateSigningError) as exc:
        raise WindowsUpdateBuildError(
            f"上一稳定版更新清单 Ed25519 验证失败：{exc}"
        ) from exc


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
    release_signature_status = signing.get("status")
    if release_signature_status not in {"SIGNED", "UNSIGNED"}:
        raise WindowsUpdateBuildError("Windows 发布清单签名状态无效")
    if (
        release_signature_status == "SIGNED"
        and signing.get("timestamped") is not True
    ):
        raise WindowsUpdateBuildError("已签名 Windows 发布缺少时间戳")
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
    if record.get("signature_status") != release_signature_status:
        raise WindowsUpdateBuildError("完整安装器签名状态与发布清单不一致")
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
        or manifest_signature.get("name") != UPDATE_SIGNATURE_NAME
        or manifest_signature.get("kind") != "ed25519_signed_envelope"
        or manifest_signature.get("algorithm") != "Ed25519"
        or not isinstance(manifest_signature.get("key_id"), str)
        or manifest_signature.get("content_encoding") != "base64"
    ):
        raise WindowsUpdateBuildError(
            "上一稳定版更新清单没有有效的 Ed25519 签名声明"
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
        or base_compatibility.get("full_install_inputs")
        != compatibility.get("full_install_inputs")
    ):
        raise WindowsUpdateBuildError(
            "安装布局、Windows 服务、DCMTK 或依赖发生变化，"
            "请改用完整安装包"
        )
    _validate_base_update_artifacts(
        manifest,
        version=base_version,
        install_tree_sha256=install_tree_sha256,
    )
    return install_tree_sha256


def _validate_base_update_artifacts(
    manifest: Mapping[str, object],
    *,
    version: str,
    install_tree_sha256: str,
) -> None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise WindowsUpdateBuildError(
            "上一稳定版更新清单没有有效的更新资源"
        )

    full_installers: list[Mapping[str, object]] = []
    component_patches: list[Mapping[str, object]] = []
    seen_names: set[str] = set()
    for raw_record in artifacts:
        if not isinstance(raw_record, Mapping):
            raise WindowsUpdateBuildError("上一稳定版更新清单包含无效资源记录")
        name = raw_record.get("name")
        if not isinstance(name, str) or _SAFE_FILE_PATTERN.fullmatch(name) is None:
            raise WindowsUpdateBuildError("上一稳定版更新清单包含不安全资源名")
        canonical_name = name.casefold()
        if canonical_name in seen_names:
            raise WindowsUpdateBuildError("上一稳定版更新清单包含重复资源名")
        seen_names.add(canonical_name)
        try:
            size = int(raw_record.get("size", 0))
        except (TypeError, ValueError) as exc:
            raise WindowsUpdateBuildError("上一稳定版更新资源大小无效") from exc
        sha256 = str(raw_record.get("sha256", "")).lower()
        if size <= 0 or _SHA256_PATTERN.fullmatch(sha256) is None:
            raise WindowsUpdateBuildError("上一稳定版更新资源指纹无效")

        kind = raw_record.get("kind")
        if kind == "full_installer":
            if (
                raw_record.get("signature_status") not in {"SIGNED", "UNSIGNED"}
                or raw_record.get("preserves_user_data") is not True
                or raw_record.get("content_scope") != "application"
            ):
                raise WindowsUpdateBuildError(
                    "上一稳定版完整安装包声明无效"
                )
            full_installers.append(raw_record)
        elif kind == "component_patch":
            _validate_component_patch_record(
                raw_record,
                version=version,
                install_tree_sha256=install_tree_sha256,
            )
            component_patches.append(raw_record)
        else:
            raise WindowsUpdateBuildError(
                f"上一稳定版更新清单包含不支持的资源类型：{kind}"
            )

    if len(full_installers) > 1:
        raise WindowsUpdateBuildError(
            "上一稳定版更新清单包含多个完整安装包"
        )
    if not full_installers and not component_patches:
        raise WindowsUpdateBuildError(
            "上一稳定版必须包含可信完整安装包或可信组件增量包"
        )
    declared_full = manifest.get("full_installer")
    if full_installers:
        if declared_full != full_installers[0]:
            raise WindowsUpdateBuildError(
                "上一稳定版 full_installer 与 artifacts 不一致"
            )
    elif declared_full is not None:
        raise WindowsUpdateBuildError(
            "patch-only 基线不能声明不存在的 full_installer"
        )
    if manifest.get("component_patches") != component_patches:
        raise WindowsUpdateBuildError(
            "上一稳定版 component_patches 与 artifacts 不一致"
        )


def _component_chain_anchors(
    manifest: Mapping[str, object],
    *,
    version: str,
    install_tree_sha256: str,
) -> tuple[tuple[str, str], ...]:
    artifacts = manifest.get("artifacts")
    has_full_release = isinstance(artifacts, list) and any(
        isinstance(item, Mapping) and item.get("kind") == "full_installer"
        for item in artifacts
    )
    if has_full_release:
        return ((version, install_tree_sha256),)

    chain = manifest.get("component_chain")
    if not isinstance(chain, Mapping) or chain.get("schema_version") != 1:
        raise WindowsUpdateBuildError(
            "patch-only 基线缺少可信完整发布链锚点"
        )
    raw_anchors = chain.get("root_full_releases")
    if not isinstance(raw_anchors, list) or not raw_anchors:
        raise WindowsUpdateBuildError(
            "patch-only 基线缺少可信完整发布链锚点"
        )
    anchors: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw_anchors:
        if not isinstance(item, Mapping):
            raise WindowsUpdateBuildError("组件更新链锚点格式无效")
        anchor_version = str(item.get("version", ""))
        _validate_version(anchor_version, "组件更新链完整发布版本")
        if _version_tuple(anchor_version) >= _version_tuple(version):
            raise WindowsUpdateBuildError(
                "patch-only 组件更新链锚点版本必须低于基线版本"
            )
        anchor_tree = str(item.get("install_tree_sha256", "")).lower()
        if _SHA256_PATTERN.fullmatch(anchor_tree) is None:
            raise WindowsUpdateBuildError("组件更新链锚点树指纹无效")
        if anchor_version in seen:
            raise WindowsUpdateBuildError("组件更新链锚点版本重复")
        seen.add(anchor_version)
        anchors.append((anchor_version, anchor_tree))
    return tuple(anchors)


def _validate_component_patch_record(
    record: Mapping[str, object],
    *,
    version: str,
    install_tree_sha256: str,
) -> None:
    base_version = str(record.get("base_version", ""))
    _validate_version(base_version, "上一稳定版增量包基础版本")
    if _version_tuple(base_version) >= _version_tuple(version):
        raise WindowsUpdateBuildError("上一稳定版增量包基础版本无效")
    base_tree_sha256 = str(record.get("base_tree_sha256", "")).lower()
    target_tree_sha256 = str(record.get("target_tree_sha256", "")).lower()
    if (
        _SHA256_PATTERN.fullmatch(base_tree_sha256) is None
        or target_tree_sha256 != install_tree_sha256
        or record.get("signature_status") != "NOT_APPLICABLE"
        or record.get("preserves_user_data") is not True
        or record.get("content_scope") != "application"
        or record.get("layout_version") != UPDATE_LAYOUT_VERSION
        or record.get("install_path_allowlist") != list(INSTALL_PATH_ALLOWLIST)
        or record.get("removed_paths") != []
    ):
        raise WindowsUpdateBuildError("上一稳定版组件增量包布局或树指纹无效")

    files = record.get("files")
    if not isinstance(files, list) or not files:
        raise WindowsUpdateBuildError("上一稳定版组件增量包缺少文件清单")
    seen_paths: set[str] = set()
    for item in files:
        if not isinstance(item, Mapping):
            raise WindowsUpdateBuildError("上一稳定版增量文件记录无效")
        path = str(item.get("path", "")).replace("\\", "/")
        canonical_path = path.casefold()
        if (
            not _is_allowed_install_path(path)
            or _is_protected_state_path(path)
            or canonical_path in seen_paths
        ):
            raise WindowsUpdateBuildError("上一稳定版增量文件路径无效或重复")
        seen_paths.add(canonical_path)
        try:
            size = int(item.get("size", -1))
        except (TypeError, ValueError) as exc:
            raise WindowsUpdateBuildError("上一稳定版增量文件大小无效") from exc
        sha256 = str(item.get("sha256", "")).lower()
        if size < 0 or _SHA256_PATTERN.fullmatch(sha256) is None:
            raise WindowsUpdateBuildError("上一稳定版增量文件指纹无效")
        if item.get("base_missing") is True:
            if item.get("base_size") is not None or item.get("base_sha256"):
                raise WindowsUpdateBuildError("上一稳定版新增文件基础状态冲突")
        else:
            try:
                base_size = int(item.get("base_size", -1))
            except (TypeError, ValueError) as exc:
                raise WindowsUpdateBuildError(
                    "上一稳定版增量文件基础大小无效"
                ) from exc
            base_sha256 = str(item.get("base_sha256", "")).lower()
            if base_size < 0 or _SHA256_PATTERN.fullmatch(base_sha256) is None:
                raise WindowsUpdateBuildError(
                    "上一稳定版增量文件基础指纹无效"
                )


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


def _write_component_baseline(
    output: Path,
    *,
    payload_root: Path,
    files: Mapping[str, FileRecord],
) -> None:
    """Write the exact install tree used only by future release builders."""

    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for relative in sorted(files):
                source = payload_root / PurePosixPath(relative)
                _write_deterministic_zip_bytes(
                    archive,
                    relative,
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


def _load_update_private_key(
    private_key_path: str | Path | None = None,
) -> Ed25519PrivateKey:
    """Load the release-only private key from an explicit path or environment."""

    configured_path = private_key_path or os.environ.get(
        "DCMGET_UPDATE_SIGNING_PRIVATE_KEY_PATH"
    )
    configured_base64 = os.environ.get(
        "DCMGET_UPDATE_SIGNING_PRIVATE_KEY_BASE64"
    )
    if configured_path and configured_base64:
        raise WindowsUpdateBuildError(
            "更新签名私钥路径和 Base64 环境变量不能同时配置"
        )
    if configured_path:
        source_path = Path(configured_path).expanduser()
        if source_path.is_symlink() or not source_path.is_file():
            raise WindowsUpdateBuildError(
                f"更新签名私钥不存在或类型无效：{source_path}"
            )
        path = source_path.resolve()
        if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise WindowsUpdateBuildError(
                "更新签名私钥权限过宽；请执行 chmod 600 后重试"
            )
        try:
            private_key_pem = path.read_bytes()
        except OSError as exc:
            raise WindowsUpdateBuildError(
                f"无法读取更新签名私钥：{path}"
            ) from exc
    elif configured_base64:
        try:
            private_key_pem = base64.b64decode(
                configured_base64.encode("ascii"),
                validate=True,
            )
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise WindowsUpdateBuildError(
                "DCMGET_UPDATE_SIGNING_PRIVATE_KEY_BASE64 不是有效 Base64"
            ) from exc
    else:
        raise WindowsUpdateBuildError(
            "缺少更新签名私钥；请配置 --update-private-key、"
            "DCMGET_UPDATE_SIGNING_PRIVATE_KEY_PATH 或"
            " DCMGET_UPDATE_SIGNING_PRIVATE_KEY_BASE64"
        )
    password_text = os.environ.get("DCMGET_UPDATE_SIGNING_PRIVATE_KEY_PASSWORD")
    password = password_text.encode("utf-8") if password_text else None
    try:
        return load_private_key(private_key_pem, password=password)
    except UpdateSigningError as exc:
        raise WindowsUpdateBuildError(f"无法加载更新签名私钥：{exc}") from exc


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


def _normalise_baselines(
    values: Iterable[ComponentBaseline],
    *,
    version: str,
) -> tuple[ComponentBaseline, ...]:
    by_version: dict[str, ComponentBaseline] = {}
    for value in values:
        if not isinstance(value, ComponentBaseline):
            raise WindowsUpdateBuildError("组件更新基线格式无效")
        _validate_version(value.version, "基础版本")
        if _version_tuple(value.version) >= _version_tuple(version):
            raise WindowsUpdateBuildError("基础版本必须低于目标版本")
        if value.version in by_version:
            raise WindowsUpdateBuildError(
                f"组件更新基线版本重复：{value.version}"
            )
        by_version[value.version] = value
    ordered = sorted(
        by_version.values(),
        key=lambda item: _version_tuple(item.version),
        reverse=True,
    )
    return tuple(ordered[:MAX_COMPONENT_BASELINES])


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


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
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
    parser.add_argument(
        "--patch-only",
        action="store_true",
        help="只发布组件增量包，不要求或声明完整安装包",
    )
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
    parser.add_argument(
        "--update-private-key",
        type=_path,
        help=(
            "Ed25519 私钥 PEM 路径；也可用"
            " DCMGET_UPDATE_SIGNING_PRIVATE_KEY_PATH 或 Base64 secret 环境变量"
        ),
    )
    parser.add_argument(
        "--update-key-id",
        default=os.environ.get(
            "DCMGET_UPDATE_SIGNING_KEY_ID", DEFAULT_UPDATE_KEY_ID
        ),
    )
    parser.add_argument(
        "--baseline",
        nargs=4,
        action="append",
        metavar=("VERSION", "INSTALL_ROOT", "MANIFEST", "SIGNATURE"),
        default=[],
        help="可信基线；可重复，自动选取版本最近的五个",
    )
    args = parser.parse_args(argv)
    baselines = [
        ComponentBaseline(
            version=value[0],
            install_root=Path(value[1]),
            update_manifest=Path(value[2]),
            update_signature=Path(value[3]),
        )
        for value in args.baseline
    ]
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
        baselines=baselines,
        patch_only=args.patch_only,
        update_private_key=_load_update_private_key(args.update_private_key),
        update_key_id=args.update_key_id,
    )
    print(result.manifest_path)
    print(result.signature_path)
    for path in result.component_patch_paths:
        print(path)
    if result.baseline_snapshot_path is not None:
        print(result.baseline_snapshot_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
