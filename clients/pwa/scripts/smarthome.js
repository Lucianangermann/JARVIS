// JARVIS PWA — Smart Home panel controller.
// Handles the 🏠 button, device control, scenes, and adapter status.

import { httpBase, authHeader } from "./config.js";

const panel    = () => document.getElementById("smarthome-panel");
const statusEl = () => document.getElementById("sh-status");

function setStatus(msg, isError = false) {
  const el = statusEl();
  if (!el) return;
  el.textContent = msg;
  el.style.color = isError ? "#ff4444" : "#00e5ff";
  if (msg) setTimeout(() => { if (el.textContent === msg) el.textContent = ""; }, 4000);
}

async function api(path, method = "GET", body = null) {
  const base = httpBase();
  if (!base) { setStatus("Nicht verbunden", true); return null; }
  try {
    const opts = {
      method,
      headers: { ...authHeader(), "Content-Type": "application/json" },
    };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(base + path, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } catch (e) {
    setStatus("Fehler: " + e.message, true);
    return null;
  }
}

async function control(action, extra = {}) {
  const data = await api("/smarthome/control", "POST", { action, ...extra });
  if (data?.result) setStatus(data.result);
}

async function runScene(name) {
  setStatus(`Szene '${name}' wird aktiviert…`);
  const data = await api("/smarthome/scenes/run", "POST", { name });
  if (data?.result) setStatus(data.result);
}

// Returns the currently selected device name, or null for "alle".
function selectedDevice() {
  const sel = document.getElementById("sh-device-select");
  return sel?.value || null;
}

async function loadDevices() {
  const data = await api("/smarthome/devices");
  const sel = document.getElementById("sh-device-select");
  if (!sel || !data) return;

  // Remove all options except the first "Alle Lichter" placeholder.
  while (sel.options.length > 1) sel.remove(1);

  for (const d of (data.devices || [])) {
    const opt = document.createElement("option");
    opt.value = d.name;
    opt.textContent = `${d.name} (${d.type})`;
    sel.appendChild(opt);
  }
}

async function loadStatus() {
  const data = await api("/smarthome/status");
  if (!data) return;
  const list = document.getElementById("sh-platform-list");
  if (!list) return;
  list.innerHTML = "";
  for (const adapter of (data.adapters || [])) {
    const dot = adapter.connected ? "🟢" : adapter.enabled ? "🔴" : "⚫";
    const div = document.createElement("div");
    div.className = "sh-platform-item";
    div.textContent = `${dot} ${adapter.platform}`;
    list.appendChild(div);
  }
  const devices = data.devices || {};
  const total = data.total_devices || 0;
  if (total > 0) {
    const summary = Object.entries(devices)
      .map(([t, n]) => `${n} ${t}`).join(", ");
    setStatus(`${total} Geräte: ${summary}`);
  } else {
    setStatus("Keine Geräte gefunden — GOVEE_API_KEY in .env prüfen");
  }
}

export function init() {
  const btn = document.getElementById("act-smarthome");
  if (!btn) return;

  // Open panel
  btn.addEventListener("click", () => {
    const p = panel();
    if (!p) return;
    p.setAttribute("aria-hidden", "false");
    p.classList.add("open");
    loadStatus();
    loadDevices();
  });

  // Close panel
  document.getElementById("sh-close")?.addEventListener("click", () => {
    const p = panel();
    p?.setAttribute("aria-hidden", "true");
    p?.classList.remove("open");
  });

  // Light controls (always broadcast)
  document.getElementById("sh-lights-on")?.addEventListener("click",
    () => control("command", { command: "alle lichter an" }));
  document.getElementById("sh-lights-off")?.addEventListener("click",
    () => control("command", { command: "alle lichter aus" }));

  // Plug controls (always broadcast)
  document.getElementById("sh-plugs-on")?.addEventListener("click",
    () => control("command", { command: "alle steckdosen an" }));
  document.getElementById("sh-plugs-off")?.addEventListener("click",
    () => control("command", { command: "alle steckdosen aus" }));

  // Brightness slider display
  const slider = document.getElementById("sh-brightness");
  const valEl  = document.getElementById("sh-brightness-val");
  slider?.addEventListener("input", () => {
    if (valEl) valEl.textContent = slider.value + "%";
  });

  // Brightness apply — use selected device or broadcast via NL command
  document.getElementById("sh-brightness-apply")?.addEventListener("click", () => {
    const level  = parseInt(slider?.value || "70", 10);
    const device = selectedDevice();
    if (device) {
      control("brightness", { device, level });
    } else {
      control("command", { command: `alle lichter ${level}%` });
    }
  });

  // Color buttons — use selected device or broadcast via NL command
  document.querySelectorAll(".sh-color").forEach(colorBtn => {
    colorBtn.addEventListener("click", () => {
      const color  = colorBtn.dataset.color;
      const device = selectedDevice();
      if (device) {
        control("color", { device, color });
      } else {
        control("command", { command: `alle lichter ${color}` });
      }
    });
  });

  // Scene buttons
  document.querySelectorAll(".sh-scene-btn").forEach(sceneBtn => {
    sceneBtn.addEventListener("click", () => {
      runScene(sceneBtn.dataset.scene);
    });
  });
}

// Auto-init when module loads.
document.addEventListener("DOMContentLoaded", init);
