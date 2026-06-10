// JARVIS Electron HUD — Communication mini-panel.
// Wires the 💬 button to a compact overlay: unread counts, missed calls,
// DND toggle, quick translate. Same config/api pattern as smarthome.js.

let _baseUrl = null;
let _token   = null;
let _dnd     = false;

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
    console.warn("[comm]", e);
    return null;
  }
}

function msg(text) {
  const el = document.getElementById("comm-msg");
  if (el) { el.textContent = text; setTimeout(() => { if (el.textContent === text) el.textContent = ""; }, 6000); }
}

async function loadUnread() {
  const d = await api("/communication/messages/unread");
  const el = document.getElementById("comm-unread");
  if (d && el) {
    el.textContent =
      `iMessage ${(d.imessage || []).length} · Telegram ${(d.telegram || []).length}`;
  }
}

export function init() {
  const panel = document.getElementById("comm-panel-hud");
  const btn   = document.querySelector('[data-action="comm"]');
  if (!panel || !btn) return;

  const open  = () => { panel.removeAttribute("hidden"); loadUnread(); };
  const close = () => panel.setAttribute("hidden", "");

  btn.addEventListener("click", () => panel.hasAttribute("hidden") ? open() : close());
  document.getElementById("comm-panel-close")?.addEventListener("click", close);

  document.getElementById("comm-missed")?.addEventListener("click", async () => {
    const r = await api("/communication/calls/missed");
    msg(r?.spoken || "—");
  });
  document.getElementById("comm-dnd")?.addEventListener("click", async () => {
    _dnd = !_dnd;
    const r = await api("/communication/notifications/dnd", "POST", { enabled: _dnd });
    msg(r?.dnd ? "Nicht stören AN." : "Nicht stören AUS.");
  });
  document.querySelectorAll(".comm-tr-go").forEach(b =>
    b.addEventListener("click", async () => {
      const text = (document.getElementById("comm-tr")?.value || "").trim();
      if (!text) { msg("Text eingeben."); return; }
      msg("Übersetze…");
      const r = await api("/communication/translate", "POST",
                          { text, target_lang: b.dataset.to, source_lang: "auto" });
      msg(r?.translation || "—");
    }));
}
