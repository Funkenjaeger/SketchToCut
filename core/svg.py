"""Render a list of geometry-IR elements to a 1:1-scale SVG string.

Coordinate handling (the whole point of the tool):
  * Input coordinates are in centimetres. We multiply by ``cm_to(unit)`` so the
    SVG carries true physical size -- ``width``/``height`` get the real unit
    suffix (e.g. ``in``/``mm``) and ``viewBox`` is numerically equal, i.e.
    **1 SVG user-unit == 1 output unit**. (Some tools instead assume 96
    user-units per inch; if a cutter mis-scales, that convention is the first
    thing to revisit -- confirm with a physical calibration cut.)
  * SVG's Y axis grows downward while a sketch's grows upward, so every Y is
    flipped: ``y_svg = (maxy - y) * s``. This keeps the cut mask looking the
    same as it does in Fusion.
"""

from . import geometry as g


def _num(v: float, decimals: int) -> str:
    """Fixed-point number with trailing zeros trimmed; never ``-0``."""
    v = round(v, decimals)
    if v == 0.0:
        v = 0.0  # collapse -0.0 -> 0.0
    s = ("%.*f" % (decimals, v)).rstrip("0").rstrip(".")
    return s if s and s != "-0" else "0"


def render(elements, unit="in", stroke_width=0.01, stroke="black",
           margin=0.0, decimals=4):
    """Return an SVG document string for ``elements`` (cm-space geometry).

    ``stroke_width`` and ``margin`` are expressed in the output ``unit`` (which
    equals SVG user units here). Many cutters cut the path centreline and
    ignore stroke width; it is mainly for on-screen visibility.
    """
    bbox = g.bounding_box(elements)
    if bbox is None:
        raise ValueError("No geometry to export.")
    minx, miny, maxx, maxy = bbox
    s = g.cm_to(unit)
    suffix = g.unit_suffix(unit)

    width = (maxx - minx) * s + 2.0 * margin
    height = (maxy - miny) * s + 2.0 * margin

    def X(x):
        return (x - minx) * s + margin

    def Y(y):
        return (maxy - y) * s + margin  # flip

    def n(v):
        return _num(v, decimals)

    body = []
    for e in elements:
        if isinstance(e, g.Line):
            body.append('<path d="M %s %s L %s %s" />' % (
                n(X(e.p0[0])), n(Y(e.p0[1])), n(X(e.p1[0])), n(Y(e.p1[1]))))

        elif isinstance(e, g.Polyline):
            pts = e.points
            if len(pts) < 2:
                continue
            d = "M %s %s" % (n(X(pts[0][0])), n(Y(pts[0][1])))
            for p in pts[1:]:
                d += " L %s %s" % (n(X(p[0])), n(Y(p[1])))
            if e.closed:
                d += " Z"
            body.append('<path d="%s" />' % d)

        elif isinstance(e, g.Circle):
            body.append('<circle cx="%s" cy="%s" r="%s" />' % (
                n(X(e.center[0])), n(Y(e.center[1])), n(e.radius * s)))

        elif isinstance(e, g.Arc):
            sp, ep = e.start_point(), e.end_point()
            r = n(e.radius * s)
            large = 1 if e.sweep() > __import__("math").pi + 1e-12 else 0
            # A CCW arc in sketch space renders CCW on the (Y-flipped) page,
            # which is SVG sweep-flag 0; CW -> flag 1. See test_core.py.
            sweep_flag = 0 if e.ccw else 1
            body.append('<path d="M %s %s A %s %s 0 %d %d %s %s" />' % (
                n(X(sp[0])), n(Y(sp[1])), r, r, large, sweep_flag,
                n(X(ep[0])), n(Y(ep[1]))))

        elif isinstance(e, g.Ellipse):
            import math
            cx, cy = n(X(e.center[0])), n(Y(e.center[1]))
            rx, ry = n(e.r_major * s), n(e.r_minor * s)
            # Y-flip mirrors the major-axis angle.
            deg = n(-math.degrees(e.rotation))
            body.append(
                '<ellipse cx="%s" cy="%s" rx="%s" ry="%s" '
                'transform="rotate(%s %s %s)" />' % (
                    cx, cy, rx, ry, deg, cx, cy))
        else:
            raise TypeError("Unsupported element type: %r" % (type(e),))

    header = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        'width="%s%s" height="%s%s" viewBox="0 0 %s %s">\n' % (
            n(width), suffix, n(height), suffix, n(width), n(height)))
    group_open = (
        '<g fill="none" stroke="%s" stroke-width="%s" '
        'stroke-linecap="round" stroke-linejoin="round">\n' % (
            stroke, n(stroke_width)))
    group_close = "</g>\n"
    footer = "</svg>\n"

    return header + group_open + "\n".join(body) + "\n" + group_close + footer
