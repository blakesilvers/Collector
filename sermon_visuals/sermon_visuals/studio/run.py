"""Studio orchestration: word timings -> storyboard -> budgeted assets -> layered render."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict
from pathlib import Path

from .. import compose, verses
from ..config import Config
from ..imagegen import ImageProvider, make_provider
from ..transcribe import find_audio, get_transcript
from . import assets, plan, render
from .levels import LEVEL_BLURBS, STYLES, layers_for

log = logging.getLogger(__name__)


def _duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         check=True, capture_output=True, text=True).stdout.strip()
    return float(out)


def studio_dir(cfg: Config, stem: str, start: float, minutes: float | None) -> Path:
    tag = "full" if not minutes and not start else f"clip_{int(start):04d}_{int((minutes or 0) * 60):04d}"
    return cfg.paths.output_dir / stem / "studio" / tag


def build_timeline(board: plan.Storyboard, words: list[plan.Word], sentences: list[plan.Sentence], duration: float,
                   shots: list[assets.Shot], layers, overlay_dir: Path, W: int, H: int, max_words: int, min_page_seconds: float) -> render.Timeline:
    starts = {s.beat.first: s.start for s in shots}
    # scripture readings -> overlay pages
    scriptures = []
    for r in board.readings:
        s = max(0.0, sentences[r.first].s - 0.2)
        e = min(duration, sentences[r.last].e + 0.6)
        scriptures.append(verses.Scripture(start=s, end=e, reference=r.reference, version=r.version, text=r.text))
    scriptures = verses.validate_scriptures(scriptures, duration)
    fonts = compose.Fonts.resolve()
    overlays = verses.build_overlays(scriptures, overlay_dir, W, H, fonts, max_words, min_page_seconds, "bottom") if scriptures else []
    reading_ids = {i for r in board.readings for i in range(r.first, r.last + 1)}

    callouts, cards = [], []
    for shot in shots:
        b = shot.beat
        w0, w1 = sentences[b.first].first, sentences[b.last].last
        if layers.cards and b.card:
            n = len(b.card.items)
            times, cursor = [], w0
            for k, item in enumerate(b.card.items):
                keys = [x for x in plan._norm(item.label).split() if len(x) > 3]
                hit = next((i for i in range(cursor, w1 + 1) if plan._norm(words[i].w) in keys), None)
                even = shot.start + 0.6 + k * max(shot.duration - 1.5, 0.5) / max(n, 1)
                t = words[hit].s if hit is not None else even
                if times and t <= times[-1]:
                    t = times[-1] + 0.5
                times.append(min(t, shot.end - 0.5))
                cursor = (hit + 1) if hit is not None else cursor
            cards.append(render.CardSpec(card=b.card, start=shot.start + 0.3, end=shot.end - 0.05, item_times=times))
        elif layers.callouts and b.key_phrase.strip() and not (set(range(b.first, b.last + 1)) & reading_ids):
            timed = plan.phrase_times(b.key_phrase, words, w0, w1)
            if timed:  # shown only if the words were really spoken in this beat
                end = min(shot.end - 0.05, max(timed[-1][1] + 2.4, timed[0][1] + 3.0))
                callouts.append(render.Callout(words=timed, end=end))
    chapters = [render.ChapterMark(number=k + 1, title=c.title, start=starts.get(c.first, 0.0)) for k, c in enumerate(board.chapters)]
    return render.Timeline(
        duration=duration,
        shots=[render.ShotSpec(start=s.start, end=s.end, image=s.image, clip=s.clip if s.animate else None, clip_seconds=s.clip_seconds) for s in shots],
        callouts=callouts, cards=cards, chapters=chapters, verses=overlays,
        caption_words=words if layers.captions else [],
        progress=layers.progress, ink_in=layers.ink_in, camera=layers.camera, show_chapters=layers.chapters,
    )


def shots_from(board: plan.Storyboard, sentences: list[plan.Sentence], duration: float) -> list[assets.Shot]:
    cuts = [0.0] + [max(0.0, sentences[b.first].s - 0.15) for b in board.beats[1:]] + [duration]
    return [assets.Shot(index=i, start=cuts[i], end=cuts[i + 1], beat=b) for i, b in enumerate(board.beats) if cuts[i + 1] > cuts[i]]


def run_studio(
    cfg: Config,
    stem: str,
    level: int | None = None,
    start: float = 0.0,
    minutes: float | None = None,
    assume_yes: bool = False,
    plan_only: bool = False,
    replan: bool = False,
    draft: bool = False,
    preview: bool = False,
    provider: ImageProvider | None = None,
    motion: assets.MotionProvider | None = None,
    words: list[plan.Word] | None = None,
    board: plan.Storyboard | None = None,
    confirm=input,
) -> dict:
    st = cfg.studio
    level = level or st.level
    layers = layers_for(level, st.layers)
    audio = find_audio(cfg.paths.input_dir, stem)
    if audio is None:
        raise FileNotFoundError(f"No audio named {stem}.* in {cfg.paths.input_dir}")
    work = studio_dir(cfg, stem, start, minutes)
    work.mkdir(parents=True, exist_ok=True)
    full = _duration(audio)
    start = min(max(start, 0.0), max(full - 5, 0))
    length = min(minutes * 60, full - start) if minutes else full - start
    print(f"\nStudio: {stem}  ·  {length / 60:.1f} min from {start / 60:.1f} min  ·  level {level} ({LEVEL_BLURBS[level]})")

    # 1. word timings
    wav = work / "audio.wav"
    words_path = work / "words.json"
    if words is None:
        if words_path.exists():
            words = plan.load_words(words_path)
        else:
            plan.cut_audio(audio, wav, start, length)
            words = plan.align_words(wav, st.whisper_model)
            plan.save_words(words, words_path)
    else:
        plan.save_words(words, words_path)
    words = [w for w in words if w.s < length]
    sentences = plan.sentences_from_words(words)

    # 2. storyboard
    board_path = work / f"storyboard_L{level}.json"
    ledger = assets.Ledger(work / "spend.json")
    if board is None and board_path.exists() and not replan:
        board = plan.Storyboard.model_validate_json(board_path.read_text(encoding="utf-8"))
    elif board is None:
        try:
            reference = get_transcript(cfg, stem, audio, cfg.paths.output_dir / stem).read_text(encoding="utf-8")
        except Exception:
            reference = ""
        board = plan.make_storyboard(cfg, sentences, reference, layers, st.direction)
        ledger.add(round(st.storyboard_usd * max(length / 1800.0, 0.1), 2), storyboards=1)  # an estimate; the exact figure is on your Anthropic bill
    board = plan.repair(board, len(sentences))
    board_path.write_text(board.model_dump_json(indent=2), encoding="utf-8")

    # 3. shots, animation choice, budget
    shots = shots_from(board, sentences, length)
    style = STYLES.get(st.style, st.style)
    assets.choose_animated(shots, 0.0 if draft else layers.motion_share)
    assets_dir = work / ("draft_assets" if draft else "assets")
    assets.assign_paths(shots, assets_dir, style, cfg.style.negative, cfg.style.depict_christ, st.motion_model)
    image_usd = 0.0 if draft else st.image_usd
    budget = max(3.0, st.budget_usd * length / max(full, 1.0))
    dropped = assets.fit_to_budget(shots, budget - ledger.data["usd"], image_usd, st.video_usd_per_second, 0.0)
    assets.assign_paths(shots, assets_dir, style, cfg.style.negative, cfg.style.depict_christ, st.motion_model)
    est = assets.estimate(shots, image_usd, st.video_usd_per_second, 0.0)
    n_anim = sum(1 for s in shots if s.animate)
    print(f"  {len(shots)} shots ({n_anim} animated), {len(board.chapters)} chapters, {len(board.readings)} scripture readings, "
          f"{sum(1 for b in board.beats if b.card)} info cards, {sum(1 for b in board.beats if b.key_phrase.strip())} key phrases")
    print(f"  To generate now: {est.new_images} pictures (${est.image_usd:.2f}) + {est.new_clip_seconds}s of animation (${est.video_usd:.2f})"
          f"  =  ${est.total:.2f}.  Already made and reused: {est.cached_images} pictures, {est.cached_clips} clips.")
    print(f"  Spent so far on this clip: ${ledger.data['usd']:.2f}.  Budget for this stretch: ${budget:.2f} "
          f"(${st.budget_usd:.0f} per full program, pro-rated).")
    if dropped:
        print(f"  {dropped} planned animation(s) were turned back into stills to stay inside the budget.")
    summary = {"work": str(work), "shots": len(shots), "animated": n_anim, "estimate": round(est.total, 2), "storyboard": str(board_path)}
    if plan_only:
        print(f"  Plan only. Storyboard: {board_path}")
        return summary
    if est.total > 0.5 and not assume_yes:
        if confirm(f"  Spend about ${est.total:.2f}? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  Stopped before spending. The storyboard is saved; run again to continue.")
            return summary

    # 4. assets
    if draft:
        provider = provider or assets.DraftArt()
    provider = provider or make_provider(cfg)
    if n_anim and motion is None:
        motion = assets.OmniMotion(st.motion_model, st.motion_resolution)
    report = assets.generate_assets(cfg, shots, style, provider, motion, ledger, image_usd, st.video_usd_per_second, st.workers)

    # 5. render
    scale = 0.5 if preview else 1.0
    W, H = int(st.width * scale) // 2 * 2, int(st.height * scale) // 2 * 2
    tl = build_timeline(board, words, sentences, length, shots, layers, work / f"verse_overlays_{W}", W, H,
                        cfg.verses.max_words, cfg.verses.min_page_seconds)
    name = f"{stem}_L{level}{'_draft' if draft else ''}{'_preview' if preview else ''}"
    out = work / f"{name}.mp4"
    sheet = work / f"{name}_contact_sheet.jpg"
    render.render(tl, audio, out, audio_start=start, W=W, H=H, fps=st.fps, sheet=sheet)
    summary.update({"video": str(out), "contact_sheet": str(sheet), "spent": ledger.data["usd"],
                    "image_failures": report.image_failures, "clip_failures": report.clip_failures})
    (work / "last_run.json").write_text(json.dumps({**summary, "level": level, "layers": asdict(layers)}, indent=2), encoding="utf-8")
    print(f"\n  Video:          {out}")
    print(f"  Contact sheet:  {sheet}   (send this one image to Claude for feedback)")
    print(f"  Storyboard:     {board_path}   (edit a scene, run again, and only that picture is redrawn)")
    print(f"  Total spent on this clip so far: ${ledger.data['usd']:.2f}")
    if report.image_failures or report.clip_failures:
        print(f"  Note: {len(report.image_failures)} picture(s) and {len(report.clip_failures)} animation(s) failed and were covered by neighbours or stills. Run again to retry them.")
    return summary
