"use strict";

const { app, BrowserWindow, ipcMain, screen } = require("electron");
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

let mainWindow = null;

function placeBottomRight(win, { width, height }) {
  const { workArea } = screen.getPrimaryDisplay();
  win.setBounds({
    width,
    height,
    x: workArea.x + workArea.width - width - EDGE_MARGIN,
    y: workArea.y + workArea.height - height - EDGE_MARGIN,
  });
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

  placeBottomRight(mainWindow, ORB);

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
  const target = state === "idle" ? ORB : HUD;
  placeBottomRight(mainWindow, target);
  return { width: target.width, height: target.height };
});

ipcMain.handle("jarvis:quit", () => {
  app.quit();
});

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  // On macOS apps typically stay alive until Cmd+Q. We mirror that.
  if (process.platform !== "darwin") app.quit();
});
