"""Standalone unit tests for the dependency-free SVG core.

Run from the add-in folder (so `core` is importable) with a normal Python --
no Fusion required:

    python test_core.py

Exercises 1:1 scaling, the Y-flip, unit conversion, bbox/translate, and the
arc sweep-flag convention.
"""

import math
import random
import re
import sys

from core import geometry as g
from core import svg
from core import dxf
from core import loops
from core import fiducials
from core import fitting
from core import tiling
from core import palette
from core import packing


_failures = []


def check(cond, msg):
    if cond:
        print("  PASS:", msg)
    else:
        print("  FAIL:", msg)
        _failures.append(msg)


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def attr(svg_text, name):
    m = re.search(r'%s="([^"]*)"' % name, svg_text)
    return m.group(1) if m else None


def test_square_mm():
    print("test_square_mm (100mm square, unit=mm):")
    # 10cm x 10cm == 100mm x 100mm, bottom-left at origin.
    square = g.Polyline([(0, 0), (10, 0), (10, 10), (0, 10)], closed=True)
    out = svg.render([square], unit="mm", margin=0.0)

    check(attr(out, "width") == "100mm", "width == 100mm")
    check(attr(out, "height") == "100mm", "height == 100mm")
    check(attr(out, "viewBox") == "0 0 100 100", "viewBox == 0 0 100 100")

    # Y-flip: sketch (0,0) is bottom-left -> SVG y == height (100);
    #          sketch (0,10) is top-left -> SVG y == 0.
    d = attr(out, "d")
    first = d.split("L")[0]  # "M 0 100 "
    check(first.strip() == "M 0 100", "bottom-left (0,0) maps to SVG (0,100)")
    check("L 100 100" in d, "point (10,0) maps to SVG (100,100)")
    check("L 100 0" in d, "top-right (10,10) maps to SVG (100,0)")
    check(d.rstrip().endswith("Z"), "closed polyline ends with Z")


def test_inch_scaling():
    print("test_inch_scaling (2.54cm line, unit=in):")
    # A 2.54cm horizontal line == exactly 1 inch.
    line = g.Line((0, 0), (2.54, 0))
    out = svg.render([line], unit="in", margin=0.0)
    check(attr(out, "width") == "1in", "2.54cm width renders as 1in")
    # Degenerate height (a pure horizontal line) -> 0; just confirm no crash.
    check(attr(out, "height") is not None, "height attribute present")


def test_bbox_translate_margin():
    print("test_bbox_translate_margin (offset geometry + margin):")
    # Geometry not at origin: a 5x3 cm box with min corner at (2, 4).
    poly = g.Polyline([(2, 4), (7, 4), (7, 7), (2, 7)], closed=True)
    out = svg.render([poly], unit="mm", margin=5.0)  # 5mm margin
    # 5cm x 3cm == 50mm x 30mm, plus 2*5mm margin -> 60 x 40.
    check(attr(out, "width") == "60mm", "width == 50mm geometry + 10mm margin")
    check(attr(out, "height") == "40mm", "height == 30mm geometry + 10mm margin")
    d = attr(out, "d")
    # min-corner (2,4) -> +margin in X, and flipped+margin in Y (top of page).
    # X: (2-2)*10 + 5 = 5 ; Y: (7-4)*10 + 5 = 35
    check(d.startswith("M 5 35"), "offset min-corner maps with margin + flip")


def test_circle():
    print("test_circle (native circle, unit=mm):")
    c = g.Circle((1, 1), 1)  # center (1,1)cm, r=1cm -> r=10mm
    out = svg.render([c], unit="mm")
    check(attr(out, "r") == "10", "radius 1cm renders as r=10 (mm)")
    check(attr(out, "cx") == "10", "cx maps to 10mm")
    check(attr(out, "cy") == "10", "cy maps to 10mm (flipped, symmetric here)")


def test_arc_sweep_flag():
    print("test_arc_sweep_flag (quarter arc, unit=cm):")
    # CCW quarter arc, center origin, r=5, from angle 0 -> pi/2.
    arc = g.Arc((0, 0), 5, 0.0, math.pi / 2.0, ccw=True)
    ext = arc.extents()
    check(all(approx(a, b) for a, b in zip(ext, (0, 0, 5, 5))),
          "arc extents == (0,0,5,5)  (got: %r)" % (ext,))
    out = svg.render([arc], unit="cm", margin=0.0)
    d = attr(out, "d")
    # start (5,0)->SVG(5,5); end (0,5)->SVG(0,0); large=0, sweep=0 for CCW.
    check(d == "M 5 5 A 5 5 0 0 0 0 0",
          "CCW quarter arc -> large-arc=0, sweep-flag=0  (got: %s)" % d)

    arc_cw = g.Arc((0, 0), 5, 0.0, math.pi / 2.0, ccw=False)
    d2 = attr(svg.render([arc_cw], unit="cm", margin=0.0), "d")
    check(" 0 1 " in d2, "CW arc -> sweep-flag=1  (got: %s)" % d2)


def test_ellipse_extents():
    print("test_ellipse_extents (axis-aligned + rotated):")
    e = g.Ellipse((0, 0), 4, 2, 0.0)
    check(e.extents() == (-4, -2, 4, 2), "axis-aligned ellipse bbox")
    e90 = g.Ellipse((0, 0), 4, 2, math.pi / 2.0)
    minx, miny, maxx, maxy = e90.extents()
    check(approx(maxx, 2) and approx(maxy, 4),
          "90-deg rotated ellipse swaps extents")


