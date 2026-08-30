"""Reading and writing the JSONL the log and the grader exchange."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


def read(path: Path) -> Iterator[dict[str, Any]]:
    """Yield records, skipping anything unparseable.

    A half-written final line is normal if the log is read while the server is
    running, and it should cost you that one record rather than the run.
    """
    with path.open(encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                print(f"  ! skipped unparseable line {number} of {path.name}")


def read_all(paths: list[Path]) -> list[dict[str, Any]]:
    return [record for path in paths for record in read(path)]


def resolve(target: Path) -> list[Path]:
    """A directory means every day file in it; a file means that file."""
    if target.is_dir():
        return sorted(target.glob("searches-*.jsonl"))
    return [target] if target.exists() else []


def write(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
