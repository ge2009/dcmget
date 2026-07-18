from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QProcess, QStandardPaths, Qt, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .instance_shortcut import (
    InstanceShortcutError,
    ShortcutExistsError,
    build_instance_launch_command,
    create_instance_shortcut,
    default_instance_shortcut_name,
)
from .profile_manager import ProfileInfo, ProfileManager, ProfileManagerError


class ProfileManagerDialog(QDialog):
    """Manage independently launchable one-task DcmGet profiles."""

    def __init__(
        self,
        parent=None,
        *,
        manager: ProfileManager | None = None,
        project_root: str | Path | None = None,
        current_profile_number: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.manager = manager or ProfileManager()
        self.project_root = Path(project_root).resolve() if project_root else None
        self.current_profile_number = current_profile_number
        self._profiles: dict[int, ProfileInfo] = {}
        self.setWindowTitle("Profile 管理")
        self.setMinimumSize(900, 440)
        self.resize(1080, 520)
        self._build_ui()
        self._refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel("一个 Profile 对应一个独立下载任务、接收端口和目标目录。")
        title.setWordWrap(True)
        layout.addWidget(title)
        note = QLabel(
            "复制 Profile 会自动推荐新端口，但不会擅自修改接收 AE。"
            "并行使用前，请在新 Profile 设置中配置独立接收 AE，并同步 PACS 的 AE/端口映射。"
        )
        note.setWordWrap(True)
        note.setObjectName("InlineWarning")
        layout.addWidget(note)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ("名称", "编号", "接收 AE", "端口", "调用 AE", "PACS AE", "目标目录", "状态")
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        for column in range(6):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.Stretch)
        header.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.table.itemDoubleClicked.connect(
            lambda _item, _column: self._launch_selected()
        )
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self._refresh)
        actions.addWidget(self.refresh_button)
        self.clone_button = QPushButton("复制 Profile")
        self.clone_button.clicked.connect(self._clone_selected)
        actions.addWidget(self.clone_button)
        self.rename_button = QPushButton("重命名")
        self.rename_button.clicked.connect(self._rename_selected)
        actions.addWidget(self.rename_button)
        self.delete_button = QPushButton("删除")
        self.delete_button.clicked.connect(self._delete_selected)
        actions.addWidget(self.delete_button)
        actions.addStretch()
        self.open_config_button = QPushButton("打开配置目录")
        self.open_config_button.clicked.connect(self._open_config_directory)
        actions.addWidget(self.open_config_button)
        self.shortcut_button = QPushButton("创建快捷方式")
        self.shortcut_button.clicked.connect(self._create_shortcut)
        actions.addWidget(self.shortcut_button)
        self.launch_button = QPushButton("启动 / 切换")
        self.launch_button.setObjectName("PrimaryButton")
        self.launch_button.clicked.connect(self._launch_selected)
        actions.addWidget(self.launch_button)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        actions.addWidget(close_button)
        layout.addLayout(actions)

    def _refresh(self, select_number: int | None = None) -> None:
        try:
            profiles = self.manager.list_profiles()
        except ProfileManagerError as exc:
            QMessageBox.critical(self, "Profile 管理失败", str(exc))
            return
        previous = select_number if select_number is not None else self._selected_number()
        self._profiles = {profile.number: profile for profile in profiles}
        self.table.setRowCount(len(profiles))
        selected_row = -1
        for row, profile in enumerate(profiles):
            values = (
                profile.display_name,
                str(profile.number),
                profile.storage_ae_title,
                str(profile.storage_port),
                profile.calling_ae_title,
                profile.pacs_ae_title,
                profile.destination_directory,
                self._status_text(profile),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, profile.number)
                if column in {1, 3}:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, column, item)
            if profile.number == previous:
                selected_row = row
        if selected_row < 0 and profiles:
            selected_row = 0
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        self._update_actions()

    @staticmethod
    def _status_text(profile: ProfileInfo) -> str:
        states = []
        if profile.is_running:
            states.append("运行中")
        if profile.has_recovery:
            states.append("有恢复任务")
        return " / ".join(states) or "空闲"

    def _selected_number(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        value = item.data(Qt.UserRole) if item is not None else None
        return int(value) if value is not None else None

    def _selected_profile(self) -> ProfileInfo | None:
        number = self._selected_number()
        return self._profiles.get(number) if number is not None else None

    def _update_actions(self) -> None:
        profile = self._selected_profile()
        enabled = profile is not None
        self.clone_button.setEnabled(enabled)
        self.rename_button.setEnabled(enabled)
        self.shortcut_button.setEnabled(enabled)
        self.launch_button.setEnabled(enabled)
        self.open_config_button.setEnabled(enabled)
        can_delete = bool(
            profile is not None
            and not profile.is_running
            and not profile.has_recovery
        )
        self.delete_button.setEnabled(can_delete)

    def _clone_selected(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        name, accepted = QInputDialog.getText(
            self,
            "复制 Profile",
            "新 Profile 显示名称：",
            text=f"{profile.display_name} 副本",
        )
        if not accepted:
            return
        try:
            result = self.manager.clone_profile(profile.number, display_name=name)
        except ProfileManagerError as exc:
            QMessageBox.critical(self, "复制 Profile 失败", str(exc))
            return
        self._refresh(result.profile.number)
        answer = QMessageBox.question(
            self,
            "Profile 已复制",
            f"已创建 Profile {result.profile.number}，推荐监听端口 {result.recommended_port}。\n\n"
            f"接收 AE 仍为 {result.profile.storage_ae_title}。并行使用前，请启动新 Profile，"
            "在设置中配置独立接收 AE，并同步 PACS 的 AE/端口映射。\n\n现在启动新 Profile 吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self._launch_profile(result.profile)

    def _rename_selected(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        name, accepted = QInputDialog.getText(
            self,
            "重命名 Profile",
            "Profile 显示名称：",
            text=profile.display_name,
        )
        if not accepted:
            return
        try:
            renamed = self.manager.rename_profile(profile.number, name)
        except ProfileManagerError as exc:
            QMessageBox.critical(self, "重命名失败", str(exc))
            return
        self._refresh(renamed.number)

    def _delete_selected(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        answer = QMessageBox.question(
            self,
            "删除 Profile",
            f"确定删除“{profile.display_name}”（Profile {profile.number}）的配置吗？\n\n"
            "下载结果、PDI、日志和目标目录不会删除。此操作无法撤销。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.manager.delete_profile(profile.number)
        except ProfileManagerError as exc:
            QMessageBox.critical(self, "删除 Profile 失败", str(exc))
            return
        self._refresh()

    def _open_config_directory(self) -> None:
        profile = self._selected_profile()
        if profile is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(profile.config_path.parent)))

    def _create_shortcut(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        name, accepted = QInputDialog.getText(
            self,
            "创建 Profile 快捷方式",
            "快捷方式名称：",
            text=default_instance_shortcut_name(
                profile.storage_port, profile.storage_ae_title
            ),
        )
        if not accepted:
            return
        desktop_text = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
        destination = Path(desktop_text) if desktop_text else Path.home() / "Desktop"
        try:
            path = create_instance_shortcut(
                profile.number,
                name,
                destination,
                project_root=self.project_root,
            )
        except ShortcutExistsError as exc:
            if QMessageBox.question(
                self,
                "快捷方式已存在",
                f"{exc.path}\n\n是否覆盖？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            ) != QMessageBox.Yes:
                return
            try:
                path = create_instance_shortcut(
                    profile.number,
                    name,
                    destination,
                    project_root=self.project_root,
                    overwrite=True,
                )
            except InstanceShortcutError as retry_exc:
                QMessageBox.critical(self, "创建快捷方式失败", str(retry_exc))
                return
        except InstanceShortcutError as exc:
            QMessageBox.critical(self, "创建快捷方式失败", str(exc))
            return
        QMessageBox.information(self, "快捷方式已创建", str(path))

    def _launch_selected(self) -> None:
        profile = self._selected_profile()
        if profile is not None:
            self._launch_profile(profile)

    def _launch_profile(self, profile: ProfileInfo) -> None:
        try:
            command = build_instance_launch_command(
                profile.number,
                project_root=self.project_root,
            )
        except InstanceShortcutError as exc:
            QMessageBox.critical(self, "无法启动 Profile", str(exc))
            return
        launch_result = QProcess.startDetached(
            str(command.target),
            list(command.arguments),
            str(command.working_directory),
        )
        started = (
            bool(launch_result[0])
            if isinstance(launch_result, tuple)
            else bool(launch_result)
        )
        if not started:
            QMessageBox.critical(self, "无法启动 Profile", "操作系统未能启动 DcmGet。")
