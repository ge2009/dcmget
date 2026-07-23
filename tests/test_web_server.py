from __future__ import annotations

import sys
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from dcmget import web_server as web_server_module
from dcmget.config import AppConfig, save_config
from dcmget.core import ToolPaths
from dcmget.web_security import bootstrap_web_security
from dcmget.web_server import DcmGetWebServer, create_web_app


PORT = 8787
LOCAL_URL = f"http://127.0.0.1:{PORT}"
LOCAL_ORIGIN = {"Origin": LOCAL_URL}


class FakeService:
    def __init__(self):
        self.status = "idle"
        self.task_id = ""
        self.started_with: tuple[AppConfig, ToolPaths, list[str]] | None = None
        self.calls: list[str] = []
        self.stopped = False
        self.events = [
            {
                "id": 1,
                "type": "log",
                "timestamp": "2026-07-20T00:00:00+00:00",
                "payload": {"level": "error", "source": "test", "message": "failure"},
            }
        ]

    def snapshot(self) -> dict[str, Any]:
        active = self.status in {"starting_receiver", "downloading", "paused"}
        return {
            "event_id": 1,
            "status": self.status,
            "message": "",
            "operation": "download" if active else "",
            "task": {
                "id": self.task_id,
                "profile": "i1",
                "total": 2 if self.task_id else 0,
                "large_batch": False,
                "accessions": ["A1", "A2"] if self.task_id else [],
                "destination": "",
            },
            "progress": {
                "processed": 0,
                "total": 2 if self.task_id else 0,
                "file_count": 0,
                "speed_bytes_per_second": 0,
            },
            "results": [],
            "pdi": None,
            "verification": None,
            "actions": {"can_start": not active, "can_cancel": active},
            "authorization": {"registered": False, "trial_remaining": 30},
            "error_logs": [],
        }

    def start_task(self, config: AppConfig, tools: ToolPaths, accessions: list[str]):
        assert isinstance(config, AppConfig)
        assert isinstance(tools, ToolPaths)
        self.started_with = (config, tools, list(accessions))
        self.task_id = "task-1"
        self.status = "downloading"
        return self.snapshot()

    def resume_task(self, tools: ToolPaths):
        self.calls.append("resume_task")
        self.status = "downloading"
        return self.snapshot()

    def pause(self):
        self.calls.append("pause")
        self.status = "paused"
        return self.snapshot()

    def resume(self):
        self.calls.append("resume")
        self.status = "downloading"
        return self.snapshot()

    def cancel(self):
        self.calls.append("cancel")
        self.status = "cancelled"
        return self.snapshot()

    def end_task(self):
        self.calls.append("end_task")
        self.status = "ended"
        return self.snapshot()

    def retry_failed(self, tools: ToolPaths):
        self.calls.append("retry_failed")
        return self.snapshot()

    def accept_partial(self):
        self.calls.append("accept_partial")
        return self.snapshot()

    def retry_pdi(self, tools: ToolPaths):
        self.calls.append("retry_pdi")
        return self.snapshot()

    def verify_pdi(self, root: str):
        self.calls.append(f"verify:{root}")
        return {"ok": True, "root": root}

    def events_since(self, after_id: int = 0, *, limit: int = 200):
        return [event for event in self.events if event["id"] > after_id][:limit]

    def subscribe(self, callback):
        return lambda: None

    def shutdown(self):
        self.stopped = True
        return True


@dataclass
class WebFixture:
    service: FakeService
    client: TestClient
    security: Any
    config_path: Path
    allowed_root: Path
    tools: ToolPaths

    def setup(self) -> str:
        response = self.client.get("/api/bootstrap")
        assert response.status_code == 200, response.text
        return response.json()["csrf_token"]


