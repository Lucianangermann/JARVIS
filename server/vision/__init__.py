"""JARVIS vision layer (Phase 1).

This package adds Claude-Vision–powered analysis of arbitrary images
to JARVIS: screen captures, OCR on still images, and the shared
plumbing both rely on.

Design rule shared with the intelligence layer: vision must NEVER
crash JARVIS. Every public entry point is wrapped in best-effort
exception handling and returns ``None`` (or a descriptive error
string) on failure. The optional ``mss`` import is gated so the
server still boots if the vision deps aren't installed.

Phase 1+2 surface:
  * VisionManager       — base coordinator + image_to_base64 utility
  * ScreenReader        — full-screen or region capture + analysis
  * OCR                 — text extraction + visual translation
  * DocumentScanner     — receipts / contracts / business cards / …
  * ObjectRecognizer    — identify / plant / food / animal / damage / barcode
  * ImageComparator     — pairwise diff + screen-snapshot flow

Later phases (motion_detector, live translation, API routes, …) plug
into the same VisionManager.
"""
from .vision_manager import VisionManager  # noqa: F401
from .screen_reader import ScreenReader  # noqa: F401
from .ocr import OCR  # noqa: F401
from .document_scanner import DocumentScanner, DocumentResult  # noqa: F401
from .object_recognition import (
    ObjectRecognizer, RecognitionResult, BarcodeResult,
)  # noqa: F401
from .comparator import ImageComparator, ComparisonResult  # noqa: F401
