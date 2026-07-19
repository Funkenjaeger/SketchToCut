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

import os
import traceback

import adsk.core
import adsk.fusion

from .core import geometry as geom
from .core import svg as svgwriter

# --- constants -------------------------------------------------------------
CMD_ID = "SketchToSVG_ExportCmd"
CMD_NAME = "Export Sketch to SVG"
CMD_TOOLTIP = ("Export the active sketch's profile curves to a 1:1-scale SVG "
               "for a vinyl cutter.")
PANEL_ID = "SolidScriptsAddinsPanel"

# Chord tolerance for flattening curves, in centimetres (0.0025 cm = 0.025 mm).
FLATTEN_TOL_CM = 0.0025

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


# --- command handlers ------------------------------------------------------
class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs

            unit = "in" if inputs.itemById("units").selectedItem.index == 0 else "mm"
            include_construction = inputs.itemById("includeConstruction").value
            stroke_width = inputs.itemById("strokeWidth").value

            sketch = _resolve_sketch()
            if not sketch:
                _ui.messageBox(
                    "No sketch found.\n\nOpen a sketch for edit (double-click it), "
                    "or select one in the browser tree, then run again.")
                return

            elements = extract_elements(sketch, include_construction)
            if not elements:
                _ui.messageBox(
                    "That sketch has no exportable profile curves "
                    "(construction/reference geometry is skipped).")
                return

            svg_text = svgwriter.render(
                elements, unit=unit, stroke_width=stroke_width)

            # Choose output path.
            dlg = _ui.createFileDialog()
            dlg.title = "Save SVG"
            dlg.filter = "SVG files (*.svg)"
            safe_name = "".join(c for c in sketch.name
                                if c.isalnum() or c in " _-").strip() or "sketch"
            dlg.initialFilename = safe_name + ".svg"
            if dlg.showSave() != adsk.core.DialogResults.DialogOK:
                return
            path = dlg.filename
            if not path.lower().endswith(".svg"):
                path += ".svg"

            with open(path, "w", encoding="utf-8") as fp:
                fp.write(svg_text)

            bbox = geom.bounding_box(elements)
            s = geom.cm_to(unit)
            w = (bbox[2] - bbox[0]) * s
            h = (bbox[3] - bbox[1]) * s
            _ui.messageBox(
                "Exported %d curve(s).\nSize: %.3f x %.3f %s (1:1)\n\n%s" % (
                    len(elements), w, h, unit, path))
        except:  # noqa: E722 -- surface any failure to the user
            if _ui:
                _ui.messageBox("Export failed:\n{}".format(traceback.format_exc()))


class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs

            units = inputs.addDropDownCommandInput(
                "units", "Output units",
                adsk.core.DropDownStyles.TextListDropDownStyle)
            units.listItems.add("Inch", True)      # index 0, default
            units.listItems.add("Millimeter", False)

            inputs.addBoolValueInput(
                "includeConstruction", "Include construction geometry",
                True, "", False)

            inputs.addFloatSpinnerCommandInput(
                "strokeWidth", "Stroke width (output units)",
                "", 0.0, 10.0, 0.005, 0.01)

            inputs.addTextBoxCommandInput(
                "hint", "",
                "Exports the active/selected sketch's profile curves at true "
                "1:1 scale. You'll pick the save location next.", 3, True)

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