def test_units_and_empty():
    print("test_units_and_empty (helpers + guards):")
    check(approx(g.cm_to("in"), 1 / 2.54), "cm_to(in)")
    check(approx(g.cm_to("mm"), 10.0), "cm_to(mm)")
    try:
        svg.render([], unit="mm")
        check(False, "empty geometry should raise")
    except ValueError:
        check(True, "empty geometry raises ValueError")


def test_chain_loop():
    print("test_chain_loop (order/orientation tolerant closed ring):")
    # A unit square given as 4 segments, shuffled and some reversed.
    segs = [
        [(0, 0), (1, 0)],
        [(1, 1), (1, 0)],   # reversed
        [(0, 1), (1, 1)],
        [(0, 0), (0, 1)],   # reversed
    ]
    ring = loops.chain_loop(segs)
    check(len(ring) == 4, "square chains to 4 unique vertices (got %d)" % len(ring))
    # Every original corner is present.
    corners = {(0, 0), (1, 0), (1, 1), (0, 1)}
    check(set(ring) == corners, "ring visits exactly the 4 corners")
    # Consecutive vertices are adjacent (unit edges only, no diagonals).
    ok = True
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if not approx(math.hypot(b[0] - a[0], b[1] - a[1]), 1.0):
            ok = False
    check(ok, "consecutive vertices are connected edges (no diagonal jumps)")

    # A multi-point polyline segment (flattened arc) chains too.
    segs2 = [[(0, 0), (2, 0)], [(2, 0), (1, 0.5), (0, 0)]]
    ring2 = loops.chain_loop(segs2)
    check(len(ring2) == 3, "polyline + line chains into a 3-vertex ring")

    # Disconnected input raises.
    try:
        loops.chain_loop([[(0, 0), (1, 0)], [(5, 5), (6, 5)]])
        check(False, "disconnected loop should raise")
    except ValueError:
        check(True, "disconnected loop raises ValueError")


def test_edge_ticks():
    print("test_edge_ticks (perpendicular, correct side + count):")
    # Horizontal shared edge from (0,0) to (10,0); interior is above (toward +y).
    edge = [(0, 0), (10, 0)]
    ticks = fiducials.edge_ticks(edge, toward=(5, 5), length=0.6, spacing=4.0,
                                 inset=0.3)
    check(len(ticks) >= 2, "at least end ticks produced (got %d)" % len(ticks))
    all_perp = True
    all_up = True
    right_len = True
    for t in ticks:
        dx = t.p1[0] - t.p0[0]
        dy = t.p1[1] - t.p0[1]
        if not approx(dx, 0.0):
            all_perp = False           # perpendicular to a horizontal edge => vertical
        if dy <= 0:
            all_up = False             # interior is +y
        if not approx(math.hypot(dx, dy), 0.3):
            right_len = False          # half of length 0.6
    check(all_perp, "ticks are perpendicular to the edge")
    check(all_up, "ticks point toward the interior (+y)")
    check(right_len, "each tick is length/2 long")

    # Flip the interior side -> ticks point down.
    ticks_dn = fiducials.edge_ticks(edge, toward=(5, -5), length=0.6, spacing=4.0)
    check(all(t.p1[1] < t.p0[1] for t in ticks_dn),
          "interior below -> ticks point -y")


def test_fit_rotation():
    print("test_fit_rotation (auto-rotate to fit a bed):")
    # 18 x 6 piece vs a 12 x 24 bed: does NOT fit at 0 (18>12) but fits at 90.
    rect_18x6 = [(0, 0), (18, 0), (18, 6), (0, 6)]
    theta = fitting.fit_rotation(rect_18x6, 12, 24, step_deg=1.0)
    check(theta is not None, "18x6 fits in 12x24 at some rotation")
    bw, bh = fitting.aabb_size(fitting.rotate_points(rect_18x6, theta))
    check(bw <= 12 + 1e-6 and bh <= 24 + 1e-6,
          "rotated 18x6 AABB fits within 12x24 (got %.2f x %.2f)" % (bw, bh))
    check(approx(theta, math.radians(90), tol=math.radians(0.01)),
          "18x6 prefers a clean 90 deg (got %.1f deg)" % math.degrees(theta))

    # 10 x 6 already fits a 12 x 24 bed -> theta 0, no rotation.
    check(fitting.fit_rotation([(0, 0), (10, 0), (10, 6), (0, 6)], 12, 24) == 0.0,
          "already-fitting piece returns theta=0")

    # A 30 x 30 piece cannot fit a 12 x 24 bed at any angle.
    big = [(0, 0), (30, 0), (30, 30), (0, 30)]
    check(fitting.fit_rotation(big, 12, 24) is None,
          "oversized piece returns None")

    # A long thin bar at 45 deg only fits a smallish square bed when rotated.
    bar = [(0, 0), (14, 14), (13.3, 14.7), (-0.7, 0.7)]  # ~20 long, ~1 wide, at 45
    check(fitting.fit_rotation(bar, 2, 22, step_deg=1.0) is not None,
          "diagonal bar fits a 2x22 bed at a non-orthogonal rotation")


def test_svg_fiducials_and_labels():
    print("test_svg_fiducials_and_labels (separate group + text):")
    piece = g.Polyline([(0, 0), (5, 0), (5, 5), (0, 5)], closed=True)
    tick = g.Line((2.5, 0), (2.5, 0.3))
    out = svg.render([piece], unit="mm", fiducials=[tick],
                     labels=[("A", (2.5, 2.5), 1.0)])
    check('class="fiducial"' in out, "a fiducial group is present")
    check(out.count("<g ") == 2, "exactly two groups (cut + fiducial)")
    check("<text" in out and ">A<" in out, "label text 'A' emitted")
    # Label sits inside the fiducial group, not the cut group.
    frag = out.split('class="fiducial"')[1]
    check("<text" in frag, "label is inside the fiducial group")


