"""Daily report of newly bookmarked posts: Markdown (for reading or feeding to an assistant),
HTML (for a browser) and JSON (for programs)."""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from .models import Bookmark

CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;color:#1c1c1c;line-height:1.45}
h1{font-size:1.5rem}.meta{color:#666;font-size:.9rem;margin-bottom:1.5rem}
.card{border:1px solid #ddd;border-radius:10px;padding:.9rem 1.1rem;margin:.9rem 0}
.who{font-weight:600}.who span{font-weight:400;color:#666}.date{color:#888;font-size:.85rem;float:right}
.text{white-space:pre-wrap;margin:.5rem 0}.q{border-left:3px solid #ccc;padding-left:.7rem;color:#444;margin:.5rem 0;white-space:pre-wrap}
.links a{display:block;font-size:.9rem;word-break:break-all}.media{color:#666;font-size:.85rem}
.open{font-size:.85rem}.num{color:#888;font-size:.8rem}.empty{color:#666}
@media(prefers-color-scheme:dark){body{background:#111;color:#eee}.card{border-color:#333}.q{color:#bbb}.date,.num,.meta,.media{color:#999}}
"""


def _when(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%b %d, %Y %H:%M")
    except (ValueError, TypeError):
        return iso or ""


def _media_summary(b: Bookmark) -> str:
    if not b.media:
        return ""
    kinds = {}
    for m in b.media:
        kinds[m.get("type", "media")] = kinds.get(m.get("type", "media"), 0) + 1
    return ", ".join(f"{n} {k}{'s' if n > 1 else ''}" for k, n in kinds.items())


def to_markdown(bookmarks: list[Bookmark], meta: dict) -> str:
    out = [f"# X bookmarks — {meta.get('date', '')}", ""]
    out.append(f"{len(bookmarks)} new bookmark{'s' if len(bookmarks) != 1 else ''}"
               + (f" since {meta['since']}" if meta.get("since") else "")
               + f". {meta.get('total', '?')} stored in total.")
    if meta.get("note"):
        out += ["", f"_{meta['note']}_"]
    out.append("")
    if not bookmarks:
        out.append("Nothing new.")
    for i, b in enumerate(bookmarks, 1):
        out.append(f"## {i}. @{b.author_handle} ({b.author_name}) · {_when(b.created_at)}")
        out.append("")
        out.append("\n".join("> " + line for line in (b.text or "(no text)").splitlines()) or "> (no text)")
        if b.quoted:
            out.append("")
            out.append(f"Quoting @{b.quoted.get('author_handle', '')}: "
                       + " ".join((b.quoted.get("text") or "").split())[:400]
                       + (f" <{b.quoted.get('url')}>" if b.quoted.get("url") else ""))
        if b.links:
            out.append("")
            out.append("Links: " + " ".join(f"<{u}>" for u in b.links))
        ms = _media_summary(b)
        if ms:
            out.append(f"Media: {ms}")
        if b.metrics:
            shown = {k: v for k, v in b.metrics.items() if k in ("favorite_count", "views", "bookmark_count")}
            if shown:
                out.append("Stats: " + ", ".join(f"{k.replace('_count', '')} {v:,}" for k, v in shown.items()))
        out.append(f"Post: <{b.url}>")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _card(i: int, b: Bookmark) -> str:
    parts = [f'<div class="card"><span class="date">{html.escape(_when(b.created_at))}</span>'
             f'<div class="who"><span class="num">{i}.</span> @{html.escape(b.author_handle)} '
             f'<span>{html.escape(b.author_name)}</span></div>'
             f'<div class="text">{html.escape(b.text or "(no text)")}</div>']
    if b.quoted:
        parts.append(f'<div class="q">@{html.escape(b.quoted.get("author_handle", ""))}: '
                     f'{html.escape(b.quoted.get("text") or "")}</div>')
    if b.links:
        parts.append('<div class="links">' + "".join(
            f'<a href="{html.escape(u)}" target="_blank" rel="noopener">{html.escape(u)}</a>' for u in b.links) + "</div>")
    ms = _media_summary(b)
    if ms:
        parts.append(f'<div class="media">Media: {html.escape(ms)}</div>')
    parts.append(f'<div class="open"><a href="{html.escape(b.url)}" target="_blank" rel="noopener">Open on X</a></div></div>')
    return "".join(parts)


def to_html(bookmarks: list[Bookmark], meta: dict) -> str:
    body = "".join(_card(i, b) for i, b in enumerate(bookmarks, 1)) or '<p class="empty">Nothing new.</p>'
    since = f" since {html.escape(meta['since'])}" if meta.get("since") else ""
    note = f'<p class="meta">{html.escape(meta["note"])}</p>' if meta.get("note") else ""
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>X bookmarks {html.escape(str(meta.get("date", "")))}</title><style>{CSS}</style></head><body>'
            f'<h1>X bookmarks — {html.escape(str(meta.get("date", "")))}</h1>'
            f'<p class="meta">{len(bookmarks)} new{since}. {meta.get("total", "?")} stored in total.</p>{note}{body}</body></html>')


def write_all(out_dir: Path, bookmarks: list[Bookmark], meta: dict, stamp: str | None = None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y-%m-%d")
    paths = {"md": out_dir / f"bookmarks_{stamp}.md", "html": out_dir / f"bookmarks_{stamp}.html",
             "json": out_dir / f"bookmarks_{stamp}.json"}
    paths["md"].write_text(to_markdown(bookmarks, meta), encoding="utf-8")
    paths["html"].write_text(to_html(bookmarks, meta), encoding="utf-8")
    paths["json"].write_text(json.dumps({"meta": meta, "bookmarks": [b.to_row() for b in bookmarks]},
                                        indent=2, ensure_ascii=False), encoding="utf-8")
    return paths
