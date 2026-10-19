"""Build the dashboard's character art with three expressions.

  default  square eyes                (most of the game)
  happy    the original ^ ^ eyes      (their team won)
  sad      -_- flat eyes and mouth    (their team lost)

Input: the extracted pose frames in assets/characters_src/frames (treated as
image data only). Output: avalon/dashboard/static/chars/<role>_<expr>_{idle,talk}.webp
and a contact sheet assets/expressions_sheet.png for visual checking.

Run: .tools/venv/bin/python assets/make_expressions.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "characters_src" / "frames"
OUT = ROOT / "avalon" / "dashboard" / "static" / "chars"
ROLES = ["merlin", "percival", "loyal_servant", "mordred", "morgana", "assassin", "oberon"]
SIZE = 256


def dominant_face_color(rgb, alpha, box):
    x0, y0, x1, y1 = box
    region = rgb[y0:y1, x0:x1][alpha[y0:y1, x0:x1]]
    q = (region // 24) * 24 + 12
    vals, counts = np.unique(q.reshape(-1, 3), axis=0, return_counts=True)
    return vals[np.argmax(counts)].astype(int)


def find_eyes(img: np.ndarray):
    """Return two eye components (bbox, mask, color) or raise; widen the face-colour tolerance as needed."""
    for thr in (48, 64, 80, 96):
        try:
            return _find_eyes(img, thr)
        except RuntimeError:
            continue
    raise RuntimeError("eyes not found")


def _find_eyes(img: np.ndarray, thr: int):
    rgb = img[..., :3].astype(int)
    alpha = img[..., 3] > 100
    ys, xs = np.where(alpha)
    bx0, bx1, by0, by1 = xs.min(), xs.max(), ys.min(), ys.max()
    w, h = bx1 - bx0, by1 - by0
    # the face sits in the middle band of the body
    head = (bx0 + int(0.25 * w), by0 + int(0.22 * h), bx0 + int(0.75 * w), by0 + int(0.62 * h))
    face = dominant_face_color(rgb, alpha, head)
    dist = np.sqrt(((rgb - face) ** 2).sum(-1))
    face_mask = (dist < thr) & alpha
    filled = ndimage.binary_fill_holes(face_mask)
    holes = filled & ~face_mask
    lab, n = ndimage.label(holes)
    comps = []
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        if sl is None:
            continue
        m = lab[sl] == i
        area = int(m.sum())
        hh, ww = m.shape
        cy, cx = (sl[0].start + sl[0].stop) / 2, (sl[1].start + sl[1].stop) / 2
        if area < 120 or area > 9000 or ww < 18 or hh < 8 or ww > 160 or hh > 110:
            continue
        if ww < 0.9 * hh:          # eyes are chevrons, at least as wide as tall (visor slits are not)
            continue
        if not (head[1] - 0.1 * h <= cy <= head[3] + 0.05 * h):
            continue
        comps.append({"sl": sl, "mask": m, "area": area, "cx": cx, "cy": cy, "w": ww, "h": hh, "lab": i})
    best, score = None, 1e9
    for i in range(len(comps)):
        for j in range(i + 1, len(comps)):
            a, b = comps[i], comps[j]
            if abs(a["cy"] - b["cy"]) > 18 or abs(a["cx"] - b["cx"]) < 40 or abs(a["cx"] - b["cx"]) > 0.6 * w:
                continue
            ra = max(a["area"], b["area"]) / max(1, min(a["area"], b["area"]))
            if ra > 2.2:
                continue
            s = abs(a["cy"] - b["cy"]) + 20 * (ra - 1) + abs(a["w"] - b["w"]) - 0.002 * (a["area"] + b["area"])
            if s < score:
                best, score = (a, b), s
    if not best:
        raise RuntimeError("eyes not found")
    eyes = sorted(best, key=lambda c: c["cx"])
    for e in eyes:
        px = rgb[e["sl"]][e["mask"]]
        e["color"] = tuple(int(v) for v in np.median(px, axis=0))
    return eyes, face, lab


def edit(img: np.ndarray, expr: str, ref: tuple[int, int] | None = None) -> Image.Image:
    """ref = (eye width, eye height) measured once per character, so eyes keep one size."""
    if expr == "happy":
        return Image.fromarray(img)
    eyes, face, lab = find_eyes(img)
    out = img.copy()
    rgb = out[..., :3]
    for e in eyes:                                      # erase the chevrons
        m = np.zeros(lab.shape, bool)
        m[e["sl"]] = e["mask"]
        m = ndimage.binary_dilation(m, iterations=3)
        ring = ndimage.binary_dilation(m, iterations=6) & ~m
        local = np.median(rgb[ring & (out[..., 3] > 100)], axis=0) if ring.any() else face
        rgb[m] = local.astype(np.uint8)
    im = Image.fromarray(out)
    d = ImageDraw.Draw(im)
    ew, eh = ref or (int(np.mean([e["w"] for e in eyes])), int(np.mean([e["h"] for e in eyes])))
    col = eyes[0]["color"] + (255,)
    for e in eyes:
        cx, cy = e["cx"], e["cy"]
        if expr == "default":
            side = max(14, int(0.62 * min(ew, eh * 1.6)))
            d.rectangle([cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2], fill=col)
        elif expr == "sad":
            bw, bh = int(0.95 * ew), max(9, int(0.28 * eh))
            d.rectangle([cx - bw / 2, cy - bh / 2 + eh * 0.15, cx + bw / 2, cy + bh / 2 + eh * 0.15], fill=col)
    if expr == "sad":                                   # the "_" between the eyes
        mx = (eyes[0]["cx"] + eyes[1]["cx"]) / 2
        my = max(e["cy"] for e in eyes) + eh * 0.95
        mw, mh = int(0.55 * ew), max(7, int(0.2 * eh))
        d.rectangle([mx - mw / 2, my - mh / 2, mx + mw / 2, my + mh / 2], fill=col)
    return im


def talk_frames(im: Image.Image) -> list[Image.Image]:
    """Speaking loop from a single pose: stretch/squash anchored at the feet plus a small bob.

    No pose switching, so arms and hands stay put and the eyes keep their size.
    """
    w, h = im.size
    out = []
    for sy, bob in [(1.0, 0), (1.018, -4), (1.032, -8), (1.018, -4), (1.0, 0), (0.988, 2)]:
        sx = 1 / sy ** 0.5
        nw, nh = int(w * sx), int(h * sy)
        frame = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        scaled = im.resize((nw, nh), Image.LANCZOS)
        frame.alpha_composite(scaled, ((w - nw) // 2, h - nh + bob - 0))
        out.append(frame)
    return out


def to_webp(frames: list[Image.Image], path: Path, animated: bool):
    with tempfile.TemporaryDirectory() as td:
        for k, f in enumerate(frames):
            f.resize((SIZE, SIZE), Image.LANCZOS).save(f"{td}/f{k:02d}.png")
        if animated:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", "6", "-i", f"{td}/f%02d.png",
                            "-c:v", "libwebp_anim", "-q:v", "80", "-loop", "0", str(path)], check=True)
        else:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", f"{td}/f00.png",
                            "-c:v", "libwebp", "-q:v", "85", str(path)], check=True)


def gif_frames(role: str):
    from PIL import ImageSequence
    gif = Image.open(ROOT / "assets" / "characters_src" / "gifs" / f"{role}.gif")
    out = []
    for f in ImageSequence.Iterator(gif):
        out.append((np.array(f.convert("RGBA")), int(f.info.get("duration") or 200)))
    return out


def save_anim(frames: list[Image.Image], durations: list[int], path: Path):
    small = [f.resize((SIZE, SIZE), Image.LANCZOS) for f in frames]
    small[0].save(path, save_all=True, append_images=small[1:], duration=durations, loop=0, quality=80, method=4)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    sheet = Image.new("RGBA", (4 * 3 * 96, len(ROLES) * 96), (245, 240, 230, 255))
    problems = []
    for r, role in enumerate(ROLES):
        poses = [np.array(Image.open(SRC / f"{role}_pose_{k}.png").convert("RGBA")) for k in range(4)]
        eyes0, _, _ = find_eyes(poses[0])
        ref = (int(np.mean([e["w"] for e in eyes0])), int(np.mean([e["h"] for e in eyes0])))
        for e_i, expr in enumerate(["default", "happy", "sad"]):
            try:
                base = edit(poses[0], expr, ref)
            except RuntimeError as err:
                problems.append(f"{role} {expr}: {err}")
                base = Image.fromarray(poses[0])
            # talking: the artist's own 12-frame GIF (real hand movement), eyes edited per frame at one size
            frames, durs, last = [], [], None
            for k, (fr, dur) in enumerate(gif_frames(role)):
                try:
                    last = edit(fr, expr, ref)
                except RuntimeError:
                    problems.append(f"{role} gif frame {k} {expr}: reused previous frame")
                    last = last or Image.fromarray(fr)
                frames.append(last)
                durs.append(dur)
            for k in range(4):
                sheet.alpha_composite(frames[k * 3].resize((96, 96), Image.LANCZOS), ((e_i * 4 + k) * 96, r * 96))
            to_webp([base], OUT / f"{role}_{expr}_idle.webp", animated=False)
            save_anim(frames, durs, OUT / f"{role}_{expr}_talk.webp")
    sheet.save(ROOT / "assets" / "expressions_sheet.png")
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
