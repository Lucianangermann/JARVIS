"""Local wake-word voice loop for the MacBook server — streaming version.

Design
------
Earlier versions recorded fixed 4 s windows in a tight loop. That left a
gap of ~1–2 s after each window while Whisper ran — anything you said
during that gap was lost. This version uses a continuous mic stream with
energy-based voice-activity detection (VAD):

    * ``sd.InputStream`` runs continuously, pushing 100 ms PCM blocks
      into an in-memory queue.
    * We treat the user as **silent** until RMS crosses
      ``SPEECH_RMS_THRESHOLD`` in any block. From that moment we start
      accumulating blocks.
    * When the RMS stays under ``SILENCE_RMS_THRESHOLD`` for
      ``SILENCE_HANG_S`` continuously, the segment is closed and sent
      to Whisper.
    * If the transcript contains the wake word, the rest of the
      transcript is the command. The Brain replies, TTS speaks the
      answer, the queue is drained (so JARVIS doesn't transcribe its
      own voice), and the loop continues listening.

Privacy
-------
The mic stream is local; nothing leaves the machine until Claude sees
the transcript (text only). ``[MIC] speech`` / ``[MIC] silence`` is
printed so it's visible when audio is being captured for transcription.

Run
---
Launched automatically by ``main.py`` when ``JARVIS_LOCAL_VOICE=1``. For
standalone testing without the HTTP server::

    python -m server.voice_loop
"""
from __future__ import annotations

import queue
import threading
import time
from typing import TYPE_CHECKING

from .config import settings

if TYPE_CHECKING:
    from .brain import Brain


# --- Audio config -------------------------------------------------------- #

SAMPLE_RATE = 16_000
BLOCK_S = 0.1                          # 100 ms PCM blocks → smooth VAD
BLOCK_SIZE = int(SAMPLE_RATE * BLOCK_S)

# Hysteresis: a higher threshold to *open* the segment, a lower one to
# count as silence — avoids flapping on borderline blocks.
SPEECH_RMS_THRESHOLD = 600
SILENCE_RMS_THRESHOLD = 300

# How long sustained silence ends a segment, and the minimum / maximum
# segment length we'll transcribe at all.
SILENCE_HANG_S = 0.8                   # 800 ms of silence → segment done
MIN_SPEECH_S = 0.3                     # discard blips shorter than this
MAX_SPEECH_S = 15.0                    # cap utterances so Whisper stays snappy

# Tiny prefix preserved before the first speech block — captures the
# attack of the wake word that the threshold detector just missed.
PRE_ROLL_S = 0.4
PRE_ROLL_BLOCKS = int(PRE_ROLL_S / BLOCK_S)


_stop_event = threading.Event()


def request_stop() -> None:
    """Ask the loop to exit at the next iteration boundary."""
    _stop_event.set()


