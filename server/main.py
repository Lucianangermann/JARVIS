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
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .auth import authorize_websocket, require_token
from .brain import Brain
from .config import settings
from . import events
from .mac_control import dispatcher as mac_dispatcher
from .mac_control import kill_switch as mac_kill_switch

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


# --- App lifecycle -------------------------------------------------------- #

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    print("[JARVIS] starting up")
    print(f"[JARVIS] model={settings.MODEL}")
    print(f"[JARVIS] listening on http://{settings.HOST}:{settings.PORT}")
    print(f"[JARVIS] web UI at  http://{settings.HOST}:{settings.PORT}/web")
    if not _VOICE_OK:
        print(f"[JARVIS] voice stack NOT loaded ({_VOICE_ERR}) — /audio disabled,")
        print("[JARVIS] text endpoints still work. Install requirements-voice.txt to enable.")
    app.state.brain = Brain()

    # Capture the running asyncio loop for cross-thread event publishes
    # from voice_loop's thread. Must happen BEFORE the voice thread
    # starts, otherwise its first publish() lands a no-op.
    events.set_loop(asyncio.get_running_loop())

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

    try:
        yield
    finally:
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
                reply = await asyncio.to_thread(brain.reply, token, user_text)
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


# --- Entry point ---------------------------------------------------------- #

def run() -> None:
    """``python -m server.main`` launches uvicorn with our settings."""
    import uvicorn

    uvicorn.run(
        "server.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()
