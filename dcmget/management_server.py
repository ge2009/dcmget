from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Iterable
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPCookieProcessor,
    ProxyHandler,
    Request,
    build_opener,
)

from .profile_manager import (
    ProfileInfo,
    ProfileManager,
    ProfileManagerError,
    WINDOWS_MANAGEMENT_PORT,
)
from .profile_runtime_state import PROFILE_RUNTIME_FILE_NAME, ProfileRuntimeState
from .profile_web_operations import ProfileWebOperations
from .runtime import resource_root
from .windows_service_control import windows_service_operation_handlers
from .web_server import DcmGetWebServer


LOGGER = logging.getLogger(__name__)
WINDOWS_MANAGEMENT_HOST = "0.0.0.0"
PROFILE_PROXY_TIMEOUT_SECONDS = 30.0
_PROFILE_TOPOLOGY_FIELDS = (
    "calling_ae_title",
    "pacs_ae_title",
    "storage_ae_title",
    "storage_port",
    "web_port",
)

_PROFILE_PROXY_ROUTES = frozenset(
    {
        ("GET", "bootstrap"),
        ("GET", "snapshot"),
        ("GET", "task"),
        ("GET", "config"),
        ("GET", "license"),
        ("GET", "pdi"),
        ("GET", "fs/roots"),
        ("GET", "fs/list"),
        ("GET", "files/directories"),
        ("GET", "ops/health"),
        ("GET", "ops/profile"),
        ("GET", "ops/diagnostics"),
        ("GET", "events"),
        ("POST", "task/start"),
        ("POST", "tasks/start"),
        ("POST", "tasks/resume-saved"),
        ("POST", "task/pause"),
        ("POST", "tasks/pause"),
        ("POST", "task/resume"),
        ("POST", "tasks/resume"),
        ("POST", "task/cancel"),
        ("POST", "tasks/cancel"),
        ("POST", "task/retry"),
        ("POST", "tasks/retry-failed"),
        ("POST", "task/accept-partial"),
        ("POST", "tasks/accept-partial"),
        ("POST", "pdi/retry"),
        ("POST", "pdi/verify"),
        ("POST", "pdi/open"),
        ("POST", "preflight"),
        ("POST", "license/activate"),
        ("POST", "files/accessions"),
        ("POST", "operations/health"),
        ("PUT", "config"),
    }
)
_PROFILE_PROXY_OPERATIONS = frozenset(
    {
        "open-destination",
        "open-pdi",
        "open-log-directory",
        "open-data-directory",
        "acceptance-report",
        "profile-backup",
        "support-bundle",
    }
)


