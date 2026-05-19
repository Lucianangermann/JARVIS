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

import os
import queue
import threading
import time
from typing import TYPE_CHECKING

from . import events
from .config import settings


# ---- HUD event publishing ------------------------------------------------ #
# Voice transitions get broadcast to every connected WS client (Electron
# HUD, iPhone web, etc.) so they can mirror what the mic loop is doing.
# `_last_state` dedupes repeats so the bus doesn't flood with the same
# value during quiet stretches of the main loop.

_last_state: str | None = None
_state_lock = threading.Lock()


def _emit_state(name: str) -> None:
    """Publish a voice_state event iff the state actually changed."""
    global _last_state
    with _state_lock:
        if _last_state == name:
            return
        _last_state = name
    events.publish({"type": "voice_state", "state": name})


def _emit_user_message(text: str) -> None:
    if text:
        events.publish({"type": "user_message", "text": text})


def _emit_jarvis_reply(text: str) -> None:
    if text:
        events.publish({"type": "jarvis_reply", "text": text})


# ---- mid-reply interrupt (from HTTP /interrupt or hotkey) ---------------- #
# These refs are populated by run() once the loop is initialised. interrupt()
# uses them to cancel an in-flight brain reply and silence TTS without
# arming the kill switch (which would block all Tier 2+ actions until a
# manual resume). The lock keeps a rapid double-hotkey press from racing.

_interrupt_lock = threading.Lock()
_brain_cancel_ref: threading.Event | None = None
_tts_ref = None                                       # the tts module
_audio_q_ref: "queue.Queue | None" = None             # mic block queue


def interrupt(reason: str = "user request") -> dict:
    """Cut JARVIS off mid-reply. Sets brain_cancel so the worker
    thread drops its pending reply, calls tts.stop() to silence the
    speakers immediately, and drains the mic queue so the residual
    JARVIS audio doesn't get re-ingested. Safe to call when nothing's
    happening — returns ``{brain: False, tts: False, ok: True}`` and
    is a no-op. Returns ``ok: False`` only if voice_loop isn't
    running yet (server still booting / JARVIS_LOCAL_VOICE=0)."""
    with _interrupt_lock:
        if _brain_cancel_ref is None:
            return {"ok": False, "reason": "voice loop not running"}

        stopped = {"ok": True, "brain": False, "tts": False}

        if not _brain_cancel_ref.is_set():
            _brain_cancel_ref.set()
            stopped["brain"] = True

        if _tts_ref is not None and not _tts_ref.is_idle():
            _tts_ref.stop()
            stopped["tts"] = True

        if _audio_q_ref is not None:
            _drain(_audio_q_ref)

        if stopped["brain"] or stopped["tts"]:
            print(f"[JARVIS] interrupted: {reason}")
            events.publish({"type": "voice_state", "state": "listening"})
        return stopped

if TYPE_CHECKING:
    from .brain import Brain


# --- Audio config -------------------------------------------------------- #

SAMPLE_RATE = 16_000
# 10 ms blocks: that's the frame size Speex's echo canceller is tuned
# for. We aggregate 10 of them into a "logical block" for VAD purposes
# so the rest of the loop stays simple.
BLOCK_S = 0.01
BLOCK_SIZE = int(SAMPLE_RATE * BLOCK_S)   # 160 samples per audio tick

# Hysteresis for the *idle* VAD: only fires on confident user speech.
# Earlier we'd lowered this to 400 to catch quiet "Stop"-mid-TTS — but
# fast-interrupt does that job better now, so we can keep the idle
# threshold strict and stop hallucinating commands from breathing or
# room noise. Numbers tuned for built-in MacBook mic in a quiet room.
#
# If your mic delivers weaker signal (Voice Isolation on, mic far
# from the user, external mic with low pre-amp, …) bump these down
# via the env vars below. A short calibration: enable JARVIS_MIC_DEBUG=1
# for 60 s, watch the printed RMS while speaking normally, then set
# SPEECH_RMS_THRESHOLD slightly above your idle/ambient ceiling.
SPEECH_RMS_THRESHOLD = int(os.getenv("SPEECH_RMS_THRESHOLD", "900"))
SILENCE_RMS_THRESHOLD = int(os.getenv("SILENCE_RMS_THRESHOLD", "400"))
# Continuous RMS debug print — every 1 s. Useful to figure out what
# levels your mic actually delivers when you say "Jarvis". Off in
# production because it's noisy.
_MIC_DEBUG = os.getenv("JARVIS_MIC_DEBUG", "0") not in {"", "0", "false", "no"}

