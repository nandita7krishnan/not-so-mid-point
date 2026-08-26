"""The limiter as wired into the API: does a real request get a 429?"""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import get_settings


@pytest.fixture
def client(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RECOMMEND_PER_HOUR", "2")
    monkeypatch.setenv("RECOMMEND_PER_DAY", "50")
    monkeypatch.setenv("GLOBAL_RECOMMEND_PER_DAY", "500")
    monkeypatch.setenv("AUTOCOMPLETE_PER_MINUTE", "3")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "")  # keep it offline
    main._limiter = type(main._limiter)()
    yield TestClient(main.app)
    get_settings.cache_clear()


def _payload():
    return {
        "people": [
            {"address": "Ballard, Seattle, WA"},
            {"address": "Columbia City, Seattle, WA"},
        ]
    }


def test_recommend_returns_429_once_the_hourly_limit_is_spent(client):
    # No Maps key, so these 503 -- but they still consume the allowance, which
    # is what matters: a failing request costs us the same rate budget.
    for _ in range(2):
        assert client.post("/api/recommend", json=_payload()).status_code == 503

    blocked = client.post("/api/recommend", json=_payload())
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    assert int(blocked.headers["Retry-After"]) > 0
    assert "Too many searches" in blocked.json()["detail"]


def test_autocomplete_has_its_own_looser_allowance(client):
    for _ in range(3):
        assert client.post("/api/places/autocomplete", json={"q": "ball"}).status_code == 200
    assert client.post("/api/places/autocomplete", json={"q": "ball"}).status_code == 429


def test_the_two_endpoints_do_not_share_a_budget(client):
    """Typing an address must not eat into the search allowance."""
    for _ in range(3):
        client.post("/api/places/autocomplete", json={"q": "ball"})
    assert client.post("/api/places/autocomplete", json={"q": "ball"}).status_code == 429
    # Searching is still permitted.
    assert client.post("/api/recommend", json=_payload()).status_code == 503


def test_health_is_never_rate_limited(client):
    for _ in range(20):
        assert client.get("/api/health").status_code == 200


def test_health_reports_whether_limiting_is_on(client):
    assert client.get("/api/health").json()["rate_limited"] is True


def test_limiting_is_off_by_default_for_local_development(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.delenv("RATE_LIMIT_ENABLED", raising=False)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "")
    main._limiter = type(main._limiter)()
    local = TestClient(main.app)
    for _ in range(15):
        assert local.post("/api/recommend", json=_payload()).status_code == 503
    get_settings.cache_clear()
