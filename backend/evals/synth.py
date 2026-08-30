"""Synthetic eval cases, drawn from public anchors.

The eval set does not need the people who used the app; it needs journeys
shaped like theirs. So the two things the search log was trying to do at once
are split here:

  - Real logs supply the *distribution*: party size, mode mix, budgets, weight
    settings, how far apart people typically start. Coarsening to 1.1 km blurs
    none of that, so the log can stay as coarse as it likes.
  - This module supplies the *origins*, drawn from the anchor file -- published
    transit stops, exact coordinates, nobody's home.

That split buys three things the log alone cannot. Precision, because an anchor
is an exact published point. Volume, because sampling is free where real
traffic is not. And cases real traffic will not produce for months: five people
on transit from the edges of the county, one person 40 km out, everybody on the
same block. Those are where fairness maths tends to break.

Exact coordinates also make a case replayable. A logged real search cannot be
re-run when the scorer changes -- the rounding means a replay is a different
search -- but a synthetic case is the same search every time, which is what
makes the frozen fixtures in fixtures.py worth having.
"""
from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional, Sequence

from app.anchors import Anchor, AnchorSet
from app.geo import haversine_m, offset
from app.state import LatLng

MODES = ("driving", "transit", "bicycling", "walking")

# Distance bands in km between the first person and each subsequent one. A
# shape is nothing more than a choice of band plus a mode rule, which keeps
# "what is this case testing" legible in one line.
SHAPES: dict[str, dict[str, Any]] = {
    # The bread and butter: a few km apart, mixed modes, ordinary budget.
    "typical": {"band": (2.0, 12.0)},
    # Everyone already together. The fairness term has nothing to separate the
    # candidates with, so preference should dominate and usually does not.
    "same_block": {"band": (0.05, 0.4), "max_time_min": 30},
    # One person far enough out that any fair answer hurts somebody. Tests that
    # the failure message is honest rather than the ranking being clever.
    "one_far_out": {"band": (2.0, 6.0), "outlier_km": (25.0, 40.0)},
    # The case the tool exists for, and the one real traffic rarely produces.
    "all_transit_edges": {"band": (12.0, 25.0), "modes": ("transit",),
                          "max_time_min": 75, "max_transfers": 3},
    # A driver and a rider meeting in the middle land somewhere quite different
    # from two riders, so the mix is forced rather than sampled.
    "mixed_mode": {"band": (4.0, 15.0), "modes": ("driving", "transit")},
}

_CATEGORY_POOL = (
    ["cafe"], ["restaurant"], ["park"], ["bar"], ["cafe", "park"],
    ["restaurant", "bar"], ["library"],
)
_FREE_TEXT_POOL = (
    "somewhere quiet with outdoor seating",
    "a running trail",
    "good coffee and space to work",
    "cheap eats, nothing fancy",
    "a park where a dog is welcome",
)


@dataclass
class Distribution:
    """What a search usually looks like. Learned, or the defaults below."""

    party_sizes: dict[int, float] = field(default_factory=lambda: {2: 0.6, 3: 0.25, 4: 0.1, 5: 0.05})
    modes: dict[str, float] = field(default_factory=lambda: {"driving": 0.45, "transit": 0.35, "walking": 0.12, "bicycling": 0.08})
    max_time_min: Sequence[int] = (30, 45, 45, 60, 90)
    max_transfers: Sequence[int] = (1, 2, 2, 3)
    fairness_modes: dict[str, float] = field(default_factory=lambda: {"gap": 0.5, "total": 0.5})
    free_text_rate: float = 0.3
    weights: Sequence[tuple[float, float, float]] = (
        (0.4, 0.4, 0.2), (0.6, 0.3, 0.1), (0.2, 0.7, 0.1), (0.34, 0.33, 0.33),
    )
    require_open_rate: float = 0.2
    # Local hours, mapped straight onto the departure timestamp's UTC hour by
    # the runner. Rush hour and Sunday morning are different searches.
    departure_hours: Sequence[int] = (8, 9, 12, 17, 18, 19)
    departure_weekdays: Sequence[int] = (0, 2, 4, 5, 6)

    @classmethod
    def from_records(cls, records: Iterable[dict[str, Any]], *, minimum: int = 20) -> "Distribution":
        """Learn what real searches look like, falling back where thin.

        Below `minimum` records a learned frequency is noise -- three searches
        would make one person's habits the population -- so the defaults stand
        and only the fields with enough support are replaced.
        """
        requests = [r.get("request", {}) for r in records]
        requests = [r for r in requests if r]
        if len(requests) < minimum:
            return cls()

        sizes = Counter(r["party_size"] for r in requests if r.get("party_size"))
        modes = Counter(p["mode"] for r in requests for p in r.get("people", []) if p.get("mode"))
        fairness = Counter(r["fairness_mode"] for r in requests if r.get("fairness_mode"))
        times = [r["max_time_min"] for r in requests if r.get("max_time_min")]
        transfers = [r["max_transfers"] for r in requests if r.get("max_transfers") is not None]
        free_text = sum(1 for r in requests if (r.get("free_text") or "").strip())
        require_open = sum(1 for r in requests if r.get("require_open"))

        base = cls()
        return replace(
            base,
            party_sizes=_normalise(sizes) or base.party_sizes,
            modes=_normalise(modes) or base.modes,
            fairness_modes=_normalise(fairness) or base.fairness_modes,
            max_time_min=times or base.max_time_min,
            max_transfers=transfers or base.max_transfers,
            free_text_rate=free_text / len(requests),
            require_open_rate=require_open / len(requests),
        )


