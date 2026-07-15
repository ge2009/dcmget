from __future__ import annotations

import html
import os
from pathlib import Path

from PyQt5.QtCore import QObject, QSettings, Qt, QThread, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QCloseEvent, QDesktopServices, QIcon, QKeySequence
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QShortcut,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QFileDialog,
    QHeaderView,
    QAbstractItemView,
)

from . import __version__
from .auth_ui import activate_gui, entitlement_text, prepare_download_entitlement
from .config import (
    ANONYMIZATION_PROFILE_OPTIONS,
    DEFAULT_ANONYMIZATION_PROFILE,
    AppConfig,
    DIRECTORY_TEMPLATES,
    load_config,
    parse_accessions,
    save_config,
)
from .core import (
    AccessionResult,
    AccessionStatus,
    BatchSummary,
    DcmtkResolver,
    DownloadRunner,
    PreflightResult,
    ToolPaths,
    log_directory,
    preflight,
)
from .licensing import consume_trial


COLORS = {
    "primary": "#0369A1",
    "primary_hover": "#075985",
    "success": "#047857",
    "warning": "#B45309",
    "danger": "#B91C1C",
    "background": "#F8FAFC",
    "surface": "#FFFFFF",
    "text": "#0F172A",
    "muted": "#475569",
    "border": "#CBD5E1",
}


