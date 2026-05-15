"""Tests for the mac_control staged-permission system.

Covers
------
- Kill switch blocks Tier 2+ and allows Tier 1; resume restores.
- Tier of every action is its declared tier (intrinsic, not caller-chosen).
- Sandbox rejects blocked paths AND symlink escapes.
- Confirmation timeout purges stale pendings.
- Dispatcher flow: Tier 3 → pending → consume runs handler.
- Tier 4 wrong-password leaves pending alive for retry.
- Password redaction in action_logger.
- Unknown action / disabled mac_control rejection paths.
- Voice kill-switch phrase detection (matches "jarvis halt", not "halt" alone).

Side effects
------------
No tests run AppleScript, hit the network, or touch user files outside
a per-test temp dir under ~/Documents (which the sandbox allows). All
artifacts are removed via fixtures.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

# Make sure mac_control sees MAC_CONTROL_ENABLED=1 before any of its
# modules look at settings. (The tier modules read settings at import.)
os.environ.setdefault("MAC_CONTROL_ENABLED", "1")
os.environ.setdefault("JARVIS_SUDO_PASSWORD", "testpw")

from server.config import settings  # noqa: E402
from server.mac_control import (  # noqa: E402
    action_logger, confirmation, dispatcher, kill_switch, permission_manager,
    tier3_files,
)
from server.mac_control.permission_manager import Tier  # noqa: E402
from server import stt  # noqa: E402


# --- shared fixtures ------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Clean global state between tests so order doesn't matter."""
    # mac_control on by default; tier3 confirmation ON so the pending-flow
    # tests are deterministic regardless of the caller's .env.
    monkeypatch.setattr(settings, "MAC_CONTROL_ENABLED", True)
    monkeypatch.setattr(settings, "JARVIS_SUDO_PASSWORD", "testpw")
    monkeypatch.setattr(settings, "MAC_TIER3_AUTO_CONFIRM", False)
    permission_manager.lock_tier2()
    if kill_switch.is_set():
        kill_switch.resume()
    # Empty the confirmation store
    for p in confirmation.list_pending():
        confirmation.cancel(p.id)
    yield
    if kill_switch.is_set():
        kill_switch.resume()
    permission_manager.lock_tier2()


@pytest.fixture
def sandbox_dir():
    """A throwaway directory inside the sandbox we can write to."""
    base = Path.home() / "Documents" / f"jarvis_pytest_{os.getpid()}"
    base.mkdir(exist_ok=True)
    yield base
    # rmtree — these are temp paths we created ourselves.
    import shutil
    shutil.rmtree(base, ignore_errors=True)


# --- registry + intrinsic tiers -------------------------------------------- #

def test_every_action_has_intrinsic_tier():
    actions = permission_manager.all_actions()
    assert len(actions) >= 30, "expected 30+ actions registered"
    # No duplicates
    names = [a.name for a in actions]
    assert len(names) == len(set(names))
    # Tier is always one of the enum values
    for a in actions:
        assert a.tier in (Tier.INFO, Tier.APPS, Tier.FILES, Tier.SYSTEM)


def test_specific_tier_assignment():
    """Spot-check that actions are in the tier we expect."""
    assert permission_manager.get("get_time").tier == Tier.INFO
    assert permission_manager.get("send_notification").tier == Tier.APPS
    assert permission_manager.get("move").tier == Tier.FILES
    assert permission_manager.get("terminal").tier == Tier.SYSTEM


def test_caller_cannot_override_tier():
    """Even if the dispatch caller passes ``tier`` in params, the
    registry's tier is what's consulted. (Defence-in-depth — no caller
    code reads a 'tier' kwarg today, but we'd notice if any did.)"""
    env = dispatcher.dispatch("get_time", {"tier": 4})
    assert env["status"] == "ok"
    assert env["tier"] == 1


# --- kill switch ----------------------------------------------------------- #

def test_kill_switch_blocks_tier2_plus_but_allows_tier1():
    kill_switch.trigger("pytest")
    assert dispatcher.dispatch("get_time")["status"] == "ok"
    r2 = dispatcher.dispatch("send_notification", {"title": "x", "body": "y"})
    r3 = dispatcher.dispatch("list_dir", {"path": "~/Desktop"})
    r4 = dispatcher.dispatch("terminal", {"command": "display_sleep"})
    assert r2["status"] == "rejected" and "Kill" in r2["reason"]
    assert r3["status"] == "rejected"
    assert r4["status"] == "rejected"


