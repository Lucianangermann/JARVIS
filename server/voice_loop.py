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
SPEECH_RMS_THRESHOLD = 900
SILENCE_RMS_THRESHOLD = 400

# How long sustained silence ends a segment, and the minimum / maximum
# segment length we'll transcribe at all.
SILENCE_HANG_S = 0.8                   # 800 ms of silence → segment done
MIN_SPEECH_S = 0.5                     # discard blips shorter than this
MAX_SPEECH_S = 15.0                    # cap utterances so Whisper stays snappy

# Tiny prefix preserved before the first speech block — captures the
# attack of the wake word that the threshold detector just missed.
PRE_ROLL_S = 0.4
PRE_ROLL_BLOCKS = int(PRE_ROLL_S / BLOCK_S)

# Barge-in: while JARVIS is currently speaking or thinking, the VAD's
# silence-bounded segments don't fire (no silence — mic hears the
# speakers). So we transcribe a rolling window every BARGE_IN_INTERVAL_S
# and check for stop phrases. The rolling buffer is BARGE_IN_WINDOW_S
# long; we only trigger Whisper if the last bit looks like user speech
# (RMS over BARGE_IN_RMS_FLOOR) to avoid wasting CPU on idle background.
# Shorter window + faster cadence = more chances to catch a brief "Stop"
# uttered between TTS sentences.
BARGE_IN_WINDOW_S = 1.0
BARGE_IN_INTERVAL_S = 0.35
BARGE_IN_RMS_FLOOR = 500        # last-300ms RMS gate (relaxed)
BARGE_IN_RMS_TAIL_S = 0.3
BARGE_IN_WINDOW_BLOCKS = int(BARGE_IN_WINDOW_S / BLOCK_S)

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

