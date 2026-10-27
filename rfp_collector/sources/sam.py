"""SAM.gov Contract Opportunities (federal) via the public Get Opportunities API v2.

Requires a free SAM.gov API key (SAM.gov > sign in > Account Details >
API Key), supplied as SAM_API_KEY or `[sam] api_key` in the config.

Non-federal keys get a small daily request quota, so the queries are kept
few (one per NAICS/PSC code, nationwide, open notices only) and every
response is cached on disk for the day; re-running the same day costs nothing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import geo
from ..http import get_json
from ..models import Opportunity

log = logging.getLogger(__name__)

ENDPOINT = "https://api.sam.gov/opportunities/v2/search"
PAGE = 1000


def _d(s) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt, n in (("%Y-%m-%d", 10), ("%m/%d/%Y", 10)):
        try:
            return datetime.strptime(s[:n], fmt).date()
        except ValueError:
            continue
    return None


def _name(v) -> str:
    if isinstance(v, dict):
        return v.get("name") or v.get("code") or ""
    return v or ""


def _code(v) -> str:
    if isinstance(v, dict):
        return v.get("code") or v.get("name") or ""
    return v or ""


def _cached(cache_dir: Path, params: dict):
    key = hashlib.sha1(json.dumps({k: v for k, v in params.items() if k != "api_key"}, sort_keys=True).encode()).hexdigest()[:16]
    path = cache_dir / f"sam_{date.today().isoformat()}_{key}.json"
    if path.exists():
        return json.loads(path.read_text())
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    data = get_json(url, timeout=90, retries=2)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def fetch(cfg: dict, cache_dir: Path) -> list[Opportunity]:
    sam = cfg.get("sam", {})
    key = os.environ.get("SAM_API_KEY") or sam.get("api_key")
    if not key:
        log.warning("SAM.gov skipped: no API key (set SAM_API_KEY or [sam] api_key)")
        return []
    today = date.today()
    base = {
        "api_key": key,
        "postedFrom": (today - timedelta(days=int(sam.get("posted_within_days", 90)))).strftime("%m/%d/%Y"),
        "postedTo": today.strftime("%m/%d/%Y"),
        "rdlfrom": today.strftime("%m/%d/%Y"),
        "rdlto": (today + timedelta(days=364)).strftime("%m/%d/%Y"),
        "limit": PAGE,
    }
    queries = [{"ncode": c} for c in sam.get("naics", ["561720", "561790"])]
    queries += [{"ccode": c} for c in sam.get("psc", [])]
    queries += [{"title": t} for t in sam.get("title_keywords", [])]

    seen: dict[str, Opportunity] = {}
    for q in queries:
        offset = 0
        while True:
            params = {**base, **q, "offset": offset}
            try:
                data = _cached(cache_dir, params)
            except Exception as e:  # quota errors come back as HTTP 429/403
                log.error("SAM.gov query %s failed: %s", q, e)
                break
            rows = data.get("opportunitiesData") or []
            for r in rows:
                opp = _to_opp(r)
                seen.setdefault(opp.source_id, opp)
            total = int(data.get("totalRecords") or 0)
            log.info("sam %s offset %d: %d rows (total %d)", q, offset, len(rows), total)
            offset += 1
            if not rows or offset * PAGE >= total or offset >= int(sam.get("max_pages_per_query", 3)):
                break
    return list(seen.values())


def _to_opp(r: dict) -> Opportunity:
    pop = r.get("placeOfPerformance") or {}
    office = r.get("officeAddress") or {}
    country = _code(pop.get("country"))
    domestic = country in ("", "USA", "US") and not ("-" in _code(pop.get("state")))
    city, state, zipc = _name(pop.get("city")), _code(pop.get("state")), pop.get("zip") or ""
    if city == "0":
        city = ""
    if not domestic:
        # Foreign postal codes (Madrid 28006) collide with U.S. ZIPs; never geocode them.
        zipc, state = "", ""
    codes = [c for c in (r.get("naicsCode"), r.get("classificationCode")) if c]
    opp = Opportunity(
        source="sam",
        source_id=r.get("noticeId") or r.get("solicitationNumber") or "",
        title=(r.get("title") or "").strip(),
        agency=r.get("fullParentPathName") or r.get("organizationName") or "",
        url=r.get("uiLink") or "",
        posted=_d(r.get("postedDate")),
        due=_d(r.get("responseDeadLine")),
        due_text=str(r.get("responseDeadLine") or ""),
        status=f"{r.get('type') or ''}{' (set-aside: ' + r['typeOfSetAsideDescription'] + ')' if r.get('typeOfSetAsideDescription') else ''}",
        codes=codes,
        # The API returns a URL for the description, not the text itself.
        description="",
        location_hint=", ".join(x for x in (city, state, zipc) if x),
    )
    cands = []
    loc = geo.lookup_zip(zipc)
    if loc:
        cands.append(geo.Location(", ".join(x for x in (city, zipc) if x) or loc.name, state, loc.lat, loc.lon, "zip", f"place of performance {zipc}"))
    elif city and state:
        loc = geo.lookup_place(city, state)
        if loc:
            cands.append(loc)
    if not cands and domestic:
        # Fall back to the contracting office; flagged by its evidence text.
        oz = office.get("zipcode") or office.get("zip")
        loc = geo.lookup_zip(oz)
        if loc:
            cands.append(geo.Location(office.get("city") or oz, office.get("state") or "", loc.lat, loc.lon, "zip", f"contracting office {oz} (no place of performance)"))
    opp.candidates = cands
    return opp
