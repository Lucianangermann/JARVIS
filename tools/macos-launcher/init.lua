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

hs.hotkey.bind(MOD, "J", function()
    -- `open -g` keeps Finder/Dock from foregrounding the launcher;
    -- the launcher is LSUIElement so there's no Dock icon anyway.
    hs.execute("open -g '/Applications/JARVIS Launcher.app'")
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
