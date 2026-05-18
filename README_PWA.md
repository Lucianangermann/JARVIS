# JARVIS — iPhone PWA

A holographic-HUD progressive web app that pairs with the JARVIS
server and turns an iPhone into a full remote: push-to-talk, live
streamed sentences, kill-switch, type-input fallback. Installs to
the home screen via Safari → "Add to Home Screen", runs full-screen
with no browser chrome, and works either on the home Wi-Fi or
anywhere via Tailscale.

## What you get

| | |
|---|---|
| **Installable** | Standalone-mode PWA; no Safari UI once on the home screen |
| **Push-to-talk** | Hold the hex button → records → server transcribes → streams the reply sentence by sentence |
| **Streaming** | Live `jarvis_partial` events render the bubble in real time, matching the desktop HUD |
| **Auto-route** | Tries the LAN URL first; falls back to Tailscale if the LAN isn't reachable |
| **Settings panel** | In-app — paste the local URL, Tailscale URL, and bearer token; "Test" pings both routes |
| **Type input** | When voice isn't appropriate, the search hex button opens a text panel |
| **Kill switch** | The STOP badge fires `POST /interrupt` — same semantic as desktop Cmd+Shift+J |

## 1. Enable HTTPS on the server (one-time)

iOS Safari refuses microphone access **and** Add-to-Home-Screen on
plain HTTP. If your machine is on Tailscale you already have valid
HTTPS certificates issued by Tailscale's CA — they're sitting in
the project root:

```
macbook-pro-von-lucian-1.tail1a2633.ts.net.crt
macbook-pro-von-lucian-1.tail1a2633.ts.net.key
```

(filenames will reflect *your* machine's Tailscale hostname.)

Open `.env` and point JARVIS at them:

```dotenv
JARVIS_SSL_CERT=./macbook-pro-von-lucian-1.tail1a2633.ts.net.crt
JARVIS_SSL_KEY=./macbook-pro-von-lucian-1.tail1a2633.ts.net.key
```

Restart the server. The startup log now reports HTTPS:

```
[JARVIS] listening on https://127.0.0.1:8000
[JARVIS] PWA at     https://127.0.0.1:8000/app
```

If both env vars are empty the server runs plain HTTP and prints a
clear warning that the iPhone PWA install + mic won't work.

### Don't have a Tailscale cert?

Run `tailscale cert <your-machine>.tailnet.ts.net` once on the
MacBook — Tailscale provisions a Let's-Encrypt-issued PEM pair into
the current directory. Point the env vars at those files and
restart. The certs auto-renew on `tailscale up`.

## 2. Find your URLs

On the MacBook:

```bash
# LAN URL — your home-network IP
ipconfig getifaddr en0
# → https://192.168.1.42:8000/app

# Tailscale URL — works from anywhere
tailscale ip -4
# → https://100.x.y.z:8000/app
```

You'll also see them in the server's startup log.

## 3. Install on iPhone — home Wi-Fi first

1. Connect the iPhone to the same Wi-Fi as the MacBook.
2. Open **Safari** (must be Safari for the PWA install hooks to
   take effect).
3. Go to `https://<LAN-IP>:8000/app`.
   - Safari will warn about the certificate the first time because
     it's signed by Tailscale's CA, not a public one. Tap
     **"Show Details" → "visit this website"** to trust it.
4. Tap the **Share** button → **"Add to Home Screen"**.
5. Confirm → the JARVIS icon appears on the home screen.
6. Open the icon. The app launches full-screen.
7. The settings panel opens automatically on first run. Paste:
   - **Local server URL** — `https://192.168.1.42:8000`
   - **Tailscale URL** — `https://100.x.y.z:8000`
   - **Bearer token** — copy from `JARVIS_AUTH_TOKEN` in the
     MacBook's `.env`.
8. Tap **Save**. The dot in the status bar goes green and the
   "LOCAL" route badge appears.

## 4. Outside the home — via Tailscale

1. Install **Tailscale** from the App Store. Sign in with the same
   tailnet account as the MacBook.
2. Open the JARVIS app from the home screen.
3. As soon as Tailscale connects, the in-app config falls back to
   the Tailscale route automatically (the LAN URL won't resolve
   when you're off the home network).
4. Mic + streaming reply work identically over Tailscale — the
   round-trip is a few ms slower than LAN, no big deal.

## 5. Using it

### Talking
- **Hold** the big hex button → live recording. Pulses red. The
  central visualiser animates off the mic level.
- **Release** → audio uploads to `POST /transcribe`, server
  transcribes via macOS Speech.framework, the transcript hits the
  brain via the WebSocket, sentences stream back into the chat
  bubble and (on the MacBook) into the speakers.
- **Drag finger off the button before releasing** → recording is
  cancelled, nothing is sent.

### Typing
- Tap the magnifier hex button → type panel slides up. Press
  enter or **Send** to dispatch the same way voice does.

### Interrupt
- Tap the green **STOP** badge to cut JARVIS off mid-reply (same
  effect as the desktop Cmd+Shift+J shortcut — calls
  `POST /interrupt`, doesn't arm the kill switch).

### Settings
- Tap the gear hex button anytime to update URLs / token, ping
  both routes, switch the preferred route order.

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Microphone permission denied" | iOS Safari → JARVIS site is blocked from mic | iPhone → Settings → Safari → Camera/Microphone → JARVIS site → Allow |
| "Microphone requires HTTPS" | Server is on plain HTTP | Set `JARVIS_SSL_CERT` / `JARVIS_SSL_KEY` in `.env`, restart |
| Can't install to home screen | Browser isn't Safari, or page is HTTP | Open in Safari, ensure HTTPS |
| Self-signed cert warning | Tailscale cert isn't trusted on this device | On first visit: Show Details → "visit this website" — Safari caches the trust decision |
| Dot stays red | Wrong URL or token | Open settings, tap "Test" to ping both routes — `OK / X ms` confirms reachability |
| Streaming bubble doesn't grow | WebSocket not connected, but `/transcribe` worked | Look at the dot: amber = reconnecting (5 s backoff), red = error. Re-save settings to force reconnect. |
| Audio uploads but no transcript comes back | `JARVIS_LOCAL_VOICE=0` on the server, macOS Speech not authorised | Set `JARVIS_LOCAL_VOICE=1` once + accept the Speech-Recognition permission dialog; or `tccutil reset SpeechRecognition` to re-prompt |
| Sentences feel laggy over Tailscale | Tailscale relay (DERP) instead of direct connection | `tailscale netcheck` on the MacBook to confirm a direct path; otherwise it's a tailnet routing issue, not JARVIS |

## 7. Updating the PWA

The service worker caches the app shell (HTML, CSS, JS, icons) so
the PWA boots offline. When you ship new client files, the
`CACHE_VERSION` constant in `clients/pwa/sw.js` should be bumped
(eg. `jarvis-pwa-v2`). On next launch the old cache is dropped and
the new shell is fetched. API responses are never cached.

## 8. Files

```
clients/pwa/
├── index.html          shell + meta tags
├── manifest.json       icons, name, theme color
├── sw.js               service worker (offline + cache-first shell)
├── gen_icons.py        one-shot Pillow script for the three PNGs
├── assets/             icon-192, icon-512, apple-touch-icon
├── styles/             main + animations + hud + mobile
└── scripts/            config / websocket / visualizer / ptt / app
```

Server side: `server/main.py` exposes `POST /transcribe` (audio →
text, no Claude call) and mounts `clients/pwa/` under `/app`.
HTTPS via `JARVIS_SSL_CERT` + `JARVIS_SSL_KEY` in `.env`.
