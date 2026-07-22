from __future__ import annotations

import asyncio
import ipaddress
import inspect
import json
import logging
import os
import re
import tempfile
import threading
import time
import string
from collections.abc import Callable, Iterable, Mapping
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import unquote

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .accession_import import AccessionImportError, ColumnSelectionError, ImportLimits, import_accession_file
from .config import AppConfig, load_config, save_config
from .core import DcmtkResolver, ToolPaths, preflight as run_core_preflight
from .licensing import LicenseError, machine_code, save_license
from .release_notes import load_release_notes

from .web_security import (
    DirectoryRoot,
    HostPolicy,
    SafeDirectoryBrowser,
    UnsafePathError,
    WebSecurityContext,
    WebSession,
    bootstrap_web_security,
    is_loopback_address,
)


LOGGER = logging.getLogger(__name__)
SESSION_COOKIE = "dcmget_session"
LEGACY_SESSION_COOKIE_PORT = 8787
MAX_JSON_BODY_BYTES = 4 * 1024 * 1024
MAX_PROFILE_PROXY_BODY_BYTES = 16 * 1024 * 1024
MAX_ACCESSIONS = 100_000
MAX_ACCESSION_LENGTH = 256
SSE_POLL_SECONDS = 0.5
SSE_HEARTBEAT_SECONDS = 15.0
_SAFE_EVENT_TYPE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MUTATING_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_IPV6_ULA_NETWORK = ipaddress.ip_network("fc00::/7")
_MANAGEMENT_OPERATIONS = frozenset(
    {
        "profile-list",
        "profile-create",
        "profile-clone",
        "profile-update",
        "profile-rename",
        "profile-delete",
        "profile-launch",
        "profile-stop",
        "profile-launch-all",
        "profile-shortcut",
        "windows-service-status",
        "windows-service-start",
        "windows-service-stop",
    }
)


class NiceGuiSocketSecurityMiddleware:
    """Apply the DcmGet HTTP trust boundary to NiceGUI's WebSocket channel."""

    def __init__(
        self,
        app: object,
        *,
        mount_path: str,
        host_policy: HostPolicy,
        security: WebSecurityContext,
        session_cookie: str,
        management_mode: bool,
    ) -> None:
        self.app = app
        self.socket_prefix = f"{mount_path.rstrip('/')}/_nicegui_ws/"
        self.host_policy = host_policy
        self.security = security
        self.session_cookie = session_cookie
        self.management_mode = bool(management_mode)

    async def __call__(self, scope: dict[str, Any], receive: object, send: object) -> None:
        if scope.get("type") != "websocket" or not str(scope.get("path", "")).startswith(
            self.socket_prefix
        ):
            await self.app(scope, receive, send)  # type: ignore[misc]
            return

        headers = {
            bytes(key).decode("latin-1").lower(): bytes(value).decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        client = scope.get("client") or ("", 0)
        client_ip = str(client[0]) if client else ""
        allowed = self.host_policy.allows_host_header(headers.get("host", ""))
        allowed = allowed and self.host_policy.allows_origin(headers.get("origin"))
        if self.management_mode:
            allowed = allowed and is_management_peer_address(client_ip)

        token = ""
        try:
            cookie = SimpleCookie()
            cookie.load(headers.get("cookie", ""))
            morsel = cookie.get(self.session_cookie)
            token = morsel.value if morsel is not None else ""
        except CookieError:
            allowed = False
        session = self.security.sessions.get(token, touch=False) if token else None
        allowed = allowed and session is not None and hmac_compare(session.remote_ip, client_ip)
        if not allowed:
            await send(  # type: ignore[operator]
                {
                    "type": "websocket.close",
                    "code": 1008,
                    "reason": "DcmGet WebSocket trust check failed",
                }
            )
            return
        await self.app(scope, receive, send)  # type: ignore[misc]


@runtime_checkable
class WebAppService(Protocol):
    """Qt-free application boundary consumed by the local Web server."""

    def snapshot(self) -> dict[str, object]: ...

    def start_task(
        self,
        config: Mapping[str, object],
        tools: object,
        accessions: list[str],
    ) -> object: ...

    def resume_task(self, tools: object) -> object: ...

    def pause(self) -> object: ...

    def resume(self) -> object: ...

    def cancel(self) -> object: ...

    def retry_failed(self, tools: object) -> object: ...

    def accept_partial(self) -> object: ...

    def retry_pdi(self, tools: object) -> object: ...

    def verify_pdi(self, root: str) -> object: ...

    def events_since(self, after_id: int = 0, limit: int = 200) -> list[dict[str, object]]: ...

    def subscribe(self, callback: Callable[[dict[str, object]], None]) -> Callable[[], None]: ...

    def shutdown(self) -> object: ...


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else ""


def session_cookie_name(server_port: int) -> str:
    """Isolate same-host Web sessions by port while preserving Profile 8787."""

    port = int(server_port)
    if port == LEGACY_SESSION_COOKIE_PORT:
        return SESSION_COOKIE
    return f"{SESSION_COOKIE}_{port}"


def is_management_peer_address(value: str) -> bool:
    """Return whether an actual socket peer may mutate the management hub."""

    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0].strip())
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in _RFC1918_NETWORKS)
    return address in _IPV6_ULA_NETWORK


async def _json_body(request: Request) -> dict[str, Any]:
    content_length = request.headers.get("content-length", "")
    if content_length:
        try:
            if int(content_length) > MAX_JSON_BODY_BYTES:
                raise HTTPException(status_code=413, detail="请求内容过大")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length 无效") from exc
    body = await request.body()
    if len(body) > MAX_JSON_BODY_BYTES:
        raise HTTPException(status_code=413, detail="请求内容过大")
    try:
        payload = json.loads(body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="JSON 请求无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON 顶层必须是对象")
    return payload


def _tools_payload(payload: Mapping[str, Any]) -> object:
    tools = payload.get("tools", {})
    if tools is None:
        return {}
    if not isinstance(tools, (dict, list, str)):
        raise HTTPException(status_code=400, detail="tools 参数无效")
    return tools


def _task_config(base: AppConfig, payload: Mapping[str, Any]) -> AppConfig:
    values = base.to_dict()
    explicit = payload.get("config")
    if explicit is not None:
        if not isinstance(explicit, dict):
            raise HTTPException(status_code=400, detail="config 参数无效")
        values.update(explicit)
    destination = payload.get("destination")
    if destination is not None:
        if not isinstance(destination, str) or not destination.strip():
            raise HTTPException(status_code=400, detail="保存目录无效")
        values["dicom_destination_folder"] = destination.strip()
    pdi = payload.get("pdi")
    if pdi is not None:
        if not isinstance(pdi, dict):
            raise HTTPException(status_code=400, detail="PDI 参数无效")
        if "enabled" in pdi:
            values["pdi_export_enabled"] = bool(pdi["enabled"])
        if "output_folder" in pdi:
            values["pdi_output_folder"] = str(pdi["output_folder"] or "").strip()
    config = AppConfig.from_dict(values)
    errors = config.validate()
    if errors:
        raise HTTPException(status_code=422, detail={"message": "配置校验失败", "fields": errors})
    return config