@pytest.fixture
def web(tmp_path: Path) -> WebFixture:
    state = tmp_path / "state"
    config_path = tmp_path / "config.json"
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    config = AppConfig(
        dicom_destination_folder=str(allowed),
        pdi_output_folder=str(allowed / "PDI"),
        pdi_institution_name="测试医院",
        web_port=PORT,
    )
    save_config(config_path, config)
    tools = ToolPaths(
        movescu=tmp_path / "bin" / "movescu",
        storescp=tmp_path / "bin" / "storescp",
        bin_dir=tmp_path / "bin",
        version="3.7.0",
    )
    service = FakeService()
    security = bootstrap_web_security(state)
    app = create_web_app(
        service,
        security=security,
        server_port=PORT,
        trusted_hosts=("127.0.0.1", "192.168.1.50"),
        static_root=Path(__file__).resolve().parents[1] / "dcmget" / "webui",
        directory_roots={"data": allowed},
        config_path=config_path,
        project_root=tmp_path,
        profile_metadata={
            "name": "i1",
            "data_dir": str(state),
            "lan_url": "http://192.168.1.50:8787/",
        },
        tools_provider=lambda _config: tools,
        preflight_provider=lambda _config: {
            "checks": [
                ("DCMTK 工具", True, "3.7.0"),
                ("保存目录", True, "可写"),
                ("接收端口", True, "可用"),
            ],
            "errors": {},
        },
    )
    client = TestClient(
        app,
        base_url=LOCAL_URL,
        client=("127.0.0.1", 50123),
    )
    return WebFixture(service, client, security, config_path, allowed, tools)


def test_anonymous_bootstrap_creates_ip_bound_session_and_loads_app(web: WebFixture):
    initial = web.client.get("/api/bootstrap")
    assert initial.status_code == 200
    assert initial.json()["auth"] == {
        "authenticated": True,
        "setup_required": False,
        "first_run": False,
        "passwordless": True,
    }
    assert initial.json()["setup_required"] is False
    assert initial.json()["can_setup_here"] is False
    assert initial.json()["csrf_token"]
    assert initial.json()["web"]["insecure_http"] is True
    assert initial.json()["web"]["lan_url"] == "http://192.168.1.50:8787/"
    assert initial.json()["web"]["local_session"] is True
    assert initial.json()["config"]["web_port"] == PORT
    assert initial.json()["profile"]["name"] == "i1"
    assert initial.json()["task"]["status"] == "idle"

    remote = TestClient(
        web.client.app,
        base_url="http://192.168.1.50:8787",
        client=("192.168.1.20", 50124),
    )
    remote_bootstrap = remote.get("/api/bootstrap").json()
    assert remote_bootstrap["authenticated"] is True
    assert remote_bootstrap["csrf_token"]
    assert remote_bootstrap["can_setup_here"] is False
    assert remote_bootstrap["web"]["local_session"] is False

    assert web.client.get("/").status_code == 200
    assert web.client.get("/favicon.ico").status_code == 204
    assert web.client.get("/assets/app.js").status_code == 200
    assert web.client.get("/assets/theme.js").status_code == 200


def test_nicegui_workspace_uses_scoped_csp_and_secures_websocket_session(
    tmp_path: Path,
):
    security = bootstrap_web_security(tmp_path / "state")
    app = create_web_app(
        FakeService(),
        security=security,
        server_host="127.0.0.1",
        server_port=PORT,
        trusted_hosts=("127.0.0.1",),
        nicegui_mount_path="/workspace",
    )

    @app.websocket("/workspace/_nicegui_ws/socket.io/")
    async def test_socket(socket: WebSocket):
        await socket.accept()
        await socket.send_text("ready")
        await socket.close()

    client = TestClient(
        app,
        base_url=LOCAL_URL,
        client=("127.0.0.1", 50126),
    )
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/workspace/"
    assert client.cookies.get("dcmget_session")

    workspace = client.get("/workspace/missing")
    assert workspace.status_code == 404
    workspace_csp = workspace.headers["content-security-policy"]
    assert "style-src 'self' 'unsafe-inline'" in workspace_csp
    assert "script-src 'self' 'unsafe-inline' 'unsafe-eval'" in workspace_csp
    assert f"connect-src 'self' ws://127.0.0.1:{PORT} wss://127.0.0.1:{PORT}" in workspace_csp
    assert "connect-src 'self' ws: wss:" not in workspace_csp

    api = client.get("/api/bootstrap")
    api_csp = api.headers["content-security-policy"]
    assert "'unsafe-inline'" not in api_csp
    assert "'unsafe-eval'" not in api_csp
    assert "ws:" not in api_csp

    with client.websocket_connect(
        "/workspace/_nicegui_ws/socket.io/",
        headers={
            "Host": f"127.0.0.1:{PORT}",
            "Origin": LOCAL_URL,
            "Cookie": f"dcmget_session={client.cookies.get('dcmget_session')}",
        },
    ) as socket:
        assert socket.receive_text() == "ready"

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/workspace/_nicegui_ws/socket.io/",
            headers={
                "Host": f"127.0.0.1:{PORT}",
                "Origin": "http://attacker.invalid",
                "Cookie": f"dcmget_session={client.cookies.get('dcmget_session')}",
            },
        ):
            pass

    no_session = TestClient(
        app,
        base_url=LOCAL_URL,
        client=("127.0.0.1", 50127),
    )
    with pytest.raises(WebSocketDisconnect):
        with no_session.websocket_connect(
            "/workspace/_nicegui_ws/socket.io/",
            headers={"Host": f"127.0.0.1:{PORT}", "Origin": LOCAL_URL},
        ):
            pass


