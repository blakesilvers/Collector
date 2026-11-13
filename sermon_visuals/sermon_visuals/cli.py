"""Command line: init, run, review, choose, list."""

from __future__ import annotations

import argparse
import logging
import sys
from importlib import resources
from pathlib import Path

from . import env, pipeline, review, update
from .config import load_config


def cmd_init(args) -> int:
    target = Path(args.config)
    if target.exists():
        print(f"{target} already exists; not overwriting.")
    else:
        example = resources.files("sermon_visuals").joinpath("config.example.toml")
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Wrote {target}")
    env_path = target.resolve().parent / ".env"
    if not env_path.exists():
        env_path.write_text(env.ENV_TEMPLATE, encoding="utf-8")
        print(f"Wrote {env_path}")
    for d in ("programs", "assets", "output"):
        (target.resolve().parent / d).mkdir(exist_ok=True)
    print()
    print("Next steps:")
    print(f"  1. Open {env_path} in a text editor and paste your API keys after the = signs.")
    print(f"  2. Open {target} and set the program name, speaker and house style.")
    print("  3. Put audio files (and transcripts, if you have them) in programs/.")
    print("  4. python -m sermon_visuals doctor     # checks everything is in place")
    print("  5. python -m sermon_visuals run        # then open output/review.html")
    return 0


def cmd_doctor(args) -> int:
    cfg = load_config(args.config)
    env_path = Path(args.config).resolve().parent / ".env"
    bad = 0
    for ok, msg in env.doctor(cfg, env_path):
        print(("  OK   " if ok else "  FIX  ") + msg)
        bad += 0 if ok or "(optional)" in msg else 1
    print()
    print("Everything needed is in place. Run: python -m sermon_visuals run" if bad == 0 else f"{bad} item(s) need attention before `run` will get far.")
    return 0 if bad == 0 else 1


