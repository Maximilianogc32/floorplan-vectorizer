"""
floorplan - core library for vectorizing architectural floor plans into clean,
interactive SVG.

Specialised for ONE job: take a raster floor plan (dark strokes = walls, light
areas = rooms) and produce an SVG with three layers:

    1. rooms  - one interactive <path> per enclosed space (id, hover, click)
    2. walls  - the real wall structure, traced as vector and drawn on top
    3. labels - room name + computed floor area

An architect wants a drawing that reads like a plan (walls visible) plus a room
schedule (areas in m2). Each room carries a real-world area when a drawing scale
is supplied. Dependencies: numpy + opencv-python only; SVG is written by hand.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Room:
    contour: np.ndarray
    holes: List[np.ndarray] = field(default_factory=list)
    area_px: float = 0.0
    area_units: Optional[float] = None
    centroid: Tuple[float, float] = (0.0, 0.0)
    rid: str = ""
    label: str = ""

    def bbox(self) -> Tuple[int, int, int, int]:
        xs = self.contour[:, 0]; ys = self.contour[:, 1]
        return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


# --------------------------------------------------------------------------- #
# Image IO + scale
# --------------------------------------------------------------------------- #
def load_image(path: str, max_dim: int = 2400) -> Tuple[np.ndarray, float]:
    """Read an image (BGR). Returns (image, resize_scale). Large scans are
    downscaled for speed; resize_scale lets measurements be corrected back."""
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"Could not read image: {path}")
    h, w = img.shape[:2]
    scale = min(1.0, max_dim / float(max(h, w)))
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_AREA)
    return img, scale


def parse_calibration(spec: str) -> float:
    """'x1,y1,x2,y2,length_m' (measured on the ORIGINAL image) -> pixels/metre.
    Lets an architect click a known dimension instead of guessing a scale."""
    parts = [float(p) for p in re.split(r"[,;\s]+", spec.strip()) if p != ""]
    if len(parts) != 5:
        raise ValueError("calibrate expects 'x1,y1,x2,y2,length_m'")
    x1, y1, x2, y2, length = parts
    dist = math.hypot(x2 - x1, y2 - y1)
    if length <= 0:
        raise ValueError("calibration length must be > 0")
    return dist / length


# --------------------------------------------------------------------------- #
# Wall detection
# --------------------------------------------------------------------------- #
def binarize_walls(gray: np.ndarray, method: str = "adaptive",
                   fixed_thresh: int = 0) -> np.ndarray:
    """Binary mask, walls = 255. 'adaptive' is robust to uneven lighting in
    scans; 'fixed' (Otsu or fixed_thresh) suits clean digital / grey-wall plans
    where adaptive over-segments."""
    gray = cv2.medianBlur(gray, 3)  # tame scan / JPEG speckle before threshold
    if method == "fixed":
        t = fixed_thresh if fixed_thresh > 0 else int(
            cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[0])
        _, walls = cv2.threshold(gray, t, 255, cv2.THRESH_BINARY_INV)
    else:
        walls = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                      cv2.THRESH_BINARY_INV, 25, 10)
    return walls


def estimate_wall_thickness(walls: np.ndarray) -> float:
    """Median wall stroke width in px (distance transform ridge * 2). Used to
    auto-size morphology so the tool works across drawings at any resolution."""
    dist = cv2.distanceTransform(walls, cv2.DIST_L2, 3)
    ridge = dist[dist > 1.0]
    if ridge.size == 0:
        return 3.0
    return float(np.median(ridge) * 2.0)


def seal_doorways(walls: np.ndarray, seal_len: int, thickness: float) -> np.ndarray:
    """Bridge doorway openings so each room becomes fully enclosed.

    A door is a gap in an otherwise straight wall, so the two stubs on either
    side are collinear. Closing with a horizontal line kernel bridges gaps along
    horizontal walls, and a vertical line kernel bridges gaps along vertical
    walls. A line kernel does NOT thicken the wall perpendicular to itself, so
    rooms keep their full size -- unlike a square/disk kernel, which must be as
    wide as the door and would eat into the rooms. A small isotropic close first
    repairs anti-aliasing.
    """
    t = max(1, int(round(thickness)))
    if t > 1:
        walls = cv2.morphologyEx(
            walls, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (t, t)))
    if seal_len <= 1:
        return walls
    h_k = cv2.getStructuringElement(cv2.MORPH_RECT, (seal_len, 1))
    v_k = cv2.getStructuringElement(cv2.MORPH_RECT, (1, seal_len))
    return cv2.bitwise_or(
        cv2.morphologyEx(walls, cv2.MORPH_CLOSE, h_k),
        cv2.morphologyEx(walls, cv2.MORPH_CLOSE, v_k))


def _interior_count(walls_sealed: np.ndarray, min_area: float) -> int:
    """How many interior rooms (components not touching the image border and
    above min_area) a given sealed-wall mask yields. Used to auto-tune sealing."""
    floor = cv2.bitwise_not(walls_sealed)
    n, _, stats, _ = cv2.connectedComponentsWithStats(floor, connectivity=4)
    h, w = walls_sealed.shape
    c = 0
    for lbl in range(1, n):
        if stats[lbl, cv2.CC_STAT_AREA] < min_area:
            continue
        x = stats[lbl, cv2.CC_STAT_LEFT]; y = stats[lbl, cv2.CC_STAT_TOP]
        bw = stats[lbl, cv2.CC_STAT_WIDTH]; bh = stats[lbl, cv2.CC_STAT_HEIGHT]
        if x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1:
            continue
        c += 1
    return c


def auto_seal_len(walls: np.ndarray, thickness: float, min_area: float) -> int:
    """Pick a doorway-sealing length without a known scale.

    As we seal wider gaps, real doorways close and rooms separate, so the room
    count rises; seal too wide and adjacent rooms start merging through thin
    walls, so the count falls again. We sweep a range and take the smallest
    length that reaches the peak count -- the point where every door is just
    sealed but no rooms have been destroyed yet.
    """
    h, w = walls.shape
    lo = max(3, int(round(thickness * 2)))
    hi = max(lo + 1, int(round(0.14 * min(h, w))))
    best_len, best_count = lo, -1
    for L in np.linspace(lo, hi, 8):
        L = int(round(L))
        count = _interior_count(seal_doorways(walls, L, thickness), min_area)
        if count > best_count:
            best_count, best_len = count, L
    return best_len


def denoise_walls(walls: np.ndarray, min_blob: int) -> np.ndarray:
    """Drop tiny isolated wall blobs (scan grain, JPEG blocks, text dots) that
    would otherwise fragment rooms into dozens of slivers. Real walls are long,
    high-area components, so a small area floor removes noise without touching
    structure."""
    n, lab, stats, _ = cv2.connectedComponentsWithStats(walls, connectivity=8)
    keep = np.zeros_like(walls)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_blob:
            keep[lab == i] = 255
    return keep


# --------------------------------------------------------------------------- #
# Room detection
# --------------------------------------------------------------------------- #
def detect_rooms(img: np.ndarray, min_area_frac: float = 0.0012,
                 close_gaps: Optional[int] = None, seal_doors: bool = True,
                 seal_len: Optional[int] = None, simplify: float = 0.004,
                 keep_orthogonal: bool = True, wall_method: str = "adaptive",
                 fixed_thresh: int = 0) -> Tuple[List[Room], np.ndarray]:
    """Detect rooms in a floor plan; return (rooms, wall_mask).

    Pipeline: binarize -> seal doorways (directional, see seal_doorways) ->
    connected components of the floor -> drop any component touching the image
    border (exterior / frame) -> trace each interior space, keeping inner
    courtyards as holes. Geometry is simplified and snapped orthogonal so the
    output reads like a drafted plan.

    Sealing length: close_gaps (isotropic) overrides everything; else seal_len
    (from a known scale, ideal) is used; else it is found automatically by
    sweeping (auto_seal_len).
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    walls = binarize_walls(gray, wall_method, fixed_thresh)
    h, w = gray.shape
    thickness = estimate_wall_thickness(walls)
    walls = denoise_walls(walls, min_blob=max(12, int(round(thickness * thickness * 1.5))))
    min_area = min_area_frac * float(h * w)

    if close_gaps is not None:
        walls_sealed = walls
        if close_gaps > 0:
            k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_gaps, close_gaps))
            walls_sealed = cv2.morphologyEx(walls, cv2.MORPH_CLOSE, k)
    elif seal_doors:
        if seal_len:
            L = max(int(round(thickness * 2)), min(seal_len, int(0.25 * min(h, w))))
        else:
            L = auto_seal_len(walls, thickness, min_area)
        walls_sealed = seal_doorways(walls, L, thickness)
    else:
        walls_sealed = seal_doorways(walls, 1, thickness)

    floor = cv2.bitwise_not(walls_sealed)
    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(floor, connectivity=4)

    rooms: List[Room] = []
    for lbl in range(1, n_labels):
        area = float(stats[lbl, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        x = stats[lbl, cv2.CC_STAT_LEFT]; y = stats[lbl, cv2.CC_STAT_TOP]
        bw = stats[lbl, cv2.CC_STAT_WIDTH]; bh = stats[lbl, cv2.CC_STAT_HEIGHT]
        # Drop anything reaching the image border: that's the exterior or a
        # leftover frame sliver, never an interior room (plans have a margin).
        if x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1:
            continue
        mask = np.uint8(labels == lbl) * 255
        cnts, hier = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            continue
        areas = [cv2.contourArea(c) for c in cnts]
        oi = int(np.argmax(areas))
        outer = _simplify(cnts[oi], simplify)
        if len(outer) < 3:
            continue
        if keep_orthogonal:
            outer = _snap_orthogonal(outer)
        holes = []
        if hier is not None:
            child = hier[0][oi][2]
            while child != -1:
                if cv2.contourArea(cnts[child]) >= min_area:
                    hp = _simplify(cnts[child], simplify)
                    if keep_orthogonal:
                        hp = _snap_orthogonal(hp)
                    if len(hp) >= 3:
                        holes.append(hp)
                child = hier[0][child][0]
        rooms.append(Room(contour=outer, holes=holes, area_px=area,
                          centroid=_label_point(outer, holes)))

    rooms.sort(key=lambda r: -r.area_px)
    return rooms, walls_sealed


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def _simplify(contour: np.ndarray, epsilon_frac: float) -> np.ndarray:
    peri = cv2.arcLength(contour, True)
    eps = max(1.0, epsilon_frac * peri)
    return cv2.approxPolyDP(contour, eps, True).reshape(-1, 2)


def _snap_orthogonal(poly: np.ndarray, tol_deg: float = 14.0) -> np.ndarray:
    pts = poly.astype(np.float32).copy()
    n = len(pts)
    for i in range(n):
        a = pts[i]; b = pts[(i + 1) % n]
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        if min(abs(ang), abs(abs(ang) - 180)) < tol_deg:
            yv = (a[1] + b[1]) / 2.0; pts[i][1] = yv; pts[(i + 1) % n][1] = yv
        elif abs(abs(ang) - 90) < tol_deg:
            xv = (a[0] + b[0]) / 2.0; pts[i][0] = xv; pts[(i + 1) % n][0] = xv
    return pts


def _centroid(contour: np.ndarray) -> Tuple[float, float]:
    m = cv2.moments(contour.astype(np.int32))
    if abs(m["m00"]) < 1e-6:
        return float(contour[:, 0].mean()), float(contour[:, 1].mean())
    return m["m10"] / m["m00"], m["m01"] / m["m00"]


def _label_point(contour: np.ndarray,
                 holes: Sequence[np.ndarray] = ()) -> Tuple[float, float]:
    """Pole of inaccessibility: interior point farthest from any edge. Keeps a
    room's label inside the room even for L-shaped / concave spaces."""
    pts = contour.astype(np.int32)
    x0, y0 = pts[:, 0].min(), pts[:, 1].min()
    x1, y1 = pts[:, 0].max(), pts[:, 1].max()
    pad = 2
    w = int(x1 - x0) + 2 * pad
    h = int(y1 - y0) + 2 * pad
    if w < 3 or h < 3:
        return _centroid(contour)
    mask = np.zeros((h, w), np.uint8)
    cv2.fillPoly(mask, [(pts - [x0 - pad, y0 - pad]).astype(np.int32)], 255)
    for hole in holes:
        hp = (np.asarray(hole, np.int32) - [x0 - pad, y0 - pad]).astype(np.int32)
        cv2.fillPoly(mask, [hp], 0)
    dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    _, _, _, maxloc = cv2.minMaxLoc(dist)
    return float(maxloc[0] + x0 - pad), float(maxloc[1] + y0 - pad)


def _slug(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower())
    return text.strip("-") or "room"


# --------------------------------------------------------------------------- #
# Areas + ids
# --------------------------------------------------------------------------- #
M2_TO_FT2 = 10.7639


def apply_scale(rooms: Sequence[Room], px_per_metre: float,
                resize_scale: float = 1.0, units: str = "m") -> None:
    """Fill room.area_units. px_per_metre is on the ORIGINAL image; resize_scale
    corrects for any downscale at load time."""
    if not px_per_metre or px_per_metre <= 0:
        return
    ppm = px_per_metre * resize_scale
    for r in rooms:
        a_m2 = r.area_px / (ppm * ppm)
        r.area_units = a_m2 * M2_TO_FT2 if units == "ft" else a_m2


def assign_ids(rooms: Sequence[Room], prefix: str = "room",
               labels: Optional[Sequence[str]] = None) -> None:
    for i, r in enumerate(rooms, start=1):
        if labels and i - 1 < len(labels) and labels[i - 1]:
            r.label = labels[i - 1]
            r.rid = f"{prefix}-{_slug(labels[i - 1])}"
        else:
            r.rid = f"{prefix}-{i}"


def total_area(rooms: Sequence[Room]) -> Tuple[float, Optional[float]]:
    apx = sum(r.area_px for r in rooms)
    has = any(r.area_units is not None for r in rooms)
    au = sum(r.area_units for r in rooms if r.area_units is not None)
    return apx, (au if has else None)


# --------------------------------------------------------------------------- #
# SVG generation
# --------------------------------------------------------------------------- #
def _ring(pts: np.ndarray) -> str:
    out = [f"M {pts[0][0]:.1f} {pts[0][1]:.1f}"]
    for x, y in pts[1:]:
        out.append(f"L {x:.1f} {y:.1f}")
    out.append("Z")
    return " ".join(out)


def _path_d(contour: np.ndarray, holes: Sequence[np.ndarray]) -> str:
    d = _ring(contour)
    for hole in holes:
        d += " " + _ring(hole)
    return d


def _wall_path(wall_mask: np.ndarray, simplify: float = 0.002) -> str:
    cnts, hier = cv2.findContours(wall_mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    segs = []
    for c in cnts:
        if cv2.contourArea(c) < 4:
            continue
        p = _simplify(c, simplify)
        if len(p) >= 3:
            segs.append(_ring(p))
    return " ".join(segs)


def _fmt_area(r: Room, units: str) -> Optional[str]:
    if r.area_units is None:
        return None
    u = "ft2" if units == "ft" else "m2"
    return f"{r.area_units:.1f} {u}"


def build_svg(rooms: Sequence[Room], wall_mask: Optional[np.ndarray],
              width: int, height: int, title: str = "Floor plan",
              show_labels: bool = True, show_area: bool = True,
              interactive: bool = True, units: str = "m",
              room_fill: str = "#eef2f6", wall_fill: str = "#2b3440") -> str:
    P: List[str] = []
    P.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-label="{html.escape(title)}" class="floorplan">')
    P.append(f"<title>{html.escape(title)}</title>")

    css = (
        "\n  .floorplan .room { fill: " + room_fill + "; stroke: none; cursor: pointer;"
        " transition: fill .15s ease; }\n"
        "  .floorplan .room:hover { fill: #d6e4f0; }\n"
        "  .floorplan .room:focus { outline: none; fill: #cfe0ef; }\n"
        "  .floorplan .room.is-selected { fill: #bcd7ef; }\n"
        "  .floorplan .walls { fill: " + wall_fill + "; fill-rule: evenodd; pointer-events: none; }\n"
        "  .floorplan .room-name { font: 600 13px system-ui, -apple-system, 'Segoe UI', sans-serif;"
        " fill: #1c2733; text-anchor: middle; pointer-events: none; user-select: none; }\n"
        "  .floorplan .room-area { font: 400 11px system-ui, -apple-system, 'Segoe UI', sans-serif;"
        " fill: #5b6b7c; text-anchor: middle; pointer-events: none; user-select: none; }")
    P.append(f"<style>{css}\n</style>")

    P.append('<g class="rooms">')
    for r in rooms:
        d = _path_d(r.contour, r.holes)
        extra = ' tabindex="0" role="button"' if interactive else ""
        area_attr = f' data-area-units="{r.area_units:.2f}"' if r.area_units is not None else ""
        label_attr = f' data-label="{html.escape(r.label)}"' if r.label else ""
        P.append(
            f'  <path id="{r.rid}" class="room" d="{d}" '
            f'data-area-px="{r.area_px:.0f}"{area_attr}{label_attr}{extra}>'
            f'<title>{html.escape(r.label or r.rid)}</title></path>')
    P.append("</g>")

    if wall_mask is not None:
        wd = _wall_path(wall_mask)
        if wd:
            P.append(f'<path class="walls" d="{wd}"/>')

    if show_labels:
        P.append('<g class="labels">')
        for r in rooms:
            cx, cy = r.centroid
            name = r.label or r.rid.split("-")[-1]
            area = _fmt_area(r, units) if show_area else None
            if area:
                y1 = cy - 4; y2 = cy + 12
                P.append(f'  <text class="room-name" x="{cx:.0f}" y="{y1:.0f}">{html.escape(name)}</text>')
                P.append(f'  <text class="room-area" x="{cx:.0f}" y="{y2:.0f}">{html.escape(area)}</text>')
            else:
                P.append(f'  <text class="room-name" x="{cx:.0f}" y="{cy:.0f}">{html.escape(name)}</text>')
        P.append("</g>")

    if interactive:
        P.append('<script><![CDATA[\n'
            '(function () {\n'
            "  var svg = document.currentScript.closest('svg');\n"
            '  if (!svg) return;\n'
            '  function select(el) {\n'
            "    svg.querySelectorAll('.room.is-selected').forEach(function (n) {\n"
            "      n.classList.remove('is-selected'); });\n"
            "    el.classList.add('is-selected');\n"
            '    svg.dispatchEvent(new CustomEvent("room:select", { bubbles: true, detail: {\n'
            '      id: el.id,\n'
            '      label: el.getAttribute("data-label") || el.id,\n'
            '      areaPx: Number(el.getAttribute("data-area-px")),\n'
            '      area: el.hasAttribute("data-area-units") ? Number(el.getAttribute("data-area-units")) : null\n'
            '    }}));\n'
            '  }\n'
            "  svg.querySelectorAll('.room').forEach(function (el) {\n"
            "    el.addEventListener('click', function () { select(el); });\n"
            "    el.addEventListener('keydown', function (e) {\n"
            "      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(el); }\n"
            '    });\n'
            '  });\n'
            '})();\n'
            ']]></script>')

    P.append("</svg>")
    return "\n".join(P)
