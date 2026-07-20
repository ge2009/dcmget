from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dcmget.instance_profile as instance_profile_module
from dcmget.config import AppConfig, load_config, save_config
from dcmget.core import AccessionResult, AccessionStatus
from dcmget.instance_profile import (
    InstanceProfileError,
    ProfileInUseError,
    acquire_instance_profile,
    instance_activation_path,
    migrate_legacy_checkpoint_to_profile,
    migrate_task_catalog_to_profiles,
)
from dcmget.profile_manager import WINDOWS_MANAGEMENT_PORT
from dcmget.task_manager import TaskCatalog
from dcmget.task_state import TaskCheckpoint, TaskCheckpointStore, TaskStateError


def _claim_profile_in_spawned_process(
    kwargs,
    begin_event,
    release_event,
    output_queue,
) -> None:
    profile = None
    try:
        if not begin_event.wait(10):
            raise TimeoutError("等待并发分配开始超时")
        profile = acquire_instance_profile(**kwargs)
        output_queue.put(("ok", profile.number))
        if not release_event.wait(20):
            raise TimeoutError("等待释放实例槽位超时")
    except Exception as exc:
        output_queue.put(("error", repr(exc)))
    finally:
        if profile is not None:
            profile.close()


def _profile_kwargs(tmp_path: Path) -> dict[str, Path]:
    template = tmp_path / "template" / "config.json"
    save_config(
        template,
        AppConfig(
            storage_ae_title="TEMPLATE",
            storage_port=6666,
            dicom_destination_folder=str(tmp_path / "template-dicom"),
        ),
    )
    return {
        "state_root": tmp_path / "state",
        "config_root": tmp_path / "config",
        "template_config_path": template,
    }


def _checkpoint(
    task_id: str,
    *,
    phase: str = "downloading",
    pdi_attempt_id: str = "",
) -> TaskCheckpoint:
    return TaskCheckpoint(
        task_id=task_id,
        config=AppConfig(
            storage_ae_title="IMPORTED",
            storage_port=6677,
            dicom_destination_folder="/dicom/imported",
        ),
        accessions=["DONE", "PARTIAL", "PENDING"],
        results=[
            AccessionResult(
                "DONE",
                AccessionStatus.COMPLETED,
                file_count=1,
                received_bytes=128,
                archived_files=["/dicom/imported/done.dcm"],
            )
        ],
        partial_results={
            "PARTIAL": AccessionResult(
                "PARTIAL",
                AccessionStatus.CANCELLED,
                file_count=1,
                received_bytes=64,
                archived_files=["/dicom/imported/partial.dcm"],
            )
        },
        trial_required=True,
        created_at=datetime.now(timezone.utc).isoformat(),
        phase=phase,
        pdi_attempt_id=pdi_attempt_id,
    )


def test_profile_uses_persisted_display_name_without_affecting_claim(tmp_path):
    kwargs = _profile_kwargs(tmp_path)
    metadata = (
        kwargs["config_root"]
        / "instances"
        / "i1"
        / "profile-meta.json"
    )
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            {
                "schema": "dcmget-profile-meta",
                "version": 1,
                "display_name": "CT 夜班下载",
            }
        ),
        encoding="utf-8",
    )

    profile = acquire_instance_profile(1, **kwargs)
    try:
        assert profile.label == "CT 夜班下载"
    finally:
        profile.close()


