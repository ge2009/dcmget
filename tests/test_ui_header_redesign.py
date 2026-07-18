from __future__ import annotations

from PyQt5.QtCore import QSettings
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QApplication
import pytest

import dcmget.ui as ui_module
from dcmget.ui import DcmGetWindow


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    path = tmp_path / "header-settings.ini"
    monkeypatch.setattr(
        ui_module,
        "QSettings",
        lambda *_args: QSettings(str(path), QSettings.IniFormat),
    )


def make_window(qtbot, tmp_path, **kwargs):
    window = DcmGetWindow(
        tmp_path / "config.json",
        tmp_path,
        tmp_path / "active-task.sqlite3",
        offer_task_resume=False,
        **kwargs,
    )
    qtbot.addWidget(window)
    window.show()
    QApplication.processEvents()
    return window


def test_compact_header_keeps_primary_controls_inside_683_logical_pixels(
    qtbot, tmp_path, monkeypatch
):
    logo = QPixmap(64, 64)
    logo.fill()
    assert logo.save(str(tmp_path / "logo.png"))
    monkeypatch.setattr(ui_module, "entitlement_text", lambda: "已注册 · Windows设备")
    window = make_window(qtbot, tmp_path, instance_label="影像下载一组")
    window._set_tool_status("DCMTK 3.7.0 已就绪", "ok")
    window.setMinimumSize(1, 1)
    window.resize(683, 480)
    QApplication.processEvents()

    visible_controls = (
        window.compact_logo,
        window.app_title,
        window.app_subtitle,
        window.system_status,
        window.header_settings_button,
        window.more_button,
    )
    assert all(control.isVisible() for control in visible_controls)
    assert window.app_logo.isHidden()
    assert window.header.height() < 70
    for control in visible_controls:
        assert control.geometry().left() >= 0
        assert control.geometry().right() < window.header.width()

    assert window.app_subtitle.toolTip() == "下载通道：影像下载一组"
    assert window.system_status.text() == "正常"
    assert window.entitlement_status.isHidden()


def test_more_menu_owns_secondary_actions_and_trial_is_compact(
    qtbot, tmp_path, monkeypatch
):
    monkeypatch.setattr(ui_module, "entitlement_text", lambda: "免费试用剩余 12 次")
    window = make_window(qtbot, tmp_path, profile_number=2)
    window._set_tool_status("DCMTK 3.7.0 已就绪", "ok")
    QApplication.processEvents()

    action_texts = [action.text() for action in window.more_menu.actions()]
    assert "授权：免费试用剩余 12 次" in action_texts
    assert "输入注册码" in action_texts
    assert "版本说明" in action_texts
    assert "诊断日志" in action_texts
    assert "运维工具" in action_texts
    assert "创建实例快捷方式" in action_texts
    assert "试用" in window.system_status.text()
    assert window.registration_button.isHidden()
    assert window.release_notes_button.isHidden()
    assert window.diagnostic_log_button.isHidden()

    QApplication.processEvents()
    assert QApplication.focusWidget() is window.accession_edit
    assert QApplication.focusWidget() is not window.registration_button
