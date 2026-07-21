from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import DICOM_download_ui as entry
import dcmget.management_server as management_server_module
from dcmget.config import AppConfig, save_config
from dcmget.management_server import (
    ProfileApiProxy,
    WINDOWS_MANAGEMENT_HOST,
    WindowsManagementService,
    create_windows_management_server,
    _profile_proxy_route_allowed,
)
from dcmget.profile_manager import ProfileInfo, ProfileManager, WINDOWS_MANAGEMENT_PORT
from dcmget.web_server import (
    DcmGetWebServer,
    is_management_peer_address,
    session_cookie_name,
)
from dcmget.profile_runtime_state import (
    PROFILE_RUNTIME_SCHEMA,
    PROFILE_RUNTIME_VERSION,
    ProfileRuntimeState,
    ProfileRuntimeStateError,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANAGER_URL = f"http://192.168.1.50:{WINDOWS_MANAGEMENT_PORT}"


def _profile_manager(tmp_path: Path) -> ProfileManager:
    return ProfileManager(
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
        port_probe=lambda _host, _port: True,
    )


def _write_profile(tmp_path: Path, number: int = 1) -> Path:
    path = tmp_path / "config" / "instances" / f"i{number}" / "config.json"
    save_config(
        path,
        AppConfig(
            storage_port=6666,
            web_port=8787,
            dicom_destination_folder=str(tmp_path / "dicom"),
        ),
    )
    return path


def _csrf(client: TestClient) -> str:
    response = client.get("/api/bootstrap")
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def test_management_service_is_task_free_and_idempotently_stoppable():
    service = WindowsManagementService()

    assert service.snapshot()["status"] == "idle"
    assert service.snapshot()["operation"] == "management"
    assert service.events_since() == []
    assert service.health() == {"ok": True, "mode": "manager"}
    assert service.shutdown() is True
    assert service.shutdown() is True
    assert service.snapshot()["status"] == "stopped"


def test_profile_runtime_state_is_atomic_sorted_and_stopped_by_default(tmp_path: Path):
    path = tmp_path / "management" / "profile-runtime.json"
    state = ProfileRuntimeState(path)

    assert state.desired_profiles() == ()
    state.set_desired(3, True)
    state.set_desired(1, True)
    state.set_desired(3, False)

    assert state.desired_profiles() == (1,)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema": PROFILE_RUNTIME_SCHEMA,
        "version": PROFILE_RUNTIME_VERSION,
        "desired_running_profiles": [1],
    }
    assert list(path.parent.glob(".profile-runtime.json.*.tmp")) == []


def test_profile_runtime_state_rejects_corrupt_file(tmp_path: Path):
    path = tmp_path / "management" / "profile-runtime.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ProfileRuntimeStateError, match="无法读取"):
        ProfileRuntimeState(path).desired_profiles()


def test_session_cookie_names_preserve_default_profile_and_isolate_other_ports():
    assert session_cookie_name(8787) == "dcmget_session"
    assert session_cookie_name(8786) == "dcmget_session_8786"
    assert session_cookie_name(8788) == "dcmget_session_8788"


def test_manager_and_profile_ports_keep_simultaneous_browser_sessions(tmp_path: Path):
    manager = create_windows_management_server(
        profile_manager=_profile_manager(tmp_path),
        project_root=PROJECT_ROOT,
        state_directory=tmp_path / "manager-state",
        trusted_hosts=("127.0.0.1",),
    )
    profile = DcmGetWebServer(
        WindowsManagementService(),
        state_directory=tmp_path / "profile-state",
        host="0.0.0.0",
        port=8787,
        trusted_hosts=("127.0.0.1",),
    )

    class PortDispatcher:
        async def __call__(self, scope, receive, send):
            port = int(scope.get("server", ("", 0))[1])
            app = manager.app if port == 8786 else profile.app
            await app(scope, receive, send)

    browser = TestClient(
        PortDispatcher(),
        base_url="http://127.0.0.1:8786",
        client=("127.0.0.1", 50127),
    )

    manager_bootstrap = browser.get("http://127.0.0.1:8786/api/bootstrap")
    profile_bootstrap = browser.get("http://127.0.0.1:8787/api/bootstrap")

    assert manager_bootstrap.status_code == 200
    assert profile_bootstrap.status_code == 200
    assert manager_bootstrap.json()["csrf_token"] != profile_bootstrap.json()["csrf_token"]
    assert browser.cookies.get("dcmget_session_8786")
    assert browser.cookies.get("dcmget_session")
    assert manager.app.state.session_cookie_name == "dcmget_session_8786"
    assert profile.app.state.session_cookie_name == "dcmget_session"
    assert browser.get("http://127.0.0.1:8786/api/snapshot").status_code == 200
    assert browser.get("http://127.0.0.1:8787/api/snapshot").status_code == 200


