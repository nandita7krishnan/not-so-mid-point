"""Lightweight usage counters.

Answers two questions without a third-party analytics service: how many people
are using this, and how many of them are hitting a wall.

Visitors are counted by a salted hash of their IP, never the address itself, and
only the hash's first bytes are kept. Nothing here can be turned back into a
person or a location.

State lives in process memory, so a restart resets it. Every event is also
written to the log, and the hosting platform retains those, so the log is the
durable record and this is the convenient one.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# Keep a fortnight; enough to see a trend, small enough to ignore memory.
RETAIN_DAYS = 14


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class DayStats:
    day: str
    visitors: set[str] = field(default_factory=set)
    searches_ok: int = 0
    searches_no_result: int = 0
    blocked_personal: int = 0
    blocked_global: int = 0
    autocompletes: int = 0
    upstream_errors: int = 0

    @property
    def searches_attempted(self) -> int:
        return (
            self.searches_ok
            + self.searches_no_result
            + self.blocked_personal
            + self.blocked_global
            + self.upstream_errors
        )

    def as_dict(self) -> dict[str, Any]:
        attempted = self.searches_attempted
        blocked = self.blocked_personal + self.blocked_global
        return {
            "day": self.day,
            "visitors": len(self.visitors),
            "searches_attempted": attempted,
            "searches_ok": self.searches_ok,
            "searches_no_result": self.searches_no_result,
            "blocked_total": blocked,
            "blocked_personal": self.blocked_personal,
            "blocked_daily_cap": self.blocked_global,
            "upstream_errors": self.upstream_errors,
            "autocompletes": self.autocompletes,
            # The headline number: what share of attempts hit a wall.
            "blocked_pct": round(blocked / attempted * 100, 1) if attempted else 0.0,
        }


class Stats:
    def __init__(self, salt: Optional[str] = None) -> None:
        # A per-process random salt means visitor hashes cannot be correlated
        # across restarts or with any other system. Set VISITOR_SALT to keep
        # counts stable across restarts, at the cost of that property.
        self._salt = salt or os.environ.get("VISITOR_SALT") or os.urandom(16).hex()
        self._days: dict[str, DayStats] = {}
        self._lock = threading.Lock()

    def visitor_id(self, client_key: str) -> str:
        digest = hashlib.sha256(f"{self._salt}:{client_key}".encode()).hexdigest()
        return digest[:12]

    def _day(self, day: Optional[str] = None) -> DayStats:
        day = day or _today()
        entry = self._days.get(day)
        if entry is None:
            entry = self._days[day] = DayStats(day=day)
            for stale in sorted(self._days)[:-RETAIN_DAYS]:
                del self._days[stale]
        return entry

    def record(self, event: str, client_key: str, day: Optional[str] = None) -> None:
        """Count one event. Unknown events are ignored rather than raising --
        metrics must never be able to break a request."""
        vid = self.visitor_id(client_key)
        with self._lock:
            entry = self._day(day)
            entry.visitors.add(vid)
            if hasattr(entry, event) and isinstance(getattr(entry, event), int):
                setattr(entry, event, getattr(entry, event) + 1)
            else:
                return
        log.info("usage event=%s visitor=%s day=%s", event, vid, entry.day)

    def snapshot(self, days: int = RETAIN_DAYS) -> dict[str, Any]:
        with self._lock:
            recent = [self._days[d].as_dict() for d in sorted(self._days)[-days:]]
        totals: dict[str, Any] = {}
        for row in recent:
            for k, v in row.items():
                if isinstance(v, int):
                    totals[k] = totals.get(k, 0) + v
        attempted = totals.get("searches_attempted", 0)
        blocked = totals.get("blocked_total", 0)
        totals["blocked_pct"] = round(blocked / attempted * 100, 1) if attempted else 0.0
        # Visitors cannot be summed across days without double counting anyone
        # who returned, so the total is dropped rather than reported wrongly.
        totals.pop("visitors", None)
        return {"days": recent, "totals": totals, "note": "visitors are per-day uniques"}


STATS = Stats()
