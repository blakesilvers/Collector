"""Clean-up and checks for the ink (whiteboard) style.

The picture model draws "ink on paper" as a photograph of a sketchbook: warm grey paper, a vignette, the page
edge and binding, a strip of wooden desk in a corner, and stray colours beside the orange accent. `normalize`
turns that back into a drawing on clean white paper for the video; `qc` measures what is left so the owner can be
told which pictures may be worth redrawing. Both work on a copy: cached picture files are never modified.

Everything here is plain numpy on a small working copy for the measurements, applied at full size."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PIL import Image

# The one accent colour of the style is a burnt orange; browns share its hue. Hues inside ACCENT_HUES keep their
# colour, hues further out than ACCENT_RAMP degrees lose it entirely, with a linear ramp between.
ACCENT_HUES = (17.0, 38.0)
ACCENT_RAMP = 5.0
WHITE_POINT = 0.93          # after flattening, anything at least this bright (relative to the paper) becomes white
BORDER = 0.02               # the border strip qc measures: 2% of the shorter side
MARGIN_FADE = 0.04          # the outer margin gently faded to white: 4% of the shorter side
EDGE_SEARCH = 0.06          # how deep (share of the side) a page edge or desk strip is looked for
WORK_SIZE = 720             # long side of the working copy used for measurements


# ---------- small numpy helpers ----------

def _box(a: np.ndarray, r: int, axis: int) -> np.ndarray:
    if r <= 0:
        return a
    n = a.shape[axis]
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float64)
    hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return ((hi - lo) / (2 * r + 1)).astype(np.float32)


def _blur(a: np.ndarray, r: int) -> np.ndarray:
    """Near-Gaussian blur: three box passes along each axis."""
    for _ in range(3):
        a = _box(_box(a, r, 0), r, 1)
    return a


def _maxf(a: np.ndarray, r: int) -> np.ndarray:
    out = a.copy()
    for axis in (0, 1):
        src = out.copy()
        n = src.shape[axis]
        for k in range(1, r + 1):
            idx_a, idx_b = np.arange(k, n), np.arange(0, n - k)
            fwd = np.take(src, idx_a, axis=axis)
            bwd = np.take(src, idx_b, axis=axis)
            sl_lo = [slice(None)] * a.ndim
            sl_hi = [slice(None)] * a.ndim
            sl_lo[axis], sl_hi[axis] = slice(0, n - k), slice(k, n)
            out[tuple(sl_lo)] = np.maximum(out[tuple(sl_lo)], fwd)
            out[tuple(sl_hi)] = np.maximum(out[tuple(sl_hi)], bwd)
    return out


def _lum(rgb: np.ndarray) -> np.ndarray:
    return rgb @ np.array([0.2126, 0.7152, 0.0722], np.float32)


def _hue_sat(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """HSV hue in degrees and saturation (0..1)."""
    mx, mn = rgb.max(-1), rgb.min(-1)
    d = mx - mn
    sat = np.where(mx > 1e-4, d / np.maximum(mx, 1e-4), 0.0)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    dd = np.maximum(d, 1e-6)
    h = np.where(mx == r, ((g - b) / dd) % 6, np.where(mx == g, (b - r) / dd + 2, (r - g) / dd + 4)) * 60.0
    return np.where(d > 1e-6, h, 0.0).astype(np.float32), sat.astype(np.float32)


def _accent_weight(hue: np.ndarray) -> np.ndarray:
    lo, hi = ACCENT_HUES
    below = np.clip((hue - (lo - ACCENT_RAMP)) / ACCENT_RAMP, 0, 1)
    above = np.clip(((hi + ACCENT_RAMP) - hue) / ACCENT_RAMP, 0, 1)
    return np.minimum(below, above).astype(np.float32)


def _small(img: Image.Image) -> tuple[np.ndarray, float]:
    """Working copy (long side WORK_SIZE) as float RGB 0..1, and its scale relative to the original."""
    W, H = img.size
    s = min(1.0, WORK_SIZE / max(W, H))
    im = img.convert("RGB")
    if s < 1.0:
        im = im.resize((max(1, round(W * s)), max(1, round(H * s))), Image.BOX)
    return np.asarray(im, np.float32) / 255.0, s


def _up(a: np.ndarray, W: int, H: int) -> np.ndarray:
    """Resize a float map (H, W) or (H, W, C) to full size, bilinear."""
    if a.ndim == 2:
        return np.asarray(Image.fromarray(a.astype(np.float32), "F").resize((W, H), Image.BILINEAR), np.float32)
    return np.stack([_up(a[..., c], W, H) for c in range(a.shape[2])], -1)


def _dist_to_border(H: int, W: int) -> np.ndarray:
    ys = np.minimum(np.arange(H), np.arange(H)[::-1])[:, None]
    xs = np.minimum(np.arange(W), np.arange(W)[::-1])[None, :]
    return np.minimum(ys, xs).astype(np.float32)


# ---------- page edges, desk and shadows at the border ----------

def _side_depths(lum: np.ndarray, paper: float, segments: int = 12, run: int = 4) -> np.ndarray | None:
    """For the side at column 0 of `lum` (rows run along the side): how many pixels in from the border the page
    begins, per row. None if no off-page strip is found. Rows are split into segments; in each, the median
    brightness at each depth is followed inward until it reaches paper and stays there for `run` pixels.
    Segments where the drawing itself reaches the border (no paper within reach) take the median of the others."""
    H, W = lum.shape
    reach = max(run + 2, int(W * EDGE_SEARCH))
    edges = np.linspace(0, H, segments + 1).astype(int)
    found: list[float | None] = []
    for a, b in zip(edges[:-1], edges[1:]):
        prof = np.median(lum[a:b, :reach + run], axis=0)
        ok = prof >= paper - 0.045
        depth = None
        for d in range(reach):
            if ok[d:d + run].all():
                depth = d
                break
        found.append(depth)
    known = [d for d in found if d is not None]
    if len(known) < segments / 3 or max(known) == 0:
        return None
    fill = float(np.median(known))
    vals = np.array([fill if d is None else d for d in found], np.float32)
    centres = (edges[:-1] + edges[1:]) / 2.0
    return np.interp(np.arange(H), centres, vals).astype(np.float32)


def off_page_mask(rgb: np.ndarray) -> np.ndarray:
    """1 where a pixel is outside the drawn page (desk, binding, the neighbouring page, a page edge), found only in
    strips that touch the border. Same size as `rgb` (a working copy)."""
    lum = _lum(rgb)
    H, W = lum.shape
    paper = float(np.percentile(lum, 90))
    mask = np.zeros((H, W), np.float32)
    views = {
        "left": lum, "right": lum[:, ::-1], "top": lum.T, "bottom": lum[::-1].T,
    }
    for side, view in views.items():
        depth = _side_depths(view, paper)
        if depth is None:
            continue
        n = view.shape[1]
        cols = np.arange(n, dtype=np.float32)[None, :]
        # 1 out to the page edge, then a two-pixel feather (plus one pixel to swallow the edge line itself)
        m = np.clip((depth[:, None] + 2.0 - cols) / 2.0, 0, 1)
        if side == "left":
            mask = np.maximum(mask, m)
        elif side == "right":
            mask = np.maximum(mask, m[:, ::-1])
        elif side == "top":
            mask = np.maximum(mask, m.T)
        else:
            mask = np.maximum(mask, m.T[::-1])
    return mask


# ---------- the clean-up ----------

def normalize(img: Image.Image) -> Image.Image:
    """A drawing on clean white paper: paper flattened to white, off-page strips at the border (desk, binding,
    page edges, their shadows) and a thin outer margin faded to white, colours other than the orange accent and
    its browns pulled to grey. Line work and its tones are kept. Returns a new RGB image of the same size."""
    full = np.asarray(img.convert("RGB"), np.float32) / 255.0
    H, W = full.shape[:2]
    small, s = _small(img)
    h, w = small.shape[:2]

    # 1. where the page is not
    off = off_page_mask(small)

    # 2. the paper's own tone and lighting, estimated from paper-like pixels only and interpolated across the
    # drawing (normalized convolution), so dark fills and wash keep their tone
    lum = _lum(small)
    r = max(2, round(min(h, w) / 70))
    local_paper = _blur(_maxf(lum, r), 3 * r)
    _, sat = _hue_sat(small)
    level = float(np.percentile(lum, 90))
    # bright for its neighbourhood, and bright at all: inside a large dark fill the neighbourhood is dark too
    paper = ((lum >= local_paper - 0.06) & (lum >= 0.7 * level) & (sat < 0.2) & (off < 0.5)).astype(np.float32)
    R = max(4, round(min(h, w) / 16))
    weight = _blur(paper, R)
    field_ = _blur(small * paper[..., None], R) / np.maximum(weight, 1e-3)[..., None]
    field_ = np.where(weight[..., None] > 0.02, field_, level)
    field_ = np.clip(field_, 0.6, 1.0)  # photographed paper is never darker than this; a darker estimate is a fill

    # 3. flatten and set the white point
    out = full / _up(field_, W, H)
    out = np.clip(out / WHITE_POINT, 0.0, 1.0)

    # 4. colours other than the accent go to grey
    hue, _ = _hue_sat(out)
    y = _lum(out)[..., None]
    out = y + (out - y) * _accent_weight(hue)[..., None]

    # 5. off-page strips and a thin margin go to white
    fade = np.maximum(_up(off, W, H), 0.0)
    depth = _dist_to_border(H, W) / max(1.0, MARGIN_FADE * min(H, W))
    margin = 1.0 - np.clip(depth, 0, 1) ** 2 * (3 - 2 * np.clip(depth, 0, 1))
    fade = np.maximum(fade, margin)[..., None]
    out = out + (1.0 - out) * fade
    return Image.fromarray((np.clip(out, 0, 1) * 255.0 + 0.5).astype(np.uint8), "RGB")


# ---------- the checks ----------

@dataclass
class Quality:
    grey_wash: float          # share of the picture covered by mid-grey shading (wash or dense hatching)
    off_palette: float        # share of the picture in saturated colours other than the orange accent and browns
    border_lum: float         # mean brightness (0..1, relative luminance of sRGB values) of the outer 2% strip
    halo: bool                # an orange blob in the upper third next to a head-like shape (best effort)
    halo_boxes: list[tuple[int, int, int, int]] = field(default_factory=list)  # (x0, y0, x1, y1) in the image

    def reasons(self) -> list[str]:
        """Plain-English reasons this picture may be worth redrawing (empty if none)."""
        out = []
        if self.grey_wash > 0.30:
            out.append(f"heavy grey shading over {self.grey_wash:.0%} of the picture")
        if self.border_lum < 0.97:
            out.append("the edges are still not clean white paper (the drawing runs to the edge, or a desk is in shot)")
        if self.halo:
            out.append("orange near the top of a head; check it does not look like a halo")
        return out


def _components(mask: np.ndarray) -> list[np.ndarray]:
    """Connected regions (4-neighbour) of a small boolean mask, as arrays of (y, x)."""
    H, W = mask.shape
    seen = np.zeros_like(mask, bool)
    regions = []
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys, xs):
        if seen[y0, x0]:
            continue
        stack = [(y0, x0)]
        seen[y0, x0] = True
        pts = []
        while stack:
            y, x = stack.pop()
            pts.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        regions.append(np.array(pts))
    return regions


def _enclosed(ink: np.ndarray) -> np.ndarray:
    """Pixels not reachable from the window's edge without crossing ink: the insides of closed outlines."""
    free = ~ink
    reach = np.zeros_like(free)
    reach[0, :], reach[-1, :], reach[:, 0], reach[:, -1] = free[0, :], free[-1, :], free[:, 0], free[:, -1]
    while True:
        grown = reach.copy()
        grown[1:] |= reach[:-1]
        grown[:-1] |= reach[1:]
        grown[:, 1:] |= reach[:, :-1]
        grown[:, :-1] |= reach[:, 1:]
        grown &= free
        if (grown == reach).all():
            return free & ~reach
        reach = grown


