"""SketchToCut -- Fusion 360 add-in.

Exports the active (or selected) sketch's profile curves to 1:1-scale SVG or
DXF cut files -- paper masks on a vinyl cutter (to trace/cut plywood etc.), or
DXFs for a laser cutter / SendCutSend.

Design notes
------------
* All heavy lifting (units, Y-flip, SVG emission) lives in the dependency-free
  ``core`` package, which is unit-tested outside Fusion. This file only:
    1. builds the command UI,
    2. resolves the target sketch,
    3. extracts geometry via the Fusion API into the core IR (in centimetres),
    4. writes the SVG.
* Geometry extraction is deliberately robust over crisp: lines stay lines and
  full circles stay circles, but arcs / ellipses / splines / conics are
  flattened to polylines through the curve evaluator. That sidesteps arc-flag
  and rotation-under-flip edge cases entirely; a paper cutter flattens curves
  internally anyway.
* Fusion's API returns every length in centimetres regardless of document
  units, so the IR is populated in cm and the core scales once at emit time.
"""

import json
import math
import os
import traceback

import adsk.core
import adsk.fusion

from .core import geometry as geom
from .core import svg as svgwriter
from .core import dxf as dxfwriter
from .core import loops as looplib
from .core import fiducials as fidlib
from .core import fitting as fitlib
from .core import tiling as tilelib
from .core import palette as palettelib
from .core import packing as packlib


def _elem_polygon(elem):
    """A closed polygon (list of points) for an outer/hole element."""
    if isinstance(elem, geom.Polyline):
        return list(elem.points)
    return geom.sample_element_points(elem)  # circle/ellipse -> polygon


def _drop_unfitting_fiducials(pieces):
    """Drop fiducial ticks that poke out of their piece -- both halves of a pair.

    Ticks are grouped by their base point (``tick.p0`` -- the point on the shared
    cut, identical for the two adjacent pieces). A tick fits iff its tip
    (``tick.p1``) is inside its piece's outer polygon and not inside any hole.
    If any tick in a group fails, the whole group is dropped so a seam never ends
    up with a lone half-tick.
    """
    groups = {}  # base-point key -> list of (id(tick), fits)
    for p in pieces:
        outer = _elem_polygon(p["outer"])
        holes = [_elem_polygon(h) for h in p["holes"]]
        for t in p.get("fiducials", []):
            fits = (geom.point_in_polygon(t.p1, outer)
                    and not any(geom.point_in_polygon(t.p1, h) for h in holes))
            key = (round(t.p0[0], 4), round(t.p0[1], 4))
            groups.setdefault(key, []).append((id(t), fits))

    drop = set()
    for members in groups.values():
        if not all(fits for _tid, fits in members):
            drop.update(tid for tid, _fits in members)

    if drop:
        for p in pieces:
            if p.get("fiducials"):
                p["fiducials"] = [t for t in p["fiducials"] if id(t) not in drop]


def _tile_to_piece(tile, opts):
    """Turn a tiling.tile_piece() dict into an output-piece dict."""
    outer = geom.Polyline(tile["outer"], closed=True)
    holes = []
    for h, seams in zip(tile["holes"], tile["hole_seam_edges"]):
        for pts, closed in tilelib.hole_cut_paths(h, seams):
            holes.append(geom.Polyline(pts, closed=closed))
    centroid = geom.polygon_centroid(tile["outer"])
    fids = []
    if opts["fid_enabled"]:
        for a, b in tile["cut_edges"]:
            seg = [a, b] if a <= b else [b, a]  # canonical -> adjacent tiles match
            fids.extend(fidlib.edge_ticks(
                seg, centroid, length=opts["fid_len_cm"],
                spacing=opts["fid_spacing_cm"], inset=opts["fid_inset_cm"]))
        # Poster-overlap crop marks (only present when overlap > 0) render on the
        # fiducial layer/color like the ticks; both adjacent tiles carry the same
        # marks on the shared grid line, so lining them up registers the sheets.
        for a, b in tile.get("crop_marks", []):
            fids.append(geom.Line(a, b))
    return {"outer": outer, "holes": holes, "cloud": list(tile["outer"]),
            "centroid": centroid, "fiducials": fids, "_theta": 0.0}


def _piece_group(p, rotate=True, dx=0.0, dy=0.0, with_fiducials=True):
    """A render-ready group for a piece: geometry optionally rotated (to its fit
    orientation) and translated, tagged with its palette color / DXF layer."""
    theta = p["_theta"] if rotate else 0.0
    center = p["centroid"]

    def tf(e):
        if rotate:
            e = geom.rotate_element(e, theta, center)
        if dx or dy:
            e = geom.translate_element(e, dx, dy)
        return e

    fids = [tf(f) for f in p.get("fiducials", [])] if with_fiducials else []
    return {"outer": tf(p["outer"]), "holes": [tf(h) for h in p["holes"]],
            "fiducials": fids, "color": p["color"], "aci": p["aci"],
            "layer": "PIECE_%s" % p["letter"]}


