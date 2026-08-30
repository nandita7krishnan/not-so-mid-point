"""Pulling records off Render, without a Render.

The network half is one function; everything worth testing is the paging, the
merging and the idempotence, so the API is handed in as a fake.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.searchlog import STDOUT_MARKER
from evals import dataset, pull


def record(rid: str, day: str = "2026-08-30") -> dict:
    return {"id": rid, "ts": f"{day}T02:43:44Z", "request": {"party_size": 2}}


def line(rid: str, day: str = "2026-08-30") -> str:
    return f"Aug 30 02:43:44 AM INFO app.searchlog: {STDOUT_MARKER} {json.dumps(record(rid, day))}"


def test_since_accepts_hours_and_days():
    assert pull.parse_since("24h") == timedelta(hours=24)
    assert pull.parse_since("7d") == timedelta(days=7)


def test_a_bad_since_says_so_rather_than_guessing():
    with pytest.raises(pull.PullError, match="24h or 7d"):
        pull.parse_since("last tuesday")


def test_messages_survive_a_payload_it_did_not_expect():
    """A schema change elsewhere in the response should not stop a pull."""
    assert pull.messages({"logs": [{"message": "a"}, {"noMessage": 1}]}) == ["a"]
    assert pull.messages([{"message": "a"}, "b"]) == ["a", "b"]
    assert pull.messages({"unexpected": True}) == []


def test_it_follows_the_pagination_cursor():
    pages = [
        {"logs": [{"message": line("aaa")}], "hasMore": True, "nextEndTime": "2026-08-30T01:00:00Z"},
        {"logs": [{"message": line("bbb")}], "hasMore": False},
    ]
    seen = []

    def fake_get(url, key):
        seen.append(url)
        return pages[len(seen) - 1]

    now = datetime.now(timezone.utc)
    out = list(pull.fetch_pages(service_id="srv-1", key="k", start=now - timedelta(hours=1),
                                end=now, get=fake_get))
    assert len(out) == 2
    assert "2026-08-30T01%3A00%3A00Z" in seen[1]


def test_it_stops_when_the_cursor_stops_moving():
    """A cursor that never advances would otherwise spin until MAX_PAGES."""
    calls = []

    def fake_get(url, key):
        calls.append(url)
        return {"logs": [], "hasMore": True, "nextEndTime": "2026-08-30T02:00:00Z"}

    end = datetime(2026, 8, 30, 2, tzinfo=timezone.utc)
    list(pull.fetch_pages(service_id="srv-1", key="k", start=end - timedelta(hours=1),
                          end=end, get=fake_get))
    assert len(calls) == 1


def test_records_land_in_one_file_per_day(tmp_path):
    added, total = pull.merge(tmp_path, [record("aaa"), record("bbb", "2026-08-29")])
    assert (added, total) == (2, 2)
    assert {p.name for p in tmp_path.glob("*.jsonl")} == {
        "searches-2026-08-30.jsonl", "searches-2026-08-29.jsonl"
    }


def test_a_second_pull_of_the_same_window_changes_nothing(tmp_path):
    """Cron overlaps windows constantly; a re-run has to be a no-op."""
    pull.merge(tmp_path, [record("aaa"), record("bbb")])
    added, total = pull.merge(tmp_path, [record("aaa"), record("bbb")])
    assert (added, total) == (0, 2)


def test_what_is_already_on_disk_is_never_rewritten(tmp_path):
    """The local copy is the durable one, so a pull adds to it and no more."""
    kept = record("aaa")
    kept["note"] = "annotated by hand"
    dataset.write(tmp_path / "searches-2026-08-30.jsonl", [kept])

    pull.merge(tmp_path, [record("aaa"), record("ccc")])
    on_disk = {r["id"]: r for r in dataset.read(tmp_path / "searches-2026-08-30.jsonl")}
    assert on_disk["aaa"]["note"] == "annotated by hand"
    assert set(on_disk) == {"aaa", "ccc"}


def test_a_record_without_a_timestamp_still_lands_somewhere(tmp_path):
    pull.merge(tmp_path, [{"id": "aaa", "request": {}}])
    assert (tmp_path / "searches-undated.jsonl").exists()
