# Todo

## Depth Improvements (active)

Four directions: agentic planning, LLM resilience, self-monitoring, reproducibility+e2e.

### Batch 1 — LLM resilience  ✅
- [x] Anthropic client: max_retries (SDK auto-retries 429/500/connection) + timeout (config CLAUDE_MAX_RETRIES/CLAUDE_TIMEOUT_S).
- [x] Cost guard: rolling-hour Claude-call cap (MAX_CLAUDE_CALLS_PER_HOUR) checked in reply() → refuses without an API call when a runaway loop would blow the budget.
- [x] Model escalation: `_pick_model` routes "denk gründlich nach / step by step / ausführlich" turns to MODEL_HARD (Sonnet); threaded through tool loop → stream.
- [x] Friendlier German failure message after retries exhausted. tests/test_brain.py +3 (10 total).

### Batch 2 — agentic multi-step planning
- [ ] Orchestrate compound requests ("plane meinen Morgen", "mach mich startklar")

### Batch 3 — self-monitoring + owner-alerting
- [ ] Watchdog auto-restart of failed subsystems; Telegram self-alert; /health; metrics

### Batch 4 — reproducibility + e2e
- [ ] Lockfile; startup config validation; e2e pipeline tests

---

## System Improvements (active)

Four directions chosen. Executing in batches, commit per batch.

### Batch 1 — CI + logging  ✅
- [x] `.github/workflows/test.yml` — pytest on push/PR (ubuntu, light deps, dummy env, CI-skips network/heavy tests). Verified locally: 65 passed, 2 skipped.
- [x] `server/common/logging_setup.py` — rotating `logs/jarvis.log` + tee of all print() output (no 432-call sweep). Wired into `run()` (not lifespan → pytest untouched). Verified prints/logging/stderr all captured.

### Batch 2 — wire dormant features  ✅
- [x] Proactive triggers wired to real sources: forgotten_task → task_manager.get_overdue, important_email → mail_tool unread count (package_delivery left a stub — no real tracking source).
- [x] Per-IP rate-limit + IP-block (anomaly/digital) wired as `security_rate_gate` dependency on /chat + /audio.
- [x] Delegated access live: `authorize_chat` accepts owner OR guest temp token; guest commands restricted to allowed level + audited. Verified: guest lights allowed, guest email refused, owner unaffected, bad token 401, rate-gate 429.

### Batch 3 — DB encryption at rest  ✅
- [x] `server/common/crypto.py` FieldCipher (Fernet, opt-in via JARVIS_DB_KEY, `enc:` prefix for graceful legacy-plaintext migration).
- [x] communication.db: message content/translated_content encrypted at rest; finance.db: expense merchant/description encrypted (queryable columns stay plaintext — full-file needs SQLCipher).
- [x] config + .env.example + requirements (cryptography); tests/test_crypto.py (5); CI runs it. Verified: raw row ciphertext, reads decrypt, off-without-key passthrough, wrong-key graceful.

### Batch 4 — test_brain.py + lifespan registry  ✅
- [x] tests/test_brain.py (7): dispatch table covers every registered tool, unknown→None, table cached, security/communication short-circuit routing, handler-error swallowed, empty-input guard. In CI.
- [x] `_wire_subsystem` helper unifies the build/start/attach/log boilerplate for the independent layers (productivity/entertainment/finance). Bridged subsystems (security/communication, with cross-bridges) intentionally keep inline wiring — a full declarative registry doesn't fit their interdependencies. Boot verified.

---

## System Review Fixes (done)

Four parallel review agents (correctness / architecture / security / resources)
→ fixed in batches; 72 tests pass, full boot verified.

**Batch A — bugs/perf:** Telegram boot bug (`asyncio.run` in running loop → silent dead inbound) → sync `connect_blocking`; Telegram chat_id-hijack guard (learn only in setup); Telegram long-poll (25s) vs 3s short-poll; blocking-in-async → `to_thread` (`/finance/*`, digital_security subprocess/HTTP); embedding/ChromaDB warmup off the boot path (was ~10s stall); Cocoa pump gated behind `_VOICE_OK`; Productivity/Entertainment `stop()` + lifespan finally + briefing conn-leak fix.

