"""Text-to-speech.

Two backends:

* **macOS** — shells out to the system ``say`` command. This is far more
  reliable than pyttsx3 for long replies (pyttsx3's macOS driver stops
  after the first ``runAndWait()`` cycle in many setups), and it can
  reach every installed system voice including premium ones like
  Markus / Yannick.
* **Other OSes** — falls back to ``pyttsx3`` driving the local engine.

Callers don't care: both paths share the single worker thread + queue.
``speak(text)`` is non-blocking; ``wait()`` blocks until the queue is
drained (the voice loop uses it so the mic doesn't reopen while JARVIS
is still talking).

Swap-ability:
    Replace this module with another backend (Piper, OpenAI TTS, …) —
    the rest of the server only imports ``speak`` / ``wait`` / ``shutdown``.
"""
from __future__ import annotations

import queue
import re
import subprocess
import sys
import threading

_DARWIN = sys.platform == "darwin"

_jobs: "queue.Queue[str | None]" = queue.Queue()
_worker: threading.Thread | None = None
_engine = None
_lock = threading.Lock()
_idle = threading.Event()
_idle.set()  # starts idle — set means "nothing pending"


# ---- Markdown / emoji stripper ------------------------------------------ #
# Claude tends to format replies with **bold**, *italic*, `code`, bullet
# points, etc. pyttsx3 reads these literally ("star star important star
# star"). Strip them before sending to the engine.