# How long sustained silence ends a segment, and the minimum / maximum
# segment length we'll transcribe at all.
SILENCE_HANG_S = 0.8                   # 800 ms of silence → segment done
MIN_SPEECH_S = 0.5                     # discard blips shorter than this
MAX_SPEECH_S = 15.0                    # cap utterances so Whisper stays snappy

# Tiny prefix preserved before the first speech block — captures the
# attack of the wake word that the threshold detector just missed.
PRE_ROLL_S = 0.4
PRE_ROLL_BLOCKS = int(PRE_ROLL_S / BLOCK_S)

# Speech-onset hardening: require N consecutive blocks above the speech
# RMS threshold before we declare in_speech. A single 10 ms peak — which
# is what a keyboard click looks like — won't pass; a real vowel onset
# (40-80 ms of continuous energy) will. Keeps Whisper from hallucinating
# random German words out of typing noise.
SPEECH_SEED_BLOCKS = 3   # 30 ms of uninterrupted above-threshold audio

# Minimum mic RMS we'll consider "user voice" while the speakers are
# actively playing. Calibrated for a soft-spoken user at laptop-mic
# distance with Markus playing through built-in speakers — AEC residual
# during pure JARVIS speech sits at clean_rms 5-50, well below 400.
USER_VOICE_RMS_GATE = 400

# How aggressively the gate scales with the speaker output level. We
# used to multiply 0.4 × out_rms, which meant loud Markus required the
# user to be louder than him to pass — physically not happening over
# laptop speakers. 0.1× still gives the AEC some headroom for residual.
USER_VOICE_GATE_OUT_FACTOR = 0.1

