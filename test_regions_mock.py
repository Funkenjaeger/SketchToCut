"""Integration test for SketchToSVG per-region export using a mock Fusion API.

Fakes just the slice of adsk.core / adsk.fusion that extract_regions and
run_per_region_export touch, then drives the REAL add-in code. Scenarios:
  * split      -- rectangle cut in two: shared-edge fiducials, A/B + MASTER.
  * hole       -- square with a circular hole: void-disc suppressed, one file.
  * fit        -- auto-rotate to fit, oversized piece skipped.
  * trim       -- region whose top edge is only PART of a long line; must use
                  the trimmed ProfileCurve.geometry, not the full sketch curve
                  (regression for the "top arc bleeding into the SVG" bug).

Profile geometry is modelled in sketch space (what ProfileCurve.geometry
returns), and each profile curve carries an independent trimmed geometry vs a
full sketchEntity geometry so the trim behaviour can be exercised.
"""

import importlib.util
import math
import os
import sys
import tempfile
import types

ADDIN = os.path.dirname(os.path.abspath(__file__))

LINE3D = "Line3D"
CIRCLE3D = "Circle3D"

# ---- fake adsk.core (geometry lives here) ---------------------------------
core = types.ModuleType("adsk.core")
core.CommandEventHandler = type("CommandEventHandler", (), {})
core.CommandCreatedEventHandler = type("CommandCreatedEventHandler", (), {})
core.DropDownStyles = type("DropDownStyles", (), {"TextListDropDownStyle": 0})
core.Application = types.SimpleNamespace(get=staticmethod(lambda: None))
core.Point3D = type("Point3D", (), {"create": staticmethod(
    lambda x, y, z=0.0: types.SimpleNamespace(x=x, y=y, z=z))})
core.Line3D = type("Line3D", (), {"classType": staticmethod(lambda: LINE3D)})
core.Circle3D = type("Circle3D", (), {"classType": staticmethod(lambda: CIRCLE3D)})

fusion = types.ModuleType("adsk.fusion")
fusion.Profile = type("Profile", (), {"classType": staticmethod(lambda: "Profile")})
fusion.Sketch = type("Sketch", (), {"classType": staticmethod(lambda: "Sketch")})

adsk = types.ModuleType("adsk")
adsk.core, adsk.fusion = core, fusion
sys.modules.update({"adsk": adsk, "adsk.core": core, "adsk.fusion": fusion})


# ---- fake geometry / profile objects --------------------------------------
class _Pt:
    def __init__(self, x, y):
        self.x, self.y = x, y


class L3:
    def __init__(self, p0, p1):
        self.objectType = LINE3D
        self.startPoint = _Pt(*p0)
        self.endPoint = _Pt(*p1)


class _CircEval:
    def __init__(self, c, r):
        self.c, self.r = c, r

    def getParameterExtents(self):
        return (True, 0.0, 2 * math.pi)

    def getStrokes(self, a, b, tol):
        return (True, [_Pt(self.c[0] + self.r * math.cos(2 * math.pi * i / 16),
                           self.c[1] + self.r * math.sin(2 * math.pi * i / 16))
                       for i in range(17)])


class C3:
    def __init__(self, center, r):
        self.objectType = CIRCLE3D
        self.center = _Pt(*center)
        self.radius = r
        self.evaluator = _CircEval(center, r)


class Entity:
    def __init__(self, full_geom):
        self.geometry = full_geom
        self.isConstruction = False
        self.isReference = False


class PC:
    def __init__(self, trimmed, full):
        self.geometry = trimmed          # ProfileCurve.geometry (trimmed)
        self.sketchEntity = Entity(full)  # full untrimmed curve


class Coll:
    def __init__(self, items):
        self._i = list(items)

    @property
    def count(self):
        return len(self._i)

    def item(self, i):
        return self._i[i]


class Loop:
    def __init__(self, pcs, is_outer):
        self.isOuter = is_outer
        self.profileCurves = Coll(pcs)


class Profile:
    def __init__(self, loops):
        self.objectType = "Profile"
        self.profileLoops = Coll(loops)