def _task_view(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    task = dict(snapshot.get("task") or {})
    progress = snapshot.get("progress")
    if isinstance(progress, dict):
        task.update(progress)
    task.update(
        {
            "status": snapshot.get("status", "idle"),
            "message": snapshot.get("message", ""),
            "operation": snapshot.get("operation", ""),
            "results": snapshot.get("results"),
            "pdi": snapshot.get("pdi"),
            "verification": snapshot.get("verification"),
            "actions": snapshot.get("actions", {}),
            "authorization": snapshot.get("authorization", {}),
            "error_logs": snapshot.get("error_logs", []),
        }
    )
    return jsonable_encoder(task)


def _preflight_key(name: str) -> str:
    if "DCMTK" in name or "工具" in name:
        return "dcmtk"
    if "保存目录" in name or "输出目录" in name or "空间" in name:
        return "destination"
    if "端口" in name or "接收器" in name:
        return "receiver"
    if "PACS" in name or "配置" in name:
        return "config"
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "check"


def _validated_accessions(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_ACCESSIONS:
        raise HTTPException(status_code=400, detail="检查号列表无效或超过上限")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise HTTPException(status_code=400, detail="检查号必须是文本")
        normalized = item.strip()
        if not normalized or len(normalized) > MAX_ACCESSION_LENGTH or "\x00" in normalized:
            raise HTTPException(status_code=400, detail="检查号内容无效")
        result.append(normalized)
    if not result:
        raise HTTPException(status_code=400, detail="至少需要一个检查号")
    return result


async def _invoke(service: object, method_name: str, *args: object, **kwargs: object) -> Any:
    method = getattr(service, method_name, None)
    if not callable(method):
        raise HTTPException(status_code=501, detail=f"当前服务不支持 {method_name}")
    try:
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return jsonable_encoder(result)
    except HTTPException:
        raise
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "请求参数无效") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc) or "当前状态不允许此操作") from exc


async def _call_handler(
    handler: Callable[[dict[str, Any]], object], payload: dict[str, Any]
) -> Any:
    try:
        result = handler(payload)
        if inspect.isawaitable(result):
            result = await result
        return jsonable_encoder(result)
    except HTTPException:
        raise
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "运维参数无效") from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc) or "运维操作失败") from exc


def _snapshot_section(snapshot: Mapping[str, Any], key: str, default: object) -> object:
    value = snapshot.get(key, default)
    return jsonable_encoder(value)


def _default_login_html() -> str:
    return """<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">
<title>DcmGet</title></head><body><main><h1>DcmGet Web</h1>
<p>Web 前端资源尚未安装。请检查安装包完整性。</p></main></body></html>"""


def _release_note_entries(markdown: str, *, limit: int = 12) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            if current is not None:
                entries.append(current)
                if len(entries) >= limit:
                    break
            heading = line[3:].strip()
            version, separator, remainder = heading.partition("（")
            current = {
                "version": version.strip(),
                "date": remainder.rstrip("）").strip() if separator else "",
                "items": [],
            }
        elif current is not None and line.startswith("- "):
            items = current["items"]
            assert isinstance(items, list)
            items.append(line[2:].strip())
    if current is not None and len(entries) < limit:
        entries.append(current)
    return entries


def _validated_static_root(value: str | Path | None) -> tuple[Path | None, Path | None]:
    if value is None:
        return None, None
    source = Path(value).expanduser()
    if source.is_symlink():
        raise ValueError("Web 静态资源目录不能是符号链接")
    root = source.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Web 静态资源目录无效")
    index = root / "index.html"
    if not index.is_file() or index.is_symlink():
        raise ValueError("Web 静态资源缺少 index.html")
    return root, index


def _directory_browser(
    roots: Mapping[str, str | Path] | Iterable[DirectoryRoot] | None,
) -> SafeDirectoryBrowser | None:
    if roots is None:
        return None
    return SafeDirectoryBrowser(roots)


def default_directory_roots() -> dict[str, Path]:
    """Return conservative server-side roots suitable for a folder picker."""

    roots: dict[str, Path] = {}
    home = Path.home().resolve()
    if home.is_dir() and not home.is_symlink():
        roots["home"] = home
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.is_dir():
                roots[f"drive-{letter.lower()}"] = drive
    else:
        for name, candidate in (
            ("volumes", Path("/Volumes")),
            ("mnt", Path("/mnt")),
            ("media", Path("/media")),
        ):
            if candidate.is_dir() and not candidate.is_symlink():
                roots[name] = candidate
    return roots


