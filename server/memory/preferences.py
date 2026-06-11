"""User response preferences that shape EVERY reply.

The memory layer stores facts; this stores *how the user wants JARVIS to
respond* — length, tone, language — and renders them into a system-prompt
block so every turn adapts. Set explicitly ("antworte kürzer", "sei
förmlicher", "antworte auf englisch") and persisted to
``data/preferences.json``. A process-wide singleton both the brain and the
context builder read.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DEFAULTS: dict[str, str] = {
    "length": "normal",     # kurz | normal | ausführlich
    "tone": "normal",       # locker | normal | förmlich
    "language": "auto",     # auto | de | en
}

_LENGTH_TEXT = {
    "kurz": "Antworte sehr knapp, 1–2 Sätze.",
    "ausführlich": "Antworte ausführlich und mit Details.",
}
_TONE_TEXT = {
    "locker": "Sprich locker und freundschaftlich, duze den Nutzer.",
    "förmlich": "Sprich förmlich und sieze den Nutzer.",
}
_LANG_TEXT = {
    "de": "Antworte immer auf Deutsch.",
    "en": "Always answer in English.",
}


class Preferences:
    def __init__(self, path: Path | str = "data/preferences.json") -> None:
        self._path = Path(path)
        self._prefs: dict[str, str] = dict(_DEFAULTS)
        self._load()

    def _load(self) -> None:
        if self._path.is_file():
            try:
                data = json.loads(self._path.read_text("utf-8"))
                if isinstance(data, dict):
                    self._prefs.update({k: v for k, v in data.items()
                                        if k in _DEFAULTS})
            except Exception as exc:  # noqa: BLE001
                print(f"[Preferences] load failed: {exc}")

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._prefs, ensure_ascii=False, indent=2), "utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"[Preferences] save failed: {exc}")

    def set(self, key: str, value: str) -> bool:
        if key not in _DEFAULTS:
            return False
        self._prefs[key] = value
        self._save()
        return True

    def get(self, key: str) -> str:
        return self._prefs.get(key, _DEFAULTS.get(key, ""))

    def all(self) -> dict[str, str]:
        return dict(self._prefs)

    def as_prompt_block(self) -> str:
        """Render the non-default preferences into a system-prompt snippet,
        or '' when everything is at the default (don't bloat the prompt)."""
        lines: list[str] = []
        if (t := _LENGTH_TEXT.get(self._prefs["length"])):
            lines.append("- " + t)
        if (t := _TONE_TEXT.get(self._prefs["tone"])):
            lines.append("- " + t)
        if (t := _LANG_TEXT.get(self._prefs["language"])):
            lines.append("- " + t)
        if not lines:
            return ""
        return "## Antwort-Präferenzen des Nutzers (beachten)\n" + "\n".join(lines)


# Process-wide singleton.
preferences = Preferences()