# Fast interrupt: while JARVIS is busy, the user's voice (already
# AEC'd + gated) must stay above FAST_INTERRUPT_RMS for at least
# FAST_INTERRUPT_MIN_S of *consecutive* 10 ms blocks AND contain a
# sub-run of FAST_INTERRUPT_MIN_RUN_BLOCKS uninterrupted hits.
#
# - The streak length rejects isolated keyboard clicks (each keystroke
#   is a 10-30 ms RMS spike followed by a quiet gap).
# - The uninterrupted-run requirement rejects *bursts* of clicks (fast
#   typing or a double-click can chain enough hits + tolerated misses
#   to fake a long streak, but no single click sustains 40 ms+ of
#   continuous energy the way a vowel does).
# - The miss-tolerance handles natural vocal RMS dips inside a single
#   sustained vowel (otherwise pitch fluctuations could reset the run
#   half-way through a clean "Stop").
# - The threshold sits clearly above typical AEC residual (~50-300
#   during pure JARVIS) and below user voice mid-vowel (~700-1500).
FAST_INTERRUPT_RMS = 500
FAST_INTERRUPT_MIN_S = 0.13
FAST_INTERRUPT_MIN_BLOCKS = int(FAST_INTERRUPT_MIN_S / BLOCK_S)
FAST_INTERRUPT_MISS_TOLERANCE = 2   # allow 2 sub-threshold blocks (20ms dip)
# Minimum uninterrupted run of above-threshold blocks anywhere in the
# streak. 30 ms is the lower bound for a steady-state vowel — quiet
# voices still cross it easily, while keyboard/mouse clicks (1-2 block
# transients) cannot, even when chained into a burst.
FAST_INTERRUPT_MIN_RUN_BLOCKS = 3

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
    # fall back to plain mic capture (no barge-in over speakers).
    aec = aec_mod.try_create()
    if aec is not None:
        print(f"[JARVIS] AEC enabled — Speex echo canceller "
              f"(frame={aec.frame_size}, sr={SAMPLE_RATE})")
    else:
        print("[JARVIS] AEC OFF — barge-in won't work over laptop speakers; "
              "use headphones for reliable interrupt.")

    audio_q: queue.Queue = queue.Queue()

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
        # the first 1-2 seconds AFTER a BARGE-IN (when JARVIS was just
        # cut off and far has gone to zero but mic still has reverb
        # tail) come back distorted. Skipping AEC on silent far blocks
        # keeps the filter state stable.
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

    # Fast-interrupt streak tracker.
    #   `count`      — blocks since the streak started (hits + tolerated misses)
    #   `misses`     — consecutive sub-threshold blocks in the current dip
    #   `run`        — current uninterrupted hit run (resets on any miss)
    #   `max_run`    — longest uninterrupted hit run seen in this streak
    # The streak fires only when `count >= MIN_BLOCKS` AND
    # `max_run >= MIN_RUN_BLOCKS`. That combination passes a clean vowel
    # but rejects keyboard/mouse bursts (whose runs stay at 1-3 blocks
    # even if the overall streak length looks long enough).
    fast_int_count = 0
    fast_int_misses = 0
    fast_int_run = 0
    fast_int_max_run = 0

    # Brain work runs in a worker thread so the main loop can keep
    # processing audio (and hear "Stop" / "Halt") while JARVIS thinks
    # and speaks. brain_cancel is set when the user barges in — the
    # thread will then discard its reply instead of sending it to TTS.
    brain_thread: threading.Thread | None = None
    brain_cancel = threading.Event()

    def _brain_work(cmd: str, cancel_evt: threading.Event) -> None:
        try:
            reply = brain.reply(session, cmd)
        except Exception as exc:  # noqa: BLE001 — surface to user
            reply = f"Entschuldige, etwas ist schiefgelaufen: {exc}"
        if cancel_evt.is_set():
            print(f"[JARVIS] (cancelled; discarding reply: {reply[:60]!r}…)")
            return
        print(f"[JARVIS] {reply}")
        tts.speak(reply)

    # Barge-in monitor: while JARVIS is busy, periodically transcribe a
    # rolling 1.5 s buffer and look for stop phrases. Runs in a daemon
    # thread so it doesn't slow the main loop down. A single global
    # transcribe_lock prevents *any* two Whisper calls from overlapping
    # (avoids the macOS OMP "forking while parallel region is active"
    # warning and the silent failures it can mask).
    barge_in_buffer: "collections.deque" = collections.deque(
        maxlen=BARGE_IN_WINDOW_BLOCKS
    )
    transcribe_lock = threading.Lock()
    last_barge_check_t = 0.0

    def _barge_in_check(blocks_snapshot: list) -> None:
        if not transcribe_lock.acquire(blocking=False):
            return  # main loop is mid-transcription — skip this round
        try:
            pcm = np.concatenate(blocks_snapshot).tobytes()
            wav = stt._pcm_to_wav(pcm, sample_rate=SAMPLE_RATE)  # noqa: SLF001
            try:
                transcript = stt.transcribe(wav, sample_rate=SAMPLE_RATE)
            except Exception as exc:  # noqa: BLE001
                print(f"[JARVIS] barge-in transcribe error: {exc}")
                return
            if not transcript:
                return

            # Three ways to interrupt JARVIS mid-flight:
            #   1. A standalone stop phrase exactly ("Stop", "Halt", …)
            #   2. An utterance *starting* with a stop word — catches
            #      Whisper variants like "stoppt", "stop hör auf",
            #      "okay stop", "halt mal", …
            #   3. The wake word at the START of the transcript — user
            #      is talking *over* JARVIS. JARVIS rarely starts his
            #      own sentences with "Jarvis".
            if (
                stt.is_stop_phrase(transcript)
                or stt.starts_with_stop_phrase(transcript)
                or stt.starts_with_wake_word(transcript)
            ):
                print(f"[JARVIS] BARGE-IN: {transcript!r}")
                brain_cancel.set()
                tts.stop()
                barge_in_buffer.clear()
            # Otherwise: most likely JARVIS-on-speakers, just ignore
            # without logging — keeps the console quiet.
        finally:
            transcribe_lock.release()

    # Track the previous tick's busy state so we can react to the idle→busy
    # transition (the moment TTS starts). See the buffer-clear below.
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

                # Feed the barge-in rolling buffer regardless of state.
                barge_in_buffer.append(block)

                brain_alive_now = brain_thread is not None and brain_thread.is_alive()
                tts_busy_now = not tts.is_idle()
                busy_now = brain_alive_now or tts_busy_now

                # idle → busy transition: clear the barge-in rolling buffer
                # so audio captured *before* JARVIS started talking can't
                # trigger a false barge-in. Common case this fixes: the user
                # said "Jarvis, was kannst du …", the tail of that utterance
                # is still in the rolling buffer when TTS starts replying,
                # and the barge-in check sees "starts with wake word" → cancels
                # the reply we just generated.
                if busy_now and not last_busy:
                    barge_in_buffer.clear()
                last_busy = busy_now

                if busy_now:
                    # While JARVIS is talking or thinking, completely
                    # ignore the regular VAD: the mic hears the speakers
                    # and would otherwise build up 15-second segments of
                    # JARVIS's own voice. Reset state so the moment we
                    # go idle we listen fresh.
                    in_speech = False
                    buf.clear()
                    silence_blocks = 0
                    pre_roll.clear()

                    # FAST INTERRUPT: count blocks above threshold,
                    # tolerating up to FAST_INTERRUPT_MISS_TOLERANCE
                    # short dips in a row. We also track the longest
                    # uninterrupted hit run — fires only when *both* the
                    # total streak is long enough AND a single vowel-like
                    # run sat above the threshold. Keystrokes & mouse
                    # clicks have runs of 1-3 blocks, so even a burst of
                    # them won't satisfy the run requirement.
                    if rms > FAST_INTERRUPT_RMS:
                        fast_int_count += 1
                        fast_int_misses = 0
                        fast_int_run += 1
                        if fast_int_run > fast_int_max_run:
                            fast_int_max_run = fast_int_run
                    elif fast_int_count > 0:
                        fast_int_misses += 1
                        fast_int_run = 0
                        if fast_int_misses > FAST_INTERRUPT_MISS_TOLERANCE:
                            fast_int_count = 0
                            fast_int_misses = 0
                            fast_int_max_run = 0
                        else:
                            fast_int_count += 1  # still inside the streak
                    if (
                        fast_int_count >= FAST_INTERRUPT_MIN_BLOCKS
                        and fast_int_max_run >= FAST_INTERRUPT_MIN_RUN_BLOCKS
                    ):
                        print(f"[JARVIS] FAST INTERRUPT (energy burst, "
                              f"rms~{rms:.0f}, "
                              f"~{fast_int_count * BLOCK_S * 1000:.0f}ms, "
                              f"run={fast_int_max_run * BLOCK_S * 1000:.0f}ms)")
                        brain_cancel.set()
                        tts.stop()
                        fast_int_count = 0
                        fast_int_misses = 0
                        fast_int_run = 0
                        fast_int_max_run = 0
                        barge_in_buffer.clear()
                        _drain(audio_q)
                        continue

                    # Slower Whisper-based barge-in for quieter
                    # utterances that don't trigger the fast path
                    # (e.g. whispered "stop", or "Jarvis, …" prefix).
                    now_t = time.monotonic()
                    if (
                        len(barge_in_buffer) == BARGE_IN_WINDOW_BLOCKS
                        and (now_t - last_barge_check_t) >= BARGE_IN_INTERVAL_S
                    ):
                        last_barge_check_t = now_t
                        snapshot = list(barge_in_buffer)
                        threading.Thread(
                            target=_barge_in_check,
                            args=(snapshot,),
                            name="jarvis-barge",
                            daemon=True,
                        ).start()
                    continue

                # Idle: reset fast-interrupt counters so a stale streak
                # doesn't carry into the next TTS session.
                fast_int_count = 0
                fast_int_misses = 0
                fast_int_run = 0
                fast_int_max_run = 0

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

                # Serialise with any in-flight barge-in transcription to
                # avoid the OMP "forking while parallel region is active"
                # warning + the silent transcription failures it masks.
                with transcribe_lock:
                    try:
                        transcript = stt.transcribe(wav, sample_rate=SAMPLE_RATE)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[JARVIS] transcribe error: {exc}")
                        transcript = ""

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

                # ─── BUSY: ignore everything except stop phrases ───────────
                # While brain is thinking or JARVIS is speaking, the only
                # accepted utterance is a barge-in stop. New commands are
                # ignored so we don't stack work.
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
