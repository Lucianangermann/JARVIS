#!/usr/bin/env bash
# Install the JARVIS server as a launchd user agent: auto-start at login +
# restart on crash. Run once:  bash deploy/install-launchd.command
# Uninstall:  launchctl unload ~/Library/LaunchAgents/com.jarvis.server.plist
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.jarvis.server"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -x "$PROJECT/.venv/bin/python" ]; then
  echo "✗ $PROJECT/.venv/bin/python not found — create the venv first."
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT/logs"
# Substitute the absolute project path into the template.
sed "s#__PROJECT__#$PROJECT#g" "$PROJECT/deploy/com.jarvis.server.plist" > "$DEST"

# Reload (idempotent): unload an existing one first, then load.
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"

echo "✓ Installed $LABEL → $DEST"
echo "  JARVIS will now start at login and restart on crash."
echo "  Logs: $PROJECT/logs/launchd.{out,err}.log"
echo "  Stop/disable:  launchctl unload \"$DEST\""
echo
echo "⚠ Headless mode — don't also run the Electron HUD launcher (port :8000 clash)."
