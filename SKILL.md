---
name: floorplan-vectorizer
description: >-
  Convert a raster image of an architectural floor plan (PNG/JPG of a building
  plan, blueprint, apartment layout, house plan, scanned plan, CAD export) into
  a clean, INTERACTIVE SVG. It detects each enclosed room as its own clickable
  path with a stable id, renders the real wall structure as a layer, computes
  each room's floor area in m2 or ft2 from a drawing scale, and labels every
  room. Use this whenever someone uploads or points to a picture of a floor
  plan and wants it vectorized, made interactive/clickable, turned into an SVG
  web map, measured for room areas, or converted into a room schedule/area
  takeoff. Trigger on phrases like "vectorize this floor plan", "make this plan
  interactive", "turn this blueprint into clickable rooms", "detect the rooms",
  "convert plan to SVG", "calculate room areas from this plan", even when the
  word "SVG" is not used. Do NOT use for geographic/country maps, for tracing
  logos or photographs, or for editing an SVG that already has room regions.
---

# Floor Plan Vectorizer

Turn a picture of an architectural floor plan into an interactive, measured SVG.
The detection is done by a bundled, dependency-light tool (`scripts/vectorize.py`,
needs only `numpy` + `opencv-python`). Your job is to work like a careful
draftsperson: set the scale, run it, read the room schedule, look at the render,
and refine until every room is right.

## What the tool produces

A single self-contained SVG with three layers:

1. **rooms** — one `<path class="room" id="room-...">` per enclosed space, with
   hover styling and click/Tab+Enter selection (fires a `room:select` event).
2. **walls** — the actual wall structure, traced as vector and drawn on top, so
   the output reads like a drafted plan rather than coloured boxes.
3. **labels** — each room's name and computed floor area.

Optionally a `--manifest` JSON (a room schedule: id, label, area in px and m2,
centroid, bounding box) and a `--demo` HTML viewer.

## The core command

```bash
python scripts/vectorize.py PLAN.png [options]
```

Writes `PLAN.svg` next to the input. Key flags:

| Flag | Meaning |
|------|---------|
| `--calibrate "x1,y1,x2,y2,len_m"` | derive the scale from a known dimension you measure on the image (e.g. a 5.00 m wall between two pixel points) |
| `--scale PX_PER_M` | set the scale directly (pixels per metre) |
| `--units m\|ft` | report areas in m² (default) or ft² |
| `--labels "A,B,C"` | name rooms in **descending-area order**; names become ids and on-plan text |
| `--manifest` | also write the room schedule JSON |
| `--demo` | also write an interactive viewer HTML |
| `--static` | omit the interactivity script (plain SVG) |
| `--no-walls` / `--no-labels` | drop the wall layer / the text |

Detection tuning (usually not needed — sealing is automatic):

| Flag | Meaning |
|------|---------|
| `--wall-method fixed` | for clean digital plans or light-grey walls where the default adaptive threshold over-segments |
| `--threshold N` | grey cutoff for `fixed` (0 = automatic/Otsu) |
| `--min-area F` | ignore rooms below fraction F of the image (raise to kill speckles) |
| `--close-gaps N` | manual isotropic gap sealing (escape hatch; prefer the automatic sealing) |
| `--no-seal-doors` | treat doorways as open (rooms will merge across them) |
| `--no-orthogonal` | keep raw angles instead of snapping walls to H/V (use for rotated/diagonal plans) |

## How it works (so you can reason about failures)

The image is binarized (walls = dark). Doorways are gaps in otherwise straight
walls, so they're sealed with **directional line kernels** — a horizontal kernel
bridges gaps in horizontal walls, a vertical one in vertical walls — which closes
openings without thickening walls into the rooms. With a known scale, the seal
length is set to the widest realistic door (~1.1 m); **without** a scale, the
tool sweeps a range of seal lengths and keeps the one that yields the most stable
room count. The floor is then split into connected components; anything touching
the image border is the exterior (or a frame sliver) and is dropped; each
remaining interior space becomes a room, with interior courtyards kept as holes.

The single biggest quality lever is **scale**. Always calibrate when you can —
it makes door sealing reliable AND gives real areas.

## Recommended workflow

Treat the first run as a draft and verify against what a plan *should* contain:

1. **Calibrate the scale.** Find a known dimension on the drawing (a dimensioned
   wall, a door you can assume ≈0.9 m, a stated room size). Read its two
   endpoints in pixels and pass `--calibrate "x1,y1,x2,y2,length_m"`. If a
   pixels-per-metre value is known, use `--scale` instead.
2. **First pass** with `--manifest`. Check the printed room count and total area
   against your expectation. Plans that come back with **too few** rooms have
   walls leaking across doorways or thin/faint lines — try `--wall-method fixed`,
   nudge `--threshold`, or raise `--close-gaps`. **Too many** tiny rooms → raise
   `--min-area`.
3. **Render and inspect — don't guess.** Rasterize to PNG and actually look
   (e.g. `python -c "import cairosvg; cairosvg.svg2png(url='PLAN.svg',
   write_to='preview.png', output_width=900)"`), or open the `--demo` page.
   Confirm rooms are separated correctly, walls read cleanly, and each label
   sits inside its room with a believable area.
4. **Name the rooms** once geometry is right: pass `--labels` in
   descending-area order so ids are stable and meaningful (`room-kitchen`).
5. **Deliver** the SVG (plus the schedule/demo if useful) and show the user a
   rendered preview, not just the file.

A note on what "perfect" means here: the value is a plan a developer can drop
into a web page and bind behaviour to, and a room schedule an architect can
trust. So getting **every room separated**, **stable ids**, and **correct areas**
matters more than pixel-perfect wall tracing. Spend your effort there, and verify
the area total against the gross floor area you'd expect.

## Using the SVG in a web page

```js
document.querySelector('.floorplan').addEventListener('room:select', e => {
  // e.detail = { id, label, areaPx, area }   // area is in the chosen units (or null)
  console.log(e.detail.label, e.detail.area, 'm²');
});
```

Style rooms by id or via the `.room` / `.is-selected` classes. The SVG is
self-contained (inline CSS + JS); pass `--static` to wire up your own behaviour.

## Edge cases and deeper tuning

For scanned/noisy plans, light-grey or hatched walls, CAD exports, plans with
furniture or dimension lines, keeping ids stable across re-runs, and adjusting
detection in code, read `references/tuning.md`. The `examples/` folder has a
worked apartment plan (source PNG, SVG, schedule JSON, demo HTML).
