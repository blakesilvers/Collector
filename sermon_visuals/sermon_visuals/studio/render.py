"""Frame-by-frame compositor. Every frame is built in Python (picture, camera move, transition,
then the text layers) and piped to ffmpeg, so adding a new visual layer is a function, not a
filtergraph."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..compose import SANS_CANDIDATES, SERIF_CANDIDATES, cover, wrap
from ..verses import Overlay
from .plan import Card, Word

log = logging.getLogger(__name__)

ACCENT = (240, 128, 40)
CREAM = (250, 246, 236)
DARK = (12, 10, 8)
MUTED = (200, 192, 176)

DISPLAY_CANDIDATES = [
    "C:/Windows/Fonts/seguibl.ttf", "C:/Windows/Fonts/ariblk.ttf", "C:/Windows/Fonts/impact.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf", "/Library/Fonts/Arial Black.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
SANS_BOLD_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _first(cands: list[str], fallback: list[str]) -> str:
    for c in cands + fallback:
        if Path(c).exists():
            return c
    raise FileNotFoundError("no usable .ttf font found on this computer")


class Type:
    def __init__(self):
        self.display = _first(DISPLAY_CANDIDATES, SANS_CANDIDATES)
        self.bold = _first(SANS_BOLD_CANDIDATES, SANS_CANDIDATES)
        self.sans = _first(SANS_CANDIDATES, [])
        self.serif = _first(SERIF_CANDIDATES, SANS_CANDIDATES)
        self._cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    def get(self, which: str, size: int) -> ImageFont.FreeTypeFont:
        key = (which, size)
        if key not in self._cache:
            self._cache[key] = ImageFont.truetype(getattr(self, which), size)
        return self._cache[key]


# ---------- timeline description ----------

@dataclass
class ShotSpec:
    start: float
    end: float
    image: Path
    clip: Path | None = None
    clip_seconds: float = 0.0


@dataclass
class Callout:
    words: list[tuple[str, float]]
    end: float


@dataclass
class CardSpec:
    card: Card
    start: float
    end: float
    item_times: list[float]


@dataclass
class ChapterMark:
    number: int
    title: str
    start: float


@dataclass
class Timeline:
    duration: float
    shots: list[ShotSpec]
    callouts: list[Callout] = field(default_factory=list)
    cards: list[CardSpec] = field(default_factory=list)
    chapters: list[ChapterMark] = field(default_factory=list)
    verses: list[Overlay] = field(default_factory=list)
    caption_words: list[Word] = field(default_factory=list)
    progress: bool = True
    ink_in: bool = True
    camera: float = 0.1
    show_chapters: bool = True


# ---------- small helpers ----------

def blend(frame: np.ndarray, layer: np.ndarray, x: int, y: int, opacity: float = 1.0) -> None:
    """Alpha-composite an RGBA layer onto an RGB frame in place, clipped to the frame."""
    if opacity <= 0:
        return
    h, w = layer.shape[:2]
    H, W = frame.shape[:2]
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
    if x0 >= x1 or y0 >= y1:
        return
    sub = layer[y0 - y:y1 - y, x0 - x:x1 - x]
    a = sub[..., 3:4].astype(np.float32) * (opacity / 255.0)
    region = frame[y0:y1, x0:x1].astype(np.float32)
    region += (sub[..., :3].astype(np.float32) - region) * a
    frame[y0:y1, x0:x1] = region.astype(np.uint8)


def ease(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


def envelope(t: float, start: float, end: float, fade_in: float = 0.25, fade_out: float = 0.3) -> float:
    if t < start or t > end:
        return 0.0
    return min(1.0, (t - start) / max(fade_in, 1e-6), (end - t) / max(fade_out, 1e-6))


def trim(layer: Image.Image) -> tuple[np.ndarray, int, int]:
    """Crop an RGBA image to its visible box. Returns (array, x, y) of the crop."""
    box = layer.getbbox()
    if not box:
        return np.zeros((1, 1, 4), np.uint8), 0, 0
    return np.asarray(layer.crop(box)).copy(), box[0], box[1]


def caption_chunks(words: list[Word], max_words: int = 6, max_gap: float = 0.6) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        stop = (
            nxt is None or len(cur) >= max_words or w.w[-1] in ".?!"
            or (w.w[-1] in ",;:" and len(cur) >= 4) or (nxt.s - w.e > max_gap)
        )
        if stop:
            chunks.append(cur)
            cur = []
    return chunks


class ClipReader:
    """Streams an animated clip's frames from ffmpeg, stretched (up to 1.5x) to fill its shot."""

    def __init__(self, path: Path, W: int, H: int, fps: int, stretch: float):
        self.size = W * H * 3
        self.shape = (H, W, 3)
        vf = f"setpts={stretch:.4f}*PTS,fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
        self.proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-i", str(path), "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-an", "-"],
            stdout=subprocess.PIPE,
        )
        self.last: np.ndarray | None = None

    def read(self) -> np.ndarray | None:
        buf = self.proc.stdout.read(self.size) if self.proc.stdout else b""
        if len(buf) == self.size:
            self.last = np.frombuffer(buf, np.uint8).reshape(self.shape).copy()
        return None if self.last is None else self.last.copy()

    def close(self) -> None:
        try:
            if self.proc.stdout:
                self.proc.stdout.close()
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass


