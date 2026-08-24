"""Nodes A and B -- per-person reachability.

One Directions (transit) call per candidate neighbourhood, per person, all
fanned out concurrently. A and B write to different state keys and never touch
each other's, which is what lets LangGraph run them in parallel.
"""
from __future__ import annotations

import asyncio
import time

from ..runtime import deps_from_config
from ..state import Leg, MeetingState


async def _reachability(state: MeetingState, config: dict, person: int) -> dict:
    started = time.perf_counter()
    if state.get("failure"):
        return {}

    deps = deps_from_config(config)
    location = state[f"person{person}_location"]
    mode = state.get(f"person{person}_mode", "transit")
    area = state["search_area"]
    departure = state["departure_time"]

    legs: list[Leg] = list(
        await asyncio.gather(
            *(
                deps.maps.travel_leg(
                    location.coords, cand.coords, cand.name, departure, mode
                )
                for cand in area.candidates
            )
        )
    )
    key = f"person{person}_reachability"
    return {key: legs, "timings": {key: time.perf_counter() - started}}


async def person1_reachability_node(state: MeetingState, config: dict) -> dict:
    return await _reachability(state, config, 1)


async def person2_reachability_node(state: MeetingState, config: dict) -> dict:
    return await _reachability(state, config, 2)
