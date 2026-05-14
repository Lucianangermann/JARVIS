"""Web search.

By default we lean on Anthropic's built-in ``web_search`` server-side tool
(see ``brain.Brain._tools``) — Claude issues the query, Anthropic executes
it and returns annotated results. No code is needed here for that path.

This module exists so you can swap in a custom search backend later (e.g.
a corporate index, Brave, SerpAPI). Implement ``search(query) -> str`` and
add a corresponding tool definition in ``brain.py``.
"""
from __future__ import annotations


def search(query: str) -> str:  # pragma: no cover — placeholder
    """Return search results for ``query`` as plain text.

    Not used in the current build (web_search is server-side). Wired up
    here so future tool definitions in brain.py have a clear home.
    """
    raise NotImplementedError(
        "Custom web search is not implemented — Claude uses the server-side "
        "web_search tool instead. Replace this stub when adding a custom backend."
    )