def test_rotate_and_centroid():
    print("test_rotate_and_centroid (geometry helpers):")
    sq = [(0, 0), (2, 0), (2, 2), (0, 2)]
    cx, cy = g.polygon_centroid(sq)
    check(approx(cx, 1) and approx(cy, 1), "square centroid is its center")
    # Rotating a Circle moves its center, keeps radius.
    c = g.Circle((2, 0), 1)
    rc = g.rotate_element(c, math.pi / 2, center=(0, 0))
    check(approx(rc.center[0], 0) and approx(rc.center[1], 2) and rc.radius == 1,
          "circle rotates about origin, radius unchanged")
    check(g.point_in_polygon((1, 1), sq), "point inside square")
    check(not g.point_in_polygon((3, 1), sq), "point outside square")
    check(not g.point_in_polygon((-1, 1), sq), "point left of square outside")


def test_tiling():
    print("test_tiling (rect clip + grid split):")
    sq = [(0, 0), (2, 0), (2, 2), (0, 2)]
    clipped = tiling.clip_polygon_rect(sq, 1, 0, 2, 2)   # keep right half
    check(len(clipped) == 1, "convex clip -> a single loop (got %d)"
          % len(clipped))
    xs = [p[0] for p in clipped[0]]
    check(clipped and approx(min(xs), 1) and approx(max(xs), 2),
          "clip to right half keeps x in [1,2]")
    check(tiling.clip_polygon_rect(sq, 5, 5, 6, 6) == [],
          "clip fully outside -> empty")

    big = [(0, 0), (30, 0), (30, 30), (0, 30)]
    tiles = tiling.tile_piece(big, [], 12, 24)
    check(len(tiles) == 6, "30x30 -> 3x2 = 6 tiles (got %d)" % len(tiles))
    okfit = True
    for t in tiles:
        tx = [p[0] for p in t["outer"]]
        ty = [p[1] for p in t["outer"]]
        if (max(tx) - min(tx)) > 12 + 1e-6 or (max(ty) - min(ty)) > 24 + 1e-6:
            okfit = False
    check(okfit, "each tile fits within 12x24")
    check(any(t["cut_edges"] for t in tiles), "interior tiles report cut edges")
    check(len(tiling.tile_piece(big, [], 12, 24, rotation_deg=90)) >= 1,
          "rotated grid still tiles")

    # A piece that already fits a tile yields a single tile with no cuts.
    one = tiling.tile_piece([(0, 0), (5, 0), (5, 5), (0, 5)], [], 12, 24)
    check(len(one) == 1 and not one[0]["cut_edges"],
          "piece smaller than a tile -> 1 tile, no cut edges")

    # A hole straddling a tile boundary re-closes with an edge that
    # duplicates the outer's own seam cut on that line (item 4 of the
    # auto-tiling refinements: "holes straddling a tile boundary produce a
    # doubled cut"). hole_seam_edges surfaces exactly that overlap.
    outer20 = [(0, 0), (20, 0), (20, 10), (0, 10)]
    straddle = [(8, 4), (12, 4), (12, 6), (8, 6)]  # crosses x=10
    tiles_h = tiling.tile_piece(outer20, [straddle], tile_w=10, tile_h=10)
    check(len(tiles_h) == 2, "20x10 piece with straddling hole -> 2 tiles")
    for t in tiles_h:
        seams = t["hole_seam_edges"][0]
        check(len(seams) == 1, "straddling hole reports exactly 1 seam edge "
              "in tile (row=%d col=%d), got %d" % (t["row"], t["col"], len(seams)))
        a, b = seams[0]
        check(approx(a[0], 10) and approx(b[0], 10),
              "the reported hole seam edge lies on the tile boundary x=10")

    # A hole fully inside one tile (not touching any boundary) reports no
    # seam edges for either tile.
    inside = [(2, 4), (4, 4), (4, 6), (2, 6)]
    tiles_i = tiling.tile_piece(outer20, [inside], tile_w=10, tile_h=10)
    for t in tiles_i:
        seams = [s for hseam in t["hole_seam_edges"] for s in hseam]
        check(not seams, "hole fully inside a tile -> no hole_seam_edges "
              "(row=%d col=%d)" % (t["row"], t["col"]))

    # Item 4 fix: hole_cut_paths() drops the duplicated seam edge instead of
    # re-closing through it (the outer boundary already cuts that segment).
    for t in tiles_h:
        h = t["holes"][0]
        seams = t["hole_seam_edges"][0]
        paths = tiling.hole_cut_paths(h, seams)
        check(len(paths) == 1 and paths[0][1] is False,
              "straddling hole -> one OPEN cut path, not a re-closed loop "
              "(row=%d col=%d)" % (t["row"], t["col"]))
        pts = paths[0][0]
        edges = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        a, b = seams[0]
        dup = any((approx(p[0], a[0]) and approx(p[1], a[1]) and
                   approx(q[0], b[0]) and approx(q[1], b[1])) or
                  (approx(p[0], b[0]) and approx(p[1], b[1]) and
                   approx(q[0], a[0]) and approx(q[1], a[1]))
                  for p, q in edges)
        check(not dup, "open cut path excludes the outer's own seam segment "
              "(row=%d col=%d)" % (t["row"], t["col"]))
    for t in tiles_i:
        if not t["holes"]:
            continue
        h = t["holes"][0]
        paths = tiling.hole_cut_paths(h, [])
        check(len(paths) == 1 and paths[0][1] is True and paths[0][0] == h,
              "hole with no seam edges -> unchanged closed ring")

    # Sliver-aware partition.
    check(tiling._partition(0, 13, 12, 3) == [0, 10, 13],
          "sub-min remainder shifts: [0,10,13]")
    check(tiling._partition(0, 13, 12, 0) == [0, 12, 13],
          "min=0 keeps fixed grid remainder: [0,12,13]")
    check(tiling._partition(0, 24, 12, 3) == [0, 12, 24],
          "exact multiple: [0,12,24]")
    check(tiling._partition(0, 23, 12, 3) == [0, 12, 23],
          "big remainder kept: [0,12,23]")
    check(tiling._partition(0, 5, 12, 3) == [0, 5],
          "shorter than a tile: single span")


