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

  // iOS gives every standalone PWA install its own isolated
  // localStorage — separate from Safari and from previous installs.
  // Each time the user removes + re-adds the home-screen bookmark
  // they'd otherwise have to retype the URL + token. Self-configure
  // from what we know:
  //   • The PWA was served by SOMEONE — that someone is the server.
  //     Default the Tailscale URL to window.location.origin if empty.
  //   • If the install URL carried a ?token=... query param, store
  //     it then strip it from the visible URL (defence-in-depth so
  //     the secret isn't sitting in browser history after first run).
  //     The home-screen bookmark itself still holds the seeded URL,
  //     which is what makes subsequent re-installs zero-touch.
  const auto = {};
  if (!cfg.tailscale && window.location?.origin?.startsWith("http")) {
    auto.tailscale = window.location.origin;
  }
  try {
    const params = new URLSearchParams(window.location.search || "");
    if (!cfg.token) {
      const t = params.get("token");
      if (t) auto.token = t;
    }
    if (!cfg.local) {
      const l = params.get("local");
      if (l) auto.local = l;
    }
    if (Object.keys(auto).length) {
      cfg = { ...cfg, ...auto };
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg)); } catch {}
    }
    // Drop the token from the address bar (history.replaceState is a
    // no-op on the actual home-screen bookmark, but tidies up the
    // visible URL while running).
    if (params.has("token")) {
      params.delete("token");
      const q = params.toString();
      const newUrl = window.location.pathname +
                     (q ? "?" + q : "") +
                     (window.location.hash || "");
      try { window.history.replaceState({}, "", newUrl); } catch {}
    }
  } catch (e) {
    console.warn("[cfg] auto-derive failed:", e);
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
