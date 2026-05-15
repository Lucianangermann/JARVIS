# Todo

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
