"""Node C -- neighbourhood shortlist (fan-in from A and B).

Intersects the two reachability sets, drops anything failing the hard budget,
and attaches the fairness score. When nothing survives, it does not return an
empty list: it works out the smallest relaxation that would have helped and
says so, which is the defined failure mode the PRD asks for.
"""
from __future__ import annotations

import time
from typing import Optional

from ..config import get_settings
from ..state import (
    Budget,
    GraphFailure,
    Leg,
    MeetingState,
    ShortlistEntry,
    uses_transit,
)


def fairness_raw(t1: float, t2: float, mode: str, k: float) -> float:
    """Section 7. Higher is better (both branches are negated costs).

    The `-k * max(t1, t2)` term is the ceiling that stops a fair-but-awful
    40/40 split outranking a 15/20 one."""
    if mode == "absolute":
        return -(t1 + t2)
    return -abs(t1 - t2) - k * max(t1, t2)


def _relaxation_advice(
    pairs: list[tuple[Leg, Leg]], budget: Budget
) -> tuple[str, dict]:
    """Given that nothing passed, find the closest near-miss and describe it."""
    reachable = [(a, b) for a, b in pairs if a.reachable and b.reachable]
    if not reachable:
        return (
            "No neighbourhood between you has a transit route from both starting points.",
            {},
        )

    def worst_time(pair: tuple[Leg, Leg]) -> float:
        return max(pair[0].duration_min, pair[1].duration_min)

    def worst_transfers(pair: tuple[Leg, Leg]) -> int:
        # A driving or walking leg has no transfers to count against the budget.
        return max(leg.transfers for leg in pair if leg.mode == "transit") if any(
            leg.mode == "transit" for leg in pair
        ) else 0

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
        "reachable_both": len(reachable),
    }


async def shortlist_node(state: MeetingState, config: dict) -> dict:
    started = time.perf_counter()
    if state.get("failure"):
        return {}

    settings = get_settings()
    budget: Budget = state["budget"]
    mode = state.get("fairness_mode", "gap")
    area = state["search_area"]
    by_name = {c.name: c.coords for c in area.candidates}

    transit_in_play = uses_transit(
        state.get("person1_mode", "transit"), state.get("person2_mode", "transit")
    )

    p1_legs = {leg.neighbourhood: leg for leg in state.get("person1_reachability", [])}
    p2_legs = {leg.neighbourhood: leg for leg in state.get("person2_reachability", [])}

    pairs: list[tuple[Leg, Leg]] = []
    entries: list[ShortlistEntry] = []
    for name, coords in by_name.items():
        a, b = p1_legs.get(name), p2_legs.get(name)
        if a is None or b is None:
            continue
        pairs.append((a, b))
        if not (a.reachable and b.reachable):
            continue
        if a.duration_min > budget.max_time_min or b.duration_min > budget.max_time_min:
            continue
        # The transfer budget only applies to whichever person is on transit.
        if any(
            leg.mode == "transit" and leg.transfers > budget.max_transfers
            for leg in (a, b)
        ):
            continue
        entries.append(
            ShortlistEntry(
                neighbourhood=name,
                coords=coords,
                p1=a,
                p2=b,
                gap_min=abs(a.duration_min - b.duration_min),
                max_min=max(a.duration_min, b.duration_min),
                total_min=a.duration_min + b.duration_min,
                total_transfers=a.transfers + b.transfers,
                transfers_meaningful=transit_in_play,
                fairness_raw=fairness_raw(
                    a.duration_min, b.duration_min, mode, settings.fairness_ceiling_k
                ),
            )
        )

    if not entries:
        suggestion, detail = _relaxation_advice(pairs, budget)
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
                    + " for both of you."
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
