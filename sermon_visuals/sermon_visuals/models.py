"""Data shapes shared across stages. The Brief is what Claude returns; the manifest tracks progress."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class PullQuote(BaseModel):
    text: str = Field(description="Verbatim words of the speaker, 8 to 30 words.")
    why: str = Field(description="One sentence on why this line is worth sharing.")


class VisualConcept(BaseModel):
    name: str = Field(description="Two to four word label for the concept.")
    image_prompt: str = Field(
        description="Scene, mood, composition and light for an illustrator. No text, no lettering, no likeness of the speaker."
    )
    rationale: str = Field(description="How the image connects to the program's message.")


class Brief(BaseModel):
    title: str
    one_line: str = Field(description="One sentence a listener would read on a podcast app.")
    description: str = Field(description="Two or three sentences for a YouTube description.")
    primary_scripture: str = Field(description="Reference only, e.g. 'Matthew 5:3-12'. Empty string if none.")
    other_scriptures: list[str] = Field(default_factory=list)
    themes: list[str] = Field(description="Three to six short theme tags.")
    pull_quotes: list[PullQuote]
    visual_concepts: list[VisualConcept]
    thumbnail_text: str = Field(description="At most five words.")
    content_flags: list[str] = Field(default_factory=list)


class QuoteCheck(BaseModel):
    text: str
    verified: bool


class ArtCandidate(BaseModel):
    concept: int
    index: int
    path: str
    provider: str
    model: str
    prompt: str


Stage = Literal["transcript", "brief", "images", "compose", "video"]


class Manifest(BaseModel):
    """Per-program state. Written to <output>/<stem>/manifest.json after every stage."""

    stem: str
    audio: str | None = None
    transcript: str | None = None
    captions: str | None = None
    brief: str | None = None
    quote_checks: list[QuoteCheck] = Field(default_factory=list)
    art: list[ArtCandidate] = Field(default_factory=list)
    chosen_art: str | None = None
    cards: dict[str, str] = Field(default_factory=dict)
    video: str | None = None
    illustrated: str | None = None
    sketches: list[str] = Field(default_factory=list)
    verses: str | None = None
    done: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path, stem: str) -> "Manifest":
        if path.exists():
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        return cls(stem=stem)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    def mark(self, stage: str) -> None:
        if stage not in self.done:
            self.done.append(stage)
        self.errors.pop(stage, None)


def load_brief(path: Path) -> Brief:
    return Brief.model_validate(json.loads(path.read_text(encoding="utf-8")))
