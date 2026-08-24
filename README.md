# Not-So-Mid-Point

**Find a meeting spot that's actually fair to everyone.**

Two to five people, scattered starting points, and the perennial question of
where to meet. The usual answer — pick something "in the middle" — quietly
favours whoever has the better transit connection. Not-So-Mid-Point measures
what each journey really costs, in travel time and transfers, then finds places
that are fair on those counts *and* somewhere the group actually wants to be.

It does not compute a geographic midpoint. It asks Google what each person's
trip to each candidate area actually takes, and optimises from there.

---

## What it does

Give it everyone's addresses and what the group is up for. It returns three
ranked suggestions, each showing every journey in full:

```
1. Storyville Coffee Pike Place — Belltown
   Ana    40 min   transit    D Line
   Ben    19 min   drive      9.6 km
   Cleo   42 min   walk       2.9 km
   Dev    36 min   bike       9.5 km
   Spread across you: 22 min · combined 137 min
```

- **Two to five people.** Add and remove participants freely; the fairness maths
  generalises rather than special-casing pairs.
- **Per-person travel modes** — bus/train, drive, bike, or walk, chosen
  independently. A driver and a rider meeting in the middle land somewhere quite
  different from two riders.
- **Two definitions of fair**, as an explicit toggle: minimise the *spread*
  between the best- and worst-off person, or minimise the *total*. They give genuinely different answers,
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
   ├─ reach_person1 ─┐
   ├─ …              ├─ Node C  shortlist + fairness
   └─ reach_person5 ─┘     → Node D  venue discovery
                           → Node E  scoring
                           → Node F  validation → top 3
```

There is a static reachability node per party slot, so all of them run in a
single superstep however many people are involved; unused slots return
immediately.

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

**$0 for a repeat** — responses cache for 24 hours, so adjusting sliders on the
same group is free. A cold search costs, by party count:

| Parties | Cost/search | Free searches/month |
|---:|---:|---:|
| 2 | $0.41 | 312 |
| 3 | $0.54 | 208 |
| 5 | $0.79 | 125 |

Routing scales linearly with the group (N × 16 Distance Matrix elements, N × 8
Directions calls); only the venue search is fixed at 5 calls. Distance Matrix
bills **per element** (origins × destinations), which makes it the binding
constraint despite being one request per person. `SWEEP_LIMIT` (16) and
`MAX_CANDIDATE_NEIGHBOURHOODS` (8) are the dials — see
[METHODOLOGY.md §9](METHODOLOGY.md#9-cost-model).

Set a budget alert before experimenting. Prices from Google's pricing page,
August 2026 — verify against Console → Billing → Reports.

---

## Tests

```bash
cd backend && ../.venv/bin/python -m pytest -q
```

65 tests run against a fake Maps client, so the whole graph — including the
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

No accounts or saved history. Five participants maximum. Scheduled transit data
rather than live disruptions. Web only.
