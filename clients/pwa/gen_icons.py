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


def _draw_emblem(draw: ImageDraw.ImageDraw, *, cx: float, cy: float,
                 size: float,
                 ring_color: tuple[int, int, int, int],
                 core_color: tuple[int, int, int, int]) -> None:
    """Iron-Man arc-reactor inspired emblem: concentric rings, an
    inscribed hex, six radial spokes pointing to the hex vertices,
    and a bright filled core at the centre. Inherently symmetric —
    no glyph-balancing tricks needed."""
    r_outer = size * 0.46
    r_hex   = size * 0.40
    r_inner = size * 0.24
    r_core  = size * 0.085

    line_w  = max(2, int(size * 0.022))
    thin_w  = max(1, int(size * 0.014))

    # Outer thin ring
    draw.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer],
                 outline=ring_color, width=thin_w)

    # Hex frame (HUD reference to the desktop / PWA hex buttons)
    draw.polygon(_hex_points(cx, cy, r_hex),
                 outline=ring_color, width=line_w)

    # Inner ring around the core
    draw.ellipse([cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner],
                 outline=ring_color, width=line_w)

    # Six radial spokes from inner ring to hex vertices
    spoke_w = max(2, int(size * 0.026))
    for vx, vy in _hex_points(cx, cy, r_hex):
        # Vector from cx,cy to vertex, normalised
        dx, dy = vx - cx, vy - cy
        d = math.hypot(dx, dy) or 1.0
        nx, ny = dx / d, dy / d
        # Spoke from just outside the inner ring to just inside the hex
        x1 = cx + nx * (r_inner + line_w)
        y1 = cy + ny * (r_inner + line_w)
        x2 = cx + nx * (r_hex - line_w)
        y2 = cy + ny * (r_hex - line_w)
        draw.line([(x1, y1), (x2, y2)], fill=ring_color, width=spoke_w)

    # Filled central core — the reactor's bright point
    draw.ellipse([cx - r_core, cy - r_core, cx + r_core, cy + r_core],
                 fill=core_color)


def _render(size: int) -> Image.Image:
    """Produce one icon at ``size × size`` pixels."""
    bg = (0, 8, 15, 255)
    primary = (0, 212, 255, 255)
    glow = (0, 255, 255, 220)

    img = Image.new("RGBA", (size, size), bg)
    draw = ImageDraw.Draw(img)

    cx, cy = size / 2, size / 2

    _draw_emblem(draw, cx=cx, cy=cy, size=size,
                 ring_color=primary, core_color=primary)

    # Glow pass: blur a copy of the emblem and composite under the
    # crisp foreground for that classic HUD bloom.
    glow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow_layer)
    _draw_emblem(gdraw, cx=cx, cy=cy, size=size,
                 ring_color=(0, 255, 255, 100),
                 core_color=glow)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(size * 0.045))

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
