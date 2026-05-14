"""Centralised settings, loaded from .env via python-dotenv.

Everything else in the server reads from `settings` so we can keep secrets
out of the codebase and let tests monkey-patch a single object.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (parent of this file's parent).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _required(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val or val.startswith("replace") or val.endswith("REPLACE-ME"):
        raise RuntimeError(
            f"Missing required env var {key!r}. Copy .env.example to .env and fill it in."
        )
    return val


def _csv(key: str, default: str) -> list[str]:
    raw = os.getenv(key, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings:
    # --- Secrets ---
    ANTHROPIC_API_KEY: str = _required("ANTHROPIC_API_KEY")
    JARVIS_AUTH_TOKEN: str = _required("JARVIS_AUTH_TOKEN")

    # --- Network ---
    HOST: str = os.getenv("HOST", "127.0.0.1")
    PORT: int = int(os.getenv("PORT", "8000"))
    ALLOWED_ORIGINS: list[str] = _csv(
        "ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
    )

    # --- Model ---
    MODEL: str = os.getenv("MODEL", "claude-haiku-4-5-20251001")
    SYSTEM_PROMPT: str = os.getenv(
        "SYSTEM_PROMPT",
        "You are JARVIS, a concise and capable voice assistant. "
        "Keep spoken replies under 3 sentences unless the user asks for detail. "
        "Refuse anything outside the whitelisted command set.",
    )

    # --- Voice ---
    WAKE_WORD: str = os.getenv("WAKE_WORD", "jarvis").lower()
    WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "base")
    # Force Whisper to a specific language ("de", "en", …). Empty string
    # lets Whisper auto-detect, which can flip to English on short
    # German utterances ("stopp" → "up"). Default to German.
    WHISPER_LANGUAGE: str = os.getenv("WHISPER_LANGUAGE", "de")
    # Substring or full id of the TTS voice. Empty = autoselect based on
    # TTS_LANGUAGE. Example values: "Anna", "Markus", "Samantha", or a
    # full id like "com.apple.voice.compact.de-DE.Anna".
    TTS_VOICE: str = os.getenv("TTS_VOICE", "")
    # Preferred BCP-47 locale prefix when autoselecting (e.g. "de", "en").
    TTS_LANGUAGE: str = os.getenv("TTS_LANGUAGE", "de")
    # Speaking rate in words/min. macOS default ≈ 200; we slow down a bit
    # for clarity.
    TTS_RATE: int = int(os.getenv("TTS_RATE", "180"))
    # After the wake word fires, stay active for this many seconds — every
    # speech segment is treated as a command, no wake word needed — until
    # the user says one of the end phrases ("okay das war's", "tschüss",
    # …) or the timeout lapses. 0 disables follow-up mode entirely.
    FOLLOWUP_TIMEOUT_S: float = float(os.getenv("FOLLOWUP_TIMEOUT_S", "60"))

    # --- Spotify Web API (search only) ---
    # Create a developer app at https://developer.spotify.com/dashboard
    # and paste Client ID / Client Secret here. Required for play_track /
    # play_playlist; the basic play/pause/next/previous commands work
    # without it (they just steer whatever Spotify is currently doing).
    SPOTIFY_CLIENT_ID: str = os.getenv("SPOTIFY_CLIENT_ID", "")
    SPOTIFY_CLIENT_SECRET: str = os.getenv("SPOTIFY_CLIENT_SECRET", "")
    SPOTIFY_MARKET: str = os.getenv("SPOTIFY_MARKET", "DE")

    # --- Safety limits ---
    MAX_INPUT_LENGTH: int = 500
    RATE_LIMIT_PER_MINUTE: int = 10
    MAX_HISTORY_TURNS: int = 20  # user+assistant pairs kept per session

    # --- Paths ---
    LOG_DIR: Path = PROJECT_ROOT / "logs"
    REJECTED_LOG: Path = LOG_DIR / "rejected.log"


settings = Settings()
settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