class AccessionTextEdit(QPlainTextEdit):
    file_dropped = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setPlaceholderText("每行一个检查号；也可以拖入 TXT 文件")
        self.setMinimumHeight(92)
        self.setAccessibleName("检查号输入")

    def dragEnterEvent(self, event):  # type: ignore[override]
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile() and urls[0].toLocalFile().lower().endswith(".txt"):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):  # type: ignore[override]
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            self.file_dropped.emit(urls[0].toLocalFile())
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class SettingsPage(QWidget):
    saved = pyqtSignal(object)
    back_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._config = AppConfig()
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 24)
        outer.setSpacing(16)

        title_row = QHBoxLayout()
        back = QToolButton()
        back.setIcon(self.style().standardIcon(QStyle.SP_ArrowBack))
        back.setToolTip("返回任务主页")
        back.clicked.connect(self.back_requested)
        title = QLabel("连接、接收与匿名设置")
        title.setObjectName("PageTitle")
        title_row.addWidget(back)
        title_row.addWidget(title)
        title_row.addStretch()
        outer.addLayout(title_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(16)

        dcmtk_card, dcmtk_form = self._card("DCMTK 工具")
        dcmtk_row = QWidget()
        dcmtk_row_layout = QHBoxLayout(dcmtk_row)
        dcmtk_row_layout.setContentsMargins(0, 0, 0, 0)
        self.dcmtk_edit = QLineEdit()
        self.dcmtk_edit.setPlaceholderText("留空时自动检测系统 PATH 或 .runtime")
        browse = QPushButton("选择目录")
        browse.clicked.connect(self._browse_dcmtk)
        dcmtk_row_layout.addWidget(self.dcmtk_edit, 1)
        dcmtk_row_layout.addWidget(browse)
        dcmtk_form.addRow("bin 目录", dcmtk_row)
        self.dcmtk_hint = QLabel("尚未检测")
        self.dcmtk_hint.setObjectName("FieldHint")
        dcmtk_form.addRow("工具状态", self.dcmtk_hint)

        pacs_card, pacs_form = self._card("PACS 连接")
        self.pacs_host_edit = QLineEdit()
        self.pacs_port_spin = self._port_spin()
        self.calling_ae_edit = QLineEdit()
        self.pacs_ae_edit = QLineEdit()
        for widget in (self.calling_ae_edit, self.pacs_ae_edit):
            widget.setMaxLength(16)
        pacs_form.addRow("PACS 地址", self.pacs_host_edit)
        pacs_form.addRow("PACS 端口", self.pacs_port_spin)
        pacs_form.addRow("本机调用 AE", self.calling_ae_edit)
        pacs_form.addRow("PACS AE", self.pacs_ae_edit)

        receiver_card, receiver_form = self._card("DICOM 接收器")
        self.storage_ae_edit = QLineEdit()
        self.storage_ae_edit.setMaxLength(16)
        self.storage_port_spin = self._port_spin()
        self.directory_template_combo = QComboBox()
        self.directory_template_combo.setEditable(True)
        self.directory_template_combo.addItems(DIRECTORY_TEMPLATES)
        self.directory_template_combo.setToolTip(
            "可组合 {PatientID}、{AccessionNumber}、{StudyInstanceUID}"
        )
        self.log_size_spin = QSpinBox()
        self.log_size_spin.setRange(1, 4096)
        self.log_size_spin.setSuffix(" MB")
        receiver_form.addRow("接收 AE", self.storage_ae_edit)
        receiver_form.addRow("监听端口", self.storage_port_spin)
        receiver_form.addRow("目录模板", self.directory_template_combo)
        directory_hint = QLabel(
            "可编辑组合：{PatientID}、{AccessionNumber}、{StudyInstanceUID}"
        )
        directory_hint.setObjectName("FieldHint")
        directory_hint.setWordWrap(True)
        receiver_form.addRow("", directory_hint)
        receiver_form.addRow("单个日志上限", self.log_size_spin)

        anonymization_card, anonymization_form = self._card("下载后匿名处理")
        self.anonymization_enabled_checkbox = QCheckBox(
            "归档前自动处理 DICOM 元数据"
        )
        self.anonymization_profile_combo = QComboBox()
        self.anonymization_profile_combo.setAccessibleName("匿名方案")
        for profile_id, label, _description in ANONYMIZATION_PROFILE_OPTIONS:
            self.anonymization_profile_combo.addItem(label, profile_id)
        self.anonymization_profile_hint = QLabel()
        self.anonymization_profile_hint.setObjectName("FieldHint")
        self.anonymization_profile_hint.setWordWrap(True)
        self.anonymization_warning = QLabel(
            "归档成功后不保留原始副本。仅处理 DICOM 元数据，不会清除像素中烧录的文字、人脸特征，"
            "研究/严格方案会拒绝已标记的烧录或可识别视觉特征，以及 PDF、SR、图形标注、缩略图和叠加层。"
        )
        self.anonymization_warning.setObjectName("WarningText")
        self.anonymization_warning.setWordWrap(True)
        anonymization_form.addRow("启用匿名", self.anonymization_enabled_checkbox)
        anonymization_form.addRow("匿名方案", self.anonymization_profile_combo)
        anonymization_form.addRow("", self.anonymization_profile_hint)
        anonymization_form.addRow("", self.anonymization_warning)
        self.anonymization_enabled_checkbox.toggled.connect(
            self._update_anonymization_state
        )
        self.anonymization_profile_combo.currentIndexChanged.connect(
            self._update_anonymization_state
        )

        self.error_label = QLabel()
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        content_layout.addWidget(dcmtk_card)
        content_layout.addWidget(pacs_card)
        content_layout.addWidget(receiver_card)
        content_layout.addWidget(anonymization_card)
        content_layout.addWidget(self.error_label)
        content_layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.back_requested)
        save = QPushButton("保存设置")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        outer.addLayout(buttons)

        self._field_widgets = {
            "dcmtk_bin_dir": self.dcmtk_edit,
            "pacs_server_ip": self.pacs_host_edit,
            "pacs_server_port": self.pacs_port_spin,
            "calling_ae_title": self.calling_ae_edit,
            "pacs_ae_title": self.pacs_ae_edit,
            "storage_ae_title": self.storage_ae_edit,
            "storage_port": self.storage_port_spin,
            "directory_template": self.directory_template_combo,
            "anonymization_profile": self.anonymization_profile_combo,
            "max_log_file_size_bytes": self.log_size_spin,
        }

    def _card(self, title: str) -> tuple[QFrame, QFormLayout]:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        heading = QLabel(title)
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(12)
        layout.addLayout(form)
        return card, form

    @staticmethod
    def _port_spin() -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(1, 65535)
        spin.setButtonSymbols(QSpinBox.PlusMinus)
        return spin

    def set_config(self, config: AppConfig) -> None:
        self._config = config
        self.dcmtk_edit.setText(config.dcmtk_bin_dir)
        self.pacs_host_edit.setText(config.pacs_server_ip)
        self.pacs_port_spin.setValue(config.pacs_server_port)
        self.calling_ae_edit.setText(config.calling_ae_title)
        self.pacs_ae_edit.setText(config.pacs_ae_title)
        self.storage_ae_edit.setText(config.storage_ae_title)
        self.storage_port_spin.setValue(config.storage_port)
        self.directory_template_combo.setCurrentText(config.directory_template)
        self.anonymization_enabled_checkbox.setChecked(config.anonymization_enabled)
        profile_index = self.anonymization_profile_combo.findData(
            config.anonymization_profile
        )
        if profile_index < 0:
            profile_index = self.anonymization_profile_combo.findData(
                DEFAULT_ANONYMIZATION_PROFILE
            )
        self.anonymization_profile_combo.setCurrentIndex(profile_index)
        self._update_anonymization_state()
        self.log_size_spin.setValue(max(1, config.max_log_file_size_bytes // (1024 * 1024)))
        self.apply_errors({})

    def config(self) -> AppConfig:
        return AppConfig(
            dcmtk_bin_dir=self.dcmtk_edit.text().strip(),
            access_numbers_file_path=self._config.access_numbers_file_path,
            dicom_destination_folder=self._config.dicom_destination_folder,
            pacs_server_ip=self.pacs_host_edit.text().strip(),
            pacs_server_port=self.pacs_port_spin.value(),
            calling_ae_title=self.calling_ae_edit.text().strip(),
            pacs_ae_title=self.pacs_ae_edit.text().strip(),
            storage_ae_title=self.storage_ae_edit.text().strip(),
            storage_port=self.storage_port_spin.value(),
            directory_template=self.directory_template_combo.currentText().strip(),
            anonymization_enabled=self.anonymization_enabled_checkbox.isChecked(),
            anonymization_profile=str(
                self.anonymization_profile_combo.currentData()
                or DEFAULT_ANONYMIZATION_PROFILE
            ),
            max_log_file_size_bytes=self.log_size_spin.value() * 1024 * 1024,
        )

    def _update_anonymization_state(self, _value=None) -> None:
        enabled = self.anonymization_enabled_checkbox.isChecked()
        self.anonymization_profile_combo.setEnabled(enabled)
        profile_id = str(
            self.anonymization_profile_combo.currentData()
            or DEFAULT_ANONYMIZATION_PROFILE
        )
        descriptions = {
            item_id: description
            for item_id, _label, description in ANONYMIZATION_PROFILE_OPTIONS
        }
        self.anonymization_profile_hint.setText(
            "元数据处理：" + descriptions.get(profile_id, "请选择匿名方案")
        )

    def apply_errors(self, errors: dict[str, str]) -> None:
        for field, widget in self._field_widgets.items():
            message = errors.get(field, "")
            widget.setProperty("invalid", bool(message))
            widget.setToolTip(message)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        messages = list(dict.fromkeys(errors.values()))
        self.error_label.setText("\n".join(messages))
        self.error_label.setVisible(bool(messages))

    def set_dcmtk_status(self, text: str, ok: bool) -> None:
        self.dcmtk_hint.setText(text)
        self.dcmtk_hint.setProperty("status", "ok" if ok else "error")
        self.dcmtk_hint.style().unpolish(self.dcmtk_hint)
        self.dcmtk_hint.style().polish(self.dcmtk_hint)

    def _browse_dcmtk(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择 DCMTK bin 目录", self.dcmtk_edit.text())
        if selected:
            self.dcmtk_edit.setText(selected)

    def _save(self) -> None:
        config = self.config()
        errors = config.validate()
        self.apply_errors(errors)
        if errors:
            return
        self.saved.emit(config)


class DownloadWorker(QObject):
    log = pyqtSignal(str, str, str)
    state = pyqtSignal(str)
    progress = pyqtSignal(int, int, object)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    trial_consumed = pyqtSignal(str)

    def __init__(
        self,
        config: AppConfig,
        tools: ToolPaths,
        accessions: list[str],
        consume_trial_on_ready: bool = False,
    ):
        super().__init__()
        self.config = config
        self.tools = tools
        self.accessions = accessions
        self.consume_trial_on_ready = consume_trial_on_ready
        self.runner: DownloadRunner | None = None
        self.cancel_requested = False

    @pyqtSlot()
    def run(self) -> None:
        try:
            self.runner = DownloadRunner(
                self.config,
                self.tools,
                log_callback=self.log.emit,
                state_callback=self.state.emit,
                progress_callback=self.progress.emit,
                ready_callback=(
                    self._consume_trial if self.consume_trial_on_ready else None
                ),
            )
            if self.cancel_requested:
                self.runner.request_cancel()
            self.finished.emit(self.runner.run(self.accessions))
        except Exception as exc:  # keep worker failures visible in the UI
            self.failed.emit(str(exc))

    def request_cancel(self) -> None:
        self.cancel_requested = True
        if self.runner:
            self.runner.request_cancel()

    def _consume_trial(self) -> None:
        trial = consume_trial()
        self.trial_consumed.emit(f"本次使用免费试用，剩余 {trial.remaining} 次")


class DcmGetWindow(QMainWindow):
    def __init__(self, config_path: str | Path, project_root: str | Path):
        super().__init__()
        self.config_path = Path(config_path)
        self.project_root = Path(project_root)
        self.config = load_config(self.config_path)
        self.resolver = DcmtkResolver(self.project_root)
        self.tools: ToolPaths | None = None
        self.worker: DownloadWorker | None = None
        self.worker_thread: QThread | None = None
        self.last_summary: BatchSummary | None = None
        self._closing_after_cancel = False
        self.current_accessions: list[str] = []
        self.row_by_accession: dict[str, int] = {}
        self.settings_store = QSettings("DcmGet", "DcmGet2")
        self._log_panel_expanded = self.settings_store.value(
            "window/log_expanded", True, type=bool
        )
        self._task_form_expanded = self.settings_store.value(
            "window/task_form_expanded", True, type=bool
        )

        self.setWindowTitle(f"DcmGet {__version__} - DICOM 下载工作台")
        self.setMinimumSize(1024, 720)
        self.resize(1180, 820)
        logo = self.project_root / "logo.png"
        if logo.exists():
            self.setWindowIcon(QIcon(str(logo)))
        self._build_ui()
        self._restore_ui_state()
        self.settings_page.set_config(self.config)
        last_destination = self.settings_store.value("task/destination", "", type=str)
        self.destination_edit.setText(last_destination or self.config.dicom_destination_folder)
        self._load_configured_accessions()
        self._update_accession_preview()
        QTimer.singleShot(0, self._refresh_tool_status)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_header())

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_task_page())
        self.settings_page = SettingsPage()
        self.settings_page.saved.connect(self._save_settings)
        self.settings_page.back_requested.connect(self._cancel_settings)
        self.pages.addWidget(self.settings_page)
        root_layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self.setStyleSheet(APP_STYLESHEET)

        QShortcut(QKeySequence("Ctrl+O"), self, activated=self._choose_accession_file)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._start_download)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self._show_settings)

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("Header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 14, 24, 14)
        title_box = QVBoxLayout()
        title = QLabel(f"DcmGet {__version__}")
        title.setObjectName("AppTitle")
        subtitle = QLabel("DICOM 批量下载工作台")
        subtitle.setObjectName("HeaderSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        layout.addLayout(title_box)
        layout.addStretch()
        self.tool_status = QLabel("正在检测 DCMTK…")
        self.tool_status.setObjectName("StatusPill")
        self.tool_status.setProperty("status", "pending")
        layout.addWidget(self.tool_status)
        self.entitlement_status = QLabel()
        self.entitlement_status.setObjectName("StatusPill")
        self._refresh_entitlement_status()
        layout.addWidget(self.entitlement_status)
        self.registration_button = QToolButton()
        self.registration_button.setText("软件注册")
        self.registration_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.registration_button.clicked.connect(self._show_activation)
        layout.addWidget(self.registration_button)
        self.settings_button = QToolButton()
        self.settings_button.setText("设置")
        self.settings_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.settings_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.settings_button.setToolTip("连接、接收与匿名设置（Ctrl+,）")
        self.settings_button.clicked.connect(self._show_settings)
        layout.addWidget(self.settings_button)
        return header

    def _build_task_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(14)

        input_card = QFrame()
        input_card.setObjectName("Card")
        input_layout = QVBoxLayout(input_card)
        input_header = QHBoxLayout()
        section = QLabel("新建下载任务")
        section.setObjectName("SectionTitle")
        input_header.addWidget(section)
        input_header.addStretch()
        self.task_form_toggle_button = QToolButton()
        self.task_form_toggle_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.task_form_toggle_button.setAccessibleName("展开或收起新建任务")
        self.task_form_toggle_button.clicked.connect(self._toggle_task_form)
        input_header.addWidget(self.task_form_toggle_button)
        input_layout.addLayout(input_header)

        self.task_form_body = QWidget()
        grid = QGridLayout(self.task_form_body)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(10)
        grid.addWidget(QLabel("检查号"), 0, 0, Qt.AlignTop)
        self.accession_edit = AccessionTextEdit()
        self.accession_edit.textChanged.connect(self._update_accession_preview)
        self.accession_edit.file_dropped.connect(self._load_accession_file)
        grid.addWidget(self.accession_edit, 0, 1, 1, 2)
        self.accession_button = QPushButton("选择 TXT")
        self.accession_button.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.accession_button.clicked.connect(self._choose_accession_file)
        self.accession_summary = QLabel("有效 0 · 空行 0 · 重复 0")
        self.accession_summary.setObjectName("FieldHint")
        helper_row = QHBoxLayout()
        helper_row.addWidget(self.accession_button)
        helper_row.addWidget(self.accession_summary)
        helper_row.addStretch()
        grid.addLayout(helper_row, 1, 1, 1, 2)

        grid.addWidget(QLabel("保存目录"), 2, 0)
        self.destination_edit = QLineEdit()
        self.destination_edit.setAccessibleName("DICOM 保存目录")
        grid.addWidget(self.destination_edit, 2, 1)
        self.destination_button = QPushButton("选择目录")
        self.destination_button.clicked.connect(self._choose_destination)
        grid.addWidget(self.destination_button, 2, 2)
        grid.setColumnStretch(1, 1)
        input_layout.addWidget(self.task_form_body)
        self._set_task_form_expanded(self._task_form_expanded)
        layout.addWidget(input_card)

        preflight_card = QFrame()
        preflight_card.setObjectName("Card")
        preflight_layout = QHBoxLayout(preflight_card)
        preflight_title = QLabel("启动预检")
        preflight_title.setObjectName("SectionTitle")
        preflight_layout.addWidget(preflight_title)
        self.preflight_labels: list[QLabel] = []
        for text in ("DCMTK 待检测", "保存目录待检测", "端口待检测", "PACS 待检测"):
            label = QLabel(text)
            label.setObjectName("CheckPill")
            label.setProperty("status", "pending")
            self.preflight_labels.append(label)
            preflight_layout.addWidget(label)
        preflight_layout.addStretch()
        layout.addWidget(preflight_card)

        action_row = QHBoxLayout()
        self.progress_label = QLabel("尚未开始")
        self.progress_label.setObjectName("ProgressText")
        action_row.addWidget(self.progress_label)
        action_row.addStretch()
        self.retry_button = QPushButton("重试失败项")
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self._retry_failed)
        self.log_toggle_button = QPushButton(
            "收起日志" if self._log_panel_expanded else "展开日志"
        )
        self.log_toggle_button.clicked.connect(self._toggle_log_panel)
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("DangerButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_download)
        self.start_button = QPushButton("开始下载")
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.setToolTip("开始下载（Ctrl+Enter）")
        self.start_button.clicked.connect(self._start_download)
        action_row.addWidget(self.retry_button)
        action_row.addWidget(self.log_toggle_button)
        action_row.addWidget(self.stop_button)
        action_row.addWidget(self.start_button)
        layout.addLayout(action_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)

        self.task_splitter = QSplitter(Qt.Vertical)
        self.task_table = QTableWidget(0, 5)
        self.task_table.setHorizontalHeaderLabels(["检查号", "状态", "文件数", "耗时", "详情"])
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.task_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.task_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.task_table.setAlternatingRowColors(True)
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.task_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.task_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.task_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.task_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.task_table.doubleClicked.connect(self._open_selected_result)
        self.task_splitter.addWidget(self.task_table)
        self.log_panel = self._build_log_panel()
        self.task_splitter.addWidget(self.log_panel)
        self.log_panel.setVisible(self._log_panel_expanded)
        self.task_splitter.setSizes([360, 180])
        layout.addWidget(self.task_splitter, 1)
        return page

    def _build_log_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Card")
        layout = QVBoxLayout(panel)
        header = QHBoxLayout()
        title = QLabel("运行日志")
        title.setObjectName("SectionTitle")
        header.addWidget(title)
        header.addStretch()
        open_result = QPushButton("打开结果")
        open_result.clicked.connect(self._open_selected_result)
        copy_error = QPushButton("复制详情")
        copy_error.clicked.connect(self._copy_selected_detail)
        clear = QPushButton("清空日志")
        clear.clicked.connect(lambda: self.log_edit.clear())
        open_logs = QPushButton("日志目录")
        open_logs.clicked.connect(self._open_log_directory)
        for button in (open_result, copy_error, clear, open_logs):
            header.addWidget(button)
        layout.addLayout(header)
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.document().setMaximumBlockCount(5000)
        self.log_edit.setAccessibleName("运行日志")
        layout.addWidget(self.log_edit)
        return panel

    def _show_settings(self) -> None:
        if self.worker:
            return
        self.settings_page.set_config(self.config)
        self.pages.setCurrentIndex(1)

    def _cancel_settings(self) -> None:
        self.settings_page.set_config(self.config)
        self.pages.setCurrentIndex(0)

    def _show_activation(self) -> None:
        if self.worker:
            return
        if activate_gui(self):
            self._refresh_entitlement_status()
            self._append_log("授权", "软件注册成功", "success")

    def _refresh_entitlement_status(self) -> None:
        text = entitlement_text()
        self.entitlement_status.setText(text)
        self.entitlement_status.setProperty(
            "status", "ok" if text.startswith("已注册") else "warning"
        )
        self.entitlement_status.style().unpolish(self.entitlement_status)
        self.entitlement_status.style().polish(self.entitlement_status)

    def _save_settings(self, config: AppConfig) -> None:
        config.access_numbers_file_path = self.config.access_numbers_file_path
        config.dicom_destination_folder = self.destination_edit.text().strip()
        self.config = config
        save_config(self.config_path, self.config)
        self.pages.setCurrentIndex(0)
        self._refresh_tool_status()
        self._append_log("应用", "设置已保存", "success")

    def _refresh_tool_status(self) -> None:
        try:
            self.tools = self.resolver.resolve(self.config.dcmtk_bin_dir)
            text = f"DCMTK {self.tools.version} 已就绪"
            self._set_tool_status(text, "ok")
            self.settings_page.set_dcmtk_status(text, True)
        except Exception as exc:
            self.tools = None
            self._set_tool_status("DCMTK 未就绪", "error")
            self.settings_page.set_dcmtk_status(str(exc), False)

    def _set_tool_status(self, text: str, status: str) -> None:
        self.tool_status.setText(text)
        self.tool_status.setProperty("status", status)
        self.tool_status.style().unpolish(self.tool_status)
        self.tool_status.style().polish(self.tool_status)

    def _load_configured_accessions(self) -> None:
        path = Path(self.config.access_numbers_file_path).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        if path.exists():
            try:
                self.accession_edit.setPlainText(path.read_text(encoding="utf-8-sig"))
            except OSError:
                pass

    def _choose_accession_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择检查号文件",
            self.config.access_numbers_file_path,
            "文本文件 (*.txt);;所有文件 (*)",
        )
        if selected:
            self._load_accession_file(selected)

    def _load_accession_file(self, path: str) -> None:
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except OSError as exc:
            QMessageBox.critical(self, "无法读取文件", str(exc))
            return
        self.config.access_numbers_file_path = path
        self.accession_edit.setPlainText(text)
        self._append_log("应用", f"已载入检查号文件：{path}", "info")

    def _choose_destination(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择 DICOM 保存目录", self.destination_edit.text()
        )
        if selected:
            self.destination_edit.setText(selected)

    def _update_accession_preview(self) -> None:
        parsed = parse_accessions(self.accession_edit.toPlainText())
        self.current_accessions = parsed.values
        self.accession_summary.setText(
            f"有效 {len(parsed.values)} · 空行 {parsed.blank_count} · 重复 {parsed.duplicate_count}"
        )
        if self.worker:
            return
        self._populate_waiting_rows(parsed.values)

    def _populate_waiting_rows(self, accessions: list[str]) -> None:
        self.task_table.setRowCount(len(accessions))
        self.row_by_accession = {}
        for row, accession in enumerate(accessions):
            self.row_by_accession[accession] = row
            self.task_table.setItem(row, 0, QTableWidgetItem(accession))
            self._set_result_row(
                AccessionResult(accession=accession, status=AccessionStatus.WAITING), row
            )

    def _start_download(self, override: list[str] | None = None) -> None:
        if self.worker:
            return
        accessions = override or self.current_accessions
        if not accessions:
            self._append_log("应用", "请先导入至少一个检查号", "error")
            QMessageBox.warning(self, "没有检查号", "请选择 TXT 文件或粘贴检查号后再开始。")
            return

        self.config.access_numbers_file_path = self.config.access_numbers_file_path or "access.txt"
        self.config.dicom_destination_folder = self.destination_edit.text().strip()
        check = preflight(self.config, self.resolver)
        self._show_preflight(check)
        self.settings_page.apply_errors(check.errors)
        if not check.ok or check.tools is None:
            self._append_log("预检", "启动预检未通过，请修正标红设置", "error")
            QMessageBox.warning(self, "预检未通过", "请查看启动预检和设置页中的错误提示。")
            if any(key != "dicom_destination_folder" for key in check.errors):
                self.pages.setCurrentIndex(1)
            return

        entitled, use_trial, entitlement_message = prepare_download_entitlement(self)
        if not entitled:
            if entitlement_message:
                self._append_log("授权", entitlement_message, "error")
                QMessageBox.warning(self, "无法开始下载", entitlement_message)
            return
        if entitlement_message == "已完成软件注册":
            self._refresh_entitlement_status()
            self._append_log("授权", entitlement_message, "success")

        save_config(self.config_path, self.config)
        self.tools = check.tools
        self._populate_waiting_rows(accessions)
        self.progress_bar.setValue(0)
        self.progress_label.setText(f"准备下载 0/{len(accessions)}")
        self._set_running(True)

        thread = QThread(self)
        worker = DownloadWorker(
            self.config,
            check.tools,
            list(accessions),
            consume_trial_on_ready=use_trial,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log.connect(self._append_log)
        worker.state.connect(self._on_worker_state)
        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(self._on_worker_finished)
        worker.failed.connect(self._on_worker_failed)
        worker.trial_consumed.connect(self._on_trial_consumed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.worker_thread = thread
        self.worker = worker
        thread.start()

    def _on_trial_consumed(self, message: str) -> None:
        self._refresh_entitlement_status()
        self._append_log("授权", message, "info")

    def _show_preflight(self, check: PreflightResult) -> None:
        for label, (name, ok, message) in zip(self.preflight_labels, check.checks):
            label.setText(f"{name}：{message}")
            label.setProperty("status", "ok" if ok else "error")
            label.setToolTip(message)
            label.style().unpolish(label)
            label.style().polish(label)

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.retry_button.setEnabled(False if running else bool(self.last_summary and self.last_summary.failed_accessions))
        self.accession_edit.setReadOnly(running)
        self.destination_edit.setReadOnly(running)
        self.accession_button.setEnabled(not running)
        self.destination_button.setEnabled(not running)
        self.settings_button.setEnabled(not running)
        self.registration_button.setEnabled(not running)

    def _on_worker_state(self, state: str) -> None:
        labels = {
            "starting_receiver": "正在启动 DICOM 接收器…",
            "downloading": "接收器已就绪，正在下载…",
            "stopping": "正在停止后台进程…",
            "completed": "任务已完成",
            "partial": "任务完成，部分检查号失败",
            "cancelled": "任务已取消",
        }
        self.progress_label.setText(labels.get(state, state))
        if state in {"starting_receiver", "stopping"}:
            self.progress_bar.setRange(0, 0)
        elif state == "downloading" and self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 100)

    def _on_worker_progress(self, index: int, total: int, result: AccessionResult) -> None:
        self.progress_bar.setRange(0, total)
        completed = index - 1 if result.status == AccessionStatus.DOWNLOADING else index
        self.progress_bar.setValue(completed)
        self.progress_label.setText(
            f"{index}/{total} · {result.accession} · {result.status.value} · {result.file_count} 个文件"
        )
        self._set_result_row(result)

    def _set_result_row(self, result: AccessionResult, row: int | None = None) -> None:
        target_row = self.row_by_accession.get(result.accession) if row is None else row
        if target_row is None:
            return
        status_item = QTableWidgetItem(result.status.value)
        color = {
            AccessionStatus.COMPLETED: COLORS["success"],
            AccessionStatus.NO_DATA: COLORS["warning"],
            AccessionStatus.PARTIAL: COLORS["warning"],
            AccessionStatus.FAILED: COLORS["danger"],
            AccessionStatus.CANCELLED: COLORS["muted"],
            AccessionStatus.DOWNLOADING: COLORS["primary"],
        }.get(result.status, COLORS["muted"])
        status_item.setForeground(QColor(color))
        icon = {
            AccessionStatus.WAITING: QStyle.SP_FileDialogInfoView,
            AccessionStatus.DOWNLOADING: QStyle.SP_BrowserReload,
            AccessionStatus.COMPLETED: QStyle.SP_DialogApplyButton,
            AccessionStatus.NO_DATA: QStyle.SP_MessageBoxWarning,
            AccessionStatus.PARTIAL: QStyle.SP_MessageBoxWarning,
            AccessionStatus.FAILED: QStyle.SP_MessageBoxCritical,
            AccessionStatus.CANCELLED: QStyle.SP_DialogCancelButton,
        }.get(result.status)
        if icon is not None:
            status_item.setIcon(self.style().standardIcon(icon))
        status_item.setData(Qt.UserRole, result.output_directory)
        self.task_table.setItem(target_row, 1, status_item)
        self.task_table.setItem(target_row, 2, QTableWidgetItem(str(result.file_count)))
        duration = f"{result.duration_seconds:.1f}s" if result.duration_seconds else "—"
        self.task_table.setItem(target_row, 3, QTableWidgetItem(duration))
        self.task_table.setItem(target_row, 4, QTableWidgetItem(result.message or "等待处理"))

    def _on_worker_finished(self, summary: BatchSummary) -> None:
        self.last_summary = summary
        self._finish_worker()
        if self._closing_after_cancel:
            return
        if summary.cancelled:
            title, message = "任务已取消", "已停止下载，已收到的 DICOM 文件已保留。"
        elif summary.exit_code == 2:
            title = "任务部分完成"
            message = f"有 {len(summary.failed_accessions)} 个检查号失败，可点击“重试失败项”。"
        else:
            title, message = "下载完成", "所有检查号均已处理完成。"
        QMessageBox.information(self, title, message)

    def _on_worker_failed(self, message: str) -> None:
        self._append_log("应用", message, "error")
        self.progress_label.setText("任务启动失败")
        self._finish_worker()
        if self._closing_after_cancel:
            return
        QMessageBox.critical(self, "下载失败", message)

    def _finish_worker(self) -> None:
        self.worker = None
        self.worker_thread = None
        self.progress_bar.setRange(0, max(1, len(self.current_accessions)))
        self._set_running(False)

    def _stop_download(self) -> None:
        if not self.worker:
            return
        answer = QMessageBox.question(
            self,
            "停止下载",
            "确定停止当前任务吗？已收到的文件会保留。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.stop_button.setEnabled(False)
            self.progress_label.setText("正在停止…")
            self.worker.request_cancel()

    def _retry_failed(self) -> None:
        if self.last_summary:
            failed = self.last_summary.failed_accessions
            if failed:
                self._start_download(failed)

    def _append_log(self, source: str, message: str, level: str) -> None:
        colors = {
            "debug": COLORS["muted"],
            "info": COLORS["text"],
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }
        safe_source = html.escape(source)
        safe_message = html.escape(message)
        color = colors.get(level, COLORS["text"])
        self.log_edit.append(
            f'<span style="color:{COLORS["muted"]}">[{safe_source}]</span> '
            f'<span style="color:{color}">{safe_message}</span>'
        )
        scrollbar = self.log_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _selected_result_directory(self) -> str:
        row = self.task_table.currentRow()
        if row < 0:
            return ""
        item = self.task_table.item(row, 1)
        return str(item.data(Qt.UserRole) or "") if item else ""

    def _open_selected_result(self) -> None:
        directory = self._selected_result_directory() or self.destination_edit.text()
        path = Path(directory).expanduser()
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _copy_selected_detail(self) -> None:
        row = self.task_table.currentRow()
        if row >= 0:
            values = [self.task_table.item(row, column) for column in range(self.task_table.columnCount())]
            QApplication.clipboard().setText(" | ".join(item.text() if item else "" for item in values))

    def _open_log_directory(self) -> None:
        config = AppConfig.from_dict(self.config.to_dict())
        config.dicom_destination_folder = self.destination_edit.text().strip()
        path = log_directory(config)
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _toggle_log_panel(self) -> None:
        self._log_panel_expanded = not self._log_panel_expanded
        self.log_panel.setVisible(self._log_panel_expanded)
        self.log_toggle_button.setText(
            "收起日志" if self._log_panel_expanded else "展开日志"
        )

    def _set_task_form_expanded(self, expanded: bool) -> None:
        self._task_form_expanded = expanded
        self.task_form_body.setVisible(expanded)
        self.task_form_toggle_button.setArrowType(
            Qt.DownArrow if expanded else Qt.RightArrow
        )
        self.task_form_toggle_button.setText("收起" if expanded else "展开")
        self.task_form_toggle_button.setToolTip(
            "收起检查号和保存目录" if expanded else "展开新建任务输入"
        )

    def _toggle_task_form(self) -> None:
        self._set_task_form_expanded(not self._task_form_expanded)

    def _restore_ui_state(self) -> None:
        geometry = self.settings_store.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        splitter = self.settings_store.value("window/splitter")
        if splitter:
            self.task_splitter.restoreState(splitter)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker:
            if self._closing_after_cancel:
                event.ignore()
                return
            answer = QMessageBox.question(
                self,
                "退出 DcmGet",
                "下载仍在进行。停止任务并退出吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self._closing_after_cancel = True
            self.worker.request_cancel()
            if self.worker_thread:
                self.worker_thread.finished.connect(self.close)
            self.progress_label.setText("正在停止后台进程并退出…")
            event.ignore()
            return
        self.settings_store.setValue("window/geometry", self.saveGeometry())
        self.settings_store.setValue("window/splitter", self.task_splitter.saveState())
        self.settings_store.setValue("window/log_expanded", self._log_panel_expanded)
        self.settings_store.setValue(
            "window/task_form_expanded", self._task_form_expanded
        )
        self.settings_store.setValue("task/destination", self.destination_edit.text().strip())
        self.settings_store.sync()
        event.accept()


APP_STYLESHEET = f"""
QWidget {{
    color: {COLORS['text']};
    font-size: 13px;
}}
QMainWindow, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget {{
    background: {COLORS['background']};
}}
QFrame#Header {{
    background: {COLORS['surface']};
    border-bottom: 1px solid {COLORS['border']};
}}
QFrame#Card {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
}}
QLabel#AppTitle {{ font-size: 20px; font-weight: 700; color: {COLORS['text']}; }}
QLabel#HeaderSubtitle, QLabel#FieldHint {{ color: {COLORS['muted']}; }}
QLabel#PageTitle {{ font-size: 20px; font-weight: 700; }}
QLabel#SectionTitle {{ font-size: 15px; font-weight: 650; }}
QLabel#ProgressText {{ color: {COLORS['muted']}; font-weight: 600; }}
QLabel#ErrorText {{ color: {COLORS['danger']}; background: #FEF2F2; border: 1px solid #FECACA; padding: 9px; border-radius: 6px; }}
QLabel#WarningText {{ color: {COLORS['warning']}; background: #FFF7ED; border: 1px solid #FED7AA; padding: 9px; border-radius: 6px; }}
QLabel#StatusPill, QLabel#CheckPill {{ padding: 6px 10px; border-radius: 12px; background: #F1F5F9; color: {COLORS['muted']}; }}
QLabel#StatusPill[status="ok"], QLabel#CheckPill[status="ok"], QLabel#FieldHint[status="ok"] {{ background: #ECFDF5; color: {COLORS['success']}; }}
QLabel#StatusPill[status="warning"] {{ background: #FFF7ED; color: {COLORS['warning']}; }}
QLabel#StatusPill[status="error"], QLabel#CheckPill[status="error"], QLabel#FieldHint[status="error"] {{ background: #FEF2F2; color: {COLORS['danger']}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox, QTableWidget {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
    selection-background-color: #BAE6FD;
    selection-color: {COLORS['text']};
}}
QLineEdit, QSpinBox, QComboBox {{ padding: 7px 9px; min-height: 20px; }}
QPlainTextEdit, QTextEdit {{ padding: 8px; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus, QTableWidget:focus {{ border: 2px solid #38BDF8; }}
QLineEdit[invalid="true"], QSpinBox[invalid="true"], QComboBox[invalid="true"] {{ border: 2px solid {COLORS['danger']}; background: #FEF2F2; }}
QPushButton, QToolButton {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
    padding: 7px 12px;
}}
QPushButton:hover, QToolButton:hover {{ background: #F1F5F9; border-color: #94A3B8; }}
QPushButton:focus, QToolButton:focus {{ border: 2px solid #38BDF8; }}
QPushButton:disabled, QToolButton:disabled {{ color: #94A3B8; background: #F8FAFC; }}
QPushButton#PrimaryButton {{ background: {COLORS['primary']}; color: white; border-color: {COLORS['primary']}; font-weight: 650; }}
QPushButton#PrimaryButton:hover {{ background: {COLORS['primary_hover']}; }}
QPushButton#DangerButton {{ color: {COLORS['danger']}; border-color: #FCA5A5; }}
QPushButton#DangerButton:hover {{ background: #FEF2F2; }}
QProgressBar {{ border: 0; background: #E2E8F0; border-radius: 4px; min-height: 8px; max-height: 8px; }}
QProgressBar::chunk {{ background: {COLORS['primary']}; border-radius: 4px; }}
QHeaderView::section {{ background: #F1F5F9; color: {COLORS['muted']}; padding: 8px; border: 0; border-bottom: 1px solid {COLORS['border']}; font-weight: 650; }}
QTableWidget {{ gridline-color: #E2E8F0; alternate-background-color: #F8FAFC; }}
QTableWidget::item {{ padding: 6px; }}
QTableWidget::item:selected {{ background: #E0F2FE; color: {COLORS['text']}; }}
QScrollBar:vertical {{ width: 10px; background: transparent; }}
QScrollBar::handle:vertical {{ background: #CBD5E1; border-radius: 5px; min-height: 28px; }}
"""
