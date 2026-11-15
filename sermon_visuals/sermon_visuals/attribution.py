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

RECLAIM_WINDOW = 12  # lines after a cut-off verse that are given back to scripture if the block turns out to go on

NUMBERED_BOOKS = r"(?:Samuel|Kings|Chronicles|Corinthians|Thessalonians|Timothy|Peter|John|Esdras|Maccabees)"
_VERSIONS = (r"(?:A?KJV|NKJV|KJ21|MKJV|NIV|NIRV|ESV|NASB|NASU|NRSV|RSV|ASV|NLT|YLT|RV|GNB|GNT|TEV|NEB|REB|NJB|JB|CEV|"
             r"MSG|AMP|HCSB|CSB|NET|LXX|DRA|WEB|Moffatt|Phillips|Weymouth|Darby|[A-Z]{1,5}V)")
_BOOK = r"(?:(?:[1-3]|I{1,3})\s*)?(?:St\.?\s+)?[A-Z][A-Za-z]*\.?(?:\s+(?:of\s+)?[A-Z][A-Za-z]*\.?)*"
_VERSES = r"\d{1,3}(?:\s*[-–]\s*\d{1,3})?"
_CHAPTER = (rf"\d{{1,3}}(?:\s*[:.]\s*{_VERSES}(?:\s*,\s*{_VERSES})*)?"
            r"(?:\s*[-–]\s*\d{1,3}(?:\s*[:.]\s*\d{1,3})?)?")
_REFERENCE = re.compile(rf"^{_BOOK}\s+{_CHAPTER}(?:\s*\(?{_VERSIONS}\)?)?[.:]?$")  # "Luke 1", "Ps. 23", "John 3:16 (ESV)."
_VERSION = re.compile(rf"^\(?{_VERSIONS}\)?\.?$")
_VERSE_START = re.compile(r"^\d{1,3}\.?\s+[\[(“\"‘'A-Z]")  # "5 To be taxed", "2. Even as"
_NOT_VERSE = re.compile(rf"^[1-3]\s+{NUMBERED_BOOKS}\b")  # "1 John begins by saying" is his, not verse 1
_DASH_CREDIT = re.compile(r"^[—–―-]\s*[A-Z]")  # "— C. S. Lewis, Mere Christianity."
_CLOSING = tuple(".,;:!?\"'”’)]…—–-»")
_SINGLE_CLOSE = re.compile(r"’(?![A-Za-z])")  # a closing single quote, not the apostrophe in "author’s"


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


def _credit(line: str) -> bool:
    """A short line naming a source: "American Presbyterian Church  Christ's Birth", the same with a full stop
    (then every longer word must be capitalised), or one that starts with a dash ("— C. S. Lewis")."""
    if len(line) > SHORT_LINE:
        return False
    if _DASH_CREDIT.match(line):
        return True
    bare = line.rstrip(".")
    if _is_closed(bare) or not _titled(bare):
        return False
    return bare == line or not any(w[:1].islower() and len(w) >= 4 for w in bare.split())


def _classify_lines(lines: list[str]) -> list[str | None]:
    kinds: list[str | None] = [None] * len(lines)  # None = blank
    in_block = False
    continuation = 0
    cut_at: int | None = None  # where a long verse was assumed to end; given back if the block goes on

    def previous(i: int) -> int | None:
        """The nearest line above that is not blank or a short note (a quotation's last line can be short)."""
        j = i - 1
        while j >= 0 and kinds[j] in (None, EDITOR_NOTE):
            j -= 1
        return j if j >= 0 else None

    def claim_back(i: int, kind: str) -> None:
        """Mark his lines above i as `kind`, back to a line ending in a colon (his lead-in), a scripture or
        other-author line, or the top; headings and notes on the way are passed over. Used when a credit
        line or a stray version label shows a block has just ended."""
        j = i - 1
        while j >= 0:
            if kinds[j] is None or kinds[j] == EDITOR_NOTE:
                j -= 1
                continue
            if kinds[j] != SPEAKER or lines[j].endswith(":"):
                break
            kinds[j] = kind
            j -= 1

    def reclaim(i: int) -> None:
        """A block marker after a cut-off verse: the lines since the cut were scripture after all."""
        nonlocal cut_at
        if cut_at is not None:
            for j in range(cut_at, i):
                if kinds[j] == SPEAKER:
                    kinds[j] = SCRIPTURE
        cut_at = None

    for i, line in enumerate(lines):
        if not line:
            continue
        if cut_at is not None and i - cut_at > RECLAIM_WINDOW:
            cut_at = None
        if _VERSION.match(line):
            kinds[i] = SCRIPTURE
            reclaim(i)
            if not in_block:
                claim_back(i, SCRIPTURE)
            in_block, continuation = False, 0
        elif _REFERENCE.match(line) or (_VERSE_START.match(line) and not _NOT_VERSE.match(line)):
            kinds[i], in_block, continuation = SCRIPTURE, True, 0
            reclaim(i)
        elif in_block and continuation < MAX_VERSE_CONTINUATION:
            kinds[i] = SCRIPTURE
            continuation += 1
        else:
            if in_block:  # a verse ran on past the limit: assume the block ended, but remember where
                cut_at = i
            in_block = False
            p = previous(i)
            if p is not None and kinds[p] == SPEAKER and _credit(line):
                kinds[i] = OTHER_AUTHOR  # a credit line: the block above it is someone else's
                claim_back(i, OTHER_AUTHOR)
                cut_at = None
            elif len(line) <= SHORT_LINE and not _is_closed(line):
                kinds[i] = EDITOR_NOTE  # title, byline, heading, address block, or a short wrapped line
            else:
                kinds[i] = SPEAKER
    return kinds


def _quoted_regions(text: str) -> list[tuple[int, int, str]]:
    """(start, end, kind) for [bracketed] notes and quotations in a run of his prose. A quotation with no closing
    mark runs to the end of the run (it goes on into the next paragraph); a closing mark with no opening one
    means the quotation began before the run, so it covers the run from its start."""
    regions: list[tuple[int, int, str]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        end = None
        if ch == "[":
            j = text.find("]", i + 1)
            end, kind = (n if j < 0 else j + 1), EDITOR_NOTE
        elif ch in "“«\"":
            j = text.find({"“": "”", "«": "»", '"': '"'}[ch], i + 1)
            end, kind = (n if j < 0 else j + 1), "quote"
        elif ch == "‘":
            m = _SINGLE_CLOSE.search(text, i + 1)
            end, kind = (n if m is None else m.end()), "quote"
        elif ch in "”»":
            regions = [(0, i + 1, "quote")]  # everything before it was inside the quotation
        if end is not None:
            regions.append((i, end, kind))
            i = end
        else:
            i += 1
    return regions


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

    for start, end, kind in _quoted_regions(text):
        if kind == "quote":
            if len(normalize(text[start:end]).split()) < MIN_QUOTED_WORDS:
                continue
            kind = OTHER_AUTHOR
        add(SPEAKER, cursor, start)
        add(kind, start, end)
        cursor = end
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
    """SPEAKER if the phrase sits wholly inside one stretch of his own words and nowhere else; otherwise the
    kind of the first other span that holds it (a phrase he says himself and also reads from scripture or
    another author cannot be told apart on screen, so it is not his); None if it is nowhere as one piece."""
    pattern = _pattern(phrase)
    if pattern is None:
        return None
    his, other = False, None
    for span in spans:
        if pattern.search(normalize(span.text)):
            if span.kind == SPEAKER:
                his = True
            else:
                other = other or span.kind
    return other or (SPEAKER if his else None)


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
