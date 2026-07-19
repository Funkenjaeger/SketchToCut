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


def main():
    for t in (test_square_mm, test_inch_scaling, test_bbox_translate_margin,
              test_circle, test_arc_sweep_flag, test_ellipse_extents,
              test_units_and_empty):
        t()
    print()
    if _failures:
        print("%d FAILURE(S)" % len(_failures))
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
