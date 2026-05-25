// JARVIS PWA — Productivity panel
// Fetches score + top-3 tasks from the server and wires the panel UI.
// Sends voice commands through the WebSocket for Pomodoro + mail actions.

import * as cfg from "./config.js";
import * as ws  from "./websocket.js";

const panel      = document.getElementById("productivity-panel");
const scoreEl    = document.getElementById("prod-score");
const detailEl   = document.getElementById("prod-score-detail");
const tasksEl    = document.getElementById("prod-tasks");
const statusEl   = document.getElementById("prod-status");
const closeBtn   = document.getElementById("prod-close");
const pomodoroBtn = document.getElementById("prod-pomodoro");
const mailsBtn   = document.getElementById("prod-mails");
const openBtn    = document.getElementById("act-productivity");

// ── Panel open / close ───────────────────────────────────────────────────

function openPanel() {
  panel.classList.add("open");
  panel.setAttribute("aria-hidden", "false");
  loadData();
}

function closePanel() {
  panel.classList.remove("open");
  panel.setAttribute("aria-hidden", "true");
}

openBtn.addEventListener("click", openPanel);
closeBtn.addEventListener("click", closePanel);

// ── Data loading ─────────────────────────────────────────────────────────

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

async function loadData() {
  scoreEl.textContent   = "…";
  detailEl.textContent  = "";
  tasksEl.innerHTML     = '<span class="sh-loading">Lade...</span>';
  statusEl.textContent  = "";

  // Score
  const score = await apiGet("/productivity/analytics/today");
  if (score) {
    scoreEl.textContent  = `${score.score} / 10`;
    detailEl.textContent =
      `${score.tasks_done} von ${score.tasks_planned} Tasks · ` +
      `${Math.round(score.focus_minutes)} min Fokus`;
  } else {
    scoreEl.textContent  = "–";
    detailEl.textContent = "Nicht verfügbar";
  }

  // Top 3 tasks
  const top3 = await apiGet("/productivity/tasks/top3");
  if (top3 && top3.tasks && top3.tasks.length > 0) {
    tasksEl.innerHTML = "";
    top3.tasks.forEach((task, i) => {
      const row = document.createElement("div");
      row.className = "sh-device-row";
      row.style.cssText = "padding:4px 0;gap:8px;align-items:center;";
      const num = document.createElement("span");
      num.style.cssText = "color:#00d4ff;min-width:18px;font-weight:600;";
      num.textContent   = `${i + 1}.`;
      const title = document.createElement("span");
      title.style.flex  = "1";
      title.textContent = task.title;
      if (task.due_date) {
        const due = document.createElement("span");
        due.style.cssText = "font-size:0.75em;opacity:0.6;";
        due.textContent   = task.due_date;
        row.append(num, title, due);
      } else {
        row.append(num, title);
      }
      tasksEl.appendChild(row);
    });
  } else if (top3) {
    tasksEl.textContent = "Keine offenen Tasks.";
  } else {
    tasksEl.textContent = "Nicht verfügbar.";
  }
}

// ── Actions ──────────────────────────────────────────────────────────────

pomodoroBtn.addEventListener("click", async () => {
  statusEl.textContent = "Starte Pomodoro…";
  try {
    await ws.send("starte pomodoro");
    statusEl.textContent = "Pomodoro gestartet.";
  } catch {
    statusEl.textContent = "Fehler beim Starten.";
  }
});

mailsBtn.addEventListener("click", async () => {
  statusEl.textContent = "Lade Mails…";
  try {
    await ws.send("mails zusammenfassen");
    closePanel();
  } catch {
    statusEl.textContent = "Fehler beim Laden der Mails.";
  }
});
