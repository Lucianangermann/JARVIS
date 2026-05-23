// JARVIS PWA — main wiring.
//
// One source of truth for the body's state attributes that drive
// the CSS state machine + the visualizer. Subscribes to:
//   - ws.onConnectionChange  → status-bar dot + label
//   - ws.onEvent(jarvis_*)   → streaming chat bubble updates
//   - ws.onEvent(voice_state) → idle/processing/speaking
//   - ptt.onTranscript       → send transcript via ws.send
//   - ptt.onAmplitude        → drive the central visualiser
// And renders three transient UI surfaces: the chat pane, the
// settings slide-up panel, and the type-input slide-up panel.

import * as cfg    from "./config.js";
import * as ws     from "./websocket.js";
import * as viz    from "./visualizer.js";
import * as ptt    from "./ptt.js";
import * as camera from "./camera.js";

// ── DOM handles ──────────────────────────────────────────────
const body        = document.body;
const clockEl     = document.getElementById("clock");
const connDot     = document.getElementById("conn-dot");
const connLabel   = document.getElementById("conn-label");
const connRoute   = document.getElementById("conn-route");
const chatPane    = document.getElementById("chat-pane");
const pttBtn      = document.getElementById("ptt-btn");
const pttLabel    = document.getElementById("ptt-label");
const pttTimer    = document.getElementById("ptt-timer");
const settingsPanel = document.getElementById("settings-panel");
const textPanel   = document.getElementById("text-panel");
const cfgLocal    = document.getElementById("cfg-local");
const cfgTail     = document.getElementById("cfg-tailscale");
const cfgToken    = document.getElementById("cfg-token");
const cfgStatus   = document.getElementById("cfg-status");
const cfgTest     = document.getElementById("cfg-test");
const cfgSave     = document.getElementById("cfg-save");
const cfgClose    = document.getElementById("cfg-close");
const textInput   = document.getElementById("text-input");
const textSend    = document.getElementById("text-send");
const textClose   = document.getElementById("text-close");
const visualizer  = document.getElementById("visualizer");
const killBadge   = document.getElementById("act-kill");

// ── Clock ────────────────────────────────────────────────────
function tickClock() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  clockEl.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
tickClock();
setInterval(tickClock, 1000);

// ── State machine ────────────────────────────────────────────
let currentState = "idle";
function setState(next) {
  if (currentState === next) return;
  currentState = next;
  body.dataset.state = next;
  viz.setState(next === "active" ? "idle" : next);   // idle alias
}

// ── Visualizer + amplitude bridge ────────────────────────────
viz.start(visualizer);
ptt.onAmplitude((v) => viz.setAmplitude(v));

// ── Connection indicator ─────────────────────────────────────
ws.onConnectionChange((s) => {
  switch (s) {
    case ws.STATE.ONLINE:
      body.dataset.conn = "online";
      connLabel.textContent = "ONLINE";
      connRoute.textContent = cfg.activeRouteName().toUpperCase();
      break;
    case ws.STATE.CONNECTING:
      body.dataset.conn = "connecting";
      connLabel.textContent = "CONNECTING";
      connRoute.textContent = "";
      break;
    case ws.STATE.OFFLINE:
    case ws.STATE.IDLE:
      body.dataset.conn = "offline";
      connLabel.textContent = "OFFLINE";
      connRoute.textContent = "";
      break;
    case ws.STATE.ERROR:
      body.dataset.conn = "error";
      connLabel.textContent = "ERROR";
      connRoute.textContent = "";
      break;
  }
});

// ── Chat bubble plumbing ────────────────────────────────────
let liveJarvisBubble = null;
const CHAT_MAX = 100;
const STICKY_BOTTOM_PX = 40;

function isPinnedToBottom() {
  return chatPane.scrollHeight - chatPane.scrollTop - chatPane.clientHeight
         <= STICKY_BOTTOM_PX;
}