def test_import_checkpoint_preserves_identity_progress_and_pdi_state(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    source = _checkpoint(
        "a" * 32,
        phase="pdi_running",
        pdi_attempt_id="b" * 32,
    )

    imported = store.import_checkpoint(source)
    repeated = store.import_checkpoint(source)

    assert imported.task_id == source.task_id
    assert repeated.task_id == source.task_id
    assert imported.config.to_dict() == source.config.to_dict()
    assert imported.accessions == source.accessions
    assert imported.results == source.results
    assert imported.partial_results == source.partial_results
    assert imported.trial_required
    assert imported.created_at == source.created_at
    assert imported.phase == "pdi_running"
    assert imported.pdi_attempt_id == "b" * 32
    assert imported.pending_accessions == ["PARTIAL", "PENDING"]
    assert not store.lease_held
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o777 == 0o600


def test_import_checkpoint_never_overwrites_a_different_task(tmp_path):
    store = TaskCheckpointStore(tmp_path / "active-task.sqlite3")
    original = store.start(AppConfig(), ["ORIGINAL"], trial_required=False)

    with pytest.raises(TaskStateError, match="另一个未完成任务"):
        store.import_checkpoint(_checkpoint("c" * 32))

    restored = store.load_required()
    assert restored.task_id == original.task_id
    assert restored.accessions == ["ORIGINAL"]


def test_profile_slots_keep_config_state_logs_and_settings_isolated(tmp_path):
    kwargs = _profile_kwargs(tmp_path)
    first = acquire_instance_profile(**kwargs)
    try:
        assert first.number == 1
        assert first.slot_name == "i1"
        assert first.label == "实例 1"
        assert first.settings_name == "DcmGet2-i1"
        assert first.state_directory == kwargs["state_root"] / "instances" / "i1"
        assert first.task_state_path == first.state_directory / "active-task.sqlite3"
        assert first.log_directory == first.state_directory / "logs"
        assert first.activation_path == first.state_directory / "gui-instance.json"
        assert first.log_directory.is_dir()
        assert first.config_path == kwargs["config_root"] / "instances" / "i1" / "config.json"
        assert load_config(first.config_path).storage_ae_title == "TEMPLATE"

        first_config = load_config(first.config_path)
        first_config.storage_ae_title = "FIRST"
        first_config.storage_port = 7001
        first_config.web_port = 9000
        save_config(first.config_path, first_config)

        second = acquire_instance_profile(**kwargs)
        try:
            assert second.number == 2
            assert second.settings_name == "DcmGet2-i2"
            assert second.config_path != first.config_path
            assert second.task_state_path != first.task_state_path
            assert second.log_directory != first.log_directory
            copied = load_config(second.config_path)
            assert copied.storage_ae_title == "FIRST"
            assert copied.storage_port == 7002
            assert copied.web_port == 9001
            with pytest.raises(InstanceProfileError, match="已在运行"):
                acquire_instance_profile(2, **kwargs)

            third = acquire_instance_profile(**kwargs)
            try:
                assert third.number == 3
                assert load_config(third.config_path).storage_port == 7003
                assert load_config(third.config_path).web_port == 9002
            finally:
                third.close()
        finally:
            second.close()
    finally:
        first.close()

    explicit = acquire_instance_profile("2", **kwargs)
    assert explicit.number == 2
    explicit.close()


def test_new_profile_moves_template_off_windows_management_port_without_rewriting_template(
    tmp_path,
):
    kwargs = _profile_kwargs(tmp_path)
    template = kwargs["template_config_path"]
    save_config(
        template,
        AppConfig(storage_port=6666, web_port=WINDOWS_MANAGEMENT_PORT),
    )
    original = template.read_bytes()

    profile = acquire_instance_profile(**kwargs)
    try:
        assert load_config(profile.config_path).web_port == 8787
    finally:
        profile.close()

    assert template.read_bytes() == original


def test_existing_profile_on_management_port_is_never_rewritten_during_claim(
    tmp_path,
):
    kwargs = _profile_kwargs(tmp_path)
    config_path = kwargs["config_root"] / "instances" / "i1" / "config.json"
    save_config(
        config_path,
        AppConfig(storage_port=6666, web_port=WINDOWS_MANAGEMENT_PORT),
    )
    original = config_path.read_bytes()

    profile = acquire_instance_profile(1, **kwargs)
    profile.close()

    assert config_path.read_bytes() == original


def test_instance_activation_path_is_stable_without_claiming_profile(tmp_path):
    path = instance_activation_path(7, state_root=tmp_path / "state")

    assert path == (tmp_path / "state" / "instances" / "i7" / "gui-instance.json").resolve()
    assert not path.exists()


def test_custom_config_is_only_a_template_for_the_canonical_profile_root(
    tmp_path, monkeypatch
):
    canonical_template = tmp_path / "canonical" / "config.json"
    custom_template = tmp_path / "custom" / "site.json"
    save_config(custom_template, AppConfig(storage_ae_title="CUSTOM"))
    monkeypatch.setattr(
        instance_profile_module,
        "default_config_path",
        lambda: canonical_template,
    )

    profile = acquire_instance_profile(
        2,
        state_root=tmp_path / "state",
        template_config_path=custom_template,
    )
    try:
        assert profile.config_path == (
            canonical_template.parent / "instances" / "i2" / "config.json"
        ).resolve()
        assert load_config(profile.config_path).storage_ae_title == "CUSTOM"
    finally:
        profile.close()


def test_old_custom_profile_config_is_migrated_to_canonical_root(
    tmp_path, monkeypatch
):
    canonical_template = tmp_path / "canonical" / "config.json"
    custom_template = tmp_path / "custom" / "site.json"
    save_config(custom_template, AppConfig(storage_ae_title="TEMPLATE"))
    legacy_profile = custom_template.parent / "instances" / "i3" / "config.json"
    save_config(
        legacy_profile,
        AppConfig(storage_ae_title="LEGACY", storage_port=7003),
    )
    monkeypatch.setattr(
        instance_profile_module,
        "default_config_path",
        lambda: canonical_template,
    )

    profile = acquire_instance_profile(
        3,
        state_root=tmp_path / "state",
        template_config_path=custom_template,
    )
    try:
        migrated = load_config(profile.config_path)
        assert migrated.storage_ae_title == "LEGACY"
        assert migrated.storage_port == 7003
        assert legacy_profile.is_file()
    finally:
        profile.close()


def test_explicit_busy_profile_uses_specific_error_type(tmp_path):
    kwargs = _profile_kwargs(tmp_path)
    profile = acquire_instance_profile(4, **kwargs)
    try:
        with pytest.raises(ProfileInUseError, match="实例 4 已在运行"):
            acquire_instance_profile(4, **kwargs)
    finally:
        profile.close()


def test_profile_close_supports_weak_method_on_python_310(tmp_path):
    profile = acquire_instance_profile(**_profile_kwargs(tmp_path))
    try:
        close = weakref.WeakMethod(profile.close)
        callback = close()
        assert callback is not None
        assert profile.lock_held
        callback()
        assert not profile.lock_held
    finally:
        profile.close()


def test_auto_allocation_prefers_an_idle_profile_with_recovery_state(tmp_path):
    kwargs = _profile_kwargs(tmp_path)
    first = acquire_instance_profile(1, **kwargs)
    first.close()
    second = acquire_instance_profile(2, **kwargs)
    TaskCheckpointStore(second.task_state_path).start(
        AppConfig(), ["RECOVER-ME"], trial_required=False
    )
    second.close()

    selected = acquire_instance_profile(**kwargs)
    try:
        assert selected.number == 2
        assert TaskCheckpointStore(selected.task_state_path).load_required().accessions == [
            "RECOVER-ME"
        ]
    finally:
        selected.close()


def test_auto_allocation_uses_the_lowest_missing_slot_number(tmp_path):
    kwargs = _profile_kwargs(tmp_path)
    third = acquire_instance_profile(3, **kwargs)
    third.close()
    first = acquire_instance_profile(1, **kwargs)
    try:
        selected = acquire_instance_profile(**kwargs)
        try:
            assert selected.number == 2
        finally:
            selected.close()
    finally:
        first.close()


def test_concurrent_auto_allocation_never_returns_the_same_slot(tmp_path):
    kwargs = _profile_kwargs(tmp_path)
    release = threading.Event()
    all_acquired = threading.Event()
    numbers: list[int] = []
    failures: list[BaseException] = []
    guard = threading.Lock()

    def claim() -> None:
        profile = None
        try:
            profile = acquire_instance_profile(**kwargs)
            with guard:
                numbers.append(profile.number)
                if len(numbers) == 4:
                    all_acquired.set()
            assert release.wait(5)
        except BaseException as exc:  # keep worker failures visible to the test
            with guard:
                failures.append(exc)
            all_acquired.set()
        finally:
            if profile is not None:
                profile.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(claim) for _ in range(4)]
        try:
            assert all_acquired.wait(10)
            assert failures == []
            assert sorted(numbers) == [1, 2, 3, 4]
        finally:
            release.set()
        for future in futures:
            future.result(timeout=5)


