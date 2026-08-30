"""Grade logged searches with a model, and summarise the result.

    cd backend
    python -m evals.judge                    # grade today's log
    python -m evals.judge --checks-only      # deterministic only, no API calls
    python -m evals.judge evals/log/searches-2026-08-29.jsonl --limit 20

Two things guard against the usual trap of a judge that agrees with whatever it
is shown. The deterministic checks in `checks.py` run first and are never
overridden by the model -- if a pick breaks the travel budget, that is settled
before the model is consulted. And the model is shown the journey numbers rather
than the app's own scores for fairness, so it is judging the outcome instead of
grading the scorer's homework.

Grades are advisory. They are for spotting which searches to go and look at, and
for noticing a trend across a fortnight, not for gating anything.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from evals import checks, dataset  # noqa: E402

# Criteria scored 1-5 on a served answer. Kept short and non-overlapping: a
# rubric with eight near-synonyms produces eight copies of one opinion.
CRITERIA = ("preference_fit", "fairness", "spot_quality", "explanation")
FAILURE_CRITERIA = ("refusal_correct", "suggestion_actionable")

SYSTEM = """\
You are evaluating a tool that suggests where a group should meet. It measures \
each person's real travel time and transfers to candidate areas, then ranks \
venues on fairness, how well they match what the group asked for, and transfer \
count.

Score each criterion 1-5, where 1 is a clear failure, 3 is acceptable, and 5 is \
what an attentive local would have suggested. Judge only from the data given. \
Do not assume a venue has qualities that are not listed, and do not reward an \
answer for being confidently worded.

