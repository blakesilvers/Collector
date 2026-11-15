"""Stage 4: artwork + text -> finished cards. Text is rendered here with Pillow, never by the image model."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Format:
    name: str
    width: int
    height: int
    aspect: str


FORMATS = {
    "youtube": Format("youtube", 1920, 1080, "16:9"),
    "square": Format("square", 1080, 1080, "1:1"),
    "vertical": Format("vertical", 1080, 1920, "9:16"),
}


SERIF_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
    "/Library/Fonts/Georgia Bold.ttf",
    "C:/Windows/Fonts/georgiab.ttf",
    "C:/Windows/Fonts/timesbd.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
]
SANS_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


def find_font(configured: str, candidates: list[str]) -> str:
    """Return the configured font if it exists, else the first known system font, else raise."""
    if configured and configured != "auto":
        if Path(configured).exists():
            return configured
        raise FileNotFoundError(f"Font not found: {configured}. Set compose.font_serif / font_sans in config.toml to a .ttf on this computer, or \"auto\".")
    for c in candidates:
        if Path(c).exists():
            return c
    raise FileNotFoundError("No usable .ttf font found. Set compose.font_serif and compose.font_sans in config.toml to a font file on this computer.")


@dataclass
class Fonts:
    serif: str
    sans: str

    @classmethod
    def resolve(cls, serif: str = "auto", sans: str = "auto") -> "Fonts":
        return cls(find_font(serif, SERIF_CANDIDATES), find_font(sans, SANS_CANDIDATES))

    def serif_at(self, size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(self.serif, size)

    def sans_at(self, size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(self.sans, size)


def cover(img: Image.Image, width: int, height: int) -> Image.Image:
    """Scale and centre-crop so the image fills width x height."""
    scale = max(width / img.width, height / img.height)
    resized = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    make_font,
    max_width: int,
    max_height: int,
    start: int,
    minimum: int = 28,
    spacing: float = 1.2,
) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """Shrink the font until the wrapped block fits. Returns (font, lines, line_height)."""
    size = start
    while True:
        font = make_font(size)
        lines = wrap(draw, text, font, max_width)
        line_h = round(size * spacing)
        if (len(lines) * line_h <= max_height and all(draw.textlength(l, font=font) <= max_width for l in lines)) or size <= minimum:
            return font, lines, line_h
        size -= 2


def _darken(base: Image.Image, strength: float = 0.55, bottom_bias: bool = True) -> Image.Image:
    """Lay a vertical gradient (transparent top -> dark bottom) so light text stays readable."""
    w, h = base.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = overlay.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        alpha = int(255 * strength * (t if bottom_bias else 1.0) ** 1.4)
        for x in range(w):
            px[x, y] = (0, 0, 0, alpha)
    return Image.alpha_composite(base.convert("RGBA"), overlay)


def _paste_logo(canvas: Image.Image, logo: Path | None, margin: int, height: int) -> None:
    if not logo:
        return
    lg = Image.open(logo).convert("RGBA")
    scale = height / lg.height
    lg = lg.resize((round(lg.width * scale), height), Image.LANCZOS)
    canvas.alpha_composite(lg, (canvas.width - lg.width - margin, canvas.height - lg.height - margin))


def render_quote_card(
    art: Path,
    out: Path,
    fmt: Format,
    fonts: Fonts,
    quote: str,
    attribution: str,
    program: str,
    logo: Path | None = None,
) -> Path:
    base = cover(Image.open(art).convert("RGB"), fmt.width, fmt.height)
    canvas = _darken(base, strength=0.55, bottom_bias=False)
    draw = ImageDraw.Draw(canvas)
    margin = round(fmt.width * 0.08)
    box_w = fmt.width - 2 * margin
    box_h = round(fmt.height * 0.55)

    font, lines, line_h = fit_text(draw, f"“{quote}”", fonts.serif_at, box_w, box_h, start=round(fmt.width * 0.06))
    block_h = len(lines) * line_h
    y = (fmt.height - block_h) // 2 - round(fmt.height * 0.04)
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((fmt.width - w) / 2, y), line, font=font, fill=(250, 246, 236))
        y += line_h

    small = fonts.sans_at(round(fmt.width * 0.026))
    y += round(line_h * 0.6)
    w = draw.textlength(attribution, font=small)
    draw.text(((fmt.width - w) / 2, y), attribution, font=small, fill=(222, 206, 170))

    tiny = fonts.sans_at(round(fmt.width * 0.02))
    draw.text((margin, fmt.height - margin - tiny.size), program.upper(), font=tiny, fill=(222, 206, 170))
    _paste_logo(canvas, logo, margin, round(fmt.height * 0.07))

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out, "PNG", optimize=True)
    return out


def render_title_frame(
    art: Path,
    out: Path,
    fmt: Format,
    fonts: Fonts,
    title: str,
    subtitle: str,
    program: str,
    speaker_photo: Path | None = None,
    logo: Path | None = None,
    title_scale: float = 0.11,
) -> Path:
    """Thumbnail / video frame: big title lower-left, optional circular speaker photo lower-right.
    title_scale is the starting font size as a fraction of frame height; the video frame uses a
    smaller one so a scripture band at the top never collides with the title."""
    base = cover(Image.open(art).convert("RGB"), fmt.width, fmt.height)
    canvas = _darken(base, strength=0.8, bottom_bias=True)
    draw = ImageDraw.Draw(canvas)
    margin = round(fmt.width * 0.06)

    photo_w = 0
    if speaker_photo:
        d = round(fmt.height * 0.42)
        photo = cover(Image.open(speaker_photo).convert("RGB"), d, d)
        mask = Image.new("L", (d, d), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, d, d), fill=255)
        ring = Image.new("RGBA", (d + 12, d + 12), (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse((0, 0, d + 12, d + 12), fill=(222, 206, 170, 255))
        canvas.alpha_composite(ring, (fmt.width - d - margin - 6, fmt.height - d - margin - 6))
        canvas.paste(photo, (fmt.width - d - margin, fmt.height - d - margin), mask)
        photo_w = d + margin

    box_w = fmt.width - 2 * margin - photo_w
    box_h = round(fmt.height * (0.38 if title_scale >= 0.1 else 0.26))
    font, lines, line_h = fit_text(draw, title, fonts.serif_at, box_w, box_h, start=round(fmt.height * title_scale), minimum=40)
    sub = fonts.sans_at(round(fmt.height * 0.038))
    tiny = fonts.sans_at(round(fmt.height * 0.028))

    y = fmt.height - margin - tiny.size - round(line_h * 0.5) - sub.size - round(line_h * 0.4) - len(lines) * line_h
    for line in lines:
        draw.text((margin, y), line, font=font, fill=(250, 246, 236))
        y += line_h
    y += round(line_h * 0.4)
    draw.text((margin, y), subtitle, font=sub, fill=(222, 206, 170))
    draw.text((margin, fmt.height - margin - tiny.size), program.upper(), font=tiny, fill=(222, 206, 170))
    if not speaker_photo:
        _paste_logo(canvas, logo, margin, round(fmt.height * 0.08))

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out, "PNG", optimize=True)
    return out
