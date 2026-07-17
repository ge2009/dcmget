from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from dcmget.core import DcmtkResolver
from dcmget.runtime import portable_dcmtk_bin, set_portable_dcmtk_bin
from dcmget.windows_portable_runtime import (
    MANIFEST_NAME,
    PortableRuntimeError,
    create_portable_runtime_manifest,
    prepare_windows_portable_dcmtk,
    publish_portable_dcmtk,
)
from scripts.build_windows import (
    DCMTK_PACKAGE_DIRECTORY,
    WINDOWS_DCMTK_DATA_DIRECTORIES,
    WINDOWS_DCMTK_DATA_FILES,
    WINDOWS_DCMTK_PE_FILES,
    pyinstaller_args,
    stage_minimal_windows_dcmtk,
    verify_packaged_dcmtk_tree,
)


def _make_bundle(root: Path, marker: bytes = b"first") -> tuple[Path, Path]:
    runtime = root / ".runtime" / "dcmtk" / "windows-x86_64"
    bin_directory = runtime / "dcmtk-3.7.0-win64-dynamic" / "bin"
    bin_directory.mkdir(parents=True)
    (bin_directory / "movescu.exe").write_bytes(b"movescu-" + marker)
    (bin_directory / "storescp.exe").write_bytes(b"storescp-" + marker)
    (bin_directory / "dcmmkdir.exe").write_bytes(b"dcmmkdir-" + marker)
    (bin_directory / "dcmdump.exe").write_bytes(b"dcmdump-" + marker)
    (bin_directory / "dcmtk.dll").write_bytes(b"dll-" + marker)
    data = runtime / "dcmtk-3.7.0-win64-dynamic" / "share" / "dicom.dic"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"dictionary-" + marker)
    create_portable_runtime_manifest(runtime, bin_directory, root / MANIFEST_NAME)
    return runtime, bin_directory


def _publish_worker(
    bundle: str,
    state: str,
    start_event,
    queue: multiprocessing.Queue,
) -> None:
    try:
        if not start_event.wait(timeout=20):
            raise TimeoutError("test process did not receive the start signal")
        queue.put(("ok", str(publish_portable_dcmtk(bundle, state))))
    except Exception as exc:  # pragma: no cover - only reported by a failed child
        queue.put(("error", repr(exc)))


def test_manifest_is_deterministic_and_covers_every_payload_file(tmp_path: Path):
    bundle = tmp_path / "bundle"
    runtime, bin_directory = _make_bundle(bundle)
    first = (bundle / MANIFEST_NAME).read_bytes()
    create_portable_runtime_manifest(runtime, bin_directory, bundle / MANIFEST_NAME)

    manifest = json.loads((bundle / MANIFEST_NAME).read_text(encoding="utf-8"))
    expected = {
        path.relative_to(runtime).as_posix()
        for path in runtime.rglob("*")
        if path.is_file()
    }

    assert (bundle / MANIFEST_NAME).read_bytes() == first
    assert [record["path"] for record in manifest["files"]] == sorted(expected)
    assert all(len(record["sha256"]) == 64 for record in manifest["files"])


def test_minimal_windows_runtime_contains_only_the_release_allowlist(tmp_path: Path):
    source = tmp_path / "official-runtime"
    package = source / DCMTK_PACKAGE_DIRECTORY
    for name in WINDOWS_DCMTK_PE_FILES:
        path = package / "bin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode("ascii"))
    for relative in WINDOWS_DCMTK_DATA_FILES:
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    expected_mapping_files = []
    for index, relative in enumerate(WINDOWS_DCMTK_DATA_DIRECTORIES):
        path = package / relative / f"mapping-{index}.dat"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mapping")
        expected_mapping_files.append(path.relative_to(source))
    (package / "bin" / "dcmj2pnm.exe").write_bytes(b"unused")
    (package / "bin" / "dcmdjpeg.exe").write_bytes(b"unused")
    man = package / "man" / "storescp.txt"
    man.parent.mkdir()
    man.write_text("unused", encoding="utf-8")

    staged, staged_bin = stage_minimal_windows_dcmtk(
        source, tmp_path / "staged-runtime"
    )

    actual = {
        path.relative_to(staged)
        for path in staged.rglob("*")
        if path.is_file()
    }
    expected = {
        Path(DCMTK_PACKAGE_DIRECTORY) / "bin" / name
        for name in WINDOWS_DCMTK_PE_FILES
    }
    expected.update(
        Path(DCMTK_PACKAGE_DIRECTORY) / relative
        for relative in WINDOWS_DCMTK_DATA_FILES
    )
    expected.update(expected_mapping_files)
    assert actual == expected
    assert staged_bin == staged / DCMTK_PACKAGE_DIRECTORY / "bin"
    assert not list(staged.rglob("dcmj2pnm.exe"))
    assert not list(staged.rglob("dcmdjpeg.exe"))
    assert not list(staged.rglob("man"))


