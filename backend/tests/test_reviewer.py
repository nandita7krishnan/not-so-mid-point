"""Node F: the hard-constraint checks that a weighted score must not override."""
import pytest

from app.nodes.reviewer import reviewer_node
from app.runtime import RunDeps
from app.state import (
    Budget,
    LatLng,
    Leg,
    RankedVenue,
    ScoreBreakdown,
    ShortlistEntry,
    Venue,
)


def _entry(name="Capitol Hill", minutes=20, transfers=1) -> ShortlistEntry:
    leg = Leg(neighbourhood=name, reachable=True, duration_min=minutes, transfers=transfers)
    return ShortlistEntry(
        neighbourhood=name,
        coords=LatLng(lat=47.62, lng=-122.32),
        legs=[leg, leg.model_copy()],
        gap_min=0.0,
        max_min=minutes,
        total_min=minutes * 2,
        total_transfers=transfers * 2,
        fairness_raw=-8.0,
    )


def _candidate(name, score, **venue_kwargs) -> RankedVenue:
    venue = Venue(
        place_id=name,
        name=name,
        address="",
        category="Coffee shop",
        coords=LatLng(lat=47.62, lng=-122.32),
        neighbourhood="Capitol Hill",
        types=["cafe"],
        **venue_kwargs,
    )
    return RankedVenue(
        venue=venue,
        shortlist=_entry(),
        scores=ScoreBreakdown(fairness=1.0, preference=1.0, transfers=1.0, final=score),
    )


def _state(candidates, **overrides):
    state = {
        "budget": Budget(max_time_min=45, max_transfers=2),
        "require_open": False,
        "ranked_venues": candidates,
        "categories": ["coffee"],
        "free_text": "",
        "people": [],
    }
    state.update(overrides)
    return state


CONFIG = {"configurable": {"deps": RunDeps(maps=None, llm=None)}}


async def test_falls_back_past_a_permanently_closed_top_pick():
    candidates = [
        _candidate("Gone", 0.99, business_status="CLOSED_PERMANENTLY"),
        _candidate("Open A", 0.8),
        _candidate("Open B", 0.7),
        _candidate("Open C", 0.6),
    ]
    result = await reviewer_node(_state(candidates), CONFIG)

    names = [p.venue.name for p in result["final_top_3"]]
    assert names == ["Open A", "Open B", "Open C"]
    assert any("Gone" in w for w in result["warnings"])


async def test_require_open_filters_currently_closed_venues():
    candidates = [
        _candidate("Shut", 0.99, open_now=False),
        _candidate("Open A", 0.8, open_now=True),
    ]
    result = await reviewer_node(_state(candidates, require_open=True), CONFIG)

    assert [p.venue.name for p in result["final_top_3"]] == ["Open A"]


async def test_unknown_opening_hours_are_not_treated_as_closed():
    result = await reviewer_node(
        _state([_candidate("Unknown hours", 0.9, open_now=None)], require_open=True), CONFIG
    )
    assert [p.venue.name for p in result["final_top_3"]] == ["Unknown hours"]


async def test_all_candidates_rejected_produces_a_failure_not_an_empty_list():
    candidates = [_candidate("Gone", 0.9, business_status="CLOSED_PERMANENTLY")]
    result = await reviewer_node(_state(candidates), CONFIG)

    assert result["final_top_3"] == []
    assert result["failure"].node == "reviewer"
    assert result["failure"].suggestion


async def test_duplicate_places_are_not_recommended_twice():
    duplicate = _candidate("Same Place", 0.9)
    candidates = [duplicate, duplicate.model_copy(deep=True), _candidate("Other", 0.5)]
    result = await reviewer_node(_state(candidates), CONFIG)

    assert [p.venue.name for p in result["final_top_3"]] == ["Same Place", "Other"]


# --- "an even trip" has to survive being read next to the numbers ------------


def test_a_small_gap_on_a_short_journey_is_not_an_even_trip():
    """The case grading turned up: 4 minutes apart on a 12 minute journey is
    one person travelling half again as long as the other."""
    from app.nodes.reviewer import _reads_as_even

    assert not _reads_as_even(4.0, 12.0)


def test_the_same_gap_on_a_long_journey_is():
    from app.nodes.reviewer import _reads_as_even

    assert _reads_as_even(4.0, 40.0)


def test_a_gap_inside_traffic_noise_is_even_at_any_length():
    from app.nodes.reviewer import _reads_as_even

    assert _reads_as_even(1.5, 6.0)


def test_a_wide_gap_is_never_even_however_long_the_trip():
    from app.nodes.reviewer import _reads_as_even

    assert not _reads_as_even(20.0, 200.0)
