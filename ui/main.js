"use strict";

const { app, BrowserWindow, ipcMain, screen } = require("electron");
const path = require("node:path");

// Suppress the Chromium ANGLE/EGL error spam on Intel macOS:
//   "EGL Driver message (Error) eglQueryDeviceAttribEXT: Bad attribute"
// fires every frame because the Intel iGPU doesn't expose the
// attributes Chromium's GPU process queries. Software rendering is
// perfectly fast enough for a small overlay with one Canvas — the
// trade-off is invisible at 480x340 and removes the log flood that
// makes the terminal unusable. Must be called BEFORE app.whenReady().
app.disableHardwareAcceleration();

// ---- window sizing per state ---- //
// IDLE shows the small orb in the bottom-right corner; ACTIVE/SPEAKING/
// PROCESSING expand to the full HUD. Resizing is done programmatically
// — the user can't drag a resize handle (no frame).
// Orb window is sized to fit the 110px circle + room for its drop-
// shadow glow + the rotating outer accent ring. Anything past the
// orb's edge is genuinely transparent — no rectangle to mask.
const ORB = { width: 170, height: 170 };
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
    frame: false,
    resizable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    hasShadow: false,
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
