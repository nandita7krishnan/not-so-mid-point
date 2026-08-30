"""Deterministic checks over one logged search.

These run first and cost nothing. They catch the failures that are matters of
fact rather than taste -- a result over the stated budget, a ranking where all
three picks sit on the same street -- and a model judge should never be asked
to adjudicate something arithmetic.

Every check returns either a bool (a contract the answer either honoured or
broke) or a number (a property worth trending). Bools that come back False are
the ones worth looking at; `violations` collects them.
"""
from __future__ import annotations

from typing import Any, Optional

# Checks whose False value means the answer broke a promise the UI made, rather
# than merely scoring poorly.
CONTRACTS = (
    "budget_respected",
    "transfers_respected",
    "all_legs_reachable",
    "ratings_present",
    "explanations_present",
)


def _results(record: dict[str, Any]) -> list[dict[str, Any]]:
    return record.get("response", {}).get("results", [])


def _legs(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result.get("journey", {}).get("legs", [])


def run(record: dict[str, Any]) -> dict[str, Any]:
    request = record.get("request", {})
    response = record.get("response", {})
    results = _results(record)
    ok = response.get("ok", False)

    checks: dict[str, Any] = {
        "ok": ok,
        "party_size": request.get("party_size", 0),
        "result_count": len(results),
        "latency_s": response.get("timings", {}).get("total"),
    }

    if not ok:
        failure = response.get("failure") or {}
        checks.update(
            {
                # A refusal is only a good refusal if it says what to change.
                "failure_has_suggestion": bool(failure.get("suggestion")),
                "failure_node": failure.get("node", ""),
                # Nothing survived the shortlist, or nothing survived scoring?
                # The distinction is the whole difference between "your budget
                # is too tight" and "we found nowhere to go".
                "shortlist_was_empty": not response.get("shortlist"),
            }
        )
        return checks

    budget = request.get("max_time_min", 0)
    max_transfers = request.get("max_transfers", 0)
    all_legs = [leg for result in results for leg in _legs(result)]

    checks.update(
        {
            "budget_respected": all(
                leg.get("duration_min", 0) <= budget for leg in all_legs
            ),
            "transfers_respected": all(
                leg.get("transfers", 0) <= max_transfers
                for leg in all_legs
                if leg.get("mode") == "transit"
            ),
            "all_legs_reachable": all(leg.get("reachable", False) for leg in all_legs),
            # Ranking is built on Google ratings, so a pick without one was
            # ranked on something weaker than the method claims.
            "ratings_present": all(r.get("rating") is not None for r in results),
            "explanations_present": all(
                (r.get("why") or "").strip() for r in results
            ),
            # Three picks on one block is technically a ranking and practically
            # a single suggestion.
            "distinct_neighbourhoods": len(
                {r.get("neighbourhood") for r in results}
            ),
            "distinct_categories": len({r.get("category") for r in results}),
            "top_spread_min": _top(results, lambda r: r["journey"]["gap_min"]),
            "top_worst_leg_min": _top(
                results,
                lambda r: max((leg.get("duration_min", 0) for leg in _legs(r)), default=0),
            ),
            "top_total_min": _top(results, lambda r: r["journey"]["total_min"]),
            "top_final_score": _top(results, lambda r: r["scores"]["final"]),
            # Headroom the answer left unused: a spread of 4 min against a
            # 45 min budget is a different result from 4 min against 15.
            "budget_headroom_min": _headroom(results, budget),
            # Components the scorer dropped because every candidate scored
            # alike. Persistently inactive weights mean a slider that does
            # nothing, which is worth knowing before a user finds out.
            "inactive_components": (
                results[0].get("scores", {}).get("inactive", []) if results else []
            ),
            "warnings": len(response.get("warnings", [])),
        }
    )
    checks["violations"] = [
        name for name in CONTRACTS if checks.get(name) is False
    ]
    return checks


def _top(results: list[dict[str, Any]], pick) -> Optional[float]:
    if not results:
        return None
    try:
        return round(float(pick(results[0])), 2)
    except (KeyError, TypeError, ValueError):
        return None


def _headroom(results: list[dict[str, Any]], budget: int) -> Optional[float]:
    if not results or not budget:
        return None
    worst = max(
        (leg.get("duration_min", 0) for leg in _legs(results[0])), default=None
    )
    return round(budget - worst, 1) if worst is not None else None