def cmd_list(args) -> int:
    cfg = load_config(args.config)
    for stem in pipeline.discover_stems(cfg):
        m = pipeline.load_manifest(cfg, stem)
        status = ", ".join(m.done) or "-"
        errs = "; ".join(f"{k}: {v}" for k, v in m.errors.items())
        print(f"{stem:40s} done: {status}" + (f"  ERRORS: {errs}" if errs else ""))
    return 0


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    stages = [s.strip() for s in args.stages.split(",")] if args.stages else pipeline.DEFAULT_STAGES
    unknown = [s for s in stages if s not in pipeline.STAGES]
    if unknown:
        print(f"Unknown stage(s): {unknown}. Choose from {pipeline.STAGES}", file=sys.stderr)
        return 2
    stems = args.only or pipeline.discover_stems(cfg)
    missing = env.missing_keys(cfg, stages, env.needs_transcription(cfg, stems))
    if missing:
        print(env.explain_missing(missing, Path(args.config).resolve().parent / ".env"), file=sys.stderr)
        return 2
    if not stems:
        print(f"No programs found in {cfg.paths.input_dir}", file=sys.stderr)
        return 1
    manifests = [pipeline.run_program(cfg, stem, stages, force=args.force) for stem in stems]
    failed = [m.stem for m in manifests if m.errors]
    page = review.build_review(cfg.paths.output_dir, [pipeline.load_manifest(cfg, s) for s in pipeline.discover_stems(cfg)])
    print(f"Review page: {page}")
    if failed:
        print(f"{len(failed)} program(s) had errors: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def cmd_illustrate(args) -> int:
    """Shortcut for `run --stages transcript,illustrate` (plus brief/compose first if the title frame is wanted)."""
    args.stages = "transcript,illustrate"
    return cmd_run(args)


def cmd_studio(args) -> int:
    import os
    from .studio.run import run_studio

    cfg = load_config(args.config)
    needed = [] if args.draft else ["GEMINI_API_KEY" if cfg.images.provider == "gemini" else "OPENAI_API_KEY"]
    needed.append("ANTHROPIC_API_KEY")
    missing = [k for k in dict.fromkeys(needed) if not os.environ.get(k)]
    if missing:
        print(env.explain_missing(missing, Path(args.config).resolve().parent / ".env"), file=sys.stderr)
        return 2
    try:
        run_studio(cfg, args.stem, level=args.level, start=args.start * 60, minutes=args.minutes, assume_yes=args.yes,
                   plan_only=args.plan_only, replan=args.replan, draft=args.draft, preview=args.preview)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Studio stopped: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_update(args) -> int:
    return update.run_update(reinstall=not args.no_install)


def cmd_review(args) -> int:
    cfg = load_config(args.config)
    page = review.build_review(cfg.paths.output_dir, [pipeline.load_manifest(cfg, s) for s in pipeline.discover_stems(cfg)])
    print(page)
    return 0


def cmd_choose(args) -> int:
    cfg = load_config(args.config)
    m = pipeline.load_manifest(cfg, args.stem)
    match = [a for a in m.art if Path(a.path).name == args.art or a.path == args.art]
    if not match:
        print(f"No artwork named {args.art!r} for {args.stem}. Known: {[Path(a.path).name for a in m.art]}", file=sys.stderr)
        return 1
    m.chosen_art = match[0].path
    for stage in ("compose", "video"):
        if stage in m.done:
            m.done.remove(stage)
    m.save(pipeline.program_dir(cfg, args.stem) / "manifest.json")
    print(f"{args.stem}: chose {Path(m.chosen_art).name}. Re-run: python -m sermon_visuals run --only {args.stem} --stages compose,video")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sermon-visuals", description="Artwork, quote cards and video for archived sermon audio.")
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="write config.toml and create folders").set_defaults(fn=cmd_init)
    sub.add_parser("doctor", help="check Python, ffmpeg, fonts, keys and inputs").set_defaults(fn=cmd_doctor)
    sub.add_parser("list", help="show each program's progress").set_defaults(fn=cmd_list)
    r = sub.add_parser("run", help="run the pipeline")
    r.add_argument("--only", nargs="*", metavar="STEM", help="limit to these programs")
    r.add_argument("--stages", help=f"comma-separated subset of {','.join(pipeline.STAGES)}")
    r.add_argument("--force", action="store_true", help="redo stages already marked done")
    r.set_defaults(fn=cmd_run)
    il = sub.add_parser("illustrate", help="illustrated-narration video (one sketch per idea, timed to the audio)")
    il.add_argument("--only", nargs="*", metavar="STEM")
    il.add_argument("--force", action="store_true", help="re-plan and re-draw even if already done")
    il.set_defaults(fn=cmd_illustrate)
    sp = sub.add_parser("studio", help="densely illustrated, layered video with an intensity dial (levels 1 to 5)")
    sp.add_argument("stem", help="program name, e.g. about_christmas")
    sp.add_argument("--level", type=int, choices=[1, 2, 3, 4, 5], help="visual intensity; default from config")
    sp.add_argument("--minutes", type=float, help="render only this many minutes (start with 5)")
    sp.add_argument("--start", type=float, default=0.0, help="minute to start from (default 0)")
    sp.add_argument("--draft", action="store_true", help="free placeholder pictures, to check pacing before paying for art")
    sp.add_argument("--preview", action="store_true", help="half-size render, about 3x faster")
    sp.add_argument("--plan-only", action="store_true", help="make the storyboard and the cost estimate, then stop")
    sp.add_argument("--replan", action="store_true", help="discard the saved storyboard for this level and make a new one")
    sp.add_argument("--yes", action="store_true", help="skip the spend confirmation")
    sp.set_defaults(fn=cmd_studio)
    up = sub.add_parser("update", help="download the latest code from GitHub and replace it in place (keeps config, keys, programs, output)")
    up.add_argument("--no-install", action="store_true", help="skip pip install after updating")
    up.set_defaults(fn=cmd_update)
    sub.add_parser("review", help="rebuild output/review.html").set_defaults(fn=cmd_review)
    c = sub.add_parser("choose", help="pick which artwork a program's cards use")
    c.add_argument("stem")
    c.add_argument("art", help="file name shown on the review page, e.g. concept2_1.png")
    c.set_defaults(fn=cmd_choose)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    env.load_dotenv(Path(args.config).resolve().parent / ".env")
    return args.fn(args)
