"""Node C -- neighbourhood shortlist (fan-in from every reachability node).

Intersects all parties' reachable neighbourhoods, drops anything failing the
hard budget, and attaches the fairness score. When nothing survives it does not
return an empty list: it works out the smallest relaxation that would have
helped and says so.
"""
from __future__ import annotations

import time

from ..config import get_settings
from ..state import (
    Budget,
    GraphFailure,
    Leg,
    MeetingState,
    Person,
    ShortlistEntry,
    uses_transit,
)


def fairness_raw(times: list[float], mode: str, k: float) -> float:
    """Section 7, generalised to any number of parties. Higher is better.

    With two people the spread is simply |t1 - t2|; with more it is the gap
    between the best-off and worst-off person, which is the quantity that
    actually feels unfair. The `-k * max` term is the ceiling that stops a
    uniformly awful trip outranking a slightly uneven but much shorter one.
    """
    if not times:
        return 0.0
    if mode == "absolute":
        return -sum(times)
    return -(max(times) - min(times)) - k * max(times)


def _relaxation_advice(
    rows: list[list[Leg]], budget: Budget
) -> tuple[str, dict]:
    """Given that nothing passed, find the closest near-miss and describe it."""
    reachable = [legs for legs in rows if all(leg.reachable for leg in legs)]
    if not reachable:
        return (
            "No neighbourhood between you has a route from every starting point.",
            {},
        )

    def worst_time(legs: list[Leg]) -> float:
        return max(leg.duration_min for leg in legs)

    def worst_transfers(legs: list[Leg]) -> int:
        transit = [leg.transfers for leg in legs if leg.mode == "transit"]
        return max(transit) if transit else 0

    best_time = min(reachable, key=worst_time)
    best_transfers = min(reachable, key=worst_transfers)
    needed_time = worst_time(best_time)
    needed_transfers = worst_transfers(best_transfers)

    parts = []
    if needed_time > budget.max_time_min:
        parts.append(
            f"raise the travel time limit to about {int(needed_time) + 1} min "
            f"(closest option: {best_time[0].neighbourhood})"
        )
    if needed_transfers > budget.max_transfers:
        parts.append(
            f"allow {needed_transfers} transfer{'s' if needed_transfers != 1 else ''} "
            f"(closest option: {best_transfers[0].neighbourhood})"
        )
    if not parts:
        parts.append("widen the search area or pick different starting points")
    return "Try to " + ", or ".join(parts) + ".", {
        "min_max_time_min": round(needed_time, 1),
        "min_max_transfers": needed_transfers,
        "reachable_all": len(reachable),
    }


async def shortlist_node(state: MeetingState, config: dict) -> dict:
    started = time.perf_counter()
    if state.get("failure"):
        return {}

    settings = get_settings()
    budget: Budget = state["budget"]
    mode = state.get("fairness_mode", "gap")
    people: list[Person] = state["people"]
    area = state["search_area"]
    by_name = {c.name: c.coords for c in area.candidates}

    transit_in_play = uses_transit(*(person.mode for person in people))
    reach = state.get("reachability", {})
    # index -> {neighbourhood -> Leg}
    indexed = {
        i: {leg.neighbourhood: leg for leg in reach.get(i, [])} for i in range(len(people))
    }

    rows: list[list[Leg]] = []
    entries: list[ShortlistEntry] = []
    for name, coords in by_name.items():
        legs = [indexed[i].get(name) for i in range(len(people))]
        if any(leg is None for leg in legs):
            continue
        legs = [leg for leg in legs if leg is not None]
        rows.append(legs)

        if not all(leg.reachable for leg in legs):
            continue
        if any(leg.duration_min > budget.max_time_min for leg in legs):
            continue
        # The transfer budget only applies to whoever is actually on transit.
        if any(
            leg.mode == "transit" and leg.transfers > budget.max_transfers for leg in legs
        ):
            continue

        times = [leg.duration_min for leg in legs]
        entries.append(
            ShortlistEntry(
                neighbourhood=name,
                coords=coords,
                legs=legs,
                gap_min=max(times) - min(times),
                max_min=max(times),
                total_min=sum(times),
                total_transfers=sum(leg.transfers for leg in legs),
                fairness_raw=fairness_raw(times, mode, settings.fairness_ceiling_k),
                transfers_meaningful=transit_in_play,
            )
        )

    if not entries:
        suggestion, detail = _relaxation_advice(rows, budget)
        return {
            "failure": GraphFailure(
                node="shortlist",
                reason=(
                    f"No neighbourhood works within {budget.max_time_min} min"
                    + (
                        f" and {budget.max_transfers} transfer"
                        f"{'s' if budget.max_transfers != 1 else ''}"
                        if transit_in_play
                        else ""
                    )
                    + (" for both of you." if len(people) == 2 else f" for all {len(people)} of you.")
                ),
                suggestion=suggestion,
                detail=detail,
            ),
            "shortlisted_neighbourhoods": [],
            "timings": {"shortlist": time.perf_counter() - started},
        }

    entries.sort(key=lambda e: e.fairness_raw, reverse=True)
    return {
        "shortlisted_neighbourhoods": entries,
        "timings": {"shortlist": time.perf_counter() - started},
    }
