from __future__ import annotations

import http.client
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

import dcmget.pdi_server as pdi_server
from dcmget.pdi_server import PdiHttpServer, validate_pdi_root, viewer_url


def _write_viewer_checksums(root: Path) -> None:
    checksum_path = root / pdi_server.OHIF_PAYLOAD_CHECKSUMS
    lines = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path == checksum_path:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(root).as_posix()}")
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def served_pdi(tmp_path: Path):
    (tmp_path / "VIEWER" / "OHIF").mkdir(parents=True)
    (tmp_path / "VIEWER" / "OHIF" / "index.html").write_text("OHIF INDEX")
    (tmp_path / "VIEWER" / "OHIF" / "app.js").write_bytes(b"javascript")
    (tmp_path / "VIEWER" / ".dcmget").mkdir()
    (tmp_path / "DICOM").mkdir()
    (tmp_path / "DICOM" / "I000001").write_bytes(b"0123456789")
    (tmp_path / "VIEWER" / ".dcmget" / "index").write_text(
        json.dumps({"studies": []}), encoding="utf-8"
    )
    server = PdiHttpServer(tmp_path, "127.0.0.1", 0, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _request(server: PdiHttpServer, path: str, headers: dict[str, str] | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
    connection.request("GET", path, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    result = response.status, dict(response.getheaders()), body
    connection.close()
    return result


def test_server_serves_spa_manifest_and_extensionless_dicom(served_pdi) -> None:
    status, headers, body = _request(served_pdi, "/viewer/dicomjson/?url=x&lng=zh")
    assert status == 200 and body == b"OHIF INDEX"
    assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"
    assert "connect-src 'self'" in headers["Content-Security-Policy"]

    status, headers, body = _request(served_pdi, "/api/studies")
    assert status == 200 and json.loads(body)["studies"] == []
    assert headers["Cache-Control"] == "no-store"

    status, _headers, _body = _request(served_pdi, "/api/unknown")
    assert status == 404

    for private_path in ("/DCMGET_STUDIES.json", "/VIEWER/.dcmget/index"):
        status, _headers, _body = _request(served_pdi, private_path)
        assert status == 404

    status, headers, body = _request(served_pdi, "/DICOM/I000001")
    assert status == 200 and body == b"0123456789"
    assert headers["Content-Type"] == "application/dicom"


def test_windowed_server_serves_when_stderr_is_unavailable(
    served_pdi, monkeypatch
) -> None:
    served_pdi.quiet = False
    with monkeypatch.context() as context:
        context.setattr(sys, "stderr", None)
        status, _headers, body = _request(
            served_pdi, "/viewer/dicomjson/?url=x&lng=zh"
        )
    assert status == 200
    assert body == b"OHIF INDEX"


def test_viewer_diagnostic_log_is_private(tmp_path, monkeypatch) -> None:
    log_path = tmp_path / "private" / "dcmget-pdi-viewer.log"
    monkeypatch.setattr(pdi_server, "viewer_log_path", lambda: log_path)

    logger = pdi_server._configure_viewer_logger()
    assert logger is not None
    logger.error("viewer diagnostic marker")
    for handler in logger.handlers:
        handler.flush()
        handler.close()
    logger.handlers.clear()

    assert "viewer diagnostic marker" in log_path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert log_path.stat().st_mode & 0o777 == 0o600


def test_server_supports_byte_ranges(served_pdi) -> None:
    status, headers, body = _request(
        served_pdi, "/DICOM/I000001?frame=1", {"Range": "bytes=2-5"}
    )
    assert status == 206 and body == b"2345"
    assert headers["Content-Range"] == "bytes 2-5/10"

    status, error_headers, _body = _request(
        served_pdi, "/DICOM/I000001", {"Range": "bytes=99-100"}
    )
    assert status == 416
    assert error_headers["Content-Range"] == "bytes */10"
    assert error_headers["Cache-Control"] == "no-store"

    status, headers, body = _request(
        served_pdi, "/DICOM/I000001", {"Range": "bytes=0-9"}
    )
    assert status == 206 and body == b"0123456789"
    assert headers["Content-Range"] == "bytes 0-9/10"


def test_server_rejects_escape_and_writes_nothing(served_pdi, tmp_path: Path) -> None:
    status, _headers, _body = _request(served_pdi, "/DICOM/../../secret")
    assert status == 404
    connection = http.client.HTTPConnection(
        "127.0.0.1", served_pdi.server_address[1]
    )
    connection.request("POST", "/DICOM/I000001", body=b"replace")
    response = connection.getresponse()
    response.read()
    assert response.status == 405
    connection.close()
    assert (Path(served_pdi.pdi_root) / "DICOM" / "I000001").read_bytes() == b"0123456789"

    status, error_headers, body = _request(served_pdi, "/missing-script.js")
    assert status == 404 and b"OHIF INDEX" not in body
    assert error_headers["Cache-Control"] == "no-store"


def test_root_validation_and_viewer_url(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="PDI 目录不完整"):
        validate_pdi_root(tmp_path)
    assert viewer_url(12345) == (
        "http://127.0.0.1:12345/viewer/dicomjson/"
        "?url=%2Fapi%2Fstudies&lng=zh"
    )


def test_server_rejects_untrusted_host_and_non_loopback_binding(served_pdi) -> None:
    status, error_headers, _body = _request(
        served_pdi,
        "/api/studies",
        {"Host": "evil.example:9999"},
    )
    assert status == 421
    assert error_headers["Cache-Control"] == "no-store"

    with pytest.raises(ValueError, match="127.0.0.1"):
        PdiHttpServer(Path(served_pdi.pdi_root), "0.0.0.0", 0, quiet=True)


def test_root_validation_rejects_symlinked_content(tmp_path: Path) -> None:
    root = tmp_path / "pdi"
    outside = tmp_path / "outside"
    (root / "VIEWER" / "OHIF").mkdir(parents=True)
    (root / "VIEWER" / "OHIF" / "index.html").write_text("OHIF")
    (root / "DCMGET_STUDIES.json").write_text('{"studies": []}')
    outside.mkdir()
    (outside / "secret").write_text("patient data")
    try:
        (root / "DICOM").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(FileNotFoundError, match="DICOM"):
        validate_pdi_root(root)


def test_new_pdi_validates_viewer_checksum_without_reading_dicom(
    tmp_path: Path, monkeypatch
) -> None:
    viewer = tmp_path / "VIEWER" / "OHIF"
    viewer.mkdir(parents=True)
    (viewer / "index.html").write_text("OHIF")
    (viewer / "app.js").write_text("viewer")
    (tmp_path / "VIEWER" / ".dcmget").mkdir()
    (tmp_path / "VIEWER" / ".dcmget" / "index").write_text('{"studies": []}')
    (tmp_path / "DICOM").mkdir()
    (tmp_path / "DICOM" / "I000001").write_bytes(b"patient dicom")
    _write_viewer_checksums(viewer)

    hashed: list[Path] = []
    original = pdi_server._sha256

    def record(path: Path) -> str:
        hashed.append(path)
        return original(path)

    monkeypatch.setattr(pdi_server, "_sha256", record)
    assert validate_pdi_root(tmp_path) == tmp_path.resolve()
    assert hashed
    assert all(tmp_path / "DICOM" not in path.parents for path in hashed)

    (viewer / "app.js").write_text("corrupt")
    with pytest.raises(RuntimeError, match="资源校验失败"):
        validate_pdi_root(tmp_path)


def test_viewer_payload_rejects_aggregate_size_before_hashing(
    tmp_path: Path, monkeypatch
) -> None:
    viewer = tmp_path / "OHIF"
    viewer.mkdir()
    first = viewer / "first.bin"
    second = viewer / "second.bin"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    (viewer / pdi_server.OHIF_PAYLOAD_CHECKSUMS).write_text(
        f"{'0' * 64}  first.bin\n{'0' * 64}  second.bin\n",
        encoding="utf-8",
    )
    assert pdi_server.MAX_VIEWER_TOTAL_BYTES == 512 * 1024 * 1024
    monkeypatch.setattr(pdi_server, "MAX_VIEWER_TOTAL_BYTES", 1)

    def unexpected_hash(_path: Path) -> str:
        pytest.fail("总体积超限应在计算哈希前被拒绝")

    monkeypatch.setattr(pdi_server, "_sha256", unexpected_hash)
    with pytest.raises(RuntimeError, match="总体积"):
        pdi_server.verify_viewer_payload(viewer)


def test_server_exits_when_idle_and_requests_renew_timeout(tmp_path: Path) -> None:
    assert pdi_server.DEFAULT_IDLE_TIMEOUT_SECONDS >= 4 * 60 * 60
    (tmp_path / "VIEWER" / "OHIF").mkdir(parents=True)
    (tmp_path / "VIEWER" / "OHIF" / "index.html").write_text("OHIF")
    (tmp_path / "VIEWER" / ".dcmget").mkdir()
    (tmp_path / "VIEWER" / ".dcmget" / "index").write_text('{"studies": []}')
    (tmp_path / "DICOM").mkdir()
    server = PdiHttpServer(
        tmp_path,
        "127.0.0.1",
        0,
        quiet=True,
        idle_timeout_seconds=0.4,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        time.sleep(0.25)
        status, _headers, _body = _request(server, "/api/studies")
        assert status == 200
        time.sleep(0.25)
        assert thread.is_alive(), "HTTP 请求未续期空闲超时"
        thread.join(timeout=1.5)
        assert not thread.is_alive()
        assert server.idle_expired
    finally:
        if thread.is_alive():
            server.shutdown()
            thread.join(timeout=3)
        server.server_close()


def test_new_pdi_requires_viewer_checksum_manifest(tmp_path: Path) -> None:
    (tmp_path / "VIEWER" / "OHIF").mkdir(parents=True)
    (tmp_path / "VIEWER" / "OHIF" / "index.html").write_text("OHIF")
    (tmp_path / "VIEWER" / ".dcmget").mkdir()
    (tmp_path / "VIEWER" / ".dcmget" / "index").write_text('{"studies": []}')
    (tmp_path / "DICOM").mkdir()

    with pytest.raises(FileNotFoundError, match="资源校验清单"):
        validate_pdi_root(tmp_path)


def test_legacy_index_is_private_but_still_supported(tmp_path: Path) -> None:
    (tmp_path / "VIEWER" / "OHIF").mkdir(parents=True)
    (tmp_path / "VIEWER" / "OHIF" / "index.html").write_text("OHIF")
    (tmp_path / "DICOM").mkdir()
    (tmp_path / "DCMGET_STUDIES.json").write_text(
        '{"studies": []}', encoding="utf-8"
    )

    assert validate_pdi_root(tmp_path) == tmp_path.resolve()
    server = PdiHttpServer(tmp_path, "127.0.0.1", 0, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, body = _request(server, "/api/studies")
        assert status == 200 and json.loads(body)["studies"] == []
        status, _headers, _body = _request(server, "/DCMGET_STUDIES.json")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
