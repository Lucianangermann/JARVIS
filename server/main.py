"""FastAPI entry point for the JARVIS server.

Endpoints
---------
GET  /              — health check (no auth).
POST /chat          — single-shot text turn. JSON: {"text": "..."}.
POST /audio         — upload a WAV; we transcribe + run a turn.
WS   /ws            — bidirectional text chat over a single socket.
                       Connect with ?token=<JARVIS_AUTH_TOKEN> (or an
                       Authorization: Bearer header for non-browser clients).
GET  /web           — serves the iPhone-friendly web UI.

Console output
--------------
We tag every line so a single terminal can host the whole demo:
    [JARVIS]            server-side log lines
    [YOU]               text we received from a client
    [CLIENT: web]       client classifier, derived from User-Agent
    [MIC ON]/[MIC OFF]  printed by stt.py whenever audio is captured
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from pathlib import Path
from typing import Any

import secrets

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.security.utils import get_authorization_scheme_param
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import _check_rate, authorize_websocket, require_token
from .brain import Brain
from .config import settings
from . import events
from .intelligence import IntelligenceManager
from .mac_control import dispatcher as mac_dispatcher
from .mac_control import kill_switch as mac_kill_switch

# Vision is optional — mss / opencv may not be installed. The vision
# package's import itself is cheap (subcomponents load lazily), but
# guard anyway so a missing dep on a minimal install doesn't block
# the rest of the server.
try:
    from .vision import VisionManager  # noqa: F401
    _VISION_OK = True
except Exception as _vision_exc:  # noqa: BLE001
    print(f"[JARVIS] vision module unavailable: {_vision_exc}")
    VisionManager = None  # type: ignore[assignment,misc]
    _VISION_OK = False

# Smart Home layer — import directly from the module, not via __init__,
# so the brain's smarthome_tools import doesn't cascade into this.
try:
    from .smarthome.smarthome_manager import SmartHomeManager
    _SMARTHOME_OK = True
except Exception as _smarthome_exc:  # noqa: BLE001
    print(f"[JARVIS] smarthome module unavailable: {_smarthome_exc}")
    SmartHomeManager = None  # type: ignore[assignment,misc]
    _SMARTHOME_OK = False

# stt and tts are optional — the voice stack (whisper, pyttsx3, …) may not
# be installed. The text endpoints work without them.
try:
    from . import tts  # noqa: F401
    from .stt import transcribe
    _VOICE_OK = True
except Exception as _voice_exc:  # noqa: BLE001
    transcribe = None  # type: ignore[assignment]
    tts = None  # type: ignore[assignment]
    _VOICE_OK = False
    _VOICE_ERR = repr(_voice_exc)

WEB_DIR = Path(__file__).resolve().parent.parent / "clients" / "web"
PWA_DIR = Path(__file__).resolve().parent.parent / "clients" / "pwa"


# --- App lifecycle -------------------------------------------------------- #

def _wire_subsystem(app: FastAPI, label: str, factory: Any, *,
                    brain_attr: str | None = None,
                    state_attr: str | None = None, start: bool = True) -> Any:
    """Uniform subsystem wiring: build (best-effort), optionally start(),
    attach to app.state + brain, log. Returns the manager or None. Used for
    the independent layers; subsystems with bespoke cross-bridges
    (security/communication) keep their inline wiring below."""
    try:
        mgr = factory()
        if start and hasattr(mgr, "start"):
            mgr.start()
        if state_attr:
            setattr(app.state, state_attr, mgr)
        if brain_attr:
            setattr(app.state.brain, brain_attr, mgr)
        print(f"[{label}] wired to brain ✓")
        return mgr
    except Exception as exc:  # noqa: BLE001
        print(f"[{label}] init failed: {exc}")
        return None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    print("[JARVIS] starting up")
    print(f"[JARVIS] model={settings.MODEL}")
    # Scheme follows whether uvicorn was launched with --ssl-* flags.
    # run() below reads JARVIS_SSL_CERT/_KEY from .env and passes
    # them through, so the scheme here matches.
    scheme = "https" if settings.JARVIS_SSL_CERT and settings.JARVIS_SSL_KEY else "http"
    base = f"{scheme}://{settings.HOST}:{settings.PORT}"
    print(f"[JARVIS] listening on {base}")
    print(f"[JARVIS] web UI at  {base}/web")
    if PWA_DIR.exists():
        print(f"[JARVIS] PWA at     {base}/app")
        if scheme == "http":
            print("[JARVIS]   ⚠ iPhone PWA install + microphone need HTTPS.")
            print("[JARVIS]   ⚠ Set JARVIS_SSL_CERT / JARVIS_SSL_KEY in .env.")
    if not _VOICE_OK:
        print(f"[JARVIS] voice stack NOT loaded ({_VOICE_ERR}) — /audio disabled,")
        print("[JARVIS] text endpoints still work. Install requirements-voice.txt to enable.")
    app.state.brain = Brain()

    # Capture the running asyncio loop for cross-thread event publishes
    # from voice_loop's thread. Must happen BEFORE the voice thread
    # starts, otherwise its first publish() lands a no-op.
    events.set_loop(asyncio.get_running_loop())

    # Intelligence layer. Best-effort: any boot failure logs and we
    # keep going — brain.reply has a None-check and degrades silently.
    intelligence: IntelligenceManager | None = None
    try:
        intelligence = IntelligenceManager()

        def _briefing_to_users(text: str) -> None:
            """Deliver a scheduled briefing to every connected client:
            speak it on the Mac (if voice stack is loaded) and push
            it to PWA / HUD listeners via the WS event bus."""
            print(f"[INTEL] briefing: {text}")
            try:
                events.publish({"type": "jarvis_reply", "text": text})
            except Exception as exc:  # noqa: BLE001
                print(f"[INTEL] briefing publish failed: {exc}")
            if _VOICE_OK and tts is not None:
                try:
                    tts.speak(text)
                except Exception as exc:  # noqa: BLE001
                    print(f"[INTEL] briefing tts failed: {exc}")

        intelligence.set_briefing_handler(_briefing_to_users)

        def _notification_to_users(text: str, priority: str) -> None:
            """Proactive-engine sink. Always pushes to PWA/HUD; speaks
            on the Mac speakers unless this is a low-priority item
            while the user is on a meeting/focus path. We don't have a
            "natural pause" detector yet, so the priority axis is the
            only knob — refined in slice 5."""
            tag = priority.upper()
            print(f"[PROACTIVE/{tag}] {text}")
            try:
                events.publish({
                    "type": "jarvis_notification",
                    "priority": priority,
                    "text": text,
                })
            except Exception as exc:  # noqa: BLE001
                print(f"[PROACTIVE] publish failed: {exc}")
            # The ProactiveEngine already gates by activity state
            # (sleeping / in_meeting suppress non-high), so by the
            # time we get here it's safe to speak.
            if _VOICE_OK and tts is not None:
                try:
                    tts.speak(text)
                except Exception as exc:  # noqa: BLE001
                    print(f"[PROACTIVE] tts failed: {exc}")

        intelligence.set_notification_handler(_notification_to_users)
        intelligence.start()
        app.state.intelligence = intelligence
        app.state.brain.intelligence = intelligence
    except Exception as exc:  # noqa: BLE001
        print(f"[INTEL] init failed, continuing without intelligence: {exc}")
        intelligence = None

    # Vision layer. Best-effort like intelligence — brain.vision is
    # None until this block succeeds, and every vision call-site
    # guards against that. We share the brain's Anthropic client so
    # Claude Vision and Claude chat hit the same auth/quota pool.
    if _VISION_OK and VisionManager is not None:
        try:
            vision = VisionManager(client=app.state.brain.client)
            app.state.vision = vision
            app.state.brain.vision = vision
            print("[VISION] manager ready (screen + ocr + scanner + "
                  "recognizer + comparator + motion + translator)")
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] init failed, continuing without vision: {exc}")
    else:
        print("[VISION] disabled (deps missing or import failed)")

    # Smart Home layer. Wired immediately; adapter connections run in
    # background so the server becomes ready without waiting for API
    # calls (Govee, HA, etc.) that may take several seconds.
    if _SMARTHOME_OK and SmartHomeManager is not None:
        try:
            smarthome = SmartHomeManager()
            app.state.smarthome = smarthome
            app.state.brain.smarthome = smarthome

            async def _start_smarthome(mgr: "SmartHomeManager") -> None:
                try:
                    await mgr.start()
                    app.state.brain.refresh_smarthome_tool()
                    print("[SMARTHOME] wired to brain ✓")
                except Exception as _exc:  # noqa: BLE001
                    import traceback as _tb
                    print(f"[SMARTHOME] init failed — smart home disabled: {_exc}")
                    _tb.print_exc()

            asyncio.create_task(_start_smarthome(smarthome))
        except Exception as exc:  # noqa: BLE001
            print(f"[SMARTHOME] could not create manager: {exc}")
    else:
        print("[SMARTHOME] disabled (module unavailable)")

    # Productivity + entertainment — independent layers, uniform wiring.
    def _build_productivity() -> Any:
        from .productivity.productivity_manager import ProductivityManager
        return ProductivityManager(Path("data/jarvis.db"),
                                   client=app.state.brain.client)
    _wire_subsystem(app, "PRODUCTIVITY", _build_productivity,
                    brain_attr="_productivity", state_attr="productivity")

    def _build_entertainment() -> Any:
        from .entertainment.entertainment_manager import EntertainmentManager
        return EntertainmentManager(Path("data/jarvis.db"),
                                    app.state.brain.client,
                                    getattr(app.state, "smarthome", None))
    _wire_subsystem(app, "ENTERTAINMENT", _build_entertainment,
                    brain_attr="_entertainment", state_attr="entertainment")

    # Security & monitoring layer. Best-effort like every other layer:
    # a boot failure logs and JARVIS keeps running (the brain's
    # _security None-check makes the short-circuit a no-op).
    try:
        from .security import SecurityManager

        def _security_speak(text: str, severity: str) -> None:
            """Voice + HUD sink for security alerts. Pushes to every
            client and speaks on the Mac. CRITICAL items always speak."""
            print(f"[SECURITY/{severity}] {text}")
            try:
                events.publish({
                    "type": "jarvis_notification",
                    "priority": "high" if severity in ("HIGH", "CRITICAL") else "normal",
                    "text": text,
                })
            except Exception as exc:  # noqa: BLE001
                print(f"[SECURITY] publish failed: {exc}")
            if _VOICE_OK and tts is not None:
                try:
                    tts.speak(text)
                except Exception as exc:  # noqa: BLE001
                    print(f"[SECURITY] tts failed: {exc}")

        def _security_notify(message: str, contacts: list[str]) -> None:
            """Emergency-contact transport. We don't ship an SMS gateway,
            so this pushes a high-priority event (which the PWA surfaces)
            and logs the intended recipients. Swap in iMessage/WhatsApp
            here when a transport is configured."""
            print(f"[SECURITY] emergency notify → {contacts}: {message}")
            try:
                events.publish({
                    "type": "emergency_notification",
                    "text": message,
                    "contacts": contacts,
                })
            except Exception as exc:  # noqa: BLE001
                print(f"[SECURITY] notify publish failed: {exc}")

        security = SecurityManager(
            db_path=Path("data/security.db"),
            vision_manager=getattr(app.state, "vision", None),
            smarthome=getattr(app.state, "smarthome", None),
            speak_handler=_security_speak,
            notify_handler=_security_notify,
        )
        security.start()
        app.state.security = security
        app.state.brain._security = security
        print("[SECURITY] wired to brain ✓")
    except Exception as exc:  # noqa: BLE001
        print(f"[SECURITY] init failed, continuing without security: {exc}")
        security = None  # type: ignore[assignment]

    # Communication layer. Best-effort like every other layer.
    try:
        from .communication import CommunicationManager

        def _comm_speak(text: str) -> None:
            if _VOICE_OK and tts is not None:
                try:
                    tts.speak(text)
                except Exception as exc:  # noqa: BLE001
                    print(f"[COMM] tts failed: {exc}")

        def _comm_ui(event: dict) -> None:
            try:
                events.publish(event)
            except Exception as exc:  # noqa: BLE001
                print(f"[COMM] ui publish failed: {exc}")

        def _comm_macos(title: str, body: str) -> None:
            # Route a macOS toast through the Tier-1 notification action.
            try:
                from .mac_control import tier2_apps
                tier2_apps._send_notification(title=title, body=body)  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001
                print(f"[COMM] macos notify failed: {exc}")

        def _comm_meeting() -> bool:
            intel = getattr(app.state, "intelligence", None)
            if intel is None:
                return False
            try:
                ctx = intel.get_context_for_brain() or ""
                return "meeting" in ctx.lower() or "termin" in ctx.lower()
            except Exception:  # noqa: BLE001
                return False

        communication = CommunicationManager(
            db_path=Path("data/communication.db"),
            client=app.state.brain.client,
            speak_handler=_comm_speak,
            ui_handler=_comm_ui,
            macos_handler=_comm_macos,
            meeting_probe=_comm_meeting,
        )
        communication.start()
        app.state.communication = communication
        app.state.brain._communication = communication
        # Phased migration: let the security layer also push through the
        # notification center (adds DND/quiet-hours/telegram to its alerts)
        # without removing its existing direct speak path.
        if security is not None and communication.notifications is not None:
            try:
                security._speak = (  # noqa: SLF001
                    lambda msg, sev, _nc=communication.notifications:
                    _nc.send("Sicherheit", msg,
                             "critical" if sev in ("HIGH", "CRITICAL") else "medium",
                             "security"))
            except Exception as exc:  # noqa: BLE001
                print(f"[COMM] security→center bridge failed: {exc}")

        # Emergency → Telegram: emergency contact notifications (SOS, fire,
        # intrusion) now ALSO push to the owner's iPhone via the Telegram
        # bot, the most reliable mobile channel. Keeps the existing
        # log + event-bus path; degrades to a no-op if Telegram isn't set up.
        emergency = getattr(security, "emergency", None) if security else None
        if emergency is not None and communication.telegram is not None:
            try:
                _orig_notify = emergency._notify  # noqa: SLF001

                def _emergency_notify_all(message: str, contacts: list[str],
                                          _orig=_orig_notify,
                                          _tg=communication.telegram) -> None:
                    if _orig is not None:
                        try:
                            _orig(message, contacts)
                        except Exception as exc:  # noqa: BLE001
                            print(f"[COMM] emergency orig-notify failed: {exc}")
                    try:
                        if _tg.configured:
                            _tg.notify_sync("NOTFALL", message, "critical")
                    except Exception as exc:  # noqa: BLE001
                        print(f"[COMM] emergency telegram push failed: {exc}")

                emergency._notify = _emergency_notify_all  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001
                print(f"[COMM] emergency→telegram bridge failed: {exc}")

        # Route intelligence briefings + proactive notifications through the
        # NotificationCenter too, so they respect DND / quiet-hours (a 7am
        # briefing or a low-priority nudge no longer speaks during quiet
        # hours). Re-points the handlers now that the center exists.
        if intelligence is not None and communication.notifications is not None:
            try:
                _nc = communication.notifications

                def _intel_notify(text: str, priority: str, _nc=_nc) -> None:
                    _nc.send("JARVIS", text,
                             priority if priority in ("high", "critical") else "medium",
                             "proactive")

                def _intel_briefing(text: str, _nc=_nc) -> None:
                    _nc.send("Briefing", text, "medium", "intelligence")

                intelligence.set_notification_handler(_intel_notify)
                intelligence.set_briefing_handler(_intel_briefing)
            except Exception as exc:  # noqa: BLE001
                print(f"[COMM] intelligence→center bridge failed: {exc}")
        print("[COMM] wired to brain ✓")
    except Exception as exc:  # noqa: BLE001
        print(f"[COMM] init failed, continuing without communication: {exc}")
        communication = None  # type: ignore[assignment]

    # Finance layer. Best-effort. Price alerts route through the comm
    # NotificationCenter (so they respect DND/quiet-hours + reach Telegram).
    def _build_finance() -> Any:
        from .finance import FinanceManager
        _nc = getattr(communication, "notifications", None) \
            if communication is not None else None
        return FinanceManager(db_path=Path("data/finance.db"),
                              client=app.state.brain.client,
                              notification_center=_nc)
    finance = _wire_subsystem(app, "FINANCE", _build_finance,
                              brain_attr="_finance", state_attr="finance")

    # On macOS, periodically drain the main-thread NSRunLoop so Cocoa
    # framework callbacks (Speech.framework's SFSpeechRecognizer in
    # particular) actually get delivered. Apple posts those completions
    # onto the main runloop; uvicorn's asyncio owns the main thread but
    # never pumps NSRunLoop, so without this task the callbacks queue
    # up forever and recognition tasks time out. runUntilDate_ with a
    # zero-second date is non-blocking — it just processes whatever's
    # already pending and returns immediately, so the ~20 ms sleep is
    # the cost ceiling.
    # Only run the 20ms Cocoa pump (≈50 wakeups/s, a real battery cost) when
    # the voice stack is installed — it exists solely to deliver
    # Speech.framework STT callbacks. A headless/text-only server (no voice
    # deps) doesn't need it and shouldn't pay for it.
    runloop_pump_task: asyncio.Task | None = None
    if os.uname().sysname == "Darwin" and _VOICE_OK:
        try:
            from Foundation import NSDate, NSRunLoop  # type: ignore[import-not-found]

            async def _pump_main_runloop() -> None:
                while True:
                    NSRunLoop.mainRunLoop().runUntilDate_(
                        NSDate.dateWithTimeIntervalSinceNow_(0.0)
                    )
                    await asyncio.sleep(0.02)

            runloop_pump_task = asyncio.create_task(
                _pump_main_runloop(), name="cocoa-runloop-pump"
            )
            print("[JARVIS] Cocoa main-runloop pump active "
                  "(needed for Speech.framework callbacks)")
        except Exception as exc:  # noqa: BLE001 — PyObjC missing, fine
            print(f"[JARVIS] runloop pump skipped: {exc}")

    # Optional: run the local wake-word loop on the MacBook in a background
    # thread. Enabled with JARVIS_LOCAL_VOICE=1 in the environment / .env.
    voice_thread: threading.Thread | None = None
    if os.getenv("JARVIS_LOCAL_VOICE", "0") == "1":
        if not _VOICE_OK:
            print("[JARVIS] JARVIS_LOCAL_VOICE=1 but voice stack is not installed — skipping.")
        else:
            from . import voice_loop

            voice_thread = threading.Thread(
                target=voice_loop.run,
                args=(app.state.brain,),
                name="jarvis-voice",
                # NOT a daemon: the thread holds a live PortAudio
                # InputStream, and if the interpreter kills it mid-cleanup
                # we get a double-free crash on macOS. We join it
                # explicitly in the finally block below.
                daemon=False,
            )
            voice_thread.start()
            print("[JARVIS] local voice loop started (JARVIS_LOCAL_VOICE=1)")

            # Startup greeting — speaks after a brief pause so the audio
            # system is fully initialised before the first TTS call.
            def _startup_greeting() -> None:
                import time as _time
                _time.sleep(2.5)
                try:
                    _hour = _time.localtime().tm_hour
                    if _hour < 12:
                        _msg = "Guten Morgen, Lucian. Ich bin online und einsatzbereit."
                    elif _hour < 18:
                        _msg = "Guten Mittag, Lucian. Ich bin online und einsatzbereit."
                    else:
                        _msg = "Guten Abend, Lucian. Ich bin online und einsatzbereit."
                    if tts is not None:
                        tts.speak(_msg)
                except Exception as _exc:  # noqa: BLE001
                    print(f"[JARVIS] startup greeting failed: {_exc}")

            threading.Thread(target=_startup_greeting, daemon=True,
                             name="jarvis-greeting").start()

    try:
        yield
    finally:
        if intelligence is not None:
            try:
                intelligence.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"[INTEL] stop error: {exc}")
        if security is not None:
            try:
                security.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"[SECURITY] stop error: {exc}")
        if communication is not None:
            try:
                communication.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"[COMM] stop error: {exc}")
        if finance is not None:
            try:
                finance.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"[FINANCE] stop error: {exc}")
        # Productivity + entertainment hold SQLite connections; close them so
        # WAL is flushed (accessed via app.state to avoid unbound-name issues
        # if their init failed).
        for _mgr_name in ("productivity", "entertainment"):
            _m = getattr(app.state, _mgr_name, None)
            if _m is not None and hasattr(_m, "stop"):
                try:
                    _m.stop()
                except Exception as exc:  # noqa: BLE001
                    print(f"[{_mgr_name.upper()}] stop error: {exc}")
        if runloop_pump_task is not None:
            runloop_pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runloop_pump_task
        if voice_thread is not None:
            from . import voice_loop

            voice_loop.request_stop()
            voice_thread.join(timeout=5.0)
            if voice_thread.is_alive():
                print("[JARVIS] voice thread did not stop cleanly within 5s")
        if _VOICE_OK and tts is not None:
            tts.shutdown()
        print("[JARVIS] shutdown complete")


app = FastAPI(title="JARVIS", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=False,  # we use Bearer tokens, not cookies
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


# --- Helpers -------------------------------------------------------------- #

def authorize_chat(request: Request,
                   authorization: str | None = Header(default=None)) -> str:
    """Like require_token, but also accepts a live guest/family temp token
    (from /security/access/temp). Owner token → full access; guest token →
    request.state.guest_token is set so /chat can restrict the command to
    the guest's allowed level. Other routes keep using require_token
    (owner-only)."""
    scheme, token = get_authorization_scheme_param(authorization)
    if not token or scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing or malformed Bearer token.",
                            headers={"WWW-Authenticate": "Bearer"})
    if secrets.compare_digest(token, settings.JARVIS_AUTH_TOKEN):
        _check_rate(token)
        request.state.guest_token = None
        return token
    # Guest temp token?
    sec = getattr(request.app.state, "security", None)
    access = getattr(sec, "access", None) if sec is not None else None
    if access is not None and access._grant_for(token) is not None:  # noqa: SLF001
        _check_rate(token)
        request.state.guest_token = token
        return token
    raise HTTPException(status_code=401, detail="Invalid token.",
                        headers={"WWW-Authenticate": "Bearer"})


def security_rate_gate(request: Request) -> None:
    """Per-IP rate limit + block check, wired into the main request paths.
    Complements require_token's per-token limiter (a different axis) and
    finally puts the security layer's anomaly detector + auto-block list on
    the live path. No-op when the security layer isn't loaded."""
    sec = getattr(request.app.state, "security", None)
    if sec is None:
        return
    ip = request.client.host if request.client else "local"
    if getattr(sec, "digital", None) is not None and sec.digital.is_blocked(ip):
        raise HTTPException(status_code=403, detail="IP blockiert.")
    if getattr(sec, "anomaly", None) is not None \
            and not sec.anomaly.rate_limit_check(ip):
        raise HTTPException(status_code=429, detail="Zu viele Anfragen (per IP).")


