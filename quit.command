#!/usr/bin/env bash
# Hotkey stop for the JARVIS Electron HUD.
#
# We tried SIGTERM via pkill first — Electron's C++ signal handler
# captures it before Node has a chance to run our SIGTERM handler in
# ui/main.js, so will-quit never fires and the Python server child
# becomes an orphan. The fix is to ask the app to quit through
# AppleScript, which routes through Cocoa's normal app-quit lifecycle
# and DOES trigger will-quit → killServer() → SIGTERM the python
# child cleanly.
#
# Why we still avoid SIGKILL'ing python: when JARVIS_LOCAL_VOICE=1
# the server owns a PortAudio InputStream and a Speech.framework
# recogniser. SIGKILL mid-shutdown wedges the audio device until
# coreaudiod GCs, and the next launch's voice_loop aborts with
# SIGABRT trying to claim it.
set -u

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG="$PROJECT_ROOT/logs/launch.log"
mkdir -p "$PROJECT_ROOT/logs"

ELECTRON_PAT="Electron.*${PROJECT_ROOT}/ui"
SERVER_PAT="python.* -m server\.main"

if ! pgrep -f "$ELECTRON_PAT" >/dev/null 2>&1 \
   && ! pgrep -f "$SERVER_PAT" >/dev/null 2>&1; then
  echo "[$(date '+%F %T')] stop: nothing running — no-op" >> "$LOG"
  exit 0
fi

echo "[$(date '+%F %T')] stopping HUD (AppleScript quit)" >> "$LOG"
# Cocoa-driven quit so Electron's will-quit fires properly. The
# `with timeout` keeps us from blocking the user's shortcut forever
# if Electron is in a weird state.
osascript -e 'with timeout of 12 seconds
    try
        tell application "Electron" to quit
    end try
end timeout' >/dev/null 2>&1 || true

# Wait up to 15 s for the chain (Electron will-quit → killServer →
# python SIGTERM → uvicorn lifespan shutdown → voice_loop close) to
# wind down.
for i in $(seq 1 15); do
  sleep 1
  if ! pgrep -f "$ELECTRON_PAT" >/dev/null 2>&1 \
     && ! pgrep -f "$SERVER_PAT" >/dev/null 2>&1; then
    echo "[$(date '+%F %T')] HUD stopped cleanly after ${i}s" >> "$LOG"
    exit 0
  fi
done

# Electron is a GUI process — SIGKILL'ing it isn't audio-damaging,
# so this part is fine if it didn't honour the AppleScript quit.
# The Python child is left for Electron's will-quit to clean up; if
# it survived 15 s of cleanup it's stuck on something native and
# SIGKILL would only make the next launch's audio worse.
if pgrep -f "$ELECTRON_PAT" >/dev/null 2>&1; then
  echo "[$(date '+%F %T')] Electron didn't honour quit — SIGKILL Electron only" >> "$LOG"
  pkill -KILL -f "$ELECTRON_PAT" || true
fi
if pgrep -f "$SERVER_PAT" >/dev/null 2>&1; then
  echo "[$(date '+%F %T')] WARNING: python server still alive after 15s — leaving it." >> "$LOG"
  echo "[$(date '+%F %T')]   if it's stuck for good, kill it manually:" >> "$LOG"
  echo "[$(date '+%F %T')]     pkill -TERM -f 'python.* -m server.main'" >> "$LOG"
  echo "[$(date '+%F %T')]   SIGKILL would wedge the audio device for the next launch." >> "$LOG"
fi
exit 0