def _normalise(counter: Counter) -> dict[Any, float]:
    total = sum(counter.values())
    return {key: value / total for key, value in counter.items()} if total else {}


def _pick(rng: random.Random, weighted: dict[Any, float]) -> Any:
    keys = list(weighted)
    return rng.choices(keys, weights=[weighted[k] for k in keys], k=1)[0]


def _anchor_near(
    anchors: AnchorSet,
    origin: LatLng,
    km: float,
    rng: random.Random,
    *,
    tries: int = 40,
) -> Optional[Anchor]:
    """An anchor about `km` from `origin`, in a random direction.

    Projects the target point and takes the closest anchor to it, rather than
    scanning every anchor for one at the right radius: the feed is not uniform,
    so a bearing plus a nearest lookup lands on a stop that actually exists in
    that direction instead of always finding the same ring of them.
    """
    best: Optional[tuple[float, Anchor]] = None
    for _ in range(tries):
        bearing = rng.uniform(0, 2 * math.pi)
        target = offset(origin, math.cos(bearing) * km * 1000, math.sin(bearing) * km * 1000)
        anchor = anchors.nearest(target, min_neighbours=1, max_distance_m=km * 400 + 800)
        if anchor is None:
            continue
        error = abs(haversine_m(origin, anchor.coords) / 1000 - km)
        if best is None or error < best[0]:
            best = (error, anchor)
        if error < max(0.3, km * 0.15):
            break
    return best[1] if best else None


def sample(
    anchors: AnchorSet,
    *,
    rng: random.Random,
    dist: Optional[Distribution] = None,
    shape: str = "typical",
) -> dict[str, Any]:
    """One synthetic case: who starts where, in what mode, asking for what."""
    if not anchors.anchors:
        raise ValueError("no anchors; run `python -m evals.fetch_anchors` first")
    dist = dist or Distribution()
    rules = SHAPES[shape]

    size = int(_pick(rng, dist.party_sizes))
    first = rng.choice(anchors.anchors)
    chosen: list[Anchor] = [first]
    low, high = rules["band"]
    for index in range(1, size):
        outlier = rules.get("outlier_km") if index == size - 1 else None
        span = outlier or (low, high)
        anchor = _anchor_near(anchors, first.coords, rng.uniform(*span), rng)
        chosen.append(anchor or rng.choice(anchors.anchors))

    allowed = rules.get("modes")
    people = []
    for index, anchor in enumerate(chosen):
        if allowed:
            # Cycle rather than sample, so "mixed_mode" is reliably mixed
            # instead of drawing the same mode twice and testing nothing.
            mode = allowed[index % len(allowed)]
        else:
            mode = _pick(rng, dist.modes)
        people.append({
            "id": f"P{index + 1}",
            "area": anchor.name,
            "area_source": "stop",
            "coords": {"lat": anchor.coords.lat, "lng": anchor.coords.lng},
            "mode": mode,
        })

    use_free_text = rng.random() < dist.free_text_rate
    weights = rng.choice(list(dist.weights))
    return {
        # Drawn from the seeded rng, not uuid4: a case's id names its fixture
        # file, so the same seed has to reach the same frozen answers.
        "id": f"{rng.getrandbits(48):012x}",
        "shape": shape,
        "people": people,
        "request": {
            "categories": [] if use_free_text else list(rng.choice(_CATEGORY_POOL)),
            "free_text": rng.choice(_FREE_TEXT_POOL) if use_free_text else "",
            "max_time_min": int(rules.get("max_time_min") or rng.choice(list(dist.max_time_min))),
            "max_transfers": int(rules.get("max_transfers") or rng.choice(list(dist.max_transfers))),
            "weights": {"fairness": weights[0], "preference": weights[1], "transfers": weights[2]},
            "fairness_mode": _pick(rng, dist.fairness_modes),
            "require_open": rng.random() < dist.require_open_rate,
            "departure_hour": rng.choice(list(dist.departure_hours)),
            "departure_weekday": rng.choice(list(dist.departure_weekdays)),
        },
    }


def sample_many(
    anchors: AnchorSet,
    count: int,
    *,
    seed: int = 0,
    dist: Optional[Distribution] = None,
    shapes: Optional[Sequence[str]] = None,
) -> list[dict[str, Any]]:
    """`count` cases, cycling the shapes so every one is represented.

    Round-robin rather than random: a 10-case run that happens to draw no
    transit-from-the-edges case has not tested the thing most likely to be
    broken.
    """
    rng = random.Random(seed)
    shapes = list(shapes or SHAPES)
    return [
        sample(anchors, rng=rng, dist=dist, shape=shapes[i % len(shapes)])
        for i in range(count)
    ]
