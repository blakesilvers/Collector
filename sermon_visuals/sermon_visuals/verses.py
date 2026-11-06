"""On-screen scripture: whenever the narrator reads a passage, its reference and words appear in a
lower band for the duration of the reading. Overlays are rendered with Pillow as transparent PNGs
and composited by ffmpeg, so the words are set in a real font and never by an image model."""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from .compose import Fonts, fit_text


class Scripture(BaseModel):
    start: float = Field(description="Second at which the narrator begins reading the passage aloud.")
    end: float = Field(description="Second at which the reading ends.")
    reference: str = Field(description="Book chapter:verse(s), e.g. 'Luke 2:8-12'.")
    version: str = Field(default="", description="Translation label if the transcript states one, e.g. 'AKJV'; else empty.")
    text: str = Field(description="The words of the passage exactly as read aloud, verse numbers omitted.")


class Overlay(BaseModel):
    png: str
    start: float
    end: float


def validate_scriptures(items: list[Scripture], duration: float) -> list[Scripture]:
    out = []
    for s in sorted(items, key=lambda s: s.start):
        start, end = max(0.0, s.start), min(s.end, duration)
        if end - start < 1.0 or not s.text.strip() or not s.reference.strip():
            continue
        if out and start < out[-1].end:
            start = out[-1].end
            if end - start < 1.0:
                continue
        out.append(Scripture(start=start, end=end, reference=s.reference.strip(), version=s.version.strip(), text=" ".join(s.text.split())))
    return out


def paginate(text: str, max_words: int) -> list[str]:
    """Split long passages at sentence or clause boundaries into pages of at most max_words."""
    words = text.split()
    if len(words) <= max_words:
        return [text]
    pieces = re.split(r"(?<=[.;:!?])\s+", text)
    pages, current = [], ""
    for piece in pieces:
        trial = f"{current} {piece}".strip()
        if len(trial.split()) <= max_words or not current:
            current = trial
        else:
            pages.append(current)
            current = piece
        while len(current.split()) > max_words:  # one monstrous sentence: hard-split it
            w = current.split()
            pages.append(" ".join(w[:max_words]))
            current = " ".join(w[max_words:])
    if current:
        pages.append(current)
    return pages


def render_overlay(reference: str, version: str, text: str, width: int, height: int, fonts: Fonts, out: Path, position: str = "bottom") -> Path:
    """Transparent full-frame PNG with a dark band (bottom or top) holding the reference and the passage."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = round(width * 0.06)
    pad = round(height * 0.03)
    ref_font = fonts.sans_at(round(height * 0.03))
    label = reference.upper() + (f"  ·  {version.upper()}" if version else "")
    box_w = width - 2 * margin
    body_font, lines, line_h = fit_text(draw, text, fonts.serif_at, box_w, round(height * 0.34), start=round(height * 0.045), minimum=round(height * 0.022), spacing=1.25)
    band_h = pad + ref_font.size + round(line_h * 0.5) + len(lines) * line_h + pad
    top = round(height * 0.04) if position == "top" else height - band_h - round(height * 0.04)
    draw.rounded_rectangle((margin - pad, top, width - margin + pad, top + band_h), radius=18, fill=(12, 10, 8, 200))
    y = top + pad
    draw.text((margin, y), label, font=ref_font, fill=(222, 206, 170, 255))
    y += ref_font.size + round(line_h * 0.5)
    for line in lines:
        draw.text((margin, y), line, font=body_font, fill=(250, 246, 236, 255))
        y += line_h
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out


def build_overlays(scriptures: list[Scripture], out_dir: Path, width: int, height: int, fonts: Fonts, max_words: int = 55, min_page_seconds: float = 4.0, position: str = "bottom") -> list[Overlay]:
    """Every word of every reading is shown. Long passages page through the reading's window; when the
    window is short, pages get denser (smaller type) before they get faster, and never get dropped."""
    overlays: list[Overlay] = []
    for i, s in enumerate(scriptures, start=1):
        window = s.end - s.start
        pages = paginate(s.text, max_words)
        words_per_page = max_words
        while len(pages) * min_page_seconds > window and words_per_page < max_words * 2:
            words_per_page += 10
            pages = paginate(s.text, words_per_page)
        per = window / len(pages)
        for j, page in enumerate(pages, start=1):
            png = render_overlay(s.reference, s.version, page, width, height, fonts, out_dir / f"verse_{i:03d}_{j}.png", position)
            overlays.append(Overlay(png=str(png), start=s.start + (j - 1) * per, end=s.start + j * per))
    return overlays


def overlay_filter(base_label: str, first_input_index: int, overlays: list[Overlay], fade: float = 0.4) -> tuple[list[str], str, str]:
    """Return (extra ffmpeg input args, filter snippet, final label) that composites each overlay during its window."""
    if not overlays:
        return [], "", base_label
    inputs: list[str] = []
    parts: list[str] = []
    prev = base_label
    for k, ov in enumerate(overlays):
        idx = first_input_index + k
        inputs += ["-loop", "1", "-i", ov.png]
        d = min(fade, max((ov.end - ov.start) / 3, 0.1))
        parts.append(
            f"[{idx}:v]format=rgba,fade=t=in:st={ov.start:.3f}:d={d:.3f}:alpha=1,fade=t=out:st={max(ov.end - d, ov.start):.3f}:d={d:.3f}:alpha=1[ov{k}]"
        )
        label = f"vo{k}"
        parts.append(f"[{prev}][ov{k}]overlay=0:0:enable='between(t,{ov.start:.3f},{ov.end:.3f})'[{label}]")
        prev = label
    return inputs, ";".join(parts), prev