def test_kill_switch_resume_relocks_tier2():
    """After kill switch, Tier 2 must NOT remain unlocked. Re-confirming
    is required so a forgotten resume can't silently re-enable apps."""
    # Unlock T2 normally
    permission_manager.unlock_tier2()
    assert permission_manager.tier2_is_unlocked()
    kill_switch.trigger("pytest")
    kill_switch.resume()
    assert not permission_manager.tier2_is_unlocked()


# --- sandbox --------------------------------------------------------------- #

@pytest.mark.parametrize("path", [
    "/etc/passwd",
    "/etc/shadow",
    "~/.ssh",
    "~/.ssh/id_rsa",
    "~/Library/Keychains",
    "~/Library/Cookies",
    "/System/Library/CoreServices",
    "~/Desktop/../../etc/hosts",
])
def test_sandbox_blocks_outside(path):
    with pytest.raises(tier3_files.SandboxError):
        tier3_files._validate_path(path, must_exist=False)


def test_sandbox_blocks_symlink_escape(sandbox_dir):
    """A symlink in an allowed dir pointing outside the sandbox must
    NOT pass the check — that's the whole point of using .resolve()."""
    link = sandbox_dir / "escape"
    link.symlink_to("/etc/hosts")
    with pytest.raises(tier3_files.SandboxError):
        tier3_files._validate_path(str(link), must_exist=True)


def test_sandbox_allows_files_under_documents(sandbox_dir):
    """Sanity: ordinary paths under ~/Documents work."""
    target = sandbox_dir / "ok.txt"
    target.write_text("hi")
    resolved = tier3_files._validate_path(str(target), must_exist=True)
    assert resolved.name == "ok.txt"


# --- confirmation timeout -------------------------------------------------- #

def test_confirmation_timeout_purges(monkeypatch):
    """Pendings older than CONFIRMATION_TIMEOUT_S are gone on next access."""
    monkeypatch.setattr(confirmation, "CONFIRMATION_TIMEOUT_S", 0.05)
    p = confirmation.stash(
        tier=3, action="x", handler=lambda: "ran", summary="s",
    )
    assert confirmation.peek(p.id) is not None
    time.sleep(0.1)
    assert confirmation.peek(p.id) is None
    # consume returns None too
    assert confirmation.consume(p.id) is None


# --- dispatcher pending flow ----------------------------------------------- #

def test_tier3_dispatch_returns_pending_then_consume_runs(sandbox_dir):
    target = sandbox_dir / "note.txt"
    env = dispatcher.dispatch("create_file",
                              {"path": str(target), "content": "hello"})
    assert env["status"] == "pending"
    assert env["tier"] == 3
    pid = env["pending_id"]
    # File should NOT exist yet — handler hasn't run.
    assert not target.exists()
    final = dispatcher.consume(pid)
    assert final["status"] == "ok"
    assert target.exists() and target.read_text() == "hello"


def test_tier3_auto_confirm_runs_inline(sandbox_dir, monkeypatch):
    """With MAC_TIER3_AUTO_CONFIRM=True the dispatcher must skip the
    pending step — the user's explicit command IS the confirmation."""
    monkeypatch.setattr(settings, "MAC_TIER3_AUTO_CONFIRM", True)
    target = sandbox_dir / "auto.txt"
    env = dispatcher.dispatch("create_file",
                              {"path": str(target), "content": "auto"})
    assert env["status"] == "ok"
    assert env["tier"] == 3
    assert target.exists() and target.read_text() == "auto"


def test_tier3_cancel_does_not_run(sandbox_dir):
    target = sandbox_dir / "should_not_appear.txt"
    env = dispatcher.dispatch("create_file",
                              {"path": str(target), "content": "x"})
    pid = env["pending_id"]
    cancel_env = dispatcher.cancel(pid)
    assert cancel_env["status"] == "ok"
    assert not target.exists()


# --- tier 4 password ------------------------------------------------------- #

def test_dispatch_dedups_identical_pending(sandbox_dir):
    """Calling the same Tier-3 action twice with the same params must
    return the same pending_id — otherwise retry loops stack identical
    cards in the UI (the actual bug the user reported)."""
    target = sandbox_dir / "dedup.txt"
    a = dispatcher.dispatch("create_file", {"path": str(target), "content": "x"})
    b = dispatcher.dispatch("create_file", {"path": str(target), "content": "x"})
    assert a["status"] == "pending" and b["status"] == "pending"
    assert a["pending_id"] == b["pending_id"]
    assert b.get("deduped") is True
    # Only one pending entry exists
    assert len(confirmation.list_pending()) == 1


