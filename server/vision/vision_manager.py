"""Central coordinator for the vision layer.

The manager owns the shared Anthropic client used for Claude Vision
calls, hosts the ``image_to_base64`` utility every subcomponent needs,
and lazily instantiates the subcomponents (ScreenReader, OCR, …) so
constructing the manager is cheap and side-effect-free.

Phase 1 deliberately ships only ScreenReader + OCR. The other
subcomponents from the original spec (DocumentScanner, ObjectRecognizer,
ImageComparator, MotionDetector, …) will become attributes here in
Phase 2/3 without changing this file's public shape.

Failure model
-------------
Every public entry point catches its own exceptions and either returns
``None`` or a German-language error string suitable for TTS. The brain
treats vision as an optional capability — ``brain.vision`` may be
``None`` if the deps aren't installed — so a crashed vision call never
takes the chat loop with it.
"""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import settings

if TYPE_CHECKING:
    from anthropic import Anthropic

# Image bounds: Claude Vision accepts up to 8000 px on the long edge
# but bills per resized 1568×1568 tile, so anything past ~1920 wastes
# tokens for no gain. JPEG quality 85 is the inflection point past
# which extra quality is invisible on screenshots.
_MAX_IMAGE_LONG_EDGE = int(os.getenv("JARVIS_VISION_MAX_PX", "1920"))
_JPEG_QUALITY = int(os.getenv("JARVIS_VISION_JPEG_QUALITY", "85"))

# Hard cap on the encoded payload. Claude Vision rejects >5 MB and
# anything close to that is signalling we should have downsampled
# harder. We hand back ``None`` so callers can surface a sane error
# rather than retrying forever.
_MAX_BYTES = 5 * 1024 * 1024


def _gate_dep(name: str) -> Any:
    """Try to import a vision dep, returning ``None`` on failure.

    Phase 1 only needs Pillow (always present) and the Anthropic SDK
    (always present). We still keep this helper because Phase 2+
    optional deps (opencv-python, pyzbar) will route through the same
    path and we want a single gating idiom across the file.
    """
    try:
        return __import__(name)
    except Exception:  # noqa: BLE001
        return None


