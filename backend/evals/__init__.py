"""Offline evaluation over the search log.

`app/searchlog.py` writes what was asked and what came back. This package turns
that into a graded dataset: deterministic checks that cost nothing, plus a
model judging the answer against criteria the deterministic checks can't reach.
"""
