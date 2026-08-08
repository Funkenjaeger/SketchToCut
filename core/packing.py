"""Pack already-fitted pieces into bed-sized bins (dependency-free).

Used by the one-file export: every piece has already been rotated by
:func:`core.fitting.fit_rotation` (or split by :mod:`core.tiling`) so that its
own axis-aligned bounding box fits the bed. That says nothing about the
*packed* result -- laying pieces end to end along one axis with no bound at all
happily produces an arrangement twice as long as the material. Here the bed is
the bin, and the packer opens a second bin (a second output file) rather than
ever emitting an arrangement that overflows it.

The contract, in one line: **every bin this returns fits within
``bin_w x bin_h``.** That is an invariant, not a best effort -- there is no
failure mode for "too many pieces", only more bins. The single legitimate
error is a piece whose own bounding box does not fit the bin; ``fitting.py``
rules that out upstream, so it means the caller skipped the fit step, and it
raises rather than silently producing an over-bin bin.

Algorithm: first-fit-decreasing by height, on shelves. Pieces are sorted
tallest first; each is placed on the first open shelf that still has room, else
a new shelf is opened below the last one, else a new bin is opened. ``gap``
separates pieces within a shelf and shelves from each other (never at a bin
edge). Shelf packing is not optimal -- it is O(n^2), stable, easy to reason
about, and its worst case is bounded by the invariant above rather than by the
material.

Shelf orientation follows the dialog's "arrange along" choice:

``"X"``
    Shelves are **rows**: pieces run across the bed in X, rows stack up in Y.
``"Y"`` (default)
    Shelves are **columns**: pieces stack down a column in Y, columns advance
    in X. Implemented by transposing into the row frame and back, so the two
    orientations share one algorithm and one proof.

Roll stock needs no special case: entering a huge bed height yields a bin tall
enough that everything lands in one bin, still wrapped at the bed width.
"""

from typing import List, NamedTuple, Sequence, Tuple

_EPS = 1e-9

Box = Tuple[float, float, float, float]     # (minx, miny, maxx, maxy)


class Placement(NamedTuple):
    """Where one input box goes: translate it by ``(dx, dy)``.

    ``index`` is its position in the ``boxes`` sequence handed to :func:`pack`
    (packing reorders pieces, so the caller cannot rely on output order).
    """
    index: int
    dx: float
    dy: float


class Bin(NamedTuple):
    """One bed's worth of pieces. ``width``/``height`` are the extents actually
    used, both guaranteed <= the ``bin_w``/``bin_h`` passed to :func:`pack`."""
    placements: List[Placement]
    width: float
    height: float


class PieceTooLargeError(ValueError):
    """A single piece's bounding box does not fit the bin at all.

    Upstream (``core.fitting.fit_rotation``) is supposed to have rotated every
    piece to fit, or ``core.tiling`` to have split it. Reaching the packer with
    an oversized piece means that step was skipped.
    """


def _sizes(boxes: Sequence[Box]) -> List[Tuple[float, float]]:
    return [(b[2] - b[0], b[3] - b[1]) for b in boxes]


