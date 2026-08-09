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


def _tick_bases(polyline: Sequence[Point], spacing: float, inset: float
                ) -> List[Tuple[Point, Tuple[float, float]]]:
    """Base points + unit tangents along ``polyline`` where ticks are placed.

    Factored out of :func:`edge_ticks` so :func:`shared_edge_ticks` can place
    its ticks at the *same* base points (measured from the same edge, in the
    same traversal order) -- that is what makes the two halves of a seam
    coincide and pair up in the caller's fit/drop pass.
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

    bases = []
    for pos in positions:
        p, (tx, ty) = _point_and_tangent(pts, seglens, pos)
        if tx == 0.0 and ty == 0.0:
            continue
        bases.append((p, (tx, ty)))
    return bases


def _tick_toward(p: Point, tangent: Tuple[float, float], toward: Point,
                 half: float) -> g.Line:
    """A single half-tick at ``p``, perpendicular to ``tangent``, reaching
    ``half`` toward the ``toward`` point (the owning piece's interior)."""
    tx, ty = tangent
    nx, ny = -ty, tx  # perpendicular
    if nx * (toward[0] - p[0]) + ny * (toward[1] - p[1]) < 0:
        nx, ny = -nx, -ny
    return g.Line((p[0], p[1]), (p[0] + nx * half, p[1] + ny * half))


def edge_ticks(polyline: Sequence[Point], toward: Point, length: float = 0.6,
               spacing: float = 5.0, inset: float = 0.3) -> List[g.Line]:
    """Tick marks along a shared edge, pointing toward the piece interior.

    ``polyline`` is the shared edge (>=2 points, cm). ``toward`` is a point
    inside the owning piece (its centroid) used to choose the interior side.
    Each tick reaches ``length/2`` inward from the seam. Ticks are placed at
    both ends (``inset`` from the corners) and every ``spacing`` between.
    Returns Line elements in the same cm space.
    """
    half = length / 2.0
    return [_tick_toward(p, t, toward, half)
            for p, t in _tick_bases(polyline, spacing, inset)]


def _on_segment(p: Point, a: Point, b: Point, tol: float) -> bool:
    """Is ``p`` within ``tol`` of the *segment* ``a->b`` (not just its line)?"""
    dx, dy = b[0] - a[0], b[1] - a[1]
    dd = dx * dx + dy * dy
    if dd <= tol * tol:
        return math.hypot(p[0] - a[0], p[1] - a[1]) <= tol
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / dd
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    cx, cy = a[0] + t * dx, a[1] + t * dy
    return math.hypot(p[0] - cx, p[1] - cy) <= tol


def _on_ring(ring: Sequence[Point], p: Point, tol: float) -> bool:
    """Does ``p`` lie on the boundary of the closed ``ring`` (within ``tol``)?"""
    n = len(ring)
    return any(_on_segment(p, ring[i], ring[(i + 1) % n], tol)
               for i in range(n))


def shared_edge_ticks(edge: Sequence[Point], ring: Sequence[Point],
                      toward: Point, length: float = 0.6, spacing: float = 5.0,
                      inset: float = 0.3, tol: float = 1e-6) -> List[g.Line]:
    """Matched ticks for one tile ``ring`` along a shared region boundary ``edge``.

    When an oversized region is auto-tiled, its outer boundary is split across
    several tiles. Where that boundary is shared with an *adjacent* region
    (tiled or not), the neighbour already gets ticks from
    ``edge_ticks(edge, neighbour_centroid)``. This returns the matching ticks
    for a single tile of the tiled side: the base points of ``edge_ticks`` on
    the *full* ``edge`` that actually fall on THIS tile's boundary, each pointed
    toward ``toward`` (the tile interior).

    Because both sides derive their base points from the identical ``edge``
    polyline (same points, same order), the half-ticks coincide exactly, so the
    caller's base-point pairing (``_drop_unfitting_fiducials``) treats them as
    one seam and the marks line up when the pieces are butted back together.

    A tile whose boundary does not touch ``edge`` gets no ticks (``[]``). Only
    base points lying on the tile boundary are emitted, so the ticks split
    cleanly across the tiles that share the edge with no duplication except at a
    base point landing exactly on a tile-to-tile corner (rare; both tiles claim
    it, which is harmless).
    """
    half = length / 2.0
    return [_tick_toward(p, t, toward, half)
            for p, t in _tick_bases(edge, spacing, inset)
            if _on_ring(ring, p, tol)]
