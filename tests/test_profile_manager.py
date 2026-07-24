from __future__ import annotations

import json
from pathlib import Path

import pytest
from filelock import FileLock

import dcmget.profile_manager as profile_manager_module
from dcmget.config import AppConfig, load_config, save_config
from dcmget.profile_manager import (
    ProfileInUseError,
    ProfileManager,
    ProfileManagerError,
    ProfileNotFoundError,
    ProfileRecoveryExistsError,
    WINDOWS_MANAGEMENT_PORT,
)


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
    destination: Path | None = None,
    storage_ae: str = "DCMGET",
    web_port: int = 8787,
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
            dicom_destination_folder=str(destination or tmp_path / f"dicom-{number}"),
        ),
    )
    return path


def test_list_profiles_exposes_identity_endpoint_and_status(tmp_path):
    config_path = _write_profile(
        tmp_path,
        2,
        port=6672,
        storage_ae="DCMGET2",
    )
    recovery = (
        tmp_path / "state" / "instances" / "i2" / "active-task.sqlite3"
    )
    recovery.parent.mkdir(parents=True)
    recovery.write_bytes(b"checkpoint")

    profiles = _manager(tmp_path).list_profiles()

    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.number == 2
    assert profile.display_name == "实例 2"
    assert profile.config_path == config_path
    assert profile.pacs_server_ip == "127.0.0.1"
    assert profile.pacs_server_port == 8104
    assert profile.calling_ae_title == "CALLING"
    assert profile.pacs_ae_title == "PACS01"
    assert profile.storage_ae_title == "DCMGET2"
    assert profile.storage_port == 6672
    assert profile.web_port == 8787
    assert profile.destination_directory == str(tmp_path / "dicom-2")
    assert not profile.is_running
    assert profile.has_recovery


def test_create_profile_allocates_safe_defaults_without_starting(tmp_path):
    checked: list[int] = []

    def probe(_host: str, port: int) -> bool:
        checked.append(port)
        return port not in {6666, 8787}

    manager = _manager(tmp_path, port_probe=probe)

    profile = manager.create_profile(display_name="CT 工作站")

    assert profile.number == 1
    assert profile.display_name == "CT 工作站"
    assert profile.storage_port == 6667
    assert profile.web_port == 8788
    assert not profile.is_running
    assert profile.config_path.is_file()
    assert load_config(profile.config_path).web_bind_address == "127.0.0.1"
    assert checked == [6666, 6667, 8787, 8788]


def test_rename_profile_persists_utf8_metadata_atomically(tmp_path):
    _write_profile(tmp_path, 1)
    manager = _manager(tmp_path)

    renamed = manager.rename_profile(1, "放射科夜班")

    metadata_path = (
        tmp_path / "config" / "instances" / "i1" / "profile-meta.json"
    )
    assert renamed.display_name == "放射科夜班"
    assert manager.get_profile(1).display_name == "放射科夜班"
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == {
        "schema": "dcmget-profile-meta",
        "version": 1,
        "display_name": "放射科夜班",
    }
    assert list(metadata_path.parent.glob(".profile-meta.json.*.tmp")) == []


def test_clone_allocates_new_number_and_changes_only_new_profile_port(tmp_path):
    source_path = _write_profile(tmp_path, 1, port=6666, storage_ae="SOURCE")
    _write_profile(tmp_path, 2, port=6667, storage_ae="SECOND")
    checked: list[int] = []

    def probe(_host: str, port: int) -> bool:
        checked.append(port)
        return port >= 6669

    manager = _manager(tmp_path, port_probe=probe)
    result = manager.clone_profile(1, display_name="CT 下载 2")

    assert result.source_number == 1
    assert result.profile.number == 3
    assert result.profile.display_name == "CT 下载 2"
    assert result.recommended_port == 6669
    assert result.recommended_web_port == 8788
    assert result.profile.storage_port == 6669
    assert result.profile.web_port == 8788
    assert result.profile.storage_ae_title == "SOURCE"
    assert checked == [6668, 6669, 8788]
    assert load_config(source_path).storage_port == 6666
    cloned = load_config(result.profile.config_path)
    source = load_config(source_path)
    assert {
        **cloned.to_dict(),
        "storage_port": source.storage_port,
        "web_port": source.web_port,
        "web_bind_address": source.web_bind_address,
    } == source.to_dict()
    assert cloned.web_bind_address == "127.0.0.1"


