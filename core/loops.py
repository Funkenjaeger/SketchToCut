"""Chain a profile loop's curves into a single closed ring (dependency-free).

Fusion gives a profile loop as an unordered-ish set of curves; the segments
that make up one closed boundary are not guaranteed to arrive head-to-tail or
consistently oriented. ``chain_loop`` takes each curve already flattened to a
list of 2D points (cm) and walks endpoint-to-endpoint to produce one ordered,
closed polyline. This guarantees the per-region export closes each piece along
its cut lines.
"""

import math
from typing import List, Sequence, Tuple

Point = Tuple[float, float]


def _close(a: Point, b: Point, tol: float) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def chain_loop(curves: Sequence[Sequence[Point]], tol: float = 1e-4) -> List[Point]:
    """Order ``curves`` (each a polyline of >=2 points) into one closed ring.

    Returns the ring's vertices with no repeated closing point (closure is
    implied). Raises ``ValueError`` if the curves do not form a single closed
    loop (leftover/disconnected segments).
    """
    remaining = [list(c) for c in curves if len(c) >= 2]
    if not remaining:
        return []

    chain = remaining.pop(0)
    progress = True
    while remaining and progress:
        progress = False
        end = chain[-1]
        for i, c in enumerate(remaining):
            if _close(end, c[0], tol):
                chain.extend(c[1:])
                remaining.pop(i)
                progress = True
                break
            if _close(end, c[-1], tol):
                chain.extend(list(reversed(c))[1:])
                remaining.pop(i)
                progress = True
                break
        if progress:
            continue
        # Nothing attached to the tail; try growing from the head instead.
        start = chain[0]
        for i, c in enumerate(remaining):
            if _close(start, c[-1], tol):
                chain = c[:-1] + chain
                remaining.pop(i)
                progress = True
                break
            if _close(start, c[0], tol):
                chain = list(reversed(c))[:-1] + chain
                remaining.pop(i)
                progress = True
                break

    if remaining:
        raise ValueError(
            "loop did not chain into a single ring; %d segment(s) left over"
            % len(remaining))

    # Drop a duplicated closing vertex if the walk returned to the start.
    if len(chain) > 1 and _close(chain[0], chain[-1], tol):
        chain = chain[:-1]
    # Remove any consecutive duplicate points.
    cleaned = [chain[0]]
    for p in chain[1:]:
        if not _close(cleaned[-1], p, tol):
            cleaned.append(p)
    return cleaned
