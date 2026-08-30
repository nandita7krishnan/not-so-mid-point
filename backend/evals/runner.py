"""Run synthetic cases through the real graph and write eval records.

    python -m evals.fetch_anchors                     # once, build the anchors
    python -m evals.runner --record --count 12        # spends quota, freezes it
    python -m evals.runner                            # replays, costs nothing
    python -m evals.judge evals/synthetic             # grade what came out

Record mode is the only mode that talks to Google. It keeps the cases it drew
and every Maps response they provoked, so re-running after a change to the
scoring is free and compares like with like: the same journeys, the same
venues, a different ranking.

Records come out in exactly the schema searchlog.py writes, so `checks.py` and
`judge.py` do not know or care whether a search was real.

One thing the fixtures do not freeze is Claude. The graph's LLM calls are
optional and have deterministic fallbacks, so by default this runs without a
key and grades those. `--llm` includes the model, at the cost of a live call
per run and answers that vary between runs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app import anchors as anchor_lib
from app import payload as payload_builder
from app import searchlog
from app.config import get_settings
from app.graph import run_graph
from app.providers.llm import LLMClient
from app.providers.maps import MapsClient
from app.runtime import RunDeps
from app.state import Budget, LatLng, Location, Person, Weights

from . import dataset, synth
from .fixtures import FixtureMaps

CASES_PATH = Path("evals/cases.json")
FIXTURE_DIR = Path("evals/fixtures")
OUT_DIR = Path("evals/synthetic")


class CaseRequest:
    """The fields of a RecommendRequest that the payload and the log read.

    Not the FastAPI model itself: that one carries addresses and session
    tokens, and a synthetic case has neither.
    """

    def __init__(self, spec: dict[str, Any]) -> None:
        self.categories = list(spec.get("categories", []))
        self.free_text = spec.get("free_text", "")
        self.max_time_min = int(spec["max_time_min"])
        self.max_transfers = int(spec["max_transfers"])
        self.weights = Weights(**spec["weights"])
        self.fairness_mode = spec.get("fairness_mode", "gap")
        self.require_open = bool(spec.get("require_open", False))


def departure_for(spec: dict[str, Any]) -> int:
    """The next occurrence of the case's weekday and hour, in the future.

    Transit routing needs a future departure, so the timestamp cannot be
    frozen into the case. The fixture key leaves departure out for exactly this
    reason: the same case replays next week without re-recording.
    """
    now = datetime.now(timezone.utc)
    ahead = (int(spec.get("departure_weekday", 2)) - now.weekday()) % 7 or 7
    when = (now + timedelta(days=ahead)).replace(
        hour=int(spec.get("departure_hour", 12)), minute=0, second=0, microsecond=0
    )
    return int(when.timestamp())


def people_for(case: dict[str, Any]) -> list[Person]:
    return [
        Person(
            label=person["id"],
            location=Location(
                query=person["area"],
                label=person["area"],
                coords=LatLng(**person["coords"]),
            ),
            mode=person["mode"],
        )
        for person in case["people"]
    ]


async def run_case(case: dict[str, Any], maps: Any, llm: Optional[LLMClient]) -> dict[str, Any]:
    spec = case["request"]
    request = CaseRequest(spec)
    people = people_for(case)
    departure = departure_for(spec)

    state = await run_graph(
        {
            "people": people,
            "budget": Budget(
                max_time_min=request.max_time_min,
                max_transfers=request.max_transfers,
            ),
            "weights": request.weights,
            "fairness_mode": request.fairness_mode,
            "categories": request.categories,
            "free_text": request.free_text,
            "departure_time": departure,
            "require_open": request.require_open,
        },
        RunDeps(maps=maps, llm=llm),
    )

    payload = payload_builder.build(
        state=state, people=people, request=request, departure=departure
    )
    record = searchlog.build_record(
        people=people, request=request, response=payload, departure=departure
    )
    # The scrubbing in build_record exists to blur a real start. There is
    # nothing here to blur -- every origin is a published stop -- so the case's
    # own people replace the coarsened ones, and the record keeps the exact
    # coordinates that make it replayable.
    record["request"]["people"] = case["people"]
    record["synthetic"] = True
    record["case_id"] = case["id"]
    record["shape"] = case["shape"]
    record["visitor"] = ""
    return record


async def run_all(
    cases: list[dict[str, Any]],
    *,
    record_mode: bool,
    use_llm: bool = False,
    fixture_dir: Path = FIXTURE_DIR,
) -> list[dict[str, Any]]:
    llm = LLMClient() if use_llm and get_settings().llm_enabled else None
    records = []
    for index, case in enumerate(cases, 1):
        fixture_path = fixture_dir / f"{case['id']}.json"
        inner = MapsClient() if record_mode else None
        maps = FixtureMaps(fixture_path, inner)
        try:
            result = await run_case(case, maps, llm)
        finally:
            await maps.aclose()
        if record_mode:
            maps.save()
        served = "no result" if result["response"]["failure"] else "ok"
        print(f"  [{index}/{len(cases)}] {case['shape']:<18} {served}")
        records.append(result)
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true", help="call Google and freeze the answers")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--shape", action="append", default=[], choices=list(synth.SHAPES))
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--log", type=Path, default=None, help="real log to learn the distribution from")
    parser.add_argument("--llm", action="store_true", help="include Claude; costs a call per run")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    if args.record:
        anchors = anchor_lib.load(get_settings().search_log_anchors)
        if not len(anchors):
            print("no anchors; run `python -m evals.fetch_anchors` first")
            return 1
        distribution = synth.Distribution()
        if args.log:
            records = dataset.read_all(dataset.resolve(args.log))
            distribution = synth.Distribution.from_records(records)
            print(f"  distribution learned from {len(records)} logged searches")
        cases = synth.sample_many(
            anchors, args.count, seed=args.seed,
            dist=distribution, shapes=args.shape or None,
        )
        args.cases.parent.mkdir(parents=True, exist_ok=True)
        args.cases.write_text(json.dumps(cases, indent=1), encoding="utf-8")
        print(f"  {len(cases)} cases -> {args.cases}")
    else:
        if not args.cases.exists():
            print(f"no cases at {args.cases}; run once with --record")
            return 1
        cases = json.loads(args.cases.read_text(encoding="utf-8"))

    records = asyncio.run(run_all(cases, record_mode=args.record, use_llm=args.llm))
    out = args.out / f"searches-synthetic-{args.seed}.jsonl"
    dataset.write(out, records)
    print(f"  {len(records)} records -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
