"""Alignment fiducials for abutting pieces (dependency-free).

A shared cut edge between two pieces gets short **perpendicular tick** marks,
placed on each piece's interior side. When the pieces are butted back together
the two half-ticks meet into one continuous line crossing the seam, confirming
correct position and no gap. All lengths are in centimetres.
"""

import math
from typing import List, Sequence, Tuple

from . import geometry as g

Point = Tuple[float, float]


def _point_and_tangent(pts: Sequence[Point], seglens: Sequence[float],
                       s: float) -> Tuple[Point, Tuple[float, float]]:
    """Point at arc-length ``s`` along polyline ``pts`` and the local unit tangent."""
    acc = 0.0
    for i, d in enumerate(seglens):
        if d <= 0:
            continue
        if acc + d >= s or i == len(seglens) - 1:
            t = min(max((s - acc) / d, 0.0), 1.0)
            a = pts[i]
            b = pts[i + 1]
            px = a[0] + (b[0] - a[0]) * t
            py = a[1] + (b[1] - a[1]) * t
            tx = (b[0] - a[0]) / d
            ty = (b[1] - a[1]) / d
            return ((px, py), (tx, ty))
        acc += d
    a, b = pts[0], pts[-1]
    return (pts[-1], (0.0, 0.0))


def edge_ticks(polyline: Sequence[Point], toward: Point, length: float = 0.6,
               spacing: float = 5.0, inset: float = 0.3) -> List[g.Line]:
    """Tick marks along a shared edge, pointing toward the piece interior.

    ``polyline`` is the shared edge (>=2 points, cm). ``toward`` is a point
    inside the owning piece (its centroid) used to choose the interior side.
    Each tick reaches ``length/2`` inward from the seam. Ticks are placed at
    both ends (``inset`` from the corners) and every ``spacing`` between.
    Returns Line elements in the same cm space.
    """
    pts = list(polyline)
    if len(pts) < 2:
        return []
    seglens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])]
    total = sum(seglens)
    if total <= 0:
        return []

    if total <= 2 * inset:
        positions = [total / 2.0]
    else:
        positions = [inset]
        s = inset + spacing
        while s < total - inset - 1e-9:
            positions.append(s)
            s += spacing
        positions.append(total - inset)

    half = length / 2.0
    ticks = []
    for pos in positions:
        p, (tx, ty) = _point_and_tangent(pts, seglens, pos)
        if tx == 0.0 and ty == 0.0:
            continue
        nx, ny = -ty, tx  # perpendicular
        # Point the tick toward the piece interior.
        if nx * (toward[0] - p[0]) + ny * (toward[1] - p[1]) < 0:
            nx, ny = -nx, -ny
        ticks.append(g.Line((p[0], p[1]), (p[0] + nx * half, p[1] + ny * half)))
    return ticks
