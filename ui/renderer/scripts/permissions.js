// ============================================================
// JARVIS overlay — permission status polling
//
// Polls GET /permissions on a fixed interval and broadcasts the
// latest snapshot to subscribers. Powers:
//   • the tier-indicator dots in the status bar
//   • the kill-switch badge
//   • the pending-action card stack
//
// We poll (not stream) because the server's /ws is text-chat only —
// it has no event channel for permission/kill-switch changes. 5 s is
// the same rhythm the iPhone web client uses; it's cheap (~200 B JSON
// per request, all localhost) and keeps the UI responsive enough that
// a confirmation card appears within ~5 s of being created.
//
// Authorize via Bearer header — query-token auth (the WS form) is a
// browser concession; for plain HTTP we use the canonical Authorization
// header which the server already accepts via require_token.
// ============================================================

const POLL_INTERVAL_MS = 5000;

let timer    = null;
let baseUrl  = null;
let token    = null;
let snapshot = null;

const listeners = new Set();

function emit() {
  for (const cb of listeners) {
    try { cb(snapshot); } catch (e) { console.error("[perms] listener threw:", e); }
  }
}

async function pollOnce() {
  if (!baseUrl || !token) return;
  try {
    const res = await fetch(`${baseUrl}/permissions`, {
      headers: { "Authorization": `Bearer ${token}` },
    });
    if (!res.ok) {
      console.warn(`[perms] /permissions HTTP ${res.status}`);
      return;
    }
    snapshot = await res.json();
    emit();
  } catch (err) {
    // Network errors during reconnects are expected — the server may
    // be restarting. Don't spam the console; just skip this tick.
    if (err?.name !== "AbortError" && err?.name !== "TypeError") {
      console.warn("[perms] poll failed:", err);
    }
  }
}

/** Start polling. Idempotent — calling start() twice is safe. */
export async function start() {
  if (timer) return;
  if (!window.jarvis?.getConfig) {
    console.warn("[perms] preload missing — polling disabled");
    return;
  }
  const cfg = await window.jarvis.getConfig();
  if (!cfg?.token) {
    console.warn("[perms] no auth token — polling disabled");
    return;
  }
  baseUrl = `http://${cfg.host}:${cfg.port}`;
  token   = cfg.token;

  // Fire one immediately so the UI doesn't sit empty for 5 s on boot.
  pollOnce();
  timer = setInterval(pollOnce, POLL_INTERVAL_MS);
}

export function stop() {
  if (timer) { clearInterval(timer); timer = null; }
}

/** Latest snapshot. Null until the first poll resolves. */
export function getSnapshot() { return snapshot; }

/** Subscribe to permission updates. Returns an unsubscribe fn.
 *  Fires immediately with the current snapshot if one exists. */
export function onUpdate(cb) {
  listeners.add(cb);
  if (snapshot) {
    try { cb(snapshot); } catch (e) { console.error("[perms] seed listener threw:", e); }
  }
  return () => listeners.delete(cb);
}

/** Hit POST /confirm or /tier4-confirm based on the action's tier.
 *  Returns the server envelope on success, throws on HTTP error.
 *  Triggers an immediate re-poll so the card stack updates fast. */
export async function confirmAction(id, { approve, password = null, tier4 = false } = {}) {
  if (!baseUrl || !token) throw new Error("permissions not initialised");
  const path = tier4 ? "/tier4-confirm" : "/confirm";
  const body = tier4
    ? { id, approve, password }
    : { id, approve };
  const res = await fetch(`${baseUrl}${path}`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status} ${detail}`);
  }
  // Re-poll so the card disappears immediately on approve/cancel.
  pollOnce();
  return res.json();
}

/** Trigger the kill switch via POST /emergency-stop. */
export async function emergencyStop() {
  if (!baseUrl || !token) throw new Error("permissions not initialised");
  const res = await fetch(`${baseUrl}/emergency-stop`, {
    method: "POST",
    headers: { "Authorization": `Bearer ${token}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  pollOnce();
  return res.json();
}

/** Cut JARVIS off mid-reply via POST /interrupt. Doesn't arm the
 *  kill switch — Tier 2+ actions stay enabled. Mapped to the
 *  Cmd+Shift+J global hotkey in the Electron HUD. */
export async function interrupt() {
  if (!baseUrl || !token) throw new Error("permissions not initialised");
  const res = await fetch(`${baseUrl}/interrupt`, {
    method: "POST",
    headers: { "Authorization": `Bearer ${token}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  // No re-poll needed; /interrupt doesn't change /permissions state.
  return res.json();
}

/** Clear the kill switch via POST /resume. */
export async function resume() {
  if (!baseUrl || !token) throw new Error("permissions not initialised");
  const res = await fetch(`${baseUrl}/resume`, {
    method: "POST",
    headers: { "Authorization": `Bearer ${token}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  pollOnce();
  return res.json();
}
