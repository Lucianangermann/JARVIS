"""Adaptive prompt compression — keeps system prompt tokens lean.

Two compression targets:

1. Profile known_facts: over time, the profile accumulates near-duplicate
   facts ("User lives in Berlin", "User is based in Berlin", "Berlin").
   Haiku merges them into a compact, unique list.

2. Self-improvement lessons: delegates to the existing consolidate_lessons()
   which was already implemented in SelfImprovementDB.

Both are best-effort: failures return an informative message, never raise.
Intended to be run weekly (via weekly_summary / self_reflect action).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .profile_manager import ProfileManager
    from .self_improvement import SelfImprovementDB

_FACTS_PROMPT = """\
Diese Fakten sind über den Nutzer in JARVIS gespeichert:
{facts}

Aufgabe: Dedupliziere und komprimiere diese Liste.
Behalte ALLE einzigartigen Informationen, aber fasse ähnliche Aussagen zusammen.
Antworte NUR mit der komprimierten Liste (ein Eintrag pro Zeile, kein Präfix wie "-" oder "•")."""


class PromptCompressor:
    """Stateless compressor — no own DB, operates on passed-in objects."""

    # ── profile facts ────────────────────────────────────────────────── #

    def compress_profile_facts(
        self, profile: "ProfileManager", client: Any,
    ) -> str:
        """Merge redundant known_facts via Haiku. Returns spoken summary."""
        if not profile.available:
            return "Profil nicht verfügbar."
        facts: list[str] = (
            profile.get().get("context", {}).get("known_facts", [])
        )
        if len(facts) < 5:
            return f"Nur {len(facts)} Fakten — noch nicht genug zum Komprimieren."

        prompt = _FACTS_PROMPT.format(
            facts="\n".join(f"- {f}" for f in facts)
        )
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            merged_text = ""
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    merged_text = (block.text or "").strip()

            if not merged_text:
                return "Haiku hat keine Antwort geliefert."

            new_facts = [
                line.strip()
                for line in merged_text.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ][:20]

            if not new_facts:
                return "Komprimierung ergab eine leere Liste — übersprungen."

            original = len(facts)
            with profile._lock:
                ctx = profile._profile.setdefault("context", {})
                ctx["known_facts"] = new_facts
                profile._save()

            saved = len(new_facts)
            delta = original - saved
            return (
                f"{original} Fakten → {saved} komprimiert"
                + (f" (−{delta} Duplikate entfernt)." if delta > 0 else " (keine Duplikate gefunden).")
            )
        except Exception as exc:
            return f"Profil-Komprimierung fehlgeschlagen: {exc}"

    # ── lessons ──────────────────────────────────────────────────────── #

    def compress_lessons(self, si: "SelfImprovementDB", client: Any) -> str:
        """Delegates to the existing consolidate_lessons routine."""
        if not si.available:
            return "Self-improvement nicht verfügbar."
        try:
            return si.consolidate_lessons(client)
        except Exception as exc:
            return f"Regel-Komprimierung fehlgeschlagen: {exc}"

    # ── combined ─────────────────────────────────────────────────────── #

    def run(
        self,
        profile: "ProfileManager",
        si: "SelfImprovementDB",
        client: Any,
    ) -> str:
        """Run both compressions, return combined spoken summary."""
        facts_result = self.compress_profile_facts(profile, client)
        lessons_result = self.compress_lessons(si, client)
        return f"Profil: {facts_result}\nRegeln: {lessons_result}"
