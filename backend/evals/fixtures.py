"""Record a case's Maps traffic once, then replay it for free.

Every Places search costs real money against a 1,000-call monthly allowance,
so grading the scorer twice must not cost twice. A synthetic case is the same
search every time it runs -- exact coordinates, fixed departure -- which means
its Maps responses can be frozen the first time and served from disk forever
after. Change the scoring, re-run the whole suite, spend nothing.

This only works because the origins are exact. A logged real search is rounded
to ~1.1 km, so replaying one is not replaying the search that happened; the
frozen answers would belong to a different question.

A replay miss is deliberately an error rather than a live call. If the graph
starts asking something the fixture does not hold, that is a change in what the
graph does, and it should show up as a loud failure rather than a silent bill.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from app.state import Leg, Location, LatLng

_MODELS = {"Location": Location, "Leg": Leg}


class FixtureMiss(RuntimeError):
    """The graph asked something the fixture does not hold."""


def _point(value: Any) -> Any:
    """Coordinates at 5dp (~1 m), which is finer than any answer depends on."""
    if isinstance(value, LatLng):
        return [round(value.lat, 5), round(value.lng, 5)]
    if isinstance(value, (list, tuple)):
        return [_point(item) for item in value]
    return value


def _key(method: str, *args: Any) -> str:
    # `departure_time` is deliberately not part of the key. A case fixes its
    # own departure, so it never varies within a fixture, and leaving it out
    # means a fixture recorded yesterday still replays today.
    return json.dumps([method, *[_point(a) for a in args]], sort_keys=True, default=str)


def _encode(value: Any) -> Any:
    if isinstance(value, (Location, Leg)):
        return {"_model": type(value).__name__, "data": value.model_dump()}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and "_model" in value:
        return _MODELS[value["_model"]](**value["data"])
    return value


class FixtureMaps:
    """Duck-types the MapsClient surface the graph uses.

    With `inner`, every call goes upstream and the answer is kept. Without one,
    answers come from the file and nothing touches the network.
    """

    def __init__(self, path: Path | str, inner: Optional[Any] = None) -> None:
        self.path = Path(path)
        self.inner = inner
        self.calls: dict[str, int] = {}
        self.entries: dict[str, Any] = {}
        if inner is None:
            self.entries = json.loads(self.path.read_text(encoding="utf-8"))

    @property
    def recording(self) -> bool:
        return self.inner is not None

    async def _call(self, method: str, key_args: tuple[Any, ...], *args: Any, **kwargs: Any) -> Any:
        key = _key(method, *key_args)
        self.calls[method] = self.calls.get(method, 0) + 1
        if self.recording:
            result = await getattr(self.inner, method)(*args, **kwargs)
            self.entries[key] = _encode(result)
            return result
        if key not in self.entries:
            raise FixtureMiss(f"{method} not in {self.path.name}: {key}")
        stored = self.entries[key]
        if isinstance(stored, list):
            return [_decode(item) for item in stored]
        return _decode(stored)

    async def geocode(self, address: str) -> Location:
        return await self._call("geocode", (address,), address)

    async def reverse_geocode(self, point: LatLng) -> str:
        return await self._call("reverse_geocode", (point,), point)

    async def travel_durations(self, origin, destinations, departure_time, mode="transit"):
        return await self._call(
            "travel_durations", (origin, destinations, mode),
            origin, destinations, departure_time, mode,
        )

    async def travel_leg(self, origin, destination, name, departure_time, mode="transit") -> Leg:
        return await self._call(
            "travel_leg", (origin, destination, name, mode),
            origin, destination, name, departure_time, mode,
        )

    async def search_nearby(self, center, radius_m, included_types, max_results):
        return await self._call(
            "search_nearby", (center, radius_m, sorted(included_types), max_results),
            center, radius_m, included_types, max_results,
        )

    async def search_text(self, center, radius_m, text_query, max_results):
        return await self._call(
            "search_text", (center, radius_m, text_query, max_results),
            center, radius_m, text_query, max_results,
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.entries, indent=1, sort_keys=True, default=str),
            encoding="utf-8",
        )

    async def aclose(self) -> None:
        if self.inner is not None:
            await self.inner.aclose()
