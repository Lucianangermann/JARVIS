#!/usr/bin/env bash
# Install the JARVIS Launcher .app bundle to /Applications and
# (optionally) drop the Hammerspoon config into ~/.hammerspoon.
#
# Why all this scaffolding: macOS 13+ blocks `+ Add app` in the
# Microphone / Speech Recognition privacy panels. Apps can only get
# in by REQUESTING access, and TCC requires the responsible process
# to ship a proper Info.plist with the right usage descriptions.
# We build a tiny launcher bundle to satisfy that requirement, then
# Hammerspoon's hotkey opens the bundle (instead of running the
# bare launch.command directly) so the spawned Python server can
# claim Mic + Speech.framework without getting SIGABRT'd by TCC.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$HERE/../.." && pwd)"
APP_DIR="/Applications/JARVIS Launcher.app"

echo "→ installing $APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
cp "$HERE/Info.plist"        "$APP_DIR/Contents/Info.plist"
cp "$HERE/JARVIS-Launcher"   "$APP_DIR/Contents/MacOS/JARVIS-Launcher"
chmod +x "$APP_DIR/Contents/MacOS/JARVIS-Launcher"

# Re-register with LaunchServices so `open` picks up the new bundle
# (or the updated executable / Info.plist on subsequent installs).
LS_REGISTER=/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister
"$LS_REGISTER" -f "$APP_DIR" >/dev/null 2>&1 || true
echo "  ✓ bundle in /Applications"

# Hammerspoon config — drop ours in unless the user already has one;
# in that case we tell them what to add so we don't clobber it.
HS_DIR="$HOME/.hammerspoon"
HS_INIT="$HS_DIR/init.lua"
mkdir -p "$HS_DIR"
if [ -e "$HS_INIT" ] && ! grep -q "JARVIS Launcher" "$HS_INIT" 2>/dev/null; then
  echo "→ ~/.hammerspoon/init.lua already exists and doesn't mention JARVIS."
  echo "  Append the contents of $HERE/init.lua to it (or replace), then"
  echo "  reload Hammerspoon."
else
  cp "$HERE/init.lua" "$HS_INIT"
  echo "  ✓ Hammerspoon config installed to $HS_INIT"
fi

echo
echo "Done. Next steps:"
echo "  1. Open Hammerspoon and 'Reload Config' from the menu-bar icon."
echo "  2. Press ⌃⌥⌘J once — macOS prompts for Microphone + Speech"
echo "     Recognition permission. Accept both."
echo "  3. JARVIS HUD should appear, voice loop initialised, no SIGABRT."
echo "  4. ⌃⌥⌘K stops everything cleanly."
