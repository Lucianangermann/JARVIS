// ============================================================
// JARVIS overlay — main renderer logic
//
// Owns:
//   • State machine (IDLE / ACTIVE / SPEAKING / PROCESSING)
//   • Mock chat history (typewriter effect)
//   • Status-bar clock + connection label
//   • Hex button handlers + state cycling
//   • Keyboard shortcuts (renderer scope; main-process global
//     shortcuts are a Phase-2 add)
//
// The renderer never touches Node directly — `window.jarvis` (from
// preload.js via contextBridge) is the only privileged surface.
// ============================================================

import { setVisualizerState } from "./visualizer.js";

const STATES = ["idle", "active", "speaking", "processing"];

const body       = document.body;
const chatPane   = document.getElementById("chat-pane");
const clockEl    = document.getElementById("clock");
const connDot    = document.getElementById("conn-dot");
const connLabel  = document.getElementById("conn-label");
const orb        = document.getElementById("orb");
const cmdInput   = document.getElementById("cmd-input");

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

/** Public for Phase 2 / WebSocket: flip the connection indicator. */
export function setConnection(state) {
  // state ∈ {"online", "offline", "error"}
  connDot.classList.remove("bad", "warn");
  if (state === "online") {
    connLabel.textContent = "ONLINE";
  } else if (state === "offline") {
    connDot.classList.add("warn");
    connLabel.textContent = "OFFLINE";
  } else {
    connDot.classList.add("bad");
    connLabel.textContent = "ERROR";
  }
}

// Phase-1 defaults: pretend we're offline. Phase 2 will wire to /ws.
setConnection("offline");

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
    addMessage("you", text);
    // Phase-1: simulate a thinking → speaking → idle cycle so we can
    // see all four states in action. Phase 2 replaces this with a
    // real WebSocket round-trip.
    setState("processing");
    setTimeout(async () => {
      setState("speaking");
      await addMessage("jarvis", mockReply(text));
      setTimeout(() => setState("active"), 600);
    }, 900);
  } else if (ev.key === "Escape") {
    setState("idle");
  }
});

function mockReply(userText) {
  const t = userText.toLowerCase();
  if (t.includes("hallo") || t.includes("hi")) return "Guten Abend. Wobei darf ich helfen?";
  if (t.includes("wie spät") || t.includes("zeit")) {
    return `Es ist ${clockEl.textContent} Uhr.`;
  }
  if (t.includes("status")) return "Alle Systeme nominal. T2 unlocked, Kill-Switch armed.";
  return `Verstanden. Ich verarbeite »${userText}«…`;
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