**Batch B — retention/confirm/mic:** message-content 7-day prune throttled-daily (not just boot); camera snapshot prune hourly while running; confirm-before-send hardened with server `pending_id` + separate `/confirm` (closed `confirm=true` bypass) for messaging+email; `server/mic_lock.py` coordinates meeting/voice-auth, meeting declines under `JARVIS_LOCAL_VOICE=1`.

**Batch C — voice-auth wired:** `process_request` now runs on the `/audio` path (real speaker-verify gate, no-op when off); error path logs durably + fails CLOSED for `critical`.

**Batch D — dedup:** intelligence briefings/proactive routed through NotificationCenter (DND/quiet-hours); shared `common/claude_json.py` replaced 4 identical fence-parsers.

**Deferred refactors — done (separate careful pass):**
- [x] Shared `common/sqlite_store.py` `ThreadSafeDB` base → security/communication/finance/knowledge DBs subclass it (removed ~4× boilerplate; public method names preserved via aliases: finance `execute`, flashcards `_write`/`_query`). 66 DB-layer tests pass.
- [x] Brain tool-dispatch table (`_tool_dispatch`) replaces the 14-branch elif chain — all 29 registered tools covered, verified routing. 144 tests pass.
- [x] `osa()` promoted to canonical `common/applescript.py`; `communication/applescript.py` is a back-compat shim. Full migration of the 12 legacy `osascript -e` tool callers NOT done blind — needs a Mac to verify each rewritten AppleScript (they already escape quotes; marginal gain vs real risk of breaking mail/notes/reminders headless). Recommend doing it interactively at the Mac.

---

## Finance Layer (active)

New `server/finance/` package. Clean slate (no existing finance code). Reuses:
Claude client (categorization), NotificationCenter (price alerts), mail_tool
(receipt/subscription scan), httpx, SQLite pattern. Decisions baked in:
market data = Yahoo Finance chart API (free, no key) + CoinGecko crypto
fallback; storage = data/finance.db; default currency EUR.

### Phase 1 — Expenses & Budgets  ✅
- [x] `finance/finance_db.py` — SQLite (expenses, budgets, subscriptions, watchlist), thread-safe
- [x] `finance/expense_tracker.py` — keyword-first categorization (Claude fallback), monthly budgets + warnings, summaries
- [x] Verified: REWE→lebensmittel/Netflix→abos/Shell→transport, budget-exceeded warning, monthly summary

### Phase 2 — Market watchlist + alerts  ✅
- [x] `finance/market.py` — Yahoo Finance prices (stock/crypto/etf) + CoinGecko fallback, watchlist, portfolio value
- [x] price alerts (background poll → NotificationCenter when target crossed, rising-edge then disarm)
- [x] Verified LIVE: AAPL/SAP.DE/BTC-EUR prices, portfolio per currency, alert fires once

### Phase 3 — Subscription / receipt detection  ✅
- [x] `finance/subscription_detector.py` — Claude extracts recurring charges from mail text; scan_mail wrapper (best-effort); upsert + spoken summary

### Phase 4 — Integration  ✅
- [x] `finance_manager.py` coordinator (start market poll); brain tool `finance` (own tools.py + _exec_finance + lazy _get_finance)
- [x] main.py lifespan wiring (price alerts → comm NotificationCenter) + `/finance/*` routes (expenses/summary/budgets/watchlist/portfolio/price)
- [x] morning briefing over-budget line (no poll thread); tests/test_finance.py (8)
- [x] Bug fixed: keyword categorization used substring → "buch" matched "Buchung"; switched to word-boundary regex
- [x] Verified end-to-end via TestClient (tool + routes + live prices); 72 tests pass, no regressions

---

## Second Brain / Knowledge (active)

Builds heavily on the existing memory layer (ChromaDB `knowledge` collection,
`save_knowledge`/`search_knowledge`, local free embeddings). Decisions baked in:
flashcards/SRS → new `server/knowledge/` package + `data/knowledge.db` SQLite
(SM-2); remember/recall reuses `memory.long_term`.

### Gap analysis (HAVE vs NEW)
- HAVE: `long_term.save_knowledge/search_knowledge`, embeddings (local, free), memory lifecycle, OCR+document_scanner, notes_tool.
- BUG: brain `add_knowledge_note` calls non-existent `store_knowledge()` → try/except swallows it → "remember" silently saves NOTHING. Fix first.
- NEW: recall tool, knowledge→system-prompt injection, list/by-category, flashcards+SRS, document→knowledge, daily review, /memory/knowledge routes.

