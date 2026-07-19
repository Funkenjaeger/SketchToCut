"""Integration test for SketchToSVG per-region export using a mock Fusion API.

Fakes just the slice of adsk.core / adsk.fusion that extract_regions and
run_per_region_export touch, then drives the REAL add-in code against three
scenarios: a split rectangle (shared-edge fiducials), a square-with-hole
(void suppression), and fit/skip (auto-rotate + oversized skip).
"""

import importlib.util
import math
import os
import re
import sys
import tempfile
import types

ADDIN = os.path.dirname(os.path.abspath(__file__))

LINE = "SketchLine"
CIRCLE = "SketchCircle"

# ---- fake adsk.core -------------------------------------------------------
core = types.ModuleType("adsk.core")


class _P:
    def __init__(self, x, y, z=0.0):
        self.x, self.y, self.z = x, y, z

    def transformBy(self, m):
        return True


class Point3D:
    @staticmethod
    def create(x, y, z=0.0):
        return _P(x, y, z)


class CommandEventHandler:
    pass


class CommandCreatedEventHandler:
    pass


class DropDownStyles:
    TextListDropDownStyle = 0


core.Point3D = Point3D
core.CommandEventHandler = CommandEventHandler
core.CommandCreatedEventHandler = CommandCreatedEventHandler
core.DropDownStyles = DropDownStyles
core.Application = types.SimpleNamespace(get=staticmethod(lambda: None))

# ---- fake adsk.fusion -----------------------------------------------------
fusion = types.ModuleType("adsk.fusion")


def _mk_classtype(name):
    return staticmethod(lambda n=name: n)


fusion.SketchLine = type("SketchLine", (), {"classType": _mk_classtype(LINE)})
fusion.SketchCircle = type("SketchCircle", (), {"classType": _mk_classtype(CIRCLE)})
fusion.Sketch = type("Sketch", (), {"classType": _mk_classtype("Sketch")})
fusion.Profile = type("Profile", (), {"classType": _mk_classtype("Profile")})

adsk = types.ModuleType("adsk")
adsk.core = core
adsk.fusion = fusion
sys.modules["adsk"] = adsk
sys.modules["adsk.core"] = core
sys.modules["adsk.fusion"] = fusion


# ---- fake sketch objects --------------------------------------------------
class Coll:
    def __init__(self, items):
        self._items = list(items)

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return self._items[i]

    def __iter__(self):
        return iter(self._items)


class Matrix:
    def copy(self):
        return self

    def invert(self):
        return True


class FakeLine:
    def __init__(self, p0, p1):
        self.objectType = LINE
        self.isConstruction = False
        self.isReference = False
        self.worldGeometry = types.SimpleNamespace(
            startPoint=_P(*p0), endPoint=_P(*p1))


class _CircleEval:
    def __init__(self, c, r):
        self.c, self.r = c, r

    def getParameterExtents(self):
        return (True, 0.0, 2 * math.pi)

    def getStrokes(self, a, b, tol):
        pts = [_P(self.c[0] + self.r * math.cos(2 * math.pi * i / 16),
                  self.c[1] + self.r * math.sin(2 * math.pi * i / 16))
               for i in range(17)]
        return (True, pts)


class FakeCircle:
    def __init__(self, center, r):
        self.objectType = CIRCLE
        self.isConstruction = False
        self.isReference = False
        self.radius = r
        self.worldGeometry = types.SimpleNamespace(
            center=_P(*center), evaluator=_CircleEval(center, r))


class PC:
    def __init__(self, entity):
        self.sketchEntity = entity


class Loop:
    def __init__(self, entities, is_outer):
        self.isOuter = is_outer
        self.profileCurves = Coll([PC(e) for e in entities])


class Profile:
    def __init__(self, loops):
        self.objectType = "Profile"
        self.profileLoops = Coll(loops)


def rect_lines(x0, y0, x1, y1):
    c = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [FakeLine(c[i], c[(i + 1) % 4]) for i in range(4)]


class Sketch:
    def __init__(self, profiles, stray_lines=None):
        self.name = "TestSketch"
        self.transform = Matrix()
        self.profiles = Coll(profiles)
        empty = Coll([])
        self.sketchCurves = types.SimpleNamespace(
            sketchLines=Coll(stray_lines or []), sketchArcs=empty,
            sketchCircles=empty, sketchEllipses=empty, sketchFittedSplines=empty,
            sketchFixedSplines=empty, sketchControlPointSplines=empty,
            sketchConicCurves=empty)


# ---- load the real add-in module as a package submodule -------------------
pkg = types.ModuleType("stsvg")
pkg.__path__ = [ADDIN]
sys.modules["stsvg"] = pkg
spec = importlib.util.spec_from_file_location(
    "stsvg.SketchToSVG", os.path.join(ADDIN, "SketchToSVG.py"))
