"""Node F -- reviewer / presenter.

Validates the ranked list against the hard constraints that shouldn't be
softened by a weighted score -- permanently closed venues, currently shut when
the user asked for open, budget violations -- and walks down the ranking until
three survive. Then it writes the "why this spot" line for each.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from ..runtime import deps_from_config
from ..state import MODE_LABELS, Budget, GraphFailure, MeetingState, RankedVenue

REJECTED_STATUSES = {"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"}


def _rejection_reason(
    candidate: RankedVenue, budget: Budget, require_open: bool
) -> Optional[str]:
    venue = candidate.venue
    if venue.business_status in REJECTED_STATUSES:
        return f"{venue.name}: {venue.business_status.replace('_', ' ').lower()}"
    if require_open and venue.open_now is False:
        return f"{venue.name}: closed right now"
    entry = candidate.shortlist
    if any(leg.duration_min > budget.max_time_min for leg in entry.legs):
        return f"{venue.name}: over the travel time budget on re-check"
    # Only transit legs can breach a transfer budget.
    if any(
        leg.mode == "transit" and leg.transfers > budget.max_transfers
        for leg in entry.legs
    ):
        return f"{venue.name}: over the transfer budget on re-check"
    return None


def _template_why(candidate: RankedVenue) -> str:
    entry = candidate.shortlist
    venue = candidate.venue
    everyone = "both of you" if len(entry.legs) == 2 else f"all {len(entry.legs)} of you"
    fairness = (
        f"an even trip for {everyone}"
        if entry.gap_min < 5
        else f"a {entry.gap_min:.0f} min spread between you"
    )
    if not entry.transfers_meaningful:
        # Nobody is on transit, so describe the modes instead of transfers.
        modes = " and ".join(
            sorted({MODE_LABELS.get(leg.mode, leg.mode) for leg in entry.legs})
        )
        detail = f"by {modes}"
    elif entry.total_transfers == 0:
        detail = "with no transfers either way"
    else:
        detail = (
            f"with {entry.total_transfers} "
            f"transfer{'s' if entry.total_transfers != 1 else ''} total"
        )
    rating = f", rated {venue.rating}★" if venue.rating else ""
    return (
        f"{venue.name} is a {venue.category.lower()} in {entry.neighbourhood}{rating}. "
        f"Getting there is {fairness}, {detail}."
    )


async def reviewer_node(state: MeetingState, config: dict) -> dict:
    started = time.perf_counter()
    if state.get("failure"):
        return {}

    deps = deps_from_config(config)
    budget: Budget = state["budget"]
    require_open = state.get("require_open", False)
    ranked = state.get("ranked_venues", [])

    picks: list[RankedVenue] = []
    seen: set[str] = set()
    rejections: list[str] = []
    for candidate in ranked:
        if len(picks) == 3:
            break
        if candidate.venue.place_id in seen:
            continue
        reason = _rejection_reason(candidate, budget, require_open)
        if reason:
            rejections.append(reason)
            continue
        seen.add(candidate.venue.place_id)
        picks.append(candidate)

    if not picks:
        return {
            "final_top_3": [],
            "failure": GraphFailure(
                node="reviewer",
                reason="Every candidate venue failed a final check.",
                suggestion=(
                    "If you asked for places open right now, try turning that off, "
                    "or widen the travel time budget."
                ),
                detail={"rejected": rejections[:10]},
            ),
            "timings": {"reviewer": time.perf_counter() - started},
        }

    for pick in picks:
        pick.why = _template_why(pick)

    if deps.llm is not None:
        people = state["people"]

        def journeys(pick) -> str:
            return "\n".join(
                f"{person.label} ({MODE_LABELS.get(leg.mode, leg.mode)}): "
                f"{leg.duration_min:.0f} min, {leg.transfers} transfers ({leg.summary})."
                for person, leg in zip(people, pick.shortlist.legs)
            )

        prompts = [
            (
                f"Venue: {p.venue.name}, a {p.venue.category} in {p.shortlist.neighbourhood}"
                + (f", rated {p.venue.rating} from {p.venue.rating_count} reviews" if p.venue.rating else "")
                + f".\n{journeys(p)}\n"
                f"They asked for: {state.get('free_text') or ', '.join(state.get('categories', [])) or 'anywhere'}.\n"
                f"Preference note: {p.venue.preference_reason}\n\n"
                "Write the one-sentence reason this spot suits them."
            )
            for p in picks
        ]
        texts = await asyncio.gather(
            *(deps.llm.explain(prompt) for prompt in prompts), return_exceptions=True
        )
        for pick, text in zip(picks, texts):
            if isinstance(text, str) and text:
                pick.why = text

    warnings: list[str] = []
    if len(picks) < 3:
        warnings.append(
            f"Only {len(picks)} spot{'s' if len(picks) != 1 else ''} passed the final checks."
        )
    if rejections:
        warnings.append(f"{len(rejections)} higher-ranked venue(s) were dropped: {rejections[0]}.")

    return {
        "final_top_3": picks,
        "warnings": warnings,
        "timings": {"reviewer": time.perf_counter() - started},
    }
