from __future__ import annotations

import atexit
import faulthandler
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import stat
import sys
import threading
from typing import IO

from .runtime import ensure_application_log_dir


DIAGNOSTIC_LOG_NAME = "dcmget-diagnostics.log"
CRASH_LOG_NAME = "dcmget-crash.log"
DEFAULT_MAX_LOG_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3


class DiagnosticsError(RuntimeError):
    pass


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        flags |= getattr(os, "O_NOINHERIT", 0)
        descriptor = os.open(self.baseFilename, flags, 0o600)
        return os.fdopen(
            descriptor,
            self.mode,
            encoding=self.encoding,
            errors=self.errors,
        )

    def doRollover(self) -> None:
        super().doRollover()
        _make_private(Path(self.baseFilename))
        for index in range(1, self.backupCount + 1):
            _make_private(Path(f"{self.baseFilename}.{index}"))


class _DiagnosticsState:
    def __init__(
        self,
        directory: Path,
        logger: logging.Logger,
        crash_stream: IO[str],
    ) -> None:
        self.directory = directory
        self.logger = logger
        self.crash_stream = crash_stream
        self.had_unhandled_exception = False
        self.qt_handler = None
        self.original_excepthook = sys.excepthook
        self.original_threading_excepthook = threading.excepthook
        self.original_unraisablehook = sys.unraisablehook


_state: _DiagnosticsState | None = None
_install_lock = threading.Lock()


def diagnostic_log_directory() -> Path:
    if _state is not None:
        return _state.directory
    return ensure_application_log_dir()


def diagnostic_log_path() -> Path:
    return diagnostic_log_directory() / DIAGNOSTIC_LOG_NAME


def crash_log_path() -> Path:
    return diagnostic_log_directory() / CRASH_LOG_NAME


def install_diagnostics(
    app_version: str = "",
    *,
    directory: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_LOG_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> Path:
    global _state
    with _install_lock:
        if _state is not None:
            return _state.directory / DIAGNOSTIC_LOG_NAME

        log_directory = (
            Path(directory).expanduser().resolve()
            if directory is not None
            else ensure_application_log_dir()
        )
        log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _make_private(log_directory, directory=True)

        log_path = log_directory / DIAGNOSTIC_LOG_NAME
        handler = _PrivateRotatingFileHandler(
            log_path,
            maxBytes=max(1024, int(max_bytes)),
            backupCount=max(1, int(backup_count)),
            encoding="utf-8",
        )
        _make_private(log_path)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
        )
        logger = logging.getLogger("dcmget.diagnostics")
        for existing_handler in logger.handlers:
            existing_handler.close()
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(handler)

        crash_path = log_directory / CRASH_LOG_NAME
        crash_stream = _open_private_append(crash_path)
        _state = _DiagnosticsState(log_directory, logger, crash_stream)
        faulthandler.enable(file=crash_stream, all_threads=True)
        _install_exception_hooks()
        atexit.register(_mark_process_exit)

        version_text = app_version or "unknown"
        logger.info(
            "SESSION START pid=%s version=%s python=%s platform=%s frozen=%s executable=%s",
            os.getpid(),
            version_text,
            sys.version.split()[0],
            sys.platform,
            bool(getattr(sys, "frozen", False)),
            sys.executable,
        )
        crash_stream.write(
            f"\n--- DcmGet session start pid={os.getpid()} version={version_text} ---\n"
        )
        crash_stream.flush()
        return log_path


def install_qt_message_handler() -> None:
    if _state is None:
        install_diagnostics()
    assert _state is not None
    if _state.qt_handler is not None:
        return

    from PyQt5 import QtCore

    levels = {
        int(QtCore.QtWarningMsg): logging.WARNING,
        int(QtCore.QtCriticalMsg): logging.ERROR,
        int(QtCore.QtFatalMsg): logging.CRITICAL,
    }

    def qt_message_handler(mode, context, message) -> None:
        level = levels.get(int(mode))
        if level is None:
            return
        category = str(getattr(context, "category", "") or "qt")
        source_file = str(getattr(context, "file", "") or "")
        source_line = int(getattr(context, "line", 0) or 0)
        location = f" {source_file}:{source_line}" if source_file else ""
        try:
            _state.logger.log(level, "Qt[%s]%s %s", category, location, message)
            _flush_logs()
        except Exception:
            _write_crash_line(f"Qt[{category}]{location} {message}")

    _state.qt_handler = qt_message_handler
    QtCore.qInstallMessageHandler(qt_message_handler)