Be willing to give low scores. An evaluation where everything scores 4 tells \
the developer nothing.\
"""


class Criterion(BaseModel):
    score: int = Field(ge=1, le=5)
    reason: str = Field("", description="At most 25 words, citing specifics.")


class Judgement(BaseModel):
    preference_fit: Criterion = Field(
        description="Do the suggested venues match the categories and free text asked for?"
    )
    fairness: Criterion = Field(
        description=(
            "Given each person's mode and travel time, is the burden defensibly "
            "shared? Judge the journeys, not the app's own fairness score."
        )
    )
    spot_quality: Criterion = Field(
        description="Would a group actually want to meet at these places?"
    )
    explanation: Criterion = Field(
        description=(
            "Is each 'why' line accurate to the journey data, and free of "
            "invented detail about the venue?"
        )
    )
    overall: int = Field(ge=1, le=5, description="Holistic verdict, not an average.")
    notes: str = Field("", description="At most 30 words on the single biggest issue.")


class FailureJudgement(BaseModel):
    refusal_correct: Criterion = Field(
        description="Given the constraints, was returning nothing the right call?"
    )
    suggestion_actionable: Criterion = Field(
        description="Does it tell the user something specific they could change?"
    )
    overall: int = Field(ge=1, le=5)
    notes: str = Field("", description="At most 30 words.")


def render(record: dict[str, Any]) -> str:
    """The case as prose. A model judges a readable brief better than raw JSON."""
    request = record["request"]
    response = record["response"]
    lines: list[str] = ["THE GROUP"]
    for person in request["people"]:
        # "near" is not padding: the area is the closest seeded neighbourhood to
        # a deliberately rounded coordinate, and the judge should not read it as
        # an exact starting point.
        where = (
            f"near {person['area']}"
            if person.get("area_source") == "seed"
            else f"in {person['area']}"
        )
        lines.append(f"  {person['id']}: starting {where}, travelling by {person['mode']}")

    asked = request.get("free_text") or ", ".join(request.get("categories") or []) or "anywhere"
    weights = request["weights"]
    lines += [
        "",
        "WHAT THEY ASKED FOR",
        f"  Looking for: {asked}",
        f"  Limits: at most {request['max_time_min']} min travel, "
        f"at most {request['max_transfers']} transfers",
        f"  Fairness setting: {'minimise the spread between people' if request['fairness_mode'] == 'gap' else 'minimise total travel'}",
        f"  Priorities: fairness {weights['fairness']:.0%}, "
        f"preference {weights['preference']:.0%}, transfers {weights['transfers']:.0%}",
    ]
    if request.get("require_open"):
        lines.append("  Must be open at the time of the search.")

    if not response["ok"]:
        failure = response.get("failure") or {}
        lines += [
            "",
            "WHAT IT RETURNED: nothing.",
            f"  Reason given: {failure.get('reason', '(none)')}",
            f"  Suggestion given: {failure.get('suggestion') or '(none)'}",
            f"  Areas it had managed to reach: "
            f"{', '.join(e['neighbourhood'] for e in response.get('shortlist', [])) or '(none)'}",
        ]
        return "\n".join(lines)

    lines += ["", "WHAT IT SUGGESTED"]
    ids = [p["id"] for p in request["people"]]
    for result in response["results"]:
        rating = (
            f"{result['rating']}/5 from {result['rating_count']} reviews"
            if result.get("rating") is not None
            else "unrated"
        )
        lines.append(
            f"  {result['rank']}. {result['name']} -- {result['category']} "
            f"in {result['neighbourhood']}, {rating}"
        )
        for person_id, leg in zip(ids, result["journey"]["legs"]):
            transfers = (
                f", {leg['transfers']} transfer{'s' if leg['transfers'] != 1 else ''}"
                if leg["mode"] == "transit"
                else ""
            )
            lines.append(
                f"       {person_id}: {leg['duration_min']:.0f} min by {leg['mode']}{transfers}"
            )
        journey = result["journey"]
        lines.append(
            f"       spread {journey['gap_min']:.0f} min, combined {journey['total_min']:.0f} min"
        )
        lines.append(f"       it said: {result['why']}")
    return "\n".join(lines)


async def judge_one(client, model: str, record: dict[str, Any]) -> Optional[dict[str, Any]]:
    schema = Judgement if record["response"]["ok"] else FailureJudgement
    try:
        response = await client.messages.parse(
            model=model,
            max_tokens=2000,
            system=SYSTEM,
            messages=[{"role": "user", "content": render(record) + "\n\nEvaluate this."}],
            output_format=schema,
            output_config={"effort": "medium"},
        )
    except Exception as exc:  # noqa: BLE001 -- one bad case must not end the run
        print(f"  ! {record['id']}: judging failed ({exc})")
        return None
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return None
    return response.parsed_output.model_dump()


async def grade(records: list[dict[str, Any]], *, checks_only: bool) -> list[dict[str, Any]]:
    graded = [
        {
            "id": record["id"],
            "ts": record["ts"],
            "checks": checks.run(record),
            "judgement": None,
            "record": record,
        }
        for record in records
    ]
    if checks_only:
        return graded

    settings = get_settings()
    if not settings.llm_enabled:
        print("! ANTHROPIC_API_KEY is not set; deterministic checks only.")
        return graded

    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    # Bounded so a fortnight's log doesn't open a hundred connections at once.
    gate = asyncio.Semaphore(5)

    async def one(row):
        async with gate:
            row["judgement"] = await judge_one(client, settings.llm_model, row["record"])

    await asyncio.gather(*(one(row) for row in graded))
    return graded


def summarise(graded: list[dict[str, Any]]) -> None:
    total = len(graded)
    served = [row for row in graded if row["checks"].get("ok")]
    refused = [row for row in graded if not row["checks"].get("ok")]
    print(f"\n{total} search{'es' if total != 1 else ''}: {len(served)} served, {len(refused)} returned nothing")

    violations: dict[str, int] = {}
    for row in served:
        for name in row["checks"].get("violations", []):
            violations[name] = violations.get(name, 0) + 1
    if violations:
        print("\nContract violations (these are bugs, not opinions):")
        for name, count in sorted(violations.items(), key=lambda kv: -kv[1]):
            print(f"  {name:<24} {count}/{len(served)}")
    elif served:
        print("\nContract violations: none")

    def mean(values: list[float]) -> str:
        clean = [v for v in values if v is not None]
        return f"{statistics.mean(clean):.2f}" if clean else "--"

    if served:
        print("\nDeterministic:")
        for label, key in (
            ("spread of top pick (min)", "top_spread_min"),
            ("worst leg of top pick (min)", "top_worst_leg_min"),
            ("budget headroom (min)", "budget_headroom_min"),
            ("distinct neighbourhoods in top 3", "distinct_neighbourhoods"),
            ("latency (s)", "latency_s"),
        ):
            print(f"  {label:<34} {mean([row['checks'].get(key) for row in served])}")

        inactive: dict[str, int] = {}
        for row in served:
            for name in row["checks"].get("inactive_components") or []:
                inactive[name] = inactive.get(name, 0) + 1
        for name, count in sorted(inactive.items(), key=lambda kv: -kv[1]):
            print(f"  {'scorer ignored ' + name:<34} {count}/{len(served)} searches")

    judged = [row for row in graded if row["judgement"]]
    if not judged:
        return
    print(f"\nJudged ({len(judged)} of {total}), mean score out of 5:")
    for group, rows in (
        (CRITERIA, [r for r in judged if r["checks"].get("ok")]),
        (FAILURE_CRITERIA, [r for r in judged if not r["checks"].get("ok")]),
    ):
        if not rows:
            continue
        for name in (*group, "overall"):
            values = [
                r["judgement"][name]["score"]
                if isinstance(r["judgement"].get(name), dict)
                else r["judgement"].get(name)
                for r in rows
            ]
            print(f"  {name:<34} {mean(values)}")

    worst = sorted(judged, key=lambda r: r["judgement"].get("overall", 5))[:5]
    print("\nWorst-scoring searches, look at these first:")
    for row in worst:
        print(f"  [{row['judgement'].get('overall')}/5] {row['id']}  {row['judgement'].get('notes', '')}")


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        nargs="?",
        default=settings.search_log_dir,
        help="A log file, or a directory of them (default: %(default)s).",
    )
    parser.add_argument("--limit", type=int, default=0, help="Grade at most N, newest first.")
    parser.add_argument("--checks-only", action="store_true", help="No model calls, no cost.")
    parser.add_argument("--out", default="evals/graded", help="Where to write the graded set.")
    args = parser.parse_args()

    paths = dataset.resolve(Path(args.target))
    if not paths:
        print(f"No search log at {args.target}. Set SEARCH_LOG_ENABLED=true and run some searches.")
        return 1

    records = dataset.read_all(paths)
    records.sort(key=lambda r: r.get("ts", ""), reverse=True)
    if args.limit:
        records = records[: args.limit]
    if not records:
        print("Log is empty.")
        return 1

    print(f"Grading {len(records)} search(es) from {len(paths)} file(s)...")
    graded = asyncio.run(grade(records, checks_only=args.checks_only))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out = Path(args.out) / f"graded-{stamp}.jsonl"
    dataset.write(out, graded)
    summarise(graded)
    print(f"\nGraded dataset: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
