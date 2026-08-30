# Building the eval set

Two questions look alike and are not. *How much is this used?* is answered by
the counters at `/api/stats`. *Are the suggestions any good?* needs the actual
question and the actual answer, and that is what this describes.

The tension is that the most useful thing to record — where each person started
— is the one genuinely personal thing a search carries. `Location.label` is
Google's `formatted_address`, so for a typed house number the label *is* the
doorstep. The way out is to stop asking one record to be both realistic and
personal.

## Two sources, doing different jobs

**Real searches supply the distribution.** Party size, mode mix, budgets, weight
settings, roughly how far apart people start. Coarsening blurs none of that, so
the log can stay as coarse as it likes. Off unless `SEARCH_LOG_ENABLED=true`.

**Synthetic cases supply the origins**, drawn from published transit stops.
Exact coordinates, nobody's home, and as many as you want.

## Anchors: a sharper name that still is not a home

Snapping a start to the nearest of ~50 neighbourhood centroids is safe but
coarse — Ballard is a whole district in one word. Anchors buy the precision
back. Build the file once:

```bash
cd backend
python -m evals.fetch_anchors      # King County Metro + Sound Transit GTFS
```

That writes `data/anchors.csv` (`name,lat,lng`) from the feeds' `stops.txt`. It
is not committed: feeds change every few weeks and a stale stop list is worse
than none. With no file, every lookup misses and behaviour is exactly what it
was before anchors existed.

What changes with the file present:

| | Without anchors | With anchors |
|---|---|---|
| `area` | `Ballard` (~50 possible) | `NW Market St & 22nd Ave NW` |
| `area_coarse` | `Ballard` | `Ballard` — kept, because a stop name is sharper and much harder to read |
| coordinate | the real one, rounded to ~1.1 km | the stop's own published point |
| precision | district | ~200–400 m in the city |

The second row is the interesting one. A snapped record carries a coordinate
somebody published, not a blurred version of a real one — so it is *both* more
precise and less revealing than rounding the truth. It is also the more honest
origin for anyone not driving: a transit journey starts at a stop.

**The density gate** is what keeps this from being a loophole. A stop that is
the only one for a mile serves a handful of houses, so naming it comes close to
naming one of them. A stop is used only where at least three *distinctly named*
anchors sit within 500 m of the point — distinct by name because a street corner
is usually two stops, one per direction, and one corner should not satisfy two
thirds of the gate on its own. Everywhere else the lookup returns nothing and
the neighbourhood snap runs instead, which is why `area_source` (`stop`, `seed`,
or `label`) is recorded next to every name.

Against the real Puget Sound feeds (5,807 anchors), that answers for 46 of the
50 seeded neighbourhoods, naming a stop a median 177 m from the point — against
the ~1.1 km the rounding gave. It refuses in the four sparsest, and everywhere
outside the metro.

A stop is also only used within 600 m of the point. That ceiling matters more
than it looks: at 1.2 km a snapped coordinate could sit *further* from the truth
than the 2dp rounding it was supposed to improve on, making the record sharper
in name only. Against the real feeds it costs nothing — the worst stop is 392 m
away.

Both gate thresholds are settings: `SEARCH_LOG_ANCHOR_MIN_NEIGHBOURS`,
`SEARCH_LOG_ANCHOR_RADIUS_M`.

## Synthetic cases

```bash
python -m evals.runner --record --count 12    # calls Google, freezes the answers
python -m evals.runner                        # replays, costs nothing
python -m evals.judge evals/synthetic         # grade what came out
```

`--record` draws cases, runs them through the real graph, and writes records in
exactly the schema the search log writes — so `checks.py` and `judge.py` cannot
tell a synthetic search from a real one. Pass `--log evals/log` to draw from
what real traffic actually looks like instead of the built-in defaults; below 20
records the defaults stand, because three searches are one person's habits
rather than a population.

Cases are round-robined across five shapes, so a short run cannot skip the one
most likely to be broken:

| Shape | What it is for |
|---|---|
| `typical` | a few km apart, mixed modes, ordinary budget |
| `same_block` | everyone already together, so fairness cannot separate the candidates |
| `one_far_out` | somebody 25–40 km away, where no answer is fair and the failure message has to be honest |
| `all_transit_edges` | the case the tool exists for, and the one real traffic rarely produces |
| `mixed_mode` | a driver and a rider, who meet somewhere quite different from two riders |

Everything is drawn from a seeded RNG, case ids included, so `--seed 7` is the
same twelve searches every time.

### Why exact origins pay for themselves

Each `--record` run costs real Places quota, so the Maps traffic is frozen per
case into `evals/fixtures/` and replayed after that. Change the scoring, re-run
the suite, spend nothing — the same journeys and the same venues, ranked
differently. A replay that asks something the fixture does not hold fails
loudly rather than quietly making a live call.

This only works because the origins are exact. A real logged search is rounded
to ~1.1 km, so replaying one is not replaying the search that happened: the
frozen answers would belong to a different question.

Two caveats worth knowing. The fixtures are **not committed** — Google's terms
cap how long most Places content may be cached, so re-record rather than share
them. And the fixtures freeze Maps, not Claude: runs use the graph's
deterministic fallbacks unless you pass `--llm`, which costs a live call per run
and gives answers that vary between runs.

## On the deployed app

The blueprint turns the log on (`SEARCH_LOG_ENABLED=true`) and builds the anchor
file during the deploy, so a live search is recorded with a stop name rather
than a district.

The catch is the disk. Render's filesystem is ephemeral — every deploy and every
wake from sleep starts with an empty one — so `evals/log/` on the instance is a
convenience, not storage. Each record is therefore also emitted as a single log
line prefixed `SEARCHLOG`, and the host's log retention is the durable copy:

```bash
# Render dashboard > the service > Logs > Download
python -m evals.from_logs ~/Downloads/render-logs.txt
python -m evals.judge evals/log
```

Records are de-duplicated by id, so overlapping downloads can be concatenated
without care. `SEARCH_LOG_TO_STDOUT=false` turns the log line off if you would
rather it not be there.

## What a record holds

Everything except who searched: the selections (categories or free text, budget,
weights, fairness mode, departure hour and weekday), every person's leg (mode,
minutes, transfers, reachable), the shortlist, and all three results in full —
venue, address, rating, component scores and the "why" line. Participants are
`P1`…`P5`; the typed address is never written. Venue details are kept whole,
since a business address is public.
