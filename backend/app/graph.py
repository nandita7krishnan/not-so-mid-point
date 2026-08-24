"""The agent graph.

    Node 0 (search area)
        |-> reach_person1 --|
        |-> ...             |-> Node C (shortlist)
        |-> reach_person5 --|      -> Node D (spot finder)
                                   -> Node E (scorer)
                                   -> Node F (reviewer) -> END

The reachability nodes are edges out of the same node into the same fan-in, so
LangGraph runs them in one superstep. Slots beyond the party count return
immediately. Every key written concurrently (`reachability`, `warnings`,
`timings`) carries a reducer in MeetingState.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from .nodes.reachability import REACHABILITY_NODES
from .nodes.reviewer import reviewer_node
from .nodes.scorer import scorer_node
from .nodes.search_area import search_area_node
from .nodes.shortlist import shortlist_node
from .nodes.spot_finder import spot_finder_node
from .runtime import RunDeps
from .state import MeetingState


def build_graph():
    graph = StateGraph(MeetingState)
    graph.add_node("node0_search_area", search_area_node)
    for index, node in enumerate(REACHABILITY_NODES):
        graph.add_node(f"reach_person{index + 1}", node)
    graph.add_node("nodeC_shortlist", shortlist_node)
    graph.add_node("nodeD_spots", spot_finder_node)
    graph.add_node("nodeE_score", scorer_node)
    graph.add_node("nodeF_review", reviewer_node)

    graph.add_edge(START, "node0_search_area")
    # Every reachability node hangs off Node 0 and feeds the same fan-in, so
    # they all execute in one superstep.
    for index in range(len(REACHABILITY_NODES)):
        graph.add_edge("node0_search_area", f"reach_person{index + 1}")
        graph.add_edge(f"reach_person{index + 1}", "nodeC_shortlist")
    graph.add_edge("nodeC_shortlist", "nodeD_spots")
    graph.add_edge("nodeD_spots", "nodeE_score")
    graph.add_edge("nodeE_score", "nodeF_review")
    graph.add_edge("nodeF_review", END)
    return graph.compile()


COMPILED = build_graph()


async def run_graph(initial: dict[str, Any], deps: RunDeps) -> MeetingState:
    started = time.perf_counter()
    # Seed every accumulating key so a short-circuited run still returns a
    # predictably shaped state instead of missing keys.
    seed = {
        "warnings": [],
        "timings": {},
        "failure": None,
        "reachability": {},
        "shortlisted_neighbourhoods": [],
        "candidate_venues": [],
        "ranked_venues": [],
        "final_top_3": [],
    }
    result = await COMPILED.ainvoke(
        {**seed, **initial},
        config={"configurable": {"deps": deps}},
    )
    result.setdefault("timings", {})["total"] = time.perf_counter() - started
    return result
