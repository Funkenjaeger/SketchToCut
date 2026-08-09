"""Tile an oversized piece into bed-sized tiles (dependency-free).

Used as the fallback when a region does not fit the cutter/laser bed at any
rotation (see :func:`core.fitting.fit_rotation`). The piece polygon (outer loop
+ holes, in cm) is clipped against an axis-aligned grid of tile rectangles with
a Weiler-Atherton style boolean (see :func:`clip_polygon_rect`), so each tile
piece is **re-closed** along the cut lines -- and a concave piece whose
intersection with one tile falls into several disjoint regions yields one loop
per region, not a single loop stitched together along the tile edge. Edges that
fall on an interior grid line are reported as ``cut_edges`` so the caller can
drop matching alignment fiducials on both sides of every seam.

The grid can be rotated (``rotation_deg``): geometry is taken into the grid's
frame, tiled axis-aligned, and the results rotated back. At 0 deg the tile
dimensions are honoured verbatim.

Limitations (documented, acceptable for the manual-sectioning-first workflow):
  * Tiles are butt-joint by default (``overlap=0``); pass ``overlap`` for a
    poster-style shared margin with crop marks (see :func:`tile_piece`).
  * Self-intersecting input polygons are out of scope; the clip assumes each
    input ring is simple.
  * Two regions that meet *only* along a zero-width stretch of the tile edge --
    a grid line landing exactly on the floor of a notch -- still come back as
    one loop that runs out and back along that line. Closed-set-wise they do
    touch; splitting them would need an explicit "open the contact" rule.
"""

import math

_TOL = 1e-6


def _lerp(a, b, t):
    return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))


def _same(p, q, tol=_TOL):
    return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol


def _signed_area(pts):
    s = 0.0
    for a, b in zip(pts, pts[1:] + pts[:1]):
        s += a[0] * b[1] - b[0] * a[1]
    return 0.5 * s


