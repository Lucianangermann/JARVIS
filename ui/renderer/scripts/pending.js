// ============================================================
// JARVIS overlay — pending action cards
//
// Renders confirmation cards for Tier 2 / 3 / 4 actions the brain
// asked the server to run. The list comes from permissions.js
// (snapshot.pending[]), refreshed every 5 s.
//
// Stable cards: we reconcile by pending.id instead of clear-and-
// rebuild, so a Tier-4 password the user is mid-way through typing
// doesn't get blown away by the next poll.
//
// API for the server:
//   POST /confirm          { id, approve: bool }            T1-T3
//   POST /tier4-confirm    { id, approve: bool, password }  T4 only
// Both go through permissions.confirmAction().
// ============================================================

import * as perms from "./permissions.js";

const stack = document.getElementById("pending-stack");

export function init() {
  if (!stack) return;
  perms.onUpdate(render);
}

function render(snap) {
  const list = (snap && Array.isArray(snap.pending)) ? snap.pending : [];
  const wanted = new Set(list.map((p) => String(p.id)));

  // Remove cards whose pending entries are gone (approved / cancelled
  // / expired). Touch only those — leave the rest alone so any in-
  // flight password input keeps its focus + value.
  for (const card of [...stack.children]) {
    if (!wanted.has(card.dataset.pid)) card.remove();
  }

  // Insert cards for newly-pending IDs.
  const have = new Set([...stack.children].map((c) => c.dataset.pid));
  for (const p of list) {
    const id = String(p.id);
    if (have.has(id)) continue;
    stack.appendChild(buildCard(p));
  }
}

function buildCard(p) {
  const tier = Number(p.tier) || 1;
  const requiresPassword = !!p.requires_password;

  const card = document.createElement("div");
  card.className = `pending-card tier-${tier}`;
  card.dataset.pid = String(p.id);

  // Header: T-badge + action name
  const head = document.createElement("div");
  head.className = "pc-head";
  const tierBadge = document.createElement("span");
  tierBadge.className = "pc-tier";
  tierBadge.textContent = `T${tier}`;
  const actionLbl = document.createElement("span");
  actionLbl.textContent = p.action || "PENDING ACTION";
  head.append(tierBadge, actionLbl);

  // Summary line — the human-readable description from the brain.
  const sum = document.createElement("div");
  sum.className = "pc-summary";
  sum.textContent = p.summary || "(no description)";

  card.append(head, sum);

  // Tier 4: password field above the approve/cancel row.
  let pwInput = null;
  if (requiresPassword) {
    const pwRow = document.createElement("div");
    pwRow.className = "pc-row";
    pwInput = document.createElement("input");
    pwInput.type = "password";
    pwInput.placeholder = "Tier-4 Passwort";
    pwInput.autocomplete = "off";
    pwInput.spellcheck = false;
    pwRow.appendChild(pwInput);
    card.appendChild(pwRow);
  }

  // Approve / Cancel buttons.
  const btnRow = document.createElement("div");
  btnRow.className = "pc-row";
  const approve = document.createElement("button");
  approve.className = "pc-approve";
  approve.type = "button";
  approve.textContent = "✓ APPROVE";
  const cancel = document.createElement("button");
  cancel.className = "pc-cancel";
  cancel.type = "button";
  cancel.textContent = "✕ CANCEL";
  btnRow.append(approve, cancel);
  card.appendChild(btnRow);

  // --- handlers --- //
  // While a request is in flight, disable both buttons (and the
  // password field) so the user can't double-submit. On error we
  // re-enable and show the message inline.
  const setBusy = (busy) => {
    approve.disabled = busy;
    cancel.disabled = busy;
    if (pwInput) pwInput.disabled = busy;
  };

  const submit = async (approveDecision) => {
    setBusy(true);
    try {
      await perms.confirmAction(p.id, {
        approve: approveDecision,
        password: pwInput?.value ?? null,
        tier4: requiresPassword,
      });
      // Card removal happens on the next poll (immediate, since
      // confirmAction() re-polls); no need to remove it manually.
    } catch (err) {
      console.warn("[pending] confirm failed:", err);
      flashError(card, err.message || String(err));
      setBusy(false);
    }
  };

  approve.addEventListener("click", () => submit(true));
  cancel.addEventListener("click", () => submit(false));

  // Enter inside the password field is Approve. Escape is Cancel.
  if (pwInput) {
    pwInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter")  { ev.preventDefault(); submit(true); }
      else if (ev.key === "Escape") { ev.preventDefault(); submit(false); }
    });
    // Autofocus the password input — it's the only thing the user
    // can do next, and a new tier-4 card always preempts the chat.
    queueMicrotask(() => pwInput.focus());
  }

  return card;
}

function flashError(card, msg) {
  // Append (or replace) an inline error line. Keeps the card around
  // so the user can retry without re-entering the password.
  let err = card.querySelector(".pc-error");
  if (!err) {
    err = document.createElement("div");
    err.className = "pc-error";
    err.style.cssText =
      "font-size:10px;color:var(--jarvis-danger);" +
      "letter-spacing:0.1em;text-transform:none;";
    card.appendChild(err);
  }
  err.textContent = `[ERROR] ${msg}`;
}
