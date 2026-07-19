"""SketchToSVG -- Fusion 360 add-in.

Exports the active (or selected) sketch's profile curves to a 1:1-scale SVG
suitable for a vinyl cutter, for making paper masks to trace/cut plywood etc.

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

import math
import os
import traceback

import adsk.core
import adsk.fusion

from .core import geometry as geom
from .core import svg as svgwriter
from .core import loops as looplib
from .core import fiducials as fidlib
from .core import fitting as fitlib

# --- constants -------------------------------------------------------------
CMD_ID = "SketchToSVG_ExportCmd"
CMD_NAME = "Export Sketch to SVG"
CMD_TOOLTIP = ("Export the active sketch's profile curves to a 1:1-scale SVG "
               "for a vinyl cutter.")
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


def run_per_region_export(sketch, target_profiles, opts):
    """Fit each region to the bed, write one SVG per piece + a master. Returns a summary."""
    survivors, key_to_pieces, key_points, open_count = extract_regions(
        sketch, target_profiles)
    if not survivors:
        return ("No closed regions found to export.\n\nDraw closed profiles "
                "(and slice them with lines), then run again.")

    if opts["fid_enabled"]:
        for k, idxs in key_to_pieces.items():
            if len(idxs) < 2:
                continue  # only shared (cut) edges get fiducials
            pts = key_points.get(k)
            if not pts:
                continue
            for i in idxs:
                survivors[i].setdefault("fiducials", []).extend(
                    fidlib.edge_ticks(pts, survivors[i]["centroid"],
                                      length=opts["fid_len_cm"],
                                      spacing=opts["fid_spacing_cm"],
                                      inset=opts["fid_inset_cm"]))

    unit, sw = opts["unit"], opts["stroke_width"]
    wc, hc = opts["bed_w_cm"], opts["bed_h_cm"]
    folder, base = opts["folder"], opts["base"]

    # A single region needs no letter suffix and no assembly map.
    single = len(survivors) == 1

    written, skipped = [], []
    master_elements, master_labels, master_fiducials = [], [], []
    for p in survivors:
        letter = p["letter"]
        lh = _label_height_cm(p["outer"])
        master_elements.append(p["outer"])
        master_elements.extend(p["holes"])
        master_labels.append((letter, p["centroid"], lh))
        # Fiducials in original (un-rotated) coords so the master shows how the
        # matching ticks on adjacent pieces line up along their shared cuts.
        master_fiducials.extend(p.get("fiducials", []))

        theta = fitlib.fit_rotation(p["cloud"], wc, hc, step_deg=FIT_STEP_DEG)
        if theta is None:
            skipped.append(letter)
            continue
        center = p["centroid"]
        outer_r = geom.rotate_element(p["outer"], theta, center)
        holes_r = [geom.rotate_element(h, theta, center) for h in p["holes"]]
        fids_r = [geom.rotate_element(f, theta, center)
                  for f in p.get("fiducials", [])]
        labels = ([(letter, center, lh)]
                  if opts["label_on_pieces"] and not single else None)
        svg_text = svgwriter.render(
            [outer_r] + holes_r, unit=unit, stroke_width=sw,
            fiducials=fids_r or None, fiducial_stroke=opts["fid_color"],
            labels=labels)
        fname = "%s.svg" % base if single else "%s_%s.svg" % (base, letter)
        with open(os.path.join(folder, fname), "w", encoding="utf-8") as fp:
            fp.write(svg_text)
        written.append(letter)

    if not single:
        master_svg = svgwriter.render(
            master_elements, unit=unit, stroke_width=sw,
            fiducials=master_fiducials or None,
            fiducial_stroke=opts["fid_color"], labels=master_labels)
        with open(os.path.join(folder, "%s_MASTER.svg" % base),
                  "w", encoding="utf-8") as fp:
            fp.write(master_svg)

    if single:
        if written:
            lines = ["Exported 1 region to:\n%s" % os.path.join(
                folder, "%s.svg" % base)]
        else:
            lines = ["The single region does not fit the %g x %g %s bed at any "
                     "rotation." % (opts["bed_w"], opts["bed_h"], unit)]
    else:
        lines = ["Exported %d piece file(s) + %s_MASTER.svg to:\n%s\n" % (
            len(written), base, folder)]
        lines.append("Pieces: %s" % (", ".join(written) if written else "(none)"))
        if skipped:
            lines.append(
                "\nSKIPPED (don't fit the %g x %g %s bed at any rotation): %s\n"
                "Increase the bed size or slice these smaller (auto-tiling is a "
                "future feature)." % (opts["bed_w"], opts["bed_h"], unit,
                                      ", ".join(skipped)))
    if open_count:
        lines.append("\n%d open curve(s) not part of any region were skipped."
                     % open_count)
    return "\n".join(lines)


# --- command handlers ------------------------------------------------------
class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            unit = "in" if inputs.itemById("units").selectedItem.index == 0 else "mm"
            stroke_width = inputs.itemById("strokeWidth").value
            if inputs.itemById("exportMode").selectedItem.index == 0:
                self._export_whole(inputs, unit, stroke_width)
            else:
                self._export_regions(inputs, unit, stroke_width)
        except:  # noqa: E722 -- surface any failure to the user
            if _ui:
                _ui.messageBox("Export failed:\n{}".format(traceback.format_exc()))

    def _export_whole(self, inputs, unit, stroke_width):
        include_construction = inputs.itemById("includeConstruction").value
        sketch = _resolve_sketch()
        if not sketch:
            _ui.messageBox(
                "No sketch found.\n\nOpen a sketch for edit (double-click it), "
                "or select one in the browser tree, then run again.")
            return
        elements = extract_elements(sketch, include_construction)
        if not elements:
            _ui.messageBox("That sketch has no exportable profile curves "
                           "(construction/reference geometry is skipped).")
            return
        svg_text = svgwriter.render(elements, unit=unit, stroke_width=stroke_width)

        dlg = _ui.createFileDialog()
        dlg.title = "Save SVG"
        dlg.filter = "SVG files (*.svg)"
        safe = "".join(c for c in sketch.name
                       if c.isalnum() or c in " _-").strip() or "sketch"
        dlg.initialFilename = safe + ".svg"
        if dlg.showSave() != adsk.core.DialogResults.DialogOK:
            return
        path = dlg.filename
        if not path.lower().endswith(".svg"):
            path += ".svg"
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(svg_text)

        bbox = geom.bounding_box(elements)
        s = geom.cm_to(unit)
        _ui.messageBox("Exported %d curve(s).\nSize: %.3f x %.3f %s (1:1)\n\n%s" % (
            len(elements), (bbox[2] - bbox[0]) * s, (bbox[3] - bbox[1]) * s,
            unit, path))

    def _export_regions(self, inputs, unit, stroke_width):
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
        bed_w = inputs.itemById("bedW").value
        bed_h = inputs.itemById("bedH").value

        dlg = _ui.createFileDialog()
        dlg.title = "Choose output folder + base name (per-piece files are added)"
        dlg.filter = "SVG files (*.svg)"
        safe = "".join(c for c in sketch.name
                       if c.isalnum() or c in " _-").strip() or "sketch"
        dlg.initialFilename = safe + ".svg"
        if dlg.showSave() != adsk.core.DialogResults.DialogOK:
            return
        chosen = dlg.filename
        folder = os.path.dirname(chosen)
        base = os.path.splitext(os.path.basename(chosen))[0] or safe

        opts = {
            "unit": unit, "stroke_width": stroke_width,
            "bed_w": bed_w, "bed_h": bed_h,
            "bed_w_cm": bed_w / s, "bed_h_cm": bed_h / s,
            "fid_enabled": inputs.itemById("fidEnabled").value,
            "fid_len_cm": inputs.itemById("fidLen").value / 10.0,   # mm -> cm
            "fid_spacing_cm": inputs.itemById("fidSpacing").value / 10.0,
            "fid_inset_cm": 0.3,
            "fid_color": "red",
            "label_on_pieces": inputs.itemById("labelPieces").value,
            "folder": folder, "base": base,
        }
        _ui.messageBox(run_per_region_export(sketch, targets, opts))


class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            text_list = adsk.core.DropDownStyles.TextListDropDownStyle

            mode = inputs.addDropDownCommandInput("exportMode", "Export mode",
                                                  text_list)
            mode.listItems.add("Whole sketch (single SVG)", True)   # index 0
            mode.listItems.add("One file per region", False)        # index 1

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

            units = inputs.addDropDownCommandInput("units", "Output units",
                                                   text_list)
            units.listItems.add("Inch", True)      # index 0, default
            units.listItems.add("Millimeter", False)

            inputs.addBoolValueInput(
                "includeConstruction",
                "Include construction geometry (whole-sketch mode)",
                True, "", False)

            inputs.addFloatSpinnerCommandInput(
                "strokeWidth", "Stroke width (output units)",
                "", 0.0, 10.0, 0.005, 0.01)

            grp = inputs.addGroupCommandInput("perRegion", "Per-region options")
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

            inputs.addTextBoxCommandInput(
                "hint", "",
                "Whole-sketch: one 1:1 SVG. Per-region: one SVG per closed "
                "region (selected regions, else all), auto-rotated to fit the "
                "bed, plus a MASTER assembly map. You'll pick the output "
                "folder/name next.", 4, True)

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
            _ui.messageBox("Failed to start SketchToSVG:\n{}".format(
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
            _ui.messageBox("Failed to stop SketchToSVG:\n{}".format(
                traceback.format_exc()))
