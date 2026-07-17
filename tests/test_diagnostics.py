from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_python(
    source: str, *, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    process_environment = os.environ.copy()
    process_environment["PYTHONPATH"] = str(ROOT)
    if environment is not None:
        process_environment.update(environment)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=ROOT,
        env=process_environment,
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
        for path in sorted(directory.glob("dcmget-diagnostics-*.log*"))
    )


def test_unavailable_application_log_directory_uses_secure_temporary_directory(
    tmp_path,
):
    state_root = tmp_path / "unavailable-state"
    temporary_root = tmp_path / "system-temporary"
    temporary_root.mkdir()
    result = _run_python(
        f"""
        import os
        from pathlib import Path
        import stat
        import sys

        state_root = Path({str(state_root)!r})
        if sys.platform == "win32":
            blocked = state_root / "local" / "DcmGet"
        elif sys.platform == "darwin":
            blocked = state_root / "home" / "Library" / "Application Support" / "DcmGet"
        else:
            blocked = state_root / "xdg" / "dcmget"
        blocked.parent.mkdir(parents=True)
        blocked.write_text("not a directory", encoding="utf-8")

        import DICOM_download_ui
        from dcmget.diagnostics import (
            crash_log_path,
            diagnostic_log_path,
            record_exception,
        )

        path = diagnostic_log_path()
        assert path == diagnostic_log_path()
        assert path.is_file()
        assert path.parent != blocked
        assert path.parent.name.startswith("DcmGet-diagnostics-")
        assert crash_log_path().is_file()
        if os.name != "nt":
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        try:
            raise RuntimeError("temporary fallback marker")
        except RuntimeError as exc:
            record_exception("temporary fallback context", exc)
        text = path.read_text(encoding="utf-8")
        assert "fallback is active" in text
        assert "temporary fallback marker" in text
        print(path)
        """,
        environment={
            "HOME": str(state_root / "home"),
            "LOCALAPPDATA": str(state_root / "local"),
            "XDG_STATE_HOME": str(state_root / "xdg"),
            "TMPDIR": str(temporary_root),
            "TEMP": str(temporary_root),
            "TMP": str(temporary_root),
        },
    )

    assert result.returncode == 0, result.stderr
    fallback_path = Path(result.stdout.strip())
    assert fallback_path.parent.parent == temporary_root


