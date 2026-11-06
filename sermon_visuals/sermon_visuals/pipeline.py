"""Orchestration: run each stage per program, record progress in manifest.json, never redo finished work."""

from __future__ import annotations

import logging
from pathlib import Path

from . import compose, extract, imagegen, transcribe, video
from .config import Config
from .models import ArtCandidate, Manifest, load_brief

log = logging.getLogger(__name__)

STAGES = ["transcript", "brief", "images", "compose", "video"]


def discover_stems(cfg: Config) -> list[str]:
    stems = set()
    for p in cfg.paths.input_dir.iterdir() if cfg.paths.input_dir.exists() else []:
        if p.suffix.lower() in transcribe.AUDIO_EXTS or p.suffix.lower() in (".txt", ".pdf"):
            stems.add(p.stem)
    return sorted(stems)


def program_dir(cfg: Config, stem: str) -> Path:
    return cfg.paths.output_dir / stem


def load_manifest(cfg: Config, stem: str) -> Manifest:
    return Manifest.load(program_dir(cfg, stem) / "manifest.json", stem)


def _save(cfg: Config, m: Manifest) -> None:
    m.save(program_dir(cfg, m.stem) / "manifest.json")


def stage_transcript(cfg: Config, m: Manifest) -> None:
    out_dir = program_dir(cfg, m.stem)
    audio = transcribe.find_audio(cfg.paths.input_dir, m.stem)
    m.audio = str(audio) if audio else None
    captions = transcribe.find_sidecar(cfg.paths.input_dir, m.stem, ".srt")
    m.captions = str(captions) if captions else None
    m.transcript = str(transcribe.get_transcript(cfg, m.stem, audio, out_dir))


def stage_brief(cfg: Config, m: Manifest) -> None:
    text = Path(m.transcript).read_text(encoding="utf-8")
    brief = extract.extract_brief(cfg, text)
    m.quote_checks = extract.verify_quotes(brief, text)
    path = program_dir(cfg, m.stem) / "brief.json"
    extract.write_brief(brief, path)
    m.brief = str(path)
    bad = [q.text for q in m.quote_checks if not q.verified]
    if bad:
        log.warning("%s: %d quote(s) not found verbatim in transcript", m.stem, len(bad))


def stage_images(cfg: Config, m: Manifest, provider: imagegen.ImageProvider | None = None) -> None:
    brief = load_brief(Path(m.brief))
    provider = provider or imagegen.make_provider(cfg)
    art_dir = program_dir(cfg, m.stem) / "art"
    art_dir.mkdir(parents=True, exist_ok=True)
    # Generate in 16:9; cards are centre-cropped from it. Switch to per-format generation if crops disappoint.
    aspect = compose.FORMATS["youtube"].aspect
    for ci, concept in enumerate(brief.visual_concepts, start=1):
        prompt = imagegen.build_prompt(cfg, concept.image_prompt)
        for k in range(1, cfg.images.candidates + 1):
            out = art_dir / f"concept{ci}_{k}.png"
            if out.exists():
                continue
            log.info("%s: generating concept %d candidate %d (%s)", m.stem, ci, k, provider.model)
            data = provider.generate(prompt, aspect, cfg.style.reference_images)
            out.write_bytes(data)
            m.art.append(ArtCandidate(concept=ci, index=k, path=str(out), provider=provider.name, model=provider.model, prompt=prompt))
    if m.art and not m.chosen_art:
        m.chosen_art = m.art[0].path


def stage_compose(cfg: Config, m: Manifest) -> None:
    if not m.chosen_art:
        raise RuntimeError("no artwork chosen; run the images stage or `choose` first")
    brief = load_brief(Path(m.brief))
    art = Path(m.chosen_art)
    fonts = compose.Fonts.resolve(cfg.compose.font_serif, cfg.compose.font_sans)
    cards_dir = program_dir(cfg, m.stem) / "cards"
    attribution = f"— {cfg.ministry.speaker}" if cfg.ministry.speaker else ""
    subtitle = " · ".join(x for x in [cfg.ministry.speaker, brief.primary_scripture] if x)
    m.cards = {}
    for name in cfg.compose.formats:
        fmt = compose.FORMATS[name]
        if name == "youtube":
            out = compose.render_title_frame(
                art, cards_dir / "youtube_frame.png", fmt, fonts,
                title=brief.title, subtitle=subtitle, program=cfg.ministry.program_name,
                speaker_photo=cfg.ministry.speaker_photo, logo=cfg.ministry.logo,
            )
            m.cards["youtube_frame"] = str(out)
            thumb = compose.render_title_frame(
                art, cards_dir / "thumbnail.png", fmt, fonts,
                title=brief.thumbnail_text or brief.title, subtitle=cfg.ministry.speaker, program=cfg.ministry.program_name,
                speaker_photo=cfg.ministry.speaker_photo, logo=cfg.ministry.logo,
            )
            m.cards["thumbnail"] = str(thumb)
        else:
            for qi, q in enumerate(brief.pull_quotes, start=1):
                check = next((c for c in m.quote_checks if c.text == q.text), None)
                if check and not check.verified:
                    continue  # never put an unverified quote on a card
                out = compose.render_quote_card(
                    art, cards_dir / f"quote{qi}_{name}.png", fmt, fonts,
                    quote=q.text, attribution=attribution, program=cfg.ministry.program_name, logo=cfg.ministry.logo,
                )
                m.cards[f"quote{qi}_{name}"] = str(out)


def stage_video(cfg: Config, m: Manifest) -> None:
    if not cfg.video.enabled:
        return
    if not m.audio:
        raise RuntimeError("no audio file for this program")
    frame = m.cards.get("youtube_frame")
    if not frame:
        raise RuntimeError("youtube_frame missing; include 'youtube' in compose.formats")
    captions = Path(m.captions) if (cfg.video.burn_captions and m.captions) else None
    out = program_dir(cfg, m.stem) / f"{m.stem}.mp4"
    m.video = str(video.render_video(Path(frame), Path(m.audio), out, captions))


RUNNERS = {
    "transcript": stage_transcript,
    "brief": stage_brief,
    "images": stage_images,
    "compose": stage_compose,
    "video": stage_video,
}


def run_program(cfg: Config, stem: str, stages: list[str], force: bool = False) -> Manifest:
    m = load_manifest(cfg, stem)
    for stage in STAGES:
        if stage not in stages:
            continue
        if stage in m.done and not force:
            log.info("%s: %s already done, skipping", stem, stage)
            continue
        if force and stage == "images":
            m.art, m.chosen_art = [], None
        try:
            RUNNERS[stage](cfg, m)
            m.mark(stage)
        except Exception as exc:  # record and stop this program; others continue
            log.error("%s: %s failed: %s", stem, stage, exc)
            m.errors[stage] = str(exc)
            _save(cfg, m)
            break
        _save(cfg, m)
    return m
