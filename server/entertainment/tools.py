"""Claude tool_use schemas for the entertainment layer."""
from __future__ import annotations

from typing import Any


def entertainment_tools() -> list[dict[str, Any]]:
    """Return list of tool schemas for brain.py."""
    return [
        {
            "name": "play_mood_music",
            "description": (
                "Play music matching a mood or activity. "
                "Uses Spotify (if configured) or Apple Music. "
                "Moods: entspannt, konzentriert, glücklich, traurig, sport, "
                "party, schlafen, romantisch, morgen, gaming."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "mood": {
                        "type": "string",
                        "description": (
                            "The mood or activity, e.g. 'entspannt', 'konzentriert', "
                            "'sport', 'party', 'schlafen', 'romantisch', 'morgen', 'gaming'."
                        ),
                    },
                },
                "required": ["mood"],
                "additionalProperties": False,
            },
        },
        {
            "name": "manage_watchlist",
            "description": (
                "Manage the movie and TV show watchlist. "
                "action='add' adds a title, 'list' shows the watchlist, "
                "'mark_watched' marks something as seen, "
                "'what_to_watch' suggests something to watch."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "list", "mark_watched", "what_to_watch"],
                        "description": "The watchlist operation to perform.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Movie or show title (for add / mark_watched).",
                    },
                    "type": {
                        "type": "string",
                        "description": "Media type: movie or show.",
                    },
                    "rating": {
                        "type": "integer",
                        "description": "Rating 1-10 (for mark_watched).",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "play_game",
            "description": (
                "Start or interact with a voice game. "
                "action='joke' tells a joke, 'riddle' gives a riddle, "
                "'fact' shares an interesting fact, 'story' starts a story, "
                "'trivia_start' begins a trivia game, "
                "'twenty_questions' plays 20 questions, "
                "'stop_game' ends the current game, "
                "'answer' submits an answer to the active game."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "joke", "riddle", "riddle_answer", "fact", "story",
                            "trivia_start", "twenty_questions", "stop_game", "answer",
                        ],
                        "description": "The game action to perform.",
                    },
                    "text": {
                        "type": "string",
                        "description": "User answer for ongoing games, or topic for fact.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category for trivia or joke.",
                    },
                    "difficulty": {
                        "type": "string",
                        "description": "Difficulty for trivia: leicht, mittel, schwer.",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "manage_gaming_mode",
            "description": (
                "Track gaming sessions and optionally adjust smart home lighting. "
                "action='start' begins a gaming session (purple lights). "
                "action='stop' ends the session and restores lights. "
                "action='stats' reports today's/this week's gaming time."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start", "stop", "stats"],
                        "description": "Gaming mode action.",
                    },
                    "game_name": {
                        "type": "string",
                        "description": "Name of the game being played (for start).",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_birthdays",
            "description": (
                "Check upcoming birthdays from macOS Contacts. "
                "Returns names and dates for birthdays in the next N days."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "days_ahead": {
                        "type": "integer",
                        "description": "How many days ahead to check (default 7).",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_news_briefing",
            "description": (
                "Fetch current news headlines and return a spoken German briefing. "
                "category='general' for all news, 'tech' for technology news. "
                "items controls how many headlines to include."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["general", "tech", "science", "sport"],
                        "description": "News category (default general).",
                    },
                    "items": {
                        "type": "integer",
                        "description": "Number of headlines to include (default 5).",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    ]
