"""Studio orchestration: word timings -> storyboard -> budgeted assets -> layered render."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict
from pathlib import Path

from .. import attribution, compose, verses
from ..config import Config
from ..imagegen import ImageProvider, content_guard, make_provider
from ..transcribe import edited_transcript, find_audio, get_transcript
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
                   shots: list[assets.Shot], layers, overlay_dir: Path, W: int, H: int, max_words: int, min_page_seconds: float,
                   *, spans: list[attribution.Span] | None) -> render.Timeline:
    """`spans` is the reference transcript split by attribution.split_transcript, or None when there is none
    (then key phrases are checked against the audio only)."""
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
    shown, _ = plan.vet_key_phrases(board, words, sentences, spans, layers.cards) if layers.callouts else ({}, [])

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
        elif shot.index in shown:  # his own words, really spoken in this beat, outside any scripture reading
            timed = shown[shot.index]
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


def report_key_phrases(board: plan.Storyboard, words: list[plan.Word], sentences: list[plan.Sentence], layers,
                       spans: list[attribution.Span] | None) -> None:
    """Say, in plain words, which key phrases will be shown and why any were dropped."""
    if not layers.callouts:
        return
    if spans is None:
        print("  Attribution could not be checked: there is no edited transcript for this program, so key phrases "
              "were checked against the audio only, not for whether they are his own words.")
    shown, dropped = plan.vet_key_phrases(board, words, sentences, spans, layers.cards)
    if not shown and not dropped:
        return
    print(f"  Key phrases: {len(shown)} will be shown, {len(dropped)} dropped.")
    for i, phrase, why in dropped:
        print(f"    Beat {i + 1}, \"{phrase}\": {plan.DROP_REASONS[why]}.")


def report_writing_risk(shots: list[assets.Shot]) -> None:
    """Warn (never block) about scenes still to be drawn that mention things which often come out with
    garbled writing on them."""
    risky = [(s.index + 1, words) for s in shots if s.image and not s.image.exists()
             for words in [plan.lint_scene(s.beat.scene)] if words]
    if not risky:
        return
    print("  Warning: these scenes mention things that often come out with garbled writing on them. To change one, "
          "edit its scene in the storyboard file and run again (only that picture is redrawn):")
    for beat, words in risky:
        print(f"    Beat {beat}: {', '.join(words)}")


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
    plan_estimate: float | None = None,
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
    budget = max(3.0, st.budget_usd * length / max(full, 1.0))
    if board is None and board_path.exists() and not replan:
        board = plan.Storyboard.model_validate_json(board_path.read_text(encoding="utf-8"))
    elif board is None:
        # The storyboard is a paid Claude call: price it, check the budget and ask before anything is sent.
        plan_usd = plan.storyboard_estimate(st, length) if plan_estimate is None else plan_estimate
        left = budget - ledger.data["usd"]

        def over_budget(most: float) -> RuntimeError:
            return RuntimeError(
                f"Making the plan should cost about ${plan_usd:.2f} but could cost up to ${most:.2f} if Claude's reply runs "
                f"to full length, and only ${max(left, 0.0):.2f} is left of the ${budget:.2f} budget for this stretch "
                f"(${st.budget_usd:.0f} per full program, pro-rated). Nothing was spent on the plan. If you mean to spend more, "
                "raise budget_usd under [studio] in config.toml and run the same command again.")

        most = max(plan_usd, plan.storyboard_ceiling(cfg, sentences, layers, st.direction))
        if most > left:
            raise over_budget(most)
        print(f"  The plan (storyboard) is made by Claude. Estimated cost: about ${plan_usd:.2f} (at most ${most:.2f}). "
              "This is an estimate; the exact figure is on your Anthropic bill.")
        if not assume_yes and confirm(f"  Spend about ${plan_usd:.2f} on the plan? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  Stopped before spending. Nothing was charged and no plan was saved.")
            return {"work": str(work), "stopped": "plan not approved"}
        try:
            reference = get_transcript(cfg, stem, audio, cfg.paths.output_dir / stem).read_text(encoding="utf-8")
        except Exception:
            reference = ""
        most = max(plan_usd, plan.storyboard_ceiling(cfg, sentences, layers, st.direction, reference))
        if most > left:  # the transcript turned out longer than allowed for; check again before sending it
            raise over_budget(most)
        try:
            board, actual = plan.make_storyboard(cfg, sentences, reference, layers, st.direction)
        except plan.StoryboardFailed as exc:
            if exc.charged:  # count it, so the budget stays honest even when the plan was unusable
                if exc.usd is not None:
                    ledger.add(round(exc.usd, 4), storyboards=1)
                else:  # cost unknown; a cut-off reply may have run to full length, so count the most it could be
                    ledger.add(round(most, 4), storyboards=1)
                    print(f"  The failed attempt was counted as ${most:.2f}, the most it could have cost, "
                          "so the budget stays on the safe side. Your Anthropic bill shows the exact figure.")
            raise
        if actual is None:  # no token counts: count the most it could have cost, so the budget stays safe
            ledger.add(round(most, 4), storyboards=1)
            print(f"  Plan made. Counted as ${most:.2f}, the most it could have cost, because Claude did not report "
                  "token counts. Your Anthropic bill shows the exact figure.")
        else:
            ledger.add(round(actual, 4), storyboards=1)
            print(f"  Plan made. Cost: ${actual:.2f}, worked out from the token counts Claude reported.")
    board = plan.repair(board, len(sentences))
    board_path.write_text(board.model_dump_json(indent=2), encoding="utf-8")
    # Words shown as his must be his: key phrases are checked against his own words in the edited transcript.
    reference = edited_transcript(cfg, stem)
    spans = attribution.split_transcript(reference) if reference is not None else None

    # 3. shots, animation choice, budget
    shots = shots_from(board, sentences, length)
    style = STYLES.get(st.style, st.style)
    guard = content_guard(cfg)
    assets.choose_animated(shots, 0.0 if draft else layers.motion_share)
    assets_dir = work / ("draft_assets" if draft else "assets")
    assets.assign_paths(shots, assets_dir, style, guard, st.motion_model)
    image_usd = 0.0 if draft else st.image_usd
    try:
        dropped = assets.fit_to_budget(shots, budget - ledger.data["usd"], image_usd, st.video_usd_per_second, 0.0)
    except assets.OverBudget as exc:
        # A hard stop: no --yes or answer to a question can get past it.
        message = (f"{exc} The budget for this stretch is ${budget:.2f} (${st.budget_usd:.0f} per full program, pro-rated) "
                   f"and ${ledger.data['usd']:.2f} has been spent on it so far. To go ahead, raise budget_usd under [studio] "
                   "in config.toml, or choose a shorter stretch with --minutes, then run the same command again.")
        if plan_only:
            print(f"  Note: {message}")
            return {"work": str(work), "stopped": "over budget", "storyboard": str(board_path)}
        raise RuntimeError(message) from None
    assets.assign_paths(shots, assets_dir, style, guard, st.motion_model)
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
    report_key_phrases(board, words, sentences, layers, spans)
    report_writing_risk(shots)
    summary = {"work": str(work), "shots": len(shots), "animated": n_anim, "estimate": round(est.total, 2), "storyboard": str(board_path)}
    if plan_only:
        print(f"  Plan only. Storyboard: {board_path}")
        return summary
    if est.total > 0 and not assume_yes:
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
                        cfg.verses.max_words, cfg.verses.min_page_seconds, spans=spans)
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
    if report.motion_stopped:
        print(f"  Note: {report.motion_stopped}")
    clip_failures = 0 if report.motion_stopped else len(report.clip_failures)
    if report.image_failures or clip_failures:
        print(f"  Note: {len(report.image_failures)} picture(s) and {clip_failures} animation(s) failed and were covered by neighbours or stills. Run again to retry them.")
    return summary
