"""The intensity dial. Each level is a preset of layers; config can override any single layer."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace


@dataclass(frozen=True)
class Layers:
    beat_seconds: float      # how often the picture changes
    callouts: bool           # key phrases pop on screen word by word as they are spoken
    chapters: bool           # chapter title cards and a persistent chapter label
    progress: bool           # thin progress bar
    cards: bool              # info cards: lists, timelines, definitions
    captions: bool           # word-highlighted captions
    ink_in: bool             # each new picture inks in over the last one instead of dissolving
    motion_share: float      # fraction of running time shown as AI-animated clips
    camera: float            # how far the virtual camera travels on stills (0 = static)


LEVELS: dict[int, Layers] = {
    1: Layers(beat_seconds=40, callouts=False, chapters=True, progress=False, cards=False, captions=False, ink_in=False, motion_share=0.0, camera=0.05),
    2: Layers(beat_seconds=20, callouts=True, chapters=True, progress=True, cards=False, captions=False, ink_in=True, motion_share=0.0, camera=0.08),
    3: Layers(beat_seconds=12, callouts=True, chapters=True, progress=True, cards=True, captions=False, ink_in=True, motion_share=0.0, camera=0.10),
    4: Layers(beat_seconds=9, callouts=True, chapters=True, progress=True, cards=True, captions=True, ink_in=True, motion_share=0.30, camera=0.12),
    5: Layers(beat_seconds=7, callouts=True, chapters=True, progress=True, cards=True, captions=True, ink_in=True, motion_share=0.70, camera=0.14),
}

LEVEL_BLURBS = {
    1: "a new picture about every 40 seconds, chapter titles, scripture on screen",
    2: "picture every 20 seconds, key phrases pop on screen, progress bar, pictures ink in",
    3: "picture every 12 seconds, plus info cards (lists, timelines, definitions)",
    4: "picture every 9 seconds, word-highlighted captions, about 30% of the time AI-animated",
    5: "picture every 7 seconds, about 70% of the time AI-animated",
}


def layers_for(level: int, overrides: dict | None = None) -> Layers:
    if level not in LEVELS:
        raise ValueError(f"level must be 1 to 5, got {level}")
    base = LEVELS[level]
    valid = {f.name for f in fields(Layers)}
    clean = {k: v for k, v in (overrides or {}).items() if k in valid}
    unknown = set(overrides or {}) - valid
    if unknown:
        raise ValueError(f"unknown [studio.layers] setting(s): {sorted(unknown)}; valid: {sorted(valid)}")
    return replace(base, **clean)


# Styles drawn like a whiteboard explainer: black line on plain white paper with one accent. The planner, the text
# layers and the picture clean-up all switch on this (keyed by the [studio] style name in config.toml).
WHITEBOARD_STYLES = frozenset({"ink"})


def is_whiteboard(style: str) -> bool:
    return (style or "").strip().lower() in WHITEBOARD_STYLES


STYLES = {
    "graphic": (
        "Bold graphic editorial illustration. Thick confident black ink outlines, flat saturated colour fields in a limited "
        "palette of deep navy, burnt orange, warm cream and antique gold, strong silhouettes, dramatic directional light, "
        "high contrast, subtle screen-print texture. Cinematic composition with one clear focal subject and open space around it."
    ),
    # Chosen in a paid style test (2026-11-14): subjects drawn as separate icons on blank paper, no background, and
    # one simple way of drawing every figure. Changing this text changes the cache key of every ink picture.
    "ink": (
        "Whiteboard-explainer illustration in black felt-tip pen with a light grey marker wash and one burnt-orange accent "
        "colour. Draw only the subjects named in the scene, each as a separate, self-contained drawing standing alone on "
        "blank white paper with clear white space between them and wide empty margins, like icons drawn on a whiteboard; at "
        "most a short ground line under a figure. No background: no room, walls, floor, landscape, horizon, sky, frame or "
        "border. Simple generic figures: round heads, dot eyes, plain robes, no detailed faces. Every figure is drawn the "
        "same simple way: round head, dot eyes, a plain head cloth or short hair, no wrinkles, no detailed beards."
    ),
    "woodcut": (
        "Modern woodcut / linocut print illustration, heavy carved black shapes, two spot colours (ochre and deep teal) on cream "
        "paper, bold simplified forms, visible print grain, striking poster-like composition."
    ),
    "painterly": (
        "Painterly editorial illustration, rich warm palette (ochre, umber, deep blue), strong light and shadow, visible brushwork, "
        "dignified and cinematic, in the tradition of classic Bible-lands painting."
    ),
}
