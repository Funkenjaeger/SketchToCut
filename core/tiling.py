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


def tile_piece(outer, holes, tile_w, tile_h, rotation_deg=0.0):
    """Split a piece into bed-sized tiles.

    ``outer`` is the piece's closed outer polygon (cm); ``holes`` a list of
    closed hole polygons. Returns a list of tile dicts::

        {"outer": [...], "holes": [[...], ...], "cut_edges": [(a, b), ...],
         "row": j, "col": i}

    with all coordinates back in the original frame.
    """
    rot = math.radians(rotation_deg)
    o = _rot(outer, -rot)
    hs = [_rot(h, -rot) for h in holes]

    xs = [p[0] for p in o]
    ys = [p[1] for p in o]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    ncols = max(1, int(math.ceil((maxx - minx - _TOL) / tile_w)))
    nrows = max(1, int(math.ceil((maxy - miny - _TOL) / tile_h)))

    tiles = []
    for j in range(nrows):
        ry0 = miny + j * tile_h
        ry1 = ry0 + tile_h
        for i in range(ncols):
            rx0 = minx + i * tile_w
            rx1 = rx0 + tile_w
            co = clip_polygon_rect(o, rx0, ry0, rx1, ry1)
            if len(co) < 3:
                continue
            chs = []
            for h in hs:
                ch = clip_polygon_rect(h, rx0, ry0, rx1, ry1)
                if len(ch) >= 3:
                    chs.append(ch)

            cut = []
            for a, b in zip(co, co[1:] + co[:1]):
                internal = (
                    (i > 0 and abs(a[0] - rx0) < _TOL and abs(b[0] - rx0) < _TOL) or
                    (i < ncols - 1 and abs(a[0] - rx1) < _TOL and abs(b[0] - rx1) < _TOL) or
                    (j > 0 and abs(a[1] - ry0) < _TOL and abs(b[1] - ry0) < _TOL) or
                    (j < nrows - 1 and abs(a[1] - ry1) < _TOL and abs(b[1] - ry1) < _TOL))
                if internal:
                    cut.append((a, b))

            tiles.append({
                "outer": _rot(co, rot),
                "holes": [_rot(ch, rot) for ch in chs],
                "cut_edges": [(_rot([a], rot)[0], _rot([b], rot)[0])
                              for a, b in cut],
                "row": j, "col": i,
            })
    return tiles
