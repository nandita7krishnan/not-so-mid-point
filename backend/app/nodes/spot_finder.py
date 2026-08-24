"""Node D -- spot finder.

Turns categories (and optional free text) into Places queries, runs them across
the shortlisted neighbourhoods, and attaches a 0-1 preference-match score.

The LLM is used twice and is optional both times: once to expand free text into
place types and a text query, once to re-rank what came back. If either call is
unavailable or returns nothing usable, the fixed-category path carries the node
on its own -- the fallback the PRD calls for in Section 10.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from ..config import (
    LLM_RERANK_LIMIT,
    NEIGHBOURHOODS_SEARCHED,
    NEIGHBOURHOOD_RADIUS_M,
    get_settings,
)
from ..geo import haversine_m
from ..providers.llm import ALLOWED_PLACE_TYPES
from ..providers.maps import MapsError
from ..runtime import deps_from_config
from ..state import GraphFailure, LatLng, MeetingState, PreferenceSpec, Venue

# The fixed multi-select categories in the form, mapped to Places API types.
CATEGORY_TYPES: dict[str, list[str]] = {
    "coffee": ["cafe", "coffee_shop"],
    "restaurant": ["restaurant"],
    "bar": ["bar", "wine_bar", "pub"],
    "brewery": ["brewery"],
    "park": ["park", "garden"],
    "museum": ["museum"],
    "art": ["art_gallery"],
    "books": ["book_store", "library"],
    "dessert": ["bakery", "ice_cream_shop"],
    "movies": ["movie_theater"],
    "music": ["performing_arts_theater", "night_club"],
    "shopping": ["shopping_mall"],
    "activity": ["bowling_alley", "tourist_attraction"],
}

DEFAULT_TYPES = ["cafe", "restaurant", "park"]


def _types_for_categories(categories: list[str]) -> list[str]:
    out: list[str] = []
    for category in categories:
        for place_type in CATEGORY_TYPES.get(category.strip().lower(), []):
            if place_type not in out:
                out.append(place_type)
    return out


# Words that carry no search signal, stripped before using free text as keywords.
_STOPWORDS = {
    "with", "that", "some", "somewhere", "place", "places", "near", "good",
    "nice", "have", "want", "like", "looking", "would", "there", "where",
}


def _keywords(free_text: str) -> list[str]:
    seen: list[str] = []
    for word in free_text.lower().replace(",", " ").split():
        word = word.strip(".!?'\"")
        if len(word) > 3 and word not in _STOPWORDS and word not in seen:
            seen.append(word)
    return seen[:5]


async def _build_spec(state: MeetingState, deps) -> PreferenceSpec:
    categories = state.get("categories", []) or []
    free_text = (state.get("free_text") or "").strip()
    base_types = _types_for_categories(categories)

    if not free_text:
        return PreferenceSpec(
            place_types=base_types or DEFAULT_TYPES,
            keywords=[],
            text_query="",
            source="categories",
            rationale="Matched on the selected categories.",
        )

    if deps.llm is None:
        # No LLM, but free text is still usable: Places' text search takes a
        # natural-language query directly. Throwing the text away here (as an
        # earlier version did) silently ignored what the user actually asked for.
        return PreferenceSpec(
            place_types=base_types,
            keywords=_keywords(free_text),
            text_query=free_text,
            source="text-only",
            rationale=f"Searched Google Places for \u201c{free_text}\u201d directly.",
        )

    interpretation = await deps.llm.interpret_preferences(free_text, categories)
    if interpretation is None:
        return PreferenceSpec(
            place_types=base_types,
            keywords=_keywords(free_text),
            text_query=free_text,
            source="llm-fallback",
            rationale="Could not interpret the free text; searched for it directly instead.",
        )

    # Only trust types that are actually in the Places vocabulary.
    llm_types = [t for t in interpretation.place_types if t in ALLOWED_PLACE_TYPES]
    merged = base_types + [t for t in llm_types if t not in base_types]
    if not merged:
        merged = DEFAULT_TYPES
    return PreferenceSpec(
        place_types=merged[:6],
        keywords=[k.lower() for k in interpretation.keywords][:5],
        text_query=interpretation.text_query or free_text,
        source="llm",
        rationale=interpretation.rationale or "Interpreted from your description.",
    )


def _to_venue(raw: dict[str, Any], neighbourhood: str) -> Optional[Venue]:
    place_id = raw.get("id")
    location = raw.get("location") or {}
    if not place_id or "latitude" not in location:
        return None
    types = raw.get("types", []) or []
    return Venue(
        place_id=place_id,
        name=(raw.get("displayName") or {}).get("text", "Unnamed place"),
        address=raw.get("formattedAddress", ""),
        category=(raw.get("primaryTypeDisplayName") or {}).get(
            "text", (raw.get("types") or ["place"])[0].replace("_", " ")
        ),
        coords=LatLng(lat=location["latitude"], lng=location["longitude"]),
        neighbourhood=neighbourhood,
        rating=raw.get("rating"),
        rating_count=raw.get("userRatingCount", 0) or 0,
        price_level=raw.get("priceLevel"),
        open_now=(raw.get("currentOpeningHours") or {}).get("openNow"),
        business_status=raw.get("businessStatus", "OPERATIONAL"),
        types=types,
        # Places lists the primary type first; an incidental tag further down the
        # list is much weaker evidence of what a venue actually is.
        primary_type=types[0] if types else "",
    )


# Bayesian shrinkage prior: a venue is treated as if it already carried
# PRIOR_COUNT reviews at PRIOR_MEAN, so a handful of glowing reviews barely moves
# it while hundreds of consistent ones do. PRIOR_COUNT=100 is what makes a
# 5.0-from-30 rank below a 4.5-from-887.
PRIOR_COUNT = 100.0
PRIOR_MEAN = 4.2
# Ceiling on how far the prior may lift a venue's own rating, in stars.
MAX_UPWARD_PULL = 0.4


def _quality(venue: Venue) -> float:
    """Rating shrunk toward the prior by review count, then scaled to 0-1.

    A plain average lets a 5.0 from 3 reviews beat a 4.5 from 900. The shrunk
    average weights the venue's own rating by how much evidence backs it."""
    if venue.rating is None or venue.rating_count <= 0:
        return _scale_rating(PRIOR_MEAN)
    weight = venue.rating_count / (venue.rating_count + PRIOR_COUNT)
    shrunk = weight * venue.rating + (1 - weight) * PRIOR_MEAN
    # Shrinkage may temper a thin rating, but it must not launder a bad one:
    # without this cap a 2.3-star venue with 4 reviews presents as ~4.1 stars,
    # because the prior does nearly all the work.
    shrunk = min(shrunk, venue.rating + MAX_UPWARD_PULL)
    return _scale_rating(shrunk)