def _area(loop):
    s = 0.0
    for a, b in zip(loop, loop[1:] + loop[:1]):
        s += a[0] * b[1] - b[0] * a[1]
    return abs(0.5 * s)


def _seam_overlap(loop, eps=1e-9):
    """Length of the longest zero-width seam in a closed ``loop``.

    A seam is two *distinct* edges of the same loop that are collinear and
    overlap along a shared stretch -- the loop doubling back on itself. That
    is exactly the artefact Sutherland-Hodgman leaves when it bridges two
    disjoint intersection regions along the tile boundary, and it is what
    makes the result a single degenerate loop instead of two real ones.

    Deliberately not restricted to *consecutive* edges: in the U-shape case
    below the bridge is the far end of a long boundary edge, several vertices
    away from the return trip along the same line.
    """
    edges = list(zip(loop, loop[1:] + loop[:1]))
    worst = 0.0
    for i in range(len(edges)):
        p, q = edges[i]
        dx, dy = q[0] - p[0], q[1] - p[1]
        dd = dx * dx + dy * dy
        if dd <= eps:
            continue
        for j in range(i + 1, len(edges)):
            r, s = edges[j]
            # collinear? both direction and offset cross-products vanish
            if abs(dx * (s[1] - r[1]) - dy * (s[0] - r[0])) > eps:
                continue
            if abs(dx * (r[1] - p[1]) - dy * (r[0] - p[0])) > eps:
                continue
            tr = ((r[0] - p[0]) * dx + (r[1] - p[1]) * dy) / dd
            ts = ((s[0] - p[0]) * dx + (s[1] - p[1]) * dy) / dd
            lo, hi = min(tr, ts), max(tr, ts)
            span = min(hi, 1.0) - max(lo, 0.0)
            if span > 0:
                worst = max(worst, span * math.sqrt(dd))
    return worst


