"""Render geometry-IR elements to a DXF string (dependency-free).

A sibling to :mod:`core.svg` for the laser-cut workflow (e.g. uploading to
SendCutSend). Same IR, same 1:1 physical scaling, but:

  * **No Y-flip.** DXF uses a Y-up coordinate system like the sketch, so
    coordinates map directly (SVG had to flip because its Y grows downward).
  * Output is R12 (AC1009) ASCII DXF with POLYLINE / LINE / CIRCLE / ARC / TEXT
    -- the lowest-common-denominator format that virtually every CAM tool reads
    (no entity handles or subclass markers to get wrong). Curves already arrive
    flattened to polylines from extraction.
  * ``$INSUNITS`` / ``$MEASUREMENT`` are set so the consumer knows the units
    (SendCutSend also confirms units on upload).
  * Cut geometry goes on layer ``0``; fiducials on ``FIDUCIAL``; labels on
    ``LABEL`` -- so a laser shop can ignore/delete the non-cut layers.

Validated against ezdxf in the test suite (ezdxf is a dev/test dependency
only; it is never imported at runtime).
"""

import math

from . import geometry as g

_INSUNITS = {"in": 1, "mm": 4, "cm": 5}


def _pair(code, value):
    return "%d\n%s" % (code, value)


def _num(v, decimals):
    return ("%.*f" % (decimals, v))


def render_groups(groups, unit="in", fiducials=None, labels=None, margin=0.0,
                  decimals=6):
    """Render multiple layer-groups into one DXF (shared coordinate frame).

    ``groups`` is a list of ``{"elements": [...], "layer": str, "color": aci}``
    -- each piece goes on its own layer/color so a laser SW can separate them.
    ``fiducials`` and ``labels`` go on the FIDUCIAL / LABEL layers.
    """
    fiducials = fiducials or []
    labels = labels or []

    all_elems = []
    for grp in groups:
        all_elems.extend(grp.get("elements", []))
    bbox = g.bounding_box(all_elems + list(fiducials))
    if bbox is None:
        raise ValueError("No geometry to export.")
    minx, miny, maxx, maxy = bbox
    s = g.cm_to(unit)
    suffix = g.unit_suffix(unit)
    insunits = _INSUNITS[suffix]
    measurement = 0 if suffix == "in" else 1

    def X(x):
        return (x - minx) * s + margin

    def Y(y):
        return (y - miny) * s + margin  # NO flip; DXF is Y-up

    def n(v):
        return _num(v, decimals)

    w = (maxx - minx) * s + 2 * margin
    h = (maxy - miny) * s + 2 * margin

    out = []

    def emit(code, value):
        out.append(_pair(code, value))

    seen = set()
    layers = []
    for name, color in ([("0", 7), ("FIDUCIAL", 1), ("LABEL", 1)]
                        + [(grp.get("layer", "0"), grp.get("color", 7))
                           for grp in groups]):
        if name in seen:
            continue
        seen.add(name)
        layers.append((name, color))

    # ---- HEADER ----
    emit(0, "SECTION"); emit(2, "HEADER")
    emit(9, "$ACADVER"); emit(1, "AC1009")
    emit(9, "$INSUNITS"); emit(70, insunits)
    emit(9, "$MEASUREMENT"); emit(70, measurement)
    emit(9, "$EXTMIN"); emit(10, n(0.0)); emit(20, n(0.0)); emit(30, n(0.0))
    emit(9, "$EXTMAX"); emit(10, n(w)); emit(20, n(h)); emit(30, n(0.0))
    emit(0, "ENDSEC")

    # ---- TABLES (LTYPE + LAYER) ----
    emit(0, "SECTION"); emit(2, "TABLES")
    emit(0, "TABLE"); emit(2, "LTYPE"); emit(70, 1)
    emit(0, "LTYPE"); emit(2, "CONTINUOUS"); emit(70, 0)
    emit(3, "Solid line"); emit(72, 65); emit(73, 0); emit(40, n(0.0))
    emit(0, "ENDTAB")
    emit(0, "TABLE"); emit(2, "LAYER"); emit(70, len(layers))
    for name, color in layers:
        emit(0, "LAYER"); emit(2, name); emit(70, 0)
        emit(62, color); emit(6, "CONTINUOUS")
    emit(0, "ENDTAB")
    emit(0, "ENDSEC")

    # ---- ENTITIES ----
    emit(0, "SECTION"); emit(2, "ENTITIES")
    for grp in groups:
        layer = grp.get("layer", "0")
        for e in grp.get("elements", []):
            _emit_element(e, emit, X, Y, n, s, layer)
    for e in fiducials:
        _emit_element(e, emit, X, Y, n, s, "FIDUCIAL")
    for text, (lx, ly), height_cm in labels:
        _emit_text(emit, text, X(lx), Y(ly), height_cm * s, n)
    emit(0, "ENDSEC")

    emit(0, "EOF")
    return "\n".join(out) + "\n"


