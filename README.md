# SketchToSVG

A Fusion 360 add-in that exports the active sketch's profile curves to a
**1:1-scale SVG**, for cutting paper masks on a vinyl cutter (e.g. to trace or
cut plywood).

## Why

Vinyl-cutter software often only ingests image formats, but usually accepts
SVG. Getting a correctly-scaled vector outline out of Fusion for cutting is
otherwise fiddly. This makes it one click.

## Install

Fusion loads add-ins from its `API/AddIns` folder. The project lives in a
normal location and is exposed to Fusion via a directory junction:

```
# Windows (no admin needed):
mklink /J "%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\SketchToSVG" "C:\path\to\SketchToSVG"
```

Then in Fusion: **Utilities → Add-Ins (Shift+S) → Add-Ins tab → select
SketchToSVG → Run** (tick *Run on Startup* to keep it loaded).

## Use

1. Open a sketch for edit (double-click it), or select one in the browser.
2. Click **Export Sketch to SVG** in the Solid tab's *Add-Ins* panel.
3. Choose units (inch/mm), then a save location.

Only real profile curves are exported; construction and projected/reference
geometry are skipped (construction can be re-enabled in the dialog).

## Design

* **`core/`** — dependency-free geometry IR (`geometry.py`) and the SVG writer
  (`svg.py`). Handles unit scaling, the sketch→SVG Y-flip, and bounding-box
  translation. Imports nothing outside the stdlib, so it runs both inside
  Fusion's sandboxed interpreter and under a plain `python` for testing.
* **`SketchToSVG.py`** — the add-in: command UI, sketch resolution, and
  geometry extraction via the Fusion API into the core IR (in centimetres,
  the API's native unit).

Arcs / ellipses / splines are flattened to polylines through the curve
evaluator (robust, and cutters flatten internally anyway); lines and full
circles stay crisp.

## Test

The core is unit-tested without Fusion:

```
python test_core.py
```

Covers 1:1 scaling, unit conversion, the Y-flip, bbox/translate, and the arc
sweep-flag convention.

## Scale calibration

SVG unit interpretation varies between tools (1 user-unit = 1 physical unit
here, vs. some tools assuming 96 units/inch). Before trusting cuts, export a
known square (e.g. 100 mm), cut it, and measure to confirm true 1:1.

## Roadmap

* Auto-tiling: split geometry larger than the cutter bed (e.g. 12×24 in) into
  multiple SVG tiles with overlap + registration marks for reassembly.
