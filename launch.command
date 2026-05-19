#!/usr/bin/env bash
# Hotkey launcher for the JARVIS Electron HUD.
# Bound via Shortcuts.app → "Run Shell Script" with a global keyboard
# shortcut. Idempotent: if the Electron HUD is already running we
# just bring it to the front instead of spawning a second instance
# (which would also spawn a second uvicorn and fail to bind to :8000).
set -u

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG="$PROJECT_ROOT/logs/launch.log"
mkdir -p "$PROJECT_ROOT/logs"

# Already running? Two checks because Electron and the Python server
# can outlive each other (a SIGTERM'd Electron may leave its child
# python alive for a few seconds while voice_loop shuts down; an
# Electron crash leaves an orphaned server). If EITHER is up we
# refuse to spawn a new pair — a second python would crash on
# port 8000 in use and Electron alone would talk to the wrong server.
if pgrep -f "Electron.*${PROJECT_ROOT}/ui" >/dev/null 2>&1 \
   || pgrep -f "python.* -m server\.main" >/dev/null 2>&1; then
  echo "[$(date '+%F %T')] HUD/server already running — bringing to front" >> "$LOG"
  osascript -e 'tell application "System Events" to set frontmost of every process whose name is "Electron" to true' >/dev/null 2>&1 || true
  exit 0
fi

echo "[$(date '+%F %T')] launching HUD" >> "$LOG"
# nohup + detached so the shortcut doesn't block waiting on us; the
# child keeps running after this script exits. stdout/stderr go to
# the log so we can diagnose boot failures.
cd "$PROJECT_ROOT/ui" || exit 1
nohup npm start >>"$LOG" 2>&1 &
disown
exit 0
