"""Freezing a case's Maps traffic, so grading it twice costs once."""
from __future__ import annotations

import asyncio

import pytest

from app.state import LatLng, Leg, Location
from evals.fixtures import FixtureMaps, FixtureMiss

from conftest import FakeMaps

A = LatLng(lat=47.6062, lng=-122.3321)
B = LatLng(lat=47.6685, lng=-122.3835)


def record(path):
    inner = FakeMaps()
    maps = FixtureMaps(path, inner)

    async def go():
        return (
            await maps.geocode("Ballard"),
            await maps.travel_leg(A, B, "Ballard", 1_700_000_000, "transit"),
            await maps.travel_durations(A, [B], 1_700_000_000, "driving"),
            await maps.search_nearby(A, 1200, ["cafe"], 3),
        )

    results = asyncio.run(go())
    maps.save()
    return results, inner


def test_a_replay_returns_what_was_recorded(tmp_path):
    path = tmp_path / "case.json"
    (location, leg, durations, places), inner = record(path)

    replay = FixtureMaps(path)

    async def go():
        return (
            await replay.geocode("Ballard"),
            await replay.travel_leg(A, B, "Ballard", 1_700_000_000, "transit"),
            await replay.travel_durations(A, [B], 1_700_000_000, "driving"),
            await replay.search_nearby(A, 1200, ["cafe"], 3),
        )

    again = asyncio.run(go())
    assert again == (location, leg, durations, places)
    assert isinstance(again[0], Location) and isinstance(again[1], Leg)
    # Nothing new reached the upstream client during the replay.
    assert inner.calls == {"geocode": 1, "travel_leg": 1, "travel_durations": 1, "search_nearby": 1}


def test_a_replay_is_free_of_the_wall_clock(tmp_path):
    """Departure is deliberately not part of the key: transit routing needs a
    future timestamp, so a fixture recorded last week must still replay."""
    path = tmp_path / "case.json"
    record(path)
    replay = FixtureMaps(path)
    leg = asyncio.run(replay.travel_leg(A, B, "Ballard", 1_900_000_000, "transit"))
    assert leg.reachable


def test_asking_something_new_fails_loudly(tmp_path):
    """A silent live call here would be a bill nobody asked for."""
    path = tmp_path / "case.json"
    record(path)
    replay = FixtureMaps(path)
    with pytest.raises(FixtureMiss, match="travel_leg"):
        asyncio.run(replay.travel_leg(A, B, "Fremont", 1_700_000_000, "transit"))


def test_the_mode_is_part_of_the_key(tmp_path):
    """Same two points, different journey. Collapsing these would silently
    grade a driver's answer against a bus rider's."""
    path = tmp_path / "case.json"
    record(path)
    replay = FixtureMaps(path)
    with pytest.raises(FixtureMiss):
        asyncio.run(replay.travel_leg(A, B, "Ballard", 1_700_000_000, "walking"))