def _halo_boxes(rgb: np.ndarray, scale: float) -> list[tuple[int, int, int, int]]:
    """Orange blobs in the upper third that wrap round a light, roundish, closed ink outline about a head's size
    (orange on at least half the sides, including all three sides above it). Best effort: see `Quality.halo`."""
    H, W = rgb.shape[:2]
    hue, sat = _hue_sat(rgb)
    val = rgb.max(-1)
    orange = (hue >= 10) & (hue <= 50) & (sat > 0.45) & (val > 0.55)
    lum = _lum(rgb)
    boxes = []
    area_min, area_max = 0.0004 * H * W, 0.04 * H * W
    for pts in _components(orange):
        if not (area_min <= len(pts) <= area_max):
            continue
        y0, x0 = pts.min(0)
        y1, x1 = pts.max(0)
        if (y0 + y1) / 2 > H / 3:
            continue
        bh, bw = y1 - y0 + 1, x1 - x0 + 1
        wy0, wy1 = max(0, y0 - bh), min(H, y1 + bh + 1)
        wx0, wx1 = max(0, x0 - bw), min(W, x1 + bw + 1)
        window = lum[wy0:wy1, wx0:wx1]
        glow = orange[wy0:wy1, wx0:wx1]
        yy, xx = np.mgrid[0:wy1 - wy0, 0:wx1 - wx0]
        for hole in _components(_enclosed(window < 0.45)):
            if len(hole) < 12 or window[hole[:, 0], hole[:, 1]].mean() < 0.6:  # a face is light; a pot's mouth is dark
                continue
            hy0, hx0 = hole.min(0)
            hy1, hx1 = hole.max(0)
            hh, hw = hy1 - hy0 + 1, hx1 - hx0 + 1
            if not (0.6 <= hh / hw <= 1.7 and len(hole) / (hh * hw) >= 0.55
                    and 0.25 * max(bh, bw) <= max(hh, hw) <= 2.5 * max(bh, bw)):
                continue
            cy, cx, r = (hy0 + hy1) / 2, (hx0 + hx1) / 2, max(hh, hw) / 2
            d = np.hypot(yy - cy, xx - cx)
            ring = glow & (d >= 0.8 * r) & (d <= 2.2 * r)
            sector = ((np.degrees(np.arctan2(-(yy - cy), xx - cx)) + 360 + 22.5) % 360 // 45).astype(int)  # 2 = up
            hits = {k for k in range(8) if (ring & (sector == k)).sum() >= 3}
            if len(hits) >= 4 and {1, 2, 3} <= hits:  # above it on all three upper sides, as a halo or rays are
                boxes.append(tuple(int(round(v / scale)) for v in (x0, y0, x1 + 1, y1 + 1)))
                break
    return boxes


def qc(img: Image.Image) -> Quality:
    rgb, s = _small(img)
    H, W = rgb.shape[:2]
    lum = _lum(rgb)
    hue, sat = _hue_sat(rgb)
    soft = _blur(lum, 1)
    grey = (soft >= 0.25) & (soft <= 0.85) & (sat < 0.25)
    off = (sat > 0.25) & (rgb.max(-1) > 0.15) & (_accent_weight(hue) <= 0.0)
    b = max(1, round(BORDER * min(H, W)))
    strip = np.concatenate([lum[:b].ravel(), lum[-b:].ravel(), lum[b:-b, :b].ravel(), lum[b:-b, -b:].ravel()])
    boxes = _halo_boxes(rgb, s)
    return Quality(grey_wash=float(grey.mean()), off_palette=float(off.mean()), border_lum=float(strip.mean()),
                   halo=bool(boxes), halo_boxes=boxes)
