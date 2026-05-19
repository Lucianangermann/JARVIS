#!/usr/bin/env bash
# Hotkey stop for the JARVIS Electron HUD. SIGTERM goes to Electron;
# its will-quit handler in ui/main.js cleans up the Python server
# child (SIGTERM → uvicorn graceful shutdown → SIGKILL after 4 s if
# it ignores us). We give the whole chain ~5 s, then sweep any
# stragglers with SIGKILL so the user isn't left with phantom
# processes after a press.
set -u

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG="$PROJECT_ROOT/logs/launch.log"
mkdir -p "$PROJECT_ROOT/logs"

if pgrep -f "Electron.*${PROJECT_ROOT}/ui" >/dev/null 2>&1; then
  echo "[$(date '+%F %T')] stopping HUD (SIGTERM)" >> "$LOG"
  pkill -TERM -f "Electron.*${PROJECT_ROOT}/ui" || true
  # Give Electron's will-quit + the server's lifespan shutdown time
  # to run cleanly. 5 s covers the worst-case 4 s SIGKILL fallback
  # inside killServer().
  for _ in 1 2 3 4 5; do
    sleep 1
    pgrep -f "Electron.*${PROJECT_ROOT}/ui" >/dev/null 2>&1 || break
  done
  if pgrep -f "Electron.*${PROJECT_ROOT}/ui" >/dev/null 2>&1; then
    echo "[$(date '+%F %T')] Electron stuck — SIGKILL" >> "$LOG"
    pkill -KILL -f "Electron.*${PROJECT_ROOT}/ui" || true
  fi
  # Belt-and-braces sweep for an orphaned server child. The .venv
  # python is a symlink to /usr/local/Cellar/...; ps reports the
  # RESOLVED path, so a pattern anchored to the project's .venv path
  # silently misses. Match on the import target "server.main" — it
  # appears in argv unchanged and uniquely identifies our process.
  if pgrep -f "python.* -m server\.main" >/dev/null 2>&1; then
    echo "[$(date '+%F %T')] orphaned server child — SIGTERM" >> "$LOG"
    pkill -TERM -f "python.* -m server\.main" || true
    sleep 2
    if pgrep -f "python.* -m server\.main" >/dev/null 2>&1; then
      echo "[$(date '+%F %T')] server still alive — SIGKILL" >> "$LOG"
      pkill -KILL -f "python.* -m server\.main" || true
    fi
  fi
  echo "[$(date '+%F %T')] HUD stopped" >> "$LOG"
else
  echo "[$(date '+%F %T')] stop: HUD not running — no-op" >> "$LOG"
fi
exit 0
