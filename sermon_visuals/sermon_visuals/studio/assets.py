"""Pictures and animated clips for each shot, generated in parallel, cached by content, and
counted against a budget. Nothing is generated twice: edit a scene in storyboard.json and only
that shot is redrawn."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageDraw, ImageFont

from ..config import Config
from ..imagegen import ImageProvider
from .plan import Beat

log = logging.getLogger(__name__)

MAX_CLIP_SECONDS = 15
MIN_CLIP_SECONDS = 3


@dataclass
class Shot:
    index: int
    start: float
    end: float
    beat: Beat
    image: Path | None = None
    clip: Path | None = None
    clip_seconds: int = 0
    animate: bool = False

    @property
    def duration(self) -> float:
        return self.end - self.start


class MotionProvider(Protocol):
    model: str

    def animate(self, image: Path, prompt: str, seconds: int) -> bytes: ...


class OmniMotion:
    """Image-to-video with Google's Gemini Omni Flash through the Interactions API."""

    def __init__(self, model: str, resolution: str = "720p"):
        from google import genai

        self.model = model
        self.resolution = resolution
        self.client = genai.Client()

    def animate(self, image: Path, prompt: str, seconds: int) -> bytes:
        buf = io.BytesIO()
        with Image.open(image) as im:
            im.convert("RGB").resize((1280, 720), Image.LANCZOS).save(buf, "JPEG", quality=92)
        interaction = self.client.interactions.create(
            model=self.model,
            input=[
                {"type": "image", "data": base64.b64encode(buf.getvalue()).decode("ascii"), "mime_type": "image/jpeg"},
                {"type": "text", "text": prompt},
            ],
            response_format={"type": "video", "aspect_ratio": "16:9", "resolution": self.resolution, "duration": f"{seconds}s"},
        )
        video = getattr(interaction, "output_video", None)
        data = getattr(video, "data", None)
        if not data:
            raise RuntimeError("the video model returned no video")
        return base64.b64decode(data) if isinstance(data, str) else bytes(data)


class DraftArt:
    """Free placeholder pictures (the scene description on a colour field) for checking a storyboard's pacing."""

    name = "draft"
    model = "draft"
    PALETTE = [(24, 44, 78), (150, 72, 28), (60, 82, 70), (92, 46, 62), (40, 40, 48), (120, 96, 40)]

    def generate(self, prompt: str, aspect: str, references: list[Path]) -> bytes:
        scene = prompt.split("SCENE:", 1)[-1].strip()
        h = int(hashlib.sha1(scene.encode()).hexdigest(), 16)
        base = self.PALETTE[h % len(self.PALETTE)]
        img = Image.new("RGB", (1600, 900), base)
        d = ImageDraw.Draw(img)
        for k in range(6):  # a few bold shapes so camera moves and transitions are visible
            x, y, r = (h >> (k * 9)) % 1600, (h >> (k * 7 + 3)) % 900, 80 + (h >> (k * 5)) % 220
            tone = tuple(min(255, c + 30 + 18 * k) for c in base)
            d.ellipse((x - r, y - r, x + r, y + r), fill=tone, outline=(10, 10, 12), width=10)
        try:
            from ..compose import Fonts, wrap
            font = ImageFont.truetype(Fonts.resolve().sans, 34)
            lines = wrap(d, scene, font, 1300)[:8]
        except Exception:
            font, lines = ImageFont.load_default(), [scene[:90]]
        y = 900 - 60 - 44 * len(lines)
        d.rectangle((100, y - 24, 1500, 860), fill=(0, 0, 0))
        for line in lines:
            d.text((130, y), line, font=font, fill=(240, 236, 226))
            y += 44
        out = io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue()


def image_prompt(style: str, negative: str, scene: str, depict_christ: bool) -> str:
    guard = "" if depict_christ else " Do not depict Jesus, God, or any divine figure."
    return f"{style} {negative}{guard} SCENE: {scene.strip()}"


def motion_prompt(beat: Beat) -> str:
    return (
        f"Animate this illustration. {beat.motion.strip()} Keep exactly the same art style, colours, characters and composition "
        "as the image. Smooth, gentle, continuous motion with a slow cinematic camera move. No cuts. No text, captions, titles or logos. "
        "No speech or narration."
    )