def test_nicegui_workspace_mounts_in_an_isolated_runtime(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    code = f"""
from pathlib import Path
from fastapi.testclient import TestClient
from nicegui import run as nicegui_run
from dcmget.management_server import WindowsManagementService
from dcmget.nicegui_ui import install_nicegui
from dcmget.web_security import bootstrap_web_security
from dcmget.web_server import create_web_app

state = Path({str(tmp_path / 'nicegui-state')!r})
security = bootstrap_web_security(state)
app = create_web_app(
    WindowsManagementService(),
    security=security,
    server_host='127.0.0.1',
    server_port={PORT},
    trusted_hosts=('127.0.0.1',),
    nicegui_mount_path='/workspace',
)
install_nicegui(app, mount_path='/workspace')
nicegui_run.setup = lambda: None
with TestClient(
    app,
    base_url='http://127.0.0.1:{PORT}',
    client=('127.0.0.1', 50128),
) as client:
    root_response = client.get('/', follow_redirects=False)
    assert root_response.status_code == 307
    page = client.get('/workspace/')
    assert page.status_code == 200
    assert 'DcmGet DICOM 影像下载' in page.text
    assert 'data-dcmget-theme-bootstrap' in page.text
    assert '/workspace/_nicegui/' in page.text
    assert client.get('/workspace/favicon.ico').status_code == 200
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_windowed_runtime_builds_uvicorn_server_without_console_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    server = DcmGetWebServer(
        FakeService(),
        state_directory=tmp_path / "state",
        host="127.0.0.1",
        port=18787,
        static_root=Path(__file__).resolve().parents[1] / "dcmget" / "webui",
    )

    uvicorn_server = server._make_uvicorn_server()

    assert uvicorn_server.config.log_config is None


def test_host_origin_session_and_csrf_are_enforced(web: WebFixture):
    csrf = web.setup()

    bad_host = web.client.get("/api/task", headers={"Host": "attacker.example:8787"})
    assert bad_host.status_code == 421
    no_csrf = web.client.post("/api/task/pause", headers=LOCAL_ORIGIN, json={})
    assert no_csrf.status_code == 403
    bad_origin = web.client.post(
        "/api/task/pause",
        headers={"Origin": "http://attacker.example:8787", "X-CSRF-Token": csrf},
        json={},
    )
    assert bad_origin.status_code == 403
    unauthorized = TestClient(
        web.client.app,
        base_url=LOCAL_URL,
        client=("127.0.0.1", 50125),
    ).get("/api/events/stream")
    assert unauthorized.status_code == 401

    response = web.client.get("/api/task")
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert "access-control-allow-origin" not in response.headers


def test_intentional_bare_http_is_reported_as_warning_not_failure(web: WebFixture):
    csrf = web.setup()

    response = web.client.post(
        "/api/operations/health",
        json={},
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    transport = next(
        check for check in response.json()["checks"] if check["name"] == "Web 传输模式"
    )
    assert transport["ok"] is True
    assert transport["severity"] == "warning"


def test_task_routes_build_server_side_config_and_tools(web: WebFixture):
    csrf = web.setup()
    payload = {
        "accessions": ["A1", "A2"],
        "destination": str(web.allowed_root),
        "pdi": {"enabled": True, "output_folder": str(web.allowed_root / "PDI")},
        "tools": {"movescu": "C:/attacker.exe"},
    }

    preflight = web.client.post(
        "/api/preflight",
        json=payload,
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )
    assert preflight.status_code == 200
    assert preflight.json()["ok"]
    assert {item["key"] for item in preflight.json()["checks"]} >= {
        "config",
        "dcmtk",
        "destination",
        "receiver",
    }

    response = web.client.post(
        "/api/task/start",
        json=payload,
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )

    assert response.status_code == 200, response.text
    config, tools, accessions = web.service.started_with or (None, None, None)
    assert config.dicom_destination_folder == str(web.allowed_root)
    assert config.pdi_export_enabled
    assert tools is web.tools
    assert accessions == ["A1", "A2"]
    assert response.json()["task"]["status"] == "downloading"

    pause = web.client.post(
        "/api/task/pause",
        json={},
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )
    assert pause.status_code == 200
    assert pause.json()["task"]["status"] == "paused"
    resumed = web.client.post(
        "/api/task/resume",
        json={},
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )
    assert resumed.status_code == 200
    assert web.service.calls[-1] == "resume"

    web.service.status = "interrupted"
    recovered = web.client.post(
        "/api/task/resume",
        json={},
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )
    assert recovered.status_code == 200
    assert web.service.calls[-1] == "resume_task"
    assert recovered.json()["task"]["status"] == "downloading"


def test_preflight_reports_current_task_before_its_receiver_port_as_conflict(
    web: WebFixture,
):
    csrf = web.setup()
    web.service.task_id = "active-task"
    web.service.status = "downloading"

    response = web.client.post(
        "/api/preflight",
        json={
            "accessions": ["A1"],
            "destination": str(web.allowed_root),
        },
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert set(payload["errors"]) == {"task_state"}
    assert payload["checks"] == [
        {
            "key": "task",
            "name": "当前任务",
            "ok": False,
            "message": (
                "当前 Profile 已有下载任务，接收端口 6666 "
                "正由该任务使用；请先继续或结束当前任务，不能同时新建任务"
            ),
        }
    ]


def test_end_task_route_is_distinct_from_recoverable_cancel(web: WebFixture):
    csrf = web.setup()
    web.service.task_id = "active-task"
    web.service.status = "interrupted"

    response = web.client.post(
        "/api/tasks/end",
        json={},
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert web.service.calls == ["end_task"]
    assert response.json()["task"]["status"] == "ended"


def test_default_web_preflight_still_reports_external_receiver_port_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state = tmp_path / "state"
    config_path = tmp_path / "config.json"
    destination = tmp_path / "dicom"
    destination.mkdir()
    save_config(
        config_path,
        AppConfig(
            dicom_destination_folder=str(destination),
            storage_port=6663,
            web_port=PORT,
        ),
    )
    tools = ToolPaths(
        movescu=tmp_path / "bin" / "movescu",
        storescp=tmp_path / "bin" / "storescp",
        bin_dir=tmp_path / "bin",
        version="3.7.0",
    )
    observed: list[bool] = []

    def fake_preflight(config, _resolver, *, check_port=True):
        observed.append(bool(check_port))
        return {
            "checks": [
                (
                    "接收端口",
                    False,
                    "端口 6663 已被其他程序占用",
                )
            ],
            "errors": {"storage_port": "端口 6663 已被其他程序占用"},
        }

    monkeypatch.setattr(web_server_module, "run_core_preflight", fake_preflight)
    security = bootstrap_web_security(state)
    app = create_web_app(
        FakeService(),
        security=security,
        server_port=PORT,
        trusted_hosts=("127.0.0.1",),
        static_root=Path(__file__).resolve().parents[1] / "dcmget" / "webui",
        directory_roots={"data": destination},
        config_path=config_path,
        project_root=tmp_path,
        tools_provider=lambda _config: tools,
    )
    with TestClient(
        app,
        base_url=LOCAL_URL,
        client=("127.0.0.1", 50123),
    ) as client:
        csrf = client.get("/api/bootstrap").json()["csrf_token"]
        response = client.post(
            "/api/preflight",
            json={"accessions": ["A1"], "destination": str(destination)},
            headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
        )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert observed == [True]
    receiver = next(
        item for item in response.json()["checks"] if item["key"] == "receiver"
    )
    assert receiver["message"] == "端口 6663 已被其他程序占用"


def test_config_update_is_validated_and_locked_during_active_task(web: WebFixture):
    csrf = web.setup()
    current = web.client.get("/api/config").json()["config"]
    current["pacs_server_port"] = 11112
    saved = web.client.put(
        "/api/config",
        json=current,
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )
    assert saved.status_code == 200
    assert saved.json()["config"]["pacs_server_port"] == 11112

    partial = web.client.put(
        "/api/config",
        json={"pacs_server_port": 11113},
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )
    assert partial.status_code == 200
    assert partial.json()["config"]["pacs_server_port"] == 11113
    assert partial.json()["config"]["storage_port"] == current["storage_port"]
    assert partial.json()["config"]["dicom_destination_folder"] == current["dicom_destination_folder"]

    web.service.status = "downloading"
    blocked = web.client.put(
        "/api/config",
        json=current,
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )
    assert blocked.status_code == 409


def test_raw_accession_import_and_safe_directory_listing(web: WebFixture):
    csrf = web.setup()
    (web.allowed_root / "child").mkdir()
    imported = web.client.post(
        "/api/files/accessions",
        content="A001\n\nA001\nA002\n".encode(),
        headers={
            **LOCAL_ORIGIN,
            "X-CSRF-Token": csrf,
            "X-File-Name": "access.txt",
            "Content-Type": "text/plain",
        },
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["accessions"] == ["A001", "A002"]
    assert imported.json()["duplicate_count"] == 1

    listing = web.client.get(
        "/api/files/directories",
        params={"path": str(web.allowed_root), "purpose": "destination"},
    )
    assert listing.status_code == 200
    assert listing.json()["directories"] == [
        {"name": "child", "path": str(web.allowed_root / "child")}
    ]
    escaped = web.client.get(
        "/api/files/directories",
        params={"path": str(web.allowed_root.parent), "purpose": "destination"},
    )
    assert escaped.status_code == 400


def test_remote_anonymous_session_cannot_shutdown_server(web: WebFixture):
    csrf = web.setup()
    web.client.app.routes  # Keep fixture app alive for both clients.
    # The fixture did not install a shutdown callback; local shutdown still
    # exercises service cleanup, while the remote request must not reach it.
    remote = TestClient(
        web.client.app,
        base_url="http://192.168.1.50:8787",
        client=("192.168.1.20", 50124),
    )
    remote_bootstrap = remote.get("/api/bootstrap").json()
    remote_csrf = remote_bootstrap["csrf_token"]
    assert remote_bootstrap["web"]["local_session"] is False
    denied = remote.post(
        "/api/ops/shutdown",
        json={},
        headers={
            "Origin": "http://192.168.1.50:8787",
            "X-CSRF-Token": remote_csrf,
        },
    )
    assert denied.status_code == 403
    assert not web.service.stopped

    local = web.client.post(
        "/api/operations/shutdown",
        json={},
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )
    assert local.status_code == 200
    assert web.service.stopped


def test_shutdown_failure_keeps_web_server_running_and_returns_conflict(
    web: WebFixture,
):
    csrf = web.setup()
    web.service.shutdown = lambda: False  # type: ignore[method-assign]

    response = web.client.post(
        "/api/ops/shutdown",
        json={},
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert "DCMTK" in response.json()["detail"]


def test_legacy_auth_endpoints_remain_compatible_without_password(web: WebFixture):
    csrf = web.setup()
    changed = web.client.post(
        "/api/admin/password",
        json={},
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )
    assert changed.status_code == 200
    assert changed.json() == {
        "ok": True,
        "passwordless": True,
        "login_required": False,
    }

    login = web.client.post(
        "/api/login",
        json={},
        headers=LOCAL_ORIGIN,
    )
    assert login.status_code == 200
    assert login.json()["passwordless"] is True

    legacy_setup = web.client.post(
        "/api/setup",
        json={},
        headers=LOCAL_ORIGIN,
    )
    assert legacy_setup.status_code == 200
    assert legacy_setup.json()["bootstrap"]["auth"]["passwordless"] is True


def test_legacy_login_never_requests_or_rate_limits_password(web: WebFixture):
    for _attempt in range(8):
        response = web.client.post(
            "/api/login",
            json={"password": "legacy-value-is-ignored"},
            headers=LOCAL_ORIGIN,
        )
        assert response.status_code == 200
        assert response.json()["passwordless"] is True
        assert "retry-after" not in response.headers


def test_license_status_exposes_machine_code_and_rejects_bad_token(web: WebFixture):
    csrf = web.setup()

    status = web.client.get("/api/license")
    assert status.status_code == 200
    assert status.json()["machine_code"]
    assert "license" in status.json()

    invalid = web.client.post(
        "/api/license/activate",
        json={"token": "not-a-license"},
        headers={**LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )
    assert invalid.status_code == 400
