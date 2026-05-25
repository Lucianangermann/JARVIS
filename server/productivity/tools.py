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
                "Save a note, idea, learning, or decision to JARVIS memory. "
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
    ]