function addMessage(who, text) {
  const stick = isPinnedToBottom();
  const msg = document.createElement("div");
  msg.className = `chat-msg ${who}`;
  const whoEl = document.createElement("span");
  whoEl.className = "who";
  whoEl.textContent = who === "you" ? "YOU ›" : "J.A.R.V.I.S. ›";
  const bodyEl = document.createElement("span");
  bodyEl.className = "body";
  bodyEl.textContent = text;
  msg.append(whoEl, bodyEl);
  chatPane.appendChild(msg);
  while (chatPane.children.length > CHAT_MAX) chatPane.firstChild.remove();
  if (stick) chatPane.scrollTop = chatPane.scrollHeight;
  return bodyEl;
}

// ── In-app debug log ─────────────────────────────────────────
// iOS standalone PWAs can't be inspected without a newer macOS,
// so we surface TTS / audio diagnostics directly in the chat pane.
// Toggle the visible flag to suppress once debugging is done.
const DEBUG_VISIBLE = false;
function logDebug(msg) {
  console.log("[debug]", msg);
  if (!DEBUG_VISIBLE) return;
  const stick = isPinnedToBottom();
  const el = document.createElement("div");
  el.className = "chat-msg debug";
  el.style.cssText = "font-size:11px;opacity:0.7;padding:4px 8px;" +
    "color:#7fd8ff;border:1px dashed rgba(127,216,255,0.3);" +
    "background:rgba(0,40,60,0.25);border-radius:6px;";
  const t = new Date();
  const ts = `${String(t.getMinutes()).padStart(2,"0")}:${String(t.getSeconds()).padStart(2,"0")}`;
  el.textContent = `· ${ts} ${msg}`;
  chatPane.appendChild(el);
  while (chatPane.children.length > CHAT_MAX) chatPane.firstChild.remove();
  if (stick) chatPane.scrollTop = chatPane.scrollHeight;
}
// Surface global JS errors that would otherwise be invisible.
window.addEventListener("error", (ev) =>
  logDebug(`JS error: ${ev.message} @ ${ev.filename}:${ev.lineno}`));
window.addEventListener("unhandledrejection", (ev) =>
  logDebug(`promise rejected: ${ev.reason?.message || ev.reason}`));

function appendPartial(text) {
  if (!text) return;
  const stick = isPinnedToBottom();
  if (liveJarvisBubble === null) {
    const msg = document.createElement("div");
    msg.className = "chat-msg jarvis";
    const whoEl = document.createElement("span");
    whoEl.className = "who";
    whoEl.textContent = "J.A.R.V.I.S. ›";
    const bodyEl = document.createElement("span");
    bodyEl.className = "body cursor";
    msg.append(whoEl, bodyEl);
    chatPane.appendChild(msg);
    while (chatPane.children.length > CHAT_MAX) chatPane.firstChild.remove();
    liveJarvisBubble = bodyEl;
  }
  const prev = liveJarvisBubble.textContent;
  const sep = (prev && !prev.endsWith(" ")) ? " " : "";
  liveJarvisBubble.textContent = prev + sep + text;
  if (stick) chatPane.scrollTop = chatPane.scrollHeight;
}

function finalizeJarvis(text) {
  if (liveJarvisBubble !== null) {
    if (text && text.trim()) liveJarvisBubble.textContent = text;
    liveJarvisBubble.classList.remove("cursor");
  }
  liveJarvisBubble = null;
}

function abandonJarvis() {
  if (liveJarvisBubble !== null) liveJarvisBubble.classList.remove("cursor");
  liveJarvisBubble = null;
}

// ── Server-pushed event handlers ─────────────────────────────
ws.onEvent("voice_state", ({ state }) => {
  switch (state) {
    case "transcribing":
    case "thinking":   setState("processing"); break;
    case "speaking":   setState("speaking");   break;
    case "listening":
      abandonJarvis();
      if (currentState !== "idle") setState("active");
      break;
  }
});

ws.onEvent("user_message", ({ text }) => {
  abandonJarvis();
  if (text) addMessage("you", text);
});

ws.onEvent("jarvis_partial", ({ text }) => {
  appendPartial(text);
  speakSentence(text);
});

ws.onEvent("jarvis_reply", ({ text }) => {
  if (liveJarvisBubble !== null) finalizeJarvis(text);
  else if (text) addMessage("jarvis", text);
  // Speak the final reply only if the streaming partials never
  // arrived (eg. /ws served a non-streaming path). Otherwise the
  // partial-stream already enqueued everything.
  if (!ttsSpokeAnythingThisTurn) speakSentence(text);
  ttsSpokeAnythingThisTurn = false;
});