def _client_tag(request: Request) -> str:
    """Cheap UA-based client classifier purely for the console log line."""
    ua = (request.headers.get("user-agent") or "").lower()
    if "iphone" in ua or "ipad" in ua:
        return "iPhone"
    if "windows" in ua:
        return "Windows"
    if "mozilla" in ua:
        return "web"
    return "unknown"


def _sanitize(text: str) -> str:
    """Drop control chars and clip to MAX_INPUT_LENGTH."""
    cleaned = "".join(ch for ch in text if ch.isprintable() or ch in (" \t"))
    return cleaned.strip()[: settings.MAX_INPUT_LENGTH]


# --- Models --------------------------------------------------------------- #

class ChatRequest(BaseModel):
    text: str = Field(..., max_length=settings.MAX_INPUT_LENGTH)
    speak: bool = False  # if True, also speak the reply through server speakers


class ChatResponse(BaseModel):
    reply: str
    # Populated by /audio so the web client can render what was actually
    # transcribed alongside the reply. Empty for /chat.
    transcript: str | None = None


class ConfirmRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    approve: bool = True


class Tier4ConfirmRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=200)


# --- Routes --------------------------------------------------------------- #

@app.get("/")
def health() -> dict[str, Any]:
    return {"name": "JARVIS", "model": settings.MODEL, "ok": True}


