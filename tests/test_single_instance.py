from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from dcmget.single_instance import SingleInstance, SingleInstanceError


def test_second_instance_notifies_primary(tmp_path):
    path = tmp_path / "gui-instance.json"
    received: list[dict[str, object]] = []
    notified = threading.Event()
    primary = SingleInstance(
        path,
        activation_handler=lambda payload: (received.append(payload), notified.set()),
    )
    secondary = SingleInstance(path)
    try:
        assert primary.start()
        assert not secondary.start({"action": "activate", "source": "second"})
        assert notified.wait(2)
        assert received == [{"action": "activate", "source": "second"}]
        assert primary.is_primary
        assert not secondary.is_primary
    finally:
        secondary.close()
        primary.close()


def test_notification_waits_until_window_handler_is_registered(tmp_path):
    path = tmp_path / "gui-instance.json"
    primary = SingleInstance(path)
    secondary = SingleInstance(path)
    received: list[dict[str, object]] = []
    try:
        assert primary.start()
        assert not secondary.start({"action": "activate"})
        primary.set_activation_handler(received.append)
        assert received == [{"action": "activate"}]
    finally:
        secondary.close()
        primary.close()


def test_notify_existing_never_claims_an_absent_primary(tmp_path):
    path = tmp_path / "gui-instance.json"
    notifier = SingleInstance(path, startup_timeout=0.1, connect_timeout=0.05)

    assert not notifier.notify_existing({"action": "activate", "profile": 3})
    assert not notifier.is_primary
    assert not path.exists()
    assert not Path(str(path) + ".lock").exists()


def test_notify_existing_wakes_matching_profile(tmp_path):
    path = tmp_path / "gui-instance.json"
    received = []
    notified = threading.Event()
    primary = SingleInstance(
        path,
        activation_handler=lambda payload: (received.append(payload), notified.set()),
    )
    notifier = SingleInstance(path)
    try:
        assert primary.start()
        assert notifier.notify_existing({"action": "activate", "profile": 5})
        assert notified.wait(2)
        assert received == [{"action": "activate", "profile": 5}]
        assert not notifier.is_primary
    finally:
        notifier.close()
        primary.close()


def test_stale_endpoint_is_replaced_when_process_lock_is_free(tmp_path):
    path = tmp_path / "gui-instance.json"
    path.write_text(
        json.dumps({"version": 1, "pid": 999999, "port": 1, "token": "x" * 43}),
        encoding="utf-8",
    )
    instance = SingleInstance(path)
    try:
        assert instance.start()
        metadata = json.loads(path.read_text(encoding="utf-8"))
        assert metadata["pid"] != 999999
        assert metadata["port"] != 1
    finally:
        instance.close()
    assert not path.exists()


def test_locked_but_unreachable_endpoint_does_not_start_second_primary(tmp_path):
    path = tmp_path / "gui-instance.json"
    primary = SingleInstance(path)
    try:
        assert primary.start()
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["port"] = _unused_port()
        path.write_text(json.dumps(metadata), encoding="utf-8")
        secondary = SingleInstance(path, startup_timeout=0.1, connect_timeout=0.05)
        with pytest.raises(SingleInstanceError, match="无法通知"):
            secondary.start()
        assert not secondary.is_primary
        secondary.close()
    finally:
        primary.close()


def test_close_releases_process_lock_for_next_instance(tmp_path):
    path = tmp_path / "gui-instance.json"
    first = SingleInstance(path)
    assert first.start()
    first.close()

    replacement = SingleInstance(path)
    try:
        assert replacement.start()
    finally:
        replacement.close()


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
