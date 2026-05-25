"use strict";

const { app, BrowserWindow, globalShortcut, ipcMain, screen } = require("electron");
const path = require("node:path");
const fs   = require("node:fs");
const { spawn } = require("node:child_process");

// Project root = parent of ui/. Used to locate .env and the python
// server module, both of which live one directory up from this file.
const PROJECT_ROOT = path.resolve(__dirname, "..");

// ---- .env parsing ---- //
// The Python server already owns its .env via python-dotenv; we read
// the same file here so the Electron client can talk to it with the
// same auth token and host/port without the user duplicating config.
// Bespoke parser instead of pulling in `dotenv` as an npm dep — the
// format is trivial (KEY=VALUE per line, # comments) and we only
// need three keys.
function loadDotEnv(envPath) {
  if (!fs.existsSync(envPath)) {
    console.warn(`[JARVIS] .env not found at ${envPath} — chat will refuse to connect.`);
    return {};
  }
  const out = {};
  for (const raw of fs.readFileSync(envPath, "utf8").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    let val = line.slice(eq + 1).trim();
    // Strip matched surrounding quotes — common .env style.
    if ((val.startsWith('"') && val.endsWith('"')) ||
        (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1);
    }
    out[key] = val;
  }
  return out;
}

const dotenv = loadDotEnv(path.join(PROJECT_ROOT, ".env"));
// The server boots in HTTPS mode iff both cert + key are set in .env
// (see server/main.py::run). Mirror that decision here so the renderer
// can pick wss://+https:// when needed. Without this the HUD silently
// fails to connect whenever the iPhone PWA path is enabled.
const SSL_ENABLED = Boolean(dotenv.JARVIS_SSL_CERT && dotenv.JARVIS_SSL_KEY);
const SERVER_CONFIG = {
  token: dotenv.JARVIS_AUTH_TOKEN || "",
  // Server binds to 127.0.0.1 by default; we always connect to
  // localhost regardless of HOST=0.0.0.0 (which is for LAN exposure,
  // not for the local client).
  host: "127.0.0.1",
  port: parseInt(dotenv.PORT || "8000", 10),
  ssl: SSL_ENABLED,
};
if (!SERVER_CONFIG.token) {
  console.warn("[JARVIS] JARVIS_AUTH_TOKEN missing from .env — the renderer will report OFFLINE.");
}

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

// When the user enables HTTPS via Tailscale certs (for the iPhone PWA),
// the Electron HUD still connects through 127.0.0.1 — but the cert's
// CN is the Tailscale hostname, not the loopback IP. Chromium rejects
// that as CERT_COMMON_NAME_INVALID and the WS/permissions fetch silently
// die. Accept the cert only for our own server (loopback + the configured
// port). Scope is intentionally narrow so we don't blanket-trust every
// bad cert the browser meets.
if (SSL_ENABLED) {
  app.on("certificate-error", (event, _webContents, url, _error, _cert, callback) => {
    try {
      const u = new URL(url);
      const ok = (u.hostname === "127.0.0.1" || u.hostname === "localhost")
              && String(u.port || "") === String(SERVER_CONFIG.port);
      if (ok) {
        event.preventDefault();
        callback(true);
        return;
      }
    } catch { /* fall through */ }
    callback(false);
  });
}

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

// When the user manually drags the window we remember the centre of
// that position and preserve it across idle↔HUD size changes instead
// of snapping back to the corner.
let _userCenter = null; // { x, y } in screen coords

// State-aware positioning. The HUD wants its panel-rectangle inset
// from the screen corner by EDGE_MARGIN — straightforward. The ORB
// state is different: the window is intentionally much bigger than
// the orb circle (so the breathing glow doesn't get clipped to a
// rectangle), so anchoring the WINDOW corner would push the orb far
// from the screen corner. Instead we anchor the orb's CIRCLE to the
// corner and let the window extend past the screen edge — macOS just
// clips the off-screen padding, which is empty glow space anyway.
function placeForState(win, state) {
  const target = state === "idle" ? ORB : HUD;

  // If the user has dragged the window, preserve their chosen centre
  // and only resize around it — don't snap back to the corner.
  if (_userCenter) {
    win.setBounds({
      width:  target.width,
      height: target.height,
      x: Math.round(_userCenter.x - target.width  / 2),
      y: Math.round(_userCenter.y - target.height / 2),
    });
    return;
  }

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

  // Track manual moves. We save the window centre so placeForState
  // can resize without snapping back to the screen corner.
  mainWindow.on("moved", () => {
    if (!mainWindow) return;
    const [x, y] = mainWindow.getPosition();
    const [w, h] = mainWindow.getSize();
    _userCenter = { x: x + w / 2, y: y + h / 2 };
  });

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

// Renderer asks for the WS URL + auth token on boot. Shipped over IPC
// rather than baked into the renderer at build time so the user can
// rotate the token in .env without rebuilding anything.
ipcMain.handle("jarvis:get-config", () => ({
  token: SERVER_CONFIG.token,
  host:  SERVER_CONFIG.host,
  port:  SERVER_CONFIG.port,
  ssl:   SERVER_CONFIG.ssl,
}));

// ---- server child process ---- //
// Electron spawns `python -m server.main` from PROJECT_ROOT and owns
// its lifecycle. The user opted into this in setup — they get a
// single "double-click the app" UX without manually starting uvicorn.
// Skippable via JARVIS_NO_SPAWN=1 if you want to run the server in a
// separate terminal during dev (e.g. for cleaner backend logs).
let serverProcess = null;

function pythonBinary() {
  // Prefer the project's .venv — that's where the deps live. Falls
  // back to whatever `python3` resolves to on PATH (works if the user
  // installed deps system-wide, will fail noisily otherwise).
  const venvPy = path.join(PROJECT_ROOT, ".venv", "bin", "python");
  return fs.existsSync(venvPy) ? venvPy : "python3";
}

function spawnServer() {
  if (process.env.JARVIS_NO_SPAWN === "1") {
    console.log("[JARVIS] JARVIS_NO_SPAWN=1 — assuming the server is already running.");
    return;
  }
  const py = pythonBinary();
  console.log(`[JARVIS] spawning server: ${py} -m server.main  (cwd=${PROJECT_ROOT})`);
  serverProcess = spawn(py, ["-m", "server.main"], {
    cwd: PROJECT_ROOT,
    env: process.env,
    // Pipe so we can prefix each line with [server] in our terminal.
    stdio: ["ignore", "pipe", "pipe"],
  });

  const prefix = (stream, tag) => {
    let buf = "";
    stream.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (line) console.log(`[${tag}] ${line}`);
      }
    });
  };
  prefix(serverProcess.stdout, "server");
  prefix(serverProcess.stderr, "server!");

  serverProcess.on("exit", (code, signal) => {
    console.log(`[JARVIS] server exited code=${code} signal=${signal}`);
    serverProcess = null;
  });
  serverProcess.on("error", (err) => {
    console.error(`[JARVIS] server spawn error: ${err.message}`);
    serverProcess = null;
  });
}

