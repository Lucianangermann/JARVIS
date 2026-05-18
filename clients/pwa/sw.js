// JARVIS PWA service worker.
//
// Goals:
// 1. Cache the app shell (HTML, CSS, JS, icons) so the PWA boots
//    instantly and survives transient network failures.
// 2. NEVER cache the API surface (/ws, /transcribe, /memory/*,
//    /chat, /permissions, /confirm) — those are live, per-turn,
//    auth-gated. Caching would either leak stale data or break auth.
// 3. Show a minimal inline offline page when the shell isn't
//    cached AND the network is unreachable (first-visit-while-offline).
//
// iOS Safari runs service workers only over HTTPS or localhost — the
// outer setup doc explains how to enable HTTPS via the Tailscale
// cert. On plain HTTP this script simply doesn't get installed.

const CACHE_VERSION = "jarvis-pwa-v11";
const APP_SHELL = [
  "./",
  "./index.html",
  "./manifest.json",
  "./styles/main.css",
  "./styles/animations.css",
  "./styles/hud.css",
  "./styles/mobile.css",
  "./scripts/config.js",
  "./scripts/websocket.js",
  "./scripts/visualizer.js",
  "./scripts/ptt.js",
  "./scripts/app.js",
  "./assets/icon-192.png",
  "./assets/icon-512.png",
  "./assets/apple-touch-icon.png",
];

// Anything under these path prefixes is live API surface — always
// hit the network, never cache.
const NETWORK_ONLY_PREFIXES = [
  "/ws",            // WebSocket upgrade
  "/transcribe",    // raw audio → text (POST, multipart)
  "/tts/",          // text → audio (per-reply, never cache)
  "/chat",          // legacy HTTP chat
  "/memory/",       // memory API
  "/permissions",   // mac_control status
  "/confirm",       // tier confirmation
  "/tier4-confirm",
  "/emergency-stop",
  "/resume",
  "/interrupt",
];


self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  // Drop older cache versions whenever we bump CACHE_VERSION.
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE_VERSION)
          .map((k) => caches.delete(k)),
    )).then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") {
    // POSTs (eg. /transcribe) always go straight through.
    return;
  }
  const url = new URL(req.url);
  // Skip non-same-origin requests entirely so this SW doesn't
  // try to handle Google Fonts / CDN traffic.
  if (url.origin !== self.location.origin) return;

  if (NETWORK_ONLY_PREFIXES.some((p) => url.pathname.startsWith(p))) {
    // No interception — let the browser do its thing.
    return;
  }

  // Cache-first for the app shell, with a background-refresh so
  // updates land without a hard reload. Falls back to a minimal
  // inline offline page if both the cache AND the network fail.
  event.respondWith(
    caches.match(req).then((cached) => {
      const networkFetch = fetch(req)
        .then((resp) => {
          if (resp && resp.status === 200) {
            const copy = resp.clone();
            caches.open(CACHE_VERSION).then((c) => c.put(req, copy));
          }
          return resp;
        })
        .catch(() => null);
      return cached || networkFetch.then((resp) => resp || offlineFallback());
    }),
  );
});


function offlineFallback() {
  const body = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>J.A.R.V.I.S. offline</title>
<style>
  html, body {
    margin: 0; padding: 0; height: 100%;
    background: #00080F;
    color: #00D4FF;
    font-family: "Orbitron", system-ui, sans-serif;
    letter-spacing: 0.25em;
    display: flex; align-items: center; justify-content: center;
    text-align: center;
  }
  h1 { font-size: 24px; margin-bottom: 12px; }
  p  { font-size: 12px; opacity: 0.7; max-width: 280px; line-height: 1.6; }
</style>
</head>
<body>
  <div>
    <h1>J.A.R.V.I.S. OFFLINE</h1>
    <p>Connect to network to resume.<br>
       The HUD will reattach as soon as the server is reachable.</p>
  </div>
</body>
</html>`;
  return new Response(body, {
    status: 503,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}
