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

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import authorize_websocket, require_token
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

    # Productivity layer
    try:
        from .productivity.productivity_manager import ProductivityManager
        productivity = ProductivityManager(Path("data/jarvis.db"))
        productivity.start()
        app.state.productivity = productivity
        app.state.brain._productivity = productivity
        print("[PRODUCTIVITY] wired to brain ✓")
    except Exception as exc:  # noqa: BLE001
        print(f"[PRODUCTIVITY] init failed: {exc}")

    # Entertainment layer
    try:
        from .entertainment.entertainment_manager import EntertainmentManager
        entertainment = EntertainmentManager(
            Path("data/jarvis.db"),
            app.state.brain.client,
            getattr(app.state, "smarthome", None),
        )
        entertainment.start()
        app.state.entertainment = entertainment
        app.state.brain._entertainment = entertainment
        print("[ENTERTAINMENT] wired to brain ✓")
    except Exception as exc:  # noqa: BLE001
        print(f"[ENTERTAINMENT] init failed: {exc}")

    # On macOS, periodically drain the main-thread NSRunLoop so Cocoa
    # framework callbacks (Speech.framework's SFSpeechRecognizer in
    # particular) actually get delivered. Apple posts those completions
    # onto the main runloop; uvicorn's asyncio owns the main thread but
    # never pumps NSRunLoop, so without this task the callbacks queue
    # up forever and recognition tasks time out. runUntilDate_ with a
    # zero-second date is non-blocking — it just processes whatever's
    # already pending and returns immediately, so the ~20 ms sleep is
    # the cost ceiling.
    runloop_pump_task: asyncio.Task | None = None
    if os.uname().sysname == "Darwin":
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
    token: str = Depends(require_token),
) -> ChatResponse:
    user_text = _sanitize(payload.text)
    if not user_text:
        raise HTTPException(status_code=400, detail="Empty message.")

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