def test_management_hub_uses_fixed_listener_and_survives_broken_profile_config(
    tmp_path: Path,
):
    broken = tmp_path / "config" / "instances" / "i1" / "config.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{broken profile", encoding="utf-8")
    original = broken.read_bytes()
    server = create_windows_management_server(
        profile_manager=_profile_manager(tmp_path),
        project_root=PROJECT_ROOT,
        state_directory=tmp_path / "manager-state",
        trusted_hosts=("127.0.0.1",),
        static_root=PROJECT_ROOT / "dcmget" / "webui",
    )

    assert server.host == WINDOWS_MANAGEMENT_HOST == "0.0.0.0"
    assert server.port == WINDOWS_MANAGEMENT_PORT == 8786
    assert server.management_mode is True
    assert server._make_uvicorn_server().config.proxy_headers is False

    with TestClient(
        server.app,
        base_url=f"http://127.0.0.1:{WINDOWS_MANAGEMENT_PORT}",
        client=("127.0.0.1", 50123),
    ) as client:
        assert client.get("/?page=operations").status_code == 200
        csrf = _csrf(client)
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["mode"] == "manager"
        assert bootstrap["profile"]["mode"] == "manager"
        assert bootstrap["profile"]["server_port"] == WINDOWS_MANAGEMENT_PORT
        assert bootstrap["config"]["web_port"] == WINDOWS_MANAGEMENT_PORT

        failed_list = client.post(
            "/api/operations/profile-list",
            json={},
            headers={
                "Origin": f"http://127.0.0.1:{WINDOWS_MANAGEMENT_PORT}",
                "X-CSRF-Token": csrf,
            },
        )
        assert failed_list.status_code == 409
        assert client.get("/api/bootstrap").status_code == 200

    assert broken.read_bytes() == original


def test_first_management_start_creates_one_stopped_default_profile(tmp_path: Path):
    manager = _profile_manager(tmp_path)
    state_directory = tmp_path / "manager-state"
    server = create_windows_management_server(
        profile_manager=manager,
        project_root=PROJECT_ROOT,
        state_directory=state_directory,
        trusted_hosts=("127.0.0.1",),
    )

    profiles = manager.list_profiles()

    assert len(profiles) == 1
    assert profiles[0].number == 1
    assert not profiles[0].is_running
    assert ProfileRuntimeState(
        state_directory / "profile-runtime.json"
    ).desired_profiles() == ()
    with TestClient(
        server.app,
        base_url="http://127.0.0.1:8786",
        client=("127.0.0.1", 50123),
    ) as client:
        _csrf(client)
        response = client.get("/api/management/profiles")
    assert response.status_code == 200
    assert response.json()["profiles"][0]["desired_running"] is False


