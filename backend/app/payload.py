"""The `/api/recommend` response body, built in one place.

The eval runner drives the graph directly rather than through HTTP, and it has
to grade the same object a browser would receive. Building the payload here
means the two cannot drift: a field added for the UI shows up in the eval
records without anyone remembering to add it twice.
"""
from __future__ import annotations

from typing import Any, Optional

from .state import uses_transit


def dump(model: Any) -> Optional[dict[str, Any]]:
    return model.model_dump() if model is not None else None


def build(
    *,
    state: dict[str, Any],
    people: list[Any],
    request: Any,
    departure: int,
) -> dict[str, Any]:
    failure = state.get("failure")
    return {
        "ok": failure is None,
        "people": [person.model_dump() for person in people],
        "departure_time": departure,
        "fairness_mode": request.fairness_mode,
        "transfers_apply": uses_transit(*(person.mode for person in people)),
        "weights": request.weights.normalized().model_dump(),
        "search_area": dump(state.get("search_area")),
        "shortlist": [e.model_dump() for e in state.get("shortlisted_neighbourhoods", [])],
        "preference_spec": dump(state.get("preference_spec")),
        "results": [r.model_dump() for r in state.get("final_top_3", [])],
        "failure": dump(failure),
        "warnings": state.get("warnings", []),
        "timings": {k: round(v, 3) for k, v in state.get("timings", {}).items()},
    }
