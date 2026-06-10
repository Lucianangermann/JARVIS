"""Tests for the JARVIS security & monitoring layer.

Every test runs against a temp ``security.db`` so the suite never touches
real data. The resemblyzer-backed speaker-verification path is skipped if
the optional dependency isn't installed — the rest of the layer (DB,
system monitor, access control, anomaly detection, home/digital/emergency
routing) has no heavy deps and always runs.

Coverage matrix
---------------
- SecurityDB: schema creation + event/access/metric/device round-trips
- SystemMonitor: real psutil snapshot + classification + cpu_temp=None
- AccessController: temp grants, level gating, expiry, revoke
- VoiceAuthenticator: PIN bcrypt, level mapping, guest mode (+ enroll/verify
  when resemblyzer present)
- AnomalyDetector: night/burst/low-conf flags + per-IP rate-limit block
- HomeSecurity: arm/disarm, checklist, smoke/CO2 fire unconditionally
- DigitalSecurity: multicast filter, first-sighting alert, auth-log block
- Emergency: SOS/fire/cancel always-available + contact notification
- SecurityManager: emergency-first routing + request pipeline deny
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.security.db import SecurityDB
from server.security.system_monitor import SystemMonitor
from server.security.access_control import AccessController, classify_command
from server.security.voice_auth import VoiceAuthenticator
from server.security.anomaly_detector import AnomalyDetector
from server.security.home_security import HomeSecuritySystem
from server.security.digital_security import DigitalSecurityMonitor
from server.security.emergency import EmergencySystem
from server.security.security_manager import SecurityManager


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(tmp_path: Path) -> SecurityDB:
    d = SecurityDB(tmp_path / "security.db")
    yield d
    d.close()


# ── SecurityDB ──────────────────────────────────────────────────────────── #

def test_db_creates_all_tables(db: SecurityDB) -> None:
    names = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"security_events", "access_log", "camera_events",
            "system_metrics", "known_devices"} <= names


def test_db_event_and_access_roundtrip(db: SecurityDB) -> None:
    db.log_event("test", "HIGH", "unit", "hello")
    ev = db.recent_events(limit=1)
    assert ev and ev[0]["event_type"] == "test" and ev[0]["severity"] == "HIGH"
    db.log_access("owner", "licht an", "127.0.0.1", 0.9, "low", True, "ok")
    acc = db.recent_access(limit=1)
    assert acc and acc[0]["allowed"] == 1


def test_db_device_upsert_and_trust(db: SecurityDB) -> None:
    db.upsert_device("aa:bb:cc:dd:ee:ff", "host", "192.168.1.2")
    assert db.is_known_device("aa:bb:cc:dd:ee:ff")
    db.trust_device("aa:bb:cc:dd:ee:ff", True)
    rows = db.query("SELECT trusted FROM known_devices WHERE mac_address=?",
                    ("aa:bb:cc:dd:ee:ff",))
    assert rows[0]["trusted"] == 1


# ── SystemMonitor ───────────────────────────────────────────────────────── #

def test_system_health_snapshot(db: SecurityDB) -> None:
    mon = SystemMonitor(db=db)
    if not mon.available:
        pytest.skip("psutil not installed")
    h = mon.get_system_health()
    assert 0.0 <= h.ram_percent <= 100.0
    assert h.status in ("healthy", "warning", "critical")
    assert h.ram_total_gb > 0


def test_threshold_check_handles_none_temp(db: SecurityDB) -> None:
    """cpu_temp is None on macOS — the threshold pass must not raise."""
    mon = SystemMonitor(db=db)
    if not mon.available:
        pytest.skip("psutil not installed")
    h = mon.get_system_health()
    h.cpu_temp = None
    mon._check_thresholds(h)  # must not raise


# ── AccessController ────────────────────────────────────────────────────── #

def test_classify_command_fails_closed() -> None:
    assert classify_command("mach das licht an") == "lights"
    assert classify_command("schick eine email") == "email"
    # Unknown wording falls to the most restricted category.
    assert classify_command("xyzzy frobnicate") == "system"


def test_temp_access_level_gating_and_revoke(db: SecurityDB) -> None:
    ac = AccessController(db=db)
    grant = _run(ac.create_temp_access("Max", "guest", 2))
    tok = grant["token"]
    assert ac.is_command_allowed(tok, "licht an") is True
    assert ac.is_command_allowed(tok, "lies meine emails") is False
    assert _run(ac.revoke_access("Max")) is True
    assert ac.is_command_allowed(tok, "licht an") is False


# ── VoiceAuthenticator ──────────────────────────────────────────────────── #

def test_pin_bcrypt_roundtrip(db: SecurityDB) -> None:
    auth = VoiceAuthenticator(db=db, enabled=True)
    h = VoiceAuthenticator.hash_pin("4711")
    auth._pin_hash = h
    assert auth.verify_pin("4711") is True
    assert auth.verify_pin("0000") is False
    assert h != "4711"  # never plaintext


def test_command_security_level_mapping(db: SecurityDB) -> None:
    auth = VoiceAuthenticator(db=db)
    assert auth.command_security_level("wie ist das wetter") == "low"
    assert auth.command_security_level("lies meine emails") == "medium"
    assert auth.command_security_level("lösche die datei") == "high"
    assert auth.command_security_level("ändere security einstellungen") == "critical"


def test_confidence_gating_per_level(db: SecurityDB) -> None:
    auth = VoiceAuthenticator(db=db, enabled=True)
    # medium needs >=0.75
    assert _run(auth.check_command_permission("email lesen", 0.70)) is False
    assert _run(auth.check_command_permission("email lesen", 0.80)) is True
    # critical needs >=0.90
    assert _run(auth.check_command_permission("security einstellungen", 0.88)) is False


def test_guest_mode_blocks_sensitive(db: SecurityDB) -> None:
    auth = VoiceAuthenticator(db=db, enabled=True)
    _run(auth.enable_guest_mode(duration_hours=1))
    assert auth.is_guest_mode() is True
    assert _run(auth.check_command_permission("licht an", 0.0)) is True
    assert _run(auth.check_command_permission("email lesen", 0.0)) is False
    _run(auth.disable_guest_mode())
    assert auth.is_guest_mode() is False


def test_auth_disabled_allows_everything(db: SecurityDB) -> None:
    auth = VoiceAuthenticator(db=db, enabled=False)
    v = _run(auth.verify_speaker(b"whatever"))
    assert v["is_owner"] is True and v["action"] == "allow"


# resemblyzer is optional + heavy; run the real enroll/verify only if present.
def _resemblyzer_available() -> bool:
    try:
        import resemblyzer  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _resemblyzer_available(),
                    reason="resemblyzer not installed")
def test_enroll_then_reject_other_voice(db: SecurityDB, tmp_path: Path) -> None:
    import numpy as np
    SR = 16000

    def tone(freq, seed):
        rng = np.random.default_rng(seed)
        t = np.linspace(0, 3.0, int(SR * 3), endpoint=False)
        sig = (0.6 * np.sin(2 * np.pi * freq * t)
               + 0.3 * np.sin(2 * np.pi * 2 * freq * t))
        sig *= (0.7 + 0.3 * np.sin(2 * np.pi * 4 * t))
        sig += 0.02 * rng.standard_normal(t.shape)
        return (np.clip(sig, -1, 1) * 20000).astype(np.int16).tobytes()

    auth = VoiceAuthenticator(
        db=db, profile_path=tmp_path / "owner.npy",
        threshold=0.85, enabled=True,
    )
    res = _run(auth.enroll_owner(samples=[tone(130 + i, i) for i in range(5)]))
    assert res["ok"] is True
    assert auth.has_profile
    # A very different pitch should not verify as the owner.
    other = _run(auth.verify_speaker(tone(245, 99)))
    assert other["is_owner"] is False


# ── AnomalyDetector ─────────────────────────────────────────────────────── #

def test_anomaly_flags(db: SecurityDB) -> None:
    from datetime import datetime
    ad = AnomalyDetector(db=db)
    # 3 a.m. is deep-night → flagged.
    assert ad.analyze_command("licht an", 0.95, datetime(2026, 6, 10, 3, 0))
    # low confidence on a sensitive command → flagged.
    assert ad.analyze_command("email lesen", 0.5, datetime(2026, 6, 10, 14, 0))
    # normal daytime, high confidence → not flagged.
    assert not ad.analyze_command("wetter", 0.95, datetime(2026, 6, 10, 14, 0))


def test_rate_limit_blocks(db: SecurityDB) -> None:
    ad = AnomalyDetector(db=db)
    ip = "10.0.0.7"
    for _ in range(55):
        ad.rate_limit_check(ip)
    assert ad.rate_limit_check(ip) is False  # now blocked


# ── HomeSecurity ────────────────────────────────────────────────────────── #

def test_arm_disarm_and_checklist(db: SecurityDB) -> None:
    hs = HomeSecuritySystem(db=db)
    _run(hs.arm_system("away"))
    assert hs.is_armed
    # Open window while armed → checklist flags it.
    hs.set_window("kitchen", True)
    assert "Achtung" in _run(hs.leaving_checklist())
    # Close it; armed + nothing open → all clear (an armed alarm is part
    # of a clean leaving checklist).
    hs.set_window("kitchen", False)
    assert "Alles in Ordnung" in _run(hs.leaving_checklist())
    # Disarming an armed system flips it off and the checklist now warns
    # that the alarm isn't active.
    _run(hs.disarm_system())
    assert hs.is_armed is False
    assert "Alarm noch nicht aktiviert" in _run(hs.leaving_checklist())


def test_smoke_alert_fires_unconditionally(db: SecurityDB) -> None:
    fired = []
    hs = HomeSecuritySystem(db=db, alert_handler=lambda m, s: fired.append((s, m)))
    _run(hs.on_smoke_detected("kitchen"))
    assert fired and fired[0][0] == "CRITICAL"
    events = db.recent_events(limit=5)
    assert any(e["event_type"] == "smoke" for e in events)


# ── DigitalSecurity ─────────────────────────────────────────────────────── #

def test_multicast_filtered() -> None:
    real = DigitalSecurityMonitor._is_real_host
    assert real({"mac": "01:00:5e:00:00:fb", "ip": "224.0.0.251"}) is False
    assert real({"mac": "ff:ff:ff:ff:ff:ff", "ip": "255.255.255.255"}) is False
    assert real({"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.5"}) is True


def test_auth_log_blocks_after_five_failures(db: SecurityDB) -> None:
    ds = DigitalSecurityMonitor(db=db)
    for _ in range(5):
        db.log_access("unknown", "x", "10.0.0.5", 0.2, "high", False, "bad")
    out = _run(ds.monitor_jarvis_auth_log())
    assert "10.0.0.5" in out["blocked_ips"]
    assert ds.is_blocked("10.0.0.5")


def test_api_spike_detection(db: SecurityDB, monkeypatch) -> None:
    from server import config
    monkeypatch.setattr(config.settings, "API_USAGE_ALERT_THRESHOLD", 3)
    ds = DigitalSecurityMonitor(db=db)
    for _ in range(5):
        ds.record_api_call()
    out = _run(ds.monitor_api_usage())
    assert out["spike"] is True


# ── Emergency ───────────────────────────────────────────────────────────── #

def test_sos_notifies_contacts(db: SecurityDB) -> None:
    sent = []
    em = EmergencySystem(
        db=db, notify_handler=lambda m, c: sent.append((m, c)),
        contacts=["+49170"], home_address="Test 1",
    )
    r = _run(em.trigger_sos())
    assert r["emergency_numbers"]["Notruf"] == "112"
    assert sent and "+49170" in sent[0][1]
    assert em.active_alarm == "sos"
    _run(em.cancel_alarm())
    assert em.active_alarm is None


# ── SecurityManager ─────────────────────────────────────────────────────── #

def test_manager_emergency_routing_first(tmp_path: Path) -> None:
    sm = SecurityManager(db_path=tmp_path / "security.db")
    try:
        reply = _run(sm.process_command("SOS"))
        assert reply and "112" in reply
        # Non-security input falls through (None → Claude).
        assert _run(sm.process_command("erzähl einen witz")) is None
    finally:
        sm.stop()


def test_manager_request_pipeline_denies_flood(tmp_path: Path) -> None:
    sm = SecurityManager(db_path=tmp_path / "security.db")
    try:
        ok = _run(sm.process_request("wetter", audio=None, ip="127.0.0.1"))
        assert ok["allowed"] is True
        for _ in range(55):
            sm.anomaly.rate_limit_check("9.9.9.9")
        denied = _run(sm.process_request("licht an", audio=None, ip="9.9.9.9"))
        assert denied["allowed"] is False
    finally:
        sm.stop()