class ProfileApiProxy:
    """Allowlisted, same-host JSON bridge to independently running Profiles."""

    def __init__(
        self,
        manager: ProfileManager,
        *,
        timeout_seconds: float = PROFILE_PROXY_TIMEOUT_SECONDS,
    ) -> None:
        self.manager = manager
        self.timeout_seconds = float(timeout_seconds)
        self._sessions: dict[int, tuple[int, object, str]] = {}
        self._sessions_lock = threading.RLock()
        self._profile_locks_guard = threading.Lock()
        self._profile_locks: dict[int, threading.RLock] = {}

    async def request(
        self,
        *,
        profile_number: int,
        method: str,
        api_path: str,
        query: str,
        body: bytes,
        content_type: str,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._request_sync,
            profile_number,
            method,
            api_path,
            query,
            body,
            content_type,
        )

    def shutdown_profile(self, profile: ProfileInfo) -> None:
        result = self._perform(
            profile,
            "POST",
            "ops/shutdown",
            "",
            b"{}",
            "application/json",
        )
        if int(result["status_code"]) >= 400:
            payload = result.get("payload")
            raise RuntimeError(f"停止实例 {profile.number} 失败：{payload}")
        with self._sessions_lock:
            self._sessions.pop(profile.number, None)

    def _request_sync(
        self,
        profile_number: int,
        method: str,
        api_path: str,
        query: str,
        body: bytes,
        content_type: str,
    ) -> dict[str, object]:
        normalized_method = str(method).upper()
        normalized_path = str(api_path).strip("/")
        if not _profile_proxy_route_allowed(normalized_method, normalized_path):
            raise ValueError("该 Profile API 不允许通过管理中心访问")
        profile = self.manager.get_profile(profile_number)
        if not profile.is_running:
            raise RuntimeError(f"实例 {profile.number} 尚未启动")
        if normalized_method == "PUT" and normalized_path == "config":
            rejected = _reject_running_topology_update(profile, body)
            if rejected is not None:
                return rejected
        return self._perform(
            profile,
            normalized_method,
            normalized_path,
            query,
            body,
            content_type,
        )

    def _perform(
        self,
        profile: ProfileInfo,
        method: str,
        api_path: str,
        query: str,
        body: bytes,
        content_type: str,
    ) -> dict[str, object]:
        with self._profile_lock(profile.number):
            for attempt in range(2):
                opener, csrf = self._profile_session(profile)
                url = f"http://127.0.0.1:{profile.web_port}/api/{api_path}"
                if query:
                    url = f"{url}?{query}"
                headers = {"Accept": "application/json"}
                if method in {"POST", "PUT", "PATCH", "DELETE"}:
                    headers.update(
                        {
                            "Origin": f"http://127.0.0.1:{profile.web_port}",
                            "X-CSRF-Token": csrf,
                            "Content-Type": content_type or "application/json",
                        }
                    )
                request = Request(
                    url,
                    data=body if method != "GET" else None,
                    headers=headers,
                    method=method,
                )
                try:
                    with opener.open(request, timeout=self.timeout_seconds) as response:
                        return _proxy_json_response(response.status, response.read())
                except HTTPError as exc:
                    if exc.code in {401, 403} and attempt == 0:
                        with self._sessions_lock:
                            self._sessions.pop(profile.number, None)
                        continue
                    return _proxy_json_response(exc.code, exc.read())
                except URLError as exc:
                    with self._sessions_lock:
                        self._sessions.pop(profile.number, None)
                    reason = getattr(exc, "reason", exc)
                    raise RuntimeError(
                        f"无法连接实例 {profile.number} 的 Web 服务：{reason}"
                    ) from exc
            raise RuntimeError(f"实例 {profile.number} 的 Web 会话已失效")

    def _profile_session(self, profile: ProfileInfo) -> tuple[object, str]:
        with self._sessions_lock:
            cached = self._sessions.get(profile.number)
        if cached is not None and cached[0] == profile.web_port:
            return cached[1], cached[2]
        opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        url = f"http://127.0.0.1:{profile.web_port}/api/bootstrap"
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"无法建立实例 {profile.number} 的本机会话：{exc}"
            ) from exc
        csrf = payload.get("csrf_token") if isinstance(payload, dict) else None
        if not isinstance(csrf, str) or not csrf:
            raise RuntimeError(f"实例 {profile.number} 未返回有效的 CSRF 令牌")
        with self._sessions_lock:
            self._sessions[profile.number] = (profile.web_port, opener, csrf)
        return opener, csrf

    def _profile_lock(self, profile_number: int) -> threading.RLock:
        with self._profile_locks_guard:
            return self._profile_locks.setdefault(profile_number, threading.RLock())


def _reject_running_topology_update(
    profile: ProfileInfo,
    body: bytes,
) -> dict[str, object] | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "status_code": 400,
            "payload": {"detail": "配置请求必须是有效的 JSON 对象"},
        }
    if not isinstance(payload, dict):
        return {
            "status_code": 400,
            "payload": {"detail": "配置请求必须是 JSON 对象"},
        }
    changed = [
        field
        for field in _PROFILE_TOPOLOGY_FIELDS
        if field in payload
        and _topology_value(payload[field]) != _topology_value(getattr(profile, field))
    ]
    if not changed:
        return None
    return {
        "status_code": 409,
        "payload": {
            "detail": "实例正在运行，请先停止后再修改端口或 AE",
            "fields": changed,
        },
    }


