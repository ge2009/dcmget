from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from .profile_manager import ProfileManager, WINDOWS_MANAGEMENT_PORT
from .profile_web_operations import ProfileWebOperations
from .runtime import resource_root
from .windows_service_control import windows_service_operation_handlers
from .web_server import DcmGetWebServer


LOGGER = logging.getLogger(__name__)
WINDOWS_MANAGEMENT_HOST = "0.0.0.0"


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
    log_level: str = "info",
) -> DcmGetWebServer:
    """Build the fixed management hub without claiming or validating a Profile."""

    manager = profile_manager or ProfileManager()
    root = Path(project_root or resource_root()).expanduser().resolve()
    management_state = Path(
        state_directory or manager.state_root / "management"
    ).expanduser().resolve()
    profile_operations = ProfileWebOperations(manager=manager, project_root=root)
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
        management_mode=True,
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
