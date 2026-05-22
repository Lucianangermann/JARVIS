// ============================================================
// JARVIS overlay — main renderer logic
//
// Owns:
//   • State machine (IDLE / ACTIVE / SPEAKING / PROCESSING)
//   • Chat history rendering (typewriter effect)
//   • Status-bar clock + connection label
//   • Hex button handlers + state cycling
//   • Keyboard shortcuts (renderer scope) + Cmd+J toggle from main
//   • Server roundtrip via ws.js (real Claude reply over /ws)
//
// The renderer never touches Node directly — `window.jarvis` (from
// preload.js via contextBridge) is the only privileged surface.
// ============================================================

import { setVisualizerState } from "./visualizer.js";
import * as ws from "./ws.js";
import * as perms from "./permissions.js";
import * as pending from "./pending.js";
import * as smarthome from "./smarthome.js";

const STATES = ["idle", "active", "speaking", "processing"];

const body       = document.body;
const chatPane   = document.getElementById("chat-pane");
const clockEl    = document.getElementById("clock");
const connDot    = document.getElementById("conn-dot");
const connLabel  = document.getElementById("conn-label");
const orb        = document.getElementById("orb");
const cmdInput   = document.getElementById("cmd-input");
const killBadge  = document.getElementById("kill-badge");

let currentState = "idle";
let typewriterId = 0;     // monotonic id so an in-flight typewriter can be cancelled

// ---- state machine -------------------------------------------------

async function setState(next) {
  if (!STATES.includes(next) || next === currentState) return;
  console.log(`[JARVIS UI] ${currentState.toUpperCase()} → ${next.toUpperCase()}`);
  currentState = next;
  body.dataset.state = next;
  setVisualizerState(next);

  // Tell the main process to resize/reposition the OS window. If we're
  // running outside Electron (e.g. opening index.html directly in a
  // browser for quick visual tweaks), `window.jarvis` is absent — fall
  // back to a no-op.
  if (window.jarvis?.setState) {
    try { await window.jarvis.setState(next); }
    catch (err) { console.warn("[JARVIS UI] setState IPC failed:", err); }
  }
}

// ---- typewriter chat ---------------------------------------------

/** Max chat messages kept in the DOM. Older ones get dropped from
 *  the top so the pane doesn't grow unbounded over a long session.
 *  100 is plenty to scroll back through a few conversations. */
const CHAT_HISTORY_LIMIT = 100;

/** Distance (px) from the bottom within which we still treat the
 *  scrollbar as "at the bottom". Picks up tiny rounding errors after
 *  the typewriter ticks. */
const STICKY_BOTTOM_PX = 40;

function isPinnedToBottom() {
  return chatPane.scrollHeight - chatPane.scrollTop - chatPane.clientHeight
         <= STICKY_BOTTOM_PX;
}

/** The chat bubble currently being filled by streaming jarvis_partial
 *  events. Reset to null when a turn finishes (jarvis_reply received,
 *  user starts a new message, or voice loop returns to listening). */
let liveJarvisBubble = null;

/** Streaming append: each partial extends the same JARVIS bubble.
 *  The first partial of a turn creates the bubble; subsequent
 *  partials extend its body text. No typewriter — partials arrive
 *  at natural typing pace from Claude's stream already. */
function appendJarvisPartial(text) {
  if (!text) return;
  const stick = isPinnedToBottom();
  if (liveJarvisBubble === null) {
    const msg = document.createElement("div");
    msg.className = "chat-msg jarvis";
    const whoEl = document.createElement("span");
    whoEl.className = "who";
    whoEl.textContent = "J.A.R.V.I.S. ›";
    const body = document.createElement("span");
    body.className = "body cursor";
    msg.append(whoEl, body);
    chatPane.appendChild(msg);
    while (chatPane.children.length > CHAT_HISTORY_LIMIT) {
      chatPane.firstChild.remove();
    }
    liveJarvisBubble = body;
  }
  const prev = liveJarvisBubble.textContent;
  // Separator: only insert a space if the new chunk starts a fresh
  // sentence (i.e. the previous chunk already ended with terminal
  // punctuation). The server flushes per sentence so this almost
  // always wants one space.
  const sep = (prev && !prev.endsWith(" ")) ? " " : "";
  liveJarvisBubble.textContent = prev + sep + text;
  if (stick) chatPane.scrollTop = chatPane.scrollHeight;
}

