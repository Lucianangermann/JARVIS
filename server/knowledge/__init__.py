"""JARVIS knowledge layer — flashcards + spaced repetition (Second Brain).

Remember/recall lives in the memory layer (ChromaDB knowledge collection);
this package adds the structured learning side: SM-2 flashcards in
data/knowledge.db.
"""
from __future__ import annotations

from .flashcards import FlashcardManager
from .lerntrack import LerntrackDB

__all__ = ["FlashcardManager", "LerntrackDB"]