def _point_in_polygon(p, poly):
    """Ray-cast containment test (points exactly on an edge are undefined)."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            xc = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if x < xc:
                inside = not inside
    return inside


def _snap(p, xmin, ymin, xmax, ymax, tol):
    """Pull a point onto the rect boundary when it is within ``tol`` of it.

    Keeps seam detection (:func:`_seam_edges`, exact-ish comparisons) and the
    perimeter parameterisation below working on coordinates that only came
    within rounding distance of a grid line.
    """
    x, y = p
    if abs(x - xmin) <= tol:
        x = xmin
    elif abs(x - xmax) <= tol:
        x = xmax
    if abs(y - ymin) <= tol:
        y = ymin
    elif abs(y - ymax) <= tol:
        y = ymax
    return (x, y)


def _clip_segment(a, b, xmin, ymin, xmax, ymax, tol):
    """Liang-Barsky: the ``(t0, t1)`` slice of segment ``a->b`` inside the rect.

    Returns ``None`` when the segment misses the rect. A segment lying exactly
    along a rect edge counts as inside (matching the closed-rect convention
    used everywhere else here).
    """
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - xmin), (dx, xmax - a[0]),
                 (-dy, a[1] - ymin), (dy, ymax - a[1])):
        if p == 0.0:
            if q < -tol:
                return None                 # parallel to this edge, outside it
            continue
        r = q / p
        if p < 0.0:
            if r > t1:
                return None
            if r > t0:
                t0 = r
        else:
            if r < t0:
                return None
            if r < t1:
                t1 = r
    return t0, t1


def _inside_chains(pts, xmin, ymin, xmax, ymax, tol):
    """Split a ring into its maximal runs *inside* the rect.

    Returns ``(chains, closed_ring)``. ``chains`` are open polylines that each
    start and end on the rect boundary, in ring order. ``closed_ring`` is set
    instead (with ``chains`` empty) when the ring never leaves the rect.
    """
    n = len(pts)
    chains = []
    cur = None
    first_from_vertex0 = False
    left = False                # did the ring ever step outside the rect?
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        seg = _clip_segment(a, b, xmin, ymin, xmax, ymax, tol)
        if seg is None:
            left = True
            if cur is not None:
                chains.append(cur)
                cur = None
            continue
        t0, t1 = seg
        p0 = _snap(_lerp(a, b, t0), xmin, ymin, xmax, ymax, tol)
        p1 = _snap(_lerp(a, b, t1), xmin, ymin, xmax, ymax, tol)
        if cur is not None and _same(cur[-1], p0, tol):
            if not _same(cur[-1], p1, tol):
                cur.append(p1)
            continue
        if cur is not None:
            chains.append(cur)
            left = True             # a break between two inside runs
        cur = [p0] if _same(p0, p1, tol) else [p0, p1]
        if i == 0 and t0 <= tol:
            first_from_vertex0 = True

    if cur is not None:
        if not chains and not left:
            ring = cur[:-1] if len(cur) > 1 and _same(cur[0], cur[-1], tol) \
                else cur
            return [], ring
        if first_from_vertex0 and _same(cur[-1], chains[0][0], tol):
            chains[0] = cur + chains[0][1:]     # the ring wrapped past pts[0]
        else:
            chains.append(cur)
    # A chain that never leaves its entry point is a graze, not a region: drop
    # it (that removes one entry *and* one exit, so the walk stays balanced).
    return [c for c in chains if not _degenerate(c, tol)], None


def _degenerate(chain, tol):
    return all(_same(chain[0], p, tol) for p in chain)


def _perimeter_param(p, xmin, ymin, xmax, ymax):
    """Position of a boundary point along the rect perimeter, CCW from
    ``(xmin, ymin)``. Corners get one consistent value from either side."""
    x, y = p
    w, h = xmax - xmin, ymax - ymin

    def clamp(v, hi):
        return 0.0 if v < 0.0 else (hi if v > hi else v)

    db, dr = abs(y - ymin), abs(x - xmax)
    dt, dl = abs(y - ymax), abs(x - xmin)
    m = min(db, dr, dt, dl)
    if db == m:
        return clamp(x - xmin, w)
    if dr == m:
        return w + clamp(y - ymin, h)
    if dt == m:
        return w + h + clamp(xmax - x, w)
    return 2.0 * w + h + clamp(ymax - y, h)


def _stitch(chains, xmin, ymin, xmax, ymax, tol):
    """Close the inside-chains into loops by walking the rect boundary.

    Weiler-Atherton's rule, specialised to a rectangle: leaving the subject at
    an exit point, follow the clip boundary in the subject's own winding
    direction (CCW here) until the *first* entry point, and resume there.
    Disjoint intersections therefore close into separate loops instead of being
    bridged by a zero-width seam along the tile edge.
    """
    w, h = xmax - xmin, ymax - ymin
    per = 2.0 * (w + h)
    if per <= 0.0:
        return []
    corners = ((xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax))
    cpar = (0.0, w, w + h, 2.0 * w + h)

    starts = [_perimeter_param(c[0], xmin, ymin, xmax, ymax) for c in chains]
    ends = [_perimeter_param(c[-1], xmin, ymin, xmax, ymax) for c in chains]

    nxt = []
    for i in range(len(chains)):
        best, bestd = 0, None
        for j in range(len(chains)):
            d = (starts[j] - ends[i]) % per
            if bestd is None or d < bestd:
                best, bestd = j, d
        nxt.append((best, bestd))

    out = []
    seen = set()
    for i in range(len(chains)):
        if i in seen:
            continue
        loop = []
        j = i
        while j not in seen:
            seen.add(j)
            loop.extend(chains[j])
            k, d = nxt[j]
            walk = []
            for ci in range(4):
                cd = (cpar[ci] - ends[j]) % per
                if tol < cd < d - tol:
                    walk.append((cd, corners[ci]))
            walk.sort()
            loop.extend(c for _cd, c in walk)
            j = k
        if len(loop) >= 3:
            out.append(loop)
    return out


def _dedup(loop, tol):
    out = []
    for p in loop:
        if not out or not _same(out[-1], p, tol):
            out.append(p)
    while len(out) > 1 and _same(out[0], out[-1], tol):
        out.pop()
    return out


def clip_polygon_rect(poly, xmin, ymin, xmax, ymax, tol=_TOL):
    """Intersect a closed polygon with an axis-aligned rect.

    Returns a **list of closed loops** (each a list of points, no repeated
    closing vertex) -- the intersection of a concave polygon with a rectangle
    can be several disjoint regions, and each comes back as its own loop.
    An empty list means the polygon and the rect do not overlap in area.

    The implementation is Weiler-Atherton specialised to a rectangular clip
    window: walk the subject ring collecting the runs that lie inside the rect
    (:func:`_inside_chains`), then close those runs by walking the rect
    perimeter from each exit point to the next entry point in winding order
    (:func:`_stitch`). Sutherland-Hodgman was used here previously; it is
    smaller but cannot represent a disjoint result, and joined the regions with
    a zero-width seam running back along the tile boundary instead.

    Input winding is preserved in the output. Degenerate results (fewer than 3
    distinct points, or effectively zero area) are dropped.
    """
    if not poly or len(poly) < 3:
        return []
    flip = _signed_area(poly) < 0.0
    pts = list(reversed(poly)) if flip else list(poly)

    chains, ring = _inside_chains(pts, xmin, ymin, xmax, ymax, tol)
    if ring is not None:
        raw = [ring]
    elif chains:
        raw = _stitch(chains, xmin, ymin, xmax, ymax, tol)
    else:
        # The ring misses the rect entirely: the rect is either wholly inside
        # the polygon (result: the whole rect) or wholly outside it.
        mid = (0.5 * (xmin + xmax), 0.5 * (ymin + ymax))
        raw = [[(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]] \
            if _point_in_polygon(mid, pts) else []

    out = []
    for loop in raw:
        loop = _dedup(loop, tol)
        if len(loop) >= 3 and abs(_signed_area(loop)) > tol:
            out.append(list(reversed(loop)) if flip else loop)
    return out


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


def _crop_marks(i, j, ncols, nrows, gx0, gx1, gy0, gy1, crop_len):
    """Short trim guides on the *nominal* grid lines a tile straddles.

    With ``overlap > 0`` a tile's cut rectangle is grown past the grid line, so
    there is no butt seam to place matched ticks on. Instead each interior grid
    line the cell borders gets two short marks lying *on that line* (one at each
    end of the cell), pointing along it. Both tiles that share the line carry the
    same two marks at the same coordinates, so overlaying the two prints and
    lining the marks up registers the overlap; trimming along a mark yields a
    clean butt join. ``crop_len`` is clamped so a mark never exceeds half its
    grid line's span. Marks are grid-based: on a concave clip one can fall
    outside the actual cut shape (documented, acceptable for the poster case).
    """
    marks = []
    lx = min(crop_len, 0.5 * (gy1 - gy0))     # along-Y marks for vertical lines
    ly = min(crop_len, 0.5 * (gx1 - gx0))     # along-X marks for horizontal lines
    if i > 0 and lx > 0:                       # left grid line x == gx0
        marks.append(((gx0, gy0), (gx0, gy0 + lx)))
        marks.append(((gx0, gy1), (gx0, gy1 - lx)))
    if i < ncols - 1 and lx > 0:               # right grid line x == gx1
        marks.append(((gx1, gy0), (gx1, gy0 + lx)))
        marks.append(((gx1, gy1), (gx1, gy1 - lx)))
    if j > 0 and ly > 0:                       # bottom grid line y == gy0
        marks.append(((gx0, gy0), (gx0 + ly, gy0)))
        marks.append(((gx1, gy0), (gx1 - ly, gy0)))
    if j < nrows - 1 and ly > 0:               # top grid line y == gy1
        marks.append(((gx0, gy1), (gx0 + ly, gy1)))
        marks.append(((gx1, gy1), (gx1 - ly, gy1)))
    return marks


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


def _holes_of(loop, all_loops, hole_loops):
    """Which clipped holes belong to the clipped outer ``loop``.

    With a single outer loop per tile (the overwhelmingly common case) every
    hole is its own -- no test needed. When one tile rectangle caught several
    disjoint parts of a concave piece, each clipped hole sits inside exactly
    one of them; it is assigned by sampling the hole's vertices and edge
    midpoints (a vertex may sit exactly on a shared tile edge, where a
    containment test is undefined, so the best-scoring outer wins rather than
    the first).
    """
    if len(all_loops) < 2:
        return list(hole_loops)
    mine = []
    for hl in hole_loops:
        samples = list(hl)
        samples.extend(_lerp(a, b, 0.5)
                       for a, b in zip(hl, hl[1:] + hl[:1]))
        best, score = all_loops[0], 0    # never drop a hole on an all-zero tie
        for cand in all_loops:
            n = sum(1 for s in samples if _point_in_polygon(s, cand))
            if n > score:
                best, score = cand, n
        if best is loop:
            mine.append(hl)
    return mine


def tile_piece(outer, holes, tile_w, tile_h, rotation_deg=0.0, min_size=0.0,
               overlap=0.0, crop_len=0.6):
    """Split a piece into bed-sized tiles.

    ``outer`` is the piece's closed outer polygon (cm); ``holes`` a list of
    closed hole polygons. Returns a list of tile dicts::

        {"outer": [...], "holes": [[...], ...], "cut_edges": [(a, b), ...],
         "hole_seam_edges": [[(a, b), ...], ...], "crop_marks": [(a, b), ...],
         "row": j, "col": i}

    with all coordinates back in the original frame.

    ``overlap`` (cm, default ``0`` -> exact butt-joint, output unchanged) grows
    every tile past each *interior* grid line by ``overlap / 2`` on that side, so
    two adjacent tiles share a margin ``overlap`` wide (poster-style). The grid
    itself is laid out on ``tile_w - overlap`` x ``tile_h - overlap`` cells so a
    grown tile still fits ``tile_w`` x ``tile_h`` (a tile never exceeds the bed).
    An overlapped tile carries ``crop_marks`` (short trim guides on the nominal
    grid lines, see :func:`_crop_marks`) *instead* of ``cut_edges`` -- there is
    no shared butt seam to match ticks on -- and its ``cut_edges`` /
    ``hole_seam_edges`` come back empty. With ``overlap == 0`` ``crop_marks`` is
    empty and ``cut_edges`` / ``hole_seam_edges`` behave exactly as before.
    ``crop_len`` is the length of each crop mark (clamped to half its line span).

    One grid cell can yield **more than one** tile dict: a concave piece can
    meet a single tile rectangle in several disjoint regions, and those are
    genuinely separate physical pieces. They share the same ``row``/``col``.

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
    if overlap < 0.0:
        raise ValueError("overlap must be >= 0 (got %r)" % (overlap,))
    if overlap >= tile_w or overlap >= tile_h:
        raise ValueError("overlap %r must be smaller than the tile (%r x %r)"
                         % (overlap, tile_w, tile_h))
    half_ov = 0.5 * overlap

    rot = math.radians(rotation_deg)
    o = _rot(outer, -rot)
    hs = [_rot(h, -rot) for h in holes]

    xs = [p[0] for p in o]
    ys = [p[1] for p in o]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    # Lay the grid on cells shrunk by the overlap so a grown tile still fits the
    # bed; at overlap == 0 this is the original full-size partition.
    xb = _partition(minx, maxx, tile_w - overlap, min_size)
    yb = _partition(miny, maxy, tile_h - overlap, min_size)
    ncols = len(xb) - 1
    nrows = len(yb) - 1

    tiles = []
    for j in range(nrows):
        gy0, gy1 = yb[j], yb[j + 1]                      # nominal cell (grid)
        cy0 = gy0 - (half_ov if j > 0 else 0.0)          # grown clip rect
        cy1 = gy1 + (half_ov if j < nrows - 1 else 0.0)
        for i in range(ncols):
            gx0, gx1 = xb[i], xb[i + 1]
            cx0 = gx0 - (half_ov if i > 0 else 0.0)
            cx1 = gx1 + (half_ov if i < ncols - 1 else 0.0)
            outers = clip_polygon_rect(o, cx0, cy0, cx1, cy1)
            if not outers:
                continue
            hole_loops = []
            for h in hs:
                hole_loops.extend(clip_polygon_rect(h, cx0, cy0, cx1, cy1))

            def _back(edges):
                return [(_rot([a], rot)[0], _rot([b], rot)[0]) for a, b in edges]

            crops = _back(_crop_marks(i, j, ncols, nrows,
                                      gx0, gx1, gy0, gy1, crop_len)) \
                if overlap > 0.0 else []

            for co in outers:
                chs = _holes_of(co, outers, hole_loops)
                # Butt-joint seams (and their doubled-hole cuts) only make sense
                # when tiles meet exactly on the grid line; with an overlap the
                # crop marks take over and there are no shared seam edges.
                if overlap > 0.0:
                    cut = []
                    hole_seams = [[] for _ in chs]
                else:
                    cut = _seam_edges(co, i, j, ncols, nrows,
                                      gx0, gx1, gy0, gy1)
                    hole_seams = [
                        _seam_edges(ch, i, j, ncols, nrows, gx0, gx1, gy0, gy1)
                        for ch in chs]
                tiles.append({
                    "outer": _rot(co, rot),
                    "holes": [_rot(ch, rot) for ch in chs],
                    "cut_edges": _back(cut),
                    "hole_seam_edges": [_back(hseam) for hseam in hole_seams],
                    "crop_marks": crops,
                    "row": j, "col": i,
                })
    return tiles
