-- JARVIS global hotkeys for Hammerspoon.
--
-- Copy this file to ~/.hammerspoon/init.lua (or `cat` it in if you
-- already have other Hammerspoon config). Reload Hammerspoon's
-- config from the menu-bar icon for the bindings to take effect.
--
-- Default bindings:
--   ⌃⌥⌘J  → start JARVIS  (idempotent — focuses existing HUD)
--   ⌃⌥⌘K  → stop JARVIS   (AppleScript quit + audio-safe cleanup)
--
-- Start routes through the `JARVIS Launcher.app` bundle in
-- /Applications so macOS can attach the necessary Microphone and
-- Speech Recognition TCC permissions to it. See install.sh for
-- the bundle installation.

local JARVIS_ROOT = os.getenv("HOME") .. "/Documents/JARVIS"
local MOD = {"ctrl", "alt", "cmd"}

-- Allow AppleScript control so the config can be reloaded remotely
-- (useful for dev/debug). Cheap, no other side effects.
hs.allowAppleScript(true)

hs.hotkey.bind(MOD, "J", function()
    -- Use NSWorkspace.launchApplication (= hs.application.launchOrFocus)
    -- rather than `hs.execute("open …")`. The shell `open` runs as a
    -- child of /bin/sh which is a child of Hammerspoon, so the
    -- spawned chain stays inside Hammerspoon's mach coalition — TCC
    -- then pins the responsible process back on Hammerspoon and
    -- crashes the voice loop with __TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__.
    -- launchOrFocus goes straight through NSWorkspace, which puts
    -- the launched bundle in its OWN coalition. With the bundle
    -- ad-hoc signed + carrying the Mic/Speech usage descriptions in
    -- Info.plist, TCC then correctly anchors permissions on the
    -- launcher instead of on Hammerspoon.
    hs.application.launchOrFocus("JARVIS Launcher")
    hs.alert.show("JARVIS starting", 0.7)
end)

hs.hotkey.bind(MOD, "K", function()
    -- Stop doesn't need TCC permissions (just signals + AppleScript
    -- quit) so it can run as a direct script call.
    local t = hs.task.new(JARVIS_ROOT .. "/quit.command", nil)
    t:start()
    hs.alert.show("JARVIS stopping", 0.7)
end)

hs.alert.show("Hammerspoon: JARVIS hotkeys ready", 1.2)