### Phase 1 — Remember & Recall (make it actually work)  ✅
- [x] Fixed brain bug: `store_knowledge` → `save_knowledge` (was silently saving NOTHING via swallowed AttributeError); now honest on failure
- [x] `recall_knowledge` brain tool ("was weiß ich über X") + tool def + dispatch
- [x] Inject relevant knowledge into system prompt (`context_builder._knowledge_block`, tightened distance <0.55 so it's empty for unrelated queries — calibrated against MiniLM)
- [x] long_term: `list_knowledge` (newest-first, by-category)
- [x] `/memory/knowledge/search` + `/memory/knowledge/list` routes (auth)
- [x] Verified: add_knowledge_note now persists (count=1), recall works, list/search routes 200, prompt-injection relevant-only. tests/test_knowledge.py (5). 128 tests pass.

### Phase 2 — Flashcards + Spaced Repetition  ✅
- [x] `server/knowledge/flashcards.py` (SM-2 SRS, data/knowledge.db, thread-safe), add/review/due/schedule
- [x] auto-generate cards from text via Claude (`generate_from_text`)
- [x] brain tool `flashcards` (add/due/next/reveal/grade/generate/stats) + lazy `_get_flashcards`
- [x] Verified: SM-2 progression 1→6→15.6d, fail-reset, feedback→quality, full quiz loop via brain; tests/test_flashcards.py (8)

### Phase 3 — Document → Knowledge  (composable; dedicated flow deferred)
- [x] DECISION: the pieces already compose — vision OCR (`vision.ocr.extract_text`) → `add_knowledge_note`
      (now fixed) → optional `flashcards generate`. A dedicated vision-coupled ingest module is fragile +
      hard to test headless, so deferred until needed rather than built speculatively.

### Phase 4 — Integration  ✅
- [x] Morning briefing hook: "N Karteikarten stehen zur Wiederholung an" (lightweight WAL read, no thread)
- [x] Routes: `/knowledge/flashcards/due`, POST `/knowledge/flashcards`, POST `/knowledge/flashcards/{id}/review`
- [x] Verified end-to-end via TestClient (add→due→review→0, 404, 401); 64 tests pass, no regressions

---

## Synergy Pack (active)

Quick, reuse-heavy wins connecting existing layers.

### 1. Emergency → Telegram push  ✅
- [x] Bridge security.emergency notify_handler → communication.telegram (push to owner's iPhone)
- [x] Keeps existing log + event-bus publish; adds Telegram as an extra channel
- [x] Wired in main.py after BOTH security + communication built (wraps emergency._notify)
- [x] Fixed: emergency now always invokes notify (owner push fires even with NO contacts configured)
- [x] Verified via TestClient: SOS → Telegram sendMessage with 🚨 to owner chat_id; no-ops cleanly without token/chat_id

### 2. Meeting assistant  ✅
- [x] `productivity/meeting_assistant.py` — record (best-effort sounddevice chunks + STT) → Claude summary + action items + decisions → tasks (TaskManager) + Apple Note
- [x] Reuses stt, brain.client, task_manager, notes_tool
- [x] Exposed as Claude tool `meeting_control` (start/stop/status/summarize) in productivity tools + brain executor
- [x] ProductivityManager wires `self.meeting` with brain client (main.py passes client)
- [x] Fixed: stop_recording short-join (no 15s hang on "beende das Meeting")
- [x] tests/test_meeting_assistant.py (6); verified end-to-end with real Claude (transcript → 2 action-item tasks); 51 tests pass, no regressions
- [ ] Known limit: mic contention if voice_loop is recording simultaneously (best-effort; summarize path works on any transcript)

### 3. Electron HUD Phase 1  ✅ (already built) + panels added
- [x] FINDING: HUD Phase 1 static visuals already complete — emblem SVG (rings/ticks/dots/
      bracket-arcs/wordmark), corner brackets, IDLE orb + particle aura, state-driven canvas
      visualizer (setVisualizerState + per-state amplitude model), status bar, chat pane. No rebuild needed.
- [x] Real gap filled instead: ported Security 🔒 + Communication 💬 panels into the Electron HUD
      (were only in the PWA). New hex-buttons + compact panels (reuse .sh-panel CSS) + security.js/comm.js
      (same window.jarvis.getConfig REST pattern as smarthome.js) + app.js wiring. JS syntax-checked.

---

## JARVIS Communication Layer — PLAN (not started)

Goal: a communication hub under `server/communication/` (messaging, calls, email,
notifications, social, translation, automation). Same build philosophy as the
security layer: phased, best-effort (never crash JARVIS), confirm-before-send,
verified after each phase.

### Gap analysis — what we already HAVE vs need to BUILD
| Subsystem | Status | Reuse |
|---|---|---|
| Email | PARTIAL | `tools/mail_tool.py`: `list_unread/read_message/send_message/get_unread_count` (multi-account loop, safe escaping). Extend: attachments, templates, account selection, analytics. |
| iMessage | NEW | AppleScript-template pattern from `mac_control/tier2_apps.py` (argv injection-safe). No send/read exists yet. |
| Telegram | NEW | Use Telegram **Bot HTTP API via `requests`** (already a dep) — NOT python-telegram-bot (avoids a second asyncio loop conflicting with FastAPI). sendMessage/getUpdates. |
| Notifications | HAVE (extend) | `events.publish()` bus, `intelligence/proactive.py` priority model (high/medium/low + meeting/sleep suppression), `mac_control` `display notification`. Add: DND/quiet-hours, persistent history, unified `NotificationCenter`. |
| Translation | HAVE (extend) | `vision/translator.py` = image OCR+translate. NEW = text↔text via the brain's Claude client (reuse `vision_manager.analyze_image` call pattern, text-only). |
| Calls/FaceTime | NEW | `open_app()` foundation only. Write FaceTime/Contacts AppleScript. |
| Social/Birthdays | PARTIAL | `entertainment/birthdays.py` (Contacts), `tools/news.py` (RSS). Reddit=RSS (free). Twitter/LinkedIn = no real free API → best-effort/stub. |
| Reminders/follow-ups | HAVE | `tools/reminders_tool.py` + `productivity/task_manager.py` for callback reminders + follow-up tracking. |
| Confirm-before-send | HAVE | `mac_control/confirmation.py` (stash/peek/consume, 30s TTL) — reuse for every message/email send. |
| Mass-notify hook | HAVE | `security/emergency.py` `NotifyHandler` + main.py `_security_notify` — swap in real iMessage/Telegram transport now that we build it. |

### Decisions LOCKED (user-confirmed 2026-06-10)
- **Telegram transport:** Bot HTTP API via `requests` (no python-telegram-bot, no second asyncio loop). ✓
- **WhatsApp:** DEFERRED — not built now; leave a clean adapter seam for later. ✓
- **Social scope:** Reddit-RSS + birthdays reuse + Claude post-drafts (never auto-post). Twitter/LinkedIn = stubs returning a clear "API key / not configured" message. ✓
- **Notification-center migration:** phased & low-risk — build center (DND/quiet-hours/history), route NEW comm + security/intelligence notifications through it; leave existing `tts.speak` calls in place for now. ✓
- ⇒ Spec Phase 5 (WhatsApp) dropped. Build P1→P4 then P6 integration.

### Build phases (mirrors spec order, trimmed to what's sane)
- [x] **P1 Foundation:** ✅ `communication.db` (5 tables + 7-day content prune), `notification_center.py` (priority routing + DND/quiet-hours/meeting suppression + batching + history), `translation/translator.py` (Claude text↔text + detect). Verified: retention prune, DND lets only critical through, DE→EN + French detect + auto-translate incoming.
- [x] **P2 Messaging:** ✅ `imessage.py` (AppleScript send + chat.db read, Full-Disk-Access-graceful), `telegram_bot.py` (REST send/poll/notify, chat_id auto-learn), `messaging_manager.py` (unified, confirm-before-send 30s TTL, broadcast rate-limit, Claude summarize/draft), `setup_telegram.py`, `applescript.py` (shared safe osa). Verified: chat.db parse + Apple-ts, **injection-safe (evil body → argv not script)**, Telegram mocked, confirm/broadcast/cancel/TTL.
- [x] **P3 Calls & Email:** ✅ `call_manager.py` (FaceTime/tel via Contacts AppleScript, missed calls from CallHistory+db, callback→reminders_tool, voicemail honest-unsupported), `email_templates.py` (built-ins + user JSON + safe fill), `email_analyzer.py` (Claude importance/summary + newsletter/unsub heuristics), `email_manager.py` (extends mail_tool: multi-account, templates, argv-safe attachments, confirm-before-send). Verified: template fill+persist, Claude importance/summary, attachment validation, make_call scheme + callback reminder, all argv-safe.
- [x] **P4 Automation & Social:** ✅ `comm_automation.py` (auto-reply rules w/ VIP exceptions + JSON persist, follow-up tracking, broadcast→messaging, status/OOO + auto-revert), `social_manager.py` (Reddit-RSS live, birthdays reuse, Claude drafts ≤char-limit, Twitter/LinkedIn honest stubs). Verified: VIP bypass, follow-up due/cleared, status+OOO persist, live Reddit, 280-char draft.
- [ ] **P5 (optional) WhatsApp:** only if user opts in.
- [x] **P6 Integration:** ✅ `communication_manager.py` (coordinator + NL routing + confirm-flow); brain `_communication` short-circuit (after security, before Claude) + `_run_communication_command`; main.py lifespan wiring (speak/ui/macos/meeting handlers, Telegram connect+poll) + security-alerts bridged through NotificationCenter; full `/communication/*` routes; PWA 💬 panel + communication.js + reused CSS; morning-briefing comm line; config block + .env.example + httpx pin; tests/test_communication.py (22). Verified end-to-end via TestClient: routes 200, `/chat` short-circuits "übersetze…"→"what time is it" + "neue nachrichten" without Claude, 401 w/o token. 117 tests pass, 246 collect, no regressions.

## Communication Review
**Delivered:** 18-module communication layer under `server/communication/` (messaging/calls/email/notifications/social/translation/automation + coordinator), wired into brain + main.py + PWA + intelligence. 22-test suite. WhatsApp deferred per decision.

**Key reuse (not rebuilt):** `mail_tool` (email base), `reminders_tool` (callbacks), `birthdays` (social), `events`/proactive priority model (notifications), Claude client (translation/summaries/drafts), `mac_control` AppleScript safety pattern, confirmation TTL pattern.

**Design:** never auto-send (confirm-before-send 30s TTL everywhere), message content pruned after 7 days, injection-safe AppleScript (argv not interpolation), Telegram via plain Bot HTTP API (no python-telegram-bot), honest stubs for Twitter/LinkedIn, all best-effort (never crash JARVIS). Security alerts now flow through NotificationCenter (gains DND/quiet-hours/telegram) while keeping their direct path.

**Needs real-world setup (documented in .env.example):** Telegram bot token+chat_id (run setup_telegram), Full Disk Access for iMessage/call-history reads, Twitter paid API for live mentions.

### Hard rules (from spec)
- NEVER auto-send a message — always preview + confirm (reuse confirmation.py).
- NEVER store message content > 7 days (prune job).
- Translation uses existing Claude client (no extra cost).
- Comm failures NEVER crash JARVIS (best-effort everywhere).
- Notification center initialized BEFORE other comm systems.

---

## JARVIS Security & Monitoring Layer (active)

Complete security/monitoring layer under `server/security/`. Summary after each file; user can interrupt.

**Constraints (hard rules):**
- Security failures must NEVER crash JARVIS — best-effort try/except everywhere.
- Smoke/Water/CO2 alerts + SOS are NEVER blocked by any filter or auth check.
- PIN stored as bcrypt hash, never plaintext. Voice profiles local-only. Snapshots auto-deleted after retention.
- Env: `.venv` Python 3.11, numpy<2.0, cv2 4.10. psutil 7.2.2 installed. resemblyzer TODO (Phase 2).

### Phase 1 — Foundation ✅
- [x] `security/__init__.py` — package exports
- [x] `security/db.py` — SQLite schema + connection helper (security.db, 5 tables)
- [x] `security/system_monitor.py` — psutil health + thresholds + background loop (works immediately)
- [x] `security/access_control.py` — token-based guest/family/temp access
- [x] Verify: real metrics returned, tables created (smoke test passed; fixed `arp -a` DNS hang → `-an`)

### Phase 2 — Authentication ✅
- [x] `security/voice_auth.py` — resemblyzer enroll/verify + bcrypt PIN fallback + guest mode
- [x] `security/anomaly_detector.py` — pattern learning + per-IP rate limiting
- [x] resemblyzer+torch installed (setuptools pinned <81 for webrtcvad/pkg_resources); config.py security block added
- [x] Verify: enroll→0.36 reject other voice, PIN bcrypt, level gating, guest mode, burst/rate-limit/baseline all pass

### Phase 3 — Monitoring ✅
- [x] `security/camera_monitor.py` — extends vision motion_detector + Claude Vision + zones/schedule
- [x] `security/home_security.py` — sensors via smarthome, arm/disarm, leaving checklist, smoke/water/CO2
- [x] `security/digital_security.py` — network scan, API usage, HIBP, tailscale, ports, auth-log
- [x] Verify: vision-JSON parse (+fences/non-json), alert levels, checklist, smoke/CO2 fire unconditionally,
      net scan (multicast filtered, first-sighting-only alerts), ports, tailscale, auth-log block at 5

### Phase 4 — Emergency ✅
- [x] `security/emergency.py` — SOS / fire / intrusion / medical + notifications (always-available, never auth-gated)
- [x] `security/security_manager.py` — central coordinator (start/process_request/process_command/briefing)
- [x] Verify: emergency routing, full request pipeline (rate-limit deny), all trigger phrases, briefing
- [x] Fixed: eager f-string on cpu_temp=None; camera auto-start now gated by CAMERA_ENABLED (force= override)

### Phase 5 — Integration ✅
- [x] Wire SecurityManager into main.py lifespan (+ speak/notify handlers) + brain._security + shutdown
- [x] Brain security short-circuit (emergency-first, pre-Claude) + record_api_call at stream call site
- [x] All `/security/*` API routes (voice/camera/home/digital/system/emergency/access) with auth
- [x] PWA security panel (🔒 nav button, arm/disarm, health bars, camera, netscan, SOS) + security.js + CSS
- [x] Intelligence morning-briefing hook (overnight security events, WAL concurrent read)
- [x] `.env.example` block, requirements.txt (psutil/bcrypt) + requirements-voice.txt (resemblyzer/setuptools<81)
- [x] tests/test_security.py — 23 tests, all pass; no regressions (224 collect, 72 mac_control pass)
- [x] End-to-end TestClient: lifespan wires security, routes 200, /chat "system status" short-circuits, 401 w/o token
- [x] Refinement: network scan fires ONE summary TTS alert, not one per device

## Review
**Delivered:** Complete 9-module security/monitoring layer (`server/security/`), wired into brain + main.py + PWA + intelligence, with a 23-test suite. All phases verified with real psutil/resemblyzer/SQLite + a full TestClient boot.

**Bugs found & fixed during build:**
1. `arp -a` hung 5s on reverse-DNS → `arp -an` (numeric).
2. setuptools 82 removed `pkg_resources` that webrtcvad needs → pinned `setuptools<81`.
3. Eager f-string `f"{cpu_temp:.0f}"` crashed monitor loop when temp=None (macOS) → safe format.
4. Camera auto-started on arm/SOS ignoring `CAMERA_ENABLED` → gated with `force=` override.
5. Network scan spoke one alert per unknown device → single summary alert.

**Design decisions:**
- Security failures never crash JARVIS (best-effort try/except everywhere; process_request fails OPEN for the single owner).
- Emergency/safety paths (SOS, smoke/water/CO2) bypass all auth/guest gating and are checked first.
- Voice auth: fail-open for owner when degraded (single-user trust model), real gate when enrolled+enabled.
- Camera off by default (privacy); resemblyzer optional (PIN fallback).

**Not wired (needs real-world config, documented in .env.example):**
- Emergency SMS/WhatsApp transport (notify_handler currently logs + pushes PWA event).
- Real door/window/smoke sensors (manual model + smarthome adapters; most adapters still stubs).
- HaveIBeenPwned email lookup (needs paid API key).

---

## macOS Full Control — v1 Plan

Per user spec (4 tiers, kill switch, action logger, full Tier 4 with password). Built in checkpoints — summary after each, user can interrupt.

**Design decisions held over from review:**
- Tier is intrinsic to each action function (lookup table), not chosen by Claude — anti-prompt-injection
- Kill switch reuses existing voice_loop stop-phrase machinery + adds API endpoint (no second always-on thread)
- AppleScript: pure template library with parameter substitution at fixed slots — never string interpolation of free text
- Path canonicalization (`Path(p).expanduser().resolve()`) before every sandbox check
- Terminal whitelist: arg validation per-command, not just name match
- Confirmation: 30s hard timeout → CANCELLED. One pending confirmation per session

### Checkpoint A — Scaffold + Infrastructure
- [ ] `server/mac_control/__init__.py` + skeleton
- [ ] `permission_manager.py` — tier lookup, unlock state, password check
- [ ] `action_logger.py` — three rotating logs (actions / rejected / confirmations)
- [ ] `confirmation.py` — per-session pending slot, 30s timeout, yes/ja & no/nein parser
- [ ] `kill_switch.py` — threading.Event + API to set/check/resume, hooked to voice stop-phrases

### Checkpoint B — Tier 1 + Tier 2
- [ ] `tier1_info.py` — time, date, battery, wifi, volume read, clipboard read, weather (open-meteo, no key)
- [ ] `tier2_apps.py` — AppleScript template lib: Music/Spotify transport, Safari open URL, volume/brightness, notifications, whitelisted open-app

### Checkpoint C — Tier 3 + Tier 4
- [ ] `tier3_files.py` — sandboxed read/list/create/rename/move, trash-only delete via Finder
- [ ] `tier4_system.py` — password gate, terminal whitelist (arg-validated), brew install/uninstall, screenshot → vision, opening System Prefs panes (read-only)

### Checkpoint D — Brain + API + Web UI
- [ ] Extend `brain.py`: register `mac_action` tool; dispatch via permission_manager
- [ ] Extend `main.py`: `/permissions`, `/confirm`, `/emergency-stop`, `/resume` routes; WS confirmation roundtrip
- [ ] Update `clients/web/index.html`: permission status row in header, pending confirmation prompt UI

### Checkpoint E — Setup + Tests + Docs
- [ ] `server/mac_control/setup_permissions.py` — TCC permission checker + guide
- [ ] `tests/test_mac_control.py` — kill switch, tier boundaries, sandbox escape attempts, confirmation timeout, log presence
- [ ] `README_MAC_CONTROL.md` — install steps, permission flow, kill switch usage, hard rules

### Checkpoint F — Commit
- [ ] requirements.txt updates
- [ ] git commit + push

## Absolute hard rules (enforced in code)
- AppleScript NEVER interpolates raw LLM/user text into the script body — only into named, escaped slots
- Path sandbox check uses `.resolve()` (symlink-aware) AND verifies ALLOWED prefix AND blocks BLOCKED prefixes
- `JARVIS_SUDO_PASSWORD` never appears in any log line (filter at logger level)
- Trash-only delete; no `os.remove` / `shutil.rmtree` reachable from any tier
- Tier 4 actions: password match required EACH time, no session unlock
- Kill switch state blocks all Tier 2+ actions until explicit resume

---

## JARVIS Electron Overlay — Phase 1 (static)

Goal: build the complete static overlay with all visuals + state
transitions working in `ui/`. No WebSocket, no mic, no sounds — those
come in Phase 2 once you've eyeballed the visuals.

### Architecture decision
- Visualizer is **state-driven** in Phase 1 (no real mic input). The
  server's voice_loop owns the only mic capture; the overlay is a
  status reflector + controls surface.
- WebSocket integration deferred to Phase 2 so we don't waste cycles
  on auth/retry plumbing before the visuals are confirmed-good.

### Checkpoints
- [ ] UI-A: Electron skeleton (package.json, main.js, preload.js, blank window)
- [ ] UI-B: Design system + animation library (CSS only)
- [ ] UI-C: Static HUD — rotating ring, hexagon core, corner brackets,
        grid, scanline, particles
- [ ] UI-D: State machine — IDLE orb / ACTIVE / SPEAKING / PROCESSING
        with click + keyboard switching for manual testing
- [ ] UI-E: Canvas visualizer — circular bars around hexagon, idle pulse,
        speaking-state animation
- [ ] UI-F: Chat display + status bar + hex buttons + input field

### Phase 2 (after user confirms visuals)
- WebSocket /ws integration
- /permissions polling for tier indicator
- Sound effects (need user-provided MP3s)
- Global keyboard shortcuts (Cmd+Shift+J, etc.)
- Optional mic visualizer
- electron-builder packaging
