"""Claude calls for Node D (free-text preference interpretation) and Node F
(the human-readable "why this spot" line).

Both are optional: every caller has a deterministic fallback, so the graph
still returns results when ANTHROPIC_API_KEY is unset or a call fails.
"""
from __future__ import annotations

import logging
from typing import Optional

from anthropic import APIError, AsyncAnthropic
from pydantic import BaseModel, Field

from ..config import get_settings

log = logging.getLogger(__name__)

# Places API (New) type values we let the model choose from. Constraining the
# vocabulary is what keeps the interpretation step usable as a query directly.
ALLOWED_PLACE_TYPES = [
    "cafe", "coffee_shop", "bakery", "restaurant", "bar", "pub", "wine_bar",
    "brewery", "ice_cream_shop", "tea_house", "park", "hiking_area", "garden",
    "museum", "art_gallery", "book_store", "library", "movie_theater",
    "performing_arts_theater", "tourist_attraction", "shopping_mall",
    "night_club", "bowling_alley", "gym", "spa", "zoo", "aquarium",
]


class PreferenceInterpretation(BaseModel):
    place_types: list[str] = Field(
        default_factory=list,
        description="Up to 4 Places API type values from the allowed list.",
    )
    keywords: list[str] = Field(
        default_factory=list, description="Up to 5 short descriptive keywords."
    )
    text_query: str = Field(
        "", description="A single natural-language Places text search query."
    )
    rationale: str = Field("", description="One short sentence on the interpretation.")


class VenueJudgement(BaseModel):
    place_id: str
    score: float = Field(ge=0.0, le=1.0)
    reason: str = Field("", description="At most 12 words on the fit.")


class VenueRanking(BaseModel):
    judgements: list[VenueJudgement] = Field(default_factory=list)


class LLMClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.llm_model
        self._effort = settings.llm_effort

    async def _parse(self, prompt: str, system: str, schema: type[BaseModel]):
        """One structured call. Returns None on any failure or refusal so the
        caller can fall back rather than surface an error to the user."""
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=2000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                output_config={"effort": self._effort},
            )
        except APIError as exc:
            log.warning("LLM call failed (%s); falling back", exc)
            return None
        if response.stop_reason == "refusal":
            log.warning("LLM declined the request; falling back")
            return None
        return response.parsed_output

    async def interpret_preferences(
        self, free_text: str, categories: list[str]
    ) -> Optional[PreferenceInterpretation]:
        system = (
            "You translate a person's description of the kind of place they want to "
            "meet at into Google Places API search parameters. Choose place_types "
            "only from the allowed list. Be conservative: prefer fewer, more "
            "accurate types over a broad net."
        )
        prompt = (
            f"Allowed place types: {', '.join(ALLOWED_PLACE_TYPES)}\n\n"
            f"Categories the two people already selected: "
            f"{', '.join(categories) if categories else '(none)'}\n"
            f"What they wrote: {free_text!r}\n\n"
            "Return search parameters for finding venues that fit."
        )
        return await self._parse(prompt, system, PreferenceInterpretation)

    async def rank_venues(
        self, free_text: str, categories: list[str], venues: list[dict]
    ) -> Optional[VenueRanking]:
        system = (
            "You score how well each venue matches what two people asked for. "
            "Score 0.0-1.0. Judge only from the supplied fields; do not invent "
            "details about a venue. Return one judgement per venue, same place_id."
        )
        lines = "\n".join(
            f"- {v['place_id']} | {v['name']} | types: {', '.join(v['types'][:6])} | "
            f"rating {v.get('rating') or 'n/a'} ({v.get('rating_count', 0)} reviews)"
            for v in venues
        )
        prompt = (
            f"They want: {free_text!r}\n"
            f"Selected categories: {', '.join(categories) if categories else '(none)'}\n\n"
            f"Venues:\n{lines}"
        )
        return await self._parse(prompt, system, VenueRanking)

    async def explain(self, prompt: str) -> Optional[str]:
        system = (
            "You write one short, concrete sentence explaining why a meeting spot "
            "suits two people. Use only the facts given. No greetings, no hedging, "
            "no exclamation marks."
        )
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=300,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "low"},
            )
        except APIError as exc:
            log.warning("LLM explain failed (%s); using template copy", exc)
            return None
        if response.stop_reason == "refusal":
            return None
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return text or None
