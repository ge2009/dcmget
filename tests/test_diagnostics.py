from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys
import textwrap

import pytest
from PyQt5.QtWidgets import QApplication


ROOT = Path(__file__).resolve().parents[1]


def _run_python(source: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )


def _combined_logs(directory: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(directory.glob("dcmget-diagnostics.log*"))
    )


def test_diagnostics_are_private_rotated_and_mark_normal_exit(tmp_path):
    logs = tmp_path / "logs"
    result = _run_python(
        f"""
        import logging
        from dcmget.diagnostics import install_diagnostics

        path = install_diagnostics(
            "test-version", directory={str(logs)!r}, max_bytes=1024, backup_count=2
        )
        logger = logging.getLogger("dcmget.diagnostics")
        for index in range(80):
            logger.warning("rotation marker %03d %s", index, "x" * 80)
        print(path)
        """
    )

    assert result.returncode == 0, result.stderr
    assert (logs / "dcmget-diagnostics.log").is_file()
    assert (logs / "dcmget-diagnostics.log.1").is_file()
    assert (logs / "dcmget-crash.log").is_file()
    if os.name != "nt":
        assert stat.S_IMODE(logs.stat().st_mode) == 0o700
        assert stat.S_IMODE((logs / "dcmget-diagnostics.log").stat().st_mode) == 0o600
        assert stat.S_IMODE((logs / "dcmget-crash.log").stat().st_mode) == 0o600
    combined = _combined_logs(logs)
    assert "rotation marker" in combined
    assert "SESSION NORMAL EXIT" in combined


def test_python_exception_hooks_and_record_exception_keep_tracebacks(tmp_path):
    logs = tmp_path / "logs"
    result = _run_python(
        f"""
        import gc
        import sys
        import threading
        from dcmget.diagnostics import install_diagnostics, record_exception

        install_diagnostics("hooks", directory={str(logs)!r})
        try:
            raise ValueError("record marker")
        except ValueError as exc:
            record_exception("record context", exc)

        try:
            raise KeyError("main hook marker")
        except KeyError:
            sys.excepthook(*sys.exc_info())

        def fail_thread():
            raise RuntimeError("thread hook marker")

        thread = threading.Thread(target=fail_thread, name="diagnostic-test-thread")
        thread.start()
        thread.join()

        class BrokenFinalizer:
            def __del__(self):
                raise LookupError("unraisable hook marker")

        value = BrokenFinalizer()
        del value
        gc.collect()
        """
    )

    assert result.returncode == 0
    log = _combined_logs(logs)
    assert "record context" in log and "ValueError: record marker" in log
    assert "Unhandled main-thread exception" in log and "KeyError: 'main hook marker'" in log
    assert "diagnostic-test-thread" in log and "RuntimeError: thread hook marker" in log
    assert "Unraisable exception" in log and "LookupError: unraisable hook marker" in log
    assert "SESSION UNHANDLED ERROR" in log


def test_qt_messages_and_faulthandler_use_diagnostic_files(tmp_path):
    logs = tmp_path / "logs"
    result = _run_python(
        f"""
        import faulthandler
        from PyQt5.QtCore import qCritical, qWarning
        from dcmget.diagnostics import install_diagnostics, install_qt_message_handler

        install_diagnostics("qt", directory={str(logs)!r})
        install_qt_message_handler()
        qWarning("qt warning marker")
        qCritical("qt critical marker")
        assert faulthandler.is_enabled()
        """
    )

    assert result.returncode == 0, result.stderr
    log = _combined_logs(logs)
    assert "qt warning marker" in log
    assert "qt critical marker" in log
    crash = (logs / "dcmget-crash.log").read_text(encoding="utf-8")
    assert "DcmGet session start" in crash
    assert "session normal exit" in crash