function killServer() {
  if (!serverProcess) return;
  console.log("[JARVIS] stopping server child process");
  // SIGTERM first — uvicorn's lifespan handler runs a clean shutdown
  // (joins the voice thread, closes the TTS engine). SIGKILL as a
  // last resort if it ignores us for 4 s.
  serverProcess.kill("SIGTERM");
  const proc = serverProcess;
  setTimeout(() => {
    if (proc && !proc.killed) {
      console.warn("[JARVIS] server didn't exit on SIGTERM — sending SIGKILL");
      proc.kill("SIGKILL");
    }
  }, 4000);
}

// ---- global hotkeys ---- //
// Main owns the OS-level shortcut registrations; the renderer owns
// state and the network roundtrip to the server. Both hotkeys are
// system-wide so the user can summon / interrupt JARVIS from any app.
const TOGGLE_ACCELERATOR    = "CommandOrControl+J";       // open/close HUD
const INTERRUPT_ACCELERATOR = "CommandOrControl+Shift+J"; // cut JARVIS off

function registerGlobalHotkeys() {
  const okToggle = globalShortcut.register(TOGGLE_ACCELERATOR, () => {
    if (!mainWindow) return;
    mainWindow.webContents.send("jarvis:toggle");
    // Surface the window when summoning so the user can interact
    // immediately. focus() is a no-op when we're already focused, so
    // pressing the hotkey to dismiss doesn't have a side-effect.
    mainWindow.showInactive();
    mainWindow.focus();
  });
  if (!okToggle) {
    console.warn(`[JARVIS] Could not register ${TOGGLE_ACCELERATOR} — already taken.`);
  }

  const okInterrupt = globalShortcut.register(INTERRUPT_ACCELERATOR, () => {
    if (!mainWindow) return;
    // Renderer fires POST /interrupt — it already holds the auth
    // token via getConfig() and has the http client wired up in
    // permissions.js. No focus / window flip here: the user usually
    // hits Cmd+Shift+J while JARVIS is mid-speech and they want it
    // silenced, not bumped to the front.
    mainWindow.webContents.send("jarvis:interrupt");
  });
  if (!okInterrupt) {
    console.warn(`[JARVIS] Could not register ${INTERRUPT_ACCELERATOR} — already taken.`);
  }
}

app.whenReady().then(() => {
  spawnServer();
  createWindow();
  registerGlobalHotkeys();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// globalShortcut keeps a process-wide registration; releasing on quit
// is required to free the accelerator for other apps / a fresh launch.
// killServer() sends SIGTERM first so uvicorn's lifespan runs a clean
// shutdown — joining the voice thread and closing TTS — before the
// process actually disappears.
app.on("will-quit", () => {
  globalShortcut.unregisterAll();
  killServer();
});

// macOS does NOT exit Electron on SIGTERM by default — no `will-quit`,
// no server cleanup, no process exit. Our hotkey quit script
// (quit.command) relies on SIGTERM to wind everything down cleanly,
// so we have to bridge the signal into app.quit() ourselves. SIGINT
// (^C in a terminal) gets the same handler for parity. Without this
// the entire stop-via-hotkey flow is a no-op and stale Electron
// instances accumulate forever in `ps`.
for (const sig of ["SIGTERM", "SIGINT", "SIGHUP"]) {
  process.on(sig, () => {
    console.log(`[JARVIS] received ${sig} — quitting`);
    app.quit();
  });
}

app.on("window-all-closed", () => {
  // On macOS apps typically stay alive until Cmd+Q. We mirror that.
  if (process.platform !== "darwin") app.quit();
});
