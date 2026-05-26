// JARVIS PWA — Entertainment panel
// Wires mood music, watchlist, and voice game buttons to the WebSocket.

import * as cfg from "./config.js";
import * as ws  from "./websocket.js";

const panel        = document.getElementById("entertainment-panel");
const statusEl     = document.getElementById("ent-status");
const closeBtn     = document.getElementById("ent-close");
const openBtn      = document.getElementById("act-entertainment");
const watchlistBtn = document.getElementById("ent-watchlist-load");
const watchlistEl  = document.getElementById("ent-watchlist-items");
const triviaBtn    = document.getElementById("ent-trivia");
const jokeBtn      = document.getElementById("ent-joke");
const riddleBtn    = document.getElementById("ent-riddle");
const factBtn      = document.getElementById("ent-fact");

// ── Panel open / close ────────────────────────────────────────────────────

function openPanel() {
  panel.classList.add("open");
  panel.setAttribute("aria-hidden", "false");
}

function closePanel() {
  panel.classList.remove("open");
  panel.setAttribute("aria-hidden", "true");
  statusEl.textContent = "";
}

openBtn.addEventListener("click", openPanel);
closeBtn.addEventListener("click", closePanel);

// ── Helpers ───────────────────────────────────────────────────────────────

function setStatus(msg) {
  statusEl.textContent = msg;
}

async function sendCommand(text) {
  try {
    await ws.send(text);
    closePanel();
  } catch {
    setStatus("Verbindungsfehler.");
  }
}

async function apiGet(path) {
  const base = cfg.httpBase();
  if (!base) return null;
  try {
    const r = await fetch(`${base}${path}`, {
      method: "GET",
      headers: cfg.authHeader(),
      cache: "no-store",
    });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

// ── Mood music buttons ────────────────────────────────────────────────────

document.querySelectorAll(".ent-mood").forEach((btn) => {
  btn.addEventListener("click", () => {
    const mood = btn.dataset.mood;
    sendCommand(`spiele ${mood} musik`);
  });
});

// ── Watchlist ─────────────────────────────────────────────────────────────

watchlistBtn.addEventListener("click", async () => {
  watchlistEl.innerHTML = '<span class="sh-loading">Lade...</span>';
  const data = await apiGet("/entertainment/watchlist");
  if (!data || !data.items || data.items.length === 0) {
    watchlistEl.textContent = "Watchlist leer.";
    return;
  }
  watchlistEl.innerHTML = "";
  data.items.slice(0, 8).forEach((item, i) => {
    const row = document.createElement("div");
    row.style.cssText = "padding:3px 0;border-bottom:1px solid #1a2a3a;";
    row.textContent = `${i + 1}. ${item.title}`;
    if (item.type && item.type !== "unknown") {
      const badge = document.createElement("span");
      badge.style.cssText = "margin-left:6px;font-size:0.75em;opacity:0.6;";
      badge.textContent = `(${item.type})`;
      row.appendChild(badge);
    }
    watchlistEl.appendChild(row);
  });
});

// ── Game buttons ──────────────────────────────────────────────────────────

triviaBtn.addEventListener("click", () => sendCommand("starte trivia"));
jokeBtn.addEventListener("click",   () => sendCommand("erzähl mir einen witz"));
riddleBtn.addEventListener("click", () => sendCommand("stell mir ein rätsel"));
factBtn.addEventListener("click",   () => sendCommand("nenne mir einen interessanten fakt"));
