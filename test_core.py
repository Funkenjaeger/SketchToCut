"""Standalone unit tests for the dependency-free SVG core.

Run from the add-in folder (so `core` is importable) with a normal Python --
no Fusion required:

    python test_core.py

Exercises 1:1 scaling, the Y-flip, unit conversion, bbox/translate, and the
arc sweep-flag convention.
"""

import math
import re
import sys

from core import geometry as g
from core import svg
from core import dxf
from core import loops
from core import fiducials
from core import fitting


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
              test_rotate_and_centroid, test_dxf):
        t()
    print()
    if _failures:
        print("%d FAILURE(S)" % len(_failures))
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