def test_spawned_processes_never_receive_the_same_slot(tmp_path):
    kwargs = _profile_kwargs(tmp_path)
    context = multiprocessing.get_context("spawn")
    begin_event = context.Event()
    release_event = context.Event()
    output_queue = context.Queue()
    processes = [
        context.Process(
            target=_claim_profile_in_spawned_process,
            args=(kwargs, begin_event, release_event, output_queue),
        )
        for _ in range(4)
    ]
    outcomes = []
    try:
        for process in processes:
            process.start()
        begin_event.set()
        outcomes = [output_queue.get(timeout=20) for _ in processes]
        assert [status for status, _value in outcomes] == ["ok"] * 4
        assert sorted(value for _status, value in outcomes) == [1, 2, 3, 4]
    finally:
        release_event.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        output_queue.close()
        output_queue.join_thread()

    assert all(process.exitcode == 0 for process in processes)


def test_multitask_catalog_migration_is_complete_read_only_and_idempotent(
    tmp_path,
    monkeypatch,
):
    kwargs = _profile_kwargs(tmp_path)
    catalog_path = tmp_path / "legacy" / "tasks.sqlite3"
    catalog = TaskCatalog(
        catalog_path,
        legacy_path=tmp_path / "legacy" / "missing-active-task.sqlite3",
        auto_migrate=False,
    )
    config = AppConfig(
        storage_ae_title="LEGACY",
        storage_port=6666,
        dicom_destination_folder=str(tmp_path / "legacy-dicom"),
    )

    queued = catalog.create_task(config, ["Q-DONE", "Q-PENDING"])
    catalog.record_result(
        queued.task_id,
        AccessionResult(
            "Q-DONE",
            AccessionStatus.COMPLETED,
            archived_files=[str(tmp_path / "legacy-dicom" / "q.dcm")],
        ),
    )

    paused = catalog.create_task(config, ["PAUSED", "PAUSED-PARTIAL"])
    catalog.record_result(
        paused.task_id,
        AccessionResult(
            "PAUSED-PARTIAL",
            AccessionStatus.CANCELLED,
            archived_files=[str(tmp_path / "legacy-dicom" / "partial.dcm")],
        ),
    )
    catalog.set_phase(paused.task_id, "paused")

    failed = catalog.create_task(config, ["FAILED"])
    catalog.record_result(
        failed.task_id,
        AccessionResult("FAILED", AccessionStatus.FAILED, message="timeout"),
    )
    catalog.set_phase(failed.task_id, "failed")

    retryable = catalog.create_task(config, ["RETRY"])
    catalog.record_result(
        retryable.task_id,
        AccessionResult("RETRY", AccessionStatus.FAILED, message="network"),
    )
    catalog.set_phase(retryable.task_id, "download_retryable")

    pdi = catalog.create_task(config, ["PDI"])
    catalog.record_result(
        pdi.task_id,
        AccessionResult(
            "PDI",
            AccessionStatus.COMPLETED,
            archived_files=[str(tmp_path / "legacy-dicom" / "pdi.dcm")],
        ),
    )
    pdi_attempt, _reused = catalog.begin_pdi_attempt(
        pdi.task_id, reuse_existing=False
    )

    completed = catalog.create_task(config, ["COMPLETED"])
    catalog.set_phase(completed.task_id, "completed")
    cancelled = catalog.create_task(config, ["CANCELLED"])
    catalog.set_phase(cancelled.task_id, "cancelled")

    original_phases = {
        summary.task_id: summary.phase for summary in catalog.list_tasks()
    }
    original_catalog = catalog_path.read_bytes()
    result = migrate_task_catalog_to_profiles(catalog_path, **kwargs)

    expected_ids = {
        queued.task_id,
        paused.task_id,
        failed.task_id,
        retryable.task_id,
        pdi.task_id,
    }
    assert {item.task_id for item in result.migrated} == expected_ids
    assert len({item.profile_number for item in result.migrated}) == 5
    assert set(result.skipped_task_ids) == {completed.task_id, cancelled.task_id}
    assert result.marker_path.is_file()
    assert catalog_path.is_file()
    assert catalog_path.read_bytes() == original_catalog
    assert {
        summary.task_id: summary.phase for summary in catalog.list_tasks()
    } == original_phases

    imported = {
        item.task_id: TaskCheckpointStore(
            kwargs["state_root"]
            / "instances"
            / f"i{item.profile_number}"
            / "active-task.sqlite3"
        ).load_required()
        for item in result.migrated
    }
    assert imported[queued.task_id].phase == "downloading"
    assert imported[queued.task_id].pending_accessions == ["Q-PENDING"]
    assert imported[paused.task_id].phase == "downloading"
    assert imported[paused.task_id].partial_results[
        "PAUSED-PARTIAL"
    ].archived_files
    assert imported[failed.task_id].phase == "download_retryable"
    assert imported[retryable.task_id].phase == "download_retryable"
    assert imported[pdi.task_id].phase == "pdi_running"
    assert imported[pdi.task_id].pdi_attempt_id == pdi_attempt
    assert imported[pdi.task_id].task_id == pdi.task_id

    def reject_catalog_reload(_path):
        raise AssertionError("completed catalog migration must use its marker")

    monkeypatch.setattr(
        instance_profile_module,
        "_read_migratable_catalog",
        reject_catalog_reload,
    )
    repeated = migrate_task_catalog_to_profiles(catalog_path, **kwargs)
    assert repeated.migrated == ()
    assert set(repeated.already_migrated_task_ids) == expected_ids

    removed = result.migrated[0]
    removed_path = (
        kwargs["state_root"]
        / "instances"
        / f"i{removed.profile_number}"
        / "active-task.sqlite3"
    )
    removed_path.unlink()
    after_clear = migrate_task_catalog_to_profiles(catalog_path, **kwargs)
    assert after_clear.migrated == ()
    assert removed.task_id in after_clear.already_migrated_task_ids
    assert not removed_path.exists()
    marker = json.loads(result.marker_path.read_text(encoding="utf-8"))
    assert set(marker["task_ids"]) == expected_ids