/** Mark the live bubble complete. If ``fullText`` is provided we
 *  replace the bubble's contents — guards against the rare case
 *  where partials dropped a chunk on the wire. */
function finalizeJarvisBubble(fullText) {
  if (liveJarvisBubble !== null) {
    if (fullText && fullText.trim()) {
      liveJarvisBubble.textContent = fullText;
    }
    liveJarvisBubble.classList.remove("cursor");
  }
  liveJarvisBubble = null;
}

/** Called on interrupt / listening transitions — closes any
 *  in-progress bubble without overwriting its partial content,
 *  so the user can see what was spoken before the cancel. */
function abandonJarvisBubble() {
  if (liveJarvisBubble !== null) {
    liveJarvisBubble.classList.remove("cursor");
  }
  liveJarvisBubble = null;
}

/** Append a message to the chat pane with a typewriter effect.
 *  who: "you" | "jarvis"
 *  Returns a promise that resolves when typing finishes.
 *
 *  Scroll behaviour: if the user was already reading the most recent
 *  messages (within STICKY_BOTTOM_PX of the bottom), we follow new
 *  output to the bottom on every typewriter tick. If they've scrolled
 *  up to re-read older context, we leave their position alone so a
 *  new message doesn't yank them away. */
function addMessage(who, text) {
  const stick = isPinnedToBottom();

  const msg = document.createElement("div");
  msg.className = `chat-msg ${who}`;
  const whoEl = document.createElement("span");
  whoEl.className = "who";
  whoEl.textContent = who === "you" ? "YOU ›" : "J.A.R.V.I.S. ›";
  const body = document.createElement("span");
  body.className = "body cursor";
  msg.append(whoEl, body);
  chatPane.appendChild(msg);

  // Trim old history off the top — keeps DOM size bounded.
  while (chatPane.children.length > CHAT_HISTORY_LIMIT) {
    chatPane.firstChild.remove();
  }

  if (stick) chatPane.scrollTop = chatPane.scrollHeight;

  // Typewriter — JS-driven so we can vary speed per char if we ever
  // want to (e.g. slow down at punctuation). For now, flat 22 ms/char.
  // Per-tick we re-check isPinnedToBottom so the auto-follow yields
  // the moment the user manually scrolls up mid-typewriter to re-read
  // earlier context.
  const myId = ++typewriterId;
  return new Promise((resolve) => {
    let i = 0;
    const tick = () => {
      if (myId !== typewriterId) return;            // a newer message replaced us
      body.textContent = text.slice(0, i);
      if (stick && isPinnedToBottom()) {
        chatPane.scrollTop = chatPane.scrollHeight;
      }
      if (i >= text.length) {
        body.classList.remove("cursor");
        return resolve();
      }
      i++;
      setTimeout(tick, 22);
    };
    tick();
  });
}

// ---- status bar ---------------------------------------------------

function tickClock() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  clockEl.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
tickClock();
setInterval(tickClock, 1000);

/** Reflect the WS connection state in the status-bar indicator.
 *  Mapped from ws.STATE — green for ONLINE, amber for CONNECTING/
 *  OFFLINE (transient, reconnect loop is running), red for ERROR
 *  (misconfig / auth fail — won't fix itself). */
export function setConnection(state) {
  connDot.classList.remove("bad", "warn");
  switch (state) {
    case ws.STATE.ONLINE:
      connLabel.textContent = "ONLINE";
      break;
    case ws.STATE.CONNECTING:
      connDot.classList.add("warn");
      connLabel.textContent = "CONNECTING";
      break;
    case ws.STATE.OFFLINE:
    case ws.STATE.IDLE:
      connDot.classList.add("warn");
      connLabel.textContent = "OFFLINE";
      break;
    case ws.STATE.ERROR:
    default:
      connDot.classList.add("bad");
      connLabel.textContent = "ERROR";
      break;
  }
}

// Wire the connection indicator + kick off the socket. The listener
// is called synchronously with the current state so the dot starts
// correctly even before the first transition.
ws.onConnectionChange(setConnection);

