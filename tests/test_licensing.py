from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import dcmget.licensing as licensing
from dcmget.licensing import LicenseError


@pytest.fixture
def signing_key(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    private_path = tmp_path / "private.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_path, public_pem


def test_daily_password_uses_local_calendar_date():
    today = date(2026, 7, 14)

    assert licensing.daily_password(today) == "20260714"
    assert licensing.validate_daily_password("20260714", today)
    assert not licensing.validate_daily_password("20260713", today)


def test_issue_validate_save_and_load_license(signing_key, tmp_path):
    private_path, public_pem = signing_key
    target = "ABCDEF-123456-7890AB-CDEF12"
    token = licensing.issue_license(
        private_path,
        target,
        "测试医院",
        issued_on=date(2026, 7, 14),
    )

    info = licensing.validate_license(
        token,
        expected_machine_code=target,
        today=date(2026, 7, 14),
        public_key_pem=public_pem,
    )
    output = tmp_path / "license.lic"
    licensing.save_license(
        token,
        output,
        expected_machine_code=target,
        public_key_pem=public_pem,
    )
    loaded = licensing.load_license(
        output,
        expected_machine_code=target,
        today=date(2026, 7, 14),
        public_key_pem=public_pem,
    )

    assert info.customer == "测试医院"
    assert info.expires_on is None
    assert loaded == info
    assert output.read_text(encoding="utf-8").strip() == token


def test_license_rejects_other_machine_tampering_and_expiry(signing_key):
    private_path, public_pem = signing_key
    target = "ABCDEF-123456-7890AB-CDEF12"
    token = licensing.issue_license(
        private_path,
        target,
        "测试医院",
        expires_on=date(2026, 7, 14),
    )

    with pytest.raises(LicenseError, match="其他电脑"):
        licensing.validate_license(
            token,
            expected_machine_code="000000-000000-000000-000000",
            public_key_pem=public_pem,
        )
    prefix, payload, signature = token.split(".")
    tampered_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(LicenseError, match="签名无效"):
        licensing.validate_license(
            f"{prefix}.{payload}.{tampered_signature}",
            expected_machine_code=target,
            public_key_pem=public_pem,
        )
    with pytest.raises(LicenseError, match="到期"):
        licensing.validate_license(
            token,
            expected_machine_code=target,
            today=date(2026, 7, 15),
            public_key_pem=public_pem,
        )


def test_machine_code_is_stable_and_formatted(monkeypatch):
    monkeypatch.setattr(licensing, "_machine_identifier", lambda: "fixed-machine")

    first = licensing.machine_code()

    assert first == licensing.machine_code()
    assert len(first) == 27
    assert [len(part) for part in first.split("-")] == [6, 6, 6, 6]


def test_normalize_machine_code_accepts_separators_and_rejects_bad_input():
    assert (
        licensing.normalize_machine_code("abcdef 123456 7890ab cdef12")
        == "ABCDEF-123456-7890AB-CDEF12"
    )
    with pytest.raises(LicenseError, match="24"):
        licensing.normalize_machine_code("too-short")


def test_trial_allows_exactly_thirty_download_tasks(tmp_path):
    path = tmp_path / "trial.json"
    target = "ABCDEF-123456-7890AB-CDEF12"

    assert licensing.trial_status(path, target).remaining == 30
    for expected_remaining in range(29, -1, -1):
        assert licensing.consume_trial(path, target).remaining == expected_remaining

    assert licensing.trial_status(path, target).used == 30
    with pytest.raises(LicenseError, match="试用已用完"):
        licensing.consume_trial(path, target)


def test_trial_state_is_machine_bound_and_tamper_evident(tmp_path):
    path = tmp_path / "trial.json"
    target = "ABCDEF-123456-7890AB-CDEF12"
    licensing.consume_trial(path, target)

    other = licensing.trial_status(path, "000000-000000-000000-000000")
    assert other.remaining == 0

    path.write_text(path.read_text(encoding="utf-8").replace('"used":1', '"used":0'))
    tampered = licensing.trial_status(path, target)
    assert tampered.remaining == 0


def test_trial_anchor_prevents_reset_when_primary_state_is_deleted(
    tmp_path, monkeypatch
):
    primary = tmp_path / "config" / "trial.json"
    anchor = tmp_path / "state" / ".trial-anchor"
    target = "ABCDEF-123456-7890AB-CDEF12"
    monkeypatch.setattr(licensing, "default_trial_path", lambda: primary)
    monkeypatch.setattr(licensing, "default_trial_anchor_path", lambda: anchor)
    monkeypatch.setattr(licensing, "machine_code", lambda: target)

    licensing.consume_trial()
    primary.unlink()

    assert licensing.trial_status().used == 1
    assert licensing.trial_status().remaining == 29


def test_concurrent_trial_consumption_does_not_lose_updates(tmp_path):
    path = tmp_path / "trial.json"
    target = "ABCDEF-123456-7890AB-CDEF12"

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(
            pool.map(
                lambda _index: licensing.consume_trial(path, target),
                range(30),
            )
        )

    assert sorted(result.remaining for result in results) == list(range(30))
    assert licensing.trial_status(path, target).used == 30


def test_trial_task_id_is_idempotent_even_for_the_thirtieth_use(tmp_path):
    path = tmp_path / "trial.json"
    target = "ABCDEF-123456-7890AB-CDEF12"
    for _index in range(29):
        licensing.consume_trial(path, target)
    task_id = "a" * 32

    first = licensing.consume_trial(path, target, task_id=task_id)
    resumed = licensing.consume_trial(path, target, task_id=task_id)

    assert first.remaining == 0
    assert resumed == first
    assert licensing.trial_task_consumed(task_id, path, target)
    with pytest.raises(LicenseError, match="试用已用完"):
        licensing.consume_trial(path, target, task_id="b" * 32)


def test_version_one_trial_state_is_read_and_migrated_on_next_use(tmp_path):
    path = tmp_path / "trial.json"
    target = "ABCDEF-123456-7890AB-CDEF12"
    path.write_text(
        licensing.json.dumps(
            {
                "checksum": licensing._trial_checksum(target, 3, version=1),
                "machine": target,
                "used": 3,
                "version": 1,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    assert licensing.trial_status(path, target).used == 3
    licensing.consume_trial(path, target, task_id="c" * 32)

    saved = licensing.json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == licensing.TRIAL_STATE_VERSION
    assert saved["task_ids"] == ["c" * 32]


def test_trial_task_ledger_survives_other_tasks_and_cli_style_consumption(tmp_path):
    path = tmp_path / "trial.json"
    target = "ABCDEF-123456-7890AB-CDEF12"
    task_a = "a" * 32
    task_b = "b" * 32

    licensing.consume_trial(path, target, task_id=task_a)
    licensing.consume_trial(path, target, task_id=task_b)
    licensing.consume_trial(path, target)
    resumed = licensing.consume_trial(path, target, task_id=task_a)

    assert resumed.used == 3
    assert licensing.trial_task_consumed(task_a, path, target)
    assert licensing.trial_task_consumed(task_b, path, target)


def test_windows_installer_uses_machine_shared_trial_anchor(tmp_path, monkeypatch):
    program_data = tmp_path / "ProgramData"
    shared = program_data / "DcmGet"
    shared.mkdir(parents=True)
    monkeypatch.setattr(licensing.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", str(program_data))

    assert licensing.default_trial_anchor_path() == shared / ".trial-anchor"
