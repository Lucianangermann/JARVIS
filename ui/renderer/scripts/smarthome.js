// JARVIS Electron HUD — Smart Home mini-panel
// Wires the ⌂ button to a compact overlay for quick light control.

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
      headers: {
        "Authorization": `Bearer ${_token}`,
        "Content-Type":  "application/json",
      },
    };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(_baseUrl + path, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  } catch (e) {
    console.warn("[smarthome]", e);
    return null;
  }
}

async function control(action, extra = {}) {
  return api("/smarthome/control", "POST", { action, ...extra });
}

async function runScene(name) {
  return api("/smarthome/scenes/run", "POST", { name });
}

export function init() {
  const panel    = document.getElementById("sh-panel");
  const homeBtn  = document.querySelector('[data-action="home"]');
  if (!panel || !homeBtn) return;

  function open()  { panel.removeAttribute("hidden"); }
  function close() { panel.setAttribute("hidden", ""); }

  homeBtn.addEventListener("click", () => {
    panel.hasAttribute("hidden") ? open() : close();
  });

  document.getElementById("sh-panel-close")?.addEventListener("click", close);

  document.getElementById("sh-all-on")?.addEventListener("click", async () => {
    await control("command", { command: "alle lichter an" });
  });
  document.getElementById("sh-all-off")?.addEventListener("click", async () => {
    await control("command", { command: "alle lichter aus" });
  });

  // Brightness quick buttons
  document.querySelectorAll(".sh-bright").forEach(btn => {
    btn.addEventListener("click", async () => {
      const level = parseInt(btn.dataset.level, 10);
      await control("command", { command: `alle lichter ${level}%` });
    });
  });

  // Color buttons (broadcast to all)
  document.querySelectorAll(".sh-color").forEach(btn => {
    btn.addEventListener("click", async () => {
      const color = btn.dataset.color;
      await control("command", { command: `alle lichter ${color}` });
    });
  });

  // Scene buttons
  document.querySelectorAll(".sh-scene-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      await runScene(btn.dataset.scene);
    });
  });
}