# Once the gate opens we hold it open for this long, even if the level
# drops back below threshold. This captures full words — consonants
# like the "st" in "Stop" are much quieter than vowels and would
# otherwise be sliced off, leaving Whisper just "op" → it transcribes
# that as English "up", which never matches a stop phrase.
GATE_HANGOVER_S = 0.6
GATE_HANGOVER_BLOCKS = int(GATE_HANGOVER_S / BLOCK_S)


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

    from . import aec as aec_mod
    from . import stt, tts

    # Expose tts for the cross-thread interrupt() helper above. Cleared
    # in the finally block when the loop exits.
    global _tts_ref
    _tts_ref = tts

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

    # Try to start the echo canceller. If speexdsp isn't installed, we
    # fall back to plain mic capture (no echo cancellation).
    aec = aec_mod.try_create()
    if aec is not None:
        print(f"[JARVIS] AEC enabled — Speex echo canceller "
              f"(frame={aec.frame_size}, sr={SAMPLE_RATE})")
    else:
        print("[JARVIS] AEC OFF — mic will hear the speakers; "
              "use headphones for a clean signal.")

    audio_q: queue.Queue = queue.Queue()
    global _audio_q_ref
    _audio_q_ref = audio_q

    # Diagnostics: every ~1 s log mean RMS for input / output / cleaned.
    # If AEC is actually cancelling, cleaned RMS during TTS should be
    # significantly lower than indata RMS. If cleaned ≈ indata, the
    # cancellation isn't happening (filter not converging, latency
    # mismatch, sample-rate disagreement, …).
    diag_state = {
        "blocks": 0,
        "in_acc": 0.0,
        "out_acc": 0.0,
        "clean_acc": 0.0,
        # Hangover countdown: while > 0, the gate stays open even if the
        # mic falls below USER_VOICE_RMS_GATE for a few blocks.
        "gate_open_left": 0,
    }
    diag_every = 100  # 100 × 10 ms = 1 s

    # Full-duplex callback: every 10 ms we get one mic frame (indata) and
    # the speaker output buffer (outdata) that we must fill. We feed
    # outdata from the TTS playback buffer, then ask the AEC to subtract
    # that signal from the mic, then push the cleaned mic samples for
    # the VAD/Whisper pipeline.
    def _callback(indata, outdata, frames, time_info, status):  # noqa: ARG001
        if status:
            # NOTE: don't print() — the audio thread is real-time. We
            # could surface this via a flag, but it's usually benign
            # (input/output overflow) so we ignore it silently.
            pass

        # 1) Speakers: pull next chunk from TTS playback buffer.
        tts_samples = tts.pull(frames)            # shape (frames,) int16
        outdata[:, 0] = tts_samples

        # 2) AEC: subtract speaker echo from mic input — but ONLY when
        # the speakers are actually emitting something. Running AEC on a
        # zero "far" signal causes Speex's adaptive filter to drift, so
        # the first 1-2 seconds after the far signal stops (eg. an
        # abrupt tts.stop() from the kill switch, while the mic still
        # has reverb tail) come back distorted. Skipping AEC on silent
        # far blocks keeps the filter state stable.
        mic_samples = indata[:, 0]
        far_block_rms = float(
            np.sqrt(np.mean(tts_samples.astype(np.int32) ** 2))
        )
        if aec is not None and frames == aec.frame_size and far_block_rms > 100:
            try:
                cleaned_bytes = aec.process(
                    mic_samples.tobytes(), tts_samples.tobytes()
                )
                cleaned = np.frombuffer(cleaned_bytes, dtype=np.int16)
            except Exception:  # noqa: BLE001
                # Falls through to raw mic if AEC blows up unexpectedly.
                cleaned = mic_samples
        else:
            cleaned = mic_samples

        # 3) Level gate (with hangover): while speakers play TTS audio,
        # the mic mostly captures attenuated echo. Gate it out unless
        # the cleaned mic rises clearly above the echo floor. Once it
        # does we hold the gate open for GATE_HANGOVER_S so leading
        # consonants and trailing tails of a word aren't sliced off.
        out_rms_block = float(
            np.sqrt(np.mean(tts_samples.astype(np.int32) ** 2))
        )
        if out_rms_block > 300:  # speakers actively playing
            clean_rms_block = float(
                np.sqrt(np.mean(cleaned.astype(np.int32) ** 2))
            )
            gate_thresh = max(
                USER_VOICE_RMS_GATE,
                USER_VOICE_GATE_OUT_FACTOR * out_rms_block,
            )
            if clean_rms_block > gate_thresh:
                diag_state["gate_open_left"] = GATE_HANGOVER_BLOCKS
            if diag_state["gate_open_left"] > 0:
                diag_state["gate_open_left"] -= 1
                # pass cleaned through unchanged
            else:
                cleaned = np.zeros_like(cleaned)
        else:
            # Speakers silent — reset hangover so we don't carry it over.
            diag_state["gate_open_left"] = 0

        # 3) Diagnostics — log every 1 s, but only when TTS is actively
        # playing (otherwise nothing interesting to see).
        in_rms = float(np.sqrt(np.mean(mic_samples.astype(np.int32) ** 2)))
        out_rms = float(np.sqrt(np.mean(tts_samples.astype(np.int32) ** 2)))
        clean_rms = float(np.sqrt(np.mean(cleaned.astype(np.int32) ** 2)))
        diag_state["in_acc"] += in_rms
        diag_state["out_acc"] += out_rms
        diag_state["clean_acc"] += clean_rms
        diag_state["blocks"] += 1
        if diag_state["blocks"] >= diag_every:
            avg_in = diag_state["in_acc"] / diag_state["blocks"]
            avg_out = diag_state["out_acc"] / diag_state["blocks"]
            avg_clean = diag_state["clean_acc"] / diag_state["blocks"]
            # Only print when speakers are actually playing — otherwise
            # we'd spam the log with idle measurements.
            if avg_out > 50:
                reduction = (
                    20 * np.log10(avg_clean / avg_in) if avg_in > 1 else 0
                )
                print(
                    f"[AEC] in_rms={avg_in:6.0f}  out_rms={avg_out:6.0f}  "
                    f"clean_rms={avg_clean:6.0f}  reduction={reduction:+.1f}dB"
                )
            diag_state["blocks"] = 0
            diag_state["in_acc"] = 0.0
            diag_state["out_acc"] = 0.0
            diag_state["clean_acc"] = 0.0

        # 4) Hand the cleaned audio to the main loop.
        audio_q.put(cleaned.copy())

    # Rolling pre-roll so we don't clip the first phoneme of "jarvis".
    pre_roll: "collections.deque" = collections.deque(maxlen=PRE_ROLL_BLOCKS)

    in_speech = False
    speech_seed = 0     # consecutive above-threshold blocks before in_speech latches
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

    # Brain work runs in a worker thread so the main loop can keep
    # pumping audio while JARVIS thinks. brain_cancel is set by the
    # module-level interrupt() helper (POST /interrupt or the
    # Cmd+Shift+J hotkey) — the worker thread checks it after
    # brain.reply() returns and discards the reply if requested.
    brain_thread: threading.Thread | None = None
    brain_cancel = threading.Event()
    global _brain_cancel_ref
    _brain_cancel_ref = brain_cancel

    def _brain_work(cmd: str, cancel_evt: threading.Event) -> None:
        """Run one user command through Claude.

        Streaming pipeline: brain.reply() now opens a streaming
        Anthropic call and pushes each completed sentence into
        ``tts.speak()`` AND a ``jarvis_partial`` event in real time —
        the first sentence reaches the speakers ~300-500 ms after
        token-1, instead of after the whole turn lands. By the time
        brain.reply() returns we've ALREADY spoken (or are still
        speaking) the answer. Calling tts.speak(reply) again here
        would re-speak the entire response, so we deliberately don't.

        We still emit a single ``jarvis_reply`` event at the end so
        the HUD can finalise its in-progress bubble with the full
        assembled text (useful for chat-only clients that aren't
        watching ``jarvis_partial`` yet, and for the chat history).
        """
        t_brain = time.monotonic()
        try:
            reply = brain.reply(session, cmd)
        except Exception as exc:  # noqa: BLE001 — surface to user
            reply = f"Entschuldige, etwas ist schiefgelaufen: {exc}"
            # The streaming path never produced any audio for this
            # turn, so we DO need to speak the error message here.
            tts.speak(reply)
        print(f"[TIMING] brain.reply: {(time.monotonic() - t_brain)*1000:.0f}ms")
        if cancel_evt.is_set():
            print(f"[JARVIS] (cancelled; discarding reply: {reply[:60]!r}…)")
            return
        print(f"[JARVIS] {reply}")
        _emit_jarvis_reply(reply)
        # No tts.speak(reply) here on purpose — the brain streamed it
        # sentence-by-sentence during the model call. The "speaking"
        # voice_state event is fired by the main loop's per-tick
        # heartbeat as soon as tts_busy_now flips True.

    # Track the previous tick's busy state so we can react to the idle→busy
    # transition (the moment TTS starts) for buffer hygiene.
    last_busy = False

    try:
        with sd.Stream(
            samplerate=SAMPLE_RATE,
            channels=(1, 1),         # mono in, mono out
            dtype="int16",
            blocksize=BLOCK_SIZE,    # 10 ms — matches AEC frame size
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

                # Optional live RMS log for tuning the thresholds. We
                # accumulate per-block readings and print once a second
                # so the line rate stays sane (we get ~100 blocks/sec).
                if _MIC_DEBUG:
                    _dbg = locals().setdefault("_mic_dbg", {"acc": 0.0, "peak": 0.0, "n": 0, "t": time.monotonic()})
                    _dbg["acc"] += rms
                    _dbg["peak"] = max(_dbg["peak"], rms)
                    _dbg["n"] += 1
                    if time.monotonic() - _dbg["t"] >= 1.0:
                        avg = _dbg["acc"] / _dbg["n"] if _dbg["n"] else 0.0
                        print(f"[MIC] avg_rms={avg:6.0f}  peak={_dbg['peak']:6.0f}  "
                              f"thresholds: speech>{SPEECH_RMS_THRESHOLD} silence<{SILENCE_RMS_THRESHOLD}")
                        _dbg["acc"] = 0.0
                        _dbg["peak"] = 0.0
                        _dbg["n"] = 0
                        _dbg["t"] = time.monotonic()

                brain_alive_now = brain_thread is not None and brain_thread.is_alive()
                tts_busy_now = not tts.is_idle()
                busy_now = brain_alive_now or tts_busy_now
                last_busy = busy_now

                # Per-tick state heartbeat for the HUD. _emit_state dedups
                # so this only actually publishes on a real transition,
                # but emitting every iteration means we cover paths that
                # don't have explicit emit calls (e.g. the canned
                # "Ja?" / "Alles klar" / kill-switch TTS responses,
                # or a transcribe that didn't match the wake word).
                if in_speech:
                    pass                        # user is mid-utterance
                elif brain_alive_now:
                    _emit_state("thinking")
                elif tts_busy_now:
                    _emit_state("speaking")
                else:
                    _emit_state("listening")

                if busy_now:
                    # While JARVIS is talking or thinking, completely
                    # ignore the mic: the speakers leak into it and
                    # would otherwise build up 15-second segments of
                    # JARVIS's own voice. No barge-in — both the
                    # energy-burst and Whisper-rolling paths were
                    # cancelling replies on TTS echo, so the user has
                    # to wait for JARVIS to finish before speaking
                    # again. (Kill-switch and stop phrases still
                    # work — they go through the normal segment-closed
                    # transcribe path as soon as JARVIS goes idle.)
                    in_speech = False
                    speech_seed = 0
                    buf.clear()
                    silence_blocks = 0
                    pre_roll.clear()
                    continue

                if not in_speech:
                    pre_roll.append(block)
                    if rms > SPEECH_RMS_THRESHOLD:
                        speech_seed += 1
                        if speech_seed >= SPEECH_SEED_BLOCKS:
                            in_speech = True
                            speech_seed = 0
                            buf = list(pre_roll)        # keep the pre-roll
                            buf.append(block)
                            silence_blocks = 0
                            print("[MIC] speech started")
                    else:
                        # A single quiet block resets the seed run — that's
                        # what filters keyboard clicks (10-30 ms peaks with
                        # long quiet gaps) from triggering speech onset.
                        speech_seed = 0
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

                segment_s = BLOCK_S * len(buf)
                print(f"[MIC] segment closed ({segment_s:.1f}s) — transcribing…")
                _emit_state("transcribing")
                pcm_block = np.concatenate(buf)
                # Pre-STT gain: Apple Speech.framework rejects quiet
                # audio with "recognition error: Retry" even when our
                # VAD considered the segment speech. We normalise the
                # segment toward a known target peak (60 % of int16
                # full-scale) so Apple sees a hearty signal. Cap the
                # gain at 12× so we don't amplify hiss into a hurricane
                # on a hot mic. Override via JARVIS_STT_TARGET_PEAK
                # (0 disables) for tuning.
                target_peak = int(os.getenv("JARVIS_STT_TARGET_PEAK", "19660"))
                peak = int(np.max(np.abs(pcm_block))) or 1
                if target_peak > 0 and peak < target_peak:
                    gain = min(12.0, target_peak / peak)
                    boosted = (pcm_block.astype(np.int32) * gain).clip(
                        -32768, 32767
                    ).astype(np.int16)
                    print(f"[MIC] pre-STT gain ×{gain:.1f} (peak {peak} → "
                          f"{int(np.max(np.abs(boosted)))})")
                    pcm_block = boosted
                pcm = pcm_block.tobytes()
                wav = stt._pcm_to_wav(pcm, sample_rate=SAMPLE_RATE)  # noqa: SLF001
                buf = []
                silence_blocks = 0
                pre_roll.clear()

                t_e2e = time.monotonic()
                try:
                    transcript = stt.transcribe(wav, sample_rate=SAMPLE_RATE)
                except Exception as exc:  # noqa: BLE001
                    print(f"[JARVIS] transcribe error: {exc}")
                    transcript = ""
                print(f"[TIMING] transcribe end-to-end: "
                      f"{(time.monotonic() - t_e2e)*1000:.0f}ms")

                if not transcript:
                    continue
                if stt.looks_like_hallucination(transcript):
                    print(f"[JARVIS] (whisper hallucination, ignored): {transcript!r}")
                    continue

                # Snapshot busy state — used by several branches below.
                brain_alive = brain_thread is not None and brain_thread.is_alive()
                busy = brain_alive or (not tts.is_idle())

                # ─── KILL SWITCH / RESUME ──────────────────────────────────
                # "Jarvis halt" / "Notaus" → disarm the whole mac_control
                # surface. Distinct from plain stop phrases (which only
                # barge in) — kill_switch stays set until "Jarvis weiter".
                if stt.is_kill_switch_phrase(transcript):
                    from .mac_control import kill_switch as _ks
                    _ks.trigger(f"voice: {transcript!r}")
                    brain_cancel.set()
                    tts.stop()
                    tts.speak("Kill-Switch aktiv. Sag 'Jarvis weiter' zum Fortfahren.")
                    _drain(audio_q)
                    continue
                if stt.is_resume_phrase(transcript):
                    from .mac_control import kill_switch as _ks
                    if _ks.is_set():
                        _ks.resume()
                        tts.speak("Wieder aktiv.")
                    continue

                # ─── STOP / END PHRASE always wins ─────────────────────────
                # "Stop", "Halt", "Warte", … cut JARVIS off mid-flight.
                # End phrases ("okay das war's", "tschüss", …) also exit
                # follow-up mode. starts_with_stop_phrase catches Whisper
                # variants (stoppt, stoppen, "stop hör auf", "ok halt").
                if stt.is_stop_phrase(transcript) or stt.starts_with_stop_phrase(transcript):
                    is_end = stt.is_end_phrase(transcript)
                    if busy:
                        print(f"[JARVIS] stop heard mid-flight: {transcript!r}")
                        brain_cancel.set()
                        tts.stop()
                    else:
                        print(f"[JARVIS] stop heard while idle: {transcript!r}")
                    if is_end:
                        followup_until = 0.0
                        if not busy:
                            # Quietly acknowledge the deliberate goodbye.
                            tts.speak("Alles klar. Bis später.")
                    _drain(audio_q)
                    continue

                # ─── BUSY safety net ───────────────────────────────────────
                # The top-of-loop busy_now guard already drops audio
                # while JARVIS is talking/thinking, so reaching this
                # check usually means the brain became busy *during*
                # transcription (eg. a parallel /chat HTTP request).
                # Drop the transcript so we don't stack work.
                if busy:
                    print(f"[JARVIS] busy — ignoring: {transcript!r}")
                    continue

                # ─── Follow-up window check ────────────────────────────────
                now = time.monotonic()
                in_followup = followup_window > 0 and now < followup_until
                if followup_until and not in_followup:
                    print("[JARVIS] follow-up window lapsed — listening for "
                          f"wake word again ({settings.WAKE_WORD!r})")
                    followup_until = 0.0

                # ─── Command extraction ────────────────────────────────────
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
                        if followup_window > 0:
                            followup_until = time.monotonic() + followup_window
                        continue

                if not command.strip():
                    # Empty-after-strip — refresh the window and listen on.
                    if followup_window > 0:
                        followup_until = time.monotonic() + followup_window
                    continue

                # ─── Dispatch brain in worker thread ───────────────────────
                # We do NOT block here — the main loop keeps pumping audio
                # so the user can say "Stop" mid-reply.
                print(f"[CLIENT: MacBook] [YOU·voice] {command}")
                _emit_user_message(command)
                _emit_state("thinking")
                brain_cancel.clear()
                brain_thread = threading.Thread(
                    target=_brain_work,
                    args=(command, brain_cancel),
                    name="jarvis-brain",
                    daemon=True,
                )
                brain_thread.start()

                # Extend the follow-up window optimistically.
                if followup_window > 0:
                    followup_until = time.monotonic() + followup_window
    finally:
        if brain_thread is not None and brain_thread.is_alive():
            brain_cancel.set()
            tts.stop()
            brain_thread.join(timeout=2.0)
        # Clear the module-level refs so a stale interrupt() call after
        # shutdown can't dereference garbage state. The lock prevents a
        # race with a concurrent /interrupt request mid-shutdown.
        with _interrupt_lock:
            globals()["_brain_cancel_ref"] = None
            globals()["_tts_ref"] = None
            globals()["_audio_q_ref"] = None
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