def test_cancel_all_drains_pending(sandbox_dir):
    """Bulk cancel must clear every pending in one call."""
    for n in range(3):
        dispatcher.dispatch("create_file",
                            {"path": str(sandbox_dir / f"f{n}.txt"), "content": "x"})
    assert len(confirmation.list_pending()) == 3
    env = dispatcher.cancel_all()
    assert env["cancelled"] == 3
    assert env["remaining"] == 0
    assert confirmation.list_pending() == []


def test_tier4_wrong_password_keeps_pending(monkeypatch):
    """Tippfehler beim Passwort darf die Pending nicht verbrennen."""
    env = dispatcher.dispatch("terminal", {"command": "display_sleep"})
    pid = env["pending_id"]
    bad = dispatcher.consume(pid, password="not the password")
    assert bad["status"] == "rejected"
    assert confirmation.peek(pid) is not None, "pending must survive wrong pw"


def test_tier4_without_password_configured_rejects(monkeypatch):
    monkeypatch.setattr(settings, "JARVIS_SUDO_PASSWORD", "")
    env = dispatcher.dispatch("terminal", {"command": "display_sleep"})
    assert env["status"] == "rejected"
    assert "JARVIS_SUDO_PASSWORD" in env["reason"]


# --- password redaction ---------------------------------------------------- #

def test_password_redacted_in_logs(monkeypatch, tmp_path):
    """If the password ever slips into a log message, the redactor
    must turn it into ***REDACTED*** before it hits disk."""
    secret = "supersecret-pw-42"
    monkeypatch.setattr(settings, "JARVIS_SUDO_PASSWORD", secret)
    action_logger.log_action(4, "noop", "SUCCESS",
                             f"accidental leak: {secret} in message")
    log_path = settings.LOG_DIR / "actions.log"
    contents = log_path.read_text()
    assert secret not in contents
    assert "***REDACTED***" in contents


# --- disabled / unknown ---------------------------------------------------- #

def test_unknown_action_rejected():
    env = dispatcher.dispatch("totally_made_up_action")
    assert env["status"] == "rejected"
    assert "unbekannt" in env["reason"].lower()


def test_mac_control_disabled_rejects(monkeypatch):
    monkeypatch.setattr(settings, "MAC_CONTROL_ENABLED", False)
    env = dispatcher.dispatch("get_time")
    assert env["status"] == "rejected"
    assert "MAC_CONTROL_ENABLED" in env["reason"]


# --- create_note ----------------------------------------------------------- #

def test_create_note_rejects_empty_title():
    from server.mac_control import tier2_apps
    assert "title fehlt" in tier2_apps._create_note(title="", body="x").lower()
    assert "title fehlt" in tier2_apps._create_note(title="   ", body="x").lower()


def test_create_note_html_escape(monkeypatch):
    """User content with HTML special chars must be escaped BEFORE
    osascript so Notes renders it as text, not markup. We intercept
    the osascript invocation to inspect what would be passed."""
    from server.mac_control import tier2_apps
    captured = {}

    def fake_osa(script, *args, **_kw):
        captured["args"] = args
        return ""

    monkeypatch.setattr(tier2_apps, "_osa", fake_osa)
    tier2_apps._create_note(title="t", body="<b>bold</b>\nline2 & more")
    body_arg = captured["args"][1]
    assert "&lt;b&gt;bold&lt;/b&gt;" in body_arg
    assert "<br>" in body_arg
    assert "&amp;" in body_arg
    # Raw < and & should be gone (note: <br> is the only allowed tag)
    assert "<b>" not in body_arg


def test_create_note_truncates_long_body(monkeypatch):
    from server.mac_control import tier2_apps
    monkeypatch.setattr(tier2_apps, "_osa", lambda *a, **k: "")
    long_body = "x" * 20_000
    result = tier2_apps._create_note(title="t", body=long_body)
    assert "gekürzt" in result.lower() or "gekuerzt" in result.lower()


# --- open_app (permissive) ------------------------------------------------- #

