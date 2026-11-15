"""The drawn build for whiteboard (ink) pictures: each new picture is drawn onto blank paper, item by item, lines
first, then the grey wash, then the orange accents, the way a whiteboard illustrator builds a page. Now and then
(see choose_puffs) a few countable objects arrive in a small hand-drawn puff instead. There is never a drawing
hand on screen.

Nothing here invents or changes a picture: every frame is a blend between the finished picture and the blank
paper under it, so the last frame of a build is the finished picture itself, pixel for pixel.

Per picture, once (and cached by content hash beside the pictures):
  * the paper under the drawing (the sheet with the marks lifted off),
  * each pixel's class: line (dark marker strokes and their soft rims), wash, or orange accent,
  * an automatic split into items (no model is asked), judged; a poor split is replaced by the whole picture
    drawn as one item outward from its main subject,
  * the order the line pixels go down in. With the optional packages (opencv-python-headless and scikit-image,
    the "strokes" extra) the lines are traced into centre-line pen strokes and drawn stroke by stroke, outlines
    before details before hatching. Without them the ink flows outward along the lines from where each item
    starts (the simpler pixel-flow build), and a plain note says so; nothing fails.
Per shot (cheap): when each item enters, which gives each pixel a start time. Per frame: one lookup table and a
composite inside the boxes of the items that are changing."""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

log = logging.getLogger(__name__)

VERSION = 1                 # bump whenever the analysis changes, so older cached analyses are not reused
LINE, WASH, ORANGE, PUFFED = 0, 1, 2, 3
BAND = 16000                # codes per class in the packed uint16 map (4 classes)
SOFT = np.array([0.12, 0.40, 0.30, 0.06], np.float32)  # seconds a pixel of each class takes to come in
TAIL = float(SOFT[:3].max())
FADE = 0.30                 # the previous picture clears to the new sheet's blank paper before drawing starts
PUFF_SECONDS = 0.5
PUFF_SETTLE = 0.22
K = 8                       # coarse grid factor for the split
STROKE_PACKAGES = "opencv-python-headless and scikit-image"
STROKES_NOTE = (f"Note: pictures are drawn on with the simpler ink flow, because the optional packages for pen-stroke "
                f"order ({STROKE_PACKAGES}) are not installed. To get pen strokes, run: pip install -e \".[strokes]\"")


# ---------------------------------------------------------------- small array helpers

def _lum(a: np.ndarray) -> np.ndarray:
    return a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722


def _box(a: np.ndarray, k: int) -> np.ndarray:
    """Block mean by k (the remainder is cropped)."""
    H, W = a.shape[:2]
    h, w = H // k, W // k
    return a[:h * k, :w * k].reshape(h, k, w, k, *a.shape[2:]).mean(axis=(1, 3))


def _blockmax(a: np.ndarray, k: int) -> np.ndarray:
    H, W = a.shape[:2]
    h, w = H // k, W // k
    return a[:h * k, :w * k].reshape(h, k, w, k).max(axis=(1, 3))


def _up(a: np.ndarray, W: int, H: int) -> np.ndarray:
    """Bilinear resize of a float map to (H, W)."""
    return np.asarray(Image.fromarray(a.astype(np.float32), "F").resize((W, H), Image.BILINEAR), np.float32)


def _maxf(a: np.ndarray, size: int) -> np.ndarray:
    """Separable square max filter (edge-padded)."""
    r = size // 2
    out = a.astype(np.float32)
    for ax in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[ax] = (r, r)
        p = np.pad(out, pad, mode="edge")
        n = out.shape[ax]
        out = np.max([np.take(p, range(k, k + n), axis=ax) for k in range(size)], axis=0)
    return out


def _blur(a: np.ndarray, r: int) -> np.ndarray:
    """Approximate Gaussian: three separable box passes of radius r (edge-padded)."""
    out = a.astype(np.float32)
    r = max(int(r), 1)
    for _ in range(3):
        for ax in (0, 1):
            pad = [(0, 0), (0, 0)]
            pad[ax] = (r + 1, r)
            c = np.cumsum(np.pad(out, pad, mode="edge"), axis=ax)
            n = out.shape[ax]
            out = (np.take(c, range(2 * r + 1, 2 * r + 1 + n), axis=ax) - np.take(c, range(0, n), axis=ax)) / (2 * r + 1)
    return out


def _rank(v: np.ndarray) -> np.ndarray:
    """Rank-equalise to 0..1 (ties broken by position, so the result is deterministic)."""
    out = np.empty(v.shape, np.float32)
    if v.size:
        out[np.argsort(v, kind="stable")] = np.linspace(0, 1, v.size, dtype=np.float32)
    return out


