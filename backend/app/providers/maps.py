"""Google Maps Platform client: Geocoding, Distance Matrix, Directions, Places (New).

Every call goes through the disk cache (`app.cache`) and a shared semaphore so a
single request can fan out without tripping per-second quotas.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from .. import cache
from ..config import get_settings
from ..geo import bounding_rectangle
from ..state import MODE_LABELS, LatLng, Leg, Location, TravelMode

log = logging.getLogger(__name__)

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"
DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"
PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
PLACES_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_AUTOCOMPLETE_URL = "https://places.googleapis.com/v1/places:autocomplete"
PLACES_DETAILS_URL = "https://places.googleapis.com/v1/places"

# Place Details only needs enough to place a pin and label it.
DETAILS_FIELD_MASK = "id,displayName,formattedAddress,location"

PLACES_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.types",
        "places.primaryTypeDisplayName",
        "places.rating",
        "places.userRatingCount",
        "places.businessStatus",
        "places.currentOpeningHours.openNow",
        "places.priceLevel",
    ]
)

# Google statuses that mean "the request was fine, there is just no answer".
_EMPTY_STATUSES = {"ZERO_RESULTS", "NOT_FOUND"}


class MapsError(RuntimeError):
    """A Maps call failed in a way the caller cannot route around."""


class MapsClient:
    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        settings = get_settings()
        if not settings.maps_enabled:
            raise MapsError(
                "GOOGLE_MAPS_API_KEY is not set. Copy backend/.env.example to "
                "backend/.env and add a key with Geocoding, Directions, Distance "
                "Matrix and Places API (New) enabled."
            )
        self._settings = settings
        self._key = settings.google_maps_api_key
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(timeout=settings.request_timeout_seconds)
        self._sem = asyncio.Semaphore(settings.max_concurrent_requests)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> "MapsClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ raw
    async def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._sem:
            try:
                resp = await self._http.get(url, params={**params, "key": self._key})
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise MapsError(f"{url} request failed: {exc}") from exc
        data = resp.json()
        status = data.get("status", "UNKNOWN")
        if status in _EMPTY_STATUSES:
            return data
        if status != "OK":
            raise MapsError(f"{url} returned {status}: {data.get('error_message', '')}".strip())
        return data

    async def _post_places(
        self, url: str, body: dict[str, Any], field_mask: Optional[str] = PLACES_FIELD_MASK
    ) -> dict[str, Any]:
        headers = {"X-Goog-Api-Key": self._key, "Content-Type": "application/json"}
        if field_mask:
            headers["X-Goog-FieldMask"] = field_mask
        async with self._sem:
            try:
                resp = await self._http.post(url, json=body, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise MapsError(
                    f"Places request failed ({exc.response.status_code}): {exc.response.text[:300]}"
                ) from exc
            except httpx.HTTPError as exc:
                raise MapsError(f"Places request failed: {exc}") from exc
        return resp.json()

    # ------------------------------------------------------------- geocoding
    async def geocode(self, address: str) -> Location:
        key = {"address": address.strip().lower()}
        cached = cache.get("geocode", key)
        if cached is None:
            data = await self._get_json(GEOCODE_URL, {"address": address})
            results = data.get("results") or []
            if not results:
                raise MapsError(f"Could not find a location for {address!r}.")
            top = results[0]
            cached = {
                "label": top.get("formatted_address", address),
                "lat": top["geometry"]["location"]["lat"],
                "lng": top["geometry"]["location"]["lng"],
            }
            cache.put("geocode", key, cached)
        return Location(
            query=address,
            label=cached["label"],
            coords=LatLng(lat=cached["lat"], lng=cached["lng"]),
        )

    async def reverse_geocode(self, point: LatLng) -> str:
        key = {"lat": round(point.lat, 4), "lng": round(point.lng, 4)}
        cached = cache.get("revgeocode", key)
        if cached is None:
            try:
                data = await self._get_json(
                    GEOCODE_URL,
                    {"latlng": f"{point.lat},{point.lng}", "result_type": "neighborhood|locality"},
                )
            except MapsError:
                return f"{point.lat:.3f}, {point.lng:.3f}"
            results = data.get("results") or []
            cached = results[0].get("formatted_address", "").split(",")[0] if results else ""
            cache.put("revgeocode", key, cached)
        return cached or f"{point.lat:.3f}, {point.lng:.3f}"

    # -------------------------------------------------------- distance matrix
    async def travel_durations(
        self,
        origin: LatLng,
        destinations: list[LatLng],
        departure_time: int,
        mode: TravelMode = "transit",
    ) -> list[Optional[float]]:
        """Duration in minutes to each destination in `mode`, `None` where no
        route exists. One request covers every destination, which is why Node 0
        uses this rather than N Directions calls for its coarse sweep."""
        if not destinations:
            return []
        key = {
            "o": [round(origin.lat, 5), round(origin.lng, 5)],
            "d": [[round(d.lat, 5), round(d.lng, 5)] for d in destinations],
            "t": _round_hour(departure_time),
            "m": mode,
        }
        cached = cache.get("matrix", key)
        if cached is None:
            data = await self._get_json(
                DISTANCE_MATRIX_URL,
                {
                    "origins": f"{origin.lat},{origin.lng}",
                    "destinations": "|".join(f"{d.lat},{d.lng}" for d in destinations),
                    "mode": mode,
                    "departure_time": departure_time,
                },
            )
            rows = data.get("rows") or [{}]
            elements = rows[0].get("elements", [])
            cached = [
                (_element_duration(el) / 60.0)
                if el.get("status") == "OK" and _element_duration(el)
                else None
                for el in elements
            ]
            # Pad if Google returned fewer elements than we asked for.
            cached += [None] * (len(destinations) - len(cached))
            cache.put("matrix", key, cached)
        return cached

    # ------------------------------------------------------------- directions
    async def travel_leg(
        self,
        origin: LatLng,
        destination: LatLng,
        name: str,
        departure_time: int,
        mode: TravelMode = "transit",
    ) -> Leg:
        """Full route, including the transfer count the scorer needs (transit only)."""
        key = {
            "o": [round(origin.lat, 5), round(origin.lng, 5)],
            "d": [round(destination.lat, 5), round(destination.lng, 5)],
            "t": _round_hour(departure_time),
            "m": mode,
        }
        cached = cache.get("directions", key)
        if cached is None:
            try:
                data = await self._get_json(
                    DIRECTIONS_URL,
                    {
                        "origin": f"{origin.lat},{origin.lng}",
                        "destination": f"{destination.lat},{destination.lng}",
                        "mode": mode,
                        "departure_time": departure_time,
                        "alternatives": "false",
                    },
                )
            except MapsError as exc:
                # A single unroutable neighbourhood must not sink the request.
                return Leg(neighbourhood=name, reachable=False, mode=mode, error=str(exc))
            cached = _parse_directions(data, mode)
            cache.put("directions", key, cached)
        if cached is None or not cached.get("reachable"):
            return Leg(
                neighbourhood=name,
                reachable=False,
                mode=mode,
                error=f"No {MODE_LABELS.get(mode, mode)} route found.",
            )
        return Leg(
            neighbourhood=name,
            reachable=True,
            mode=mode,
            duration_min=cached["duration_min"],
            transfers=cached["transfers"],
            summary=cached["summary"],
        )

    # ----------------------------------------------------------------- places
    async def search_nearby(
        self, center: LatLng, radius_m: int, included_types: list[str], max_results: int
    ) -> list[dict[str, Any]]:
        body = {
            "includedTypes": included_types,
            "maxResultCount": min(max(max_results, 1), 20),
            "rankPreference": "POPULARITY",
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": center.lat, "longitude": center.lng},
                    "radius": float(radius_m),
                }
            },
        }
        key = {"kind": "nearby", **body}
        cached = cache.get("places", key)
        if cached is None:
            data = await self._post_places(PLACES_NEARBY_URL, body)
            cached = data.get("places", [])
            cache.put("places", key, cached)
        return cached

    async def search_text(
        self, center: LatLng, radius_m: int, text_query: str, max_results: int
    ) -> list[dict[str, Any]]:
        # locationBias is only a hint -- Google will happily return a venue
        # kilometres outside it, which then inherits the wrong neighbourhood's
        # travel time. locationRestriction is a hard boundary, but text search
        # only accepts it as a rectangle.
        low, high = bounding_rectangle(center, radius_m)
        body = {
            "textQuery": text_query,
            "maxResultCount": min(max(max_results, 1), 20),
            "locationRestriction": {
                "rectangle": {
                    "low": {"latitude": low.lat, "longitude": low.lng},
                    "high": {"latitude": high.lat, "longitude": high.lng},
                }
            },
        }
        key = {"kind": "text", **body}
        cached = cache.get("places", key)
        if cached is None:
            data = await self._post_places(PLACES_TEXT_URL, body)
            cached = data.get("places", [])
            cache.put("places", key, cached)
        return cached

    # ---------------------------------------------------------- autocomplete
    async def autocomplete(
        self, query: str, session_token: str, bias: LatLng, bias_radius_m: int
    ) -> list[dict[str, str]]:
        """Address suggestions for a partial query.

        `session_token` is what keeps this affordable: Google bills every
        keystroke request plus the final Details lookup as a single session when
        they share a token. Without one, each request is billed separately.
        """
        query = query.strip()
        if len(query) < 3:
            return []
        # Cached on the query alone -- the session token must never be part of
        # the key, or every session would miss.
        key = {"q": query.lower(), "b": [round(bias.lat, 3), round(bias.lng, 3)]}
        cached = cache.get("autocomplete", key)
        if cached is None:
            body = {
                "input": query,
                "sessionToken": session_token,
                "locationBias": {
                    "circle": {
                        "center": {"latitude": bias.lat, "longitude": bias.lng},
                        "radius": float(bias_radius_m),
                    }
                },
            }
            # Autocomplete does not take a field mask.
            data = await self._post_places(PLACES_AUTOCOMPLETE_URL, body, field_mask=None)
            cached = []
            for suggestion in data.get("suggestions", []):
                prediction = suggestion.get("placePrediction")
                if not prediction:
                    continue  # query predictions have no place to resolve
                structured = prediction.get("structuredFormat", {})
                cached.append(
                    {
                        "place_id": prediction.get("placeId", ""),
                        "text": prediction.get("text", {}).get("text", ""),
                        "main": structured.get("mainText", {}).get("text", ""),
                        "secondary": structured.get("secondaryText", {}).get("text", ""),
                    }
                )
            cache.put("autocomplete", key, cached)
        return cached

    async def place_location(self, place_id: str, session_token: str) -> Location:
        """Resolve a place_id to coordinates, closing the autocomplete session.

        This replaces the Geocoding call for autocompleted addresses -- and it
        cannot be ambiguous the way a free-text address can."""
        key = {"pid": place_id}
        cached = cache.get("details", key)
        if cached is None:
            headers = {
                "X-Goog-Api-Key": self._key,
                "X-Goog-FieldMask": DETAILS_FIELD_MASK,
            }
            params = {"sessionToken": session_token} if session_token else {}
            async with self._sem:
                try:
                    resp = await self._http.get(
                        f"{PLACES_DETAILS_URL}/{place_id}", headers=headers, params=params
                    )
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise MapsError(
                        f"Could not look up that place ({exc.response.status_code})."
                    ) from exc
                except httpx.HTTPError as exc:
                    raise MapsError(f"Place lookup failed: {exc}") from exc
            data = resp.json()
            location = data.get("location") or {}
            if "latitude" not in location:
                raise MapsError("That place has no location on file.")
            cached = {
                "label": data.get("formattedAddress")
                or (data.get("displayName") or {}).get("text", place_id),
                "lat": location["latitude"],
                "lng": location["longitude"],
            }
            cache.put("details", key, cached)
        return Location(
            query=cached["label"],
            label=cached["label"],
            coords=LatLng(lat=cached["lat"], lng=cached["lng"]),
        )



def _round_hour(departure_time: int) -> int:
    """Cache key granularity: transit patterns don't change minute to minute,
    so bucket departures by the hour to get useful cache hits during testing."""
    return departure_time - (departure_time % 3600)


def _element_duration(element: dict[str, Any]) -> float:
    """Prefer traffic-aware duration when Google supplies one (driving mode)."""
    traffic = element.get("duration_in_traffic") or {}
    if traffic.get("value"):
        return traffic["value"]
    return element.get("duration", {}).get("value", 0)


def _parse_directions(data: dict[str, Any], mode: str = "transit") -> Optional[dict[str, Any]]:
    routes = data.get("routes") or []
    if not routes:
        return {"reachable": False}
    legs = routes[0].get("legs") or []
    if not legs:
        return {"reachable": False}
    leg = legs[0]
    steps = leg.get("steps", [])
    transit_steps = [s for s in steps if s.get("travel_mode") == "TRANSIT"]
    duration = _element_duration(leg) / 60.0

    if mode != "transit":
        distance_km = leg.get("distance", {}).get("value", 0) / 1000
        return {
            "reachable": True,
            "duration_min": duration,
            # Only transit has transfers; a drive or a walk is always one leg.
            "transfers": 0,
            "summary": f"{distance_km:.1f} km {MODE_LABELS.get(mode, mode)}",
        }

    lines = []
    for step in transit_steps:
        line = step.get("transit_details", {}).get("line", {})
        lines.append(line.get("short_name") or line.get("name") or "transit")
    return {
        "reachable": True,
        "duration_min": duration,
        # A transfer is a change *between* vehicles: two transit legs = one transfer.
        "transfers": max(0, len(transit_steps) - 1),
        "summary": " → ".join(lines) if lines else "walk",
    }