// ---- voice events from the server's local mic loop ----------------
// When JARVIS_LOCAL_VOICE=1 is set on the server, voice_loop publishes
// state transitions over /ws as it works through the wake-word →
// transcribe → think → speak pipeline. We mirror those onto the HUD
// state machine so what the user sees matches what the assistant is
// doing in real time. Text-input chat still drives setState itself
// (runTurn below); voice and text share the same visual states.
ws.onEvent("voice_state", ({ state }) => {
  switch (state) {
    case "transcribing":
    case "thinking":
      // Auto-open the HUD: the user is interacting, even if they
      // last left it collapsed to the orb.
      setState("processing");
      break;
    case "speaking":
      setState("speaking");
      break;
    case "listening":
      // Server is back to passive wake-word listening. If a live
      // streaming bubble is still showing the cursor (interrupted
      // mid-stream), close it off. Otherwise the normal finalize
      // path via jarvis_reply already cleared it.
      abandonJarvisBubble();
      // If the user already collapsed to the orb, don't pull it
      // back open — just park us in "active" otherwise so the HUD
      // stays usable for typed follow-ups.
      if (currentState !== "idle") setState("active");
      break;
  }
});

ws.onEvent("user_message", ({ text }) => {
  // Voice path: transcribed user turn arrives. Reset any lingering
  // jarvis-side live bubble so the next partial creates a fresh one.
  abandonJarvisBubble();
  if (text) addMessage("you", text);
});

ws.onEvent("jarvis_partial", ({ text }) => {
  // Streaming text from the brain: each partial extends the same
  // bubble. Fires for both voice-path and text-path turns — the
  // brain publishes partials regardless of which client triggered.
  appendJarvisPartial(text);
});

ws.onEvent("jarvis_reply", ({ text }) => {
  // Voice-path: marks the streamed reply complete. If partials had
  // been arriving the live bubble is finalised in place; otherwise
  // (early failure, no partials) we still want SOMETHING shown.
  if (liveJarvisBubble !== null) {
    finalizeJarvisBubble(text);
  } else if (text) {
    addMessage("jarvis", text);
  }
});

ws.connect();

// ---- permission status (tier dots + kill-switch badge) ------------

/** Highest reachable capability tier — drives how many tier dots light
 *  up in the status bar. T1 is always present when the server reports
 *  enabled; T2/T4 depend on session unlock / password configuration.
 *  T3 is per-action confirm — always reachable if enabled, so we treat
 *  "enabled" alone as floor=3. */
function highestTier(p) {
  if (!p || !p.enabled) return 1;
  if (p.tier4_available) return 4;
  if (p.tier2_unlocked)  return 3;     // T2 unlocked + T3 confirmable
  return 3;                            // T3 always confirmable when enabled
}

function applyPermissionSnapshot(snap) {
  if (!snap) return;
  body.dataset.tier = String(highestTier(snap));
  const killed = !!(snap.kill_switch && snap.kill_switch.killed);
  body.dataset.kill = killed ? "true" : "false";
  if (killBadge) {
    killBadge.textContent = killed ? "KILLED" : "STOP";
    if (killed && snap.kill_switch?.reason) {
      killBadge.title = `Killed: ${snap.kill_switch.reason} — click to resume.`;
    } else {
      killBadge.title = "Click to trigger emergency stop (Esc)";
    }
  }
  // Pending action cards are rendered by a separate module (next chunk).
}

perms.onUpdate(applyPermissionSnapshot);
pending.init();          // subscribes to perms.onUpdate too
perms.start();

if (killBadge) {
  killBadge.addEventListener("click", async () => {
    const snap = perms.getSnapshot();
    const killed = !!(snap?.kill_switch && snap.kill_switch.killed);
    try {
      if (killed) await perms.resume();
      else        await perms.emergencyStop();
    } catch (err) {
      console.warn("[JARVIS UI] kill-switch toggle failed:", err);
      addMessage("jarvis", `[ERROR] kill-switch: ${err.message || err}`);
    }
  });
}

// ---- hex buttons --------------------------------------------------

