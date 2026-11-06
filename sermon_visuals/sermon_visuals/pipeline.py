"""Orchestration: run each stage per program, record progress in manifest.json, never redo finished work."""

from __future__ import annotations

import logging
from pathlib import Path

from . import compose, extract, illustrate, imagegen, transcribe, verses, video
from .config import Config
from .models import ArtCandidate, Manifest, load_brief

log = logging.getLogger(__name__)

STAGES = ["transcript", "brief", "images", "compose", "video", "illustrate"]
DEFAULT_STAGES = ["transcript", "brief", "images", "compose", "video"]  # illustrate is opt-in


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


def art_for_format(cfg: Config, m: Manifest, fmt: compose.Format, provider: imagegen.ImageProvider | None = None) -> Path:
    """The chosen painting, re-framed for this format by the image model (cached), or the original if recompose is off."""
    chosen = Path(m.chosen_art)
    if fmt.aspect == compose.FORMATS["youtube"].aspect or not cfg.images.recompose:
        return chosen
    out = chosen.with_name(f"{chosen.stem}_{fmt.name}.png")
    if not out.exists():
        provider = provider or imagegen.make_provider(cfg)
        log.info("%s: re-framing %s for %s", m.stem, chosen.name, fmt.name)
        out.write_bytes(provider.generate(imagegen.recompose_prompt(cfg, fmt.aspect), fmt.aspect, [chosen]))
    return out


def stage_compose(cfg: Config, m: Manifest, provider: imagegen.ImageProvider | None = None) -> None:
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
            fmt_art = art_for_format(cfg, m, fmt, provider)
            for qi, q in enumerate(brief.pull_quotes, start=1):
                check = next((c for c in m.quote_checks if c.text == q.text), None)
                if check and not check.verified:
                    continue  # never put an unverified quote on a card
                out = compose.render_quote_card(
                    fmt_art, cards_dir / f"quote{qi}_{name}.png", fmt, fonts,
                    quote=q.text, attribution=attribution, program=cfg.ministry.program_name, logo=cfg.ministry.logo,
                )
                m.cards[f"quote{qi}_{name}"] = str(out)


def ensure_plan(cfg: Config, m: Manifest, segments=None) -> tuple[list, list]:
    """Segments and scripture readings for this program, planned once by Gemini from the audio and cached as editable JSON."""
    pdir = program_dir(cfg, m.stem) / "illustrated"
    plan_path, verses_path = pdir / "segments.json", pdir / "verses.json"
    if segments is not None:
        scriptures = illustrate.read_scriptures(verses_path) if verses_path.exists() else []
        return segments, scriptures
    if plan_path.exists() and verses_path.exists():
        return illustrate.read_plan(plan_path), illustrate.read_scriptures(verses_path)
    if not m.audio or not m.transcript:
        raise RuntimeError("planning needs the audio file and a transcript")
    segs, scriptures = illustrate.plan_segments(cfg, Path(m.audio), Path(m.transcript).read_text(encoding="utf-8"))
    illustrate.write_plan(segs, plan_path)
    illustrate.write_srt(segs, pdir / "segments.srt")
    illustrate.write_scriptures(scriptures, verses_path)
    m.verses = str(verses_path)
    return segs, scriptures


def verse_overlays(cfg: Config, m: Manifest, scriptures: list, position: str = "bottom", width: int = 1920, height: int = 1080) -> list[verses.Overlay]:
    if not cfg.verses.enabled or not scriptures:
        return []
    fonts = compose.Fonts.resolve(cfg.compose.font_serif, cfg.compose.font_sans)
    return verses.build_overlays(scriptures, program_dir(cfg, m.stem) / "illustrated" / f"verse_overlays_{position}", width, height, fonts,
                                 cfg.verses.max_words, cfg.verses.min_page_seconds, position)


def stage_video(cfg: Config, m: Manifest) -> None:
    if not cfg.video.enabled:
        return
    if not m.audio:
        raise RuntimeError("no audio file for this program")
    frame = m.cards.get("youtube_frame")
    if not frame:
        raise RuntimeError("youtube_frame missing; include 'youtube' in compose.formats")
    captions = Path(m.captions) if (cfg.video.burn_captions and m.captions) else None
    overlays = []
    if cfg.video.show_verses and cfg.verses.enabled:
        _, scriptures = ensure_plan(cfg, m)
        overlays = verse_overlays(cfg, m, scriptures, position="top")  # the title frame's text lives at the bottom
    out = program_dir(cfg, m.stem) / f"{m.stem}.mp4"
    m.video = str(video.render_video(Path(frame), Path(m.audio), out, captions, overlays=overlays))


def stage_illustrate(cfg: Config, m: Manifest, provider: imagegen.ImageProvider | None = None, segments=None) -> None:
    """Illustrated-narration video. Needs audio and a transcript; the brief is used for the title frame if present."""
    if not m.audio:
        raise RuntimeError("no audio file for this program")
    if not m.transcript:
        raise RuntimeError("no transcript; run the transcript stage first")
    pdir = program_dir(cfg, m.stem) / "illustrated"
    segments, scriptures = ensure_plan(cfg, m, segments)
    sketches = illustrate.draw_segments(cfg, segments, pdir / "sketches", provider)
    frames = [(p, seg.end - seg.start) for p, seg in zip(sketches, segments)]
    title = m.cards.get("youtube_frame")
    if title and cfg.illustrate.title_seconds > 0:
        frames.insert(0, (Path(title), cfg.illustrate.title_seconds))
        frames[1] = (frames[1][0], max(frames[1][1] - cfg.illustrate.title_seconds, 1.0))  # keep sync with audio
    out = pdir / f"{m.stem}_illustrated.mp4"
    overlays = verse_overlays(cfg, m, scriptures)
    m.illustrated = str(illustrate.assemble(frames, Path(m.audio), out, cfg.illustrate.fps, cfg.illustrate.crossfade, cfg.illustrate.push_in, overlays))
    m.sketches = [str(p) for p in sketches]


RUNNERS = {
    "transcript": stage_transcript,
    "brief": stage_brief,
    "images": stage_images,
    "compose": stage_compose,
    "video": stage_video,
    "illustrate": stage_illustrate,
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