def render(elements, unit="in", fiducials=None, labels=None, margin=0.0,
           decimals=6):
    """Single-layer DXF (cut geometry on layer 0). Delegates to
    :func:`render_groups`."""
    return render_groups([{"elements": elements, "layer": "0", "color": 7}],
                         unit=unit, fiducials=fiducials, labels=labels,
                         margin=margin, decimals=decimals)


def _polyline(emit, pts, closed, n, layer):
    # R12 POLYLINE: a header entity (66=vertices-follow, 70=closed flag) with a
    # default 0,0,0 point, then VERTEX entities, terminated by SEQEND.
    emit(0, "POLYLINE"); emit(8, layer)
    emit(66, 1); emit(70, 1 if closed else 0)
    emit(10, n(0.0)); emit(20, n(0.0)); emit(30, n(0.0))
    for x, y in pts:
        emit(0, "VERTEX"); emit(8, layer)
        emit(10, n(x)); emit(20, n(y)); emit(30, n(0.0))
    emit(0, "SEQEND"); emit(8, layer)


def _emit_element(e, emit, X, Y, n, s, layer):
    if isinstance(e, g.Line):
        emit(0, "LINE"); emit(8, layer)
        emit(10, n(X(e.p0[0]))); emit(20, n(Y(e.p0[1]))); emit(30, n(0.0))
        emit(11, n(X(e.p1[0]))); emit(21, n(Y(e.p1[1]))); emit(31, n(0.0))

    elif isinstance(e, g.Polyline):
        if len(e.points) < 2:
            return
        _polyline(emit, [(X(p[0]), Y(p[1])) for p in e.points], e.closed, n, layer)

    elif isinstance(e, g.Circle):
        emit(0, "CIRCLE"); emit(8, layer)
        emit(10, n(X(e.center[0]))); emit(20, n(Y(e.center[1]))); emit(30, n(0.0))
        emit(40, n(e.radius * s))

    elif isinstance(e, g.Arc):
        # DXF arcs run CCW from start to end angle. No Y-flip here, so a CCW
        # sketch arc stays CCW; a CW one is emitted with swapped angles.
        cx, cy = X(e.center[0]), Y(e.center[1])
        start = math.degrees(e.start_angle)
        end = math.degrees(e.end_angle)
        if not e.ccw:
            start, end = end, start
        emit(0, "ARC"); emit(8, layer)
        emit(10, n(cx)); emit(20, n(cy)); emit(30, n(0.0))
        emit(40, n(e.radius * s)); emit(50, n(start)); emit(51, n(end))

    elif isinstance(e, g.Ellipse):
        pts = [(X(p[0]), Y(p[1])) for p in g.sample_element_points(e)]
        _polyline(emit, pts, True, n, layer)

    else:
        raise TypeError("Unsupported element type: %r" % (type(e),))


def _emit_text(emit, text, x, y, height, n):
    emit(0, "TEXT"); emit(8, "LABEL")
    emit(10, n(x)); emit(20, n(y)); emit(30, n(0.0))
    emit(40, n(height)); emit(1, str(text))
    emit(72, 1); emit(73, 2)              # halign center, valign middle
    emit(11, n(x)); emit(21, n(y)); emit(31, n(0.0))