// ── iPhone-side Text-to-Speech (server-synthesised) ──────────
// iOS Safari's Web Speech API is broken in standalone (Add-to-Home-
// Screen) PWAs — `speechSynthesis.speak()` silently no-ops even
// after a user-gesture primer. We tried that route and got nothing.
// Workaround: fetch synthesized audio from the server (`GET /tts/
// synthesize`) and play it through an HTMLAudioElement, which works
// reliably on iOS.
//
// Queue model: each streamed sentence is enqueued; one player plays
// them sequentially so partials don't overlap. A new turn (or STOP)
// drops the queue + aborts the current playback.
//
// Prefetch pipeline: as soon as a sentence is enqueued we kick off
// the /tts/synthesize fetch in parallel and stash the in-flight
// Promise<blobUrl>. By the time the current sentence finishes playing,
// the next sentence's audio blob is (usually) already downloaded —
// so the gap between sentences shrinks from "HTTP RTT + `say` synth
// + blob transfer" to "just the new <audio> src swap." For typical
// 2-3 sentence German replies this collapses ~1-2 s of dead air per
// sentence into something close to natural prosody.
//
// iOS still requires a user gesture to start the FIRST audio
// playback of the session — we cover that with primeTts() on every
// PTT/send touch, which loads + plays a near-silent AIFF inside the
// gesture. After that, queued plays work fire-and-forget.
const ttsAudio = new Audio();
ttsAudio.preload = "auto";
ttsAudio.playsInline = true;
let ttsQueue = [];
let ttsPlaying = false;
let ttsCurrentUrl = null;
let ttsSpokeAnythingThisTurn = false;

async function ttsSynthUrl(text) {
  const base = cfg.httpBase();
  if (!base) { logDebug("tts: no httpBase"); return null; }
  const headers = cfg.authHeader();
  const url = `${base}/tts/synthesize?text=${encodeURIComponent(text)}`;
  logDebug(`tts: GET /tts/synthesize "${text.slice(0,30)}"`);
  // Single retry on 429 — the server's per-token bucket is shared
  // with the /permissions poll, so a transient overrun is plausible
  // even at a generous limit. 1.2 s gives the 60 s sliding window
  // enough slack to drop one entry.
  for (let attempt = 0; attempt < 2; attempt++) {
    const r = await fetch(url, { method: "GET", headers, cache: "no-store" });
    logDebug(`tts: HTTP ${r.status}${attempt ? " (retry)" : ""}`);
    if (r.status === 429 && attempt === 0) {
      await new Promise((resolve) => setTimeout(resolve, 1200));
      continue;
    }
    if (!r.ok) throw new Error(`tts http ${r.status}`);
    const blob = await r.blob();
    logDebug(`tts: blob ${blob.size}B type=${blob.type || "?"}`);
    return URL.createObjectURL(blob);
  }
  throw new Error("tts: retries exhausted");
}

function ttsRevoke(url) {
  if (!url) return;
  try { URL.revokeObjectURL(url); } catch { /* ignore */ }
}

async function ttsPlayNext() {
  if (ttsPlaying) { logDebug(`tts: playNext skip — already playing, q=${ttsQueue.length}`); return; }
  const next = ttsQueue.shift();
  if (!next) { logDebug("tts: playNext queue empty"); return; }
  ttsPlaying = true;
  logDebug(`tts: playNext starting "${next.text.slice(0,28)}" q=${ttsQueue.length} remaining`);
  try {
    // urlPromise was kicked off in speakSentence() the moment the
    // sentence arrived from the WebSocket. By now it's usually
    // already resolved — await is a no-op in the common path.
    const url = await next.urlPromise;
    if (!url) { ttsPlaying = false; if (ttsQueue.length > 0) ttsPlayNext(); return; }
    ttsCurrentUrl = url;
    // iOS quirk: reusing the same <audio> element across multiple
    // src changes can leave the old media graph hanging and the
    // next .play() silently no-ops. Explicit pause() + load() forces
    // the element to re-read the new src cleanly. This is the fix
    // for "only the first sentence plays".
    try { ttsAudio.pause(); } catch { /* ignore */ }
    ttsAudio.src = url;
    ttsAudio.load();
    ttsAudio.volume = 1.0;
    logDebug(`tts: play() unlocked=${ttsUnlocked}`);
    await ttsAudio.play();
    logDebug("tts: play() resolved (playback started)");
  } catch (e) {
    const name = e?.name || "?";
    const msg  = e?.message || String(e);
    logDebug(`tts: play FAILED ${name}: ${msg}`);
    ttsPlaying = false;
    if (ttsCurrentUrl) { ttsRevoke(ttsCurrentUrl); ttsCurrentUrl = null; }
    if (ttsQueue.length > 0) ttsPlayNext();
  }
}

