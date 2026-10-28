"""Offline geocoding against bundled U.S. Census Gazetteer data.

Bid listings rarely carry coordinates. We infer a location from the text we do
have (agency name, title, place-of-performance city/zip) by matching it to
Census places, counties and ZIP code tabulation areas, then measure the
great-circle distance to the office.
"""
from __future__ import annotations

import csv
import gzip
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"

# Place names that are also everyday words in bid titles. A bare match on one
# of these is ignored; "City of Center" or "Center ISD" still resolves.
AMBIGUOUS_PLACES = {
    "center", "commerce", "mission", "price", "post", "industry", "energy",
    "garden", "golden", "grand", "hospital", "justice", "lake", "liberty",
    "union", "park", "point", "plains", "rule", "ranger", "star", "sun",
    "valley", "cross", "early", "march", "may", "best", "royal", "state",
    "national", "service", "services", "city", "county", "north", "south",
    "east", "west", "central", "college", "university", "academy", "chase",
    "garrison", "global", "golf", "harbor", "highland", "holiday", "home",
    "hope", "humble", "jewett", "joy", "lone", "major", "mobile", "nash",
    "normal", "orange grove", "paradise", "pearl", "pine", "pleasant", "plum",
    "providence", "quail", "reno", "richland", "rio", "river", "roma", "rose",
    "salt", "silver", "smith", "spring", "summit", "sunset", "tool", "venus",
    "victory", "vista", "water", "welcome", "white", "winters", "wood", "york",
    "texas", "texas city", "angus", "bee", "berry", "blue", "buffalo", "camp",
    "cash", "christmas", "cool", "crane", "dale", "deport", "edge", "elm",
    "fate", "ferris", "fields", "fruitvale", "goodrich", "groves", "hawk",
    "hill", "honey", "iron", "kingdom", "ivy", "lamb", "lane", "lima",
    "lucas", "midway", "miles", "mineral", "nevada", "newark", "nome",
    "office", "palace", "pecan", "point", "reed", "salem", "seven points",
    "shore", "station", "streetman", "sunrise", "trinity", "troy", "tulip",
    "turkey", "west point", "windom", "start", "blanket", "limestone",
    "unit", "memorial", "region", "statewide", "annual", "contract", "level",
    "medina", "media", "loving", "sabine", "tom", "draw", "lavon", "mustang",
}

# Text that sits between a place name and an organisation word, e.g. the
# "ISD" in "Tyler ISD" or "City of" in "City of Tyler".
_PLACE_CONTEXT_AFTER = r"(?:ISD|CISD|Independent|City|County|College|Junior|Community|Regional|Airport|Area|District|Office|Campus|Housing|Public|Utilities|Water|Maintenance|Facility|Center|Area Office|State|Hospital|Library|Police|Fire|Parks|,\s*(?:TX|Tex|Texas|LA|AR|OK)\b)"
_PLACE_CONTEXT_BEFORE = r"(?:City of|Town of|Village of|Port of|in|at|near)\s+$"

_WORD = re.compile(r"[A-Za-z][A-Za-z'.&-]*")
_COUNTY = re.compile(r"\b([A-Z][A-Za-z'.-]*(?:\s+[A-Z][A-Za-z'.-]*){0,2})\s+(County|Parish|Co)(?![A-Za-z])", re.I)


@dataclass(frozen=True)
class Location:
    name: str
    state: str
    lat: float
    lon: float
    kind: str          # place | county | zip | override | office
    evidence: str = ""  # the text that produced the match

    def label(self) -> str:
        return f"{self.name}, {self.state}"


def distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _read(name):
    with gzip.open(DATA_DIR / name, "rt", encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


@lru_cache(maxsize=1)
def _places():
    out: dict[str, dict[str, tuple[str, float, float]]] = {}
    for r in _read("places.csv.gz"):
        key = r["name"].lower()
        # Keep the first (Census lists the incorporated place before CDPs of
        # the same name only by GEOID, so prefer whichever comes first).
        out.setdefault(r["state"], {}).setdefault(key, (r["name"], float(r["lat"]), float(r["lon"])))
    return out


@lru_cache(maxsize=1)
def _counties():
    out: dict[str, dict[str, tuple[str, float, float]]] = {}
    for r in _read("counties.csv.gz"):
        base = re.sub(r"\s+(County|Parish|Borough|Census Area|Municipality|city)$", "", r["name"]).lower()
        out.setdefault(r["state"], {})[base] = (r["name"], float(r["lat"]), float(r["lon"]))
    return out


@lru_cache(maxsize=1)
def _zctas():
    return {r["zip"]: (float(r["lat"]), float(r["lon"])) for r in _read("zcta.csv.gz")}


def lookup_zip(zipcode: str | None) -> Location | None:
    if not zipcode:
        return None
    z = re.sub(r"\D", "", str(zipcode))[:5]
    hit = _zctas().get(z)
    if hit:
        return Location(z, "", hit[0], hit[1], "zip", z)
    return None


def lookup_place(name: str, state: str) -> Location | None:
    if not name or not state:
        return None
    key = name.strip().lower()
    hit = _places().get(state.upper(), {}).get(key)
    if hit:
        return Location(hit[0], state.upper(), hit[1], hit[2], "place", name)
    base = re.sub(r"\s+(county|parish)$", "", key)
    hit = _counties().get(state.upper(), {}).get(base)
    if hit:
        return Location(hit[0], state.upper(), hit[1], hit[2], "county", name)
    return None


def parse_location_spec(spec: str) -> Location | None:
    """'Tyler, TX' or '32.31,-95.24' -> Location."""
    spec = spec.strip()
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", spec)
    if m:
        return Location(spec, "", float(m.group(1)), float(m.group(2)), "override", spec)
    m = re.fullmatch(r"(.+?),\s*([A-Za-z]{2})", spec)
    if m:
        return lookup_place(m.group(1), m.group(2))
    return None


def _is_shouting(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.8


def find_locations(text: str, states: list[str]) -> list[Location]:
    """Every place/county in `states` that `text` plausibly names."""
    if not text:
        return []
    found: list[Location] = []
    seen = set()

    def add(loc: Location):
        k = (loc.name, loc.state)
        if k not in seen:
            seen.add(k)
            found.append(loc)

    counties = _counties()
    county_spans = []
    for m in _COUNTY.finditer(text):
        words = m.group(1).split()
        # "Annual Contract Smith County" -> try the longest suffix that is a county.
        for i in range(len(words)):
            base = " ".join(words[i:]).lower()
            for st in states:
                hit = counties.get(st, {}).get(base)
                if hit:
                    add(Location(hit[0], st, hit[1], hit[2], "county", m.group(0)))
                    county_spans.append((m.start(), m.end()))
                    break
            else:
                continue
            break

    shouting = _is_shouting(text)
    places = _places()
    tokens = [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(text)]
    for i in range(len(tokens)):
        for n in (4, 3, 2, 1):
            if i + n > len(tokens):
                continue
            chunk = tokens[i:i + n]
            words = [w.strip(".'&-") for w, _, _ in chunk]
            if not all(words):
                continue
            if not shouting and not all(w[0].isupper() for w in words if w.lower() not in ("de", "la", "of")):
                continue
            phrase = " ".join(words)
            key = phrase.lower()
            start, end = chunk[0][1], chunk[-1][2]
            if any(a <= start < b for a, b in county_spans):
                continue
            before, after = text[max(0, start - 20):start], text[end:end + 30].lstrip()
            in_context = bool(re.search(_PLACE_CONTEXT_BEFORE, before, re.I) or re.match(_PLACE_CONTEXT_AFTER, after, re.I))
            if key in AMBIGUOUS_PLACES and not in_context:
                continue
            if shouting and len(key) < 5 and not in_context:
                continue
            for st in states:
                hit = places.get(st, {}).get(key)
                if hit:
                    add(Location(hit[0], st, hit[1], hit[2], "place", text[start:end]))
                    break
            else:
                continue
            break
    return found
