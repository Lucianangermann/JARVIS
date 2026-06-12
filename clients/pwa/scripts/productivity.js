// JARVIS PWA — Productivity panel
// Loads score, goals, journal summary, and top-3 tasks from REST.
// Sends voice commands via WebSocket for all action buttons.

import * as cfg from "./config.js";
import * as ws  from "./websocket.js";

const panel        = document.getElementById("productivity-panel");
const scoreEl      = document.getElementById("prod-score");
const detailEl     = document.getElementById("prod-score-detail");
const moodEl       = document.getElementById("prod-mood");
const goalsEl      = document.getElementById("prod-goals");
const journalEl    = document.getElementById("prod-journal");
const tasksEl      = document.getElementById("prod-tasks");
const statusEl     = document.getElementById("prod-status");
const closeBtn     = document.getElementById("prod-close");
const pomodoroBtn  = document.getElementById("prod-pomodoro");
const mailsBtn     = document.getElementById("prod-mails");
const lernplanBtn  = document.getElementById("prod-lernplan");
const goalBtn      = document.getElementById("prod-goals-btn");
const journalBtn   = document.getElementById("prod-journal-btn");
const stimmungBtn  = document.getElementById("prod-stimmung");
const moodLogger   = document.getElementById("prod-mood-logger");
const moodGrid     = document.getElementById("prod-mood-grid");
const openBtn      = document.getElementById("act-productivity");

// ── Panel open / close ───────────────────────────────────────────────────

function openPanel() {
  panel.classList.add("open");
  panel.setAttribute("aria-hidden", "false");
  loadData();
}

function closePanel() {
  panel.classList.remove("open");
  panel.setAttribute("aria-hidden", "true");
  moodLogger.style.display = "none";
}

openBtn.addEventListener("click", openPanel);
closeBtn.addEventListener("click", closePanel);

// ── Helpers ──────────────────────────────────────────────────────────────

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

function setStatus(msg, isError = false) {
  statusEl.textContent = msg;
  statusEl.style.color = isError ? "#ff4d4d" : "";
}

async function nlCommand(cmd, closePanelAfter = false) {
  try {
    await ws.send(cmd);
    if (closePanelAfter) closePanel();
  } catch {
    setStatus("Verbindungsfehler.", true);
  }
}

// ── Data loading ─────────────────────────────────────────────────────────