def test_tiling_concave_disjoint():
    print("test_tiling_concave_disjoint (item 2: real polygon-rect boolean):")
    # U-shape, CCW: 10x10 with a notch x in [3,7] cut down to y=3.
    #   ###   ###      legs:  x in [0,3] and x in [7,10], up to y=10
    #   ###   ###      base:  y in [0,3], full width
    #   ##########
    u = [(0, 0), (10, 0), (10, 10), (7, 10), (7, 3), (3, 3), (3, 10), (0, 10)]

    # A band above the notch floor meets the U in TWO disjoint rectangles.
    loops = tiling.clip_polygon_rect(u, 0, 5, 10, 10)
    check(len(loops) == 2,
          "U-shape clipped above the notch -> 2 separate loops (got %d)"
          % len(loops))
    check(all(approx(_area(l), 15.0) for l in loops),
          "each disjoint loop is a 3x5 region, area 15 (got %s)"
          % [round(_area(l), 4) for l in loops])
    for k, l in enumerate(loops):
        check(_seam_overlap(l) <= 1e-9,
              "loop %d carries no zero-width seam (longest overlap %.4f)"
              % (k, _seam_overlap(l)))
    spans = sorted((min(p[0] for p in l), max(p[0] for p in l)) for l in loops)
    check(spans == [(0, 3), (7, 10)],
          "the two loops are the two legs, x in [0,3] and [7,10] (got %s)"
          % (spans,))
    check(all(approx(min(p[1] for p in l), 5) and approx(max(p[1] for p in l), 10)
              for l in loops), "both loops span the band's full height")

    # Same shape, vertical band: crosses both legs and the base -> connected.
    joined = tiling.clip_polygon_rect(u, 0, 0, 10, 10)
    check(len(joined) == 1, "band covering the base stays 1 connected loop")
    check(approx(_area(joined[0]), _area(u)), "connected clip keeps full area")

    # An L-shape sliced by a VERTICAL band (the other axis) -> 2 regions.
    #   L: tall left arm + a foot along the bottom, notch at top-right.
    ell = [(0, 0), (10, 0), (10, 3), (3, 3), (3, 10), (0, 10)]
    lloops = tiling.clip_polygon_rect(ell, 5, 0, 8, 10)
    check(len(lloops) == 1, "L-shape band right of the arm -> 1 loop (the foot)")
    # C-shape (notch opening left) sliced by a VERTICAL band -> 2 regions.
    cee = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 7), (7, 7), (7, 3), (0, 3)]
    cee_loops = tiling.clip_polygon_rect(cee, 0, 0, 5, 10)
    check(len(cee_loops) == 2,
          "C-shape sliced by a vertical band -> 2 loops (got %d)"
          % len(cee_loops))
    check(all(approx(_area(l), 15.0) for l in cee_loops),
          "each vertical-band loop is 5x3, area 15 (got %s)"
          % [round(_area(l), 4) for l in cee_loops])
    check(all(_seam_overlap(l) <= 1e-9 for l in cee_loops),
          "neither vertical-band loop carries a zero-width seam")

    # Three teeth -> three disjoint regions from one clip.
    comb = [(0, 0), (12, 0), (12, 8), (10, 8), (10, 2), (8, 2), (8, 8),
            (6, 8), (6, 2), (4, 2), (4, 8), (2, 8), (2, 2), (0, 2)]
    cloops = tiling.clip_polygon_rect(comb, 0, 4, 12, 8)
    check(len(cloops) == 3, "3-tooth comb clipped above the gullets -> 3 loops "
          "(got %d)" % len(cloops))
    check(approx(sum(_area(l) for l in cloops), 3 * 2 * 4),
          "the 3 loops total 24 (3 teeth x 2 x 4)")
    check(all(_seam_overlap(l) <= 1e-9 for l in cloops),
          "no comb loop carries a zero-width seam")

    # Rect entirely inside the polygon (the ring never touches it).
    inner = tiling.clip_polygon_rect([(0, 0), (20, 0), (20, 20), (0, 20)],
                                     5, 5, 8, 9)
    check(len(inner) == 1 and approx(_area(inner[0]), 12),
          "rect wholly inside the polygon clips to the whole rect")
    # Polygon entirely inside the rect comes back unchanged.
    whole = tiling.clip_polygon_rect(u, -1, -1, 11, 11)
    check(len(whole) == 1 and whole[0] == u,
          "polygon wholly inside the rect is returned verbatim")
    check(tiling.clip_polygon_rect(u, 4, 4, 6, 6) == [],
          "rect inside the notch (outside the piece) -> no loops")

    # Input winding is preserved, both ways.
    cw = list(reversed(u))
    cwloops = tiling.clip_polygon_rect(cw, 0, 5, 10, 10)
    check(len(cwloops) == 2, "clockwise input also yields 2 loops")

    def signed(loop):
        s = 0.0
        for a, b in zip(loop, loop[1:] + loop[:1]):
            s += a[0] * b[1] - b[0] * a[1]
        return s
    check(all(signed(l) > 0 for l in loops) and all(signed(l) < 0 for l in cwloops),
          "output winding follows the input winding")

    # --- through tile_piece: disjoint regions become separate tiles ---
    tiles = tiling.tile_piece(u, [], tile_w=10, tile_h=5)
    check(len(tiles) == 3,
          "U-shape on a 10x5 grid -> 3 tiles (row 0 whole, row 1 split in 2), "
          "got %d" % len(tiles))
    upper = [t for t in tiles if t["row"] == 1]
    check(len(upper) == 2, "the upper grid cell yields 2 tiles (got %d)"
          % len(upper))
    check(all(t["col"] == upper[0]["col"] for t in upper),
          "both upper tiles report the same grid cell")
    check(all(approx(_area(t["outer"]), 15.0) for t in upper),
          "each upper tile is one 3x5 leg")
    check(all(_seam_overlap(t["outer"]) <= 1e-9 for t in tiles),
          "no tile outline doubles back on itself")
    check(approx(sum(_area(t["outer"]) for t in tiles), _area(u)),
          "the tiles' areas sum to the piece's area (nothing lost or doubled)")

    # Holes land on the component that actually contains them.
    hl = [(1, 6), (2, 6), (2, 7), (1, 7)]      # in the left leg
    hr = [(8, 6), (9, 6), (9, 7), (8, 7)]      # in the right leg
    htiles = [t for t in tiling.tile_piece(u, [hl, hr], tile_w=10, tile_h=5)
              if t["row"] == 1]
    check(len(htiles) == 2 and all(len(t["holes"]) == 1 for t in htiles),
          "each split tile carries exactly one of the two holes (got %s)"
          % [len(t["holes"]) for t in htiles])
    okhole = True
    for t in htiles:
        ox = [p[0] for p in t["outer"]]
        hx = [p[0] for p in t["holes"][0]]
        if not (min(ox) <= min(hx) and max(hx) <= max(ox)):
            okhole = False
    check(okhole, "each hole sits inside the tile it was assigned to")


