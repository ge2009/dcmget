from __future__ import annotations

from pathlib import Path

import pytest
from PyQt5.QtCore import QPoint, QSettings, Qt, QThread
from PyQt5.QtWidgets import QApplication, QLabel, QMessageBox

from dcmget.config import AppConfig, load_config, save_config
from dcmget.core import (
    AccessionResult,
    AccessionStatus,
    BatchSummary,
    PreflightResult,
    ToolPaths,
)
from dcmget.task_state import TaskCheckpointStore
import dcmget.ui as ui_module
from dcmget.ui import DcmGetWindow


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    settings_path = tmp_path / "progressive-redesign-settings.ini"
    monkeypatch.setattr(
        ui_module,
        "QSettings",
        lambda *_args: QSettings(str(settings_path), QSettings.IniFormat),
    )


def make_window(
    qtbot,
    tmp_path: Path,
    *,
    offer_task_resume: bool = False,
) -> DcmGetWindow:
    window = DcmGetWindow(
        tmp_path / "config.json",
        tmp_path,
        tmp_path / "active-task.sqlite3",
        offer_task_resume=offer_task_resume,
        enable_multi_task=False,
    )
    qtbot.addWidget(window)
    window.show()
    QApplication.processEvents()
    return window


def test_idle_phase_hides_pending_checks_empty_results_and_empty_table(
    qtbot,
    tmp_path,
):
    window = make_window(qtbot, tmp_path)

    assert window.preflight_card.isHidden()
    assert window.task_result_summary.isHidden()
    assert window.task_table.isHidden()
    for button in (
        window.open_destination_button,
        window.open_task_log_button,
        window.open_acceptance_report_button,
        window.open_conflict_button,
    ):
        assert button.isHidden()


def test_idle_primary_action_stays_in_first_683_by_480_viewport(
    qtbot,
    tmp_path,
):
    window = make_window(qtbot, tmp_path)
    window.setMinimumSize(1, 1)
    window.resize(683, 480)
    QApplication.processEvents()

    viewport = window.task_scroll.viewport()
    top_left = window.start_button.mapTo(viewport, QPoint(0, 0))
    assert window.task_scroll.horizontalScrollBar().maximum() == 0
    assert 0 <= top_left.y()
    assert top_left.y() + window.start_button.height() <= viewport.height()


def test_running_phase_shows_progress_and_hides_editor_and_start_action(
    qtbot,
    tmp_path,
):
    window = make_window(qtbot, tmp_path)
    destination = tmp_path / "dicom"
    window.accession_edit.setPlainText("A001\nA002")
    window.destination_edit.setText(str(destination))
    window._display_total = 2
    window._populate_waiting_rows(["A001", "A002"])
    window.worker = object()  # type: ignore[assignment]

    window._set_running(True)
    window._on_worker_progress(
        1,
        2,
        AccessionResult(
            "A001",
            AccessionStatus.DOWNLOADING,
            file_count=3,
            speed_bytes_per_second=2 * 1024 * 1024,
        ),
    )
    QApplication.processEvents()
    try:
        assert window.task_form_body.isHidden()
        assert window.task_form_summary.isVisible()
        assert window.task_section_title.text() == "当前任务"
        assert window.start_button.isHidden()
        assert window.progress_label.isVisible()
        assert "A001" in window.progress_label.text()
        assert "3 个文件" in window.progress_label.text()
        assert window.progress_bar.isVisible()
        assert window.task_table.isVisible()
        assert window.more_button.isEnabled()
        assert window.diagnostic_log_action.isEnabled()
        assert not window.maintenance_menu.menuAction().isEnabled()
    finally:
        window.worker = None


def test_completed_phase_preserves_results_and_prepares_empty_next_draft(
    qtbot,
    tmp_path,
):
    window = make_window(qtbot, tmp_path)
    result = AccessionResult("A001", AccessionStatus.COMPLETED, file_count=3)
    window.accession_edit.setPlainText("A001")
    window._display_total = 1
    window._populate_waiting_rows(["A001"])
    window._set_result_row(result)
    window.last_summary = BatchSummary([result])
    window._update_task_result_summary([result])

    window._show_download_completion()

    assert window.current_accessions == []
    assert window.accession_edit.toPlainText() == ""
    assert window.task_form_body.isHidden()
    assert window.task_table.rowCount() == 1
    assert window.task_table.item(0, 1).text() == "完成"
    assert window.task_result_summary.isVisible()
    assert window.start_button.isVisible()
    assert not window.start_button.isEnabled()


