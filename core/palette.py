"""Distinct color palette for multi-piece "one file" output (dependency-free).

``distinct_colors(n)`` returns ``n`` visually distinct hex colors (evenly spaced
hues). A color-aware cutter SW (e.g. a vinyl cutter) can then peel each piece
onto its own sheet by color. ``colorsys`` is stdlib, so this stays importable
inside Fusion's sandboxed interpreter.
"""

import colorsys


def distinct_colors(n):
    """Return ``n`` distinct ``#RRGGBB`` strings (evenly spaced hues)."""
    if n <= 0:
        return []
    out = []
    for i in range(n):
        hue = (i / float(n)) % 1.0
        r, g, b = colorsys.hls_to_rgb(hue, 0.45, 0.65)
        out.append("#%02X%02X%02X" % (int(r * 255 + 0.5),
                                       int(g * 255 + 0.5),
                                       int(b * 255 + 0.5)))
    return out


# AutoCAD Color Index (ACI) values for DXF layers, cycling through the standard
# 1..9 spectrum (skip 7=white/black so it reads on any background).
_ACI_CYCLE = [1, 2, 3, 4, 5, 6, 8, 9, 30, 40, 50, 90, 140, 190, 210, 230]


def aci_color(i):
    """A DXF AutoCAD Color Index for the i-th piece."""
    return _ACI_CYCLE[i % len(_ACI_CYCLE)]
