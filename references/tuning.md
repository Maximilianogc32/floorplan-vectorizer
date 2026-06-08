# Tuning & Troubleshooting — Floor Plans

Read this when a first pass is wrong and the basic flags in SKILL.md aren't
enough.

## Table of contents
- [Scale & areas](#scale--areas)
- [Too few rooms (rooms merging)](#too-few-rooms-rooms-merging)
- [Too many rooms (over-segmentation)](#too-many-rooms-over-segmentation)
- [Walls: scans, grey walls, hatching](#walls-scans-grey-walls-hatching)
- [Furniture, dimensions, legends](#furniture-dimensions-legends)
- [Keeping ids stable across re-runs](#keeping-ids-stable-across-re-runs)
- [Performance](#performance)
- [Going further in code](#going-further-in-code)

## Scale & areas

Areas are only as good as the scale. Two ways to set it:

- `--calibrate "x1,y1,x2,y2,length_m"` — measure a known dimension on the image
  (a dimensioned wall, or a door you assume ≈0.9 m). Endpoints are in pixels of
  the *original* image; the tool corrects for any internal downscale.
- `--scale PX_PER_M` — if you already know pixels-per-metre.

Sanity check: the printed total should be close to the gross floor area you
expect. If every room is off by the same factor, your calibration length or
endpoints are wrong. Switch units with `--units ft`.

Beyond enabling areas, a known scale makes **door sealing** reliable: openings
are bridged up to ~1.1 m (the widest typical door). This is the single most
effective thing you can do for clean results.

## Too few rooms (rooms merging)

Rooms bleed into each other when walls have openings the sealer didn't close, or
walls are too faint to detect.

- Provide a scale (`--calibrate`/`--scale`) so doorway sealing is sized in real
  metres.
- If walls are light grey or thin, switch to `--wall-method fixed` and tune
  `--threshold` (e.g. 180–210 for light walls; higher keeps more as wall).
- As a manual escape hatch, force sealing with `--close-gaps N` (try 9, 13, 19).
  This is isotropic, so don't overdo it — it can erode thin rooms.

## Too many rooms (over-segmentation)

- Tiny speckle "rooms": raise `--min-area` (e.g. `0.003`).
- A wall texture/hatch is fragmenting a space: switch `--wall-method fixed`, or
  raise `--threshold` so light hatching isn't read as wall.
- Furniture outlines becoming rooms: see below.

## Walls: scans, grey walls, hatching

The default `adaptive` threshold handles uneven lighting in scans. For clean
vector/CAD exports, or plans with solid light-grey poché walls, `fixed`
(Otsu by default, or an explicit `--threshold`) is usually cleaner. If walls are
drawn as hatching rather than solid fill, raise `--threshold` so the white gaps
between hatch lines aren't counted as room, or pre-fill walls in an editor.

## Furniture, dimensions, legends

Furniture blocks, dimension strings, north arrows, and title blocks can become
spurious rooms. Best fixes, in order: crop the legend/title block out of the
image; raise `--min-area` to drop small furniture-sized regions; if furniture is
drawn in a lighter line weight than walls, use `--wall-method fixed` with a
`--threshold` that ignores it.

## Keeping ids stable across re-runs

Rooms are numbered by descending area (`room-1`, `room-2`, …), which can
renumber if you edit the source. Pass `--labels` so each room gets a slugged,
stable id (`Kitchen` → `room-kitchen`). For anything going into production,
always label.

## Performance

Inputs are downscaled so the longest side is `--max-dim` px (default 2400)
before processing. For very large architectural scans, 3000–3500 keeps fine
detail; for quick previews, 1500 is faster. Real-world measurements are
corrected back to the original resolution automatically.

## Going further in code

`scripts/floorplan.py` exposes the internals:

- `detect_rooms(img, min_area_frac, close_gaps, seal_doors, seal_len, simplify,
  keep_orthogonal, wall_method, fixed_thresh)` → `(rooms, wall_mask)`
- `seal_doorways(walls, seal_len, thickness)` — the directional door bridging
- `auto_seal_len(walls, thickness, min_area)` — the scale-free sweep
- `apply_scale(rooms, px_per_metre, resize_scale, units)` — fills `area_units`
- `build_svg(rooms, wall_mask, w, h, ...)` — composition (colors, labels, walls)

Each `Room` carries its outer contour, holes, `area_px`, `area_units`, centroid
(a pole-of-inaccessibility label anchor that always lands inside the room), id
and label, so you can post-process (merge, rename, recolor, compute a schedule)
before calling `build_svg`. The `simplify` epsilon (fraction of perimeter,
~0.003–0.006) trades polygon detail for cleanliness.
