from __future__ import annotations

from pathlib import Path

import pytest

import dcmget.profile_web_operations as operations_module
from dcmget.config import AppConfig, save_config
from dcmget.profile_manager import ProfileManager
from dcmget.profile_web_operations import ProfileWebOperations


def _manager(tmp_path: Path, **kwargs: object) -> ProfileManager:
    return ProfileManager(
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
        **kwargs,
    )


def _write_profile(
    tmp_path: Path,
    number: int,
    *,
    port: int = 6666,
    web_port: int = 8787,
    storage_ae: str = "DCMGET",
) -> Path:
    path = tmp_path / "config" / "instances" / f"i{number}" / "config.json"
    save_config(
        path,
        AppConfig(
            calling_ae_title="CALLING",
            pacs_ae_title="PACS01",
            storage_ae_title=storage_ae,
            storage_port=port,
            web_port=web_port,
            dicom_destination_folder=str(tmp_path / f"dicom-{number}"),
        ),
    )
    return path


def _operations(tmp_path: Path, **kwargs: object) -> ProfileWebOperations:
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"MZ")
    entrypoint = tmp_path / "DICOM_download_ui.py"
    entrypoint.write_text("print('ok')\n", encoding="utf-8")
    return ProfileWebOperations(
        manager=_manager(tmp_path, port_probe=lambda _host, _port: True),
        project_root=tmp_path,
        executable=executable,
        frozen=False,
        desktop_directory=tmp_path / "Desktop",
        **kwargs,
    )


def test_list_profiles_returns_json_safe_profiles(tmp_path):
    _write_profile(tmp_path, 1, port=6667, web_port=8788, storage_ae="AE01")

    result = _operations(tmp_path).list_profiles()

    assert result["count"] == 1
    profile = result["profiles"][0]
    assert profile["number"] == 1
    assert profile["display_name"] == "实例 1"
    assert profile["storage_port"] == 6667
    assert profile["web_port"] == 8788
    assert isinstance(profile["config_path"], str)
    assert profile["storage_ae_title"] == "AE01"


def test_clone_rename_and_delete_round_trip(tmp_path):
    _write_profile(tmp_path, 1)
    operations = _operations(tmp_path)

    cloned = operations.clone_profile(
        {"source_profile_number": 1, "display_name": "夜班实例"}
    )
    renamed = operations.rename_profile(
        {"profile_number": 2, "display_name": "夜班实例-改"}
    )
    deleted = operations.delete_profile({"profile_number": 2})

    assert cloned["profile"]["number"] == 2
    assert cloned["profile"]["display_name"] == "夜班实例"
    assert renamed["profile"]["display_name"] == "夜班实例-改"
    assert deleted == {"ok": True, "deleted_profile_number": 2}
    assert operations.list_profiles()["count"] == 1


def test_launch_profile_uses_subprocess_popen_without_shell(tmp_path, monkeypatch):
    _write_profile(tmp_path, 3)
    calls: list[tuple[list[str], dict[str, object]]] = []

    class _FakeProcess:
        pid = 43210

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        calls.append((command, kwargs))
        return _FakeProcess()

    operations = _operations(tmp_path, popen=fake_popen)

    result = operations.launch_profile({"profile_number": 3})

    command, kwargs = calls[0]
    assert result["pid"] == 43210
    assert command[0].endswith("python.exe")
    assert command[-2:] == ["--profile", "3"]
    assert kwargs["shell"] is False
    assert str(kwargs["cwd"]).endswith(str(tmp_path))


def test_create_shortcut_defaults_to_desktop_and_profile_based_name(
    tmp_path,
    monkeypatch,
):
    _write_profile(tmp_path, 5, port=7777, storage_ae="AE05")
    captured: dict[str, object] = {}

    def fake_create_instance_shortcut(
        profile_number: int,
        name: str,
        destination_directory: str | Path,
        **kwargs: object,
    ) -> Path:
        captured["profile_number"] = profile_number
        captured["name"] = name
        captured["destination_directory"] = Path(destination_directory)
        captured["overwrite"] = kwargs["overwrite"]
        path = Path(destination_directory) / f"{name}.command"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return path

    monkeypatch.setattr(
        operations_module,
        "create_instance_shortcut",
        fake_create_instance_shortcut,
    )
    operations = _operations(tmp_path)

    result = operations.create_shortcut({"profile_number": 5, "overwrite": True})

    assert captured["profile_number"] == 5
    assert captured["name"] == "dcmget-7777-AE05"
    assert captured["destination_directory"] == (tmp_path / "Desktop").resolve()
    assert captured["overwrite"] is True
    assert result["shortcut"]["destination_directory"] == str(
        (tmp_path / "Desktop").resolve()
    )


@pytest.mark.parametrize(
    ("method_name", "payload", "message"),
    [
        ("clone_profile", {"source_profile_number": True}, "实例编号必须在 1 到 9999 之间"),
        ("rename_profile", {"profile_number": 1, "display_name": 1}, "Profile 显示名必须是字符串"),
        ("delete_profile", {"profile_number": 0}, "实例编号必须在 1 到 9999 之间"),
        ("launch_profile", {"profile_number": "bad"}, "实例编号必须在 1 到 9999 之间"),
        ("create_shortcut", {"profile_number": 1, "overwrite": "yes"}, "overwrite 必须是布尔值"),
    ],
)
def test_payload_validation_is_strict(tmp_path, method_name, payload, message):
    _write_profile(tmp_path, 1)
    operations = _operations(tmp_path)

    with pytest.raises(ValueError, match=message):
        getattr(operations, method_name)(payload)
