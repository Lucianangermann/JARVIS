"""Daily learning curriculum generator.

Combines lerntrack open subjects + flashcard due count into a
prioritised study plan. Falls back to a rule-based plan when no
Claude client is available.
"""
from __future__ import annotations

from typing import Any


def generate_curriculum(
    *,
    lerntrack: Any = None,
    flashcard_manager: Any = None,
    available_minutes: int = 60,
    client: Any = None,
) -> str:
    """Return a spoken daily learning plan string.

    Priority order: fällige Karteikarten first (Spaced Repetition),
    then 'bearbeitet' subjects (in progress), then 'offen' subjects.
    """
    due_cards = 0
    if flashcard_manager is not None:
        try:
            due_cards = flashcard_manager.due_count()
        except Exception:
            pass

    all_subjects: list[dict] = []
    if lerntrack is not None:
        try:
            in_progress = lerntrack.list_group(status="bearbeitet")
            open_subs = lerntrack.list_group(status="offen")
            all_subjects = in_progress + open_subs
        except Exception:
            pass

    if not due_cards and not all_subjects:
        return ("Kein Lernmaterial vorhanden. "
                "Füge Lernziele via track_learning oder Karteikarten via flashcards hinzu.")

    if client is None:
        return _rule_based(due_cards, all_subjects, available_minutes)

    context_lines: list[str] = [f"Verfügbare Lernzeit: {available_minutes} Minuten."]
    if due_cards:
        context_lines.append(f"Fällige Karteikarten: {due_cards} (Spaced Repetition!).")
    for s in all_subjects[:6]:
        context_lines.append(
            f"Lernziel '{s['display_name']}' — Status: {s['status']}"
            + (f", Gruppe: {s['subject_group']}" if s.get("subject_group") else "")
            + "."
        )

    prompt = (
        "Erstelle einen konkreten Lernplan für heute auf Deutsch in 2-3 Sätzen. "
        "Nenne konkrete Zeitblöcke (z.B. '20min Statistik, dann 15min Karteikarten'). "
        "Priorisiere Karteikarten wegen Spaced Repetition, dann überfällige Themen. "
        "Kein Markdown, kein Bullet-Format — nur Fließtext.\n\n"
        + "\n".join(context_lines)
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=130,
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "text" and block.text:
                return block.text.strip()
    except Exception as exc:
        print(f"[Curriculum] LLM failed: {exc}")

    return _rule_based(due_cards, all_subjects, available_minutes)


def _rule_based(
    due_cards: int,
    subjects: list[dict],
    available_minutes: int,
) -> str:
    time_left = available_minutes
    plan: list[str] = []

    if due_cards:
        card_time = min(15, time_left)
        plural = "n" if due_cards != 1 else ""
        plan.append(f"{card_time}min Karteikarten ({due_cards} fällig{plural})")
        time_left -= card_time

    for s in subjects[:3]:
        if time_left <= 0:
            break
        block = min(20, time_left)
        plan.append(f"{block}min {s['display_name']}")
        time_left -= block

    if not plan:
        return "Kein Lernplan für heute möglich."
    used = available_minutes - time_left
    return "Lernplan: " + ", dann ".join(plan) + f" — gesamt ~{used}min."