def test_private_manager_peer_keeps_host_origin_session_and_csrf_controls(
    tmp_path: Path,
):
    _write_profile(tmp_path)
    server = create_windows_management_server(
        profile_manager=_profile_manager(tmp_path),
        project_root=PROJECT_ROOT,
        state_directory=tmp_path / "manager-state",
        trusted_hosts=("192.168.1.50",),
        static_root=PROJECT_ROOT / "dcmget" / "webui",
    )
    client = TestClient(
        server.app,
        base_url=MANAGER_URL,
        client=("192.168.1.20", 50124),
    )
    csrf = _csrf(client)

    missing_csrf = client.post(
        "/api/operations/profile-list",
        json={},
        headers={"Origin": MANAGER_URL},
    )
    assert missing_csrf.status_code == 403
    bad_origin = client.post(
        "/api/operations/profile-list",
        json={},
        headers={
            "Origin": f"http://attacker.invalid:{WINDOWS_MANAGEMENT_PORT}",
            "X-CSRF-Token": csrf,
        },
    )
    assert bad_origin.status_code == 403
    listed = client.post(
        "/api/operations/profile-list",
        json={},
        headers={"Origin": MANAGER_URL, "X-CSRF-Token": csrf},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["count"] == 1


def test_management_hub_exposes_same_origin_profile_proxy(
    tmp_path: Path,
):
    calls: list[dict[str, object]] = []

    async def proxy(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {
            "status_code": 200,
            "payload": {"csrf_token": "upstream-secret", "status": "idle"},
        }

    server = DcmGetWebServer(
        WindowsManagementService(),
        state_directory=tmp_path / "manager-state",
        host="0.0.0.0",
        port=8786,
        trusted_hosts=("192.168.1.50",),
        management_mode=True,
        profile_api_proxy=proxy,
    )
    client = TestClient(
        server.app,
        base_url=MANAGER_URL,
        client=("192.168.1.20", 50124),
    )
    csrf = _csrf(client)

    response = client.get("/api/management/profiles/2/bootstrap")

    assert response.status_code == 200
    assert response.json() == {
        "csrf_token": csrf,
        "status": "idle",
        "web": {
            "url": f"{MANAGER_URL}/",
            "lan_url": f"{MANAGER_URL}/",
            "local_session": False,
        },
    }
    assert calls[0]["profile_number"] == 2
    assert calls[0]["method"] == "GET"
    denied = client.post(
        "/api/management/profiles/2/task/pause",
        json={},
        headers={"Origin": MANAGER_URL},
    )
    assert denied.status_code == 403
    allowed = client.post(
        "/api/management/profiles/2/task/pause",
        json={},
        headers={"Origin": MANAGER_URL, "X-CSRF-Token": csrf},
    )
    assert allowed.status_code == 200


def test_profile_proxy_allowlist_blocks_shutdown_and_arbitrary_paths():
    assert _profile_proxy_route_allowed("GET", "snapshot")
    assert _profile_proxy_route_allowed("POST", "operations/open-destination")
    assert _profile_proxy_route_allowed("POST", "operations/profile-backup")
    assert _profile_proxy_route_allowed("POST", "operations/support-bundle")
    assert not _profile_proxy_route_allowed("POST", "ops/shutdown")
    assert not _profile_proxy_route_allowed("GET", "http://attacker.invalid")


def test_profile_proxy_uses_loopback_configured_port_and_internal_csrf(tmp_path: Path):
    profile = ProfileInfo(
        number=2,
        display_name="实例 2",
        config_path=tmp_path / "config.json",
        pacs_server_ip="203.0.113.20",
        pacs_server_port=104,
        calling_ae_title="DCMGET",
        pacs_ae_title="PACS",
        storage_ae_title="DCMGET",
        storage_port=6667,
        web_port=8899,
        destination_directory=str(tmp_path / "dicom"),
        is_running=True,
        has_recovery=False,
    )

    class Manager:
        def get_profile(self, number: int) -> ProfileInfo:
            assert number == 2
            return profile

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok":true}'

    requests = []

    class Opener:
        def open(self, request, timeout: float):
            requests.append((request, timeout))
            return Response()

    proxy = ProfileApiProxy(Manager())  # type: ignore[arg-type]

    def profile_session(_profile: ProfileInfo):
        return Opener(), "internal-csrf"

    proxy._profile_session = profile_session  # type: ignore[method-assign]

    result = proxy._request_sync(
        2,
        "POST",
        "task/pause",
        "",
        b"{}",
        "application/json",
    )

    request = requests[0][0]
    assert result == {"status_code": 200, "payload": {"ok": True}}
    assert request.full_url == "http://127.0.0.1:8899/api/task/pause"
    assert request.get_header("X-csrf-token") == "internal-csrf"
    assert request.get_header("Origin") == "http://127.0.0.1:8899"


def test_profile_proxy_rejects_running_topology_changes_but_allows_unchanged_values(
    tmp_path: Path,
):
    profile = ProfileInfo(
        number=2,
        display_name="实例 2",
        config_path=tmp_path / "config.json",
        pacs_server_ip="203.0.113.20",
        pacs_server_port=104,
        calling_ae_title="DCMGET",
        pacs_ae_title="PACS",
        storage_ae_title="DCMGET",
        storage_port=6667,
        web_port=8899,
        destination_directory=str(tmp_path / "dicom"),
        is_running=True,
        has_recovery=False,
    )

    class Manager:
        def get_profile(self, number: int) -> ProfileInfo:
            assert number == 2
            return profile

    calls: list[tuple[object, ...]] = []
    proxy = ProfileApiProxy(Manager())  # type: ignore[arg-type]

    def perform(*args: object) -> dict[str, object]:
        calls.append(args)
        return {"status_code": 200, "payload": {"ok": True}}

    proxy._perform = perform  # type: ignore[method-assign]

    blocked = proxy._request_sync(
        2,
        "PUT",
        "config",
        "",
        json.dumps({"web_port": 8900, "storage_ae_title": "OTHER"}).encode(),
        "application/json",
    )

    assert blocked == {
        "status_code": 409,
        "payload": {
            "detail": "实例正在运行，请先停止后再修改端口或 AE",
            "fields": ["storage_ae_title", "web_port"],
        },
    }
    assert calls == []

    allowed = proxy._request_sync(
        2,
        "PUT",
        "config",
        "",
        json.dumps(
            {
                "calling_ae_title": "DCMGET",
                "pacs_ae_title": "PACS",
                "storage_ae_title": "DCMGET",
                "storage_port": "6667",
                "web_port": 8899,
                "pdi_export_enabled": True,
            }
        ).encode(),
        "application/json",
    )

    assert allowed == {"status_code": 200, "payload": {"ok": True}}
    assert len(calls) == 1


def test_profile_proxy_slow_profile_does_not_block_other_profiles(tmp_path: Path):
    profiles = {
        number: ProfileInfo(
            number=number,
            display_name=f"实例 {number}",
            config_path=tmp_path / f"config-{number}.json",
            pacs_server_ip="127.0.0.1",
            pacs_server_port=104,
            calling_ae_title=f"CALL{number}",
            pacs_ae_title="PACS",
            storage_ae_title=f"STORE{number}",
            storage_port=6665 + number,
            web_port=8786 + number,
            destination_directory=str(tmp_path / f"dicom-{number}"),
            is_running=True,
            has_recovery=False,
        )
        for number in (1, 2)
    }
    first_started = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()

    class Manager:
        def get_profile(self, number: int) -> ProfileInfo:
            return profiles[number]

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok":true}'

    class Opener:
        def __init__(self, profile_number: int) -> None:
            self.profile_number = profile_number

        def open(self, _request: object, timeout: float) -> Response:
            assert timeout == 30.0
            if self.profile_number == 1:
                first_started.set()
                assert release_first.wait(2)
            else:
                second_done.set()
            return Response()

    proxy = ProfileApiProxy(Manager())  # type: ignore[arg-type]
    proxy._profile_session = (  # type: ignore[method-assign]
        lambda profile: (Opener(profile.number), "internal-csrf")
    )

    first = threading.Thread(
        target=proxy._perform,
        args=(profiles[1], "GET", "snapshot", "", b"", "application/json"),
    )
    second = threading.Thread(
        target=proxy._perform,
        args=(profiles[2], "GET", "snapshot", "", b"", "application/json"),
    )
    first.start()
    assert first_started.wait(2)
    second.start()
    assert second_done.wait(1)
    release_first.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()


def test_public_actual_peer_cannot_mutate_manager_via_proxy_headers(tmp_path: Path):
    _write_profile(tmp_path)
    server = create_windows_management_server(
        profile_manager=_profile_manager(tmp_path),
        project_root=PROJECT_ROOT,
        state_directory=tmp_path / "manager-state",
        trusted_hosts=("192.168.1.50",),
    )
    client = TestClient(
        server.app,
        base_url=MANAGER_URL,
        client=("8.8.8.8", 50125),
    )
    csrf = _csrf(client)

    denied_read = client.get("/api/management/profiles")

    denied = client.post(
        "/api/operations/profile-list",
        json={},
        headers={
            "Origin": MANAGER_URL,
            "X-CSRF-Token": csrf,
            "X-Forwarded-For": "192.168.1.20",
            "Forwarded": "for=192.168.1.20",
        },
    )

    assert denied_read.status_code == 403
    assert denied.status_code == 403


def test_profile_server_operations_remain_loopback_only_for_private_remote(
    tmp_path: Path,
):
    called: list[dict[str, object]] = []
    server = DcmGetWebServer(
        WindowsManagementService(),
        state_directory=tmp_path / "profile-state",
        host="0.0.0.0",
        port=8787,
        trusted_hosts=("192.168.1.50",),
        profile_metadata={"mode": "profile"},
        operation_handlers={"profile-list": lambda payload: called.append(payload) or {}},
    )
    client = TestClient(
        server.app,
        base_url="http://192.168.1.50:8787",
        client=("192.168.1.20", 50126),
    )
    csrf = _csrf(client)

    denied = client.post(
        "/api/operations/profile-list",
        json={},
        headers={
            "Origin": "http://192.168.1.50:8787",
            "X-CSRF-Token": csrf,
        },
    )

    assert denied.status_code == 403
    assert called == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.20",
        "fc00::10",
        "fd12:3456::1",
        "::ffff:192.168.1.20",
    ],
)
def test_management_peer_policy_allows_only_loopback_rfc1918_and_ula(address: str):
    assert is_management_peer_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "",
        "localhost",
        "8.8.8.8",
        "100.64.0.1",
        "169.254.1.1",
        "172.15.255.255",
        "172.32.0.1",
        "203.0.113.1",
        "fe80::1",
        "2001:db8::1",
    ],
)
def test_management_peer_policy_rejects_everything_else(address: str):
    assert not is_management_peer_address(address)