@app.post("/chat", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    request: Request,
    token: str = Depends(authorize_chat),
    _gate: None = Depends(security_rate_gate),
) -> ChatResponse:
    user_text = _sanitize(payload.text)
    if not user_text:
        raise HTTPException(status_code=400, detail="Empty message.")

    # Delegated access: if this is a guest temp token, restrict to the
    # guest's allowed command set and audit the attempt.
    guest_token = getattr(request.state, "guest_token", None)
    if guest_token:
        sec = getattr(request.app.state, "security", None)
        access = getattr(sec, "access", None) if sec is not None else None
        allowed = access is not None and access.is_command_allowed(guest_token, user_text)
        if access is not None:
            try:
                access._db.log_access(  # noqa: SLF001
                    "guest", user_text,
                    request.client.host if request.client else None,
                    None, "guest", allowed, "delegated access")
            except Exception:  # noqa: BLE001
                pass
        if not allowed:
            return ChatResponse(
                reply="Dieser Befehl ist im Gastzugang nicht erlaubt.")

    tag = _client_tag(request)
    print(f"[CLIENT: {tag}] [YOU] {user_text}")
    reply = request.app.state.brain.reply(token, user_text)
    print(f"[JARVIS] {reply}")
    if payload.speak and _VOICE_OK and tts is not None:
        tts.speak(reply)
    return ChatResponse(reply=reply)


