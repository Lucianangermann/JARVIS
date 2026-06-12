"""Claude tool_use definitions for brain.py productivity integration."""
from __future__ import annotations

from typing import Any


def productivity_tools() -> list[dict[str, Any]]:
    """Return list of tool schemas for Claude."""
    return [
        {
            "name": "manage_tasks",
            "description": (
                "Add, list, complete tasks and manage projects. "
                "Use action='add' to create a new task, "
                "'list_today' to see today's tasks, "
                "'top3' for the three most important tasks, "
                "'complete' to mark a task done by id, "
                "'project_status' to see a project's progress, "
                "'list_overdue' to see overdue tasks."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "list_today", "top3", "complete",
                                 "project_status", "list_overdue"],
                        "description": "The operation to perform.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Task title (for action='add').",
                    },
                    "priority": {
                        "type": "integer",
                        "description": (
                            "Priority 1=urgent+important, 2=important, "
                            "3=urgent, 4=neither. Default 2."
                        ),
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Due date in YYYY-MM-DD format.",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project name (for add or project_status).",
                    },
                    "task_id": {
                        "type": "integer",
                        "description": "Task ID (for action='complete').",
                    },
                    "context": {
                        "type": "string",
                        "description": "work / personal / errand.",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "manage_focus",
            "description": (
                "Start/stop a Pomodoro timer or a manual time tracker. "
                "action='start_pomodoro' begins a 25-min focus session. "
                "action='stop_pomodoro' cancels it early. "
                "action='start_timer' logs time to a project. "
                "action='stop_timer' ends the running timer. "
                "action='time_today' returns today's tracked time summary."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start_pomodoro", "stop_pomodoro",
                                 "start_timer", "stop_timer", "time_today"],
                    },
                    "task": {
                        "type": "string",
                        "description": "Task name for the Pomodoro session.",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project name for time tracking.",
                    },
                    "minutes": {
                        "type": "integer",
                        "description": "Pomodoro duration in minutes (default 25).",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_productivity_score",
            "description": (
                "Get the user's productivity score and insights. "
                "period='today' returns today's score (0–10), tasks done, "
                "and focus minutes. period='week' returns a weekly summary."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["today", "week"],
                        "description": "Time period to report on.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "add_knowledge_note",
            "description": (
                "Save a note, idea, learning, fact, or decision to JARVIS's "
                "long-term knowledge (semantic memory). Use this whenever the "
                "user says 'merk dir …' / 'remember …' / 'speichere …'. "
                "category: idea / learning / reference / decision."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The text content to save.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["idea", "learning", "reference", "decision"],
                        "description": "Category tag for the note.",
                    },
                },
                "required": ["content"],
                "additionalProperties": False,
            },
        },
        {
            "name": "recall_knowledge",
            "description": (
                "Semantic-search JARVIS's long-term knowledge for what the "
                "user previously asked to remember. Use for 'was weiß ich "
                "über …' / 'woran wolltest du mich erinnern' / 'what do I "
                "know about …'. Returns the most relevant saved notes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look up in saved knowledge.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["idea", "learning", "reference", "decision"],
                        "description": "Optional: restrict to a category.",
                    },
                    "n": {
                        "type": "integer",
                        "description": "Max results (default 5).",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "flashcards",
            "description": (
                "Spaced-repetition flashcards (Second Brain). Actions: "
                "'add' (front+back) to create a card; 'due' to report how "
                "many cards are due; 'next' to get the next due card's "
                "question (returns front + card_id); 'reveal' (card_id) to "
                "get the answer; 'grade' (card_id + feedback like richtig/"
                "falsch/einfach/schwer) to schedule the next review; "
                "'generate' (text) to auto-create cards from a topic; "
                "'stats' for totals. Use for 'erstelle eine Karteikarte', "
                "'quiz mich', 'fällige Karten'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "due", "next", "reveal", "grade",
                                 "generate", "stats"],
                    },
                    "front": {"type": "string", "description": "Question (for add)."},
                    "back": {"type": "string", "description": "Answer (for add)."},
                    "category": {"type": "string"},
                    "card_id": {"type": "integer",
                                "description": "Card id (reveal/grade)."},
                    "feedback": {"type": "string",
                                 "description": "Self-grade for 'grade': "
                                                "richtig/falsch/einfach/schwer."},
                    "text": {"type": "string",
                             "description": "Topic/text for 'generate'."},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "schedule_action",
            "description": (
                "Schedule a deferred reminder that JARVIS will SPEAK ALOUD at "
                "a future time — for 'erinnere mich in 2 Stunden an X', 'um 18 "
                "Uhr Y', 'morgen um 9 Z'. YOU compute the timing: pass "
                "delay_minutes (minutes from now) OR at ('HH:MM') optionally "
                "combined with date ('morgen', 'übermorgen', or 'YYYY-MM-DD'). "
                "Without a date, a past 'HH:MM' rolls to tomorrow. "
                "action='schedule' (default) needs message; 'list' shows "
                "pending; 'cancel' needs id."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["schedule", "list", "cancel"]},
                    "message": {"type": "string",
                                "description": "What to remind about."},
                    "delay_minutes": {"type": "integer",
                                      "description": "Minutes from now."},
                    "at": {"type": "string",
                           "description": "Clock time 'HH:MM'."},
                    "date": {"type": "string",
                             "description": "Optional date for 'at': 'morgen', "
                                            "'übermorgen', or 'YYYY-MM-DD'."},
                    "id": {"type": "integer", "description": "Trigger id (cancel)."},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_email_smart_summary",
            "description": (
                "Get a smart summary of unread emails from Apple Mail. "
                "Filters for important messages and summarises them in German."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "meeting_control",
            "description": (
                "Record and process meetings. action='start' begins "
                "recording the meeting from the microphone; action='stop' "
                "ends it, transcribes, summarises via AI, turns action items "
                "into tasks, and saves a note; action='status' reports "
                "whether a recording is running; action='summarize' "
                "processes a transcript passed in 'transcript' without "
                "recording. Use 'start'/'stop' for phrases like 'nimm das "
                "Meeting auf' / 'beende das Meeting'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start", "stop", "status", "summarize"],
                        "description": "The operation to perform.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Meeting title (for start/stop).",
                    },
                    "transcript": {
                        "type": "string",
                        "description": "Transcript text (for action='summarize').",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    ]
