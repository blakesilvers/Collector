"""Whose words are these? Splits a reference transcript into spans that are the speaker's own words,
scripture, another author's words, or an editor's note, so nothing but his own words is ever shown on
screen as his.

Built for the ministry's edited transcripts (PDF or .txt), whose layout is:
- the speaker's paragraphs, wrapped at a fixed width, often ending in a colon before a reading;
- scripture blocks: a reference line ("Luke 1", "1 John 4"), numbered verse lines, then a version
  label line ("AKJV", "KJV"); blocks float away from where they are read, and a block can start with
  verse lines and no reference line;
- another author's words (an article he reads) set apart and followed by a short credit line with no
  closing punctuation ("American Presbyterian Church  Christ's Birth");
- [bracketed] notes, inside scripture or his paragraphs;
- a title and byline at the top and the ministry's address block at the end.

It is conservative by design: when it is unsure whether a stretch is his, it treats it as not his.
Quotations of four or more words inside his paragraphs (in quotation marks) are treated as someone
else's words, and a phrase must sit wholly inside one stretch of his own words to count as his.
A machine transcript of the audio has none of these marks, so everything in it counts as his."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

SPEAKER = "speaker"
SCRIPTURE = "scripture"
OTHER_AUTHOR = "other-author"
EDITOR_NOTE = "editor-note"

SHORT_LINE = 80  # a line this short with no closing punctuation is a heading, credit or footer, not his prose
MAX_VERSE_CONTINUATION = 6  # lines of a verse without a new verse number before a block is assumed to have ended
MIN_QUOTED_WORDS = 4  # shorter quotations inside his sentences ("the Word", "nativity") stay his

NUMBERED_BOOKS = r"(?:Samuel|Kings|Chronicles|Corinthians|Thessalonians|Timothy|Peter|John|Esdras|Maccabees)"
_REFERENCE = re.compile(
    r"^(?:[1-3]\s*)?[A-Z][a-z]+(?:\s+(?:of\s+)?[A-Z][a-z]+)*\s+\d{1,3}(?::\d{1,3}(?:\s*[-–]\s*\d{1,3})?)?$")
_VERSION = re.compile(
    r"^\(?(?:A?KJV|NKJV|KJ21|MKJV|NIV|NIRV|ESV|NASB|NASU|NRSV|RSV|ASV|NLT|YLT|RV|GNB|GNT|TEV|NEB|REB|NJB|JB|CEV|MSG|AMP|"
    r"HCSB|CSB|NET|LXX|DRA|WEB|Moffatt|Phillips|Weymouth|Darby|[A-Z]{1,5}V)\)?\.?$")
_VERSE_START = re.compile(r"^\d{1,3}\s+[\[(“\"‘'A-Z]")
_NOT_VERSE = re.compile(rf"^[1-3]\s+{NUMBERED_BOOKS}\b")  # "1 John begins by saying" is his, not verse 1
_CLOSING = tuple(".,;:!?\"'”’)]…—–-")
_INLINE = re.compile(r"\[[^\]]*\]|“[^“”]*”|\"[^\"]*\"")


@dataclass
class Span:
    kind: str
    text: str
    first_line: int  # 1-based, inclusive
    last_line: int


def normalize(text: str) -> str:
    """Lower-case, fold compatibility characters (no-break spaces, U+037E), strip accents and punctuation,
    collapse whitespace. Used only for matching words."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean(line: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", line).split())


def _is_closed(line: str) -> bool:
    return line.endswith(_CLOSING)


def _titled(line: str) -> bool:
    """Looks like a name or title: two or more capitalised words."""
    return sum(1 for w in line.split() if w[:1].isupper()) >= 2


