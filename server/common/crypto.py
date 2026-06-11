"""App-level field encryption for sensitive at-rest content.

The data DBs are plaintext SQLite, so anyone who reads the file (a stray
backup, Time Machine, another app) can read your messages / finances. For
a security-focused assistant that's a real gap. SQLCipher would encrypt
the whole file transparently but needs a native dependency; instead we
encrypt the *free-text content* columns at the application boundary with
Fernet (AES-128-CBC + HMAC, from the already-present ``cryptography``).

Scope + caveats:
  * We encrypt content bodies (message text, expense merchant/notes), NOT
    columns used in WHERE clauses (contact, category, timestamps) — Fernet
    is non-deterministic so those couldn't be queried. Full-file
    confidentiality would need SQLCipher.
  * Opt-in: set ``JARVIS_DB_KEY`` (a Fernet key — generate one with
    ``python -m server.common.crypto``). Unset ⇒ passthrough (no
    encryption), so existing setups are unaffected and there's no
    token-rotation footgun.
  * Graceful migration: encrypted values carry an ``enc:`` prefix, so a DB
    with mixed legacy-plaintext + new-encrypted rows reads fine — decrypt
    leaves non-prefixed values untouched.
"""
from __future__ import annotations

from typing import Any

from ..config import settings

try:
    from cryptography.fernet import Fernet
    _CRYPTO_OK = True
except Exception:  # noqa: BLE001
    Fernet = None  # type: ignore[assignment,misc]
    _CRYPTO_OK = False

_PREFIX = "enc:"


class FieldCipher:
    """Encrypt/decrypt individual text fields. No-op passthrough when no
    key is configured or ``cryptography`` is missing."""

    def __init__(self) -> None:
        self._fernet: Any = None
        key = getattr(settings, "JARVIS_DB_KEY", "")
        if _CRYPTO_OK and key:
            try:
                self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
            except Exception as exc:  # noqa: BLE001
                print(f"[crypto] invalid JARVIS_DB_KEY, encryption off: {exc}")
                self._fernet = None

    @property
    def active(self) -> bool:
        return self._fernet is not None

    def encrypt(self, text: str | None) -> str | None:
        if not text or self._fernet is None:
            return text
        try:
            return _PREFIX + self._fernet.encrypt(text.encode("utf-8")).decode("ascii")
        except Exception:  # noqa: BLE001
            return text

    def decrypt(self, text: str | None) -> str | None:
        if not isinstance(text, str) or not text.startswith(_PREFIX):
            return text  # legacy plaintext or non-string — leave as-is
        if self._fernet is None:
            return text  # key gone — can't decrypt; return the marker
        try:
            return self._fernet.decrypt(text[len(_PREFIX):].encode("ascii")).decode("utf-8")
        except Exception:  # noqa: BLE001
            return text


# Process-wide singleton.
cipher = FieldCipher()


if __name__ == "__main__":  # generate a key for the .env
    if not _CRYPTO_OK:
        print("cryptography not installed.")
    else:
        print("Add this to your .env:\n")
        print(f"JARVIS_DB_KEY={Fernet.generate_key().decode()}")
