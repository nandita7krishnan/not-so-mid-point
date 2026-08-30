"""The search log: does it capture enough to evaluate, and does it leak?

The second question is the load-bearing one. The record exists to be kept, and
possibly shared, so the scrubbing has to hold for a real street address rather
than the tidy neighbourhood names the rest of the tests use.
"""
import json

import pytest

from app import searchlog
from app.config import get_settings
from app.main import PartyRequest, RecommendRequest
from app.state import LatLng, Location, Person

# What Google actually returns for a typed house number, which is the case the
# scrubbing has to survive.
DOORSTEP = "5200 Ballard Ave NW, Seattle, WA 98107, USA"


def _person(label: str, address: str, mode: str = "transit", lat=47.66853, lng=-122.38351):
    return Person(
        label=label,
        location=Location(query=address, label=address, coords=LatLng(lat=lat, lng=lng)),
        mode=mode,
    )


def _request(**overrides):
    payload = {
        "people": [
            PartyRequest(address=DOORSTEP, mode="transit"),
            PartyRequest(address="Columbia City, Seattle, WA", mode="driving"),
        ],
        "categories": ["coffee"],
        "free_text": "",
        "max_time_min": 45,
        "max_transfers": 2,
    }
    payload.update(overrides)
    return RecommendRequest(**payload)


def _response(ok=True):
    journey = {
        "neighbourhood": "Belltown",
        "coords": {"lat": 47.6145, "lng": -122.3462},
        "legs": [
            {"neighbourhood": "Belltown", "reachable": True, "mode": "transit",
             "duration_min": 40.0, "transfers": 1, "summary": "D Line", "error": ""},
            {"neighbourhood": "Belltown", "reachable": True, "mode": "driving",
             "duration_min": 19.0, "transfers": 0, "summary": "9.6 km", "error": ""},
        ],
        "gap_min": 21.0, "max_min": 40.0, "total_min": 59.0,
        "total_transfers": 1, "fairness_raw": 0.6, "transfers_meaningful": True,
    }
    return {
        "ok": ok,
        "transfers_apply": True,
        "preference_spec": {"place_types": ["cafe"], "keywords": [], "text_query": "",
                            "source": "categories", "rationale": ""},
        "search_area": {
            "center": {"lat": 47.61428, "lng": -122.33459},
            "radius_m": 8000,
            "candidates": [{"name": "Belltown", "coords": {"lat": 47.61, "lng": -122.34}}],
            "balance_note": "",
        },
        "shortlist": [journey],
        "results": [
            {
                "venue": {
                    "place_id": "abc", "name": "Storyville Coffee",
                    "address": "94 Pike St, Seattle, WA 98101",
                    "category": "Coffee shop",
                    "coords": {"lat": 47.6089, "lng": -122.3404},
                    "neighbourhood": "Belltown", "rating": 4.6, "rating_count": 2100,
                    "price_level": None, "open_now": True,
                    "business_status": "OPERATIONAL", "types": ["cafe"],
                    "primary_type": "coffee_shop",
                    "preference_score": 0.9, "preference_reason": "",
                },
                "shortlist": journey,
                "scores": {"fairness": 0.8, "preference": 0.9, "transfers": 0.75,
                           "final": 0.83, "inactive": []},
                "why": "An even trip for both of you.",
            }
        ] if ok else [],
        "failure": None if ok else {
            "node": "shortlist", "reason": "Nothing was reachable in time.",
            "suggestion": "Raise the travel time limit to about 55 min.", "detail": {},
        },
        "warnings": [],
        "timings": {"total": 6.2},
    }


def _record(**kwargs):
    return searchlog.build_record(
        people=kwargs.pop("people", [
            _person("Ana", DOORSTEP, "transit"),
            _person("Ben", "Columbia City, Seattle, WA", "driving", 47.5595, -122.2855),
        ]),
        request=kwargs.pop("request", _request()),
        response=kwargs.pop("response", _response()),
        departure=kwargs.pop("departure", 1_700_000_000),
        visitor=kwargs.pop("visitor", "hashed123"),
        precision=kwargs.pop("precision", 2),
    )


# ------------------------------------------------------------------ scrubbing

def test_a_typed_street_address_never_reaches_the_record():
    blob = json.dumps(_record())
    assert "5200" not in blob
    assert "Ballard Ave" not in blob
    assert "98107" not in blob


def test_a_start_is_named_by_the_nearest_seeded_neighbourhood():
    """The vocabulary is a closed list, so no address can survive the mapping --
    whatever Google returns, the output is one of ~50 names we already had."""
    record = _record()
    areas = [p["area"] for p in record["request"]["people"]]
    assert areas == ["Ballard", "Columbia City"]
    assert {p["area_source"] for p in record["request"]["people"]} == {"seed"}
    # The names came from the seed list, not from the address that was typed.
    seeded = {n.name for n in searchlog.NEIGHBOURHOODS}
    assert set(areas) <= seeded


