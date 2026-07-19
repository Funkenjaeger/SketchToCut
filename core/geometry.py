"""Dependency-free 2D geometry intermediate representation (IR).

All coordinates are 2D tuples ``(x, y)`` in **centimetres** -- the native unit
Fusion's API returns for every length. Nothing here scales to the output unit;
that happens once, in :mod:`core.svg`, at emit time.

Element types intentionally cover both crisp primitives (Line, Circle,
Ellipse, Arc) and the catch-all Polyline. The Fusion add-in currently emits
Lines, Circles and Polylines (arcs/splines/ellipses are flattened via the
curve evaluator for robustness), but the primitive types are fully implemented
and unit-tested so a future path can emit crisp arcs/ellipses without rework.
"""

import math
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

Point = Tuple[float, float]

TWO_PI = 2.0 * math.pi


def cm_to(unit: str) -> float:
    """Scale factor from centimetres to ``unit`` (the SVG output unit)."""
    u = unit.lower()
    if u in ("in", "inch", "inches"):
        return 1.0 / 2.54
    if u in ("mm", "millimeter", "millimeters", "millimetre", "millimetres"):
        return 10.0
    if u in ("cm", "centimeter", "centimeters", "centimetre", "centimetres"):
        return 1.0
    raise ValueError("Unsupported unit: %r" % (unit,))


def unit_suffix(unit: str) -> str:
    """CSS length suffix for the SVG ``width``/``height`` attributes."""
    u = unit.lower()
    if u in ("in", "inch", "inches"):
        return "in"
    if u in ("mm", "millimeter", "millimeters", "millimetre", "millimetres"):
        return "mm"
    if u in ("cm", "centimeter", "centimeters", "centimetre", "centimetres"):
        return "cm"
    raise ValueError("Unsupported unit: %r" % (unit,))


def _norm(a: float) -> float:
    """Normalise an angle to ``[0, 2*pi)``."""
    a = math.fmod(a, TWO_PI)
    if a < 0:
        a += TWO_PI
    return a


def arc_contains_angle(start: float, end: float, ccw: bool, a: float) -> bool:
    """True if angle ``a`` lies on the arc swept from ``start`` to ``end``."""
    if ccw:
        sweep = _norm(end - start)
        d = _norm(a - start)
    else:
        sweep = _norm(start - end)
        d = _norm(start - a)
    if sweep <= 1e-12:
        sweep = TWO_PI  # degenerate: treat as a full turn
    return d <= sweep + 1e-9


@dataclass
class Line:
    p0: Point
    p1: Point

    def extents(self) -> Tuple[float, float, float, float]:
        (x0, y0), (x1, y1) = self.p0, self.p1
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


@dataclass
class Polyline:
    points: List[Point]
    closed: bool = False

    def extents(self) -> Tuple[float, float, float, float]:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class Circle:
    center: Point
    radius: float

    def extents(self) -> Tuple[float, float, float, float]:
        cx, cy = self.center
        r = self.radius
        return (cx - r, cy - r, cx + r, cy + r)


@dataclass
class Arc:
    center: Point
    radius: float
    start_angle: float  # radians, measured CCW from +x
    end_angle: float
    ccw: bool = True

    def start_point(self) -> Point:
        cx, cy = self.center
        return (cx + self.radius * math.cos(self.start_angle),
                cy + self.radius * math.sin(self.start_angle))

    def end_point(self) -> Point:
        cx, cy = self.center
        return (cx + self.radius * math.cos(self.end_angle),
                cy + self.radius * math.sin(self.end_angle))

    def sweep(self) -> float:
        """Positive angular extent in ``(0, 2*pi]``."""
        s = _norm(self.end_angle - self.start_angle) if self.ccw \
            else _norm(self.start_angle - self.end_angle)
        return s if s > 1e-12 else TWO_PI

    def extents(self) -> Tuple[float, float, float, float]:
        cx, cy = self.center
        r = self.radius
        pts = [self.start_point(), self.end_point()]
        for k in range(4):  # cardinal extremes that fall within the sweep
            a = k * (math.pi / 2.0)
            if arc_contains_angle(self.start_angle, self.end_angle, self.ccw, a):
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class Ellipse:
    center: Point
    r_major: float
    r_minor: float
    rotation: float = 0.0  # radians, angle of the major axis from +x

    def extents(self) -> Tuple[float, float, float, float]:
        cx, cy = self.center
        c = math.cos(self.rotation)
        s = math.sin(self.rotation)
        hw = math.hypot(self.r_major * c, self.r_minor * s)
        hh = math.hypot(self.r_major * s, self.r_minor * c)
        return (cx - hw, cy - hh, cx + hw, cy + hh)


