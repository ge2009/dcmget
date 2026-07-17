from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping

from filelock import FileLock, Timeout

from .runtime import ensure_application_state_dir


MAX_MESSAGE_BYTES = 64 * 1024


class SingleInstanceError(RuntimeError):
    pass


ActivationHandler = Callable[[dict[str, object]], None]


def default_single_instance_path() -> Path:
    return ensure_application_state_dir() / "gui-instance.json"


class SingleInstance:
    """Own the GUI process lock or notify the process that already owns it.

    The file lock is authoritative.  The loopback socket is used only to carry
    activation messages, so stale endpoint metadata can never create a second
    primary instance.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        activation_handler: ActivationHandler | None = None,
        startup_timeout: float = 2.0,
        connect_timeout: float = 0.25,
    ):
        self.path = Path(path).expanduser() if path else default_single_instance_path()
        self._lock = FileLock(str(self.path) + ".lock")
        self._activation_handler = activation_handler
        self._startup_timeout = max(0.05, float(startup_timeout))
        self._connect_timeout = max(0.05, float(connect_timeout))
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()
        self._state_lock = threading.RLock()
        self._pending: list[dict[str, object]] = []
        self._token = ""
        self._primary = False

    @property
    def is_primary(self) -> bool:
        with self._state_lock:
            return self._primary

    def start(self, payload: Mapping[str, object] | None = None) -> bool:
        """Return ``True`` for the primary, ``False`` after waking it.

        A short retry window covers the interval between the primary acquiring
        its process lock and publishing its loopback endpoint.
        """

        message = dict(payload or {"action": "activate"})
        deadline = time.monotonic() + self._startup_timeout
        while True:
            if self._try_become_primary():
                return True
            if self._notify_primary(message):
                return False
            if time.monotonic() >= deadline:
                if self._try_become_primary():
                    return True
                raise SingleInstanceError(
                    "检测到 DcmGet 主实例，但无法通知其显示窗口；"
                    "请稍后重试。"
                )
            time.sleep(0.05)

    def set_activation_handler(self, handler: ActivationHandler | None) -> None:
        with self._state_lock:
            self._activation_handler = handler
            pending = self._pending if handler is not None else []
            if handler is not None:
                self._pending = []
        if handler is not None:
            for payload in pending:
                self._dispatch(handler, payload)

    def close(self) -> None:
        with self._state_lock:
            was_primary = self._primary
            self._primary = False
            server = self._server
            self._server = None
            thread = self._thread
            self._thread = None
            self._stop_requested.set()
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if was_primary:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
        if self._lock.is_locked:
            self._lock.release()

    def __enter__(self) -> SingleInstance:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _try_become_primary(self) -> bool:
        with self._state_lock:
            if self._primary:
                return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._lock.acquire(timeout=0)
        except Timeout:
            return False
        try:
            self._start_server()
        except Exception:
            self._lock.release()
            raise
        return True

    def _start_server(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen(4)
            server.settimeout(0.2)
            token = secrets.token_urlsafe(32)
            port = int(server.getsockname()[1])
            self._write_metadata(port, token)
        except Exception:
            server.close()
            raise
        with self._state_lock:
            self._token = token
            self._server = server
            self._stop_requested.clear()
            self._primary = True
            self._thread = threading.Thread(
                target=self._serve,
                name="dcmget-single-instance",
                daemon=True,
            )
            self._thread.start()

    def _write_metadata(self, port: int, token: str) -> None:
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        payload = json.dumps(
            {"version": 1, "pid": os.getpid(), "port": port, "token": token},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            temporary.write_text(payload, encoding="utf-8")
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _read_metadata(self) -> tuple[int, str] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                return None
            port = int(payload["port"])
            token = str(payload["token"])
            if not 1 <= port <= 65535 or len(token) < 32:
                return None
            return port, token
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _notify_primary(self, payload: dict[str, object]) -> bool:
        endpoint = self._read_metadata()
        if endpoint is None:
            return False
        port, token = endpoint
        request = json.dumps(
            {"token": token, "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(request) > MAX_MESSAGE_BYTES:
            raise SingleInstanceError("单实例唤醒消息过大")
        try:
            with socket.create_connection(
                ("127.0.0.1", port), timeout=self._connect_timeout
            ) as client:
                client.settimeout(self._connect_timeout)
                client.sendall(request)
                return client.recv(16) == b"OK\n"
        except OSError:
            return False

    def _serve(self) -> None:
        while not self._stop_requested.is_set():
            with self._state_lock:
                server = self._server
            if server is None:
                return
            try:
                client, _address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with client:
                client.settimeout(self._connect_timeout)
                try:
                    raw = self._receive_line(client)
                    request = json.loads(raw.decode("utf-8"))
                    if not isinstance(request, dict):
                        raise ValueError("invalid message")
                    token = str(request.get("token", ""))
                    payload = request.get("payload")
                    if not hmac.compare_digest(token, self._token):
                        raise ValueError("invalid token")
                    if not isinstance(payload, dict):
                        raise ValueError("invalid payload")
                    normalized = {str(key): value for key, value in payload.items()}
                    self._handle_activation(normalized)
                    client.sendall(b"OK\n")
                except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
                    try:
                        client.sendall(b"ERROR\n")
                    except OSError:
                        pass

    @staticmethod
    def _receive_line(client: socket.socket) -> bytes:
        chunks = bytearray()
        while len(chunks) <= MAX_MESSAGE_BYTES:
            block = client.recv(min(4096, MAX_MESSAGE_BYTES + 1 - len(chunks)))
            if not block:
                break
            newline = block.find(b"\n")
            if newline >= 0:
                chunks.extend(block[:newline])
                break
            chunks.extend(block)
        if not chunks or len(chunks) > MAX_MESSAGE_BYTES:
            raise ValueError("invalid message size")
        return bytes(chunks)

    def _handle_activation(self, payload: dict[str, object]) -> None:
        with self._state_lock:
            handler = self._activation_handler
            if handler is None:
                self._pending.append(payload)
                return
        self._dispatch(handler, payload)

    @staticmethod
    def _dispatch(handler: ActivationHandler, payload: dict[str, object]) -> None:
        try:
            handler(payload)
        except Exception:
            # Activation must never terminate the socket listener.  The GUI's
            # Qt bridge is responsible for recording handler-side failures.
            pass
