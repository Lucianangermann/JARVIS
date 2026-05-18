"""Generate JARVIS PWA icon PNGs.

One-shot script. Run once after cloning:

    .venv/bin/python clients/pwa/gen_icons.py

Produces three PNGs in clients/pwa/assets/:
    icon-192.png            Android / desktop PWA manifest
    icon-512.png            high-res for splash + install dialog
    apple-touch-icon.png    iOS Add-to-Home-Screen (180×180)

The design mirrors the Electron HUD: black field, cyan glowing
"J" in Orbitron-y geometric letterforms, ringed by a faint hex.
PIL only — no fonts on disk needed; the J is drawn as polygons so
the icon looks identical on any system that runs the script.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


OUTPUT_DIR = Path(__file__).resolve().parent / "assets"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _hex_points(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    """Six vertices of a regular hexagon, pointy-top."""
    pts = []
    for i in range(6):
        angle = math.pi / 2 + (i * math.pi / 3)
        pts.append((cx + r * math.cos(angle), cy - r * math.sin(angle)))
    return pts


def _draw_j(draw: ImageDraw.ImageDraw, *, cx: float, cy: float, h: float,
            color: tuple[int, int, int, int]) -> None:
    """Stylised geometric J. Two strokes — a horizontal top and a
    vertical-then-hooked descender — drawn as polygons so we don't
    depend on a system font."""
    stroke = max(2, int(h * 0.16))
    half_top = h * 0.32
    # Top bar
    draw.rectangle([cx - half_top, cy - h / 2,
                    cx + half_top, cy - h / 2 + stroke], fill=color)
    # Vertical stem (slightly right of centre, matches the cap)
    stem_x = cx + half_top * 0.55
    draw.rectangle([stem_x - stroke / 2, cy - h / 2,
                    stem_x + stroke / 2, cy + h * 0.28], fill=color)
    # Hook
    hook_radius = h * 0.18
    draw.arc(
        [stem_x - 2 * hook_radius, cy + h * 0.28 - hook_radius,
         stem_x, cy + h * 0.28 + hook_radius],
        start=0, end=180, fill=color, width=stroke,
    )


def _render(size: int) -> Image.Image:
    """Produce one icon at ``size × size`` pixels."""
    bg = (0, 8, 15, 255)
    primary = (0, 212, 255, 255)
    glow = (0, 255, 255, 200)

    img = Image.new("RGBA", (size, size), bg)
    draw = ImageDraw.Draw(img)

    cx, cy = size / 2, size / 2

    # Faint hexagonal border + outer ring — gives the icon the same
    # technical-blueprint feel as the Electron emblem.
    ring_r_outer = size * 0.44
    ring_r_inner = size * 0.40
    draw.ellipse([cx - ring_r_outer, cy - ring_r_outer,
                  cx + ring_r_outer, cy + ring_r_outer],
                 outline=(0, 212, 255, 110), width=max(1, size // 96))
    draw.polygon(_hex_points(cx, cy, ring_r_inner),
                 outline=(0, 212, 255, 80), width=max(1, size // 128))

    # Bright "J" in the centre
    _draw_j(draw, cx=cx, cy=cy, h=size * 0.50, color=primary)

    # Glow pass: blur a copy of the J + ring and screen-blend back.
    glow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow_layer)
    _draw_j(gdraw, cx=cx, cy=cy, h=size * 0.50, color=glow)
    gdraw.ellipse([cx - ring_r_outer, cy - ring_r_outer,
                   cx + ring_r_outer, cy + ring_r_outer],
                  outline=(0, 255, 255, 90), width=max(1, size // 64))
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(size * 0.04))
    # Composite the glow under the crisp foreground.
    base = img.copy()
    base = Image.alpha_composite(base, glow_layer)
    base = Image.alpha_composite(base, img)
    return base


def main() -> None:
    for fname, size in (
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("apple-touch-icon.png", 180),
    ):
        img = _render(size)
        path = OUTPUT_DIR / fname
        img.save(path, "PNG", optimize=True)
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
