// ============================================================
// JARVIS overlay — WebSocket connection
//
// Owns the lifetime of a single WS to the Python server. The wire
// format is dictated by server/main.py:
//   client → server   {"text": "..."}
//   server → client   {"reply": "..."}  |  {"error": "..."}
//
// API:
//   connect()                  — kick off the connection (idempotent)
//   send(text)  → Promise      — resolves with the reply string or
//                                rejects with an Error
//   onConnectionChange(cb)     — subscribe to state changes
//   STATE                      — string constants for the above
//
// Reconnect: exponential backoff 1s → 2s → 4s → 8s (cap).
// Single in-flight request: the server processes one turn at a time,
// so the UI guards against parallel sends by rejecting overlapping
// calls — the renderer pins the HUD to "processing" while waiting,
// which already prevents the user from typing another message.
// ============================================================

export const STATE = Object.freeze({
  IDLE:        "idle",         // never connected yet
  CONNECTING:  "connecting",
  ONLINE:      "online",
  OFFLINE:     "offline",      // had been connected, dropped
  ERROR:       "error",        // misconfig or auth failure
});

const BACKOFF_START_MS = 1000;
const BACKOFF_MAX_MS   = 8000;

let socket = null;
let state  = STATE.IDLE;
let pending = null;             // { resolve, reject } for in-flight send
let reconnectTimer = null;
let backoffMs = BACKOFF_START_MS;

const listeners = new Set();          // connection-state listeners
const eventListeners = new Map();     // type → Set<callback> for server-pushed events

function setState(next) {
  if (state === next) return;
  state = next;
  for (const cb of listeners) {
    try { cb(next); } catch (e) { console.error("[ws] listener threw:", e); }
  }
}

export function onConnectionChange(cb) {
  listeners.add(cb);
  cb(state);     // sync seed so callers don't miss the current value
  return () => listeners.delete(cb);
}

/** Subscribe to server-pushed typed events. Messages whose JSON has a
 *  "type" string field are dispatched here instead of resolving the
 *  in-flight send() promise — they're independent from the
 *  request/response chat flow.
 *
 *    onEvent("voice_state",   ({state}) => …)
 *    onEvent("user_message",  ({text})  => …)
 *    onEvent("jarvis_reply",  ({text})  => …)
 *
 *  Returns an unsubscribe fn. */
export function onEvent(type, cb) {
  let set = eventListeners.get(type);
  if (!set) { set = new Set(); eventListeners.set(type, set); }
  set.add(cb);
  return () => set.delete(cb);
}

function dispatchEvent(data) {
  const set = eventListeners.get(data.type);
  if (!set || set.size === 0) {
    // Not fatal — server may push event kinds the renderer doesn't
    // care about yet. Log once at warn so we can spot mismatches.
    console.debug("[ws] no listener for event:", data);
    return;
  }
  for (const cb of set) {
    try { cb(data); } catch (e) { console.error("[ws] event listener threw:", e); }
  }
}

async function buildUrl() {
  if (!window.jarvis?.getConfig) {
    // Running outside Electron (e.g. browsing index.html directly).
    throw new Error("preload missing — not running inside Electron");
  }
  const cfg = await window.jarvis.getConfig();
  if (!cfg?.token) {
    throw new Error("auth token missing — set JARVIS_AUTH_TOKEN in .env");
  }
  const scheme = cfg.ssl ? "wss" : "ws";
  return `${scheme}://${cfg.host}:${cfg.port}/ws?token=${encodeURIComponent(cfg.token)}`;
}

export async function connect() {
  // Idempotent: if a socket is already open or opening, do nothing.
  if (socket && (socket.readyState === WebSocket.OPEN ||
                 socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  setState(STATE.CONNECTING);

  let url;
  try {
    url = await buildUrl();
  } catch (err) {
    console.warn("[ws] cannot build URL:", err.message);
    setState(STATE.ERROR);
    return;   // no reconnect — config won't fix itself by retrying
  }

  let sock;
  try {
    sock = new WebSocket(url);
  } catch (err) {
    console.warn("[ws] WebSocket ctor threw:", err);
    setState(STATE.ERROR);
    scheduleReconnect();
    return;
  }
  socket = sock;

  sock.addEventListener("open", () => {
    backoffMs = BACKOFF_START_MS;
    setState(STATE.ONLINE);
  });

  sock.addEventListener("message", (ev) => {
    let data;
    try {
      data = JSON.parse(ev.data);
    } catch {
      console.warn("[ws] non-JSON message ignored:", ev.data);
      return;
    }
    // Server-pushed typed events (voice_state, user_message, jarvis_reply)
    // are independent of the request/response chat flow.
    if (typeof data.type === "string") {
      dispatchEvent(data);
      return;
    }
    // Otherwise it's a reply/error for the in-flight send().
    if (!pending) {
      console.warn("[ws] unsolicited reply/error:", data);
      return;
    }
    const p = pending;
    pending = null;
    if (typeof data.error === "string")      p.reject(new Error(data.error));
    else if (typeof data.reply === "string") p.resolve(data);  // full object, not just .reply
    else p.reject(new Error("unexpected message shape"));
  });

  sock.addEventListener("close", () => {
    if (pending) {
      pending.reject(new Error("connection closed mid-request"));
      pending = null;
    }
    // Don't downgrade ERROR → OFFLINE; an explicit ERROR is more
    // useful for the UI (red dot vs. amber).
    if (state !== STATE.ERROR) setState(STATE.OFFLINE);
    socket = null;
    scheduleReconnect();
  });

  sock.addEventListener("error", () => {
    // The browser doesn't expose the actual reason ("security error
    // ...") to JS — we just know something went wrong. The matching
    // close event will fire right after; reconnect is scheduled there.
    console.warn("[ws] socket error event");
  });
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, backoffMs);
  backoffMs = Math.min(backoffMs * 2, BACKOFF_MAX_MS);
}

export async function send(text) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    throw new Error("server offline");
  }
  if (pending) {
    throw new Error("another message is still pending");
  }
  return new Promise((resolve, reject) => {
    pending = { resolve, reject };
    try {
      socket.send(JSON.stringify({ text }));
    } catch (err) {
      pending = null;
      reject(err);
    }
  });
}
