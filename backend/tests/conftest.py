"""A fake MapsClient so the whole graph can be exercised without a live key."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.geo import haversine_m  # noqa: E402
from app.state import LatLng, Leg, Location, Person  # noqa: E402


# Rough minutes-per-km and fixed overhead per mode, so the fake behaves the way
# the real APIs do: driving fastest, walking slowest, transit with wait time.
_MODE_COST = {
    "transit": (3.0, 8.0),
    "driving": (1.5, 2.0),
    "bicycling": (3.5, 1.0),
    "walking": (12.0, 0.0),
}


def _minutes(a: LatLng, b: LatLng, mode: str = "transit") -> float:
    per_km, overhead = _MODE_COST.get(mode, _MODE_COST["transit"])
    return round(haversine_m(a, b) / 1000 * per_km + overhead, 1)


class FakeMaps:
    """Duck-types the MapsClient surface the nodes actually use."""

    def __init__(self, *, unreachable: set[str] | None = None, venue_overrides=None,
                 km_per_transfer: float = 6.0):
        self.unreachable = unreachable or set()
        self.km_per_transfer = km_per_transfer
        self.venue_overrides = venue_overrides or {}
        self.calls: dict[str, int] = {}

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    async def geocode(self, address: str) -> Location:
        self._count("geocode")
        known = {
            "ballard": (47.6685, -122.3835),
            "columbia city": (47.5595, -122.2855),
            "redmond": (47.6740, -122.1215),
            "capitol hill": (47.6229, -122.3212),
            "west seattle": (47.5610, -122.3870),
            "greenwood": (47.6905, -122.3550),
        }
        for key, (lat, lng) in known.items():
            if key in address.lower():
                return Location(query=address, label=f"{key.title()}, Seattle, WA", coords=LatLng(lat=lat, lng=lng))
        return Location(query=address, label=address, coords=LatLng(lat=47.62, lng=-122.33))

    async def reverse_geocode(self, point: LatLng) -> str:
        self._count("reverse_geocode")
        return f"Area {point.lat:.2f},{point.lng:.2f}"

    async def travel_durations(self, origin, destinations, departure_time, mode="transit"):
        self._count("travel_durations")
        return [_minutes(origin, d, mode) for d in destinations]

    async def travel_leg(self, origin, destination, name, departure_time, mode="transit") -> Leg:
        self._count("travel_leg")
        if name in self.unreachable:
            return Leg(neighbourhood=name, reachable=False, mode=mode, error="no route")
        km = haversine_m(origin, destination) / 1000
        return Leg(
            neighbourhood=name,
            reachable=True,
            mode=mode,
            duration_min=_minutes(origin, destination, mode),
            # Only transit accrues transfers, exactly as the real client behaves.
            transfers=min(3, int(km // self.km_per_transfer)) if mode == "transit" else 0,
            summary="Bus 40" if mode == "transit" else f"{km:.1f} km {mode}",
        )

    async def search_nearby(self, center, radius_m, included_types, max_results):
        self._count("search_nearby")
        return self._places(center, included_types, max_results)

    def _places(self, center, included_types, max_results):
        key = (round(center.lat, 3), round(center.lng, 3))
        if key in self.venue_overrides:
            return self.venue_overrides[key]
        return [
            {
                "id": f"place-{key}-{i}",
                "displayName": {"text": f"Cafe {i} @ {key[0]}"},
                "formattedAddress": "123 Example St",
                "location": {"latitude": center.lat + i * 0.001, "longitude": center.lng},
                "types": [included_types[0] if included_types else "cafe"],
                "primaryTypeDisplayName": {"text": "Coffee shop"},
                "rating": 4.0 + i * 0.2,
                "userRatingCount": 100 + i * 50,
                "businessStatus": "OPERATIONAL",
                "currentOpeningHours": {"openNow": True},
            }
            for i in range(min(3, max_results))
        ]

    async def search_text(self, center, radius_m, text_query, max_results):
        self._count("search_text")
        # Deliberately does not go through search_nearby, so call counts stay
        # attributable to the endpoint the node actually chose.
        return self._places(center, ["cafe"], max_results)

    async def autocomplete(self, query, session_token, bias, bias_radius_m):
        self._count("autocomplete")
        if len(query.strip()) < 3:
            return []
        return [
            {"place_id": f"pid-{query.lower()}-{i}", "text": f"{query} result {i}",
             "main": f"{query} result {i}", "secondary": "Seattle, WA, USA"}
            for i in range(3)
        ]

    async def place_location(self, place_id, session_token) -> Location:
        self._count("place_location")
        return Location(query=place_id, label=f"Resolved {place_id}",
                        coords=LatLng(lat=47.62, lng=-122.33))

    async def aclose(self) -> None:
        pass


async def make_people(fake: "FakeMaps", *specs) -> list[Person]:
    """specs: (address, mode) tuples, or bare addresses defaulting to transit."""
    people = []
    for i, spec in enumerate(specs):
        address, mode = spec if isinstance(spec, tuple) else (spec, "transit")
        people.append(
            Person(
                label=f"Person {i + 1}",
                location=await fake.geocode(address),
                mode=mode,
            )
        )
    return people


@pytest.fixture
def fake_maps() -> FakeMaps:
    return FakeMaps()


@pytest.fixture
def base_request():
    return {
        "categories": ["coffee"],
        "free_text": "",
        "departure_time": 1_700_000_000,
        "require_open": False,
    }
