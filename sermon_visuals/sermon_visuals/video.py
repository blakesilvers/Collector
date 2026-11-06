"""Stage 5: still frame + audio -> MP4 for YouTube, with optional burned-in captions from an SRT file."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _escape_filter_path(p: Path) -> str:
    # ffmpeg filter arguments treat ':' and '\' and "'" specially.
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def build_command(frame: Path, audio: Path, out: Path, captions: Path | None = None, width: int = 1920, height: int = 1080) -> list[str]:
    vf = [f"scale={width}:{height}", "format=yuv420p"]
    if captions:
        style = "FontName=DejaVu Sans,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=3,Alignment=2,MarginV=48"
        vf.append(f"subtitles='{_escape_filter_path(captions)}':force_style='{style}'")
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", "2", "-i", str(frame),
        "-i", str(audio),
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-crf", "23",
        "-vf", ",".join(vf),
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        "-shortest", str(out),
    ]


def render_video(frame: Path, audio: Path, out: Path, captions: Path | None = None) -> Path:
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed or not on PATH.")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_command(frame, audio, out, captions)
    log.info("Rendering video %s", out.name)
    subprocess.run(cmd, check=True)
    return out
