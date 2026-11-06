"""Load config.toml into a plain, validated structure."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Paths:
    root: Path
    input_dir: Path
    output_dir: Path
    assets_dir: Path


@dataclass
class Ministry:
    program_name: str = "Program"
    speaker: str = ""
    organization: str = ""
    audience_notes: str = ""
    logo: Path | None = None
    speaker_photo: Path | None = None


@dataclass
class Style:
    prompt: str = ""
    negative: str = "No text, letters, numbers, logos or watermarks anywhere in the image."
    reference_images: list[Path] = field(default_factory=list)
    depict_christ: bool = False
    avoid: list[str] = field(default_factory=list)


@dataclass
class Extract:
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    pull_quotes: int = 3
    visual_concepts: int = 2


@dataclass
class Transcribe:
    provider: str = "gemini"
    model: str = "gemini-3.5-flash-lite"


@dataclass
class Images:
    provider: str = "gemini"
    gemini_model: str = "gemini-3.1-flash-image"
    openai_model: str = "gpt-image-2"
    candidates: int = 2
    image_size: str = "2K"
    recompose: bool = True


@dataclass
class Compose:
    formats: list[str] = field(default_factory=lambda: ["youtube", "square", "vertical"])
    font_serif: str = "auto"
    font_sans: str = "auto"


@dataclass
class Video:
    enabled: bool = True
    burn_captions: bool = True
    show_verses: bool = True


@dataclass
class Verses:
    enabled: bool = True
    max_words: int = 55
    min_page_seconds: float = 4.0


@dataclass
class Illustrate:
    segment_seconds: int = 60
    min_seconds: float = 8.0
    plan_model: str = "gemini-3.6-flash"
    style: str = (
        "Hand-drawn black ink marker sketch on off-white paper, loose confident linework, light cross-hatching, "
        "generous white space, one accent colour (burnt orange) used sparingly, the feel of a whiteboard illustrator's drawing."
    )
    reference_images: list[Path] = field(default_factory=list)
    chain_reference: bool = True
    fps: int = 24
    crossfade: float = 0.6
    push_in: bool = True
    title_seconds: float = 4.0


@dataclass
class Config:
    paths: Paths
    ministry: Ministry
    style: Style
    extract: Extract
    transcribe: Transcribe
    images: Images
    compose: Compose
    video: Video
    verses: Verses
    illustrate: Illustrate


def _asset(assets_dir: Path, name: str | None) -> Path | None:
    if not name:
        return None
    p = assets_dir / name
    return p if p.exists() else None


def load_config(path: str | os.PathLike = "config.toml") -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `python -m sermon_visuals init` to create one from config.example.toml."
        )
    with path.open("rb") as f:
        raw = tomllib.load(f)
    root = path.resolve().parent

    p = raw.get("paths", {})
    paths = Paths(
        root=root,
        input_dir=root / p.get("input_dir", "programs"),
        output_dir=root / p.get("output_dir", "output"),
        assets_dir=root / p.get("assets_dir", "assets"),
    )
    m = raw.get("ministry", {})
    ministry = Ministry(
        program_name=m.get("program_name", "Program"),
        speaker=m.get("speaker", ""),
        organization=m.get("organization", ""),
        audience_notes=" ".join(m.get("audience_notes", "").split()),
        logo=_asset(paths.assets_dir, m.get("logo")),
        speaker_photo=_asset(paths.assets_dir, m.get("speaker_photo")),
    )
    s = raw.get("style", {})
    refs = [paths.assets_dir / r for r in s.get("reference_images", [])]
    missing = [str(r) for r in refs if not r.exists()]
    if missing:
        raise FileNotFoundError(f"style.reference_images not found: {', '.join(missing)}")
    style = Style(
        prompt=" ".join(s.get("prompt", "").split()),
        negative=s.get("negative", Style.negative),
        reference_images=refs,
        depict_christ=bool(s.get("depict_christ", False)),
        avoid=list(s.get("avoid", [])),
    )
    e = raw.get("extract", {})
    extract = Extract(**{k: e[k] for k in Extract.__dataclass_fields__ if k in e})
    t = raw.get("transcribe", {})
    transcribe = Transcribe(**{k: t[k] for k in Transcribe.__dataclass_fields__ if k in t})
    i = raw.get("images", {})
    images = Images(**{k: i[k] for k in Images.__dataclass_fields__ if k in i})
    c = raw.get("compose", {})
    compose = Compose(**{k: c[k] for k in Compose.__dataclass_fields__ if k in c})
    v = raw.get("video", {})
    video = Video(**{k: v[k] for k in Video.__dataclass_fields__ if k in v})
    ve = raw.get("verses", {})
    verses = Verses(**{k: ve[k] for k in Verses.__dataclass_fields__ if k in ve})
    il = raw.get("illustrate", {})
    il_refs = [paths.assets_dir / r for r in il.get("reference_images", [])]
    missing = [str(r) for r in il_refs if not r.exists()]
    if missing:
        raise FileNotFoundError(f"illustrate.reference_images not found: {', '.join(missing)}")
    illustrate = Illustrate(
        **{k: il[k] for k in Illustrate.__dataclass_fields__ if k in il and k not in ("reference_images", "style")},
        reference_images=il_refs,
        style=" ".join(il.get("style", Illustrate.style).split()),
    )

    if images.provider not in ("gemini", "openai"):
        raise ValueError(f"images.provider must be gemini or openai, got {images.provider!r}")
    if transcribe.provider not in ("gemini", "openai", "none"):
        raise ValueError(f"transcribe.provider must be gemini, openai or none, got {transcribe.provider!r}")
    bad = [f for f in compose.formats if f not in ("youtube", "square", "vertical")]
    if bad:
        raise ValueError(f"compose.formats has unknown entries: {bad}")

    return Config(paths, ministry, style, extract, transcribe, images, compose, video, verses, illustrate)
