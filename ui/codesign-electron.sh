#!/usr/bin/env bash
# Ad-hoc code-sign the Electron binary that npm dropped under
# node_modules/electron/dist/Electron.app, so macOS TCC can track
# the app's identity and persist the Microphone (and Camera /
# Screen Recording / etc.) permission grant the user gives it.
#
# Why this matters:
#   * npm installs Electron as a plain-tarball binary with NO code
#     signature. macOS Sequoia's TCC silently denies permission
#     prompts for unsigned binaries instead of asking the user, so
#     the JARVIS voice loop receives all-zero mic buffers and the
#     user has no way to grant access through System Settings
#     (which only shows apps that have ALREADY asked).
#   * The original commit 7db57b0 fixed this by ad-hoc signing the
#     bundle manually after npm install, but that step has to be
#     re-run after every `npm install` / `npm ci` or a fresh
#     checkout because npm overwrites node_modules. Wiring it into
#     postinstall makes it automatic.
#
# Safe to run multiple times — codesign --force will replace any
# existing signature (ad-hoc or otherwise) without complaint. No
# sudo required; we sign with the ad-hoc identity ("-"), which
# produces a stable cdhash for TCC even without a developer cert.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$SCRIPT_DIR/node_modules/electron/dist/Electron.app"

if [ ! -d "$APP" ]; then
  echo "[codesign-electron] $APP not found — skipping (electron not installed yet?)"
  exit 0
fi

# Add NSSpeechRecognitionUsageDescription to the Info.plist if it
# isn't there. Electron ships with NSMicrophone + NSCamera usage
# descriptions but NOT speech recognition, so JARVIS' stt_macos
# (SFSpeechRecognizer / Apple's Speech.framework) trips TCC and
# the Python child SIGABRTs with a "TCC namespace" termination
# pointing at this exact missing key. plutil -insert is a no-op
# when the key already exists (it errors), so we test first.
PLIST="$APP/Contents/Info.plist"
if ! plutil -extract NSSpeechRecognitionUsageDescription raw "$PLIST" >/dev/null 2>&1; then
  echo "[codesign-electron] adding NSSpeechRecognitionUsageDescription to Info.plist"
  plutil -insert NSSpeechRecognitionUsageDescription \
    -string "JARVIS uses Apple's Speech Recognition to transcribe voice commands locally." \
    "$PLIST"
fi

# --force: replace any existing signature
# --deep:  recursively sign nested Mach-O binaries (Electron Helper etc.)
# --sign -: ad-hoc identity. No keychain, no Apple ID required.
# IMPORTANT: must run AFTER the Info.plist edit above — codesign
# embeds the plist's hash in the signature, so editing the plist
# after signing invalidates the signature on macOS Sequoia.
echo "[codesign-electron] signing $APP"
codesign --force --deep --sign - "$APP"

# Quick verification so a broken signing step is loud, not silent.
codesign -dv "$APP" 2>&1 | grep -E "^Signature=" || {
  echo "[codesign-electron] WARNING: codesign verify did not return a Signature line"
}
