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
    """Long-lived text chat over a single WS.

    Wire format (both directions): one JSON message per turn,
        client → server  {"text": "..."}
        server → client  {"reply": "..."}    or   {"error": "..."}
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

    try:
        while True:
            payload = await websocket.receive_json()
            user_text = _sanitize(str(payload.get("text", "")))
            if not user_text:
                await websocket.send_json({"error": "empty message"})
                continue

            print(f"[CLIENT: {tag}] [YOU] {user_text}")
            try:
                reply = brain.reply(token, user_text)
            except HTTPException as exc:
                await websocket.send_json({"error": exc.detail})
                continue

            print(f"[JARVIS] {reply}")
            await websocket.send_json({"reply": reply})
    except WebSocketDisconnect:
        print(f"[JARVIS] ws disconnected client={tag}")
    except Exception as exc:  # noqa: BLE001
        print(f"[JARVIS] ws error: {exc}")
        with contextlib.suppress(Exception):
            await websocket.send_json({"error": str(exc)})
            await websocket.close()


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
