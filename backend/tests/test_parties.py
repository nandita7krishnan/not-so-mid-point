"""Three to five parties: fairness generalisation and the parallel fan-out."""
import pytest
from conftest import FakeMaps, make_people

from app.graph import run_graph
from app.nodes.shortlist import fairness_raw
from app.runtime import RunDeps
from app.state import MAX_PARTIES, MIN_PARTIES, Budget, Weights

ADDRESSES = [
    "Ballard, Seattle",
    "Columbia City, Seattle",
    "Capitol Hill, Seattle",
    "West Seattle, Seattle",
    "Greenwood, Seattle",
]
K = 0.4


async def _run(fake: FakeMaps, count: int, *, modes=None, **overrides):
    modes = modes or ["transit"] * count
    people = await make_people(fake, *zip(ADDRESSES[:count], modes))
    initial = {
        "people": people,
        "budget": Budget(max_time_min=90, max_transfers=4),
        "weights": Weights(),
        "fairness_mode": "gap",
        "categories": ["coffee"],
        "free_text": "",
        "departure_time": 1_700_000_000,
        "require_open": False,
    }
    initial.update(overrides)
    return await run_graph(initial, RunDeps(maps=fake, llm=None))


def test_fairness_reduces_to_the_two_party_formula():
    """The generalisation must not change existing two-party behaviour."""
    assert fairness_raw([15, 20], "gap", K) == -abs(15 - 20) - K * 20
    assert fairness_raw([40, 40], "gap", K) == -K * 40


def test_fairness_uses_the_spread_across_all_parties():
    # Best-off 15, worst-off 45: a 30-minute spread, whatever sits between.
    assert fairness_raw([15, 30, 45], "gap", K) == -30 - K * 45
    # An even trio beats an uneven one with the same worst case.
    assert fairness_raw([40, 40, 40], "gap", K) > fairness_raw([10, 25, 40], "gap", K)


def test_absolute_mode_sums_every_party():
    assert fairness_raw([10, 20, 30], "absolute", K) == -60


@pytest.mark.parametrize("count", [2, 3, 4, 5])
async def test_graph_runs_for_every_supported_party_count(count):
    fake = FakeMaps()
    state = await _run(fake, count)

    assert state.get("failure") is None, state.get("failure")
    assert len(state["final_top_3"]) == 3
    for pick in state["final_top_3"]:
        assert len(pick.shortlist.legs) == count
        assert all(leg.reachable for leg in pick.shortlist.legs)


@pytest.mark.parametrize("count", [2, 3, 4, 5])
async def test_every_party_gets_their_own_reachability_entry(count):
    fake = FakeMaps()
    state = await _run(fake, count)

    assert sorted(state["reachability"].keys()) == list(range(count))
    names = {c.name for c in state["search_area"].candidates}
    for index in range(count):
        assert {leg.neighbourhood for leg in state["reachability"][index]} == names


async def test_unused_party_slots_cost_nothing():
    """Five static nodes exist, but a three-party run must not route for five."""
    fake = FakeMaps()
    await _run(fake, 3)

    candidates = 8  # max_candidate_neighbourhoods
    assert fake.calls["travel_leg"] <= 3 * candidates
    assert fake.calls["travel_durations"] == 3


async def test_mixed_modes_across_more_than_two_parties():
    fake = FakeMaps()
    state = await _run(fake, 4, modes=["transit", "driving", "bicycling", "walking"])

    legs = state["final_top_3"][0].shortlist.legs
    assert [leg.mode for leg in legs] == ["transit", "driving", "bicycling", "walking"]
    # Only the transit party can carry transfers.
    assert all(leg.transfers == 0 for leg in legs if leg.mode != "transit")


async def test_transfer_budget_ignored_when_no_party_takes_transit():
    fake = FakeMaps()
    state = await _run(
        fake, 3, modes=["driving", "walking", "bicycling"],
        budget=Budget(max_time_min=90, max_transfers=0),
    )
    assert state.get("failure") is None
    assert state["shortlisted_neighbourhoods"]
    assert state["final_top_3"][0].scores.inactive == ["transfers"]


async def test_spread_is_reported_across_all_parties():
    fake = FakeMaps()
    state = await _run(fake, 4)

    for entry in state["shortlisted_neighbourhoods"]:
        times = [leg.duration_min for leg in entry.legs]
        assert entry.gap_min == pytest.approx(max(times) - min(times))
        assert entry.total_min == pytest.approx(sum(times))
        assert entry.max_min == pytest.approx(max(times))


async def test_one_unreachable_party_removes_the_neighbourhood_for_everyone():
    fake = FakeMaps()
    baseline = await _run(fake, 3)
    blocked = {c.name for c in baseline["search_area"].candidates[:2]}

    state = await _run(FakeMaps(unreachable=blocked), 3)

    survivors = {e.neighbourhood for e in state["shortlisted_neighbourhoods"]}
    assert blocked.isdisjoint(survivors)


async def test_party_count_bounds_are_declared():
    assert MIN_PARTIES == 2
    assert MAX_PARTIES == 5
