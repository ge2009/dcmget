from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts import download_dcmtk
from scripts.build_windows import find_dcmtk_bin


def _tool_name(name: str, key: str) -> str:
    return f"{name}.exe" if key.startswith("windows") else name


def _archive_root(key: str) -> str:
    return f"dcmtk-fixture-{key}"


def _write_dcmtk_archive(path: Path, key: str, include_link: bool = False) -> None:
    root = _archive_root(key)
    members = {
        f"{root}/bin/{_tool_name(name, key)}": f"{key}:{name}\n".encode()
        for name in download_dcmtk.REQUIRED_TOOLS
    }
    members[f"{root}/share/LICENSE.txt"] = b"fixture license\n"
    if key.startswith("windows"):
        members[f"{root}/bin/dcmnet.dll"] = b"fixture dcmnet dll\n"
    if include_link:
        members[f"{root}/lib/libdcmtk.so"] = b"fixture shared library\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    if key.startswith("windows"):
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        return
    with tarfile.open(path, "w:bz2") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.mode = 0o755
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if include_link:
            link = tarfile.TarInfo(f"{root}/lib/libdcmtk-current.so")
            link.type = tarfile.SYMTYPE
            link.linkname = "libdcmtk.so"
            archive.addfile(link)


def _prepare_archive(
    project_root: Path,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
    include_link: bool = False,
) -> Path:
    archive = (
        project_root / ".runtime" / "downloads" / download_dcmtk.PACKAGES[key]
    )
    _write_dcmtk_archive(archive, key, include_link)
    monkeypatch.setitem(
        download_dcmtk.EXPECTED_SHA256,
        key,
        download_dcmtk.sha256(archive),
    )
    return archive


def _target(project_root: Path, key: str) -> Path:
    return project_root / ".runtime" / "dcmtk" / key


@pytest.mark.parametrize("key", sorted(download_dcmtk.PACKAGES))
def test_install_writes_and_reuses_archive_attested_manifest_for_every_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    archive = _prepare_archive(tmp_path, key, monkeypatch)

    def no_download(*_args, **_kwargs) -> None:
        raise AssertionError("a verified archive must not be downloaded")

    monkeypatch.setattr(download_dcmtk, "download", no_download)
    bin_dir = download_dcmtk.install(tmp_path, key)
    target = _target(tmp_path, key)
    manifest = json.loads(
        (target / download_dcmtk.MANIFEST_NAME).read_text(encoding="utf-8")
    )

    assert manifest["platform"] == key
    assert manifest["version"] == download_dcmtk.VERSION
    assert manifest["archive_sha256"] == download_dcmtk.sha256(archive)
    assert set(manifest["tools"]) == set(download_dcmtk.REQUIRED_TOOLS)
    for record in manifest["tools"].values():
        relative = Path(record["path"])
        assert not relative.is_absolute() and ".." not in relative.parts
        assert record["sha256"] == download_dcmtk.sha256(target / relative)

    assert download_dcmtk.install(tmp_path, key) == bin_dir
    assert download_dcmtk.validate_installation(target, key) == bin_dir


def test_missing_archive_is_reacquired_before_reusing_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "windows-x86_64"
    archive = _prepare_archive(tmp_path, key, monkeypatch)
    source = tmp_path / "verified-source.zip"
    shutil.copy2(archive, source)
    bin_dir = download_dcmtk.install(tmp_path, key)
    archive.unlink()
    calls: list[str] = []

    def fake_download(url: str, destination: Path, attempts: int = 6) -> None:
        calls.append(url)
        shutil.copy2(source, destination)

    monkeypatch.setattr(download_dcmtk, "download", fake_download)

    assert download_dcmtk.install(tmp_path, key) == bin_dir
    assert calls == [f"{download_dcmtk.BASE_URL}/{download_dcmtk.PACKAGES[key]}"]
    assert download_dcmtk.sha256(archive) == download_dcmtk.EXPECTED_SHA256[key]


def test_forged_tool_and_manifest_hash_are_rejected_by_archive_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "windows-x86_64"
    _prepare_archive(tmp_path, key, monkeypatch)
    bin_dir = download_dcmtk.install(tmp_path, key)
    target = _target(tmp_path, key)
    tool = bin_dir / "movescu.exe"
    tool.write_bytes(b"forged executable")
    manifest_path = target / download_dcmtk.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tools"]["movescu"]["sha256"] = download_dcmtk.sha256(tool)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(download_dcmtk.IntegrityError, match="官方归档"):
        download_dcmtk.validate_installation(target, key)

    repaired_bin = download_dcmtk.install(tmp_path, key)
    assert (repaired_bin / "movescu.exe").read_bytes() == f"{key}:movescu\n".encode()


