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


def _write_pinned_viewer(root: Path, index_text: str = "TRUSTED OHIF") -> Path:
    root.mkdir(parents=True)
    (root / "index.html").write_text(index_text, encoding="utf-8")
    (root / "app-config.js").write_text("window.config = {};", encoding="utf-8")
    (root / pdi_server.OHIF_PROVENANCE_FILE).write_text(
        json.dumps(
            {
                "package_name": "@ohif/app",
                "version": pdi_server.PINNED_OHIF_VERSION,
                "source_sha256": pdi_server.PINNED_OHIF_SOURCE_SHA256,
            }
        ),
        encoding="utf-8",
    )
    _write_viewer_checksums(root)
    return root


def _study_index_payload(
    url: str = "dicomweb:/DICOM/I000001",
) -> dict[str, object]:
    return {
        "studies": [
            {
                "StudyInstanceUID": "1.2.3",
                "series": [
                    {
                        "SeriesInstanceUID": "1.2.3.4",
                        "instances": [{"metadata": {}, "url": url}],
                    }
                ],
            }
        ]
    }


def _write_study_index(
    root: Path,
    payload: object | None = None,
    *,
    legacy: bool = False,
) -> Path:
    path = (
        root / pdi_server.LEGACY_STUDY_INDEX
        if legacy
        else root / pdi_server.STUDY_INDEX
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_study_index_payload() if payload is None else payload),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def served_pdi(tmp_path: Path):
    (tmp_path / "VIEWER" / "OHIF").mkdir(parents=True)
    (tmp_path / "VIEWER" / "OHIF" / "index.html").write_text("OHIF INDEX")
    (tmp_path / "VIEWER" / "OHIF" / "app.js").write_bytes(b"javascript")
    (tmp_path / "VIEWER" / ".dcmget").mkdir()
    (tmp_path / "DICOM").mkdir()
    (tmp_path / "DICOM" / "I000001").write_bytes(b"0123456789")
    _write_study_index(tmp_path)
    server = PdiHttpServer(tmp_path, "127.0.0.1", 0, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _request(
    server: PdiHttpServer,
    path: str,
    headers: dict[str, str] | None = None,
    *,
    method: str = "GET",
):
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=3
    )
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        connection.close()


def _expire_idle_server(server: PdiHttpServer) -> None:
    with server._idle_condition:
        server._last_activity = (
            time.monotonic() - server.idle_timeout_seconds - 1
        )
        server._idle_condition.notify_all()


def test_main_rejects_unsupported_runtime_before_starting_server(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        pdi_server,
        "ensure_supported_runtime",
        lambda: (_ for _ in ()).throw(
            pdi_server.ArchitectureError("32 位运行时")
        ),
    )
    started = []
    monkeypatch.setattr(
        pdi_server,
        "run_server",
        lambda *_args, **_kwargs: started.append(True),
    )

    assert pdi_server.main([]) == 1
    assert started == []
    assert "运行环境不受支持" in capsys.readouterr().err


def _authenticate(server: PdiHttpServer) -> dict[str, str]:
    status, headers, body = _request(server, f"/open/{server.session_token}")
    assert status == 303 and body == b""
    return {"Cookie": headers["Set-Cookie"].partition(";")[0]}


def test_server_opens_authenticated_session_and_redirects_to_directory(
    served_pdi,
) -> None:
    status, _headers, _body = _request(served_pdi, "/api/studies")
    assert status == 403

    status, headers, body = _request(
        served_pdi, f"/open/{served_pdi.session_token}"
    )
    assert status == 303 and body == b""
    assert headers["Location"] == (
        "/viewer/directory/?url=%2Fapi%2Fstudies&lng=zh"
    )
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(f"{served_pdi.cookie_name}={served_pdi.session_token};")
    assert "Path=/" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie

    status, _headers, _body = _request(
        served_pdi,
        f"/ready/{served_pdi.session_token}",
        method="HEAD",
    )
    assert status == 200
    status, _headers, _body = _request(
        served_pdi,
        "/ready/invalid-token",
        method="HEAD",
    )
    assert status == 404


