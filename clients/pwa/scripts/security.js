// JARVIS PWA — Security panel controller.
// Handles the 🔒 button: arm/disarm, system-health bars, camera,
// network scan, and the SOS button. Mirrors smarthome.js's fetch +
// panel-toggle pattern.

import { httpBase, authHeader } from "./config.js";

const panel    = () => document.getElementById("security-panel");
const statusEl = () => document.getElementById("sec-status");

let camOn = false;

function setStatus(msg, isError = false) {
  const el = statusEl();
  if (!el) return;
  el.textContent = msg;
  el.style.color = isError ? "#ff4444" : "#00e5ff";
  if (msg) setTimeout(() => { if (el.textContent === msg) el.textContent = ""; }, 5000);
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

// ── system health bars ──────────────────────────────────────────────────── #

function setBar(id, pct, label) {
  const fill = document.getElementById(`sec-${id}-fill`);
  const val  = document.getElementById(`sec-${id}-val`);
  if (fill) {
    fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    // green < 70, amber < 90, red ≥ 90.
    fill.style.background = pct >= 90 ? "#ff3b3b" : pct >= 70 ? "#ffb02e" : "#00e5ff";
  }
  if (val) val.textContent = label;
}

async function loadHealth() {
  const h = await api("/security/system/health");
  if (!h) return;
  setBar("cpu",  h.cpu_percent,  `${Math.round(h.cpu_percent)}%`);
  setBar("ram",  h.ram_percent,  `${Math.round(h.ram_percent)}%`);
  setBar("disk", h.disk_percent, `${Math.round(h.disk_percent)}%`);
  if (h.battery_percent === null || h.battery_percent === undefined) {
    setBar("batt", 0, "—");
  } else {
    const plug = h.battery_charging ? " 🔌" : "";
    setBar("batt", h.battery_percent, `${h.battery_percent}%${plug}`);
  }
}

// ── home status ─────────────────────────────────────────────────────────── #

async function loadArmState() {
  const s = await api("/security/home/status");
  const el = document.getElementById("sec-arm-state");
  if (s && el) {
    el.textContent = s.armed ? `AKTIV (${s.mode})` : "DEAKTIVIERT";
    el.style.color = s.armed ? "#ff6a6a" : "#00e5ff";
  }
}

async function arm(mode) {
  const r = await api("/security/home/arm", "POST", { mode });
  if (r?.spoken) setStatus(r.spoken);
  loadArmState();
}

async function disarm() {
  const r = await api("/security/home/disarm", "POST");
  if (r?.spoken) setStatus(r.spoken);
  loadArmState();
}

async function checklist() {
  const r = await api("/security/home/checklist");
  if (r?.spoken) setStatus(r.spoken);
}

// ── camera ──────────────────────────────────────────────────────────────── #

async function whosAtDoor() {
  setStatus("Analysiere Türkamera…");
  const r = await api("/security/camera/snapshot");
  if (r?.description) setStatus(r.description);
}

async function toggleCamera() {
  if (camOn) {
    await api("/security/camera/stop", "POST");
    camOn = false;
    setStatus("Kameraüberwachung deaktiviert.");
  } else {
    const r = await api("/security/camera/start", "POST",
                        { force: true, sensitivity: "medium" });
    camOn = !!r?.ok;
    setStatus(r?.ok ? "Kameraüberwachung aktiv." : `Kamera: ${r?.error || "Fehler"}`, !r?.ok);
  }
}

// ── digital + emergency ─────────────────────────────────────────────────── #

async function netscan() {
  setStatus("Scanne Netzwerk…");
  const r = await api("/security/digital/network");
  if (r) {
    setStatus(`${r.total} Geräte, ${r.unknown_count} unbekannt.`,
              r.unknown_count > 0);
  }
}

async function sos() {
  if (!confirm("SOS-Notfall auslösen? Notfallkontakte werden benachrichtigt.")) return;
  const r = await api("/security/emergency/sos", "POST");
  if (r) setStatus("🚨 SOS aktiviert! Notruf 112, Polizei 110.", true);
}

// ── init ────────────────────────────────────────────────────────────────── #

export function init() {
  const btn = document.getElementById("act-security");
  if (!btn) return;

  btn.addEventListener("click", () => {
    const p = panel();
    if (!p) return;
    p.setAttribute("aria-hidden", "false");
    p.classList.add("open");
    loadArmState();
    loadHealth();
  });

  document.getElementById("sec-close")?.addEventListener("click", () => {
    const p = panel();
    p?.setAttribute("aria-hidden", "true");
    p?.classList.remove("open");
  });

  document.getElementById("sec-arm-away")?.addEventListener("click", () => arm("away"));
  document.getElementById("sec-arm-night")?.addEventListener("click", () => arm("night"));
  document.getElementById("sec-disarm")?.addEventListener("click", disarm);
  document.getElementById("sec-checklist")?.addEventListener("click", checklist);
  document.getElementById("sec-health-refresh")?.addEventListener("click", loadHealth);
  document.getElementById("sec-door")?.addEventListener("click", whosAtDoor);
  document.getElementById("sec-cam-toggle")?.addEventListener("click", toggleCamera);
  document.getElementById("sec-netscan")?.addEventListener("click", netscan);
  document.getElementById("sec-sos")?.addEventListener("click", sos);
}

document.addEventListener("DOMContentLoaded", init);
