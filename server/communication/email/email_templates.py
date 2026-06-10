"""Email template storage + variable filling.

Built-in templates ship in code; user templates persist to
``data/email_templates.json``. Filling uses a forgiving formatter so a
missing variable leaves its ``{placeholder}`` intact instead of raising —
a half-filled draft is more useful (and safer) than a crash mid-send.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BUILT_IN_TEMPLATES: dict[str, dict[str, str]] = {
    "meeting_request": {
        "subject": "Meeting Request — {topic}",
        "body": ("Hi {name},\n\nI'd like to schedule a meeting about {topic}. "
                 "Would {date} work for you?\n\nBest regards"),
    },
    "follow_up": {
        "subject": "Follow-up: {original_subject}",
        "body": ("Hi {name},\n\nJust following up on my previous email. "
                 "Let me know if you have any questions.\n\nBest regards"),
    },
    "out_of_office": {
        "subject": "Out of Office: {dates}",
        "body": ("I am currently out of office from {start} to {end}. "
                 "I will respond to your message when I return.\n\nBest regards"),
    },
    "danke": {
        "subject": "Vielen Dank",
        "body": ("Hallo {name},\n\nVielen Dank für {reason}. "
                 "Ich melde mich bald wieder.\n\nViele Grüße"),
    },
}


class _SafeDict(dict):
    """Leaves unknown {placeholders} untouched instead of raising."""
    def __missing__(self, key: str) -> str:  # noqa: D401
        return "{" + key + "}"


class EmailTemplateManager:
    def __init__(self, path: Path | str = "data/email_templates.json") -> None:
        self._path = Path(path)
        self._user: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.is_file():
            try:
                self._user = json.loads(self._path.read_text("utf-8"))
            except Exception as exc:  # noqa: BLE001
                print(f"[EmailTemplates] load failed: {exc}")
                self._user = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._user, ensure_ascii=False, indent=2), "utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"[EmailTemplates] save failed: {exc}")

    def all_names(self) -> list[str]:
        return sorted(set(BUILT_IN_TEMPLATES) | set(self._user))

    def get(self, name: str) -> dict[str, str] | None:
        # User templates override built-ins of the same name.
        return self._user.get(name) or BUILT_IN_TEMPLATES.get(name)

    def save_template(self, name: str, subject: str, body: str) -> dict[str, Any]:
        self._user[name] = {"subject": subject, "body": body}
        self._save()
        return {"ok": True, "name": name}

    def delete_template(self, name: str) -> bool:
        if name in self._user:
            del self._user[name]
            self._save()
            return True
        return False

    def fill(self, name: str, variables: dict[str, str]) -> dict[str, str] | None:
        """Return {subject, body} with variables substituted, or None if
        the template doesn't exist."""
        tpl = self.get(name)
        if tpl is None:
            return None
        safe = _SafeDict(variables or {})
        return {
            "subject": tpl["subject"].format_map(safe),
            "body": tpl["body"].format_map(safe),
        }
