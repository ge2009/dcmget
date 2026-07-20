from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import stat
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping
from urllib.parse import urlsplit


AUTH_FILE_NAME = "web-auth.json"
AUTH_FILE_VERSION = 1
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_BYTES = 1024
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60
DEFAULT_LOGIN_WINDOW_SECONDS = 5 * 60
DEFAULT_LOGIN_FAILURES = 5
DEFAULT_LOGIN_LOCK_SECONDS = 15 * 60


class WebSecurityError(RuntimeError):
    """Raised when the local Web security state cannot be trusted."""


class UnsafePathError(ValueError):
    """Raised when a directory-browser request escapes its allowlisted root."""


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise WebSecurityError(f"安全状态目录无效：{path}")
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _atomic_private_json(path: Path, payload: Mapping[str, object]) -> None:
    if path.is_symlink():
        raise WebSecurityError(f"拒绝写入符号链接：{path}")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOINHERIT", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def generate_admin_password() -> str:
    """Return a URL-safe password with at least 192 bits of entropy."""

    return secrets.token_urlsafe(24)


def _password_bytes(password: str) -> bytes:
    if not isinstance(password, str):
        raise TypeError("密码必须是文本")
    encoded = password.encode("utf-8")
    if not encoded or len(encoded) > PASSWORD_MAX_BYTES:
        raise ValueError("密码长度无效")
    return encoded


def _password_record(password: str, *, setup_complete: bool) -> dict[str, object]:
    encoded = _password_bytes(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        encoded,
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return {
        "version": AUTH_FILE_VERSION,
        "algorithm": "scrypt",
        "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "digest": base64.urlsafe_b64encode(digest).decode("ascii"),
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "dklen": SCRYPT_DKLEN,
        "setup_complete": bool(setup_complete),
    }


def _decode_b64(value: object, *, field: str) -> bytes:
    if not isinstance(value, str) or len(value) > 256:
        raise WebSecurityError(f"认证文件字段无效：{field}")
    try:
        return base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeError) as exc:
        raise WebSecurityError(f"认证文件字段无效：{field}") from exc


