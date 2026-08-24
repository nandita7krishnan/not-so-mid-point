"""The shared state object that travels through the whole agent graph.

`MeetingState` is the LangGraph channel dict (PRD Section 5). Everything it
holds is a plain pydantic model so the same types serialise straight out of
the FastAPI endpoint.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, Optional, TypedDict

from pydantic import BaseModel, Field

FairnessMode = Literal["gap", "absolute"]

# Google's travel modes. Only "transit" has a transfer concept, which is why the
# transfer budget is conditional throughout the graph.
TravelMode = Literal["transit", "driving", "bicycling", "walking"]

MODE_LABELS: dict[str, str] = {
    "transit": "bus/train",
    "driving": "drive",
    "bicycling": "bike",
    "walking": "walk",
}


def uses_transit(*modes: str) -> bool:
    """The transfer budget and the transfer weight only mean something when at
    least one person is actually riding transit."""
    return any(mode == "transit" for mode in modes)


class LatLng(BaseModel):
    lat: float
    lng: float


class Location(BaseModel):
    query: str
    label: str
    coords: LatLng


class Budget(BaseModel):
    max_time_min: int = Field(45, ge=5, le=180)
    max_transfers: int = Field(2, ge=0, le=5)


class Weights(BaseModel):
    """Scoring weights. Normalised rather than rejected if they don't sum to 1."""

    fairness: float = Field(0.4, ge=0)
    preference: float = Field(0.4, ge=0)
    transfers: float = Field(0.2, ge=0)

    def normalized(self) -> "Weights":
        total = self.fairness + self.preference + self.transfers
        if total <= 0:
            return Weights(fairness=1 / 3, preference=1 / 3, transfers=1 / 3)
        return Weights(
            fairness=self.fairness / total,
            preference=self.preference / total,
            transfers=self.transfers / total,
        )


class Neighbourhood(BaseModel):
    name: str
    coords: LatLng


class SearchArea(BaseModel):
    """Node 0's output: where in the map it is worth looking at all."""

    center: LatLng
    radius_m: int
    candidates: list[Neighbourhood]
    balance_note: str = ""


class Leg(BaseModel):
    """One person's trip to one neighbourhood, in their chosen mode."""

    neighbourhood: str
    reachable: bool
    mode: TravelMode = "transit"
    duration_min: float = 0.0
    # Always 0 for non-transit modes -- a drive has no transfers.
    transfers: int = 0
    summary: str = ""  # e.g. "Bus 40 -> Link 1 Line"
    error: str = ""


class ShortlistEntry(BaseModel):
    neighbourhood: str
    coords: LatLng
    p1: Leg
    p2: Leg
    gap_min: float
    max_min: float
    total_min: float
    total_transfers: int
    fairness_raw: float
    # False when neither person is on transit, so the UI can explain why the
    # transfers component is flat instead of showing a misleading 100%.
    transfers_meaningful: bool = True


class Venue(BaseModel):
    place_id: str
    name: str
    address: str
    category: str
    coords: LatLng
    neighbourhood: str
    rating: Optional[float] = None
    rating_count: int = 0
    price_level: Optional[str] = None
    open_now: Optional[bool] = None
    business_status: str = "OPERATIONAL"
    types: list[str] = Field(default_factory=list)
    primary_type: str = ""
    preference_score: float = 0.0  # 0-1
    preference_reason: str = ""


class ScoreBreakdown(BaseModel):
    fairness: float
    preference: float
    transfers: float
    final: float
    # Components that could not discriminate (every candidate scored alike), so
    # they were dropped from the weighting rather than silently flattened.
    inactive: list[str] = Field(default_factory=list)


class RankedVenue(BaseModel):
    venue: Venue
    shortlist: ShortlistEntry
    scores: ScoreBreakdown
    why: str = ""


class PreferenceSpec(BaseModel):
    """Node D's interpretation of the user's categories + free text."""

    place_types: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    text_query: str = ""
    source: Literal["categories", "llm", "llm-fallback", "text-only"] = "categories"
    rationale: str = ""


class GraphFailure(BaseModel):
    """Set by any node that cannot continue. Node F turns this into a message."""

    node: str
    reason: str
    suggestion: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class MeetingState(TypedDict, total=False):
    # inputs
    person1_location: Location
    person2_location: Location
    person1_mode: TravelMode
    person2_mode: TravelMode
    budget: Budget
    weights: Weights
    fairness_mode: FairnessMode
    categories: list[str]
    free_text: str
    departure_time: int  # unix seconds
    require_open: bool

    # accumulated by the graph
    search_area: SearchArea
    person1_reachability: list[Leg]
    person2_reachability: list[Leg]
    shortlisted_neighbourhoods: list[ShortlistEntry]
    preference_spec: PreferenceSpec
    candidate_venues: list[Venue]
    ranked_venues: list[RankedVenue]
    final_top_3: list[RankedVenue]

    # cross-cutting
    failure: Optional[GraphFailure]
    warnings: Annotated[list[str], operator.add]
    timings: Annotated[dict[str, float], operator.or_]
