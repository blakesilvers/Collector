"""Word timings (local Whisper) and the storyboard (Claude). Every time on screen is derived from
the audio's own word timestamps, so pictures and words land where the speaker actually says them."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..config import Config
from .levels import Layers

log = logging.getLogger(__name__)


# ---------- word timings ----------

class Word(BaseModel):
    w: str
    s: float
    e: float


class Sentence(BaseModel):
    id: int
    first: int  # index of first word
    last: int   # index of last word (inclusive)
    s: float
    e: float
    text: str


def cut_audio(audio: Path, out: Path, start: float, duration: float | None) -> Path:
    """Mono 16 kHz WAV of the requested stretch, for the aligner."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}"]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", str(audio), "-ac", "1", "-ar", "16000", str(out)]
    subprocess.run(cmd, check=True)
    return out


def align_words(wav: Path, model_name: str = "small.en") -> list[Word]:
    """Word-level timestamps from faster-whisper, running locally (no API cost)."""
    from faster_whisper import WhisperModel

    log.info("Listening for word timings with Whisper %s (first run downloads the model)", model_name)
    import wave

    import numpy as np

    # Decode the WAV ourselves (it is already 16 kHz mono) so no particular PyAV version is needed.
    with wave.open(str(wav), "rb") as f:
        if f.getframerate() != 16000 or f.getnchannels() != 1 or f.getsampwidth() != 2:
            raise RuntimeError("expected a 16 kHz mono 16-bit WAV")
        samples = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(samples, word_timestamps=True, vad_filter=True, language="en")
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append(Word(w=text, s=float(w.start), e=float(w.end)))
    if not words:
        raise RuntimeError("Whisper heard no words in this audio.")
    return words


def sentences_from_words(words: list[Word], max_words: int = 28) -> list[Sentence]:
    out: list[Sentence] = []
    start = 0
    for i, w in enumerate(words):
        end_mark = w.w[-1] in ".?!" or (w.w[-1] in ",;:" and i - start + 1 >= max_words * 0.7)
        long_pause = i + 1 < len(words) and words[i + 1].s - w.e > 0.9 and i - start + 1 >= 4
        if end_mark or long_pause or i - start + 1 >= max_words or i == len(words) - 1:
            out.append(Sentence(id=len(out), first=start, last=i, s=words[start].s, e=w.e,
                                text=" ".join(x.w for x in words[start:i + 1])))
            start = i + 1
    return out


def save_words(words: list[Word], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([w.model_dump() for w in words]), encoding="utf-8")


def load_words(path: Path) -> list[Word]:
    return [Word.model_validate(d) for d in json.loads(path.read_text(encoding="utf-8"))]


# ---------- storyboard ----------

class CardItem(BaseModel):
    label: str = Field(description="At most 5 words.")
    detail: str = Field(description="At most 12 words; may be empty.")


class Card(BaseModel):
    type: Literal["list", "timeline", "definition"]
    title: str = Field(description="At most 5 words.")
    items: list[CardItem] = Field(description="2 to 5 items (1 for a definition: label = the term, detail = its meaning).")


class Beat(BaseModel):
    first: int = Field(description="Id of the first sentence this picture covers.")
    last: int = Field(description="Id of the last sentence this picture covers.")
    scene: str = Field(description="What to draw: subject, action, setting, light, framing. No text or lettering in the picture.")
    motion: str = Field(description="How this picture would move if animated for a few seconds: one subject action and one camera move.")
    hero: bool = Field(description="True for the moments that most deserve animation: turning points, vivid action, emotional peaks.")
    key_phrase: str = Field(description="The speaker's own most quotable 3 to 8 consecutive words from these sentences, copied exactly; empty if none stands out.")
    card: Card | None = Field(default=None, description="Only when the speaker is listing, sequencing, dating or defining something.")


class Chapter(BaseModel):
    first: int = Field(description="Id of the sentence where this chapter begins.")
    title: str = Field(description="2 to 5 words, plain and specific.")


class Reading(BaseModel):
    first: int
    last: int
    reference: str = Field(description="Book chapter:verse(s).")
    version: str = Field(description="Translation label if the reference transcript gives one, else empty.")
    text: str = Field(description="The passage as read, verse numbers and bracketed notes omitted; prefer the reference transcript's wording.")


class Storyboard(BaseModel):
    title: str
    chapters: list[Chapter]
    beats: list[Beat]
    readings: list[Reading]


SYSTEM = """You are the director of an illustrated teaching video. The audio is a Bible teacher's
radio program; nothing in the audio may be changed. Your job is to plan what the viewer sees so
that the pictures carry the argument: every picture must show the idea being spoken at that moment.
Decorative pictures that merely fit the mood are a failure.

You receive the program as numbered sentences with times. Refer to sentences only by id.

Beats. Divide every sentence into consecutive beats (no gaps, no overlaps, in order, covering the
first sentence through the last). Aim for the target beat length you are given; cut a beat where
the thought turns, never mid-sentence. For each beat write a scene an illustrator can draw at once:
concrete subject, action, setting, light and framing. Prefer people doing things, objects, places,
maps without labels, hands, scrolls, journeys, contrasts shown side by side. Vary the framing from
beat to beat (wide, medium, close, overhead) so consecutive pictures feel like cuts in a film.
Describe any recurring person the same way every time (age, build, clothing and its colours) so
they stay recognisable. Never put text, letters or numbers in a picture. Never draw the speaker.

Key phrases. At most one per beat, only when the speaker says something worth seeing in type. It
must be consecutive words copied exactly from that beat's sentences. Leave it empty during
scripture readings and on beats that have a card.

Cards. Use a card when the speaker lists, sequences, dates or defines: a list of reasons, a chain
of events with months or years, a Greek or Hebrew word and its meaning. Cards are worth more than
key phrases; use them wherever the content is structured, and nowhere else.

Chapters. Three to six for a full half-hour program (two or three for a five-minute excerpt), each
starting on a beat boundary, the first at sentence 0.

Readings. List every stretch where the speaker reads or quotes scripture aloud, by sentence ids.
Take the reference, version label and wording from the reference transcript when it has them.
Do not list passages he only mentions.

Hero beats. Mark the beats that most deserve animation; write every beat's motion line regardless."""


