"""FastAPI surface: one endpoint that runs the graph, plus the static frontend."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from .config import get_settings  # noqa: E402  (must follow load_dotenv)
from .graph import run_graph  # noqa: E402
from .providers.llm import LLMClient  # noqa: E402
from .providers.maps import MapsClient, MapsError  # noqa: E402
from .runtime import RunDeps  # noqa: E402
from .state import Budget, FairnessMode, LatLng, TravelMode, Weights, uses_transit  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("point-not-so-mid")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

app = FastAPI(title="Point-Not-So-Mid", version="1.0")


class AutocompleteRequest(BaseModel):
    q: str = Field(..., max_length=200)
    session: str = Field("", max_length=100)


class RecommendRequest(BaseModel):
    person1: str = Field(..., min_length=2, description="Person 1 starting address")
    person2: str = Field(..., min_length=2, description="Person 2 starting address")
    person1_mode: TravelMode = "transit"
    person2_mode: TravelMode = "transit"
    # Set when the address came from the autocomplete dropdown. Resolving a
    # place_id is unambiguous and closes the billing session opened by typing,
    # so it replaces the Geocoding call entirely.
    person1_place_id: str = ""
    person2_place_id: str = ""
    session1: str = ""
    session2: str = ""
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
        "maps_configured": settings.maps_enabled,
        "llm_configured": settings.llm_enabled,
        "llm_model": settings.llm_model if settings.llm_enabled else None,
    }


@app.post("/api/places/autocomplete")
async def autocomplete(request: AutocompleteRequest) -> dict[str, Any]:
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
async def recommend(request: RecommendRequest) -> dict[str, Any]:
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
        async def resolve(address: str, place_id: str, session: str):
            if place_id:
                return await maps.place_location(place_id, session)
            return await maps.geocode(address)

        try:
            p1, p2 = await asyncio.gather(
                resolve(request.person1, request.person1_place_id, request.session1),
                resolve(request.person2, request.person2_place_id, request.session2),
            )
        except MapsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            state = await run_graph(
                {
                    "person1_location": p1,
                    "person2_location": p2,
                    "person1_mode": request.person1_mode,
                    "person2_mode": request.person2_mode,
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
            raise HTTPException(status_code=502, detail=f"Google Maps API error: {exc}") from exc
    finally:
        await maps.aclose()

    failure = state.get("failure")
    return {
        "ok": failure is None,
        "person1": p1.model_dump(),
        "person2": p2.model_dump(),
        "departure_time": departure,
        "fairness_mode": request.fairness_mode,
        "modes": {"person1": request.person1_mode, "person2": request.person2_mode},
        "transfers_apply": uses_transit(request.person1_mode, request.person2_mode),
        "weights": request.weights.normalized().model_dump(),
        "search_area": _dump(state.get("search_area")),
        "shortlist": [e.model_dump() for e in state.get("shortlisted_neighbourhoods", [])],
        "preference_spec": _dump(state.get("preference_spec")),
        "results": [r.model_dump() for r in state.get("final_top_3", [])],
        "failure": _dump(failure),
        "warnings": state.get("warnings", []),
        "timings": {k: round(v, 3) for k, v in state.get("timings", {}).items()},
    }


def _dump(model: Any) -> Optional[dict[str, Any]]:
    return model.model_dump() if model is not None else None


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
