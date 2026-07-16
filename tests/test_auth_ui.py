from __future__ import annotations

from PyQt5.QtWidgets import QApplication, QDialog

import dcmget.auth_ui as auth_ui
from dcmget.auth_ui import ActivationDialog
from dcmget.licensing import LicenseError, TrialInfo


def test_activation_dialog_copies_machine_code_and_saves_token(
    qtbot, monkeypatch
):
    monkeypatch.setattr(auth_ui, "machine_code", lambda: "ABCDEF-123456-7890AB-CDEF12")
    saved = []
    monkeypatch.setattr(auth_ui, "save_license", lambda token: saved.append(token))
    dialog = ActivationDialog()
    qtbot.addWidget(dialog)
    dialog.show()

    dialog._copy_machine_code()
    assert dialog.machine_code_edit.text() == "ABCDEF-123456-7890AB-CDEF12"
    assert QApplication.clipboard().text() == dialog.machine_code_edit.text()

    dialog.token_edit.setPlainText("DGM1.payload.signature")
    dialog._activate()

    assert saved == ["DGM1.payload.signature"]
    assert dialog.result() == QDialog.Accepted


def test_unregistered_download_is_prepared_to_consume_trial_when_ready(monkeypatch):
    monkeypatch.setattr(
        auth_ui,
        "load_license",
        lambda: (_ for _ in ()).throw(LicenseError("尚未注册")),
    )
    monkeypatch.setattr(auth_ui, "trial_status", lambda: TrialInfo(used=0, remaining=30))

    allowed, use_trial, message = auth_ui.prepare_download_entitlement()

    assert allowed
    assert use_trial
    assert "剩余 30 次" in message


def test_authorize_gui_allows_remaining_trial_without_login(monkeypatch):
    monkeypatch.setattr(
        auth_ui,
        "load_license",
        lambda: (_ for _ in ()).throw(LicenseError("尚未注册")),
    )
    monkeypatch.setattr(
        auth_ui,
        "trial_status",
        lambda: TrialInfo(used=1, remaining=29),
    )

    class UnexpectedActivation:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("试用期内不应弹出注册窗口")

    monkeypatch.setattr(auth_ui, "ActivationDialog", UnexpectedActivation)

    assert auth_ui.authorize_gui()


def test_consumed_thirtieth_trial_task_can_reenter_for_resume(monkeypatch):
    monkeypatch.setattr(
        auth_ui,
        "load_license",
        lambda: (_ for _ in ()).throw(LicenseError("尚未注册")),
    )
    monkeypatch.setattr(auth_ui, "trial_status", lambda: TrialInfo(used=30, remaining=0))
    monkeypatch.setattr(
        auth_ui,
        "trial_task_consumed",
        lambda task_id: task_id == "a" * 32,
    )

    assert auth_ui.authorize_gui("a" * 32)
