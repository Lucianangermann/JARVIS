# Todo

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
