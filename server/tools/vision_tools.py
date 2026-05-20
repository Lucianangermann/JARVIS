"""Claude tool_use schemas for the vision layer.

These are the JSON schemas Claude sees when deciding whether to look
at the screen mid-reply. They're separate from the trigger-phrase
short-circuit in ``brain.py``: triggers are for explicit user phrases
("was siehst du auf meinem bildschirm"), tools are for inferred
intent ("warum funktioniert das nicht?" — Claude can decide to call
``check_screen_for_errors`` without being told).

The schemas are intentionally small. We only expose vision actions
that are safe to invoke autonomously (i.e. don't capture the camera,
don't start long-running monitors). The camera + motion-detection
actions stay behind the explicit voice triggers because auto-firing
them surprise-records the user.
"""
from __future__ import annotations

from typing import Any


def analyze_screen_tool() -> dict[str, Any]:
    """Generic screen-look-at tool. Claude passes a question; we
    capture the screen and forward to Claude Vision (yes, this is
    a recursive-ish call to the model — but cheap because Haiku
    Vision is fast and the screen payload caps at 1920 px)."""
    return {
        "name": "analyze_screen",
        "description": (
            "Capture and analyse the user's MacBook screen. Call this "
            "when the user's question implies you should look at what "
            "they're seeing (e.g. 'why is this red', 'is the build "
            "still running', 'fix this'). Privacy indicators print "
            "server-side; the user always knows when the screen was "
            "read. Pass a ``question`` that describes what you want "
            "to look for; broad prompts work fine ('describe what's "
            "open' or 'find the error message'). Returns the model's "
            "text reply or an error string."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "What to extract from the screen. Free-form; "
                        "also accepts the presets 'describe', 'error', "
                        "'code', 'read' for the standard German "
                        "system prompts."
                    ),
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    }


def check_screen_for_errors_tool() -> dict[str, Any]:
    """Specialised wrapper for the most common autonomous case: the
    user said something is broken, Claude wants to see what's
    actually on screen before answering. Same as analyze_screen but
    with the 'error' preset baked in so Claude doesn't have to
    invent the right German prompt."""
    return {
        "name": "check_screen_for_errors",
        "description": (
            "Capture the user's screen and check specifically for "
            "error messages, warning dialogs, exceptions, or stack "
            "traces. Use this when the user mentions a problem or "
            "asks for help and you suspect there's a visible error. "
            "Takes no parameters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }


def read_screen_text_tool() -> dict[str, Any]:
    """Pure OCR pass — useful when the user pastes ambiguous text in
    chat and Claude wants to see exactly what's on screen for
    grounding."""
    return {
        "name": "read_screen_text",
        "description": (
            "Capture the user's screen and extract ALL visible text "
            "exactly as displayed. Use this when grounding a reply "
            "in something the user is reading right now. Takes no "
            "parameters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }


def vision_tools() -> list[dict[str, Any]]:
    """Single entry-point the brain calls to populate its tool list."""
    return [
        analyze_screen_tool(),
        check_screen_for_errors_tool(),
        read_screen_text_tool(),
    ]
