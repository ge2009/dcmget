from __future__ import annotations

import hashlib
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from pydicom import dcmread
from pydicom.tag import Tag
from pydicom.uid import UID
from pynetdicom import (
    AE,
    ALL_TRANSFER_SYNTAXES,
    AllStoragePresentationContexts,
    evt,
)
from pynetdicom import _config as pynetdicom_config
from pynetdicom.sop_class import Verification

from .runtime import ensure_application_state_dir


StorageLogCallback = Callable[[str, str, str], None]

_ACCESSION_NUMBER = Tag(0x0008, 0x0050)
_SOP_INSTANCE_UID = Tag(0x0008, 0x0018)
_STUDY_INSTANCE_UID = Tag(0x0020, 0x000D)
_STORE_SUCCESS = 0x0000
_STORE_FAILURE = 0xC000

_chunked_receive_lock = threading.Lock()
_chunked_receive_users = 0
_chunked_receive_previous = False


@dataclass(frozen=True, slots=True)
class StorageRoute:
    token: str
    accession: str
    directory: Path


class PynetdicomStorageSCP:
    """One cross-platform, concurrent C-STORE SCP shared by all tasks.

    Incoming objects are routed by an exact Accession Number match or a Study
    Instance UID already bound to that route. A compatibility fallback for
    tagless legacy PACS objects must be explicitly enabled for serial mode.
    Unknown objects are preserved in the private quarantine directory and
    reported as failed C-STORE sub-operations instead of being guessed.
    """

    def __init__(
        self,
        ae_title: str,
        port: int,
        *,
        bind_address: str = "0.0.0.0",
        quarantine_directory: str | Path | None = None,
        log_callback: StorageLogCallback | None = None,
        maximum_associations: int = 32,
        allow_single_route_fallback: bool = False,
    ) -> None:
        title = ae_title.strip()
        if not title or len(title) > 16 or not title.isascii():
            raise ValueError("接收 AE 必须是 1-16 个 ASCII 字符")
        if not 0 <= int(port) <= 65535:
            raise ValueError("接收端口必须在 0-65535 之间")
        if maximum_associations < 1:
            raise ValueError("最大 association 数必须大于 0")

        self.ae_title = title
        self.port = int(port)
        self.bind_address = bind_address
        self.maximum_associations = int(maximum_associations)
        self.allow_single_route_fallback = bool(allow_single_route_fallback)
        self.quarantine_directory = (
            Path(quarantine_directory).expanduser()
            if quarantine_directory is not None
            else ensure_application_state_dir() / "quarantine" / "receiver"
        )
        self.log_callback = log_callback or (
            lambda _source, _message, _level: None
        )

        self._lifecycle_lock = threading.RLock()
        self._route_lock = threading.RLock()
        self._route_condition = threading.Condition(self._route_lock)
        self._publish_lock = threading.Lock()
        self._routes: dict[str, StorageRoute] = {}
        self._accession_routes: dict[str, str] = {}
        self._study_routes: dict[str, str] = {}
        self._route_studies: dict[str, set[str]] = {}
        self._route_inflight: dict[str, int] = {}
        self._ae: AE | None = None
        self._server: object | None = None
        self._chunked_receive_enabled = False
        self._started_once = False

    @property
    def listening_port(self) -> int:
        with self._lifecycle_lock:
            server = self._server
            if server is None:
                return self.port
            address = getattr(server, "server_address", None)
            if isinstance(address, tuple) and len(address) >= 2:
                return int(address[1])
            return self.port

    def start(self) -> PynetdicomStorageSCP:
        with self._lifecycle_lock:
            if self._server is not None:
                return self
            self.quarantine_directory.mkdir(
                parents=True, exist_ok=True, mode=0o700
            )
            _enable_chunked_receive()
            self._chunked_receive_enabled = True
            try:
                ae = AE(ae_title=self.ae_title)
                ae.require_called_aet = True
                ae.maximum_associations = self.maximum_associations
                ae.acse_timeout = 30
                ae.dimse_timeout = 300
                ae.network_timeout = 60
                for context in AllStoragePresentationContexts:
                    ae.add_supported_context(
                        context.abstract_syntax,
                        ALL_TRANSFER_SYNTAXES,
                    )
                ae.add_supported_context(Verification)
                server = ae.start_server(
                    (self.bind_address, self.port),
                    block=False,
                    evt_handlers=[(evt.EVT_C_STORE, self._handle_store)],
                )
                if server is None:
                    raise RuntimeError("pynetdicom 未返回接收服务句柄")
            except Exception:
                self._disable_chunked_receive()
                raise
            self._ae = ae
            self._server = server
            self._started_once = True
            self._emit(
                "接收器",
                (
                    f"pynetdicom 共享接收器已监听 {self.bind_address}:"
                    f"{self.listening_port}（AE {self.ae_title}，"
                    f"最多 {self.maximum_associations} 个并发 association）"
                ),
                "success",
            )
            return self

    def stop(self) -> None:
        with self._lifecycle_lock:
            server = self._server
            self._server = None
            self._ae = None
            try:
                if server is not None:
                    shutdown = getattr(server, "shutdown", None)
                    if callable(shutdown):
                        shutdown()
                    self._emit("接收器", "pynetdicom 共享接收器已停止", "info")
            finally:
                self._disable_chunked_receive()

    def poll(self) -> int | None:
        """Match the liveness subset of ``subprocess.Popen.poll``."""

        with self._lifecycle_lock:
            server = self._server
            if server is None:
                return 0 if self._started_once else 1
            socket = getattr(server, "socket", None)
            fileno = getattr(socket, "fileno", None)
            if callable(fileno):
                try:
                    if fileno() < 0:
                        return 1
                except OSError:
                    return 1
            return None

    def register_route(
        self,
        accession: str,
        directory: str | Path,
        *,
        study_instance_uids: tuple[str, ...] = (),
    ) -> StorageRoute:
        value = _clean_value(accession)
        if not value:
            raise ValueError("检查号不能为空")
        destination = Path(directory).expanduser()
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        route = StorageRoute(uuid.uuid4().hex, value, destination)
        with self._route_lock:
            if value in self._accession_routes:
                raise ValueError(f"检查号 {value} 已注册接收路由")
            self._routes[route.token] = route
            self._accession_routes[value] = route.token
            self._route_studies[route.token] = set()
            self._route_inflight[route.token] = 0
            try:
                for study_uid in study_instance_uids:
                    self._bind_study_uid_locked(route.token, study_uid)
            except Exception:
                self._remove_route_locked(route.token)
                self._route_inflight.pop(route.token, None)
                raise
        return route

    def unregister_route(self, route: StorageRoute | str) -> None:
        token = route.token if isinstance(route, StorageRoute) else route
        with self._route_condition:
            self._remove_route_locked(token)
            while self._route_inflight.get(token, 0):
                self._route_condition.wait(timeout=0.1)
            self._route_inflight.pop(token, None)

    def bind_study_uid(self, route: StorageRoute | str, study_uid: str) -> None:
        token = route.token if isinstance(route, StorageRoute) else route
        with self._route_lock:
            if token not in self._routes:
                raise KeyError("接收路由不存在")
            self._bind_study_uid_locked(token, study_uid)

    @contextmanager
    def receive_context(
        self,
        accession: str,
        directory: str | Path,
        *,
        study_instance_uids: tuple[str, ...] = (),
    ) -> Iterator[StorageRoute]:
        route = self.register_route(
            accession,
            directory,
            study_instance_uids=study_instance_uids,
        )
        try:
            yield route
        finally:
            self.unregister_route(route)

    def _handle_store(self, event: object) -> int:
        source: Path | None = None
        try:
            source = Path(getattr(event, "dataset_path"))
            dataset = dcmread(
                source,
                stop_before_pixels=True,
                specific_tags=[
                    _ACCESSION_NUMBER,
                    _SOP_INSTANCE_UID,
                    _STUDY_INSTANCE_UID,
                ],
            )
            accession = _clean_value(dataset.get("AccessionNumber", ""))
            study_uid = _clean_uid(dataset.get("StudyInstanceUID", ""))
            sop_uid = _clean_uid(dataset.get("SOPInstanceUID", ""))
            file_meta = getattr(event, "file_meta")
            command_sop_uid = _clean_uid(
                getattr(file_meta, "MediaStorageSOPInstanceUID", "")
            )
            if not sop_uid or not command_sop_uid or sop_uid != command_sop_uid:
                raise ValueError("数据集与 C-STORE 命令的 SOP Instance UID 不一致")

            route, reason = self._resolve_route(accession, study_uid)
            if route is None:
                quarantined = self._quarantine(source, command_sop_uid)
                self._emit(
                    "接收器",
                    f"收到无法归属的 DICOM，已隔离到 {quarantined}：{reason}",
                    "warning",
                )
                return _STORE_FAILURE

            try:
                outcome, destination = self._publish(
                    source,
                    route.directory,
                    f"{command_sop_uid}.dcm",
                )
            finally:
                self._release_route(route.token)
            if outcome == "conflict":
                self._emit(
                    "接收器",
                    f"SOP Instance UID 内容冲突，已隔离新文件：{destination}",
                    "error",
                )
                return _STORE_FAILURE
            if outcome == "duplicate":
                self._emit(
                    "接收器",
                    f"忽略内容相同的重复实例 {command_sop_uid}",
                    "warning",
                )
            return _STORE_SUCCESS
        except Exception as exc:
            quarantined: Path | None = None
            if source is not None and source.is_file():
                try:
                    quarantined = self._quarantine(source, uuid.uuid4().hex)
                except OSError:
                    quarantined = None
            suffix = f"，原始文件已隔离到 {quarantined}" if quarantined else ""
            self._emit("接收器", f"C-STORE 保存失败：{exc}{suffix}", "error")
            return _STORE_FAILURE

    def _resolve_route(
        self, accession: str, study_uid: str
    ) -> tuple[StorageRoute | None, str]:
        with self._route_lock:
            accession_token = self._accession_routes.get(accession) if accession else None
            study_token = self._study_routes.get(study_uid) if study_uid else None
            if accession_token is not None:
                if study_token is not None and study_token != accession_token:
                    return None, "检查号与已绑定 Study Instance UID 冲突"
                if study_uid:
                    self._bind_study_uid_locked(accession_token, study_uid)
                return self._claim_route_locked(accession_token), ""
            if study_token is not None:
                return self._claim_route_locked(study_token), ""
            if accession:
                return None, "检查号未注册"
            if study_uid:
                if self.allow_single_route_fallback and len(self._routes) == 1:
                    token = next(iter(self._routes))
                    self._bind_study_uid_locked(token, study_uid)
                    return self._claim_route_locked(token), ""
                return None, "检查号缺失且 Study Instance UID 尚未绑定"
            return None, "检查号和 Study Instance UID 均缺失"

    def _bind_study_uid_locked(self, token: str, study_uid: str) -> None:
        value = _clean_uid(study_uid)
        if not value:
            raise ValueError("Study Instance UID 无效")
        existing = self._study_routes.get(value)
        if existing is not None and existing != token:
            raise ValueError("Study Instance UID 已绑定到另一个接收路由")
        self._study_routes[value] = token
        self._route_studies[token].add(value)

    def _remove_route_locked(self, token: str) -> None:
        route = self._routes.pop(token, None)
        if route is None:
            return
        self._accession_routes.pop(route.accession, None)
        for study_uid in self._route_studies.pop(token, set()):
            if self._study_routes.get(study_uid) == token:
                self._study_routes.pop(study_uid, None)

    def _claim_route_locked(self, token: str) -> StorageRoute | None:
        route = self._routes.get(token)
        if route is None:
            return None
        self._route_inflight[token] = self._route_inflight.get(token, 0) + 1
        return route

    def _release_route(self, token: str) -> None:
        with self._route_condition:
            count = self._route_inflight.get(token, 0)
            if count > 0:
                self._route_inflight[token] = count - 1
            self._route_condition.notify_all()

    def _publish(
        self, source: Path, directory: Path, filename: str
    ) -> tuple[str, Path]:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = directory / f".incoming-{uuid.uuid4().hex}.part"
        try:
            _copy_file(source, temporary)
            destination = directory / filename
            with self._publish_lock:
                if not destination.exists():
                    os.replace(temporary, destination)
                    return "stored", destination
                if _same_content(temporary, destination):
                    temporary.unlink()
                    return "duplicate", destination
                conflict = self._unique_quarantine_path(f"conflict-{filename}")
                os.replace(temporary, conflict)
                return "conflict", conflict
        finally:
            temporary.unlink(missing_ok=True)

    def _quarantine(self, source: Path, sop_uid: str) -> Path:
        safe_uid = "".join(
            character for character in sop_uid if character.isdigit() or character == "."
        ).strip(".")
        filename = (
            f"unassigned-{safe_uid or 'unknown'}-{uuid.uuid4().hex[:8]}.dcm"
        )
        destination = self._unique_quarantine_path(filename)
        temporary = self.quarantine_directory / f".incoming-{uuid.uuid4().hex}.part"
        try:
            _copy_file(source, temporary)
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    def _unique_quarantine_path(self, filename: str) -> Path:
        self.quarantine_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        candidate = self.quarantine_directory / filename
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix or ".dcm"
        return candidate.with_name(f"{stem}-{uuid.uuid4().hex[:8]}{suffix}")

    def _disable_chunked_receive(self) -> None:
        if not self._chunked_receive_enabled:
            return
        self._chunked_receive_enabled = False
        _disable_chunked_receive()

    def _emit(self, source: str, message: str, level: str) -> None:
        try:
            self.log_callback(source, message, level)
        except Exception:
            pass


def _clean_value(value: object) -> str:
    return str(value or "").strip()


def _clean_uid(value: object) -> str:
    text = _clean_value(value)
    if not text:
        return ""
    uid = UID(text)
    return text if uid.is_valid else ""


def _copy_file(source: Path, destination: Path) -> None:
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    try:
        destination.chmod(0o600)
    except OSError:
        pass


def _same_content(first: Path, second: Path) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    return _file_digest(first) == _file_digest(second)


def _file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.digest()


def _enable_chunked_receive() -> None:
    global _chunked_receive_previous, _chunked_receive_users
    with _chunked_receive_lock:
        if _chunked_receive_users == 0:
            _chunked_receive_previous = bool(
                pynetdicom_config.STORE_RECV_CHUNKED_DATASET
            )
            pynetdicom_config.STORE_RECV_CHUNKED_DATASET = True
        _chunked_receive_users += 1


def _disable_chunked_receive() -> None:
    global _chunked_receive_users
    with _chunked_receive_lock:
        if _chunked_receive_users == 0:
            return
        _chunked_receive_users -= 1
        if _chunked_receive_users == 0:
            pynetdicom_config.STORE_RECV_CHUNKED_DATASET = (
                _chunked_receive_previous
            )
