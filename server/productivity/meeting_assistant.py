"""Meeting assistant — record, transcribe, summarise, extract action items.

The valuable, deterministic core is :meth:`process_transcript`: given a
meeting transcript it asks Claude for a summary plus a list of action
items, turns each action item into a task (reusing TaskManager), and
saves the summary as an Apple Note. That path is fully testable and works
on any transcript — live mic recording or pasted text.

Live recording (:meth:`start_recording` / :meth:`stop_recording`) is
best-effort: it captures the mic in chunks via sounddevice and transcribes
each with the existing STT, appending to a transcript buffer. If the voice
stack isn't installed it degrades to a clear "recording unavailable"
instead of failing — and the process path still works on a supplied
transcript.

Everything is best-effort; a failure returns a safe default rather than
raising, so the meeting flow never crashes JARVIS.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from ..config import settings

_SAMPLE_RATE = 16_000
_CHUNK_SECONDS = 15.0  # transcribe in 15s chunks to bound memory + latency

_SUMMARY_PROMPT = (
    "You are a meeting assistant. Given the transcript below, return ONLY "
    "JSON (no prose) of the form:\n"
    '{"summary": "2-3 sentence German summary", '
    '"action_items": ["actionable task in German", ...], '
    '"decisions": ["key decision in German", ...]}\n'
    "Action items must be concrete tasks someone has to do. If there are "
    "none, return an empty list.\n\nTranscript:\n"
)


class MeetingAssistant:
    def __init__(self, task_manager: Any = None, client: Any = None) -> None:
        self._tasks = task_manager
        self._client = client
        self._recording = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._buffer: list[str] = []
        self._started_at: float | None = None
        self._title: str = "Meeting"

    # ── state ──────────────────────────────────────────────────────────── #

    def is_recording(self) -> bool:
        return self._recording

    @property
    def transcript(self) -> str:
        return " ".join(self._buffer).strip()

    # ── recording (best-effort) ────────────────────────────────────────── #

    def start_recording(self, title: str = "Meeting") -> dict[str, Any]:
        if self._recording:
            return {"ok": False, "error": "Es läuft bereits eine Aufnahme."}
        try:
            import sounddevice  # noqa: F401
            from .. import stt  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return {"ok": False,
                    "error": f"Aufnahme nicht verfügbar (Voice-Stack fehlt): {exc}"}
        self._title = title
        self._buffer = []
        self._started_at = time.time()
        self._recording = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._record_loop, name="jarvis-meeting", daemon=True)
        self._thread.start()
        print(f"[Meeting] recording started: {title}")
        return {"ok": True, "spoken": f"Meeting-Aufnahme gestartet: {title}."}

    def _record_loop(self) -> None:
        try:
            import numpy as np
            import sounddevice as sd
            from .. import stt
        except Exception as exc:  # noqa: BLE001
            print(f"[Meeting] record loop aborted: {exc}")
            self._recording = False
            return
        frames = int(_CHUNK_SECONDS * _SAMPLE_RATE)
        while not self._stop.is_set():
            try:
                rec = sd.rec(frames, samplerate=_SAMPLE_RATE, channels=1,
                             dtype="int16")
                sd.wait()
                if self._stop.is_set():
                    break
                pcm = rec.astype(np.int16).tobytes()
                wav = stt._pcm_to_wav(pcm, sample_rate=_SAMPLE_RATE)  # noqa: SLF001
                text = stt.transcribe(wav, sample_rate=_SAMPLE_RATE)
                if text and not stt.looks_like_hallucination(text):
                    self._buffer.append(text)
            except Exception as exc:  # noqa: BLE001
                print(f"[Meeting] chunk failed: {exc}")
                if self._stop.wait(0.5):
                    break

    def stop_recording(self) -> str:
        self._stop.set()
        self._recording = False
        if self._thread is not None:
            # Short join so "beende das Meeting" returns promptly — the
            # in-flight chunk (still inside a blocking sd.wait) is dropped;
            # all completed chunks are already in the buffer. The daemon
            # thread exits on its own once the current capture returns.
            self._thread.join(timeout=2.0)
            self._thread = None
        return self.transcript

    # ── summarise + extract (the testable core) ────────────────────────── #

    async def process_transcript(self, transcript: str,
                                 title: str | None = None) -> dict[str, Any]:
        title = title or self._title
        if not transcript.strip():
            return {"ok": False, "spoken": "Kein Transkript vorhanden."}
        data = self._summarise(transcript)
        summary = data.get("summary") or "Keine Zusammenfassung verfügbar."
        action_items = [a for a in data.get("action_items", []) if a]
        decisions = data.get("decisions", [])

        # Each action item becomes a task.
        created = 0
        if self._tasks is not None:
            for item in action_items:
                try:
                    if self._tasks.add_task(item, priority=2, context="work",
                                            tags="meeting"):
                        created += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[Meeting] add_task failed: {exc}")

        # Save the summary as an Apple Note (best-effort).
        self._save_note(title, summary, action_items, decisions, transcript)

        spoken = self._spoken(summary, created, action_items)
        return {"ok": True, "summary": summary, "action_items": action_items,
                "decisions": decisions, "tasks_created": created,
                "spoken": spoken}

    async def end_meeting(self, title: str | None = None) -> dict[str, Any]:
        """Stop recording (if active) and process the transcript."""
        transcript = self.stop_recording() if self._recording else self.transcript
        if not transcript:
            return {"ok": False,
                    "spoken": "Kein Meeting aktiv oder kein Transkript."}
        return await self.process_transcript(transcript, title)

    def _summarise(self, transcript: str) -> dict[str, Any]:
        if self._client is None:
            return {"summary": "", "action_items": [], "decisions": []}
        try:
            resp = self._client.messages.create(
                model=settings.MODEL, max_tokens=1024,
                messages=[{"role": "user",
                           "content": _SUMMARY_PROMPT + transcript[:8000]}])
            raw = ""
            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    raw = (b.text or "").strip()
                    break
            return self._parse_json(raw)
        except Exception as exc:  # noqa: BLE001
            print(f"[Meeting] summarise failed: {exc}")
            return {"summary": "", "action_items": [], "decisions": []}

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if "```" in text:
            for part in text.split("```"):
                p = part.strip()
                if p.startswith("{") or p.startswith("json"):
                    text = p[4:].strip() if p.startswith("json") else p
                    break
        if not text.startswith("{"):
            i, j = text.find("{"), text.rfind("}")
            if i != -1 and j != -1:
                text = text[i:j + 1]
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {"summary": raw[:300], "action_items": [], "decisions": []}

    def _save_note(self, title: str, summary: str, items: list[str],
                   decisions: list[str], transcript: str) -> None:
        try:
            from ..tools import notes_tool
            ts = time.strftime("%Y-%m-%d %H:%M")
            body_lines = [f"Meeting: {title} ({ts})", "", "Zusammenfassung:",
                          summary, ""]
            if decisions:
                body_lines += ["Entscheidungen:"] + [f"- {d}" for d in decisions] + [""]
            if items:
                body_lines += ["Action Items:"] + [f"- {a}" for a in items] + [""]
            body_lines += ["Transkript:", transcript[:4000]]
            notes_tool.create_note(f"Meeting: {title} {ts}", "\n".join(body_lines))
        except Exception as exc:  # noqa: BLE001
            print(f"[Meeting] note save failed: {exc}")

    @staticmethod
    def _spoken(summary: str, created: int, items: list[str]) -> str:
        parts = [f"Meeting zusammengefasst. {summary}"]
        if created:
            parts.append(f"{created} Action Item{'s' if created != 1 else ''} "
                         f"als Tasks angelegt.")
        elif not items:
            parts.append("Keine Action Items.")
        return " ".join(parts)