@app.post("/transcribe")
async def transcribe_endpoint(
    request: Request,
    audio: UploadFile,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    """Speech-to-text only — no Claude call.

    The iPhone PWA records via MediaRecorder (audio/mp4 from Safari,
    audio/webm elsewhere), POSTs the blob here, and then forwards
    the returned text via the existing ``/ws`` so the brain's
    streaming pipeline plays back unchanged.

    Backend pick mirrors stt._load_model:
        - macOS Speech.framework gets the raw m4a / webm / wav
          through SFSpeechURLRecognitionRequest (AVFoundation
          decodes any of them natively).
        - Faster-whisper / vanilla whisper expect WAV, so we let
          stt.transcribe handle that path (it'll error on non-WAV
          and the PWA shows the error — the doc tells users to
          prefer the macos backend on iPhone).
    """
    if not _VOICE_OK:
        raise HTTPException(
            status_code=503,
            detail="Voice stack not installed. "
                   "Run: pip install -r requirements-voice.txt",
        )

    raw = await audio.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty upload.")
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio too large.")

    content_type = (audio.content_type or "").lower()
    if "mp4" in content_type or "m4a" in content_type:
        suffix = ".m4a"
    elif "webm" in content_type:
        suffix = ".webm"
    elif "ogg" in content_type:
        suffix = ".ogg"
    elif "wav" in content_type or raw[:4] == b"RIFF":
        suffix = ".wav"
    else:
        suffix = ".bin"

    # Try macOS Speech.framework first (best for non-WAV formats
    # because AVFoundation handles m4a/webm decoding natively).
    transcript = ""
    if suffix in (".m4a", ".webm", ".ogg", ".wav"):
        try:
            from . import stt_macos
        except Exception:  # noqa: BLE001
            stt_macos = None  # type: ignore[assignment]
        if stt_macos is not None and stt_macos.is_available() \
           and stt_macos.authorization_status() == 3:  # _STATUS_AUTHORIZED
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fp:
                fp.write(raw)
                tmp = Path(fp.name)
            try:
                transcript = stt_macos.transcribe_wav(str(tmp))
            except Exception as exc:  # noqa: BLE001
                print(f"[JARVIS] /transcribe macos backend error: {exc}")
            finally:
                tmp.unlink(missing_ok=True)

    # Fallback to Whisper if macOS path is unavailable or returned empty.
    if not transcript and transcribe is not None and raw[:4] == b"RIFF":
        try:
            transcript = transcribe(raw)
        except Exception as exc:  # noqa: BLE001
            print(f"[JARVIS] /transcribe whisper backend error: {exc}")

    transcript = _sanitize(transcript or "")
    tag = _client_tag(request)
    if transcript:
        print(f"[CLIENT: {tag}] [TRANSCRIBE] {transcript}")
    else:
        print(f"[CLIENT: {tag}] [TRANSCRIBE] (no speech detected, "
              f"format={content_type!r}, {len(raw)} bytes)")
    return {"transcript": transcript}


@app.post("/audio", response_model=ChatResponse)
async def audio(
    request: Request,
    file: UploadFile,
    token: str = Depends(require_token),
    _gate: None = Depends(security_rate_gate),
) -> ChatResponse:
    """Upload a 16 kHz mono WAV; we transcribe + run the same /chat pipeline."""
    if not _VOICE_OK or transcribe is None:
        raise HTTPException(
            status_code=503,
            detail="Voice stack not installed. "
                   "Run: pip install -r requirements-voice.txt",
        )
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty upload.")
    if len(raw) > 25 * 1024 * 1024:  # 25 MB safety cap
        raise HTTPException(status_code=413, detail="Audio too large.")

    transcript = transcribe(raw)
    transcript = _sanitize(transcript)
    if not transcript:
        return ChatResponse(reply="I didn't catch any speech.")

    tag = _client_tag(request)
    print(f"[CLIENT: {tag}] [YOU·audio] {transcript}")

    # Voice authentication actually runs HERE — the audio path is the only
    # place we have both the spoken command and its audio. The security
    # pipeline (speaker verify → anomaly → per-command permission level)
    # gates the turn. No-op when VOICE_AUTH_ENABLED=false (verify returns
    # allow); a real gate once an owner profile is enrolled.
    sec = getattr(request.app.state, "security", None)
    if sec is not None:
        try:
            ip = request.client.host if request.client else "audio"
            verdict = await sec.process_request(transcript, audio=raw, ip=ip)
            if not verdict.get("allowed", True):
                msg = f"Zugriff verweigert: {verdict.get('reason', 'nicht autorisiert')}."
                print(f"[SECURITY] audio turn denied: {verdict.get('reason')}")
                return ChatResponse(reply=msg, transcript=transcript)
        except Exception as exc:  # noqa: BLE001 — never block on the gate
            print(f"[SECURITY] audio gate error (allowing): {exc}")

    reply = request.app.state.brain.reply(token, transcript)
    print(f"[JARVIS] {reply}")
    return ChatResponse(reply=reply, transcript=transcript)


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    """Long-lived text chat + server-pushed events over a single WS.

    Wire format:
        client → server  {"text": "..."}                     # chat turn
        server → client  {"reply": "..."}                    # chat answer
                         {"error": "..."}                    # chat failure
                         {"type": "voice_state", ...}        # voice activity
                         {"type": "user_message", ...}       # transcribed speech
                         {"type": "jarvis_reply", ...}       # voice-path reply

    Concurrency: receive_json() and the fan-out task both write to the
    same socket, so all sends are serialised through `send_lock` — the
    underlying ASGI sender isn't safe to call from two tasks at once.
    """
    await websocket.accept()
    token = await authorize_websocket(websocket)
    if token is None:
        return

    brain: Brain = websocket.app.state.brain
    ua = (websocket.headers.get("user-agent") or "").lower()
    tag = (
        "iPhone" if "iphone" in ua or "ipad" in ua
        else "Windows" if "windows" in ua
        else "web" if "mozilla" in ua
        else "unknown"
    )
    print(f"[JARVIS] ws connected client={tag}")

    send_lock = asyncio.Lock()

    async def send_json(payload: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    # Subscribe to the server-side event bus and stream entries out to
    # this client. Cancelled in the finally block so the queue is
    # unsubscribed even on abrupt disconnects.
    event_queue = events.subscribe()

    async def fanout() -> None:
        try:
            while True:
                ev = await event_queue.get()
                await send_json(ev)
        except asyncio.CancelledError:
            raise
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"[JARVIS] ws fanout error: {exc}")

    fanout_task = asyncio.create_task(fanout(), name=f"ws-fanout-{tag}")

    try:
        while True:
            payload = await websocket.receive_json()
            user_text = _sanitize(str(payload.get("text", "")))
            if not user_text:
                await send_json({"error": "empty message"})
                continue

            print(f"[CLIENT: {tag}] [YOU] {user_text}")
            try:
                # brain.reply is synchronous and streams sentences
                # via events.publish() (loop.call_soon_threadsafe).
                # If we ran it directly on the asyncio thread the
                # entire stream would queue up and only deliver at
                # the end — defeating streaming. Punt to a worker
                # thread so the loop stays free to dispatch fanout +
                # the Cocoa runloop pump while Claude is generating.
                #
                # speak_locally=False: the requester is on the other end
                # of this WebSocket (PWA, browser, …), not standing at
                # the Mac. They get the reply text + can synthesise
                # speech themselves; the Mac stays silent so we don't
                # talk over the remote client.
                reply = await asyncio.to_thread(
                    brain.reply, token, user_text, speak_locally=False,
                )
            except HTTPException as exc:
                await send_json({"error": exc.detail})
                continue

            print(f"[JARVIS] {reply}")
            await send_json({"reply": reply})
    except WebSocketDisconnect:
        print(f"[JARVIS] ws disconnected client={tag}")
    except Exception as exc:  # noqa: BLE001
        print(f"[JARVIS] ws error: {exc}")
        with contextlib.suppress(Exception):
            await send_json({"error": str(exc)})
            await websocket.close()
    finally:
        fanout_task.cancel()
        events.unsubscribe(event_queue)
        with contextlib.suppress(asyncio.CancelledError):
            await fanout_task


# --- Remote TTS for the iPhone PWA --------------------------------------- #
# iOS Safari's Web Speech API is unreliable in standalone-mode PWAs —
# speak() silently no-ops even after a user-gesture primer. Instead we
# synthesize audio server-side via macOS' `say` command and stream the
# bytes back; the PWA plays them through an <audio> element, which
# works rock-solid on iOS. Same voice the local Mac TTS uses (configurable
# via TTS_VOICE in .env, default "Markus" for German).
@app.get("/tts/synthesize")
async def tts_synthesize(
    text: str,
    request: Request,
    token: str = Depends(require_token),
) -> Response:
    import asyncio as _aio
    import tempfile
    text = _sanitize(text)[:2000]
    if not text:
        raise HTTPException(status_code=400, detail="Empty text.")
    voice = os.getenv("TTS_VOICE", "Markus")
    # AIFF is what `say -o` emits natively; Safari + iOS play it
    # through <audio> without conversion. We skip WAV/MP3 transcode
    # to keep latency low — the file is small and only hits the
    # local Tailscale link.
    out = Path(tempfile.mkstemp(suffix=".aiff")[1])
    try:
        proc = await _aio.create_subprocess_exec(
            "say", "-v", voice, "-o", str(out), "--", text,
            stdout=_aio.subprocess.DEVNULL,
            stderr=_aio.subprocess.PIPE,
        )
        try:
            _, err = await _aio.wait_for(proc.communicate(), timeout=10.0)
        except _aio.TimeoutError:
            proc.kill()
            raise HTTPException(status_code=504, detail="TTS synth timed out.")
        if proc.returncode != 0:
            detail = (err.decode("utf-8", "replace") if err else "say failed").strip()
            raise HTTPException(status_code=500, detail=f"TTS error: {detail}")
        data = out.read_bytes()
    finally:
        out.unlink(missing_ok=True)
    return Response(
        content=data,
        media_type="audio/aiff",
        headers={"Cache-Control": "no-store"},
    )


# --- mac_control routes --------------------------------------------------- #

@app.get("/permissions")
def permissions(token: str = Depends(require_token)) -> dict[str, Any]:
    """Snapshot of the permission/kill-switch state, plus current pending
    actions. Used by the web UI to render the status row + confirmation
    cards. Never includes the Tier-4 password or its hash."""
    return mac_dispatcher.status()


def _speak_result(envelope: dict[str, Any]) -> None:
    """Announce a confirm/tier4-confirm outcome over the local Mac speakers.

    Only fires when the voice stack is loaded — i.e. running on the Mac
    that hosts the actual TTS. Browser clients still get the result text
    via their own SpeechSynthesis (handled in index.html).
    """
    if not (_VOICE_OK and tts is not None):
        return
    text = (envelope.get("result") or envelope.get("reason") or "").strip()
    if not text:
        return
    # Cap to avoid the speakers narrating a 100-line directory listing.
    if len(text) > 240:
        text = text[:240] + " …"
    tts.speak(text)


@app.post("/confirm")
def confirm(
    payload: ConfirmRequest, token: str = Depends(require_token),
) -> dict[str, Any]:
    """Tier 2 / Tier 3 confirmation. Approves and runs (approve=True) or
    cancels (approve=False) a pending action. Tier 4 pendings here return
    a rejection — they must go through /tier4-confirm with the password.
    """
    from .mac_control import confirmation as _cf

    peek = _cf.peek(payload.id)
    if peek is None:
        raise HTTPException(status_code=404, detail="Pending action not found or expired.")
    if peek.requires_password:
        raise HTTPException(
            status_code=400,
            detail="This pending action requires Tier-4 password — use /tier4-confirm.",
        )
    envelope = (mac_dispatcher.consume(payload.id) if payload.approve
                else mac_dispatcher.cancel(payload.id))
    _speak_result(envelope)
    return envelope


@app.post("/tier4-confirm")
def tier4_confirm(
    payload: Tier4ConfirmRequest, token: str = Depends(require_token),
) -> dict[str, Any]:
    """Tier 4 confirmation: requires the JARVIS_SUDO_PASSWORD value. The
    password is checked via constant-time compare in the dispatcher and
    is never logged."""
    envelope = mac_dispatcher.consume(payload.id, password=payload.password)
    _speak_result(envelope)
    return envelope


@app.post("/pending/clear")
def pending_clear(token: str = Depends(require_token)) -> dict[str, Any]:
    """Bulk-cancel every outstanding pending action — handy when retry
    loops stacked up duplicates and the user wants a clean slate."""
    return mac_dispatcher.cancel_all()


@app.post("/interrupt")
def interrupt(token: str = Depends(require_token)) -> dict[str, Any]:
    """Cut JARVIS off mid-reply WITHOUT arming the kill switch.

    Cancels any in-flight brain reply (so the response gets discarded)
    and stops TTS playback immediately, then drains the mic queue so
    the speaker tail doesn't get re-ingested. Tier 2+ actions stay
    enabled — use /emergency-stop for the harder cut. Mapped to
    Cmd+Shift+J in the Electron HUD.
    """
    try:
        from . import voice_loop as _vl
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f"voice loop unavailable: {exc}") from exc
    return _vl.interrupt(reason="API request")


@app.post("/emergency-stop")
def emergency_stop(token: str = Depends(require_token)) -> dict[str, Any]:
    """Trigger the kill switch — all Tier 2+ actions refuse until /resume."""
    mac_kill_switch.trigger("api request")
    return mac_kill_switch.status()


# --- memory routes ---------------------------------------------------- #
# Routes onto the MemoryManager carried by the brain. All require the
# bearer token like the rest of the API. Read-only routes are
# tolerant of degraded subsystems; write routes (forget / wipe)
# refuse if the layer they need is unavailable.

def _memory(request: Request):
    """Pull the live MemoryManager off the brain. None-safe so the
    server still starts even if memory init failed."""
    brain = getattr(request.app.state, "brain", None)
    return getattr(brain, "memory", None) if brain else None


