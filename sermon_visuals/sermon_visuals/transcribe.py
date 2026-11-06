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

    log.info("Transcribing %s with %s (%s)", audio.name, cfg.transcribe.model, cfg.transcribe.provider)
    if cfg.transcribe.provider == "gemini":
        text = _transcribe_gemini(audio, cfg.transcribe.model)
    else:
        text = _transcribe_openai(audio, cfg.transcribe.model)
    if not text.strip():
        raise RuntimeError(f"Transcription of {audio.name} came back empty.")
    out_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(text.strip() + "\n", encoding="utf-8")
    return cached


def _transcribe_openai(audio: Path, model: str) -> str:
    from openai import OpenAI

    client = OpenAI()
    with audio.open("rb") as f:
        result = client.audio.transcriptions.create(model=model, file=f, response_format="text")
    return result if isinstance(result, str) else getattr(result, "text", str(result))


TRANSCRIBE_PROMPT = (
    "Transcribe this audio word for word. Output only the spoken words as plain paragraphs: "
    "no timestamps, no speaker labels, no headings, no commentary, no summary. "
    "Keep every sentence; do not condense or paraphrase."
)


def _transcribe_gemini(audio: Path, model: str) -> str:
    """Upload the audio with the Files API, wait until it is ready, then ask a Gemini text model to transcribe it."""
    import time

    from google import genai
    from google.genai import types

    client = genai.Client()  # reads GEMINI_API_KEY
    uploaded = client.files.upload(file=audio)
    while uploaded.state == types.FileState.PROCESSING:
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
    if uploaded.state == types.FileState.FAILED:
        raise RuntimeError(f"Gemini could not process {audio.name}.")
    try:
        response = client.models.generate_content(
            model=model,
            contents=[uploaded, TRANSCRIBE_PROMPT],
            config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=65536),
        )
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:  # best effort; uploads expire on their own
            pass
    return response.text or ""