def test_macos_plugin_failure_is_logged_with_readable_log_path(tmp_path):
    logs = tmp_path / "logs"
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    result = _run_python(
        f"""
        import dcmget.diagnostics as diagnostics

        diagnostics.install_diagnostics("mac-failure", directory={str(logs)!r})
        diagnostics.sys.platform = "darwin"
        try:
            diagnostics.prepare_macos_qt_plugins({str(plugins)!r})
        except diagnostics.DiagnosticsError as exc:
            print(exc)
        else:
            raise AssertionError("missing Cocoa plugin should fail")
        """
    )

    assert result.returncode == 0, result.stderr
    assert "诊断日志" in result.stdout
    log = _combined_logs(logs)
    assert "macOS Qt 插件准备失败" in log
    assert "缺少 macOS Qt Cocoa 平台插件" in log


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS file flags")
def test_macos_hidden_cocoa_plugin_is_unhidden_before_qapplication(tmp_path):
    logs = tmp_path / "logs"
    plugins = tmp_path / "plugins"
    cocoa = plugins / "platforms" / "libqcocoa.dylib"
    cocoa.parent.mkdir(parents=True)
    cocoa.write_bytes(b"test")
    os.chflags(cocoa, cocoa.stat().st_flags | stat.UF_HIDDEN)

    result = _run_python(
        f"""
        import stat
        from pathlib import Path
        import dcmget.diagnostics as diagnostics

        diagnostics.install_diagnostics("mac-hidden", directory={str(logs)!r})
        plugin = diagnostics.prepare_macos_qt_plugins({str(plugins)!r})
        assert plugin == Path({str(cocoa)!r})
        assert not (plugin.stat().st_flags & stat.UF_HIDDEN)
        """
    )

    assert result.returncode == 0, result.stderr


def test_ui_self_test_no_longer_constructs_daily_password_dialog(monkeypatch):
    import DICOM_download_ui as entry

    events: list[str] = []

    class FakeApplication:
        def processEvents(self):
            events.append("events")

    class FakeWindow:
        def __init__(self, *_args, **_kwargs):
            events.append("window")

        def show(self):
            events.append("show")

        def isVisible(self):
            return True

        def centralWidget(self):
            return object()

        def close(self):
            events.append("close")

    monkeypatch.setattr(entry, "create_application", FakeApplication)
    monkeypatch.setattr(entry, "DcmGetWindow", FakeWindow)

    assert entry.run_ui_self_test("unused.json") == 0
    assert not hasattr(entry, "DailyPasswordDialog")
    assert events == ["window", "show", "events", "close", "events"]


def test_self_test_startup_error_does_not_open_modal_dialog(
    monkeypatch, tmp_path, capsys
):
    import DICOM_download_ui as entry

    diagnostic = tmp_path / "dcmget-diagnostics.log"
    recorded = []
    monkeypatch.setattr(
        entry,
        "run_self_test",
        lambda _path: (_ for _ in ()).throw(RuntimeError("self-test marker")),
    )
    monkeypatch.setattr(
        entry,
        "record_exception",
        lambda context, exc: recorded.append((context, exc)),
    )
    monkeypatch.setattr(entry, "diagnostic_log_path", lambda: diagnostic)
    monkeypatch.setattr(
        entry.QMessageBox,
        "critical",
        lambda *_args: (_ for _ in ()).throw(AssertionError("modal dialog opened")),
    )

    assert entry.main(["--self-test", "--config", str(tmp_path / "config.json")]) == 1
    assert recorded and str(recorded[0][1]) == "self-test marker"
    assert str(diagnostic) in capsys.readouterr().err


def test_main_window_startup_error_shows_diagnostic_log_path(
    qtbot, monkeypatch, tmp_path
):
    import DICOM_download_ui as entry

    app = QApplication.instance()
    assert app is not None
    diagnostic = tmp_path / "dcmget-diagnostics.log"
    messages = []

    class EmptyTaskStore:
        def load(self):
            return None

    monkeypatch.setattr(entry, "create_application", lambda: app)
    monkeypatch.setattr(entry, "TaskCheckpointStore", EmptyTaskStore)
    monkeypatch.setattr(entry, "authorize_gui", lambda _task_id: True)
    monkeypatch.setattr(
        entry,
        "DcmGetWindow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("window marker")),
    )
    monkeypatch.setattr(entry, "record_exception", lambda *_args: None)
    monkeypatch.setattr(entry, "diagnostic_log_path", lambda: diagnostic)
    monkeypatch.setattr(
        entry.QMessageBox,
        "critical",
        lambda _parent, title, message: messages.append((title, message)),
    )

    assert entry.main(["--config", str(tmp_path / "config.json")]) == 1
    assert messages
    assert "window marker" in messages[0][1]
    assert str(diagnostic) in messages[0][1]