def test_multitask_catalog_migration_does_not_resume_cancelling_task(tmp_path):
    kwargs = _profile_kwargs(tmp_path)
    catalog_path = tmp_path / "legacy" / "tasks.sqlite3"
    catalog = TaskCatalog(
        catalog_path,
        legacy_path=tmp_path / "legacy" / "missing-active-task.sqlite3",
        auto_migrate=False,
    )
    cancelling = catalog.create_task(AppConfig(), ["CANCELLING"])
    catalog.set_phase(cancelling.task_id, "cancelling")

    result = migrate_task_catalog_to_profiles(catalog_path, **kwargs)

    assert result.migrated == ()
    assert result.skipped_task_ids == (cancelling.task_id,)
    assert not (kwargs["state_root"] / "instances" / "i1").exists()


def test_catalog_migration_cleans_task_and_receiver_processes_read_only(
    tmp_path,
    monkeypatch,
):
    kwargs = _profile_kwargs(tmp_path)
    catalog_path = tmp_path / "legacy" / "tasks.sqlite3"
    catalog = TaskCatalog(
        catalog_path,
        legacy_path=tmp_path / "legacy" / "missing.sqlite3",
        auto_migrate=False,
    )
    task = catalog.create_task(AppConfig(), ["A001"])
    with sqlite3.connect(catalog_path) as connection:
        connection.execute(
            """
            INSERT INTO task_processes(
                task_id, kind, pid, process_created_at, executable,
                command_line_json, process_group_id
            ) VALUES (?, 'movescu', 101, 10.5, '/tools/movescu', ?, 0)
            """,
            (task.task_id, json.dumps(["/tools/movescu", "PACS", "104"])),
        )
        connection.execute(
            """
            INSERT INTO receiver_sessions(
                session_id, pid, process_created_at, executable,
                command_line_json, process_group_id, started_at
            ) VALUES ('receiver-one', 102, 11.5, '/tools/storescp', ?, 0, ?)
            """,
            (
                json.dumps(["/tools/storescp", "6666"]),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    original_catalog = catalog_path.read_bytes()
    cleaned: list[tuple[str, dict[str, object]]] = []

    def clean(identity, label):
        cleaned.append((label, identity))
        return True, f"cleaned {label}"

    monkeypatch.setattr(instance_profile_module, "_cleanup_process_identity", clean)

    result = migrate_task_catalog_to_profiles(catalog_path, **kwargs)

    assert result.migrated[0].task_id == task.task_id
    assert [label for label, _identity in cleaned] == [
        f"movescu（任务 {task.task_id[:8]}）",
        "storescp（接收会话 receiver）",
    ]
    assert [identity["pid"] for _label, identity in cleaned] == [101, 102]
    assert result.marker_path.is_file()
    assert catalog_path.read_bytes() == original_catalog


def test_catalog_migration_does_not_mark_unresolved_legacy_process(
    tmp_path,
    monkeypatch,
):
    kwargs = _profile_kwargs(tmp_path)
    catalog_path = tmp_path / "legacy" / "tasks.sqlite3"
    catalog = TaskCatalog(
        catalog_path,
        legacy_path=tmp_path / "legacy" / "missing.sqlite3",
        auto_migrate=False,
    )
    task = catalog.create_task(AppConfig(), ["A001"])
    with sqlite3.connect(catalog_path) as connection:
        connection.execute(
            """
            INSERT INTO task_processes(
                task_id, kind, pid, process_created_at, executable,
                command_line_json, process_group_id
            ) VALUES (?, 'pdi', 103, 12.5, '/tools/pdi', '[]', 0)
            """,
            (task.task_id,),
        )
    original_catalog = catalog_path.read_bytes()
    monkeypatch.setattr(
        instance_profile_module,
        "_cleanup_process_identity",
        lambda _identity, _label: (False, "测试遗留进程无法终止"),
    )

    with pytest.raises(InstanceProfileError, match="无法终止"):
        migrate_task_catalog_to_profiles(catalog_path, **kwargs)

    marker = kwargs["state_root"] / "instances" / ".tasks-migrated-v1.json"
    assert not marker.exists()
    assert catalog_path.read_bytes() == original_catalog


def test_catalog_migration_refuses_to_race_an_active_legacy_scheduler(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        instance_profile_module,
        "MIGRATION_LOCK_TIMEOUT_SECONDS",
        0.05,
    )
    kwargs = _profile_kwargs(tmp_path)
    catalog_path = tmp_path / "legacy" / "tasks.sqlite3"
    catalog = TaskCatalog(
        catalog_path,
        legacy_path=tmp_path / "legacy" / "missing.sqlite3",
        auto_migrate=False,
    )
    catalog.create_task(AppConfig(), ["A001"])
    assert catalog.try_acquire_foreground_lease()
    try:
        with pytest.raises(InstanceProfileError, match="仍被另一个"):
            migrate_task_catalog_to_profiles(catalog_path, **kwargs)
    finally:
        catalog.release_foreground_lease()


def test_concurrent_catalog_migrations_wait_and_import_each_task_once(tmp_path):
    kwargs = _profile_kwargs(tmp_path)
    catalog_path = tmp_path / "legacy" / "tasks.sqlite3"
    catalog = TaskCatalog(
        catalog_path,
        legacy_path=tmp_path / "legacy" / "missing.sqlite3",
        auto_migrate=False,
    )
    task = catalog.create_task(AppConfig(), ["A001"])
    barrier = threading.Barrier(2)

    def migrate():
        barrier.wait(timeout=5)
        return migrate_task_catalog_to_profiles(catalog_path, **kwargs)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(migrate), pool.submit(migrate)]
        results = [future.result(timeout=10) for future in futures]

    assert sum(len(result.migrated) for result in results) == 1
    assert {
        item.task_id
        for result in results
        for item in result.migrated
    } == {task.task_id}
    assert sum(
        task.task_id in result.already_migrated_task_ids
        for result in results
    ) == 1


def test_legacy_checkpoint_migration_is_read_only_complete_and_idempotent(
    tmp_path,
    monkeypatch,
):
    kwargs = _profile_kwargs(tmp_path)
    legacy_path = tmp_path / "legacy" / "active-task.sqlite3"
    legacy = TaskCheckpointStore(legacy_path)
    config = AppConfig(
        storage_ae_title="LEGACY",
        storage_port=6688,
        dicom_destination_folder=str(tmp_path / "legacy-dicom"),
    )
    checkpoint = legacy.start(
        config,
        ["DONE", "PARTIAL", "PENDING"],
        trial_required=True,
    )
    legacy.record_result(
        checkpoint.task_id,
        AccessionResult(
            "DONE",
            AccessionStatus.COMPLETED,
            file_count=1,
            received_bytes=128,
            archived_files=[str(tmp_path / "legacy-dicom" / "done.dcm")],
        ),
    )
    legacy.record_result(
        checkpoint.task_id,
        AccessionResult(
            "PARTIAL",
            AccessionStatus.CANCELLED,
            file_count=1,
            received_bytes=64,
            archived_files=[str(tmp_path / "legacy-dicom" / "partial.dcm")],
        ),
    )
    pdi_attempt_id, _reused = legacy.begin_pdi_attempt(
        checkpoint.task_id,
        reuse_existing=False,
    )
    source_checkpoint = legacy.load_required()
    original_source = legacy_path.read_bytes()

    result = migrate_legacy_checkpoint_to_profile(legacy_path, **kwargs)

    assert result.migrated is not None
    imported_path = (
        kwargs["state_root"]
        / "instances"
        / f"i{result.migrated.profile_number}"
        / "active-task.sqlite3"
    )
    imported = TaskCheckpointStore(imported_path).load_required()
    assert imported.task_id == source_checkpoint.task_id
    assert imported.config.to_dict() == source_checkpoint.config.to_dict()
    assert imported.accessions == source_checkpoint.accessions
    assert imported.results == source_checkpoint.results
    assert imported.partial_results == source_checkpoint.partial_results
    assert imported.trial_required == source_checkpoint.trial_required
    assert imported.created_at == source_checkpoint.created_at
    assert imported.phase == "pdi_running"
    assert imported.pdi_attempt_id == pdi_attempt_id
    assert legacy_path.is_file()
    assert legacy_path.read_bytes() == original_source

    def reject_checkpoint_reload(*_args, **_kwargs):
        raise AssertionError("completed legacy migration must use its marker")

    monkeypatch.setattr(
        TaskCheckpointStore,
        "load_required",
        reject_checkpoint_reload,
    )
    repeated = migrate_legacy_checkpoint_to_profile(legacy_path, **kwargs)
    assert repeated.migrated is None
    assert repeated.already_migrated

    imported_path.unlink()
    after_clear = migrate_legacy_checkpoint_to_profile(legacy_path, **kwargs)
    assert after_clear.migrated is None
    assert after_clear.already_migrated
    assert not imported_path.exists()
    marker = json.loads(result.marker_path.read_text(encoding="utf-8"))
    assert marker["source"] == str(legacy_path.resolve())
    assert marker["task_ids"] == [checkpoint.task_id]
    assert legacy_path.read_bytes() == original_source


def test_legacy_checkpoint_migration_cleans_recorded_process_read_only(
    tmp_path,
    monkeypatch,
):
    kwargs = _profile_kwargs(tmp_path)
    legacy_path = tmp_path / "legacy" / "active-task.sqlite3"
    legacy = TaskCheckpointStore(legacy_path)
    checkpoint = legacy.start(AppConfig(), ["A001"], trial_required=False)
    identity = {
        "command_line": ["/tools/storescp", "6666"],
        "created_at": 13.5,
        "executable": "/tools/storescp",
        "pid": 104,
        "process_group_id": 0,
    }
    with sqlite3.connect(legacy_path) as connection:
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('process:storescp', ?)",
            (json.dumps(identity),),
        )
    original_source = legacy_path.read_bytes()
    cleaned: list[tuple[dict[str, object], str]] = []

    def clean(record, label):
        cleaned.append((record, label))
        return True, "cleaned"

    monkeypatch.setattr(instance_profile_module, "_cleanup_process_identity", clean)

    result = migrate_legacy_checkpoint_to_profile(legacy_path, **kwargs)

    assert result.migrated is not None
    assert result.migrated.task_id == checkpoint.task_id
    assert cleaned == [(identity, "storescp")]
    assert result.marker_path.is_file()
    assert legacy_path.read_bytes() == original_source


