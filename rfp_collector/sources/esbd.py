"""Texas Electronic State Business Daily (txsmartbuy.gov/esbd).

Uses the same JSON service the public ESBD search page calls. Only the
read-only list search is used. ESBD's server-side keyword search is slow and
unreliable, so we page through every open solicitation (a few hundred,
24 per page) and filter locally. Statewide and local Texas buyers post here,
but many cities and school districts do not, so pair it with watched pages.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

from .. import geo
from ..http import get_json
from ..models import Opportunity

log = logging.getLogger(__name__)

SERVICE = "https://www.txsmartbuy.gov/app/extensions/CPA/CPAMain/1.0.0/services/ESBD.Service.ss?c=852252&n=2"
DETAIL_URL = "https://www.txsmartbuy.gov/esbd/{sid}"
STATUS_OPEN = "1"  # "Posted" and "Addendum Posted"


def _date(s: str) -> date | None:
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M %P"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = re.match(r"(\d{1,2}/\d{1,2}/\d{4})", s)
    return datetime.strptime(m.group(1), "%m/%d/%Y").date() if m else None


# NIGP classes searched in --history mode (all statuses, not just open).
HISTORY_NIGP = ("91081", "91003", "96221")


def fetch(cfg: dict, history: bool = False) -> list[Opportunity]:
    """Every open solicitation; with history=True, also past bids in HISTORY_NIGP."""
    out = _pages({"status": STATUS_OPEN})
    if history:
        for code in cfg.get("esbd", {}).get("history_nigp", HISTORY_NIGP):
            out.extend(_pages({"nigp": str(code)}))
    return out


def _pages(filters: dict, max_pages: int = 60) -> list[Opportunity]:
    query = {"page": 1, "urlRoot": "esbd", **filters}
    out: list[Opportunity] = []
    while query["page"] <= max_pages:
        d = get_json(SERVICE, data=query, timeout=90)
        lines = d.get("lines") or []
        for ln in lines:
            out.append(_to_opp(ln))
        total = int(d.get("totalRecordsFound") or 0)
        per = int(d.get("recordsPerPage") or 24)
        log.info("esbd %s page %d: %d records (total %d)", filters, query["page"], len(lines), total)
        if not lines or query["page"] * per >= total:
            break
        query["page"] += 1
    return out


def _to_opp(ln: dict) -> Opportunity:
    sid = ln.get("solicitationId") or ln.get("internalid") or ""
    codes = [c.strip() for c in re.split(r"[;\n]", ln.get("nigpCodes") or "") if c.strip()]
    agency = re.sub(r"\s+-\s+[A-Z]?\d+$", "", ln.get("agencyName") or "").strip()
    opp = Opportunity(
        source="esbd",
        source_id=str(sid),
        title=(ln.get("title") or "").strip(),
        agency=agency,
        url=DETAIL_URL.format(sid=sid),
        posted=_date(ln.get("postingDate")),
        due=_date(ln.get("responseDue")),
        due_text=" ".join(x for x in (ln.get("responseDue"), ln.get("responseTime")) if x),
        status=ln.get("statusName") or "",
        codes=codes,
        location_hint=agency,
    )
    if ln.get("url"):
        opp.description = f"Agency portal: {ln['url']}"
    # Every ESBD buyer is a Texas entity; the agency name usually names the
    # city/county/ISD, and state-agency titles often name the site.
    opp.candidates = geo.find_locations(agency, ["TX"]) + geo.find_locations(opp.title, ["TX"])
    return opp
