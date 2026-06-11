"""Speaker verification + PIN fallback so only the owner drives JARVIS.

Speaker embeddings come from resemblyzer (a small pretrained GE2E
encoder, ~17 MB, runs locally on CPU — no audio ever leaves the machine).
Enrollment averages several utterances into one 256-d owner embedding
saved to ``data/voice_profiles/owner.npy``. Verification embeds the
incoming utterance and takes the cosine similarity; resemblyzer
embeddings are already L2-normalised, so a dot product *is* the cosine.

Design choices that matter:
  * **Lazy, guarded model load.** The encoder (and torch) load on first
    use, not at import, so the security package stays importable on a box
    without resemblyzer installed.
  * **Fail-open for the owner, fail-closed for strangers — but only when
    auth is OFF or degraded.** With ``VOICE_AUTH_ENABLED=0`` (default
    until you enrol) nothing is enforced. With it ON but the encoder or
    profile missing, we log the degradation and allow, because bricking
    your own assistant is a worse outcome than the single-user risk here
    (this matches the project's documented single-user trust model). With
    it ON and everything present, real verification gates every command.
  * **PIN is bcrypt-hashed, never plaintext**, read from ``JARVIS_PIN``.

Audio in is whatever the voice stack produces: 16 kHz int16 PCM, either
raw or wrapped in a WAV header. ``_to_waveform`` normalises both to the
float waveform resemblyzer wants.
"""
from __future__ import annotations

import io
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

from ..config import settings

# bcrypt is a hard dep of the project (already installed); guard anyway so
# a broken install degrades to "no PIN" rather than taking the layer down.
try:
    import bcrypt  # type: ignore[import-not-found]
    _BCRYPT_OK = True
except Exception as _bcrypt_exc:  # noqa: BLE001
    print(f"[VoiceAuth] bcrypt unavailable: {_bcrypt_exc}")
    bcrypt = None  # type: ignore[assignment]
    _BCRYPT_OK = False

_SAMPLE_RATE = 16_000

# Command → security level. The map is keyword-based; a command resolves
# to the HIGHEST level whose keywords it matches (fail-safe: ambiguous
# wording lands on the stricter tier).
SECURITY_LEVELS: dict[str, list[str]] = {
    "low":      ["weather", "time", "music", "lights_basic"],
    "medium":   ["email", "calendar", "tasks", "smart_home"],
    "high":     ["files", "system_commands", "send_messages"],
    "critical": ["tier4_mac", "memory_wipe", "security_settings"],
}

# Minimum speaker confidence required per level (spec §1).
_LEVEL_MIN_CONFIDENCE: dict[str, float] = {
    "low": 0.65,
    "medium": 0.75,
    "high": 0.85,
    "critical": 0.90,
}

# Keyword → level, checked strict→loose so the strictest match wins.
_LEVEL_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("critical", ("tier 4", "tier4", "sudo", "passwort ändern", "security",
                  "sicherheitseinstellung", "memory wipe", "speicher löschen",
                  "alles löschen", "notaus")),
    ("high", ("datei", "file", "ordner", "terminal", "befehl ausführen",
              "schick", "send", "nachricht senden", "whatsapp", "überweis",
              "zahlung", "payment")),
    ("medium", ("email", "e-mail", "mail", "kalender", "calendar", "termin",
                "task", "aufgabe", "smart home", "smarthome", "heizung",
                "thermostat")),
    ("low", ("wetter", "weather", "uhrzeit", "zeit", "time", "musik", "music",
             "licht", "lights", "lampe")),
]