def test_tiling_overlap():
    print("test_tiling_overlap (item 1: poster overlap + crop marks):")
    big = [(0, 0), (30, 0), (30, 30), (0, 30)]

    # overlap == 0 must reproduce today's butt-joint output exactly: no crop
    # marks, seam cut_edges still present, still 6 tiles.
    base = tiling.tile_piece(big, [], 12, 24)
    zero = tiling.tile_piece(big, [], 12, 24, overlap=0.0)
    check(all(t["crop_marks"] == [] for t in zero),
          "overlap=0 emits no crop marks")
    check(any(t["cut_edges"] for t in zero),
          "overlap=0 still reports butt-joint cut_edges")
    check([t["outer"] for t in base] == [t["outer"] for t in zero],
          "overlap=0 tile geometry is byte-identical to the default call")

    # 20x10 piece, 10x10 tiles, 2cm overlap: grid on 8x8 cells -> 3x2 tiles.
    piece = [(0, 0), (20, 0), (20, 10), (0, 10)]
    ov = 2.0
    tiles = tiling.tile_piece(piece, [], tile_w=10, tile_h=10, overlap=ov)

    # No grown tile exceeds the bed, and each carries crop marks not cut_edges.
    okfit = True
    for t in tiles:
        tx = [p[0] for p in t["outer"]]
        ty = [p[1] for p in t["outer"]]
        if (max(tx) - min(tx)) > 10 + 1e-6 or (max(ty) - min(ty)) > 10 + 1e-6:
            okfit = False
    check(okfit, "every overlapped tile still fits within 10x10")
    check(all(t["cut_edges"] == [] for t in tiles),
          "overlapped tiles report no butt-joint cut_edges")
    check(all(all(hs == [] for hs in t["hole_seam_edges"]) for t in tiles),
          "overlapped tiles report no hole seam edges")
    check(all(t["crop_marks"] for t in tiles),
          "every overlapped tile carries crop marks")

    # The overlap really doubles material along the seams: covered area exceeds
    # the piece area (butt-joint would equal it exactly).
    check(sum(_area(t["outer"]) for t in tiles) > _area(piece) + 1e-6,
          "overlapped tiles cover MORE than the piece area (got %.2f vs %.2f)"
          % (sum(_area(t["outer"]) for t in tiles), _area(piece)))

    # Two horizontally-adjacent tiles share a margin exactly `overlap` wide.
    row0 = sorted([t for t in tiles if t["row"] == 0], key=lambda t: t["col"])
    left, right = row0[0], row0[1]
    lxmax = max(p[0] for p in left["outer"])
    rxmin = min(p[0] for p in right["outer"])
    check(approx(lxmax - rxmin, ov),
          "adjacent tiles overlap by exactly %.1fcm in x (got %.4f)"
          % (ov, lxmax - rxmin))

    # Crop marks lie on the nominal grid line x=8, and the two tiles sharing
    # that seam carry the SAME marks -> overlaying and lining them up registers
    # the sheets.
    def marks_on_x(t, xval):
        return {tuple(round(c, 6) for c in a) + tuple(round(c, 6) for c in b)
                for a, b in t["crop_marks"]
                if approx(a[0], xval) and approx(b[0], xval)}
    lm = marks_on_x(left, 8.0)
    rm = marks_on_x(right, 8.0)
    check(len(lm) == 2 and lm == rm,
          "both tiles carry identical crop marks on the shared grid line x=8 "
          "(left=%s right=%s)" % (sorted(lm), sorted(rm)))
    # A crop mark is a short segment; here length == crop_len default 0.6.
    for a, b in left["crop_marks"]:
        check(approx(math.hypot(b[0] - a[0], b[1] - a[1]), 0.6),
              "crop mark length is crop_len (0.6)")
        break

    # An overlap as big as the tile is a degenerate request and is rejected.
    try:
        tiling.tile_piece(piece, [], 10, 10, overlap=10.0)
        check(False, "overlap >= tile should raise")
    except ValueError:
        check(True, "overlap >= tile size raises ValueError")


def test_one_file_core():
    print("test_one_file_core (translate, palette, render_groups):")
    t = g.translate_element(g.Line((0, 0), (1, 1)), 5, 3)
    check(t.p0 == (5, 3) and t.p1 == (6, 4), "translate_element shifts a line")
    check(g.translate_element(g.Circle((1, 1), 2), 3, 0).center == (4, 1),
          "translate_element shifts a circle center")

    cols = palette.distinct_colors(5)
    check(len(cols) == 5 and len(set(cols)) == 5, "5 distinct palette colors")
    check(all(c.startswith("#") and len(c) == 7 for c in cols), "hex #RRGGBB")
    check(palette.distinct_colors(0) == [], "0 colors -> empty")

    pieces = [
        {"outer": g.Polyline([(0, 0), (1, 0), (1, 1), (0, 1)], closed=True),
         "holes": [], "fiducials": [], "color": "#AA0088"},
        {"outer": g.Polyline([(2, 0), (3, 0), (3, 1), (2, 1)], closed=True),
         "holes": [], "fiducials": [], "color": "#0088AA"},
    ]
    out = svg.render_pieces(pieces, unit="mm", filled=True)
    check('fill="#AA0088"' in out and 'fill="#0088AA"' in out,
          "both piece fill colors present")
    check('stroke="none"' in out, "filled shapes carry no stroke")
    # Two side-by-side unit squares span 3cm -> 30mm wide, 1cm -> 10mm tall.
    check(attr(out, "viewBox") == "0 0 30 10",
          "render_pieces uses one shared bbox (30x10 mm)")

    # A piece with a hole -> one compound even-odd path (2 subpaths).
    holed = svg.render_pieces([{
        "outer": g.Polyline([(0, 0), (4, 0), (4, 4), (0, 4)], closed=True),
        "holes": [g.Circle((2, 2), 1)], "fiducials": [], "color": "#123456"}],
        unit="mm", filled=True)
    check('fill-rule="evenodd"' in holed and attr(holed, "d").count("M") == 2,
          "hole -> even-odd compound path (outer + hole subpaths)")


def _rects(boxes, bins):
    """The placed rectangle of every piece, per bin, in bin coordinates."""
    out = []
    for b in bins:
        rs = []
        for pl in b.placements:
            minx, miny, maxx, maxy = boxes[pl.index]
            rs.append((minx + pl.dx, miny + pl.dy, maxx + pl.dx, maxy + pl.dy))
        out.append(rs)
    return out


