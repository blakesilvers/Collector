"""Decide whether a solicitation is window-cleaning / pressure-washing work."""
from __future__ import annotations

import re

from .models import Opportunity

# (label, category, regex). Matched case-insensitively against title, then description.
STRONG_TERMS = [
    ("window cleaning", "window", r"\bwindows?\s+(?:and\s+[\w-]+\s+|&\s+[\w-]+\s+)?(?:cleaning|washing|clean|wash)\b"),
    ("window washing", "window", r"\bwindow[- ]?wash"),
    ("glass cleaning", "window", r"\b(?:exterior\s+|interior\s+)?glass\s+(?:cleaning|washing)\b"),
    ("high-rise windows", "window", r"\bhigh[- ]rise\s+(?:window|glass|building\s+clean)"),
    ("pressure washing", "pressure", r"\bpressure[- ]?(?:wash|clean)(?:ing|ed)?\b(?!\s*(?:er|ers|machine|unit|equipment|trailer)s?\b)"),
    ("power washing", "pressure", r"\bpower[- ]?wash(?:ing|ed)?\b(?!\s*(?:er|ers|machine|unit|equipment|trailer)s?\b)"),
    ("soft washing", "pressure", r"\bsoft[- ]?wash"),
    ("hydro washing", "pressure", r"\bhydro[- ]?(?:wash|blast)"),
    ("steam/pressure cleaning", "pressure", r"\bsteam\s+(?:and\s+pressure\s+)?clean"),
    ("exterior building cleaning", "pressure", r"\bexterior\s+(?:building\s+|facade\s+|surface\s+)?(?:cleaning|washing)\b"),
    ("building washing", "pressure", r"\bbuilding\s+(?:exterior\s+)?(?:wash|washing|cleaning,?\s+exterior)\b"),
    ("facade cleaning", "pressure", r"\bfa[cç]ade\s+(?:clean|wash|restoration)"),
    ("surface cleaning", "pressure", r"\b(?:concrete|sidewalk|pavement|plaza|parking\s+(?:garage|lot|deck|structure)|garage|roof|gutter|dumpster\s+pad|bleacher|stadium|grandstand|awning|canopy|panel)s?\s+(?:clean[- ]?up|cleaning|washing|wash)\b"),
    ("graffiti removal", "pressure", r"\bgraffiti\s+(?:removal|abatement|clean)"),
    ("fleet washing", "pressure", r"\b(?:fleet|vehicle|bus|truck|equipment)\s+(?:washing|wash(?:ing)?\s+services?)\b"),
]

# Work that often bundles window cleaning but is not the core trade.
RELATED_TERMS = [
    ("janitorial", "related", r"\bjanitorial\b"),
    ("custodial", "related", r"\bcustodial\b"),
    ("building cleaning", "related", r"\b(?:building|facility|facilities|office|commercial)\s+cleaning\b"),
    ("post-construction cleaning", "related", r"\b(?:post[- ]construction|final|make[- ]ready)\s+clean"),
]

# Titles that use the words but are not a cleaning service.
NEGATIVE_TERMS = [
    r"\bwindows?\s+(?:install|replac|repair|tint|film|blind|treatment|covering|unit|air\s+condition|a/?c\b)",
    r"\bwindshield",
    r"\b(?:microsoft\s+)?windows\s+(?:server|10|11|os|licen)",
    r"\bdish\s?wash",
    r"\b(?:pressure|power)\s+washers?\b(?!\s+serv)",
    r"\bcar\s+wash\s+(?:equipment|system|install)",
]

CODE_TERMS = {
    # NIGP (Texas ESBD) — confirmed on txsmartbuy.gov
    "91081": ("NIGP 910-81 Window Washing", "window", 3),
    "91003": ("NIGP 910-03 Building Cleaning, Exterior", "pressure", 3),
    "96221": ("NIGP 962-21 Cleaning Svcs, Steam & Pressure", "related", 1),
    "91039": ("NIGP 910-39 Janitorial/Custodial", "related", 1),
    # NAICS (SAM.gov)
    "561720": ("NAICS 561720 Janitorial (incl. window cleaning)", "related", 1),
    "561790": ("NAICS 561790 Other Services to Buildings (incl. pressure washing)", "related", 1),
    # PSC (SAM.gov)
    "S201": ("PSC S201 Custodial/Janitorial", "related", 1),
    "S299": ("PSC S299 Housekeeping-Other", "related", 1),
}


def _compile(items):
    return [(label, cat, re.compile(rx, re.I)) for label, cat, rx in items]


_STRONG = _compile(STRONG_TERMS)
_RELATED = _compile(RELATED_TERMS)
_NEGATIVE = [re.compile(rx, re.I) for rx in NEGATIVE_TERMS]


def _scan(text: str, compiled, extra_negative=()):
    hits = []
    for label, cat, rx in compiled:
        for m in rx.finditer(text):
            window = text[max(0, m.start() - 5): m.end() + 25]
            if any(n.search(window) for n in _NEGATIVE) or any(n.search(window) for n in extra_negative):
                continue
            hits.append((label, cat))
            break
    return hits


def score(opp: Opportunity, extra_keywords: list[str] | None = None, exclude: list[str] | None = None) -> None:
    """Populate opp.match_score / match_terms / category in place.

    Scoring: strong term in title 3, in description 2; code matches per
    CODE_TERMS; related (janitorial) terms 1. A score of 0 means no match.
    """
    extra = _compile([(k, "window" if "window" in k.lower() else "pressure", re.escape(k)) for k in (extra_keywords or [])])
    excl = [re.compile(re.escape(x), re.I) for x in (exclude or [])]
    title, desc = opp.title or "", opp.description or ""
    if any(x.search(title) for x in excl):
        opp.match_score, opp.match_terms, opp.category = 0, [], ""
        return

    points, terms, cats = 0, [], set()
    for label, cat in _scan(title, _STRONG + extra, excl):
        points += 3; terms.append(label); cats.add(cat)
    for label, cat in _scan(desc, _STRONG + extra, excl):
        if label not in terms:
            points += 2; terms.append(label + " (description)"); cats.add(cat)
    for code in opp.codes:
        digits = re.match(r"\s*([A-Z]?\d+)", code)
        if not digits:
            continue
        hit = CODE_TERMS.get(digits.group(1))
        if hit and hit[0] not in terms:
            points += hit[2]; terms.append(hit[0]); cats.add(hit[1])
    if not cats - {"related"}:
        for label, cat in _scan(title + " " + desc, _RELATED, excl):
            if label not in terms:
                points += 1; terms.append(label); cats.add(cat)

    core = cats & {"window", "pressure"}
    opp.category = "both" if len(core) == 2 else (next(iter(core)) if core else ("related" if cats else ""))
    opp.match_score, opp.match_terms = points, terms