def test_minimal_windows_runtime_rejects_missing_required_dll(tmp_path: Path):
    source = tmp_path / "official-runtime"
    package = source / DCMTK_PACKAGE_DIRECTORY
    for relative in WINDOWS_DCMTK_DATA_DIRECTORIES:
        path = package / relative / "mapping.dat"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mapping")
    for relative in WINDOWS_DCMTK_DATA_FILES:
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")

    with pytest.raises(FileNotFoundError, match="dcmdata.dll"):
        stage_minimal_windows_dcmtk(source, tmp_path / "staged-runtime")


def test_minimal_windows_runtime_rejects_symlinks(tmp_path: Path):
    source = tmp_path / "official-runtime"
    package = source / DCMTK_PACKAGE_DIRECTORY
    for name in WINDOWS_DCMTK_PE_FILES:
        path = package / "bin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode("ascii"))
    for relative in WINDOWS_DCMTK_DATA_FILES:
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
    for relative in WINDOWS_DCMTK_DATA_DIRECTORIES:
        path = package / relative / "mapping.dat"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mapping")
    linked = package / WINDOWS_DCMTK_DATA_DIRECTORIES[0] / "linked.dat"
    linked.symlink_to("mapping.dat")

    with pytest.raises(RuntimeError, match="符号链接"):
        stage_minimal_windows_dcmtk(source, tmp_path / "staged-runtime")


def test_packaged_runtime_must_exactly_match_the_minimal_staging_tree(tmp_path: Path):
    reference = tmp_path / "reference"
    packaged = tmp_path / "packaged"
    for root in (reference, packaged):
        path = root / DCMTK_PACKAGE_DIRECTORY / "bin" / "movescu.exe"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"same")

    verify_packaged_dcmtk_tree(packaged, reference)

    (packaged / DCMTK_PACKAGE_DIRECTORY / "bin" / "dcmj2pnm.exe").write_bytes(
        b"extra"
    )
    with pytest.raises(RuntimeError, match="文件集合"):
        verify_packaged_dcmtk_tree(packaged, reference)


def test_portable_manifest_requires_pdi_and_validation_tools(tmp_path: Path):
    bundle = tmp_path / "bundle"
    runtime, bin_directory = _make_bundle(bundle)
    (bin_directory / "dcmmkdir.exe").unlink()

    with pytest.raises(PortableRuntimeError, match="必需"):
        create_portable_runtime_manifest(
            runtime,
            bin_directory,
            bundle / "invalid-manifest.json",
        )