def test_outside_the_seeded_area_it_falls_back_to_the_city_tail():
    far = _person("Ana", "10 Downing St, London SW1A 2AA, UK", lat=51.5034, lng=-0.1276)
    record = _record(people=[far, _person("Ben", "Columbia City, Seattle, WA",
                                         lat=47.5595, lng=-122.2855)])
    p1 = record["request"]["people"][0]
    assert p1["area_source"] == "label"
    assert p1["area"] == "London, UK"  # street line dropped, postcode trimmed
    assert "Downing" not in json.dumps(record)


def test_postcodes_are_stripped_whatever_shape_they_are():
    """A UK postcode narrows to about fifteen households, so a digits-only rule
    would not have been enough."""
    assert searchlog._coarse_area("10 Downing St, London SW1A 2AA, UK") == "London, UK"
    assert searchlog._coarse_area("1 Infinite Loop, Cupertino, CA 95014") == "Cupertino, CA"


def test_the_fallback_keeps_the_city_and_drops_the_street():
    assert searchlog._coarse_area(DOORSTEP) == "Seattle, WA, USA"
    # Already coarse, and stays that way rather than being reduced to nothing.
    assert searchlog._coarse_area("Seattle, WA") == "Seattle, WA"
    assert searchlog._coarse_area("Belltown") == "Belltown"


def test_participant_names_are_replaced_by_position():
    record = _record()
    assert [p["id"] for p in record["request"]["people"]] == ["P1", "P2"]
    assert "Ana" not in json.dumps(record)


def test_coordinates_are_rounded_to_a_neighbourhood():
    person = record_coords(_record())[0]
    # 47.66853 was the input; 2dp is ~1.1 km of ambiguity.
    assert person == {"lat": 47.67, "lng": -122.38}


def test_precision_is_configurable_for_a_coarser_log():
    assert record_coords(_record(precision=1))[0] == {"lat": 47.7, "lng": -122.4}


def record_coords(record):
    return [p["coords"] for p in record["request"]["people"]]


def test_venue_addresses_are_kept_whole():
    """A business address is public, and an eval needs it to check the pick."""
    assert "94 Pike St" in json.dumps(_record())


# -------------------------------------------------------------- completeness

def test_the_record_carries_what_an_eval_needs_to_judge():
    record = _record()
    request, response = record["request"], record["response"]

    assert request["categories"] == ["coffee"]
    assert request["max_time_min"] == 45
    assert request["party_size"] == 2
    assert [p["mode"] for p in request["people"]] == ["transit", "driving"]
    # Weights are stored normalised, so they read as the shares the scorer used.
    assert pytest.approx(sum(request["weights"].values())) == 1.0

    result = response["results"][0]
    assert result["rank"] == 1
    assert result["name"] == "Storyville Coffee"
    assert result["scores"]["final"] == 0.83
    assert result["why"]
    # The per-person journeys are what makes a fairness judgement possible.
    assert [leg["duration_min"] for leg in result["journey"]["legs"]] == [40.0, 19.0]


def test_a_search_that_found_nothing_is_logged_with_its_reason():
    record = _record(response=_response(ok=False))
    assert record["response"]["ok"] is False
    assert record["response"]["results"] == []
    assert "Raise the travel time limit" in record["response"]["failure"]["suggestion"]


def test_departure_is_decomposed_for_slicing_by_time_of_day():
    request = _record()["request"]
    assert 0 <= request["departure_hour_utc"] <= 23
    assert 0 <= request["departure_weekday"] <= 6


# ------------------------------------------------------------------- writing

