"""Build a single static HTML page so a reviewer can look at every program's brief and artwork."""

from __future__ import annotations

import html
import json
from pathlib import Path

from .models import Manifest

CSS = """
body{font-family:system-ui,sans-serif;margin:0;padding:24px;background:#f5f2ec;color:#222}
h1{font-weight:600}article{background:#fff;border-radius:12px;padding:20px;margin:0 0 28px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
h2{margin:0 0 4px}.meta{color:#666;font-size:14px}.flag{background:#fff1d6;border-left:4px solid #e0a52e;padding:8px 12px;margin:10px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-top:12px}
.grid figure{margin:0}.grid img{width:100%;border-radius:8px;display:block}.grid figcaption{font-size:12px;color:#555;margin-top:4px}
.chosen{outline:3px solid #2e7d32}blockquote{margin:8px 0;padding:8px 12px;border-left:3px solid #999}
.unverified{border-left-color:#c62828;background:#fdecea}code{background:#eee;padding:1px 4px;border-radius:4px}
details{margin-top:8px}
"""


def _rel(path: str | None, base: Path) -> str:
    if not path:
        return ""
    try:
        return Path(path).resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path


def build_review(output_dir: Path, manifests: list[Manifest]) -> Path:
    parts = [f"<!doctype html><meta charset=utf-8><title>Sermon visuals review</title><style>{CSS}</style>",
             "<h1>Sermon visuals review</h1>",
             "<p class=meta>Pick artwork with <code>python -m sermon_visuals choose &lt;stem&gt; &lt;art file&gt;</code>, then re-run <code>run --stages compose,video</code>.</p>"]
    for m in manifests:
        brief = {}
        if m.brief and Path(m.brief).exists():
            brief = json.loads(Path(m.brief).read_text(encoding="utf-8"))
        parts.append("<article>")
        parts.append(f"<h2>{html.escape(brief.get('title', m.stem))}</h2>")
        parts.append(f"<div class=meta>{html.escape(m.stem)} · done: {', '.join(m.done) or 'nothing'}"
                     + (f" · scripture: {html.escape(brief['primary_scripture'])}" if brief.get("primary_scripture") else "") + "</div>")
        if brief.get("one_line"):
            parts.append(f"<p>{html.escape(brief['one_line'])}</p>")
        for stage, err in m.errors.items():
            parts.append(f"<div class=flag><b>{html.escape(stage)} failed:</b> {html.escape(err)}</div>")
        for flag in brief.get("content_flags", []):
            parts.append(f"<div class=flag>{html.escape(flag)}</div>")
        for q in m.quote_checks:
            cls = "" if q.verified else " class=unverified"
            note = "" if q.verified else " <b>(not found verbatim in transcript, check before use)</b>"
            parts.append(f"<blockquote{cls}>{html.escape(q.text)}{note}</blockquote>")
        if m.art:
            parts.append("<div class=grid>")
            for a in m.art:
                cls = " class=chosen" if m.chosen_art == a.path else ""
                parts.append(f"<figure{cls}><img src='{html.escape(_rel(a.path, output_dir))}' loading=lazy>"
                             f"<figcaption>{html.escape(Path(a.path).name)} · {html.escape(a.model)}</figcaption></figure>")
            parts.append("</div>")
        if m.cards:
            parts.append("<div class=grid>")
            for name, p in m.cards.items():
                parts.append(f"<figure><img src='{html.escape(_rel(p, output_dir))}' loading=lazy><figcaption>{html.escape(name)}</figcaption></figure>")
            parts.append("</div>")
        if m.video:
            parts.append(f"<p><a href='{html.escape(_rel(m.video, output_dir))}'>video</a></p>")
        if brief:
            parts.append("<details><summary>Full brief</summary><pre>" + html.escape(json.dumps(brief, indent=2)) + "</pre></details>")
        parts.append("</article>")
    out = output_dir / "review.html"
    out.write_text("\n".join(parts), encoding="utf-8")
    return out
