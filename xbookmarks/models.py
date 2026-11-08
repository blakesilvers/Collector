from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Bookmark:
    id: str
    url: str
    author_handle: str
    author_name: str
    text: str
    created_at: str                      # ISO 8601 (UTC) when the post was written
    lang: str = ""
    links: list[str] = field(default_factory=list)
    media: list[dict] = field(default_factory=list)   # {"type": "photo|video|animated_gif", "url": ...}
    quoted: dict | None = None           # {"author_handle", "author_name", "text", "url"}
    is_retweet: bool = False
    reply_to: str = ""                   # handle the post replies to, if any
    metrics: dict = field(default_factory=dict)
    position: int = 0                    # 0 = most recently bookmarked in this capture
    link_info: list[dict] = field(default_factory=list)   # enrich.LinkInfo.to_dict() per link
    raw: dict | None = None
    is_new: bool = False
    first_seen: str = ""

    def to_row(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        return d
