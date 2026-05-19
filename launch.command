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

# Already running? Electron's main process shows up as "Electron" with
# our project path in its argv. pgrep -f matches against the whole
# command line so this is reliable.
if pgrep -f "Electron.*${PROJECT_ROOT}/ui" >/dev/null 2>&1; then
  echo "[$(date '+%F %T')] HUD already running — bringing to front" >> "$LOG"
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
