"""Tests for at-rest field encryption (server/common/crypto.py)."""
from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from cryptography.fernet import Fernet  # noqa: E402

from server.common import crypto  # noqa: E402


def _cipher_with_key(monkeypatch, key: str) -> crypto.FieldCipher:
    monkeypatch.setattr(crypto.settings, "JARVIS_DB_KEY", key)
    return crypto.FieldCipher()


def test_roundtrip_when_keyed(monkeypatch) -> None:
    c = _cipher_with_key(monkeypatch, Fernet.generate_key().decode())
    assert c.active
    enc = c.encrypt("Schatz, ich liebe dich")
    assert enc.startswith("enc:")
    assert "liebe" not in enc                 # actually encrypted
    assert c.decrypt(enc) == "Schatz, ich liebe dich"


def test_legacy_plaintext_passthrough(monkeypatch) -> None:
    c = _cipher_with_key(monkeypatch, Fernet.generate_key().decode())
    # A value without the enc: prefix is legacy plaintext — returned as-is.
    assert c.decrypt("alte klartext nachricht") == "alte klartext nachricht"


def test_off_without_key(monkeypatch) -> None:
    c = _cipher_with_key(monkeypatch, "")
    assert c.active is False
    assert c.encrypt("hallo") == "hallo"      # passthrough
    assert c.decrypt("hallo") == "hallo"


def test_none_and_empty_safe(monkeypatch) -> None:
    c = _cipher_with_key(monkeypatch, Fernet.generate_key().decode())
    assert c.encrypt(None) is None
    assert c.encrypt("") == ""
    assert c.decrypt(None) is None


def test_wrong_key_cannot_decrypt(monkeypatch) -> None:
    c1 = _cipher_with_key(monkeypatch, Fernet.generate_key().decode())
    enc = c1.encrypt("geheim")
    c2 = _cipher_with_key(monkeypatch, Fernet.generate_key().decode())
    # Different key → decrypt fails gracefully, returns the marker unchanged.
    assert c2.decrypt(enc) == enc
