"""Reading records back out of a host's log stream."""
from __future__ import annotations

import json

from app.searchlog import STDOUT_MARKER
from evals import from_logs

RECORD = {"id": "abc123", "request": {"party_size": 2}, "response": {"ok": True}}


def render_line(record) -> str:
    """What Render's log viewer actually gives you: its own prefix, then ours."""
    return f'Aug 29 12:00:01 PM INFO app.searchlog: {STDOUT_MARKER} {json.dumps(record)}'


def test_it_finds_records_among_ordinary_log_noise():
    lines = [
        "==> Deploying...",
        'INFO:     10.0.0.1:0 - "POST /api/recommend HTTP/1.1" 200 OK',
        render_line(RECORD),
        "Traceback (most recent call last):",
    ]
    assert list(from_logs.extract(lines)) == [RECORD]


def test_a_truncated_line_costs_that_record_only():
    good = render_line(RECORD)
    truncated = render_line(RECORD)[:-20]
    assert list(from_logs.extract([truncated, good])) == [RECORD]


def test_overlapping_downloads_are_safe_to_concatenate():
    other = {"id": "def456", "request": {"party_size": 3}}
    records = from_logs.dedupe(
        from_logs.extract([render_line(RECORD), render_line(other), render_line(RECORD)])
    )
    assert [r["id"] for r in records] == ["abc123", "def456"]


def test_a_line_that_only_mentions_the_marker_is_not_a_record():
    assert list(from_logs.extract([f"grep {STDOUT_MARKER} to find them"])) == []
