"""Local wake-word voice loop for the MacBook server.

How it works
------------
1. Continuously record short windows (``LISTEN_WINDOW_S``) from the default
   mic. Whisper transcribes each window.
2. If the wake word (``settings.WAKE_WORD``, default ``"jarvis"``) appears
   anywhere in the transcript:
       - If text follows the wake word in the same window, that is the
         command — run it through the Brain and speak the reply.
       - Otherwise, beep-style TTS prompt ("Yes?") and record one more
         ``COMMAND_WINDOW_S`` window as the command.
3. Loop. ``[MIC ON]`` / ``[MIC OFF]`` is printed every time the mic opens
   so it's obvious when audio is captured.

Privacy
-------
Whisper runs entirely locally. No audio leaves the machine until Claude
sees the transcript (text only). The mic is open only inside the
``listen_once`` calls below — see ``[MIC ON]/[MIC OFF]`` markers.

Run
---
This is launched automatically by ``main.py`` when ``JARVIS_LOCAL_VOICE=1``.
For standalone testing without the HTTP server::

    python -m server.voice_loop
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from .config import settings

if TYPE_CHECKING:
    from .brain import Brain

# Tunables — short windows = lower wake-word latency, more CPU.
LISTEN_WINDOW_S = 4.0      # rolling listen window for the wake word
COMMAND_WINDOW_S = 6.0     # follow-up window after a bare "jarvis"
COOLDOWN_S = 0.2           # gap between captures to let TTS finish + drain queue


_stop_event = threading.Event()


def request_stop() -> None:
    """Ask the loop to exit at the next iteration boundary."""
    _stop_event.set()


def run(brain: "Brain", session_id: str | None = None) -> None:
    """Block forever (until ``request_stop()``) running the wake-word loop.

    ``session_id`` keys the Brain's per-session history. Defaults to the
    server's auth token so a voice session shares memory with the
    ``Authorization: Bearer …`` HTTP/WS clients.
    """
    # Imported here so the module can be imported in environments without
    # the voice stack — we only fail when run() is actually called.
    from . import stt, tts

    session = session_id or settings.JARVIS_AUTH_TOKEN

    print(f"[JARVIS] local voice loop ready — wake word: {settings.WAKE_WORD!r}")
    print(f"[JARVIS] window={LISTEN_WINDOW_S}s   say '{settings.WAKE_WORD} <command>'")

    # Preload whisper once so the first wake-word hit isn't laggy.
    try:
        stt._load_model()  # noqa: SLF001 — intentional warm-up
    except Exception as exc:  # noqa: BLE001
        print(f"[JARVIS] could not load whisper model: {exc}")
        return

    while not _stop_event.is_set():
        try:
            transcript = stt.listen_once(duration_s=LISTEN_WINDOW_S)
        except Exception as exc:  # noqa: BLE001 — mic may disappear, etc.
            print(f"[JARVIS] mic error: {exc}; retrying in 2s")
            _stop_event.wait(2.0)
            continue

        if not transcript:
            continue
        if not stt.has_wake_word(transcript):
            # Comment this out if you want quieter logs.
            # print(f"[JARVIS] (no wake word: {transcript!r})")
            continue

        command = stt.strip_wake_word(transcript)
        print(f"[JARVIS] wake word fired. heard={transcript!r}")

        if not command:
            # Bare "jarvis" with nothing after → ask for the actual command.
            tts.speak("Yes?")
            # Give TTS a moment to start so we don't record ourselves.
            time.sleep(0.6)
            try:
                command = stt.listen_once(duration_s=COMMAND_WINDOW_S).strip()
            except Exception as exc:  # noqa: BLE001
                print(f"[JARVIS] mic error during command capture: {exc}")
                continue
            if not command:
                tts.speak("I didn't catch that.")
                continue

        print(f"[CLIENT: MacBook] [YOU·voice] {command}")
        try:
            reply = brain.reply(session, command)
        except Exception as exc:  # noqa: BLE001
            reply = f"Sorry, something went wrong: {exc}"
        print(f"[JARVIS] {reply}")
        tts.speak(reply)

        # Brief cooldown so the loop doesn't immediately re-record TTS output.
        _stop_event.wait(COOLDOWN_S)

    print("[JARVIS] local voice loop stopped.")


# Allow `python -m server.voice_loop` for a server-less voice test.
if __name__ == "__main__":  # pragma: no cover
    from .brain import Brain

    run(Brain())
