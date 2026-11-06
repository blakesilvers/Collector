"""Tiny .env loader and key preflight, so nobody has to learn `export`."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .config import Config

KEY_INFO = {
    "ANTHROPIC_API_KEY": "the brief (titles, quotes, image ideas). Get one at https://console.anthropic.com/settings/keys",
    "GEMINI_API_KEY": "images with Google Nano Banana. Get one at https://aistudio.google.com/apikey",
    "OPENAI_API_KEY": "images with OpenAI and/or transcription. Get one at https://platform.openai.com/api-keys",
}

ENV_TEMPLATE = """# API keys for sermon-visuals. One per line, no quotes. This file is private; do not share or commit it.
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
OPENAI_API_KEY=
"""


def load_dotenv(path: Path) -> list[str]:
    """Load KEY=VALUE lines into os.environ without overriding values already set. Returns keys loaded."""
    loaded = []
    if not path.exists():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def required_keys(cfg: Config, stages: list[str]) -> list[str]:
    keys = []
    if "brief" in stages:
        keys.append("ANTHROPIC_API_KEY")
    if "images" in stages:
        keys.append("GEMINI_API_KEY" if cfg.images.provider == "gemini" else "OPENAI_API_KEY")
    if "transcript" in stages and cfg.transcribe.provider == "openai":
        keys.append("OPENAI_API_KEY")
    return sorted(set(keys))


def missing_keys(cfg: Config, stages: list[str]) -> list[str]:
    return [k for k in required_keys(cfg, stages) if not os.environ.get(k)]


def explain_missing(keys: list[str], env_path: Path) -> str:
    lines = [f"Missing API key(s). Open {env_path} in a text editor and fill in:"]
    for k in keys:
        lines.append(f"  {k}   needed for {KEY_INFO.get(k, '')}")
    return "\n".join(lines)


def doctor(cfg: Config, env_path: Path) -> list[tuple[bool, str]]:
    """Return (ok, message) checks for the `doctor` command."""
    from . import compose
    from .pipeline import discover_stems

    checks: list[tuple[bool, str]] = []
    v = sys.version_info
    checks.append((v >= (3, 11), f"Python {v.major}.{v.minor}.{v.micro} (needs 3.11 or newer)"))
    checks.append((shutil.which("ffmpeg") is not None, "ffmpeg on PATH (needed for the video stage; brew install ffmpeg / winget install ffmpeg / apt install ffmpeg)"))
    try:
        fonts = compose.Fonts.resolve(cfg.compose.font_serif, cfg.compose.font_sans)
        checks.append((True, f"fonts: {Path(fonts.serif).name}, {Path(fonts.sans).name}"))
    except FileNotFoundError as exc:
        checks.append((False, str(exc)))
    checks.append((env_path.exists(), f".env file at {env_path}"))
    for k in required_keys(cfg, ["transcript", "brief", "images"]):
        checks.append((bool(os.environ.get(k)), f"{k} set (for {KEY_INFO[k].split('.')[0]})"))
    checks.append((cfg.ministry.logo is not None, f"logo in {cfg.paths.assets_dir} (optional)"))
    checks.append((cfg.ministry.speaker_photo is not None, f"speaker photo in {cfg.paths.assets_dir} (optional)"))
    n = len(discover_stems(cfg))
    checks.append((n > 0, f"{n} program(s) found in {cfg.paths.input_dir}"))
    return checks
