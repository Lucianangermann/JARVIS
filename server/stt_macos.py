"""macOS native speech recognition backend.

Wraps Apple's `SFSpeechRecognizer` (Speech.framework) via PyObjC. On
Intel Macs without a Neural Engine it still beats faster-whisper by
~10-20× because Apple's pipeline runs in a hardware-accelerated
daemon (`com.apple.SpeechRecognitionCore.speechrecognitiond`) and
ships pre-warmed model weights.

Typical latency on this project's Intel Haswell test machine:
    JARVIS (1.7 s audio)  →  ~250-500 ms transcribe

Permissions
-----------
First use triggers the macOS speech-recognition consent dialog. Once
the user clicks Allow, the grant persists in the TCC database. To
reset for testing: ``tccutil reset SpeechRecognition``.

If the calling Python binary doesn't have the
``NSSpeechRecognitionUsageDescription`` Info.plist key (most CLI
pythons don't), macOS still shows the dialog with a generic message;
the grant is recorded against the parent Terminal / Electron bundle.

Privacy
-------
We force ``requiresOnDeviceRecognition`` whenever the recognizer
reports that it's supported, so the audio never leaves the machine.
On older macOS (or unsupported locales) this silently falls back to
Apple's servers — log says which path is active.
"""
from __future__ import annotations

import threading
import time
from typing import Any

# Probe imports. If PyObjC + Speech aren't available (non-macOS, or
# pyobjc-framework-Speech not installed), the module advertises
# is_available()==False and the caller picks another backend.
try:
    from Foundation import (  # type: ignore[import-not-found]
        NSDate,
        NSLocale,
        NSRunLoop,
        NSURL,
    )
    from Speech import (  # type: ignore[import-not-found]
        SFSpeechRecognizer,
        SFSpeechURLRecognitionRequest,
    )

    _IMPORT_OK = True
    _IMPORT_ERR = ""
except Exception as _exc:  # noqa: BLE001
    _IMPORT_OK = False
    _IMPORT_ERR = repr(_exc)


# SFSpeechRecognizerAuthorizationStatus values. Hard-coded so we don't
# need to import the constants (they're plain ints in the framework).
_STATUS_NOT_DETERMINED = 0
_STATUS_DENIED         = 1
_STATUS_RESTRICTED     = 2
_STATUS_AUTHORIZED     = 3

_STATUS_NAMES = {
    _STATUS_NOT_DETERMINED: "notDetermined",
    _STATUS_DENIED:         "denied",
    _STATUS_RESTRICTED:     "restricted",
    _STATUS_AUTHORIZED:     "authorized",
}


# Cached recognizer keyed by locale. SFSpeechRecognizer initialisation
# touches the daemon and isn't free; reuse one per locale per process.
_recognizers: dict[str, Any] = {}
_recognizers_lock = threading.Lock()


def is_available() -> bool:
    """Are the imports usable on this OS / install? Cheap, no daemon hit."""
    return _IMPORT_OK


def import_error() -> str:
    return _IMPORT_ERR


def authorization_status() -> int:
    """Synchronous status check — no permission prompt, no runloop pump."""
    if not _IMPORT_OK:
        return _STATUS_DENIED
    return int(SFSpeechRecognizer.authorizationStatus())


def authorization_status_name() -> str:
    return _STATUS_NAMES.get(authorization_status(), "unknown")


def _pump_runloop_until(event: threading.Event, timeout_s: float) -> bool:
    """Spin the current thread's NSRunLoop until `event` fires or we
    time out. Needed because requestAuthorization_'s completion is
    delivered to the main queue — without a running loop on *some*
    thread, the callback never fires."""
    loop = NSRunLoop.currentRunLoop()
    deadline = time.monotonic() + timeout_s
    while not event.is_set() and time.monotonic() < deadline:
        loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
    return event.is_set()


def request_permission(timeout_s: float = 30.0) -> int:
    """Trigger the macOS consent dialog if status is notDetermined,
    otherwise return the cached status synchronously.

    Returns one of the _STATUS_* constants. Blocks up to `timeout_s`
    while pumping the runloop so the framework can deliver its
    completion block to us.
    """
    if not _IMPORT_OK:
        return _STATUS_DENIED

    status = authorization_status()
    if status != _STATUS_NOT_DETERMINED:
        return status

    event = threading.Event()
    holder: list[int | None] = [None]

    def _cb(new_status):
        holder[0] = int(new_status)
        event.set()

    SFSpeechRecognizer.requestAuthorization_(_cb)
    _pump_runloop_until(event, timeout_s)
    return holder[0] if holder[0] is not None else _STATUS_NOT_DETERMINED