def _ease(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


# ---------------------------------------------------------------- paper and classes

def paper_small(S: np.ndarray, k: int = 8) -> np.ndarray:
    """The sheet with the drawing lifted off, at 1/k size: a coarse brightness closing with the sheet's interior
    filled in only from cells that are plain paper (so large dark washes leave no ghost behind). A desk strip
    along a whole side of the picture is kept."""
    H, W = S.shape[:2]
    small = Image.fromarray(S).resize((max(W // k, 1), max(H // k, 1)), Image.BOX)
    c = np.asarray(small.filter(ImageFilter.MaxFilter(15)).filter(ImageFilter.MinFilter(15)), np.float32)
    sm = np.asarray(small, np.float32)
    ref = np.percentile(sm.reshape(-1, 3), 85, axis=0)
    cl = c / 255.0
    csat = (cl.max(axis=2) - cl.min(axis=2)) / np.maximum(cl.max(axis=2), 1e-3)
    ref_lum = float(_lum(ref[None, None, :])[0, 0])
    sheet = _lum(c) > 0.80 * ref_lum
    h, w = sheet.shape
    yy, xx = np.mgrid[0:h, 0:w]
    edge = np.minimum(np.minimum(xx, w - 1 - xx) / w, np.minimum(yy, h - 1 - yy) / h) < 0.06
    cand = edge & ~sheet & (csat > 0.22)
    desk = np.zeros_like(cand)
    for arr, flip in ((cand, False), (cand[:, ::-1], True)):
        run = np.cumprod(arr, axis=1).astype(bool)
        if run[:, 0].mean() >= 0.4:
            desk |= run[:, ::-1] if flip else run
    for arr, flip in ((cand, False), (cand[::-1, :], True)):
        run = np.cumprod(arr, axis=0).astype(bool)
        if run[0, :].mean() >= 0.4:
            desk |= run[::-1, :] if flip else run
    ref_sat = float((ref.max() - ref.min()) / max(ref.max(), 1e-3))
    plain = (_lum(c) > 0.93 * ref_lum) & (csat < ref_sat + 0.06) & ~desk
    wgt = plain.astype(np.float32)
    den = _blur(wgt, 12)
    fill = np.stack([_blur(c[..., ch] * wgt, 12) for ch in range(3)], axis=2) / np.maximum(den, 1e-4)[..., None]
    fill = np.where(den[..., None] > 1e-3, fill, ref)
    paper = np.where(desk[..., None], c, fill)
    pim = Image.fromarray(np.clip(paper, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.5))
    return np.asarray(pim).copy()


def paper_full(small: np.ndarray, W: int, H: int) -> np.ndarray:
    return np.asarray(Image.fromarray(small).resize((W, H), Image.BILINEAR), np.uint8).copy()


def classify(S: np.ndarray, P: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(class map: LINE / WASH / ORANGE, darkness 0..1 relative to the paper)."""
    s = S.astype(np.float32) / 255.0
    p = P.astype(np.float32) / 255.0
    rel = _lum(s) / np.maximum(_lum(p), 1e-3)
    mx, mn = s.max(axis=2), s.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1e-3)
    r, g, b = s[..., 0], s[..., 1], s[..., 2]
    hue_ok = (r > g) & (g > b) & ((r - g) / np.maximum(r - b, 1e-3) > 0.25)
    orange = hue_ok & (sat > 0.52) & (mx > 0.50)
    core = (rel < 0.47) & ~orange
    near = np.asarray(Image.fromarray((core * 255).astype(np.uint8), "L").filter(ImageFilter.MaxFilter(3))) > 0
    line = core | (near & (rel < 0.86) & ~orange)
    cls = np.full(S.shape[:2], WASH, np.uint8)
    cls[orange] = ORANGE
    cls[line] = LINE
    return cls, np.clip(1.0 - rel, 0, 1)


def subject_seed(S: np.ndarray, dark: np.ndarray, k: int = 8) -> tuple[float, float]:
    """Main-subject guess in normalised (x, y): where colour and ink concentrate, weighted to the centre."""
    s = _box(S.astype(np.float32) / 255.0, k)
    mx, mn = s.max(axis=2), s.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1e-3)
    d = _box(dark, k)
    h, w = d.shape
    sal = _blur(0.6 * d + 1.0 * sat, max(w // 24, 1))
    ys, xs = np.mgrid[0:h, 0:w]
    centre = np.exp(-(((xs / w - 0.5) / 0.30) ** 2 + ((ys / h - 0.5) / 0.38) ** 2) / 2)
    j = int(np.argmax(sal * centre))
    return (j % w + 0.5) / w, (j // w + 0.5) / h


# ---------------------------------------------------------------- the item split

def _components(mask: np.ndarray) -> np.ndarray:
    """8-connected labels on a small grid. 0 = background."""
    h, w = mask.shape
    m = mask.ravel().tolist()
    lab = [0] * (h * w)
    n = 0
    for i0 in np.flatnonzero(mask.ravel()).tolist():
        if lab[i0]:
            continue
        n += 1
        lab[i0] = n
        stack = [i0]
        while stack:
            i = stack.pop()
            y, x = divmod(i, w)
            for dy in (-1, 0, 1):
                yy = y + dy
                if 0 <= yy < h:
                    for dx in (-1, 0, 1):
                        xx = x + dx
                        if 0 <= xx < w:
                            j = yy * w + xx
                            if m[j] and not lab[j]:
                                lab[j] = n
                                stack.append(j)
    return np.asarray(lab, np.int32).reshape(h, w)


def _dijkstra_labels(cost: np.ndarray, seeds: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Multi-source Dijkstra on a small 8-connected grid; seeds is an int map (0 = not a seed). (dist, label)."""
    h, w = cost.shape
    c = cost.ravel().tolist()
    dist = [math.inf] * (h * w)
    lab = [0] * (h * w)
    pq = []
    for i in np.flatnonzero(seeds.ravel()).tolist():
        dist[i] = 0.0
        lab[i] = int(seeds.flat[i])
        pq.append((0.0, i))
    heapq.heapify(pq)
    nb = [(-1, -1, 1.4142), (-1, 0, 1.0), (-1, 1, 1.4142), (0, -1, 1.0), (0, 1, 1.0), (1, -1, 1.4142), (1, 0, 1.0), (1, 1, 1.4142)]
    while pq:
        d0, i = heapq.heappop(pq)
        if d0 > dist[i]:
            continue
        y, x = divmod(i, w)
        ci, li = c[i], lab[i]
        for dy, dx, f in nb:
            yy, xx = y + dy, x + dx
            if 0 <= yy < h and 0 <= xx < w:
                j = yy * w + xx
                nd = d0 + f * 0.5 * (ci + c[j])
                if nd < dist[j]:
                    dist[j] = nd
                    lab[j] = li
                    heapq.heappush(pq, (nd, j))
    return np.asarray(dist, np.float32).reshape(h, w), np.asarray(lab, np.int32).reshape(h, w)


@dataclass
class Item:
    label: int
    area: int                               # ink pixels
    cx: float                               # centre of its ink, source pixels
    cy: float
    bbox: tuple[int, int, int, int]         # every pixel of the item (x0, y0, x1, y1)
    ink_bbox: tuple[int, int, int, int]     # its ink only (sizes the puff)
    setting: bool = False                   # ink far from every object: the background, drawn last
    seed: tuple[float, float] | None = None  # where the pen starts on it (the top of its core)


@dataclass
class Split:
    items: list[Item]
    label: np.ndarray        # full-size int map, every pixel belongs to one item
    dist: np.ndarray         # full-size distance from the item's own core along the ink
    coarse: np.ndarray       # coarse label map (0 = no ink)
    ink_density: np.ndarray  # coarse share of ink per cell


def _upsample_labels(lab: np.ndarray, W: int, H: int) -> np.ndarray:
    h, w = lab.shape
    big = np.asarray(Image.fromarray(lab.astype(np.int32), "I").resize((w * K, h * K), Image.NEAREST), np.int32)
    out = np.zeros((H, W), np.int32)
    out[:h * K, :w * K] = big[:H, :W]
    if h * K < H:
        out[h * K:, :] = out[h * K - 1:h * K, :]
    if w * K < W:
        out[:, w * K:] = out[:, w * K - 1:w * K]
    return out


def _items_from(L: np.ndarray, ink: np.ndarray, cores: np.ndarray, n: int) -> list[Item]:
    H, W = L.shape
    items = []
    ys, xs = np.mgrid[0:H:4, 0:W:4]
    Ls, inks = L[::4, ::4], ink[::4, ::4]
    for i in range(1, n + 2):
        whole = L == i
        if not whole.any():
            continue
        m = (Ls == i) & inks
        if not m.any():
            continue
        wy, wx = np.nonzero(whole)
        yy, xx = ys[m], xs[m]
        it = Item(label=i, area=int(m.sum()) * 16, cx=float(xx.mean()), cy=float(yy.mean()),
                  bbox=(int(wx.min()), int(wy.min()), int(wx.max()) + 1, int(wy.max()) + 1),
                  ink_bbox=(int(xx.min()), int(yy.min()), int(min(xx.max() + 4, W)), int(min(yy.max() + 4, H))),
                  setting=(i == n + 1))
        cy_, cx_ = np.nonzero(cores == i)
        if cy_.size:
            j = int(np.argmin(cy_ + 0.3 * np.abs(cx_ - cx_.mean())))
            it.seed = (float((cx_[j] + 0.5) * K), float((cy_[j] + 0.5) * K))
        items.append(it)
    return items


def split_items(cls: np.ndarray, dark: np.ndarray, max_items: int = 7, jump: float = 9.0, setting_dist: float = 34.0,
                gap: int = 5, merge_ratio: float = 0.45) -> Split:
    """Objects from where the illustrator put colour and ink; every inked cell joins the object it reaches most
    cheaply along the ink; ink far from every object is the setting. Biggest and most central first, setting last."""
    H, W = cls.shape
    ink = (cls == LINE) | (cls == ORANGE) | ((cls == WASH) & (dark > 0.12))
    colour = (cls == ORANGE) | ((cls == WASH) & (dark > 0.12))
    D = _box(ink.astype(np.float32), K)
    C = _box(colour.astype(np.float32), K)
    Ld = _box((cls == LINE).astype(np.float32), K)
    h, w = D.shape
    obj = _blur(C + 0.35 * Ld, 3)
    inked = D > 0.03
    vals = obj[inked]
    cores, thr = np.zeros((h, w), np.int32), 1.0
    for q in (0.72, 0.64, 0.56, 0.48, 0.40):
        thr = float(np.quantile(vals, q)) if vals.size else 1.0
        lab = _components(_maxf((obj > thr).astype(np.float32), gap) > 0.5)
        n = int(lab.max())
        areas = np.bincount(lab.ravel(), minlength=n + 1)
        keep = [i for i in range(1, n + 1) if areas[i] >= 0.004 * h * w]
        if len(keep) >= 3 or q == 0.40:
            keep = sorted(keep, key=lambda i: -areas[i])[:max_items]
            cores = np.zeros_like(lab)
            for k, i in enumerate(keep, start=1):
                cores[lab == i] = k
            break
    cost = 1.0 + (jump - 1.0) * (1.0 - np.clip(D * 6.0, 0, 1))
    if cores.max() == 0:  # nothing that looks like an object: one item from the centre
        cores[h // 2, w // 2] = 1
    dist, lab = _dijkstra_labels(cost, cores)
    # one figure in differently shaded clothes comes out as touching cores: merge neighbours whose shared border
    # runs through densely coloured cells (separate objects meet across paper or sparse lines)
    strong = obj > 0.85 * thr
    n = int(cores.max())
    parent = list(range(n + 1))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    shared: dict[tuple[int, int], int] = {}
    for a_, b_, s_ in ((lab[:, :-1], lab[:, 1:], strong[:, :-1] & strong[:, 1:]),
                       (lab[:-1, :], lab[1:, :], strong[:-1, :] & strong[1:, :])):
        sel = s_ & (a_ != b_) & (a_ > 0) & (b_ > 0)
        for x, y in zip(a_[sel].tolist(), b_[sel].tolist()):
            key = (min(x, y), max(x, y))
            shared[key] = shared.get(key, 0) + 1
    core_area = np.bincount(cores.ravel(), minlength=n + 1)
    for (x, y), cnt in sorted(shared.items()):
        if cnt >= merge_ratio * math.sqrt(max(min(core_area[x], core_area[y]), 1)):
            parent[find(x)] = find(y)
    roots = sorted({find(i) for i in range(1, n + 1)})
    remap = np.zeros(n + 1, np.int32)
    for k, r in enumerate(roots, start=1):
        for i in range(1, n + 1):
            if find(i) == r:
                remap[i] = k
    cores = remap[cores]
    lab = remap[lab]
    n = len(roots)

    def labels(cores_, dist_, lab_, n_):
        lab_ = np.where(inked, lab_, 0)
        lab_[inked & (dist_ > setting_dist)] = n_ + 1
        L_ = _upsample_labels(lab_, W, H)
        L_[~ink] = 0
        return lab_, L_
    coarse, L = labels(cores, dist, lab, n)
    # fold tiny objects (under 3% of the ink) into their neighbours by running again without them
    total = max(int(ink.sum()), 1)
    areas = np.bincount(L.ravel(), minlength=n + 2)
    small = [i for i in range(1, n + 1) if areas[i] < 0.03 * total]
    if small and n - len(small) >= 1:
        for i in small:
            cores[cores == i] = 0
        ids = sorted(set(np.unique(cores).tolist()) - {0})
        rem = np.zeros_like(cores)
        for k, i in enumerate(ids, start=1):
            rem[cores == i] = k
        cores = rem
        dist, lab = _dijkstra_labels(cost, cores)
        n = int(cores.max())
        coarse, L = labels(cores, dist, lab, n)
    # every pixel (paper too) belongs to the item of its cell, so each item's box covers all of it
    every = _upsample_labels(coarse, W, H)
    L = np.where(L > 0, L, every)
    L[L == 0] = n + 1
    items = _items_from(L, ink, cores, n)

    def score(it: Item) -> float:
        cw = math.exp(-(((it.cx / W - 0.5) / 0.35) ** 2 + ((it.cy / H - 0.5) / 0.45) ** 2) / 2)
        return it.area * (0.4 + cw)
    objs = sorted([it for it in items if not it.setting], key=score, reverse=True)
    items = objs + [it for it in items if it.setting]
    finite = np.isfinite(dist)
    dist = np.where(finite, dist, dist[finite].max() if finite.any() else 0.0)
    return Split(items=items, label=L, dist=_up(dist, W, H), coarse=coarse, ink_density=D)


def whole_split(S: np.ndarray, cls: np.ndarray, dark: np.ndarray, jump: float = 9.0) -> Split:
    """The fallback when the split is not trusted: the picture is one item, drawn outward from its main subject
    along the ink."""
    H, W = cls.shape
    seed = subject_seed(S, dark)
    ink = (cls == LINE) | (cls == ORANGE)
    D = _box(ink.astype(np.float32), K)
    h, w = D.shape
    cost = 1.0 + (jump - 1.0) * (1.0 - np.clip(D * 6.0, 0, 1))
    seeds = np.zeros((h, w), np.int32)
    seeds[min(int(seed[1] * h), h - 1), min(int(seed[0] * w), w - 1)] = 1
    dist, _ = _dijkstra_labels(cost, seeds)
    it = Item(label=1, area=int(ink.sum()), cx=seed[0] * W, cy=seed[1] * H, bbox=(0, 0, W, H), ink_bbox=(0, 0, W, H),
              seed=(seed[0] * W, seed[1] * H))
    return Split(items=[it], label=np.ones((H, W), np.int32), dist=_up(dist, W, H),
                 coarse=(D > 0.03).astype(np.int32), ink_density=D)


def judge_split(split: Split) -> tuple[bool, str]:
    """Whether the automatic split can be trusted to build the picture item by item. When in doubt, it cannot:
    the whole picture is then drawn as one item, which always looks sensible."""
    objs = [it for it in split.items if not it.setting]
    total = max(sum(it.area for it in split.items), 1)
    if len(objs) < 2:
        return False, "one main subject"
    setting = sum(it.area for it in split.items if it.setting) / total
    if setting > 0.45:
        return False, "most of the drawing is background"
    if max(it.area for it in objs) / total > 0.75:
        return False, "one item is nearly the whole drawing"
    # an object cut in two: two items meeting along a long border through inked cells
    lab, D = split.coarse, split.ink_density
    obj_ids = {it.label for it in objs}
    inked = D > 0.08
    shared: dict[tuple[int, int], int] = {}
    for a_, b_, s_ in ((lab[:, :-1], lab[:, 1:], inked[:, :-1] & inked[:, 1:]), (lab[:-1, :], lab[1:, :], inked[:-1, :] & inked[1:, :])):
        sel = s_ & (a_ != b_)
        for x, y in zip(a_[sel].tolist(), b_[sel].tolist()):
            if x in obj_ids and y in obj_ids:
                key = (min(x, y), max(x, y))
                shared[key] = shared.get(key, 0) + 1
    cells = np.bincount(lab.ravel(), minlength=max(obj_ids) + 2)
    worst = 0.0
    for (x, y), cnt in shared.items():
        worst = max(worst, cnt / math.sqrt(max(min(cells[x], cells[y]), 1)))
    if worst > CUT_IN_TWO:
        return False, f"an object looks cut in two ({worst:.2f})"
    return True, f"{len(objs)} items"


# Shared inked border / sqrt(smaller item's cells) above which two items look like one object cut in two. Set on
# the 21 pictures of the first 5-minute clip: every split judged sensible by eye scored 0.73 or less; two clear
# cuts (a seated figure halved, tablets and scrolls cut across) scored 0.81 and 0.89. Errs toward the whole picture.
CUT_IN_TWO = 0.78


# ---------------------------------------------------------------- pen strokes (optional packages)

_STROKES_OK: bool | None = None


def strokes_available() -> bool:
    """True when the optional packages for pen-stroke order can be imported."""
    global _STROKES_OK
    if _STROKES_OK is None:
        try:
            import cv2  # noqa: F401
            import scipy.ndimage  # noqa: F401
            import skimage.morphology  # noqa: F401
            _STROKES_OK = True
        except Exception:
            _STROKES_OK = False
    return _STROKES_OK


def extract_strokes(S: np.ndarray, ink_level: int = 95, max_width: int = 2000) -> list[tuple[np.ndarray, np.ndarray]]:
    """Centre-line pen strokes of the black line work, in the order a person might draw them: outlines (long or
    bold strokes), then details, then hatching, each tier pen-nearest first. Returns [(points (n, 2) as x, y in
    the picture's pixels, half-widths (n,))]. Needs opencv-python-headless and scikit-image."""
    import cv2
    from skimage.morphology import skeletonize

    H0, W0 = S.shape[:2]
    scale = min(1.0, max_width / W0)
    rgb = cv2.resize(S, (round(W0 * scale), round(H0 * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else S
    H, W = rgb.shape[:2]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    dark = gray < ink_level
    bh = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)))
    thin = (bh > 45) & (gray < 175)
    mask = dark | (thin & (hsv[..., 1] < 140))
    mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8)) > 0
    n_cc, cc, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    keep = stats[:, cv2.CC_STAT_AREA] >= 12
    keep[0] = False
    mask = keep[cc]
    if not mask.any():
        return []
    dist = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 3)
    skel = skeletonize(mask)
    Sk = skel.astype(np.uint8)
    nb = cv2.filter2D(Sk, -1, np.ones((3, 3), np.float32), borderType=cv2.BORDER_CONSTANT) - Sk
    nb = nb * Sk
    offs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    skl = Sk.tolist()

    def neighbours(y, x):
        for dy, dx in offs:
            yy, xx = y + dy, x + dx
            if 0 <= yy < H and 0 <= xx < W and skl[yy][xx]:
                yield yy, xx

    junc = (nb >= 3) & (Sk > 0)
    nj, jlab, _, cents = cv2.connectedComponentsWithStats(junc.astype(np.uint8), connectivity=8)
    jcent = {lab: (float(cents[lab][1]), float(cents[lab][0])) for lab in range(1, nj)}
    jl = jlab.tolist()
    seen = np.zeros((H, W), bool)
    segs: list[list] = []

    def trace_from(y, x, start_cluster):
        path = [(float(y), float(x))]
        seen[y, x] = True
        cy, cx = y, x
        while True:
            nxt, endc = None, 0
            for ny, nx in neighbours(cy, cx):
                if jl[ny][nx]:
                    if jl[ny][nx] != start_cluster or len(path) > 2:
                        endc = jl[ny][nx]
                    continue
                if not seen[ny, nx]:
                    nxt = (ny, nx)
                    break
            if nxt is None:
                return path, endc
            seen[nxt] = True
            cy, cx = nxt
            path.append((float(cy), float(cx)))

    for y, x in zip(*np.nonzero(junc)):
        c0 = jl[y][x]
        for ny, nx in neighbours(int(y), int(x)):
            if not jl[ny][nx] and not seen[ny, nx]:
                p, c1 = trace_from(ny, nx, c0)
                segs.append([[jcent[c0]] + p + ([jcent[c1]] if c1 else []), c0, c1])
    for y, x in zip(*np.nonzero((nb <= 1) & (Sk > 0))):
        if not seen[y, x]:
            p, c1 = trace_from(int(y), int(x), 0)
            segs.append([p + ([jcent[c1]] if c1 else []), 0, c1])
    for y, x in zip(*np.nonzero((Sk > 0) & ~seen & ~junc)):
        if not seen[y, x]:
            p, _ = trace_from(int(y), int(x), 0)
            segs.append([p + [p[0]], 0, 0])

    # join segments through a junction where the line carries straight on, so a stroke is a pen movement
    def outdir(p, at_start):
        q = p[:8] if at_start else p[::-1][:8]
        v = np.array([q[-1][1] - q[0][1], q[-1][0] - q[0][0]], float)
        nv = np.linalg.norm(v)
        return v / nv if nv else v

    by_cluster: dict[int, list[tuple[int, int]]] = {}
    for i, (p, c0, c1) in enumerate(segs):
        if c0:
            by_cluster.setdefault(c0, []).append((i, 0))
        if c1:
            by_cluster.setdefault(c1, []).append((i, 1))
    link: dict[tuple[int, int], tuple[int, int]] = {}
    for lab in sorted(by_cluster):
        lst = by_cluster[lab]
        pairs = []
        for a in range(len(lst)):
            for b in range(a + 1, len(lst)):
                (i, ei), (j, ej) = lst[a], lst[b]
                if i == j:
                    continue
                pairs.append((float(np.dot(outdir(segs[i][0], ei == 0), outdir(segs[j][0], ej == 0))), lst[a], lst[b]))
        pairs.sort()
        for c, ea, eb in pairs:
            if c > -0.75:
                break
            if ea in link or eb in link:
                continue
            link[ea], link[eb] = eb, ea
    used = [False] * len(segs)
    paths = []

    def chain_from(i, enter_end):
        out: list = []
        cur, ent = i, enter_end
        while True:
            used[cur] = True
            p = segs[cur][0] if ent == 0 else segs[cur][0][::-1]
            out.extend(p if not out else p[1:])
            nxt = link.get((cur, 1 - ent))
            if nxt is None or used[nxt[0]]:
                return out
            cur, ent = nxt

    for i in range(len(segs)):
        if used[i]:
            continue
        cur, ent, guard = i, 0, 0
        while True:
            prev = link.get((cur, ent))
            if prev is None or prev[0] == i or guard > len(segs):
                break
            cur, ent = prev[0], 1 - prev[1]
            guard += 1
        paths.append(chain_from(cur, ent))

    strokes = []
    for p in paths:
        if len(p) < 4:
            continue
        arr = np.array([[x, y] for y, x in p], np.float32)
        seglen = float(np.sum(np.linalg.norm(np.diff(arr, axis=0), axis=1)))
        if seglen < 6:
            continue
        approx = cv2.approxPolyDP(arr.reshape(-1, 1, 2), 0.8, False).reshape(-1, 2)
        if len(approx) < 2:
            continue
        d = np.r_[0, np.cumsum(np.linalg.norm(np.diff(approx, axis=0), axis=1))]
        npts = max(2, int(d[-1] / 4) + 1)
        t = np.linspace(0, d[-1], npts)
        rx, ry = np.interp(t, d, approx[:, 0]), np.interp(t, d, approx[:, 1])
        widths = dist[np.clip(np.round(ry).astype(int), 0, H - 1), np.clip(np.round(rx).astype(int), 0, W - 1)]
        widths = np.clip(widths.astype(np.float64), 0.8, 8.0)
        mw = float(np.median(widths))
        tier = 1 if (seglen > 120 or (mw > 2.6 and seglen > 40)) else 2 if (seglen > 35 or mw > 2.2) else 3
        strokes.append({"pts": np.c_[rx, ry], "w": widths, "tier": tier})
    ordered = []
    pen = np.array([W * 0.2, H * 0.2])
    for tier in (1, 2, 3):
        pool = [s for s in strokes if s["tier"] == tier]
        if not pool:
            continue
        starts = np.array([s["pts"][0] for s in pool])
        ends = np.array([s["pts"][-1] for s in pool])
        taken = np.zeros(len(pool), bool)
        for _ in range(len(pool)):
            d0 = np.linalg.norm(starts - pen, axis=1)
            d1 = np.linalg.norm(ends - pen, axis=1)
            d0[taken] = 1e18
            d1[taken] = 1e18
            k0, k1 = int(np.argmin(d0)), int(np.argmin(d1))
            k, flip = (k0, False) if d0[k0] <= d1[k1] else (k1, True)
            taken[k] = True
            s = pool[k]
            if flip:
                s["pts"], s["w"] = s["pts"][::-1], s["w"][::-1]
            ordered.append(s)
            pen = s["pts"][-1]
    inv = 1.0 / scale
    return [(s["pts"] * inv, s["w"] * inv) for s in ordered]


def stroke_progress(S: np.ndarray) -> np.ndarray:
    """For every pixel a pen stroke covers, how far through the whole drawing the pen is when it gets there (0..1);
    -1 where no stroke passes. Earlier strokes win where strokes cross."""
    import cv2

    H, W = S.shape[:2]
    strokes = extract_strokes(S)
    out = np.full((H, W), -1.0, np.float32)
    if not strokes:
        return out
    lengths = [np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))] for p, _ in strokes]
    total = max(float(sum(c[-1] for c in lengths)), 1e-6)
    before = np.cumsum([0.0] + [c[-1] for c in lengths[:-1]])
    for k in range(len(strokes) - 1, -1, -1):  # last stroke first, so earlier strokes are drawn over it
        pts, r = strokes[k]
        prog = (before[k] + lengths[k]) / total
        ip = np.round(pts).astype(np.int32)
        for j in range(len(ip) - 2, -1, -1):
            cv2.line(out, (int(ip[j, 0]), int(ip[j, 1])), (int(ip[j + 1, 0]), int(ip[j + 1, 1])), float(prog[j]),
                     thickness=max(2, int(round(2 * r[j] + 3))))
    return out


