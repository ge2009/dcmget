from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from scripts import build_windows_cli

EXPECTED_DCMTK_BIN_FILES = (
    "movescu.exe",
    "storescp.exe",
    "dcmdump.exe",
    "dcmtls.dll",
    "dcmnet.dll",
    "dcmdata.dll",
    "oflog.dll",
    "ofstd.dll",
    "oficonv.dll",
)
EXPECTED_DCMTK_DATA_FILES = (
    "share/dcmtk-3.7.0/dicom.dic",
    "share/doc/dcmtk-3.7.0/COPYRIGHT",
    "share/doc/dcmtk-3.7.0/VERSION",
)


def _create_package_tree(root: Path) -> None:
    for relative in build_windows_cli.expected_package_files():
        if relative == "FILES-SHA256.txt":
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"payload:{relative}".encode())
    build_windows_cli.write_package_checksums(root)


def test_cli_dcmtk_allowlist_is_download_only() -> None:
    assert build_windows_cli.DCMTK_BIN_FILES == EXPECTED_DCMTK_BIN_FILES
    assert build_windows_cli.DCMTK_DATA_FILES == EXPECTED_DCMTK_DATA_FILES
    assert "dcmmkdir.exe" not in build_windows_cli.DCMTK_BIN_FILES
    assert "dcmj2pnm.exe" not in build_windows_cli.DCMTK_BIN_FILES
    assert "dcmdjpeg.exe" not in build_windows_cli.DCMTK_BIN_FILES


def test_cli_package_allowlist_has_no_gui_or_pdi_payload() -> None:
    files = set(build_windows_cli.expected_package_files())
    assert "DcmGetCLI.exe" in files
    assert "config.json" in files
    assert "access.txt" in files
    assert "FILES-SHA256.txt" in files
    assert not any("DcmGet.exe" == Path(name).name for name in files)
    assert not any(
        marker in name.casefold()
        for name in files
        for marker in ("pdi", "ohif", "webview", "react")
    )


def test_cli_package_tree_and_archive_match_exact_allowlist(tmp_path: Path) -> None:
    package_root = tmp_path / "package" / "DcmGetCLI"
    _create_package_tree(package_root)
    archive = tmp_path / "release" / "DcmGetCLI-test-windows-x64.zip"

    build_windows_cli.write_package_archive(package_root, archive)

    expected = {
        f"DcmGetCLI/{relative}"
        for relative in build_windows_cli.expected_package_files()
    }
    with zipfile.ZipFile(archive) as package:
        assert set(package.namelist()) == expected

    checksum_lines = (package_root / "FILES-SHA256.txt").read_text(
        encoding="ascii"
    ).splitlines()
    assert len(checksum_lines) == len(expected) - 1
    for line in checksum_lines:
        digest, relative = line.split("  ", 1)
        assert digest == hashlib.sha256((package_root / relative).read_bytes()).hexdigest()


def test_cli_package_tree_rejects_unexpected_file(tmp_path: Path) -> None:
    package_root = tmp_path / "DcmGetCLI"
    _create_package_tree(package_root)
    (package_root / "DcmGet.exe").write_bytes(b"gui")

    with pytest.raises(RuntimeError, match="多出 DcmGet.exe"):
        build_windows_cli.validate_package_tree(package_root)


def test_cli_archive_verifier_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("DcmGetCLI/../outside.txt", b"unsafe")

    with pytest.raises(RuntimeError, match="不安全路径"):
        build_windows_cli.verify_package_archive(archive)


def test_stage_dcmtk_copies_only_the_cli_runtime(tmp_path: Path) -> None:
    package_root = tmp_path / "dcmtk-3.7.0-win64-dynamic"
    source_bin = package_root / "bin"
    for relative in (
        *(Path("bin") / name for name in EXPECTED_DCMTK_BIN_FILES),
        *(Path(name) for name in EXPECTED_DCMTK_DATA_FILES),
    ):
        path = package_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.as_posix().encode())
    (source_bin / "dcmmkdir.exe").write_bytes(b"must not be copied")

    destination = tmp_path / "staged"
    build_windows_cli.stage_dcmtk(source_bin, destination)

    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    expected = {
        *(f"bin/{name}" for name in EXPECTED_DCMTK_BIN_FILES),
        *EXPECTED_DCMTK_DATA_FILES,
    }
    assert actual == expected


def test_pyinstaller_entry_explicitly_excludes_non_cli_dependencies(
    tmp_path: Path,
) -> None:
    arguments = build_windows_cli.pyinstaller_args(
        tmp_path / "icon.ico",
        tmp_path / "version.txt",
    )
    excluded = {
        arguments[index + 1]
        for index, value in enumerate(arguments[:-1])
        if value == "--exclude-module"
    }
    assert {
        "DICOM_download_script",
        "DICOM_download_ui",
        "cryptography",
        "dcmget.anonymization",
        "dcmget.licensing",
        "dcmget.pdi",
        "dcmget.pdi_server",
        "dcmget.web_server",
        "fastapi",
        "pynetdicom",
        "uvicorn",
        "webview",
    } <= excluded
    assert "--console" in arguments
    assert "--onefile" in arguments


def test_cli_source_and_requirements_do_not_depend_on_gui_pdi_or_licensing() -> None:
    root = Path(__file__).resolve().parents[1]
    cli = (root / "DICOM_download_cli.py").read_text(encoding="utf-8")
    requirements = (root / "requirements-cli.txt").read_text(
        encoding="utf-8"
    ).casefold()

    assert "import DICOM_download_script" not in cli
    assert "from dcmget.pdi" not in cli
    assert "from dcmget.licensing" not in cli
    for dependency in (
        "cryptography",
        "fastapi",
        "pywebview",
        "pynetdicom",
        "uvicorn",
    ):
        assert dependency not in requirements


def test_cli_workflow_is_separate_and_x64_only() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (
        root / ".github" / "workflows" / "windows-cli-release.yml"
    ).read_text(encoding="utf-8")
    build = (root / "scripts" / "build_windows_cli.py").read_text(
        encoding="utf-8"
    )

    assert "runs-on: windows-2025" in workflow
    assert "architecture: x64" in workflow
    assert "scripts/build_windows_cli.py" in workflow
    assert "tests/test_windows_cli_release.py" in workflow
    assert "release/windows-cli/" in workflow
    assert "actions/setup-node" not in workflow
    assert "requirements-dev.txt" not in workflow
    assert "scripts/prepare_ohif.py" not in workflow
    assert "packaging/windows/dcmget.iss" not in workflow
    assert "require_amd64_pe(executable" in build
    assert "for name in DCMTK_BIN_FILES" in build
