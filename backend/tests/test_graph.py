"""End-to-end graph tests against the fake Maps client."""
import pytest
from conftest import FakeMaps, make_people

from app.graph import run_graph
from app.runtime import RunDeps
from app.state import Budget, Weights


async def _run(fake: FakeMaps, *, modes=("transit", "transit"), addresses=None, **overrides):
    addresses = addresses or ("Ballard, Seattle", "Columbia City, Seattle")
    initial = {
        "people": await make_people(fake, *zip(addresses, modes)),
        "budget": Budget(max_time_min=60, max_transfers=3),
        "weights": Weights(),
        "fairness_mode": "gap",
        "categories": ["coffee"],
        "free_text": "",
        "departure_time": 1_700_000_000,
        "require_open": False,
    }
    initial.update(overrides)
    return await run_graph(initial, RunDeps(maps=fake, llm=None))


async def test_happy_path_returns_three_spots(fake_maps):
    state = await _run(fake_maps)

    assert state.get("failure") is None
    assert len(state["final_top_3"]) == 3
    for pick in state["final_top_3"]:
        assert pick.venue.name
        assert all(leg.reachable for leg in pick.shortlist.legs)
        assert pick.why  # Node F always produces copy, LLM or template
    # Ranked in descending final score.
    finals = [p.scores.final for p in state["final_top_3"]]
    assert finals == sorted(finals, reverse=True)


async def test_search_area_lands_between_the_two_people(fake_maps):
    state = await _run(fake_maps)
    area = state["search_area"]
    p1 = state["people"][0].location.coords
    p2 = state["people"][1].location.coords
    assert min(p1.lat, p2.lat) <= area.center.lat <= max(p1.lat, p2.lat)
    assert area.candidates and area.radius_m > 0


async def test_reachability_nodes_both_cover_every_candidate(fake_maps):
    state = await _run(fake_maps)
    names = {c.name for c in state["search_area"].candidates}
    assert {leg.neighbourhood for leg in state["reachability"][0]} == names
    assert {leg.neighbourhood for leg in state["reachability"][1]} == names


async def test_hard_budget_is_enforced_by_the_shortlist(fake_maps):
    state = await _run(fake_maps, budget=Budget(max_time_min=25, max_transfers=3))
    for entry in state["shortlisted_neighbourhoods"]:
        assert entry.legs[0].duration_min <= 25
        assert entry.legs[1].duration_min <= 25


async def test_impossible_budget_fails_with_actionable_advice(fake_maps):
    state = await _run(fake_maps, budget=Budget(max_time_min=6, max_transfers=0))

    failure = state["failure"]
    assert failure is not None
    assert failure.node == "shortlist"
    assert state["final_top_3"] == []
    # The PRD's requirement: say what to relax, don't just return nothing.
    assert "raise the travel time limit" in failure.suggestion
    assert failure.detail["min_max_time_min"] > 6


async def test_unroutable_neighbourhoods_are_dropped_not_fatal(fake_maps):
    everything = await _run(fake_maps)
    blocked = {c.name for c in everything["search_area"].candidates[:2]}

    state = await _run(FakeMaps(unreachable=blocked))

    assert state.get("failure") is None
    assert blocked.isdisjoint({e.neighbourhood for e in state["shortlisted_neighbourhoods"]})


async def test_weights_change_the_ranking(fake_maps):
    fair = await _run(fake_maps, weights=Weights(fairness=1, preference=0, transfers=0))
    pref = await _run(fake_maps, weights=Weights(fairness=0, preference=1, transfers=0))

    fair_top = fair["final_top_3"][0]
    pref_top = pref["final_top_3"][0]
    assert fair_top.scores.fairness >= pref_top.scores.fairness
    assert pref_top.scores.preference >= fair_top.scores.preference


async def test_fairness_mode_is_visible_in_the_shortlist(fake_maps):
    gap = await _run(fake_maps, fairness_mode="gap")
    absolute = await _run(fake_maps, fairness_mode="absolute")

    # The toggle reaches all the way back to Node 0, so the two modes search
    # different balance points -- which is exactly the "visibly changes results"
    # behaviour Section 11 asks for.
    gap_top = gap["shortlisted_neighbourhoods"][0]
    abs_top = absolute["shortlisted_neighbourhoods"][0]
    assert gap_top.gap_min <= abs_top.gap_min
    assert abs_top.total_min <= gap_top.total_min


async def test_timings_are_recorded_for_every_node(fake_maps):
    state = await _run(fake_maps)
    for key in ("search_area", "shortlist", "spot_finder", "scorer", "reviewer", "total"):
        assert key in state["timings"]