ttsAudio.addEventListener("play",    () => logDebug("tts evt: 'play'"));
ttsAudio.addEventListener("playing", () => logDebug("tts evt: 'playing'"));
ttsAudio.addEventListener("pause",   () => logDebug("tts evt: 'pause'"));
ttsAudio.addEventListener("stalled", () => logDebug("tts evt: 'stalled'"));
ttsAudio.addEventListener("ended", () => {
  logDebug(`tts evt: 'ended', q=${ttsQueue.length}`);
  if (ttsCurrentUrl) { ttsRevoke(ttsCurrentUrl); ttsCurrentUrl = null; }
  ttsPlaying = false;
  if (ttsQueue.length > 0) ttsPlayNext();
});
ttsAudio.addEventListener("error", () => {
  const err = ttsAudio.error;
  logDebug(`tts evt: 'error' code=${err?.code || "?"} msg=${err?.message || ""}`);
  if (ttsCurrentUrl) { ttsRevoke(ttsCurrentUrl); ttsCurrentUrl = null; }
  ttsPlaying = false;
  if (ttsQueue.length > 0) ttsPlayNext();
});

// 1-second silent WAV as a data: URL — bundled inline so playback
// starts SYNCHRONOUSLY inside the touch handler. Any fetch() would
// push the actual .play() call past the gesture window and iOS
// would reject it.
const SILENT_WAV =
  "data:audio/wav;base64,UklGRkQAAABXQVZFZm10IBAAAAABAAEAESsAACJWAAACABAAZGF0YSAAAAAA" +
  "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
let ttsUnlocked = false;

/** Call from inside a touch / click handler. Plays a sync data:
 *  URL silent clip to satisfy iOS' user-gesture requirement for
 *  audio playback. After this, fetched-audio plays work. */
function primeTts() {
  if (ttsUnlocked) { logDebug("tts: prime skip (already unlocked)"); return; }
  try {
    ttsAudio.src = SILENT_WAV;
    ttsAudio.volume = 1.0;
    // .play() returns a promise; we don't await — what matters is
    // that the call HAPPENED synchronously inside the gesture.
    const p = ttsAudio.play();
    logDebug("tts: prime play() called sync in gesture");
    if (p && p.catch) p.catch((e) =>
      logDebug(`tts: prime REJECTED ${e?.name || ""}: ${e?.message || e}`));
    if (p && p.then)  p.then(() => logDebug("tts: prime play resolved"));
    ttsUnlocked = true;
  } catch (e) {
    logDebug(`tts: prime threw ${e?.name || ""}: ${e?.message || e}`);
  }
}

function speakSentence(text) {
  if (!text) return;
  const trimmed = text.trim();
  if (!trimmed) return;
  // Start the network fetch IMMEDIATELY — don't wait until playback
  // reaches this slot. ttsSynthUrl is async; the returned promise
  // resolves to a blob URL (or null on failure). Storing the promise
  // (not the resolved value) lets ttsPlayNext() await it later
  // without us having to coordinate completion order.
  const urlPromise = ttsSynthUrl(trimmed).catch((e) => {
    logDebug(`tts: prefetch failed for "${trimmed.slice(0,28)}": ${e?.message || e}`);
    return null;
  });
  ttsQueue.push({ text: trimmed, urlPromise });
  ttsSpokeAnythingThisTurn = true;
  logDebug(`tts: enqueued+prefetch "${trimmed.slice(0,28)}" q=${ttsQueue.length} playing=${ttsPlaying}`);
  if (!ttsPlaying) ttsPlayNext();
}