def _ensure_recognizer(locale: str):
    """Build (and cache) an SFSpeechRecognizer for `locale`. Raises
    RuntimeError if the recognizer isn't available for this locale
    (e.g. user hasn't downloaded the on-device language pack and
    server fallback is off)."""
    with _recognizers_lock:
        cached = _recognizers.get(locale)
        if cached is not None:
            return cached

        loc = NSLocale.localeWithLocaleIdentifier_(locale)
        rec = SFSpeechRecognizer.alloc().initWithLocale_(loc)
        if rec is None:
            raise RuntimeError(f"SFSpeechRecognizer init returned nil for locale {locale!r}")
        if not rec.isAvailable():
            raise RuntimeError(
                f"SFSpeechRecognizer not available for locale {locale!r} "
                "(download the language pack in System Settings → Keyboard "
                "→ Dictation, or switch STT_LOCALE)."
            )
        _recognizers[locale] = rec
        return rec


def transcribe_wav(wav_path: str, locale: str = "de-DE",
                   timeout_s: float = 10.0,
                   on_device_only: bool = False) -> str:
    """Transcribe a WAV file via Speech.framework. Synchronous.

    Returns lowercase whitespace-collapsed text. Empty string if the
    framework reports a final result with no transcription (silence /
    unintelligible audio).

    on_device_only=False (the default) lets Apple use server fallback
    when the on-device DE model isn't installed — without that fallback
    the recognition silently hangs on machines that haven't downloaded
    the locale pack. Set True for strict-privacy setups where you've
    verified the language pack is present.
    """
    if not _IMPORT_OK:
        raise RuntimeError(f"Speech.framework unavailable: {_IMPORT_ERR}")

    status = authorization_status()
    if status != _STATUS_AUTHORIZED:
        raise PermissionError(
            f"speech recognition not authorized (status={_STATUS_NAMES.get(status, status)}); "
            "grant via System Settings → Privacy & Security → Speech Recognition."
        )

    recognizer = _ensure_recognizer(locale)

    # One-time diagnostic. Logs only on the first call per (locale, recognizer)
    # so we can see in the server console what mode the daemon picked.
    if not getattr(recognizer, "_jarvis_logged", False):
        try:
            on_dev = bool(recognizer.supportsOnDeviceRecognition())
        except Exception:  # noqa: BLE001 — older OS
            on_dev = False
        print(f"[JARVIS] Speech recognizer ready: locale={locale} "
              f"available={bool(recognizer.isAvailable())} "
              f"on_device_supported={on_dev} "
              f"on_device_forced={on_device_only}")
        # Stash a flag right on the recognizer object so we only log once.
        try:
            recognizer._jarvis_logged = True  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    url = NSURL.fileURLWithPath_(wav_path)
    request = SFSpeechURLRecognitionRequest.alloc().initWithURL_(url)
    request.setShouldReportPartialResults_(False)
    if on_device_only:
        try:
            if recognizer.supportsOnDeviceRecognition():
                request.setRequiresOnDeviceRecognition_(True)
        except Exception:  # noqa: BLE001 — older OS, attribute missing
            pass

    event = threading.Event()
    result_holder: list[str | None] = [None]
    error_holder: list[Any] = [None]

    def _handler(result, error):
        if error is not None:
            error_holder[0] = error
            event.set()
            return
        if result is None:
            return
        if result.isFinal():
            best = result.bestTranscription()
            result_holder[0] = (
                str(best.formattedString()) if best is not None else ""
            )
            event.set()

    task = recognizer.recognitionTaskWithRequest_resultHandler_(
        request, _handler,
    )

    # Pump the local NSRunLoop while waiting. PyObjC's bridge can deliver
    # GCD-scheduled blocks via the runloop on the calling thread; without
    # an active runloop, Python may never see the completion. Costs
    # nothing if the block arrives on a real dispatch queue — we just
    # idle a fraction of a millisecond per pump.
    if not _pump_runloop_until(event, timeout_s):
        try:
            task.cancel()
        except Exception:  # noqa: BLE001
            pass
        raise TimeoutError(f"Speech.framework recognition timed out after {timeout_s}s")

    if error_holder[0] is not None:
        err = error_holder[0]
        try:
            msg = str(err.localizedDescription())
        except Exception:  # noqa: BLE001
            msg = str(err)
        raise RuntimeError(f"Speech.framework recognition error: {msg}")

    text = (result_holder[0] or "").strip()
    return " ".join(text.lower().split())