def test_legacy_checkpoint_migration_does_not_mark_unresolved_process(
    tmp_path,
    monkeypatch,
):
    kwargs = _profile_kwargs(tmp_path)
    legacy_path = tmp_path / "legacy" / "active-task.sqlite3"
    legacy = TaskCheckpointStore(legacy_path)
    legacy.start(AppConfig(), ["A001"], trial_required=False)
    identity = {
        "command_line": ["/tools/movescu", "PACS", "104"],
        "created_at": 14.5,
        "executable": "/tools/movescu",
        "pid": 105,
        "process_group_id": 0,
    }
    with sqlite3.connect(legacy_path) as connection:
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('process:movescu', ?)",
            (json.dumps(identity),),
        )
    original_source = legacy_path.read_bytes()
    monkeypatch.setattr(
        instance_profile_module,
        "_cleanup_process_identity",
        lambda _identity, _label: (False, "旧版 movescu 无法安全清理"),
    )

    with pytest.raises(InstanceProfileError, match="无法安全清理"):
        migrate_legacy_checkpoint_to_profile(legacy_path, **kwargs)

    marker = kwargs["state_root"] / "instances" / ".active-task-migrated-v1.json"
    assert not marker.exists()
    assert legacy_path.read_bytes() == original_source


def test_legacy_migration_deduplicates_task_already_imported_from_catalog(tmp_path):
    kwargs = _profile_kwargs(tmp_path)
    legacy_path = tmp_path / "legacy" / "active-task.sqlite3"
    legacy = TaskCheckpointStore(legacy_path)
    checkpoint = legacy.start(
        AppConfig(dicom_destination_folder=str(tmp_path / "legacy-dicom")),
        ["A001", "A002"],
        trial_required=False,
    )
    catalog_path = tmp_path / "legacy" / "tasks.sqlite3"
    TaskCatalog(catalog_path, legacy_path=legacy_path, auto_migrate=True)

    catalog_result = migrate_task_catalog_to_profiles(catalog_path, **kwargs)
    legacy_result = migrate_legacy_checkpoint_to_profile(legacy_path, **kwargs)

    assert [item.task_id for item in catalog_result.migrated] == [
        checkpoint.task_id
    ]
    assert legacy_result.migrated is None
    assert legacy_result.already_migrated
    checkpoints = list(
        (kwargs["state_root"] / "instances").glob("i*/active-task.sqlite3")
    )
    assert len(checkpoints) == 1
    assert TaskCheckpointStore(checkpoints[0]).load_required().task_id == (
        checkpoint.task_id
    )


