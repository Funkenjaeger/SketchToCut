"""Tile an oversized piece into bed-sized tiles (dependency-free).

Used as the fallback when a region does not fit the cutter/laser bed at any
rotation (see :func:`core.fitting.fit_rotation`). The piece polygon (outer loop
+ holes, in cm) is clipped against an axis-aligned grid of tile rectangles via
Sutherland-Hodgman, so each tile piece is **re-closed** along the cut lines.
Edges that fall on an interior grid line are reported as ``cut_edges`` so the
caller can drop matching alignment fiducials on both sides of every seam.

The grid can be rotated (``rotation_deg``): geometry is taken into the grid's
frame, tiled axis-aligned, and the results rotated back. At 0 deg the tile
dimensions are honoured verbatim.

Limitations (documented, acceptable for the manual-sectioning-first workflow):
  * Butt-joint tiles only (no overlap yet).
  * Sutherland-Hodgman connects disjoint intersections of a concave piece with
    a seam along the tile boundary rather than producing separate loops.
"""

import math

_TOL = 1e-6


def clip_polygon_rect(poly, xmin, ymin, xmax, ymax):
    """Sutherland-Hodgman clip of a closed polygon to an axis-aligned rect."""
    if not poly:
        return []

    def clip(pts, inside, intersect):
        out = []
        n = len(pts)
        for i in range(n):
            cur, prev = pts[i], pts[i - 1]
            ci, pi = inside(cur), inside(prev)
            if ci:
                if not pi:
                    out.append(intersect(prev, cur))
                out.append(cur)
            elif pi:
                out.append(intersect(prev, cur))
        return out

    def ix(p, q, xc):
        if q[0] == p[0]:
            return (xc, p[1])
        t = (xc - p[0]) / (q[0] - p[0])
        return (xc, p[1] + t * (q[1] - p[1]))

    def iy(p, q, yc):
        if q[1] == p[1]:
            return (p[0], yc)
        t = (yc - p[1]) / (q[1] - p[1])
        return (p[0] + t * (q[0] - p[0]), yc)

    pts = poly
    pts = clip(pts, lambda p: p[0] >= xmin - _TOL, lambda p, q: ix(p, q, xmin))
    pts = clip(pts, lambda p: p[0] <= xmax + _TOL, lambda p, q: ix(p, q, xmax))
    pts = clip(pts, lambda p: p[1] >= ymin - _TOL, lambda p, q: iy(p, q, ymin))
    pts = clip(pts, lambda p: p[1] <= ymax + _TOL, lambda p, q: iy(p, q, ymax))
    return pts


def _rot(pts, ang):
    c, s = math.cos(ang), math.sin(ang)
    return [(x * c - y * s, x * s + y * c) for x, y in pts]


def _partition(lo, hi, tile, min_size):
    """Cut boundaries ``[lo, ..., hi]`` along one axis.

    Greedy full-size tiles (use most of the bed); only deviate to avoid a
    remainder strip below ``min_size``. When the leftover would be a sub-min
    sliver, the last cut shifts so that strip == ``min_size`` and its neighbor
    stays as large as possible. ``min_size == 0`` -> plain fixed grid with a
    (possibly tiny) remainder as the last tile.
    """
    length = hi - lo
    if length <= tile + _TOL:
        return [lo, hi]
    min_size = min(min_size, tile)
    n_full = int(math.floor(length / tile + _TOL))
    r = length - n_full * tile
    bounds = [lo + i * tile for i in range(n_full + 1)]  # lo .. lo + n_full*tile
    if r <= _TOL:
        return bounds                       # exact multiple of the tile
    if r >= min_size:
        bounds.append(hi)                   # remainder tile is big enough
        return bounds
    # Sub-min remainder: pin the last strip to min_size, shrink its neighbor.
    bounds[-1] = hi - min_size
    bounds.append(hi)
    return bounds


def _seam_edges(ring, i, j, ncols, nrows, rx0, rx1, ry0, ry1):
    """Edges of a closed ``ring`` (already clipped to this tile) that fall on
    one of the tile's *interior* grid lines -- i.e. a seam shared with a
    neighboring tile, not the outer bed/material edge."""
    out = []
    for a, b in zip(ring, ring[1:] + ring[:1]):
        internal = (
            (i > 0 and abs(a[0] - rx0) < _TOL and abs(b[0] - rx0) < _TOL) or
            (i < ncols - 1 and abs(a[0] - rx1) < _TOL and abs(b[0] - rx1) < _TOL) or
            (j > 0 and abs(a[1] - ry0) < _TOL and abs(b[1] - ry0) < _TOL) or
            (j < nrows - 1 and abs(a[1] - ry1) < _TOL and abs(b[1] - ry1) < _TOL))
        if internal:
            out.append((a, b))
    return out