# ---------------------------------------------------------------- the analysis (cached)

@dataclass
class Analysis:
    paper_small: np.ndarray   # uint8 (h, w, 3): the blank sheet at 1/8 size
    cls: np.ndarray           # uint8 (H, W): LINE / WASH / ORANGE
    label: np.ndarray         # uint8 (H, W): item label of every pixel
    order: np.ndarray         # uint16 (H, W): when each line pixel goes down within its item (0..65535)
    items: list[Item]
    whole: bool               # the split was not trusted: one item, drawn outward from the main subject
    strokes: bool             # the line order follows pen strokes
    reason: str = ""

    @property
    def objects(self) -> list[Item]:
        return [it for it in self.items if not it.setting]

    def paper(self) -> np.ndarray:
        H, W = self.cls.shape
        return paper_full(self.paper_small, W, H)


def cache_key(S: np.ndarray, strokes: bool) -> str:
    h = hashlib.sha256()
    h.update(f"drawon v{VERSION} {S.shape} strokes={int(strokes)}".encode())
    h.update(np.ascontiguousarray(S).tobytes())
    return h.hexdigest()[:32]


def _save(path: Path, a: Analysis) -> None:
    meta = {"items": [asdict(it) for it in a.items], "whole": a.whole, "strokes": a.strokes, "reason": a.reason}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + f".tmp{os.getpid()}.npz")
    np.savez_compressed(tmp, paper=a.paper_small, cls=a.cls, label=a.label, order=a.order,
                        meta=np.frombuffer(json.dumps(meta).encode("utf-8"), np.uint8))
    os.replace(tmp, path)


