// JARVIS PWA — WebSocket layer.
//
// Follows the actual server protocol from server/main.py:
//
//   client → server   {"text": "..."}
//   server → client   {"reply": "..."}           — full chat reply
//                     {"error": "..."}           — chat failure
//                     {"type": "voice_state",    "state": "..."}
//                     {"type": "user_message",   "text":  "..."}
//                     {"type": "jarvis_partial", "text":  "..."}
//                     {"type": "jarvis_reply",   "text":  "..."}
//
// We auth via the `?token=` query param (see config.wsUrl()) because
// browsers can't set custom WebSocket headers — that's the path
// authorize_websocket() in server/main.py expects.
//
// Reconnect: simple 1→8 s exponential backoff. The brain sees the
// reconnected client as the same session (auth_token is the session
// id) so history persists across drops.

import * as cfg from "./config.js";

export const STATE = Object.freeze({
  IDLE:        "idle",
  CONNECTING:  "connecting",
  ONLINE:      "online",
  OFFLINE:     "offline",
  ERROR:       "error",
});

const BACKOFF_START_MS = 1000;
const BACKOFF_MAX_MS   = 8000;

let socket = null;
let state  = STATE.IDLE;
let pending = null;
let reconnectTimer = null;
let backoffMs = BACKOFF_START_MS;

const stateListeners = new Set();
const eventListeners = new Map();   // type → Set<callback>


export function getState() { return state; }

function setState(next) {
  if (state === next) return;
  state = next;
  for (const cb of stateListeners) {
    try { cb(next); } catch (e) { console.error("[ws] listener:", e); }
  }
}

export function onConnectionChange(cb) {
  stateListeners.add(cb);
  cb(state);
  return () => stateListeners.delete(cb);
}

/** Subscribe to typed server events: voice_state, user_message,
 *  jarvis_partial, jarvis_reply. */
export function onEvent(type, cb) {
  let set = eventListeners.get(type);
  if (!set) { set = new Set(); eventListeners.set(type, set); }
  set.add(cb);
  return () => set.delete(cb);
}

function dispatchEvent(data) {
  const set = eventListeners.get(data.type);
  if (!set || set.size === 0) return;
  for (const cb of set) {
    try { cb(data); } catch (e) { console.error("[ws] event:", e); }
  }
}


/** Open the WebSocket (idempotent). Resolves once the connection
 *  is open OR an error puts us in ERROR state. Caller doesn't have
 *  to await — the message handlers handle late connection too. */
export async function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN ||
                 socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  setState(STATE.CONNECTING);

  // Make sure we have a route + token. pick() probes both servers
  // and chooses one; if both are dead we land in ERROR.
  const base = await cfg.pick();
  if (!base) {
    setState(STATE.ERROR);
    scheduleReconnect();
    return;
  }

  const url = cfg.wsUrl();
  if (!url) {
    setState(STATE.ERROR);
    return;     // nothing we can do until the user pastes a token
  }

  let sock;
  try {
    sock = new WebSocket(url);
  } catch (err) {
    console.warn("[ws] ctor:", err);
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
    try { data = JSON.parse(ev.data); }
    catch { console.warn("[ws] non-JSON ignored"); return; }
    if (typeof data.type === "string") {
      dispatchEvent(data);
      return;
    }
    if (!pending) {
      console.warn("[ws] unsolicited reply:", data);
      return;
    }
    const p = pending;
    pending = null;
    if (typeof data.error === "string")      p.reject(new Error(data.error));
    else if (typeof data.reply === "string") p.resolve(data.reply);
    else p.reject(new Error("unexpected message shape"));
  });
  sock.addEventListener("close", () => {
    if (pending) {
      pending.reject(new Error("connection closed mid-request"));
      pending = null;
    }
    if (state !== STATE.ERROR) setState(STATE.OFFLINE);
    socket = null;
    scheduleReconnect();
  });
  sock.addEventListener("error", () => {
    // Browser doesn't expose the cause; matching `close` will
    // schedule the reconnect.
    console.warn("[ws] socket error");
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

/** Send a chat turn. Promise resolves with the assembled reply
 *  text. Streaming partials arrive in parallel via onEvent. */
export async function send(text) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    throw new Error("server offline");
  }
  if (pending) throw new Error("another message is still pending");
  return new Promise((resolve, reject) => {
    pending = { resolve, reject };
    try { socket.send(JSON.stringify({ text })); }
    catch (err) { pending = null; reject(err); }
  });
}

/** Force a fresh route probe (used after the user edits the
 *  settings panel). Drops the current socket so the next send
 *  reopens on the new route. */
export async function reconnect() {
  if (socket) {
    try { socket.close(); } catch {}
    socket = null;
  }
  setState(STATE.CONNECTING);
  return connect();
}