def test_nothing_is_written_unless_the_log_is_switched_on(tmp_path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("SEARCH_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("SEARCH_LOG_ENABLED", raising=False)
    searchlog.record(
        people=[_person("Ana", DOORSTEP)],
        request=_request(),
        response=_response(),
        departure=1_700_000_000,
    )
    assert list(tmp_path.iterdir()) == []
    get_settings.cache_clear()


def test_enabled_it_appends_one_json_line_per_search(tmp_path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("SEARCH_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("SEARCH_LOG_ENABLED", "true")
    for _ in range(3):
        searchlog.record(
            people=[_person("Ana", DOORSTEP), _person("Ben", "Columbia City, Seattle, WA")],
            request=_request(),
            response=_response(),
            departure=1_700_000_000,
        )
    written = list(tmp_path.glob("searches-*.jsonl"))
    assert len(written) == 1
    lines = written[0].read_text().strip().split("\n")
    assert len(lines) == 3
    ids = {json.loads(line)["id"] for line in lines}
    assert len(ids) == 3  # each search is its own case, not a dedup key
    get_settings.cache_clear()


def test_a_broken_log_never_breaks_the_search(tmp_path, monkeypatch):
    """The search has already been paid for and answered by this point."""
    get_settings.cache_clear()
    monkeypatch.setenv("SEARCH_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("SEARCH_LOG_ENABLED", "true")
    monkeypatch.setattr(
        searchlog, "build_record", lambda **kw: (_ for _ in ()).throw(ValueError("boom"))
    )
    searchlog.record(
        people=[_person("Ana", DOORSTEP)],
        request=_request(),
        response=_response(),
        departure=1_700_000_000,
    )  # must not raise
    get_settings.cache_clear()


# --- anchors: a sharper name for a start, still never a home -----------------


def _record_with(anchor_file, lat, lng):
    from app import anchors

    anchors._cache.clear()
    people = [_person("Ana", DOORSTEP, lat=lat, lng=lng), _person("Ben", "Columbia City")]
    return searchlog.build_record(
        people=people, request=_request(), response=_response(), departure=1_700_000_000
    )


def test_a_dense_start_is_named_by_its_stop_not_its_neighbourhood(anchor_file):
    """The precision the anchors exist for: a stop, not a district."""
    from conftest import CORE

    record = _record_with(anchor_file, CORE.lat + 0.0005, CORE.lng + 0.0005)
    first = record["request"]["people"][0]
    assert first["area_source"] == "stop"
    assert first["area"].startswith("Core ")


def test_a_stop_snapped_start_carries_the_stops_own_coordinate(anchor_file):
    """Not a rounded version of where the person was: the published point.

    That is what makes the record replayable, and it is also why the usual 2dp
    rounding does not apply to it -- there is nothing personal left to blur.
    """
    from conftest import CORE

    record = _record_with(anchor_file, CORE.lat + 0.0005, CORE.lng + 0.0005)
    coords = record["request"]["people"][0]["coords"]
    assert coords == {"lat": CORE.lat, "lng": CORE.lng}
    assert coords != {"lat": round(CORE.lat + 0.0005, 2), "lng": round(CORE.lng + 0.0005, 2)}


def test_a_sparse_start_falls_back_to_the_neighbourhood(anchor_file):
    """Where a stop would name a handful of houses, the coarse name wins."""
    record = _record_with(anchor_file, 47.5595, -122.2855)  # Columbia City, no core stops
    first = record["request"]["people"][0]
    assert first["area_source"] == "seed"
    assert first["area"] == "Columbia City"
    assert first["coords"] == {"lat": 47.56, "lng": -122.29}


def test_without_an_anchor_file_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_LOG_ANCHORS", str(tmp_path / "absent.csv"))
    get_settings.cache_clear()
    record = _record_with(None, 47.66853, -122.38351)
    assert record["request"]["people"][0]["area_source"] == "seed"


# --- durability: the copy that survives an ephemeral disk --------------------


def test_a_record_is_also_emitted_as_a_log_line(tmp_path, monkeypatch, caplog):
    """The file is gone at the next restart on a free host, so the log line is
    the durable copy rather than a convenience."""
    import logging

    get_settings.cache_clear()
    monkeypatch.setenv("SEARCH_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("SEARCH_LOG_ENABLED", "true")
    with caplog.at_level(logging.INFO, logger="app.searchlog"):
        searchlog.record(
            people=[_person("Ana", DOORSTEP), _person("Ben", "Columbia City")],
            request=_request(), response=_response(), departure=1_700_000_000,
        )

    lines = [r.getMessage() for r in caplog.records if searchlog.STDOUT_MARKER in r.getMessage()]
    assert len(lines) == 1
    payload = json.loads(lines[0].split(searchlog.STDOUT_MARKER + " ", 1)[1])
    assert payload["request"]["people"][0]["id"] == "P1"
    assert DOORSTEP not in lines[0]


def test_the_log_line_can_be_switched_off(tmp_path, monkeypatch, caplog):
    import logging

    get_settings.cache_clear()
    monkeypatch.setenv("SEARCH_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("SEARCH_LOG_ENABLED", "true")
    monkeypatch.setenv("SEARCH_LOG_TO_STDOUT", "false")
    with caplog.at_level(logging.INFO, logger="app.searchlog"):
        searchlog.record(
            people=[_person("Ana", DOORSTEP), _person("Ben", "Columbia City")],
            request=_request(), response=_response(), departure=1_700_000_000,
        )
    assert not [r for r in caplog.records if searchlog.STDOUT_MARKER in r.getMessage()]


def test_a_stop_snapped_start_still_names_its_district(anchor_file):
    """A stop name is sharper and much less legible. Both are kept, because
    the coarse one was already safe to write down."""
    from conftest import CORE

    record = _record_with(anchor_file, CORE.lat + 0.0005, CORE.lng + 0.0005)
    first = record["request"]["people"][0]
    assert first["area"].startswith("Core ")
    assert first["area_coarse"] == "Downtown Seattle"


def test_a_pick_records_where_it_actually_is(anchor_file):
    """Journey times belong to the neighbourhood centroid while the venue can
    sit up to a radius away, so an eval needs both points to judge the claim."""
    record = _record_with(anchor_file, 47.66853, -122.38351)
    result = record["response"]["results"][0]
    assert result["coords"] == {"lat": 47.6089, "lng": -122.3404}
    assert result["journey"]["coords"] == {"lat": 47.6145, "lng": -122.3462}