def test_concurrent_legacy_migrations_wait_and_import_the_task_once(tmp_path):
    kwargs = _profile_kwargs(tmp_path)
    legacy_path = tmp_path / "legacy" / "active-task.sqlite3"
    legacy = TaskCheckpointStore(legacy_path)
    checkpoint = legacy.start(AppConfig(), ["A001"], trial_required=False)
    barrier = threading.Barrier(2)

    def migrate():
        barrier.wait(timeout=5)
        return migrate_legacy_checkpoint_to_profile(legacy_path, **kwargs)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(migrate), pool.submit(migrate)]
        results = [future.result(timeout=10) for future in futures]

    assert sum(result.migrated is not None for result in results) == 1
    assert {
        result.migrated.task_id
        for result in results
        if result.migrated is not None
    } == {checkpoint.task_id}
    assert sum(result.already_migrated for result in results) == 1


def test_legacy_migration_refuses_to_race_an_active_old_instance(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        instance_profile_module,
        "MIGRATION_LOCK_TIMEOUT_SECONDS",
        0.05,
    )
    kwargs = _profile_kwargs(tmp_path)
    legacy_path = tmp_path / "legacy" / "active-task.sqlite3"
    legacy = TaskCheckpointStore(legacy_path)
    legacy.start(AppConfig(), ["A001"], trial_required=False)
    assert legacy.try_acquire_lease()
    try:
        with pytest.raises(InstanceProfileError, match="仍被另一个"):
            migrate_legacy_checkpoint_to_profile(legacy_path, **kwargs)
    finally:
        legacy.release_lease()