def _request(cfg: Config, sentences: list[Sentence], reference: str, layers: Layers, direction: str) -> str:
    rules = [
        f"Program: {cfg.ministry.program_name}. Speaker: {cfg.ministry.speaker or 'unknown'}.",
        f"Target beat length: about {layers.beat_seconds:.0f} seconds (so roughly {max(1, round((sentences[-1].e - sentences[0].s) / layers.beat_seconds))} beats).",
        f"Key phrases: {'yes' if layers.callouts else 'leave every key_phrase empty'}. Cards: {'yes' if layers.cards else 'set every card to null'}.",
        f"depict_christ: {'true' if cfg.style.depict_christ else 'false. Never depict Jesus or God in any form, including as an infant; show the setting, the witnesses, hands, light, the manger, instead'}.",
    ]
    if cfg.style.avoid:
        rules.append("Avoid in pictures: " + "; ".join(cfg.style.avoid) + ".")
    if cfg.ministry.audience_notes:
        rules.append("Audience: " + cfg.ministry.audience_notes)
    if direction.strip():
        rules.append("Director's notes from the ministry (follow these): " + " ".join(direction.split()))
    lines = "\n".join(f"[{s.id}] ({s.s:.1f}-{s.e:.1f}s) {s.text}" for s in sentences)
    ref = f"\n\n<reference_transcript>\n{reference.strip()}\n</reference_transcript>" if reference.strip() else ""
    return "\n".join(rules) + "\n\n<sentences>\n" + lines + "\n</sentences>" + ref


def make_storyboard(cfg: Config, sentences: list[Sentence], reference: str, layers: Layers, direction: str = "") -> Storyboard:
    import anthropic

    client = anthropic.Anthropic()
    log.info("Storyboarding %d sentences with %s", len(sentences), cfg.extract.model)
    with client.messages.stream(
        model=cfg.extract.model,
        max_tokens=64000,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": _request(cfg, sentences, reference, layers, direction)}],
        output_format=Storyboard,
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise RuntimeError("Claude declined to storyboard this program.")
    if message.stop_reason == "max_tokens":
        raise RuntimeError("Storyboard was cut off; try a shorter clip or a lower level.")
    board = getattr(message, "parsed_output", None)
    if board is None:
        text = "".join(b.text for b in message.content if b.type == "text")
        board = Storyboard.model_validate_json(text)
    return board


# ---------- grounding the storyboard in the word timings ----------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9']+", " ", s.lower().replace("’", "'")).strip()


def repair(board: Storyboard, n_sentences: int) -> Storyboard:
    """Make beats contiguous over sentences 0..n-1, drop broken items, keep everything in range."""
    last_id = n_sentences - 1
    beats = sorted((b for b in board.beats if b.scene.strip()), key=lambda b: b.first)
    fixed: list[Beat] = []
    for b in beats:
        first = fixed[-1].last + 1 if fixed else 0
        last = min(max(b.last, first), last_id)
        if first > last_id:
            break
        fixed.append(b.model_copy(update={"first": first, "last": last}))
    if not fixed:
        raise ValueError("storyboard has no usable beats")
    fixed[-1] = fixed[-1].model_copy(update={"last": last_id})
    starts = {b.first for b in fixed}
    chapters = []
    for c in sorted(board.chapters, key=lambda c: c.first):
        snap = max((s for s in starts if s <= c.first), default=0)
        if c.title.strip() and all(x.first != snap for x in chapters):
            chapters.append(Chapter(first=snap, title=c.title.strip()))
    if not chapters or chapters[0].first != 0:
        chapters.insert(0, Chapter(first=0, title=board.title.strip() or "Introduction"))
    readings = []
    for r in sorted(board.readings, key=lambda r: r.first):
        first, last = max(0, r.first), min(r.last, last_id)
        if readings and first <= readings[-1].last:
            first = readings[-1].last + 1
        if first <= last and r.text.strip() and r.reference.strip():
            readings.append(r.model_copy(update={"first": first, "last": last}))
    for i, b in enumerate(fixed):
        if b.card and not (1 <= len(b.card.items) <= 5):
            fixed[i] = b.model_copy(update={"card": None})
    return Storyboard(title=board.title, chapters=chapters, beats=fixed, readings=readings)


def phrase_times(phrase: str, words: list[Word], first: int, last: int) -> list[tuple[str, float]]:
    """(word, start) for each word of a phrase found verbatim in words[first..last]; empty if absent."""
    target = _norm(phrase).split()
    if not target:
        return []
    span = [(i, _norm(words[i].w)) for i in range(first, last + 1)]
    span = [(i, w) for i, w in span if w]
    flat: list[tuple[int, str]] = []
    for i, w in span:
        for part in w.split():
            flat.append((i, part))
    for k in range(len(flat) - len(target) + 1):
        if [p for _, p in flat[k:k + len(target)]] == target:
            idxs = sorted({i for i, _ in flat[k:k + len(target)]})
            return [(words[i].w.strip('"“”'), words[i].s) for i in idxs]
    return []
