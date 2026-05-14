"""Optional helper tools for the brain.

The brain currently uses Anthropic's *server-side* ``web_search`` tool
directly (no implementation needed on our end), and routes all system
actions through ``server.command_guard``. The modules in this package are
thin wrappers kept for future extension and discoverability — e.g. if you
later swap web_search for a custom search backend, edit ``search.py``
without touching ``brain.py``.
"""