# ---------- the renderer ----------

class Renderer:
    def __init__(self, tl: Timeline, W: int = 1920, H: int = 1080, fps: int = 24):
        self.tl, self.W, self.H, self.fps = tl, W, H, fps
        self.type = Type()
        self.margin = round(W * 0.05)
        self._shot_i = -1
        self._src: Image.Image | None = None
        self._clip: ClipReader | None = None
        self._order: np.ndarray | None = None
        self._prev: np.ndarray | None = None   # last frame of the previous shot, for transitions
        self._last_base: np.ndarray | None = None
        self._callout_layers: dict[int, list[tuple[np.ndarray, int, int]]] = {}
        self._card_layers: dict[int, list[tuple[np.ndarray, int, int]]] = {}
        self._chapter_cards: dict[int, tuple[np.ndarray, int, int]] = {}
        self._chips: dict[int, tuple[np.ndarray, int, int]] = {}
        self._verse_cache: tuple[str, tuple[np.ndarray, int, int]] | None = None
        self._chunks = caption_chunks(tl.caption_words) if tl.caption_words else []
        self._chunk_i = 0
        self._cap_cache: dict[tuple[int, int], tuple[np.ndarray, int, int]] = {}

    # ----- base picture -----

    def _open_shot(self, i: int) -> None:
        shot = self.tl.shots[i]
        if self._clip:
            self._clip.close()
            self._clip = None
        self._prev = self._last_base
        self._shot_i = i
        self._order = None
        if shot.clip and shot.clip.exists():
            stretch = min(max((shot.end - shot.start) / max(shot.clip_seconds, 0.1), 1.0), 1.5)
            self._clip = ClipReader(shot.clip, self.W, self.H, self.fps, stretch)
            self._src = None
        else:
            slack = 1.0 + self.tl.camera + 0.02
            with Image.open(shot.image) as im:
                self._src = cover(im.convert("RGB"), round(self.W * slack), round(self.H * slack))

    def _camera(self, i: int, u: float) -> np.ndarray:
        """One of six camera moves over a still, chosen by shot number so neighbours differ."""
        src, c = self._src, self.tl.camera
        SW, SH = src.size
        kind = i % 6
        z0, z1, cx0, cx1, cy0, cy1 = 1.0, 1.0 + c, 0.5, 0.5, 0.5, 0.5          # 0: push in
        if kind == 1:
            z0 = z1 = 1.0 + c; cx0, cx1 = 0.0, 1.0                             # pan right
        elif kind == 2:
            z0, z1 = 1.0 + c, 1.0                                              # pull out
        elif kind == 3:
            z0 = z1 = 1.0 + c; cx0, cx1 = 1.0, 0.0                             # pan left
        elif kind == 4:
            cy1 = 0.25; cx1 = 0.6                                              # push in toward upper right
        elif kind == 5:
            z0 = z1 = 1.0 + c; cy0, cy1 = 1.0, 0.0                             # tilt up
        z = z0 + (z1 - z0) * u
        bw, bh = SW / z, SH / z
        cx = cx0 + (cx1 - cx0) * u
        cy = cy0 + (cy1 - cy0) * u
        x = (SW - bw) * cx
        y = (SH - bh) * cy
        out = src.transform((self.W, self.H), Image.Transform.EXTENT, (x, y, x + bw, y + bh), resample=Image.Resampling.BILINEAR)
        return np.array(out)

    def _ink_order(self, cur: np.ndarray, i: int) -> np.ndarray:
        """When each pixel of the new picture arrives (0 = first): dark lines lead, in a sweep across the frame."""
        H, W = cur.shape[:2]
        lum = cur.astype(np.float32).mean(axis=2) / 255.0
        xs = np.linspace(0, 1, W, dtype=np.float32)[None, :]
        ys = np.linspace(0, 1, H, dtype=np.float32)[:, None]
        sweeps = [xs * 0.85 + ys * 0.15, ys * 0.8 + xs * 0.2, (1 - xs) * 0.85 + ys * 0.15, xs * 0.5 + ys * 0.5]
        sweep = sweeps[i % len(sweeps)]
        rng = np.random.default_rng(i)
        small = Image.fromarray((rng.random((H // 90 + 2, W // 90 + 2)) * 255).astype(np.uint8))
        noise = np.asarray(small.resize((W, H), Image.BICUBIC), dtype=np.float32) / 255.0
        order = 0.55 * sweep + 0.37 * lum + 0.08 * noise
        return order / order.max()

    def _base(self, t: float) -> np.ndarray:
        shots = self.tl.shots
        i = self._shot_i if self._shot_i >= 0 else 0
        while i + 1 < len(shots) and t >= shots[i + 1].start:
            i += 1
        if i != self._shot_i:
            self._open_shot(i)
        shot = shots[i]
        local = t - shot.start
        if self._clip:
            cur = self._clip.read()
            if cur is None:
                cur = self._prev.copy() if self._prev is not None else np.zeros((self.H, self.W, 3), np.uint8)
        else:
            cur = self._camera(i, local / max(shot.end - shot.start, 1e-6))
        self._last_base = cur
        if self._prev is None:
            return cur.copy()
        if self._clip is None and self.tl.ink_in:
            T = 0.75
            if local < T:
                if self._order is None:
                    self._order = self._ink_order(cur, i)
                p, soft = ease(local / T), 0.16
                a = np.clip((p * (1 + soft) - self._order) / soft, 0, 1)[..., None]
                prev = self._prev.astype(np.float32)
                return (prev + (cur.astype(np.float32) - prev) * a).astype(np.uint8)
        else:
            T = 0.3
            if local < T:
                a = local / T
                return (self._prev.astype(np.float32) * (1 - a) + cur.astype(np.float32) * a).astype(np.uint8)
        return cur.copy()

    # ----- text layers -----

    def _stroke_text(self, d: ImageDraw.ImageDraw, xy, text, font, fill, sw):
        d.text((xy[0] + sw // 2 + 2, xy[1] + sw // 2 + 3), text, font=font, fill=(0, 0, 0, 150), stroke_width=sw, stroke_fill=(0, 0, 0, 150))
        d.text(xy, text, font=font, fill=fill, stroke_width=sw, stroke_fill=(*DARK, 255))

    def _build_callout(self, k: int) -> list[tuple[np.ndarray, int, int]]:
        words = [w.upper().strip(".,;:!?\"“”") for w, _ in self.tl.callouts[k].words]
        size = round(self.H * 0.088)
        max_w = round(self.W * 0.6)
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        while True:
            font = self.type.get("display", size)
            lines = wrap(probe, " ".join(words), font, max_w)
            if len(lines) <= 3 or size <= round(self.H * 0.05):
                break
            size -= 4
        sw = max(3, size // 14)
        line_h = round(size * 1.12)
        space = probe.textlength(" ", font=font)
        positions, y, n = [], 0, 0
        for line in lines:
            x = 0.0
            for w in line.split():
                positions.append((round(x), y))
                x += probe.textlength(w, font=font) + space
                n += 1
            y += line_h
        states = []
        for upto in range(1, len(words) + 1):
            img = Image.new("RGBA", (max_w + 40, line_h * len(lines) + 40), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            for j in range(upto):
                colour = (*ACCENT, 255) if j == upto - 1 and upto < len(words) else (*CREAM, 255)
                self._stroke_text(d, (positions[j][0] + 16, positions[j][1] + 10), words[j], font, colour, sw)
            if upto == len(words):  # underline the finished phrase
                yb = positions[-1][1] + line_h + 6
                d.rounded_rectangle((16, yb, 16 + round(size * 1.6), yb + max(6, size // 12)), radius=4, fill=(*ACCENT, 255))
            states.append(trim(img))
        return states

    def _draw_callout(self, frame: np.ndarray, t: float) -> None:
        for k, c in enumerate(self.tl.callouts):
            if not c.words or t < c.words[0][1] - 0.05 or t > c.end:
                continue
            if k not in self._callout_layers:
                self._callout_layers = {k: self._build_callout(k)}  # keep only the active one
            shown = sum(1 for _, s in c.words if s - 0.05 <= t)
            layer, ox, oy = self._callout_layers[k][max(shown, 1) - 1]
            since = t - (c.words[max(shown, 1) - 1][1] - 0.05)
            pop = round((1 - ease(since / 0.14)) * self.H * 0.012)
            op = min(1.0, (c.end - t) / 0.3)
            blend(frame, layer, self.margin + ox, round(self.H * 0.15) + oy + pop, op)
            return

    def _build_card(self, k: int) -> list[tuple[np.ndarray, int, int]]:
        spec = self.tl.cards[k]
        card = spec.card
        pw = round(self.W * 0.42)
        pad = round(self.H * 0.035)
        title_f = self.type.get("bold", round(self.H * 0.028))
        big = card.type == "definition"
        label_f = self.type.get("display", round(self.H * (0.075 if big else 0.048)))
        detail_f = self.type.get("sans", round(self.H * (0.038 if big else 0.032)))
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        inset = pad + (round(self.H * 0.035) if card.type != "definition" else 0)
        text_w = pw - inset - pad
        rows = []
        for it in card.items:
            ll = wrap(probe, it.label.strip(), label_f, text_w)
            dl = wrap(probe, it.detail.strip(), detail_f, text_w) if it.detail.strip() else []
            rows.append((ll, dl))
        lh, dh = round(label_f.size * 1.15), round(detail_f.size * 1.3)
        gap = round(self.H * 0.022)
        head = pad + title_f.size + gap
        row_h = [len(ll) * lh + len(dl) * dh + gap for ll, dl in rows]
        ph = head + sum(row_h) + pad - gap + pad // 2
        states = []
        for upto in range(0, len(rows) + 1):
            img = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((0, 0, pw - 1, ph - 1), radius=22, fill=(*DARK, 225))
            d.rounded_rectangle((0, 0, 9, ph - 1), radius=4, fill=(*ACCENT, 255))
            d.text((pad, pad), card.title.upper(), font=title_f, fill=(*ACCENT, 255))
            y = head
            for j, (ll, dl) in enumerate(rows):
                if j >= upto:
                    break
                newest = j == upto - 1 and upto < len(rows)
                if card.type != "definition":
                    cx, cy, r = pad + round(self.H * 0.009), y + lh // 2, round(self.H * 0.009)
                    if card.type == "timeline" and j + 1 < upto:
                        d.line((cx, cy, cx, cy + row_h[j]), fill=(*MUTED, 255), width=3)
                    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*ACCENT, 255))
                for line in ll:
                    d.text((inset, y), line, font=label_f, fill=(*ACCENT, 255) if newest else (*CREAM, 255))
                    y += lh
                for line in dl:
                    d.text((inset, y), line, font=detail_f, fill=(*MUTED, 255))
                    y += dh
                y += gap
            states.append((np.asarray(img).copy(), 0, 0))
        return states

    def _draw_card(self, frame: np.ndarray, t: float) -> bool:
        for k, spec in enumerate(self.tl.cards):
            if t < spec.start or t > spec.end:
                continue
            if k not in self._card_layers:
                self._card_layers = {k: self._build_card(k)}
            shown = sum(1 for s in spec.item_times if s <= t)
            layer, _, _ = self._card_layers[k][shown]
            slide = round((1 - ease((t - spec.start) / 0.35)) * self.W * 0.06)
            op = envelope(t, spec.start, spec.end, 0.25, 0.3)
            blend(frame, layer, self.W - layer.shape[1] - self.margin + slide, round(self.H * 0.13), op)
            return True
        return False

    def _verse_layer(self, png: str) -> tuple[np.ndarray, int, int]:
        if not self._verse_cache or self._verse_cache[0] != png:
            with Image.open(png) as im:
                im = im.convert("RGBA")
                if im.size != (self.W, self.H):
                    im = im.resize((self.W, self.H), Image.LANCZOS)
                self._verse_cache = (png, trim(im))
        return self._verse_cache[1]

    def _draw_verse(self, frame: np.ndarray, t: float) -> bool:
        for ov in self.tl.verses:
            if ov.start <= t <= ov.end:
                layer, x, y = self._verse_layer(ov.png)
                blend(frame, layer, x, y - round(self.H * 0.012), envelope(t, ov.start, ov.end, 0.3, 0.3))
                return True
        return False

    def _draw_captions(self, frame: np.ndarray, t: float) -> None:
        ch = self._chunks
        while self._chunk_i + 1 < len(ch) and t >= ch[self._chunk_i + 1][0].s - 0.05:
            self._chunk_i += 1
            self._cap_cache.clear()
        if not ch:
            return
        chunk = ch[self._chunk_i]
        if t < chunk[0].s - 0.05 or t > chunk[-1].e + 0.35:
            return
        active = max(0, sum(1 for w in chunk if w.s - 0.03 <= t) - 1)
        key = (self._chunk_i, active)
        if key not in self._cap_cache:
            font = self.type.get("bold", round(self.H * 0.044))
            sw = max(3, font.size // 11)
            probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
            space = probe.textlength(" ", font=font)
            widths = [probe.textlength(w.w, font=font) for w in chunk]
            total = round(sum(widths) + space * (len(chunk) - 1))
            img = Image.new("RGBA", (total + 60, round(font.size * 1.7)), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            x = 30.0
            for j, w in enumerate(chunk):
                self._stroke_text(d, (round(x), 8), w.w, font, (*ACCENT, 255) if j == active else (*CREAM, 255), sw)
                x += widths[j] + space
            self._cap_cache[key] = (np.asarray(img).copy(), 0, 0)
        layer = self._cap_cache[key][0]
        blend(frame, layer, (self.W - layer.shape[1]) // 2, round(self.H * 0.84))

    def _chapter_at(self, t: float) -> ChapterMark | None:
        cur = None
        for c in self.tl.chapters:
            if c.start <= t:
                cur = c
        return cur

    def _draw_chapter(self, frame: np.ndarray, t: float) -> bool:
        """Returns True while the full-screen chapter card is up."""
        c = self._chapter_at(t)
        if c is None or not self.tl.show_chapters:
            return False
        flash = envelope(t, c.start, c.start + 2.6, 0.3, 0.45)
        if flash > 0:
            frame[:] = (frame.astype(np.float32) * (1 - 0.62 * flash)).astype(np.uint8)
            if c.number not in self._chapter_cards:
                img = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                small = self.type.get("bold", round(self.H * 0.03))
                size = round(self.H * 0.1)
                while True:
                    big = self.type.get("display", size)
                    lines = wrap(d, c.title.upper(), big, round(self.W * 0.8))
                    if len(lines) <= 2 or size < round(self.H * 0.06):
                        break
                    size -= 6
                lh = round(size * 1.12)
                y = (self.H - lh * len(lines)) // 2
                label = f"PART {c.number}"
                d.text(((self.W - d.textlength(label, font=small)) / 2, y - small.size * 2.2), label, font=small, fill=(*ACCENT, 255))
                for line in lines:
                    self._stroke_text(d, ((self.W - d.textlength(line, font=big)) / 2, y), line, big, (*CREAM, 255), max(3, size // 16))
                    y += lh
                d.rounded_rectangle((self.W // 2 - size, y + 14, self.W // 2 + size, y + 14 + max(6, size // 12)), radius=4, fill=(*ACCENT, 255))
                self._chapter_cards = {c.number: trim(img)}
            layer, x, y = self._chapter_cards[c.number]
            rise = round((1 - ease((t - c.start) / 0.4)) * self.H * 0.02)
            blend(frame, layer, x, y + rise, flash)
            return True
        if c.number not in self._chips:
            font = self.type.get("bold", round(self.H * 0.022))
            text = f"{c.number}  ·  {c.title.upper()}"
            probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
            w = round(probe.textlength(text, font=font)) + round(self.H * 0.04)
            h = round(font.size * 2.0)
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=(*DARK, 190))
            d.text((round(self.H * 0.02), (h - font.size) // 2 - 2), text, font=font, fill=(*CREAM, 255))
            self._chips = {c.number: (np.asarray(img).copy(), 0, 0)}
        blend(frame, self._chips[c.number][0], self.margin, round(self.H * 0.045), 0.95)
        return False

    def _draw_progress(self, frame: np.ndarray, t: float) -> None:
        h = max(4, round(self.H * 0.007))
        w = round(self.W * min(t / max(self.tl.duration, 1e-6), 1.0))
        frame[self.H - h:, :] = (frame[self.H - h:, :].astype(np.float32) * 0.45).astype(np.uint8)
        frame[self.H - h:, :w] = ACCENT

    # ----- frame -----

    def frame(self, t: float) -> np.ndarray:
        f = self._base(t)
        in_chapter_card = self._draw_chapter(f, t)
        verse = False if in_chapter_card else self._draw_verse(f, t)
        card = False if (in_chapter_card or verse) else self._draw_card(f, t)
        if not (in_chapter_card or verse or card):
            self._draw_callout(f, t)
        if self._chunks and not (verse or in_chapter_card):
            self._draw_captions(f, t)
        if self.tl.progress:
            self._draw_progress(f, t)
        return f

    def close(self) -> None:
        if self._clip:
            self._clip.close()


def contact_sheet(frames: list[tuple[float, np.ndarray]], out: Path, cols: int = 4) -> Path:
    """A grid of frames with timestamps: one image that shows the whole clip at a glance."""
    if not frames:
        return out
    tw, th = 480, 270
    rows = (len(frames) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * th), (20, 20, 22))
    d = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(_first(SANS_BOLD_CANDIDATES, SANS_CANDIDATES), 20)
    for k, (t, arr) in enumerate(frames):
        x, y = (k % cols) * tw, (k // cols) * th
        sheet.paste(Image.fromarray(arr).resize((tw, th), Image.BILINEAR), (x, y))
        label = f"{int(t // 60)}:{int(t % 60):02d}"
        d.rectangle((x + tw - 64, y + th - 34, x + tw, y + th - 6), fill=(0, 0, 0))
        d.text((x + tw - 56, y + th - 31), label, font=font, fill=(255, 255, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "JPEG", quality=85)
    return out


def render(tl: Timeline, audio: Path, out: Path, audio_start: float = 0.0, W: int = 1920, H: int = 1080, fps: int = 24,
           sheet: Path | None = None, sheet_frames: int = 24) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    total = max(1, int(round(tl.duration * fps)))
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
        "-ss", f"{audio_start:.3f}", "-t", f"{tl.duration:.3f}", "-i", str(audio),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest", str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    r = Renderer(tl, W, H, fps)
    picks = {int((k + 0.5) * total / sheet_frames) for k in range(sheet_frames)} if sheet else set()
    kept: list[tuple[float, np.ndarray]] = []
    step = max(1, total // 20)
    try:
        for n in range(total):
            f = r.frame(n / fps)
            proc.stdin.write(f.tobytes())
            if n in picks:
                kept.append((n / fps, f[::2, ::2].copy()))
            if n % step == 0:
                log.info("Rendering %3d%%", round(100 * n / total))
    finally:
        r.close()
        if proc.stdin:
            proc.stdin.close()
        code = proc.wait()
    if code != 0:
        raise RuntimeError("ffmpeg failed while encoding the video")
    if sheet:
        contact_sheet(kept, sheet)
    return out
