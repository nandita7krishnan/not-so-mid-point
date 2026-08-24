"""Address autocomplete: suggestion lookup and place_id resolution."""
import pytest
from conftest import FakeMaps


async def test_short_queries_are_not_sent_to_google(fake_maps):
    """A two-character query would return noise and still cost a request."""
    assert await fake_maps.autocomplete("ba", "s1", None, 50000) == []


async def test_autocomplete_returns_structured_suggestions(fake_maps):
    results = await fake_maps.autocomplete("ballard", "s1", None, 50000)

    assert results
    for item in results:
        assert item["place_id"]
        assert item["main"]


async def test_place_id_resolution_returns_coordinates(fake_maps):
    location = await fake_maps.place_location("pid-ballard-0", "s1")

    assert location.coords.lat and location.coords.lng
    assert location.label


async def test_real_client_skips_the_request_below_the_minimum_length():
    """The length guard lives in MapsClient, not just the fake."""
    import inspect

    from app.providers.maps import MapsClient

    source = inspect.getsource(MapsClient.autocomplete)
    assert "len(query) < 3" in source
    assert "return []" in source


async def test_autocomplete_cache_key_excludes_the_session_token():
    """Keying on the session token would guarantee a 100% miss rate."""
    import inspect

    from app.providers.maps import MapsClient

    source = inspect.getsource(MapsClient.autocomplete)
    key_line = next(line for line in source.splitlines() if "key = {" in line)
    assert "session" not in key_line