def record_exception(context: str, exc: BaseException) -> None:
    if _state is None:
        install_diagnostics()
    assert _state is not None
    _state.logger.error(
        "%s: %s",
        context,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    _flush_logs()


def prepare_macos_qt_plugins(plugin_root: str | Path | None = None) -> Path | None:
    if sys.platform != "darwin":
        return None
    if _state is None:
        install_diagnostics()
    if plugin_root is None:
        from PyQt5.QtCore import QLibraryInfo

        plugin_root = QLibraryInfo.location(QLibraryInfo.PluginsPath)

    root = Path(plugin_root).expanduser().resolve()
    cocoa_plugin = root / "platforms" / "libqcocoa.dylib"
    try:
        if not root.is_dir():
            raise DiagnosticsError(f"PyQt5 插件目录不存在：{root}")
        hidden_flag = getattr(stat, "UF_HIDDEN", 0x00008000)
        failures: list[str] = []
        paths = [root, *(path for path in root.rglob("*") if not path.is_symlink())]
        for path in paths:
            try:
                flags = _path_flags(path)
                if flags & hidden_flag:
                    _remove_hidden_flag(path, flags & ~hidden_flag)
            except OSError as exc:
                failures.append(f"{path}: {exc}")
        if failures:
            raise DiagnosticsError(
                "无法清除 macOS Qt 插件的隐藏标记：" + "; ".join(failures[:3])
            )
        if not cocoa_plugin.is_file():
            raise DiagnosticsError(f"缺少 macOS Qt Cocoa 平台插件：{cocoa_plugin}")
        if _path_flags(cocoa_plugin) & hidden_flag:
            raise DiagnosticsError(f"macOS Qt Cocoa 平台插件仍被隐藏：{cocoa_plugin}")
    except (OSError, DiagnosticsError) as exc:
        record_exception("macOS Qt 插件准备失败", exc)
        raise DiagnosticsError(
            f"无法准备 macOS 图形界面组件：{exc}。诊断日志：{diagnostic_log_path()}"
        ) from exc

    assert _state is not None
    _state.logger.info("macOS Qt Cocoa plugin ready: %s", cocoa_plugin)
    return cocoa_plugin


def _install_exception_hooks() -> None:
    def exception_hook(exc_type, exc_value, exc_traceback) -> None:
        assert _state is not None
        _state.had_unhandled_exception = True
        _state.logger.critical(
            "Unhandled main-thread exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        _flush_logs()
        if _state.original_excepthook is not sys.__excepthook__ or sys.stderr is not None:
            _state.original_excepthook(exc_type, exc_value, exc_traceback)

    def thread_exception_hook(args) -> None:
        assert _state is not None
        _state.had_unhandled_exception = True
        thread_name = getattr(getattr(args, "thread", None), "name", "unknown")
        _state.logger.critical(
            "Unhandled Python thread exception thread=%s",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        _flush_logs()
        if (
            _state.original_threading_excepthook is not threading.__excepthook__
            or sys.stderr is not None
        ):
            _state.original_threading_excepthook(args)

    def unraisable_hook(args) -> None:
        assert _state is not None
        _state.had_unhandled_exception = True
        target = getattr(args, "object", None)
        detail = str(getattr(args, "err_msg", None) or "").strip()
        message = f"Unraisable exception: {detail}" if detail else "Unraisable exception"
        _state.logger.critical(
            "%s object=%r",
            message,
            target,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        _flush_logs()
        if _state.original_unraisablehook is not sys.__unraisablehook__ or sys.stderr is not None:
            _state.original_unraisablehook(args)

    sys.excepthook = exception_hook
    threading.excepthook = thread_exception_hook
    sys.unraisablehook = unraisable_hook


def _mark_process_exit() -> None:
    if _state is None:
        return
    status = "UNHANDLED ERROR" if _state.had_unhandled_exception else "NORMAL EXIT"
    _state.logger.info("SESSION %s pid=%s", status, os.getpid())
    _flush_logs()
    try:
        _state.crash_stream.write(
            f"--- DcmGet session {status.lower()} pid={os.getpid()} ---\n"
        )
        _state.crash_stream.flush()
    except (OSError, ValueError):
        pass


def _flush_logs() -> None:
    if _state is None:
        return
    for handler in _state.logger.handlers:
        handler.flush()
    try:
        _state.crash_stream.flush()
    except (OSError, ValueError):
        pass


def _write_crash_line(message: str) -> None:
    if _state is None:
        return
    try:
        _state.crash_stream.write(message + "\n")
        _state.crash_stream.flush()
    except (OSError, ValueError):
        pass


def _open_private_append(path: Path) -> IO[str]:
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    flags |= getattr(os, "O_NOINHERIT", 0)
    descriptor = os.open(path, flags, 0o600)
    _make_private(path)
    return os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)


def _make_private(path: Path, *, directory: bool = False) -> None:
    if not path.exists():
        return
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError:
        pass


def _path_flags(path: Path) -> int:
    return int(getattr(path.stat(follow_symlinks=False), "st_flags", 0))


def _remove_hidden_flag(path: Path, flags: int) -> None:
    os.chflags(path, flags, follow_symlinks=False)
