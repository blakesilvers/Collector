"""Remembers which solicitations have been reported so each run flags new ones."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .models import Opportunity

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    key TEXT PRIMARY KEY,
    title TEXT,
    agency TEXT,
    url TEXT,
    due TEXT,
    first_seen TEXT,
    last_seen TEXT
)
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(SCHEMA)

    def mark(self, opps: list[Opportunity]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        for o in opps:
            row = self.db.execute("SELECT 1 FROM seen WHERE key = ?", (o.key,)).fetchone()
            o.is_new = row is None
            if o.is_new:
                self.db.execute(
                    "INSERT INTO seen (key, title, agency, url, due, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
                    (o.key, o.title, o.agency, o.url, o.due.isoformat() if o.due else "", now, now))
            else:
                self.db.execute("UPDATE seen SET last_seen = ?, due = ? WHERE key = ?",
                                (now, o.due.isoformat() if o.due else "", o.key))
        self.db.commit()
