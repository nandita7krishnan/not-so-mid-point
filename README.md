# Not-So-Mid-Point

**Find a meeting spot that's actually fair to both of you.**

Two people, two starting points, and the perennial question of where to meet.
The usual answer — pick something "in the middle" — quietly favours whoever has
the better transit connection. Not-So-Mid-Point measures what each journey
really costs, in travel time and transfers, then finds places that are fair on
both counts *and* somewhere you both want to be.

It does not compute a geographic midpoint. It asks Google what each person's
trip to each candidate area actually takes, and optimises from there.

---

## What it does

Give it two addresses and what you're both up for. It returns three ranked
suggestions, each showing both journeys in full:

```
1. Elliott Bay Trail — 4.9★ — Belltown
   Fremont       16 min   drive · 4.0 km
   West Seattle  20 min   direct · Bus 50
   Difference between you: 4 min
```

- **Per-person travel modes** — bus/train, drive, bike, or walk, chosen
  independently. A driver and a rider meeting in the middle land somewhere quite
  different from two riders.
- **Two definitions of fair**, as an explicit toggle: minimise the *gap* between
  your journeys, or minimise the *total*. They give genuinely different answers,
  and the choice is yours rather than buried in a scoring function.
- **Adjustable priorities** — fairness, preference match, and transfer count, as
  sliders that always total 100%.
- **Interest matching** — pick categories, or just describe what you want
  ("running trail", "somewhere quiet with outdoor seating").
- **Hard limits** — maximum travel time and, when someone's on transit, maximum
  transfers.
- **Address autocomplete** so "Fremont" doesn't silently resolve to California.
- **Honest failure.** When nothing fits, it tells you what to relax: *"Try to
  raise the travel time limit to about 45 min (closest option: Belltown)."*

Seeded with ~50 Seattle-area neighbourhoods; elsewhere it falls back to sampling
and reverse geocoding.

---

## How it works

Six specialised nodes in a LangGraph state graph. The two reachability nodes run
concurrently — that parallelism is why this is a graph and not a loop.

```
Node 0  transit-aware search area
   ├─ Node A  person 1 reachability ─┐
   └─ Node B  person 2 reachability ─┴─ Node C  shortlist + fairness
                                        → Node D  venue discovery
                                        → Node E  scoring
                                        → Node F  validation → top 3
```

**[METHODOLOGY.md](METHODOLOGY.md) documents the algorithm in full** — the
corridor geometry, both fairness functions and why the ceiling constant is 0.4,
the Bayesian rating shrinkage, and why the scoring uses absolute scales rather
than min–max normalisation.

---

## Quick start

Requires Python 3.10+ (LangGraph) and a Google Maps Platform key.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt
cp backend/.env.example backend/.env      # add your key
cd backend && ../.venv/bin/python -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

### API keys

`GOOGLE_MAPS_API_KEY` needs these APIs enabled, and **billing active on the
project** — Maps rejects every request without it:

| API | Used by |
|---|---|
| Geocoding | resolving typed addresses |
| Distance Matrix | Node 0's corridor sweep |
| Directions | Nodes A/B — travel time *and* transfer counts |
| Places API (**New**) | venue search, autocomplete, place details |

Places API **(New)** is a separate product from the legacy "Places API" with a
nearly identical name in the console. The legacy one will not work.

`ANTHROPIC_API_KEY` is optional. Without it, free-text preferences go straight to
Places text search and the explanations come from templates. With it, Claude
expands your description into search types and re-ranks results by fit.

---

## Costs

≈ **$0.41 per cold search**, and **$0 for a repeat** — responses cache for 24
hours, so adjusting sliders on the same pair is free.

| SKU | Calls | Cost | Free/month | Free searches |
|---|---:|---:|---:|---:|
| Distance Matrix | 32 | $0.160 | 10,000 | **312** |
| Directions | 16 | $0.080 | 10,000 | 625 |
| Places Nearby/Text | 5 | $0.160 | 5,000 | 1,000 |
| Place Details / Geocoding | 2 | $0.010 | 10,000 | 5,000 |

Distance Matrix bills **per element** (origins × destinations), which makes it
the binding constraint despite being only two requests. `SWEEP_LIMIT` (16) and
`MAX_CANDIDATE_NEIGHBOURHOODS` (8) are the dials — see
[METHODOLOGY.md §9](METHODOLOGY.md#9-cost-model).

Set a budget alert before experimenting. Prices from Google's pricing page,
August 2026 — verify against Console → Billing → Reports.

---

## Tests

```bash
cd backend && ../.venv/bin/python -m pytest -q
```

48 tests run against a fake Maps client, so the whole graph — including the
parallel fan-out, every failure mode, and each scoring rule — is exercised
without spending an API call. Most were written as regressions against specific
bugs found in live output; those cases are documented in the methodology.

---

## Layout

```
backend/app/
  graph.py            LangGraph wiring
  state.py            the shared state object
  nodes/              one module per node
  providers/maps.py   Google Maps client — cached, concurrency-limited
  providers/llm.py    Claude calls, optional with fallbacks
  data/seattle.py     seeded neighbourhood centroids
frontend/             form, results, Leaflet map
```

`GET /api/health` reports which keys are configured.
`POST /api/recommend` runs the graph.
`POST /api/places/autocomplete` backs the address fields.

---

## Not in v1

No accounts or saved history. Two people only. Scheduled transit data rather
than live disruptions. Web only.
