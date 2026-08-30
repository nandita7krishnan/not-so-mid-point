"""Append-only record of every search, for building an eval set.

This is deliberately separate from `stats.py`. Stats answers "how many people
are using this" and keeps nothing that could identify anyone. This module keeps
the actual question and the actual answer, because that is the only thing an
eval can be run against.

What it does *not* keep is where anyone lives. A starting point is the one piece
of real personal data a search carries, and it is personal whether or not a name
is attached to it -- `Location.label` is Google's `formatted_address`, so for a
typed street address the label *is* the doorstep. Every record is therefore
coarsened on the way in, never on the way out:

  - the typed query string is dropped entirely
  - a start is named by the nearest public transit stop where the area is dense
    enough for that to describe a catchment rather than a house (see
    anchors.py), and the recorded coordinate becomes the stop's own published
    point rather than a rounded version of the real one
  - failing that, by the nearest seeded neighbourhood, so the vocabulary is
    a closed list of ~50 names that no address can be recovered from; outside
    that area the geocoded label falls back to its city tail, postcodes stripped
  - coordinates that were not snapped to a stop are rounded to
    `search_log_coord_precision` (2dp ~ 1.1 km)
  - participant names are replaced with their position, P1..P5

Snapping to a fixed list is what makes the area field safe by construction
rather than by careful string handling: the output can only ever be a name that
was already in the source, whatever Google returned. It also keeps the record
*useful*, which reducing everything to "Seattle, WA" would not -- judging
whether a spot was fair needs to know that one person started in Ballard and
another in Columbia City. Venue details are not scrubbed; those are public
businesses.

Off unless SEARCH_LOG_ENABLED is set, so a public deploy records nothing by
default and the choice to collect is explicit.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import anchors
from .config import get_settings
from .data.seattle import NEIGHBOURHOODS, in_seeded_area
from .geo import haversine_m

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Prefix on the stdout copy of a record. Distinctive on purpose: it is what
# separates a record from every other line in a host's log stream.
STDOUT_MARKER = "SEARCHLOG"

# Any token carrying a digit, once the street line is gone, is a postcode or a
# unit number rather than a place name -- "98107", and equally "SW1A 2AA", which
# a digits-only rule would have waved through even though a UK postcode narrows
# to about fifteen households. Cities do not have digits in their names, so
# dropping the lot is both simpler and safer than enumerating postcode formats.
_HAS_DIGIT = re.compile(r"\d")

_write_lock = threading.Lock()


def _nearest_seed(coords: Any) -> Optional[str]:
    """The closest seeded neighbourhood, or None outside the seeded area."""
    if not in_seeded_area(coords):
        return None
    return min(
        NEIGHBOURHOODS, key=lambda n: haversine_m(coords, n.coords)
    ).name


def _nearest_anchor(coords: Any) -> Any:
    """The nearest public anchor, if the file exists and the area is dense."""
    settings = get_settings()
    return anchors.load(settings.search_log_anchors).nearest(
        coords,
        radius_m=settings.search_log_anchor_radius_m,
        min_neighbours=settings.search_log_anchor_min_neighbours,
    )


def _area(location: Any) -> tuple[str, str]:
    """A coarse name for where someone started, and how it was derived.

    The source is recorded alongside the name because the three paths have
    genuinely different precision, and an eval reading the file should not have
    to guess which one it got.
    """
    anchor = _nearest_anchor(location.coords)
    if anchor is not None:
        return anchor.name, "stop"
    seed = _nearest_seed(location.coords)
    if seed is not None:
        return seed, "seed"
    return _coarse_area(location.label), "label"


def _place(location: Any, precision: int) -> tuple[str, str, dict[str, float]]:
    """Where someone started: a name, how it was derived, and a coordinate.

    A stop-snapped start reports the stop's own published coordinate rather
    than a rounded version of the real one. That is both sharper for an eval --
    the point is exact, so the case can be replayed -- and less revealing,
    because the number in the file is a bus stop somebody published, not an
    approximation of where a person actually was.
    """
    area, source = _area(location)
    if source == "stop":
        anchor = _nearest_anchor(location.coords)
        return area, source, {"lat": anchor.coords.lat, "lng": anchor.coords.lng}
    return area, source, _coords(location.coords, precision)


def _coarse_area(label: str) -> str:
    """Reduce a formatted address to something that names a city, not a home.

    "5200 Ballard Ave NW, Seattle, WA 98107, USA" -> "Seattle, WA, USA"

    The first component of a multi-part formatted address is the street line, so
    it is dropped whenever dropping it still leaves something. Anything shorter
    is already coarse enough to keep.
    """
    parts = [p.strip() for p in label.split(",") if p.strip()]
    if len(parts) >= 3:
        parts = parts[1:]
    cleaned = [
        " ".join(word for word in part.split() if not _HAS_DIGIT.search(word))
        for part in parts
    ]
    return ", ".join(p for p in cleaned if p)


def _round(value: float, places: int) -> float:
    return round(value, places)


def _coords(coords: Any, places: int) -> dict[str, float]:
    return {"lat": _round(coords.lat, places), "lng": _round(coords.lng, places)}


def _person(index: int, person: Any, precision: int) -> dict[str, Any]:
    area, source, coords = _place(person.location, precision)
    return {
        # Positional, so a renamed participant never reaches the file.
        "id": f"P{index + 1}",
        "area": area,
        "area_source": source,
        "coords": coords,
        "mode": person.mode,
    }


def _shortlist_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "neighbourhood": entry["neighbourhood"],
        "gap_min": entry["gap_min"],
        "max_min": entry["max_min"],
        "total_min": entry["total_min"],
        "total_transfers": entry["total_transfers"],
        "legs": [
            {
                "mode": leg["mode"],
                "reachable": leg["reachable"],
                "duration_min": leg["duration_min"],
                "transfers": leg["transfers"],
            }
            for leg in entry["legs"]
        ],
    }


def _result(rank: int, result: dict[str, Any]) -> dict[str, Any]:
    venue = result["venue"]
    entry = result["shortlist"]
    return {
        "rank": rank,
        "place_id": venue["place_id"],
        "name": venue["name"],
        # A business address is public information, so it stays whole.
        "address": venue["address"],
        "category": venue["category"],
        "primary_type": venue["primary_type"],
        "types": venue["types"],
        "neighbourhood": venue["neighbourhood"],
        "rating": venue["rating"],
        "rating_count": venue["rating_count"],
        "price_level": venue["price_level"],
        "open_now": venue["open_now"],
        "preference_score": venue["preference_score"],
        "preference_reason": venue["preference_reason"],
        "scores": result["scores"],
        "why": result["why"],
        "journey": _shortlist_entry(entry),
    }


def build_record(
    *,
    people: list[Any],
    request: Any,
    response: dict[str, Any],
    departure: int,
    visitor: str = "",
    precision: Optional[int] = None,
) -> dict[str, Any]:
    """Assemble one scrubbed record. Pure, so the scrubbing is directly testable."""
    if precision is None:
        precision = get_settings().search_log_coord_precision

    return {
        "schema": SCHEMA_VERSION,
        "id": uuid.uuid4().hex[:12],
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        # Already a salted, truncated hash. Kept so several searches from one
        # sitting can be grouped -- someone widening their budget and retrying
        # is one eval case, not three.
        "visitor": visitor,
        "request": {
            "people": [
                _person(i, person, precision) for i, person in enumerate(people)
            ],
            "party_size": len(people),
            "categories": request.categories,
            "free_text": request.free_text,
            "max_time_min": request.max_time_min,
            "max_transfers": request.max_transfers,
            "weights": request.weights.normalized().model_dump(),
            "fairness_mode": request.fairness_mode,
            "require_open": request.require_open,
            "departure_time": departure,
            # Rush hour vs. Sunday morning is a real eval dimension and neither
            # value says anything about who searched.
            "departure_hour_utc": datetime.fromtimestamp(departure, timezone.utc).hour,
            "departure_weekday": datetime.fromtimestamp(departure, timezone.utc).weekday(),
        },
        "response": {
            "ok": response["ok"],
            "transfers_apply": response["transfers_apply"],
            "preference_spec": response["preference_spec"],
            "search_area": _search_area(response.get("search_area"), precision),
            "shortlist": [_shortlist_entry(e) for e in response.get("shortlist", [])],
            "results": [
                _result(i + 1, r) for i, r in enumerate(response.get("results", []))
            ],
            "failure": response.get("failure"),
            "warnings": response.get("warnings", []),
            "timings": response.get("timings", {}),
        },
    }


def _search_area(area: Optional[dict[str, Any]], precision: int) -> Optional[dict[str, Any]]:
    if area is None:
        return None
    return {
        # The centroid sits between the participants and is derived from their
        # positions, so it gets the same rounding they do.
        "center": {
            "lat": _round(area["center"]["lat"], precision),
            "lng": _round(area["center"]["lng"], precision),
        },
        "radius_m": area["radius_m"],
        "candidates": [c["name"] for c in area.get("candidates", [])],
        "balance_note": area.get("balance_note", ""),
    }


def path_for(day: Optional[str] = None) -> Path:
    """One file per UTC day, so an eval batch is a natural unit."""
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Path(get_settings().search_log_dir) / f"searches-{day}.jsonl"


def record(
    *,
    people: list[Any],
    request: Any,
    response: dict[str, Any],
    departure: int,
    visitor: str = "",
) -> None:
    """Append one search. Never raises -- logging must not be able to fail a
    search that has already been paid for and answered."""
    settings = get_settings()
    if not settings.search_log_enabled:
        return
    try:
        entry = build_record(
            people=people,
            request=request,
            response=response,
            departure=departure,
            visitor=visitor,
        )
        line = json.dumps(entry, default=str, ensure_ascii=False)
        if settings.search_log_to_stdout:
            # One line, marker first, so a host's log viewer can be grepped for
            # it and evals/from_logs.py can read the result back. Emitted
            # before the file write, because on an ephemeral disk this is the
            # copy that survives.
            log.info("%s %s", STDOUT_MARKER, line)
        path = path_for()
        path.parent.mkdir(parents=True, exist_ok=True)
        # One `write` of one line in append mode. The app runs a single worker
        # by design (see README), and the lock covers the threadpool FastAPI
        # runs sync work on.
        with _write_lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 -- deliberately total
        log.warning("search log write failed: %s", exc)