def run(brain: "Brain", session_id: str | None = None) -> None:
    """Block forever (until ``request_stop()``) running the streaming loop."""
    # Imports are local so the module can be loaded in environments without
    # the voice stack — we only fail when run() is actually called.
    import collections

    import numpy as np
    import sounddevice as sd

    from . import stt, tts

    session = session_id or settings.JARVIS_AUTH_TOKEN

    print(f"[JARVIS] streaming voice loop ready — wake word: {settings.WAKE_WORD!r}")
    print(f"[JARVIS] say '{settings.WAKE_WORD} <command>'    "
          f"(speech_rms>{SPEECH_RMS_THRESHOLD}, silence_rms<{SILENCE_RMS_THRESHOLD})")

    # Warm up Whisper so the first hit isn't laggy.
    try:
        stt._load_model()  # noqa: SLF001 — intentional warm-up
    except Exception as exc:  # noqa: BLE001
        print(f"[JARVIS] could not load whisper model: {exc}")
        return

    audio_q: queue.Queue = queue.Queue()

    def _callback(indata, frames, time_info, status):  # noqa: ARG001
        if status:
            print(f"[JARVIS] mic status: {status}")
        # int16, mono, flat
        audio_q.put(indata.copy().reshape(-1))

    # Rolling pre-roll so we don't clip the first phoneme of "jarvis".
    pre_roll: "collections.deque" = collections.deque(maxlen=PRE_ROLL_BLOCKS)

    in_speech = False
    buf: list = []
    silence_blocks = 0
    silence_blocks_to_end = int(SILENCE_HANG_S / BLOCK_S)
    min_speech_blocks = int(MIN_SPEECH_S / BLOCK_S)
    max_speech_blocks = int(MAX_SPEECH_S / BLOCK_S)

    # Follow-up mode: once the wake word fires, the session stays active
    # for FOLLOWUP_TIMEOUT_S seconds — every segment in that window is a
    # command, no wake word needed. Refreshes after each reply; ends when
    # the user says one of the end phrases or the timer lapses.
    followup_until = 0.0  # monotonic deadline; 0 = inactive
    followup_window = settings.FOLLOWUP_TIMEOUT_S

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=BLOCK_SIZE,
            callback=_callback,
        ):
            while not _stop_event.is_set():
                try:
                    block = audio_q.get(timeout=1.0)
                except queue.Empty:
                    continue

                # RMS in int32 space to avoid int16 overflow.
                samples = block.astype(np.int32)
                rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0

                if not in_speech:
                    pre_roll.append(block)
                    if rms > SPEECH_RMS_THRESHOLD:
                        in_speech = True
                        buf = list(pre_roll)        # keep the pre-roll
                        buf.append(block)
                        silence_blocks = 0
                        print("[MIC] speech started")
                    continue

                # In-speech: keep accumulating, watch for sustained silence.
                buf.append(block)
                if rms < SILENCE_RMS_THRESHOLD:
                    silence_blocks += 1
                else:
                    silence_blocks = 0

                end_by_silence = silence_blocks >= silence_blocks_to_end
                end_by_cap = len(buf) >= max_speech_blocks
                if not (end_by_silence or end_by_cap):
                    continue

                # Segment closed — transcribe.
                in_speech = False
                if len(buf) - silence_blocks < min_speech_blocks:
                    # Too short to be real speech. Drop and reset.
                    buf = []
                    silence_blocks = 0
                    pre_roll.clear()
                    print("[MIC] (segment too short, ignored)")
                    continue

                print(f"[MIC] segment closed ({BLOCK_S * len(buf):.1f}s) — transcribing…")
                pcm = np.concatenate(buf).tobytes()
                wav = stt._pcm_to_wav(pcm, sample_rate=SAMPLE_RATE)  # noqa: SLF001
                buf = []
                silence_blocks = 0
                pre_roll.clear()

                try:
                    transcript = stt.transcribe(wav, sample_rate=SAMPLE_RATE)
                except Exception as exc:  # noqa: BLE001
                    print(f"[JARVIS] transcribe error: {exc}")
                    transcript = ""

                if not transcript:
                    continue

                # Are we still inside a follow-up window?
                now = time.monotonic()
                in_followup = followup_window > 0 and now < followup_until
                if followup_until and not in_followup:
                    print("[JARVIS] follow-up window lapsed — listening for "
                          f"wake word again ({settings.WAKE_WORD!r})")
                    followup_until = 0.0

                # End phrase always wins, even outside follow-up mode.
                if in_followup and stt.is_end_phrase(transcript):
                    print(f"[JARVIS] end phrase heard: {transcript!r}")
                    tts.speak("Alles klar. Bis später.")
                    tts.wait(timeout=10.0)
                    followup_until = 0.0
                    _drain(audio_q)
                    continue

                # Decide whether this segment is a command.
                if in_followup:
                    # Every utterance counts. Strip the wake word if the
                    # user accidentally said "Jarvis" again.
                    command = (
                        stt.strip_wake_word(transcript)
                        if stt.has_wake_word(transcript)
                        else transcript
                    )
                    print(f"[JARVIS] (follow-up) heard={transcript!r}")
                else:
                    # Idle: only react to the wake word.
                    if not stt.has_wake_word(transcript):
                        print(f"[JARVIS] (no wake word) heard: {transcript!r}")
                        continue
                    command = stt.strip_wake_word(transcript)
                    print(f"[JARVIS] wake word fired. heard={transcript!r}")
                    if not command:
                        # Just "Jarvis" — confirm and enter follow-up mode.
                        tts.speak("Ja?")
                        tts.wait(timeout=10.0)
                        if followup_window > 0:
                            followup_until = time.monotonic() + followup_window
                        _drain(audio_q)
                        continue

                if not command.strip():
                    # Empty-after-strip — refresh the window and listen on.
                    if followup_window > 0:
                        followup_until = time.monotonic() + followup_window
                    _drain(audio_q)
                    continue

                print(f"[CLIENT: MacBook] [YOU·voice] {command}")
                try:
                    reply = brain.reply(session, command)
                except Exception as exc:  # noqa: BLE001
                    reply = f"Entschuldige, etwas ist schiefgelaufen: {exc}"
                print(f"[JARVIS] {reply}")
                tts.speak(reply)
                tts.wait(timeout=120.0)

                # Extend the follow-up window — every reply gives the user
                # another FOLLOWUP_TIMEOUT_S to keep going.
                if followup_window > 0:
                    followup_until = time.monotonic() + followup_window

                # Drain anything captured while JARVIS was speaking so we
                # don't transcribe his own voice on the next round.
                _drain(audio_q)
    finally:
        print("[JARVIS] streaming voice loop stopped.")


def _drain(q: queue.Queue) -> None:
    """Best-effort flush of the audio queue."""
    drained = 0
    while True:
        try:
            q.get_nowait()
            drained += 1
        except queue.Empty:
            break
    if drained:
        print(f"[MIC] drained {drained} buffered blocks "
              f"({drained * BLOCK_S:.1f}s) after TTS")


if __name__ == "__main__":  # pragma: no cover
    from .brain import Brain

    run(Brain())
