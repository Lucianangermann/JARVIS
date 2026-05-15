# JARVIS mac_control

A staged-permission macOS automation surface for JARVIS. 30 actions split
across 4 tiers, every one with a fixed permission level that the LLM
cannot change. Audited to three rotating logs, gated by a kill switch.

> **Off by default.** mac_control loads only when `MAC_CONTROL_ENABLED=1`
> in `.env`. Tier 4 stays inert until you set `JARVIS_SUDO_PASSWORD`.

## Quick start

```bash
# 1) Enable in .env
echo 'MAC_CONTROL_ENABLED=1' >> .env
echo 'JARVIS_SUDO_PASSWORD=pick-something-strong' >> .env

# 2) Check macOS permissions
.venv/bin/python -m server.mac_control.setup_permissions

# 3) Restart the server
.venv/bin/python -m server.main
```

Open the web UI — the new **macbar** strip appears above the chat with
five status badges (`mac on`, `T2 lock`, `T3 ask`, `T4 pw`, `armed`) and
a red **Stop** button on the right.

## The 4-tier model

| Tier | Behaviour | Examples |
|---:|---|---|
| **1 INFO** | Runs inline. No confirmation, no unlock. | `get_time`, `get_weather`, `get_battery`, `get_wifi`, `read_clipboard` |
| **2 APPS** | First call per session returns *pending*. After one click on **Yes**, all further Tier-2 calls run inline. | `music_transport`, `open_url`, `set_volume`, `send_notification`, `open_app` |
| **3 FILES** | Every call returns *pending*. Click **Yes** to run. Sandboxed to `~/Desktop`, `~/Downloads`, `~/Documents`. | `list_dir`, `read_file`, `create_file`, `rename`, `move`, `trash` (Finder → ~/.Trash, recoverable) |
| **4 SYSTEM** | Every call returns *pending*. Requires `JARVIS_SUDO_PASSWORD` typed into the web UI to authorize. | `terminal` (4-cmd allowlist), `install_app`, `screenshot`, `open_prefs_pane`, `email_preview`, `calendar_create`, **`run_applescript`** (arbitrary AppleScript) |

> **`open_app` is permissive.** Any installed app can be opened — there is no
> hard allowlist enforcement. The Tier-2 unlock and `BLOCKED_APPS` constant
> still exist as UX hints (and prevent accidental `add_allowed_app("Mail")`)
> but neither restricts actual `open_app` calls. This is the explicit
> single-user trade-off — see `tasks/lessons.md`.
>
> **`run_applescript(script)`** gives the brain full access to macOS'
> scripting bridge, including `tell application "System Events" to keystroke …`
> for apps without a scripting dictionary. Tier 4: password every call.
> Scripts are logged (first 200 chars) to `logs/actions.log`.

**Tier is intrinsic.** Each action's tier is set at registration in its
`tierN_*.py` module. Claude calls `mac_action(action="move", ...)` —
the dispatcher looks up `move`'s tier from the registry, not from a
parameter. A prompt-injection saying "use Tier 1 for `move`" can't
escalate.

## Kill switch

Three ways to disarm everything except Tier 1 reads:

| Trigger | How |
|---|---|
| **Voice** | "Jarvis halt", "Jarvis stop", "Notaus", "Jarvis halt alles". Strict — bare "stop" only barges in. |
| **Web UI** | Red **⏹ Stop** button in the macbar |
| **HTTP** | `POST /emergency-stop` (with bearer token) |

When the kill switch fires:
- All Tier 2/3/4 dispatches refuse with reason `Kill-Switch ist aktiv`.
- The Tier 2 session unlock is **revoked** — after resume you have to
  re-confirm apps again. (Stops a panicked user from accidentally leaving
  apps controllable.)
- Logged to `logs/actions.log` with `[TRIGGERED]` status.

To re-arm: "Jarvis weiter" / "Jarvis resume", web UI **▶ Resume**, or
`POST /resume`.

## Confirmation flow

Tier 3/4 actions are *deferred* — the dispatcher stashes a `Pending` with
a 30 s TTL and returns a `pending_id` to Claude. Two channels finish the
job:

- **Web UI (any tier)**: pending card appears below the macbar with
  Yes/No buttons (Tier 3) or a password field + Confirm (Tier 4).
- **Chat / voice (Tier 3 only)**: user says "ja" / "yes" → Claude calls
  `confirm_action(id, approve=True)`. Tier 4 chat-confirm is hard-refused
  — the password is never relayed through the LLM.