def test_server_serves_directory_index_and_allowlisted_dicom(served_pdi) -> None:
    authenticated = _authenticate(served_pdi)
    status, headers, body = _request(
        served_pdi,
        "/viewer/directory/?url=%2Fapi%2Fstudies&lng=zh",
        authenticated,
    )
    assert status == 200 and body == b"OHIF INDEX"
    assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"
    assert "connect-src 'self'" in headers["Content-Security-Policy"]

    status, headers, body = _request(served_pdi, "/api/studies", authenticated)
    assert status == 200 and len(json.loads(body)["studies"]) == 1
    assert headers["Cache-Control"] == "no-store"

    status, _headers, _body = _request(
        served_pdi, "/api/unknown", authenticated
    )
    assert status == 404

    for private_path in ("/DCMGET_STUDIES.json", "/VIEWER/.dcmget/index"):
        status, _headers, _body = _request(
            served_pdi, private_path, authenticated
        )
        assert status == 404

    status, headers, body = _request(
        served_pdi, "/DICOM/I000001", authenticated
    )
    assert status == 200 and body == b"0123456789"
    assert headers["Content-Type"] == "application/dicom"


def test_windowed_server_serves_when_stderr_is_unavailable(
    served_pdi, monkeypatch
) -> None:
    served_pdi.quiet = False
    authenticated = _authenticate(served_pdi)
    with monkeypatch.context() as context:
        context.setattr(sys, "stderr", None)
        status, _headers, body = _request(
            served_pdi,
            "/viewer/directory/?url=%2Fapi%2Fstudies&lng=zh",
            authenticated,
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
    authenticated = _authenticate(served_pdi)
    status, headers, body = _request(
        served_pdi,
        "/DICOM/I000001?frame=1",
        {**authenticated, "Range": "bytes=2-5"},
    )
    assert status == 206 and body == b"2345"
    assert headers["Content-Range"] == "bytes 2-5/10"

    status, error_headers, _body = _request(
        served_pdi,
        "/DICOM/I000001",
        {**authenticated, "Range": "bytes=99-100"},
    )
    assert status == 416
    assert error_headers["Content-Range"] == "bytes */10"
    assert error_headers["Cache-Control"] == "no-store"

    status, headers, body = _request(
        served_pdi,
        "/DICOM/I000001",
        {**authenticated, "Range": "bytes=0-9"},
    )
    assert status == 206 and body == b"0123456789"
    assert headers["Content-Range"] == "bytes 0-9/10"


def test_server_rejects_escape_and_writes_nothing(served_pdi, tmp_path: Path) -> None:
    authenticated = _authenticate(served_pdi)
    status, _headers, _body = _request(
        served_pdi, "/DICOM/../../secret", authenticated
    )
    assert status == 404
    status, _headers, _body = _request(
        served_pdi,
        "/DICOM/I000001",
        authenticated,
        method="POST",
    )
    assert status == 405
    assert (Path(served_pdi.pdi_root) / "DICOM" / "I000001").read_bytes() == b"0123456789"

    status, error_headers, body = _request(
        served_pdi, "/missing-script.js", authenticated
    )
    assert status == 404 and b"OHIF INDEX" not in body
    assert error_headers["Cache-Control"] == "no-store"

    unlisted = Path(served_pdi.pdi_root) / "DICOM" / "I999999"
    unlisted.write_bytes(b"not indexed")
    status, _headers, _body = _request(
        served_pdi, "/DICOM/I999999", authenticated
    )
    assert status == 404


def test_root_validation_and_viewer_url(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="PDI 目录不完整"):
        validate_pdi_root(tmp_path)
    token = pdi_server.generate_session_token()
    assert viewer_url(12345, token) == f"http://127.0.0.1:12345/open/{token}"
    assert pdi_server.VIEWER_PATH == "/viewer/directory/"
    with pytest.raises(ValueError, match="令牌"):
        viewer_url(12345, "weak")


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


def test_cookie_is_required_for_static_api_and_dicom(served_pdi) -> None:
    for path in (
        "/viewer/directory/",
        "/api/studies",
        "/DICOM/I000001",
    ):
        status, _headers, _body = _request(served_pdi, path)
        assert status == 403
        status, _headers, _body = _request(
            served_pdi,
            path,
            {"Cookie": f"{served_pdi.cookie_name}=wrong"},
        )
        assert status == 403


def test_service_cookie_names_are_unique(served_pdi) -> None:
    second = PdiHttpServer(
        Path(served_pdi.pdi_root),
        "127.0.0.1",
        0,
        quiet=True,
        session_token=served_pdi.session_token,
    )
    try:
        assert second.cookie_name != served_pdi.cookie_name
    finally:
        second.server_close()


def test_study_index_is_cached_in_memory(tmp_path: Path) -> None:
    (tmp_path / "VIEWER" / "OHIF").mkdir(parents=True)
    (tmp_path / "VIEWER" / "OHIF" / "index.html").write_text("OHIF")
    (tmp_path / "DICOM").mkdir()
    (tmp_path / "DICOM" / "I000001").write_bytes(b"dicom")
    index_path = _write_study_index(tmp_path)
    expected = index_path.read_bytes()
    server = PdiHttpServer(tmp_path, "127.0.0.1", 0, quiet=True)
    index_path.write_text('{"studies": []}', encoding="utf-8")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        authenticated = _authenticate(server)
        status, _headers, body = _request(
            server, "/api/studies", authenticated
        )
        assert status == 200 and body == expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"studies": {}},
        {"studies": [{"series": {}}]},
        {"studies": [{"series": [{"instances": {}}]}]},
        {
            "studies": [
                {"series": [{"instances": [{"metadata": {}, "url": 3}]}]}
            ]
        },
        _study_index_payload("https://evil.example/DICOM/I000001"),
        _study_index_payload("dicomweb:/DICOM/../secret"),
        _study_index_payload("dicomweb:/DICOM/missing"),
        _study_index_payload("dicomweb:/DICOM/I000001?frame=0"),
    ],
)
def test_server_rejects_invalid_study_index(tmp_path: Path, payload: object) -> None:
    (tmp_path / "DICOM").mkdir()
    (tmp_path / "DICOM" / "I000001").write_bytes(b"dicom")
    _write_study_index(tmp_path, payload)
    with pytest.raises(RuntimeError, match="索引"):
        PdiHttpServer(tmp_path, "127.0.0.1", 0, quiet=True)


