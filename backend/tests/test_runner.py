"""The whole synthetic path, end to end, against the fake Maps client.

Sample cases, run them through the real graph, freeze the traffic, replay it,
and hand the records to the same checks a real search gets. If this passes, the
pieces fit: what comes out the far end is a record `judge.py` can grade without
knowing it was synthetic.
"""
from __future__ import annotations

import asyncio

import pytest

from app import anchors
from evals import checks, runner, synth

from conftest import FakeMaps, anchor_rows


@pytest.fixture
def cases(tmp_path):
    path = tmp_path / "anchors.csv"
    lines = ["name,lat,lng"] + [f"{n},{lat},{lng}" for n, lat, lng in anchor_rows()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return synth.sample_many(anchors.load(path), 3, seed=1, shapes=["typical", "mixed_mode"])


def run(cases, tmp_path, *, record_mode, monkeypatch):
    monkeypatch.setattr(runner, "MapsClient", FakeMaps)
    return asyncio.run(
        runner.run_all(
            cases, record_mode=record_mode, fixture_dir=tmp_path / "fixtures"
        )
    )


def test_a_recorded_run_produces_gradeable_records(cases, tmp_path, monkeypatch):
    records = run(cases, tmp_path, record_mode=True, monkeypatch=monkeypatch)

    assert len(records) == 3
    for record in records:
        assert record["synthetic"] is True
        assert record["shape"] in synth.SHAPES
        # No visitor, because there was no visitor.
        assert record["visitor"] == ""
        result = checks.run(record)
        assert result["party_size"] >= 2
        assert "violations" not in result or isinstance(result["violations"], list)


def test_the_exact_origins_survive_into_the_record(cases, tmp_path, monkeypatch):
    """The point of the whole exercise: no rounding, because there is nothing
    personal to round."""
    records = run(cases, tmp_path, record_mode=True, monkeypatch=monkeypatch)
    for case, record in zip(cases, records):
        assert record["request"]["people"] == case["people"]


def test_a_replay_needs_no_maps_client_at_all(cases, tmp_path, monkeypatch):
    """The cost argument. Once frozen, re-grading spends nothing."""
    run(cases, tmp_path, record_mode=True, monkeypatch=monkeypatch)

    def explode():
        raise AssertionError("replay must not build a Maps client")

    monkeypatch.setattr(runner, "MapsClient", explode)
    records = asyncio.run(
        runner.run_all(cases, record_mode=False, fixture_dir=tmp_path / "fixtures")
    )
    assert len(records) == 3


def test_a_replay_answers_the_same_way_as_the_recording(cases, tmp_path, monkeypatch):
    """Otherwise a diff after a scoring change is measuring the wrong thing."""
    first = run(cases, tmp_path, record_mode=True, monkeypatch=monkeypatch)
    second = asyncio.run(
        runner.run_all(cases, record_mode=False, fixture_dir=tmp_path / "fixtures")
    )
    for before, after in zip(first, second):
        assert _picks(before) == _picks(after)


def _picks(record):
    return [(r["name"], r["neighbourhood"]) for r in record["response"]["results"]]
