from __future__ import annotations

import csv
import html
import json
from datetime import date, datetime
from pathlib import Path

from .models import Opportunity

COLUMNS = ["is_new", "category", "title", "agency", "location", "distance_miles", "due", "due_text",
           "posted", "status", "source", "url", "match_terms", "codes", "location_evidence", "match_score",
           "description"]


def _sort_key(o: Opportunity):
    return (not o.is_new, o.due or date.max, o.distance_miles if o.distance_miles is not None else 9e9)


def write_all(out_dir: Path, nearby: list[Opportunity], unknown: list[Opportunity], meta: dict) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    nearby = sorted(nearby, key=_sort_key)
    unknown = sorted(unknown, key=_sort_key)
    paths = {
        "csv": out_dir / f"rfps_{stamp}.csv",
        "json": out_dir / f"rfps_{stamp}.json",
        "html": out_dir / f"rfps_{stamp}.html",
    }
    with paths["csv"].open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["bucket"] + COLUMNS, extrasaction="ignore")
        w.writeheader()
        for bucket, rows in (("within radius", nearby), ("location unknown", unknown)):
            for o in rows:
                w.writerow({"bucket": bucket, **o.to_row()})
    paths["json"].write_text(json.dumps({
        "meta": meta,
        "within_radius": [o.to_row() for o in nearby],
        "location_unknown": [o.to_row() for o in unknown],
    }, indent=2, default=str), encoding="utf-8")
    paths["html"].write_text(_html(nearby, unknown, meta), encoding="utf-8")
    return paths


def _rows(opps: list[Opportunity]) -> str:
    if not opps:
        return '<tr><td colspan="7" class="empty">None found.</td></tr>'
    out = []
    for o in opps:
        r = o.to_row()
        badge = '<span class="new">NEW</span> ' if o.is_new else ""
        title = f'<a href="{html.escape(o.url)}" target="_blank" rel="noopener">{html.escape(o.title)}</a>' if o.url else html.escape(o.title)
        dist = f"{r['distance_miles']} mi" if r["distance_miles"] != "" else "—"
        out.append(
            f"<tr><td>{badge}{title}<div class='sub'>{html.escape(r['match_terms'])}</div></td>"
            f"<td>{html.escape(o.agency)}</td>"
            f"<td>{html.escape(r['location'] or o.location_hint or '—')}<div class='sub'>{html.escape(r['location_evidence'])}</div></td>"
            f"<td class='num'>{dist}</td>"
            f"<td>{html.escape(o.due_text or r['due'] or '—')}</td>"
            f"<td><span class='cat {o.category}'>{html.escape(o.category)}</span></td>"
            f"<td>{html.escape(o.source)}</td></tr>")
    return "\n".join(out)


def _html(nearby, unknown, meta) -> str:
    errs = "".join(f"<li>{html.escape(e)}</li>" for e in meta.get("errors", []))
    portals = "".join(
        f'<li><a href="{html.escape(p["url"])}" target="_blank" rel="noopener">{html.escape(p["name"])}</a>'
        f'{" — " + html.escape(p.get("notes", "")) if p.get("notes") else ""}</li>' for p in meta.get("portals", []))
    head = "<tr><th>Solicitation</th><th>Agency</th><th>Location</th><th>Distance</th><th>Due</th><th>Type</th><th>Source</th></tr>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Window &amp; Pressure Washing Bids</title>
<style>
:root {{ --bg:#fff; --fg:#1d232a; --muted:#5d6874; --line:#dde3e9; --accent:#0b6bcb; --new:#c0392b; --chip:#eef3f8; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#15191e; --fg:#e6eaee; --muted:#9aa5b1; --line:#2c343c; --accent:#6cb4ff; --new:#ff7a6b; --chip:#222a33; }} }}
body {{ background:var(--bg); color:var(--fg); font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; margin:0; padding:16px; }}
h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:28px 0 8px; }}
.meta {{ color:var(--muted); margin-bottom:12px; }}
.wrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; min-width:760px; }}
th, td {{ text-align:left; vertical-align:top; padding:8px; border-bottom:1px solid var(--line); }}
th {{ font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
a {{ color:var(--accent); }} .sub {{ color:var(--muted); font-size:12px; }}
.num {{ white-space:nowrap; }} .empty {{ color:var(--muted); }}
.new {{ color:var(--new); font-weight:700; font-size:11px; }}
.cat {{ background:var(--chip); border-radius:10px; padding:1px 8px; font-size:12px; }}
</style></head><body>
<h1>Window cleaning &amp; pressure washing bids</h1>
<div class="meta">Within {meta['radius_miles']} miles of {html.escape(meta['office_name'])} · generated {html.escape(meta['generated'])} · sources: {html.escape(', '.join(meta['sources']))}</div>
<h2>Within {meta['radius_miles']} miles ({len(nearby)})</h2>
<div class="wrap"><table>{head}{_rows(nearby)}</table></div>
<h2>Location unknown: review by hand ({len(unknown)})</h2>
<p class="meta">Relevant by keyword, but no location could be found in the listing (often statewide or multi-site contracts).</p>
<div class="wrap"><table>{head}{_rows(unknown)}</table></div>
{"<h2>Portals to check by hand</h2><p class='meta'>These buyers post on login or JavaScript portals this tool cannot read. Register for their email alerts.</p><ul>" + portals + "</ul>" if portals else ""}
{"<h2>Fetch errors</h2><ul>" + errs + "</ul>" if errs else ""}
</body></html>"""
