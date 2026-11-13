"""Stage 3: visual concept -> artwork PNGs. Two providers; both receive the same house-style prompt."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Protocol

from .config import Config

log = logging.getLogger(__name__)

# (aspect ratio label, OpenAI size string) per output format.
ASPECTS = {
    "youtube": ("16:9", "1536x1024"),
    "square": ("1:1", "1024x1024"),
    "vertical": ("9:16", "1024x1536"),
}


NO_TEXT_FALLBACK = "No text, letters, numbers, logos or watermarks anywhere in the image."
NO_SCRIPT = (
    "No written words, letters, script, inscriptions or scroll text in any language; "
    "show scrolls, books, tablets and signs closed, blank or turned away."
)
NO_DIVINE = "Do not depict Jesus, God, or any divine figure in any form, including as an infant."


def content_guard(cfg: Config) -> str:
    """The rules added to every image and video prompt: no text of any kind, no depiction of God
    (unless depict_christ is switched on) and nothing on the config's avoid list. These are instructions
    to the model, not a check on what it draws."""
    parts = [cfg.style.negative.strip() or NO_TEXT_FALLBACK, NO_SCRIPT]
    if not cfg.style.depict_christ:
        parts.append(NO_DIVINE)
    avoid = [a.strip() for a in cfg.style.avoid if a.strip()]
    if avoid:
        parts.append("Do not show: " + "; ".join(avoid) + ".")
    return " ".join(parts)


def build_prompt(cfg: Config, concept_prompt: str) -> str:
    parts = [cfg.style.prompt, concept_prompt.strip(), content_guard(cfg)]
    return " ".join(p for p in parts if p)


def recompose_prompt(cfg: Config, aspect: str) -> str:
    return (
        f"Recompose this exact scene as a {aspect} image. Keep the same subject, palette, lighting and painterly style. "
        "Keep the main subject fully in frame and roughly centred, and extend the scene naturally to fill the new frame. "
        + content_guard(cfg)
    )


class ImageProvider(Protocol):
    name: str
    model: str

    def generate(self, prompt: str, aspect: str, references: list[Path]) -> bytes: ...


class GeminiProvider:
    name = "gemini"

    def __init__(self, model: str, image_size: str = "2K"):
        from google import genai

        self.model = model
        self.image_size = image_size
        self.client = genai.Client()  # reads GEMINI_API_KEY

    def generate(self, prompt: str, aspect: str, references: list[Path]) -> bytes:
        from google.genai import types
        from PIL import Image

        contents: list = []
        if references:
            contents.append("Match the visual style of the attached reference image(s). ")
            contents.extend(Image.open(r) for r in references)
        contents.append(prompt)
        response = self.client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=aspect, image_size=self.image_size),
            ),
        )
        for cand in response.candidates or []:
            for part in (cand.content.parts if cand.content else []) or []:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data
        raise RuntimeError(f"Gemini returned no image for prompt: {prompt[:80]}...")


class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str):
        from openai import OpenAI

        self.model = model
        self.client = OpenAI()  # reads OPENAI_API_KEY

    def generate(self, prompt: str, aspect: str, references: list[Path]) -> bytes:
        size = next((s for a, s in ASPECTS.values() if a == aspect), "1536x1024")
        if references:
            files = [open(r, "rb") for r in references]
            try:
                result = self.client.images.edit(
                    model=self.model,
                    image=files,
                    prompt="Match the visual style of the attached reference image(s). " + prompt,
                    size=size,
                    n=1,
                )
            finally:
                for f in files:
                    f.close()
        else:
            result = self.client.images.generate(
                model=self.model, prompt=prompt, size=size, quality="medium", n=1, output_format="png"
            )
        data = result.data[0].b64_json if result.data else None
        if not data:
            raise RuntimeError("OpenAI returned no image data.")
        return base64.b64decode(data)


def make_provider(cfg: Config) -> ImageProvider:
    if cfg.images.provider == "gemini":
        return GeminiProvider(cfg.images.gemini_model, cfg.images.image_size)
    return OpenAIProvider(cfg.images.openai_model)