function stopTts() {
  // Revoke blob URLs from any in-flight prefetches so we don't leak
  // memory. The fetches themselves can't be cancelled at this point
  // without an AbortController, but their resolved blob URLs are
  // useless once we've decided to stop — drop them as they arrive.
  const cancelled = ttsQueue;
  ttsQueue = [];
  for (const item of cancelled) {
    item.urlPromise.then((url) => { if (url) ttsRevoke(url); });
  }
  try { ttsAudio.pause(); } catch { /* ignore */ }
  if (ttsCurrentUrl) { ttsRevoke(ttsCurrentUrl); ttsCurrentUrl = null; }
  ttsPlaying = false;
  ttsSpokeAnythingThisTurn = false;
}

// ── One full text turn (used by PTT transcript + type panel) ─
async function runTurn(text) {
  // Cancel any lingering TTS from the previous turn so the iPhone
  // doesn't keep narrating the old reply while we're sending a new
  // one. The server-side brain is per-session, so the previous
  // partials are about to be replaced regardless.
  stopTts();
  abandonJarvis();
  addMessage("you", text);
  setState("processing");
  let reply;
  try {
    reply = await ws.send(text);
  } catch (err) {
    reply = `[ERROR] ${err?.message || err}`;
  }
  setState("speaking");
  if (liveJarvisBubble !== null) finalizeJarvis(reply);
  else                            addMessage("jarvis", reply);
  setState("active");
}

ptt.onTranscript((transcript) => {
  runTurn(transcript).catch((e) => console.warn("[app] runTurn:", e));
});

// ── PTT button: pointer events (covers touch + mouse) ────────
let pttPointerId = null;
pttBtn.addEventListener("pointerdown", (ev) => {
  if (ev.button !== undefined && ev.button !== 0) return;
  // Unlock Web Speech right inside the touch — by the time the
  // reply starts streaming back the gesture is long gone, and iOS
  // would otherwise silently drop our speak() calls.
  primeTts();
  if (ws.getState() !== ws.STATE.ONLINE) {
    // Try a reconnect on press so the user isn't stuck if the
    // socket dropped while the phone was idle.
    ws.connect();
    return;
  }
  ev.preventDefault();
  pttPointerId = ev.pointerId;
  pttBtn.setPointerCapture(ev.pointerId);
  pttBtn.classList.add("recording");
  pttBtn.setAttribute("aria-pressed", "true");
  pttLabel.textContent = "LISTENING…";
  ptt.start();
});

pttBtn.addEventListener("pointerup", (ev) => {
  if (ev.pointerId !== pttPointerId) return;
  pttPointerId = null;
  pttBtn.classList.remove("recording");
  pttBtn.setAttribute("aria-pressed", "false");
  pttLabel.textContent = "HOLD TO SPEAK";
  ptt.stop();
});

pttBtn.addEventListener("pointercancel", () => {
  pttPointerId = null;
  pttBtn.classList.remove("recording");
  pttBtn.setAttribute("aria-pressed", "false");
  pttLabel.textContent = "HOLD TO SPEAK";
  ptt.cancel();
});

// Timer text while holding
setInterval(() => {
  if (ptt.getState() === ptt.STATE.RECORDING) {
    const elapsed = Math.floor((performance.now() - window._pttStart) / 1000);
    pttTimer.textContent = `${elapsed}s`;
  } else if (ptt.getState() === ptt.STATE.PROCESSING) {
    pttTimer.textContent = "TRANSCRIBING";
  } else {
    pttTimer.textContent = "";
  }
}, 250);
ptt.onStateChange((s) => {
  if (s === ptt.STATE.RECORDING) window._pttStart = performance.now();
});

