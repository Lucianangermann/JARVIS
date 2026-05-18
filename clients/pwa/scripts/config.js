// JARVIS PWA — connection manager + settings persistence.
//
// We deliberately store the two server URLs (LOCAL + Tailscale) and
// the bearer token in localStorage. This file is opened from the
// homescreen as a standalone PWA, so there's no usable address bar
// to hand-edit query params — config has to live somewhere the user
// can edit from inside the app.

const STORAGE_KEY = "jarvis.pwa.cfg.v1";

/** Default placeholders. User overrides via the settings panel. */
const DEFAULTS = {
  local:     "",
  tailscale: "",
  token:     "",
  // "auto" picks the route via probing; "local" / "tailscale" pin
  // one explicitly (useful if you know which network you're on).
  prefer: "auto",
};

let cfg = { ...DEFAULTS };
let activeBase = null;     // the URL we successfully reached last
let activeRoute = "";      // "local" | "tailscale" | ""

const listeners = new Set();


export function load() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) cfg = { ...DEFAULTS, ...JSON.parse(raw) };
  } catch (e) {
    console.warn("[cfg] load failed:", e);
  }
  return { ...cfg };
}

export function save(next) {
  cfg = { ...DEFAULTS, ...cfg, ...next };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg));
  } catch (e) {
    console.warn("[cfg] save failed:", e);
  }
  for (const cb of listeners) cb(cfg);
  return { ...cfg };
}

export function get() { return { ...cfg }; }

export function onChange(cb) {
  listeners.add(cb);
  cb(cfg);
  return () => listeners.delete(cb);
}

/** Active base URL — whatever pick() last validated. May be null
 *  before the first successful probe. */
export function activeBaseUrl() { return activeBase; }

/** Which of the two routes we're using right now ("local" |
 *  "tailscale" | ""). Drives the small label in the status bar. */
export function activeRouteName() { return activeRoute; }


/** Probe both URLs in preferred order and remember whichever
 *  responds first to GET /. Returns the chosen base URL or null on
 *  total failure. */
export async function pick({ timeoutMs = 1500 } = {}) {
  const candidates = orderedCandidates();
  for (const { name, base } of candidates) {
    if (!base) continue;
    if (await reachable(base, timeoutMs)) {
      activeBase = base;
      activeRoute = name;
      return base;
    }
  }
  activeBase = null;
  activeRoute = "";
  return null;
}

/** Quick ping check used by the settings "Test" button. */
export async function ping(base, timeoutMs = 2000) {
  if (!base) return { ok: false, reason: "no url" };
  const t0 = performance.now();
  const ok = await reachable(base, timeoutMs);
  const ms = Math.round(performance.now() - t0);
  return { ok, ms };
}


function orderedCandidates() {
  const local     = { name: "local",     base: normalise(cfg.local) };
  const tailscale = { name: "tailscale", base: normalise(cfg.tailscale) };
  if (cfg.prefer === "local")     return [local, tailscale];
  if (cfg.prefer === "tailscale") return [tailscale, local];
  return [local, tailscale];
}

/** Drop trailing slashes and tolerate the user pasting a WS URL by
 *  accident — we always store the HTTP origin and derive WS from it. */
function normalise(url) {
  if (!url) return "";
  let u = url.trim();
  u = u.replace(/^ws:/, "http:").replace(/^wss:/, "https:");
  return u.replace(/\/+$/, "");
}

/** Server health probe. `GET /` is unauthenticated + cheap (returns
 *  {"ok": true}). AbortController gives us the timeout the iOS
 *  fetch API doesn't have natively. */
async function reachable(base, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(base + "/", {
      method: "GET",
      cache: "no-store",
      signal: ctrl.signal,
    });
    return r.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}


/** Convert the active HTTP base to its WebSocket sibling, with auth
 *  baked into the query string the way server/main.py's /ws expects
 *  (`?token=...` — see authorize_websocket()). */
export function wsUrl() {
  if (!activeBase) return null;
  if (!cfg.token) return null;
  const wsScheme = activeBase.startsWith("https:") ? "wss:" : "ws:";
  const host = activeBase.replace(/^https?:/, "");
  return `${wsScheme}${host}/ws?token=${encodeURIComponent(cfg.token)}`;
}

/** HTTP base for the REST endpoints. Caller appends the path. */
export function httpBase() { return activeBase; }

/** Bearer header — convenience for fetch() callers. */
export function authHeader() {
  return cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {};
}


// Auto-load on module init so first import sees the saved values.
load();
