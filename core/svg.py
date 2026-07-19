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

Per-region export adds two optional inputs:
  * ``fiducials`` -- IR elements drawn in a separate red ``class="fiducial"``
    group so they can be retargeted to a pen/score tool or deleted.
  * ``labels`` -- ``(text, (x_cm, y_cm), height_cm)`` triples drawn as ``<text>``
    in that same non-cut group (piece-index letters).
"""

import math

from . import geometry as g


def _num(v, decimals):
    """Fixed-point number with trailing zeros trimmed; never ``-0``."""
    v = round(v, decimals)
    if v == 0.0:
        v = 0.0  # collapse -0.0 -> 0.0
    s = ("%.*f" % (decimals, v)).rstrip("0").rstrip(".")
    return s if s and s != "-0" else "0"


def _element_svg(e, X, Y, n, s):
    """SVG fragment for one IR element, given the coordinate transforms."""
    if isinstance(e, g.Line):
        return '<path d="M %s %s L %s %s" />' % (
            n(X(e.p0[0])), n(Y(e.p0[1])), n(X(e.p1[0])), n(Y(e.p1[1])))

    if isinstance(e, g.Polyline):
        pts = e.points
        if len(pts) < 2:
            return ""
        d = "M %s %s" % (n(X(pts[0][0])), n(Y(pts[0][1])))
        for p in pts[1:]:
            d += " L %s %s" % (n(X(p[0])), n(Y(p[1])))
        if e.closed:
            d += " Z"
        return '<path d="%s" />' % d

    if isinstance(e, g.Circle):
        return '<circle cx="%s" cy="%s" r="%s" />' % (
            n(X(e.center[0])), n(Y(e.center[1])), n(e.radius * s))

    if isinstance(e, g.Arc):
        sp, ep = e.start_point(), e.end_point()
        r = n(e.radius * s)
        large = 1 if e.sweep() > math.pi + 1e-12 else 0
        # A CCW arc in sketch space renders CCW on the (Y-flipped) page,
        # which is SVG sweep-flag 0; CW -> flag 1. See test_core.py.
        sweep_flag = 0 if e.ccw else 1
        return '<path d="M %s %s A %s %s 0 %d %d %s %s" />' % (
            n(X(sp[0])), n(Y(sp[1])), r, r, large, sweep_flag,
            n(X(ep[0])), n(Y(ep[1])))

    if isinstance(e, g.Ellipse):
        cx, cy = n(X(e.center[0])), n(Y(e.center[1]))
        rx, ry = n(e.r_major * s), n(e.r_minor * s)
        deg = n(-math.degrees(e.rotation))  # Y-flip mirrors the major-axis angle
        return ('<ellipse cx="%s" cy="%s" rx="%s" ry="%s" '
                'transform="rotate(%s %s %s)" />' % (cx, cy, rx, ry, deg, cx, cy))

    raise TypeError("Unsupported element type: %r" % (type(e),))


def _escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _loop_d(e, X, Y, n):
    """Compound-path ``d`` for one closed loop (flattened to a polygon)."""
    pts = g.sample_element_points(e)
    if len(pts) < 3:
        return ""
    d = "M %s %s" % (n(X(pts[0][0])), n(Y(pts[0][1])))
    for p in pts[1:]:
        d += " L %s %s" % (n(X(p[0])), n(Y(p[1])))
    return d + " Z"


def render_pieces(pieces, unit="in", stroke_width=0.01, filled=True,
                  labels=None, label_stroke="red", margin=0.0, decimals=4):
    """Render colored pieces into one SVG (shared coordinate frame).

    Each piece is ``{"outer": elem, "holes": [elem...], "fiducials": [Line...],
    "color": "#RRGGBB"}``. With ``filled`` the shape is filled with its color
    (holes cut via even-odd) and fiducials are stroked in the same color (a line
    has no fill area); otherwise everything is stroked. ``labels`` (``(text,
    (x_cm, y_cm), height_cm)``) render in a separate ``class="label"`` group in
    ``label_stroke``.
    """
    labels = labels or []
    allelems = []
    for pc in pieces:
        allelems.append(pc["outer"])
        allelems.extend(pc["holes"])
        allelems.extend(pc.get("fiducials", []))
    bbox = g.bounding_box(allelems)
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

    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        'width="%s%s" height="%s%s" viewBox="0 0 %s %s">\n' % (
            n(width), suffix, n(height), suffix, n(width), n(height))]

    for pc in pieces:
        color = pc["color"]
        fids = pc.get("fiducials", [])
        if filled:
            d = " ".join(x for x in (_loop_d(e, X, Y, n)
                                     for e in [pc["outer"]] + pc["holes"]) if x)
            if d:
                parts.append('<path d="%s" fill="%s" stroke="none" '
                             'fill-rule="evenodd" />\n' % (d, color))
            if fids:
                parts.append('<g fill="none" stroke="%s" stroke-width="%s" '
                             'stroke-linecap="round" stroke-linejoin="round">\n'
                             % (color, n(stroke_width)))
                for f in fids:
                    frag = _element_svg(f, X, Y, n, s)
                    if frag:
                        parts.append(frag + "\n")
                parts.append("</g>\n")
        else:
            parts.append('<g fill="none" stroke="%s" stroke-width="%s" '
                         'stroke-linecap="round" stroke-linejoin="round">\n'
                         % (color, n(stroke_width)))
            for e in [pc["outer"]] + pc["holes"] + list(fids):
                frag = _element_svg(e, X, Y, n, s)
                if frag:
                    parts.append(frag + "\n")
            parts.append("</g>\n")

    if labels:
        parts.append('<g class="label" fill="%s" stroke="none">\n' % label_stroke)
        for text, (lx, ly), height_cm in labels:
            parts.append(
                '<text x="%s" y="%s" font-size="%s" text-anchor="middle" '
                'dominant-baseline="central" fill="%s" stroke="none">%s</text>\n' % (
                    n(X(lx)), n(Y(ly)), n(height_cm * s), label_stroke,
                    _escape(text)))
        parts.append("</g>\n")

    parts.append("</svg>\n")
    return "".join(parts)


def render(elements, unit="in", stroke_width=0.01, stroke="black",
           margin=0.0, decimals=4, fiducials=None, fiducial_stroke="red",
           labels=None):
    """Single-group stroked SVG (low-level helper; used by the test suite)."""
    fiducials = fiducials or []
    labels = labels or []
    bbox = g.bounding_box(list(elements) + list(fiducials))
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
        return (maxy - y) * s + margin

    def n(v):
        return _num(v, decimals)

    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        'width="%s%s" height="%s%s" viewBox="0 0 %s %s">\n' % (
            n(width), suffix, n(height), suffix, n(width), n(height)),
        '<g fill="none" stroke="%s" stroke-width="%s" stroke-linecap="round" '
        'stroke-linejoin="round">\n' % (stroke, n(stroke_width))]
    for e in elements:
        frag = _element_svg(e, X, Y, n, s)
        if frag:
            parts.append(frag + "\n")
    parts.append("</g>\n")

    if fiducials or labels:
        parts.append('<g class="fiducial" fill="none" stroke="%s" '
                     'stroke-width="%s" stroke-linecap="round" '
                     'stroke-linejoin="round">\n' % (fiducial_stroke, n(stroke_width)))
        for e in fiducials:
            frag = _element_svg(e, X, Y, n, s)
            if frag:
                parts.append(frag + "\n")
        for text, (lx, ly), height_cm in labels:
            parts.append(
                '<text x="%s" y="%s" font-size="%s" text-anchor="middle" '
                'dominant-baseline="central" fill="%s" stroke="none">%s</text>\n' % (
                    n(X(lx)), n(Y(ly)), n(height_cm * s), fiducial_stroke,
                    _escape(text)))
        parts.append("</g>\n")

    parts.append("</svg>\n")
    return "".join(parts)
