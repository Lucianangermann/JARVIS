"""KnowledgeExecMixin — learning-progress and task-tracking tool handlers.

Mixed into Brain. All self.* attributes are satisfied by Brain.__init__.
"""
from __future__ import annotations

from typing import Any


class KnowledgeExecMixin:
    """Exec methods for Lerntrack, task-progress notepad, and lazy getters
    for FlashcardManager and TriggerStore."""

    def _exec_track_learning(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Persistent learning-progress tracker. Lives in data/lerntrack.db —
        survives restarts and context resets."""
        from ..knowledge.lerntrack import LerntrackDB
        inp = tool_input or {}
        action = inp.get("action", "status")
        try:
            db = LerntrackDB()
        except Exception as exc:  # noqa: BLE001
            return f"Lerntrack nicht verfügbar: {exc}", True

        if action == "add":
            subjects = inp.get("subjects") or []
            group = inp.get("group", "")
            if not subjects:
                return "subjects (Liste) ist erforderlich.", True
            added = 0
            for s in subjects:
                if db.add(s, group=group):
                    added += 1
            skipped = len(subjects) - added
            msg = f"{added} Thema/Themen hinzugefügt"
            if skipped:
                msg += f" ({skipped} bereits vorhanden)"
            if group:
                msg += f" in Gruppe '{group}'"
            return msg + ".", False

        if action == "mark":
            subject = inp.get("subject", "")
            status = inp.get("status", "bearbeitet")
            if not subject:
                return "subject ist erforderlich.", True
            notes = inp.get("notes")
            ok = db.mark(subject, status, notes=notes)
            if not ok:
                return f"Thema '{subject}' nicht gefunden.", True
            msg = f"'{subject}' als '{status}' markiert."
            if status == "abgeschlossen":
                fc = self._get_flashcards()  # type: ignore[attr-defined]
                if fc is not None:
                    try:
                        text = subject
                        if notes:
                            text += f"\n{notes}"
                        ids = fc.generate_from_text(
                            text,
                            category=inp.get("group") or "lernziel",
                        )
                        if ids:
                            n = len(ids)
                            msg += (f" {n} Karteikarte{'n' if n != 1 else ''}"
                                    " automatisch generiert.")
                    except Exception:  # noqa: BLE001
                        pass
            return msg, False

        if action == "status":
            group = inp.get("group", "")
            return db.spoken_status(group), False

        if action == "list":
            group = inp.get("group", "")
            rows = db.list_group(group)
            if not rows:
                return "Keine Themen gespeichert.", False
            STATUS_ICON = {"offen": "○", "bearbeitet": "◑",
                           "abgeschlossen": "●"}
            lines = []
            cur_group = None
            for r in rows:
                g = r.get("subject_group", "")
                if g != cur_group:
                    if g:
                        lines.append(f"[{g}]")
                    cur_group = g
                icon = STATUS_ICON.get(r["status"], "?")
                lines.append(f"  {icon} {r['display_name']}")
            return "\n".join(lines), False

        if action == "delete":
            subject = inp.get("subject", "")
            if not subject:
                return "subject ist erforderlich.", True
            ok = db.delete(subject)
            return (f"'{subject}' gelöscht." if ok
                    else f"'{subject}' nicht gefunden."), not ok

        return f"Unbekannte action: {action}", True

    def _exec_track_task(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Persistent progress notepad for long multi-step tasks. Plain text
        files under ~/.jarvis/tasks/ so a context reset or restart never
        loses where JARVIS was."""
        import re as _re
        from pathlib import Path as _Path
        task_dir = _Path.home() / ".jarvis" / "tasks"
        inp = tool_input or {}
        action = inp.get("action", "load")
        try:
            task_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return f"Task-Ordner nicht verfügbar: {exc}", True

        def _safe(name: str) -> str:
            return _re.sub(r"[^a-z0-9_-]", "", (name or "").lower())[:60]

        if action == "list":
            files = sorted(task_dir.glob("*.txt"))
            if not files:
                return "Keine laufenden Aufgaben gespeichert.", False
            names = ", ".join(f.stem for f in files)
            return f"Laufende Aufgaben: {names}.", False

        name = _safe(inp.get("name", ""))
        if not name:
            return "name ist erforderlich (kurze Aufgaben-ID).", True
        path = task_dir / f"{name}.txt"

        if action == "save":
            progress = inp.get("progress", "")
            if not progress:
                return "progress ist erforderlich.", True
            import datetime as _dt
            stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            try:
                path.write_text(f"[{stamp}]\n{progress}", encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                return f"Speichern fehlgeschlagen: {exc}", True
            return f"Fortschritt für '{name}' gespeichert.", False

        if action == "load":
            if not path.is_file():
                return (f"Keine gespeicherte Aufgabe '{name}' — das ist neu.", False)
            try:
                return path.read_text(encoding="utf-8"), False
            except Exception as exc:  # noqa: BLE001
                return f"Laden fehlgeschlagen: {exc}", True

        if action == "done":
            try:
                if path.is_file():
                    path.unlink()
                return f"Aufgabe '{name}' als erledigt markiert.", False
            except Exception as exc:  # noqa: BLE001
                return f"Konnte nicht abschließen: {exc}", True

        return f"Unbekannte action: {action}", True

    def _exec_extract_lernziele(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Extract Lernziele / topics from text or a file using Claude Haiku.

        Optional ``save=true`` imports them directly into lerntrack."""
        inp = tool_input or {}
        text = inp.get("text", "").strip()
        file_path = inp.get("file_path", "").strip()

        # Load text from file if text not provided inline.
        if not text and file_path:
            from pathlib import Path as _Path
            try:
                p = _Path(file_path)
                if not p.is_file():
                    return f"Datei nicht gefunden: {file_path}", True
                text = p.read_text(encoding="utf-8", errors="replace")[:6000]
            except Exception as exc:  # noqa: BLE001
                return f"Datei lesen fehlgeschlagen: {exc}", True

        if not text:
            return "text oder file_path ist erforderlich.", True

        client = self.client  # type: ignore[attr-defined]
        if client is None:
            return "Claude-Client nicht verfügbar.", True

        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{
                    "role": "user",
                    "content": (
                        "Extrahiere aus dem folgenden Text alle Lernziele, Themen "
                        "oder Kernaussagen, die man lernen/verstehen sollte. "
                        "Antworte AUSSCHLIESSLICH als JSON-Array von Strings, "
                        "ohne Markdown, ohne Erklärung. Maximal 10 Einträge. "
                        'Beispiel: ["Ohmsches Gesetz", "Kirchhoffsche Regeln"]\n\n'
                        + text[:4000]
                    ),
                }],
            )
            import json as _json
            raw = ""
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    raw = block.text.strip()
                    break
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            topics: list[str] = _json.loads(raw)
            if not isinstance(topics, list):
                return "Extraktion lieferte kein Array.", True
            topics = [str(t).strip() for t in topics if str(t).strip()][:10]
        except Exception as exc:  # noqa: BLE001
            return f"Extraktion fehlgeschlagen: {exc}", True

        if not topics:
            return "Keine Lernziele im Text gefunden.", False

        save = bool(inp.get("save", False))
        group = str(inp.get("group") or "")

        if save:
            from ..knowledge.lerntrack import LerntrackDB as _LT
            db = _LT()
            added = 0
            for t in topics:
                if db.add(t, group=group):
                    added += 1
            msg = f"{len(topics)} Lernziele gefunden, {added} neu in lerntrack gespeichert"
            if group:
                msg += f" (Gruppe: {group})"
            return msg + ": " + ", ".join(topics) + ".", False

        joined = ", ".join(f'"{t}"' for t in topics)
        return (
            f"{len(topics)} Lernziele gefunden: {joined}. "
            "Soll ich sie in lerntrack speichern? "
            "Falls ja, wiederhole den Befehl mit save=true."
        ), False

    def _get_flashcards(self) -> Any:
        """Lazily build the flashcard manager (Second Brain SRS). Shares the
        brain's Claude client for card generation."""
        fc = getattr(self, "_flashcards", None)
        if fc is None:
            try:
                from pathlib import Path as _Path
                from ..knowledge import FlashcardManager as _FM
                _db = _Path(__file__).resolve().parents[2] / "data" / "knowledge.db"
                fc = _FM(_db, client=self.client)  # type: ignore[attr-defined]
                self._flashcards = fc  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                print(f"[Brain] flashcards init failed: {exc}")
                self._flashcards = None  # type: ignore[attr-defined]
        return self._flashcards  # type: ignore[attr-defined]

    def _get_triggers(self) -> Any:
        """Lazy deferred-action store. main.py wires a real one with a
        NotificationCenter sink + a running checker; the lazy fallback
        (tests/standalone) prints when a trigger fires."""
        trg = getattr(self, "_triggers", None)
        if trg is None:
            try:
                from pathlib import Path as _Path
                from ..intelligence.triggers import TriggerStore
                _db = _Path(__file__).resolve().parents[2] / "data" / "triggers.db"
                trg = TriggerStore(_db)
                self._triggers = trg  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                print(f"[Brain] triggers init failed: {exc}")
                self._triggers = None  # type: ignore[attr-defined]
        return self._triggers  # type: ignore[attr-defined]
