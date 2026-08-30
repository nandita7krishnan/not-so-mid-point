"""A closed list of public anchor points, and a gated nearest-anchor lookup.

The problem this solves: a neighbourhood centroid is a safe name for where
someone started, but a coarse one -- Ballard is 1.1 km of rounding and the
whole of a district in one word. Anchors buy precision back without ever
naming a home, because every anchor is public infrastructure from a published
transit feed. For anyone not driving it is also the more honest origin: a
transit journey starts at a stop, not at a front door.

Two properties make this safe by construction rather than by careful handling:

  - The vocabulary is closed. The output can only ever be a stop name that was
    already in the feed, whatever Google returned for the typed address.
  - The coordinate becomes the stop's own. A snapped record carries a published
    point, not a blurred version of a real one, so it is both more precise and
    less revealing than rounding the truth.

The density gate is what keeps the first property from being hollow. A stop
that is the only one for a mile serves a handful of houses, so naming it is
close to naming one of them. `AnchorSet.nearest` therefore answers only where
at least `min_neighbours` anchors sit within `radius_m` of the point -- dense
enough that the stop describes a catchment rather than a doorstep. Everywhere
else it returns None and the caller falls back to the coarser snap.

The anchor file is optional and not committed: build it with
`python -m evals.fetch_anchors`. With no file, every lookup returns None and
behaviour is exactly what it was before anchors existed.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .geo import haversine_m
from .state import LatLng

# Index cell size in degrees. ~1.1 km of latitude, ~0.75 km of longitude at
# Seattle's 47.6 degrees, which is the right order for a 500 m gate: a lookup
# touches a handful of cells rather than the whole feed.
_CELL_DEG = 0.01
_CELL_M = 700.0  # conservative metres per cell, so the ring never under-covers


@dataclass(frozen=True)
class Anchor:
    name: str
    coords: LatLng


class AnchorSet:
    """Anchors plus a coarse grid index over them."""

    def __init__(self, anchors: Iterable[Anchor]) -> None:
        self.anchors: list[Anchor] = list(anchors)
        self._grid: dict[tuple[int, int], list[Anchor]] = {}
        for anchor in self.anchors:
            self._grid.setdefault(_cell(anchor.coords), []).append(anchor)

    def __len__(self) -> int:
        return len(self.anchors)

    def _nearby(self, point: LatLng, radius_m: float) -> Iterator[Anchor]:
        """Every anchor in the cells a circle of `radius_m` could reach."""
        rings = int(radius_m / _CELL_M) + 1
        lat_cell, lng_cell = _cell(point)
        for dlat in range(-rings, rings + 1):
            for dlng in range(-rings, rings + 1):
                yield from self._grid.get((lat_cell + dlat, lng_cell + dlng), ())

    def nearest(
        self,
        point: LatLng,
        *,
        radius_m: float = 500.0,
        min_neighbours: int = 3,
        max_distance_m: float = 1200.0,
    ) -> Optional[Anchor]:
        """The closest anchor, or None where naming one would be too sharp.

        `min_neighbours` distinct anchors must sit within `radius_m` of the
        point for an answer to come back at all. A sparse area fails that test
        by construction -- if the only stop for a mile is 1.1 km away then
        nothing is within 500 m, the count is zero, and the caller gets None.

        Distinct by name, not by row. A street corner is usually two stops, one
        per direction, and a feed that spaces them further apart than the
        de-duplication threshold would otherwise let one corner satisfy two
        thirds of the gate on its own.
        """
        if not self.anchors:
            return None

        reach = max(radius_m, max_distance_m)
        candidates = [
            (haversine_m(point, anchor.coords), anchor)
            for anchor in self._nearby(point, reach)
        ]
        if not candidates:
            return None

        within = {anchor.name for distance, anchor in candidates if distance <= radius_m}
        if len(within) < min_neighbours:
            return None

        distance, anchor = min(candidates, key=lambda pair: pair[0])
        return anchor if distance <= max_distance_m else None


def _cell(point: LatLng) -> tuple[int, int]:
    return (
        int(math.floor(point.lat / _CELL_DEG)),
        int(math.floor(point.lng / _CELL_DEG)),
    )


EMPTY = AnchorSet(())

# Keyed by (path, mtime) so a rebuilt file is picked up without a restart, and
# an unchanged one is parsed once however many searches come in.
_cache: dict[tuple[str, float], AnchorSet] = {}


def load(path: str | Path) -> AnchorSet:
    """Read a `name,lat,lng` CSV. A missing or unreadable file is not an error.

    Anchors are an optional sharpening, so anything wrong with the file means
    the coarser fallback rather than a failed search.
    """
    path = Path(path)
    try:
        key = (str(path.resolve()), path.stat().st_mtime)
    except OSError:
        return EMPTY

    cached = _cache.get(key)
    if cached is not None:
        return cached

    try:
        with path.open(encoding="utf-8", newline="") as fh:
            anchors = list(_parse(csv.DictReader(fh)))
    except OSError:
        return EMPTY

    loaded = AnchorSet(anchors)
    _cache.clear()  # only ever one file in play; do not grow across rebuilds
    _cache[key] = loaded
    return loaded


def _parse(rows: Iterable[dict[str, str]]) -> Iterator[Anchor]:
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        try:
            coords = LatLng(lat=float(row["lat"]), lng=float(row["lng"]))
        except (KeyError, TypeError, ValueError):
            continue  # one malformed row should cost that row, not the file
        yield Anchor(name=name, coords=coords)