def _pack_rows(sizes, bin_w, bin_h, gap):
    """Shelf FFD in the row frame: pieces run along +x, shelves stack along +y.

    Returns a list of bins, each a list of ``(index, x, y)`` placements.
    """
    # Decreasing by height (the shelf-defining dimension); width then index
    # break ties so the result is deterministic for equal-height pieces.
    order = sorted(range(len(sizes)),
                   key=lambda i: (-sizes[i][1], -sizes[i][0], i))

    bins = []           # each: {"shelves": [...], "placed": [(i, x, y), ...]}
    for i in order:
        w, h = sizes[i]
        if w > bin_w + _EPS or h > bin_h + _EPS:
            raise PieceTooLargeError(
                "piece %d is %.4f x %.4f, which does not fit the %.4f x %.4f "
                "bin -- it should have been rotated to fit (core.fitting) or "
                "split (core.tiling) before packing" % (i, w, h, bin_w, bin_h))

        spot = None
        # 1. First fit: any shelf, in bin then shelf order.
        for b in bins:
            for sh in b["shelves"]:
                x = 0.0 if sh["empty"] else sh["x_end"] + gap
                if x + w <= bin_w + _EPS and h <= sh["h"] + _EPS:
                    spot = (b, sh, x)
                    break
            if spot:
                break
        # 2. Else a new shelf, in the first bin with the headroom for it.
        if spot is None:
            for b in bins:
                shelves = b["shelves"]
                y = 0.0 if not shelves else shelves[-1]["y"] + shelves[-1]["h"] + gap
                if y + h <= bin_h + _EPS:
                    sh = {"y": y, "h": h, "x_end": 0.0, "empty": True}
                    shelves.append(sh)
                    spot = (b, sh, 0.0)
                    break
        # 3. Else a new bin. The precondition above guarantees this fits.
        if spot is None:
            sh = {"y": 0.0, "h": h, "x_end": 0.0, "empty": True}
            b = {"shelves": [sh], "placed": []}
            bins.append(b)
            spot = (b, sh, 0.0)

        b, sh, x = spot
        b["placed"].append((i, x, sh["y"]))
        sh["x_end"] = x + w
        sh["empty"] = False

    return [b["placed"] for b in bins]


def pack(boxes: Sequence[Box], bin_w: float, bin_h: float, gap: float = 0.0,
         axis: str = "Y") -> List[Bin]:
    """Pack axis-aligned ``boxes`` into as many ``bin_w x bin_h`` bins as needed.

    ``boxes`` are ``(minx, miny, maxx, maxy)`` tuples -- exactly what
    :func:`core.geometry.bounding_box` returns for a piece's already-rotated
    geometry. The boxes are treated as final: the packer never rotates
    anything, because the rotation that made the piece fit the bed was chosen
    upstream and must be preserved.

    ``axis`` is the shelf orientation, ``"Y"`` (columns, the default) or
    ``"X"`` (rows). ``gap`` is the clearance left between neighbouring pieces
    and between shelves; no gap is added at the bin edges.

    Returns a list of :class:`Bin`, each holding :class:`Placement` records
    whose ``dx``/``dy`` translate that box so it sits inside the bin. Every
    returned bin fits within ``bin_w x bin_h``; pieces never overlap. An empty
    ``boxes`` yields an empty list.

    Raises :class:`PieceTooLargeError` if any single box exceeds the bin.
    """
    if bin_w <= 0.0 or bin_h <= 0.0:
        raise ValueError("bin must be positive (got %r x %r)" % (bin_w, bin_h))
    if gap < 0.0:
        raise ValueError("gap must be >= 0 (got %r)" % (gap,))
    if not boxes:
        return []
    if axis not in ("X", "Y"):
        raise ValueError("axis must be 'X' or 'Y' (got %r)" % (axis,))

    sizes = _sizes(boxes)
    flip = axis == "Y"
    if flip:
        # Transpose into the row frame: a w x h box becomes h x w, and the bin
        # swaps too. Reflection across the diagonal preserves both containment
        # and disjointness, so the invariant carries over unchanged.
        sizes = [(h, w) for w, h in sizes]
        bin_w, bin_h = bin_h, bin_w

    out = []
    for placed in _pack_rows(sizes, bin_w, bin_h, gap):
        placements, used_w, used_h = [], 0.0, 0.0
        for i, x, y in placed:
            w, h = sizes[i]
            used_w = max(used_w, x + w)
            used_h = max(used_h, y + h)
            px, py = (y, x) if flip else (x, y)
            placements.append(Placement(i, px - boxes[i][0], py - boxes[i][1]))
        if flip:
            used_w, used_h = used_h, used_w
        out.append(Bin(placements, used_w, used_h))
    return out