def _render_pieces_doc(piece_groups, unit, fmt, stroke_width, filled=True,
                       labels=None):
    """Render piece groups to the chosen format; returns (text, extension).

    SVG defaults to filled shapes with per-piece color, fiducials stroked in
    that same color (so they cut together on a vinyl cutter that separates by
    color) -- fiducials stay embedded per-piece for that format. With
    ``filled=False`` (the dialog's "Fill shapes" unchecked) every outline is
    stroked in the piece color instead, which is what a cutter driven off
    contour lines rather than filled artwork expects.

    ``filled`` is SVG-only: DXF is wireframe by nature and ignores it.

    DXF stays wireframe (laser cuts paths, not fills) with each piece's cut
    geometry on its own layer/color, but fiducial ticks go on the shared
    FIDUCIAL layer (render_groups's ``fiducials=`` parameter) so a laser
    workflow can hide/delete that one layer independent of any piece layer,
    per the tool's documented layer contract.
    """
    if fmt == "dxf":
        groups = [{"elements": [g["outer"]] + g["holes"],
                   "layer": g["layer"], "color": g["aci"]}
                  for g in piece_groups]
        fiducials = [f for g in piece_groups for f in g["fiducials"]]
        return (dxfwriter.render_groups(groups, unit=unit, fiducials=fiducials,
                                        labels=labels), "dxf")
    return (svgwriter.render_pieces(piece_groups, unit=unit,
                                    stroke_width=stroke_width, filled=filled,
                                    labels=labels), "svg")

# --- constants -------------------------------------------------------------
CMD_ID = "SketchToCut_ExportCmd"
CMD_NAME = "Export Sketch to Cut Files"
CMD_TOOLTIP = ("Export the active sketch's profile curves to 1:1-scale SVG or "
               "DXF cut files, auto-rotated and tiled to fit the bed.")
PANEL_ID = "SolidScriptsAddinsPanel"

# Chord tolerance for flattening curves, in centimetres (0.0025 cm = 0.025 mm).
FLATTEN_TOL_CM = 0.0025

# Rotation search step for auto-fit (degrees).
FIT_STEP_DEG = 1.0

# Keep event handlers referenced or Python garbage-collects them and events
# silently stop firing -- the classic Fusion add-in trap.
_handlers = []
_app = None
_ui = None


# --- settings persistence (remember dialog values between runs) -------------
def _settings_path():
    return os.path.join(os.path.dirname(__file__), "settings.json")