_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_BOLD_ITAL = re.compile(r"(\*+|_+)(.+?)\1")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")          # [text](url) -> text
_BARE_URL = re.compile(r"https?://\S+")
_LIST_MARKER = re.compile(r"^\s*([-*+]|\d+\.)\s+", re.MULTILINE)
_HEADING = re.compile(r"^\s*#{1,6}\s*", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
# Strip every char outside common letters/digits/whitespace/punctuation
# (this catches emoji, box-drawing, etc.). Keep German umlauts + accents.
_STRIPPABLE = re.compile(
    r"[^\w\s\.,;:!\?\-'\"()äöüÄÖÜßéèêàâîïôûç%€$°]+",
    flags=re.UNICODE,
)


def _speakable(text: str) -> str:
    """Strip markdown/emoji/URLs so pyttsx3 doesn't read them literally."""
    if not text:
        return ""
    text = _CODE_BLOCK.sub(" ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _BOLD_ITAL.sub(r"\2", text)
    text = _LINK.sub(r"\1", text)
    text = _BARE_URL.sub("link", text)
    text = _HEADING.sub("", text)
    text = _BLOCKQUOTE.sub("", text)
    text = _LIST_MARKER.sub("", text)
    text = _STRIPPABLE.sub(" ", text)
    # Collapse runs of whitespace to single spaces, but keep sentence breaks.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"\n", " ", text)
    return text.strip()


# ---- macOS `say` backend ------------------------------------------------ #

_resolved_macos_voice: str | None = None  # cached after first probe


def _list_say_voices() -> dict[str, str]:
    """Return ``{voice_name: locale}`` parsed from ``say -v ?``."""
    try:
        out = subprocess.run(
            ["say", "-v", "?"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        print(f"[JARVIS] tts: could not list say voices: {exc}")
        return {}

    voices: dict[str, str] = {}
    for line in out.splitlines():
        # Lines look like:
        #   "Anna                de_DE    # Hallo, ..."
        #   "Eddy (German (Germany)) de_DE    # ..."
        # Voice name is everything before the locale token (xx_YY).
        toks = line.rstrip().split()
        if not toks:
            continue
        name_parts: list[str] = []
        locale = ""
        for t in toks:
            # Locale codes look like "de_DE", "en_GB". Match strictly.
            if re.fullmatch(r"[a-z]{2,3}_[A-Z0-9]{2,5}", t) or re.fullmatch(
                r"[a-z]{2,3}_[a-z]{2,5}", t
            ):
                locale = t
                break
            name_parts.append(t)
        if name_parts:
            voices[" ".join(name_parts)] = locale
    return voices


def _resolve_say_voice(preferred: str, language: str) -> str:
    """Choose a `say` voice. Empty string = use `say`'s default."""
    voices = _list_say_voices()
    if not voices:
        return ""

    if preferred:
        pref = preferred.strip()
        if pref in voices:  # exact match
            return pref
        # Case-insensitive substring match
        for name in voices:
            if pref.lower() in name.lower():
                return name
        print(f"[JARVIS] tts: preferred voice {preferred!r} not installed; "
              "trying fallbacks.")

    # Autoselect male German first (per user preference), then female German.
    # Premium voices (Markus / Yannick) are downloaded on demand in macOS
    # System Settings → Accessibility → Spoken Content → System Voice.
    # Localized names matter: on a German-locale macOS the parenthetical
    # is "(Deutsch (Deutschland))", on English-locale it's "(German (Germany))".
    fallback_priority = [
        "Markus",                              # German male, premium
        "Yannick",                             # German male, premium
        "Reed (Deutsch (Deutschland))",        # German male, Eloquence
        "Reed (German (Germany))",
        "Grandpa (Deutsch (Deutschland))",     # German male, Eloquence elder
        "Grandpa (German (Germany))",
        "Rocko (Deutsch (Deutschland))",       # German male, Eloquence
        "Rocko (German (Germany))",
        "Eddy (Deutsch (Deutschland))",        # German, Eloquence
        "Eddy (German (Germany))",
        "Anna",                                # German female, default fallback
    ]
    for cand in fallback_priority:
        if cand in voices:
            return cand

    # Last resort: first voice whose locale starts with TTS_LANGUAGE.
    lang = (language or "").strip().lower()
    if lang:
        for name, loc in voices.items():
            if loc.lower().startswith(lang):
                return name

    return ""


def _say_macos(text: str) -> None:
    """Speak via macOS ``say``. Blocks until done."""
    global _resolved_macos_voice
    from .config import settings

    if _resolved_macos_voice is None:
        _resolved_macos_voice = _resolve_say_voice(
            settings.TTS_VOICE, settings.TTS_LANGUAGE
        )
        if _resolved_macos_voice:
            print(f"[JARVIS] tts: say voice={_resolved_macos_voice!r}")
        else:
            print("[JARVIS] tts: say using system default voice")

    cmd = ["say"]
    if _resolved_macos_voice:
        cmd += ["-v", _resolved_macos_voice]
    if settings.TTS_RATE:
        cmd += ["-r", str(settings.TTS_RATE)]
    cmd += ["--", text]
    try:
        subprocess.run(cmd, check=False, timeout=120)
    except subprocess.TimeoutExpired:
        print("[JARVIS] tts: say timed out (text was too long for 120s)")


# ---- pyttsx3 backend (non-macOS) ---------------------------------------- #

def _ensure_engine():
    global _engine
    if _engine is None:
        import pyttsx3  # imported lazily so a TTS-less deploy doesn't pay for it

        from .config import settings

        _engine = pyttsx3.init()
        _engine.setProperty("rate", settings.TTS_RATE)
        _select_voice(_engine, settings.TTS_VOICE, settings.TTS_LANGUAGE)
    return _engine


def _select_voice(engine, preferred: str, language: str) -> None:
    """Pick a TTS voice. ``preferred`` wins if it matches by id or name;
    otherwise we pick the first voice whose locale starts with ``language``.

    Failures are logged but never raise — TTS keeps working with whatever
    the engine chose by default.
    """
    try:
        voices = engine.getProperty("voices")
    except Exception as exc:  # noqa: BLE001
        print(f"[JARVIS] tts: could not enumerate voices: {exc}")
        return

    def _locales(voice) -> list[str]:
        out = []
        for lang in getattr(voice, "languages", []) or []:
            if isinstance(lang, (bytes, bytearray)):
                out.append(lang.decode("utf-8", "replace"))
            else:
                out.append(str(lang))
        return out

    chosen = None
    pref = preferred.strip().lower()
    if pref:
        for v in voices:
            if pref in v.id.lower() or pref in (v.name or "").lower():
                chosen = v
                break

    if chosen is None and language:
        lang = language.strip().lower()
        for v in voices:
            # Match against languages metadata first…
            if any(loc.lower().startswith(lang) for loc in _locales(v)):
                chosen = v
                break
            # …then fall back to the locale embedded in the voice id
            # (Apple ids look like com.apple.voice.compact.de-DE.Anna).
            if f".{lang}-" in v.id.lower() or f".{lang}_" in v.id.lower():
                chosen = v
                break

    if chosen is None:
        print(f"[JARVIS] tts: no voice matched preferred={preferred!r} "
              f"language={language!r}; using system default.")
        return

    try:
        engine.setProperty("voice", chosen.id)
        print(f"[JARVIS] tts: voice={chosen.name!r} ({chosen.id})")
    except Exception as exc:  # noqa: BLE001
        print(f"[JARVIS] tts: could not set voice {chosen.id!r}: {exc}")


def _run() -> None:
    # Lazily init the pyttsx3 engine only on platforms that need it.
    engine = None if _DARWIN else _ensure_engine()
    while True:
        text = _jobs.get()
        if text is None:  # shutdown sentinel
            _idle.set()
            return
        try:
            if _DARWIN:
                _say_macos(text)
            else:
                # pyttsx3 path: split long replies into sentences so the
                # engine doesn't choke on very long utterances.
                for chunk in _split_sentences(text):
                    engine.say(chunk)
                engine.runAndWait()
        except Exception as exc:  # noqa: BLE001 — TTS errors shouldn't crash the server
            print(f"[JARVIS] tts error: {exc}", file=sys.stderr)
        finally:
            # Mark idle if nothing else is pending right now.
            if _jobs.empty():
                _idle.set()


def _split_sentences(text: str) -> list[str]:
    """Naive sentence splitter — good enough for TTS pacing."""
    # Split on . ! ? ; followed by whitespace; keep the punctuation.
    parts = re.split(r"(?<=[\.!\?;])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def speak(text: str) -> None:
    """Enqueue ``text`` for the server's local speakers. Non-blocking.

    Strips markdown / URLs / emoji before queuing so the engine doesn't
    read symbols literally.
    """
    cleaned = _speakable(text)
    if not cleaned:
        return
    # Show exactly what the engine will say — so if you still hear
    # markdown, it's the sanitizer that needs a tweak (vs. Claude's reply
    # or a stale server process).
    print(f"[JARVIS·tts] {cleaned}")
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="jarvis-tts", daemon=True)
            _worker.start()
    _idle.clear()
    _jobs.put(cleaned)


def wait(timeout: float | None = None) -> bool:
    """Block until the TTS queue is fully drained (or ``timeout`` s elapse).

    Returns True if drained, False on timeout. Used by the voice loop so
    the mic doesn't reopen while JARVIS is still speaking (otherwise it
    re-records its own voice).
    """
    return _idle.wait(timeout)


def shutdown() -> None:
    """Stop the worker thread (called from the FastAPI lifespan)."""
    if _worker is not None and _worker.is_alive():
        _jobs.put(None)