def _scale_rating(rating: float) -> float:
    """Map a 3.0-5.0 star rating onto 0-1; below 3.0 is uniformly poor."""
    return max(0.0, min(1.0, (rating - 3.0) / 2.0))


def heuristic_preference(venue: Venue, spec: PreferenceSpec) -> float:
    wanted = set(spec.place_types)
    haystack = f"{venue.name} {venue.category} {' '.join(venue.types)}".lower()
    hits = sum(1 for kw in spec.keywords if kw and kw in haystack)

    if not wanted:
        # Text-only search: everything came back from the text query, so relevance
        # is carried by keyword overlap and rating rather than type matching.
        # The text endpoint already filtered for relevance, so a literal name
        # match is a mild tiebreaker, not the main signal -- otherwise any venue
        # with the search word in its name outranks genuinely better ones.
        keyword = min(1.0, hits / 2.0) if spec.keywords else 0.6
        score = 0.35 * keyword + 0.65 * _quality(venue)
        return max(0.0, min(1.0, score))

    if venue.primary_type in wanted:
        type_match = 1.0          # it really is the kind of place asked for
    elif wanted & set(venue.types):
        type_match = 0.55         # carries the tag, but it isn't what it is
    else:
        type_match = 0.2
    keyword = min(1.0, hits / 2.0) if spec.keywords else 0.5
    score = 0.60 * type_match + 0.25 * _quality(venue) + 0.15 * keyword
    return max(0.0, min(1.0, score))


