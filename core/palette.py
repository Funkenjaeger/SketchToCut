"""Distinct color palette for multi-piece "one file" output (dependency-free).

``distinct_colors(n)`` returns ``n`` visually distinct hex colors (evenly spaced
hues). A color-aware cutter SW (e.g. a vinyl cutter) can then peel each piece
onto its own sheet by color. ``colorsys`` is stdlib, so this stays importable
inside Fusion's sandboxed interpreter.
"""

import colorsys


# Hues within this band of pure red (hue 0) are skipped, so no piece color
# collides with the red reserved for piece-index lettering.
_RED_BAND = 0.06


def distinct_colors(n):
    """Return ``n`` distinct ``#RRGGBB`` strings (evenly spaced hues, no red).

    Red is reserved for lettering, so the hues are distributed across
    ``[_RED_BAND, 1 - _RED_BAND]`` and never land on pure red.
    """
    if n <= 0:
        return []
    usable = 1.0 - 2.0 * _RED_BAND
    out = []
    for i in range(n):
        hue = _RED_BAND + ((i + 0.5) / n) * usable
        r, g, b = colorsys.hls_to_rgb(hue, 0.45, 0.65)
        out.append("#%02X%02X%02X" % (int(r * 255 + 0.5),
                                       int(g * 255 + 0.5),
                                       int(b * 255 + 0.5)))
    return out


# AutoCAD Color Index (ACI) values for DXF piece layers -- skip 1 (red, reserved
# for the LABEL layer) and 7 (white/black).
_ACI_CYCLE = [2, 3, 4, 5, 6, 8, 9, 30, 40, 50, 90, 140, 190, 210, 230]


def aci_color(i):
    """A DXF AutoCAD Color Index for the i-th piece."""
    return _ACI_CYCLE[i % len(_ACI_CYCLE)]