def _pack_violations(boxes, bins, bin_w, bin_h, gap, tol=1e-6):
    """Every way a pack result can be wrong, as a list of readable strings.

    This is the invariant :func:`core.packing.pack` promises, spelled out:
    nothing escapes its bin, nothing sits closer to a neighbour than ``gap``
    (which subsumes "nothing overlaps"), every input piece is placed exactly
    once, and each bin's reported extents match what is actually in it.
    """
    bad = []
    idxs = sorted(pl.index for b in bins for pl in b.placements)
    if idxs != list(range(len(boxes))):
        bad.append("placements are not exactly the input pieces (got %s)" % idxs)

    for bi, rs in enumerate(_rects(boxes, bins)):
        for x0, y0, x1, y1 in rs:
            if x0 < -tol or y0 < -tol or x1 > bin_w + tol or y1 > bin_h + tol:
                bad.append("bin %d: piece (%.3f,%.3f)-(%.3f,%.3f) escapes the "
                           "%.2f x %.2f bin" % (bi, x0, y0, x1, y1, bin_w, bin_h))
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                a, b = rs[i], rs[j]
                # Separation along each axis; negative means they interpenetrate.
                sx = max(a[0], b[0]) - min(a[2], b[2])
                sy = max(a[1], b[1]) - min(a[3], b[3])
                if max(sx, sy) < gap - tol:
                    bad.append("bin %d: pieces %d and %d are only %.4f apart "
                               "(gap %.2f)" % (bi, i, j, max(sx, sy), gap))
        if rs:
            w = max(r[2] for r in rs)
            h = max(r[3] for r in rs)
            rep = bins[bi]
            if abs(w - rep.width) > tol or abs(h - rep.height) > tol:
                bad.append("bin %d reports %.4f x %.4f but holds %.4f x %.4f"
                           % (bi, rep.width, rep.height, w, h))
    return bad


def _boxes(sizes, spread=0.0):
    """(minx,miny,maxx,maxy) boxes of the given sizes, optionally scattered so
    the packer cannot get away with assuming pieces start at the origin."""
    out = []
    for k, (w, h) in enumerate(sizes):
        ox = -3.7 * k * spread
        oy = 11.3 * k * spread
        out.append((ox, oy, ox + w, oy + h))
    return out


def test_packing():
    print("test_packing (bed-bounded shelf packing, one bin per output sheet):")
    check(packing.pack([], 12, 24, 0.5) == [], "no pieces -> no bins")

    # A piece bigger than the bin is the one legitimate error: fitting.py is
    # supposed to have rotated it to fit (or tiling.py to have split it).
    try:
        packing.pack(_boxes([(30.0, 30.0)]), 12, 24, 0.5)
        check(False, "oversized piece should raise")
    except packing.PieceTooLargeError:
        check(True, "a piece too big for the bin raises PieceTooLargeError")
    check(issubclass(packing.PieceTooLargeError, ValueError),
          "PieceTooLargeError is a ValueError (callers can catch either)")

    # Single piece, offset geometry: lands flush in the bin corner.
    one = packing.pack([(4.0, -7.0, 9.0, -3.0)], 12, 24, 0.5)
    check(len(one) == 1 and len(one[0].placements) == 1, "1 piece -> 1 bin")
    pl = one[0].placements[0]
    check(approx(4.0 + pl.dx, 0.0) and approx(-7.0 + pl.dy, 0.0),
          "offset piece is translated to the bin origin")
    check(approx(one[0].width, 5.0) and approx(one[0].height, 4.0),
          "bin extents are the piece's own size (got %.2f x %.2f)"
          % (one[0].width, one[0].height))

    # Four 5x4 pieces, 12x24 bed, Y shelves: one column, 4*4 + 3*0.5 tall.
    # (Matches the arrangement the one-file export produced before packing --
    # the common small job must not move.)
    quad = _boxes([(5.0, 4.0)] * 4)
    ybins = packing.pack(quad, 12, 24, 0.5, "Y")
    check(len(ybins) == 1, "4 small pieces fit one bed (got %d bins)" % len(ybins))
    check(approx(ybins[0].height, 4 * 4 + 3 * 0.5) and approx(ybins[0].width, 5.0),
          "Y shelves stack a single column, 17.5 x 5 (got %.2f x %.2f)"
          % (ybins[0].height, ybins[0].width))
    check(not _pack_violations(quad, ybins, 12, 24, 0.5), "the column is legal")

    # Same pieces, X shelves: a row across the 12cm width wraps after two.
    xbins = packing.pack(quad, 12, 24, 0.5, "X")
    check(len(xbins) == 1, "X shelves also need only one bed")
    check(approx(xbins[0].width, 5 + 0.5 + 5) and approx(xbins[0].height, 4 + 0.5 + 4),
          "X shelves wrap at the 12cm width into 2 rows (got %.2f x %.2f)"
          % (xbins[0].width, xbins[0].height))
    check(not _pack_violations(quad, xbins, 12, 24, 0.5), "the rows are legal")

    # THE POINT OF THE FEATURE: more pieces than the bed holds opens another
    # bed instead of arranging off the end of the material.
    many = _boxes([(11.0, 7.0)] * 10)
    mb = packing.pack(many, 12, 24, 0.5, "Y")
    check(len(mb) > 1, "10 pieces of 11x7 cannot share one 12x24 bed (got %d bins)"
          % len(mb))
    check(not _pack_violations(many, mb, 12, 24, 0.5),
          "every bin of the multi-bin pack stays inside the bed")
    check(sum(len(b.placements) for b in mb) == 10, "no piece is dropped")

    # Roll stock (spec item 5): a huge bed height is not a special case -- it
    # just yields a bin nothing can overflow, still wrapped at the bed width.
    roll_sizes = [(3.0, 2.0)] * 40
    roll = _boxes(roll_sizes)
    rb = packing.pack(roll, 12, 999, 0.5, "X")
    check(len(rb) == 1, "roll stock (12 x 999) packs into one bin (got %d)"
          % len(rb))
    check(rb[0].width <= 12 + 1e-9,
          "roll pack still wraps at the 12cm width (used %.2f)" % rb[0].width)
    rows = set(round(r[1], 6) for r in _rects(roll, rb)[0])
    check(len(rows) > 1, "roll pack wrapped onto %d rows, not one long line"
          % len(rows))
    check(not _pack_violations(roll, rb, 12, 999, 0.5), "the roll pack is legal")
    check(len(packing.pack(roll, 12, 999, 0.5, "Y")) == 1,
          "Y shelves on roll stock also stay in one bin")

    # Deterministic: the same job twice gives the same sheets.
    check(packing.pack(many, 12, 24, 0.5, "Y") == mb, "packing is deterministic")

    # --- property test: the invariant, over many randomized piece sets ------
    rng = random.Random(20260808)
    bad = []
    runs = 400
    multi_bin = 0
    multi_shelf = 0
    for _ in range(runs):
        bin_w = rng.uniform(4.0, 40.0)
        bin_h = rng.uniform(4.0, 40.0)
        gap = rng.choice([0.0, 0.1, 0.5, 1.0])
        axis = rng.choice(["X", "Y"])
        n = rng.randint(1, 30)
        sizes = [(rng.uniform(0.05, 1.0) * bin_w, rng.uniform(0.05, 1.0) * bin_h)
                 for _ in range(n)]
        boxes = [(rng.uniform(-50, 50), rng.uniform(-50, 50), 0.0, 0.0)
                 for _ in range(n)]
        boxes = [(b[0], b[1], b[0] + s[0], b[1] + s[1])
                 for b, s in zip(boxes, sizes)]
        bins = packing.pack(boxes, bin_w, bin_h, gap, axis)
        if len(bins) > 1:
            multi_bin += 1
        if any(len(set(round(r[1], 6) for r in rs)) > 1
               or len(set(round(r[0], 6) for r in rs)) > 1
               for rs in _rects(boxes, bins)):
            multi_shelf += 1
        v = _pack_violations(boxes, bins, bin_w, bin_h, gap)
        if v:
            bad.append("axis=%s bin=%.2fx%.2f gap=%.2f n=%d: %s"
                       % (axis, bin_w, bin_h, gap, n, v[0]))
    check(not bad, "%d randomized packs all satisfy the bin invariant (%d bad, "
          "first: %s)" % (runs, len(bad), bad[0] if bad else "none"))
    # A property test that never exercised the interesting states would pass
    # trivially, so assert the sample actually reached them.
    check(multi_bin > runs // 20,
          "the random sample really does overflow onto extra bins (%d of %d runs)"
          % (multi_bin, runs))
    check(multi_shelf > runs // 20,
          "the random sample really does open extra shelves (%d of %d runs)"
          % (multi_shelf, runs))