class Sketch:
    def __init__(self, profiles):
        self.name = "TestSketch"
        self.profiles = Coll(profiles)
        empty = Coll([])
        self.sketchCurves = types.SimpleNamespace(
            sketchLines=empty, sketchArcs=empty, sketchCircles=empty,
            sketchEllipses=empty, sketchFittedSplines=empty,
            sketchFixedSplines=empty, sketchControlPointSplines=empty,
            sketchConicCurves=empty)


def line_pc(p0, p1):
    return PC(L3(p0, p1), L3(p0, p1))            # trimmed == full


def line_pc_trim(t0, t1, f0, f1):
    return PC(L3(t0, t1), L3(f0, f1))            # trimmed sub-segment of full


def circle_pc(center, r):
    return PC(C3(center, r), C3(center, r))


def rect_pcs(x0, y0, x1, y1):
    c = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [line_pc(c[i], c[(i + 1) % 4]) for i in range(4)]


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
import re  # noqa: E402

_fail = []


def check(cond, msg):
    print(("  PASS: " if cond else "  FAIL: ") + msg)
    if not cond:
        _fail.append(msg)


def attr(text, name):
    mt = re.search(r'%s="([^"]*)"' % name, text)
    return mt.group(1) if mt else None


def opts(folder, base, unit="mm", bed_w=6.0, bed_h=6.0, bed_w_cm=6.0,
         bed_h_cm=6.0, fid=True, labels=False, fmt="svg"):
    return {"unit": unit, "stroke_width": 0.01, "fmt": fmt,
            "bed_w": bed_w, "bed_h": bed_h,
            "bed_w_cm": bed_w_cm, "bed_h_cm": bed_h_cm, "fid_enabled": fid,
            "fid_len_cm": 0.6, "fid_spacing_cm": 5.0, "fid_inset_cm": 0.3,
            "fid_color": "red", "label_on_pieces": labels,
            "folder": folder, "base": base}


def read(folder, name):
    with open(os.path.join(folder, name), encoding="utf-8") as fp:
        return fp.read()


def scenario_split():
    print("scenario_split (10x4 rectangle cut at x=5 -> A|B):")
    left = Profile([Loop([line_pc((0, 0), (5, 0)), line_pc((5, 0), (5, 4)),
                          line_pc((5, 4), (0, 4)), line_pc((0, 4), (0, 0))], True)])
    right = Profile([Loop([line_pc((5, 0), (10, 0)), line_pc((10, 0), (10, 4)),
                           line_pc((10, 4), (5, 4)), line_pc((5, 4), (5, 0))], True)])
    d = tempfile.mkdtemp()
    summary = m.run_per_region_export(Sketch([left, right]), [left, right],
                                      opts(d, "split"))
    files = sorted(os.listdir(d))
    check(files == ["split_A.svg", "split_B.svg", "split_MASTER.svg"],
          "wrote A, B, MASTER (got %s)" % files)
    a = read(d, "split_A.svg")
    check('class="fiducial"' in a, "piece A has a fiducial group (shared edge)")
    check("Z" in attr(a, "d"), "piece A outline is a closed path")
    master = read(d, "split_MASTER.svg")
    check(">A<" in master, "master labels A")
    # Master's fiducial group carries both the labels AND the tick paths.
    fid_group = master.split('class="fiducial"')[1]
    check("<path" in fid_group, "master fiducial group includes tick paths")
    check("Pieces: A, B" in summary, "summary lists A, B")


def scenario_hole():
    print("scenario_hole (6x6 square + circular hole -> 1 file, disc suppressed):")
    ring = Profile([Loop(rect_pcs(0, 0, 6, 6), True), Loop([circle_pc((3, 3), 1)], False)])
    disc = Profile([Loop([circle_pc((3, 3), 1)], True)])
    d = tempfile.mkdtemp()
    m.run_per_region_export(Sketch([ring, disc]), [ring, disc],
                            opts(d, "holed", bed_w=8, bed_h=8, bed_w_cm=8, bed_h_cm=8))
    files = sorted(os.listdir(d))
    check(files == ["holed.svg"], "single region -> base.svg, no _A/MASTER: %s" % files)
    a = read(d, "holed.svg")
    check("<circle" in a, "hole present as a circle")
    check(a.count("<path") >= 1, "outer square present as a path")