async function loadData() {
  // Reset loading state
  scoreEl.textContent  = "…";
  detailEl.textContent = "";
  moodEl.textContent   = "";
  goalsEl.innerHTML    = '<span class="sh-loading">Lade...</span>';
  journalEl.innerHTML  = '<span class="sh-loading">Lade...</span>';
  tasksEl.innerHTML    = '<span class="sh-loading">Lade...</span>';
  statusEl.textContent = "";

  // Parallel fetch of all data sources
  const [score, goals, journal, mood, top3] = await Promise.all([
    apiGet("/productivity/analytics/today"),
    apiGet("/productivity/goals"),
    apiGet("/productivity/journal/today"),
    apiGet("/productivity/mood/today"),
    apiGet("/productivity/tasks/top3"),
  ]);

  // ── Score ─────────────────────────────────────────────────────────────
  if (score) {
    scoreEl.textContent  = `${score.score} / 10`;
    detailEl.textContent =
      `${score.tasks_done} von ${score.tasks_planned} Tasks · ` +
      `${Math.round(score.focus_minutes)} min Fokus`;
  } else {
    scoreEl.textContent  = "–";
    detailEl.textContent = "Nicht verfügbar";
  }

  // ── Mood ──────────────────────────────────────────────────────────────
  if (mood && mood.score != null) {
    const emoji = mood.score >= 8 ? "😄" : mood.score >= 6 ? "🙂" : mood.score >= 4 ? "😐" : "😔";
    moodEl.textContent = `${emoji} Stimmung: ${mood.score}/10`;
    if (mood.note) moodEl.title = mood.note;
  } else {
    moodEl.textContent = "Stimmung noch nicht geloggt";
  }

  // ── Goals ─────────────────────────────────────────────────────────────
  if (goals && goals.goals && goals.goals.length > 0) {
    goalsEl.innerHTML = "";
    goals.goals.forEach(g => {
      const row = document.createElement("div");
      row.className = "sh-device-row";
      row.style.cssText = "padding:5px 0;gap:8px;align-items:center;flex-wrap:wrap;";

      const title = document.createElement("span");
      title.style.flex = "1";
      title.style.fontWeight = "500";
      title.textContent = g.title;

      const pct = document.createElement("span");
      pct.style.cssText = "color:#00d4ff;font-size:0.85em;white-space:nowrap;";
      pct.textContent = `${g.progress_pct}%`;

      // Progress bar
      const barWrap = document.createElement("div");
      barWrap.style.cssText = "width:100%;height:4px;background:rgba(255,255,255,0.15);border-radius:2px;margin-top:2px;";
      const bar = document.createElement("div");
      bar.style.cssText = `height:4px;background:#00d4ff;border-radius:2px;width:${g.progress_pct}%;transition:width 0.4s;`;
      barWrap.appendChild(bar);

      const deadline = document.createElement("span");
      deadline.style.cssText = "font-size:0.72em;opacity:0.6;white-space:nowrap;";
      if (g.days_left != null) {
        if (g.days_left < 0) {
          deadline.style.color = "#ff4d4d";
          deadline.textContent = `${-g.days_left}d überfällig`;
        } else if (g.days_left === 0) {
          deadline.style.color = "#ffaa00";
          deadline.textContent = "heute!";
        } else {
          deadline.textContent = `noch ${g.days_left}d`;
        }
      }

      const top = document.createElement("div");
      top.style.cssText = "display:flex;width:100%;align-items:center;gap:8px;";
      top.append(title, pct);
      if (g.days_left != null) top.appendChild(deadline);
      row.append(top, barWrap);
      goalsEl.appendChild(row);
    });
  } else if (goals && goals.goals) {
    goalsEl.textContent = "Keine aktiven Ziele.";
  } else {
    goalsEl.textContent = "Nicht verfügbar.";
  }

  // ── Journal summary ───────────────────────────────────────────────────
  if (journal && !journal.error) {
    const lines = [];
    if (journal.tasks_done != null)
      lines.push(`✓ ${journal.tasks_done} Tasks erledigt`);
    if (journal.focus_minutes != null && journal.focus_minutes > 0)
      lines.push(`⏱ ${Math.round(journal.focus_minutes)} min Fokus`);
    if (journal.mood_score != null)
      lines.push(`💙 Mood ${journal.mood_score}/10`);
    journalEl.textContent = lines.length > 0 ? lines.join("  ·  ") : "Noch kein Eintrag für heute.";
  } else {
    journalEl.textContent = "Noch kein Eintrag für heute.";
  }

  // ── Top 3 tasks ───────────────────────────────────────────────────────
  if (top3 && top3.tasks && top3.tasks.length > 0) {
    tasksEl.innerHTML = "";
    top3.tasks.forEach((task, i) => {
      const row = document.createElement("div");
      row.className = "sh-device-row";
      row.style.cssText = "padding:4px 0;gap:8px;align-items:center;";
      const num = document.createElement("span");
      num.style.cssText = "color:#00d4ff;min-width:18px;font-weight:600;";
      num.textContent = `${i + 1}.`;
      const title = document.createElement("span");
      title.style.flex = "1";
      title.textContent = task.title;
      if (task.due_date) {
        const due = document.createElement("span");
        due.style.cssText = "font-size:0.75em;opacity:0.6;";
        due.textContent = task.due_date;
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

// ── Mood quick-logger ────────────────────────────────────────────────────

function buildMoodGrid() {
  moodGrid.innerHTML = "";
  for (let i = 1; i <= 10; i++) {
    const btn = document.createElement("button");
    btn.className = "sh-scene-btn";
    btn.style.cssText = "font-size:1.1em;padding:8px 0;";
    btn.textContent = String(i);
    btn.addEventListener("click", async () => {
      moodLogger.style.display = "none";
      setStatus(`Stimmung ${i}/10 wird geloggt…`);
      await nlCommand(`meine stimmung heute ist ${i}`);
      setStatus(`Stimmung ${i}/10 geloggt.`);
      // Reload to reflect new mood score.
      setTimeout(loadData, 1000);
    });
    moodGrid.appendChild(btn);
  }
}

// ── Action buttons ───────────────────────────────────────────────────────

pomodoroBtn.addEventListener("click", async () => {
  setStatus("Starte Pomodoro…");
  await nlCommand("starte pomodoro");
  setStatus("Pomodoro gestartet.");
});

mailsBtn.addEventListener("click", async () => {
  setStatus("Lade Mails…");
  await nlCommand("mails zusammenfassen", true);
});

lernplanBtn.addEventListener("click", async () => {
  setStatus("Erstelle Lernplan…");
  await nlCommand("erstelle einen lernplan für heute", true);
});

goalBtn.addEventListener("click", async () => {
  setStatus("Lade Ziele…");
  await nlCommand("zeig meine aktuellen ziele", true);
});

journalBtn.addEventListener("click", async () => {
  setStatus("Lade Journal…");
  await nlCommand("zeig das journal für heute", true);
});

stimmungBtn.addEventListener("click", () => {
  const visible = moodLogger.style.display !== "none";
  if (visible) {
    moodLogger.style.display = "none";
  } else {
    buildMoodGrid();
    moodLogger.style.display = "block";
  }
});
