"""JARVIS long-term memory + self-learning system.

Public surface
--------------
- :class:`MemoryManager` — the central coordinator. Brain calls
  ``session_start``, ``before_message``, ``after_message``,
  ``record_command_result``, ``session_end`` on this.
- :class:`ShortTermMemory` — in-session conversation buffer (~20 messages).
- :class:`LongTermMemory` — semantic vector store (ChromaDB).
- :class:`ErrorMemory` — SQLite-backed error history + known-fix lookups.
- :class:`ProfileManager` — durable user profile (JSON + SQLite history).
- :class:`ContextBuilder` — assembles the full system prompt.

All four memory layers degrade gracefully: if any one fails to load
(missing deps, corrupted db, …), JARVIS keeps running without that
layer. See module-level docstrings for the per-component details.
"""
# Re-exports are deferred until all modules exist (the package is
# built up file-by-file, so guard each import). Once the integration
# lands, these become the canonical public surface.
__all__: list[str] = []

try:
    from .short_term import ShortTermMemory  # noqa: F401
    __all__.append("ShortTermMemory")
except Exception:
    pass

try:
    from .memory_manager import MemoryManager  # noqa: F401
    __all__.append("MemoryManager")
except Exception:
    pass