def test_open_app_uses_open_command(monkeypatch):
    """open_app now shells out to /usr/bin/open -a, not osascript.
    Verify the right argv is built (and not via shell=True)."""
    from server.mac_control import tier2_apps

    captured = {}

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **_kw):
        captured["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(tier2_apps.subprocess, "run", fake_run)
    result = tier2_apps._open_app(name="Photoshop")
    assert "gestartet" in result.lower()
    assert captured["argv"] == ["/usr/bin/open", "-a", "Photoshop"]


def test_open_app_maps_german_aliases(monkeypatch):
    """Notizen → Notes, Erinnerungen → Reminders — so users can speak
    the localised name without hitting macOS' 'where is X?' hang."""
    from server.mac_control import tier2_apps

    captured = {}

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **_kw):
        captured["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(tier2_apps.subprocess, "run", fake_run)
    tier2_apps._open_app(name="Notizen")
    assert captured["argv"] == ["/usr/bin/open", "-a", "Notes"]
    tier2_apps._open_app(name="Erinnerungen")
    assert captured["argv"] == ["/usr/bin/open", "-a", "Reminders"]


def test_open_app_surfaces_not_found(monkeypatch):
    """When /usr/bin/open returns non-zero, the error reaches the user."""
    from server.mac_control import tier2_apps

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "Unable to find application named 'NopeApp'"

    monkeypatch.setattr(tier2_apps.subprocess, "run", lambda *a, **kw: FakeProc())
    result = tier2_apps._open_app(name="NopeApp")
    assert "nicht gefunden" in result.lower()


def test_open_app_rejects_injection_chars():
    from server.mac_control import tier2_apps
    for bad in ("../etc", "with/slash", "name\nwith\nnewline"):
        result = tier2_apps._open_app(name=bad)
        assert "unzulässige" in result.lower(), f"{bad} should be refused"


# --- run_applescript (Tier 4) ---------------------------------------------- #

def test_run_applescript_rejects_empty():
    from server.mac_control import tier4_system
    assert "fehlt" in tier4_system._run_applescript(script="").lower()
    assert "fehlt" in tier4_system._run_applescript(script="   ").lower()


def test_run_applescript_rejects_oversize():
    from server.mac_control import tier4_system
    huge = "tell me\n" * 1000
    assert "zu lang" in tier4_system._run_applescript(script=huge).lower()


def test_run_applescript_passes_script_via_stdin(monkeypatch):
    """The LLM-produced script must arrive via stdin, never on argv."""
    from server.mac_control import tier4_system
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = "result"
        stderr = ""

    def fake_run(argv, *, input=None, **_kw):
        captured["argv"] = argv
        captured["input"] = input
        return FakeProc()

    monkeypatch.setattr(tier4_system.subprocess, "run", fake_run)
    out = tier4_system._run_applescript(script='tell application "Finder" to count windows')
    # argv is fixed: just osascript and "-" (read from stdin)
    assert captured["argv"] == ["/usr/bin/osascript", "-"]
    assert captured["input"].startswith("tell application")
    assert out == "result"


def test_run_applescript_is_tier4():
    """Hard-pin the tier so a future refactor can't silently demote it."""
    from server.mac_control import permission_manager as pm
    from server.mac_control.permission_manager import Tier
    assert pm.get("run_applescript").tier == Tier.SYSTEM


# --- brain reply dedup ----------------------------------------------------- #

def test_dedupe_collapses_repeated_paragraphs():
    from server.brain import _dedupe_paragraphs
    twice = 'Ich brauche mehr Info.\n\nIch brauche mehr Info.'
    assert _dedupe_paragraphs(twice) == 'Ich brauche mehr Info.'


def test_dedupe_preserves_distinct_paragraphs():
    from server.brain import _dedupe_paragraphs
    t = "Erstens.\n\nZweitens.\n\nDrittens."
    assert _dedupe_paragraphs(t) == t


def test_dedupe_ignores_case_and_whitespace_only_differences():
    from server.brain import _dedupe_paragraphs
    t = "Hallo Welt.\n\n  HALLO  WELT. "
    assert _dedupe_paragraphs(t) == "Hallo Welt."


def test_join_text_collapses_duplicate_blocks():
    """The real bug: Haiku returned the same text in two content blocks.
    _join_text must dedupe them, not concatenate."""
    from server.brain import _join_text

    class FakeBlock:
        def __init__(self, text):
            self.type = "text"
            self.text = text

    class FakeResp:
        content = [
            FakeBlock("Ich benötige mehr Informationen."),
            FakeBlock("Ich benötige mehr Informationen."),
        ]

    assert _join_text(FakeResp()) == "Ich benötige mehr Informationen."


# --- create_reminder ------------------------------------------------------- #

def test_create_reminder_rejects_empty_title():
    from server.mac_control import tier2_apps
    assert "title fehlt" in tier2_apps._create_reminder(title="").lower()


def test_create_reminder_rejects_bad_due():
    from server.mac_control import tier2_apps
    for bad in ("tomorrow", "2026/05/20", "2026-05-20", "today at 5pm"):
        result = tier2_apps._create_reminder(title="x", due=bad)
        assert "iso 8601" in result.lower(), f"{bad} should be rejected"


def test_create_reminder_passes_due_and_list_via_argv(monkeypatch):
    """due and list ride argv (no script interpolation). Verify they
    actually reach _osa in the expected positions."""
    from server.mac_control import tier2_apps
    captured = {}
    monkeypatch.setattr(
        tier2_apps, "_osa",
        lambda script, *args, **_kw: captured.setdefault("args", args) and "",
    )
    tier2_apps._create_reminder(
        title="Milch kaufen",
        body="bei Edeka",
        due="2026-05-20T14:30",
        list="Einkauf",
    )
    args = captured["args"]
    assert args[0] == "Milch kaufen"
    assert args[1] == "bei Edeka"
    assert args[2] == "2026-05-20T14:30"
    assert args[3] == "Einkauf"


def test_create_reminder_empty_optionals(monkeypatch):
    """No due, no list → empty strings, not None or missing."""
    from server.mac_control import tier2_apps
    captured = {}
    monkeypatch.setattr(
        tier2_apps, "_osa",
        lambda script, *args, **_kw: captured.setdefault("args", args) and "",
    )
    tier2_apps._create_reminder(title="x")
    args = captured["args"]
    assert args[2] == "" and args[3] == ""


# --- runtime app allowlist ------------------------------------------------- #

@pytest.fixture
def temp_allowlist(monkeypatch, tmp_path):
    """Redirect the JSON allowlist into a per-test temp dir so we can
    add/remove without touching the real file."""
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "logs").mkdir()
    yield tmp_path