def scenario_fit():
    print("scenario_fit (single 8x3 piece auto-rotated to fit a 4x100 bed):")
    piece = Profile([Loop(rect_pcs(0, 0, 8, 3), True)])
    d = tempfile.mkdtemp()
    m.run_per_region_export(
        Sketch([piece]), [piece],
        opts(d, "fit", unit="cm", bed_w=4, bed_h=100, bed_w_cm=4, bed_h_cm=100,
             fid=False))
    files = sorted(os.listdir(d))
    check(files == ["fit.svg"], "single fitting piece -> fit.svg (got %s)" % files)
    check(attr(read(d, "fit.svg"), "width") == "3cm",
          "8x3 auto-rotated 90deg -> width 3cm")


def _dim(text, name):
    return float(attr(text, name).rstrip("cm"))


def scenario_tile():
    print("scenario_tile (30x30 region tiled into a 12x24 bed):")
    piece = Profile([Loop(rect_pcs(0, 0, 30, 30), True)])
    d = tempfile.mkdtemp()
    summary = m.run_per_region_export(
        Sketch([piece]), [piece],
        opts(d, "big", unit="cm", bed_w=12, bed_h=24, bed_w_cm=12, bed_h_cm=24,
             fid=True))
    tiles = sorted(f for f in os.listdir(d) if not f.endswith("MASTER.svg"))
    check(len(tiles) == 6, "30x30 -> ceil(30/12) x ceil(30/24) = 6 tiles (got %d)"
          % len(tiles))
    check("auto-tiled" in summary, "summary reports auto-tiling")
    fits = all(_dim(read(d, f), "width") <= 12 + 1e-6
               and _dim(read(d, f), "height") <= 24 + 1e-6 for f in tiles)
    check(fits, "every tile fits within the 12x24 bed")
    check(any('class="fiducial"' in read(d, f) for f in tiles),
          "tiles carry cut-edge fiducials")
    check('class="fiducial"' in read(d, "big_MASTER.svg"),
          "master shows the tile fiducials")


def scenario_trim():
    print("scenario_trim (top edge is a trimmed sub-segment of a long line):")
    # 4x4 square; its top edge (4,4)-(0,4) is only part of a line spanning
    # (-10,4)..(20,4). Using the full line would blow the width out to ~30.
    reg = Profile([Loop([
        line_pc((0, 0), (4, 0)),
        line_pc((4, 0), (4, 4)),
        line_pc_trim((4, 4), (0, 4), (20, 4), (-10, 4)),  # trimmed top edge
        line_pc((0, 4), (0, 0))], True)])
    d = tempfile.mkdtemp()
    m.run_per_region_export(Sketch([reg]), [reg],
                            opts(d, "trim", unit="cm", bed_w=100, bed_h=100,
                                 bed_w_cm=100, bed_h_cm=100, fid=False))
    svg = read(d, "trim.svg")
    w = attr(svg, "width")
    h = attr(svg, "height")
    check(w == "4cm", "uses TRIMMED top edge -> width 4cm, not ~30 (got %s)" % w)
    check(h == "4cm", "height 4cm (got %s)" % h)


def scenario_dxf():
    print("scenario_dxf (DXF output through tiling + fiducials):")
    piece = Profile([Loop(rect_pcs(0, 0, 30, 30), True)])
    d = tempfile.mkdtemp()
    m.run_per_region_export(
        Sketch([piece]), [piece],
        opts(d, "laser", unit="mm", bed_w=120, bed_h=240, bed_w_cm=12,
             bed_h_cm=24, fid=True, fmt="dxf"))
    files = sorted(os.listdir(d))
    check(all(f.endswith(".dxf") for f in files), "all outputs are .dxf: %s" % files)
    check(any(f == "laser_MASTER.dxf" for f in files), "DXF master emitted")
    sample = read(d, [f for f in files if f != "laser_MASTER.dxf"][0])
    check("POLYLINE" in sample and sample.rstrip().endswith("EOF"),
          "a tile DXF is well-formed (POLYLINE + EOF)")


def main():
    scenario_split()
    scenario_hole()
    scenario_fit()
    scenario_tile()
    scenario_dxf()
    scenario_trim()
    print()
    if _fail:
        print("%d FAILURE(S)" % len(_fail))
        sys.exit(1)
    print("ALL INTEGRATION CHECKS PASSED")


if __name__ == "__main__":
    main()
