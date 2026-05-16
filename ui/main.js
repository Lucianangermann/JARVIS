"use strict";

const { app, BrowserWindow, ipcMain, screen } = require("electron");
const path = require("node:path");

// ---- window sizing per state ---- //
// IDLE shows the small orb in the bottom-right corner; ACTIVE/SPEAKING/
// PROCESSING expand to the full HUD. Resizing is done programmatically
// — the user can't drag a resize handle (no frame).
const ORB = { width: 140, height: 140 };
const HUD = { width: 480, height: 340 };
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
    // macOS-only: use the system ultra-dark vibrancy so the glass effect
    // is real, not a faked rgba background. The renderer keeps its own
    // semi-transparent dark layer on top of this for the HUD contrast.
    vibrancy: process.platform === "darwin" ? "ultra-dark" : undefined,
    visualEffectState: "active",
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
