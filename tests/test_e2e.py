"""End-to-end pipeline tests against the real app via TestClient.

Covers the request paths that DON'T need a live Claude call: auth, /health,
the security/communication short-circuits in /chat (which answer
deterministically without hitting the model), guest delegated access, and
the per-IP rate gate. Boots the full lifespan (memory degrades gracefully
without chromadb, so this runs in light CI too).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.config import settings
from server.main import app

OWNER = {"Authorization": f"Bearer {settings.JARVIS_AUTH_TOKEN}"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_root_health(client) -> None:
    r = client.get("/")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_health_requires_auth(client) -> None:
    assert client.get("/health").status_code == 401
    r = client.get("/health", headers=OWNER)
    assert r.status_code == 200
    assert r.json()["status"] in ("healthy", "degraded")
    assert "subsystems" in r.json()


def test_chat_security_short_circuit_no_claude(client) -> None:
    # "system status" is handled by the security short-circuit (psutil) — a
    # deterministic answer with no model call.
    r = client.post("/chat", headers=OWNER, json={"text": "system status"})
    assert r.status_code == 200
    reply = r.json()["reply"].lower()
    assert "cpu" in reply or "ram" in reply or "prozent" in reply


def test_chat_invalid_token_401(client) -> None:
    r = client.post("/chat", headers={"Authorization": "Bearer nope"},
                    json={"text": "system status"})
    assert r.status_code == 401


def test_guest_delegated_access(client) -> None:
    # Owner mints a guest token; guest is restricted to its allowed commands.
    g = client.post("/security/access/temp", headers=OWNER,
                    json={"name": "Gast", "level": "guest", "duration_hours": 1})
    if g.status_code != 200:
        pytest.skip("security layer unavailable")
    guest = {"Authorization": f"Bearer {g.json()['token']}"}
    # An allowed command (lights) reaches the brain; a disallowed one (email)
    # is refused before any processing.
    refused = client.post("/chat", headers=guest,
                          json={"text": "lies meine emails vor"})
    assert "nicht erlaubt" in refused.json()["reply"].lower()


def test_per_ip_rate_gate(client) -> None:
    sec = getattr(app.state, "security", None)
    if sec is None or sec.anomaly is None:
        pytest.skip("security layer unavailable")
    for _ in range(55):
        sec.anomaly.rate_limit_check("testclient")
    r = client.post("/chat", headers=OWNER, json={"text": "system status"})
    assert r.status_code in (403, 429)
    # Reset so other tests aren't gated.
    sec.anomaly._ip_hits.pop("testclient", None)
    sec.anomaly._blocked_ips.pop("testclient", None)