document.querySelectorAll(".hex-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const action = btn.dataset.action;
    switch (action) {
      case "minimize":
        setState("idle");
        break;
      case "cycle":
        // Dev helper: rotate idle → active → speaking → processing → idle…
        cycleState();
        break;
      case "music":
      case "search":
        // Placeholder — will hook to /chat or /confirm in a future phase.
        addMessage("you", `[${action.toUpperCase()}] (not wired yet)`);
        break;
      case "home":
        // Handled by smarthome.js (toggles the sh-panel).
        break;
    }
  });
});

function cycleState() {
  const i = STATES.indexOf(currentState);
  const next = STATES[(i + 1) % STATES.length];
  setState(next);
}

// ---- orb click → expand to HUD ------------------------------------

orb.addEventListener("click", () => setState("active"));

// ---- input field --------------------------------------------------

cmdInput.addEventListener("keydown", async (ev) => {
  if (ev.key === "Enter") {
    ev.preventDefault();
    const text = cmdInput.value.trim();
    if (!text) return;
    cmdInput.value = "";
    await runTurn(text);
  } else if (ev.key === "Escape") {
    setState("idle");
  }
});

/** One full chat turn: echo the user line, drop into PROCESSING,
 *  await the server, then SPEAKING → ACTIVE. The brain streams its
 *  reply over ``jarvis_partial`` events while we wait on ws.send(),
 *  so the bubble fills in real time. When the response arrives in
 *  full, finalize the streamed bubble — or fall back to a typewriter
 *  bubble if no partials made it through. */
async function runTurn(text) {
  // Reset any half-finished previous-turn bubble so partials for
  // the new turn don't accidentally append to it.
  abandonJarvisBubble();
  addMessage("you", text);
  setState("processing");
  let reply;
  try {
    reply = await ws.send(text);
  } catch (err) {
    reply = `[ERROR] ${err?.message || err}`;
  }
  setState("speaking");
  if (liveJarvisBubble !== null) {
    // Streamed bubble exists — replace its contents with the
    // authoritative full text and drop the cursor. No typewriter
    // needed: the user already read the streamed sentences.
    finalizeJarvisBubble(reply);
  } else {
    // No partials arrived (eg. error before any text token, or
    // server-side events bus failed). Show the answer via the
    // classic typewriter so the user isn't staring at silence.
    await addMessage("jarvis", reply);
  }
  setState("active");
}

// ---- keyboard shortcuts (renderer scope, only while we have focus) ----

document.addEventListener("keydown", (ev) => {
  // Don't hijack the text input.
  if (ev.target === cmdInput) return;

  if (ev.key === "Escape") {
    setState("idle");
  } else if (ev.key === " ") {
    // Quick dev: spacebar cycles state when input is not focused.
    ev.preventDefault();
    cycleState();
  }
});

// ---- global hotkey toggle (Cmd/Ctrl+J) ----
// Main process owns the OS-level shortcut registration and pings us
// here. We map "toggle" onto the existing state machine: if we're
// idle, expand to the HUD; otherwise collapse back to idle. setState
// already drives both the DOM and the window resize via IPC, so this
// stays a one-liner.
window.jarvis?.onToggle?.(() => {
  setState(currentState === "idle" ? "active" : "idle");
});

// ---- global hotkey interrupt (Cmd/Ctrl+Shift+J) ----
// Fires POST /interrupt — cancels the in-flight brain reply + stops
// TTS without arming the kill switch. The server's voice_loop emits
// a "listening" voice_state event right after, which our existing
// event subscriber maps back to setState("active"); no DOM update
// needed here.
window.jarvis?.onInterrupt?.(() => {
  perms.interrupt().catch((err) => {
    console.warn("[JARVIS UI] interrupt failed:", err);
  });
});

// ---- smart home mini-panel ----------------------------------------
smarthome.init();

// ---- seed the UI with some content so first paint isn't empty ----

(function seed() {
  // Show 2 messages in the chat pane immediately, but don't auto-state-
  // transition the user — they explicitly click the orb to expand.
  addMessage("jarvis", "Online. Awaiting instruction.");
})();

// Expose for ad-hoc poking from DevTools.
window.__JARVIS_UI__ = { setState, addMessage, setConnection };
