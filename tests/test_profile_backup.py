from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from filelock import FileLock

from dcmget.config import AppConfig, load_config, save_config
from dcmget.profile_backup import (
    ProfileBackupError,
    create_profile_backup,
    discover_profile_configs,
    inspect_profile_backup,
    restore_profile_backup,
)


def _config(path: Path, *, ae: str, port: int) -> Path:
    save_config(
        path,
        AppConfig(
            storage_ae_title=ae,
            storage_port=port,
            dicom_destination_folder=str(path.parent / "dicom"),
        ),
    )
    return path


def _metadata(config_path: Path, display_name: str) -> Path:
    path = config_path.with_name("profile-meta.json")
    path.write_text(
        json.dumps(
            {
                "schema": "dcmget-profile-meta",
                "version": 1,
                "display_name": display_name,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _v1_package(output: Path, number: int, config_path: Path) -> None:
    data = config_path.read_bytes()
    manifest = {
        "schema": "dcmget-profile-backup",
        "schema_version": 1,
        "app_version": "2.10.0",
        "created_at": "2026-07-18T00:00:00+00:00",
        "contains_trial_state": False,
        "profiles": [
            {
                "profile_number": number,
                "path": f"profiles/i{number}/config.json",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
    }
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(f"profiles/i{number}/config.json", data)


def test_profile_backup_discovers_profiles_and_excludes_trial_license_and_state(
    tmp_path,
):
    config_root = tmp_path / "config"
    first = _config(
        config_root / "instances" / "i1" / "config.json",
        ae="FIRST",
        port=6666,
    )
    second = _config(
        config_root / "instances" / "i2" / "config.json",
        ae="SECOND",
        port=6667,
    )
    (config_root / "trial.json").write_text("trial-secret", encoding="utf-8")
    (config_root / "license.lic").write_text("license-secret", encoding="utf-8")
    (config_root / "instances" / "i1" / "active-task.sqlite3").write_bytes(
        b"task-state"
    )

    assert discover_profile_configs(config_root) == {1: first, 2: second}
    output = tmp_path / "profiles.zip"
    info = create_profile_backup(output, config_root=config_root)

    assert info.profile_numbers == (1, 2)
    assert inspect_profile_backup(output).profile_numbers == (1, 2)
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        combined = b"".join(archive.read(name) for name in names)
    assert names == {
        "manifest.json",
        "profiles/i1/config.json",
        "profiles/i2/config.json",
    }
    assert b"trial-secret" not in combined
    assert b"license-secret" not in combined
    assert b"task-state" not in combined


def test_restore_validates_then_backs_up_and_replaces_profiles(tmp_path):
    source_root = tmp_path / "source"
    new_first = _config(source_root / "i1.json", ae="NEWONE", port=7001)
    new_second = _config(source_root / "i2.json", ae="NEWTWO", port=7002)
    package = tmp_path / "import.zip"
    create_profile_backup(package, {1: new_first, 2: new_second})

    target_root = tmp_path / "target"
    old_first = _config(
        target_root / "instances" / "i1" / "config.json",
        ae="OLDONE",
        port=6001,
    )
    old_second = _config(
        target_root / "instances" / "i2" / "config.json",
        ae="OLDTWO",
        port=6002,
    )
    result = restore_profile_backup(
        package, config_root=target_root, state_root=tmp_path / "state"
    )

    assert result.profile_numbers == (1, 2)
    assert result.restored_paths == (old_first, old_second)
    assert result.previous_backup is not None
    assert result.previous_backup.is_file()
    assert load_config(old_first).storage_ae_title == "NEWONE"
    assert load_config(old_second).storage_port == 7002

    backup = inspect_profile_backup(result.previous_backup)
    assert backup.profile_numbers == (1, 2)
    restore_root = tmp_path / "restored-old"
    restore_profile_backup(
        result.previous_backup,
        config_root=restore_root,
        state_root=tmp_path / "restore-state",
    )
    assert (
        load_config(restore_root / "instances" / "i1" / "config.json").storage_ae_title
        == "OLDONE"
    )


def test_backup_restore_preserves_metadata_and_records_absence(tmp_path):
    source_root = tmp_path / "source"
    source_one = _config(
        source_root / "i1" / "config.json",
        ae="NEWONE",
        port=7001,
    )
    source_two = _config(
        source_root / "i2" / "config.json",
        ae="NEWTWO",
        port=7002,
    )
    _metadata(source_one, "CT 夜班")
    package = tmp_path / "profiles-v2.zip"

    create_profile_backup(package, {1: source_one, 2: source_two})

    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_version"] == 2
        assert manifest["profiles"][0]["metadata"]["path"] == (
            "profiles/i1/profile-meta.json"
        )
        assert "metadata" not in manifest["profiles"][1]
        assert "profiles/i1/profile-meta.json" in archive.namelist()

    target_root = tmp_path / "target"
    target_one = _config(
        target_root / "instances" / "i1" / "config.json",
        ae="OLDONE",
        port=6001,
    )
    target_two = _config(
        target_root / "instances" / "i2" / "config.json",
        ae="OLDTWO",
        port=6002,
    )
    _metadata(target_one, "原一")
    target_two_metadata = _metadata(target_two, "原二")

    result = restore_profile_backup(
        package, config_root=target_root, state_root=tmp_path / "state"
    )

    assert json.loads(
        target_one.with_name("profile-meta.json").read_text(encoding="utf-8")
    )["display_name"] == "CT 夜班"
    assert not target_two_metadata.exists()
    assert result.previous_backup is not None
    with zipfile.ZipFile(result.previous_backup) as archive:
        names = set(archive.namelist())
    assert "profiles/i1/profile-meta.json" in names
    assert "profiles/i2/profile-meta.json" in names

    restore_profile_backup(
        result.previous_backup,
        config_root=target_root,
        state_root=tmp_path / "state",
    )
    assert json.loads(
        target_one.with_name("profile-meta.json").read_text(encoding="utf-8")
    )["display_name"] == "原一"
    assert json.loads(
        target_two_metadata.read_text(encoding="utf-8")
    )["display_name"] == "原二"


def test_restore_v1_package_preserves_existing_metadata(tmp_path):
    source = _config(tmp_path / "source.json", ae="NEW", port=7001)
    package = tmp_path / "profiles-v1.zip"
    _v1_package(package, 1, source)
    assert inspect_profile_backup(package).profile_numbers == (1,)

    target_root = tmp_path / "target"
    target = _config(
        target_root / "instances" / "i1" / "config.json",
        ae="OLD",
        port=6001,
    )
    metadata_path = _metadata(target, "保留的显示名")
    old_metadata = metadata_path.read_bytes()

    result = restore_profile_backup(
        package, config_root=target_root, state_root=tmp_path / "state"
    )

    assert load_config(target).storage_ae_title == "NEW"
    assert metadata_path.read_bytes() == old_metadata
    assert result.previous_backup is not None
    with zipfile.ZipFile(result.previous_backup) as archive:
        assert "profiles/i1/profile-meta.json" in archive.namelist()


def test_restore_rejects_extra_trial_file_before_touching_config(tmp_path):
    source = _config(tmp_path / "source.json", ae="NEW", port=7001)
    valid = tmp_path / "valid.zip"
    create_profile_backup(valid, {1: source})
    malicious = tmp_path / "malicious.zip"
    with zipfile.ZipFile(valid) as original, zipfile.ZipFile(malicious, "w") as altered:
        for info in original.infolist():
            altered.writestr(info, original.read(info))
        altered.writestr("trial.json", "reset")

    target = _config(
        tmp_path / "target" / "instances" / "i1" / "config.json",
        ae="OLD",
        port=6001,
    )
    original_bytes = target.read_bytes()

    with pytest.raises(ProfileBackupError, match="多余文件"):
        restore_profile_backup(
            malicious,
            config_root=tmp_path / "target",
            state_root=tmp_path / "state",
        )

    assert target.read_bytes() == original_bytes
    assert not (tmp_path / "target" / "profile-backups").exists()


def test_restore_rejects_tampered_digest_and_future_config_version(tmp_path):
    source = _config(tmp_path / "source.json", ae="NEW", port=7001)
    valid = tmp_path / "valid.zip"
    create_profile_backup(valid, {1: source})

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(valid) as original, zipfile.ZipFile(tampered, "w") as altered:
        for info in original.infolist():
            data = original.read(info)
            if info.filename.endswith("config.json"):
                data += b" "
            altered.writestr(info, data)
    with pytest.raises(ProfileBackupError, match="摘要不匹配"):
        inspect_profile_backup(tampered)

    future_config = AppConfig().to_dict()
    future_config["config_version"] = 999
    data = (json.dumps(future_config) + "\n").encode()
    digest = hashlib.sha256(data).hexdigest()
    future = tmp_path / "future.zip"
    manifest = {
        "schema": "dcmget-profile-backup",
        "schema_version": 1,
        "app_version": "999.0.0",
        "created_at": "2099-01-01T00:00:00+00:00",
        "contains_trial_state": False,
        "profiles": [
            {
                "profile_number": 1,
                "path": "profiles/i1/config.json",
                "bytes": len(data),
                "sha256": digest,
            }
        ],
    }
    with zipfile.ZipFile(future, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("profiles/i1/config.json", data)
    with pytest.raises(ProfileBackupError, match="版本 999 不受支持"):
        inspect_profile_backup(future)


def test_backup_rejects_unknown_or_trial_config_fields(tmp_path):
    base = AppConfig().to_dict()
    for field in ("unexpected_setting", "trial"):
        raw = dict(base)
        raw[field] = "secret"
        path = tmp_path / f"{field}.json"
        path.write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(ProfileBackupError, match="未知字段|禁止字段"):
            create_profile_backup(tmp_path / f"{field}.zip", {1: path})


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema": "wrong",
            "version": 1,
            "display_name": "CT",
        },
        {
            "schema": "dcmget-profile-meta",
            "version": 2,
            "display_name": "CT",
        },
        {
            "schema": "dcmget-profile-meta",
            "version": 1,
            "display_name": "",
        },
        {
            "schema": "dcmget-profile-meta",
            "version": 1,
            "display_name": "bad\nname",
        },
        {
            "schema": "dcmget-profile-meta",
            "version": 1,
            "display_name": "CT",
            "trial": "secret",
        },
    ],
)
def test_backup_rejects_invalid_or_extended_profile_metadata(tmp_path, payload):
    source = _config(tmp_path / "profile" / "config.json", ae="NEW", port=7001)
    source.with_name("profile-meta.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(ProfileBackupError, match="元数据|显示名"):
        create_profile_backup(tmp_path / "invalid-metadata.zip", {1: source})


def test_restore_rejects_tampered_metadata_before_touching_target(tmp_path):
    source = _config(tmp_path / "source" / "config.json", ae="NEW", port=7001)
    _metadata(source, "新名称")
    valid = tmp_path / "valid.zip"
    create_profile_backup(valid, {1: source})
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(valid) as original, zipfile.ZipFile(tampered, "w") as altered:
        for info in original.infolist():
            data = original.read(info)
            if info.filename.endswith("profile-meta.json"):
                data += b" "
            altered.writestr(info, data)

    target_root = tmp_path / "target"
    target = _config(
        target_root / "instances" / "i1" / "config.json",
        ae="OLD",
        port=6001,
    )
    target_metadata = _metadata(target, "原名称")
    old_config = target.read_bytes()
    old_metadata = target_metadata.read_bytes()

    with pytest.raises(ProfileBackupError, match="元数据摘要不匹配"):
        restore_profile_backup(
            tampered, config_root=target_root, state_root=tmp_path / "state"
        )

    assert target.read_bytes() == old_config
    assert target_metadata.read_bytes() == old_metadata


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("path", "profiles/i2/profile-meta.json", "元数据路径无效"),
        ("bytes", True, "元数据大小无效"),
    ],
)
def test_backup_manifest_strictly_validates_metadata_descriptor(
    tmp_path, field, value, message
):
    source = _config(tmp_path / "source" / "config.json", ae="NEW", port=7001)
    _metadata(source, "CT")
    valid = tmp_path / "valid.zip"
    create_profile_backup(valid, {1: source})
    invalid = tmp_path / f"invalid-{field}.zip"

    with zipfile.ZipFile(valid) as original, zipfile.ZipFile(invalid, "w") as altered:
        for info in original.infolist():
            data = original.read(info)
            if info.filename == "manifest.json":
                manifest = json.loads(data)
                manifest["profiles"][0]["metadata"][field] = value
                data = json.dumps(manifest).encode()
            altered.writestr(info, data)

    with pytest.raises(ProfileBackupError, match=message):
        inspect_profile_backup(invalid)


def test_restore_rolls_back_replaced_profiles_if_later_write_fails(
    tmp_path, monkeypatch
):
    source_one = _config(tmp_path / "new-one.json", ae="NEWONE", port=7001)
    source_two = _config(tmp_path / "new-two.json", ae="NEWTWO", port=7002)
    package = tmp_path / "import.zip"
    create_profile_backup(package, {1: source_one, 2: source_two})
    target_root = tmp_path / "target"
    target_one = _config(
        target_root / "instances" / "i1" / "config.json",
        ae="OLDONE",
        port=6001,
    )
    target_two = _config(
        target_root / "instances" / "i2" / "config.json",
        ae="OLDTWO",
        port=6002,
    )
    old_one = target_one.read_bytes()
    old_two = target_two.read_bytes()

    import dcmget.profile_backup as profile_backup

    real_atomic_write = profile_backup._atomic_write
    calls = 0

    def fail_second(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        real_atomic_write(path, data)

    monkeypatch.setattr(profile_backup, "_atomic_write", fail_second)

    with pytest.raises(ProfileBackupError, match="simulated write failure"):
        restore_profile_backup(
            package, config_root=target_root, state_root=tmp_path / "state"
        )

    assert target_one.read_bytes() == old_one
    assert target_two.read_bytes() == old_two


def test_restore_rolls_back_config_and_metadata_when_metadata_write_fails(
    tmp_path, monkeypatch
):
    source = _config(tmp_path / "new" / "config.json", ae="NEW", port=7001)
    _metadata(source, "新名称")
    package = tmp_path / "import.zip"
    create_profile_backup(package, {1: source})

    target_root = tmp_path / "target"
    target = _config(
        target_root / "instances" / "i1" / "config.json",
        ae="OLD",
        port=6001,
    )
    target_metadata = _metadata(target, "原名称")
    old_config = target.read_bytes()
    old_metadata = target_metadata.read_bytes()

    import dcmget.profile_backup as profile_backup

    real_atomic_write = profile_backup._atomic_write
    calls = 0

    def fail_metadata(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("metadata disk failure")
        real_atomic_write(path, data)

    monkeypatch.setattr(profile_backup, "_atomic_write", fail_metadata)

    with pytest.raises(ProfileBackupError, match="metadata disk failure"):
        restore_profile_backup(
            package, config_root=target_root, state_root=tmp_path / "state"
        )

    assert target.read_bytes() == old_config
    assert target_metadata.read_bytes() == old_metadata


def test_restore_refuses_profile_held_by_running_instance(tmp_path):
    source = _config(tmp_path / "source.json", ae="NEW", port=7001)
    package = tmp_path / "profiles.zip"
    create_profile_backup(package, {1: source})
    target_root = tmp_path / "target"
    target = _config(
        target_root / "instances" / "i1" / "config.json",
        ae="OLD",
        port=6001,
    )
    old_bytes = target.read_bytes()
    state_root = tmp_path / "state"
    lock_path = state_root / "instances" / "i1.lock"
    lock_path.parent.mkdir(parents=True)
    running_lock = FileLock(str(lock_path))
    running_lock.acquire(timeout=0)
    try:
        with pytest.raises(ProfileBackupError, match="正在运行"):
            restore_profile_backup(
                package,
                config_root=target_root,
                state_root=state_root,
            )
    finally:
        running_lock.release()

    assert target.read_bytes() == old_bytes
    assert not (target_root / "profile-backups").exists()


def test_restore_allows_explicitly_owned_profile_and_reloads_disk_config(tmp_path):
    source = _config(tmp_path / "source.json", ae="NEW", port=7001)
    package = tmp_path / "profiles.zip"
    create_profile_backup(package, {1: source})
    target_root = tmp_path / "target"
    target = _config(
        target_root / "instances" / "i1" / "config.json",
        ae="OLD",
        port=6001,
    )
    state_root = tmp_path / "state"
    lock_path = state_root / "instances" / "i1.lock"
    lock_path.parent.mkdir(parents=True)
    owned_lock = FileLock(str(lock_path))
    owned_lock.acquire(timeout=0)
    try:
        result = restore_profile_backup(
            package,
            config_root=target_root,
            state_root=state_root,
            owned_profile_lock=owned_lock,
        )
    finally:
        owned_lock.release()

    assert result.profile_numbers == (1,)
    assert load_config(target).storage_ae_title == "NEW"


def test_restore_does_not_trust_an_unrelated_owned_profile_lock(tmp_path):
    source = _config(tmp_path / "source.json", ae="NEW", port=7001)
    package = tmp_path / "profiles.zip"
    create_profile_backup(package, {1: source})
    target_root = tmp_path / "target"
    target = _config(
        target_root / "instances" / "i1" / "config.json",
        ae="OLD",
        port=6001,
    )
    old_bytes = target.read_bytes()
    state_root = tmp_path / "state"
    locks = state_root / "instances"
    locks.mkdir(parents=True)
    running_lock = FileLock(str(locks / "i1.lock"))
    unrelated_lock = FileLock(str(locks / "i2.lock"))
    running_lock.acquire(timeout=0)
    unrelated_lock.acquire(timeout=0)
    try:
        with pytest.raises(ProfileBackupError, match="正在运行"):
            restore_profile_backup(
                package,
                config_root=target_root,
                state_root=state_root,
                owned_profile_lock=unrelated_lock,
            )
    finally:
        unrelated_lock.release()
        running_lock.release()

    assert target.read_bytes() == old_bytes


def test_restore_rejects_a_forged_owned_profile_lock(tmp_path):
    from types import SimpleNamespace

    source = _config(tmp_path / "source.json", ae="NEW", port=7001)
    package = tmp_path / "profiles.zip"
    create_profile_backup(package, {1: source})
    target_root = tmp_path / "target"
    target = _config(
        target_root / "instances" / "i1" / "config.json",
        ae="OLD",
        port=6001,
    )
    old_bytes = target.read_bytes()
    fake_lock = SimpleNamespace(
        is_locked=True,
        lock_file=str(tmp_path / "state" / "instances" / "i1.lock"),
    )

    with pytest.raises(ProfileBackupError, match="运行锁无效"):
        restore_profile_backup(
            package,
            config_root=target_root,
            state_root=tmp_path / "state",
            owned_profile_lock=fake_lock,
        )

    assert target.read_bytes() == old_bytes


@pytest.mark.parametrize("lock_kind", ["allocation", "profile", "config"])
def test_restore_rejects_symlinked_lock_paths(tmp_path, lock_kind):
    source = _config(tmp_path / "source.json", ae="NEW", port=7001)
    package = tmp_path / "profiles.zip"
    create_profile_backup(package, {1: source})
    target_root = tmp_path / "target"
    target = _config(
        target_root / "instances" / "i1" / "config.json",
        ae="OLD",
        port=6001,
    )
    old_bytes = target.read_bytes()
    state_root = tmp_path / "state"
    state_locks = state_root / "instances"
    state_locks.mkdir(parents=True)
    lock_paths = {
        "allocation": state_locks / ".allocate.lock",
        "profile": state_locks / "i1.lock",
        "config": target.with_name("config.json.lock"),
    }
    victim = tmp_path / f"{lock_kind}-victim.txt"
    victim.write_text("must remain intact", encoding="utf-8")
    lock_paths[lock_kind].unlink(missing_ok=True)
    try:
        lock_paths[lock_kind].symlink_to(victim)
    except OSError:
        pytest.skip("当前平台不允许创建测试符号链接")

    with pytest.raises(ProfileBackupError, match="锁路径不安全"):
        restore_profile_backup(
            package,
            config_root=target_root,
            state_root=state_root,
        )

    assert victim.read_text(encoding="utf-8") == "must remain intact"
    assert target.read_bytes() == old_bytes


def test_restore_rejects_symlinked_profile_directory(tmp_path):
    source = _config(tmp_path / "source.json", ae="NEW", port=7001)
    package = tmp_path / "profiles.zip"
    create_profile_backup(package, {1: source})
    target_root = tmp_path / "target"
    instances = target_root / "instances"
    instances.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_config = _config(outside / "config.json", ae="OUTSIDE", port=6001)
    (instances / "i1").symlink_to(outside, target_is_directory=True)
    old_bytes = outside_config.read_bytes()

    with pytest.raises(ProfileBackupError, match="配置目录不安全"):
        restore_profile_backup(
            package,
            config_root=target_root,
            state_root=tmp_path / "state",
        )

    assert outside_config.read_bytes() == old_bytes
