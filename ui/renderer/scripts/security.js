// JARVIS Electron HUD — Security mini-panel.
// Wires the 🔒 button to a compact overlay: system health, arm/disarm,
// who's-at-the-door, network scan, SOS. Same config/api pattern as
// smarthome.js.

let _baseUrl = null;
let _token   = null;

async function ensureConfig() {
  if (_baseUrl) return true;
  if (!window.jarvis?.getConfig) return false;
  const cfg = await window.jarvis.getConfig();
  if (!cfg?.token) return false;
  _baseUrl = `${cfg.ssl ? "https" : "http"}://${cfg.host}:${cfg.port}`;
  _token   = cfg.token;
  return true;
}

async function api(path, method = "GET", body = null) {
  if (!await ensureConfig()) return null;
  try {
    const opts = {
      method,
      headers: { "Authorization": `Bearer ${_token}`, "Content-Type": "application/json" },
    };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(_baseUrl + path, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  } catch (e) {
    console.warn("[security]", e);
    return null;
  }
}

function msg(text) {
  const el = document.getElementById("sec-msg");
  if (el) { el.textContent = text; setTimeout(() => { if (el.textContent === text) el.textContent = ""; }, 5000); }
}

async function loadHealth() {
  const h = await api("/security/system/health");
  const el = document.getElementById("sec-health");
  if (h && el) {
    const batt = (h.battery_percent ?? "—");
    el.textContent =
      `CPU ${Math.round(h.cpu_percent)}% · RAM ${Math.round(h.ram_percent)}% · `
      + `DISK ${Math.round(h.disk_percent)}% · AKKU ${batt}%`;
  }
}

export function init() {
  const panel = document.getElementById("sec-panel");
  const btn   = document.querySelector('[data-action="security"]');
  if (!panel || !btn) return;

  const open  = () => { panel.removeAttribute("hidden"); loadHealth(); };
  const close = () => panel.setAttribute("hidden", "");

  btn.addEventListener("click", () => panel.hasAttribute("hidden") ? open() : close());
  document.getElementById("sec-panel-close")?.addEventListener("click", close);

  document.getElementById("sec-arm")?.addEventListener("click", async () => {
    const r = await api("/security/home/arm", "POST", { mode: "away" });
    msg(r?.spoken || "Aktiviert.");
  });
  document.getElementById("sec-disarm")?.addEventListener("click", async () => {
    const r = await api("/security/home/disarm", "POST");
    msg(r?.spoken || "Deaktiviert.");
  });
  document.getElementById("sec-status")?.addEventListener("click", async () => {
    const s = await api("/security/home/status");
    msg(s ? (s.armed ? `Aktiv (${s.mode})` : "Deaktiviert") : "—");
  });
  document.getElementById("sec-door")?.addEventListener("click", async () => {
    msg("Analysiere Türkamera…");
    const r = await api("/security/camera/snapshot");
    msg(r?.description || "Kamera nicht verfügbar.");
  });
  document.getElementById("sec-net")?.addEventListener("click", async () => {
    msg("Scanne Netzwerk…");
    const r = await api("/security/digital/network");
    if (r) msg(`${r.total} Geräte, ${r.unknown_count} unbekannt.`);
  });
  document.getElementById("sec-sos")?.addEventListener("click", async () => {
    if (!confirm("SOS-Notfall auslösen?")) return;
    await api("/security/emergency/sos", "POST");
    msg("🚨 SOS aktiviert! Notruf 112, Polizei 110.");
  });
}