def _load(path: Path) -> Analysis | None:
    try:
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(bytes(z["meta"]).decode("utf-8"))
            items = [Item(**{**d, "bbox": tuple(d["bbox"]), "ink_bbox": tuple(d["ink_bbox"]),
                             "seed": tuple(d["seed"]) if d["seed"] else None}) for d in meta["items"]]
            return Analysis(paper_small=z["paper"].copy(), cls=z["cls"].copy(), label=z["label"].copy(),
                            order=z["order"].copy(), items=items, whole=meta["whole"], strokes=meta["strokes"],
                            reason=meta.get("reason", ""))
    except Exception as exc:  # a damaged cache file is simply worked out again
        log.debug("Ignoring cached analysis %s: %s", path, exc)
        return None


def analyse(S: np.ndarray, cache_dir: Path | None = None, strokes: bool | None = None) -> Analysis:
    """Everything about a picture that does not depend on the shot. Cached by content hash in cache_dir."""
    use_strokes = strokes_available() if strokes is None else (strokes and strokes_available())
    path = cache_dir / f"{cache_key(S, use_strokes)}.npz" if cache_dir else None
    if path is not None and path.exists():
        cached = _load(path)
        if cached is not None and cached.cls.shape == S.shape[:2]:
            return cached
    H, W = S.shape[:2]
    small = paper_small(S)
    P = paper_full(small, W, H)
    cls, dark = classify(S, P)
    line = cls == LINE
    split = split_items(cls, dark)
    ok, reason = judge_split(split)
    if not ok:
        split = whole_split(S, cls, dark)
    rng = np.random.default_rng(500)
    flow = split.dist + _up(rng.random((H // 32 + 1, W // 32 + 1)).astype(np.float32), W, H) * 1.5
    pen = None
    if use_strokes:
        try:
            pen = stroke_progress(S)
        except Exception as exc:  # never fail a render over the nicer order
            log.warning("Pen strokes could not be traced (%s); using the ink flow for this picture.", exc)
            pen = None
    if pen is not None:
        covered = pen >= 0
        if covered.any() and not covered.all():
            from scipy import ndimage
            idx = ndimage.distance_transform_edt(~covered, return_distances=False, return_indices=True)
            pen = pen[idx[0], idx[1]]
        elif not covered.any():
            pen = None
    order = np.zeros((H, W), np.uint16)
    label = split.label.astype(np.uint8)
    for it in split.items:
        ml = (split.label == it.label) & line
        if not ml.any():
            continue
        if pen is not None:
            v = pen[ml]
        else:
            v = flow[ml]
            if it.seed is not None:
                yy, xx = np.nonzero(ml)
                v = v + 0.8 * np.hypot(xx - it.seed[0], yy - it.seed[1]) / 8.0
        order[ml] = np.round(_rank(v) * 65535).astype(np.uint16)
    a = Analysis(paper_small=small, cls=cls, label=label, order=order, items=split.items, whole=not ok,
                 strokes=pen is not None, reason=reason)
    if path is not None:
        try:
            _save(path, a)
        except OSError as exc:
            log.warning("Could not cache the drawing analysis (%s); it will be worked out again next time.", exc)
    return a


# ---------------------------------------------------------------- timing

def build_seconds(duration: float) -> float:
    """How long the drawing itself takes: about a third of the shot, at most 4 s, at least 1.2 s (but never more
    than the shot can hold with the clearing before and the soft finish after)."""
    T = max(min(duration * 0.35, 4.0), min(1.2, 0.8 * duration))
    return max(0.2, min(T, duration - FADE - TAIL - 0.1))


def speech_times(n: int, onsets: list[float], window: float, first: float = 0.25, gap: float = 0.55) -> list[float]:
    """n entrance times inside [first, window]: even spacing, each snapped to a nearby phrase start when one is
    within reach."""
    if n <= 0:
        return []
    window = max(window, first + gap * n)
    step = (window - first) / n
    out: list[float] = []
    cands = sorted(o for o in onsets if first <= o <= window)
    for k in range(n):
        target = first + k * step
        lo = (out[-1] + gap) if out else first
        near = [o for o in cands if abs(o - target) <= 0.45 * step and o >= lo]
        out.append(min(near, key=lambda o: abs(o - target)) if near else max(target, lo))
    return out


def phrase_onsets(words, shot_start: float, shot_end: float, pause: float = 0.18) -> list[float]:
    """Shot-local starts of phrases: a word after a pause, or after a word ending in , ; : . ? !"""
    out, prev = [], None
    for w in words:
        if shot_start <= w.s < shot_end and (prev is None or w.s - prev.e >= pause or prev.w[-1:] in ",;:.?!"):
            out.append(round(w.s - shot_start, 3))
        prev = w
    return out


# ---------------------------------------------------------------- the puff

def _wobbly(cx, cy, r, phase, n=28, amp=0.09):
    pts = []
    for k in range(n):
        th = 2 * math.pi * k / n
        rr = r * (1 + amp * math.sin(3 * th + phase) + 0.6 * amp * math.sin(5 * th + 1.7 * phase) + 0.5 * amp * math.cos(2 * th - phase))
        pts.append((cx + rr * math.cos(th), cy + rr * math.sin(th)))
    return pts


def draw_puff(img: Image.Image, cx: float, cy: float, R: float, u: float, seed: int, ink, accent, scale: float = 1.0) -> None:
    """One marker-style puff onto `img` (RGB, in place) at progress u in (0, 1): it blooms fast, then breaks up."""
    if u <= 0 or u >= 1:
        return
    rng = np.random.default_rng(seed)
    nb = 9
    angs = np.linspace(0, 2 * math.pi, nb, endpoint=False) + rng.uniform(-0.2, 0.2, nb)
    rad = rng.uniform(0.30, 0.42, nb)
    grow = 1 - (1 - min(u / 0.28, 1.0)) ** 3
    d = max(0.0, (u - 0.30) / 0.70)
    boil = math.floor(u * 12) * 2.1
    S2 = 2 if R < 170 else 1
    box = max(int(R * 3.2), 4)
    x0, y0 = int(cx - box / 2), int(cy - box / 2)
    W, H = img.size
    bx0, by0, bx1, by1 = max(x0, 0), max(y0, 0), min(x0 + box, W), min(y0 + box, H)
    if bx1 <= bx0 or by1 <= by0:
        return
    layer = Image.new("RGBA", (box * S2, box * S2), (0, 0, 0, 0))
    dr = ImageDraw.Draw(layer)
    c = box * S2 / 2
    lw = max(2, round(5.5 * scale * S2 * (1 - 0.5 * d)))
    fill_a = int(235 * (1 - d) ** 1.4)
    blobs = []
    for k in range(nb):
        ring = R * (0.58 + 0.50 * d) * grow
        r = R * rad[k] * (0.35 + 0.65 * grow) * (1 - 0.55 * d)
        blobs.append((c + S2 * ring * math.cos(angs[k]), c + S2 * ring * math.sin(angs[k]) * 0.85, S2 * r, k))
    rc = R * 0.62 * (0.4 + 0.6 * grow) * (1 - 0.8 * d)
    if rc > 2:
        blobs.append((c, c, S2 * rc, nb))
    for bx, by, r, k in blobs:
        dr.polygon(_wobbly(bx, by, r, boil + k), fill=(214, 211, 205, fill_a))
    for bx, by, r, k in blobs:
        dr.polygon(_wobbly(bx - 0.18 * r, by - 0.2 * r, r * 0.62, boil + k + 1), fill=(244, 242, 236, int(fill_a * 0.9)))
    for bx, by, r, k in blobs:  # one cloud outline: only where a blob's edge is not inside another blob
        pts = _wobbly(bx, by, r, boil + k, n=40)
        nseg = len(pts)
        inside = [any(math.hypot(px - ox, py - oy) < 0.93 * orr for ox, oy, orr, kk in blobs if kk != k) for px, py in pts]
        for j in range(nseg):
            if inside[j] or inside[(j + 1) % nseg]:
                continue
            if math.sin(j * 1.9 + k * 2.3) * 0.5 + 0.5 < d * 1.5 - 0.15:  # the outline breaks into dashes, then goes
                continue
            dr.line([pts[j], pts[(j + 1) % nseg]], fill=(*ink, 255), width=lw)
    if d < 0.6:  # one off-centre swirl: the cue that this is a puff, not a bubble
        a = int(255 * (1 - d / 0.6))
        cx_, cy_ = c + S2 * R * 0.18 * grow, c - S2 * R * 0.12 * grow
        pts = []
        for j in range(26):
            th = 3.6 + j * 0.2 + boil * 0.05
            rr = S2 * R * grow * (0.05 + 0.011 * j)
            pts.append((cx_ + rr * math.cos(th), cy_ + rr * math.sin(th)))
        dr.line(pts, fill=(*ink, a), width=max(2, round(lw * 0.7)), joint="curve")
    if 0.08 < u < 0.75:  # a few accent ticks flying out during the bloom
        a = int(255 * min(1.0, (0.75 - u) / 0.25))
        for j in range(4):
            th = angs[j * 2 % nb] + 0.4
            r0 = S2 * R * (0.95 + 0.6 * d) * grow
            r1 = r0 + S2 * R * 0.22 * (1 - d)
            dr.line([(c + r0 * math.cos(th), c + r0 * math.sin(th)), (c + r1 * math.cos(th), c + r1 * math.sin(th))],
                    fill=(*accent, a), width=max(2, round(lw * 1.1)))
    if d > 0:
        for j in range(5):
            th = rng.uniform(0, 2 * math.pi)
            rr = S2 * R * (1.0 + 0.9 * d) * rng.uniform(0.8, 1.15)
            s = S2 * R * 0.09 * (1 - d)
            if s > 1.5:
                dr.ellipse((c + rr * math.cos(th) - s, c + rr * math.sin(th) - s, c + rr * math.cos(th) + s, c + rr * math.sin(th) + s),
                           outline=(*ink, int(255 * (1 - d))), width=max(1, lw // 2))
    layer = layer.resize((box, box), Image.BILINEAR)
    crop = layer.crop((bx0 - x0, by0 - y0, bx1 - x0, by1 - y0))
    img.paste(crop.convert("RGB"), (bx0, by0), crop)


# ---------------------------------------------------------------- the build of one shot

@dataclass
class Entrance:
    item: Item
    kind: str               # "draw" or "puff"
    start: float            # build seconds
    end: float
    t0: float = 0.0         # when its first pixel starts changing
    t1: float = 0.0         # when its last pixel is finished


class ShotBuild:
    """Builds one picture S (source space, the size the camera crops from) over the first part of its shot.
    image(t) gives the picture at build time t (0 = drawing starts); from `end` on it is S itself."""

    def __init__(self, S: np.ndarray, analysis: Analysis, duration: float, index: int = 0, puff: bool = False,
                 onsets: list[float] | None = None, ink=(32, 32, 32), accent=(180, 80, 30)):
        self.S, self.a, self.index = S, analysis, index
        H, W = S.shape[:2]
        self.W, self.H = W, H
        self.ink, self.accent = tuple(ink), tuple(accent)
        self.puff = bool(puff and not analysis.whole and len(analysis.objects) >= 2)
        items = analysis.items
        T = build_seconds(duration)
        ents: list[Entrance] = []
        if self.puff:
            objs = analysis.objects
            setting = [it for it in items if it.setting]
            first = min(0.9, 0.3 * T)
            for it in setting:  # the background is drawn quickly first; the objects then puff in, one by one
                ents.append(Entrance(it, "draw", 0.0, first))
            window = min(0.55 * duration, 7.0, max(duration - FADE - PUFF_SECONDS - 0.5, first + 0.55 * len(objs)))
            times = speech_times(len(objs), list(onsets or []), window, first=max(0.25, first * 0.6))
            for it, t in zip(objs, times):
                ents.append(Entrance(it, "puff", t, t + PUFF_SECONDS))
        else:
            weights = np.array([max(it.area, 1) ** 0.6 for it in items], np.float64)
            spans = weights / weights.sum() * T * 1.15  # neighbours overlap by 15%
            t = 0.0
            for it, sp in zip(items, spans):
                ents.append(Entrance(it, "draw", t, min(t + sp, T)))
                t += sp / 1.15
            ents[-1].end = T
        self.entrances = ents
        self.end = max(e.end for e in ents) + (SOFT[PUFFED] if self.puff else TAIL)
        if self.puff:
            self.end = max(self.end, max(e.end for e in ents if e.kind == "draw") + TAIL)
        # ---- each pixel's start time (build seconds) and class (PUFFED for pixels that arrive under a puff)
        cls = analysis.cls.copy()
        L = analysis.label
        line = cls == LINE
        start = np.zeros((H, W), np.float32)
        rng = np.random.default_rng(700 + index)
        ys = np.linspace(0, 1, H, dtype=np.float32)[:, None]
        xs = np.linspace(0, 1, W, dtype=np.float32)[None, :]
        sweeps = [xs * 0.8 + ys * 0.2, xs * 0.2 + ys * 0.8, (1 - xs) * 0.8 + ys * 0.2, xs * 0.55 + (1 - ys) * 0.45]
        streak = _up(rng.random((max(H // 40, 2), max(W // 260, 2))).astype(np.float32), W, H)
        tw = 0.7 * sweeps[index % len(sweeps)] + 0.3 * streak
        order = analysis.order.astype(np.float32) / 65535.0
        masks = {}
        for e in ents:
            m = L == e.item.label
            masks[e.item.label] = m
            a, b = e.start, e.end
            if e.kind == "puff":
                start[m] = a + 0.14
                cls[m] = PUFFED
                continue
            ml = m & line
            start[ml] = a + (b - a) * 0.62 * order[ml]
            mw = m & ~line
            if mw.any():
                loc = tw[mw]
                loc = (loc - loc.min()) / max(float(loc.max() - loc.min()), 1e-6)
                start[mw] = np.where(cls[mw] == ORANGE, a + (b - a) * (0.72 + 0.18 * loc), a + (b - a) * (0.50 + 0.38 * loc))
        # wash and colour never before the line work around them
        drawn = cls != PUFFED
        ls = np.where(line & drawn, start, 0.0).astype(np.float32)
        loc = _up(_maxf(_blockmax(ls, 8), 7), W, H)
        later = drawn & ~line
        start = np.where(later, np.maximum(start, loc + 0.12), start)
        soft = SOFT[cls]
        start = np.minimum(start, self.end - soft)  # every pixel is finished by the end
        self.T_code = self.end + 0.5
        self.code = (cls.astype(np.uint32) * BAND + np.round(np.clip(start / self.T_code, 0, 1) * (BAND - 1))).astype(np.uint16)
        starts_q = self.code.astype(np.float32) % BAND / (BAND - 1) * self.T_code
        for e in ents:
            m = masks[e.item.label]
            e.t0 = float(starts_q[m].min()) if m.any() else e.start
            e.t1 = float((starts_q[m] + soft[m]).max()) if m.any() else e.end
        self._starts = np.linspace(0, self.T_code, BAND, dtype=np.float32)
        self._S = Image.fromarray(S)
        self._P = Image.fromarray(analysis.paper())
        self.canvas = self._P.copy()
        self._last = -1.0

    def lut(self, t: float) -> np.ndarray:
        out = np.full(65536, 255, np.uint8)
        for c in range(4):
            a = np.clip((t - self._starts) / SOFT[c], 0.0, 1.0)
            out[c * BAND:(c + 1) * BAND] = (a * a * (3 - 2 * a) * 255 + 0.5).astype(np.uint8)
        return out

    def image(self, t: float) -> Image.Image:
        """The picture at build time t. Frames are expected in time order; going back starts again from paper."""
        if t >= self.end:
            self._last = t
            return self._S
        if t < self._last:
            self.canvas = self._P.copy()
            self._last = -1.0
        lut = None
        for e in self.entrances:
            if e.t0 - 1e-3 <= t and e.t1 >= self._last:
                lut = self.lut(t) if lut is None else lut
                x0, y0, x1, y1 = e.item.bbox
                m = Image.fromarray(np.take(lut, self.code[y0:y1, x0:x1]), "L")
                box = (x0, y0, x1, y1)
                self.canvas.paste(Image.composite(self._S.crop(box), self._P.crop(box), m), (x0, y0))
        self._last = t
        out = None
        for k, e in enumerate(self.entrances):
            if e.kind != "puff":
                continue
            u = (t - e.start) / PUFF_SECONDS
            if 0 < u < 1:
                out = self.canvas.copy() if out is None else out
                us = (t - e.start - 0.14) / PUFF_SETTLE
                if 0 < us < 1:  # just arrived, the object is a touch large and eases back
                    self._settle(out, e.item, 1 + 0.03 * math.sin(math.pi * us) * (1 - us))
                x0, y0, x1, y1 = e.item.ink_bbox
                s = self.W / 2150
                R = float(np.clip(0.48 * math.sqrt(max((x1 - x0) * (y1 - y0), 1)), 90 * s, 400 * s))
                draw_puff(out, e.item.cx, e.item.cy, R, u, seed=self.index * 31 + k, ink=self.ink, accent=self.accent,
                          scale=self.W / 2150)
        return out if out is not None else self.canvas

    def _settle(self, out: Image.Image, it: Item, s: float) -> None:
        x0, y0, x1, y1 = it.bbox
        mi = Image.fromarray(((self.a.label[y0:y1, x0:x1] == it.label) * 255).astype(np.uint8), "L")
        crop = self._S.crop((x0, y0, x1, y1))
        w, h = x1 - x0, y1 - y0
        nw, nh = max(1, round(w * s)), max(1, round(h * s))
        out.paste(self._P.crop((x0, y0, x1, y1)), (x0, y0), mi)
        cx, cy = it.cx - x0, it.cy - y0
        out.paste(crop.resize((nw, nh), Image.BILINEAR), (round(x0 + cx - cx * s), round(y0 + cy - cy * s)),
                  mi.resize((nw, nh), Image.BILINEAR))


# ---------------------------------------------------------------- when to puff (sparingly)

# A puff is a light, slightly playful entrance. It is used only where it cannot jar: never during a scripture
# reading, never on solemn content, never on people, only on countable objects, at most about one shot in four.
# Every test errs toward not puffing.
PUFF_EVERY = 4
FIGURE_WORDS = frozenset("""
man men woman women person persons people figure figures child children boy boys girl girls baby babies infant infants
son sons daughter daughters father fathers mother mothers brother brothers sister sisters family families crowd crowds
shepherd shepherds believer believers traveler travelers traveller travellers prophet prophets fisherman fishermen king
kings queen queens ruler rulers priest priests disciple disciples apostle apostles soldier soldiers servant servants
merchant merchants elder elders youth he she him his her hers they them their himself herself themselves someone
anyone everyone hand hands arm arms face faces head heads finger fingers foot feet body bodies sleeve sleeves listener
listeners speaker preacher reader readers writer writers scribe scribes teacher teachers student students guest guests
angel angels god lord jesus christ messiah saviour savior
""".split())
SOLEMN_WORDS = frozenset("""
death dead die dies died dying cross crosses crucified crucifixion crucify tomb tombs grave graves burial buried bury
blood bleeding sacrifice sacrificed sacrifices sin sins sinful sinner sinners judgment judgement condemned hell
punishment wrath mourn mourning mourned weep weeping wept tears grief grieve grieving sorrow suffering suffer suffered
suffers pain agony prayer prayers pray praying prays kneel kneeling knelt repent repentance funeral murder murdered
killed kill slain persecution persecuted martyr martyrs prison chains famine war plague lamb passover
""".split())
SACRED_WORDS = frozenset("""
bible bibles scripture scriptures temple ark covenant commandments altar altars tabernacle synagogue church churches
gospel gospels testament psalm psalms holy sacred heaven heavenly sabbath
""".split())
COUNTABLE_WORDS = frozenset("""
scroll scrolls letter letters codex codices book books note notes tablet tablets jar jars pot pots lamp lamps cup cups
bowl bowls basket baskets loaf loaves coin coins stone stones box boxes bag bags radio radios clock clocks candle
candles key keys bottle bottles vase vases pitcher pitchers jug jugs plate plates apple apples fig figs pen pens quill
quills seal seals ring rings shell shells cart carts wheel wheels chair chairs stool stools map maps calendar calendars
hourglass sundial
""".split())


@dataclass
class BeatFacts:
    """What the planner knows about one shot, for deciding whether its picture may puff in."""
    scene: str
    spoken: str = ""
    reading: bool = False     # a scripture reading overlaps the shot
    chapter: bool = False     # a chapter card opens on the shot


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", (text or "").lower()))


# Words a scene sentence commonly starts with; any other capitalised first word may be a name.
STARTERS = frozenset("""
a an the one two three four five six seven eight nine ten several many some few a pair group row rows stack pile set
close wide medium overhead side top simple plain small large old ancient empty open closed view shot drawing sketch
on in at beside near under over with between behind
""".split())


def _names_someone(scene: str) -> bool:
    """A capitalised word that is probably a name (a person or a place): any capitalised word inside a sentence, or
    a sentence's first word when it is not one of the usual opening words. Either way, unsure."""
    for sentence in re.split(r"(?<=[.;:!?])\s+", scene or ""):
        tokens = re.findall(r"[A-Za-z][A-Za-z'’-]*", sentence)
        if tokens and (tokens[0].lower() not in STARTERS or any(t[0].isupper() for t in tokens[1:])):
            return True
    return False


def may_puff(f: BeatFacts) -> bool:
    if f.reading or f.chapter:
        return False
    scene = _words(f.scene)
    if scene & FIGURE_WORDS or scene & SOLEMN_WORDS or scene & SACRED_WORDS or _words(f.spoken) & SOLEMN_WORDS:
        return False
    if _names_someone(f.scene):
        return False
    return bool(scene & COUNTABLE_WORDS)


def choose_puffs(facts: list[BeatFacts], every: int = PUFF_EVERY) -> set[int]:
    """The shots whose picture puffs in: at most one in every `every` shots, never two closer than that, never the
    opening picture, and only where may_puff allows it."""
    cap = len(facts) // every
    chosen: list[int] = []
    for i, f in enumerate(facts):
        if len(chosen) >= cap:
            break
        if i == 0 or (chosen and i - chosen[-1] < every):
            continue
        if may_puff(f):
            chosen.append(i)
    return set(chosen)
