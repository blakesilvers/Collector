"""Transcript acquisition. Prefers a transcript file next to the audio; otherwise calls OpenAI transcription."""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac")


def find_audio(input_dir: Path, stem: str) -> Path | None:
    for ext in AUDIO_EXTS:
        p = input_dir / f"{stem}{ext}"
        if p.exists():
            return p
    return None


def find_sidecar(input_dir: Path, stem: str, ext: str) -> Path | None:
    p = input_dir / f"{stem}{ext}"
    return p if p.exists() else None


def get_transcript(cfg: Config, stem: str, audio: Path | None, out_dir: Path) -> Path:
    """Return a path to a plain-text transcript, creating it from audio if allowed."""
    existing = find_sidecar(cfg.paths.input_dir, stem, ".txt")
    if existing:
        return existing
    cached = out_dir / "transcript.txt"
    if cached.exists():
        return cached
    if cfg.transcribe.provider == "none":
        raise FileNotFoundError(
            f"No transcript for {stem}: add {stem}.txt to {cfg.paths.input_dir} or set transcribe.provider = \"openai\"."
        )
    if audio is None:
        raise FileNotFoundError(f"No audio or transcript found for {stem} in {cfg.paths.input_dir}.")

    from openai import OpenAI

    client = OpenAI()
    log.info("Transcribing %s with %s", audio.name, cfg.transcribe.model)
    with audio.open("rb") as f:
        result = client.audio.transcriptions.create(
            model=cfg.transcribe.model,
            file=f,
            response_format="text",
        )
    text = result if isinstance(result, str) else getattr(result, "text", str(result))
    out_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(text.strip() + "\n", encoding="utf-8")
    return cached
