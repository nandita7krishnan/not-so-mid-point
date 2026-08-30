"""Recover search records from a host's log stream.

A free-tier instance has an ephemeral disk: `evals/log/` is gone at the next
restart, and on Render that is every deploy and every wake from sleep. So each
record is also emitted as one log line, and the host's log retention becomes
the durable copy.

    # Render dashboard > the service > Logs > Download, then:
    python -m evals.from_logs ~/Downloads/render-logs.txt
    python -m evals.judge evals/log

Anything that is not a record is ignored, so the raw stream can be passed in
whole -- request lines, tracebacks, the lot. Records are de-duplicated by id,
which makes overlapping downloads safe to concatenate.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator

from app.searchlog import STDOUT_MARKER

from . import dataset

OUT = Path("evals/log/searches-from-logs.jsonl")


def extract(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Every record in a log stream, in the order it appears.

    The marker is searched for rather than anchored, because a host prefixes
    its own timestamp and stream name to every line.
    """
    for line in lines:
        marker = line.find(STDOUT_MARKER + " ")
        if marker < 0:
            continue
        try:
            record = json.loads(line[marker + len(STDOUT_MARKER) + 1:])
        except json.JSONDecodeError:
            continue  # a truncated line costs that record, not the run
        if isinstance(record, dict) and "request" in record:
            yield record


def dedupe(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for record in records:
        seen.setdefault(record.get("id") or str(len(seen)), record)
    return list(seen.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="*", type=Path, help="log files; omit to read stdin")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    if args.logs:
        lines: list[str] = []
        for path in args.logs:
            lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
    else:
        lines = sys.stdin.read().splitlines()

    records = dedupe(extract(lines))
    if not records:
        print(f"no records found in {len(lines)} lines")
        return 1

    dataset.write(args.out, records)
    print(f"  {len(records)} records -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
