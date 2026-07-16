from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from dcmget.pdi_server import PdiHttpServer, validate_pdi_root, viewer_url


@pytest.fixture
def served_pdi(tmp_path: Path):
    (tmp_path / "VIEWER" / "OHIF").mkdir(parents=True)
    (tmp_path / "VIEWER" / "OHIF" / "index.html").write_text("OHIF INDEX")
    (tmp_path / "VIEWER" / "OHIF" / "app.js").write_bytes(b"javascript")
    (tmp_path / "DICOM").mkdir()
    (tmp_path / "DICOM" / "I000001").write_bytes(b"0123456789")
    (tmp_path / "DCMGET_STUDIES.json").write_text(
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
    assert "connect-src 'self'" in headers["Content-Security-Policy"]

    status, headers, body = _request(served_pdi, "/DCMGET_STUDIES.json")
    assert status == 200 and json.loads(body)["studies"] == []
    assert headers["Cache-Control"] == "no-store"

    status, headers, body = _request(served_pdi, "/DICOM/I000001")
    assert status == 200 and body == b"0123456789"
    assert headers["Content-Type"] == "application/dicom"


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
        "?url=%2FDCMGET_STUDIES.json&lng=zh"
    )


def test_server_rejects_untrusted_host_and_non_loopback_binding(served_pdi) -> None:
    status, error_headers, _body = _request(
        served_pdi,
        "/DCMGET_STUDIES.json",
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