@app.get("/memory/profile")
def memory_profile(request: Request,
                   token: str = Depends(require_token)) -> dict[str, Any]:
    """User profile as JSON. Sensitive fields are never stored
    (redaction runs on every write path) so this is safe to return
    in full."""
    mem = _memory(request)
    if mem is None:
        raise HTTPException(status_code=503, detail="memory unavailable")
    return mem.get_profile()


@app.get("/memory/recent")
def memory_recent(request: Request,
                  days: int = 7, limit: int = 10,
                  token: str = Depends(require_token)) -> dict[str, Any]:
    """Up to ``limit`` most-recent session summaries from the last
    ``days`` days. Used by the HUD's history pane / a future
    /memory inspector."""
    mem = _memory(request)
    if mem is None:
        raise HTTPException(status_code=503, detail="memory unavailable")
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 50))
    return {"sessions": mem.recent_sessions(days=days, limit=limit)}


@app.get("/memory/errors")
def memory_errors(request: Request,
                  token: str = Depends(require_token)) -> dict[str, Any]:
    """Commands with at least one recorded failure. Includes the
    success rate so the HUD can flag chronic flakes."""
    mem = _memory(request)
    if mem is None:
        raise HTTPException(status_code=503, detail="memory unavailable")
    return {"problematic": mem.known_errors()}


@app.get("/memory/stats")
def memory_stats(request: Request,
                 token: str = Depends(require_token)) -> dict[str, Any]:
    """Aggregated memory state across every layer — what's available,
    how many rows / vectors / facts. Drives a future /memory dashboard."""
    mem = _memory(request)
    if mem is None:
        raise HTTPException(status_code=503, detail="memory unavailable")
    return mem.stats()


@app.get("/memory/search")
def memory_search(request: Request, q: str = "", n: int = 5,
                  token: str = Depends(require_token)) -> dict[str, Any]:
    """Semantic search across past conversations. ``q`` is the query
    text, ``n`` is the result cap (1..20)."""
    mem = _memory(request)
    if mem is None:
        raise HTTPException(status_code=503, detail="memory unavailable")
    q = (q or "").strip()
    if not q:
        return {"query": q, "results": []}
    n = max(1, min(int(n), 20))
    return {"query": q, "results": mem.search(q, n_results=n)}


@app.get("/memory/knowledge/search")
def memory_knowledge_search(request: Request, q: str = "", n: int = 5,
                            category: str = "",
                            token: str = Depends(require_token)) -> dict[str, Any]:
    """Semantic search over explicitly-saved knowledge ('merk dir …')."""
    mem = _memory(request)
    if mem is None:
        raise HTTPException(status_code=503, detail="memory unavailable")
    q = (q or "").strip()
    if not q:
        return {"query": q, "results": []}
    n = max(1, min(int(n), 20))
    results = mem.long_term.search_knowledge(q, n_results=n)
    if category:
        results = [r for r in results
                   if (r.get("metadata") or {}).get("category") == category]
    return {"query": q, "results": results}


@app.get("/memory/knowledge/list")
def memory_knowledge_list(request: Request, category: str = "", limit: int = 50,
                          token: str = Depends(require_token)) -> dict[str, Any]:
    """List saved knowledge, newest first, optionally by category."""
    mem = _memory(request)
    if mem is None:
        raise HTTPException(status_code=503, detail="memory unavailable")
    limit = max(1, min(int(limit), 200))
    return {"results": mem.long_term.list_knowledge(
        category=category or None, limit=limit)}


# ── flashcards (Second Brain SRS) ───────────────────────────────────────── #

def _flashcards(request: Request) -> Any:
    brain = getattr(request.app.state, "brain", None)
    return brain._get_flashcards() if brain is not None else None  # noqa: SLF001


class _FlashcardAddRequest(BaseModel):
    front: str = Field(..., min_length=1)
    back: str = Field(..., min_length=1)
    category: str = "general"


class _FlashcardReviewRequest(BaseModel):
    quality: int | None = Field(default=None, ge=0, le=5)
    feedback: str | None = None


@app.get("/knowledge/flashcards/due")
def flashcards_due(request: Request, limit: int = 20,
                   token: str = Depends(require_token)) -> dict[str, Any]:
    fc = _flashcards(request)
    if fc is None:
        raise HTTPException(status_code=503, detail="flashcards unavailable")
    cards = fc.due_cards(limit=max(1, min(int(limit), 100)))
    return {"due": fc.due_count(), "cards": cards}


@app.post("/knowledge/flashcards")
def flashcards_add(body: _FlashcardAddRequest, request: Request,
                   token: str = Depends(require_token)) -> dict[str, Any]:
    fc = _flashcards(request)
    if fc is None:
        raise HTTPException(status_code=503, detail="flashcards unavailable")
    cid = fc.add_card(body.front, body.back, body.category)
    return {"ok": cid is not None, "id": cid}


@app.post("/knowledge/flashcards/{card_id}/review")
def flashcards_review(card_id: int, body: _FlashcardReviewRequest, request: Request,
                      token: str = Depends(require_token)) -> dict[str, Any]:
    fc = _flashcards(request)
    if fc is None:
        raise HTTPException(status_code=503, detail="flashcards unavailable")
    q = body.quality if body.quality is not None \
        else fc.quality_from_feedback(body.feedback or "richtig")
    result = fc.review_card(card_id, q)
    if not result:
        raise HTTPException(status_code=404, detail="card not found")
    return result


class _ForgetRequest(BaseModel):
    """A single entry id to drop from the vector store. The actual
    semantic equivalent — forgetting a single conversation — runs
    through chromadb directly; the brain exposes it via the manager."""
    id: str = Field(..., min_length=1, max_length=64)


@app.post("/memory/forget")
def memory_forget(payload: _ForgetRequest, request: Request,
                  token: str = Depends(require_token)) -> dict[str, Any]:
    """Remove one specific memory entry by id. Used by the future
    HUD inspector to let the user prune individual sessions / facts
    without nuking everything."""
    mem = _memory(request)
    if mem is None:
        raise HTTPException(status_code=503, detail="memory unavailable")
    # Delete from the three collections opportunistically — we don't
    # know up-front which one holds the id, but Chroma's delete is a
    # cheap no-op when the id is absent.
    ltm = mem.long_term
    if not ltm.available:
        raise HTTPException(status_code=503, detail="long-term memory unavailable")
    removed: list[str] = []
    for coll, key in ((ltm._conv, "conversations"),
                      (ltm._cmd, "commands"),
                      (ltm._kn, "knowledge")):
        try:
            coll.delete(ids=[payload.id])
            removed.append(key)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "id": payload.id, "checked": removed}


class _WipeRequest(BaseModel):
    """Required ``confirm`` field is a literal string the caller must
    type — the API layer's contribution to the spec's "double
    confirmation" rule (the HUD provides the second confirm step)."""
    confirm: str = Field(..., description="must equal 'I UNDERSTAND'")


@app.delete("/memory/all")
def memory_wipe_all(payload: _WipeRequest, request: Request,
                    token: str = Depends(require_token)) -> dict[str, Any]:
    """Full GDPR-style wipe: every conversation, command, knowledge
    entry, error row, fix, command-stat, profile field. Requires
    ``confirm == "I UNDERSTAND"`` in the request body. The action is
    logged (with timestamp) but the content is not."""
    mem = _memory(request)
    if mem is None:
        raise HTTPException(status_code=503, detail="memory unavailable")
    return mem.forget_everything(confirmation_token=payload.confirm)


@app.post("/resume")
def resume(token: str = Depends(require_token)) -> dict[str, Any]:
    """Clear the kill switch. Tier 2 stays locked — explicit reconfirm needed."""
    mac_kill_switch.resume()
    return mac_kill_switch.status()


# --- Vision API ----------------------------------------------------------- #
# The PWA uploads base64-encoded images here; the Mac screen capture
# route doesn't need an image because it grabs the screen server-side.
# Every route returns 503 when the vision manager isn't ready (deps
# missing, init failed) so the client gets a clean signal rather than
# silent 500s.
#
# Payload size cap is generous (8 MiB base64) because Claude Vision's
# 5 MiB limit lives on the DECODED image and our image_to_base64
# pipeline re-encodes anyway. We just need enough headroom for the
# encoded representation.


_MAX_VISION_IMG = 8 * 1024 * 1024


def _vision(request: Request):
    """Return the attached VisionManager, or None if vision isn't
    available on this server. Centralised so route handlers don't
    each duplicate the getattr lookup."""
    return getattr(request.app.state, "vision", None)


class VisionAnalyzeRequest(BaseModel):
    image: str = Field(..., min_length=1, max_length=_MAX_VISION_IMG)
    question: str = Field(
        default="Was siehst du auf diesem Bild?", max_length=2000,
    )


class VisionScreenRequest(BaseModel):
    # "describe" / "error" / "code" / "read" — or a free-form question
    # forwarded verbatim. Mirrors ScreenReader.analyze_screen contract.
    question: str = Field(default="describe", max_length=2000)


class VisionScanRequest(BaseModel):
    image: str = Field(..., min_length=1, max_length=_MAX_VISION_IMG)
    # "auto" | "receipt" | "invoice" | "contract" | "letter" |
    # "business_card" | "form" | "handwriting" | "table" |
    # "id_document" | "general". Unknown values normalise to general
    # in the scanner so we don't need server-side validation.
    doc_type: str = Field(default="auto", max_length=32)


class VisionTranslateRequest(BaseModel):
    image: str = Field(..., min_length=1, max_length=_MAX_VISION_IMG)
    target_language: str = Field(default="de", max_length=10)


