"""Per-person reachability -- one node per possible party.

One routing call per candidate neighbourhood, per person, fanned out
concurrently. There is a static node per party slot (up to MAX_PARTIES) rather
than a single looping node, so LangGraph still runs them all in one superstep --
the genuine parallelism the graph exists for. A slot with nobody in it returns
immediately and costs nothing.

Each node writes only its own index into the `reachability` dict, which carries
a union reducer, so concurrent writes merge instead of racing.
"""
from __future__ import annotations

import asyncio
import time

from ..runtime import deps_from_config
from ..state import MAX_PARTIES, Leg, MeetingState


async def _reachability(state: MeetingState, config: dict, index: int) -> dict:
    if state.get("failure"):
        return {}
    people = state.get("people", [])
    if index >= len(people):
        return {}  # unused party slot

    started = time.perf_counter()
    deps = deps_from_config(config)
    person = people[index]
    area = state["search_area"]
    departure = state["departure_time"]

    legs: list[Leg] = list(
        await asyncio.gather(
            *(
                deps.maps.travel_leg(
                    person.location.coords,
                    cand.coords,
                    cand.name,
                    departure,
                    person.mode,
                )
                for cand in area.candidates
            )
        )
    )
    return {
        "reachability": {index: legs},
        "timings": {f"reachability_{index + 1}": time.perf_counter() - started},
    }


def _make_node(index: int):
    async def node(state: MeetingState, config: dict) -> dict:
        return await _reachability(state, config, index)

    node.__name__ = f"person{index + 1}_reachability_node"
    return node


# person1_reachability_node ... person5_reachability_node
REACHABILITY_NODES = [_make_node(i) for i in range(MAX_PARTIES)]
