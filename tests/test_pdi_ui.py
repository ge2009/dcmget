from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtGui import QCloseEvent
from PyQt5.QtWidgets import QMessageBox

from dcmget.config import AppConfig
from dcmget.core import (
    AccessionResult,
    AccessionStatus,
    BatchSummary,
    PreflightResult,
    ToolPaths,
)
import dcmget.pdi as pdi_module
import dcmget.ui as ui_module
from dcmget.ui import DcmGetWindow, PdiWorker


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    path = tmp_path / "pdi-ui-settings.ini"
    monkeypatch.setattr(
        ui_module,
        "QSettings",
        lambda *_args: QSettings(str(path), QSettings.IniFormat),
    )


def make_window(qtbot, tmp_path):
    window = DcmGetWindow(
        tmp_path / "config.json",
        tmp_path,
        tmp_path / "active-task.sqlite3",
        offer_task_resume=False,
    )
    qtbot.addWidget(window)
    window.show()
    return window


def test_pdi_settings_are_collapsed_and_disabled_by_default(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    page = window.settings_page

    assert not page.pdi_enabled_checkbox.isChecked()
    assert page.pdi_card_body.isHidden()
    assert not page.pdi_institution_edit.isEnabled()
    assert not window.pdi_status_card.isVisible()

    qtbot.mouseClick(page.pdi_card_toggle, Qt.LeftButton)

    assert not page.pdi_card_body.isHidden()
    assert not page.pdi_institution_edit.isEnabled()


def test_enabling_pdi_expands_options_and_validates_institution(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    page = window.settings_page
    window.pages.setCurrentIndex(1)

    page.pdi_enabled_checkbox.setChecked(True)
    page._save()

    assert page.pdi_card_body.isVisible()
    assert page.pdi_institution_edit.isEnabled()
    assert page.pdi_institution_edit.property("invalid") is True
    assert "机构名称" in page.pdi_institution_edit.toolTip()
    assert "患者隐私" in page.pdi_privacy_warning.text()


def test_pdi_settings_round_trip_all_options(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    page = window.settings_page
    output = tmp_path / "portable"

    page.set_config(
        AppConfig(
            pdi_export_enabled=True,
            pdi_institution_name="海市中心医院",
            pdi_output_folder=str(output),
            pdi_include_html_preview=False,
            pdi_preview_mode="series_cover",
            pdi_include_weasis_windows=False,
        )
    )
    result = page.config()

    assert result.pdi_export_enabled
    assert result.pdi_institution_name == "海市中心医院"
    assert result.pdi_output_folder == str(output)
    assert not result.pdi_include_html_preview
    assert result.pdi_preview_mode == "series_cover"
    assert not result.pdi_include_weasis_windows
    assert not page.pdi_preview_mode_combo.isEnabled()


def test_download_completion_starts_pdi_with_exact_batch_files(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    archived = tmp_path / "study" / "image.dcm"
    window.config.pdi_export_enabled = True
    called = []
    monkeypatch.setattr(window, "_start_pdi_export", lambda files=None: called.append(files))
    summary = BatchSummary(
        [
            AccessionResult(
                "A001",
                AccessionStatus.COMPLETED,
                archived_files=[str(archived)],
            )
        ]
    )

    window._on_worker_finished(summary)

    assert called == [[str(archived)]]
    assert window.last_summary is summary


def test_download_checkpoint_is_kept_until_pdi_finishes(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    archived = tmp_path / "study" / "image.dcm"
    config = AppConfig(
        pdi_export_enabled=True,
        pdi_institution_name="测试医院",
    )
    checkpoint = window.task_store.start(config, ["A001"], trial_required=False)
    result = AccessionResult(
        "A001",
        AccessionStatus.COMPLETED,
        archived_files=[str(archived)],
    )
    window.task_store.record_result(checkpoint.task_id, result)
    window.task_store.try_acquire_lease()
    window.config = config
    window._active_task_id = checkpoint.task_id
    called = []
    monkeypatch.setattr(window, "_start_pdi_export", lambda files=None: called.append(files))

    window._on_worker_finished(BatchSummary([result]))

    restored = window.task_store.load_required()
    assert restored.phase == "pdi_pending"
    assert window._pdi_task_id == checkpoint.task_id
    assert called == [[str(archived)]]
    window.task_store.release_lease()


def test_startup_can_resume_pdi_without_redownloading(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    archived = tmp_path / "study" / "image.dcm"
    config = AppConfig(
        pdi_export_enabled=True,
        pdi_institution_name="测试医院",
    )
    checkpoint = window.task_store.start(config, ["A001"], trial_required=False)
    window.task_store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.COMPLETED,
            archived_files=[str(archived)],
        ),
    )
    window.task_store.set_phase(checkpoint.task_id, "pdi_retryable")
    started = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )
    monkeypatch.setattr(
        window,
        "_start_pdi_export",
        lambda files=None: started.append(files),
    )

    window._offer_task_resume()

    assert started == [[str(archived)]]
    assert window._pdi_task_id == checkpoint.task_id
    assert window.last_summary.archived_files == [str(archived)]
    window.task_store.release_lease()


def test_pdi_success_clears_checkpoint_and_failure_keeps_retryable_phase(
    qtbot, tmp_path
):
    window = make_window(qtbot, tmp_path)
    checkpoint = window.task_store.start(AppConfig(), ["A001"], trial_required=False)
    window._pdi_task_id = checkpoint.task_id
    window.task_store.try_acquire_lease()

    window._save_pdi_checkpoint_status(completed=False)

    assert window.task_store.load_required().phase == "pdi_retryable"
    assert window._pdi_task_id == checkpoint.task_id

    window.task_store.try_acquire_lease()
    window._save_pdi_checkpoint_status(completed=True)

    assert window.task_store.load() is None
    assert window._pdi_task_id == ""


def test_preflight_area_expands_for_pdi_checks(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    checks = [
        ("DCMTK 工具", True, "已就绪"),
        ("PDI 导出工具", True, "已就绪"),
        ("保存目录", True, "目录可写"),
        ("PDI 输出目录", True, "目录可写"),
        ("接收端口", True, "端口可用"),
        ("PACS 配置", True, "127.0.0.1:8104"),
    ]

    window._show_preflight(PreflightResult(None, {}, checks))

    assert len(window.preflight_labels) == 6
    assert all(label.isVisible() for label in window.preflight_labels)
    assert window.preflight_labels[1].text().startswith("PDI 导出工具")


def test_pdi_progress_and_success_enable_open_directory(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    output = tmp_path / "PDI" / "DCMGET_PDI_20260716_120000"
    output.mkdir(parents=True)
    window.config.pdi_export_enabled = True
    window._pdi_source_files = [str(tmp_path / "image.dcm")]
    window.last_summary = BatchSummary()
    messages = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, message: messages.append((title, message)),
    )

    class Stage:
        value = "生成 DICOMDIR"

    window._on_pdi_progress(Stage(), 1, 2, "正在写入目录")

    assert window.pdi_progress_bar.maximum() == 2
    assert window.pdi_progress_bar.value() == 1
    assert "生成 DICOMDIR" in window.pdi_status_label.text()

    @dataclass
    class Result:
        status: object
        output_directory: str
        message: str = "PDI 便携目录已生成"
        warnings: list[str] = field(default_factory=list)

    class Status:
        value = "完成"

    window._on_pdi_finished(Result(Status(), str(output)))

    assert window.pdi_open_button.isEnabled()
    assert not window.pdi_retry_button.isEnabled()
    assert "已生成" in window.pdi_status_label.text()
    assert str(output) in messages[0][1]


def test_retry_pdi_does_not_restart_download(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    files = [str(tmp_path / "image.dcm")]
    window._pdi_source_files = files
    called = []
    monkeypatch.setattr(window, "_start_pdi_export", lambda values=None: called.append(values))

    window._retry_pdi()

    assert called == [None]


def test_saving_corrected_settings_preserves_failed_pdi_retry(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    archived = str(tmp_path / "image.dcm")
    window._pdi_source_files = [archived]
    checkpoint = window.task_store.start(
        AppConfig(pdi_export_enabled=True, pdi_institution_name="旧机构"),
        ["A001"],
        trial_required=False,
    )
    window.task_store.record_result(
        checkpoint.task_id,
        AccessionResult(
            "A001",
            AccessionStatus.COMPLETED,
            archived_files=[archived],
        ),
    )
    window.task_store.set_phase(checkpoint.task_id, "pdi_retryable")
    window._pdi_task_id = checkpoint.task_id

    class Status:
        value = "失败"

    @dataclass
    class Result:
        status: object = field(default_factory=Status)
        output_directory: str = ""

    window.last_pdi_result = Result()
    corrected = AppConfig(
        pdi_export_enabled=True,
        pdi_institution_name="海市中心医院",
        pdi_include_weasis_windows=False,
    )

    window._save_settings(corrected)

    assert window.pdi_retry_button.isEnabled()
    assert "可以重试" in window.pdi_status_label.text()
    restored = window.task_store.load_required()
    assert restored.config.pdi_institution_name == "海市中心医院"
    assert not restored.config.pdi_include_weasis_windows

    restarted = make_window(qtbot, tmp_path)
    started_with = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )
    monkeypatch.setattr(
        restarted,
        "_start_pdi_export",
        lambda _files=None: started_with.append(
            (
                restarted.config.pdi_institution_name,
                restarted.config.pdi_include_weasis_windows,
            )
        ),
    )
    restarted._offer_task_resume()

    assert started_with == [("海市中心医院", False)]
    restarted.task_store.release_lease()


def test_failed_pdi_checkpoint_blocks_new_download_until_discarded(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    checkpoint = window.task_store.start(
        AppConfig(pdi_export_enabled=True, pdi_institution_name="测试医院"),
        ["A001"],
        trial_required=False,
    )
    window._pdi_task_id = checkpoint.task_id
    window._pdi_source_files = [str(tmp_path / "image.dcm")]
    window.task_store.set_phase(checkpoint.task_id, "pdi_retryable")

    window._set_running(False)

    assert not window.start_button.isEnabled()
    assert window.start_button.text() == "PDI 待重试"
    assert window.discard_resume_button.isVisible()
    assert window.settings_button.isEnabled()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.Yes,
    )
    qtbot.mouseClick(window.discard_resume_button, Qt.LeftButton)

    assert window.task_store.load() is None
    assert window._pdi_task_id == ""
    assert window.start_button.isEnabled()


def test_pdi_checkpoint_guard_rejects_direct_start_and_failed_retry(
    qtbot, tmp_path, monkeypatch
):
    window = make_window(qtbot, tmp_path)
    checkpoint = window.task_store.start(
        AppConfig(pdi_export_enabled=True, pdi_institution_name="测试医院"),
        ["A001"],
        trial_required=False,
    )
    window._pdi_task_id = checkpoint.task_id
    window.current_accessions = ["NEW001"]
    window.last_summary = BatchSummary(
        [AccessionResult("FAILED001", AccessionStatus.FAILED)]
    )
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args, **_kwargs: warnings.append(_args[2]),
    )

    window._start_download()
    window._retry_failed()

    assert len(warnings) == 2
    assert all("PDI" in warning for warning in warnings)
    assert window.task_store.load_required().task_id == checkpoint.task_id


def test_pdi_thread_completion_releases_busy_state(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    window.config.pdi_export_enabled = True
    window.config.pdi_institution_name = "海市中心医院"
    window.last_summary = BatchSummary()
    window.tools = ToolPaths(
        tmp_path / "movescu",
        tmp_path / "storescp",
        tmp_path,
        "3.7.0",
    )

    class Status:
        value = "完成"

    @dataclass
    class Result:
        status: object = field(default_factory=Status)
        output_directory: str = ""
        message: str = "PDI 便携目录已生成"
        warnings: list[str] = field(default_factory=list)

    class Exporter:
        def __init__(self, *_args, **_kwargs):
            pass

        def export(self, _files):
            return Result()

        def request_cancel(self):
            pass

    monkeypatch.setattr(pdi_module, "PdiExporter", Exporter)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: None)

    window._start_pdi_export([str(tmp_path / "image.dcm")])
    qtbot.waitUntil(lambda: window.pdi_thread is None)

    assert window.pdi_worker is None
    assert not window._is_busy()
    assert window.start_button.isEnabled()
    assert window.settings_button.isEnabled()


def test_pdi_worker_uses_exporter_callbacks_and_honors_early_cancel(
    qtbot, tmp_path, monkeypatch
):
    instances = []
    result = object()

    class Exporter:
        def __init__(self, config, tools, **kwargs):
            self.config = config
            self.tools = tools
            self.kwargs = kwargs
            self.cancelled = False
            instances.append(self)

        def request_cancel(self):
            self.cancelled = True

        def export(self, files):
            assert files == [str(tmp_path / "image.dcm")]
            return result

    monkeypatch.setattr(pdi_module, "PdiExporter", Exporter)
    tools = ToolPaths(
        tmp_path / "movescu",
        tmp_path / "storescp",
        tmp_path,
        "3.7.0",
    )
    worker = PdiWorker(
        AppConfig(), tools, [str(tmp_path / "image.dcm")], tmp_path
    )
    finished = []
    worker.finished.connect(finished.append)
    worker.request_cancel()

    worker.run()

    assert instances[0].config == worker.config
    assert instances[0].tools is tools
    assert instances[0].cancelled
    assert callable(instances[0].kwargs["log_callback"])
    assert callable(instances[0].kwargs["progress_callback"])
    assert finished == [result]


def test_close_during_pdi_requests_export_cancel(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)

    class Worker:
        cancelled = False

        def request_cancel(self):
            self.cancelled = True

    worker = Worker()
    window.pdi_worker = worker  # type: ignore[assignment]
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes
    )
    event = QCloseEvent()

    window.closeEvent(event)

    assert worker.cancelled
    assert window._closing_after_cancel
    assert not event.isAccepted()