class PasswordStore:
    """Persistent administrator credential using a deliberately slow KDF.

    Only the scrypt salt and digest are persisted.  ``load_or_create`` returns
    the initial plaintext once to the launcher so it can be shown to the local
    operator; it is never written to disk by this class.
    """

    def __init__(self, state_directory: str | Path):
        self.state_directory = Path(state_directory).expanduser().resolve()
        self.path = self.state_directory / AUTH_FILE_NAME
        self._lock = threading.RLock()

    def load_or_create(self) -> str | None:
        with self._lock:
            _private_directory(self.state_directory)
            if self.path.exists():
                self._load_record()
                return None
            password = generate_admin_password()
            _atomic_private_json(
                self.path,
                _password_record(password, setup_complete=False),
            )
            return password

    def verify(self, password: str) -> bool:
        try:
            encoded = _password_bytes(password)
        except (TypeError, ValueError):
            return False
        with self._lock:
            record = self._load_record()
        try:
            candidate = hashlib.scrypt(
                encoded,
                salt=record["salt"],
                n=record["n"],
                r=record["r"],
                p=record["p"],
                dklen=record["dklen"],
            )
        except (MemoryError, OverflowError, ValueError) as exc:
            raise WebSecurityError("无法校验管理员密码") from exc
        return hmac.compare_digest(candidate, record["digest"])

    def replace(self, password: str) -> None:
        if len(password) < PASSWORD_MIN_LENGTH:
            raise ValueError(f"新密码至少需要 {PASSWORD_MIN_LENGTH} 个字符")
        record = _password_record(password, setup_complete=True)
        with self._lock:
            _private_directory(self.state_directory)
            _atomic_private_json(self.path, record)

    def setup_complete(self) -> bool:
        with self._lock:
            return bool(self._load_record()["setup_complete"])

    def _load_record(self) -> dict[str, object]:
        if not self.path.is_file() or self.path.is_symlink():
            raise WebSecurityError(f"管理员认证文件无效：{self.path}")
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
            if os.name != "nt" and mode & 0o077:
                self.path.chmod(0o600)
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise WebSecurityError("管理员认证文件损坏") from exc
        if not isinstance(payload, dict):
            raise WebSecurityError("管理员认证文件损坏")
        if payload.get("version") != AUTH_FILE_VERSION or payload.get("algorithm") != "scrypt":
            raise WebSecurityError("不支持的管理员认证文件版本")
        try:
            n = int(payload["n"])
            r = int(payload["r"])
            p = int(payload["p"])
            dklen = int(payload["dklen"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WebSecurityError("管理员认证参数损坏") from exc
        # Do not allow a modified state file to turn login into a memory/CPU DoS.
        if (n, r, p, dklen) != (SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN):
            raise WebSecurityError("管理员认证参数不受支持")
        salt = _decode_b64(payload.get("salt"), field="salt")
        digest = _decode_b64(payload.get("digest"), field="digest")
        if len(salt) != 16 or len(digest) != dklen:
            raise WebSecurityError("管理员认证文件长度无效")
        return {
            "salt": salt,
            "digest": digest,
            "n": n,
            "r": r,
            "p": p,
            "dklen": dklen,
            "setup_complete": bool(payload.get("setup_complete", True)),
        }


@dataclass(frozen=True, slots=True)
class WebSession:
    token: str
    csrf_token: str
    remote_ip: str
    local: bool
    created_at: float
    expires_at: float


class SessionStore:
    def __init__(self, *, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS):
        if ttl_seconds < 60:
            raise ValueError("会话有效期至少为 60 秒")
        self.ttl_seconds = int(ttl_seconds)
        self._sessions: dict[str, WebSession] = {}
        self._lock = threading.RLock()

    def create(self, remote_ip: str) -> WebSession:
        now = time.time()
        session = WebSession(
            token=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            remote_ip=remote_ip,
            local=is_loopback_address(remote_ip),
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._prune(now)
            self._sessions[session.token] = session
        return session

    def get(self, token: str | None, *, touch: bool = True) -> WebSession | None:
        if not token or len(token) > 256:
            return None
        now = time.time()
        with self._lock:
            self._prune(now)
            session = self._sessions.get(token)
            if session is None:
                return None
            if touch:
                session = WebSession(
                    token=session.token,
                    csrf_token=session.csrf_token,
                    remote_ip=session.remote_ip,
                    local=session.local,
                    created_at=session.created_at,
                    expires_at=now + self.ttl_seconds,
                )
                self._sessions[token] = session
            return session

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def _prune(self, now: float) -> None:
        expired = [token for token, session in self._sessions.items() if session.expires_at <= now]
        for token in expired:
            self._sessions.pop(token, None)


class LoginRateLimiter:
    def __init__(
        self,
        *,
        max_failures: int = DEFAULT_LOGIN_FAILURES,
        window_seconds: int = DEFAULT_LOGIN_WINDOW_SECONDS,
        lock_seconds: int = DEFAULT_LOGIN_LOCK_SECONDS,
        clock=time.monotonic,
    ):
        if max_failures < 1 or window_seconds < 1 or lock_seconds < 1:
            raise ValueError("登录限速参数必须大于 0")
        self.max_failures = int(max_failures)
        self.window_seconds = float(window_seconds)
        self.lock_seconds = float(lock_seconds)
        self._clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}
        self._lock = threading.RLock()

    def retry_after(self, identity: str) -> int:
        now = self._clock()
        with self._lock:
            locked_until = self._locked_until.get(identity, 0.0)
            if locked_until <= now:
                self._locked_until.pop(identity, None)
                return 0
            return max(1, int(locked_until - now + 0.999))

    def failure(self, identity: str) -> int:
        now = self._clock()
        with self._lock:
            failures = self._failures[identity]
            cutoff = now - self.window_seconds
            while failures and failures[0] < cutoff:
                failures.popleft()
            failures.append(now)
            if len(failures) >= self.max_failures:
                self._locked_until[identity] = now + self.lock_seconds
                failures.clear()
                return int(self.lock_seconds)
            return 0

    def success(self, identity: str) -> None:
        with self._lock:
            self._failures.pop(identity, None)
            self._locked_until.pop(identity, None)


def is_loopback_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_loopback
    except ValueError:
        return value.strip().lower() == "localhost"


def discover_local_hosts() -> frozenset[str]:
    hosts = {"localhost", "127.0.0.1", "::1"}
    # Host discovery runs on the startup path.  DNS and mDNS resolution can
    # block for minutes on isolated hospital networks, so never call
    # getfqdn/getaddrinfo here.  The launcher supplies every interface address
    # separately; this fallback only needs the kernel hostname and primary IP.
    hostname = socket.gethostname().strip().lower().rstrip(".")
    if hostname:
        hosts.add(hostname)
    # Discover the primary LAN address without sending any traffic.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        hosts.add(str(probe.getsockname()[0]).lower())
    except OSError:
        pass
    finally:
        probe.close()
    return frozenset(hosts)


def _split_authority(value: str) -> tuple[str, int | None] | None:
    raw = value.strip()
    if not raw or any(character in raw for character in " /\\@,\t\r\n"):
        return None
    if raw.startswith("["):
        closing = raw.find("]")
        if closing < 0:
            return None
        host = raw[1:closing]
        remainder = raw[closing + 1 :]
        if not remainder:
            port = None
        elif remainder.startswith(":") and remainder[1:].isdigit():
            port = int(remainder[1:])
        else:
            return None
    else:
        if raw.count(":") == 1:
            host, separator, port_text = raw.rpartition(":")
            if separator and port_text.isdigit():
                port = int(port_text)
            else:
                host, port = raw, None
        elif ":" in raw:
            host, port = raw, None
        else:
            host, port = raw, None
    host = host.strip().lower().rstrip(".")
    if not host or not 0 <= (port or 0) <= 65535:
        return None
    return host, port


class HostPolicy:
    """Exact Host/Origin allowlist used to resist DNS rebinding attacks."""

    def __init__(self, port: int, trusted_hosts: Iterable[str] = ()):
        if not 1 <= int(port) <= 65535:
            raise ValueError("Web 服务端口无效")
        self.port = int(port)
        parsed: set[str] = set(discover_local_hosts())
        for value in trusted_hosts:
            if value == "*":
                raise ValueError("trusted_hosts 不允许使用通配符")
            result = _split_authority(str(value))
            if result is None:
                raise ValueError(f"trusted_hosts 项无效：{value}")
            host, configured_port = result
            if configured_port not in (None, self.port):
                raise ValueError(f"trusted_hosts 端口与服务端口不一致：{value}")
            parsed.add(host)
        self.hosts = frozenset(parsed)

    def allows_host_header(self, value: str) -> bool:
        result = _split_authority(value)
        if result is None:
            return False
        host, port = result
        effective_port = 80 if port is None else port
        return host in self.hosts and effective_port == self.port

    def allows_origin(self, value: str | None) -> bool:
        if not value or value == "null":
            return False
        try:
            parsed = urlsplit(value)
            if parsed.scheme.lower() != "http" or parsed.username or parsed.password:
                return False
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                return False
            host = parsed.hostname
            port = parsed.port or 80
        except (ValueError, UnicodeError):
            return False
        return bool(host) and host.lower().rstrip(".") in self.hosts and port == self.port


@dataclass(frozen=True, slots=True)
class DirectoryRoot:
    root_id: str
    label: str
    path: Path


class SafeDirectoryBrowser:
    """List directories under explicit roots without following symlinks."""

    def __init__(
        self,
        roots: Mapping[str, str | Path] | Iterable[DirectoryRoot],
        *,
        max_entries: int = 500,
    ):
        if max_entries < 1 or max_entries > 10_000:
            raise ValueError("目录枚举上限无效")
        normalized: dict[str, DirectoryRoot] = {}
        if isinstance(roots, Mapping):
            values = (
                DirectoryRoot(str(root_id), str(root_id), Path(path))
                for root_id, path in roots.items()
            )
        else:
            values = iter(roots)
        for item in values:
            root_id = str(item.root_id).strip()
            if not root_id or "/" in root_id or "\\" in root_id or root_id in normalized:
                raise ValueError(f"目录根标识无效：{root_id}")
            source = Path(item.path).expanduser()
            if source.is_symlink():
                raise ValueError(f"目录根不能是符号链接：{source}")
            try:
                resolved = source.resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"目录根不存在：{source}") from exc
            if not resolved.is_dir():
                raise ValueError(f"目录根不是目录：{source}")
            normalized[root_id] = DirectoryRoot(root_id, str(item.label), resolved)
        self._roots = normalized
        self.max_entries = int(max_entries)

    def roots(self) -> list[dict[str, str]]:
        return [
            {
                "id": item.root_id,
                "label": item.label,
                "display_path": str(item.path),
            }
            for item in self._roots.values()
        ]

    def list(self, root_id: str, relative_path: str = "") -> dict[str, object]:
        root = self._roots.get(root_id)
        if root is None:
            raise UnsafePathError("未知目录根")
        parts = self._relative_parts(relative_path)
        candidate = root.path.joinpath(*parts)
        self._reject_symlink_components(root.path, parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root.path)
        except (OSError, ValueError) as exc:
            raise UnsafePathError("目录不存在或超出允许范围") from exc
        if not resolved.is_dir() or resolved.is_symlink():
            raise UnsafePathError("目标不是安全目录")

        entries: list[dict[str, object]] = []
        truncated = False
        try:
            with os.scandir(resolved) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if len(entries) >= self.max_entries:
                        truncated = True
                        break
                    relative = PurePosixPath(*parts, entry.name).as_posix()
                    entries.append({"name": entry.name, "path": relative})
        except OSError as exc:
            raise UnsafePathError("无法读取目录") from exc
        entries.sort(key=lambda item: str(item["name"]).casefold())
        current = PurePosixPath(*parts).as_posix() if parts else ""
        parent = PurePosixPath(*parts[:-1]).as_posix() if parts else None
        return {
            "root_id": root.root_id,
            "path": current,
            "parent": parent,
            "entries": entries,
            "truncated": truncated,
        }

    def authorize_directory(self, value: str | Path) -> Path:
        """Resolve an absolute directory only when it is below an allowlisted root."""

        source = Path(value).expanduser()
        if source.is_symlink():
            raise UnsafePathError("目录不能是符号链接")
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise UnsafePathError("目录不存在") from exc
        if not resolved.is_dir():
            raise UnsafePathError("目标不是目录")
        for root in self._roots.values():
            try:
                relative = resolved.relative_to(root.path)
            except ValueError:
                continue
            self._reject_symlink_components(root.path, tuple(relative.parts))
            return resolved
        raise UnsafePathError("目录超出允许范围")

    def default_directory(self) -> Path:
        try:
            return next(iter(self._roots.values())).path
        except StopIteration as exc:
            raise UnsafePathError("没有可用的目录根") from exc

    def list_absolute(self, value: str | Path | None = None) -> dict[str, object]:
        selected = self.default_directory() if value in (None, "") else self.authorize_directory(value)
        listing: dict[str, object] | None = None
        for root in self._roots.values():
            try:
                relative = selected.relative_to(root.path)
            except ValueError:
                continue
            relative_text = (
                PurePosixPath(*relative.parts).as_posix() if relative.parts else ""
            )
            listing = self.list(root.root_id, relative_text)
            break
        if listing is None:
            raise UnsafePathError("目录超出允许范围")
        directories = [
            {
                "name": str(item["name"]),
                "path": str(selected / str(item["name"])),
            }
            for item in listing["entries"]
            if isinstance(item, dict)
        ]
        parent: str | None = None
        for root in self._roots.values():
            try:
                relative = selected.relative_to(root.path)
            except ValueError:
                continue
            if relative.parts:
                parent = str(selected.parent)
            break
        return {
            "path": str(selected),
            "parent": parent,
            "directories": directories,
            "truncated": bool(listing["truncated"]),
        }

    @staticmethod
    def _relative_parts(value: str) -> tuple[str, ...]:
        if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
            raise UnsafePathError("目录路径无效")
        if "\\" in value or value.startswith("/"):
            raise UnsafePathError("仅允许相对目录路径")
        path = PurePosixPath(value)
        parts = tuple(path.parts)
        if any(part in ("", ".", "..") or ":" in part for part in parts):
            raise UnsafePathError("目录路径包含非法片段")
        return parts

    @staticmethod
    def _reject_symlink_components(root: Path, parts: tuple[str, ...]) -> None:
        current = root
        for part in parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise UnsafePathError("目录路径不能包含符号链接")
            except OSError as exc:
                raise UnsafePathError("无法验证目录路径") from exc


@dataclass(slots=True)
class WebSecurityContext:
    password_store: PasswordStore
    sessions: SessionStore
    login_limiter: LoginRateLimiter
    bootstrap_password: str | None

    def status(self) -> dict[str, object]:
        return {
            "authenticated": True,
            "passwordless": True,
            "insecure_http": True,
            "transport": "http",
            "warning": "局域网 HTTP 未加密，请仅在可信内网使用。",
        }


def bootstrap_web_security(
    state_directory: str | Path,
    *,
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
) -> WebSecurityContext:
    password_store = PasswordStore(state_directory)
    # Password authentication was removed from the Web workspace.  Keep the
    # store object only as a compatibility holder for the private state path;
    # deliberately do not read or rewrite a legacy web-auth.json.  A damaged
    # credential left by an older release must not prevent the application
    # from starting in passwordless mode.
    _private_directory(password_store.state_directory)
    return WebSecurityContext(
        password_store=password_store,
        sessions=SessionStore(ttl_seconds=session_ttl_seconds),
        login_limiter=LoginRateLimiter(),
        bootstrap_password=None,
    )
