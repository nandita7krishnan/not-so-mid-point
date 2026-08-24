"""Unit tests for the Section 7 scoring rules."""
from app.nodes.scorer import fairness_score, transfer_score
from app.nodes.shortlist import fairness_raw
from app.nodes.spot_finder import heuristic_preference
from app.state import LatLng, PreferenceSpec, Venue

K = 0.4


def test_gap_fairness_prefers_shorter_trip_over_a_perfectly_even_long_one():
    """The PRD's own example: a 15/20 split should beat a 'fair' 40/40."""
    assert fairness_raw([15, 20], "gap", K) > fairness_raw([40, 40], "gap", K)


def test_gap_fairness_punishes_lopsided_trips():
    assert fairness_raw([10, 50], "gap", K) < fairness_raw([30, 30], "gap", K)


def test_absolute_mode_ignores_the_gap_entirely():
    # Same total, very different split: absolute mode scores them identically.
    assert fairness_raw([10, 50], "absolute", K) == fairness_raw([30, 30], "absolute", K)
    # ...which is exactly what the gap mode does not do.
    assert fairness_raw([10, 50], "gap", K) != fairness_raw([30, 30], "gap", K)


def test_fairness_score_is_scale_aware():
    """Regression: min-max normalisation stretched a 4-minute driving difference
    into a 0.79 fairness gap, which buried the best-matching venue at rank 7."""
    best = -8.6
    # ~0.6 minutes-equivalent worse than the best option.
    assert fairness_score(-9.2, best, 15.0) > 0.9
    # A genuinely bad option still scores near zero.
    assert fairness_score(-24.0, best, 15.0) == 0.0
    assert fairness_score(best, best, 15.0) == 1.0


def test_fairness_score_falls_monotonically_with_the_deficit():
    best = -8.0
    scores = [fairness_score(best - d, best, 15.0) for d in (0, 2, 5, 10, 20)]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_transfer_score_is_absolute_not_relative():
    # A direct trip always scores 1.0 -- it does not depend on how good or bad
    # the other candidates happen to be.
    assert transfer_score(0, 4) == 1.0
    assert transfer_score(2, 4) == 0.5
    assert transfer_score(9, 4) == 0.0


def _venue(**kwargs) -> Venue:
    types = kwargs.get("types", ["cafe"])
    kwargs.setdefault("primary_type", types[0] if types else "")
    base = dict(
        place_id="x",
        name="Somewhere",
        address="",
        category="Coffee shop",
        coords=LatLng(lat=47.6, lng=-122.3),
        neighbourhood="Capitol Hill",
        types=["cafe"],
        rating=4.5,
        rating_count=500,
    )
    base.update(kwargs)
    return Venue(**base)


def test_preference_rewards_a_type_match():
    spec = PreferenceSpec(place_types=["cafe"])
    assert heuristic_preference(_venue(), spec) > heuristic_preference(
        _venue(types=["gym"]), spec
    )


def test_rating_confidence_damps_a_five_star_from_three_reviews():
    spec = PreferenceSpec(place_types=["cafe"])
    established = heuristic_preference(_venue(rating=4.5, rating_count=900), spec)
    thin = heuristic_preference(_venue(rating=5.0, rating_count=3), spec)
    assert established > thin


def test_free_text_keywords_drop_stopwords():
    from app.nodes.spot_finder import _keywords

    assert _keywords("somewhere quiet with outdoor seating") == ["quiet", "outdoor", "seating"]
    assert _keywords("running trail") == ["running", "trail"]


def test_text_only_preference_scores_on_keywords_not_types():
    """With no categories chosen there are no types to match, so a venue whose
    name matches the free text must still outrank one that doesn't."""
    from app.nodes.spot_finder import heuristic_preference

    spec = PreferenceSpec(place_types=[], keywords=["running", "trail"], text_query="running trail")
    trail = _venue(name="Burke-Gilman Trail", category="Trail", types=["park"], rating=4.8, rating_count=900)
    bar = _venue(name="Some Bar", category="Bar", types=["bar"], rating=4.8, rating_count=900)
    assert heuristic_preference(trail, spec) > heuristic_preference(bar, spec)


def test_review_count_outweighs_a_thin_perfect_score():
    """Regression: a 5.0 from 30 reviews used to edge out a 4.5 from 887."""
    from app.nodes.spot_finder import _quality

    thin_five = _venue(rating=5.0, rating_count=30)
    established = _venue(rating=4.5, rating_count=887)
    assert _quality(established) > _quality(thin_five)


def test_quality_rises_with_review_count_at_a_fixed_rating():
    from app.nodes.spot_finder import _quality

    scores = [_quality(_venue(rating=4.6, rating_count=n)) for n in (5, 50, 500, 5000)]
    assert scores == sorted(scores), "more reviews at the same rating must score higher"


def test_unrated_venue_sits_at_the_prior_not_at_zero():
    from app.nodes.spot_finder import PRIOR_MEAN, _quality, _scale_rating

    assert _quality(_venue(rating=None, rating_count=0)) == _scale_rating(PRIOR_MEAN)


def test_primary_type_beats_an_incidental_type_tag():
    """A banh mi restaurant tagged `cafe` must not outrank an actual cafe."""
    from app.nodes.spot_finder import heuristic_preference

    spec = PreferenceSpec(place_types=["cafe", "coffee_shop"])
    real_cafe = _venue(name="Analog Coffee", types=["coffee_shop", "cafe"], rating=4.5, rating_count=500)
    banh_mi = _venue(name="Oh Yeah Banh Mi", types=["restaurant", "cafe"], rating=5.0, rating_count=30)
    assert heuristic_preference(real_cafe, spec) > heuristic_preference(banh_mi, spec)


def test_shrinkage_does_not_launder_a_bad_rating():
    """Regression: a 2.3-star venue with 4 reviews was being pulled up to an
    effective ~4.1 stars by the prior, and ranked first."""
    from app.nodes.spot_finder import _quality

    bad = _venue(rating=2.3, rating_count=4)
    good = _venue(rating=4.6, rating_count=4892)
    assert _quality(bad) < 0.2
    assert _quality(good) > _quality(bad)


def test_text_only_name_match_cannot_beat_a_far_better_venue():
    """A poorly-rated venue with the search word in its name must not outrank a
    well-rated one that lacks it."""
    from app.nodes.spot_finder import heuristic_preference

    spec = PreferenceSpec(place_types=[], keywords=["running", "trail"])
    named_but_bad = _venue(name="SODO Trail", types=["park"], rating=2.3, rating_count=4)
    unnamed_but_good = _venue(name="Lake Union Park", types=["park"], rating=4.6, rating_count=4892)
    assert heuristic_preference(unnamed_but_good, spec) > heuristic_preference(named_but_bad, spec)