def hole_cut_paths(ring, seam_edges, tol=_TOL):
    """Split a clipped hole ``ring`` into the paths that should actually be cut.

    ``seam_edges`` (from ``tile_piece``'s ``hole_seam_edges``) are edges of
    this ring that duplicate the tile's own outer-boundary seam cut on the
    same grid line (see ``tile_piece``'s docstring) -- the outer polyline
    already traces that exact segment, so re-cutting it from the hole is a
    doubled cut on the machine. This drops each matched seam edge from the
    ring instead of re-closing through it.

    Returns a list of ``(points, closed)`` pairs to emit in place of the
    single closed ring:
      * no seam edges matched -> ``[(ring, True)]`` (unchanged).
      * one contiguous run remains -> a single open path (``closed=False``)
        covering every edge except the matched seam edge(s); the outer
        boundary's own pass through that line completes the visual seam.
      * seam edges are non-adjacent (e.g. a hole clipped at a tile corner,
        touching two different grid lines) -> multiple open paths, one per
        surviving run. UNPROVEN beyond the single-seam-edge case exercised
        by the test suite.
    """
    n = len(ring)
    if n < 2 or not seam_edges:
        return [(ring, True)]

    def close(p, q):
        return abs(p[0] - q[0]) < tol and abs(p[1] - q[1]) < tol

    removed = set()
    for a, b in seam_edges:
        for i in range(n):
            p, q = ring[i], ring[(i + 1) % n]
            if (close(p, a) and close(q, b)) or (close(p, b) and close(q, a)):
                removed.add(i)
                break

    if not removed:
        return [(ring, True)]
    if len(removed) >= n:
        return []

    start = next(((i + 1) % n for i in range(n) if i in removed))
    pieces = []
    run = [ring[start]]
    i = start
    for _ in range(n):
        if i in removed:
            if len(run) >= 2:
                pieces.append((run, False))
            run = [ring[(i + 1) % n]]
        else:
            run.append(ring[(i + 1) % n])
        i = (i + 1) % n
    if len(run) >= 2:
        pieces.append((run, False))
    return pieces


def tile_piece(outer, holes, tile_w, tile_h, rotation_deg=0.0, min_size=0.0):
    """Split a piece into bed-sized tiles.

    ``outer`` is the piece's closed outer polygon (cm); ``holes`` a list of
    closed hole polygons. Returns a list of tile dicts::

        {"outer": [...], "holes": [[...], ...], "cut_edges": [(a, b), ...],
         "hole_seam_edges": [[(a, b), ...], ...], "row": j, "col": i}

    with all coordinates back in the original frame.

    ``cut_edges`` are outer-boundary edges that land on an interior grid
    line (a tile-to-tile seam) -- used to place matching alignment
    fiducials on both sides.

    ``hole_seam_edges`` is parallel to ``holes``: for each clipped hole, the
    edges of *that hole* which also land on an interior grid line. When a
    hole straddles a tile boundary, the clipped hole re-closes with a new
    edge running along the same line as the outer boundary's own seam cut
    (see ``cut_edges``) -- that segment gets cut twice (once tracing the
    outer boundary, once tracing the hole) since it is emitted as part of
    two separate closed loops. This field makes that overlap visible to
    callers (e.g. to skip a duplicate fiducial there, or warn); it does not
    by itself merge the loops or remove the redundant cut -- doing that
    correctly requires deciding how the renderer should represent a
    boundary shared between two closed loops (SVG/DXF here have no notion of
    "open" cut path), which is a follow-up design choice.
    """
    rot = math.radians(rotation_deg)
    o = _rot(outer, -rot)
    hs = [_rot(h, -rot) for h in holes]

    xs = [p[0] for p in o]
    ys = [p[1] for p in o]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    xb = _partition(minx, maxx, tile_w, min_size)
    yb = _partition(miny, maxy, tile_h, min_size)
    ncols = len(xb) - 1
    nrows = len(yb) - 1

    tiles = []
    for j in range(nrows):
        ry0, ry1 = yb[j], yb[j + 1]
        for i in range(ncols):
            rx0, rx1 = xb[i], xb[i + 1]
            co = clip_polygon_rect(o, rx0, ry0, rx1, ry1)
            if len(co) < 3:
                continue
            chs = []
            hole_seams = []
            for h in hs:
                ch = clip_polygon_rect(h, rx0, ry0, rx1, ry1)
                if len(ch) >= 3:
                    chs.append(ch)
                    hole_seams.append(
                        _seam_edges(ch, i, j, ncols, nrows, rx0, rx1, ry0, ry1))

            cut = _seam_edges(co, i, j, ncols, nrows, rx0, rx1, ry0, ry1)

            def _back(edges):
                return [(_rot([a], rot)[0], _rot([b], rot)[0]) for a, b in edges]

            tiles.append({
                "outer": _rot(co, rot),
                "holes": [_rot(ch, rot) for ch in chs],
                "cut_edges": _back(cut),
                "hole_seam_edges": [_back(hseam) for hseam in hole_seams],
                "row": j, "col": i,
            })
    return tiles