def _load_settings():
    try:
        with open(_settings_path(), "r", encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return {}


def _save_settings(data):
    try:
        with open(_settings_path(), "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2)
    except Exception:
        pass  # persistence is best-effort; never break an export over it


def _all_inputs(inputs):
    """Flatten a CommandInputs collection, descending into groups/tabs."""
    result = []
    for i in range(inputs.count):
        inp = inputs.item(i)
        result.append(inp)
        try:
            if inp.objectType in (adsk.core.GroupCommandInput.classType(),
                                  adsk.core.TabCommandInput.classType()):
                result.extend(_all_inputs(inp.children))
        except Exception:
            pass
    return result


def _input_value(inp):
    """JSON-serialisable value for a persistable input, else None to skip it."""
    try:
        ot = inp.objectType
        if ot == adsk.core.DropDownCommandInput.classType():
            return inp.selectedItem.index
        if ot == adsk.core.BoolValueCommandInput.classType():
            return bool(inp.value)
        if ot == adsk.core.FloatSpinnerCommandInput.classType():
            return float(inp.value)
        if ot == adsk.core.StringValueCommandInput.classType():
            return inp.value
    except Exception:
        pass
    return None


def _set_input_value(inp, val):
    try:
        ot = inp.objectType
        if ot == adsk.core.DropDownCommandInput.classType():
            n = inp.listItems.count
            if isinstance(val, int) and 0 <= val < n:
                inp.listItems.item(val).isSelected = True
        elif ot == adsk.core.BoolValueCommandInput.classType():
            inp.value = bool(val)
        elif ot == adsk.core.FloatSpinnerCommandInput.classType():
            inp.value = float(val)
        elif ot == adsk.core.StringValueCommandInput.classType():
            inp.value = str(val)
    except Exception:
        pass  # ignore stale/out-of-range persisted values


def _capture_settings(inputs):
    out = {}
    for inp in _all_inputs(inputs):
        val = _input_value(inp)
        if val is not None:
            out[inp.id] = val
    return out


def _apply_settings(inputs):
    data = _load_settings()
    if not data:
        return
    for inp in _all_inputs(inputs):
        if inp.id in data:
            _set_input_value(inp, data[inp.id])


# --- geometry extraction ---------------------------------------------------
def _resolve_sketch():
    """Return the sketch to export: active edit sketch, else selected, else None."""
    active = _app.activeEditObject
    if active and active.objectType == adsk.fusion.Sketch.classType():
        return adsk.fusion.Sketch.cast(active)

    sels = _ui.activeSelections
    for i in range(sels.count):
        ent = sels.item(i).entity
        if ent and ent.objectType == adsk.fusion.Sketch.classType():
            return adsk.fusion.Sketch.cast(ent)
    return None


def _make_projector(sketch):
    """Return f(worldPoint3D) -> (x_cm, y_cm) in the sketch's own plane.

    ``sketch.transform`` maps sketch space -> world; we invert it to bring
    world geometry back onto the flat sketch plane (z ~ 0), then drop z.
    """
    inv = sketch.transform.copy()
    inv.invert()

    def project(pt):
        p = adsk.core.Point3D.create(pt.x, pt.y, pt.z)
        p.transformBy(inv)
        return (p.x, p.y)

    return project


def _flatten_curve(curve3d, project):
    """Flatten any Curve3D to a list of projected 2D points via its evaluator."""
    ev = curve3d.evaluator
    ok, tmin, tmax = ev.getParameterExtents()
    if not ok:
        return []
    ok, pts = ev.getStrokes(tmin, tmax, FLATTEN_TOL_CM)
    if not ok or not pts:
        return []
    return [project(p) for p in pts]


def _skip(curve, include_construction):
    """True if this curve should be excluded from the export."""
    if getattr(curve, "isReference", False):
        return True  # projected / reference geometry
    if getattr(curve, "isConstruction", False) and not include_construction:
        return True
    return False


def extract_elements(sketch, include_construction):
    """Build a list of core geometry-IR elements (cm-space) from a sketch."""
    project = _make_projector(sketch)
    curves = sketch.sketchCurves
    elements = []

    for line in curves.sketchLines:
        if _skip(line, include_construction):
            continue
        wg = line.worldGeometry  # Line3D in world coordinates
        elements.append(geom.Line(project(wg.startPoint), project(wg.endPoint)))

    for circle in curves.sketchCircles:
        if _skip(circle, include_construction):
            continue
        wg = circle.worldGeometry  # Circle3D
        elements.append(geom.Circle(project(wg.center), circle.radius))

    # Everything else -> flattened polyline via the evaluator.
    other_collections = (
        curves.sketchArcs,
        curves.sketchEllipses,
        curves.sketchFittedSplines,
        curves.sketchFixedSplines,
        curves.sketchControlPointSplines,
        curves.sketchConicCurves,
    )
    for collection in other_collections:
        for curve in collection:
            if _skip(curve, include_construction):
                continue
            pts = _flatten_curve(curve.worldGeometry, project)
            if len(pts) >= 2:
                elements.append(geom.Polyline(pts, closed=False))

    return elements


# --- per-region (profile) export -------------------------------------------
def _iter_real_sketch_curves(sketch):
    """Yield non-construction, non-reference sketch curves across all kinds."""
    c = sketch.sketchCurves
    for coll in (c.sketchLines, c.sketchArcs, c.sketchCircles, c.sketchEllipses,
                 c.sketchFittedSplines, c.sketchFixedSplines,
                 c.sketchControlPointSplines, c.sketchConicCurves):
        for i in range(coll.count):
            cur = coll.item(i)
            if getattr(cur, "isConstruction", False):
                continue
            if getattr(cur, "isReference", False):
                continue
            yield cur


def _g3_points(g3):
    """2D points for a Curve3D geometry (sketch space): endpoints for a line,
    else flattened via the evaluator.

    Profile geometry (``ProfileCurve.geometry`` and ``SketchCurve.geometry``)
    is already in the sketch's own 2D coordinate system, so we take (x, y)
    directly -- no world->sketch projection. Crucially, ``ProfileCurve.geometry``
    is *trimmed* to the portion that bounds the profile, so a region bordered by
    only part of a long shared curve gets just that part (not the whole curve).
    """
    if g3.objectType == adsk.core.Line3D.classType():
        return [(g3.startPoint.x, g3.startPoint.y),
                (g3.endPoint.x, g3.endPoint.y)]
    ev = g3.evaluator
    ok, tmin, tmax = ev.getParameterExtents()
    if not ok:
        return []
    ok, pts = ev.getStrokes(tmin, tmax, FLATTEN_TOL_CM)
    if not ok or not pts:
        return []
    return [(p.x, p.y) for p in pts]


def _arclen_midpoint(points):
    """Geometric mid-of-curve by arc length -- independent of traversal direction."""
    if len(points) == 2:
        return ((points[0][0] + points[1][0]) / 2.0,
                (points[0][1] + points[1][1]) / 2.0)
    seg = [math.hypot(b[0] - a[0], b[1] - a[1])
           for a, b in zip(points, points[1:])]
    half = sum(seg) / 2.0
    acc = 0.0
    for i, d in enumerate(seg):
        if d > 0 and acc + d >= half:
            t = (half - acc) / d
            a, b = points[i], points[i + 1]
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        acc += d
    return points[len(points) // 2]


def _curve_key(points):
    """Geometry-based identity for a curve, independent of traversal direction.

    Same sketch edge (bordering two regions, possibly traced opposite ways)
    yields the same key: sorted endpoints + the arc-length midpoint (the
    midpoint distinguishes a line from an arc with the same endpoints). Avoids
    entityToken (Fusion hands out a fresh wrapper per access, so object
    identity is unsafe).
    """
    def r(p):
        return (round(p[0], 6), round(p[1], 6))
    a, b = r(points[0]), r(points[-1])
    m = r(_arclen_midpoint(points))
    return tuple(sorted((a, b))) + (m,)


def _letter(i):
    """0->A, 1->B, ... 25->Z, 26->AA, 27->AB (spreadsheet style)."""
    s = ""
    i += 1
    while i > 0:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


def _label_height_cm(elem):
    """Auto label height (cm): ~1/4 of the piece's smaller dimension, clamped."""
    minx, miny, maxx, maxy = elem.extents()
    return max(0.3, min(min(maxx - minx, maxy - miny) * 0.25, 3.0))


def _build_loop(loop):
    """Return (element, keyset, cloud, curves, keys) for one profile loop.

    Uses each ``ProfileCurve.geometry`` -- the trimmed, sketch-space curve that
    bounds this profile -- so partial edges of long shared curves come in only
    to the extent they border this region.
    """
    pcs = loop.profileCurves
    curves = []
    keys = []
    circle_geo = None
    for j in range(pcs.count):
        g3 = pcs.item(j).geometry
        pts = _g3_points(g3)
        if len(pts) < 2:
            continue
        curves.append(pts)
        keys.append(_curve_key(pts))
        if pcs.count == 1 and g3.objectType == adsk.core.Circle3D.classType():
            circle_geo = g3
    if circle_geo is not None:
        c = circle_geo.center
        elem = geom.Circle((c.x, c.y), circle_geo.radius)
        cloud = geom.sample_element_points(elem)
    else:
        ring = looplib.chain_loop(curves)
        elem = geom.Polyline(ring, closed=True)
        cloud = list(ring)
    return elem, frozenset(keys), cloud, curves, keys


def _all_profile_entity_keys(sketch):
    """Full-entity curve keys used by ANY profile (for open-curve detection).

    Uses the *untrimmed* ``sketchEntity.geometry`` so keys line up with the
    sketch curves compared against in the open-curve scan.
    """
    keys = set()
    profs = sketch.profiles
    for i in range(profs.count):
        lps = profs.item(i).profileLoops
        for li in range(lps.count):
            pcs = lps.item(li).profileCurves
            for j in range(pcs.count):
                pts = _g3_points(pcs.item(j).sketchEntity.geometry)
                if len(pts) >= 2:
                    keys.add(_curve_key(pts))
    return keys


def extract_regions(sketch, target_profiles):
    """Build piece dicts from profiles, suppressing hole-filler regions.

    Returns (survivors, key_to_pieces, key_points, open_count). Each survivor
    is a dict with original-orientation geometry (cm): outer element, holes,
    centroid, letter, cloud (point sample for fitting), and (later) fiducials.
    """
    raw = []
    key_points = {}
    for profile in target_profiles:
        try:
            outer_elem = None
            outer_keys = frozenset()
            holes = []
            inner_keysets = []
            cloud = []
            lps = profile.profileLoops
            for li in range(lps.count):
                loop = lps.item(li)
                elem, keyset, lcloud, curves, keys = _build_loop(loop)
                for k, pts in zip(keys, curves):
                    key_points[k] = pts
                cloud.extend(lcloud)
                if loop.isOuter:
                    outer_elem, outer_keys = elem, keyset
                else:
                    holes.append(elem)
                    inner_keysets.append(keyset)
            if outer_elem is None:
                continue
            raw.append({
                "outer": outer_elem, "holes": holes, "outer_keys": outer_keys,
                "inner_keysets": inner_keysets, "cloud": cloud,
            })
        except Exception:
            continue  # a loop that won't chain -> skip this profile, keep going

    # Suppress void-fillers: a profile whose outer loop equals another
    # profile's inner (hole) loop is the disc inside a hole -- not a piece.
    inner_all = [(idx, ks) for idx, p in enumerate(raw)
                 for ks in p["inner_keysets"]]
    survivors = []
    for idx, p in enumerate(raw):
        if any(oidx != idx and ks == p["outer_keys"] for oidx, ks in inner_all):
            continue
        survivors.append(p)

    for p in survivors:
        p["centroid"] = geom.polygon_centroid(geom.sample_element_points(p["outer"]))
        allk = set(p["outer_keys"])
        for ks in p["inner_keysets"]:
            allk |= ks
        p["all_keys"] = allk
    # Sort top->bottom (larger y first), then left->right; assign A, B, C...
    survivors.sort(key=lambda p: (-p["centroid"][1], p["centroid"][0]))
    for i, p in enumerate(survivors):
        p["letter"] = _letter(i)

    key_to_pieces = {}
    for i, p in enumerate(survivors):
        for k in p["all_keys"]:
            key_to_pieces.setdefault(k, []).append(i)

    all_keys = _all_profile_entity_keys(sketch)
    open_count = 0
    for sc in _iter_real_sketch_curves(sketch):
        pts = _g3_points(sc.geometry)
        if len(pts) >= 2 and _curve_key(pts) not in all_keys:
            open_count += 1

    return survivors, key_to_pieces, key_points, open_count


def _build_final_pieces(sketch, target_profiles, opts):
    """Shared pipeline: regions -> fiducials -> fit/tile -> letter -> drop unfitting.

    Returns ``(final_pieces, info)`` where ``info`` has ``n_tiled``, ``untileable``,
    ``open_count``, and ``error`` (a user message if nothing to export, else None).
    Each final piece dict carries ``outer``/``holes``/``fiducials``/``centroid``/
    ``letter``/``_theta`` (the fit rotation, all in original cm coords).
    """
    survivors, key_to_pieces, key_points, open_count = extract_regions(
        sketch, target_profiles)
    info = {"n_tiled": 0, "untileable": [], "open_count": open_count, "error": None}
    if not survivors:
        info["error"] = ("No closed regions found to export.\n\nDraw closed "
                         "profiles (and slice them with lines), then run again.")
        return [], info

    wc, hc = opts["bed_w_cm"], opts["bed_h_cm"]
    tile_rot = opts.get("tile_rotation_deg", 0.0)
    tile_min = opts.get("tile_min_size_cm", 0.0)
    tile_overlap = opts.get("tile_overlap_cm", 0.0)   # 0 -> butt-joint (default)

    # Precompute fit/tile status per survivor BEFORE the fiducial pass below,
    # so that pass can tell whether a shared edge's other side is going to be
    # tiled under nonzero overlap. Reused in the tiling loop further down so
    # fit_rotation only runs once per survivor.
    theta_by_survivor = [
        fitlib.fit_rotation(p["cloud"], wc, hc, step_deg=FIT_STEP_DEG)
        for p in survivors]

    if opts["fid_enabled"]:
        for k, idxs in key_to_pieces.items():
            if len(idxs) < 2:
                continue  # only shared (cut) edges get fiducials
            # If nonzero overlap means one side of this edge will be tiled,
            # that tile does NOT get a matching half placed on it (see the
            # gate in the tiling loop below -- crop marks register the sheet
            # there instead). Placing this side's half anyway would orphan
            # it: a lone tick is a group of one and _drop_unfitting_fiducials
            # will not remove it. So skip BOTH halves together here, the same
            # way the tiled side already skips its own. Fixes the 5-vs-0
            # orphan measured on a 20x20 tiled region beside a 4x20 neighbour.
            if tile_overlap != 0.0 and any(
                    theta_by_survivor[i] is None for i in idxs):
                continue
            pts = key_points.get(k)
            if not pts:
                continue
            for i in idxs:
                survivors[i].setdefault("fiducials", []).extend(
                    fidlib.edge_ticks(pts, survivors[i]["centroid"],
                                      length=opts["fid_len_cm"],
                                      spacing=opts["fid_spacing_cm"],
                                      inset=opts["fid_inset_cm"]))

    final, untileable, n_tiled = [], [], 0
    for idx, p in enumerate(survivors):
        theta = theta_by_survivor[idx]
        if theta is not None:
            p["_theta"] = theta
            final.append(p)
            continue
        tiles = tilelib.tile_piece(
            _elem_polygon(p["outer"]), [_elem_polygon(h) for h in p["holes"]],
            wc, hc, tile_rot, tile_min, overlap=tile_overlap)
        pieces = [q for q in (_tile_to_piece(t, opts) for t in tiles) if q]
        # Item 3: where this tiled region's OUTER boundary is shared with an
        # adjacent region, drop matched ticks on the tiles that abut it. The
        # neighbour's half was already placed from the same edge polyline
        # (key_points[k]) in the fiducial pass above, so the base points coincide
        # and pair up in _drop_unfitting_fiducials. (Skipped under overlap: there
        # the crop marks register the sheets and there is no butt seam.)
        #
        # FIXED (previously a KNOWN GAP): the neighbour's half of that seam used
        # to be placed unconditionally by the fiducial pass above, which did not
        # check overlap -- so with overlap > 0 a NON-tiled region abutting this
        # one kept its ticks while these tiles got none, and
        # _drop_unfitting_fiducials would not remove the orphan (it only drops a
        # group when a member's tip pokes out, and a lone tick is a group of one
        # that fits). Measured 5 ticks vs 0 on a 20x20 tiled region beside a
        # 4x20 neighbour. The pass above is now gated by the SAME
        # theta_by_survivor / tile_overlap check as this one, so neither half is
        # placed when overlap > 0 -- symmetric with this gate. Which side should
        # instead grow its own crop marks is still a separate design call, left
        # to Evan; this only removes the orphan.
        if opts["fid_enabled"] and tile_overlap == 0.0:
            for k in p["outer_keys"]:
                if len(key_to_pieces.get(k, ())) < 2:
                    continue                      # not shared with another region
                epts = key_points.get(k)
                if not epts:
                    continue
                for q in pieces:
                    q["fiducials"].extend(fidlib.shared_edge_ticks(
                        epts, list(q["outer"].points), q["centroid"],
                        length=opts["fid_len_cm"], spacing=opts["fid_spacing_cm"],
                        inset=opts["fid_inset_cm"]))
        if pieces:
            n_tiled += 1
            final.extend(pieces)
        else:
            untileable.append(p)

    if not final:
        info["error"] = ("Nothing to export -- the region(s) could not be fit "
                         "or tiled to the %g x %g %s bed."
                         % (opts["bed_w"], opts["bed_h"], opts["unit"]))
        return [], info

    final.sort(key=lambda q: (-q["centroid"][1], q["centroid"][0]))
    colors = palettelib.distinct_colors(len(final))
    for i, q in enumerate(final):
        q["letter"] = _letter(i)
        q["color"] = colors[i]           # same color across piece file + assembly
        q["aci"] = palettelib.aci_color(i)
    _drop_unfitting_fiducials(final)

    info["n_tiled"] = n_tiled
    info["untileable"] = untileable
    return final, info


def _tile_note(info, opts):
    """Trailing summary lines shared by both export modes."""
    lines = []
    if info["n_tiled"]:
        lines.append("\n%d oversized region(s) were auto-tiled to the "
                     "%g x %g %s bed." % (info["n_tiled"], opts["bed_w"],
                                          opts["bed_h"], opts["unit"]))
    if info["untileable"]:
        lines.append("\n%d region(s) could not be tiled (degenerate geometry)."
                     % len(info["untileable"]))
    if info["open_count"]:
        lines.append("\n%d open curve(s) not part of any region were skipped."
                     % info["open_count"])
    return lines


def _write_assembly(final, opts, unit, sw, fmt, folder, base):
    """Write {base}_ASSEMBLY: filled pieces in ORIGINAL position + letters.

    Uses each piece's palette color (matching the cut files) so you can map a
    color back to where it belongs. No fiducials (they'd vanish on the fills).

    Stays filled even when the cut files are stroked (``opts["filled"]`` is
    False): this is a human-readable map, never fed to the machine, and solid
    color blocks are what make the piece-to-position mapping readable at a
    glance. Only the cut files follow the dialog's fill setting.
    """
    apgs = [_piece_group(p, rotate=False, with_fiducials=False) for p in final]
    labels = [(p["letter"], p["centroid"], _label_height_cm(p["outer"]))
              for p in final]
    doc, ext = _render_pieces_doc(apgs, unit, fmt, sw, filled=True, labels=labels)
    with open(os.path.join(folder, "%s_ASSEMBLY.%s" % (base, ext)),
              "w", encoding="utf-8") as fp:
        fp.write(doc)


def run_per_region_export(sketch, target_profiles, opts):
    """One file per piece + an ASSEMBLY reference. Returns a summary."""
    final, info = _build_final_pieces(sketch, target_profiles, opts)
    if info["error"]:
        return info["error"]

    unit, sw = opts["unit"], opts["stroke_width"]
    fmt = opts.get("fmt", "svg")
    filled = opts.get("filled", True)
    folder, base = opts["folder"], opts["base"]
    single = len(final) == 1

    written, ext = [], "svg"
    for p in final:
        pg = _piece_group(p, rotate=True)
        lbls = ([(p["letter"], p["centroid"], _label_height_cm(p["outer"]))]
                if opts["label_on_pieces"] and not single else None)
        doc, ext = _render_pieces_doc([pg], unit, fmt, sw, filled=filled,
                                      labels=lbls)
        fname = "%s.%s" % (base, ext) if single \
            else "%s_%s.%s" % (base, p["letter"], ext)
        with open(os.path.join(folder, fname), "w", encoding="utf-8") as fp:
            fp.write(doc)
        written.append(p["letter"])

    if single:
        lines = ["Exported 1 piece to:\n%s"
                 % os.path.join(folder, "%s.%s" % (base, ext))]
    else:
        _write_assembly(final, opts, unit, sw, fmt, folder, base)
        lines = ["Exported %d piece file(s) + %s_ASSEMBLY.%s to:\n%s\n"
                 % (len(written), base, ext, folder)]
        lines.append("Pieces: %s" % ", ".join(written))
    return "\n".join(lines + _tile_note(info, opts))


def _sheet_name(base, ext, index, count):
    """Output filename for sheet ``index`` of ``count``.

    One sheet keeps the plain ``base.ext`` the tool has always written; two or
    more get ``base-1.ext``, ``base-2.ext``, ... so the common case is
    unchanged and a multi-sheet job is obvious from the file listing.
    """
    if count <= 1:
        return "%s.%s" % (base, ext)
    return "%s-%d.%s" % (base, index + 1, ext)


def run_one_file_export(sketch, target_profiles, opts):
    """Pack all pieces into ONE multi-color file per sheet (+ ASSEMBLY).

    The pieces are packed against the user's bed size by :mod:`core.packing`,
    which opens another sheet rather than ever arranging past the material --
    if the individual pieces fit and the packed result does not, that is a
    packing failure, not something to warn about after the fact. Returns a
    summary.
    """
    final, info = _build_final_pieces(sketch, target_profiles, opts)
    if info["error"]:
        return info["error"]

    unit, sw = opts["unit"], opts["stroke_width"]
    fmt = opts.get("fmt", "svg")
    filled = opts.get("filled", True)
    axis = opts.get("arrange_axis", "Y")
    folder, base = opts["folder"], opts["base"]
    gap = 0.5  # cm between packed pieces

    # Each piece is rotated to the fit orientation chosen upstream; the packer
    # places those already-final bounding boxes and never re-rotates them.
    boxes = []
    for p in final:
        theta, center = p["_theta"], p["centroid"]
        outer_r = geom.rotate_element(p["outer"], theta, center)
        holes_r = [geom.rotate_element(h, theta, center) for h in p["holes"]]
        boxes.append(geom.bounding_box([outer_r] + holes_r))

    bins = packlib.pack(boxes, opts["bed_w_cm"], opts["bed_h_cm"], gap, axis)

    ext = "dxf" if fmt == "dxf" else "svg"
    written = []
    for bi, b in enumerate(bins):
        piece_groups, labels = [], []
        for pl in b.placements:
            p = final[pl.index]
            piece_groups.append(_piece_group(p, rotate=True, dx=pl.dx, dy=pl.dy))
            if opts["label_on_pieces"]:
                center = p["centroid"]
                labels.append((p["letter"],
                               (center[0] + pl.dx, center[1] + pl.dy),
                               _label_height_cm(p["outer"])))
        doc, ext = _render_pieces_doc(piece_groups, unit, fmt, sw, filled=filled,
                                      labels=labels or None)
        fname = _sheet_name(base, ext, bi, len(bins))
        with open(os.path.join(folder, fname), "w", encoding="utf-8") as fp:
            fp.write(doc)
        written.append(fname)

    if len(final) > 1:
        _write_assembly(final, opts, unit, sw, fmt, folder, base)

    lines = ["Exported %d piece(s) into %d file(s) (packed to the %g x %g %s "
             "bed, shelves along %s)%s:\n%s\n"
             % (len(final), len(written), opts["bed_w"], opts["bed_h"],
                opts["unit"], axis,
                (" + %s_ASSEMBLY.%s" % (base, ext)) if len(final) > 1 else "",
                folder)]
    lines.append("Files: %s" % ", ".join(written))
    return "\n".join(lines + _tile_note(info, opts))


# --- command handlers ------------------------------------------------------
class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            unit = "in" if inputs.itemById("units").selectedItem.index == 0 else "mm"
            stroke_width = inputs.itemById("strokeWidth").value
            fmt = "dxf" if inputs.itemById("outputFormat").selectedItem.index == 1 \
                else "svg"
            one_file = inputs.itemById("exportMode").selectedItem.index == 0
            self._export(inputs, unit, stroke_width, fmt, one_file)
            _save_settings(_capture_settings(inputs))
        except:  # noqa: E722 -- surface any failure to the user
            if _ui:
                _ui.messageBox("Export failed:\n{}".format(traceback.format_exc()))

    def _export(self, inputs, unit, stroke_width, fmt, one_file):
        # Targets: profiles picked in the Regions input, else all of the sketch.
        profs = []
        sel_in = inputs.itemById("regions")
        if sel_in:
            for i in range(sel_in.selectionCount):
                ent = sel_in.selection(i).entity
                if ent and ent.objectType == adsk.fusion.Profile.classType():
                    profs.append(adsk.fusion.Profile.cast(ent))
        if profs:
            sketch = profs[0].parentSketch
            targets = profs
        else:
            sketch = _resolve_sketch()
            if not sketch:
                _ui.messageBox(
                    "No sketch or region found.\n\nOpen a sketch for edit, or "
                    "select one/more regions, then run again.")
                return
            targets = [sketch.profiles.item(i)
                       for i in range(sketch.profiles.count)]
            if not targets:
                _ui.messageBox("That sketch has no closed regions (profiles). "
                               "Add closed geometry, then run again.")
                return

        s = geom.cm_to(unit)
        ext = "dxf" if fmt == "dxf" else "svg"

        dlg = _ui.createFileDialog()
        dlg.title = ("Save one-file output" if one_file
                     else "Choose output folder + base name (per-piece files added)")
        dlg.filter = "%s files (*.%s)" % (ext.upper(), ext)
        safe = "".join(c for c in sketch.name
                       if c.isalnum() or c in " _-").strip() or "sketch"
        dlg.initialFilename = "%s.%s" % (safe, ext)
        if dlg.showSave() != adsk.core.DialogResults.DialogOK:
            return
        chosen = dlg.filename
        folder = os.path.dirname(chosen)
        base = os.path.splitext(os.path.basename(chosen))[0] or safe

        opts = {
            "unit": unit, "stroke_width": stroke_width, "fmt": fmt,
            "bed_w": inputs.itemById("bedW").value,
            "bed_h": inputs.itemById("bedH").value,
            "bed_w_cm": inputs.itemById("bedW").value / s,
            "bed_h_cm": inputs.itemById("bedH").value / s,
            "fid_enabled": inputs.itemById("fidEnabled").value,
            "fid_len_cm": inputs.itemById("fidLen").value / 10.0,   # mm -> cm
            "fid_spacing_cm": inputs.itemById("fidSpacing").value / 10.0,
            "fid_inset_cm": 0.3,
            "fid_color": "red",
            "filled": inputs.itemById("fillShapes").value,
            "label_on_pieces": inputs.itemById("labelPieces").value,
            "tile_rotation_deg": inputs.itemById("tileRotation").value,
            "tile_min_size_cm": inputs.itemById("tileMinSize").value / s,
            # `/ s` converts output units -> cm, matching the *_cm key name.
            # Dropping it would read 0.5 in as 0.5 cm: a silent 2.54x error.
            "tile_overlap_cm": inputs.itemById("tileOverlap").value / s,
            "arrange_axis": ("X" if inputs.itemById("arrangeAxis").selectedItem.index == 1
                             else "Y"),
            "folder": folder, "base": base,
        }
        if one_file:
            _ui.messageBox(run_one_file_export(sketch, targets, opts))
        else:
            _ui.messageBox(run_per_region_export(sketch, targets, opts))


class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            text_list = adsk.core.DropDownStyles.TextListDropDownStyle

            mode = inputs.addDropDownCommandInput("exportMode", "Export mode",
                                                  text_list)
            mode.listItems.add("One file (all pieces, multi-color)", True)  # idx 0
            mode.listItems.add("One file per region", False)               # idx 1

            # Explicit region picker (per-region mode). Captured here in the
            # command so it survives the dialog opening -- Fusion clears the
            # canvas pre-selection when a command starts, so reading
            # activeSelections at execute time is unreliable. Seed it from
            # whatever the user had selected when they launched the command.
            regions = inputs.addSelectionInput(
                "regions", "Regions",
                "Pick closed regions to export (leave empty = all regions)")
            regions.addSelectionFilter("Profiles")
            regions.setSelectionLimits(0, 0)  # 0 min, 0 max => unlimited
            try:
                sels = _ui.activeSelections
                for i in range(sels.count):
                    ent = sels.item(i).entity
                    if ent and ent.objectType == adsk.fusion.Profile.classType():
                        regions.addSelection(ent)
            except:  # noqa: E722 -- seeding is best-effort
                pass

            fmt = inputs.addDropDownCommandInput("outputFormat", "Output format",
                                                 text_list)
            fmt.listItems.add("SVG (vinyl cutter)", True)   # index 0, default
            fmt.listItems.add("DXF (laser / SendCutSend)", False)  # index 1

            units = inputs.addDropDownCommandInput("units", "Output units",
                                                   text_list)
            units.listItems.add("Inch", True)      # index 0, default
            units.listItems.add("Millimeter", False)

            # Shelf orientation for the one-file packer: pieces run along the
            # chosen axis within a shelf, and shelves advance across the other
            # one. (Item indices are persisted in settings.json -- keep them.)
            arrange = inputs.addDropDownCommandInput(
                "arrangeAxis", "Shelf direction (one-file mode)", text_list)
            arrange.listItems.add(
                "Y (stack down a column, wrap to the next column)", True)  # 0
            arrange.listItems.add(
                "X (stack across a row, wrap to the next row)", False)     # 1

            inputs.addFloatSpinnerCommandInput(
                "strokeWidth", "Stroke width (output units)",
                "", 0.0, 10.0, 0.005, 0.01)

            # Filled vs stroked cut files (SVG only -- DXF is always wireframe).
            # Checked keeps the long-standing filled output; unchecked emits
            # stroked outlines, which is what a vinyl cutter's contour-cut
            # wants. Deliberately positive-sense so the value maps straight to
            # render_pieces(filled=...) with no negation anywhere in between.
            inputs.addBoolValueInput(
                "fillShapes",
                "Fill shapes -- SVG only (uncheck for stroked outlines, "
                "e.g. vinyl cutter)", True, "", True)

            grp = inputs.addGroupCommandInput("perRegion", "Bed / fiducials / tiling")
            grp.isExpanded = True
            gi = grp.children
            gi.addFloatSpinnerCommandInput(
                "bedW", "Max bed width (output units)", "", 0.1, 100000.0, 0.5, 12.0)
            gi.addFloatSpinnerCommandInput(
                "bedH", "Max bed height (output units)", "", 0.1, 100000.0, 0.5, 24.0)
            gi.addBoolValueInput("fidEnabled", "Alignment fiducials", True, "", True)
            gi.addFloatSpinnerCommandInput(
                "fidLen", "Fiducial length (mm)", "", 0.5, 50.0, 0.5, 6.0)
            gi.addFloatSpinnerCommandInput(
                "fidSpacing", "Fiducial spacing (mm)", "", 1.0, 1000.0, 1.0, 50.0)
            gi.addBoolValueInput("labelPieces", "Label pieces on cut files",
                                 True, "", False)
            gi.addFloatSpinnerCommandInput(
                "tileRotation", "Tile grid rotation (deg)", "", 0.0, 90.0, 1.0, 0.0)
            gi.addFloatSpinnerCommandInput(
                "tileMinSize", "Min tile size (output units, 0=off)",
                "", 0.0, 10000.0, 0.5, 0.0)
            # Poster overlap between tiles. Output units like the bed/min-tile
            # spinners beside it (NOT mm like the fiducial ones) -- the opts key
            # is *_cm and the conversion happens there, so the label and the
            # divisor have to agree. Defaults to 0 = butt joint: that is what
            # every run has produced until now, and a nonzero default would
            # silently grow tiles and switch off the matched seam ticks.
            gi.addFloatSpinnerCommandInput(
                "tileOverlap", "Tile overlap (output units, 0=butt joint)",
                "", 0.0, 10000.0, 0.25, 0.0)

            inputs.addTextBoxCommandInput(
                "hint", "",
                "Both modes: each closed region (selected, else all) is "
                "auto-rotated to fit the bed; too-big regions are tiled. "
                "One file = all pieces packed into one multi-color file per "
                "sheet, in shelves along the chosen axis; anything that will "
                "not fit the bed spills onto another sheet (base-1, base-2, "
                "...) instead of running off the material. "
                "Per region = one file per piece. Both also emit an ASSEMBLY "
                "reference (filled, colored, letters) showing where each fits. "
                "SVG for the vinyl cutter, DXF for laser/SendCutSend.", 5, True)

            # Restore last-used values (overrides the hard-coded defaults above).
            _apply_settings(inputs)

            on_exec = ExecuteHandler()
            args.command.execute.add(on_exec)
            _handlers.append(on_exec)
        except:  # noqa: E722
            if _ui:
                _ui.messageBox("Command setup failed:\n{}".format(
                    traceback.format_exc()))


# --- add-in lifecycle ------------------------------------------------------
def run(context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface

        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = _ui.commandDefinitions.addButtonDefinition(
                CMD_ID, CMD_NAME, CMD_TOOLTIP)

        on_created = CreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)
    except:  # noqa: E722
        if _ui:
            _ui.messageBox("Failed to start SketchToCut:\n{}".format(
                traceback.format_exc()))


def stop(context):
    try:
        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
        _handlers.clear()
    except:  # noqa: E722
        if _ui:
            _ui.messageBox("Failed to stop SketchToCut:\n{}".format(
                traceback.format_exc()))
