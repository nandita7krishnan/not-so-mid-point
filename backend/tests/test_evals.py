"""The eval harness: deterministic checks, prompt rendering, and grading.

The judge's model call is stubbed. What is worth testing here is that a bad
answer is actually caught -- a judge that cannot fail anything is worse than no
judge, because it reads as a passing grade.
"""
import asyncio
import json

import pytest

from evals import checks, dataset, judge

BASE = {
    "id": "abc123",
    "ts": "2026-08-29T12:00:00Z",
    "request": {
        "people": [
            {"id": "P1", "area": "Ballard", "area_source": "seed",
             "coords": {"lat": 47.67, "lng": -122.38}, "mode": "transit"},
            {"id": "P2", "area": "Columbia City", "area_source": "seed",
             "coords": {"lat": 47.56, "lng": -122.29}, "mode": "driving"},
        ],
        "party_size": 2,
        "categories": ["coffee"],
        "free_text": "",
        "max_time_min": 45,
        "max_transfers": 2,
        "weights": {"fairness": 0.4, "preference": 0.4, "transfers": 0.2},
        "fairness_mode": "gap",
        "require_open": False,
        "departure_time": 1_700_000_000,
        "departure_hour_utc": 9,
        "departure_weekday": 2,
    },
    "response": {
        "ok": True,
        "transfers_apply": True,
        "preference_spec": {},
        "search_area": None,
        "shortlist": [],
        "results": [],
        "failure": None,
        "warnings": [],
        "timings": {"total": 6.2},
    },
}


def _result(rank, *, name="Storyville", nbhd="Belltown", rating=4.6,
            durations=(40.0, 19.0), transfers=(1, 0), why="An even trip."):
    legs = [
        {"mode": m, "reachable": True, "duration_min": d, "transfers": t}
        for m, d, t in zip(("transit", "driving"), durations, transfers)
    ]
    return {
        "rank": rank, "place_id": f"p{rank}", "name": name,
        "address": "94 Pike St", "category": "Coffee shop",
        "primary_type": "coffee_shop", "types": ["cafe"], "neighbourhood": nbhd,
        "rating": rating, "rating_count": 2100, "price_level": None,
        "open_now": True, "preference_score": 0.9, "preference_reason": "",
        "scores": {"fairness": 0.8, "preference": 0.9, "transfers": 0.75,
                   "final": 0.83, "inactive": []},
        "why": why,
        "journey": {
            "neighbourhood": nbhd, "gap_min": abs(durations[0] - durations[1]),
            "max_min": max(durations), "total_min": sum(durations),
            "total_transfers": sum(transfers), "legs": legs,
        },
    }


def _record(results=None, **response):
    record = json.loads(json.dumps(BASE))
    record["response"]["results"] = results if results is not None else [_result(1)]
    record["response"].update(response)
    return record


# ------------------------------------------------------------------- checks

def test_a_clean_answer_records_no_violations():
    result = checks.run(_record())
    assert result["violations"] == []
    assert result["result_count"] == 1
    assert result["top_spread_min"] == 21.0


def test_a_leg_over_the_stated_budget_is_caught():
    """45 min was promised; this pick takes 52. That is a bug, not a low score,
    so it must not be left to the model's judgement."""
    over = checks.run(_record([_result(1, durations=(52.0, 19.0))]))
    assert over["budget_respected"] is False
    assert "budget_respected" in over["violations"]


def test_too_many_transfers_is_caught_only_on_transit_legs():
    busy = checks.run(_record([_result(1, transfers=(4, 9))]))
    # P2 drives, so their nominal transfer count is not a breach.
    assert busy["transfers_respected"] is False
    driving_only = _result(1, transfers=(0, 9))
    driving_only["journey"]["legs"][0]["mode"] = "driving"
    assert checks.run(_record([driving_only]))["transfers_respected"] is True


def test_an_unrated_pick_is_flagged_because_ranking_claims_to_use_ratings():
    assert "ratings_present" in checks.run(_record([_result(1, rating=None)]))["violations"]


def test_a_missing_explanation_is_flagged():
    assert "explanations_present" in checks.run(_record([_result(1, why="  ")]))["violations"]


def test_three_picks_on_one_block_are_visible_as_low_diversity():
    same = [_result(i, nbhd="Belltown") for i in (1, 2, 3)]
    spread = [_result(i, nbhd=n) for i, n in enumerate(("Belltown", "Fremont", "Ballard"), 1)]
    assert checks.run(_record(same))["distinct_neighbourhoods"] == 1
    assert checks.run(_record(spread))["distinct_neighbourhoods"] == 3


def test_budget_headroom_shows_how_tight_the_answer_was():
    # Worst leg 40 min against a 45 min budget.
    assert checks.run(_record())["budget_headroom_min"] == 5.0


def test_a_refusal_is_checked_for_being_actionable():
    record = _record([], ok=False, failure={
        "node": "shortlist", "reason": "Nothing reachable.", "suggestion": "", "detail": {}})
    result = checks.run(record)
    assert result["ok"] is False
    assert result["failure_has_suggestion"] is False
    assert result["shortlist_was_empty"] is True


