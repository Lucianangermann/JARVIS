"""Acoustic echo cancellation via Speex DSP.

The voice loop runs the mic and the speakers through a single full-duplex
``sd.Stream``. On each 10 ms audio tick we get:

    * ``indata``  — what the mic just heard (user voice + speaker echo)
    * ``outdata`` — what we're about to send to the speakers (TTS audio)

We hand both to Speex's adaptive echo filter, which subtracts the
estimated room response of the speaker signal from the mic signal. The
result is the user's voice plus room noise, with the JARVIS-from-speakers
component largely removed — Whisper can then transcribe the user even
while JARVIS is talking, which is exactly what barge-in needs.

The filter adapts continuously (NLMS-style), so the first few seconds
of TTS may bleed through until it locks; after that it's effectively
silent on the AEC'd signal.

API
---
``Aec(frame_size, filter_length, sample_rate)`` — one instance per stream.
``process(near_bytes, far_bytes) -> bytes`` — both inputs are int16 PCM
mono of exactly ``frame_size * 2`` bytes; output is the cancelled mic
signal in the same format.
"""
from __future__ import annotations

import threading


class Aec:
    """Tiny thread-safe wrapper around ``speexdsp.EchoCanceller``."""

    def __init__(
        self,
        frame_size: int = 160,         # 10 ms at 16 kHz
        filter_length: int = 4800,     # ≈300 ms tail — covers most macOS
                                        # speaker→mic system latencies. The
                                        # Python wrapper of speexdsp doesn't
                                        # expose the Preprocessor, so we
                                        # lean on a long filter alone.
        sample_rate: int = 16_000,
    ) -> None:
        # Import here so the rest of JARVIS can run if speexdsp is not
        # installed — voice_loop checks Aec for None.
        from speexdsp import EchoCanceller

        self.frame_size = frame_size
        self.frame_bytes = frame_size * 2  # int16 = 2 bytes/sample
        self._ec = EchoCanceller.create(frame_size, filter_length, sample_rate)
        self._lock = threading.Lock()

    def process(self, near_bytes: bytes, far_bytes: bytes) -> bytes:
        """Cancel echo from one 10 ms frame.

        ``near_bytes`` = mic input (with echo)
        ``far_bytes``  = speaker reference (what we just sent out)
        Returns the cancelled mic signal as int16 PCM bytes.
        """
        if len(near_bytes) != self.frame_bytes or len(far_bytes) != self.frame_bytes:
            # Wrong-size frames would crash the C code. Be loud about it.
            raise ValueError(
                f"AEC expects {self.frame_bytes}-byte frames "
                f"(near={len(near_bytes)}, far={len(far_bytes)})"
            )
        with self._lock:
            return self._ec.process(near_bytes, far_bytes)


def try_create() -> "Aec | None":
    """Return an Aec instance or None if speexdsp isn't installed."""
    try:
        return Aec()
    except Exception as exc:  # noqa: BLE001
        print(f"[JARVIS] AEC unavailable ({exc}); barge-in will be unreliable.")
        return None