def _key(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


def clip_seconds_for(duration: float) -> int:
    return int(max(MIN_CLIP_SECONDS, min(MAX_CLIP_SECONDS, round(duration + 0.4))))


def choose_animated(shots: list[Shot], share: float, max_stretch: float = 1.5) -> None:
    """Mark shots to animate until `share` of the running time is covered: hero beats first, spread through the program."""
    for s in shots:
        s.animate = False
    if share <= 0 or not shots:
        return
    total = shots[-1].end - shots[0].start
    eligible = [s for s in shots if s.duration <= MAX_CLIP_SECONDS * max_stretch and s.beat.card is None]
    order = sorted(eligible, key=lambda s: (not s.beat.hero, (s.index * 7919) % 101))  # heroes, then an even scatter
    covered = 0.0
    for s in order:
        if covered >= share * total:
            break
        s.animate = True
        s.clip_seconds = clip_seconds_for(s.duration)
        covered += s.duration


@dataclass
class Estimate:
    new_images: int
    cached_images: int
    new_clip_seconds: int
    cached_clips: int
    image_usd: float
    video_usd: float
    storyboard_usd: float

    @property
    def total(self) -> float:
        return self.image_usd + self.video_usd + self.storyboard_usd


def assign_paths(shots: list[Shot], assets_dir: Path, style: str, negative: str, depict_christ: bool, motion_model: str) -> None:
    for s in shots:
        prompt = image_prompt(style, negative, s.beat.scene, depict_christ)
        s.image = assets_dir / f"img_{_key(prompt)}.png"
        s.clip = assets_dir / f"clip_{_key(prompt, s.beat.motion, str(s.clip_seconds), motion_model)}.mp4" if s.animate else None


def estimate(shots: list[Shot], image_usd: float, video_usd_per_second: float, storyboard_usd: float) -> Estimate:
    new_images = len({s.image for s in shots if s.image and not s.image.exists()})
    cached_images = len({s.image for s in shots if s.image and s.image.exists()})
    new_secs = sum(s.clip_seconds for s in shots if s.animate and s.clip and not s.clip.exists())
    cached_clips = sum(1 for s in shots if s.animate and s.clip and s.clip.exists())
    return Estimate(new_images, cached_images, new_secs, cached_clips, new_images * image_usd, new_secs * video_usd_per_second, storyboard_usd)


def fit_to_budget(shots: list[Shot], budget: float, image_usd: float, video_usd_per_second: float, storyboard_usd: float) -> int:
    """Drop animation from the least important shots until the estimate fits. Returns how many were dropped."""
    dropped = 0
    animated = sorted((s for s in shots if s.animate and s.clip and not s.clip.exists()), key=lambda s: (s.beat.hero, -s.clip_seconds))
    while animated and estimate(shots, image_usd, video_usd_per_second, storyboard_usd).total > budget:
        s = animated.pop(0)
        s.animate, s.clip, s.clip_seconds = False, None, 0
        dropped += 1
    return dropped


class Ledger:
    """Running record of what has been spent in this studio folder."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"images": 0, "clip_seconds": 0, "storyboards": 0, "usd": 0.0}
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def add(self, usd: float, **counts: int) -> None:
        with self.lock:
            for k, v in counts.items():
                self.data[k] = self.data.get(k, 0) + v
            self.data["usd"] = round(self.data["usd"] + usd, 4)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")


def _retry(fn, attempts: int = 3, wait: float = 4.0):
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # network hiccups, rate limits, an occasional empty result
            last = exc
            if i + 1 < attempts:
                time.sleep(wait * (i + 1))
    raise last  # type: ignore[misc]


@dataclass
class AssetReport:
    images_made: int = 0
    clips_made: int = 0
    image_failures: list[int] = field(default_factory=list)
    clip_failures: list[int] = field(default_factory=list)


def generate_assets(
    cfg: Config,
    shots: list[Shot],
    style: str,
    provider: ImageProvider,
    motion: MotionProvider | None,
    ledger: Ledger,
    image_usd: float,
    video_usd_per_second: float,
    workers: int = 4,
) -> AssetReport:
    report = AssetReport()
    lock = threading.Lock()
    todo: dict[Path, Shot] = {}
    for s in shots:
        if s.image and not s.image.exists():
            todo.setdefault(s.image, s)
    pending = list(todo.values())
    base_refs = list(cfg.illustrate.reference_images) + list(cfg.style.reference_images)

    def draw(s: Shot, refs: list[Path]) -> None:
        prompt = image_prompt(style, cfg.style.negative, s.beat.scene, cfg.style.depict_christ)
        try:
            data = _retry(lambda: provider.generate(prompt, "16:9", refs[-3:]))
            s.image.parent.mkdir(parents=True, exist_ok=True)
            s.image.write_bytes(data)
            ledger.add(image_usd, images=1)
            with lock:
                report.images_made += 1
            log.info("Picture %d/%d done", report.images_made, len(pending))
        except Exception as exc:
            log.warning("Picture for shot %d failed (%s); the previous picture will be held", s.index + 1, exc)
            with lock:
                report.image_failures.append(s.index)

    # The first two pictures are drawn one after the other and become the style anchors for all the rest.
    anchors: list[Path] = [s.image for s in shots if s.image and s.image.exists()][:2]
    while pending and len(anchors) < 2:
        s = pending.pop(0)
        draw(s, base_refs + anchors)
        if s.image.exists():
            anchors.append(s.image)
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(lambda s: draw(s, base_refs + anchors), pending))

    # Shots whose picture failed borrow their neighbour's, so the render always completes.
    for i, s in enumerate(shots):
        if s.image and not s.image.exists():
            donor = next((o.image for o in reversed(shots[:i]) if o.image and o.image.exists()), None) or \
                    next((o.image for o in shots[i + 1:] if o.image and o.image.exists()), None)
            if donor is None:
                raise RuntimeError("no pictures could be generated; check the image API key and model name")
            s.image, s.animate, s.clip = donor, False, None

    clips = [s for s in shots if s.animate and s.clip and not s.clip.exists()]
    if clips and motion is None:
        raise RuntimeError("animated shots were planned but no motion provider is configured")

    def move(s: Shot) -> None:
        try:
            data = _retry(lambda: motion.animate(s.image, motion_prompt(s.beat), s.clip_seconds), attempts=2, wait=8.0)
            s.clip.write_bytes(data)
            ledger.add(s.clip_seconds * video_usd_per_second, clip_seconds=s.clip_seconds)
            with lock:
                report.clips_made += 1
            log.info("Animated clip %d/%d done (%ds)", report.clips_made, len(clips), s.clip_seconds)
        except Exception as exc:
            log.warning("Animation for shot %d failed (%s); the still picture will be used", s.index + 1, exc)
            s.animate, s.clip = False, None
            with lock:
                report.clip_failures.append(s.index)

    if clips:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(move, clips))
    return report
