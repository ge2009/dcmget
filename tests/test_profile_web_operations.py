from __future__ import annotations

import threading
from pathlib import Path

import pytest
from filelock import FileLock

import dcmget.profile_web_operations as operations_module
from dcmget.config import AppConfig, save_config
from dcmget.profile_manager import ProfileManager
from dcmget.profile_web_operations import ProfileWebOperations
from dcmget.profile_runtime_state import ProfileRuntimeState


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


def _operations(
    tmp_path: Path,
    *,
    port_probe=lambda _host, _port: True,
    **kwargs: object,
) -> ProfileWebOperations:
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"MZ")
    entrypoint = tmp_path / "DICOM_download_ui.py"
    entrypoint.write_text("print('ok')\n", encoding="utf-8")
    return ProfileWebOperations(
        manager=_manager(tmp_path, port_probe=port_probe),
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
    assert profile["pacs_server_ip"] == "127.0.0.1"
    assert profile["pacs_server_port"] == 8104
    assert profile["storage_port"] == 6667
    assert profile["web_port"] == 8788
    assert profile["dicom_destination_folder"] == str(tmp_path / "dicom-1")
    assert isinstance(profile["config_path"], str)
    assert profile["storage_ae_title"] == "AE01"
    assert profile["issues"] == []
    assert profile["desired_running"] is False


def test_create_profile_is_stopped_by_default(tmp_path):
    operations = _operations(tmp_path)

    result = operations.create_profile({"display_name": "新建工作站"})

    assert result["profile"]["display_name"] == "新建工作站"
    assert result["profile"]["desired_running"] is False
    assert operations.runtime_state.desired_profiles() == ()


def test_list_profiles_surfaces_port_conflicts_without_hiding_other_profiles(tmp_path):
    _write_profile(tmp_path, 1, port=6666, web_port=8787, storage_ae="AE01")
    _write_profile(tmp_path, 3, port=6666, web_port=8789, storage_ae="AE03")

    profiles = _operations(tmp_path).list_profiles()["profiles"]

    assert len(profiles) == 2
    assert all(profile["issues"] for profile in profiles)
    assert all("6666" in profile["issues"][0] for profile in profiles)


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


def test_update_profile_saves_full_launch_configuration(tmp_path):
    _write_profile(tmp_path, 1)
    operations = _operations(tmp_path)

    result = operations.update_profile(
        {
            "profile_number": 1,
            "display_name": "CT 下载",
            "pacs_server_ip": "172.16.0.20",
            "pacs_server_port": 104,
            "calling_ae_title": "MACGET",
            "pacs_ae_title": "PACS02",
            "storage_ae_title": "MACGET",
            "storage_port": 6777,
            "web_port": 8899,
            "dicom_destination_folder": str(tmp_path / "result"),
        }
    )

    assert result["ok"]
    assert result["warnings"] == []
    assert result["errors"] == {}
    assert result["profile"]["display_name"] == "CT 下载"
    assert result["profile"]["pacs_server_ip"] == "172.16.0.20"
    assert result["profile"]["storage_port"] == 6777
    assert result["profile"]["web_port"] == 8899


def test_update_profile_rejects_running_instance(tmp_path):
    _write_profile(tmp_path, 1)
    lock_path = tmp_path / "state" / "instances" / "i1.lock"
    lock_path.parent.mkdir(parents=True)
    lock = FileLock(str(lock_path))
    lock.acquire(timeout=0)
    try:
        with pytest.raises(RuntimeError, match="先停止后再修改"):
            _operations(tmp_path).update_profile(
                {"profile_number": 1, "web_port": 8899}
            )
    finally:
        lock.release()


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
    assert command[-3:] == ["--profile", "3", "--no-open-browser"]
    assert kwargs["shell"] is False
    assert str(kwargs["cwd"]).endswith(str(tmp_path))
    assert result["url"] == "http://127.0.0.1:8787/"
    assert operations.runtime_state.desired_profiles() == (3,)


def test_launch_profile_repairs_occupied_internal_web_port(tmp_path):
    config_path = _write_profile(tmp_path, 1, web_port=8787)

    class _FakeProcess:
        pid = 43210

    operations = _operations(
        tmp_path,
        port_probe=lambda _host, port: port != 8787,
        popen=lambda _command, **_kwargs: _FakeProcess(),
    )

    result = operations.launch_profile({"profile_number": 1})

    assert result["ok"] is True
    assert result["profile"]["web_port"] == 8788
    assert result["url"] == "http://127.0.0.1:8788/"
    config = operations.manager._load_profile_config(1)
    assert config_path.is_file()
    assert config.web_port == 8788
    assert config.web_bind_address == "127.0.0.1"


def test_launch_profile_does_not_recheck_ports_after_process_starts(tmp_path):
    _write_profile(tmp_path, 1)
    spawned = False

    class _FakeProcess:
        pid = 43210

    def port_probe(_host: str, _port: int) -> bool:
        return not spawned

    def fake_popen(_command: list[str], **_kwargs: object) -> _FakeProcess:
        nonlocal spawned
        spawned = True
        return _FakeProcess()

    result = _operations(
        tmp_path,
        port_probe=port_probe,
        popen=fake_popen,
    ).launch_profile({"profile_number": 1})

    assert result["ok"] is True
    assert result["pid"] == 43210


def test_stop_profile_waits_for_lock_and_ports_before_reporting_success(tmp_path):
    _write_profile(tmp_path, 1)
    runtime = ProfileRuntimeState(tmp_path / "state" / "management" / "profile-runtime.json")
    runtime.set_desired(1, True)
    lock_path = tmp_path / "state" / "instances" / "i1.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path))
    lock.acquire(timeout=0)
    calls: list[int] = []
    desired_during_shutdown: list[bool] = []

    def shutdown(profile):
        calls.append(profile.number)
        desired_during_shutdown.append(runtime.is_desired(profile.number))
        lock.release()

    try:
        operations = _operations(
            tmp_path,
            runtime_state=runtime,
            shutdown_profile=shutdown,
            stop_timeout_seconds=0.2,
        )

        result = operations.stop_profile({"profile_number": 1})
    finally:
        if lock.is_locked:
            lock.release()

    assert result["ok"]
    assert result["stopped"]
    assert result["message"] == "Profile 已停止，Web 与 DICOM 接收端口均已释放"
    assert result["profile"]["desired_running"] is False
    assert calls == [1]
    assert desired_during_shutdown == [True]
    assert runtime.desired_profiles() == ()


def test_running_profile_without_shutdown_transport_fails_immediately(tmp_path):
    _write_profile(tmp_path, 1)
    lock = FileLock(str(tmp_path / "state" / "instances" / "i1.lock"))
    lock.acquire(timeout=0)
    try:
        operations = _operations(tmp_path, stop_timeout_seconds=0)

        with pytest.raises(RuntimeError, match="统一管理中心"):
            operations.stop_profile({"profile_number": 1})
    finally:
        lock.release()


def test_stop_profile_refuses_to_report_stopped_while_receiver_port_is_occupied(
    tmp_path,
):
    _write_profile(tmp_path, 1)
    operations = _operations(
        tmp_path,
        port_probe=lambda _host, port: port != 6666,
        stop_timeout_seconds=0,
    )

    with pytest.raises(RuntimeError, match="DICOM 接收端口 6666"):
        operations.stop_profile({"profile_number": 1})


def test_profile_manager_stop_blockers_require_lock_and_both_ports_free(tmp_path):
    _write_profile(tmp_path, 1)
    occupied = {6666, 8787}
    manager = _manager(
        tmp_path,
        port_probe=lambda _host, port: port not in occupied,
    )

    stopped, blockers = manager.wait_for_profile_stopped(1, timeout=0)

    assert not stopped
    assert blockers == ("DICOM 接收端口 6666", "Web 端口 8787")


def test_launch_all_starts_idle_profiles_and_skips_running_profile(
    tmp_path,
):
    _write_profile(tmp_path, 1, port=6666, web_port=8787)
    _write_profile(tmp_path, 2, port=6667, web_port=8788)
    lock_path = tmp_path / "state" / "instances" / "i1.lock"
    lock_path.parent.mkdir(parents=True)
    lock = FileLock(str(lock_path))
    lock.acquire(timeout=0)
    calls: list[list[str]] = []

    class _FakeProcess:
        pid = 43210

    def fake_popen(command: list[str], **_kwargs: object) -> _FakeProcess:
        calls.append(command)
        return _FakeProcess()

    try:
        result = _operations(tmp_path, popen=fake_popen).launch_all_profiles()
    finally:
        lock.release()

    assert result["ok"]
    assert result["started_count"] == 1
    assert result["skipped_count"] == 1
    assert result["error_count"] == 0
    assert result["started"][0]["profile_number"] == 2
    assert result["skipped"][0]["profile_number"] == 1
    assert calls[0][-3:] == ["--profile", "2", "--no-open-browser"]


def test_launch_all_repairs_occupied_internal_web_ports(tmp_path):
    _write_profile(tmp_path, 1, port=6666, web_port=8787)

    class _FakeProcess:
        pid = 43210

    result = _operations(
        tmp_path,
        port_probe=lambda _host, port: port != 8787,
        popen=lambda _command, **_kwargs: _FakeProcess(),
    ).launch_all_profiles()

    assert result["ok"]
    assert result["started_count"] == 1
    assert result["error_count"] == 0
    assert result["started"][0]["url"] == "http://127.0.0.1:8788/"


def test_launch_and_delete_same_profile_are_serialized(tmp_path):
    config_path = _write_profile(tmp_path, 1)
    popen_entered = threading.Event()
    release_popen = threading.Event()
    delete_done = threading.Event()
    results: dict[str, object] = {}

    class _FakeProcess:
        pid = 43210

    def fake_popen(_command: list[str], **_kwargs: object) -> _FakeProcess:
        popen_entered.set()
        assert release_popen.wait(2)
        return _FakeProcess()

    operations = _operations(tmp_path, popen=fake_popen)

    def launch() -> None:
        results["launch"] = operations.launch_profile({"profile_number": 1})

    def delete() -> None:
        try:
            operations.delete_profile({"profile_number": 1})
        except Exception as exc:
            results["delete_error"] = exc
        finally:
            delete_done.set()

    launch_thread = threading.Thread(target=launch)
    delete_thread = threading.Thread(target=delete)
    launch_thread.start()
    assert popen_entered.wait(2)
    delete_thread.start()
    assert not delete_done.wait(0.1)
    release_popen.set()
    launch_thread.join(2)
    delete_thread.join(2)

    assert not launch_thread.is_alive()
    assert not delete_thread.is_alive()
    assert isinstance(results["delete_error"], RuntimeError)
    assert "先停止后再删除" in str(results["delete_error"])
    assert config_path.is_file()
    assert operations.runtime_state.desired_profiles() == (1,)


def test_launch_all_terminates_spawned_process_when_runtime_state_write_fails(
    tmp_path,
    monkeypatch,
):
    _write_profile(tmp_path, 1)
    terminated = threading.Event()
    waited = threading.Event()

    class _FakeProcess:
        pid = 43210

        def terminate(self) -> None:
            terminated.set()

        def wait(self, *, timeout: float) -> int:
            assert timeout == 5
            waited.set()
            return 0

    operations = _operations(
        tmp_path,
        popen=lambda *_args, **_kwargs: _FakeProcess(),
    )

    def fail_write(_profile_number: int, _desired: bool) -> None:
        raise OSError("runtime state write failed")

    monkeypatch.setattr(operations.runtime_state, "set_desired", fail_write)

    result = operations.launch_all_profiles()

    assert result["ok"] is False
    assert result["started_count"] == 0
    assert result["error_count"] == 1
    assert "runtime state write failed" in result["errors"][0]["error"]
    assert terminated.is_set()
    assert waited.is_set()


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
        captured["web_port"] = kwargs["web_port"]
        path = Path(destination_directory) / f"{name}.url"
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
    assert captured["web_port"] == 8787
    assert result["shortcut"]["destination_directory"] == str(
        (tmp_path / "Desktop").resolve()
    )
    assert result["url"] == "http://127.0.0.1:8787/"


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
