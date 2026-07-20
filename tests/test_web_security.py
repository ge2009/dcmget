from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from dcmget.web_security import (
    HostPolicy,
    LoginRateLimiter,
    PasswordStore,
    SafeDirectoryBrowser,
    SessionStore,
    UnsafePathError,
    WebSecurityError,
    bootstrap_web_security,
    discover_local_hosts,
)


def _directory_symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建测试符号链接：{exc}")


def test_password_store_bootstraps_hash_then_accepts_local_setup(tmp_path: Path):
    store = PasswordStore(tmp_path / "profile")

    generated = store.load_or_create()

    assert generated
    assert len(generated) >= 32
    assert store.verify(generated)
    assert not store.setup_complete()
    contents = store.path.read_text(encoding="utf-8")
    assert generated not in contents
    assert json.loads(contents)["algorithm"] == "scrypt"
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o077 == 0

    store.replace("a-secure-admin-password")
    assert store.setup_complete()
    assert store.verify("a-secure-admin-password")
    assert not store.verify(generated)
    assert PasswordStore(tmp_path / "profile").load_or_create() is None


def test_password_store_rejects_symlink_and_modified_kdf_parameters(tmp_path: Path):
    store = PasswordStore(tmp_path / "profile")
    store.load_or_create()
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["n"] = 1 << 24
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(WebSecurityError, match="参数不受支持"):
        store.verify("anything")

    store.path.unlink()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    try:
        store.path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建测试符号链接：{exc}")
    with pytest.raises(WebSecurityError, match="认证文件无效"):
        store.load_or_create()


def test_passwordless_security_bootstrap_ignores_legacy_auth_file(tmp_path: Path):
    state = tmp_path / "profile"
    state.mkdir()
    legacy = state / "web-auth.json"
    legacy.write_text("not valid json", encoding="utf-8")

    security = bootstrap_web_security(state)

    assert security.bootstrap_password is None
    assert security.status()["authenticated"] is True
    assert security.status()["passwordless"] is True
    assert legacy.read_text(encoding="utf-8") == "not valid json"


def test_session_store_expires_and_revokes(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr("dcmget.web_security.time.time", lambda: now[0])
    sessions = SessionStore(ttl_seconds=60)
    session = sessions.create("127.0.0.1")

    assert session.local
    assert sessions.get(session.token, touch=False) == session
    now[0] += 61
    assert sessions.get(session.token, touch=False) is None

    remote = sessions.create("192.168.1.20")
    assert not remote.local
    sessions.revoke_all()
    assert sessions.get(remote.token) is None


def test_login_rate_limiter_locks_and_resets():
    now = [0.0]
    limiter = LoginRateLimiter(
        max_failures=3,
        window_seconds=60,
        lock_seconds=120,
        clock=lambda: now[0],
    )

    assert limiter.failure("client") == 0
    assert limiter.failure("client") == 0
    assert limiter.failure("client") == 120
    assert limiter.retry_after("client") == 120
    now[0] += 121
    assert limiter.retry_after("client") == 0
    limiter.failure("client")
    limiter.success("client")
    assert limiter.retry_after("client") == 0


def test_host_policy_is_exact_and_rejects_dns_rebinding():
    policy = HostPolicy(8787, ["dcmget.local", "192.168.1.8"])

    assert policy.allows_host_header("dcmget.local:8787")
    assert policy.allows_host_header("192.168.1.8:8787")
    assert policy.allows_origin("http://dcmget.local:8787")
    assert not policy.allows_host_header("dcmget.local.evil:8787")
    assert not policy.allows_host_header("dcmget.local:9999")
    assert not policy.allows_origin("https://dcmget.local:8787")
    assert not policy.allows_origin("null")
    with pytest.raises(ValueError, match="通配符"):
        HostPolicy(8787, ["*"])


def test_local_host_discovery_never_waits_for_dns(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "DCMGET-01")
    monkeypatch.setattr(
        socket,
        "getfqdn",
        lambda: pytest.fail("本机 Host 白名单不应执行 FQDN 查询"),
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: pytest.fail("本机 Host 白名单不应执行 DNS 查询"),
    )

    hosts = discover_local_hosts()

    assert {"localhost", "127.0.0.1", "::1", "dcmget-01"} <= hosts


def test_safe_directory_browser_blocks_escape_and_symlinks(tmp_path: Path):
    root = tmp_path / "allowed"
    child = root / "child"
    child.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _directory_symlink_or_skip(root / "linked", outside)
    browser = SafeDirectoryBrowser({"home": root})

    result = browser.list("home")

    assert result["entries"] == [{"name": "child", "path": "child"}]
    assert browser.authorize_directory(child) == child.resolve()
    with pytest.raises(UnsafePathError):
        browser.list("home", "../outside")
    with pytest.raises(UnsafePathError, match="符号链接"):
        browser.list("home", "linked")
    with pytest.raises(UnsafePathError, match="允许范围"):
        browser.authorize_directory(outside)


def test_absolute_directory_listing_never_exposes_symlink(tmp_path: Path):
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    _directory_symlink_or_skip(root / "jump", external)
    browser = SafeDirectoryBrowser({"root": root})

    result = browser.list_absolute(root)

    assert result["path"] == str(root.resolve())
    assert result["directories"] == [
        {"name": "a", "path": str((root / "a").resolve())}
    ]