def _classify_lines(lines: list[str]) -> list[str | None]:
    kinds: list[str | None] = [None] * len(lines)  # None = blank
    in_block = False
    continuation = 0

    def previous(i: int) -> int | None:
        j = i - 1
        while j >= 0 and kinds[j] is None:
            j -= 1
        return j if j >= 0 else None

    def claim_back(i: int, kind: str) -> None:
        """Mark his lines above i as `kind`, back to a line ending in a colon (his lead-in), a non-speaker
        line or the top. Used when a credit line or a stray version label shows a block has just ended."""
        j = i - 1
        while j >= 0:
            if kinds[j] is None:
                j -= 1
                continue
            if kinds[j] != SPEAKER or lines[j].endswith(":"):
                break
            kinds[j] = kind
            j -= 1

    for i, line in enumerate(lines):
        if not line:
            continue
        if _VERSION.match(line):
            kinds[i] = SCRIPTURE
            if not in_block:
                claim_back(i, SCRIPTURE)
            in_block, continuation = False, 0
        elif _REFERENCE.match(line):
            kinds[i], in_block, continuation = SCRIPTURE, True, 0
        elif _VERSE_START.match(line) and not _NOT_VERSE.match(line):
            kinds[i], in_block, continuation = SCRIPTURE, True, 0
        elif in_block and continuation < MAX_VERSE_CONTINUATION:
            kinds[i] = SCRIPTURE
            continuation += 1
        else:
            in_block = False
            if len(line) <= SHORT_LINE and not _is_closed(line):
                p = previous(i)
                if p is not None and kinds[p] == SPEAKER and _titled(line):
                    kinds[i] = OTHER_AUTHOR  # a credit line: the block above it is someone else's
                    claim_back(i, OTHER_AUTHOR)
                else:
                    kinds[i] = EDITOR_NOTE  # title, byline, heading, address block
            else:
                kinds[i] = SPEAKER
    return kinds


def _split_speaker(text: str, offsets: list[tuple[int, int]]) -> list[Span]:
    """Split a run of his prose at [bracketed] notes and longer quotations."""
    def line_at(pos: int) -> int:
        best = offsets[0][1]
        for start, number in offsets:
            if start <= pos:
                best = number
        return best

    spans: list[Span] = []
    cursor = 0

    def add(kind: str, a: int, b: int) -> None:
        piece = text[a:b].strip()
        if piece:
            spans.append(Span(kind, piece, line_at(a), line_at(max(a, b - 1))))

    for m in _INLINE.finditer(text):
        body = m.group(0)
        if body.startswith("["):
            kind = EDITOR_NOTE
        elif len(normalize(body).split()) >= MIN_QUOTED_WORDS:
            kind = OTHER_AUTHOR
        else:
            continue
        add(SPEAKER, cursor, m.start())
        add(kind, m.start(), m.end())
        cursor = m.end()
    add(SPEAKER, cursor, len(text))
    return spans


def split_transcript(text: str) -> list[Span]:
    """The transcript as consecutive spans, each one of SPEAKER, SCRIPTURE, OTHER_AUTHOR or EDITOR_NOTE."""
    lines = [_clean(raw) for raw in text.splitlines()]
    kinds = _classify_lines(lines)
    spans: list[Span] = []
    i = 0
    while i < len(lines):
        if kinds[i] is None:
            i += 1
            continue
        kind, j = kinds[i], i
        while j + 1 < len(lines) and kinds[j + 1] == kind:
            j += 1
        if kind == SPEAKER:
            parts, offsets, pos = [], [], 0
            for k in range(i, j + 1):
                offsets.append((pos, k + 1))
                parts.append(lines[k])
                pos += len(lines[k]) + 1
            spans.extend(_split_speaker(" ".join(parts), offsets))
        else:
            spans.append(Span(kind, " ".join(lines[i:j + 1]), i + 1, j + 1))
        i = j + 1
    return spans


def _pattern(phrase: str) -> re.Pattern | None:
    words = normalize(phrase).split()
    if not words:
        return None
    # A space between two words may be missing in the transcript ("firstcentury", "hugelyimportant"), but the
    # phrase must start and end on word boundaries.
    return re.compile(r"(?<![a-z0-9'])" + " ?".join(re.escape(w) for w in words) + r"(?![a-z0-9'])")


def whose(phrase: str, spans: list[Span]) -> str | None:
    """SPEAKER if the phrase sits wholly inside one stretch of his own words; otherwise the kind of the first
    other span that holds it; None if it is nowhere in the transcript as one piece."""
    pattern = _pattern(phrase)
    if pattern is None:
        return None
    other = None
    for span in spans:
        if pattern.search(normalize(span.text)):
            if span.kind == SPEAKER:
                return SPEAKER
            other = other or span.kind
    return other


def is_his(phrase: str, spans: list[Span]) -> bool:
    return whose(phrase, spans) == SPEAKER


REASONS = {
    SCRIPTURE: "not his own words: it is in a scripture passage in the transcript",
    OTHER_AUTHOR: "not his own words: it is in a quotation from another author in the transcript",
    EDITOR_NOTE: "not his own words: it is in an editor's note in the transcript",
    None: "not found among his own words in the transcript",
}


def counts(spans: list[Span]) -> dict[str, int]:
    """Lines of each kind (a line split between kinds counts for each)."""
    lines: dict[str, set[int]] = {}
    for s in spans:
        lines.setdefault(s.kind, set()).update(range(s.first_line, s.last_line + 1))
    return {k: len(v) for k, v in lines.items()}
