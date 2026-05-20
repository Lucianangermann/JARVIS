"""Capture the MacBook screen and route it through Claude Vision.

Uses mss for the capture (the fastest cross-platform option that
doesn't require Quartz wrangling from Python directly). The captured
RGB buffer is handed to ``VisionManager.image_to_base64`` for the
resize-and-JPEG-encode step both subcomponents share.

Privacy — every public method that touches the screen prints a
``[JARVIS 👁️ SCREEN ACTIVE]`` line at entry and ``SCREEN OFF`` at
exit, even on failure. The user can grep their server log to verify
that screen reads only happen on their explicit request.

macOS Screen Recording permission is REQUIRED. Without it mss returns
a black image and Claude will hallucinate text from the void; we
detect the all-black case and surface a clear permission-prompt
instead of silently lying about what's on screen.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from .vision_manager import VisionManager

# Prompt presets keyed by intent. ``analyze_screen()`` accepts either
# a key from this dict (so the brain trigger-phrase layer can write
# `analyze_screen("describe")`) or a raw free-form question.
_PROMPT_PRESETS: dict[str, str] = {
    "describe":
        "Beschreibe ausführlich, was auf diesem Bildschirm zu sehen "
        "ist: welche App ist aktiv, welche Inhalte werden angezeigt, "
        "welche UI-Elemente sind sichtbar. Antworte auf Deutsch.",
    "error":
        "Untersuche diesen Screenshot auf Fehlermeldungen, Warn-"
        "Dialoge, Exceptions oder Stack-Traces. Wenn etwas gefunden "
        "wird: beschreibe den Fehler kurz und schlage einen Fix vor. "
        "Falls nichts auffällig ist, sage explizit dass kein Fehler "
        "sichtbar ist. Antworte auf Deutsch.",
    "code":
        "Auf dem Screenshot ist Quellcode sichtbar. Erkläre kompakt "
        "was der Code tut, in welcher Sprache er geschrieben ist, "
        "und ob dir offensichtliche Probleme auffallen. Antworte auf "
        "Deutsch.",
    "read":
        "Extrahiere ALLEN sichtbaren Text aus diesem Screenshot. "
        "Behalte die ursprüngliche Reihenfolge und ungefähre "
        "Formatierung (Listen, Tabellen, Spalten) bei. Übersetze "
        "nichts.",
}


class ScreenReader:
    """Screen-only side of the vision layer. Holds no state beyond a
    reference to the parent manager — the most recent screenshot is
    NOT kept in memory; later phases (comparator) will manage their
    own history."""

    def __init__(self, manager: "VisionManager") -> None:
        self._mgr = manager

    # --- capture ------------------------------------------------------ #

    def capture_screen(
        self,
        region: Optional[Tuple[int, int, int, int]] = None,
    ) -> str | None:
        """Capture the screen (or a sub-region) and return base64 JPEG.

        ``region`` is an ``(x, y, width, height)`` tuple in screen
        coordinates; ``None`` means full primary display. Always
        prints the privacy indicator pair.

        Returns ``None`` if mss isn't installed, the user hasn't
        granted Screen Recording, or anything else goes wrong — the
        caller is expected to translate that into a user-facing
        "ich kann den Bildschirm gerade nicht sehen" message.
        """
        try:
            import mss
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] mss not available — install via "
                  f"`pip install mss`: {exc}")
            return None

        print("[JARVIS 👁️ SCREEN ACTIVE]")
        t0 = time.monotonic()
        b64: str | None = None
        try:
            with mss.mss() as sct:
                if region is not None:
                    x, y, w, h = region
                    monitor = {"left": int(x), "top": int(y),
                               "width": int(w), "height": int(h)}
                else:
                    # ``monitors[0]`` is the virtual "all displays
                    # combined" surface — too wide for multi-monitor
                    # users. ``monitors[1]`` is the primary display.
                    monitor = sct.monitors[1] if len(sct.monitors) > 1 \
                              else sct.monitors[0]

                raw = sct.grab(monitor)
                # mss returns BGRA pixels in a raw buffer. PIL can
                # decode that directly via frombytes; we go through
                # image_to_base64 for the resize + JPEG encode.
                from PIL import Image
                img = Image.frombytes("RGB", raw.size, raw.rgb)

            if self._is_all_black(img):
                # On macOS this is the unmistakable signature of "user
                # hasn't granted Screen Recording permission to the
                # process that called us." We refuse to send a black
                # frame to Claude since the model would happily
                # confabulate.
                print("[VISION] capture returned all-black — "
                      "Screen Recording permission likely missing. "
                      "Grant it in System Settings → Privacy & "
                      "Security → Screen Recording.")
                return None

            b64 = self._mgr.image_to_base64(img)
            if b64 is None:
                print("[VISION] image_to_base64 returned None")
            return b64
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] screen capture failed: {exc}")
            return None
        finally:
            ms = (time.monotonic() - t0) * 1000.0
            tag = "captured" if b64 else "failed"
            print(f"[JARVIS 👁️ SCREEN OFF] ({tag} in {ms:.0f} ms)")

    @staticmethod
    def _is_all_black(img: "Image") -> bool:  # type: ignore[name-defined]
        """Quick zero-pixel check on a sampled corner. macOS
        Screen Recording denial returns a uniformly black image,
        so two random corner pixels are enough to detect it without
        scanning the whole bitmap."""
        try:
            w, h = img.size
            if w < 4 or h < 4:
                return False
            corners = (img.getpixel((1, 1)),
                       img.getpixel((w - 2, h - 2)))
            for px in corners:
                # Accept RGB and RGBA; treat near-black as black.
                rgb = px[:3] if isinstance(px, tuple) else (px, px, px)
                if max(rgb) > 5:
                    return False
            return True
        except Exception:  # noqa: BLE001
            return False

    # --- analysis ----------------------------------------------------- #

    def analyze_screen(self, question: str) -> str | None:
        """Capture the screen and ask Claude about it.

        ``question`` may be one of the ``_PROMPT_PRESETS`` keys
        (``"describe"``, ``"error"``, ``"code"``, ``"read"``) for a
        polished German prompt, or a free-form question — anything we
        don't recognise is forwarded verbatim. Returns the model's
        reply or ``None`` on capture/API failure.
        """
        prompt = _PROMPT_PRESETS.get(question.strip().lower(), question)
        b64 = self.capture_screen()
        if b64 is None:
            return None
        return self._mgr.analyze_image(b64, prompt)

    def detect_error_on_screen(self) -> str | None:
        """Shortcut for the 'is there a visible error' case. Returns
        the model's text reply directly — the brain wraps this in a
        proactive nudge so a "yes, here's the fix" reply can be
        spoken without an extra round-trip."""
        return self.analyze_screen("error")

    def read_region(
        self,
        x: int, y: int, w: int, h: int,
        *, prompt: str | None = None,
    ) -> str | None:
        """Capture a sub-region and OCR it via Claude Vision. Used by
        the future "read this corner of the screen" flow — the brain
        will populate ``(x,y,w,h)`` from a user gesture or a window
        rect lookup."""
        b64 = self.capture_screen(region=(x, y, w, h))
        if b64 is None:
            return None
        return self._mgr.analyze_image(
            b64, prompt or _PROMPT_PRESETS["read"],
        )