def test_recommend_port_skips_profile_ports_and_failed_local_bind(tmp_path):
    _write_profile(tmp_path, 1, port=7000, web_port=7001)
    checked: list[int] = []

    def probe(_host: str, port: int) -> bool:
        checked.append(port)
        return port == 7002

    manager = _manager(tmp_path, port_probe=probe)

    assert manager.recommend_available_port(7000) == 7002
    assert checked == [7002]


def test_recommend_port_always_skips_windows_management_port(tmp_path):
    checked: list[int] = []
    manager = _manager(
        tmp_path,
        port_probe=lambda _host, port: checked.append(port) or True,
    )

    assert manager.recommend_available_port(WINDOWS_MANAGEMENT_PORT) == 8787
    assert checked == [8787]


def test_ensure_internal_web_endpoint_uses_loopback_and_keeps_free_port(tmp_path):
    config_path = _write_profile(tmp_path, 1, web_port=8899)
    manager = _manager(tmp_path, port_probe=lambda _host, _port: True)

    profile = manager.ensure_internal_web_endpoint(1)

    saved = load_config(config_path)
    assert profile.web_port == 8899
    assert saved.web_port == 8899
    assert saved.web_bind_address == "127.0.0.1"


def test_ensure_internal_web_endpoint_replaces_occupied_hidden_port(tmp_path):
    config_path = _write_profile(tmp_path, 1, web_port=8787)
    checked: list[tuple[str, int]] = []

    def probe(host: str, port: int) -> bool:
        checked.append((host, port))
        return port == 8788

    profile = _manager(tmp_path, port_probe=probe).ensure_internal_web_endpoint(1)

    saved = load_config(config_path)
    assert profile.web_port == 8788
    assert saved.web_port == 8788
    assert saved.web_bind_address == "127.0.0.1"
    assert checked == [("127.0.0.1", 8787), ("0.0.0.0", 8788)]


def test_ensure_internal_web_endpoint_refuses_running_profile(tmp_path):
    _write_profile(tmp_path, 1)
    lock_path = tmp_path / "state" / "instances" / "i1.lock"
    lock_path.parent.mkdir(parents=True)
    running_lock = FileLock(str(lock_path))
    running_lock.acquire(timeout=0)
    try:
        with pytest.raises(ProfileInUseError, match="不能调整内部 Web 端口"):
            _manager(tmp_path).ensure_internal_web_endpoint(1)
    finally:
        running_lock.release()