def test_server_rejects_malformed_or_oversized_study_index(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "DICOM").mkdir()
    index_path = _write_study_index(tmp_path, {"studies": []})
    index_path.write_bytes(b"not-json")
    with pytest.raises(RuntimeError, match="索引格式"):
        PdiHttpServer(tmp_path, "127.0.0.1", 0, quiet=True)

    index_path.write_text('{"studies": []}', encoding="utf-8")
    monkeypatch.setattr(pdi_server, "MAX_STUDY_INDEX_BYTES", 1)
    with pytest.raises(RuntimeError, match="64 MiB"):
        PdiHttpServer(tmp_path, "127.0.0.1", 0, quiet=True)


def test_server_rejects_dicom_replaced_with_symlink(tmp_path: Path) -> None:
    (tmp_path / "VIEWER" / "OHIF").mkdir(parents=True)
    (tmp_path / "VIEWER" / "OHIF" / "index.html").write_text("OHIF")
    (tmp_path / "DICOM").mkdir()
    dicom_path = tmp_path / "DICOM" / "I000001"
    dicom_path.write_bytes(b"dicom")
    _write_study_index(tmp_path)
    server = PdiHttpServer(tmp_path, "127.0.0.1", 0, quiet=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"patient data")
    dicom_path.unlink()
    try:
        dicom_path.symlink_to(outside)
    except OSError:
        server.server_close()
        pytest.skip("symlinks are unavailable on this platform")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        authenticated = _authenticate(server)
        status, _headers, _body = _request(
            server, "/DICOM/I000001", authenticated
        )
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


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
        idle_timeout_seconds=60,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, _body = _request(
            server,
            f"/ready/{server.session_token}",
            method="HEAD",
        )
        assert status == 200
        with server._idle_condition:
            first_activity = server._last_activity
        status, _headers, _body = _request(
            server,
            f"/ready/{server.session_token}",
            method="HEAD",
        )
        assert status == 200
        with server._idle_condition:
            assert server._last_activity > first_activity
        assert thread.is_alive(), "已认证的就绪探测未续期空闲超时"
        _expire_idle_server(server)
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert server.idle_expired
    finally:
        if thread.is_alive():
            server.shutdown()
            thread.join(timeout=3)
        server.server_close()