def test_task_page_pdi_overrides_do_not_persist_or_mutate_settings(
    qtbot,
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.json"
    profile_output = tmp_path / "profile-pdi"
    task_output = tmp_path / "task-pdi"
    task_output.mkdir()
    save_config(
        config_path,
        AppConfig(
            pdi_export_enabled=False,
            pdi_institution_name="测试医院",
            pdi_output_folder=str(profile_output),
        ),
    )
    before = config_path.read_bytes()
    window = make_window(qtbot, tmp_path)
    settings_enabled = window.settings_page.pdi_enabled_checkbox.isChecked()
    settings_output = window.settings_page.pdi_output_edit.text()
    monkeypatch.setattr(
        ui_module.QFileDialog,
        "getExistingDirectory",
        lambda *_args: str(task_output),
    )

    qtbot.mouseClick(window.quick_pdi_checkbox, Qt.LeftButton)
    qtbot.mouseClick(window.quick_pdi_output_button, Qt.LeftButton)

    assert window.quick_pdi_checkbox.isChecked()
    assert window.quick_pdi_output_label.toolTip() == str(task_output)
    assert config_path.read_bytes() == before
    persisted = load_config(config_path)
    assert not persisted.pdi_export_enabled
    assert persisted.pdi_output_folder == str(profile_output)
    assert (
        window.settings_page.pdi_enabled_checkbox.isChecked()
        == settings_enabled
    )
    assert window.settings_page.pdi_output_edit.text() == settings_output


def test_startup_recovery_is_non_modal_and_starts_only_after_continue(
    qtbot,
    tmp_path,
    monkeypatch,
):
    state_path = tmp_path / "active-task.sqlite3"
    destination = tmp_path / "dicom"
    store = TaskCheckpointStore(state_path)
    checkpoint = store.start(
        AppConfig(dicom_destination_folder=str(destination)),
        ["DONE", "PENDING"],
        trial_required=False,
    )
    store.record_result(
        checkpoint.task_id,
        AccessionResult("DONE", AccessionStatus.COMPLETED, file_count=12),
    )
    questions: list[tuple[object, ...]] = []
    started: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **_kwargs: (
            questions.append(args) or QMessageBox.No
        ),
    )

    def record_start(self, override=None, **kwargs):
        started.append((override, kwargs))

    monkeypatch.setattr(DcmGetWindow, "_start_download", record_start)

    window = make_window(qtbot, tmp_path, offer_task_resume=True)
    QApplication.processEvents()

    assert questions == []
    assert started == []
    assert window.recovery_card.isVisible()
    assert window.task_section_title.text() == "当前任务"
    assert window.recovery_continue_button.isVisible()
    assert window.recovery_continue_button.isEnabled()
    assert window.progress_label.isHidden()
    assert window.progress_bar.isHidden()
    assert window.task_result_summary.isHidden()
    assert window.task_table.item(0, 1).text() == "完成"
    assert window.task_table.item(0, 5).text() == "接收完成"
    recovery_text = "\n".join(
        label.text() for label in window.recovery_card.findChildren(QLabel)
    )
    assert "1/2" in recovery_text
    assert "文件 12" in recovery_text
    assert str(destination) in recovery_text

    qtbot.mouseClick(window.recovery_continue_button, Qt.LeftButton)

    assert len(started) == 1
    resumed = started[0][1]["resume_checkpoint"]
    assert resumed.task_id == checkpoint.task_id
    assert resumed.pending_accessions == ["PENDING"]


def test_recovery_continue_stays_available_when_task_lease_is_busy(
    qtbot,
    tmp_path,
    monkeypatch,
):
    state_path = tmp_path / "active-task.sqlite3"
    store = TaskCheckpointStore(state_path)
    store.start(AppConfig(), ["PENDING"], trial_required=False)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: None)

    window = make_window(qtbot, tmp_path, offer_task_resume=True)
    monkeypatch.setattr(window.task_store, "try_acquire_lease", lambda: False)

    qtbot.mouseClick(window.recovery_continue_button, Qt.LeftButton)

    assert window.recovery_card.isVisible()
    assert window.recovery_continue_button.isEnabled()


