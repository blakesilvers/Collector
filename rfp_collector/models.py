from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

from .geo import Location


@dataclass
class Opportunity:
    source: str                 # esbd | sam | page:<name>
    source_id: str              # stable id within the source, used for de-duplication
    title: str
    agency: str = ""
    url: str = ""
    posted: date | None = None
    due: date | None = None
    due_text: str = ""          # raw due date/time as published
    status: str = ""
    codes: list[str] = field(default_factory=list)   # NIGP / NAICS / PSC strings
    description: str = ""
    location_hint: str = ""     # free text describing where the work is
    candidates: list[Location] = field(default_factory=list)

    # filled in by the pipeline
    location: Location | None = None
    distance_miles: float | None = None
    match_score: int = 0
    match_terms: list[str] = field(default_factory=list)
    category: str = ""          # window | pressure | both | related
    is_new: bool = False

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    def to_row(self) -> dict:
        d = asdict(self)
        d.pop("candidates")
        d["location"] = self.location.label() if self.location else ""
        d["location_evidence"] = self.location.evidence if self.location else ""
        d["distance_miles"] = round(self.distance_miles, 1) if self.distance_miles is not None else ""
        d["posted"] = self.posted.isoformat() if self.posted else ""
        d["due"] = self.due.isoformat() if self.due else ""
        d["codes"] = "; ".join(self.codes)
        d["match_terms"] = "; ".join(self.match_terms)
        d["description"] = (self.description or "")[:500]
        return d
