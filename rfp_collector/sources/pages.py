"""Watched agency bid pages (cities, counties, ISDs, colleges).

Most local buyers around Tyler post bids on their own website or a hosted
portal, not on ESBD. Each `[[pages]]` entry in the config is fetched, and every
link, table row and list item on it becomes a candidate that the keyword
matcher then screens. The page's configured location is used for distance.

Types:
  html - any server-rendered page (CivicPlus Bids.aspx, plain city pages, ...)
  rss     - an RSS/Atom feed
  bonfire - a <sub>.bonfirehub.com portal (read through its public JSON feed)
CivicPlus Bids.aspx, BidNet Direct agency pages, IonWave SourcingEvents.aspx
and Jaggaer public-event pages are server-rendered and work as `html`.
Portals that only render with JavaScript or sit behind a bot challenge
(PlanetBids, Beacon, OpenGov) show nothing to this scanner; list them under
`[[portals]]` so the report reminds you to check them by hand.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote, urljoin

from .. import geo
from ..http import request
from ..models import Opportunity

log = logging.getLogger(__name__)

_BLOCKS = {"tr", "li", "a", "h2", "h3", "h4", "p", "dt", "dd"}
_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b|\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b|\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[list] = []   # [tag, text_parts, first_href]
        self.items: list[tuple[str, str | None]] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        if tag in _BLOCKS:
            href = dict(attrs).get("href") if tag == "a" else None
            self.stack.append([tag, [], href])
        if tag == "a":
            href = dict(attrs).get("href")
            for frame in self.stack:
                if frame[2] is None and href and not href.startswith(("#", "javascript:", "mailto:")):
                    frame[2] = href

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
        if tag in _BLOCKS:
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    frame = self.stack.pop(i)
                    text = re.sub(r"\s+", " ", " ".join(frame[1])).strip()
                    if 8 <= len(text) <= 600:
                        self.items.append((text, frame[2]))
                    break

    def handle_data(self, data):
        if self.skip:
            return
        for frame in self.stack:
            frame[1].append(data)


def _first_dates(text: str) -> list[date]:
    out = []
    for m in _DATE.finditer(text):
        try:
            g = m.groups()
            if g[0]:
                out.append(date(int(g[0]), int(g[1]), int(g[2])))
            elif g[3]:
                y = int(g[5]); y = y + 2000 if y < 100 else y
                out.append(date(y, int(g[3]), int(g[4])))
            else:
                out.append(date(int(g[8]), _MONTHS[g[6][:3].lower()], int(g[7])))
        except ValueError:
            continue
    return out


def _guess_due(text: str) -> date | None:
    """Due date from listing text; a listing that names only past years counts as
    due on Dec 31 of the latest one, so stale postings fall out with past-due bids."""
    due = _explicit_due(text)
    if due:
        return due
    years = [int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)]
    if years and max(years) < date.today().year:
        return date(max(years), 12, 31)
    return None


def _explicit_due(text: str) -> date | None:
    m = re.search(r"(?:due|clos\w*|deadline|opening|open)\W{0,20}(.{0,40})", text, re.I)
    if m:
        ds = _first_dates(m.group(1))
        if ds:
            return ds[0]
    ds = _first_dates(text)
    return max(ds) if ds else None


def _location(page: dict) -> geo.Location | None:
    spec = page.get("location", "")
    loc = geo.parse_location_spec(spec) if spec else None
    if spec and not loc:
        log.warning("page %s: could not resolve location %r", page.get("name"), spec)
    return loc


def _opp(page: dict, title: str, link: str | None, desc: str = "", posted=None) -> Opportunity:
    url = quote(urljoin(page["url"], link.strip()), safe=":/?#[]@!$&'()*+,;=%~") if link else page["url"]
    sid = hashlib.sha1((url + "|" + title).encode()).hexdigest()[:16]
    loc = _location(page)
    return Opportunity(
        source=f"page:{page['name']}",
        source_id=sid,
        title=title,
        agency=page["name"],
        url=url,
        posted=posted,
        due=_guess_due(title + " " + desc),
        description=desc,
        location_hint=page.get("location", ""),
        candidates=[geo.Location(loc.name, loc.state, loc.lat, loc.lon, "override", "configured page location")] if loc else [],
    )


def fetch_page(page: dict) -> list[Opportunity]:
    kind = page.get("type", "html")
    if kind == "bonfire":
        return _fetch_bonfire(page)
    raw = request(page["url"], timeout=float(page.get("timeout", 45)), retries=1)
    if kind == "rss":
        return _parse_rss(page, raw)
    p = _Collector()
    p.feed(raw.decode("utf-8", "replace"))
    # A link and the table row around it both become items; keep one per link,
    # titled by the shortest text and dated from the longest (the full row).
    by_href: dict[str, list[str]] = {}
    out, seen = [], set()
    for text, href in p.items:
        if href:
            by_href.setdefault(href, []).append(text)
        elif text not in seen:
            seen.add(text)
            out.append(_opp(page, text[:300], None, desc=text if len(text) > 300 else ""))
    for href, texts in by_href.items():
        title = min(texts, key=len)[:300]
        context = max(texts, key=len)
        o = _opp(page, title, href, desc=context if context != title else "")
        out.append(o)
    return out


def _fetch_bonfire(page: dict) -> list[Opportunity]:
    """Bonfire portals render with JavaScript but expose open projects as JSON."""
    root = re.match(r"https?://[^/]+", page["url"]).group(0)
    data = json.loads(request(root + "/PublicPortal/getOpenPublicOpportunitiesSectionData",
                              timeout=float(page.get("timeout", 45)), retries=1))
    projects = (data.get("payload") or {}).get("projects") or {}
    out = []
    for p in (projects.values() if isinstance(projects, dict) else projects):
        title = " ".join(x for x in (p.get("ReferenceID"), p.get("ProjectName")) if x).strip()
        if not title:
            continue
        o = _opp(page, title, f"{root}/opportunities/{p.get('ProjectID')}")
        o.source_id = f"bonfire-{p.get('ProjectID')}"
        close = p.get("DateClose") or ""
        o.due_text = f"{close} UTC" if close else ""
        try:
            o.due = date.fromisoformat(close[:10]) if close else None
        except ValueError:
            o.due = None
        out.append(o)
    return out


def _parse_rss(page: dict, raw: bytes) -> list[Opportunity]:
    root = ET.fromstring(raw)
    out = []
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        get = lambda name: next((c for c in item if c.tag.rsplit("}", 1)[-1] == name), None)
        title = (get("title").text or "").strip() if get("title") is not None else ""
        link_el = get("link")
        link = (link_el.text or link_el.get("href") or "").strip() if link_el is not None else ""
        desc_el = get("description")
        if desc_el is None:
            desc_el = get("summary")
        desc = re.sub(r"<[^>]+>", " ", desc_el.text or "") if desc_el is not None else ""
        desc = re.sub(r"\s+", " ", desc).strip()
        posted = None
        pd = get("pubDate")
        if pd is not None and pd.text:
            try:
                posted = parsedate_to_datetime(pd.text).date()
            except (TypeError, ValueError):
                pass
        if title:
            out.append(_opp(page, title, link, desc, posted))
    return out


def fetch(cfg: dict) -> tuple[list[Opportunity], list[str]]:
    out, errors = [], []
    for page in cfg.get("pages", []):
        if page.get("enabled", True) is False:
            continue
        try:
            items = fetch_page(page)
            log.info("page %s: %d candidate items", page["name"], len(items))
            out.extend(items)
        except Exception as e:
            msg = f"{page['name']}: {type(e).__name__}: {e}"
            log.warning("page fetch failed: %s", msg)
            errors.append(msg)
    return out, errors
