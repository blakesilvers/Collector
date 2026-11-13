"""Stage 2: transcript -> Brief, via Claude structured output. Quotes are verified against the transcript."""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path

from . import attribution
from .config import Config
from .models import Brief, QuoteCheck

log = logging.getLogger(__name__)


def _system_prompt() -> str:
    return resources.files("sermon_visuals.prompts").joinpath("extract_system.md").read_text(encoding="utf-8")


_normalize = attribution.normalize  # lower-case, strip accents and punctuation; used only for quote matching


def verify_quotes(brief: Brief, transcript: str) -> list[QuoteCheck]:
    """A quote passes only if its normalized text appears verbatim inside one stretch of the speaker's own
    words: never in a scripture block, another author's quotation or a [bracketed] editor's note."""
    spans = attribution.split_transcript(transcript)
    return [QuoteCheck(text=q.text, verified=attribution.is_his(q.text, spans)) for q in brief.pull_quotes]


def _brief_request(cfg: Config, transcript: str) -> str:
    rules = [
        f"Program: {cfg.ministry.program_name}. Speaker: {cfg.ministry.speaker or 'unknown'}.",
        f"Return exactly {cfg.extract.pull_quotes} pull quotes and {cfg.extract.visual_concepts} visual concepts.",
        f"depict_christ: {'true' if cfg.style.depict_christ else 'false'}.",
    ]
    if cfg.ministry.audience_notes:
        rules.append("Audience: " + cfg.ministry.audience_notes)
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
