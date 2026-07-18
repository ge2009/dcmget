from __future__ import annotations

from types import SimpleNamespace

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QInputDialog, QMessageBox

from dcmget.config import AppConfig, save_config
import dcmget.profile_dialog as profile_dialog_module
from dcmget.profile_dialog import ProfileManagerDialog
from dcmget.profile_manager import ProfileManager


def _write_profile(tmp_path, number: int, port: int) -> None:
    save_config(
        tmp_path
        / "config"
        / "instances"
        / f"i{number}"
        / "config.json",
        AppConfig(
            calling_ae_title="CALLING",
            pacs_ae_title="PACS01",
            storage_ae_title=f"DCMGET{number}",
            storage_port=port,
            dicom_destination_folder=str(tmp_path / f"dicom-{number}"),
        ),
    )


def test_profile_dialog_lists_profiles_and_clones_with_recommended_port(
    qtbot, tmp_path, monkeypatch
) -> None:
    _write_profile(tmp_path, 1, 6666)
    manager = ProfileManager(
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
        port_probe=lambda _host, _port: True,
    )
    dialog = ProfileManagerDialog(
        manager=manager,
        project_root=tmp_path,
        current_profile_number=1,
    )
    qtbot.addWidget(dialog)

    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 0).text() == "实例 1"
    assert dialog.table.item(0, 2).text() == "DCMGET1"
    assert dialog.table.item(0, 3).text() == "6666"

    monkeypatch.setattr(
        QInputDialog,
        "getText",
        lambda *_args, **_kwargs: ("第二下载任务", True),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.No,
    )
    qtbot.mouseClick(dialog.clone_button, Qt.LeftButton)

    assert dialog.table.rowCount() == 2
    cloned = manager.get_profile(2)
    assert cloned.display_name == "第二下载任务"
    assert cloned.storage_port == 6667
    assert cloned.storage_ae_title == "DCMGET1"


def test_profile_dialog_disables_delete_for_recovery_profile(qtbot, tmp_path) -> None:
    _write_profile(tmp_path, 1, 6666)
    recovery = (
        tmp_path / "state" / "instances" / "i1" / "active-task.sqlite3"
    )
    recovery.parent.mkdir(parents=True)
    recovery.write_bytes(b"checkpoint")
    manager = ProfileManager(
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
        port_probe=lambda _host, _port: True,
    )
    dialog = ProfileManagerDialog(manager=manager, project_root=tmp_path)
    qtbot.addWidget(dialog)

    assert "有恢复任务" in dialog.table.item(0, 7).text()
    assert not dialog.delete_button.isEnabled()


def test_profile_launch_treats_pyqt5_false_tuple_as_failure(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    _write_profile(tmp_path, 1, 6666)
    manager = ProfileManager(
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
        port_probe=lambda _host, _port: True,
    )
    dialog = ProfileManagerDialog(manager=manager, project_root=tmp_path)
    qtbot.addWidget(dialog)
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        profile_dialog_module,
        "build_instance_launch_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            target=tmp_path / "DcmGet.exe",
            arguments=("--profile", "1"),
            working_directory=tmp_path,
        ),
    )
    monkeypatch.setattr(
        profile_dialog_module.QProcess,
        "startDetached",
        lambda *_args: (False, 0),
    )
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, title, message: errors.append((title, message)),
    )

    dialog._launch_profile(manager.get_profile(1))

    assert errors == [("无法启动 Profile", "操作系统未能启动 DcmGet。")]


def test_profile_launch_accepts_pyqt5_success_tuple(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    _write_profile(tmp_path, 1, 6666)
    manager = ProfileManager(
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
        port_probe=lambda _host, _port: True,
    )
    dialog = ProfileManagerDialog(manager=manager, project_root=tmp_path)
    qtbot.addWidget(dialog)
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        profile_dialog_module,
        "build_instance_launch_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            target=tmp_path / "DcmGet.exe",
            arguments=("--profile", "1"),
            working_directory=tmp_path,
        ),
    )
    monkeypatch.setattr(
        profile_dialog_module.QProcess,
        "startDetached",
        lambda *_args: (True, 12345),
    )
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, title, message: errors.append((title, message)),
    )

    dialog._launch_profile(manager.get_profile(1))

    assert errors == []
