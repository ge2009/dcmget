from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_python(source: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source), *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _combined_logs(directory: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(directory.glob("*.log*"))
    )


def test_diagnostics_are_private_and_mark_normal_exit(tmp_path):
    logs = tmp_path / "logs"
    result = _run_python(
        """
        import logging
        import sys
        from dcmget.diagnostics import install_diagnostics

        path = install_diagnostics("web-test", directory=sys.argv[1])
        logging.getLogger("dcmget.diagnostics").warning("web marker")
        print(path)
        """,
        str(logs),
    )

    assert result.returncode == 0, result.stderr
    path = Path(result.stdout.strip())
    assert path.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(logs.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    combined = _combined_logs(logs)
    assert "web marker" in combined
    assert "SESSION NORMAL EXIT" in combined


def test_unavailable_primary_log_directory_uses_secure_temporary_fallback(tmp_path):
    result = _run_python(
        """
        import sys
        from pathlib import Path
        import dcmget.diagnostics as diagnostics

        def fail():
            raise PermissionError("primary unavailable")

        diagnostics.ensure_application_log_dir = fail
        path = diagnostics.install_diagnostics("fallback")
        assert path.is_file()
        assert path.parent.name.startswith("DcmGet-diagnostics-")
        print(path)
        """
    )

    assert result.returncode == 0, result.stderr
    fallback = Path(result.stdout.strip())
    try:
        assert "primary unavailable" in fallback.read_text(
            encoding="utf-8", errors="replace"
        )
        if os.name != "nt":
            assert stat.S_IMODE(fallback.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(fallback.stat().st_mode) == 0o600
    finally:
        for path in fallback.parent.iterdir():
            path.unlink(missing_ok=True)
        fallback.parent.rmdir()


def test_exception_hooks_and_record_exception_keep_tracebacks(tmp_path):
    logs = tmp_path / "logs"
    result = _run_python(
        """
        import sys
        import threading
        from dcmget.diagnostics import install_diagnostics, record_exception

        install_diagnostics("hooks", directory=sys.argv[1])
        try:
            raise ValueError("record marker")
        except ValueError as exc:
            record_exception("record context", exc)

        def fail():
            raise RuntimeError("thread marker")

        thread = threading.Thread(target=fail, name="web-diagnostic-thread")
        thread.start()
        thread.join()
        """,
        str(logs),
    )

    assert result.returncode == 0
    combined = _combined_logs(logs)
    assert "record context" in combined and "ValueError: record marker" in combined
    assert "web-diagnostic-thread" in combined and "RuntimeError: thread marker" in combined
    assert "SESSION UNHANDLED ERROR" in combined


def test_parallel_processes_use_independent_diagnostic_files(tmp_path):
    logs = tmp_path / "logs"
    source = textwrap.dedent(
        """
        import logging
        import sys
        from dcmget.diagnostics import install_diagnostics

        path = install_diagnostics(sys.argv[1], directory=sys.argv[2])
        logging.getLogger("dcmget.diagnostics").warning(sys.argv[1])
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
        )
        for marker in ("process-a", "process-b")
    ]
    outputs = [process.communicate(timeout=20) for process in processes]

    assert [process.returncode for process in processes] == [0, 0]
    paths = [Path(stdout.strip()) for stdout, _stderr in outputs]
    assert paths[0] != paths[1]
    assert all(path.is_file() for path in paths)
    combined = _combined_logs(logs)
    assert "process-a" in combined and "process-b" in combined
