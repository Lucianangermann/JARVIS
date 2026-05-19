"""JARVIS intelligence layer.

Slice 1 surface: IntelligenceManager coordinates a background
scheduler and a small set of routines that assemble text-ready
briefings from the tools/ package. Failures inside this package
MUST NOT crash the rest of the server — every entry point catches
and the manager itself is optional (brain.py works without it).
"""
from .intelligence_manager import IntelligenceManager  # noqa: F401