def test_unauthorized_requests_do_not_renew_idle_timeout(tmp_path: Path) -> None:
    (tmp_path / "VIEWER" / "OHIF").mkdir(parents=True)
    (tmp_path / "VIEWER" / "OHIF" / "index.html").write_text("OHIF")
    (tmp_path / "DICOM").mkdir()
    _write_study_index(tmp_path, {"studies": []})
    server = PdiHttpServer(
        tmp_path,
        "127.0.0.1",
        0,
        quiet=True,
        idle_timeout_seconds=60,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, _body = _request(
            server,
            f"/ready/{server.session_token}",
            method="HEAD",
        )
        assert status == 200
        with server._idle_condition:
            activity_before_unauthorized_request = server._last_activity
        status, _headers, _body = _request(server, "/api/studies")
        assert status == 403
        with server._idle_condition:
            assert server._last_activity == activity_before_unauthorized_request
        _expire_idle_server(server)
        thread.join(timeout=3)
        assert server.idle_expired, "未授权请求不应续期空闲超时"
        assert not thread.is_alive()
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
        status, headers, body = _request(server, f"/open/{server.session_token}")
        assert status == 303 and body == b""
        assert headers["Location"] == (
            "/viewer/dicomjson/?url=%2Fapi%2Fstudies&lng=zh"
        )
        authenticated = {
            "Cookie": headers["Set-Cookie"].partition(";")[0]
        }
        status, _headers, body = _request(
            server,
            "/viewer/dicomjson/?url=%2Fapi%2Fstudies&lng=zh",
            authenticated,
        )
        assert status == 200 and body == b"OHIF"
        status, _headers, body = _request(
            server, "/api/studies", authenticated
        )
        assert status == 200 and json.loads(body)["studies"] == []
        status, _headers, _body = _request(
            server, "/DCMGET_STUDIES.json", authenticated
        )
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("legacy", [False, True], ids=["hidden-index", "legacy-index"])
def test_app_viewer_uses_directory_route_and_never_serves_media_javascript(
    tmp_path: Path,
    legacy: bool,
) -> None:
    pdi_root = tmp_path / "external-pdi"
    media_viewer = pdi_root / "VIEWER" / "OHIF"
    media_viewer.mkdir(parents=True)
    (media_viewer / "index.html").write_text(
        "MALICIOUS MEDIA VIEWER", encoding="utf-8"
    )
    (media_viewer / "media-only.js").write_text(
        "window.location='https://invalid.example'", encoding="utf-8"
    )
    (pdi_root / "DICOM").mkdir()
    _write_study_index(pdi_root, {"studies": []}, legacy=legacy)
    trusted_viewer = _write_pinned_viewer(tmp_path / "installed-ohif")

    server = PdiHttpServer(
        pdi_root,
        "127.0.0.1",
        0,
        quiet=True,
        viewer_root=trusted_viewer,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, body = _request(server, f"/open/{server.session_token}")
        assert status == 303 and body == b""
        assert headers["Location"] == (
            "/viewer/directory/?url=%2Fapi%2Fstudies&lng=zh"
        )
        authenticated = {"Cookie": headers["Set-Cookie"].partition(";")[0]}
        status, _headers, body = _request(
            server,
            "/viewer/directory/?url=%2Fapi%2Fstudies&lng=zh",
            authenticated,
        )
        assert status == 200 and body == b"TRUSTED OHIF"
        status, _headers, _body = _request(
            server,
            "/media-only.js",
            authenticated,
        )
        assert status == 404
        status, _headers, _body = _request(
            server,
            "/DCMGET_STUDIES.json",
            authenticated,
        )
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_app_viewer_rejects_tampered_pinned_payload(tmp_path: Path) -> None:
    pdi_root = tmp_path / "pdi"
    (pdi_root / "DICOM").mkdir(parents=True)
    _write_study_index(pdi_root, {"studies": []}, legacy=True)
    trusted_viewer = _write_pinned_viewer(tmp_path / "installed-ohif")
    (trusted_viewer / "index.html").write_text("tampered", encoding="utf-8")

    with pytest.raises(RuntimeError, match="校验失败"):
        PdiHttpServer(
            pdi_root,
            "127.0.0.1",
            0,
            quiet=True,
            viewer_root=trusted_viewer,
        )


def test_repeated_frames_validate_one_dicom_path_once(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "DICOM").mkdir()
    (tmp_path / "DICOM" / "I000001").write_bytes(b"DICOM")
    payload = _study_index_payload("dicomweb:/DICOM/I000001?frame=1")
    instances = payload["studies"][0]["series"][0]["instances"]
    instances.append(
        {"metadata": {}, "url": "dicomweb:/DICOM/I000001?frame=2"}
    )
    calls = []
    real_safe_path = pdi_server._safe_dicom_path

    def counted_safe_path(root, request_path):
        calls.append(request_path)
        return real_safe_path(root, request_path)

    monkeypatch.setattr(pdi_server, "_safe_dicom_path", counted_safe_path)

    assert pdi_server._validate_study_index(payload, tmp_path) == {
        "/DICOM/I000001"
    }
    assert calls == ["/DICOM/I000001"]
