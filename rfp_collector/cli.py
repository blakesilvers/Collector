"""Collect window-cleaning and pressure-washing bids near an office.

    python -m rfp_collector                 # uses ./config.toml
    python -m rfp_collector --radius 150 --sources esbd,pages
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import tomllib
from datetime import date, datetime
from pathlib import Path

from . import geo
from .matching import score
from .models import Opportunity
from .report import write_all
from .sources import esbd, pages, sam
from .store import Store

log = logging.getLogger("rfp_collector")
ALL_SOURCES = ("esbd", "sam", "pages")


def load_config(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def apply_overrides(opps: list[Opportunity], overrides: dict[str, str]) -> None:
    """[agency_locations] "Stephen F. Austin" = "Nacogdoches, TX" beats text matching."""
    resolved = {}
    for needle, spec in overrides.items():
        loc = geo.parse_location_spec(spec)
        if loc is None:
            log.warning("agency_locations: cannot resolve %r for %r", spec, needle)
            continue
        resolved[needle.lower()] = geo.Location(loc.name, loc.state, loc.lat, loc.lon, "override", f"agency_locations[{needle!r}]")
    for o in opps:
        hay = f"{o.agency} {o.title}".lower()
        for needle, loc in resolved.items():
            if needle in hay:
                o.candidates = [loc]
                break


def choose_location(o: Opportunity, office: geo.Location) -> None:
    best = None
    for c in o.candidates:
        d = geo.distance_miles(office.lat, office.lon, c.lat, c.lon)
        if best is None or d < best[0]:
            best = (d, c)
    if best:
        o.distance_miles, o.location = best


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def run(args) -> int:
    cfg = load_config(Path(args.config))
    off = cfg["office"]
    office = geo.Location(off.get("name", "office"), off.get("state", ""), float(off["lat"]), float(off["lon"]), "office")
    radius = float(args.radius or cfg.get("radius_miles", 120))
    match_cfg = cfg.get("matching", {})
    include_related = args.include_related or match_cfg.get("include_related", False)
    sources = [s.strip() for s in (args.sources or ",".join(cfg.get("sources", ALL_SOURCES))).split(",") if s.strip()]
    base = Path(args.config).resolve().parent
    out_dir = Path(args.out) if args.out else base / cfg.get("output_dir", "reports")
    cache_dir = base / cfg.get("cache_dir", ".cache")

    opps: list[Opportunity] = []
    errors: list[str] = []
    for s in sources:
        try:
            if s == "esbd":
                got = esbd.fetch(cfg, history=args.history)
            elif s == "sam":
                got = sam.fetch(cfg, cache_dir)
            elif s == "pages":
                got, errs = pages.fetch(cfg)
                errors.extend(errs)
            else:
                log.error("unknown source %r (choose from %s)", s, ", ".join(ALL_SOURCES))
                continue
            log.info("%s: %d raw records", s, len(got))
            opps.extend(got)
        except Exception as e:
            log.exception("source %s failed", s)
            errors.append(f"{s}: {type(e).__name__}: {e}")

    apply_overrides(opps, cfg.get("agency_locations", {}))
    today = date.today()
    nearby, unknown, far = [], [], 0
    seen_titles = set()
    for o in opps:
        score(o, match_cfg.get("extra_keywords"), match_cfg.get("exclude"))
        if o.match_score == 0 or (o.category == "related" and not include_related):
            continue
        if o.match_score < int(match_cfg.get("min_score", 2)) and o.category != "related":
            continue
        if o.due and o.due < today and not (args.include_past_due or args.history):
            continue
        dedupe = (_norm(o.title), _norm(o.agency))
        if dedupe in seen_titles:
            continue
        seen_titles.add(dedupe)
        choose_location(o, office)
        if o.location is None:
            unknown.append(o)
        elif o.distance_miles <= radius:
            nearby.append(o)
        else:
            far += 1

    store = Store(base / cfg.get("database", "data/rfps.sqlite"))
    store.mark(nearby + unknown)
    meta = {
        "office_name": office.name,
        "office": [office.lat, office.lon],
        "radius_miles": int(radius) if radius.is_integer() else radius,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sources": sources,
        "raw_records": len(opps),
        "outside_radius": far,
        "errors": errors,
        "portals": cfg.get("portals", []),
    }
    paths = write_all(out_dir, nearby, [] if args.no_unknown else unknown, meta)

    print(f"\nScanned {len(opps)} records from {', '.join(sources)}.")
    print(f"  {len(nearby)} within {meta['radius_miles']} mi of {office.name} "
          f"({sum(o.is_new for o in nearby)} new); {len(unknown)} relevant with unknown location; "
          f"{far} relevant but farther away.")
    for o in sorted(nearby, key=lambda x: (x.due or date.max)):
        flag = "NEW " if o.is_new else "    "
        print(f"  {flag}{(o.due.isoformat() if o.due else 'no due date'):<11} {o.distance_miles:5.0f} mi  "
              f"{o.title[:70]}  [{o.agency[:40]}]")
    if errors:
        print("\nErrors:")
        for e in errors:
            print("  -", e)
    print("\nReports:")
    for p in paths.values():
        print("  ", p)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rfp_collector", description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--radius", type=float, help="miles from the office (default from config)")
    ap.add_argument("--sources", help=f"comma list from: {', '.join(ALL_SOURCES)}")
    ap.add_argument("--out", help="report directory (default from config)")
    ap.add_argument("--include-related", action="store_true", help="also report janitorial/custodial bids")
    ap.add_argument("--include-past-due", action="store_true")
    ap.add_argument("--history", action="store_true",
                    help="also pull closed/awarded ESBD bids in the window/pressure NIGP classes (shows who buys this work)")
    ap.add_argument("--no-unknown", action="store_true", help="omit bids whose location cannot be determined")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
