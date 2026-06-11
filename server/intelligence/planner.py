"""Agentic multi-step planning.

Where the morning *briefing* concatenates sections, the planner *gathers*
facts from many layers (calendar, tasks, weather, finance, traffic,
security) and then asks Claude to **synthesise one short, prioritised,
actionable plan**. That's the step from a status read-out to an assistant
that reasons over your day.

Every source is best-effort and independent — a failed weather lookup or a
missing manager just drops that fact; the plan is still produced from
whatever was gathered. The synthesis call reuses the brain's Claude client
(no extra dependency).
"""
from __future__ import annotations

from typing import Any

from ..config import settings


class Planner:
    def __init__(self, client: Any = None, productivity: Any = None,
                 finance: Any = None, security: Any = None) -> None:
        self._client = client
        self._productivity = productivity
        self._finance = finance
        self._security = security

    # ── fact gathering (each best-effort) ──────────────────────────────── #

    def _calendar_facts(self) -> list[str]:
        try:
            from ..tools import calendar_tool
            events = calendar_tool.get_today_events()
            if not events:
                return ["Heute keine Kalendertermine."]
            parts = []
            for e in events[:5]:
                when = e.start.strftime("%H:%M") if hasattr(e, "start") else ""
                parts.append(f"{when} {getattr(e, 'title', '')}".strip())
            return ["Termine heute: " + "; ".join(parts) + "."]
        except Exception:  # noqa: BLE001
            return []

    def _weather_facts(self) -> list[str]:
        try:
            from ..tools import weather
            w = weather.get_current()
            if w is None:
                return []
            return [f"Wetter: {getattr(w, 'condition', '')}, "
                    f"{getattr(w, 'temperature', '?')} Grad."]
        except Exception:  # noqa: BLE001
            return []

    def _task_facts(self) -> list[str]:
        if self._productivity is None:
            return []
        try:
            top = self._productivity.tasks.get_top3()
            overdue = self._productivity.tasks.get_overdue()
            facts = []
            if top:
                facts.append("Top-Aufgaben: "
                             + ", ".join(t["title"] for t in top) + ".")
            if overdue:
                facts.append(f"{len(overdue)} überfällige Aufgaben.")
            return facts or ["Keine offenen Top-Aufgaben."]
        except Exception:  # noqa: BLE001
            return []

    def _finance_facts(self) -> list[str]:
        if self._finance is None:
            return []
        try:
            total = self._finance.expenses.month_total()
            over = [b for b in self._finance.expenses.budget_status() if b["over"]]
            facts = [f"Ausgaben diesen Monat: {total:.0f} Euro."]
            if over:
                facts.append("Überschrittene Budgets: "
                             + ", ".join(b["category"] for b in over) + ".")
            return facts
        except Exception:  # noqa: BLE001
            return []

    def _gather(self) -> list[str]:
        facts: list[str] = []
        for fn in (self._calendar_facts, self._weather_facts,
                   self._task_facts, self._finance_facts):
            try:
                facts.extend(fn())
            except Exception:  # noqa: BLE001
                pass
        return facts

    def _synthesise(self, facts: list[str], instruction: str) -> str:
        if not facts:
            return "Mir fehlen gerade die Infos für einen Plan."
        if self._client is None:
            # No model — fall back to a plain read-out of the facts.
            return " ".join(facts)
        prompt = (
            "Du bist JARVIS, ein knapper Assistent. Hier sind die Fakten zum "
            f"Tag des Nutzers:\n- " + "\n- ".join(facts) + "\n\n" + instruction
            + " Antworte auf Deutsch, gesprochener Ton, kein Markdown, "
            "höchstens 4 Sätze.")
        try:
            resp = self._client.messages.create(
                model=settings.MODEL, max_tokens=400,
                messages=[{"role": "user", "content": prompt}])
            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    return (b.text or "").strip() or " ".join(facts)
        except Exception as exc:  # noqa: BLE001
            print(f"[Planner] synthesis failed: {exc}")
        return " ".join(facts)

    # ── public plans ───────────────────────────────────────────────────── #

    async def plan_day(self) -> str:
        facts = self._gather()
        return self._synthesise(
            facts,
            "Erstelle einen kurzen, priorisierten, umsetzbaren Tagesplan: "
            "was zuerst, worauf achten.")

    async def prepare_to_leave(self) -> str:
        """Action-oriented: leaving checklist + weather + travel to the next
        appointment, then offer to arm the alarm."""
        parts: list[str] = []
        if self._security is not None and getattr(self._security, "home", None):
            try:
                parts.append(await self._security.home.leaving_checklist())
            except Exception:  # noqa: BLE001
                pass
        parts.extend(self._weather_facts())
        # Travel time to the next event's location, if any.
        try:
            from ..tools import calendar_tool, traffic
            nxt = calendar_tool.get_next_event()
            loc = getattr(nxt, "location", "") if nxt else ""
            if loc:
                tt = traffic.get_travel_time(settings.HOME_ADDRESS or "", loc)
                if tt:
                    parts.append(f"Fahrzeit zu {getattr(nxt, 'title', 'Termin')}: {tt}.")
        except Exception:  # noqa: BLE001
            pass
        if self._security is not None and getattr(self._security, "home", None) \
                and not self._security.home.is_armed:
            parts.append("Soll ich den Alarm aktivieren?")
        return " ".join(p for p in parts if p) or "Alles bereit — gute Fahrt!"