async def spot_finder_node(state: MeetingState, config: dict) -> dict:
    started = time.perf_counter()
    if state.get("failure"):
        return {}

    deps = deps_from_config(config)
    settings = get_settings()
    shortlist = state.get("shortlisted_neighbourhoods", [])[:NEIGHBOURHOODS_SEARCHED]
    spec = await _build_spec(state, deps)

    async def for_neighbourhood(entry) -> list[Venue]:
        tasks = []
        # With free text but no categories there are no types to search by, so
        # the text query carries the whole request.
        if spec.place_types:
            tasks.append(
                deps.maps.search_nearby(
                    entry.coords,
                    NEIGHBOURHOOD_RADIUS_M,
                    spec.place_types,
                    settings.venues_per_neighbourhood,
                )
            )
        if spec.text_query:
            tasks.append(
                deps.maps.search_text(
                    entry.coords,
                    NEIGHBOURHOOD_RADIUS_M,
                    spec.text_query,
                    settings.venues_per_neighbourhood,
                )
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        venues: dict[str, Venue] = {}
        for result in results:
            if isinstance(result, BaseException):
                continue
            for raw in result:
                venue = _to_venue(raw, entry.neighbourhood)
                if venue is None:
                    continue
                # Travel times are measured to the neighbourhood centroid, so a
                # venue far from it would be reported with a time that isn't its
                # own. Enforce the radius client-side as well: the API boundary
                # is a rectangle, and a rectangle's corners overshoot the circle.
                if haversine_m(entry.coords, venue.coords) > NEIGHBOURHOOD_RADIUS_M:
                    continue
                venues.setdefault(venue.place_id, venue)
        return list(venues.values())

    try:
        per_neighbourhood = await asyncio.gather(*(for_neighbourhood(e) for e in shortlist))
    except MapsError as exc:
        return {
            "preference_spec": spec,
            "failure": GraphFailure(
                node="spot_finder",
                reason=f"Venue search failed: {exc}",
                suggestion="Check that Places API (New) is enabled on the API key.",
            ),
            "timings": {"spot_finder": time.perf_counter() - started},
        }

    venues: list[Venue] = [v for group in per_neighbourhood for v in group]
    if not venues:
        return {
            "preference_spec": spec,
            "candidate_venues": [],
            "failure": GraphFailure(
                node="spot_finder",
                reason="Found fair neighbourhoods, but no venues in them matched your interests.",
                suggestion=(
                    "Try selecting more categories, loosening the free-text description, "
                    "or raising the travel time limit so more neighbourhoods qualify."
                ),
                detail={"neighbourhoods_searched": [e.neighbourhood for e in shortlist]},
            ),
            "timings": {"spot_finder": time.perf_counter() - started},
        }

    for venue in venues:
        venue.preference_score = heuristic_preference(venue, spec)
        venue.preference_reason = (
            f"Matches {venue.category.lower()}"
            + (f" · {venue.rating}★ ({venue.rating_count})" if venue.rating else "")
        )

    warnings: list[str] = []
    free_text = (state.get("free_text") or "").strip()
    if free_text and deps.llm is not None:
        ranked = sorted(venues, key=lambda v: v.preference_score, reverse=True)[:LLM_RERANK_LIMIT]
        judgement = await deps.llm.rank_venues(
            free_text,
            state.get("categories", []) or [],
            [
                {
                    "place_id": v.place_id,
                    "name": v.name,
                    "types": v.types,
                    "rating": v.rating,
                    "rating_count": v.rating_count,
                }
                for v in ranked
            ],
        )
        if judgement is None or not judgement.judgements:
            warnings.append("Free-text re-ranking was unavailable; used category matching.")
        else:
            scores = {j.place_id: j for j in judgement.judgements}
            for venue in venues:
                verdict = scores.get(venue.place_id)
                if verdict is None:
                    continue
                # Blend rather than replace: the model judges fit, the heuristic
                # carries the rating signal it can't see.
                venue.preference_score = round(
                    0.7 * verdict.score + 0.3 * venue.preference_score, 4
                )
                if verdict.reason:
                    venue.preference_reason = verdict.reason

    return {
        "preference_spec": spec,
        "candidate_venues": venues,
        "warnings": warnings,
        "timings": {"spot_finder": time.perf_counter() - started},
    }
