"""Stage 5: still frame + audio -> MP4 for YouTube, with optional burned-in captions from an SRT file."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .verses import Overlay, overlay_filter

log = logging.getLogger(__name__)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _escape_filter_path(p: Path) -> str:
    # ffmpeg filter arguments treat ':' and '\' and "'" specially.
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def build_command(frame: Path, audio: Path, out: Path, captions: Path | None = None, width: int = 1920, height: int = 1080, overlays: list[Overlay] | None = None) -> list[str]:
    vf = [f"scale={width}:{height}", "format=yuv420p"]
    if captions:
        style = "FontName=DejaVu Sans,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=3,Alignment=2,MarginV=48"
        vf.append(f"subtitles='{_escape_filter_path(captions)}':force_style='{style}'")
    fps = 2 if not overlays else 12  # overlays fade in and out, which needs a few frames per second
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", str(fps), "-i", str(frame),
        "-i", str(audio),
    ]
    ov_inputs, ov_filter, last = overlay_filter("base", 2, overlays or [])
    cmd += ov_inputs
    if ov_filter:
        cmd += ["-filter_complex", f"[0:v]{','.join(vf)}[base];{ov_filter}", "-map", f"[{last}]", "-map", "1:a"]
    else:
        cmd += ["-vf", ",".join(vf)]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-crf", "23",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        "-shortest", str(out),
    ]
    return cmd


def render_video(frame: Path, audio: Path, out: Path, captions: Path | None = None, overlays: list[Overlay] | None = None) -> Path:
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed or not on PATH.")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_command(frame, audio, out, captions, overlays=overlays)
    log.info("Rendering video %s", out.name)
    subprocess.run(cmd, check=True)
    return out