def test_file_and_temporary_log_failures_use_memory_without_raising():
    result = _run_python(
        """
        import io
        import sys
        from pathlib import Path
        import dcmget.diagnostics as diagnostics

        def fail(*_args, **_kwargs):
            raise PermissionError("diagnostic storage unavailable")

        diagnostics.ensure_application_log_dir = fail
        diagnostics.tempfile.mkdtemp = fail
        sys.stderr = None

        path = diagnostics.diagnostic_log_path()
        assert isinstance(path, Path)
        assert diagnostics.crash_log_path().name.startswith("dcmget-crash-")
        assert diagnostics.crash_log_path().suffix == ".log"
        try:
            raise ValueError("memory fallback marker")
        except ValueError as exc:
            diagnostics.record_exception("memory fallback context", exc)
        diagnostics.install_qt_message_handler()

        state = diagnostics._state
        assert state is not None and not state.file_backed
        assert isinstance(state.crash_stream, io.StringIO)
        text = state.crash_stream.getvalue()
        assert "系统临时目录" in text
        assert "memory fallback marker" in text
        sys.__stdout__.write("memory fallback ok")
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "memory fallback ok"


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
    log_path = Path(result.stdout.strip())
    rotated_path = Path(f"{log_path}.1")
    crash_paths = list(logs.glob("dcmget-crash-*.log"))
    assert log_path.is_file()
    assert rotated_path.is_file()
    assert len(crash_paths) == 1
    if os.name != "nt":
        assert stat.S_IMODE(logs.stat().st_mode) == 0o700
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(crash_paths[0].stat().st_mode) == 0o600
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
    assert (
        "Unhandled main-thread exception" in log
        and "KeyError: 'main hook marker'" in log
    )
    assert "diagnostic-test-thread" in log and "RuntimeError: thread hook marker" in log
    assert (
        "Unraisable exception" in log and "LookupError: unraisable hook marker" in log
    )
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
    crash_path = next(logs.glob("dcmget-crash-*.log"))
    crash = crash_path.read_text(encoding="utf-8")
    assert "DcmGet session start" in crash
    assert "session normal exit" in crash


def test_parallel_processes_use_independent_rotating_diagnostic_files(tmp_path):
    logs = tmp_path / "logs"
    source = textwrap.dedent(
        """
        import logging
        import sys
        from dcmget.diagnostics import diagnostic_log_path, install_diagnostics

        marker = sys.argv[1]
        path = install_diagnostics(
            marker, directory=sys.argv[2], max_bytes=1024, backup_count=2
        )
        logger = logging.getLogger("dcmget.diagnostics")
        for index in range(80):
            logger.warning("%s %03d %s", marker, index, "x" * 80)
        assert diagnostic_log_path() == path
        print(path)
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", source, marker, str(logs)],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for marker in ("diagnostic-process-a", "diagnostic-process-b")
    ]
    outputs = [process.communicate(timeout=20) for process in processes]

    assert [process.returncode for process in processes] == [0, 0]
    paths = [Path(stdout.strip()) for stdout, _stderr in outputs]
    assert paths[0] != paths[1]
    assert all(path.is_file() and path.parent == logs for path in paths)
    combined = _combined_logs(logs)
    assert "diagnostic-process-a" in combined
    assert "diagnostic-process-b" in combined


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
            assert str(diagnostics.diagnostic_log_path()) in str(exc)
            print("caught expected diagnostics error")
        else:
            raise AssertionError("missing Cocoa plugin should fail")
        """
    )

    assert result.returncode == 0, result.stderr
    assert "caught expected diagnostics error" in result.stdout
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


def test_main_window_startup_error_shows_diagnostic_log_path(monkeypatch, tmp_path):
    import DICOM_download_ui as entry

    diagnostic = tmp_path / "dcmget-diagnostics.log"
    messages = []
    closes = []

    class Signal:
        def connect(self, _callback):
            pass

    class Application:
        aboutToQuit = Signal()

        @staticmethod
        def instance():
            return app

    app = Application()

    class Profile:
        config_path = tmp_path / "instances" / "i1" / "config.json"
        task_state_path = tmp_path / "state" / "i1" / "active-task.sqlite3"
        log_directory = tmp_path / "state" / "i1" / "logs"
        label = "实例 1"
        settings_name = "DcmGet2-i1"

        def close(self):
            closes.append(True)

    monkeypatch.setattr(entry, "QApplication", Application)
    monkeypatch.setattr(entry, "create_application", lambda: app)
    monkeypatch.setattr(entry, "migrate_legacy_task_state", lambda _path: None)
    monkeypatch.setattr(
        entry, "acquire_instance_profile", lambda *_args, **_kwargs: Profile()
    )
    monkeypatch.setattr(entry, "resume_authorization_task_id", lambda _path: None)
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
    assert closes == [True]


def test_gui_authorization_reads_only_selected_profile_checkpoint(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    import DICOM_download_ui as entry

    task_id = "a" * 32
    task_state_path = tmp_path / "instances" / "i7" / "active-task.sqlite3"
    observed = []

    class Store:
        def __init__(self, path):
            observed.append(Path(path))

        def load(self):
            return SimpleNamespace(task_id=task_id)

    monkeypatch.setattr(entry, "TaskCheckpointStore", Store)

    assert entry.resume_authorization_task_id(task_state_path) == task_id
    assert observed == [task_state_path]


def test_repeated_gui_launches_acquire_independent_profiles(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import DICOM_download_ui as entry

    events: list[object] = []
    profiles = []

    class Signal:
        def connect(self, callback):
            events.append(("quit-handler", callback))

    class Application:
        def __init__(self):
            self.aboutToQuit = Signal()

        def exec_(self):
            events.append("exec")
            return 0

    class Window:
        def __init__(self, *args, **kwargs):
            events.append(("window", args, kwargs))

        def show(self):
            events.append("show")

    def acquire(requested, *, template_config_path):
        number = len(profiles) + 1
        profile = SimpleNamespace(
            number=number,
            config_path=tmp_path / "config" / f"i{number}" / "config.json",
            task_state_path=tmp_path / "state" / f"i{number}" / "active-task.sqlite3",
            log_directory=tmp_path / "state" / f"i{number}" / "logs",
            label=f"实例 {number}",
            settings_name=f"DcmGet2-i{number}",
            close=lambda number=number: events.append(("close", number)),
        )
        profiles.append((requested, Path(template_config_path), profile))
        return profile

    monkeypatch.setattr(entry, "create_application", Application)
    monkeypatch.setattr(
        entry,
        "migrate_legacy_task_state",
        lambda path: events.append(("migrate", Path(path))),
    )
    monkeypatch.setattr(entry, "acquire_instance_profile", acquire)
    monkeypatch.setattr(
        entry,
        "resume_authorization_task_id",
        lambda path: f"resume-{Path(path).parent.name}",
    )
    monkeypatch.setattr(
        entry,
        "authorize_gui",
        lambda task_id: events.append(("authorize", task_id)) or True,
    )
    monkeypatch.setattr(entry, "DcmGetWindow", Window)

    template = tmp_path / "config.json"
    assert entry.main(["--config", str(template)]) == 0
    assert entry.main(["--config", str(template), "--profile", "7"]) == 0

    assert [requested for requested, _template, _profile in profiles] == [None, 7]
    windows = [event for event in events if event[0] == "window"]
    assert len(windows) == 2
    for index, (_kind, args, kwargs) in enumerate(windows, start=1):
        profile = profiles[index - 1][2]
        assert args == (
            profile.config_path,
            entry.PROJECT_ROOT,
            profile.task_state_path,
        )
        assert kwargs == {
            "offer_task_resume": True,
            "enable_multi_task": False,
            "instance_label": profile.label,
            "settings_name": profile.settings_name,
            "log_directory": profile.log_directory,
        }
    assert ("authorize", "resume-i1") in events
    assert ("authorize", "resume-i2") in events
    assert events.count(("close", 1)) == 1
    assert events.count(("close", 2)) == 1
    assert not hasattr(entry, "SingleInstance")


def test_gui_authorization_rejection_releases_profile(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import DICOM_download_ui as entry

    closed = []

    class Application:
        pass

    profile = SimpleNamespace(
        task_state_path=tmp_path / "state" / "i1" / "active-task.sqlite3",
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(entry, "create_application", Application)
    monkeypatch.setattr(entry, "migrate_legacy_task_state", lambda _path: None)
    monkeypatch.setattr(
        entry,
        "acquire_instance_profile",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(
        entry,
        "resume_authorization_task_id",
        lambda _path: "a" * 32,
    )
    monkeypatch.setattr(entry, "authorize_gui", lambda _task_id: False)
    monkeypatch.setattr(
        entry,
        "DcmGetWindow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("authorization rejection must not create a window")
        ),
    )

    assert entry.main(["--config", str(tmp_path / "config.json")]) == 1
    assert closed == [True]


def test_profile_argument_requires_positive_integer():
    import DICOM_download_ui as entry

    assert entry.build_parser().parse_args(["--profile", "3"]).profile == 3
    with pytest.raises(SystemExit):
        entry.build_parser().parse_args(["--profile", "0"])
