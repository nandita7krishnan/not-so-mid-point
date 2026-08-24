"""Per-person travel modes, and the transfer budget that only applies to transit."""
import pytest
from conftest import FakeMaps, make_people

from app.graph import run_graph
from app.runtime import RunDeps
from app.state import Budget, Weights, uses_transit


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


def test_uses_transit_predicate():
    assert uses_transit("transit", "driving")
    assert uses_transit("walking", "transit")
    assert not uses_transit("driving", "walking")


async def test_each_person_travels_in_their_own_mode(fake_maps):
    state = await _run(fake_maps, modes=("driving", "walking"))

    assert {leg.mode for leg in state["reachability"][0]} == {"driving"}
    assert {leg.mode for leg in state["reachability"][1]} == {"walking"}


async def test_driving_beats_transit_for_the_same_person(fake_maps):
    transit = await _run(fake_maps)
    driving = await _run(FakeMaps(), modes=("driving", "transit"))

    def p1_time(state):
        return state["final_top_3"][0].shortlist.legs[0].duration_min

    assert p1_time(driving) < p1_time(transit)


async def test_non_transit_legs_never_report_transfers(fake_maps):
    state = await _run(fake_maps, modes=("driving", "bicycling"))

    for entry in state["shortlisted_neighbourhoods"]:
        assert entry.legs[0].transfers == 0
        assert entry.legs[1].transfers == 0
        assert entry.transfers_meaningful is False


async def test_transfer_budget_is_ignored_when_nobody_takes_transit(fake_maps):
    """A zero-transfer budget must not filter out drivers, who have no transfers."""
    state = await _run(
        FakeMaps(), modes=("driving", "driving"),
        budget=Budget(max_time_min=60, max_transfers=0),
    )
    assert state.get("failure") is None
    assert state["shortlisted_neighbourhoods"]


async def test_transfer_budget_still_binds_on_the_transit_person(fake_maps):
    """One driver, one rider: the rider's transfers are still checked."""
    # km_per_transfer=2 makes the fake produce real transfer counts at these
    # distances, so the budget has something to actually bite on.
    strict = await _run(
        FakeMaps(km_per_transfer=2), modes=("driving", "transit"),
        budget=Budget(max_time_min=60, max_transfers=0),
    )
    loose = await _run(
        FakeMaps(km_per_transfer=2), modes=("driving", "transit"),
        budget=Budget(max_time_min=60, max_transfers=3),
    )
    for entry in strict["shortlisted_neighbourhoods"]:
        assert entry.legs[1].transfers == 0
    assert len(strict["shortlisted_neighbourhoods"]) < len(loose["shortlisted_neighbourhoods"])


async def test_transfers_component_is_dropped_when_no_one_is_on_transit(fake_maps):
    state = await _run(FakeMaps(), modes=("driving", "walking"))

    scores = state["final_top_3"][0].scores
    assert "transfers" in scores.inactive


async def test_single_neighbourhood_still_ranks_by_preference(fake_maps):
    """With one neighbourhood, fairness is constant. On an absolute scale that
    just adds the same amount to every venue, so the ranking is driven by
    preference and remains meaningful -- no component needs excluding."""
    full = await _run(fake_maps)
    candidates = [c.name for c in full["search_area"].candidates]
    survivor = candidates[0]
    state = await _run(FakeMaps(unreachable=set(candidates[1:])))

    assert {e.neighbourhood for e in state["shortlisted_neighbourhoods"]} == {survivor}

    picks = state["final_top_3"]
    fairness_values = {p.scores.fairness for p in picks}
    assert len(fairness_values) == 1, "one neighbourhood => one fairness value"
    finals = [p.scores.final for p in picks]
    assert finals == sorted(finals, reverse=True)
    assert finals[0] > finals[-1], "ranking must still discriminate"


async def test_transfers_component_uses_an_absolute_reference(fake_maps):
    """A direct trip scores 1.0 on transfers regardless of what the other
    candidates look like -- it is not graded on a curve."""
    state = await _run(fake_maps)
    for pick in state["final_top_3"]:
        if pick.shortlist.total_transfers == 0:
            assert pick.scores.transfers == 1.0


async def test_free_text_reaches_places_even_without_an_llm(fake_maps):
    """Regression: free text used to be discarded entirely when no LLM was
    configured, silently replacing the user's ask with default categories."""
    state = await _run(FakeMaps(), categories=[], free_text="running trail")

    spec = state["preference_spec"]
    assert spec.source == "text-only"
    assert spec.text_query == "running trail"
    assert spec.keywords == ["running", "trail"]
    # Crucially, it must NOT have silently substituted the default categories.
    assert spec.place_types == []


async def test_categories_only_still_uses_type_search(fake_maps):
    state = await _run(fake_maps, categories=["coffee"], free_text="")

    spec = state["preference_spec"]
    assert spec.source == "categories"
    assert "cafe" in spec.place_types
    assert spec.text_query == ""


async def test_text_search_is_used_when_no_categories_selected(fake_maps):
    fake = FakeMaps()
    await _run(fake, categories=[], free_text="running trail")
    # No types to search by, so only the text endpoint should have been hit.
    assert fake.calls.get("search_text", 0) > 0
    assert fake.calls.get("search_nearby", 0) == 0


async def test_venues_outside_the_neighbourhood_radius_are_dropped(fake_maps):
    """Regression: Places' locationBias let far-away venues through, and they
    then inherited the centroid's travel time -- a trail in Ballard was shown
    as a 13-minute drive because it was filed under South Lake Union."""
    baseline = await _run(fake_maps)
    entry = baseline["shortlisted_neighbourhoods"][0]
    key = (round(entry.coords.lat, 3), round(entry.coords.lng, 3))

    far_away = {
        "id": "far-trail",
        "displayName": {"text": "Trail Miles Away"},
        "formattedAddress": "Somewhere else",
        # ~11 km north of the neighbourhood centroid.
        "location": {"latitude": entry.coords.lat + 0.1, "longitude": entry.coords.lng},
        "types": ["park"],
        "rating": 4.9,
        "userRatingCount": 900,
        "businessStatus": "OPERATIONAL",
    }
    nearby = {**far_away, "id": "near-trail", "displayName": {"text": "Trail Right Here"},
              "location": {"latitude": entry.coords.lat, "longitude": entry.coords.lng}}

    state = await _run(FakeMaps(venue_overrides={key: [far_away, nearby]}))

    names = {v.name for v in state["candidate_venues"]}
    assert "Trail Miles Away" not in names
    assert "Trail Right Here" in names