class VoiceAuthenticator:
    """resemblyzer speaker verification with a bcrypt-PIN fallback."""

    def __init__(
        self,
        db: Any = None,
        profile_path: Path | str = "data/voice_profiles/owner.npy",
        threshold: float | None = None,
        enabled: bool | None = None,
        pin_hash: str | None = None,
    ) -> None:
        self._db = db
        self._profile_path = Path(profile_path)
        self._threshold = (
            threshold if threshold is not None else settings.VOICE_AUTH_THRESHOLD
        )
        self._enabled = (
            enabled if enabled is not None else settings.VOICE_AUTH_ENABLED
        )
        self._pin_hash = pin_hash if pin_hash is not None else settings.JARVIS_PIN
        self._encoder: Any = None  # lazy
        self._owner_embed: np.ndarray | None = None

        # Guest-mode state: when active, only "low" commands pass without
        # voice verification, and it auto-expires.
        self._guest_until: float = 0.0
        self._guest_allowed: list[str] | None = None

        # PIN-elevation: a correct PIN challenge opens a short window in which
        # commands are owner-authorised regardless of voice confidence. This
        # is the runtime rescue for a borderline voice match (cold, noise).
        self._pin_elevated_until: float = 0.0

        self._profile_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_profile()

    # ── model + profile loading ────────────────────────────────────────── #

    @property
    def has_profile(self) -> bool:
        return self._owner_embed is not None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def pin_configured(self) -> bool:
        """True when a JARVIS_PIN hash is set, so the PIN-challenge rescue
        path is actually available (no point offering it otherwise)."""
        return bool(self._pin_hash)

    def grant_pin_elevation(self, seconds: float = 60.0) -> None:
        """Open the post-PIN authorisation window. Called after a correct
        PIN challenge so the owner can re-issue the borderline command."""
        self._pin_elevated_until = time.time() + seconds

    def is_pin_elevated(self) -> bool:
        return time.time() < self._pin_elevated_until

    def _load_profile(self) -> None:
        if self._profile_path.is_file():
            try:
                self._owner_embed = np.load(self._profile_path)
                print(f"[VoiceAuth] owner profile loaded "
                      f"({self._owner_embed.shape})")
            except Exception as exc:  # noqa: BLE001
                print(f"[VoiceAuth] profile load failed: {exc}")
                self._owner_embed = None

    def _get_encoder(self) -> Any:
        """Lazily construct the resemblyzer encoder. Returns None if the
        dependency is unavailable (caller decides the fallback)."""
        if self._encoder is not None:
            return self._encoder
        try:
            from resemblyzer import VoiceEncoder  # type: ignore[import-not-found]
            self._encoder = VoiceEncoder(verbose=False)
            print("[VoiceAuth] resemblyzer encoder loaded")
        except Exception as exc:  # noqa: BLE001
            print(f"[VoiceAuth] encoder unavailable: {exc}")
            self._encoder = None
        return self._encoder

    # ── audio handling ─────────────────────────────────────────────────── #

    @staticmethod
    def _to_waveform(audio: bytes) -> np.ndarray | None:
        """Normalise WAV-or-raw-PCM int16 @16 kHz bytes to a float32
        waveform in [-1, 1] (resemblyzer's expected input)."""
        if not audio:
            return None
        try:
            raw = audio
            # WAV? Parse the PCM payload out of the RIFF container.
            if audio[:4] == b"RIFF":
                with wave.open(io.BytesIO(audio), "rb") as wf:
                    raw = wf.readframes(wf.getnframes())
            samples = np.frombuffer(raw, dtype=np.int16)
            if samples.size == 0:
                return None
            return (samples.astype(np.float32) / 32768.0)
        except Exception as exc:  # noqa: BLE001
            print(f"[VoiceAuth] waveform decode failed: {exc}")
            return None

    def _embed(self, audio: bytes) -> np.ndarray | None:
        encoder = self._get_encoder()
        if encoder is None:
            return None
        wav = self._to_waveform(audio)
        if wav is None:
            return None
        try:
            from resemblyzer import preprocess_wav  # type: ignore[import-not-found]
            processed = preprocess_wav(wav, source_sr=_SAMPLE_RATE)
            return encoder.embed_utterance(processed)
        except Exception as exc:  # noqa: BLE001
            print(f"[VoiceAuth] embed failed: {exc}")
            return None

    # ── enrollment ─────────────────────────────────────────────────────── #

    async def enroll_owner(
        self, samples: list[bytes] | None = None, num_samples: int = 5,
    ) -> dict[str, Any]:
        """Build and persist the owner embedding.

        ``samples`` — pre-recorded utterances (e.g. from the API). If
        omitted, records ``num_samples`` clips interactively from the mic
        (used by the CLI / first-run enrollment)."""
        if self._get_encoder() is None:
            return {"ok": False, "error": "resemblyzer not installed"}

        if samples is None:
            samples = self._record_samples(num_samples)
        if not samples:
            return {"ok": False, "error": "no audio samples"}

        embeds: list[np.ndarray] = []
        for clip in samples:
            e = self._embed(clip)
            if e is not None:
                embeds.append(e)
        if not embeds:
            return {"ok": False, "error": "could not embed any sample"}

        mean = np.mean(np.stack(embeds), axis=0)
        # Re-normalise the mean so cosine math stays a plain dot product.
        norm = np.linalg.norm(mean)
        owner = mean / norm if norm > 0 else mean
        try:
            np.save(self._profile_path, owner)
            self._owner_embed = owner
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"save failed: {exc}"}

        if self._db is not None:
            self._db.log_event(
                "voice_enrolled", "INFO", "voice_auth",
                f"Owner profile enrolled from {len(embeds)} samples",
            )
        print("[JARVIS] Voice profile enrolled successfully")
        return {"ok": True, "samples_used": len(embeds)}

    def _record_samples(self, n: int) -> list[bytes]:
        """Record ``n`` short clips from the mic (interactive CLI path)."""
        try:
            import sounddevice as sd  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001
            print(f"[VoiceAuth] sounddevice unavailable: {exc}")
            return []
        from .. import mic_lock
        if not mic_lock.try_acquire("voice_auth"):
            print(f"[VoiceAuth] mic busy ({mic_lock.owner()}) — cannot enroll")
            return []
        clips: list[bytes] = []
        dur = 3.0
        try:
            for i in range(n):
                print(f"[VoiceAuth] Sample {i+1}/{n} — sprich jetzt "
                      f"({dur:.0f}s)…")
                try:
                    rec = sd.rec(
                        int(dur * _SAMPLE_RATE), samplerate=_SAMPLE_RATE,
                        channels=1, dtype="int16",
                    )
                    sd.wait()
                    clips.append(rec.astype(np.int16).tobytes())
                except Exception as exc:  # noqa: BLE001
                    print(f"[VoiceAuth] recording failed: {exc}")
        finally:
            mic_lock.release("voice_auth")
        return clips

    # ── verification ───────────────────────────────────────────────────── #

    async def verify_speaker(self, audio: bytes) -> dict[str, Any]:
        """Compare an utterance to the owner profile.

        Returns ``{is_owner, confidence, action}`` where action ∈
        {allow, challenge, deny}. Thresholds: >0.85 owner, 0.65–0.85
        challenge, <0.65 deny (spec §1)."""
        # Auth disabled → not enforced.
        if not self._enabled:
            return {"is_owner": True, "confidence": 1.0, "action": "allow",
                    "note": "voice auth disabled"}

        # Degraded (no profile / no encoder) → fail OPEN for the owner,
        # but log it so the gap is visible.
        if self._owner_embed is None or self._get_encoder() is None:
            self._log_degraded()
            return {"is_owner": True, "confidence": 1.0, "action": "allow",
                    "note": "voice auth degraded (no profile/encoder)"}

        embed = self._embed(audio)
        if embed is None:
            return {"is_owner": False, "confidence": 0.0, "action": "challenge",
                    "note": "could not embed audio"}

        sim = float(np.dot(embed, self._owner_embed))
        sim = max(0.0, min(1.0, sim))  # clamp (cosine can be slightly <0)

        if sim >= self._threshold:
            action = "allow"
        elif sim >= 0.65:
            action = "challenge"
        else:
            action = "deny"
        is_owner = sim >= self._threshold

        if self._db is not None:
            self._db.log_access(
                user="owner" if is_owner else "unknown",
                command=None, ip_address=None, voice_confidence=round(sim, 3),
                permission_level=None, allowed=is_owner,
                reason=f"speaker verify → {action}",
            )
        return {"is_owner": is_owner, "confidence": round(sim, 3),
                "action": action}

    def _log_degraded(self) -> None:
        if self._db is not None:
            self._db.log_event(
                "voice_auth_degraded", "LOW", "voice_auth",
                "Verification requested but encoder/profile unavailable — "
                "allowed (single-user fail-open)",
            )

    # ── command-level permission ───────────────────────────────────────── #

    def command_security_level(self, command: str) -> str:
        """Map a free-text command to its required security level. Defaults
        to 'low' when nothing matches (read-only-ish commands)."""
        c = (command or "").lower()
        for level, keywords in _LEVEL_KEYWORDS:
            if any(k in c for k in keywords):
                return level
        return "low"

    async def check_command_permission(
        self, command: str, speaker_confidence: float,
        security_level: str | None = None,
    ) -> bool:
        """True if ``speaker_confidence`` clears the bar for the command's
        security level."""
        level = security_level or self.command_security_level(command)

        # Guest mode: only 'low' commands pass without owner-grade voice.
        if self.is_guest_mode():
            allowed = level == "low" and self._guest_command_ok(command)
            self._audit(command, speaker_confidence, level, allowed,
                        "guest mode")
            return allowed

        # Auth disabled → everything allowed.
        if not self._enabled:
            return True

        # Recent correct PIN → owner-authorised regardless of voice match.
        if self.is_pin_elevated():
            self._audit(command, speaker_confidence, level, True, "pin elevated")
            return True

        need = _LEVEL_MIN_CONFIDENCE.get(level, 0.65)
        allowed = speaker_confidence >= need
        self._audit(command, speaker_confidence, level, allowed,
                    f"need ≥{need}")
        return allowed

    def _audit(self, command: str, conf: float, level: str,
               allowed: bool, reason: str) -> None:
        if self._db is not None:
            self._db.log_access(
                user="owner", command=command, ip_address=None,
                voice_confidence=round(conf, 3), permission_level=level,
                allowed=allowed, reason=reason,
            )

    # ── PIN challenge ──────────────────────────────────────────────────── #

    @staticmethod
    def hash_pin(pin: str) -> str:
        """Return a bcrypt hash for a PIN (for storing in JARVIS_PIN)."""
        if not _BCRYPT_OK:
            raise RuntimeError("bcrypt not installed")
        return bcrypt.hashpw(pin.encode("utf-8"), bcrypt.gensalt()).decode("ascii")

    def verify_pin(self, pin: str) -> bool:
        if not (_BCRYPT_OK and self._pin_hash):
            return False
        try:
            return bcrypt.checkpw(pin.encode("utf-8"),
                                  self._pin_hash.encode("ascii"))
        except Exception as exc:  # noqa: BLE001
            print(f"[VoiceAuth] PIN check failed: {exc}")
            return False

    async def challenge(self, reason: str = "", pin: str | None = None) -> bool:
        """Fallback when voice is uncertain. If ``pin`` is supplied (API
        path) verify it directly; otherwise prompt on the TTY (CLI path)."""
        print("[JARVIS] Ich konnte deine Stimme nicht sicher erkennen. "
              "Bitte gib deinen PIN ein.")
        if pin is None:
            try:
                import getpass
                pin = getpass.getpass("PIN: ")
            except Exception:  # noqa: BLE001 — no TTY
                return False
        ok = self.verify_pin(pin)
        if self._db is not None:
            self._db.log_event(
                "pin_challenge", "INFO" if ok else "MEDIUM", "voice_auth",
                f"PIN challenge ({reason}) → {'ok' if ok else 'failed'}",
            )
        return ok

    # ── guest mode ─────────────────────────────────────────────────────── #

    def is_guest_mode(self) -> bool:
        if self._guest_until and time.time() < self._guest_until:
            return True
        if self._guest_until:  # expired → auto-clear
            self._guest_until = 0.0
            self._guest_allowed = None
        return False

    def _guest_command_ok(self, command: str) -> bool:
        if self._guest_allowed is None:
            return True  # default guest set (lights/music/weather/time)
        c = (command or "").lower()
        return any(a.lower() in c for a in self._guest_allowed)

    async def enable_guest_mode(
        self, duration_hours: int = 2, allowed_commands: list[str] | None = None,
    ) -> dict[str, Any]:
        self._guest_until = time.time() + max(1, duration_hours) * 3600
        self._guest_allowed = allowed_commands or [
            "licht", "lights", "musik", "music", "wetter", "weather",
            "uhrzeit", "zeit", "time",
        ]
        if self._db is not None:
            self._db.log_event(
                "guest_mode_on", "INFO", "voice_auth",
                f"Guest mode for {duration_hours}h",
            )
        print(f"[JARVIS] Gast-Modus aktiviert für {duration_hours} Stunden.")
        return {"ok": True, "expires": self._guest_until}

    async def disable_guest_mode(self) -> dict[str, Any]:
        self._guest_until = 0.0
        self._guest_allowed = None
        if self._db is not None:
            self._db.log_event(
                "guest_mode_off", "INFO", "voice_auth", "Guest mode deactivated",
            )
        print("[JARVIS] Guest mode deactivated")
        return {"ok": True}


# ── CLI: set a PIN hash or enroll the owner ─────────────────────────────── #

def _main() -> None:
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="JARVIS voice auth utility")
    parser.add_argument("--set-pin", action="store_true",
                        help="Hash a PIN and print the JARVIS_PIN= line")
    parser.add_argument("--enroll", action="store_true",
                        help="Record 5 mic samples and enroll the owner")
    args = parser.parse_args()

    if args.set_pin:
        import getpass
        pin = getpass.getpass("Neuer PIN: ")
        confirm = getpass.getpass("PIN bestätigen: ")
        if pin != confirm:
            print("PINs stimmen nicht überein.")
            return
        print("\nFüge das in deine .env ein:\n")
        print(f"JARVIS_PIN={VoiceAuthenticator.hash_pin(pin)}")
        return

    if args.enroll:
        auth = VoiceAuthenticator()
        result = asyncio.run(auth.enroll_owner())
        print(result)
        return

    parser.print_help()


if __name__ == "__main__":
    _main()
