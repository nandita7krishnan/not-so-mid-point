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
    # Total transfers (both people combined) at which the transfer score hits 0.
    transfer_reference: int = 4

    # --- infrastructure --------------------------------------------------
    # Centre and radius used to bias address autocomplete toward the local area.
    autocomplete_bias_lat: float = 47.62
    autocomplete_bias_lng: float = -122.33
    autocomplete_bias_radius_m: int = 50_000

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
# How many shortlisted neighbourhoods get a Places query in Node D.
NEIGHBOURHOODS_SEARCHED = 5
# Cap on venues sent to the LLM re-ranker in one call.
LLM_RERANK_LIMIT = 30