def _topology_value(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        try:
            return int(normalized)
        except ValueError:
            return normalized
    return value


def _profile_proxy_route_allowed(method: str, api_path: str) -> bool:
    if (method, api_path) in _PROFILE_PROXY_ROUTES:
        return True
    prefix = "operations/"
    return (
        method == "POST"
        and api_path.startswith(prefix)
        and api_path[len(prefix) :] in _PROFILE_PROXY_OPERATIONS
    )


def _proxy_json_response(status_code: int, body: bytes) -> dict[str, object]:
    if not body:
        payload: object = {}
    else:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Profile 服务返回了非 JSON 响应") from exc
    return {"status_code": int(status_code), "payload": payload}


class WindowsManagementService:
    """Minimal task-free service boundary for the Windows management hub."""

    def __init__(self) -> None:
        self._stopped = False

    def snapshot(self) -> dict[str, object]:
        return {
            "event_id": 0,
            "status": "stopped" if self._stopped else "idle",
            "message": "DcmGet Windows 管理中心",
            "operation": "management",
            "task": {},
            "progress": {},
            "results": [],
            "pdi": None,
            "verification": None,
            "actions": {
                "can_start": False,
                "can_pause": False,
                "can_resume": False,
                "can_cancel": False,
                "can_retry": False,
            },
            "authorization": {},
            "error_logs": [],
        }

    def health(self) -> dict[str, object]:
        return {"ok": not self._stopped, "mode": "manager"}

    def diagnostics(self) -> dict[str, object]:
        return {"mode": "manager", "status": self.snapshot()["status"]}

    def events_since(
        self,
        after_id: int = 0,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        del after_id, limit
        return []

    def subscribe(
        self,
        callback: Callable[[dict[str, object]], None],
    ) -> Callable[[], None]:
        del callback
        return lambda: None

    def shutdown(self) -> bool:
        self._stopped = True
        return True


def create_windows_management_server(
    *,
    profile_manager: ProfileManager | None = None,
    project_root: str | Path | None = None,
    state_directory: str | Path | None = None,
    trusted_hosts: Iterable[str] = (),
    static_root: str | Path | None = None,
    nicegui_enabled: bool = False,
    log_level: str = "info",
) -> DcmGetWebServer:
    """Build the fixed management hub without claiming or validating a Profile."""

    manager = profile_manager or ProfileManager()
    root = Path(project_root or resource_root()).expanduser().resolve()
    management_state = Path(
        state_directory or manager.state_root / "management"
    ).expanduser().resolve()
    try:
        profiles = manager.list_profiles()
    except ProfileManagerError as exc:
        # Keep the repair UI reachable, but never overwrite a malformed or
        # unsafe Profile while trying to provide the first-run default.
        LOGGER.warning("Profile 列表读取失败，跳过默认 Profile 创建：%s", exc)
        profiles = None
    if profiles == ():
        manager.create_profile()
    runtime_state = ProfileRuntimeState(
        management_state / PROFILE_RUNTIME_FILE_NAME
    )
    profile_proxy = ProfileApiProxy(manager)
    profile_operations = ProfileWebOperations(
        manager=manager,
        project_root=root,
        runtime_state=runtime_state,
        shutdown_profile=profile_proxy.shutdown_profile,
    )
    handlers = {
        **windows_service_operation_handlers(),
        **profile_operations.handlers(),
    }
    service = WindowsManagementService()
    return DcmGetWebServer(
        service,
        state_directory=management_state,
        host=WINDOWS_MANAGEMENT_HOST,
        port=WINDOWS_MANAGEMENT_PORT,
        trusted_hosts=trusted_hosts,
        static_root=static_root,
        directory_roots=(),
        project_root=root,
        profile_metadata={
            "id": "manager",
            "mode": "manager",
            "name": "Windows 管理中心",
            "data_dir": str(management_state),
        },
        operation_handlers=handlers,
        profile_api_proxy=profile_proxy.request,
        management_mode=True,
        nicegui_enabled=nicegui_enabled,
        log_level=log_level,
    )


def run_windows_management_server(
    *,
    profile_manager: ProfileManager | None = None,
    project_root: str | Path | None = None,
    state_directory: str | Path | None = None,
    trusted_hosts: Iterable[str] = (),
    static_root: str | Path | None = None,
    log_level: str = "info",
) -> int:
    """Run the management hub until its supervising Windows process stops it."""

    server = create_windows_management_server(
        profile_manager=profile_manager,
        project_root=project_root,
        state_directory=state_directory,
        trusted_hosts=trusted_hosts,
        static_root=static_root,
        nicegui_enabled=True,
        log_level=log_level,
    )
    try:
        LOGGER.info(
            "DcmGet Windows management hub ready at %s:%s",
            WINDOWS_MANAGEMENT_HOST,
            WINDOWS_MANAGEMENT_PORT,
        )
        server.run()
        return 0
    finally:
        server.stop(timeout=15)
