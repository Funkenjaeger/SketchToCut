"""Fit a piece within a rectangular bed by rotation (dependency-free).

Used by per-region export: before writing a piece's cut file, find a rotation
that makes its axis-aligned bounding box fit within the user's max bed WxH. If
the piece already fits at 0deg it is left unrotated; otherwise the smallest
rotation (scanning upward) that fits is used. Rotating by 90deg swaps the AABB
dimensions, so a single 0..180deg sweep covers portrait<->landscape -- no
separate W/H swap is needed. Non-orthogonal fits are found naturally.
"""

import math
from typing import List, Optional, Sequence, Tuple

Point = Tuple[float, float]


def convex_hull(points: Sequence[Point]) -> List[Point]:
    """Andrew's monotonic chain hull; returns CCW hull vertices (no repeat)."""
    pts = sorted(set((float(p[0]), float(p[1])) for p in points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def rotate_points(points: Sequence[Point], theta: float,
                  center: Point = (0.0, 0.0)) -> List[Point]:
    """Rotate points ``theta`` radians CCW about ``center``."""
    c = math.cos(theta)
    s = math.sin(theta)
    cx, cy = center
    out = []
    for x, y in points:
        dx = x - cx
        dy = y - cy
        out.append((cx + dx * c - dy * s, cy + dx * s + dy * c))
    return out


def aabb_size(points: Sequence[Point]) -> Tuple[float, float]:
    """(width, height) of the axis-aligned bounding box of ``points``."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (max(xs) - min(xs), max(ys) - min(ys))


def fit_rotation(points: Sequence[Point], w: float, h: float,
                 step_deg: float = 1.0, eps: float = 1e-6) -> Optional[float]:
    """Smallest rotation (radians) whose rotated-hull AABB fits within w x h.

    Returns 0.0 if it already fits, a positive angle if a rotation is needed,
    or ``None`` if the piece fits at no orientation. ``w``/``h`` and ``points``
    must be in the same units (centimetres, here).
    """
    hull = convex_hull(points)
    if not hull:
        return 0.0

    def fits(theta):
        bw, bh = aabb_size(rotate_points(hull, theta))
        return bw <= w + eps and bh <= h + eps

    # Prefer the clean orientations first: no rotation, then a square quarter
    # turn (portrait<->landscape). Only if neither fits do we hunt for an
    # oddball angle -- returning the smallest such rotation.
    for deg in (0.0, 90.0):
        if fits(math.radians(deg)):
            return math.radians(deg)
    steps = max(1, int(round(180.0 / step_deg)))
    for k in range(1, steps):
        deg = k * step_deg
        if deg == 90.0:
            continue
        theta = math.radians(deg)
        if fits(theta):
            return theta
    return None
