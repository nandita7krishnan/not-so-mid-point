"""Pull search records out of Render's log stream onto disk.

The instance's own `evals/log/` is on an ephemeral filesystem, and the log
stream behind it is retained for a limited window. Neither is storage. This
copies the records somewhere they will outlive both:

    export RENDER_API_KEY=rnd_...        # dashboard > Account Settings > API Keys
    export RENDER_SERVICE_ID=srv-...     # in the service's URL
    python -m evals.pull                 # the last 24 hours
    python -m evals.pull --since 7d      # or as far back as retention goes

Costs nothing. It reads Render's API and writes a file; no Google call, no
model call. Hourly from cron is polite and more than enough:

    0 * * * * cd ~/point-not-so-mid/backend && .venv/bin/python -m evals.pull

Records are merged into one file per day and de-duplicated by id, so
overlapping windows are safe and a re-run is idempotent.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import dataset, from_logs

API = "https://api.render.com/v1"
OUT_DIR = Path("evals/log")
PAGE_LIMIT = 100
# Render caps how far back the log API will look; asking for more is not an
# error, it just returns what it has.
MAX_PAGES = 200

_SINCE = re.compile(r"^(\d+)([hd])$")


class PullError(RuntimeError):
    """Something about the credentials or the API response was wrong."""


def parse_since(value: str) -> timedelta:
    match = _SINCE.match(value.strip().lower())
    if not match:
        raise PullError(f"--since wants something like 24h or 7d, not {value!r}")
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(hours=amount) if unit == "h" else timedelta(days=amount)


def _get(url: str, key: str) -> Any:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        hint = {
            401: "check RENDER_API_KEY",
            404: "check RENDER_SERVICE_ID",
        }.get(exc.code, "")
        raise PullError(f"Render API returned {exc.code}{': ' + hint if hint else ''}") from exc
    except urllib.error.URLError as exc:
        raise PullError(f"could not reach the Render API: {exc.reason}") from exc


def owner_id(service_id: str, key: str) -> str:
    """The logs endpoint is scoped by owner, which only the service knows."""
    body = _get(f"{API}/services/{service_id}", key)
    service = body.get("service", body) if isinstance(body, dict) else {}
    owner = service.get("ownerId") or service.get("owner", {}).get("id")
    if not owner:
        raise PullError("no ownerId on the service response")
    return owner


def messages(page: Any) -> list[str]:
    """The log lines in one API page, however the response is shaped.

    Deliberately forgiving: the only field this needs is the message text, and
    a schema change elsewhere in the payload should not stop a pull.
    """
    entries = page.get("logs", page) if isinstance(page, dict) else page
    if not isinstance(entries, list):
        return []
    out = []
    for entry in entries:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict) and entry.get("message"):
            out.append(str(entry["message"]))
    return out


def fetch_pages(
    *,
    service_id: str,
    key: str,
    start: datetime,
    end: datetime,
    get: Callable[[str, str], Any] = _get,
) -> Iterable[Any]:
    """Walk the log window, following Render's pagination cursor."""
    owner = owner_id(service_id, key) if get is _get else "owner"
    params = {
        "ownerId": owner,
        "resource": service_id,
        "startTime": start.isoformat().replace("+00:00", "Z"),
        "endTime": end.isoformat().replace("+00:00", "Z"),
        "limit": str(PAGE_LIMIT),
        "direction": "backward",
    }
    for _ in range(MAX_PAGES):
        page = get(f"{API}/logs?{urllib.parse.urlencode(params)}", key)
        yield page
        if not isinstance(page, dict) or not page.get("hasMore"):
            return
        cursor = page.get("nextEndTime") or page.get("nextStartTime")
        if not cursor or cursor == params["endTime"]:
            return  # no forward progress; stop rather than spin
        params["endTime"] = cursor


def merge(out_dir: Path, records: list[dict[str, Any]]) -> tuple[int, int]:
    """Fold records into one file per UTC day. Returns (new, total on disk).

    Existing records win over pulled ones with the same id: what is already on
    disk has been kept deliberately, and a re-pull should never rewrite it.
    """
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_day[str(record.get("ts", ""))[:10] or "undated"].append(record)

    added = total = 0
    for day, pulled in sorted(by_day.items()):
        path = out_dir / f"searches-{day}.jsonl"
        existing = list(dataset.read(path)) if path.exists() else []
        seen = {r.get("id") for r in existing}
        fresh = [r for r in pulled if r.get("id") not in seen]
        if fresh:
            merged = sorted(existing + fresh, key=lambda r: str(r.get("ts", "")))
            dataset.write(path, merged)
            print(f"  {len(fresh):>4} new -> {path} ({len(merged)} total)")
        added += len(fresh)
        total += len(existing) + len(fresh)
    return added, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="24h", help="how far back to look, e.g. 24h or 7d")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    key = os.environ.get("RENDER_API_KEY", "").strip()
    service_id = os.environ.get("RENDER_SERVICE_ID", "").strip()
    if not key or not service_id:
        print("set RENDER_API_KEY and RENDER_SERVICE_ID (see this module's docstring)",
              file=sys.stderr)
        return 2

    end = datetime.now(timezone.utc)
    try:
        start = end - parse_since(args.since)
        lines: list[str] = []
        for page in fetch_pages(service_id=service_id, key=key, start=start, end=end):
            lines.extend(messages(page))
    except PullError as exc:
        print(f"  ! {exc}", file=sys.stderr)
        return 1

    records = from_logs.dedupe(from_logs.extract(lines))
    if not records:
        print(f"  no records in {len(lines)} log lines from the last {args.since}")
        return 0

    added, total = merge(args.out, records)
    print(f"  {added} new of {len(records)} pulled; {total} on disk")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