def test_saving_profile_settings_does_not_overwrite_recovery_snapshot(
    qtbot,
    tmp_path,
):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "active-task.sqlite3"
    profile_output = tmp_path / "profile-pdi"
    task_output = tmp_path / "task-pdi"
    save_config(
        config_path,
        AppConfig(
            pdi_export_enabled=False,
            pdi_institution_name="Profile 医院",
            pdi_output_folder=str(profile_output),
        ),
    )
    store = TaskCheckpointStore(state_path)
    checkpoint = store.start(
        AppConfig(
            pdi_export_enabled=True,
            pdi_institution_name="任务医院",
            pdi_output_folder=str(task_output),
        ),
        ["PENDING"],
        trial_required=False,
    )
    window = make_window(qtbot, tmp_path, offer_task_resume=True)

    window._save_settings(
        AppConfig(
            pdi_export_enabled=False,
            pdi_institution_name="Profile 医院",
            pdi_output_folder=str(profile_output),
        )
    )

    persisted_task = window.task_store.load_required(
        include_archived_files=False,
    )
    assert persisted_task.task_id == checkpoint.task_id
    assert persisted_task.config.pdi_export_enabled
    assert persisted_task.config.pdi_institution_name == "任务医院"
    assert persisted_task.config.pdi_output_folder == str(task_output)
    assert window._effective_task_config().pdi_export_enabled
    assert window.quick_pdi_checkbox.isChecked()
    assert window.quick_pdi_output_label.toolTip() == str(task_output)
    persisted_profile = load_config(config_path)
    assert not persisted_profile.pdi_export_enabled
    assert persisted_profile.pdi_institution_name == "Profile 医院"
    assert persisted_profile.pdi_output_folder == str(profile_output)


def test_profile_pdi_defaults_seed_next_draft_after_completed_task(
    qtbot,
    tmp_path,
):
    window = make_window(qtbot, tmp_path)
    old_output = tmp_path / "old-task-pdi"
    new_output = tmp_path / "new-profile-pdi"
    window._active_task_config = AppConfig(
        pdi_export_enabled=False,
        pdi_output_folder=str(old_output),
    )
    window._prepare_next_task_draft()

    window._save_settings(
        AppConfig(
            pdi_export_enabled=True,
            pdi_institution_name="新机构",
            pdi_output_folder=str(new_output),
        )
    )

    assert window._active_task_config is None
    assert window._quick_pdi_enabled
    assert window._quick_pdi_output_folder == str(new_output)
    assert window.quick_pdi_checkbox.isChecked()
    assert window.quick_pdi_output_label.toolTip() == str(new_output)


def test_start_snapshots_task_pdi_overrides_without_changing_profile(
    qtbot,
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.json"
    destination = tmp_path / "dicom"
    destination.mkdir()
    profile_output = tmp_path / "profile-pdi"
    task_output = tmp_path / "task-pdi"
    save_config(
        config_path,
        AppConfig(
            pdi_export_enabled=False,
            pdi_institution_name="测试医院",
            pdi_output_folder=str(profile_output),
        ),
    )
    window = make_window(qtbot, tmp_path)
    window.accession_edit.setPlainText("A001")
    window.destination_edit.setText(str(destination))
    window.quick_pdi_checkbox.click()
    window._quick_pdi_output_folder = str(task_output)
    window._update_quick_pdi_summary()
    tools = ToolPaths(
        tmp_path / "movescu",
        tmp_path / "storescp",
        tmp_path,
        "3.7.0",
    )
    monkeypatch.setattr(
        ui_module,
        "preflight",
        lambda *_args, **_kwargs: PreflightResult(tools, {}, []),
    )
    monkeypatch.setattr(
        ui_module,
        "prepare_download_entitlement",
        lambda _parent: (True, False, ""),
    )
    monkeypatch.setattr(QThread, "start", lambda _thread: None)

    window._start_download()

    checkpoint = window.task_store.load_required(include_archived_files=False)
    assert checkpoint.config.pdi_export_enabled
    assert checkpoint.config.pdi_output_folder == str(task_output)
    persisted = load_config(config_path)
    assert not persisted.pdi_export_enabled
    assert persisted.pdi_output_folder == str(profile_output)
    assert window._effective_task_config().pdi_output_folder == str(task_output)

    window.worker = None
    window.worker_thread = None
    window.task_store.release_lease()
