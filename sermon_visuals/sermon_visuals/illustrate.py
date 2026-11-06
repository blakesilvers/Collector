"""Illustrated-narration video: the program split into idea-sized segments, one ink sketch per
segment in a consistent hand-drawn style, assembled over the audio with a slow push-in and
dissolves. A cousin of the whiteboard-illustrator format, built from stills."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from pydantic import BaseModel, Field

from .config import Config
from .imagegen import ImageProvider, make_provider
from .verses import Overlay, Scripture, overlay_filter, validate_scriptures
from .video import ffmpeg_available

log = logging.getLogger(__name__)


class Segment(BaseModel):
    start: float = Field(description="Start time in seconds from the beginning of the audio.")
    end: float = Field(description="End time in seconds.")
    gist: str = Field(description="What the speaker is saying in this stretch, at most 20 words.")
    illustration: str = Field(
        description="A concrete scene for an illustrator: objects, people, setting, action. No text or lettering."
    )


class SegmentPlan(BaseModel):
    segments: list[Segment]
    scriptures: list[Scripture] = Field(default_factory=list)


SEGMENT_PROMPT = """You are planning illustrations for a narrated program, in the manner of a whiteboard illustrator
who draws what the narrator is saying as he says it. You are given the audio and its transcript.

Divide the whole program into consecutive segments of roughly {target} seconds each (shorter where
the idea changes, longer where one image carries the point). Segments must start at 0, be in order,
not overlap, leave no gaps, and end at the end of the audio. Use the audio for the timings, not a
guess from the text.

For each segment give the gist and one illustration: a concrete, drawable scene that embodies the
point being made. Prefer objects, places, hands, scrolls, lamps, roads, buildings and symbolic
compositions. No text, lettering, numbers or logos in the scene. Do not depict the narrator.
{guardrails}

Separately, list every scripture passage the narrator reads or quotes aloud, as "scriptures": the
reference, the translation label if the transcript gives one, the words exactly as read aloud (verse
numbers omitted), and the seconds at which the reading starts and ends, from the audio. Include
passages he reads from the transcript's scripture blocks and verses he quotes from memory. Do not
include passages he merely mentions by reference without reading. If he reads a passage in parts
with commentary between, list each part separately.

