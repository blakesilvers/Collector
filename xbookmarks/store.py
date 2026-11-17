"""SQLite store of every bookmark seen, so each run can report only the new ones."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .models import Bookmark

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookmarks (
    id TEXT PRIMARY KEY,
    url TEXT, author_handle TEXT, author_name TEXT, text TEXT, created_at TEXT, lang TEXT,
    links TEXT, media TEXT, quoted TEXT, is_retweet INTEGER, reply_to TEXT, metrics TEXT,
    raw TEXT, position INTEGER, first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS links (
    url TEXT PRIMARY KEY,
    info TEXT,
    fetched_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT, finished TEXT, mode TEXT, fetched INTEGER, new INTEGER, error TEXT
);
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # -- bookmarks -----------------------------------------------------------
    def known_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT id FROM bookmarks")}

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0]

    def upsert(self, bookmarks: list[Bookmark]) -> list[Bookmark]:
        """Insert unseen bookmarks, refresh seen ones. Returns the new ones, marked is_new."""
        now = _now()
        new: list[Bookmark] = []
        for b in bookmarks:
            row = self.db.execute("SELECT first_seen FROM bookmarks WHERE id = ?", (b.id,)).fetchone()
            if row is None:
                b.is_new, b.first_seen = True, now
                self.db.execute(
                    "INSERT INTO bookmarks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (b.id, b.url, b.author_handle, b.author_name, b.text, b.created_at, b.lang,
                     json.dumps(b.links), json.dumps(b.media), json.dumps(b.quoted), int(b.is_retweet),
                     b.reply_to, json.dumps(b.metrics), json.dumps(b.raw) if b.raw else None,
                     b.position, now, now))
                new.append(b)
            else:
                b.is_new, b.first_seen = False, row["first_seen"]
                self.db.execute("UPDATE bookmarks SET metrics = ?, position = ?, last_seen = ? WHERE id = ?",
                                (json.dumps(b.metrics), b.position, now, b.id))
        self.db.commit()
        return new

    def _rows_to_bookmarks(self, rows) -> list[Bookmark]:
        out = []
        for r in rows:
            out.append(Bookmark(
                id=r["id"], url=r["url"], author_handle=r["author_handle"], author_name=r["author_name"],
                text=r["text"], created_at=r["created_at"], lang=r["lang"] or "",
                links=json.loads(r["links"] or "[]"), media=json.loads(r["media"] or "[]"),
                quoted=json.loads(r["quoted"]) if r["quoted"] and r["quoted"] != "null" else None,
                is_retweet=bool(r["is_retweet"]), reply_to=r["reply_to"] or "",
                metrics=json.loads(r["metrics"] or "{}"), position=r["position"] or 0,
                first_seen=r["first_seen"] or ""))
        return out

    def since(self, when: datetime) -> list[Bookmark]:
        """Bookmarks first seen at or after `when` (newest capture position first)."""
        rows = self.db.execute("SELECT * FROM bookmarks WHERE first_seen >= ? ORDER BY first_seen DESC, position ASC",
                               (when.astimezone().isoformat(timespec="seconds"),)).fetchall()
        return self._rows_to_bookmarks(rows)

    def last_days(self, days: int) -> list[Bookmark]:
        return self.since(datetime.now().astimezone() - timedelta(days=days))

    def all(self) -> list[Bookmark]:
        return self._rows_to_bookmarks(self.db.execute("SELECT * FROM bookmarks ORDER BY first_seen DESC, position ASC"))

    def search(self, term: str) -> list[Bookmark]:
        like = f"%{term}%"
        return self._rows_to_bookmarks(self.db.execute(
            "SELECT * FROM bookmarks WHERE text LIKE ? OR author_handle LIKE ? OR author_name LIKE ? OR links LIKE ? "
            "ORDER BY first_seen DESC", (like, like, like, like)))

    # -- links ---------------------------------------------------------------
    def link_urls(self, bookmarks: list[Bookmark]) -> list[str]:
        """Distinct outbound URLs across these bookmarks (own links plus quoted-post links are
        not available, so only `links`), in order of appearance."""
        seen, out = set(), []
        for b in bookmarks:
            for u in b.links:
                if u not in seen:
                    seen.add(u)
                    out.append(u)
        return out

    def links_missing(self, urls: list[str]) -> list[str]:
        return [u for u in urls if self.db.execute("SELECT 1 FROM links WHERE url = ? AND error = ''", (u,)).fetchone() is None]

    def save_link(self, info: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO links (url, info, fetched_at, error) VALUES (?,?,?,?)",
                        (info["url"], json.dumps(info, ensure_ascii=False), info.get("fetched_at", _now()), info.get("error", "")))
        self.db.commit()

    def link_info(self, url: str) -> dict | None:
        row = self.db.execute("SELECT info FROM links WHERE url = ?", (url,)).fetchone()
        return json.loads(row[0]) if row else None

    def attach_links(self, bookmarks: list[Bookmark]) -> None:
        for b in bookmarks:
            b.link_info = [i for i in (self.link_info(u) for u in b.links) if i]

    # -- runs ----------------------------------------------------------------
    def start_run(self, mode: str) -> int:
        cur = self.db.execute("INSERT INTO runs (started, mode) VALUES (?, ?)", (_now(), mode))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, fetched: int, new: int, error: str = "") -> None:
        self.db.execute("UPDATE runs SET finished = ?, fetched = ?, new = ?, error = ? WHERE id = ?",
                        (_now(), fetched, new, error, run_id))
        self.db.commit()

    def last_successful_run(self) -> datetime | None:
        row = self.db.execute("SELECT finished FROM runs WHERE error = '' AND finished IS NOT NULL "
                              "ORDER BY id DESC LIMIT 1").fetchone()
        return datetime.fromisoformat(row[0]) if row else None
