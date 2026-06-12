"""ProductivityExecMixin — productivity tool handler.

Mixed into Brain. All self.* attributes are satisfied by Brain.__init__.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class ProductivityExecMixin:
    """Exec method for the entire productivity tool surface (tasks, focus,
    analytics, knowledge notes, flashcards, reminders, meetings)."""

    def _exec_productivity(
        self, tool_name: str, inp: dict[str, Any],
    ) -> tuple[str, bool]:
        """Dispatch productivity tool_use calls to the ProductivityManager."""
        try:
            if self._productivity is None:  # type: ignore[attr-defined]
                from pathlib import Path as _Path
                from ..productivity.productivity_manager import ProductivityManager as _PM
                _db = _Path(__file__).resolve().parents[2] / "data" / "jarvis.db"
                self._productivity = _PM(_db)  # type: ignore[attr-defined]

            pm = self._productivity  # type: ignore[attr-defined]
            inp = inp or {}

            if tool_name == "manage_tasks":
                action = inp.get("action", "")
                if action == "add":
                    title = inp.get("title", "")
                    if not title:
                        return "title ist erforderlich.", True
                    # Auto-link to a goal if keyword overlap exists.
                    goal_id: int | None = None
                    goal_hint = ""
                    try:
                        from ..productivity.goals import GoalDB as _GoalDB
                        _gdb = _GoalDB(_DATA_DIR / "jarvis.db")
                        goal_id = _gdb.auto_link_task(title)
                        if goal_id:
                            goals = _gdb.get_active()
                            g = next((g for g in goals if g["id"] == goal_id), None)
                            if g:
                                goal_hint = f" → Ziel '{g['title'][:30]}'"
                        _gdb.close()
                    except Exception:
                        pass
                    tid = pm.tasks.add_task(
                        title,
                        priority=int(inp.get("priority", 2)),
                        due_date=inp.get("due_date"),
                        project_name=inp.get("project"),
                        context=inp.get("context", "work"),
                        goal_id=goal_id,
                    )
                    return f"Task erstellt (id={tid}): {title}{goal_hint}", False
                if action == "list_today":
                    tasks = pm.tasks.get_today_tasks()
                    if not tasks:
                        return "Keine offenen Tasks für heute.", False
                    lines = [
                        f"{t['id']}. [{t['priority']}] {t['title']}"
                        + (f" (fällig {t['due_date']})" if t.get("due_date") else "")
                        for t in tasks
                    ]
                    return "\n".join(lines), False
                if action == "top3":
                    return pm.tasks.spoken_top3(), False
                if action == "complete":
                    tid = inp.get("task_id")
                    if not tid:
                        return "task_id ist erforderlich.", True
                    ok = pm.tasks.complete_task(int(tid))
                    if ok:
                        # Auto-update goal progress from linked tasks.
                        try:
                            row = pm.tasks._conn.execute(
                                "SELECT goal_id FROM tasks WHERE id=?", (int(tid),)
                            ).fetchone()
                            if row and row[0]:
                                from ..productivity.goals import GoalDB as _GoalDB
                                _gdb = _GoalDB(_DATA_DIR / "jarvis.db")
                                _gdb.update_progress_from_tasks(int(row[0]))
                                _gdb.close()
                        except Exception:
                            pass
                    return ("Task erledigt." if ok else "Task nicht gefunden."), not ok
                if action == "project_status":
                    proj = inp.get("project", "")
                    if not proj:
                        projs = pm.tasks.list_projects()
                        if not projs:
                            return "Keine aktiven Projekte.", False
                        return "Projekte: " + ", ".join(p["name"] for p in projs), False
                    return pm.tasks.spoken_project_status(proj), False
                if action == "list_overdue":
                    overdue = pm.tasks.get_overdue()
                    if not overdue:
                        return "Keine überfälligen Tasks.", False
                    lines = [f"{t['id']}. {t['title']} (fällig {t['due_date']})"
                             for t in overdue[:10]]
                    return "\n".join(lines), False
                return f"Unbekannte action: {action}", True

            if tool_name == "manage_focus":
                action = inp.get("action", "")
                if action == "start_pomodoro":
                    mins = int(inp.get("minutes", 25))
                    return pm.focus.start_pomodoro(inp.get("task", ""), mins), False
                if action == "stop_pomodoro":
                    return pm.focus.stop_pomodoro(), False
                if action == "start_timer":
                    proj = inp.get("project", "Allgemein")
                    return pm.focus.start_timer(proj, inp.get("task", "")), False
                if action == "stop_timer":
                    return pm.focus.stop_timer(), False
                if action == "time_today":
                    return pm.focus.get_time_today(), False
                return f"Unbekannte action: {action}", True

            if tool_name == "get_productivity_score":
                period = inp.get("period", "today")
                if period == "week":
                    return pm.analytics.weekly_summary(), False
                return pm.analytics.spoken_daily_score(), False

            if tool_name == "add_knowledge_note":
                content = inp.get("content", "")
                category = inp.get("category", "idea")
                if not content:
                    return "content ist erforderlich.", True
                entry_id = self.memory.long_term.save_knowledge(  # type: ignore[attr-defined]
                    content, source="explicit", category=category,
                )
                if entry_id:
                    # Record topic tags for graph co-occurrence tracking.
                    try:
                        from ..memory.long_term import _extract_cluster_tags as _ect
                        tags = _ect(content, n=4).split(",")
                        self.memory.topic_graph.record_tags(  # type: ignore[attr-defined]
                            [t for t in tags if t])
                    except Exception:
                        pass
                    return f"Notiz gespeichert ({category}): {content[:60]}…", False
                return ("Konnte die Notiz nicht dauerhaft speichern "
                        "(Wissensspeicher nicht verfügbar)."), True

            if tool_name == "recall_knowledge":
                query = inp.get("query", "")
                if not query:
                    return "query ist erforderlich.", True
                category = inp.get("category")
                results, cluster_hints = self.memory.long_term.search_knowledge_with_clusters(  # type: ignore[attr-defined]
                    query, n_results=int(inp.get("n", 5)))
                if category:
                    results = [r for r in results
                               if r.get("metadata", {}).get("category") == category]
                if not results:
                    return f"Ich weiß nichts über '{query}'.", False
                # Log access for staleness tracking.
                try:
                    for r in results:
                        doc_id = r.get("id", "")
                        if doc_id:
                            self.memory.knowledge_staleness.record_access(doc_id)  # type: ignore[attr-defined]
                except Exception:
                    pass
                facts = [r["document"] for r in results[:5]]
                answer = "Dazu weiß ich: " + " · ".join(facts)
                # Append topic graph bridge hints if available.
                try:
                    from ..memory.long_term import _extract_cluster_tags as _ect
                    query_tags = _ect(query, n=3).split(",")
                    bridges = self.memory.topic_graph.topic_bridge(  # type: ignore[attr-defined]
                        [t for t in query_tags if t], n=2)
                    if bridges:
                        bridge_txt = " und ".join(f"'{b['tag']}'" for b in bridges)
                        answer += f" (Verwandte Themen: {bridge_txt}.)"
                except Exception:
                    pass
                if cluster_hints:
                    hints_txt = "; ".join(
                        f"Du hast {h['total']} Notiz{'en' if h['total'] != 1 else ''} "
                        f"zu '{h['tag']}'"
                        for h in cluster_hints[:2]
                    )
                    answer += f" — {hints_txt}."
                return answer, False

            if tool_name == "get_email_smart_summary":
                from ..tools.mail_tool import list_unread
                result, is_err = list_unread("INBOX")
                return result, is_err

            if tool_name == "flashcards":
                fc = self._get_flashcards()  # type: ignore[attr-defined]
                if fc is None:
                    return "Karteikarten nicht verfügbar.", True
                action = inp.get("action", "")
                if action == "add":
                    front, back = inp.get("front", ""), inp.get("back", "")
                    if not front or not back:
                        return "front und back sind erforderlich.", True
                    cid = fc.add_card(front, back, inp.get("category", "general"))
                    return (f"Karteikarte angelegt (id={cid}).", False) if cid \
                        else ("Konnte Karte nicht speichern.", True)
                if action == "due":
                    return fc.spoken_due(), False
                if action == "next":
                    due = fc.due_cards(limit=1)
                    if not due:
                        return "Keine fälligen Karten.", False
                    c = due[0]
                    return f"Frage (Karte {c['id']}): {c['front']}", False
                if action == "reveal":
                    cid = inp.get("card_id")
                    card = fc.get_card(int(cid)) if cid else None
                    return (f"Antwort: {card['back']}", False) if card \
                        else ("Karte nicht gefunden.", True)
                if action == "grade":
                    cid = inp.get("card_id")
                    if not cid:
                        return "card_id ist erforderlich.", True
                    q = fc.quality_from_feedback(inp.get("feedback", "richtig"))
                    r = fc.review_card(int(cid), q)
                    if not r:
                        return "Karte nicht gefunden.", True
                    days = r["interval_days"]
                    return (f"Gemerkt. Nächste Wiederholung in "
                            f"{days:.0f} Tag{'en' if days != 1 else ''}.", False)
                if action == "generate":
                    text = inp.get("text", "")
                    if not text:
                        return "text ist erforderlich.", True
                    ids = fc.generate_from_text(text, inp.get("category", "learning"))
                    return (f"{len(ids)} Karteikarten erstellt." if ids
                            else "Konnte keine Karten erstellen."), not ids
                if action == "stats":
                    s = fc.stats()
                    return (f"{s['total']} Karten gesamt, {s['due']} fällig.", False)
                return f"Unbekannte action: {action}", True

            if tool_name == "schedule_action":
                import time as _t
                trg = self._get_triggers()  # type: ignore[attr-defined]
                if trg is None:
                    return "Erinnerungs-Planer nicht verfügbar.", True
                action = inp.get("action", "schedule")
                if action == "list":
                    return trg.spoken_pending(), False
                if action == "cancel":
                    tid = inp.get("id")
                    if not tid:
                        return "id ist erforderlich.", True
                    ok = trg.cancel(int(tid))
                    return ("Erinnerung gelöscht." if ok else "Nicht gefunden."), not ok
                message = inp.get("message", "")
                if not message:
                    return "message ist erforderlich.", True
                fire_at: float | None = None
                if inp.get("delay_minutes") is not None:
                    fire_at = _t.time() + float(inp["delay_minutes"]) * 60
                elif inp.get("at"):
                    try:
                        hh, mm = (int(x) for x in str(inp["at"]).split(":"))
                    except Exception:  # noqa: BLE001
                        return "Konnte die Uhrzeit nicht verstehen (HH:MM).", True
                    import datetime as _dt
                    now_dt = _dt.datetime.now()
                    base = now_dt
                    date_str = str(inp.get("date", "")).strip().lower()
                    if date_str in ("morgen", "tomorrow"):
                        base = now_dt + _dt.timedelta(days=1)
                    elif date_str in ("übermorgen", "uebermorgen"):
                        base = now_dt + _dt.timedelta(days=2)
                    elif date_str:
                        try:
                            d = _dt.date.fromisoformat(date_str)
                            base = _dt.datetime(d.year, d.month, d.day)
                        except ValueError:
                            return "Datum bitte als 'YYYY-MM-DD', 'morgen' oder 'übermorgen'.", True
                    target = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if not date_str and target <= now_dt:
                        target += _dt.timedelta(days=1)
                    fire_at = target.timestamp()
                if fire_at is None:
                    return "Sag mir, wann (in X Minuten oder um HH:MM, optional mit Datum).", True
                recurrence = str(inp.get("recurrence") or "none").lower()
                rec_weekday = inp.get("recurrence_weekday")
                trg.add(fire_at, message, recurrence=recurrence,
                        recurrence_weekday=int(rec_weekday) if rec_weekday is not None else None)
                import datetime as _dt2
                fire_dt = _dt2.datetime.fromtimestamp(fire_at)
                if fire_dt.date() == _dt2.date.today():
                    when = fire_dt.strftime("%H:%M Uhr")
                else:
                    when = fire_dt.strftime("%d.%m. um %H:%M Uhr")
                rec_label = {"daily": " (täglich wiederholt)",
                             "weekly": " (wöchentlich wiederholt)",
                             "weekdays": " (Mo–Fr wiederholt)"}.get(recurrence, "")
                return f"Erinnerung für {when} gesetzt: {message}{rec_label}.", False

            if tool_name == "meeting_control":
                meeting = getattr(pm, "meeting", None)
                if meeting is None:
                    return "Meeting-Assistent nicht verfügbar.", True
                if getattr(meeting, "_client", None) is None:
                    meeting._client = self.client  # type: ignore[attr-defined]  # noqa: SLF001
                action = inp.get("action", "")
                if action == "start":
                    r = meeting.start_recording(inp.get("title", "Meeting"))
                    return (r.get("spoken") or r.get("error", "")), not r.get("ok")
                if action == "status":
                    return ("Eine Meeting-Aufnahme läuft." if meeting.is_recording()
                            else "Es läuft keine Aufnahme."), False
                if action in ("stop", "summarize"):
                    import asyncio as _aio
                    from .. import events as _events
                    if action == "summarize":
                        coro = meeting.process_transcript(
                            inp.get("transcript", ""), inp.get("title"))
                    else:
                        coro = meeting.end_meeting(inp.get("title"))
                    main_loop = _events._loop
                    if main_loop is not None and main_loop.is_running():
                        r = _aio.run_coroutine_threadsafe(
                            coro, main_loop).result(timeout=40)
                    else:
                        r = _aio.run(coro)
                    return (r.get("spoken") or "Meeting verarbeitet."), not r.get("ok")
                return f"Unbekannte action: {action}", True

            if tool_name == "search_memory":
                query = inp.get("query", "")
                if not query:
                    return "query ist erforderlich.", True
                n = int(inp.get("n") or 5)
                hits = self.memory.long_term.search_similar(  # type: ignore[attr-defined]
                    query, n_results=n)
                hits = [h for h in hits if (h.get("distance") or 1.0) < 0.85]
                if not hits:
                    return f"Keine passenden Gespräche zu '{query}' gefunden.", False
                import datetime as _dt
                parts = []
                for h in hits[:5]:
                    ts = (h.get("metadata") or {}).get("ended_at", 0)
                    when = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?"
                    body = (h.get("document") or "").strip().replace("\n", " ")
                    if len(body) > 160:
                        body = body[:160] + "…"
                    parts.append(f"[{when}] {body}")
                return "\n".join(parts), False

            if tool_name == "track_mood":
                from pathlib import Path as _Path
                from ..productivity.mood_tracker import MoodTracker as _MT
                mt = _MT(_Path("data/jarvis.db"))
                action = inp.get("action", "log")
                if action == "log":
                    score_raw = inp.get("score")
                    if score_raw is None:
                        return "score (1-10) ist erforderlich.", True
                    try:
                        score = int(score_raw)
                    except (ValueError, TypeError):
                        return "score muss eine Zahl von 1-10 sein.", True
                    note = str(inp.get("note") or "")
                    eid = mt.log(score, note)
                    mt.close()
                    if eid:
                        mood_labels = {
                            1: "sehr schlecht", 2: "schlecht", 3: "nicht so gut",
                            4: "mäßig", 5: "okay", 6: "ganz gut",
                            7: "gut", 8: "sehr gut", 9: "super", 10: "fantastisch",
                        }
                        label = mood_labels.get(score, str(score))
                        msg = f"Stimmung {score}/10 ({label}) gespeichert."
                        if note:
                            msg += f" Notiz: {note[:60]}."
                        return msg, False
                    return "Konnte Stimmung nicht speichern.", True
                if action == "today":
                    entry = mt.today_mood()
                    mt.close()
                    if not entry:
                        return "Heute noch keine Stimmung eingetragen.", False
                    score = entry["score"]
                    note = entry.get("note", "")
                    msg = f"Heutige Stimmung: {score}/10."
                    if note:
                        msg += f" '{note}'"
                    return msg, False
                if action == "weekly":
                    text = mt.spoken_weekly()
                    mt.close()
                    return text or "Keine Stimmungsdaten dieser Woche.", False
                mt.close()
                return f"Unbekannte action: {action}", True

            if tool_name == "manage_goals":
                from ..productivity.goals import GoalDB as _GoalDB
                gdb = _GoalDB(_DATA_DIR / "jarvis.db")
                action = inp.get("action", "list")
                try:
                    if action == "list":
                        return gdb.spoken_status(), False
                    if action == "add":
                        title = str(inp.get("title") or "").strip()
                        if not title:
                            return "title ist erforderlich.", True
                        gid = gdb.add(
                            title,
                            description=str(inp.get("description") or ""),
                            deadline=inp.get("deadline"),
                        )
                        if gid:
                            dl = inp.get("deadline")
                            suffix = f" (Deadline: {dl})" if dl else ""
                            return f"Ziel gespeichert (#{gid}): {title}{suffix}.", False
                        return "Konnte Ziel nicht speichern.", True
                    if action == "update":
                        gid = inp.get("goal_id")
                        pct = inp.get("pct")
                        if gid is None or pct is None:
                            return "goal_id und pct sind erforderlich.", True
                        ok = gdb.update_progress(int(gid), int(pct),
                                                 str(inp.get("note") or ""))
                        if ok:
                            # Advance the SR review interval on explicit update.
                            gdb.record_review(int(gid), pct_update=None)
                        return (f"Fortschritt aktualisiert: {pct}%." if ok
                                else "Ziel nicht gefunden."), not ok
                    if action == "achieve":
                        gid = inp.get("goal_id")
                        if gid is None:
                            return "goal_id ist erforderlich.", True
                        ok = gdb.achieve(int(gid))
                        return ("Ziel als erreicht markiert!" if ok
                                else "Ziel nicht gefunden."), not ok
                    if action == "abandon":
                        gid = inp.get("goal_id")
                        if gid is None:
                            return "goal_id ist erforderlich.", True
                        ok = gdb.abandon(int(gid))
                        return ("Ziel aufgegeben." if ok
                                else "Ziel nicht gefunden."), not ok
                    return f"Unbekannte action: {action}", True
                finally:
                    gdb.close()

            if tool_name == "journal":
                from pathlib import Path as _Path
                from ..productivity.journal import JournalDB as _Journal
                jdb = _Journal(_Path("data/jarvis.db"))
                action = inp.get("action", "today")
                try:
                    if action == "today":
                        return jdb.spoken_today(), False
                    if action == "weekly":
                        return jdb.spoken_week(), False
                    if action == "insights":
                        client = getattr(self, "client", None)  # type: ignore[attr-defined]
                        return jdb.insights(client=client), False
                    return f"Unbekannte action: {action}", True
                finally:
                    jdb.close()

            if tool_name == "study_plan":
                from pathlib import Path as _Path
                from ..productivity.curriculum import generate_curriculum as _curriculum
                available = int(inp.get("available_minutes") or 60)
                client = getattr(self, "client", None)  # type: ignore[attr-defined]
                lt = fc = None
                try:
                    from ..knowledge.lerntrack import LerntrackDB as _LT
                    lt = _LT(_DATA_DIR / "lerntrack.db")
                except Exception:
                    pass
                try:
                    fc = self._get_flashcards()  # type: ignore[attr-defined]
                except Exception:
                    pass
                result = _curriculum(
                    lerntrack=lt,
                    flashcard_manager=fc,
                    available_minutes=available,
                    client=client,
                )
                if lt is not None:
                    try:
                        lt.close()
                    except Exception:
                        pass
                return result, False

            if tool_name == "self_reflect":
                si = getattr(self.memory, "self_improvement", None)  # type: ignore[attr-defined]
                if si is None or not si.available:
                    return "Self-improvement-System nicht verfügbar.", True
                action = inp.get("action", "list")
                if action == "list":
                    lessons = si.get_active_lessons(limit=20)
                    if not lessons:
                        return "Noch keine Verhaltensregeln gelernt.", False
                    import datetime as _dt
                    lines = []
                    for r in lessons:
                        when = _dt.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
                        ltype = r.get("lesson_type") or "general"
                        conf = r.get("confidence", 0.8)
                        lines.append(
                            f"[#{r['id']} | {ltype} | conf={conf:.1f} | {when}] {r['lesson']}"
                        )
                    return "\n".join(lines), False
                if action == "remove":
                    lid = inp.get("id")
                    if lid is None:
                        return "id ist erforderlich.", True
                    ok = si.deactivate_lesson(int(lid))
                    return (f"Regel #{lid} deaktiviert." if ok
                            else f"Regel #{lid} nicht gefunden."), not ok
                if action == "add":
                    lesson = str(inp.get("lesson") or "").strip()
                    if not lesson:
                        return "lesson ist erforderlich.", True
                    ltype = str(inp.get("lesson_type") or "general").lower()
                    eid = si.add_lesson(lesson, source="manual", lesson_type=ltype)
                    if eid:
                        return f"Regel gespeichert ({ltype}): {lesson}", False
                    return "Regel konnte nicht gespeichert werden (Duplikat?).", True
                if action == "stats":
                    return si.spoken_summary(), False
                if action == "consolidate":
                    client = getattr(self, "client", None)  # type: ignore[attr-defined]
                    if client is None:
                        return "Claude-Client nicht verfügbar für Konsolidierung.", True
                    result = si.consolidate_lessons(client)
                    return result, False
                if action == "quality_audit":
                    qm = getattr(self.memory, "quality_metrics", None)  # type: ignore[attr-defined]
                    if qm is None or not qm.available:
                        return "Qualitäts-Metriken nicht verfügbar.", True
                    client = getattr(self, "client", None)  # type: ignore[attr-defined]
                    return qm.audit_report(client=client), False
                if action == "staleness_review":
                    ks = getattr(self.memory, "knowledge_staleness", None)  # type: ignore[attr-defined]
                    lt = getattr(self.memory, "long_term", None)  # type: ignore[attr-defined]
                    client = getattr(self, "client", None)  # type: ignore[attr-defined]
                    if ks is None or lt is None or client is None:
                        return "Staleness-Review nicht verfügbar.", True
                    return ks.run_weekly_review(lt, client), False
                if action == "topic_map":
                    tg = getattr(self.memory, "topic_graph", None)  # type: ignore[attr-defined]
                    if tg is None or not tg.available:
                        return "Themen-Graph nicht verfügbar.", True
                    cmap = tg.cluster_map(limit=8)
                    nodes = cmap.get("nodes", [])
                    if not nodes:
                        return "Noch keine Themen im Graphen.", False
                    lines = []
                    for n in nodes:
                        line = f"• {n['tag']} ({n['count']}x)"
                        if n.get("strongest_link"):
                            line += f" ↔ {n['strongest_link']} ({n['link_weight']}x)"
                        lines.append(line)
                    total = cmap.get("total_nodes", len(nodes))
                    return (f"Top Themen ({total} gesamt):\n" + "\n".join(lines)), False
                if action == "compress_prompt":
                    from ..memory.prompt_compressor import PromptCompressor as _PC
                    client = getattr(self, "client", None)  # type: ignore[attr-defined]
                    if client is None:
                        return "Claude-Client nicht verfügbar.", True
                    pc = _PC()
                    si = getattr(self.memory, "self_improvement", None)  # type: ignore[attr-defined]
                    result = pc.run(self.memory.profile, si, client)  # type: ignore[attr-defined]
                    # Invalidate context cache so next turn uses compressed context.
                    try:
                        self.memory.context_builder.invalidate_cache()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    return result, False
                return f"Unbekannte action: {action}", True

            return f"Unbekanntes Productivity-Tool: {tool_name}", True
        except Exception as exc:  # noqa: BLE001
            return f"Productivity-Fehler: {exc}", True