def test_allowlist_add_remove_roundtrip(temp_allowlist):
    from server.mac_control import allowlist
    # Use a name guaranteed not to be in DEFAULT_ALLOWED_APPS so this
    # test doesn't break every time we add a sensible default later.
    ok, msg = allowlist.add("TestAppZ")
    assert ok and "TestAppZ" in msg
    assert "TestAppZ" in allowlist.load_extras()
    ok, msg = allowlist.add("TestAppZ")  # duplicate
    assert not ok
    ok, _ = allowlist.remove("TestAppZ")
    assert ok and "TestAppZ" not in allowlist.load_extras()


def test_allowlist_rejects_blocked_apps(temp_allowlist):
    """Even a Tier-4 password-authenticated add must respect the hard
    BLOCKED_APPS list — this is the prompt-injection floor."""
    from server.mac_control import allowlist
    for evil in ("Mail", "Keychain Access", "1Password"):
        ok, msg = allowlist.add(evil)
        assert not ok, f"{evil} should have been refused"
        assert "Blockliste" in msg


def test_allowlist_rejects_path_injection(temp_allowlist):
    from server.mac_control import allowlist
    for bad in ("../etc", "name\nwith\nnewlines", "with/slash"):
        ok, _ = allowlist.add(bad)
        assert not ok, f"{bad} should have been refused"


def test_open_app_sees_new_allowlist_entry(temp_allowlist):
    """After adding via the allowlist module, current_allowed_apps()
    must include the new entry without a restart."""
    from server.mac_control import allowlist, tier2_apps
    assert "TestAppZ" not in tier2_apps.current_allowed_apps()
    allowlist.add("TestAppZ")
    assert "TestAppZ" in tier2_apps.current_allowed_apps()


# --- voice phrase detectors ------------------------------------------------ #

@pytest.mark.parametrize("text", [
    "jarvis halt", "Jarvis stopp", "JARVIS halt", "notaus",
    "Jarvis halt alles",
])
def test_kill_switch_phrase_matches(text):
    assert stt.is_kill_switch_phrase(text)


@pytest.mark.parametrize("text", [
    "halt",       # bare — only barge-in, not kill switch
    "stop",       # same
    "okay halt",  # same
    "halt mal",
    "",
])
def test_kill_switch_phrase_does_not_match_bare_stops(text):
    assert not stt.is_kill_switch_phrase(text)


@pytest.mark.parametrize("text", [
    "jarvis weiter", "Jarvis resume", "jarvis fortsetzen",
])
def test_resume_phrase_matches(text):
    assert stt.is_resume_phrase(text)