@pytest.mark.parametrize("value", [0, -1, 10000, True, "not-a-number"])
def test_explicit_profile_number_is_validated(tmp_path, value):
    with pytest.raises(InstanceProfileError, match="实例编号"):
        acquire_instance_profile(value, **_profile_kwargs(tmp_path))


def test_profile_web_browser_waits_until_http_service_is_ready():
    import DICOM_download_ui as entry

    attempts = 0
    opened: list[tuple[str, int]] = []

    class _Response:
        def close(self) -> None:
            return None

    def probe(_url: str, **_kwargs: object):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("service is still starting")
        return _Response()

    assert entry._open_browser_when_ready(
        "http://127.0.0.1:8787/",
        timeout=1,
        poll_interval=0,
        urlopen=probe,
        opener=lambda url, new=0: opened.append((url, new)) or True,
    )
    assert attempts == 2
    assert opened == [("http://127.0.0.1:8787/", 1)]


def test_profile_web_browser_is_not_opened_when_service_never_starts():
    import DICOM_download_ui as entry

    opened: list[str] = []

    assert not entry._open_browser_when_ready(
        "http://127.0.0.1:8787/",
        timeout=0.01,
        poll_interval=0.001,
        urlopen=lambda _url, **_kwargs: (_ for _ in ()).throw(OSError("down")),
        opener=lambda url, **_kwargs: opened.append(url) or True,
    )
    assert opened == []


def test_profile_web_launch_and_service_browser_flags_are_parsed():
    import DICOM_download_ui as entry

    args = entry.build_parser().parse_args(
        [
            "--profile",
            "3",
            "--open-profile-web",
            "--no-open-browser",
            "--storage-port",
            "6777",
            "--web-port",
            "8899",
        ]
    )

    assert args.profile == 3
    assert args.open_profile_web
    assert args.no_open_browser
    assert args.storage_port == 6777
    assert args.web_port == 8899
    assert not entry._activation_requests_browser({"action": "ensure-running"})
    assert entry._activation_requests_browser({"action": "open-profile-web"})
