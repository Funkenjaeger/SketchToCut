# SketchToSVG

A Fusion 360 add-in that exports a sketch to **1:1-scale SVG or DXF** — for
cutting paper masks on a vinyl cutter (trace/cut plywood) or sending DXFs to a
laser cutter / SendCutSend.

## Why

Vinyl-cutter software often only ingests image formats (but accepts SVG), and
getting a correctly-scaled vector outline out of Fusion for cutting or laser
quoting is otherwise fiddly. This makes it one click.

## Features

* Two export modes over the same pipeline:
  * **One file** — every piece packed into a single file, each a **distinct
    color** (peel each onto its own vinyl sheet), stacked along a chosen axis
    (Y default) within the bed width.
  * **One file per region** — one file per closed sketch region (Fusion
    profile) + a `MASTER` assembly map, so you can manually section a part by
    drawing dividing lines.
* **SVG** (vinyl) or **DXF** (R12, laser/SendCutSend) output.
* Holes preserved; a bolt-hole disc is not emitted as a spurious piece.
* Each piece **auto-rotated** to fit a user **max bed size**; regions too big to
  fit at any rotation are **auto-tiled** into bed-sized tiles, with a **min tile
  size** that biases toward full-bed tiles and avoids thin slivers.
* **Alignment fiducials** (perpendicular ticks) on shared cut edges; ticks that
  would poke out of a thin piece are dropped (both halves).
* Dialog settings are **remembered** between runs.

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

* **`core/`** — dependency-free (stdlib-only, so it runs inside Fusion's
  sandboxed interpreter *and* under a plain `python` for testing):
  `geometry.py` (IR + transforms), `svg.py` / `dxf.py` (writers), `loops.py`
  (chain profile edges into closed rings), `fiducials.py` (tick marks),
  `fitting.py` (fit-under-rotation), `tiling.py` (rectangle clip + grid).
* **`SketchToSVG.py`** — the add-in: command UI, sketch/profile resolution,
  geometry extraction via the Fusion API into the core IR (in centimetres, the
  API's native unit), and settings persistence.

Per-region extraction uses `ProfileCurve.geometry` (the *trimmed* curve that
bounds a profile), so a region bordered by part of a long shared curve gets
only that part. Arcs/ellipses/splines flatten to polylines; lines and full
circles stay crisp.

## Test

The core is unit-tested without Fusion:

```
python test_core.py
```

Covers scaling, units, Y-flip, bbox, arc flags, loop chaining, fiducials,
fit-rotation, tiling, and DXF structure. `test_regions_mock.py` drives the real
per-region/tiling/DXF export code against a mocked Fusion API. Neither test
needs Fusion or any third-party package (DXF output is separately validated
against `ezdxf` as a dev-only check).

## Scale calibration

Unit interpretation varies between tools (1 user-unit = 1 physical unit here,
vs. some assuming 96 units/inch; DXF sets `$INSUNITS` and SendCutSend confirms
units on upload). Before trusting cuts, export a known square (e.g. 100 mm),
cut it, and measure to confirm true 1:1.

## Roadmap / limitations

* Tiling is butt-joint only (no configurable overlap yet).
* Sutherland-Hodgman clipping connects a concave piece's disjoint
  tile-intersections with a seam along the tile boundary instead of separate
  loops.
* Fiducials are not matched across a tiled region's outer boundary with an
  adjacent (non-tiled) region.
* One-file packing is a single column/row (no 2-D wrap); very many pieces make a
  long strip — set a long-enough material length in your cutter SW.
