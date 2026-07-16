from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .licensing import (
    LicenseError,
    load_license,
    machine_code,
    save_license,
    trial_status,
    trial_task_consumed,
    validate_daily_password,
)


class DailyPasswordDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("DcmGet 登录")
        self.setModal(True)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        title = QLabel("登录验证")
        title.setObjectName("PageTitle")
        description = QLabel("请输入当天的 8 位授权口令后继续。")
        description.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(description)

        form = QFormLayout()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setMaxLength(8)
        self.password_edit.setPlaceholderText("8 位口令")
        self.password_edit.setAccessibleName("当天授权口令")
        self.password_edit.returnPressed.connect(self._submit)
        form.addRow("当天口令", self.password_edit)
        layout.addLayout(form)

        self.error_label = QLabel()
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText("登录")
        buttons.button(QDialogButtonBox.Cancel).setText("退出")
        buttons.accepted.connect(self._submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _submit(self) -> None:
        if validate_daily_password(self.password_edit.text()):
            self.accept()
            return
        self.error_label.setText("当天口令不正确，请检查电脑日期后重试。")
        self.error_label.show()
        self.password_edit.selectAll()
        self.password_edit.setFocus(Qt.OtherFocusReason)


class ActivationDialog(QDialog):
    def __init__(self, reason: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("DcmGet 软件注册")
        self.setModal(True)
        self.setMinimumSize(620, 420)

        layout = QVBoxLayout(self)
        title = QLabel("软件注册")
        title.setObjectName("PageTitle")
        description = QLabel("请把机器码发给授权人员，再将收到的注册码粘贴到下方。")
        description.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(description)

        machine_row = QHBoxLayout()
        machine_row.addWidget(QLabel("机器码"))
        self.machine_code_edit = QLineEdit(machine_code())
        self.machine_code_edit.setReadOnly(True)
        self.machine_code_edit.setAccessibleName("本机机器码")
        copy_button = QPushButton("复制机器码")
        copy_button.clicked.connect(self._copy_machine_code)
        machine_row.addWidget(self.machine_code_edit, 1)
        machine_row.addWidget(copy_button)
        layout.addLayout(machine_row)

        layout.addWidget(QLabel("注册码"))
        self.token_edit = QPlainTextEdit()
        self.token_edit.setPlaceholderText("粘贴以 DGM1. 开头的注册码")
        self.token_edit.setAccessibleName("注册码")
        layout.addWidget(self.token_edit, 1)

        self.error_label = QLabel(reason)
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(bool(reason))
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText("完成注册")
        buttons.button(QDialogButtonBox.Cancel).setText("退出")
        buttons.accepted.connect(self._activate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _copy_machine_code(self) -> None:
        QApplication.clipboard().setText(self.machine_code_edit.text())

    def _activate(self) -> None:
        try:
            save_license(self.token_edit.toPlainText())
        except (OSError, LicenseError) as exc:
            self.error_label.setText(str(exc))
            self.error_label.show()
            return
        self.accept()


def authorize_gui(resume_task_id: str | None = None) -> bool:
    if DailyPasswordDialog().exec_() != QDialog.Accepted:
        return False
    try:
        load_license()
        return True
    except (OSError, LicenseError):
        trial = trial_status()
        if trial.remaining > 0:
            return True
        if resume_task_id:
            try:
                if trial_task_consumed(resume_task_id):
                    return True
            except LicenseError:
                pass
        return (
            ActivationDialog("30 次免费试用已用完，请输入注册码。").exec_()
            == QDialog.Accepted
        )


def entitlement_text() -> str:
    try:
        info = load_license()
        return f"已注册 · {info.customer}"
    except (OSError, LicenseError):
        trial = trial_status()
        return f"免费试用剩余 {trial.remaining} 次"


def activate_gui(parent: QWidget | None = None) -> bool:
    accepted = ActivationDialog(parent=parent).exec_() == QDialog.Accepted
    return accepted


def prepare_download_entitlement(
    parent: QWidget | None = None,
) -> tuple[bool, bool, str]:
    try:
        load_license()
        return True, False, "已注册版本"
    except (OSError, LicenseError):
        trial = trial_status()
        if trial.remaining <= 0:
            accepted = (
                ActivationDialog("30 次免费试用已用完，请输入注册码。", parent).exec_()
                == QDialog.Accepted
            )
            return accepted, False, "已完成软件注册" if accepted else ""
        return True, True, f"免费试用剩余 {trial.remaining} 次"
