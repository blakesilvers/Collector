"""Stage 2: transcript -> Brief, via Claude structured output. Quotes are verified against the transcript."""

from __future__ import annotations

import logging
import re
import unicodedata
from importlib import resources
from pathlib import Path

from .config import Config
from .models import Brief, QuoteCheck

log = logging.getLogger(__name__)


def _system_prompt() -> str:
    return resources.files("sermon_visuals.prompts").joinpath("extract_system.md").read_text(encoding="utf-8")


def _normalize(text: str) -> str:
    """Lower-case, strip accents and punctuation, collapse whitespace. Used only for quote matching."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def verify_quotes(brief: Brief, transcript: str) -> list[QuoteCheck]:
    """A quote passes only if its normalized text appears verbatim in the normalized transcript."""
    haystack = _normalize(transcript)
    checks = []
    for q in brief.pull_quotes:
        needle = _normalize(q.text)
        checks.append(QuoteCheck(text=q.text, verified=bool(needle) and needle in haystack))
    return checks


def _brief_request(cfg: Config, transcript: str) -> str:
    rules = [
        f"Program: {cfg.ministry.program_name}. Speaker: {cfg.ministry.speaker or 'unknown'}.",
        f"Return exactly {cfg.extract.pull_quotes} pull quotes and {cfg.extract.visual_concepts} visual concepts.",
        f"depict_christ: {'true' if cfg.style.depict_christ else 'false'}.",
    ]
    if cfg.style.avoid:
        rules.append("avoid: " + "; ".join(cfg.style.avoid) + ".")
    if cfg.style.prompt:
        rules.append(
            "House style (for context only; do not repeat it inside image prompts): " + cfg.style.prompt
        )
    return "\n".join(rules) + "\n\n<transcript>\n" + transcript.strip() + "\n</transcript>"


def extract_brief(cfg: Config, transcript: str) -> Brief:
    import anthropic

    client = anthropic.Anthropic()
    log.info("Extracting brief with %s (effort=%s)", cfg.extract.model, cfg.extract.effort)
    response = client.messages.parse(
        model=cfg.extract.model,
        max_tokens=16000,
        system=[{"type": "text", "text": _system_prompt(), "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": cfg.extract.effort},
        messages=[{"role": "user", "content": _brief_request(cfg, transcript)}],
        output_format=Brief,
    )
    if response.stop_reason == "refusal":
        detail = response.stop_details.explanation if response.stop_details else ""
        raise RuntimeError(f"Claude declined to produce a brief: {detail}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Brief extraction hit max_tokens; the transcript may be too long for one call.")
    brief = response.parsed_output
    if brief is None:
        raise RuntimeError("Claude returned no parseable brief.")
    return brief


def write_brief(brief: Brief, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brief.model_dump_json(indent=2), encoding="utf-8")
