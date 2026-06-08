#!/usr/bin/env python3
"""
vectorize.py - turn a raster floor plan into a clean, interactive SVG.

Examples
--------
  # Simplest run (auto wall thickness, auto door sealing):
  python vectorize.py plan.png

  # Name the rooms (largest-first), emit a room schedule and a demo page:
  python vectorize.py plan.png --labels "Living,Bed 1,Bed 2,Kitchen,Bath" \\
      --manifest --demo

  # Real areas: calibrate from a known 5.00 m wall measured on the image,
  #             between pixels (120,840) and (740,840):
  python vectorize.py plan.png --calibrate "120,840,740,840,5.0" --manifest

  # Or supply the scale directly (pixels per metre) and report in feet:
  python vectorize.py plan.png --scale 124 --units ft

Outputs
-------
  <name>.svg        rooms layer (interactive) + walls layer + labels
  <name>.json       (--manifest) room schedule: id, label, area_px, area, bbox
  <name>.demo.html  (--demo) viewer wired to the `room:select` event

The SVG fires `room:select` on click / Enter:
  document.querySelector('.floorplan')
    .addEventListener('room:select', e => console.log(e.detail));
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import floorplan as fp


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Vectorize a floor-plan image into an interactive SVG.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("image", help="input raster floor plan (png/jpg/...)")
    p.add_argument("-o", "--output", help="output .svg path (default: alongside input)")
    p.add_argument("--title", default=None, help="SVG title / accessible label")
    p.add_argument("--labels", default=None,
                   help="comma-separated room names, applied largest-first")
    p.add_argument("--prefix", default="room", help="id prefix for rooms")
    p.add_argument("--manifest", action="store_true",
                   help="also write a <name>.json room schedule")
    p.add_argument("--demo", action="store_true",
                   help="also write a <name>.demo.html viewer page")
    p.add_argument("--static", action="store_true", help="omit interactivity JS")
    p.add_argument("--no-labels", action="store_true", help="don't draw room labels")
    p.add_argument("--no-walls", action="store_true",
                   help="don't draw the wall layer (rooms only)")
    p.add_argument("--max-dim", type=int, default=2400,
                   help="downscale longest side to this many px before processing")

    s = p.add_argument_group("scale / areas")
    s.add_argument("--scale", type=float, default=0.0,
                   help="pixels per metre on the original image (enables areas)")
    s.add_argument("--calibrate", default=None,
                   help="'x1,y1,x2,y2,length_m' on the original image -> derives scale")
    s.add_argument("--units", choices=["m", "ft"], default="m",
                   help="area units to display (m2 or ft2)")

    d = p.add_argument_group("detection tuning")
    d.add_argument("--min-area", type=float, default=0.0012,
                   help="ignore rooms smaller than this fraction of the image")
    d.add_argument("--close-gaps", type=int, default=None,
                   help="manual isotropic sealing size (overrides smart sealing)")
    d.add_argument("--no-seal-doors", action="store_true",
                   help="don't auto-seal doorway gaps")
    d.add_argument("--no-orthogonal", action="store_true",
                   help="don't snap room edges to horizontal/vertical")
    d.add_argument("--wall-method", choices=["adaptive", "fixed"], default="adaptive",
                   help="wall binarization; 'fixed' for clean digital/grey-wall plans")
    d.add_argument("--threshold", type=int, default=0,
                   help="[fixed] grey threshold 1-254 (0 = auto/Otsu)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not os.path.exists(args.image):
        print(f"error: file not found: {args.image}", file=sys.stderr)
        return 2

    img, resize_scale = fp.load_image(args.image, max_dim=args.max_dim)
    h, w = img.shape[:2]

    # Resolve scale first: a known drawing scale lets us size doorway sealing in
    # real metres (a door is at most ~1.1 m), far more reliable than guessing.
    ppm = args.scale
    if args.calibrate:
        try:
            ppm = fp.parse_calibration(args.calibrate)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    seal_len = int(round(1.1 * ppm * resize_scale)) if ppm else None

    rooms, wall_mask = fp.detect_rooms(
        img,
        min_area_frac=args.min_area,
        close_gaps=args.close_gaps,
        seal_doors=not args.no_seal_doors,
        seal_len=seal_len,
        keep_orthogonal=not args.no_orthogonal,
        wall_method=args.wall_method,
        fixed_thresh=args.threshold,
    )

    if not rooms:
        print("warning: no rooms detected. Try --wall-method fixed, adjust "
              "--threshold, lower --min-area, or set --close-gaps.",
              file=sys.stderr)

    if ppm:
        fp.apply_scale(rooms, ppm, resize_scale=resize_scale, units=args.units)

    labels = [s.strip() for s in args.labels.split(",")] if args.labels else None
    fp.assign_ids(rooms, prefix=args.prefix, labels=labels)

    title = args.title or os.path.splitext(os.path.basename(args.image))[0]
    svg = fp.build_svg(
        rooms, None if args.no_walls else wall_mask, w, h,
        title=title, show_labels=not args.no_labels,
        interactive=not args.static, units=args.units)

    out_svg = args.output or os.path.splitext(args.image)[0] + ".svg"
    os.makedirs(os.path.dirname(os.path.abspath(out_svg)), exist_ok=True)
    with open(out_svg, "w", encoding="utf-8") as f:
        f.write(svg)

    apx, au = fp.total_area(rooms)
    unit = "ft2" if args.units == "ft" else "m2"
    extra = f", total {au:.1f} {unit}" if au is not None else ""
    print(f"detected {len(rooms)} rooms{extra} -> {out_svg}")

    if args.manifest:
        manifest = {
            "source": os.path.basename(args.image),
            "width": w, "height": h, "units": args.units,
            "pixels_per_metre": round(ppm * resize_scale, 3) if ppm else None,
            "total_area_px": round(apx, 1),
            "total_area_units": round(au, 2) if au is not None else None,
            "rooms": [
                {"id": r.rid, "label": r.label or None,
                 "area_px": round(r.area_px, 1),
                 "area_units": round(r.area_units, 2) if r.area_units is not None else None,
                 "centroid": [round(r.centroid[0], 1), round(r.centroid[1], 1)],
                 "bbox": list(r.bbox())}
                for r in rooms
            ],
        }
        out_json = os.path.splitext(out_svg)[0] + ".json"
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        print(f"         schedule -> {out_json}")

    if args.demo:
        out_html = os.path.splitext(out_svg)[0] + ".demo.html"
        with open(out_html, "w", encoding="utf-8") as f:
            f.write(_demo_html(title, svg, unit))
        print(f"         demo     -> {out_html}")

    return 0


def _demo_html(title: str, svg: str, unit: str) -> str:
    unit_disp = "ft²" if unit == "ft2" else "m²"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - floor plan</title>
<style>
  body {{ margin:0; font:15px system-ui,-apple-system,"Segoe UI",sans-serif;
          background:#f4f6f9; color:#1c2733; display:flex; min-height:100vh; }}
  .stage {{ flex:1; display:flex; align-items:center; justify-content:center; padding:24px; }}
  .stage svg {{ max-width:100%; max-height:88vh; height:auto; background:#fff;
                border-radius:12px; box-shadow:0 6px 24px rgba(20,30,50,.12); }}
  aside {{ width:300px; padding:24px 20px; background:#fff; border-left:1px solid #e3e8ee; }}
  aside h1 {{ font-size:16px; margin:0 0 4px; }}
  aside p {{ color:#5b6b7c; font-size:13px; margin:0 0 16px; }}
  .pill {{ display:inline-block; background:#eef2f7; border-radius:999px; padding:3px 10px;
           font-size:12px; color:#3a4a5a; }}
  dl {{ margin:14px 0 0; }} dt {{ color:#90a0b0; font-size:12px; margin-top:10px; }}
  dd {{ margin:2px 0 0; font-weight:600; }}
</style></head><body>
  <div class="stage">{svg}</div>
  <aside>
    <h1>{title}</h1>
    <p>Click or Tab + Enter on any room.</p>
    <span class="pill" id="count">…</span> <span class="pill" id="total">—</span>
    <dl>
      <dt>Selected room</dt><dd id="sel">—</dd>
      <dt>Id</dt><dd id="rid">—</dd>
      <dt>Area</dt><dd id="area">—</dd>
    </dl>
  </aside>
<script>
  var svg = document.querySelector('.floorplan');
  var rooms = svg.querySelectorAll('.room');
  document.getElementById('count').textContent = rooms.length + ' rooms';
  var tot = 0, has = false;
  rooms.forEach(function (r) {{
    if (r.hasAttribute('data-area-units')) {{ tot += Number(r.getAttribute('data-area-units')); has = true; }}
  }});
  if (has) document.getElementById('total').textContent = tot.toFixed(1) + ' {unit_disp}';
  svg.addEventListener('room:select', function (e) {{
    document.getElementById('sel').textContent = e.detail.label;
    document.getElementById('rid').textContent = e.detail.id;
    document.getElementById('area').textContent =
      e.detail.area != null ? e.detail.area.toFixed(1) + ' {unit_disp}'
                            : e.detail.areaPx.toLocaleString() + ' px²';
  }});
</script>
</body></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