// ── Settings panel ───────────────────────────────────────────
function openSettings() {
  const c = cfg.get();
  cfgLocal.value = c.local;
  cfgTail.value  = c.tailscale;
  cfgToken.value = c.token;
  cfgStatus.textContent = "";
  settingsPanel.classList.add("open");
  settingsPanel.setAttribute("aria-hidden", "false");
}
function closeSettings() {
  settingsPanel.classList.remove("open");
  settingsPanel.setAttribute("aria-hidden", "true");
}
document.getElementById("act-settings").addEventListener("click", openSettings);
cfgClose.addEventListener("click", closeSettings);
cfgSave.addEventListener("click", async () => {
  cfg.save({
    local:     cfgLocal.value.trim(),
    tailscale: cfgTail.value.trim(),
    token:     cfgToken.value.trim(),
  });
  cfgStatus.textContent = "Saved. Reconnecting…";
  await ws.reconnect();
  cfgStatus.textContent = "Connected.";
  setTimeout(closeSettings, 600);
});
cfgTest.addEventListener("click", async () => {
  cfgStatus.textContent = "Testing…";
  const local = cfgLocal.value.trim();
  const tail  = cfgTail.value.trim();
  const a = local ? await cfg.ping(local) : { ok: false };
  const b = tail  ? await cfg.ping(tail)  : { ok: false };
  cfgStatus.textContent =
    `Local: ${a.ok ? `${a.ms} ms` : "unreachable"}   ·   ` +
    `Tailscale: ${b.ok ? `${b.ms} ms` : "unreachable"}`;
});

// ── Type-input panel ────────────────────────────────────────
function openText() {
  textPanel.classList.add("open");
  textPanel.setAttribute("aria-hidden", "false");
  setTimeout(() => textInput.focus(), 120);
}
function closeText() {
  textPanel.classList.remove("open");
  textPanel.setAttribute("aria-hidden", "true");
  textInput.value = "";
}
document.getElementById("act-search").addEventListener("click", openText);
textClose.addEventListener("click", closeText);
textSend.addEventListener("click", async () => {
  // Same primer rationale as the PTT button — must run inside the
  // gesture so the eventual replies are allowed to speak.
  primeTts();
  const v = textInput.value.trim();
  if (!v) return;
  closeText();
  await runTurn(v);
});
textInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") { ev.preventDefault(); textSend.click(); }
  else if (ev.key === "Escape") closeText();
});

// ── Music quick-action: stub → tells JARVIS naturally ────────
document.getElementById("act-music").addEventListener("click", () => {
  // Sends a natural-language command to JARVIS, who picks
  // music_transport tool through the existing tier-2 flow.
  runTurn("Spiele die aktuelle Musik weiter.").catch(() => {});
});

// ── Kill / interrupt button ─────────────────────────────────
killBadge.addEventListener("click", async () => {
  // Silence any in-flight iPhone TTS immediately — the server-side
  // /interrupt below stops the brain, but Web Speech keeps reading
  // its already-queued utterances unless we cancel them client-side.
  stopTts();
  const base = cfg.httpBase();
  if (!base) return;
  // First click after JARVIS is healthy = interrupt the current
  // reply (cheap, does not arm kill-switch). Long-press would
  // arm /emergency-stop; we keep it simple here and offer
  // interrupt only — matches the desktop Cmd+Shift+J semantic.
  try {
    await fetch(base + "/interrupt", {
      method: "POST",
      headers: cfg.authHeader(),
    });
  } catch (e) { console.warn("[app] interrupt:", e); }
});

// ── Camera ──────────────────────────────────────────────────
// Wire the camera button + panel + file-input. Passing in app-level
// helpers lets camera.js stay self-contained (no circular imports)
// and reuses the existing chat-message + state + debug-log + TTS
// paths so vision replies look AND sound exactly like a normal turn.
// speakSentence is critical here: vision uploads return text via
// HTTP, not via the WebSocket jarvis_partial stream, so without
// explicitly handing the reply to the TTS queue the PWA would
// display the answer silently.
camera.initCamera({
  addMessage, setState, logDebug, speakSentence, primeTts,
});

// ── Boot ────────────────────────────────────────────────────
ws.connect();
// If the user opens the app with no config saved, surface the
// settings panel immediately so they can paste local URL + token.
const initialCfg = cfg.get();
if (!initialCfg.token || (!initialCfg.local && !initialCfg.tailscale)) {
  openSettings();
}