# ------------------------------------------------------------------ rendering

def test_the_prompt_states_the_journeys_not_the_apps_own_scores():
    """The judge should assess the outcome, not grade the scorer's homework."""
    text = judge.render(_record())
    assert "near Ballard" in text and "near Columbia City" in text
    assert "40 min by transit, 1 transfer" in text
    assert "at most 45 min travel" in text
    # The internal fairness/preference numbers are deliberately withheld.
    assert "0.83" not in text and "fairness 0.8" not in text


def test_a_refusal_renders_with_its_reason_and_suggestion():
    text = judge.render(_record([], ok=False, failure={
        "node": "shortlist", "reason": "Nothing reachable.",
        "suggestion": "Raise the limit to 55 min.", "detail": {}}))
    assert "WHAT IT RETURNED: nothing." in text
    assert "Raise the limit to 55 min." in text


def test_free_text_is_shown_in_preference_to_bare_categories():
    record = _record()
    record["request"]["free_text"] = "somewhere quiet with outdoor seating"
    assert "somewhere quiet with outdoor seating" in judge.render(record)


# ------------------------------------------------------------------- grading

class _Stub:
    """Stands in for AsyncAnthropic. Records what it was asked."""

    def __init__(self, payload, stop_reason="end_turn"):
        self.payload, self.stop_reason, self.prompts = payload, stop_reason, []
        self.messages = self

    async def parse(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        schema = kwargs["output_format"]
        return type("R", (), {
            "stop_reason": self.stop_reason,
            "parsed_output": schema(**self.payload) if self.payload else None,
        })()


GOOD = {
    "preference_fit": {"score": 4, "reason": "Coffee shops, as asked."},
    "fairness": {"score": 2, "reason": "21 min spread is not even."},
    "spot_quality": {"score": 4, "reason": "Well rated."},
    "explanation": {"score": 1, "reason": "Calls a 21 min spread even."},
    "overall": 2,
    "notes": "The why line contradicts the journey data.",
}


def test_grading_attaches_a_judgement_per_record():
    stub = _Stub(GOOD)
    result = asyncio.run(judge.judge_one(stub, "claude-opus-5", _record()))
    assert result["overall"] == 2
    assert result["explanation"]["score"] == 1
    assert "near Ballard" in stub.prompts[0]


def test_a_refusal_is_graded_against_the_refusal_criteria():
    stub = _Stub({
        "refusal_correct": {"score": 5, "reason": "Genuinely nothing in range."},
        "suggestion_actionable": {"score": 4, "reason": "Names a number to raise."},
        "overall": 5, "notes": "",
    })
    record = _record([], ok=False, failure={
        "node": "shortlist", "reason": "x", "suggestion": "Raise to 55 min.", "detail": {}})
    result = asyncio.run(judge.judge_one(stub, "claude-opus-5", record))
    assert result["refusal_correct"]["score"] == 5


def test_a_refused_or_failed_judgement_does_not_end_the_run():
    assert asyncio.run(
        judge.judge_one(_Stub(GOOD, stop_reason="refusal"), "m", _record())
    ) is None

    class Boom:
        messages = property(lambda self: self)

        async def parse(self, **kwargs):
            raise RuntimeError("network")

    assert asyncio.run(judge.judge_one(Boom(), "m", _record())) is None


def test_checks_only_grading_makes_no_model_calls():
    graded = asyncio.run(judge.grade([_record()], checks_only=True))
    assert graded[0]["judgement"] is None
    assert graded[0]["checks"]["violations"] == []


def test_summarise_reports_violations_and_scores(capsys):
    graded = [
        {"id": "a", "ts": "", "checks": checks.run(_record([_result(1, durations=(52.0, 19.0))])),
         "judgement": GOOD, "record": _record()},
        {"id": "b", "ts": "", "checks": checks.run(_record()), "judgement": GOOD,
         "record": _record()},
    ]
    judge.summarise(graded)
    out = capsys.readouterr().out
    assert "budget_respected" in out
    assert "Worst-scoring searches" in out


# -------------------------------------------------------------------- dataset

def test_a_half_written_final_line_costs_one_record_not_the_run(tmp_path):
    path = tmp_path / "searches-2026-08-29.jsonl"
    path.write_text(json.dumps(_record()) + "\n" + '{"id": "trunc', encoding="utf-8")
    assert len(list(dataset.read(path))) == 1


def test_a_directory_resolves_to_every_day_file(tmp_path):
    for day in ("2026-08-27", "2026-08-28"):
        (tmp_path / f"searches-{day}.jsonl").write_text("")
    (tmp_path / "graded-x.jsonl").write_text("")  # not a search log
    assert [p.name for p in dataset.resolve(tmp_path)] == [
        "searches-2026-08-27.jsonl", "searches-2026-08-28.jsonl"]
