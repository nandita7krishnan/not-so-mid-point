"""Node 0 -- transit-route-aware midpoint.

The straight-line midpoint of two addresses is frequently useless: it lands in a
lake, on a freeway, or in a pocket with no service. So instead of computing a
midpoint and hoping, this node samples real candidate places along the corridor
between the two people and asks Google what transit actually costs to each one,
then keeps the zone where the two travel costs balance.

Cost: two Distance Matrix requests (one per person), each covering every
candidate at once. Transfer counts aren't available from Distance Matrix, but
Node 0 doesn't need them -- Nodes A/B get those with Directions for the far
smaller set of candidates that survive here.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from ..config import get_settings
from ..data.seattle import NEIGHBOURHOODS, in_seeded_area
from ..geo import distance_to_segment_m, haversine_m, interpolate, offset
from ..runtime import deps_from_config
from ..state import (
    GraphFailure,
    LatLng,
    Location,
    MeetingState,
    Neighbourhood,
    SearchArea,
)

# How many candidates get the coarse Distance Matrix sweep. Larger is a better
# search but a wider matrix; this stays inside one request per person.
SWEEP_LIMIT = 16


def _corridor_candidates(p1: LatLng, p2: LatLng, half_width_km: float) -> list[Neighbourhood]:
    """Seeded neighbourhoods lying in the corridor between the two people.

    Two filters: perpendicular distance from the P1-P2 line (keeps it a
    corridor, not a disc) and an ellipse on the summed distance to both people
    (stops the corridor running off past either end)."""
    separation = haversine_m(p1, p2)
    max_perp = half_width_km * 1000
    ellipse_limit = max(separation * 1.5, 6000)
    inside = [
        n
        for n in NEIGHBOURHOODS
        if distance_to_segment_m(n.coords, p1, p2) <= max_perp
        and haversine_m(n.coords, p1) + haversine_m(n.coords, p2) <= ellipse_limit
    ]
    midpoint = interpolate(p1, p2, 0.5)
    inside.sort(key=lambda n: haversine_m(n.coords, midpoint))
    return inside[:SWEEP_LIMIT]


async def _generated_candidates(
    p1: LatLng, p2: LatLng, half_width_km: float, maps: Any
) -> list[Neighbourhood]:
    """Fallback for anywhere we have no seed list: sample a grid across the
    corridor and reverse-geocode each point for a human-readable name."""
    separation = haversine_m(p1, p2)
    perp_step = min(half_width_km * 1000, max(separation * 0.2, 800))
    # Unit vector perpendicular to P1->P2, in metres.
    dlat, dlng = p2.lat - p1.lat, p2.lng - p1.lng
    norm = (dlat**2 + dlng**2) ** 0.5 or 1.0
    perp_north, perp_east = -dlng / norm, dlat / norm

    points: list[LatLng] = []
    for frac in (0.3, 0.4, 0.5, 0.6, 0.7):
        base = interpolate(p1, p2, frac)
        for lateral in (-1, 0, 1):
            points.append(
                base
                if lateral == 0
                else offset(base, perp_north * lateral * perp_step, perp_east * lateral * perp_step)
            )
    names = await asyncio.gather(*(maps.reverse_geocode(pt) for pt in points))
    seen: set[str] = set()
    out: list[Neighbourhood] = []
    for point, name in zip(points, names):
        if name in seen:
            continue
        seen.add(name)
        out.append(Neighbourhood(name=name, coords=point))
    return out[:SWEEP_LIMIT]


def _balance_cost(t1: float, t2: float, mode: str, k: float) -> float:
    """Lower is better. Mirrors the Section 7 fairness definitions so Node 0
    searches for the same thing the final scorer will reward."""
    if mode == "absolute":
        return t1 + t2
    return abs(t1 - t2) + k * max(t1, t2)


async def search_area_node(state: MeetingState, config: dict) -> dict:
    started = time.perf_counter()
    deps = deps_from_config(config)
    settings = get_settings()

    p1: Location = state["person1_location"]
    p2: Location = state["person2_location"]
    departure = state["departure_time"]
    mode = state.get("fairness_mode", "gap")

    candidates = _corridor_candidates(
        p1.coords, p2.coords, settings.corridor_half_width_km
    ) if (in_seeded_area(p1.coords) or in_seeded_area(p2.coords)) else []
    used_seed = bool(candidates)
    if not candidates:
        candidates = await _generated_candidates(
            p1.coords, p2.coords, settings.corridor_half_width_km, deps.maps
        )

    if not candidates:
        return {
            "failure": GraphFailure(
                node="search_area",
                reason="No candidate meeting areas exist between those two locations.",
                suggestion="Check both addresses are correct and in the same metro area.",
            ),
            "timings": {"search_area": time.perf_counter() - started},
        }

    coords = [c.coords for c in candidates]
    mode1 = state.get("person1_mode", "transit")
    mode2 = state.get("person2_mode", "transit")
    t1_list, t2_list = await asyncio.gather(
        deps.maps.travel_durations(p1.coords, coords, departure, mode1),
        deps.maps.travel_durations(p2.coords, coords, departure, mode2),
    )

    scored: list[tuple[float, Neighbourhood, float, float]] = []
    for cand, t1, t2 in zip(candidates, t1_list, t2_list):
        if t1 is None or t2 is None:
            continue  # unreachable from one side in their mode: a dead zone
        scored.append((_balance_cost(t1, t2, mode, settings.fairness_ceiling_k), cand, t1, t2))

    if not scored:
        return {
            "failure": GraphFailure(
                node="search_area",
                reason="No area between you is reachable from both locations.",
                suggestion=(
                    "This usually means one address has no service in the chosen travel "
                    "mode, or the two are too far apart for a single trip. Try a "
                    "different travel mode or starting point."
                ),
                detail={"candidates_tried": len(candidates)},
            ),
            "timings": {"search_area": time.perf_counter() - started},
        }

    scored.sort(key=lambda row: row[0])
    keep = scored[: settings.max_candidate_neighbourhoods]
    best_cost, best, best_t1, best_t2 = keep[0]
    center = best.coords

    # Radius has to cover every candidate we're passing on to Nodes A/B, since
    # Node D searches for venues inside this area.
    spread = max((haversine_m(center, cand.coords) for _, cand, _, _ in keep), default=0.0)
    radius_m = int(max(1500, min(spread + 1200, 12000)))

    warnings: list[str] = []
    dropped = len(candidates) - len(scored)
    if dropped:
        warnings.append(
            f"{dropped} of {len(candidates)} candidate areas had no route from one "
            "of you and were dropped."
        )

    return {
        "search_area": SearchArea(
            center=center,
            radius_m=radius_m,
            candidates=[cand for _, cand, _, _ in keep],
            balance_note=(
                f"Balance point near {best.name}: roughly {best_t1:.0f} min for "
                f"{p1.label.split(',')[0]} and {best_t2:.0f} min for "
                f"{p2.label.split(',')[0]}."
            ),
        ),
        "warnings": warnings + ([] if used_seed else ["Using generated sample points (outside the seeded Seattle area)."]),
        "timings": {"search_area": time.perf_counter() - started},
    }
