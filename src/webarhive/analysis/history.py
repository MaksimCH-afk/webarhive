"""History layer (spec §4–5): age, last activity, volume, status timeline.

Pure computation over CDX rows — no fetching of pages.

Status buckets (spec §4):
    200    → live content       → goes to topic parsing
    3xx    → redirects          → never dropped, goes to redirect analyzer
    404    → "abandoned" marker → kept as drop/death signal
    5xx +  → technical garbage  → dropped
    other  → garbage            → dropped
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from webarhive.cdx.client import CdxRow


@dataclass(frozen=True, slots=True)
class StatusTimelineEntry:
    timestamp: str  # YYYYMMDDhhmmss
    bucket: str  # "200" | "3xx" | "404" | "5xx" | "other"


@dataclass
class HistorySummary:
    total_captures: int = 0
    by_bucket: dict[str, list[CdxRow]] = field(default_factory=dict)
    first_capture_at: datetime | None = None  # "age" per spec — first archive activity
    last_capture_at: datetime | None = None
    timeline: list[StatusTimelineEntry] = field(default_factory=list)

    @property
    def age_days(self) -> int | None:
        if not self.first_capture_at:
            return None
        return (datetime.utcnow() - self.first_capture_at).days

    @property
    def live_versions(self) -> list[CdxRow]:
        """Status-200 rows — feed into topic analysis."""
        return self.by_bucket.get("200", [])

    @property
    def redirect_rows(self) -> list[CdxRow]:
        return self.by_bucket.get("3xx", [])

    @property
    def not_found_rows(self) -> list[CdxRow]:
        return self.by_bucket.get("404", [])


def _parse_ts(ts: str) -> datetime | None:
    # CDX timestamps are YYYYMMDDhhmmss, sometimes shorter.
    formats = ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d", "%Y%m", "%Y")
    for fmt in formats:
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def bucketize(rows: Iterable[CdxRow]) -> dict[str, list[CdxRow]]:
    """Split rows into status buckets. Drops 5xx/other garbage (spec §4)."""
    buckets: dict[str, list[CdxRow]] = {"200": [], "3xx": [], "404": []}
    for row in rows:
        b = row.status_bucket
        if b in ("5xx", "other"):
            continue  # dropped
        buckets[b].append(row)
    return buckets


def summarize_history(rows: Iterable[CdxRow]) -> HistorySummary:
    """Compute layer-1 summary directly from CDX rows."""
    rows_list = list(rows)
    summary = HistorySummary(total_captures=len(rows_list))
    if not rows_list:
        return summary

    summary.by_bucket = bucketize(rows_list)

    # Age = first activity in archive across all kept rows
    # (we use all rows including 404/3xx — they are still archive activity).
    times: list[datetime] = []
    timeline: list[StatusTimelineEntry] = []
    for row in rows_list:
        parsed = _parse_ts(row.timestamp)
        if parsed is None:
            continue
        times.append(parsed)
        bucket = row.status_bucket
        if bucket in ("200", "3xx", "404"):
            timeline.append(StatusTimelineEntry(row.timestamp, bucket))

    if times:
        summary.first_capture_at = min(times)
        summary.last_capture_at = max(times)

    timeline.sort(key=lambda e: e.timestamp)
    summary.timeline = timeline
    return summary