<transcript>
{transcript}
</transcript>"""


def audio_duration(audio: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def validate_segments(segments: list[Segment], duration: float, minimum: float = 8.0) -> list[Segment]:
    """Force the plan to be contiguous and inside the audio; merge slivers into their predecessor."""
    segs = sorted((s for s in segments if s.end > s.start), key=lambda s: s.start)
    if not segs:
        raise ValueError("segment plan is empty")
    fixed: list[Segment] = []
    for s in segs:
        start = fixed[-1].end if fixed else 0.0
        end = min(max(s.end, start), duration)
        if end - start < minimum and fixed:
            fixed[-1].end = end  # too short: extend the previous segment over it
            continue
        fixed.append(Segment(start=start, end=end, gist=s.gist, illustration=s.illustration))
    fixed[-1].end = duration
    return [s for s in fixed if s.end > s.start]


def plan_segments(cfg: Config, audio: Path, transcript: str) -> tuple[list[Segment], list[Scripture]]:
    from google import genai
    from google.genai import types

    client = genai.Client()
    uploaded = client.files.upload(file=audio)
    while uploaded.state == types.FileState.PROCESSING:
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
    if uploaded.state == types.FileState.FAILED:
        raise RuntimeError(f"Gemini could not process {audio.name}.")
    guardrails = "" if cfg.style.depict_christ else "Never depict Jesus or God in any form; use symbols and scenes instead."
    if cfg.style.avoid:
        guardrails += " Avoid: " + "; ".join(cfg.style.avoid) + "."
    prompt = SEGMENT_PROMPT.format(target=cfg.illustrate.segment_seconds, guardrails=guardrails, transcript=transcript.strip())
    log.info("Planning segments with %s", cfg.illustrate.plan_model)
    try:
        response = client.models.generate_content(
            model=cfg.illustrate.plan_model,
            contents=[uploaded, prompt],
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
                response_schema=SegmentPlan,
                max_output_tokens=65536,
            ),
        )
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass
    plan = SegmentPlan.model_validate_json(response.text or "{}")
    duration = audio_duration(audio)
    return validate_segments(plan.segments, duration, cfg.illustrate.min_seconds), validate_scriptures(plan.scriptures, duration)


def sketch_prompt(cfg: Config, scene: str) -> str:
    return " ".join(p for p in [cfg.illustrate.style, scene.strip(), cfg.style.negative] if p)


def draw_segments(cfg: Config, segments: list[Segment], out_dir: Path, provider: ImageProvider | None = None) -> list[Path]:
    """One sketch per segment. Each request carries the previous sketch as a style reference so the hand stays the same."""
    provider = provider or make_provider(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    previous: Path | None = None
    refs_base = list(cfg.illustrate.reference_images)
    for i, seg in enumerate(segments, start=1):
        out = out_dir / f"seg_{i:03d}.png"
        if not out.exists():
            refs = refs_base + ([previous] if previous and cfg.illustrate.chain_reference else [])
            log.info("Sketch %d/%d: %s", i, len(segments), seg.gist[:60])
            out.write_bytes(provider.generate(sketch_prompt(cfg, seg.illustration), "16:9", refs[-3:]))
        paths.append(out)
        previous = out
    return paths


def build_assembly_command(
    frames: list[tuple[Path, float]],
    audio: Path,
    out: Path,
    fps: int = 24,
    fade: float = 0.6,
    width: int = 1920,
    height: int = 1080,
    push_in: bool = True,
    overlays: list[Overlay] | None = None,
) -> list[str]:
    """ffmpeg command: each still shown for its duration with a slow push-in, dissolving into the next."""
    if not frames:
        raise ValueError("no frames to assemble")
    fade = min(fade, min(d for _, d in frames) / 2)
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for path, _ in frames:
        cmd += ["-i", str(path)]
    cmd += ["-i", str(audio)]
    parts = []
    for i, (_, dur) in enumerate(frames):
        n = max(int(round(dur * fps)), 1)
        zoom = f"zoompan=z='min(zoom+{0.08 / n:.6f},1.08)':d={n}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps}" if push_in else f"loop=loop={n - 1}:size=1:start=0,fps={fps}"
        parts.append(f"[{i}:v]scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,crop={width * 2}:{height * 2},setsar=1,{zoom},format=yuv420p[v{i}]")
    if len(frames) == 1:
        last = "v0"
    else:
        offset = 0.0
        prev = "v0"
        for i in range(1, len(frames)):
            offset += frames[i - 1][1] - fade
            label = f"x{i}"
            parts.append(f"[{prev}][v{i}]xfade=transition=fade:duration={fade}:offset={offset:.3f}[{label}]")
            prev = label
        last = prev
    ov_inputs, ov_filter, last = overlay_filter(last, len(frames) + 1, overlays or [])
    cmd += ov_inputs
    if ov_filter:
        parts.append(ov_filter)
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", f"[{last}]", "-map", f"{len(frames)}:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-r", str(fps),
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest", str(out),
    ]
    return cmd


def assemble(frames: list[tuple[Path, float]], audio: Path, out: Path, fps: int, fade: float, push_in: bool, overlays: list[Overlay] | None = None) -> Path:
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed or not on PATH.")
    out.parent.mkdir(parents=True, exist_ok=True)
    log.info("Assembling %d illustrations (%d verse overlays) into %s", len(frames), len(overlays or []), out.name)
    subprocess.run(build_assembly_command(frames, audio, out, fps=fps, fade=fade, push_in=push_in, overlays=overlays), check=True)
    return out


def write_plan(segments: list[Segment], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([s.model_dump() for s in segments], indent=2), encoding="utf-8")


def read_plan(path: Path) -> list[Segment]:
    return [Segment.model_validate(d) for d in json.loads(path.read_text(encoding="utf-8"))]


def write_scriptures(items: list[Scripture], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([s.model_dump() for s in items], indent=2), encoding="utf-8")


def read_scriptures(path: Path) -> list[Scripture]:
    return [Scripture.model_validate(d) for d in json.loads(path.read_text(encoding="utf-8"))]


def write_srt(segments: list[Segment], path: Path) -> None:
    """Segment gists as a caption track, handy for checking sync in a player."""
    def ts(t: float) -> str:
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"
    lines = []
    for i, seg in enumerate(segments, start=1):
        lines += [str(i), f"{ts(seg.start)} --> {ts(seg.end)}", seg.gist, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
