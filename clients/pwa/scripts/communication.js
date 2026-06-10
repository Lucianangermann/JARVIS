// JARVIS PWA — Communication panel controller.
// Handles the 💬 button: unread counts, missed calls, DND, translate.
// Mirrors security.js / smarthome.js fetch + panel-toggle pattern.

import { httpBase, authHeader } from "./config.js";

const panel    = () => document.getElementById("comm-panel");
const statusEl = () => document.getElementById("comm-status");

let dndOn = false;

function setStatus(msg, isError = false) {
  const el = statusEl();
  if (!el) return;
  el.textContent = msg;
  el.style.color = isError ? "#ff4444" : "#00e5ff";
  if (msg) setTimeout(() => { if (el.textContent === msg) el.textContent = ""; }, 6000);
}

async function api(path, method = "GET", body = null) {
  const base = httpBase();
  if (!base) { setStatus("Nicht verbunden", true); return null; }
  try {
    const opts = { method, headers: { ...authHeader(), "Content-Type": "application/json" } };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(base + path, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } catch (e) {
    setStatus("Fehler: " + e.message, true);
    return null;
  }
}

async function loadUnread() {
  const d = await api("/communication/messages/unread");
  if (!d) return;
  const im = document.getElementById("comm-im-count");
  const tg = document.getElementById("comm-tg-count");
  if (im) im.textContent = `${(d.imessage || []).length} ungelesen`;
  if (tg) tg.textContent = `${(d.telegram || []).length} ungelesen`;
}

async function missed() {
  const d = await api("/communication/calls/missed");
  if (d?.spoken) setStatus(d.spoken);
}

async function setDnd(on) {
  const d = await api("/communication/notifications/dnd", "POST", { enabled: on });
  if (d) {
    dndOn = !!d.dnd;
    const el = document.getElementById("comm-dnd-state");
    if (el) el.textContent = dndOn ? "AN" : "AUS";
    setStatus(dndOn ? "Nicht stören aktiviert." : "Nicht stören deaktiviert.");
  }
}

async function translate(to) {
  const text = (document.getElementById("comm-tr-text")?.value || "").trim();
  if (!text) { setStatus("Bitte Text eingeben.", true); return; }
  setStatus("Übersetze…");
  const d = await api("/communication/translate", "POST",
                      { text, target_lang: to, source_lang: "auto" });
  if (d?.translation) setStatus(d.translation);
}

export function init() {
  const btn = document.getElementById("act-comm");
  if (!btn) return;

  btn.addEventListener("click", () => {
    const p = panel();
    if (!p) return;
    p.setAttribute("aria-hidden", "false");
    p.classList.add("open");
    loadUnread();
  });

  document.getElementById("comm-close")?.addEventListener("click", () => {
    const p = panel();
    p?.setAttribute("aria-hidden", "true");
    p?.classList.remove("open");
  });

  document.getElementById("comm-unread-refresh")?.addEventListener("click", loadUnread);
  document.getElementById("comm-missed")?.addEventListener("click", missed);
  document.getElementById("comm-dnd-on")?.addEventListener("click", () => setDnd(true));
  document.getElementById("comm-dnd-off")?.addEventListener("click", () => setDnd(false));
  document.querySelectorAll(".comm-tr").forEach(b =>
    b.addEventListener("click", () => translate(b.dataset.to)));
}

document.addEventListener("DOMContentLoaded", init);
