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

  /** Subscribe to "toggle" pings from the global hotkey (Cmd+J).
   *  Returns an unsubscribe function. */
  onToggle: (callback) => {
    const handler = () => callback();
    ipcRenderer.on("jarvis:toggle", handler);
    return () => ipcRenderer.off("jarvis:toggle", handler);
  },

  /** Subscribe to "interrupt" pings from Cmd+Shift+J — fired when the
   *  user wants to cut JARVIS off mid-reply. Returns an unsubscribe fn. */
  onInterrupt: (callback) => {
    const handler = () => callback();
    ipcRenderer.on("jarvis:interrupt", handler);
    return () => ipcRenderer.off("jarvis:interrupt", handler);
  },

  /** Fetch server connection config (token + host + port) once on boot.
   *  Returns { token, host, port }. The token comes from the project's
   *  .env so the user rotates it in one place. */
  getConfig: () => ipcRenderer.invoke("jarvis:get-config"),
});
