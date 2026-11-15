"""The look of the text layers, chosen by the [studio] style name in config.toml.

Every style except ink keeps the original look: cream type with a dark outline, dark translucent cards and
scripture band (the renderer's own constants, untouched). Ink is the first light-paper theme: charcoal type with
no outline on clean white paper, paper panels with a thin ink rule, one deep orange accent, and pictures that are
cleaned up (studio.ink.normalize) as they are loaded. Only how things look changes: which words are shown, and
when the scripture band appears and pages, are decided elsewhere and are the same for every theme."""

from __future__ import annotations

from dataclasses import dataclass

from ..verses import DARK_BAND, PAPER_BAND, Band
from .levels import is_whiteboard


@dataclass(frozen=True)
class Theme:
    name: str = "dark"
    paper: bool = False               # draw the text layers for light paper
    clean_pictures: bool = False      # run each picture through studio.ink.normalize when it is loaded
    text_waits_for_picture: bool = False  # key phrases and cards appear only once their picture is fully drawn
    drawn: bool = False               # new pictures are drawn on, item by item (studio.drawon), when ink_in is on
    band: Band = DARK_BAND
    ink: tuple[int, int, int] = (32, 32, 32)          # charcoal: type and rules (paper themes)
    accent: tuple[int, int, int] = (180, 80, 30)      # deep orange, above 4.5:1 on white (paper themes)
    paper_rgb: tuple[int, int, int] = (252, 251, 248)  # panels and backings (paper themes)
    muted: tuple[int, int, int] = (74, 74, 74)        # secondary type, about 8.5:1 on white (paper themes)
    track: tuple[int, int, int] = (226, 223, 216)     # the unfilled part of the progress bar (paper themes)


DARK = Theme()
INK = Theme(name="ink", paper=True, clean_pictures=True, text_waits_for_picture=True, drawn=True, band=PAPER_BAND)


def theme_for(style: str) -> Theme:
    """INK for the whiteboard styles, DARK (the original look) for everything else, custom style sentences included."""
    return INK if is_whiteboard(style) else DARK
