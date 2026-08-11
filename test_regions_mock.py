"""Integration test for SketchToCut per-region export using a mock Fusion API.

Fakes just the slice of adsk.core / adsk.fusion that extract_regions and
run_per_region_export touch, then drives the REAL add-in code. Scenarios:
  * split      -- rectangle cut in two: shared-edge fiducials, A/B + ASSEMBLY.
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
pkg = types.ModuleType("stcut")
pkg.__path__ = [ADDIN]
sys.modules["stcut"] = pkg
spec = importlib.util.spec_from_file_location(
    "stcut.SketchToCut", os.path.join(ADDIN, "SketchToCut.py"))
m = importlib.util.module_from_spec(spec)
sys.modules["stcut.SketchToCut"] = m
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
         bed_h_cm=6.0, fid=True, labels=False, fmt="svg", filled=True):
    return {"unit": unit, "stroke_width": 0.01, "fmt": fmt,
            "bed_w": bed_w, "bed_h": bed_h,
            "bed_w_cm": bed_w_cm, "bed_h_cm": bed_h_cm, "fid_enabled": fid,
            "fid_len_cm": 0.6, "fid_spacing_cm": 5.0, "fid_inset_cm": 0.3,
            "fid_color": "red", "label_on_pieces": labels, "filled": filled,
            "folder": folder, "base": base}


def read(folder, name):
    with open(os.path.join(folder, name), encoding="utf-8") as fp:
        return fp.read()


def has_fiducials(text):
    # In fill mode a piece's fiducial ticks are a stroked group in piece color.
    return 'fill="none" stroke="#' in text


def _dim(text, name):
    return float(attr(text, name).rstrip("cm"))


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
    check(files == ["split_A.svg", "split_ASSEMBLY.svg", "split_B.svg"],
          "wrote A, B, ASSEMBLY (got %s)" % files)
    a = read(d, "split_A.svg")
    check(has_fiducials(a), "piece A carries fiducial ticks (in piece color)")
    check("Z" in attr(a, "d"), "piece A outline is a closed filled path")
    check('fill-rule="evenodd"' in a or 'fill="#' in a, "piece A is filled")
    assembly = read(d, "split_ASSEMBLY.svg")
    check(">A<" in assembly and ">B<" in assembly, "assembly labels A and B")
    check('class="label"' in assembly and not has_fiducials(assembly),
          "assembly has letters (label group), no fiducials")
    check("Pieces: A, B" in summary, "summary lists A, B")


def scenario_hole():
    print("scenario_hole (6x6 square + circular hole -> 1 file, disc suppressed):")
    ring = Profile([Loop(rect_pcs(0, 0, 6, 6), True), Loop([circle_pc((3, 3), 1)], False)])
    disc = Profile([Loop([circle_pc((3, 3), 1)], True)])
    d = tempfile.mkdtemp()
    m.run_per_region_export(Sketch([ring, disc]), [ring, disc],
                            opts(d, "holed", bed_w=8, bed_h=8, bed_w_cm=8, bed_h_cm=8))
    files = sorted(os.listdir(d))
    check(files == ["holed.svg"],
          "single region -> base.svg, no _A/ASSEMBLY: %s" % files)
    a = read(d, "holed.svg")
    check('fill-rule="evenodd"' in a, "hole -> even-odd fill")
    check(attr(a, "d").count("M") == 2, "compound path: outer + hole subpaths")


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


def scenario_tile():
    print("scenario_tile (30x30 region tiled into a 12x24 bed):")
    piece = Profile([Loop(rect_pcs(0, 0, 30, 30), True)])
    d = tempfile.mkdtemp()
    summary = m.run_per_region_export(
        Sketch([piece]), [piece],
        opts(d, "big", unit="cm", bed_w=12, bed_h=24, bed_w_cm=12, bed_h_cm=24,
             fid=True))
    tiles = sorted(f for f in os.listdir(d) if not f.endswith("ASSEMBLY.svg"))
    check(len(tiles) == 6, "30x30 -> ceil(30/12) x ceil(30/24) = 6 tiles (got %d)"
          % len(tiles))
    check("auto-tiled" in summary, "summary reports auto-tiling")
    fits = all(_dim(read(d, f), "width") <= 12 + 1e-6
               and _dim(read(d, f), "height") <= 24 + 1e-6 for f in tiles)
    check(fits, "every tile fits within the 12x24 bed")
    check(any(has_fiducials(read(d, f)) for f in tiles),
          "tiles carry cut-edge fiducials (piece color)")
    check(os.path.exists(os.path.join(d, "big_ASSEMBLY.svg")),
          "an ASSEMBLY reference is written")


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


def scenario_fiducial_drop():
    print("scenario_fiducial_drop (tick poking out of a thin piece drops the pair):")
    # A = 10x0.2 thin strip, B = 10x2 above it, sharing the y=0.2 edge. The
    # default tick reaches 0.3cm inward -> pokes through A (0.2 thick) -> both
    # halves of that shared fiducial should be dropped.
    a = Profile([Loop([line_pc((0, 0), (10, 0)), line_pc((10, 0), (10, 0.2)),
                       line_pc((10, 0.2), (0, 0.2)), line_pc((0, 0.2), (0, 0))], True)])
    b = Profile([Loop([line_pc((0, 0.2), (10, 0.2)), line_pc((10, 0.2), (10, 2.2)),
                       line_pc((10, 2.2), (0, 2.2)), line_pc((0, 2.2), (0, 0.2))], True)])
    d = tempfile.mkdtemp()
    m.run_per_region_export(Sketch([a, b]), [a, b],
                            opts(d, "thin", unit="cm", bed_w=24, bed_h=24,
                                 bed_w_cm=24, bed_h_cm=24, fid=True))
    files = [f for f in os.listdir(d) if not f.endswith("ASSEMBLY.svg")]
    check(not any(has_fiducials(read(d, f)) for f in files),
          "shared fiducial pair dropped from BOTH the thin and thick piece")


def scenario_minsize():
    print("scenario_minsize (min tile size prevents grid slivers):")
    # 13x30 region, bed 12x24: doesn't fit -> tiles. Without min, the X axis
    # leaves a 1cm sliver column; with min=3 the last cut shifts to 3cm.
    piece = Profile([Loop(rect_pcs(0, 0, 13, 30), True)])

    d0 = tempfile.mkdtemp()
    m.run_per_region_export(Sketch([piece]), [piece],
                            opts(d0, "nomin", unit="cm", bed_w=12, bed_h=24,
                                 bed_w_cm=12, bed_h_cm=24, fid=False))
    w0 = [_dim(read(d0, f), "width") for f in os.listdir(d0)
          if not f.endswith("ASSEMBLY.svg")]
    check(min(w0) < 2, "without min-size a <2cm sliver appears (min width %.2f)"
          % min(w0))

    d1 = tempfile.mkdtemp()
    o1 = opts(d1, "wmin", unit="cm", bed_w=12, bed_h=24, bed_w_cm=12,
              bed_h_cm=24, fid=False)
    o1["tile_min_size_cm"] = 3.0
    m.run_per_region_export(Sketch([piece]), [piece], o1)
    w1 = [_dim(read(d1, f), "width") for f in os.listdir(d1)
          if not f.endswith("ASSEMBLY.svg")]
    check(min(w1) >= 3 - 1e-6,
          "with min-size 3, no tile narrower than 3cm (min width %.2f)" % min(w1))


def scenario_one_file():
    print("scenario_one_file (all pieces packed into one multi-color file):")
    ps = [Profile([Loop(rect_pcs(0, 0, 5, 4), True)]),
          Profile([Loop(rect_pcs(8, 0, 13, 4), True)]),
          Profile([Loop(rect_pcs(0, 6, 5, 10), True)]),
          Profile([Loop(rect_pcs(8, 6, 13, 10), True)])]
    d = tempfile.mkdtemp()
    m.run_one_file_export(Sketch(ps), ps,
                          opts(d, "onefile", unit="cm", bed_w=12, bed_h=24,
                               bed_w_cm=12, bed_h_cm=24, fid=False))
    files = sorted(os.listdir(d))
    check(files == ["onefile.svg", "onefile_ASSEMBLY.svg"],
          "combined file + assembly (got %s)" % files)
    out = read(d, "onefile.svg")
    fills = set(re.findall(r'fill="(#[0-9A-Fa-f]{6})"', out))
    check(len(fills) == 4, "4 distinct piece fill colors (got %d)" % len(fills))
    w = _dim(out, "width")
    check(w <= 12 + 1e-6, "Y-arrange: canvas width <= bedW 12cm (got %.2f)" % w)
    # Stacked (not piled): total height ~ sum of 4x4 heights + 3 gaps of 0.5.
    h = _dim(out, "height")
    check(abs(h - (4 * 4 + 3 * 0.5)) < 1e-6, "pieces stacked, not overlapping")


def scenario_one_file_multisheet():
    """The one-file export must never arrange past the material.

    Six 4x4 pieces cannot share a 10x10 bed, and every piece fits it on its
    own -- exactly the case the old single-cursor arrangement got wrong, since
    it stacked all six into one 25.5cm-tall file and merely *printed* that the
    result had to fit. The packer spills onto a second sheet instead.
    """
    print("scenario_one_file_multisheet (pieces that overflow the bed spill "
          "onto another sheet):")
    ps = [Profile([Loop(rect_pcs(x, y, x + 4, y + 4), True)])
          for x, y in ((0, 0), (6, 0), (12, 0), (0, 6), (6, 6), (12, 6))]
    d = tempfile.mkdtemp()
    summary = m.run_one_file_export(
        Sketch(ps), ps,
        opts(d, "multi", unit="cm", bed_w=10, bed_h=10, bed_w_cm=10,
             bed_h_cm=10, fid=False))
    files = sorted(os.listdir(d))
    check(files == ["multi-1.svg", "multi-2.svg", "multi_ASSEMBLY.svg"],
          "6 pieces on a 10x10 bed -> 2 numbered sheets + 1 assembly (got %s)"
          % files)
    sheets = [f for f in files if not f.endswith("_ASSEMBLY.svg")]
    over = [(f, _dim(read(d, f), "width"), _dim(read(d, f), "height"))
            for f in sheets
            if _dim(read(d, f), "width") > 10 + 1e-6
            or _dim(read(d, f), "height") > 10 + 1e-6]
    check(not over, "every sheet fits the 10x10 bed (over: %s)" % (over or "none"))
    fills = set()
    for f in sheets:
        fills |= set(re.findall(r'fill="(#[0-9A-Fa-f]{6})"', read(d, f)))
    check(len(fills) == 6,
          "all 6 pieces are present across the sheets, one color each (got %d)"
          % len(fills))
    check("2 file(s)" in summary, "summary reports the sheet count (got %r)"
          % summary.splitlines()[0])
    check("must fit your material area" not in summary,
          "the old advisory line is gone -- the bound is structural now")


def scenario_one_file_fiducials():
    print("scenario_one_file_fiducials (fiducials in piece color, letters red):")
    left = Profile([Loop([line_pc((0, 0), (5, 0)), line_pc((5, 0), (5, 4)),
                          line_pc((5, 4), (0, 4)), line_pc((0, 4), (0, 0))], True)])
    right = Profile([Loop([line_pc((5, 0), (10, 0)), line_pc((10, 0), (10, 4)),
                           line_pc((10, 4), (5, 4)), line_pc((5, 4), (5, 0))], True)])
    d = tempfile.mkdtemp()
    m.run_one_file_export(Sketch([left, right]), [left, right],
                          opts(d, "onefid", unit="cm", bed_w=24, bed_h=24,
                               bed_w_cm=24, bed_h_cm=24, fid=True, labels=True))
    out = read(d, "onefid.svg")
    fills = [c.upper() for c in re.findall(r'fill="(#[0-9A-Fa-f]{6})"', out)]
    check("#FF0000" not in fills, "no piece fill is red (red reserved)")
    check(has_fiducials(out), "fiducial ticks present (piece-color stroke group)")
    fid_strokes = [c.upper() for c in re.findall(r'stroke="(#[0-9A-Fa-f]{6})"', out)]
    check("#FF0000" not in fid_strokes, "no fiducial stroke is red")
    labelgrp = out.split('class="label"')[1]
    check("<text" in labelgrp, "letters live in the separate red label group")
    check("<path" not in labelgrp, "no cut paths in the label group")


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
    check(any(f == "laser_ASSEMBLY.dxf" for f in files), "DXF assembly emitted")
    sample = read(d, [f for f in files if f != "laser_ASSEMBLY.dxf"][0])
    check("POLYLINE" in sample and sample.rstrip().endswith("EOF"),
          "a tile DXF is well-formed (POLYLINE + EOF)")


def _layer_entity_count(text, layer):
    """Count DXF entities ASSIGNED to `layer` (group code 8).

    Deliberately not group code 2, which is the layer-table declaration. The
    bug this guards against left the declaration in place and the layer empty,
    so a check that looked for the layer's existence would have passed
    throughout.
    """
    lines = [x.strip() for x in text.splitlines()]
    return sum(1 for i in range(len(lines) - 1)
               if lines[i] == "8" and lines[i + 1] == layer)


def scenario_dxf_fiducial_layer():
    """Regression guard for the fiducial-routing bug fixed 2026-08-01.

    `_render_pieces_doc` used to concatenate each piece's fiducials into that
    piece's own `elements` list, so `core.dxf.render_groups` received nothing
    through its `fiducials=` parameter. The DXF declared a FIDUCIAL layer and
    left it empty -- so the documented workflow step "delete the non-cut layer
    before lasering" silently deleted nothing.

    Differential rather than a hard-coded total: the same export with fiducials
    OFF must yield zero FIDUCIAL entities and with them ON must yield some.
    A fixed expected count would break every time the tiling changes, and a
    test that needs updating on unrelated changes gets deleted.
    """
    print("scenario_dxf_fiducial_layer (ticks reach the FIDUCIAL layer):")

    def export(fid):
        piece = Profile([Loop(rect_pcs(0, 0, 30, 30), True)])
        d = tempfile.mkdtemp()
        m.run_per_region_export(
            Sketch([piece]), [piece],
            opts(d, "laser", unit="mm", bed_w=120, bed_h=240, bed_w_cm=12,
                 bed_h_cm=24, fid=fid, fmt="dxf"))
        return {f: read(d, f) for f in sorted(os.listdir(d))}

    off = export(False)
    on = export(True)
    n_off = sum(_layer_entity_count(t, "FIDUCIAL") for t in off.values())
    n_on = sum(_layer_entity_count(t, "FIDUCIAL") for t in on.values())

    check(n_off == 0,
          "fiducials OFF -> no entities on the FIDUCIAL layer (got %d)" % n_off)
    check(n_on > 0,
          "fiducials ON -> ticks actually land on the FIDUCIAL layer (got %d)" % n_on)
    # The ASSEMBLY file is excluded on purpose: `_write_assembly` builds its
    # groups with `with_fiducials=False` because the pieces are drawn filled
    # and ticks would vanish under the fills. It is a reference layout, not a
    # cut file. Asserting over it would fail for a correct reason.
    cut = {f: t for f, t in on.items() if not f.endswith("_ASSEMBLY.dxf")}
    empty = [f for f, t in cut.items() if _layer_entity_count(t, "FIDUCIAL") == 0]
    check(not empty,
          "every CUT dxf carries its own ticks (empty: %s)" % (empty or "none"))


def cut_outline_is_stroked(text):
    """True iff a piece's CUT OUTLINE (not just its ticks) is stroked.

    ``'fill="none" stroke="#..."'`` alone proves nothing: filled mode emits
    exactly that group for a piece's fiducial ticks. The discriminator is what
    is INSIDE the group -- ticks are open lines, whereas a cut outline is a
    closed path (``Z``). A mutation test caught the weaker form passing against
    the very bug it was written to detect.
    """
    for chunk in text.split('<g fill="none" stroke="#')[1:]:
        if "Z" in chunk.split("</g>")[0]:
            return True
    return False


def scenario_stroked():
    """opts["filled"]=False must reach the SVG, through the real export path.

    The unit test covers render_pieces itself; this covers the WIRING -- every
    call site used to hardcode filled=True, so the un-filled renderer was
    complete but unreachable. Drives both export modes so neither regresses.
    """
    print("scenario_stroked (opts filled=False -> stroked cut files):")
    left = Profile([Loop([line_pc((0, 0), (5, 0)), line_pc((5, 0), (5, 4)),
                          line_pc((5, 4), (0, 4)), line_pc((0, 4), (0, 0))], True)])
    right = Profile([Loop([line_pc((5, 0), (10, 0)), line_pc((10, 0), (10, 4)),
                           line_pc((10, 4), (5, 4)), line_pc((5, 4), (5, 0))], True)])

    d = tempfile.mkdtemp()
    m.run_per_region_export(Sketch([left, right]), [left, right],
                            opts(d, "stroked", unit="cm", bed_w=24, bed_h=24,
                                 bed_w_cm=24, bed_h_cm=24, filled=False))
    a = read(d, "stroked_A.svg")
    check(cut_outline_is_stroked(a),
          "per-region piece's cut outline is inside a stroked group")
    check('stroke="none"' not in a and 'fill-rule="evenodd"' not in a,
          "per-region piece carries no fill-mode markers")
    check('fill="#' not in a, "no piece color is painted as a fill")

    # The ASSEMBLY is a human-readable map, not a cut file: it stays filled
    # on purpose even when the cut files are stroked.
    assembly = read(d, "stroked_ASSEMBLY.svg")
    check('fill="#' in assembly and 'stroke="none"' in assembly,
          "ASSEMBLY stays filled (it is a reference, never cut)")

    # Same geometry with the default opts must still come out filled --
    # proves the scenario is reading the flag, not just describing the tool.
    d2 = tempfile.mkdtemp()
    m.run_per_region_export(Sketch([left, right]), [left, right],
                            opts(d2, "ctl", unit="cm", bed_w=24, bed_h=24,
                                 bed_w_cm=24, bed_h_cm=24))
    ctl = read(d2, "ctl_A.svg")
    check('fill="#' in ctl and 'stroke="none"' in ctl,
          "the default (filled) control case is unchanged")
    check(not cut_outline_is_stroked(ctl),
          "and the stroked-outline probe reads FALSE on it (the probe "
          "discriminates rather than always passing)")

    # One-file mode threads the same flag through a different call site.
    d3 = tempfile.mkdtemp()
    m.run_one_file_export(Sketch([left, right]), [left, right],
                          opts(d3, "onestroke", unit="cm", bed_w=24, bed_h=24,
                               bed_w_cm=24, bed_h_cm=24, filled=False))
    one = read(d3, "onestroke.svg")
    check(cut_outline_is_stroked(one),
          "one-file output's cut outline is stroked too")
    check('stroke="none"' not in one and 'fill-rule="evenodd"' not in one,
          "one-file output carries no fill-mode markers")


def main():
    scenario_split()
    scenario_hole()
    scenario_fit()
    scenario_tile()
    scenario_minsize()
    scenario_fiducial_drop()
    scenario_one_file()
    scenario_one_file_multisheet()
    scenario_one_file_fiducials()
    scenario_dxf()
    scenario_dxf_fiducial_layer()
    scenario_trim()
    scenario_stroked()
    print()
    if _fail:
        print("%d FAILURE(S)" % len(_fail))
        sys.exit(1)
    print("ALL INTEGRATION CHECKS PASSED")


if __name__ == "__main__":
    main()
