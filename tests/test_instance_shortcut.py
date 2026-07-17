from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import dcmget.instance_shortcut as shortcut_module
from dcmget.instance_shortcut import (
    InstanceShortcutError,
    ShortcutExistsError,
    build_instance_launch_command,
    create_instance_shortcut,
    default_instance_shortcut_name,
    normalize_shortcut_name,
)


def test_default_name_uses_receiver_port_and_ae():
    assert default_instance_shortcut_name(6666, " DCMGET ") == "dcmget-6666-DCMGET"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("dcmget-6666-AE", "dcmget-6666-AE"),
        (" DcmGet:6666/AE. ", "DcmGet-6666-AE"),
        ("检查一号", "检查一号"),
        ("CON", "_CON"),
        ("LPT1.txt", "_LPT1.txt"),
    ],
)
def test_shortcut_name_is_windows_safe(value, expected):
    assert normalize_shortcut_name(value) == expected


@pytest.mark.parametrize("value", ["", "  ", "...", "***"])
def test_shortcut_name_rejects_empty_result(value):
    with pytest.raises(InstanceShortcutError, match="名称"):
        normalize_shortcut_name(value)


def test_frozen_launch_targets_current_executable_and_only_profile_argument(tmp_path):
    executable = tmp_path / "DcmGet.exe"
    executable.write_bytes(b"MZ")

    launch = build_instance_launch_command(
        7,
        project_root=tmp_path,
        executable=executable,
        frozen=True,
    )

    assert launch.target == executable.resolve()
    assert launch.arguments == ("--profile", "7")
    assert launch.working_directory == executable.resolve().parent


def test_source_launch_targets_python_and_ui_entrypoint(tmp_path):
    executable = tmp_path / "Python 3.12" / "python.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"MZ")
    entrypoint = tmp_path / "DICOM_download_ui.py"
    entrypoint.write_text("", encoding="utf-8")

    launch = build_instance_launch_command(
        3,
        project_root=tmp_path,
        executable=executable,
        frozen=False,
    )

    assert launch.target == executable.resolve()
    assert launch.arguments == (str(entrypoint.resolve()), "--profile", "3")
    assert launch.working_directory == tmp_path.resolve()


@pytest.mark.parametrize("profile_number", [0, 10000, True, "bad"])
def test_launch_rejects_invalid_profile_numbers(tmp_path, profile_number):
    executable = tmp_path / "DcmGet.exe"
    executable.write_bytes(b"MZ")

    with pytest.raises(InstanceShortcutError, match="1 到 9999"):
        build_instance_launch_command(
            profile_number,
            project_root=tmp_path,
            executable=executable,
            frozen=True,
        )


def test_launch_rejects_missing_executable(tmp_path):
    with pytest.raises(InstanceShortcutError, match="程序文件不存在"):
        build_instance_launch_command(
            1,
            project_root=tmp_path,
            executable=tmp_path / "missing.exe",
            frozen=True,
        )


def test_source_launch_rejects_missing_entrypoint(tmp_path):
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"MZ")

    with pytest.raises(InstanceShortcutError, match="源码启动文件不存在"):
        build_instance_launch_command(
            1,
            project_root=tmp_path,
            executable=executable,
            frozen=False,
        )


def test_macos_shortcut_contains_quoted_profile_launch_and_is_executable(tmp_path):
    project = tmp_path / "Project With Spaces"
    project.mkdir()
    (project / "DICOM_download_ui.py").write_text("", encoding="utf-8")
    executable = tmp_path / "Python 3.12" / "python3"
    executable.parent.mkdir()
    executable.write_bytes(b"python")
    desktop = tmp_path / "Desktop"

    result = create_instance_shortcut(
        4,
        "dcmget-6666-AE",
        desktop,
        project_root=project,
        executable=executable,
        frozen=False,
        platform="darwin",
    )

    assert result.name == "dcmget-6666-AE.command"
    content = result.read_text(encoding="utf-8")
    assert "--profile 4" in content
    assert "Project With Spaces" in content
    if os.name != "nt":
        assert result.stat().st_mode & 0o111


def test_existing_shortcut_requires_explicit_overwrite(tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    existing = desktop / "dcmget-6666-AE.command"
    existing.write_text("old", encoding="utf-8")

    with pytest.raises(ShortcutExistsError) as captured:
        create_instance_shortcut(
            1,
            "dcmget-6666-AE",
            desktop,
            project_root=tmp_path,
            executable=tmp_path / "python3",
            frozen=False,
            platform="darwin",
        )

    assert captured.value.path == existing
    assert existing.read_text(encoding="utf-8") == "old"


def test_windows_shortcut_uses_powershell_without_shell_and_passes_exact_arguments(
    tmp_path, monkeypatch
):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        Path(kwargs["env"]["DCMGET_SHORTCUT_TEMP"]).write_bytes(b"lnk")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(shortcut_module.shutil, "which", lambda _name: "powershell.exe")
    monkeypatch.setattr(shortcut_module.subprocess, "run", fake_run)
    executable = tmp_path / "DcmGet Portable.exe"
    executable.write_bytes(b"MZ")

    result = create_instance_shortcut(
        12,
        "dcmget-7777-AE12",
        tmp_path / "Desktop",
        project_root=tmp_path,
        executable=executable,
        frozen=True,
        platform="win32",
    )

    command, kwargs = calls[0]
    assert command[:3] == ["powershell.exe", "-NoProfile", "-NonInteractive"]
    assert kwargs["shell"] is False
    assert kwargs["env"]["DCMGET_SHORTCUT_TARGET"].endswith("DcmGet Portable.exe")
    assert kwargs["env"]["DCMGET_SHORTCUT_ARGUMENTS"] == "--profile 12"
    assert result.name == "dcmget-7777-AE12.lnk"
    assert result.read_bytes() == b"lnk"


def test_filesystem_errors_are_reported_as_shortcut_errors(tmp_path):
    destination = tmp_path / "Desktop"
    destination.write_text("not a directory", encoding="utf-8")

    with pytest.raises(InstanceShortcutError, match="无法写入快捷方式"):
        create_instance_shortcut(
            1,
            "dcmget-6666-AE",
            destination,
            project_root=tmp_path,
            executable=tmp_path / "python3",
            frozen=False,
            platform="darwin",
        )