def _dxf_pairs(text):
    toks = text.split("\n")
    pairs = []
    i = 0
    while i + 1 < len(toks):
        pairs.append((toks[i].strip(), toks[i + 1]))
        i += 2
    return pairs


def test_dxf():
    print("test_dxf (R12 structure + 1:1 scale, no ezdxf needed):")
    square = g.Polyline([(0, 0), (10, 0), (10, 10), (0, 10)], closed=True)
    out = dxf.render([square], unit="mm")
    check(out.startswith("0\nSECTION"), "starts with a SECTION")
    check(out.rstrip().endswith("EOF"), "ends with EOF")
    check("AC1009" in out, "declares R12 (AC1009)")
    check("POLYLINE" in out and "VERTEX" in out and "SEQEND" in out,
          "closed polyline via POLYLINE/VERTEX/SEQEND")

    pairs = _dxf_pairs(out)
    check(all(c.lstrip("-").isdigit() for c, _ in pairs),
          "every group code is an integer (code/value pairing intact)")

    # $INSUNITS == 4 (mm).
    iu = next((pairs[i + 1][1] for i, (c, v) in enumerate(pairs)
               if v == "$INSUNITS"), None)
    check(iu == "4", "mm flagged as $INSUNITS=4 (got %s)" % iu)

    # Gather VERTEX coordinates and confirm the 100mm span.
    verts = []
    for idx, (c, v) in enumerate(pairs):
        if c == "0" and v == "VERTEX":
            xs = [float(v2) for c2, v2 in pairs[idx + 1:idx + 6] if c2 == "10"]
            ys = [float(v2) for c2, v2 in pairs[idx + 1:idx + 6] if c2 == "20"]
            if xs and ys:
                verts.append((xs[0], ys[0]))
    check(len(verts) == 4, "4 vertices emitted (got %d)" % len(verts))
    vx = [p[0] for p in verts]
    vy = [p[1] for p in verts]
    check(approx(max(vx) - min(vx), 100) and approx(max(vy) - min(vy), 100),
          "square is 100x100 mm in DXF (1:1)")

    # DXF is Y-up (no flip): sketch bottom-left (0,0) maps to (0,0), not (0,100).
    check((0.0, 0.0) in [(round(x, 6), round(y, 6)) for x, y in verts],
          "no Y-flip: (0,0) stays at (0,0)")


def main():
    for t in (test_square_mm, test_inch_scaling, test_bbox_translate_margin,
              test_circle, test_arc_sweep_flag, test_ellipse_extents,
              test_units_and_empty, test_chain_loop, test_edge_ticks,
              test_fit_rotation, test_svg_fiducials_and_labels,
              test_rotate_and_centroid, test_dxf, test_tiling,
              test_tiling_concave_disjoint, test_tiling_overlap,
              test_packing, test_one_file_core):
        t()
    print()
    if _failures:
        print("%d FAILURE(S)" % len(_failures))
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