def test_ensure_internal_web_endpoint_wraps_persistence_errors(tmp_path, monkeypatch):
    _write_profile(tmp_path, 1)
    manager = _manager(tmp_path, port_probe=lambda _host, _port: True)
    monkeypatch.setattr(
        profile_manager_module,
        "save_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(ProfileManagerError, match="内部 Web 端点自动配置失败"):
        manager.ensure_internal_web_endpoint(1)


def test_update_profile_saves_launch_fields_before_start(tmp_path):
    config_path = _write_profile(tmp_path, 1)
    manager = _manager(tmp_path, port_probe=lambda _host, _port: True)

    profile = manager.update_profile(
        1,
        display_name="CT 夜班",
        pacs_server_ip="172.16.0.10",
        pacs_server_port=104,
        calling_ae_title="MACGET",
        pacs_ae_title="PACS02",
        storage_ae_title="MACGET",
        storage_port=6777,
        web_port=8899,
        dicom_destination_folder=str(tmp_path / "night-output"),
    )

    saved = load_config(config_path)
    assert profile.display_name == "CT 夜班"
    assert profile.pacs_server_ip == "172.16.0.10"
    assert profile.pacs_server_port == 104
    assert profile.calling_ae_title == "MACGET"
    assert profile.pacs_ae_title == "PACS02"
    assert profile.storage_ae_title == "MACGET"
    assert profile.storage_port == 6777
    assert profile.web_port == 8899
    assert profile.destination_directory == str(tmp_path / "night-output")
    assert saved.to_dict()["storage_port"] == 6777


def test_update_rejects_same_profile_scp_and_web_port_without_saving(tmp_path):
    config_path = _write_profile(tmp_path, 1, port=6666, web_port=8787)
    manager = _manager(tmp_path, port_probe=lambda _host, _port: True)

    with pytest.raises(ProfileManagerError, match="Web 端口不能与 DICOM 接收端口相同"):
        manager.update_profile(1, web_port=6666)

    saved = load_config(config_path)
    assert (saved.storage_port, saved.web_port) == (6666, 8787)


def test_update_rejects_cross_profile_port_conflict_without_saving(tmp_path):
    config_path = _write_profile(tmp_path, 1, port=6666, web_port=8787)
    _write_profile(tmp_path, 2, port=7777, web_port=8888)
    manager = _manager(tmp_path, port_probe=lambda _host, _port: True)

    with pytest.raises(
        ProfileManagerError,
        match=r"实例 1 的DICOM 接收端口 8888 与实例 2 的Web 端口冲突",
    ):
        manager.update_profile(1, storage_port=8888)

    assert load_config(config_path).storage_port == 6666


def test_update_rejects_port_occupied_by_another_program(tmp_path):
    config_path = _write_profile(tmp_path, 1, port=6666, web_port=8787)
    manager = _manager(
        tmp_path,
        port_probe=lambda _host, port: port != 8899,
    )

    with pytest.raises(
        ProfileManagerError,
        match=r"实例 1 的Web 端口 8899 已被其他程序占用",
    ):
        manager.update_profile(1, web_port=8899)

    assert load_config(config_path).web_port == 8787


@pytest.mark.parametrize("field", ["storage_port", "web_port"])
def test_update_rejects_windows_management_port_without_rewriting_config(
    tmp_path,
    field,
):
    config_path = _write_profile(tmp_path, 1, port=6666, web_port=8787)
    before = config_path.read_bytes()
    manager = _manager(tmp_path, port_probe=lambda _host, _port: True)

    with pytest.raises(ProfileManagerError, match="Windows 管理中心保留端口"):
        manager.update_profile(1, **{field: WINDOWS_MANAGEMENT_PORT})

    assert config_path.read_bytes() == before


def test_saved_management_port_is_reported_without_rewriting_config(tmp_path):
    config_path = _write_profile(
        tmp_path,
        1,
        port=6666,
        web_port=WINDOWS_MANAGEMENT_PORT,
    )
    before = config_path.read_bytes()

    with pytest.raises(ProfileManagerError, match="Windows 管理中心保留端口"):
        _manager(tmp_path).validate_profile_ports(1)

    assert config_path.read_bytes() == before


def test_startup_port_validation_rejects_invalid_saved_port(tmp_path):
    _write_profile(tmp_path, 1, port=0, web_port=8787)

    with pytest.raises(
        ProfileManagerError,
        match=r"实例 1 的DICOM 接收端口必须在 1 到 65535 之间",
    ):
        _manager(tmp_path).validate_profile_ports(1)


def test_update_refuses_a_running_profile(tmp_path):
    _write_profile(tmp_path, 1)
    lock_path = tmp_path / "state" / "instances" / "i1.lock"
    lock_path.parent.mkdir(parents=True)
    running_lock = FileLock(str(lock_path))
    running_lock.acquire(timeout=0)
    try:
        with pytest.raises(ProfileInUseError, match="先停止后再修改"):
            _manager(tmp_path).update_profile(1, web_port=8899)
    finally:
        running_lock.release()


def test_update_rolls_back_config_when_display_name_write_fails(
    tmp_path,
    monkeypatch,
):
    config_path = _write_profile(tmp_path, 1, port=6666, web_port=8787)
    manager = _manager(tmp_path, port_probe=lambda _host, _port: True)
    monkeypatch.setattr(
        manager,
        "_write_metadata",
        lambda _number, _name: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(ProfileManagerError, match="保存实例 1 配置失败"):
        manager.update_profile(
            1,
            display_name="新名称",
            storage_port=6777,
        )

    assert load_config(config_path).storage_port == 6666
    assert not config_path.with_name("profile-meta.json").exists()


def test_clone_never_reuses_its_new_receiver_port_for_web(tmp_path):
    _write_profile(tmp_path, 1, port=6666, web_port=6667)
    manager = _manager(tmp_path, port_probe=lambda _host, _port: True)

    result = manager.clone_profile(1)

    assert result.recommended_port == 6668
    assert result.recommended_web_port == 6669


def test_delete_refuses_running_profile(tmp_path):
    config_path = _write_profile(tmp_path, 1)
    lock_path = tmp_path / "state" / "instances" / "i1.lock"
    lock_path.parent.mkdir(parents=True)
    running_lock = FileLock(str(lock_path))
    running_lock.acquire(timeout=0)
    try:
        with pytest.raises(ProfileInUseError, match="正在运行"):
            _manager(tmp_path).delete_profile(1)
    finally:
        running_lock.release()

    assert config_path.is_file()


@pytest.mark.parametrize("suffix", ["", "-wal"])
def test_delete_refuses_profile_with_recovery_files(tmp_path, suffix):
    config_path = _write_profile(tmp_path, 1)
    recovery = (
        tmp_path
        / "state"
        / "instances"
        / "i1"
        / f"active-task.sqlite3{suffix}"
    )
    recovery.parent.mkdir(parents=True)
    recovery.write_bytes(b"checkpoint")

    with pytest.raises(ProfileRecoveryExistsError, match="恢复点"):
        _manager(tmp_path).delete_profile(1)

    assert config_path.is_file()


def test_delete_removes_only_profile_files_and_keeps_download_data(tmp_path):
    destination = tmp_path / "patient-downloads"
    destination.mkdir()
    dicom_file = destination / "image.dcm"
    dicom_file.write_bytes(b"DICM")
    config_path = _write_profile(tmp_path, 1, destination=destination)
    manager = _manager(tmp_path)
    manager.rename_profile(1, "待删除")
    metadata_path = config_path.with_name("profile-meta.json")

    manager.delete_profile(1)

    assert not config_path.exists()
    assert not metadata_path.exists()
    assert dicom_file.read_bytes() == b"DICM"
    with pytest.raises(ProfileNotFoundError):
        manager.get_profile(1)


@pytest.mark.parametrize(
    "display_name",
    ["", "   ", "bad\nname", "x" * 81],
)
def test_rename_rejects_invalid_display_names(tmp_path, display_name):
    _write_profile(tmp_path, 1)

    with pytest.raises(ProfileManagerError):
        _manager(tmp_path).rename_profile(1, display_name)

    assert not (
        tmp_path / "config" / "instances" / "i1" / "profile-meta.json"
    ).exists()


def test_clone_rolls_back_target_files_when_metadata_write_fails(
    tmp_path,
    monkeypatch,
):
    _write_profile(tmp_path, 1)
    manager = _manager(tmp_path, port_probe=lambda _host, _port: True)

    def fail_metadata(_number: int, _display_name: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_write_metadata", fail_metadata)

    with pytest.raises(ProfileManagerError, match="克隆 Profile 失败"):
        manager.clone_profile(1)

    assert not (
        tmp_path / "config" / "instances" / "i2" / "config.json"
    ).exists()


def test_list_rejects_profile_directory_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_profile(outside, 1)
    instances = tmp_path / "config" / "instances"
    instances.mkdir(parents=True)
    try:
        (instances / "i1").symlink_to(
            outside / "config" / "instances" / "i1",
            target_is_directory=True,
        )
    except OSError:
        pytest.skip("当前平台不允许创建目录符号链接")

    with pytest.raises(ProfileManagerError, match="不安全"):
        _manager(tmp_path).list_profiles()


def test_invalid_profile_number_and_starting_port_are_rejected(tmp_path):
    manager = _manager(tmp_path)

    with pytest.raises(ProfileManagerError, match="实例编号"):
        manager.get_profile("../1")
    with pytest.raises(ProfileManagerError, match="起始端口"):
        manager.recommend_available_port(80)


def test_default_port_probe_detects_an_occupied_local_port():
    with profile_manager_module.socket.socket(
        profile_manager_module.socket.AF_INET,
        profile_manager_module.socket.SOCK_STREAM,
    ) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])

        assert not profile_manager_module._port_is_available("127.0.0.1", port)
