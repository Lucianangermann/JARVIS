"use strict";

const { app, BrowserWindow, globalShortcut, ipcMain, screen } = require("electron");
const path = require("node:path");

// Suppress the Chromium ANGLE/EGL error spam on Intel macOS:
//   "EGL Driver message (Error) eglQueryDeviceAttribEXT: Bad attribute"
// fires every frame because the Intel iGPU doesn't expose the
// attributes Chromium's GPU process queries.
//
// We CAN'T fix this by disabling hardware acceleration — on macOS,
// transparent BrowserWindows require HW accel to actually be see-
// through (otherwise Chromium paints the window's bounding rectangle
// as a solid layer, which is the visible "rectangle around the orb"
// bug). So instead we raise Chromium's log threshold to FATAL only,
// which silences the per-frame EGL noise without touching the GPU
// pipeline. Must be set BEFORE app.whenReady().
app.commandLine.appendSwitch("log-level", "3"); // 0=info 1=warn 2=err 3=fatal

// ---- window sizing per state ---- //
// IDLE shows the small orb in the bottom-right corner; ACTIVE/SPEAKING/
// PROCESSING expand to the full HUD. Resizing is done programmatically
// — the user can't drag a resize handle (no frame).
//
// Orb window MUST be much larger than the 110px circle itself, because
// the orb-breathe animation paints a box-shadow with up to ~72px blur
// at peak. If the window is tight to the circle, that glow gets
// CLIPPED to the window's rectangular bounds — which is exactly the
// "rectangle around the orb" people kept seeing. With a generous
// padding, the glow has room to fall off naturally and everything
// past it is true desktop-transparent.
//   circle  = 110
//   glow    = ~72 per side at peak
//   safety  = 30
//   total   = 110 + 2*(72+30) = 314 → round up to 320
const ORB = { width: 320, height: 320 };
const HUD = { width: 520, height: 360 };
// Margin from the screen edges when sized.
const EDGE_MARGIN = 24;

// Visual radius of the orb circle inside its window (px). Used to
// anchor the CIRCLE (not the window) to the screen corner in idle.
const ORB_VISUAL_RADIUS = 55;

let mainWindow = null;

// State-aware positioning. The HUD wants its panel-rectangle inset
// from the screen corner by EDGE_MARGIN — straightforward. The ORB
// state is different: the window is intentionally much bigger than
// the orb circle (so the breathing glow doesn't get clipped to a
// rectangle), so anchoring the WINDOW corner would push the orb far
// from the screen corner. Instead we anchor the orb's CIRCLE to the
// corner and let the window extend past the screen edge — macOS just
// clips the off-screen padding, which is empty glow space anyway.
function placeForState(win, state) {
  const { workArea } = screen.getPrimaryDisplay();
  if (state === "idle") {
    const orbCenterX = workArea.x + workArea.width  - EDGE_MARGIN - ORB_VISUAL_RADIUS;
    const orbCenterY = workArea.y + workArea.height - EDGE_MARGIN - ORB_VISUAL_RADIUS;
    win.setBounds({
      width:  ORB.width,
      height: ORB.height,
      x: Math.round(orbCenterX - ORB.width  / 2),
      y: Math.round(orbCenterY - ORB.height / 2),
    });
  } else {
    win.setBounds({
      width:  HUD.width,
      height: HUD.height,
      x: workArea.x + workArea.width  - HUD.width  - EDGE_MARGIN,
      y: workArea.y + workArea.height - HUD.height - EDGE_MARGIN,
    });
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: ORB.width,
    height: ORB.height,
    transparent: true,
    // Explicit fully-transparent background. Without this, some
    // Electron versions on macOS fall back to the default opaque
    // window color before the renderer paints, which can show as a
    // brief rectangle flash and (more annoyingly) as a faint constant
    // rectangle if the GPU compositor decides the layer needs a base.
    backgroundColor: "#00000000",
    frame: false,
    resizable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    hasShadow: false,
    // Kill macOS's default rounded-corner window mask too — we draw
    // our own circle, and a rounded-rect mask on top of a transparent
    // window can show its corners as faint arcs against bright wallpapers.
    roundedCorners: false,
    // No system-level vibrancy: it paints the WHOLE window rectangle
    // with a dark blur, which was visible as a rounded-rect around the
    // orb in idle state. We do glass effects per-element via CSS
    // backdrop-filter instead, so only the orb / HUD shape blurs the
    // content behind it. Everything outside those shapes is genuinely
    // transparent.
    // Don't steal focus from whatever the user is doing in other apps.
    // Important so keyboard input keeps going to the foreground app
    // when our overlay is just a status reflector.
    focusable: true,
    fullscreenable: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  // Float above full-screen apps on macOS.
  mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  mainWindow.setAlwaysOnTop(true, "screen-saver");

  placeForState(mainWindow, "idle");

  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));

  // Dev hint: pass --dev to open DevTools detached.
  if (process.argv.includes("--dev")) {
    mainWindow.webContents.openDevTools({ mode: "detach" });
  }
}

// ---- IPC: state-driven window resize ---- //
// Renderer drives the visual state; main process owns the OS window.
// One channel "jarvis:set-state" carries the new state name; main maps
// it to the matching dimensions and re-anchors to the bottom-right.
ipcMain.handle("jarvis:set-state", (_event, state) => {
  if (!mainWindow) return;
  placeForState(mainWindow, state);
  const target = state === "idle" ? ORB : HUD;
  return { width: target.width, height: target.height };
});

ipcMain.handle("jarvis:quit", () => {
  app.quit();
});

// ---- global hotkey: Cmd/Ctrl+J toggles HUD ↔ orb ---- //
// Main owns the OS-level shortcut registration; the actual state
// machine lives in the renderer. We just ping the renderer over IPC
// and let its setState() drive both the DOM update and the window
// resize (via the existing "jarvis:set-state" handler above) — no
// duplicated state tracking, one source of truth.
const TOGGLE_ACCELERATOR = "CommandOrControl+J";

function registerGlobalHotkey() {
  const ok = globalShortcut.register(TOGGLE_ACCELERATOR, () => {
    if (!mainWindow) return;
    mainWindow.webContents.send("jarvis:toggle");
    // Surface the window when summoning so the user can interact
    // immediately. focus() is a no-op when we're already focused, so
    // pressing the hotkey to dismiss doesn't have a side-effect.
    mainWindow.showInactive();
    mainWindow.focus();
  });
  if (!ok) {
    console.warn(`[JARVIS] Could not register global hotkey ${TOGGLE_ACCELERATOR} — likely already taken by another app.`);
  }
}

app.whenReady().then(() => {
  createWindow();
  registerGlobalHotkey();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// globalShortcut keeps a process-wide registration; releasing on quit
// is required to free the accelerator for other apps / a fresh launch.
app.on("will-quit", () => {
  globalShortcut.unregisterAll();
});

app.on("window-all-closed", () => {
  // On macOS apps typically stay alive until Cmd+Q. We mirror that.
  if (process.platform !== "darwin") app.quit();
});
