# Not-So-Mid-Point

Finding the best meeting spot for a group, by travel time rather than geography.

Live at <https://not-so-mid-point.onrender.com/>. It runs on a free host that
sleeps, so the first search after a quiet spell takes about a minute to wake the
instance; after that it is normal speed.

Two to five people, scattered starting points, and the question of where to
meet. Picking something "in the middle" on a map quietly favours whoever has the
better connection to it — the geographic midpoint between a driver and a bus
rider is usually a short hop for one of them and a long haul for the other.

This tool works from journeys instead. It asks Google what each person's trip to
each candidate area actually takes, in travel time and number of transfers, and
ranks areas on how evenly that burden falls — then looks for places within the
best areas that match what the group is actually up for.

---

## What it looks like

Three people meeting for coffee or a park: Ana driving from Ballard, Ben on the
bus from Columbia City, Cass driving from the U District.

![The form, with a card per person: their name, starting address, and travel mode chosen independently](docs/screenshot-inputs.jpg)

Every answer shows its work. The map marks where each person starts, by their
initials in their own colour, alongside the three ranked spots. Each suggestion
then breaks down what the trip actually costs every person, and how the spot
scored on each component.

![Results: a map of the three starting points and three ranked spots, above the top result showing each person's travel time and the fairness, preference, transfer and final score bars](docs/screenshot-results.jpg)

---

## What it does

Give it everyone's addresses and what the group is up for. It returns three
ranked suggestions, each showing every journey in full:

```
1. Storyville Coffee Pike Place · Belltown
   Ana    40 min   transit    D Line
   Ben    19 min   drive      9.6 km
   Cleo   42 min   walk       2.9 km
   Dev    36 min   bike       9.5 km
   Spread across you: 22 min · combined 137 min
```

- **Two to five people.** Add and remove participants freely; the fairness maths
  generalises rather than special-casing pairs. Everyone starts as Person A, B,
  C… and can be renamed. The names carry through to the results and the map.
- **Per-person travel modes**: drive, bus/train, bike, or walk, chosen
  independently. A driver and a rider meeting in the middle land somewhere quite
  different from two riders.
- **Two definitions of fair**, as an explicit toggle: minimise the *spread*
  between the best- and worst-off person, or minimise the *total*. They give genuinely different answers,
  and the choice is yours rather than buried in a scoring function. It starts on
  spread for two people and on total for three or more, where holding the spread
  even tends to drag everyone toward a middle nobody chose.
- **Adjustable priorities**: fairness, preference match, and transfer count, as
  sliders that always total 100%.
- **Interest matching**: pick categories, or just describe what you want
  ("running trail", "somewhere quiet with outdoor seating").
- **Hard limits**: maximum travel time and, when someone's on transit, maximum
  transfers.
- **Address autocomplete** so "Fremont" doesn't silently resolve to California.
- **A diagnosis when nothing fits**, rather than an empty list: *"Try to raise
  the travel time limit to about 45 min (closest option: Belltown)."* The number
  comes from the closest candidate that missed.
- **A help dialog** (the `?`, top right) explaining what the tool does and how
  fairness and ranking are calculated, without leaving the page.

Seeded with ~50 Seattle-area neighbourhoods; elsewhere it falls back to sampling
and reverse geocoding.

---

## How it works

Six specialised nodes in a LangGraph state graph. The two reachability nodes run
concurrently. That parallelism is why this is a graph and not a loop.

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

**[METHODOLOGY.md](METHODOLOGY.md) documents the algorithm in full**: the
corridor geometry, both fairness functions and why the ceiling constant is 0.4,
the Bayesian rating shrinkage, and why the scoring uses absolute scales rather
than min–max normalisation.

---

## Are the answers any good?

Usage counters say how much the tool is used. They say nothing about whether the
suggestions were any good, so there is a grading harness for that. It runs in
two passes, and the split is the point.

**Deterministic checks** settle anything arithmetic — a pick over the stated
travel budget, a transit leg over the transfer budget, an unrated venue in a
ranking that claims to use ratings, three "different" suggestions on one block.
These are contract violations rather than opinions, so a model is never asked to
adjudicate them.

Then **a model scores what's left** out of 5: does the venue match what was
asked for, is the travel burden defensibly shared, would a group actually want
to meet there, and does the "why" line state only what the journey data
supports. It is shown the journeys rather than the app's own fairness and
preference numbers, so it judges the outcome instead of grading the scorer's
homework.

That last criterion has already caught something: the templated copy calls a
trip "an even trip for both of you" whenever the spread is under 5 minutes,
which reads oddly next to a 4-minute spread on a 12-minute journey.

The cases it grades come from two places. Real searches are recorded only if
the log is switched on, and are coarsened on the way in — the typed address is
dropped, a start is named by a public transit stop or the neighbourhood it falls
in, and names become `P1`…`P5`. Synthetic cases are drawn from published transit
stops instead, which gives exact coordinates without anyone's home in the file,
and lets a case be replayed against frozen Maps responses when the scoring
changes.

**[docs/EVALS.md](docs/EVALS.md)** covers both, including the density gate that
decides when naming a stop is too sharp.

---

## Layout

```
backend/app/
  graph.py            LangGraph wiring
  state.py            the shared state object
  nodes/              one module per node
  providers/maps.py   Google Maps client, cached and concurrency-limited
  providers/llm.py    Claude calls, optional with fallbacks
  data/seattle.py     seeded neighbourhood centroids
  searchlog.py        coarsened record of each search, as eval input
backend/evals/
  checks.py           deterministic checks; contract violations
  judge.py            model grading + summary, `python -m evals.judge`
frontend/             form, results, Leaflet map
```

`POST /api/recommend` runs the graph, `POST /api/places/autocomplete` backs the
address fields, and `GET /api/health` and `/api/stats` report configuration and
usage.

Running your own copy, hosting it, and the API keys and quotas that needs:
**[docs/OPERATIONS.md](docs/OPERATIONS.md)**.

---

## Not in v1

No accounts or saved history. Five participants maximum. Scheduled transit data
rather than live disruptions. Web only.
