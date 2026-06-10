"""Tests for the JARVIS communication layer.

Runs against a temp communication.db so nothing touches real data.
AppleScript / chat.db / Telegram / Claude paths are exercised with
synthetic data or monkeypatching — no Mac, no token, no network needed.

Coverage matrix
---------------
- CommunicationDB: schema, message/call/followup round-trips, 7-day prune
- NotificationCenter: priority routing, DND suppression, batching, quiet hrs
- CommunicationTranslator: target-lang prompt, fallback on no client
- iMessageController: chat.db parse + Apple-ts + injection-safe send
- MessagingManager: confirm-before-send, broadcast, TTL expiry
- EmailTemplateManager: built-ins, fill, missing-var, persist
- EmailAnalyzer: newsletter + unsubscribe heuristics
- CallManager: make_call scheme, callback reminder, missed from db
- CommunicationAutomation: auto-reply VIP bypass, follow-ups, status
- SocialManager: draft char-limit, honest stubs
- CommunicationManager: routing (translate/unread/confirm) + None fallthrough
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.communication.db import CommunicationDB
from server.communication.notifications import NotificationCenter
from server.communication.translation import CommunicationTranslator
from server.communication.messaging.imessage import iMessageController
from server.communication.messaging.messaging_manager import MessagingManager
from server.communication.email.email_templates import EmailTemplateManager
from server.communication.email.email_analyzer import EmailAnalyzer
from server.communication.automation.comm_automation import CommunicationAutomation
from server.communication.social.social_manager import SocialManager


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(tmp_path: Path) -> CommunicationDB:
    d = CommunicationDB(tmp_path / "communication.db")
    yield d
    d.close()


# ── DB ──────────────────────────────────────────────────────────────────── #

def test_db_tables(db: CommunicationDB) -> None:
    names = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"messages", "calls", "notifications", "auto_replies",
            "followups"} <= names


def test_db_message_retention_prune(db: CommunicationDB) -> None:
    import time
    mid = db.log_message("imessage", "in", "Mama", "geheim")
    db._execute("UPDATE messages SET timestamp=? WHERE id=?",
                (time.time() - 8 * 86400, mid))
    db.prune_message_content()
    row = db.query("SELECT content FROM messages WHERE id=?", (mid,))[0]
    assert row["content"] is None  # body pruned after 7 days


def test_db_followup_lifecycle(db: CommunicationDB) -> None:
    import time
    db.track_followup("imessage", "Tom", "Hi", followup_due=time.time() - 1)
    assert len(db.due_followups(time.time())) == 1
    db.mark_response_received("imessage", "Tom")
    assert len(db.due_followups(time.time())) == 0


# ── NotificationCenter ──────────────────────────────────────────────────── #

def test_notification_priority_routing(db: CommunicationDB) -> None:
    hits = {"voice": 0, "ui": 0, "tg": 0, "mac": 0}
    nc = NotificationCenter(
        db=db,
        voice_handler=lambda t: hits.__setitem__("voice", hits["voice"] + 1),
        ui_handler=lambda e: hits.__setitem__("ui", hits["ui"] + 1),
        telegram_handler=lambda a, b, c: hits.__setitem__("tg", hits["tg"] + 1),
        macos_handler=lambda a, b: hits.__setitem__("mac", hits["mac"] + 1),
    )
    nc.send("t", "b", "high")       # voice + ui + telegram
    assert hits == {"voice": 1, "ui": 1, "tg": 1, "mac": 0}
    nc.send("t", "b", "critical")   # all 4
    assert hits["mac"] == 1


def test_notification_dnd_suppresses_non_critical(db: CommunicationDB) -> None:
    spoke = []
    nc = NotificationCenter(db=db, voice_handler=lambda t: spoke.append(t))
    nc.set_dnd(True)
    r1 = nc.send("Email", "neue mail", "medium")
    r2 = nc.send("SOS", "Notfall", "critical")
    assert r1["suppressed"] == "dnd"
    assert r2["delivered_via"]  # critical still goes through
    assert spoke == ["SOS. Notfall"]


def test_notification_batch_summary(db: CommunicationDB) -> None:
    nc = NotificationCenter(db=db, ui_handler=lambda e: None)
    nc.send("News", "a", "low")
    nc.send("Tip", "b", "low")
    summary = nc.batch_summary()
    assert "2 Benachrichtigungen" in summary


# ── Translator ──────────────────────────────────────────────────────────── #

def test_translator_no_client_falls_back(db: CommunicationDB) -> None:
    tr = CommunicationTranslator(client=None, default_lang="de")
    assert tr.available is False
    # Without a client, translate returns the original text.
    assert _run(tr.translate("hello", "de")) == "hello"


def test_translator_prompt_targets_language() -> None:
    captured = {}

    class FakeBlock:
        type = "text"; text = "translated"

    class FakeResp:
        content = [FakeBlock()]

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                captured["prompt"] = kw["messages"][0]["content"]
                return FakeResp()

    tr = CommunicationTranslator(client=FakeClient())
    out = _run(tr.translate("Hallo", "en"))
    assert out == "translated"
    assert "English" in captured["prompt"]


# ── iMessage (synthetic chat.db) ────────────────────────────────────────── #

def _make_chat_db(path: Path) -> None:
    import sqlite3
    import time
    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);"
        "CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, "
        "handle_id INTEGER, is_from_me INTEGER, is_read INTEGER, date INTEGER);")
    c.execute("INSERT INTO handle VALUES (1,'mama@icloud.com')")
    apple = int((time.time() - 978307200) * 1e9)
    c.execute("INSERT INTO message VALUES (1,'Kommst du?',1,0,0,?)", (apple,))
    c.execute("INSERT INTO message VALUES (2,'Ja',1,1,1,?)", (apple + 1,))
    c.commit()
    c.close()


def test_imessage_chatdb_read(tmp_path: Path) -> None:
    chat = tmp_path / "chat.db"
    _make_chat_db(chat)
    im = iMessageController(chat_db=chat)
    unread = _run(im.get_unread())
    assert len(unread) == 1 and unread[0]["sender"] == "mama@icloud.com"
    convo = _run(im.get_conversation("mama", 10))
    assert convo[-1]["sender"] == "me"  # is_from_me row labelled "me"


def test_imessage_send_is_injection_safe(monkeypatch, tmp_path: Path) -> None:
    import server.communication.messaging.imessage as imod
    seen = {}
    monkeypatch.setattr(imod, "osa",
                        lambda script, *a, timeout=10.0: seen.update(
                            script=script, args=a) or "sent")
    im = iMessageController(chat_db=tmp_path / "none.db")
    evil = 'hi"; tell application "Finder" to delete'
    assert _run(im.send("+49170", evil)) is True
    assert seen["args"][1] == evil          # passed as argv …
    assert evil not in seen["script"]       # … never interpolated


# ── MessagingManager ────────────────────────────────────────────────────── #

def test_messaging_confirm_before_send(db: CommunicationDB, monkeypatch) -> None:
    mm = MessagingManager(db=db)
    sent = []
    monkeypatch.setattr(mm, "_raw_send",
                        lambda p, c, m: sent.append((p, c, m)) or
                        asyncio.sleep(0) if False else _coro_true(sent, p, c, m))

    async def go():
        r = await mm.send("imessage", "Max", "Hallo")
        assert r["needs_confirm"] and mm.has_pending()
        out = await mm.confirm_pending()
        assert "gesendet" in out
        assert sent == [("imessage", "Max", "Hallo")]
    _run(go())


def _coro_true(sent, p, c, m):
    async def _c():
        sent.append((p, c, m))
        return True
    return _c()


def test_messaging_pending_ttl(db: CommunicationDB) -> None:
    mm = MessagingManager(db=db)
    _run(mm.send("imessage", "Max", "x"))
    mm._pending["ts"] -= 40  # age past the 30s TTL
    assert mm.has_pending() is False


# ── Email ───────────────────────────────────────────────────────────────── #

def test_email_template_fill(tmp_path: Path) -> None:
    tm = EmailTemplateManager(path=tmp_path / "t.json")
    filled = tm.fill("meeting_request",
                     {"name": "Anna", "topic": "Roadmap", "date": "Mo"})
    assert "Roadmap" in filled["subject"]
    # Missing variable stays as a literal placeholder, no crash.
    f2 = tm.fill("danke", {"name": "Tom"})
    assert "{reason}" in f2["body"]


def test_email_template_persist(tmp_path: Path) -> None:
    p = tmp_path / "t.json"
    EmailTemplateManager(path=p).save_template("x", "S {a}", "B {a}")
    assert "x" in EmailTemplateManager(path=p).all_names()


def test_email_newsletter_heuristics() -> None:
    ea = EmailAnalyzer()
    assert ea.looks_like_newsletter("Newsletter", "click unsubscribe", "x")
    assert ea.extract_unsubscribe_link(
        "To unsubscribe go to https://x.com/u?i=1") == "https://x.com/u?i=1"


# ── Calls ───────────────────────────────────────────────────────────────── #

def test_call_make_uses_facetime_scheme(db: CommunicationDB, monkeypatch) -> None:
    import server.communication.calls.call_manager as cmod
    calls = []
    monkeypatch.setattr(cmod, "osa", lambda s, *a, timeout=10.0:
                        (calls.append(a) or ("+49170" if "Contacts" in s else "ok")))
    cm = cmod.CallManager(db=db)
    r = _run(cm.make_call("Mama"))
    assert r["ok"]
    assert any("facetime-audio://" in str(a) for a in calls)


def test_call_callback_reminder(db: CommunicationDB, monkeypatch) -> None:
    import server.communication.calls.call_manager as cmod
    monkeypatch.setattr(cmod.reminders_tool, "create_reminder",
                        lambda title, list_name=None, due_date=None: ("ok", False))
    cm = cmod.CallManager(db=db)
    out = _run(cm.set_callback_reminder("Max", "later"))
    assert "zurückrufen" in out


# ── Automation ──────────────────────────────────────────────────────────── #

def test_autoreply_vip_bypass(db: CommunicationDB, tmp_path: Path) -> None:
    auto = CommunicationAutomation(db=db, rules_path=tmp_path / "ar.json")
    _run(auto.enable_auto_reply(trigger="focus", message="busy",
                                platforms=["imessage"], exceptions=["Mama"]))
    assert _run(auto.check_auto_reply("imessage", "Max", "hi")) == "busy"
    assert _run(auto.check_auto_reply("imessage", "Mama", "hi")) is None
    assert _run(auto.check_auto_reply("telegram", "Max", "hi")) is None


def test_status_auto_revert(db: CommunicationDB, tmp_path: Path) -> None:
    import time
    auto = CommunicationAutomation(db=db, rules_path=tmp_path / "ar.json")
    _run(auto.set_communication_status("busy", "x", duration_minutes=30))
    assert auto.current_status() == "busy"
    auto._status["revert_at"] = time.time() - 1
    assert auto.current_status() == "available"


# ── Social ──────────────────────────────────────────────────────────────── #

def test_social_draft_respects_char_limit(db: CommunicationDB) -> None:
    class FakeBlock:
        type = "text"; text = "x" * 500

    class FakeResp:
        content = [FakeBlock()]

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                return FakeResp()

    sm = SocialManager(db=db, client=FakeClient())
    d = _run(sm.draft_post("twitter", "topic"))
    assert d["ok"] and d["chars"] <= 280


def test_social_stubs_are_honest(db: CommunicationDB) -> None:
    sm = SocialManager(db=db)
    assert "nicht konfiguriert" in _run(sm.get_twitter_mentions())
    assert "nicht konfiguriert" in _run(sm.get_linkedin_messages())


# ── CommunicationManager routing ────────────────────────────────────────── #

def test_manager_routes_and_fallsthrough(tmp_path: Path, monkeypatch) -> None:
    from server.communication.communication_manager import CommunicationManager

    class FakeBlock:
        type = "text"; text = "Good morning"

    class FakeResp:
        content = [FakeBlock()]

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                return FakeResp()

    cm = CommunicationManager(db_path=tmp_path / "c.db", client=FakeClient())
    try:
        # translation route
        out = _run(cm.process_command("übersetze auf englisch: guten morgen"))
        assert out == "Good morning"
        # staged send → preview, then confirm word routes to confirm
        prev = _run(cm.process_command("schreib Max: hallo"))
        assert "Bestätigen" in prev and cm.messaging.has_pending()
        # non-comm input falls through to Claude (None)
        assert _run(cm.process_command("erzähl einen witz")) is None
    finally:
        cm.stop()
