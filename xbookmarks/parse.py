"""Turn the JSON that x.com/i/bookmarks loads into Bookmark records.

The page calls a GraphQL operation named ``Bookmarks``. Its reply is a timeline:
``data.bookmark_timeline_v2.timeline.instructions[].entries[]``. Each tweet entry
wraps a ``tweet_results.result`` object. Field names below follow what X serves as
of late 2026, with fallbacks for the older layout, and unknown shapes are skipped
rather than crashing a run.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .models import Bookmark

TWEET_DATE = "%a %b %d %H:%M:%S %z %Y"


def is_bookmarks_request(url: str) -> bool:
    return re.search(r"/i/api/graphql/[^/]+/Bookmarks(\?|$)", url) is not None


def parse_timeline(payload: dict) -> tuple[list[Bookmark], str | None]:
    """Return (bookmarks in page order, bottom cursor or None)."""
    timeline = (payload.get("data") or {}).get("bookmark_timeline_v2") or {}
    timeline = timeline.get("timeline") or {}
    bookmarks: list[Bookmark] = []
    cursor = None
    for instr in timeline.get("instructions") or []:
        entries = instr.get("entries") or []
        if instr.get("type") == "TimelineAddToModule":
            entries = [{"content": {"itemContent": it.get("item", {}).get("itemContent")}}
                       for it in instr.get("moduleItems") or []]
        for entry in entries:
            content = entry.get("content") or {}
            etype = content.get("entryType") or content.get("__typename")
            if etype == "TimelineTimelineCursor" or str(entry.get("entryId", "")).startswith("cursor-"):
                if content.get("cursorType") == "Bottom" or "bottom" in str(entry.get("entryId", "")):
                    cursor = content.get("value")
                continue
            items = []
            if content.get("itemContent"):
                items.append(content["itemContent"])
            for it in content.get("items") or []:
                ic = (it.get("item") or {}).get("itemContent")
                if ic:
                    items.append(ic)
            for ic in items:
                result = (ic.get("tweet_results") or {}).get("result")
                bm = parse_tweet(result)
                if bm:
                    bookmarks.append(bm)
    for i, bm in enumerate(bookmarks):
        bm.position = i
    return bookmarks, cursor


def _unwrap(result: dict | None) -> dict | None:
    if not result:
        return None
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet")
    if not result or result.get("__typename") in ("TweetTombstone", "TweetUnavailable"):
        return None
    return result


def _user(result: dict) -> tuple[str, str]:
    user = ((result.get("core") or {}).get("user_results") or {}).get("result") or {}
    core = user.get("core") or {}
    legacy = user.get("legacy") or {}
    handle = core.get("screen_name") or legacy.get("screen_name") or ""
    name = core.get("name") or legacy.get("name") or ""
    return handle, name


def _iso(created: str) -> str:
    try:
        return datetime.strptime(created, TWEET_DATE).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return created or ""


def _expand(text: str, url_entities: list[dict]) -> str:
    for u in url_entities or []:
        if u.get("url") and u.get("expanded_url"):
            text = text.replace(u["url"], u["expanded_url"])
    return text


def _text_and_links(result: dict) -> tuple[str, list[str]]:
    legacy = result.get("legacy") or {}
    note = ((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    if note.get("text"):
        text = note["text"]
        urls = (note.get("entity_set") or {}).get("urls") or []
    else:
        text = legacy.get("full_text") or ""
        urls = (legacy.get("entities") or {}).get("urls") or []
    text = _expand(text, urls)
    # Media t.co links sit at the end of the text; drop them, the media list carries them.
    for m in (legacy.get("entities") or {}).get("media") or []:
        if m.get("url"):
            text = text.replace(m["url"], "")
    article = ((result.get("article") or {}).get("article_results") or {}).get("result") or {}
    if article.get("title"):
        text = f"[Article] {article['title']}\n{text}".strip()
    links = [u["expanded_url"] for u in urls if u.get("expanded_url")]
    return text.strip(), links


def _media(result: dict) -> list[dict]:
    legacy = result.get("legacy") or {}
    out = []
    for m in (legacy.get("extended_entities") or legacy.get("entities") or {}).get("media") or []:
        item = {"type": m.get("type", ""), "url": m.get("media_url_https") or m.get("expanded_url", "")}
        variants = (m.get("video_info") or {}).get("variants") or []
        mp4 = [v for v in variants if v.get("content_type") == "video/mp4"]
        if mp4:
            item["video_url"] = max(mp4, key=lambda v: v.get("bitrate", 0)).get("url", "")
        out.append(item)
    return out


def parse_tweet(result: dict | None, *, keep_raw: bool = True) -> Bookmark | None:
    result = _unwrap(result)
    if not result:
        return None
    legacy = result.get("legacy") or {}
    tweet_id = result.get("rest_id") or legacy.get("id_str")
    if not tweet_id:
        return None
    handle, name = _user(result)
    text, links = _text_and_links(result)
    is_retweet = False
    rt = _unwrap((legacy.get("retweeted_status_result") or {}).get("result"))
    if rt:
        is_retweet = True
        rt_handle, _ = _user(rt)
        rt_text, rt_links = _text_and_links(rt)
        text = f"RT @{rt_handle}: {rt_text}"
        links = links or rt_links
    quoted = None
    q = _unwrap((result.get("quoted_status_result") or {}).get("result"))
    if q:
        q_handle, q_name = _user(q)
        q_text, _ = _text_and_links(q)
        quoted = {"author_handle": q_handle, "author_name": q_name, "text": q_text,
                  "url": f"https://x.com/{q_handle}/status/{q.get('rest_id', '')}"}
    views = (result.get("views") or {}).get("count")
    metrics = {k: legacy.get(k) for k in ("favorite_count", "retweet_count", "reply_count",
                                           "quote_count", "bookmark_count") if legacy.get(k) is not None}
    if views is not None:
        try:
            metrics["views"] = int(views)
        except (TypeError, ValueError):
            pass
    return Bookmark(
        id=str(tweet_id),
        url=f"https://x.com/{handle or 'i'}/status/{tweet_id}",
        author_handle=handle, author_name=name, text=text,
        created_at=_iso(legacy.get("created_at", "")),
        lang=legacy.get("lang", ""), links=links, media=_media(rt or result), quoted=quoted,
        is_retweet=is_retweet, reply_to=legacy.get("in_reply_to_screen_name") or "",
        metrics=metrics, raw=result if keep_raw else None,
    )