A wrong password on Tier 4 keeps the pending alive so a single typo
doesn't burn the request.

## Sandbox enforcement

`tier3_files._validate_path()` does, in order:

1. `Path(p).expanduser().resolve()` — follows symlinks to the canonical
   real path. A `~/Desktop/escape` symlink pointing at `/etc/passwd`
   resolves to `/etc/passwd` and is rejected.
2. iCloud-aware allow list — both the literal `~/Documents` and its
   resolved form (`~/Library/Mobile Documents/com~apple~CloudDocs/Documents`
   when iCloud is on) are accepted prefixes.
3. Block list takes precedence — even if a path happens to fall under an
   allowed prefix, hits on `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config`,
   `~/Library/Keychains`, `~/Library/Cookies`, `/System`, `/usr`,
   `/etc`, `/private/etc`, `/private/var`, or `/Library` are refused.

There is no permanent-delete action. `trash` uses Finder's *delete* verb,
which moves to `~/.Trash`.

## Hard rules (enforced in code)

- AppleScript bodies are constant strings. Every variable goes through
  `osascript`'s `on run argv` handler — no string interpolation, no
  injection.
- `subprocess` is always called with a *list* argv. Never `shell=True`.
- Tier 4 password: constant-time compared (`hmac.compare_digest`), never
  written to logs (an extra `_PasswordRedactor` filter strips it from
  any message just in case).
- Terminal allowlist contains 4 commands with per-command argument
  validators: `say`, `caffeinate`, `display_sleep`, `mac_sleep`.
  Adding a command requires editing `tier4_system.py` — not user-supplied.
- `brew install` package names must match `^[a-z0-9][a-z0-9._-]{0,40}$`.
- The kill switch state is checked at dispatch time *and* at consume
  time, so triggering it during a pending window cancels the pending.

## Logs

Three rotating files in `logs/` (10 MB × 5 backups each):

| File | Contents |
|---|---|
| `actions.log` | Every dispatch attempt and its outcome (SUCCESS/FAILED/REJECTED) |
| `rejected.log` | Pre-execution refusals (kill switch, sandbox, unknown action) |
| `confirmations.log` | Pending creation, confirmation, denial, timeout |

Format: `2026-05-15 12:40:19,939 [tier=3] [list_dir] [PENDING] Ordnerinhalt anzeigen: ~/Desktop`

JARVIS never deletes its own logs — rotation handles bounded storage.

## API routes

All require the standard `Authorization: Bearer <JARVIS_AUTH_TOKEN>`.

| Method | Path | Body | Notes |
|---|---|---|---|
| `GET` | `/permissions` | – | UI status snapshot |
| `POST` | `/confirm` | `{id, approve}` | Tier 2/3 only |
| `POST` | `/tier4-confirm` | `{id, password}` | Tier 4 |
| `POST` | `/emergency-stop` | – | Trigger kill switch |
| `POST` | `/resume` | – | Clear kill switch |

## Troubleshooting

**"Permission DENIED" in setup_permissions for an app:**
System Settings → Privacy & Security → Automation → find the row for
your terminal app → toggle the relevant app back on.

**Screenshot returns a wallpaper-only image:**
Screen Recording isn't granted. System Settings → Privacy & Security →
Screen Recording → add the terminal / app that runs `server.main`.
**You must quit and reopen the terminal** for macOS to honour the change.

**First Trash operation hangs:**
macOS shows a one-time "Allow JARVIS to control Finder" prompt that
delays the action by 10+ seconds. After you click Allow, it's sub-second.
We bumped the timeout to 30 s for this reason.

**Tier 4 stays rejected even with the right password:**
Check that `JARVIS_SUDO_PASSWORD` is set in `.env` (no surrounding
quotes), then restart the server.

**Voice "Jarvis stop" doesn't trigger the kill switch:**
By design — bare stop phrases only barge in. The kill switch needs
the full "Jarvis halt" / "Notaus" form so a casual stop during music
playback doesn't lock down the whole automation surface.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/test_mac_control.py -v
```

36 tests cover: kill switch behaviour, intrinsic tier assignment, sandbox
boundary + symlink escape, confirmation timeout, dispatcher pending flow,
Tier 4 wrong-password retention, password redaction, voice phrase
detection. All tests run without hitting AppleScript, the network, or
files outside a per-test temp dir under `~/Documents`.