m = importlib.util.module_from_spec(spec)
sys.modules["stsvg.SketchToSVG"] = m
spec.loader.exec_module(m)


# ---- assertions -----------------------------------------------------------
_fail = []


def check(cond, msg):
    print(("  PASS: " if cond else "  FAIL: ") + msg)
    if not cond:
        _fail.append(msg)


def attr(text, name):
    mt = re.search(r'%s="([^"]*)"' % name, text)
    return mt.group(1) if mt else None


def opts(folder, base, unit="mm", bed_w=6.0, bed_h=6.0, bed_w_cm=6.0,
         bed_h_cm=6.0, fid=True, labels=False):
    return {"unit": unit, "stroke_width": 0.01, "bed_w": bed_w, "bed_h": bed_h,
            "bed_w_cm": bed_w_cm, "bed_h_cm": bed_h_cm, "fid_enabled": fid,
            "fid_len_cm": 0.6, "fid_spacing_cm": 5.0, "fid_inset_cm": 0.3,
            "fid_color": "red", "label_on_pieces": labels,
            "folder": folder, "base": base}


def read(folder, name):
    with open(os.path.join(folder, name), encoding="utf-8") as fp:
        return fp.read()


def scenario_split():
    print("scenario_split (10x4 rectangle cut at x=5 -> A|B):")
    shared = FakeLine((5, 0), (5, 4))
    left = Profile([Loop([FakeLine((0, 0), (5, 0)), shared,
                          FakeLine((5, 4), (0, 4)), FakeLine((0, 4), (0, 0))], True)])
    right = Profile([Loop([FakeLine((5, 0), (10, 0)), FakeLine((10, 0), (10, 4)),
                           FakeLine((10, 4), (5, 4)), shared], True)])
    sk = Sketch([left, right])
    d = tempfile.mkdtemp()
    summary = m.run_per_region_export(sk, [left, right], opts(d, "split"))
    files = sorted(os.listdir(d))
    check(files == ["split_A.svg", "split_B.svg", "split_MASTER.svg"],
          "wrote A, B, MASTER (got %s)" % files)
    a = read(d, "split_A.svg")
    check('class="fiducial"' in a, "piece A has a fiducial group (shared edge)")
    check("Z" in attr(a, "d"), "piece A outline is a closed path")
    master = read(d, "split_MASTER.svg")
    check(">A<" in master and ">B<" in master, "master labels both A and B")
    # A is left (smaller x), B is right -- both same height so x decides.
    check("Pieces: A, B" in summary, "summary lists A, B  (%r)" % summary.splitlines()[1])


def scenario_hole():
    print("scenario_hole (6x6 square with a circular hole -> one piece, disc suppressed):")
    hole = FakeCircle((3, 3), 1)
    ring = Profile([Loop(rect_lines(0, 0, 6, 6), True), Loop([hole], False)])
    disc = Profile([Loop([hole], True)])
    sk = Sketch([ring, disc])
    d = tempfile.mkdtemp()
    m.run_per_region_export(sk, [ring, disc], opts(d, "holed", bed_w=8, bed_h=8,
                                                   bed_w_cm=8, bed_h_cm=8))
    files = sorted(f for f in os.listdir(d) if not f.endswith("MASTER.svg"))
    check(files == ["holed_A.svg"], "exactly one piece file (disc suppressed): %s" % files)
    a = read(d, "holed_A.svg")
    check("<circle" in a, "the hole is present as a circle in the piece")
    check(a.count("<path") >= 1, "outer square present as a path")


def scenario_fit():
    print("scenario_fit (auto-rotate + skip oversized):")
    fit_piece = Profile([Loop(rect_lines(0, 0, 8, 3), True)])   # 8x3
    big_piece = Profile([Loop(rect_lines(20, 0, 70, 50), True)])  # 50x50, far away
    sk = Sketch([fit_piece, big_piece])
    d = tempfile.mkdtemp()
    # bed 4 x 100 cm, unit cm so numbers are direct.
    summary = m.run_per_region_export(
        sk, [fit_piece, big_piece],
        opts(d, "fit", unit="cm", bed_w=4, bed_h=100, bed_w_cm=4, bed_h_cm=100,
             fid=False))
    files = sorted(f for f in os.listdir(d) if not f.endswith("MASTER.svg"))
    check(len(files) == 1, "one piece written, one skipped (got %s)" % files)
    piece = read(d, files[0])
    w = attr(piece, "width")
    check(w == "3cm", "8x3 piece auto-rotated 90deg -> width 3cm (got %s)" % w)
    check("SKIPPED" in summary and "at any rotation): A" in summary,
          "oversized piece (letter A) reported as skipped  (%r)" % summary)


def main():
    scenario_split()
    scenario_hole()
    scenario_fit()
    print()
    if _fail:
        print("%d FAILURE(S)" % len(_fail))
        sys.exit(1)
    print("ALL INTEGRATION CHECKS PASSED")


if __name__ == "__main__":
    main()