def test_hidden_cli_manager_mode_skips_profile_and_dcmtk_startup(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(entry, "ensure_supported_runtime", lambda: None)
    monkeypatch.setattr(entry, "_lan_hosts", lambda: ("127.0.0.1", "192.168.1.50"))
    monkeypatch.setattr(
        entry,
        "validate_web_resources",
        lambda _root=entry.PROJECT_ROOT: entry.PROJECT_ROOT / "dcmget" / "webui",
    )
    monkeypatch.setattr(
        entry,
        "run_windows_management_server",
        lambda **kwargs: calls.append(kwargs) or 0,
    )
    monkeypatch.setattr(
        entry,
        "prepare_windows_portable_dcmtk",
        lambda *_args, **_kwargs: pytest.fail("管理中心不应准备 DCMTK"),
    )
    monkeypatch.setattr(
        entry,
        "migrate_legacy_task_state",
        lambda *_args, **_kwargs: pytest.fail("管理中心不应迁移 Profile 任务"),
    )
    monkeypatch.setattr(
        entry,
        "acquire_instance_profile",
        lambda *_args, **_kwargs: pytest.fail("管理中心不应获取 Profile"),
    )

    assert entry.main(["--windows-management", "--no-open-browser"]) == 0
    assert len(calls) == 1
    assert calls[0]["trusted_hosts"] == ("127.0.0.1", "192.168.1.50")
    assert entry.build_parser().parse_args(["--windows-management"]).windows_management
    assert "--windows-management" not in entry.build_parser().format_help()


def test_management_runner_always_stops_server(monkeypatch: pytest.MonkeyPatch):
    events: list[object] = []

    class FakeServer:
        def run(self) -> None:
            events.append("run")

        def stop(self, timeout: float) -> None:
            events.append(("stop", timeout))

    monkeypatch.setattr(
        management_server_module,
        "create_windows_management_server",
        lambda **_kwargs: FakeServer(),
    )

    assert management_server_module.run_windows_management_server() == 0
    assert events == ["run", ("stop", 15)]
