"""Runtime configuration, loaded from the environment / .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- credentials -----------------------------------------------------
    google_maps_api_key: str = ""
    anthropic_api_key: str = ""

    # --- LLM -------------------------------------------------------------
    llm_model: str = "claude-opus-5"
    llm_effort: str = "low"  # these are small, well-specified calls

    # --- graph tuning ----------------------------------------------------
    # How many neighbourhoods survive Node 0 and get a full Directions call
    # in Nodes A/B. Each one costs 2 Directions requests, so this is the main
    # cost/latency dial for the whole graph.
    max_candidate_neighbourhoods: int = 8
    # Corridor half-width around the P1->P2 line, in km. Neighbourhoods
    # further off the line than this are never considered.
    corridor_half_width_km: float = 4.0
    # Venues pulled per neighbourhood from Places before scoring.
    venues_per_neighbourhood: int = 8
    # Ceiling constant `k` in the gap-based fairness formula (Section 7).
    fairness_ceiling_k: float = 0.4
    # How many minutes-equivalent worse than the best option a candidate must be
    # before its fairness score reaches 0. This is what keeps the scoring
    # scale-aware: a 4-minute difference costs ~0.27, not the whole range.
    fairness_tolerance_min: float = 15.0
    # How far below the best-matching venue's preference score a candidate must
    # fall before its preference score reaches 0. Without this the component was
    # effectively dead: every venue the Places search returns is of the type
    # asked for, so raw preference scores cluster inside ~0.03 of each other
    # while fairness spans the full range, and no slider setting short of 0%
    # fairness could ever change the ranking.
    preference_tolerance: float = 0.15
    # Total transfers (both people combined) at which the transfer score hits 0.
    transfer_reference: int = 4

    # --- infrastructure --------------------------------------------------
    # Centre and radius used to bias address autocomplete toward the local area.
    autocomplete_bias_lat: float = 47.62
    autocomplete_bias_lng: float = -122.33
    autocomplete_bias_radius_m: int = 50_000

    # --- rate limiting ---------------------------------------------------
    # Off by default so local development is unencumbered; render.yaml turns it
    # on. The per-client rules stop one visitor hammering the expensive path;
    # the global daily cap is what actually bounds the Maps bill.
    rate_limit_enabled: bool = False
    recommend_per_hour: int = 12
    recommend_per_day: int = 40
    autocomplete_per_minute: int = 60
    global_recommend_per_day: int = 8
    # Only honour X-Forwarded-For when actually behind a proxy; the header is
    # trivially spoofed when the app is exposed directly.
    trust_proxy: bool = False

    cache_dir: str = ".cache"
    cache_ttl_seconds: int = 86_400  # a day, per the PRD's cost note
    request_timeout_seconds: float = 12.0
    max_concurrent_requests: int = 12

    @property
    def maps_enabled(self) -> bool:
        return bool(self.google_maps_api_key)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Radius searched for venues around each shortlisted neighbourhood centroid.
NEIGHBOURHOOD_RADIUS_M = 1200
# How many shortlisted neighbourhoods get a Places query in Node D. The
# shortlist is ordered by fairness, so this is itself a fairness filter applied
# before the weights are ever consulted: a perfect match sitting in the
# sixth-fairest neighbourhood never gets searched, whatever the sliders say.
# The window therefore widens with the preference weight, up to the number of
# neighbourhoods that exist.
NEIGHBOURHOODS_SEARCHED = 3
NEIGHBOURHOODS_SEARCHED_MAX = 4
# Cap on venues sent to the LLM re-ranker in one call.
LLM_RERANK_LIMIT = 30