@pytest.mark.parametrize("damage", ["tampered", "missing"])
def test_invalid_tool_is_reinstalled_from_verified_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    key = "windows-x86_64"
    _prepare_archive(tmp_path, key, monkeypatch)
    monkeypatch.setattr(
        download_dcmtk,
        "download",
        lambda *_args, **_kwargs: pytest.fail("verified archive should be reused"),
    )
    bin_dir = download_dcmtk.install(tmp_path, key)
    tool = bin_dir / "dcmdump.exe"
    if damage == "tampered":
        tool.write_bytes(b"tampered")
    else:
        tool.unlink()

    repaired_bin = download_dcmtk.install(tmp_path, key)

    assert (repaired_bin / "dcmdump.exe").read_bytes() == f"{key}:dcmdump\n".encode()


@pytest.mark.parametrize("damage", ["tampered", "missing", "injected"])
def test_windows_dll_payload_tamper_missing_and_injection_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    key = "windows-x86_64"
    _prepare_archive(tmp_path, key, monkeypatch)
    bin_dir = download_dcmtk.install(tmp_path, key)
    target = _target(tmp_path, key)
    dll = bin_dir / "dcmnet.dll"
    injected = bin_dir / "injected.dll"
    if damage == "tampered":
        dll.write_bytes(b"tampered dll")
    elif damage == "missing":
        dll.unlink()
    else:
        injected.write_bytes(b"injected dll")

    with pytest.raises(download_dcmtk.IntegrityError, match="归档载荷"):
        download_dcmtk.validate_installation(target, key)

    repaired_bin = download_dcmtk.install(tmp_path, key)
    assert (repaired_bin / "dcmnet.dll").read_bytes() == b"fixture dcmnet dll\n"
    assert not (repaired_bin / "injected.dll").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows CI may not permit symlink creation")
def test_archive_link_target_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "linux-x86_64"
    _prepare_archive(tmp_path, key, monkeypatch, include_link=True)
    download_dcmtk.install(tmp_path, key)
    target = _target(tmp_path, key)
    link = target / _archive_root(key) / "lib" / "libdcmtk-current.so"
    link.unlink()
    link.symlink_to("other.so")

    with pytest.raises(download_dcmtk.IntegrityError, match="链接目标"):
        download_dcmtk.validate_installation(target, key)


def test_filename_only_legacy_cache_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "windows-x86_64"
    _prepare_archive(tmp_path, key, monkeypatch)
    target = _target(tmp_path, key)
    legacy_bin = target / "legacy" / "bin"
    legacy_bin.mkdir(parents=True)
    for name in download_dcmtk.REQUIRED_TOOLS:
        (legacy_bin / _tool_name(name, key)).write_bytes(b"unverified")
    monkeypatch.setattr(
        download_dcmtk,
        "download",
        lambda *_args, **_kwargs: pytest.fail("verified archive should be reused"),
    )

    installed_bin = download_dcmtk.install(tmp_path, key)

    assert installed_bin != legacy_bin
    assert not legacy_bin.exists()
    assert download_dcmtk.validate_installation(target, key) == installed_bin


def test_corrupt_archive_is_replaced_by_a_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "windows-x86_64"
    archive = _prepare_archive(tmp_path, key, monkeypatch)
    verified_copy = tmp_path / "verified.zip"
    shutil.copy2(archive, verified_copy)
    download_dcmtk.install(tmp_path, key)
    archive.write_bytes(b"corrupt cache")
    calls: list[str] = []

    def fake_download(url: str, destination: Path, attempts: int = 6) -> None:
        calls.append(url)
        shutil.copy2(verified_copy, destination)

    monkeypatch.setattr(download_dcmtk, "download", fake_download)

    download_dcmtk.install(tmp_path, key)

    assert calls == [f"{download_dcmtk.BASE_URL}/{download_dcmtk.PACKAGES[key]}"]
    assert download_dcmtk.validate_installation(_target(tmp_path, key), key)


def test_manifest_tool_records_must_match_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "windows-x86_64"
    _prepare_archive(tmp_path, key, monkeypatch)
    download_dcmtk.install(tmp_path, key)
    target = _target(tmp_path, key)
    manifest_path = target / download_dcmtk.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tools"]["movescu"]["path"] = "../outside.exe"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(download_dcmtk.IntegrityError, match="工具记录"):
        download_dcmtk.validate_installation(target, key)


def test_windows_build_requires_archive_attested_dcmtk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "windows-x86_64"
    archive = _prepare_archive(tmp_path, key, monkeypatch)
    bin_dir = download_dcmtk.install(tmp_path, key)
    target = _target(tmp_path, key)

    assert find_dcmtk_bin(target) == bin_dir
    archive.unlink()
    with pytest.raises(FileNotFoundError, match="归档"):
        find_dcmtk_bin(target)
