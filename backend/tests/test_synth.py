"""Drawing synthetic cases: reproducible, and actually shaped as advertised.

A sampler that quietly produces the same easy case every time would make the
eval set look healthy while testing nothing, so each shape is checked against
the property it exists to create.
"""
from __future__ import annotations

import random

import pytest

from app import anchors
from app.geo import haversine_m
from app.state import LatLng
from evals import synth

from conftest import anchor_rows


@pytest.fixture
def loaded(tmp_path):
    path = tmp_path / "anchors.csv"
    lines = ["name,lat,lng"] + [f"{n},{lat},{lng}" for n, lat, lng in anchor_rows()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return anchors.load(path)


def spread_km(case) -> float:
    points = [LatLng(**p["coords"]) for p in case["people"]]
    return max(haversine_m(points[0], p) for p in points) / 1000


def test_a_seed_reproduces_the_same_cases(loaded):
    first = synth.sample_many(loaded, 6, seed=11)
    assert first == synth.sample_many(loaded, 6, seed=11)
    assert first != synth.sample_many(loaded, 6, seed=12)


def test_every_shape_appears_in_a_run(loaded):
    """Round-robin, not random: a run that skipped the transit case would be
    the one run you needed."""
    cases = synth.sample_many(loaded, len(synth.SHAPES))
    assert {c["shape"] for c in cases} == set(synth.SHAPES)


def test_origins_are_anchors_and_nothing_else(loaded):
    """The whole privacy argument: no coordinate is ever invented or personal."""
    known = {(a.coords.lat, a.coords.lng) for a in loaded.anchors}
    for case in synth.sample_many(loaded, 8, seed=3):
        for person in case["people"]:
            assert (person["coords"]["lat"], person["coords"]["lng"]) in known
            assert person["area_source"] == "stop"


def test_party_sizes_stay_inside_what_the_app_accepts(loaded):
    for case in synth.sample_many(loaded, 12, seed=5):
        assert 2 <= len(case["people"]) <= 5


def test_same_block_puts_everyone_together(loaded):
    for case in synth.sample_many(loaded, 4, seed=7, shapes=["same_block"]):
        assert spread_km(case) < 1.0


def test_one_far_out_strands_somebody(loaded):
    """A case where no answer is fair to everyone, which is where the honest
    failure message has to earn its keep."""
    cases = synth.sample_many(loaded, 4, seed=9, shapes=["one_far_out"])
    assert max(spread_km(c) for c in cases) > 15


def test_all_transit_edges_is_all_transit(loaded):
    for case in synth.sample_many(loaded, 3, seed=2, shapes=["all_transit_edges"]):
        assert {p["mode"] for p in case["people"]} == {"transit"}


def test_mixed_mode_is_reliably_mixed(loaded):
    """Cycled rather than sampled, so it cannot draw two drivers and test
    nothing."""
    for case in synth.sample_many(loaded, 4, seed=4, shapes=["mixed_mode"]):
        assert {p["mode"] for p in case["people"]} == {"driving", "transit"}


def test_it_says_what_to_do_when_there_are_no_anchors():
    with pytest.raises(ValueError, match="fetch_anchors"):
        synth.sample(anchors.EMPTY, rng=random.Random(0))


def test_a_thin_log_leaves_the_defaults_alone():
    """Three searches are one person's habits, not a population."""
    records = [{"request": {"party_size": 5, "people": [{"mode": "walking"}]}}] * 3
    assert synth.Distribution.from_records(records) == synth.Distribution()


def test_a_real_log_replaces_what_it_has_support_for():
    records = [
        {"request": {"party_size": 3, "max_time_min": 60, "max_transfers": 1,
                     "fairness_mode": "total", "free_text": "quiet",
                     "people": [{"mode": "walking"}, {"mode": "walking"}]}}
    ] * 25
    learned = synth.Distribution.from_records(records)
    assert learned.party_sizes == {3: 1.0}
    assert learned.modes == {"walking": 1.0}
    assert learned.free_text_rate == 1.0
    assert list(learned.max_time_min) == [60] * 25
    # Nothing in the log speaks to these, so the defaults survive.
    assert learned.weights == synth.Distribution().weights
