"""Usage counters: are they accurate, and do they leak anything?"""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import get_settings
from app.stats import Stats


def test_visitors_are_counted_by_hash_not_address():
    s = Stats(salt="fixed")
    vid = s.visitor_id("203.0.113.9")
    assert "203.0.113.9" not in vid
    assert len(vid) == 12
    # Same visitor, same id; different visitor, different id.
    assert vid == s.visitor_id("203.0.113.9")
    assert vid != s.visitor_id("203.0.113.10")


def test_a_different_salt_produces_a_different_id():
    """Hashes cannot be correlated across deployments or with other systems."""
    assert Stats(salt="a").visitor_id("1.1.1.1") != Stats(salt="b").visitor_id("1.1.1.1")


def test_repeat_visitors_are_counted_once_per_day():
    s = Stats(salt="fixed")
    for _ in range(5):
        s.record("searches_ok", "1.1.1.1", day="2026-08-26")
    day = s.snapshot()["days"][0]
    assert day["visitors"] == 1
    assert day["searches_ok"] == 5


def test_blocked_percentage_is_share_of_attempts():
    s = Stats(salt="fixed")
    for _ in range(3):
        s.record("searches_ok", "1.1.1.1", day="2026-08-26")
    s.record("blocked_global", "2.2.2.2", day="2026-08-26")
    day = s.snapshot()["days"][0]
    assert day["searches_attempted"] == 4
    assert day["blocked_total"] == 1
    assert day["blocked_pct"] == 25.0


def test_the_two_block_reasons_are_distinguished():
    """'Everyone is locked out for the day' and 'you personally are going too
    fast' mean very different things for whether to raise the cap."""
    s = Stats(salt="fixed")
    s.record("blocked_personal", "1.1.1.1", day="2026-08-26")
    s.record("blocked_global", "2.2.2.2", day="2026-08-26")
    day = s.snapshot()["days"][0]
    assert day["blocked_personal"] == 1
    assert day["blocked_daily_cap"] == 1


def test_visitors_are_not_summed_across_days():
    """Summing daily uniques would double count anyone who came back."""
    s = Stats(salt="fixed")
    s.record("searches_ok", "1.1.1.1", day="2026-08-25")
    s.record("searches_ok", "1.1.1.1", day="2026-08-26")
    assert "visitors" not in s.snapshot()["totals"]


def test_old_days_are_pruned():
    s = Stats(salt="fixed")
    for d in range(1, 21):
        s.record("searches_ok", "1.1.1.1", day=f"2026-08-{d:02d}")
    assert len(s.snapshot(days=14)["days"]) <= 14


def test_an_unknown_event_is_ignored_not_raised():
    """Metrics must never be able to break a request."""
    s = Stats(salt="fixed")
    s.record("not_a_real_counter", "1.1.1.1", day="2026-08-26")
    assert s.snapshot()["days"][0]["searches_attempted"] == 0


@pytest.fixture
def client(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RECOMMEND_PER_HOUR", "1")
    main._limiter = type(main._limiter)()
    main.STATS = Stats(salt="fixed")
    yield TestClient(main.app)
    get_settings.cache_clear()


def test_stats_endpoint_reports_blocked_requests(client):
    payload = {"people": [{"address": "Ballard, Seattle"}, {"address": "Columbia City, Seattle"}]}
    client.post("/api/recommend", json=payload)      # 503, no key -> counted
    blocked = client.post("/api/recommend", json=payload)
    assert blocked.status_code == 429

    body = client.get("/api/stats").json()
    assert body["totals"]["blocked_personal"] == 1
    assert body["days"][-1]["visitors"] >= 1


def test_stats_endpoint_exposes_no_addresses(client):
    client.post("/api/places/autocomplete", json={"q": "ballard"})
    raw = client.get("/api/stats").text
    assert "testclient" not in raw
    assert "127.0.0.1" not in raw
