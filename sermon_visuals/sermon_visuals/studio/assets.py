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
from ..imagegen import ImageProvider, content_guard
from .levels import is_whiteboard
from .plan import Beat

log = logging.getLogger(__name__)

MAX_CLIP_SECONDS = 15
MIN_CLIP_SECONDS = 3
MAX_REFERENCES = 3  # reference pictures sent with one picture request


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
    redraw: bool = False  # draw this picture again even though it is cached (the old one is kept beside it)

    @property
    def to_draw(self) -> bool:
        return bool(self.image) and (self.redraw or not self.image.exists())

    @property
    def to_animate(self) -> bool:
        """A clip still to be made: none yet, or its picture is to be made again (a clip is made from its picture)."""
        return bool(self.animate and self.clip) and (self.to_draw or not self.clip.exists())

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

    def generate(self, prompt: str, aspect: str, references: list[Path], *, scene_reference: bool = False) -> bytes:
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


def image_prompt(style: str, scene: str, guard: str) -> str:
    """`guard` is imagegen.content_guard(cfg). The scene stays last, after "SCENE:", so the draft art can find it."""
    return f"{style} {guard} SCENE: {scene.strip()}"


def motion_prompt(beat: Beat, guard: str) -> str:
    return (
        f"Animate this illustration. {beat.motion.strip()} Keep exactly the same art style, colours, characters and composition "
        "as the image. Smooth, gentle, continuous motion with a slow cinematic camera move. No cuts. No text, captions, titles or logos. "
        f"No speech or narration. {guard}"
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


def assign_paths(shots: list[Shot], assets_dir: Path, style: str, guard: str, motion_model: str) -> None:
    """Name each picture and clip after the exact prompt text it is made from, so any change to the
    scene, style or content guard makes a new file instead of reusing one drawn under older rules."""
    for s in shots:
        prompt = image_prompt(style, s.beat.scene, guard)
        s.image = assets_dir / f"img_{_key(prompt)}.png"
        s.clip = assets_dir / f"clip_{_key(prompt, motion_prompt(s.beat, guard), str(s.clip_seconds), motion_model)}.mp4" if s.animate else None


def estimate(shots: list[Shot], image_usd: float, video_usd_per_second: float, storyboard_usd: float) -> Estimate:
    new = {s.image for s in shots if s.to_draw}
    new_images = len(new)
    cached_images = len({s.image for s in shots if s.image and s.image.exists()} - new)
    new_secs = sum(s.clip_seconds for s in shots if s.to_animate)
    cached_clips = sum(1 for s in shots if s.animate and s.clip and s.clip.exists() and not s.to_animate)
    return Estimate(new_images, cached_images, new_secs, cached_clips, new_images * image_usd, new_secs * video_usd_per_second, storyboard_usd)


PENNY_FRACTION = 1e-6  # float slack, so "exactly what is left" is not refused over a rounding crumb


class OverBudget(RuntimeError):
    """Even with every animation turned back into a still, the pictures cost more than is left of the budget."""

    def __init__(self, cost: float, left: float):
        self.cost, self.left = cost, max(left, 0.0)
        super().__init__(
            f"The pictures would cost about ${cost:.2f}, but only ${self.left:.2f} is left of the budget, even with every "
            "planned animation turned back into a still. No pictures were generated and nothing was spent on them.")


def fit_to_budget(shots: list[Shot], budget: float, image_usd: float, video_usd_per_second: float, storyboard_usd: float) -> int:
    """Drop animation from the least important shots until the estimate fits `budget` (what is left to spend).
    Returns how many were dropped. Raises OverBudget if it still does not fit once every animation is gone, so
    nothing is generated whatever the caller was told to assume."""
    dropped = 0
    animated = sorted((s for s in shots if s.to_animate), key=lambda s: (s.beat.hero, -s.clip_seconds))
    # Written as "not fits" so a nonsense figure (NaN from a damaged spend.json) refuses instead of slipping through.
    while animated and not estimate(shots, image_usd, video_usd_per_second, storyboard_usd).total <= budget + PENNY_FRACTION:
        s = animated.pop(0)
        s.animate, s.clip, s.clip_seconds = False, None, 0
        dropped += 1
    cost = estimate(shots, image_usd, video_usd_per_second, storyboard_usd).total
    if not cost <= budget + PENNY_FRACTION:
        raise OverBudget(cost, budget)
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


def keep_aside(path: Path) -> Path:
    """Rename a picture about to be redrawn to <name>.before-redraw[-N].png and return the new path."""
    n, aside = 1, path.with_name(f"{path.stem}.before-redraw{path.suffix}")
    while aside.exists():
        n += 1
        aside = path.with_name(f"{path.stem}.before-redraw-{n}{path.suffix}")
    path.replace(aside)
    return aside


def install(path: Path, data: bytes) -> None:
    """Replace a cached file with new bytes, keeping the old one beside it. If anything fails part-way, the old
    file is put back where it was, so a failed redraw never loses the current picture."""
    fresh = path.with_name(path.stem + ".new" + path.suffix)
    fresh.write_bytes(data)
    aside = keep_aside(path)
    try:
        fresh.replace(path)
    except Exception:
        aside.replace(path)
        fresh.unlink(missing_ok=True)
        raise


def _retry(fn, attempts: int = 3, wait: float = 4.0, give_up=lambda exc: False):
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # network hiccups, rate limits, an occasional empty result
            last = exc
            if give_up(exc):
                break
            if i + 1 < attempts:
                time.sleep(wait * (i + 1))
    raise last  # type: ignore[misc]


def model_missing(exc: Exception) -> bool:
    """True for a "this model does not exist" style error, which no retry can fix. Google's Interactions API
    raises google.genai._interactions.NotFoundError (status_code 404); older google-genai errors carry `code`."""
    if getattr(exc, "status_code", None) == 404 or getattr(exc, "code", None) == 404:
        return True
    text = str(exc).lower()
    return "model" in text and any(p in text for p in ("not found", "does not exist", "unknown model"))


class _OverAllowance(RuntimeError):
    """A paid request was not sent because it would pass what is left of the budget."""


def over_allowance(exc: Exception) -> bool:
    return isinstance(exc, _OverAllowance)


@dataclass
class AssetReport:
    images_made: int = 0
    clips_made: int = 0
    image_failures: list[int] = field(default_factory=list)
    clip_failures: list[int] = field(default_factory=list)
    motion_stopped: str = ""  # plain-English reason animation was abandoned for the whole run, if it was
    budget_stopped: str = ""  # plain-English note if paid work was skipped because the budget would be passed


def picture_references(cfg: Config, anchors: list[Path]) -> list[Path]:
    """The reference pictures sent with one picture request, at most MAX_REFERENCES.

    Whiteboard styles: configured references always win over anchors (the first two pictures of the run). A fixed
    style kit (`[studio] style_refs`) is used instead of the anchors, ahead of the other configured references;
    without a kit the anchors only fill the room the configured references leave. Other styles keep the old rule:
    the last MAX_REFERENCES of the configured references followed by the anchors."""
    configured = list(cfg.illustrate.reference_images) + list(cfg.style.reference_images)
    if not is_whiteboard(cfg.studio.style):
        return (configured + list(anchors))[-MAX_REFERENCES:]
    if cfg.studio.style_refs:
        return (list(cfg.studio.style_refs) + configured)[:MAX_REFERENCES]
    return (configured + list(anchors))[:MAX_REFERENCES]


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
    budget_left: float | None = None,
) -> AssetReport:
    """`budget_left` is what may still be spent in this run. Each picture and clip reserves its cost before it is
    asked for, and is skipped if that would pass the budget, so more work than was estimated (a cached file
    deleted after the question was answered, say) can never be paid for."""
    report = AssetReport()
    lock = threading.Lock()
    reserved = 0.0

    def reserve(cost: float) -> bool:
        nonlocal reserved
        if budget_left is None:
            return True
        with lock:
            if not reserved + cost <= budget_left + PENNY_FRACTION:
                report.budget_stopped = ("Some pictures or animations were skipped because making them would have gone past "
                                         "what is left of the budget; neighbouring pictures or stills were used instead.")
                return False
            reserved += cost
            return True

    def paid(cost: float, call):
        """One request the provider may bill: reserve its cost first. Every attempt, retries included, reserves
        its own cost, because a reply that times out may still have been charged."""
        if not reserve(cost):
            raise _OverAllowance("making it would go past what is left of the budget")
        return call()
    todo: dict[Path, Shot] = {}
    made: set[Path] = set()  # pictures made in this run: any clip of theirs must be made again from them
    for s in shots:
        if s.to_draw:
            todo.setdefault(s.image, s)
    pending = list(todo.values())
    total = len(pending)  # taken now: the style anchors are popped off `pending` below
    guard = content_guard(cfg)
    kit = is_whiteboard(cfg.studio.style) and bool(cfg.studio.style_refs)
    configured = len(cfg.studio.style_refs if kit else []) + len(cfg.illustrate.reference_images) + len(cfg.style.reference_images)
    if pending and is_whiteboard(cfg.studio.style) and configured > MAX_REFERENCES:
        log.warning("%d reference pictures are configured but only %d are sent with each picture; the first %d are used "
                    "(style_refs first)", configured, MAX_REFERENCES, MAX_REFERENCES)

    def draw(s: Shot, refs: list[Path]) -> bool:
        prompt = image_prompt(style, s.beat.scene, guard)
        try:
            data = _retry(lambda: paid(image_usd, lambda: provider.generate(prompt, "16:9", refs)), give_up=over_allowance)
            s.image.parent.mkdir(parents=True, exist_ok=True)
            if s.image.exists():  # a redraw: keep the old picture beside the new one, never lose it
                install(s.image, data)
            else:
                s.image.write_bytes(data)
            ledger.add(image_usd, images=1)
            with lock:
                made.add(s.image)
                report.images_made += 1
            log.info("Picture %d/%d done", report.images_made, total)
            return True
        except Exception as exc:
            held = "the current picture is kept" if s.image.exists() else "the previous picture will be held"
            log.warning("Picture for shot %d failed (%s); %s", s.index + 1, exc, held)
            with lock:
                report.image_failures.append(s.index)
            return False

    # The first two pictures are drawn one after the other and become the style anchors for all the rest. A picture
    # being redrawn is never an anchor: it would steer its replacement back toward itself. A whiteboard style with a
    # fixed reference kit has no anchors: every picture is drawn from the kit, all in parallel.
    redrawn = {s.image for s in pending}
    anchors: list[Path] = [] if kit else \
        [s.image for s in shots if s.image and s.image.exists() and s.image not in redrawn][:2]
    while pending and not kit and len(anchors) < 2:
        s = pending.pop(0)
        if draw(s, picture_references(cfg, anchors)):
            anchors.append(s.image)
    if pending:
        refs = picture_references(cfg, anchors)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(lambda s: draw(s, refs), pending))

    # Shots whose picture failed borrow their neighbour's, so the render always completes.
    for i, s in enumerate(shots):
        if s.image and not s.image.exists():
            donor = next((o.image for o in reversed(shots[:i]) if o.image and o.image.exists()), None) or \
                    next((o.image for o in shots[i + 1:] if o.image and o.image.exists()), None)
            if donor is None:
                raise RuntimeError("no pictures could be generated; check the image API key and model name")
            s.image, s.animate, s.clip = donor, False, None

    # A clip is made when there is none, or when its picture was made in this run (a picture that could not be
    # redrawn keeps its clip: animating the old picture again would be wasted).
    clips = [s for s in shots if s.animate and s.clip and (not s.clip.exists() or s.image in made)]
    for s in clips:
        if s.clip.exists():  # its picture is new: the old clip goes aside now, so a failure cannot leave it in use
            keep_aside(s.clip)
    if clips and motion is None:
        raise RuntimeError("animated shots were planned but no motion provider is configured")

    def move(s: Shot) -> None:
        if report.motion_stopped:  # the animation model is missing; do not try (or pay for) any more clips
            s.animate, s.clip = False, None
            with lock:
                report.clip_failures.append(s.index)
            return
        try:
            data = _retry(lambda: paid(s.clip_seconds * video_usd_per_second,
                                       lambda: motion.animate(s.image, motion_prompt(s.beat, guard), s.clip_seconds)),
                          attempts=2, wait=8.0, give_up=lambda e: model_missing(e) or over_allowance(e))
            s.clip.write_bytes(data)
            ledger.add(s.clip_seconds * video_usd_per_second, clip_seconds=s.clip_seconds)
            with lock:
                report.clips_made += 1
            log.info("Animated clip %d/%d done (%ds)", report.clips_made, len(clips), s.clip_seconds)
        except Exception as exc:
            s.animate, s.clip = False, None
            with lock:
                report.clip_failures.append(s.index)
                if model_missing(exc) and not report.motion_stopped:
                    name = getattr(motion, "model", "") or "the animation model"
                    report.motion_stopped = (
                        f"Google could not find the animation model \"{name}\" (or the service that runs it), so no more "
                        "clips were tried in this run and still pictures were used instead. Check the motion_model "
                        "line under [studio] in config.toml.")
                    log.warning(report.motion_stopped)
                    return
            if not model_missing(exc):
                log.warning("Animation for shot %d failed (%s); the still picture will be used", s.index + 1, exc)

    if clips:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(move, clips))
    return report