def bounding_box(elements: Sequence[object]):
    """Axis-aligned bbox ``(minx, miny, maxx, maxy)`` over all elements, or None."""
    boxes = [e.extents() for e in elements]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


# --- polygon helpers (used by per-region export) ---------------------------
def signed_area(points: Sequence[Point]) -> float:
    """Shoelace signed area of a closed polygon; +ve for CCW winding."""
    n = len(points)
    a = 0.0
    for i in range(n):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return a / 2.0


def polygon_centroid(points: Sequence[Point]) -> Point:
    """Area centroid of a closed polygon (falls back to vertex mean if degenerate)."""
    n = len(points)
    if n == 0:
        raise ValueError("no points")
    a = signed_area(points)
    if abs(a) < 1e-12:
        return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n)
    cx = cy = 0.0
    for i in range(n):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    return (cx / (6.0 * a), cy / (6.0 * a))


def point_in_polygon(pt: Point, poly: Sequence[Point]) -> bool:
    """Ray-casting point-in-polygon test (poly is a closed ring, no repeat)."""
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and \
                (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def rotate_point(p: Point, theta: float, center: Point = (0.0, 0.0)) -> Point:
    """Rotate a point ``theta`` radians CCW about ``center``."""
    c = math.cos(theta)
    s = math.sin(theta)
    dx = p[0] - center[0]
    dy = p[1] - center[1]
    return (center[0] + dx * c - dy * s, center[1] + dx * s + dy * c)


def rotate_element(e, theta: float, center: Point = (0.0, 0.0)):
    """Return a copy of an IR element rotated ``theta`` radians about ``center``."""
    if isinstance(e, Line):
        return Line(rotate_point(e.p0, theta, center), rotate_point(e.p1, theta, center))
    if isinstance(e, Polyline):
        return Polyline([rotate_point(p, theta, center) for p in e.points], e.closed)
    if isinstance(e, Circle):
        return Circle(rotate_point(e.center, theta, center), e.radius)
    if isinstance(e, Ellipse):
        return Ellipse(rotate_point(e.center, theta, center), e.r_major, e.r_minor,
                       e.rotation + theta)
    if isinstance(e, Arc):
        return Arc(rotate_point(e.center, theta, center), e.radius,
                   e.start_angle + theta, e.end_angle + theta, e.ccw)
    raise TypeError("Cannot rotate element type: %r" % (type(e),))


def sample_element_points(e, n: int = 48) -> List[Point]:
    """A point cloud approximating an element, for hull / bbox-under-rotation."""
    if isinstance(e, Line):
        return [e.p0, e.p1]
    if isinstance(e, Polyline):
        return list(e.points)
    if isinstance(e, Circle):
        cx, cy = e.center
        return [(cx + e.radius * math.cos(TWO_PI * i / n),
                 cy + e.radius * math.sin(TWO_PI * i / n)) for i in range(n)]
    if isinstance(e, Ellipse):
        cx, cy = e.center
        c = math.cos(e.rotation)
        s = math.sin(e.rotation)
        pts = []
        for i in range(n):
            a = TWO_PI * i / n
            x = e.r_major * math.cos(a)
            y = e.r_minor * math.sin(a)
            pts.append((cx + x * c - y * s, cy + x * s + y * c))
        return pts
    if isinstance(e, Arc):
        cx, cy = e.center
        sweep = e.sweep()
        steps = max(2, int(n * sweep / TWO_PI) + 1)
        sign = 1.0 if e.ccw else -1.0
        return [(cx + e.radius * math.cos(e.start_angle + sign * sweep * i / steps),
                 cy + e.radius * math.sin(e.start_angle + sign * sweep * i / steps))
                for i in range(steps + 1)]
    raise TypeError("Cannot sample element type: %r" % (type(e),))