@app.post("/vision/analyze")
def vision_analyze(
    payload: VisionAnalyzeRequest,
    request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    """Run a free-form vision query against a base64 image. Used by
    the iPhone PWA's photo-and-ask flow."""
    vision = _vision(request)
    if vision is None:
        raise HTTPException(status_code=503, detail="vision unavailable")
    result = vision.analyze_image(payload.image, payload.question)
    if result is None:
        raise HTTPException(
            status_code=500, detail="vision analysis failed",
        )
    return {"result": result, "type": "analyze"}


@app.post("/vision/screen")
def vision_screen(
    payload: VisionScreenRequest,
    request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    """Capture the Mac's screen and analyse it. Privacy indicator
    prints server-side. Screen Recording permission must be granted
    to whatever process is running uvicorn (Electron on launcher;
    Terminal in dev)."""
    vision = _vision(request)
    if vision is None:
        raise HTTPException(status_code=503, detail="vision unavailable")
    result = vision.screen.analyze_screen(payload.question)
    if result is None:
        raise HTTPException(
            status_code=500,
            detail=(
                "screen analysis failed — likely Screen Recording "
                "permission missing for this process"
            ),
        )
    return {"result": result, "type": "screen"}


@app.post("/vision/scan")
def vision_scan(
    payload: VisionScanRequest,
    request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    """Document scanning: receipts, invoices, business cards,
    contracts, handwriting. Returns both speakable summary AND the
    JSON-shaped structured fields where applicable."""
    vision = _vision(request)
    if vision is None:
        raise HTTPException(status_code=503, detail="vision unavailable")
    result = vision.scanner.scan_document(
        payload.image, doc_type=payload.doc_type,
    )
    if result is None:
        raise HTTPException(
            status_code=500, detail="document scan failed",
        )
    return {
        "doc_type":         result.doc_type,
        "summary":          result.summary,
        "structured_data":  result.structured_data,
        "raw_text":         result.raw_text,
        "action_items":     result.action_items,
        "confidence":       result.confidence,
    }


@app.post("/vision/translate")
def vision_translate(
    payload: VisionTranslateRequest,
    request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    """Single-shot visual translation. Rate-limited live mode lives
    on the WebSocket surface (TBD) — this is for "übersetze das"
    on a still photo or screen snippet."""
    vision = _vision(request)
    if vision is None:
        raise HTTPException(status_code=503, detail="vision unavailable")
    result = vision.translator.translate_image(
        payload.image, target_language=payload.target_language,
    )
    if result is None:
        raise HTTPException(
            status_code=500, detail="translation failed",
        )
    return {
        "original":         result.original,
        "translated":       result.translated,
        "target_language":  result.target_language,
    }


# --- Smart Home API ------------------------------------------------------- #

# TTS confirmation messages for smart home actions.
_SCENE_TTS: dict[str, str] = {
    "alles_an":        "Alle Lichter angeschaltet.",
    "alles_aus":       "Alle Lichter ausgeschaltet.",
    "kinoabend":       "Kino Modus aktiviert.",
    "gute_nacht":      "Gute Nacht. Lichter werden langsam gedimmt.",
    "guten_morgen":    "Guten Morgen. Sonnenaufgang gestartet.",
    "entspannen":      "Relax Modus aktiviert.",
    "arbeiten":        "Arbeits Modus aktiviert.",
    "party":           "Party Modus gestartet.",
    "lesen":           "Lese Modus aktiviert.",
    "fokus":           "Fokus Modus aktiviert.",
    "gaming":          "Gaming Modus aktiviert.",
    "romantisch":      "Romantik Modus aktiviert.",
    "verlasse_haus":   "Tschüss. Alles ausgeschaltet.",
    "ankunft_zuhause": "Willkommen zuhause.",
}

_COLOR_TTS: dict[str, str] = {
    "rot": "Rot", "red": "Rot",
    "grün": "Grün", "green": "Grün",
    "blau": "Blau", "blue": "Blau",
    "weiß": "Weiß", "weiss": "Weiß", "white": "Weiß",
    "gelb": "Gelb", "yellow": "Gelb",
    "orange": "Orange",
    "lila": "Lila", "purple": "Lila",
    "pink": "Pink",
    "cyan": "Cyan",
    "türkis": "Türkis",
    "sonnenuntergang": "Sonnenuntergang",
    "ozean": "Ozean",
    "wald": "Wald",
    "lagerfeuer": "Lagerfeuer",
    "lavendel": "Lavendel",
    "gold": "Gold",
}


def _smarthome_tts_text(
    action: str,
    command: str | None,
    scene: str | None,
    color: str | None,
    level: int | None,
    result: str,
) -> str | None:
    """Map a smarthome action to a short spoken confirmation, or None."""
    import re as _re

    if action == "scene" and scene:
        return _SCENE_TTS.get(scene.lower().replace(" ", "_").replace("-", "_"))

    if action == "turn_on":
        return "Eingeschaltet."

    if action == "turn_off":
        return "Ausgeschaltet."

    if action == "brightness" and level is not None:
        return f"Helligkeit auf {level} Prozent."

    if action == "color" and color:
        label = _COLOR_TTS.get(color.lower(), color.capitalize())
        return f"Farbe auf {label} gewechselt."

    if action == "command" and command:
        cmd = command.lower()
        # Scene triggered internally — extract name from result string
        m = _re.search(r"Szene '([^']+)' aktiviert", result)
        if m:
            tts = _SCENE_TTS.get(m.group(1))
            if tts:
                return tts
        # Brightness pattern "X%"
        for word in cmd.split():
            if word.endswith("%") and word[:-1].isdigit():
                return f"Helligkeit auf {word[:-1]} Prozent."
        # Color
        for key, label in _COLOR_TTS.items():
            if key in cmd:
                return f"Farbe auf {label} gewechselt."
        # Power
        if any(w in cmd for w in ("aus", "off", "ausschalten")):
            return "Lichter ausgeschaltet."
        if any(w in cmd for w in ("ein", "on", "einschalten", "anmachen")):
            return "Lichter angeschaltet."
        # "an" only at word boundary to avoid "anmachen" double-match
        if _re.search(r"\ban\b", cmd):
            return "Lichter angeschaltet."

    return None


def _speak_smarthome(text: str) -> None:
    """Fire-and-forget TTS for smarthome confirmations (non-blocking)."""
    if _VOICE_OK and tts is not None and text:
        asyncio.get_event_loop().run_in_executor(None, tts.speak, text)


def _smarthome(request: Request) -> Any:
    return getattr(request.app.state, "smarthome", None)


class SmartHomeControlRequest(BaseModel):
    action: str = Field(..., description="turn_on/turn_off/brightness/color/scene/command")
    device: str | None = None
    scene: str | None = None
    command: str | None = None
    level: int | None = Field(default=None, ge=0, le=100)
    color: str | None = None


class SmartHomeSceneRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class SmartHomeLocationRequest(BaseModel):
    lat: float
    lon: float


class SmartHomeAutomationUpdateRequest(BaseModel):
    enabled: bool


@app.get("/smarthome/status")
async def smarthome_status(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sh = _smarthome(request)
    if sh is None:
        raise HTTPException(status_code=503, detail="Smart Home nicht verfügbar.")
    return sh.status()


@app.get("/smarthome/devices")
async def smarthome_devices(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sh = _smarthome(request)
    if sh is None:
        raise HTTPException(status_code=503, detail="Smart Home nicht verfügbar.")
    return {"devices": sh.get_all_devices()}


@app.post("/smarthome/devices/refresh")
async def smarthome_refresh(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sh = _smarthome(request)
    if sh is None:
        raise HTTPException(status_code=503, detail="Smart Home nicht verfügbar.")
    await sh.registry.refresh_all()
    return {"ok": True, "devices": len(sh.registry.get_all())}


@app.post("/smarthome/control")
async def smarthome_control(
    payload: SmartHomeControlRequest,
    request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    sh = _smarthome(request)
    if sh is None:
        raise HTTPException(status_code=503, detail="Smart Home nicht verfügbar.")
    from .smarthome.tools.smarthome_tools import execute_smarthome_tool
    result = await execute_smarthome_tool(
        sh,
        action=payload.action,
        command=payload.command,
        scene=payload.scene,
        device=payload.device,
        level=payload.level,
        color=payload.color,
    )
    tts_msg = _smarthome_tts_text(
        payload.action, payload.command, payload.scene,
        payload.color, payload.level, result,
    )
    if tts_msg:
        _speak_smarthome(tts_msg)
    return {"result": result}


@app.get("/smarthome/scenes")
async def smarthome_scenes(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sh = _smarthome(request)
    if sh is None:
        raise HTTPException(status_code=503, detail="Smart Home nicht verfügbar.")
    return {"scenes": sh.get_scenes()}


@app.post("/smarthome/scenes/run")
async def smarthome_run_scene(
    payload: SmartHomeSceneRequest,
    request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    sh = _smarthome(request)
    if sh is None:
        raise HTTPException(status_code=503, detail="Smart Home nicht verfügbar.")
    result = await sh.run_scene(payload.name)
    tts_msg = _SCENE_TTS.get(payload.name.lower().replace(" ", "_").replace("-", "_"))
    if tts_msg:
        _speak_smarthome(tts_msg)
    return {"result": result}


@app.get("/smarthome/automations")
async def smarthome_automations(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sh = _smarthome(request)
    if sh is None:
        raise HTTPException(status_code=503, detail="Smart Home nicht verfügbar.")
    return {"automations": sh.get_automations()}


@app.put("/smarthome/automations/{automation_id}")
async def smarthome_update_automation(
    automation_id: str,
    payload: SmartHomeAutomationUpdateRequest,
    request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    sh = _smarthome(request)
    if sh is None:
        raise HTTPException(status_code=503, detail="Smart Home nicht verfügbar.")
    if sh._automations is None:
        raise HTTPException(status_code=503, detail="Automations nicht geladen.")
    ok = sh._automations.enable(automation_id, payload.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="Automation nicht gefunden.")
    return {"ok": True, "id": automation_id, "enabled": payload.enabled}


@app.get("/smarthome/energy")
async def smarthome_energy(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sh = _smarthome(request)
    if sh is None:
        raise HTTPException(status_code=503, detail="Smart Home nicht verfügbar.")
    return await sh.get_energy()


@app.post("/smarthome/location")
async def smarthome_location(
    payload: SmartHomeLocationRequest,
    request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    sh = _smarthome(request)
    if sh is None:
        raise HTTPException(status_code=503, detail="Smart Home nicht verfügbar.")
    return await sh.update_location(payload.lat, payload.lon)


@app.post("/smarthome/adapters/{platform}/enable")
async def smarthome_enable_adapter(
    platform: str,
    request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    sh = _smarthome(request)
    if sh is None:
        raise HTTPException(status_code=503, detail="Smart Home nicht verfügbar.")
    result = await sh.enable_adapter(platform)
    return {"result": result}


# --- Productivity API ---------------------------------------------------- #

def _productivity(request: Request) -> Any:
    return getattr(request.app.state, "productivity", None)


class _AddTaskRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    priority: int = Field(default=2, ge=1, le=4)
    due_date: str | None = None
    project: str | None = None
    context: str = "work"


@app.get("/productivity/tasks/today")
def productivity_tasks_today(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    pm = _productivity(request)
    if pm is None:
        raise HTTPException(status_code=503, detail="Productivity layer unavailable.")
    return {"tasks": pm.tasks.get_today_tasks()}


@app.get("/productivity/tasks/top3")
def productivity_tasks_top3(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    pm = _productivity(request)
    if pm is None:
        raise HTTPException(status_code=503, detail="Productivity layer unavailable.")
    return {"tasks": pm.tasks.get_top3()}


@app.post("/productivity/tasks")
def productivity_add_task(
    payload: _AddTaskRequest,
    request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    pm = _productivity(request)
    if pm is None:
        raise HTTPException(status_code=503, detail="Productivity layer unavailable.")
    tid = pm.tasks.add_task(
        payload.title,
        priority=payload.priority,
        due_date=payload.due_date,
        project_name=payload.project,
        context=payload.context,
    )
    return {"ok": True, "task_id": tid}


@app.get("/productivity/focus/time-today")
def productivity_time_today(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    pm = _productivity(request)
    if pm is None:
        raise HTTPException(status_code=503, detail="Productivity layer unavailable.")
    return {"summary": pm.focus.get_time_today()}


@app.get("/productivity/analytics/today")
def productivity_analytics_today(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    pm = _productivity(request)
    if pm is None:
        raise HTTPException(status_code=503, detail="Productivity layer unavailable.")
    return pm.analytics.daily_score()


# --- Entertainment API ---------------------------------------------------- #

@app.get("/entertainment/watchlist")
def get_watchlist(
    request: Request,
    _: str = Depends(require_token),
) -> JSONResponse:
    ent = getattr(request.app.state, "entertainment", None)
    if not ent:
        return JSONResponse({"items": []})
    items = ent.watchlist.get_list("want_to_watch")
    return JSONResponse({"items": items})


@app.post("/entertainment/watchlist/add")
async def add_to_watchlist(
    request: Request,
    _: str = Depends(require_token),
) -> JSONResponse:
    body = await request.json()
    ent = getattr(request.app.state, "entertainment", None)
    if not ent:
        return JSONResponse({"error": "not available"})
    msg, err = ent.watchlist.add(
        body.get("title", ""),
        type=body.get("type", "unknown"),
    )
    return JSONResponse({"message": msg, "error": err})


@app.get("/entertainment/gaming/stats")
def gaming_stats(
    request: Request,
    _: str = Depends(require_token),
) -> JSONResponse:
    ent = getattr(request.app.state, "entertainment", None)
    if not ent:
        return JSONResponse({"stats": ""})
    msg, _ = ent.gaming.get_stats()
    return JSONResponse({"stats": msg})


# --- Finance -------------------------------------------------------------- #

def _finance(request: Request) -> Any:
    return getattr(request.app.state, "finance", None)


def _require_finance(request: Request) -> Any:
    fm = _finance(request)
    if fm is None:
        raise HTTPException(status_code=503, detail="Finance-Layer nicht verfügbar.")
    return fm


class ExpenseRequest(BaseModel):
    amount: float = Field(..., gt=0)
    merchant: str = ""
    description: str = ""
    category: str | None = None


class BudgetRequest(BaseModel):
    category: str = Field(..., min_length=1)
    monthly_limit: float = Field(..., gt=0)


class WatchRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    quantity: float = 0
    target_above: float | None = None
    target_below: float | None = None


@app.post("/finance/expenses")
async def finance_add_expense(body: ExpenseRequest, request: Request,
                              token: str = Depends(require_token)) -> dict[str, Any]:
    fm = _require_finance(request)
    return fm.expenses.add_expense(body.amount, body.merchant, body.description,
                                   body.category)


@app.get("/finance/summary")
async def finance_summary(request: Request,
                          token: str = Depends(require_token)) -> dict[str, Any]:
    fm = _require_finance(request)
    return fm.expenses.monthly_summary()


@app.post("/finance/budgets")
async def finance_set_budget(body: BudgetRequest, request: Request,
                             token: str = Depends(require_token)) -> dict[str, Any]:
    fm = _require_finance(request)
    return {"spoken": fm.expenses.set_budget(body.category, body.monthly_limit)}


@app.get("/finance/watchlist")
async def finance_watchlist(request: Request,
                            token: str = Depends(require_token)) -> dict[str, Any]:
    fm = _require_finance(request)
    return {"watchlist": await asyncio.to_thread(fm.market.refresh_prices)}


@app.post("/finance/watchlist")
async def finance_watch_add(body: WatchRequest, request: Request,
                            token: str = Depends(require_token)) -> dict[str, Any]:
    fm = _require_finance(request)
    return fm.market.add_to_watchlist(
        body.symbol, asset_type="crypto" if "-" in body.symbol else "stock",
        quantity=body.quantity, target_above=body.target_above,
        target_below=body.target_below)


@app.delete("/finance/watchlist/{symbol}")
async def finance_watch_remove(symbol: str, request: Request,
                               token: str = Depends(require_token)) -> dict[str, Any]:
    fm = _require_finance(request)
    return {"spoken": fm.market.remove_from_watchlist(symbol)}


@app.get("/finance/portfolio")
async def finance_portfolio(request: Request,
                            token: str = Depends(require_token)) -> dict[str, Any]:
    fm = _require_finance(request)
    return await asyncio.to_thread(fm.market.portfolio_value)


@app.get("/finance/price/{symbol}")
async def finance_price(symbol: str, request: Request,
                        token: str = Depends(require_token)) -> dict[str, Any]:
    fm = _require_finance(request)
    # fetch_price does a blocking httpx.get — offload so it can't freeze the
    # event loop (and every WS client / voice reply) for up to 8s.
    p = await asyncio.to_thread(fm.market.fetch_price, symbol)
    if p is None:
        raise HTTPException(status_code=404, detail=f"no price for {symbol}")
    return p


# --- Communication -------------------------------------------------------- #

def _comm(request: Request) -> Any:
    return getattr(request.app.state, "communication", None)


def _require_comm(request: Request) -> Any:
    cm = _comm(request)
    if cm is None:
        raise HTTPException(status_code=503, detail="Communication-Layer nicht verfügbar.")
    return cm


class MsgSendRequest(BaseModel):
    platform: str = Field(default="imessage")
    contact: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)


class BroadcastRequest(BaseModel):
    message: str = Field(..., min_length=1)
    contacts: list[str]
    platform: str = "imessage"


class ConfirmRequest(BaseModel):
    pending_id: str = Field(..., min_length=1)


class CallRequest(BaseModel):
    contact: str = Field(..., min_length=1)
    method: str = "auto"


class CallbackReminderRequest(BaseModel):
    contact: str = Field(..., min_length=1)
    when: str = "later"


class EmailSendRequest(BaseModel):
    to: str
    subject: str
    body: str


class TranslateRequest(BaseModel):
    text: str = Field(..., min_length=1)
    target_lang: str = "en"
    source_lang: str = "auto"


class DndRequest(BaseModel):
    enabled: bool
    until: str | None = None


class QuietHoursRequest(BaseModel):
    start: str
    end: str


class AutoReplyRequest(BaseModel):
    message: str | None = None
    platforms: list[str] | None = None
    exceptions: list[str] | None = None


class OOORequest(BaseModel):
    start_date: str
    end_date: str
    message: str | None = None


class DraftRequest(BaseModel):
    platform: str = "twitter"
    topic: str = Field(..., min_length=1)
    style: str = "professional"


# ── messaging ───────────────────────────────────────────────────────────── #

@app.get("/communication/messages/unread")
async def comm_unread(request: Request, token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return await cm.messaging.get_all_unread()


@app.post("/communication/messages/send")
async def comm_send(body: MsgSendRequest, request: Request,
                    token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    # Always stages and returns a pending_id. The actual send requires a
    # second call to /messages/confirm referencing that id — a real two-step,
    # not a caller-set boolean that fires in the same request.
    return await cm.messaging.send(body.platform, body.contact, body.message)


@app.post("/communication/messages/broadcast")
async def comm_broadcast(body: BroadcastRequest, request: Request,
                         token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return await cm.messaging.broadcast(body.message, body.contacts, [body.platform])


@app.post("/communication/messages/confirm")
async def comm_confirm(body: ConfirmRequest, request: Request,
                       token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return {"result": await cm.messaging.confirm_pending(body.pending_id)}


@app.get("/communication/messages/{platform}/{contact}")
async def comm_conversation(platform: str, contact: str, request: Request,
                            token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return {"text": await cm.messaging.read_messages(platform, contact, 10)}


# ── calls ───────────────────────────────────────────────────────────────── #

@app.post("/communication/calls/make")
async def comm_call(body: CallRequest, request: Request,
                    token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return await cm.calls.make_call(body.contact, body.method)


@app.get("/communication/calls/missed")
async def comm_missed(request: Request, token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return {"spoken": await cm.calls.get_missed_calls()}


@app.post("/communication/calls/callback-reminder")
async def comm_callback(body: CallbackReminderRequest, request: Request,
                        token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return {"spoken": await cm.calls.set_callback_reminder(body.contact, body.when)}


# ── email ───────────────────────────────────────────────────────────────── #

@app.get("/communication/email/summary")
async def comm_email_summary(request: Request, token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return {"summary": await cm.email.get_all_accounts_summary()}


@app.post("/communication/email/send")
async def comm_email_send(body: EmailSendRequest, request: Request,
                          token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    # Two-step: stages + returns pending_id; confirm via /email/confirm.
    return await cm.email.send(body.to, body.subject, body.body)


@app.post("/communication/email/confirm")
async def comm_email_confirm(body: ConfirmRequest, request: Request,
                             token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return {"result": await cm.email.confirm_pending(body.pending_id)}


@app.get("/communication/email/templates")
async def comm_email_templates(request: Request, token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return {"templates": cm.email.list_templates()}


# ── notifications ───────────────────────────────────────────────────────── #

@app.get("/communication/notifications/pending")
async def comm_notif_pending(request: Request, token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return {"pending": cm.notifications.get_pending()}


@app.post("/communication/notifications/dnd")
async def comm_dnd(body: DndRequest, request: Request,
                   token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return cm.notifications.set_dnd(body.enabled, body.until)


@app.post("/communication/notifications/quiet-hours")
async def comm_quiet(body: QuietHoursRequest, request: Request,
                     token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return cm.notifications.set_quiet_hours(body.start, body.end)


# ── translation ─────────────────────────────────────────────────────────── #

@app.post("/communication/translate")
async def comm_translate(body: TranslateRequest, request: Request,
                         token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return {"translation": await cm.translator.translate(
        body.text, body.target_lang, body.source_lang)}


# ── social ──────────────────────────────────────────────────────────────── #

@app.get("/communication/social/birthdays")
async def comm_birthdays(request: Request, token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return {"spoken": await cm.social.get_birthday_reminders()}


@app.post("/communication/social/draft")
async def comm_draft(body: DraftRequest, request: Request,
                     token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return await cm.social.draft_post(body.platform, body.topic, body.style)


# ── automation ──────────────────────────────────────────────────────────── #

@app.post("/communication/automation/auto-reply")
async def comm_autoreply(body: AutoReplyRequest, request: Request,
                         token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return await cm.automation.enable_auto_reply(
        message=body.message or settings.AUTO_REPLY_MESSAGE,
        platforms=body.platforms, exceptions=body.exceptions)


@app.post("/communication/automation/out-of-office")
async def comm_ooo(body: OOORequest, request: Request,
                   token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return await cm.automation.enable_out_of_office(
        body.start_date, body.end_date, body.message)


@app.get("/communication/automation/followups")
async def comm_followups(request: Request, token: str = Depends(require_token)) -> dict[str, Any]:
    cm = _require_comm(request)
    return {"followups": await cm.automation.check_followups()}


# --- Security & monitoring ------------------------------------------------ #

def _security(request: Request) -> Any:
    return getattr(request.app.state, "security", None)


def _require_security(request: Request) -> Any:
    sec = _security(request)
    if sec is None:
        raise HTTPException(status_code=503, detail="Security-Layer nicht verfügbar.")
    return sec


class VoiceEnrollRequest(BaseModel):
    # base64-encoded WAV/PCM samples (16 kHz int16). Optional — omitting
    # them triggers interactive mic enrollment server-side.
    samples_b64: list[str] | None = None


class CameraStartRequest(BaseModel):
    camera_index: int = 0
    sensitivity: str = Field(default="medium")
    force: bool = True  # explicit API call overrides CAMERA_ENABLED


class ArmRequest(BaseModel):
    mode: str = Field(default="away")


class TempAccessRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    level: str = Field(default="guest")
    duration_hours: int = Field(default=2, ge=1, le=72)
    allowed_commands: list[str] | None = None


class BreachCheckRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)


def _b64_to_bytes(s: str) -> bytes:
    import base64
    return base64.b64decode(s)


# ── voice auth ──────────────────────────────────────────────────────────── #

@app.post("/security/voice/enroll")
async def security_voice_enroll(
    body: VoiceEnrollRequest, request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    samples = ([_b64_to_bytes(s) for s in body.samples_b64]
               if body.samples_b64 else None)
    return await sec.voice_auth.enroll_owner(samples=samples)


@app.post("/security/voice/verify")
async def security_voice_verify(
    request: Request, audio: UploadFile,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    data = await audio.read()
    return await sec.voice_auth.verify_speaker(data)


# ── camera ──────────────────────────────────────────────────────────────── #

@app.post("/security/camera/start")
async def security_camera_start(
    body: CameraStartRequest, request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return await sec.camera.start_monitoring(
        camera_index=body.camera_index, sensitivity=body.sensitivity,
        force=body.force,
    )


@app.post("/security/camera/stop")
async def security_camera_stop(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return sec.camera.stop_monitoring()


@app.get("/security/camera/snapshot")
async def security_camera_snapshot(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return {"description": await sec.camera.whos_at_door()}


@app.get("/security/camera/events")
async def security_camera_events(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    import time as _t
    return {"events": sec.db.camera_events_since(_t.time() - 86400),
            "summary": await sec.camera.get_daily_summary()}


# ── home security ───────────────────────────────────────────────────────── #

@app.get("/security/home/status")
async def security_home_status(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return await sec.home.get_security_status()


@app.post("/security/home/arm")
async def security_home_arm(
    body: ArmRequest, request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return {"spoken": await sec.home.arm_system(body.mode)}


@app.post("/security/home/disarm")
async def security_home_disarm(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return {"spoken": await sec.home.disarm_system()}


@app.get("/security/home/checklist")
async def security_home_checklist(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return {"spoken": await sec.home.leaving_checklist()}


# ── digital security ────────────────────────────────────────────────────── #

@app.get("/security/digital/network")
async def security_digital_network(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return await sec.digital.check_network()


@app.get("/security/digital/report")
async def security_digital_report(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return {"report": await sec.digital.daily_security_report()}


@app.post("/security/digital/breach-check")
async def security_digital_breach(
    body: BreachCheckRequest, request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return await sec.digital.check_data_breaches(body.email)


# ── system monitor ──────────────────────────────────────────────────────── #

@app.get("/security/system/health")
async def security_system_health(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return sec.system.get_system_health().to_dict()


@app.get("/security/system/processes")
async def security_system_processes(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return {"processes": sec.system.get_top_processes(5)}


@app.get("/security/system/jarvis-health")
async def security_system_jarvis(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return sec.system.check_jarvis_health()


# ── emergency ───────────────────────────────────────────────────────────── #

@app.post("/security/emergency/sos")
async def security_emergency_sos(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return await sec.emergency.trigger_sos()


@app.post("/security/emergency/cancel")
async def security_emergency_cancel(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return await sec.emergency.cancel_alarm()


@app.get("/security/emergency/contacts")
async def security_emergency_contacts(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return {"contacts": sec.emergency.get_contacts()}


# ── access control ──────────────────────────────────────────────────────── #

@app.post("/security/access/temp")
async def security_access_temp(
    body: TempAccessRequest, request: Request,
    token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return await sec.access.create_temp_access(
        name=body.name, level=body.level,
        duration_hours=body.duration_hours,
        allowed_commands=body.allowed_commands,
    )


@app.delete("/security/access/revoke")
async def security_access_revoke(
    name: str, request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return {"revoked": await sec.access.revoke_access(name)}


@app.get("/security/access/sessions")
async def security_access_sessions(
    request: Request, token: str = Depends(require_token),
) -> dict[str, Any]:
    sec = _require_security(request)
    return {"sessions": await sec.access.get_active_sessions()}


# --- Web UI --------------------------------------------------------------- #

@app.get("/web")
def web_ui() -> FileResponse:
    """Serve the mobile-friendly iPhone web client."""
    index = WEB_DIR / "index.html"
    if not index.exists():
        return JSONResponse(
            {"error": "web UI missing"}, status_code=500
        )  # pragma: no cover
    return FileResponse(index, media_type="text/html")


# PWA — the iPhone-installable progressive web app. Lives under
# clients/pwa/. html=True makes FastAPI fall back to index.html for
# the SPA-style entry point so /app, /app/, /app/index.html all work.
# iOS Safari needs HTTPS for Add-to-Home-Screen + mic access — see
# README_PWA.md for the cert setup.
if PWA_DIR.exists():
    app.mount("/app", StaticFiles(directory=PWA_DIR, html=True), name="pwa")


# --- Entry point ---------------------------------------------------------- #

def run() -> None:
    """``python -m server.main`` launches uvicorn with our settings.

    If JARVIS_SSL_CERT + JARVIS_SSL_KEY are set in .env (eg. pointing
    at the project's Tailscale-issued *.ts.net.crt / .key files) we
    boot uvicorn in HTTPS mode. That's required for the iPhone PWA:
    iOS Safari blocks getUserMedia + service-worker install on plain
    HTTP. Without the cert vars we fall through to HTTP — fine for
    Electron-on-localhost dev, broken for iPhone install.
    """
    import uvicorn

    # Persist all output (logging-module records + tee'd print() lines) to a
    # rotating logs/jarvis.log. Done here (the real entrypoint), not in the
    # lifespan, so pytest/TestClient output capture is untouched.
    from .common.logging_setup import configure_logging
    configure_logging(settings.LOG_DIR)

    kwargs: dict[str, Any] = dict(
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
        log_level="info",
    )
    if settings.JARVIS_SSL_CERT and settings.JARVIS_SSL_KEY:
        cert_path = Path(settings.JARVIS_SSL_CERT).expanduser()
        key_path  = Path(settings.JARVIS_SSL_KEY).expanduser()
        if not cert_path.exists() or not key_path.exists():
            print(f"[JARVIS] ⚠ SSL files missing: cert={cert_path} key={key_path}")
            print("[JARVIS] ⚠ falling back to HTTP — iPhone PWA won't work.")
        else:
            kwargs["ssl_certfile"] = str(cert_path)
            kwargs["ssl_keyfile"]  = str(key_path)

    uvicorn.run("server.main:app", **kwargs)


if __name__ == "__main__":
    run()
