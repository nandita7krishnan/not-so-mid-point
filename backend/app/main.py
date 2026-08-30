"""FastAPI surface: one endpoint that runs the graph, plus the static frontend."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from .config import get_settings  # noqa: E402  (must follow load_dotenv)
from .graph import run_graph  # noqa: E402
from .providers.llm import LLMClient  # noqa: E402
from .providers.maps import MapsClient, MapsError  # noqa: E402
from .ratelimit import (  # noqa: E402
    GLOBAL_KEY,
    RateLimitError,
    Rule,
    SlidingWindowLimiter,
    client_key,
)
from . import payload as payload_builder  # noqa: E402
from . import searchlog  # noqa: E402
from .runtime import RunDeps  # noqa: E402
from .stats import STATS  # noqa: E402
from .state import (  # noqa: E402
    MAX_PARTIES,
    MIN_PARTIES,
    Budget,
    FairnessMode,
    LatLng,
    Person,
    TravelMode,
    Weights,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("not-so-mid-point")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

app = FastAPI(title="Not-So-Mid-Point", version="1.0")

_limiter = SlidingWindowLimiter()
_SWEEP_EVERY = 500
_requests_seen = 0


def _rules() -> dict[str, Rule]:
    settings = get_settings()
    return {
        "recommend_hour": Rule(settings.recommend_per_hour, 3600),
        "recommend_day": Rule(settings.recommend_per_day, 86_400),
        "autocomplete": Rule(settings.autocomplete_per_minute, 60),
        "global_day": Rule(settings.global_recommend_per_day, 86_400),
    }


def _who(request: Request) -> str:
    settings = get_settings()
    return client_key(
        request.headers.get("x-forwarded-for"),
        request.client.host if request.client else None,
        settings.trust_proxy,
    )


def _enforce(request: Request, scope: str, rules: list[tuple[str, Rule]]) -> None:
    """Apply each rule in turn, cheapest window first."""
    global _requests_seen
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return

    _requests_seen += 1
    if _requests_seen % _SWEEP_EVERY == 0:
        _limiter.sweep(86_400)

    who = _who(request)
    for name, rule in rules:
        key = GLOBAL_KEY if name.startswith("global") else f"{scope}:{name}:{who}"
        retry_after = _limiter.check(key, rule)
        if retry_after is None:
            continue
        STATS.record("blocked_global" if name.startswith("global") else "blocked_personal", who)
        if name.startswith("global"):
            message = (
                "This instance has hit its daily search limit, which exists to cap "
                "Google Maps costs. Try again tomorrow, or run your own copy."
            )
        else:
            message = (
                f"Too many searches from your connection ({rule.describe()}). "
                "Try again shortly."
            )
        raise RateLimitError(retry_after, message)


@app.exception_handler(RateLimitError)
async def _rate_limited(request: Request, exc: RateLimitError) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": exc.message},
        headers={"Retry-After": str(exc.retry_after)},
    )


class AutocompleteRequest(BaseModel):
    q: str = Field(..., max_length=200)
    session: str = Field("", max_length=100)


class PartyRequest(BaseModel):
    address: str = Field(..., min_length=2)
    mode: TravelMode = "driving"
    # Set when the address came from the autocomplete dropdown. Resolving a
    # place_id is unambiguous and closes the billing session opened by typing,
    # so it replaces the Geocoding call entirely.
    place_id: str = ""
    session: str = ""
    label: str = ""


class RecommendRequest(BaseModel):
    people: list[PartyRequest] = Field(
        ..., min_length=MIN_PARTIES, max_length=MAX_PARTIES
    )
    categories: list[str] = Field(default_factory=list)
    free_text: str = ""
    max_time_min: int = Field(45, ge=5, le=180)
    max_transfers: int = Field(2, ge=0, le=5)
    weights: Weights = Field(default_factory=Weights)
    fairness_mode: FairnessMode = "gap"
    require_open: bool = False
    departure_time: Optional[int] = Field(
        None, description="Unix seconds. Defaults to 30 minutes from now."
    )


@app.get("/api/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "ok": settings.maps_enabled,
        "max_parties": MAX_PARTIES,
        "maps_configured": settings.maps_enabled,
        "rate_limited": settings.rate_limit_enabled,
        "llm_configured": settings.llm_enabled,
        "llm_model": settings.llm_model if settings.llm_enabled else None,
    }


@app.get("/api/stats")
async def stats(days: int = 14) -> dict[str, Any]:
    """Aggregate usage. No IP addresses, no locations, no per-person history."""
    return STATS.snapshot(days=max(1, min(days, 14)))


@app.post("/api/places/autocomplete")
async def autocomplete(request: AutocompleteRequest, http_request: Request) -> dict[str, Any]:
    rules = _rules()
    _enforce(http_request, "autocomplete", [("autocomplete", rules["autocomplete"])])
    STATS.record("autocompletes", _who(http_request))
    settings = get_settings()
    if not settings.maps_enabled:
        return {"suggestions": []}
    maps = MapsClient()
    try:
        suggestions = await maps.autocomplete(
            request.q,
            request.session,
            LatLng(lat=settings.autocomplete_bias_lat, lng=settings.autocomplete_bias_lng),
            settings.autocomplete_bias_radius_m,
        )
    except MapsError as exc:
        # A failed suggestion lookup must never block typing.
        log.warning("autocomplete failed: %s", exc)
        return {"suggestions": []}
    finally:
        await maps.aclose()
    return {"suggestions": suggestions}


@app.post("/api/recommend")
async def recommend(request: RecommendRequest, http_request: Request) -> dict[str, Any]:
    rules = _rules()
    # Cheapest window first, and the global cap last so an individual's limit is
    # reported before the instance-wide one.
    _enforce(
        http_request,
        "recommend",
        [
            ("recommend_hour", rules["recommend_hour"]),
            ("recommend_day", rules["recommend_day"]),
            ("global_day", rules["global_day"]),
        ],
    )
    settings = get_settings()
    if not settings.maps_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "GOOGLE_MAPS_API_KEY is not set. Copy backend/.env.example to "
                "backend/.env and add a key."
            ),
        )

    # Transit routing needs a future departure time; default to soon-ish.
    departure = request.departure_time or int(time.time()) + 1800

    maps = MapsClient()
    llm = LLMClient() if settings.llm_enabled else None
    try:
        async def resolve(party: PartyRequest):
            if party.place_id:
                return await maps.place_location(party.place_id, party.session)
            return await maps.geocode(party.address)

        try:
            locations = await asyncio.gather(*(resolve(p) for p in request.people))
        except MapsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        people = [
            Person(
                label=party.label or f"Person {i + 1}",
                location=location,
                mode=party.mode,
            )
            for i, (party, location) in enumerate(zip(request.people, locations))
        ]

        try:
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
        except MapsError as exc:
            STATS.record("upstream_errors", _who(http_request))
            raise HTTPException(status_code=502, detail=f"Google Maps API error: {exc}") from exc
    finally:
        await maps.aclose()

    failure = state.get("failure")
    who = _who(http_request)
    STATS.record("searches_no_result" if failure else "searches_ok", who)
    payload = payload_builder.build(
        state=state, people=people, request=request, departure=departure
    )
    searchlog.record(
        people=people,
        request=request,
        response=payload,
        departure=departure,
        visitor=STATS.visitor_id(who),
    )
    return payload


if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        # Stamp the asset links with the files' mtimes so an edited script is
        # never served from browser cache during development.
        html = (FRONTEND_DIR / "index.html").read_text()
        for asset in ("app.js", "styles.css"):
            stamp = int((FRONTEND_DIR / asset).stat().st_mtime)
            html = html.replace(f"/static/{asset}", f"/static/{asset}?v={stamp}")
        return HTMLResponse(html)
