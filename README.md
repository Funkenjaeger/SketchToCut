# SketchToCut

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
    color** (peel each onto its own vinyl sheet), in shelves along a chosen
    axis (Y default). The packing is **bounded by your bed size**: pieces that
    do not fit spill onto another sheet (`base-1.svg`, `base-2.svg`, …) instead
    of running off the end of the material.
  * **One file per region** — one file per closed sketch region (Fusion
    profile), so you can manually section a part by drawing dividing lines.
* Both modes emit an **`ASSEMBLY`** reference (filled, colored, lettered) showing
  where every piece belongs. Shapes are **filled** (holes cut via even-odd), each
  piece a distinct color, with fiducials in the same color so they cut together;
  red is reserved for the piece letters.
* **SVG** (vinyl) or **DXF** (R12, laser/SendCutSend) output.
* Holes preserved; a bolt-hole disc is not emitted as a spurious piece.
* Each piece **auto-rotated** to fit a user **max bed size**; regions too big to
  fit at any rotation are **auto-tiled** into bed-sized tiles, with a **min tile
  size** that biases toward full-bed tiles and avoids thin slivers.
* **Alignment fiducials** (perpendicular ticks) on shared cut edges; ticks that
  would poke out of a thin piece are dropped (both halves). In **DXF** they go on
  a dedicated `FIDUCIAL` layer, so a laser workflow can hide or delete the ticks
  without touching cut geometry (which stays on the per-piece layers). In **SVG**
  they stay with their piece in the piece's color, so a color-separating vinyl
  cutter cuts them together. The `ASSEMBLY` reference carries no fiducials —
  they would vanish under the filled shapes.
* Dialog settings are **remembered** between runs.

## Example

A large panel exported in **One file** mode. It's bigger than the cutter bed, so
it's **auto-tiled** into six bed-sized pieces (A–F), each a distinct color with
matching **fiducial** ticks along the shared cuts, and packed into a single
~12-inch-wide column for the vinyl cutter to separate by color. The **ASSEMBLY**
reference shows how the pieces fit back into the whole panel.

**ASSEMBLY reference** — how the six pieces reassemble (true size ≈ 80 × 17.5 in):

<img src="docs/example-assembly.svg" width="760" alt="Assembly reference: six colored pieces A-F forming the full panel in their original positions">

**One-file cut layout** — the same pieces auto-tiled and stacked in a
color-separated column (shown scaled down; true size ≈ 12 × 89 in). That is one
sheet because the bed was 12 in wide by roll length; on a 12 × 24 in mat the
same job packs onto numbered sheets instead:

<img src="docs/example-onefile.svg" height="380" alt="One-file output: six colored tiles stacked in a 12-inch column with fiducial ticks and red letters">

### The full workflow, end to end

The images above show the *output*. The whole path, starting from a blank
sketch:

1. Draw the part in a Fusion sketch.
2. Either section it into regions yourself, or leave it oversized and let
   auto-tiling handle it.
3. Run the add-in — **Export Sketch to Cut Files** — and pick one-file mode,
   the bed size, the minimum feature size, and the output format.
4. Out comes a multi-color one-file SVG plus the **ASSEMBLY** reference sheet.
5. Load the one-file SVG into the vinyl cutter software, separate by color, cut.

A screen recording would show this better than prose, and is worth adding if the
workflow ever proves confusing enough to warrant one. Keep any media in `docs/` —
`.gitignore` carries a `!docs/*.svg` exception, so a `*.gif`/`*.png`/`*.mp4`
exception needs adding beside it before media will commit.

> **If GitHub ever stops rendering the relative SVG `<img>` tags above**, export
> the examples to PNG and swap the `src` attributes. The tags were confirmed
> rendering on github.com when they landed.

## Install

Fusion loads add-ins from its `API/AddIns` folder. The project lives in a
normal location and is exposed to Fusion via a directory junction:

```
# Windows (no admin needed):
mklink /J "%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\SketchToCut" "C:\path\to\SketchToCut"
```

Then in Fusion: **Utilities → Add-Ins (Shift+S) → Add-Ins tab → select
SketchToCut → Run** (tick *Run on Startup* to keep it loaded).

## Use

1. Open a sketch for edit (double-click it), or select one in the browser.
2. Click **Export Sketch to Cut Files** in the Solid tab's *Add-Ins* panel.
3. Choose units (inch/mm), then a save location.

Only real profile curves are exported; construction and projected/reference
geometry are skipped (construction can be re-enabled in the dialog).

## Design

* **`core/`** — dependency-free (stdlib-only, so it runs inside Fusion's
  sandboxed interpreter *and* under a plain `python` for testing):
  `geometry.py` (IR + transforms), `svg.py` / `dxf.py` (writers), `loops.py`
  (chain profile edges into closed rings), `fiducials.py` (tick marks),
  `fitting.py` (fit-under-rotation), `tiling.py` (rectangle clip + grid).
* **`SketchToCut.py`** — the add-in: command UI, sketch/profile resolution,
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

* Tiling is butt-joint by default; an optional `overlap` gives poster-style
  tiles that share a margin, with crop marks on the grid lines for alignment
  (crop marks are grid-based, so on a concave clip one can fall outside the
  actual cut shape).
* A tile-boundary line that lands exactly on the floor of a notch leaves the
  two regions either side of the notch joined by a zero-width run along that
  line, rather than split into two pieces. (Disjoint tile-intersections in
  general *are* split — see `clip_polygon_rect`.)
* Fiducials are not matched across a tiled region's outer boundary with an
  adjacent (non-tiled) region.
* One-file packing is shelf-based (first-fit-decreasing), not an optimal nest:
  it never exceeds the bed, but it will leave gaps a smarter nester would fill,
  and can therefore use one more sheet than strictly necessary.
