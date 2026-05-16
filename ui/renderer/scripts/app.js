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

/** Append a message to the chat pane with a typewriter effect.
 *  who: "you" | "jarvis"
 *  Returns a promise that resolves when typing finishes. */
function addMessage(who, text) {
  const msg = document.createElement("div");
  msg.className = `chat-msg ${who}`;
  const whoEl = document.createElement("span");
  whoEl.className = "who";
  whoEl.textContent = who === "you" ? "YOU ›" : "J.A.R.V.I.S. ›";
  const body = document.createElement("span");
  body.className = "body cursor";
  msg.append(whoEl, body);
  chatPane.appendChild(msg);

  // Trim chat to the last 3 messages so the pane never overflows.
  while (chatPane.children.length > 3) chatPane.firstChild.remove();

  // Typewriter — JS-driven so we can vary speed per char if we ever
  // want to (e.g. slow down at punctuation). For now, flat 22 ms/char.
  const myId = ++typewriterId;
  return new Promise((resolve) => {
    let i = 0;
    const tick = () => {
      if (myId !== typewriterId) return;            // a newer message replaced us
      body.firstChild?.remove?.();                  // (no-op; placeholder for future)
      body.textContent = text.slice(0, i);
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
      // Server is back to passive wake-word listening. If the user
      // already collapsed to the orb, don't pull it back open — just
      // park us in "active" otherwise so the HUD stays usable for
      // typed follow-ups.
      if (currentState !== "idle") setState("active");
      break;
  }
});

ws.onEvent("user_message", ({ text }) => {
  if (text) addMessage("you", text);
});

ws.onEvent("jarvis_reply", ({ text }) => {
  // Mirrors the text path's chat line. TTS plays the audio in parallel
  // on the server side; the typewriter visual matches the spoken pace
  // closely enough that they don't feel out of sync.
  if (text) addMessage("jarvis", text);
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
      case "home":
        // Placeholder — Phase 2 will hook to /chat or /confirm.
        addMessage("you", `[${action.toUpperCase()}] (not wired yet)`);
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
 *  await the server, then SPEAKING (with the typewriter) → ACTIVE.
 *  Errors get rendered as a chat line tagged [ERROR] so the user
 *  always sees what happened, instead of a silent stuck state. */
async function runTurn(text) {
  addMessage("you", text);
  setState("processing");
  let reply;
  try {
    reply = await ws.send(text);
  } catch (err) {
    reply = `[ERROR] ${err?.message || err}`;
  }
  setState("speaking");
  await addMessage("jarvis", reply);
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

// ---- seed the UI with some content so first paint isn't empty ----

(function seed() {
  // Show 2 messages in the chat pane immediately, but don't auto-state-
  // transition the user — they explicitly click the orb to expand.
  addMessage("jarvis", "Online. Awaiting instruction.");
})();

// Expose for ad-hoc poking from DevTools.
window.__JARVIS_UI__ = { setState, addMessage, setConnection };
