from __future__ import annotations

import pytest

from dcmget.instance_shortcut import (
    InstanceShortcutError,
    ShortcutExistsError,
    build_instance_launch_command,
    create_instance_shortcut,
    default_instance_shortcut_name,
    normalize_shortcut_name,
    profile_web_url,
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


def test_profile_web_launch_adds_explicit_open_flag(tmp_path):
    executable = tmp_path / "DcmGet.exe"
    executable.write_bytes(b"MZ")

    launch = build_instance_launch_command(
        7,
        project_root=tmp_path,
        executable=executable,
        frozen=True,
        open_profile_web=True,
    )

    assert launch.arguments == ("--profile", "7", "--open-profile-web")


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


def test_macos_shortcut_is_a_direct_profile_web_location(tmp_path):
    desktop = tmp_path / "Desktop"

    result = create_instance_shortcut(
        4,
        "dcmget-6666-AE",
        desktop,
        web_port=8787,
        platform="darwin",
    )

    assert result.name == "dcmget-6666-AE.webloc"
    content = result.read_text(encoding="utf-8")
    assert "http://127.0.0.1:8787/" in content
    assert "--profile" not in content


def test_existing_shortcut_requires_explicit_overwrite(tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    existing = desktop / "dcmget-6666-AE.webloc"
    existing.write_text("old", encoding="utf-8")

    with pytest.raises(ShortcutExistsError) as captured:
        create_instance_shortcut(
            1,
            "dcmget-6666-AE",
            desktop,
            web_port=8787,
            platform="darwin",
        )

    assert captured.value.path == existing
    assert existing.read_text(encoding="utf-8") == "old"


def test_windows_shortcut_is_a_direct_profile_web_url(tmp_path):
    result = create_instance_shortcut(
        12,
        "dcmget-7777-AE12",
        tmp_path / "Desktop",
        web_port=8899,
        platform="win32",
    )

    assert result.name == "dcmget-7777-AE12.url"
    assert result.read_text(encoding="utf-8") == (
        "[InternetShortcut]\nURL=http://127.0.0.1:8899/\nIconIndex=0\n"
    )


def test_linux_shortcut_uses_desktop_link_without_starting_a_process(tmp_path):
    result = create_instance_shortcut(
        2,
        "dcmget-6667-AE02",
        tmp_path,
        web_port=8788,
        platform="linux",
    )

    assert result.name == "dcmget-6667-AE02.desktop"
    content = result.read_text(encoding="utf-8")
    assert "Type=Link" in content
    assert "URL=http://127.0.0.1:8788/" in content
    assert "Exec=" not in content


def test_shortcut_requires_a_valid_loopback_profile_url(tmp_path):
    with pytest.raises(InstanceShortcutError, match="必须提供 Profile Web 端口"):
        create_instance_shortcut(
            1,
            "dcmget",
            tmp_path,
            platform="win32",
        )
    with pytest.raises(InstanceShortcutError, match="本机 Profile Web 地址"):
        create_instance_shortcut(
            1,
            "dcmget",
            tmp_path,
            url="https://example.com/",
            platform="win32",
        )
    assert profile_web_url(8787) == "http://127.0.0.1:8787/"


def test_filesystem_errors_are_reported_as_shortcut_errors(tmp_path):
    destination = tmp_path / "Desktop"
    destination.write_text("not a directory", encoding="utf-8")

    with pytest.raises(InstanceShortcutError, match="无法写入快捷方式"):
        create_instance_shortcut(
            1,
            "dcmget-6666-AE",
            destination,
            web_port=8787,
            platform="darwin",
        )
