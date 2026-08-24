"""Node E -- scorer.

Combines fairness (Node C), preference match (Node D) and transfer count
(Nodes A/B) into one weighted score per venue.

Each component is mapped onto 0-1 with an **absolute** scale rather than min-max
normalisation over the candidate set. Min-max has no sense of scale: it stretches
whatever spread happens to exist across the full range, so when every candidate
sits within a few minutes of every other, trivial differences get amplified into
the dominant signal. In one live run that turned a four-minute driving difference
into a 0.79 fairness gap, which buried the best-matching venue at rank 7 behind
six near-identical parks.

With absolute scales a weight of 0.4 means the same thing regardless of how
tightly the candidates happen to be clustered, and a component that doesn't vary
simply adds a constant to every venue instead of distorting the ranking.
"""
from __future__ import annotations

import time

from ..config import get_settings
from ..state import MeetingState, RankedVenue, ScoreBreakdown, Weights, uses_transit


def fairness_score(fairness_raw: float, best_raw: float, tolerance_min: float) -> float:
    """How much worse than the best available option this is, in minutes.

    1.0 is the fairest candidate; the score falls linearly, reaching 0 once a
    candidate is `tolerance_min` minutes-equivalent worse. `fairness_raw` is a
    negated penalty (see Node C), so `best_raw` is the largest value.
    """
    deficit = best_raw - fairness_raw
    if tolerance_min <= 0:
        return 1.0 if deficit <= 0 else 0.0
    return max(0.0, min(1.0, 1.0 - deficit / tolerance_min))


def transfer_score(total_transfers: int, reference: int) -> float:
    """1.0 for a door-to-door trip, falling to 0 at `reference` total transfers."""
    if reference <= 0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - total_transfers / reference))


async def scorer_node(state: MeetingState, config: dict) -> dict:
    started = time.perf_counter()
    if state.get("failure"):
        return {}

    settings = get_settings()
    venues = state.get("candidate_venues", [])
    shortlist = {e.neighbourhood: e for e in state.get("shortlisted_neighbourhoods", [])}
    weights: Weights = state["weights"].normalized()

    pairs = [(v, shortlist[v.neighbourhood]) for v in venues if v.neighbourhood in shortlist]
    if not pairs:
        return {"ranked_venues": [], "timings": {"scorer": time.perf_counter() - started}}

    best_raw = max(entry.fairness_raw for _, entry in pairs)

    # The transfer component is the one genuine exclusion: with nobody on
    # transit there are no transfers to weigh, whatever the slider says, so its
    # weight is redistributed rather than contributing a flat constant.
    transit_in_play = uses_transit(*(person.mode for person in state["people"]))
    inactive = [] if transit_in_play else ["transfers"]

    live = {"fairness": weights.fairness, "preference": weights.preference}
    if transit_in_play:
        live["transfers"] = weights.transfers
    live_total = sum(live.values())
    if live_total <= 0:
        live = {name: 1.0 for name in live}
        live_total = float(len(live))
    effective = {name: value / live_total for name, value in live.items()}

    ranked: list[RankedVenue] = []
    for venue, entry in pairs:
        parts = {
            "fairness": fairness_score(
                entry.fairness_raw, best_raw, settings.fairness_tolerance_min
            ),
            # Already an absolute 0-1 fit score -- stretching it would destroy
            # the very meaning the LLM/heuristic assigned it.
            "preference": max(0.0, min(1.0, venue.preference_score)),
            "transfers": transfer_score(entry.total_transfers, settings.transfer_reference),
        }
        final = sum(weight * parts[name] for name, weight in effective.items())
        ranked.append(
            RankedVenue(
                venue=venue,
                shortlist=entry,
                scores=ScoreBreakdown(
                    fairness=round(parts["fairness"], 4),
                    preference=round(parts["preference"], 4),
                    transfers=round(parts["transfers"], 4),
                    final=round(final, 4),
                    inactive=inactive,
                ),
            )
        )

    ranked.sort(key=lambda r: r.scores.final, reverse=True)
    return {"ranked_venues": ranked, "timings": {"scorer": time.perf_counter() - started}}
