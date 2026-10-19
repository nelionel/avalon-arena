"""Draw the board's pixel castle on a 2-unit grid and write it as vector paths.

The castle keeps the board's palette, size (792 x 424 units) and placement; every pixel is a 2 x 2 square,
twice as fine as the first version, which was traced from a raster on a 4-unit grid.

  python3 assets/board_src/src/draw-castle.py            # writes src/castle.json and src/castle-data.mjs,
  python3 assets/build_board.py                           # then rebuild the dashboard's board.js from them
Needs NumPy.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
W, H, STEP = 396, 212, 2                      # cells; each cell is STEP x STEP units of the 792 x 424 view box

INK, DEEP, MID, SOFT, LIGHT, BASE, GOLD, GOLD_HI, BLUE = range(9)
COLORS = ["#123e2e", "#536c5b", "#7b8b78", "#9da98c", "#b9c3a6", "#acbda0", "#ce9525", "#dca938", "#477a9d"]
img = np.full((H, W), -1, dtype=np.int16)
structure = np.zeros((H, W), dtype=bool)       # stone and bushes: they get an outline


def put(x, y, c, solid=True):
    if 0 <= x < W and 0 <= y < H:
        img[y, x] = c
        if solid:
            structure[y, x] = True


def rect(x0, y0, x1, y1, c, solid=True):
    for y in range(max(0, y0), min(H, y1)):
        for x in range(max(0, x0), min(W, x1)):
            put(x, y, c, solid)


def stone(x0, y0, x1, y1, *, rounded=False, tone=0):
    """A stone face with brick courses. rounded: shaded like a cylinder lit from the upper left."""
    w = x1 - x0
    ramp = [LIGHT, BASE, SOFT, MID, DEEP]
    for x in range(x0, x1):
        t = (x - x0 + 0.5) / w
        if rounded:
            band = 0 if t < 0.22 else 1 if t < 0.62 else 2 if t < 0.86 else 3
        else:
            band = 0 if t < 0.9 else 1
        band = min(band + tone, 4)
        for y in range(y0, y1):
            row = y - y0
            c = ramp[band]
            course = row % 7 == 6                                  # mortar line under every course
            joint = (x - x0 + (8 if (row // 7) % 2 else 0)) % 16 == 15
            if (course or joint) and band < 4 and 2 < x - x0 < w - 3:
                c = ramp[min(band + 1, 4)]
            put(x, y, c)


def merlons(x0, x1, y, tooth=6, gap=4, height=5, tone=0):
    """Crenellations whose bottoms sit on row y."""
    x = x0
    while x < x1:
        stone(x, y - height, min(x + tooth, x1), y, tone=tone)
        x += tooth + gap


def cone(cx, apex, base, half, *, eave=2):
    """A pointed roof: apex row to base row, half-width at the base; lit stripe on the left."""
    for y in range(apex, base):
        hw = max(1, round(half * (y - apex + 1) / (base - apex)))
        for x in range(cx - hw, cx + hw):
            c = INK
            if cx - hw + 2 <= x <= cx - hw + 3 and y > apex + 3:      # rim light along the left edge
                c = DEEP
            if (y - apex) % 6 == 5 and (x + y) % 4 == 0 and x > cx - hw + 3:   # shingle flecks
                c = DEEP
            put(x, y, c, solid=False)
    rect(cx - half - eave, base, cx + half + eave, base + 3, INK, solid=False)      # eave
    rect(cx - half - eave + 1, base + 3, cx + half + eave - 1, base + 4, DEEP, solid=False)


def window(x0, y0, w, h):
    """An arched, lit window with a cross mullion."""
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            dx = min(x - x0, x0 + w - 1 - x)
            arch = y - y0 < 3 and dx < 3 - (y - y0)
            if arch:
                continue
            edge = dx == 0 or y == y0 + h - 1 or (y - y0 < 4 and dx < 4 - (y - y0))
            c = INK if edge else GOLD_HI
            if not edge and (x == x0 + w // 2 or y == y0 + h // 2):
                c = GOLD
            put(x, y, c, solid=False)


def disc(cx, cy, r, c, solid=True):
    for y in range(cy - r, cy + r + 1):
        for x in range(cx - r, cx + r + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r + r * 0.6:
                put(x, y, c, solid)


def bush(cx, cy, r):
    """A rounded bush of three blobs: shaded body, highlight on the upper left."""
    blobs = [(cx - r * 2 // 3, cy + r // 4, r * 2 // 3), (cx, cy - r // 4, r), (cx + r * 2 // 3, cy + r // 4, r * 2 // 3)]
    for bx, by, br in blobs:
        disc(bx, by, br, DEEP)
    for bx, by, br in blobs:
        disc(bx, by - 1, br - 1, MID)
    for bx, by, br in blobs:
        disc(bx - br // 3, by - br // 3, max(1, br // 3), SOFT)


def sparkle(cx, cy, r=6):
    """A four-pointed star: two cells thick, tapering to one-cell tips."""
    for d in range(-r, r + 2):
        thick = abs(d - 0.5) < r - 1.5
        for k in ((0, 1) if thick else (0,)):
            put(cx + d, cy + k if thick else cy, BLUE, False) if abs(d - 0.5) < r else None
            put(cx + k if thick else cx, cy + d, BLUE, False) if abs(d - 0.5) < r else None


# --- the hill: a mound with a curved underside, outlined so it reads as land, not a cut-off band ----------------
hill_top, hill_bottom = {}, {}
for x in range(6, W - 6):
    u = abs(x + 0.5 - W / 2) / (W / 2 - 6)
    top = round(206 - 34 * (1 - u ** 3.2))
    bottom = round(208 - 7 * u ** 6)
    if bottom - top < 2:
        continue
    hill_top[x], hill_bottom[x] = top, bottom
for x, top in hill_top.items():
    bottom = hill_bottom[x]
    end = x - 1 not in hill_top or x + 1 not in hill_top
    for y in range(top, bottom + 1):
        c = BASE
        if end or y == bottom or y == top:
            c = MID if y == bottom or end else SOFT                    # outline: soft on top, darker below
        elif y == top + 1:
            c = LIGHT                                                    # sunlit rim
        elif y == bottom - 1:
            c = SOFT                                                     # shade along the underside
        elif y > top + 3 and (x * 13 + y * 7) % 61 in (0, 1) and y % 3 == 0:
            c = SOFT                                                     # grass tufts
        put(x, y, c, solid=False)
for y in range(186, 206):                                                # the path down from the gate
    spread = (y - 186) // 3
    rect(182 - spread, y, 212 + spread, y + 1, LIGHT, solid=False)
    if y % 4 == 1:
        rect(186 - spread, y, 190 - spread, y + 1, BASE, solid=False)
        rect(204 + spread, y, 208 + spread, y + 1, BASE, solid=False)

# --- bushes ---------------------------------------------------------------------------------------------
bush(78, 166, 15)
bush(42, 190, 8)
bush(328, 178, 13)
bush(361, 194, 9)

# --- side walls (set back, in shade) ---------------------------------------------------------------------
stone(140, 140, 158, 182, tone=2)
merlons(140, 158, 140, tooth=5, gap=3, height=4, tone=2)
stone(238, 132, 266, 182, tone=2)
merlons(238, 266, 132, tooth=5, gap=3, height=4, tone=2)
rect(250, 148, 254, 156, INK, solid=False)                               # arrow slit

# --- towers ---------------------------------------------------------------------------------------------
stone(114, 106, 148, 182, rounded=True)                                  # left tower
cone(131, 66, 106, 18)
rect(130, 58, 132, 68, INK, solid=False)
window(124, 132, 12, 20)

stone(262, 122, 296, 182, rounded=True)                                  # right tower
cone(279, 82, 122, 18)
rect(278, 74, 280, 84, INK, solid=False)
window(273, 132, 12, 20)

stone(188, 72, 228, 132, rounded=True)                                   # the keep
cone(208, 28, 72, 22)
window(202, 78, 12, 20)
rect(205, 2, 207, 30, INK, solid=False)                                  # flag pole
rect(204, 1, 208, 3, GOLD, solid=False)                                  # finial

# --- the flag, waving -----------------------------------------------------------------------------------
for x in range(207, 245):
    t = x - 207
    wave = round(2.4 * np.sin(t / 6.0))
    top, bottom = 7 + wave, 24 + wave - (t // 9)
    for y in range(top, max(top + 6, bottom)):
        c = GOLD_HI if np.sin(t / 6.0 + 0.9) > 0 else GOLD
        if y == top:
            c = GOLD_HI
        put(x, y, c, solid=False)
for y in range(9, 20):                                                     # swallowtail notch
    depth = 5 - abs(y - 14)
    for x in range(245 - max(depth, 0), 245):
        img[y + round(2.4 * np.sin(38 / 6.0)), x] = -1

# --- the gatehouse --------------------------------------------------------------------------------------
stone(156, 118, 240, 188)
merlons(156, 240, 118, tooth=7, gap=5, height=6)
stone(170, 104, 234, 120, tone=1)                                        # inner wall behind the battlements
merlons(170, 234, 104, tooth=6, gap=4, height=5, tone=1)
stone(156, 118, 240, 188)                                                # front face over the inner wall's foot
merlons(156, 240, 118, tooth=7, gap=5, height=6)
for y in range(150, 188):                                                 # arched gate with a portcullis
    half = 15 if y >= 158 else round((64 - (158 - y) ** 2) ** 0.5 * 15 / 8) if y > 150 else 0
    for x in range(197 - half, 197 + half):
        frame = x in (197 - half, 197 + half - 1) or (y < 158 and abs(x - 197) >= half - 2)
        bar = (x - 182) % 6 == 0 or (y - 150) % 7 == 0
        put(x, y, DEEP if frame else MID if bar and y > 152 else INK, solid=False)
rect(178, 188, 216, 190, SOFT, solid=False)                               # threshold
window(164, 132, 8, 14)
window(222, 132, 8, 14)

# --- outline the stone and bushes -----------------------------------------------------------------------
outline = np.zeros_like(structure)
for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
    outline |= np.roll(np.roll(structure, dy, 0), dx, 1)
outline &= ~structure & (img != INK) & (img != GOLD) & (img != GOLD_HI) & (img != BLUE)
img[outline] = DEEP

# --- sparkles (drawn last so the outline never touches them) -----------------------------------------------
sparkle(86, 56)
sparkle(328, 76, 5)


def paths() -> list[dict]:
    out = []
    for ci, color in enumerate(COLORS):
        rects, active = [], {}
        for y in range(H):
            runs, x = set(), 0
            while x < W:
                if img[y, x] != ci:
                    x += 1
                    continue
                start = x
                while x < W and img[y, x] == ci:
                    x += 1
                runs.add((start, x - start))
            for key in list(active):
                if key not in runs:
                    rects.append((*key, *active.pop(key)))
            for key in runs:
                if key in active:
                    active[key][1] += 1
                else:
                    active[key] = [y, 1]
        rects += [(*key, *v) for key, v in active.items()]
        d = "".join(f"M{x * STEP} {y * STEP}h{w * STEP}v{h * STEP}h{-w * STEP}z" for x, w, y, h in sorted(rects))
        if d:
            out.append({"fill": color, "d": d})
    return out


castle = {"viewBox": [0, 0, W * STEP, H * STEP], "placement": [232, 140], "grid": STEP, "paths": paths()}
blob = json.dumps(castle, separators=(",", ":"))
(ROOT / "src" / "castle.json").write_text(json.dumps(castle, indent=2) + "\n")
(ROOT / "src" / "castle-data.mjs").write_text(f"export const CASTLE = {blob};\n")
print(f"{sum(p['d'].count('M') for p in castle['paths'])} rectangles in {len(castle['paths'])} colors")