def test_publish_uses_stable_user_runtime_and_repairs_tampering(tmp_path: Path):
    bundle = tmp_path / "_MEI12345"
    _make_bundle(bundle)
    state = tmp_path / "LocalAppData" / "DcmGet"

    first = publish_portable_dcmtk(bundle, state)
    target = first.parent.parent
    manifest = json.loads((bundle / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert first == target / "dcmtk-3.7.0-win64-dynamic" / "bin"
    assert target.parent == state / "runtime" / "dcmtk"
    assert target.name.endswith(manifest["payload_sha256"][:16])
    assert "_MEI" not in str(first)
    assert (first / "storescp.exe").read_bytes() == b"storescp-first"

    (first / "storescp.exe").write_bytes(b"tampered")
    second = publish_portable_dcmtk(bundle, state)

    assert second == first
    assert (second / "storescp.exe").read_bytes() == b"storescp-first"
    assert not list(target.parent.glob(".*.tmp-*"))
    assert not list(target.parent.glob(".*.invalid-*"))


def test_two_processes_publish_the_same_atomic_runtime(tmp_path: Path):
    bundle = tmp_path / "bundle"
    _make_bundle(bundle)
    state = tmp_path / "state"
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    queue = context.Queue()
    processes = [
        context.Process(
            target=_publish_worker,
            args=(str(bundle), str(state), start_event, queue),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    start_event.set()
    results = [queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)

    assert all(process.exitcode == 0 for process in processes)
    assert all(status == "ok" for status, _value in results), results
    assert len({value for _status, value in results}) == 1
    runtime_root = state / "runtime" / "dcmtk"
    targets = [path for path in runtime_root.iterdir() if path.is_dir()]
    assert len(targets) == 1
    assert not list(runtime_root.glob(".*.tmp-*"))


def test_new_payload_keeps_previous_versioned_runtime(tmp_path: Path):
    first_bundle = tmp_path / "bundle-a"
    second_bundle = tmp_path / "bundle-b"
    _make_bundle(first_bundle, b"a")
    _make_bundle(second_bundle, b"b")
    state = tmp_path / "state"

    first = publish_portable_dcmtk(first_bundle, state)
    second = publish_portable_dcmtk(second_bundle, state)

    assert first != second
    assert first.is_dir() and second.is_dir()
    assert (first / "storescp.exe").read_bytes() == b"storescp-a"
    assert (second / "storescp.exe").read_bytes() == b"storescp-b"


@pytest.mark.parametrize("unsafe", ["../outside.exe", "safe/ads:stream"])
def test_manifest_rejects_unsafe_paths_before_copying(tmp_path: Path, unsafe: str):
    bundle = tmp_path / "bundle"
    _make_bundle(bundle)
    manifest_path = bundle / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = unsafe
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PortableRuntimeError, match="不安全"):
        publish_portable_dcmtk(bundle, tmp_path / "state")
    assert not (tmp_path / "outside.exe").exists()


def test_portable_marker_is_required_and_resolver_prefers_published_bin(
    tmp_path: Path, monkeypatch
):
    import dcmget.windows_portable_runtime as portable_runtime

    set_portable_dcmtk_bin(None)
    monkeypatch.setattr(portable_runtime.sys, "platform", "win32")
    monkeypatch.setattr(portable_runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(
        portable_runtime, "application_state_dir", lambda: tmp_path / "state"
    )
    try:
        assert prepare_windows_portable_dcmtk(tmp_path / "onedir") is None
        assert portable_dcmtk_bin() is None

        bundle = tmp_path / "portable"
        _make_bundle(bundle)
        published = prepare_windows_portable_dcmtk(bundle)
        assert published is not None
        candidates = list(DcmtkResolver(tmp_path / "project")._candidate_directories(""))
        assert candidates[0] == published.resolve()
        configured = tmp_path / "configured-bin"
        configured.mkdir()
        candidates = list(
            DcmtkResolver(tmp_path / "project")._candidate_directories(str(configured))
        )
        assert candidates[:2] == [configured.resolve(), published.resolve()]
    finally:
        set_portable_dcmtk_bin(None)


def test_pyinstaller_manifest_marker_is_opt_in_for_onefile(tmp_path: Path):
    common = (
        tmp_path / "icon.ico",
        tmp_path / "version.txt",
        tmp_path / "runtime",
    )
    manifest = tmp_path / MANIFEST_NAME
    onedir = pyinstaller_args("DcmGet", "--onedir", *common)
    onefile = pyinstaller_args(
        "DcmGet-Portable",
        "--onefile",
        *common,
        portable_runtime_manifest=manifest,
    )

    marker = f"{manifest}:."
    assert marker not in onedir
    assert marker in onefile
