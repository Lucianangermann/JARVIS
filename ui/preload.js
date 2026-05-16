"use strict";

// Preload runs in an isolated world with limited Node access. We expose
// only the few channels the renderer actually needs through
// contextBridge — the renderer never sees `require`, `process`, or any
// Node API directly. That keeps us aligned with Electron's recommended
// security baseline (contextIsolation:true + sandbox:true in main.js).
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("jarvis", {
  /** Switch UI state and resize the OS window to match. */
  setState: (state) => ipcRenderer.invoke("jarvis:set-state", state),

  /** Quit the overlay app (Cmd+Q equivalent, called from the X button). */
  quit: () => ipcRenderer.invoke("jarvis:quit"),
});