class VisionManager:
    """One instance per server, attached to ``brain.vision`` by the
    server lifespan when the vision deps imported cleanly."""

    def __init__(self, client: "Anthropic") -> None:
        self._client = client
        # Subcomponents are instantiated lazily so importing them
        # late (or replacing them in a test) doesn't ripple through
        # the manager API.
        self._screen: Any | None = None
        self._ocr: Any | None = None
        self._scanner: Any | None = None
        self._recognizer: Any | None = None
        self._comparator: Any | None = None
        self._motion: Any | None = None
        self._translator: Any | None = None

    # --- subcomponent accessors --------------------------------------- #

    @property
    def screen(self) -> Any:
        if self._screen is None:
            # Local import to avoid a circular-import landmine if
            # ScreenReader ever needs to ask the manager for the
            # shared client during its own __init__.
            from .screen_reader import ScreenReader
            self._screen = ScreenReader(self)
        return self._screen

    @property
    def ocr(self) -> Any:
        if self._ocr is None:
            from .ocr import OCR
            self._ocr = OCR(self)
        return self._ocr

    @property
    def scanner(self) -> Any:
        """Document scanner (Phase 2). Lazily instantiated like the
        other subcomponents so importing this module stays cheap."""
        if self._scanner is None:
            from .document_scanner import DocumentScanner
            self._scanner = DocumentScanner(self)
        return self._scanner

    @property
    def recognizer(self) -> Any:
        """Object / plant / food / animal / damage / style identifier
        (Phase 2)."""
        if self._recognizer is None:
            from .object_recognition import ObjectRecognizer
            self._recognizer = ObjectRecognizer(self)
        return self._recognizer

    @property
    def comparator(self) -> Any:
        """Pairwise image diff + screen-snapshot flow (Phase 2)."""
        if self._comparator is None:
            from .comparator import ImageComparator
            self._comparator = ImageComparator(self)
        return self._comparator

    @property
    def motion(self) -> Any:
        """OpenCV-based camera motion detector (Phase 3). Stateful —
        the same instance survives across start/stop cycles."""
        if self._motion is None:
            from .motion_detector import MotionDetector
            self._motion = MotionDetector(self)
        return self._motion

    @property
    def translator(self) -> Any:
        """Rate-limited live visual translator (Phase 3). Holds per-
        session caches; the brain forwards iPhone WebSocket session
        IDs through to the underlying ``live_translation`` call."""
        if self._translator is None:
            from .translator import Translator
            self._translator = Translator(self)
        return self._translator

    # --- image normalisation ------------------------------------------ #

    def image_to_base64(self, source: Any) -> str | None:
        """Coerce one of {path str, ``Path``, bytes, PIL.Image} into a
        base64 JPEG suitable for the Claude Vision payload.

        We always re-encode (even when the input is already JPEG bytes)
        because the resize-to-≤1920px step is non-negotiable for cost
        control. Returns ``None`` on any failure — callers should not
        try to recover, they should surface a user-visible error.
        """
        try:
            from PIL import Image  # local import: don't drag PIL into hot paths
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] Pillow not available: {exc}")
            return None

        try:
            img = self._load_image(source, Image)
            if img is None:
                return None
            img = self._resize_for_vision(img)
            # Force RGB before JPEG encode — PNGs with alpha and BMPs
            # in P/L mode can otherwise raise inside save().
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
            data = buf.getvalue()
            if len(data) > _MAX_BYTES:
                # Re-encode at lower quality once before giving up.
                # 1080p+ screenshots with heavy gradients sometimes
                # don't fit at 85 but always fit at 70.
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=70, optimize=True)
                data = buf.getvalue()
                if len(data) > _MAX_BYTES:
                    print(f"[VISION] image too large after compression: "
                          f"{len(data)/1024/1024:.1f} MiB > 5 MiB cap")
                    return None
            return base64.b64encode(data).decode("ascii")
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] image_to_base64 failed: {exc}")
            return None

    @staticmethod
    def _load_image(source: Any, Image: Any) -> Any:
        """Resolve heterogeneous ``source`` types into a PIL Image."""
        if hasattr(source, "save") and hasattr(source, "convert"):
            # Looks like a PIL Image already.
            return source
        if isinstance(source, (bytes, bytearray, memoryview)):
            return Image.open(io.BytesIO(bytes(source)))
        if isinstance(source, (str, Path)):
            p = Path(source).expanduser()
            if not p.exists():
                print(f"[VISION] image path does not exist: {p}")
                return None
            return Image.open(p)
        print(f"[VISION] unsupported image source type: {type(source).__name__}")
        return None

    @staticmethod
    def _resize_for_vision(img: Any) -> Any:
        """Cap the long edge at ``_MAX_IMAGE_LONG_EDGE`` while
        preserving aspect ratio. Smaller images pass through."""
        try:
            from PIL import Image
        except Exception:  # noqa: BLE001
            return img
        w, h = img.size
        long_edge = max(w, h)
        if long_edge <= _MAX_IMAGE_LONG_EDGE:
            return img
        scale = _MAX_IMAGE_LONG_EDGE / float(long_edge)
        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        return img.resize(new_size, Image.LANCZOS)

    # --- Claude Vision call ------------------------------------------- #

    def analyze_image(
        self,
        image_base64: str,
        prompt: str,
        *,
        max_tokens: int = 1024,
    ) -> str | None:
        """Send a (base64 JPEG, text prompt) pair to Claude Vision and
        return the model's text reply.

        Returns ``None`` on any API or shape failure — vision callers
        downstream tend to translate this into a German fallback
        message rather than swallowing the silence.
        """
        if not image_base64 or not prompt:
            return None
        try:
            response = self._client.messages.create(
                model=settings.MODEL,
                max_tokens=max_tokens,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_base64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] Claude API call failed: {exc}")
            return None

        # Response shape: response.content is a list of TextBlock /
        # other blocks. Pull the first text block; ignore tool_use
        # blocks since we don't pass tools on a vision-only turn.
        try:
            for block in response.content:
                # Anthropic SDK returns blocks with a ``type`` attribute.
                # Defensive checks because the shape is the SDK's
                # surface area, not ours.
                if getattr(block, "type", None) == "text":
                    text = getattr(block, "text", "") or ""
                    return text.strip() or None
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] failed to parse Claude response: {exc}")
        return None