def create_web_app(
    service: WebAppService | object,
    *,
    security: WebSecurityContext,
    server_host: str = "0.0.0.0",
    server_port: int = 8787,
    trusted_hosts: Iterable[str] = (),
    static_root: str | Path | None = None,
    directory_roots: Mapping[str, str | Path] | Iterable[DirectoryRoot] | None = None,
    shutdown_callback: Callable[[], None] | None = None,
    config_path: str | Path | None = None,
    project_root: str | Path | None = None,
    profile_metadata: Mapping[str, object] | None = None,
    tools_provider: Callable[[AppConfig], ToolPaths] | None = None,
    preflight_provider: Callable[[AppConfig], object] | None = None,
    operation_handlers: Mapping[str, Callable[[dict[str, Any]], object]] | None = None,
    profile_api_proxy: Callable[..., object] | None = None,
    management_mode: bool = False,
    nicegui_mount_path: str | None = None,
) -> FastAPI:
    """Create the offline, LAN-capable DcmGet HTTP application.

    ``config_path``, ``project_root`` and ``profile_metadata`` are descriptive
    launcher metadata only.  Configuration mutations always go through the
    supplied application service; the Web layer never edits project files.
    """

    if server_host not in {"0.0.0.0", "127.0.0.1", "::", "::1"}:
        # Explicit interface IPs are valid too, but hostnames are intentionally
        # not accepted as bind addresses because resolution can change later.
        try:
            import ipaddress

            ipaddress.ip_address(server_host)
        except ValueError as exc:
            raise ValueError("Web 绑定地址必须是本机 IP") from exc
    host_policy = HostPolicy(server_port, trusted_hosts)
    session_cookie = session_cookie_name(server_port)
    workspace_path = (
        "/" + nicegui_mount_path.strip("/")
        if nicegui_mount_path and nicegui_mount_path.strip("/")
        else None
    )
    static_directory, index_path = _validated_static_root(static_root)
    browser = _directory_browser(directory_roots)
    metadata = dict(profile_metadata or {})
    if management_mode:
        metadata["mode"] = "manager"
    if config_path is not None:
        metadata.setdefault("config_path", str(Path(config_path).expanduser().resolve()))
    if project_root is not None:
        metadata.setdefault("project_root", str(Path(project_root).expanduser().resolve()))
    metadata.setdefault("server_host", server_host)
    metadata.setdefault("server_port", int(server_port))
    resolved_config_path = (
        Path(config_path).expanduser().resolve() if config_path is not None else None
    )
    resolved_project_root = (
        Path(project_root).expanduser().resolve() if project_root is not None else None
    )
    resolver = (
        DcmtkResolver(resolved_project_root)
        if resolved_project_root is not None and not management_mode
        else None
    )
    if tools_provider is None and resolver is not None:
        tools_provider = lambda config: resolver.resolve(config.dcmtk_bin_dir)
    if preflight_provider is None and resolver is not None:
        preflight_provider = lambda config: run_core_preflight(config, resolver)
    handlers = dict(operation_handlers or {})
    last_config = (
        load_config(resolved_config_path)
        if resolved_config_path
        else (
            AppConfig(web_bind_address=server_host, web_port=int(server_port))
            if management_mode
            else AppConfig()
        )
    )

    def current_config() -> AppConfig:
        nonlocal last_config
        if resolved_config_path is not None:
            last_config = load_config(resolved_config_path)
        return AppConfig.from_dict(last_config.to_dict())

    def resolve_tools(config: AppConfig) -> ToolPaths:
        if tools_provider is None:
            raise HTTPException(status_code=503, detail="DCMTK 服务端解析器尚未配置")
        try:
            tools = tools_provider(config)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc) or "DCMTK 工具不可用") from exc
        if not isinstance(tools, ToolPaths):
            raise HTTPException(status_code=500, detail="DCMTK 解析器返回了无效结果")
        return tools

    app = FastAPI(
        title="DcmGet Web",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service = service
    app.state.security = security
    app.state.host_policy = host_policy
    app.state.directory_browser = browser
    app.state.profile_metadata = metadata
    app.state.session_cookie_name = session_cookie
    app.state.profile_api_proxy = profile_api_proxy
    app.state.nicegui_mount_path = workspace_path

    if workspace_path is not None:
        app.add_middleware(
            NiceGuiSocketSecurityMiddleware,
            mount_path=workspace_path,
            host_policy=host_policy,
            security=security,
            session_cookie=session_cookie,
            management_mode=management_mode,
        )

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        if not host_policy.allows_host_header(request.headers.get("host", "")):
            return JSONResponse(status_code=421, content={"detail": "Host 不受信任"})
        request_path = request.url.path
        nicegui_request = bool(
            workspace_path
            and (
                request_path == workspace_path
                or request_path.startswith(f"{workspace_path}/")
            )
        )
        nicegui_transport = bool(
            workspace_path
            and request_path.startswith(f"{workspace_path}/_nicegui_ws/")
        )
        workspace_session: WebSession | None = None
        workspace_session_created = False
        if nicegui_transport:
            if not host_policy.allows_origin(request.headers.get("origin")):
                return JSONResponse(status_code=403, content={"detail": "Origin 不受信任"})
            workspace_session, _ = request_session(request)
            if workspace_session is None:
                return JSONResponse(status_code=401, content={"detail": "会话已失效，请刷新页面"})
        elif nicegui_request:
            workspace_session, workspace_session_created = request_session(
                request,
                create=True,
            )
        if request.method.upper() in _MUTATING_HTTP_METHODS:
            if management_mode and not is_management_peer_address(_client_ip(request)):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "管理操作只允许来自服务器本机或可信私有网络"},
                )
            if not host_policy.allows_origin(request.headers.get("origin")):
                return JSONResponse(status_code=403, content={"detail": "Origin 不受信任"})
        response = await call_next(request)
        if workspace_session_created and workspace_session is not None:
            attach_session_cookie(response, workspace_session)
        response.headers["Cache-Control"] = "no-store"
        if nicegui_request:
            websocket_origin = request.headers.get("host", "").strip()
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"connect-src 'self' ws://{websocket_origin} wss://{websocket_origin}; "
                "img-src 'self' data:; font-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval'; object-src 'none'; "
                "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "style-src 'self'; script-src 'self'; object-src 'none'; "
                "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
            )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    def request_session(
        request: Request,
        *,
        create: bool = False,
    ) -> tuple[WebSession | None, bool]:
        token = request.cookies.get(session_cookie)
        session = security.sessions.get(token)
        if session is None or not hmac_compare(session.remote_ip, _client_ip(request)):
            if token:
                security.sessions.revoke(token)
            if create:
                return security.sessions.create(_client_ip(request)), True
            return None, False
        return session, False

    def attach_session_cookie(response: Response, session: WebSession) -> None:
        response.set_cookie(
            session_cookie,
            session.token,
            max_age=security.sessions.ttl_seconds,
            path="/",
            secure=False,
            httponly=True,
            samesite="strict",
        )

    def require_session(request: Request) -> WebSession:
        session, _created = request_session(request)
        if session is None:
            raise HTTPException(status_code=401, detail="会话已失效，请刷新页面")
        return session

    def require_csrf(
        request: Request,
        session: WebSession = Depends(require_session),
    ) -> WebSession:
        candidate = request.headers.get("x-csrf-token", "")
        if not hmac_compare(candidate, session.csrf_token):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
        return session

    def require_local_csrf(
        request: Request,
        session: WebSession = Depends(require_csrf),
    ) -> WebSession:
        if not session.local or not is_loopback_address(_client_ip(request)):
            raise HTTPException(status_code=403, detail="此操作只允许在服务器本机执行")
        return session

    @app.get("/", include_in_schema=False)
    @app.get("/login", include_in_schema=False)
    async def index(request: Request) -> Response:
        if workspace_path is not None:
            session, created = request_session(request, create=True)
            assert session is not None
            response = RedirectResponse(f"{workspace_path}/", status_code=307)
            if created:
                attach_session_cookie(response, session)
            return response
        if index_path is None:
            return HTMLResponse(_default_login_html())
        return FileResponse(index_path, media_type="text/html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        candidate = (
            resolved_project_root / "logo.png"
            if resolved_project_root is not None
            else None
        )
        if (
            candidate is not None
            and candidate.is_file()
            and not candidate.is_symlink()
        ):
            return FileResponse(candidate, media_type="image/png")
        return Response(status_code=204)

    async def bootstrap_payload(
        request: Request,
        session: WebSession,
    ) -> dict[str, object]:
        local_request = is_loopback_address(_client_ip(request))
        payload: dict[str, object] = {
            "version": __version__,
            "mode": metadata.get("mode", "profile"),
            "auth": {
                "authenticated": True,
                "setup_required": False,
                "first_run": False,
                "passwordless": True,
            },
            "authenticated": True,
            "setup_required": False,
            "can_setup_here": False,
            "csrf_token": session.csrf_token,
            "web": {
                "url": str(request.base_url),
                "lan_url": str(metadata.get("lan_url") or request.base_url),
                "lan_enabled": server_host in {"0.0.0.0", "::"},
                "local_session": bool(local_request and session.local),
                "insecure_http": True,
                "warning": "当前为局域网 HTTP，请仅在可信内网使用。",
            },
        }
        snapshot_value = await _invoke(service, "snapshot")
        snapshot = snapshot_value if isinstance(snapshot_value, dict) else {}
        config = current_config()
        payload.update(
            {
                "config": config.to_dict(),
                "task": _task_view(snapshot),
                "license": {
                    **dict(snapshot.get("authorization", {}) or {}),
                    "machine_code": machine_code(),
                },
                "profile": metadata,
                "profile_name": metadata.get("name", metadata.get("profile_name", "default")),
                "data_dir": metadata.get("data_dir", ""),
            }
        )
        return payload

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request) -> Response:
        session, created = request_session(request, create=True)
        assert session is not None
        response = JSONResponse(await bootstrap_payload(request, session))
        if created:
            attach_session_cookie(response, session)
        return response

    @app.post("/api/setup")
    async def setup(request: Request) -> Response:
        # Compatibility endpoint for older frontends.  Password fields are
        # accepted but ignored; opening the workspace now creates an anonymous
        # IP-bound session instead of an administrator account.
        await _json_body(request)
        session, _created = request_session(request, create=True)
        assert session is not None
        result = await bootstrap_payload(request, session)
        response = JSONResponse({"bootstrap": result, "csrf_token": session.csrf_token})
        attach_session_cookie(response, session)
        return response

    @app.post("/api/login")
    async def login(request: Request) -> Response:
        # Compatibility endpoint: no credentials are read or verified.
        await _json_body(request)
        session, _created = request_session(request, create=True)
        assert session is not None
        response = JSONResponse(
            {
                "authenticated": True,
                "passwordless": True,
                "csrf_token": session.csrf_token,
                "expires_at": session.expires_at,
                "insecure_http": True,
                "warning": "当前为局域网 HTTP，请仅在可信内网使用。",
            }
        )
        attach_session_cookie(response, session)
        return response

    @app.get("/api/session")
    async def session_status(request: Request) -> Response:
        session, created = request_session(request, create=True)
        assert session is not None
        response = JSONResponse(
            {
                "authenticated": True,
                "passwordless": True,
                "setup_required": False,
                "csrf_token": session.csrf_token,
                "expires_at": session.expires_at,
                "insecure_http": True,
                "warning": "当前为局域网 HTTP，请仅在可信内网使用。",
            }
        )
        if created:
            attach_session_cookie(response, session)
        return response

    @app.post("/api/logout")
    async def logout(
        request: Request,
        session: WebSession = Depends(require_csrf),
    ) -> Response:
        security.sessions.revoke(session.token)
        response = JSONResponse({"ok": True})
        response.delete_cookie(session_cookie, path="/")
        return response

    @app.post("/api/admin/password")
    async def change_password(
        request: Request,
        _session: WebSession = Depends(require_csrf),
    ) -> Response:
        await _json_body(request)
        return JSONResponse(
            {
                "ok": True,
                "passwordless": True,
                "login_required": False,
            }
        )

    @app.get("/api/snapshot")
    async def snapshot(_session: WebSession = Depends(require_session)) -> dict[str, object]:
        value = await _invoke(service, "snapshot")
        if not isinstance(value, dict):
            raise HTTPException(status_code=500, detail="服务状态格式无效")
        value["security"] = {
            "insecure_http": True,
            "transport": "http",
            "lan_enabled": server_host in {"0.0.0.0", "::"},
        }
        return value

    @app.get("/api/task")
    async def task_snapshot(_session: WebSession = Depends(require_session)) -> dict[str, object]:
        value = await _invoke(service, "snapshot")
        if not isinstance(value, dict):
            raise HTTPException(status_code=500, detail="服务状态格式无效")
        return {"task": _task_view(value), "snapshot": value}

    @app.post("/api/task/start")
    @app.post("/api/tasks/start")
    async def start_task(
        request: Request,
        _session: WebSession = Depends(require_csrf),
    ) -> Any:
        payload = await _json_body(request)
        nonlocal last_config
        config = _task_config(current_config(), payload)
        accessions = _validated_accessions(payload.get("accessions"))
        tools = resolve_tools(config)
        last_config = config
        result = await _invoke(service, "start_task", config, tools, accessions)
        return {"task": _task_view(result), "snapshot": result} if isinstance(result, dict) else result

    @app.post("/api/tasks/resume-saved")
    async def resume_saved(
        request: Request,
        _session: WebSession = Depends(require_csrf),
    ) -> Any:
        await _json_body(request)
        result = await _invoke(service, "resume_task", resolve_tools(current_config()))
        return {"task": _task_view(result), "snapshot": result} if isinstance(result, dict) else result

    async def no_body_action(method_name: str) -> Any:
        return await _invoke(service, method_name)

    @app.post("/api/task/pause")
    @app.post("/api/tasks/pause")
    async def pause(_session: WebSession = Depends(require_csrf)) -> Any:
        result = await no_body_action("pause")
        return {"task": _task_view(result), "snapshot": result} if isinstance(result, dict) else result

    @app.post("/api/task/resume")
    @app.post("/api/tasks/resume")
    async def resume(_session: WebSession = Depends(require_csrf)) -> Any:
        current = await _invoke(service, "snapshot")
        status = str(current.get("status", "")) if isinstance(current, dict) else ""
        operation = str(current.get("operation", "")) if isinstance(current, dict) else ""
        if operation == "download" and status in {"pause_pending", "paused"}:
            result = await no_body_action("resume")
        else:
            result = await _invoke(
                service,
                "resume_task",
                resolve_tools(current_config()),
            )
        return {"task": _task_view(result), "snapshot": result} if isinstance(result, dict) else result

    @app.post("/api/task/cancel")
    @app.post("/api/tasks/cancel")
    async def cancel(_session: WebSession = Depends(require_csrf)) -> Any:
        result = await no_body_action("cancel")
        return {"task": _task_view(result), "snapshot": result} if isinstance(result, dict) else result

    @app.post("/api/task/retry")
    @app.post("/api/tasks/retry-failed")
    async def retry_failed(
        request: Request,
        _session: WebSession = Depends(require_csrf),
    ) -> Any:
        await _json_body(request)
        result = await _invoke(service, "retry_failed", resolve_tools(current_config()))
        return {"task": _task_view(result), "snapshot": result} if isinstance(result, dict) else result

    @app.post("/api/task/accept-partial")
    @app.post("/api/tasks/accept-partial")
    async def accept_partial(_session: WebSession = Depends(require_csrf)) -> Any:
        result = await no_body_action("accept_partial")
        return {"task": _task_view(result), "snapshot": result} if isinstance(result, dict) else result

    @app.post("/api/pdi/retry")
    async def retry_pdi(
        request: Request,
        _session: WebSession = Depends(require_csrf),
    ) -> Any:
        await _json_body(request)
        result = await _invoke(service, "retry_pdi", resolve_tools(current_config()))
        return {"task": _task_view(result), "snapshot": result} if isinstance(result, dict) else result

    @app.post("/api/pdi/verify")
    async def verify_pdi(
        request: Request,
        _session: WebSession = Depends(require_csrf),
    ) -> Any:
        payload = await _json_body(request)
        root = payload.get("root")
        if root is None:
            current = await _invoke(service, "snapshot")
            pdi = current.get("pdi") if isinstance(current, dict) else None
            root = pdi.get("output_directory") if isinstance(pdi, dict) else None
        if not isinstance(root, str) or not root.strip() or len(root) > 4096:
            raise HTTPException(status_code=400, detail="当前任务没有可校验的 PDI 目录")
        if browser is not None:
            try:
                root = str(browser.authorize_directory(root))
            except UnsafePathError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        result = await _invoke(service, "verify_pdi", root)
        if isinstance(result, dict) and "status" in result:
            return {
                "ok": True,
                "message": "PDI 校验已在后台启动",
                "task": _task_view(result),
                "snapshot": result,
            }
        return result

    @app.post("/api/pdi/open")
    async def open_pdi(
        request: Request,
        _session: WebSession = Depends(require_local_csrf),
    ) -> Any:
        payload = await _json_body(request)
        handler = handlers.get("open-pdi")
        if handler is None:
            raise HTTPException(status_code=501, detail="当前服务未配置打开 PDI 操作")
        return await _call_handler(handler, payload)

    @app.get("/api/config")
    async def get_config(_session: WebSession = Depends(require_session)) -> Any:
        return {"config": current_config().to_dict()}

    @app.put("/api/config")
    async def update_config(
        request: Request,
        _session: WebSession = Depends(require_csrf),
    ) -> Any:
        nonlocal last_config
        payload = await _json_body(request)
        current = await _invoke(service, "snapshot")
        if isinstance(current, dict):
            actions = current.get("actions")
            if isinstance(actions, dict) and bool(actions.get("can_cancel")):
                raise HTTPException(status_code=409, detail="任务运行期间不能修改设置")
        values = current_config().to_dict()
        values.update(payload)
        config = AppConfig.from_dict(values)
        errors = config.validate()
        if errors:
            raise HTTPException(
                status_code=422,
                detail={"message": "配置校验失败", "fields": errors},
            )
        if resolved_config_path is None:
            raise HTTPException(status_code=501, detail="当前服务未配置配置文件路径")
        previous = current_config()
        save_config(resolved_config_path, config)
        last_config = config
        restart_fields = {
            "web_bind_address",
            "web_port",
            "web_session_timeout_minutes",
        }
        restart_required = any(
            getattr(previous, field) != getattr(config, field) for field in restart_fields
        )
        return {
            "config": config.to_dict(),
            "restart_required": restart_required,
            "message": "设置已保存" + ("，Web 监听设置重启后生效" if restart_required else ""),
        }

    @app.post("/api/preflight")
    async def preflight(
        request: Request,
        _session: WebSession = Depends(require_csrf),
    ) -> Any:
        payload = await _json_body(request)
        config = _task_config(current_config(), payload)
        if preflight_provider is None:
            raise HTTPException(status_code=503, detail="预检服务尚未配置")
        try:
            result = preflight_provider(config)
            if inspect.isawaitable(result):
                result = await result
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc) or "预检失败") from exc
        encoded = jsonable_encoder(result)
        if not isinstance(encoded, dict):
            raise HTTPException(status_code=500, detail="预检结果格式无效")
        checks = encoded.get("checks", [])
        # Add stable aliases consumed by the web UI while retaining the full
        # core checklist for support diagnostics.
        errors = encoded.get("errors", {})
        normalized = [
            {
                "key": "config",
                "name": "配置完整性",
                "ok": not bool(errors),
                "message": "配置有效" if not errors else "配置字段需要修正",
            }
        ]
        for item in checks if isinstance(checks, list) else []:
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                name = str(item[0])
                normalized.append(
                    {
                        "key": _preflight_key(name),
                        "name": name,
                        "ok": bool(item[1]),
                        "message": item[2],
                    }
                )
            elif isinstance(item, dict):
                copied = dict(item)
                name = str(copied.get("name", copied.get("label", "检查项")))
                copied.setdefault("key", _preflight_key(name))
                normalized.append(copied)
        ok = not bool(errors) and all(bool(item.get("ok")) for item in normalized)
        return {"ok": ok, "checks": normalized, "errors": errors, "raw": encoded}

    @app.get("/api/license")
    async def license_status(_session: WebSession = Depends(require_session)) -> Any:
        method = getattr(service, "license_status", None)
        if callable(method):
            result = await _invoke(service, "license_status")
            return {"license": result, "machine_code": machine_code()}
        current = await _invoke(service, "snapshot")
        license_data = (
            _snapshot_section(current, "authorization", {})
            if isinstance(current, dict)
            else {}
        )
        return {"license": license_data, "machine_code": machine_code()}

    @app.post("/api/license/activate")
    async def activate_license(
        request: Request,
        _session: WebSession = Depends(require_csrf),
    ) -> Any:
        payload = await _json_body(request)
        token = payload.get("token")
        if not isinstance(token, str) or not token.strip() or len(token) > 64 * 1024:
            raise HTTPException(status_code=400, detail="注册码无效")
        method = getattr(service, "activate_license", None)
        if callable(method):
            return await _invoke(service, "activate_license", token)
        try:
            info = save_license(token)
        except (OSError, LicenseError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "machine_code": info.machine_code,
            "license": {
                "registered": True,
                "customer": info.customer,
                "expires_on": str(info.expires_on or ""),
            },
        }

    @app.get("/api/pdi")
    async def pdi_status(_session: WebSession = Depends(require_session)) -> Any:
        current = await _invoke(service, "snapshot")
        return _snapshot_section(current, "pdi", {}) if isinstance(current, dict) else {}

    @app.get("/api/fs/roots")
    async def directory_root_list(_session: WebSession = Depends(require_session)) -> Any:
        if browser is None:
            return {"roots": []}
        return {"roots": browser.roots()}

    @app.get("/api/fs/list")
    async def directory_list(
        root_id: str = Query(..., max_length=128),
        path: str = Query("", max_length=4096),
        _session: WebSession = Depends(require_session),
    ) -> Any:
        if browser is None:
            raise HTTPException(status_code=501, detail="目录浏览未启用")
        try:
            return browser.list(root_id, path)
        except UnsafePathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/files/directories")
    async def compatible_directory_list(
        path: str = Query("", max_length=4096),
        purpose: str = Query("destination", pattern="^(destination|pdi|dcmtk)$"),
        _session: WebSession = Depends(require_session),
    ) -> Any:
        del purpose  # Both purposes use the same explicit server-side roots.
        if browser is None:
            raise HTTPException(status_code=501, detail="目录浏览未启用")
        try:
            return browser.list_absolute(path)
        except UnsafePathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/files/accessions")
    async def import_accessions(
        request: Request,
        column: str | None = Query(None, max_length=256),
        _session: WebSession = Depends(require_csrf),
    ) -> Any:
        encoded_name = request.headers.get("x-file-name", "")
        if not encoded_name or len(encoded_name) > 1024:
            raise HTTPException(status_code=400, detail="缺少安全的文件名")
        try:
            filename = unquote(encoded_name, errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="文件名编码无效") from exc
        if (
            not filename
            or filename != Path(filename).name
            or "/" in filename
            or "\\" in filename
            or "\x00" in filename
        ):
            raise HTTPException(status_code=400, detail="文件名无效")
        suffix = Path(filename).suffix.casefold()
        if suffix not in {".txt", ".csv", ".xlsx"}:
            raise HTTPException(status_code=415, detail="仅支持 TXT、CSV 和 XLSX 文件")
        limits = ImportLimits()
        content_length = request.headers.get("content-length", "")
        if content_length:
            try:
                if int(content_length) > limits.max_input_bytes:
                    raise HTTPException(status_code=413, detail="导入文件超过大小限制")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Content-Length 无效") from exc

        import_root = security.password_store.state_directory / "web-imports"
        import_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            import_root.chmod(0o700)
        except OSError:
            pass
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="upload-", suffix=suffix, dir=import_root
        )
        temporary = Path(temporary_name)
        total = 0
        try:
            with os.fdopen(descriptor, "wb") as stream:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > limits.max_input_bytes:
                        raise HTTPException(status_code=413, detail="导入文件超过大小限制")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            selected_column: str | int | None = column
            if column is not None and column.isdecimal():
                selected_column = int(column)
            try:
                result = import_accession_file(
                    temporary,
                    column=selected_column,
                    limits=limits,
                )
            except ColumnSelectionError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": str(exc),
                        "columns": jsonable_encoder(exc.columns),
                    },
                ) from exc
            except AccessionImportError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return {
                "accessions": list(result.values),
                "values": list(result.values),
                "valid_count": result.valid_count,
                "blank_count": result.blank_count,
                "duplicate_count": result.duplicate_count,
                "invalid_count": result.invalid_count,
                "invalid_values": list(result.invalid_values),
                "available_columns": jsonable_encoder(result.available_columns),
                "selected_column": jsonable_encoder(result.selected_column),
                "encoding": result.encoding,
            }
        finally:
            temporary.unlink(missing_ok=True)

    @app.get("/api/ops/health")
    async def health(_session: WebSession = Depends(require_session)) -> Any:
        method = getattr(service, "health", None)
        service_health = await _invoke(service, "health") if callable(method) else {"ok": True}
        return {
            "ok": True,
            "service": service_health,
            "security": {
                "insecure_http": True,
                "lan_enabled": server_host in {"0.0.0.0", "::"},
                "trusted_hosts": sorted(host_policy.hosts),
            },
        }

    @app.post("/api/operations/health")
    async def operation_health(
        request: Request,
        _session: WebSession = Depends(require_csrf),
    ) -> Any:
        await _json_body(request)
        config = current_config()
        if preflight_provider is None:
            return {
                "ok": True,
                "checks": [
                    {"name": "Web 后台", "ok": True, "message": "服务运行正常"},
                    {
                        "name": "传输安全",
                        "ok": True,
                        "severity": "warning",
                        "message": "当前为裸 HTTP，仅允许在可信内网使用",
                    },
                ],
            }
        try:
            value = preflight_provider(config)
            if inspect.isawaitable(value):
                value = await value
            encoded = jsonable_encoder(value)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return {
                "ok": False,
                "checks": [
                    {"name": "运行环境", "ok": False, "message": str(exc)}
                ],
            }
        checks: list[dict[str, object]] = []
        if isinstance(encoded, dict):
            for item in encoded.get("checks", []):
                if isinstance(item, (list, tuple)) and len(item) >= 3:
                    checks.append(
                        {"name": str(item[0]), "ok": bool(item[1]), "message": str(item[2])}
                    )
                elif isinstance(item, dict):
                    checks.append(item)
            errors = encoded.get("errors", {})
        else:
            errors = {}
        checks.append(
            {
                "name": "Web 传输模式",
                "ok": True,
                "severity": "warning",
                "message": "裸 HTTP 已启用；请勿暴露到公网或不可信 Wi-Fi",
            }
        )
        return {"ok": not bool(errors), "checks": checks, "errors": errors}

    @app.get("/api/operations/release-notes")
    async def release_notes(_session: WebSession = Depends(require_session)) -> Any:
        root = resolved_project_root or Path(__file__).resolve().parents[1]
        markdown = load_release_notes(root)
        return {"releases": _release_note_entries(markdown)}

    local_operations = {
        "open-destination",
        "open-pdi",
        "open-log-directory",
        "open-data-directory",
        "acceptance-report",
        *_MANAGEMENT_OPERATIONS,
    }

    async def shutdown_application() -> dict[str, Any]:
        result = await _invoke(service, "shutdown")
        if result is not True:
            raise HTTPException(
                status_code=409,
                detail="后台任务或 DCMTK 进程未能在限定时间内停止",
            )
        if shutdown_callback is not None:
            # Let this response leave the socket before stopping uvicorn.
            threading.Timer(0.15, shutdown_callback).start()
        return {"ok": True, "result": result}

    @app.post("/api/operations/{name}")
    async def operation(
        name: str,
        request: Request,
        session: WebSession = Depends(require_csrf),
    ) -> Any:
        if name in {"health", "release-notes"}:
            raise HTTPException(status_code=405, detail="操作方法不允许")
        if name == "shutdown":
            if not session.local or not is_loopback_address(_client_ip(request)):
                raise HTTPException(status_code=403, detail="此操作只允许在服务器本机执行")
            await _json_body(request)
            return await shutdown_application()
        handler = handlers.get(name)
        if handler is None:
            raise HTTPException(status_code=501, detail=f"当前服务未配置运维操作：{name}")
        if name in local_operations:
            local_peer = session.local and is_loopback_address(_client_ip(request))
            private_manager_peer = (
                management_mode
                and name in _MANAGEMENT_OPERATIONS
                and is_management_peer_address(_client_ip(request))
            )
            if not local_peer and not private_manager_peer:
                raise HTTPException(status_code=403, detail="此操作只允许在服务器本机执行")
        payload = await _json_body(request)
        return await _call_handler(handler, payload)

    @app.get("/api/ops/profile")
    async def profile(_session: WebSession = Depends(require_session)) -> Any:
        return metadata

    @app.get("/api/ops/diagnostics")
    async def diagnostics(_session: WebSession = Depends(require_session)) -> Any:
        return await _invoke(service, "diagnostics")

    def require_management_peer(request: Request) -> None:
        if not management_mode or not is_management_peer_address(_client_ip(request)):
            raise HTTPException(
                status_code=403,
                detail="管理操作只允许来自服务器本机或可信私有网络",
            )

    def management_handler(name: str) -> Callable[[dict[str, Any]], object]:
        handler = handlers.get(name)
        if handler is None:
            raise HTTPException(status_code=501, detail=f"管理操作未配置：{name}")
        return handler

    @app.get("/api/management/profiles")
    async def management_profiles(
        request: Request,
        _session: WebSession = Depends(require_session),
    ) -> Any:
        require_management_peer(request)
        return await _call_handler(management_handler("profile-list"), {})

    @app.post("/api/management/profiles")
    async def management_create_profile(
        request: Request,
        _session: WebSession = Depends(require_csrf),
    ) -> Any:
        require_management_peer(request)
        payload = await _json_body(request)
        return await _call_handler(management_handler("profile-create"), payload)

    @app.post("/api/management/profiles/{profile_number}/start")
    async def management_start_profile(
        profile_number: int,
        request: Request,
        _session: WebSession = Depends(require_csrf),
    ) -> Any:
        require_management_peer(request)
        await _json_body(request)
        return await _call_handler(
            management_handler("profile-launch"),
            {"profile_number": profile_number},
        )

    @app.post("/api/management/profiles/{profile_number}/stop")
    async def management_stop_profile(
        profile_number: int,
        request: Request,
        _session: WebSession = Depends(require_csrf),
    ) -> Any:
        require_management_peer(request)
        await _json_body(request)
        return await _call_handler(
            management_handler("profile-stop"),
            {"profile_number": profile_number},
        )

    @app.api_route(
        "/api/management/profiles/{profile_number}/{api_path:path}",
        methods=["GET", "POST", "PUT"],
    )
    async def management_profile_api(
        profile_number: int,
        api_path: str,
        request: Request,
        session: WebSession = Depends(require_session),
    ) -> Response:
        """Same-origin JSON bridge from the manager to one loopback Profile.

        The bridge callable owns the upstream route allowlist.  Browser cookies,
        Profile ports and upstream CSRF tokens never cross the management
        boundary; mutations still require the manager session's CSRF token.
        """

        if not management_mode or profile_api_proxy is None:
            raise HTTPException(status_code=404, detail="Profile 代理未启用")
        require_management_peer(request)
        if not 1 <= profile_number <= 9999:
            raise HTTPException(status_code=400, detail="Profile 编号无效")
        method = request.method.upper()
        if method in _MUTATING_HTTP_METHODS:
            candidate = request.headers.get("x-csrf-token", "")
            if not hmac_compare(candidate, session.csrf_token):
                raise HTTPException(status_code=403, detail="CSRF 校验失败")
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_PROFILE_PROXY_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="Profile 请求体过大")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Content-Length 无效") from exc
        body = await request.body()
        if len(body) > MAX_PROFILE_PROXY_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Profile 请求体过大")
        try:
            result = profile_api_proxy(
                profile_number=profile_number,
                method=method,
                api_path=api_path,
                query=request.url.query,
                body=body,
                content_type=request.headers.get("content-type", ""),
            )
            if inspect.isawaitable(result):
                result = await result
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=502,
                detail=str(exc) or "Profile 服务暂不可用",
            ) from exc
        if not isinstance(result, Mapping):
            raise HTTPException(status_code=502, detail="Profile 代理响应无效")
        try:
            status_code = int(result.get("status_code", 200))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="Profile 代理状态码无效") from exc
        payload = result.get("payload", {})
        if api_path.strip("/") == "bootstrap" and isinstance(payload, Mapping):
            manager_url = str(request.base_url)
            upstream_web = payload.get("web")
            web = dict(upstream_web) if isinstance(upstream_web, Mapping) else {}
            web.update(
                {
                    "url": manager_url,
                    "lan_url": manager_url,
                    "local_session": is_loopback_address(_client_ip(request)),
                }
            )
            payload = {
                **payload,
                "csrf_token": session.csrf_token,
                "web": web,
            }
        return JSONResponse(
            status_code=status_code,
            content=jsonable_encoder(payload),
        )

    @app.post("/api/ops/shutdown")
    async def shutdown(
        request: Request,
        _session: WebSession = Depends(require_local_csrf),
    ) -> Any:
        await _json_body(request)
        return await shutdown_application()

    @app.get("/api/events")
    async def events(
        after_id: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        _session: WebSession = Depends(require_session),
    ) -> Any:
        return {"events": await _invoke(service, "events_since", after_id=after_id, limit=limit)}

    @app.get("/api/events/stream")
    async def event_stream(
        request: Request,
        after_id: int = Query(0, ge=0),
        session: WebSession = Depends(require_session),
    ) -> StreamingResponse:
        async def generate():
            cursor = int(after_id)
            last_output = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                live = security.sessions.get(session.token, touch=False)
                if live is None:
                    return
                batch = await _invoke(service, "events_since", after_id=cursor, limit=200)
                if not isinstance(batch, list):
                    LOGGER.error("events_since returned non-list value")
                    return
                if batch:
                    for raw_event in batch:
                        if not isinstance(raw_event, dict):
                            continue
                        try:
                            event_id = int(raw_event.get("id", cursor + 1))
                        except (TypeError, ValueError):
                            event_id = cursor + 1
                        cursor = max(cursor, event_id)
                        event_type = str(raw_event.get("type", "message"))
                        if not _SAFE_EVENT_TYPE.fullmatch(event_type):
                            event_type = "message"
                        payload = raw_event.get("payload", {})
                        outbound_type = event_type
                        if event_type in {
                            "state",
                            "progress",
                            "task_started",
                            "pdi_finished",
                            "pdi_progress",
                            "verification_progress",
                        }:
                            live_snapshot = await _invoke(service, "snapshot")
                            if isinstance(live_snapshot, dict):
                                payload = {"task": _task_view(live_snapshot)}
                                outbound_type = "task"
                        outbound = {
                            "id": event_id,
                            "type": outbound_type,
                            "timestamp": raw_event.get("timestamp"),
                            "payload": payload,
                        }
                        encoded = json.dumps(
                            jsonable_encoder(outbound),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        yield f"id: {event_id}\nevent: {outbound_type}\ndata: {encoded}\n\n"
                        last_output = time.monotonic()
                elif time.monotonic() - last_output >= SSE_HEARTBEAT_SECONDS:
                    yield ": keepalive\n\n"
                    last_output = time.monotonic()
                await asyncio.sleep(SSE_POLL_SECONDS)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    if static_directory is not None:
        # Frontend assets are a flat, bundled directory; StaticFiles rejects
        # traversal and does not follow symlinks by default.
        app.mount(
            "/assets",
            StaticFiles(directory=str(static_directory), html=False, follow_symlink=False),
            name="assets",
        )

    return app


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    try:
        return hmac.compare_digest(str(left), str(right))
    except TypeError:
        return False


class DcmGetWebServer:
    """Small lifecycle wrapper around uvicorn for launchers and tests."""

    def __init__(
        self,
        service: WebAppService | object,
        *,
        state_directory: str | Path,
        host: str = "0.0.0.0",
        port: int = 8787,
        trusted_hosts: Iterable[str] = (),
        static_root: str | Path | None = None,
        directory_roots: Mapping[str, str | Path] | Iterable[DirectoryRoot] | None = None,
        config_path: str | Path | None = None,
        project_root: str | Path | None = None,
        profile_metadata: Mapping[str, object] | None = None,
        session_ttl_seconds: int = 8 * 60 * 60,
        session_timeout_minutes: int | None = None,
        tools_provider: Callable[[AppConfig], ToolPaths] | None = None,
        preflight_provider: Callable[[AppConfig], object] | None = None,
        operation_handlers: Mapping[str, Callable[[dict[str, Any]], object]] | None = None,
        profile_api_proxy: Callable[..., object] | None = None,
        management_mode: bool = False,
        nicegui_enabled: bool = False,
        log_level: str = "info",
    ):
        self.service = service
        self.host = host
        self.port = int(port)
        self.management_mode = bool(management_mode)
        self.nicegui_enabled = bool(nicegui_enabled)
        effective_ttl = (
            int(session_timeout_minutes) * 60
            if session_timeout_minutes is not None
            else int(session_ttl_seconds)
        )
        self.security = bootstrap_web_security(
            state_directory,
            session_ttl_seconds=effective_ttl,
        )
        self.log_level = log_level
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._stop_lock = threading.Lock()
        if static_root is None:
            bundled_webui = Path(__file__).with_name("webui")
            if bundled_webui.is_dir():
                static_root = bundled_webui
        if directory_roots is None:
            directory_roots = default_directory_roots()
        self.app = create_web_app(
            service,
            security=self.security,
            server_host=host,
            server_port=self.port,
            trusted_hosts=trusted_hosts,
            static_root=static_root,
            directory_roots=directory_roots,
            shutdown_callback=self.request_shutdown,
            config_path=config_path,
            project_root=project_root,
            profile_metadata=profile_metadata,
            tools_provider=tools_provider,
            preflight_provider=preflight_provider,
            operation_handlers=operation_handlers,
            profile_api_proxy=profile_api_proxy,
            management_mode=self.management_mode,
            nicegui_mount_path="/workspace" if self.nicegui_enabled else None,
        )
        if self.nicegui_enabled:
            try:
                from .nicegui_ui import install_nicegui
            except ImportError as exc:
                raise RuntimeError(
                    "NiceGUI 界面组件未安装，请先执行 pip install -r requirements.txt"
                ) from exc

            install_nicegui(self.app, mount_path="/workspace")

    @property
    def bootstrap_password(self) -> str | None:
        """Compatibility signal; first-run setup no longer displays this value."""

        return self.security.bootstrap_password

    @property
    def url(self) -> str:
        display_host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        if ":" in display_host and not display_host.startswith("["):
            display_host = f"[{display_host}]"
        return f"http://{display_host}:{self.port}/"

    @property
    def security_status(self) -> dict[str, object]:
        return {
            "insecure_http": True,
            "transport": "http",
            "host": self.host,
            "port": self.port,
            "lan_enabled": self.host in {"0.0.0.0", "::"},
        }

    def _make_uvicorn_server(self):
        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError("缺少 Web 运行依赖 uvicorn") from exc
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level=self.log_level,
            # PyInstaller's Windows ``--windowed`` mode sets stdout/stderr to
            # None.  Uvicorn's default formatter calls ``isatty()`` on those
            # streams while Config is constructed, before the server starts.
            # DcmGet already installs its own rotating diagnostic handlers.
            log_config=None,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            date_header=True,
        )
        return uvicorn.Server(config)

    def run(self) -> None:
        if self._server is not None:
            raise RuntimeError("Web 服务已经启动")
        self._server = self._make_uvicorn_server()
        try:
            self._server.run()
        finally:
            self._server = None

    def start_background(self, timeout: float = 10.0) -> str:
        if self._thread is not None and self._thread.is_alive():
            return self.url
        if timeout <= 0:
            raise ValueError("启动超时必须大于 0")
        self._server = self._make_uvicorn_server()

        def runner() -> None:
            try:
                self._server.run()
            finally:
                self._server = None

        self._thread = threading.Thread(target=runner, name="DcmGetWebServer", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            server = self._server
            if server is not None and bool(getattr(server, "started", False)):
                return self.url
            if not self._thread.is_alive():
                break
            time.sleep(0.025)
        self.request_shutdown()
        self._thread.join(timeout=1)
        raise RuntimeError("DcmGet Web 服务启动失败或超时")

    def request_shutdown(self) -> None:
        server = self._server
        if server is not None:
            server.should_exit = True

    def stop(self, timeout: float = 10.0) -> None:
        with self._stop_lock:
            shutdown = getattr(self.service, "shutdown", None)
            if callable(shutdown) and shutdown() is not True:
                raise RuntimeError("DcmGet 后台进程未能在超时内停止")
            self.security.sessions.revoke_all()
            self.request_shutdown()
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, timeout))
                if thread.is_alive():
                    raise RuntimeError("DcmGet Web 服务未能在超时内停止")
            self._thread = None
