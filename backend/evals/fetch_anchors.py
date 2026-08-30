"""Build the public-anchor file from one or more GTFS feeds.

    python -m evals.fetch_anchors                       # the default feeds
    python -m evals.fetch_anchors --feed path/to.zip    # a local copy
    python -m evals.fetch_anchors --out data/anchors.csv

GTFS is the right source because a stop is public infrastructure with a
published name and coordinate: naming one can never leak where somebody lives,
and the name is stable enough to group by across days. `stops.txt` is the only
file read; routes, trips and schedules are irrelevant here.

The output is a `name,lat,lng` CSV read by app/anchors.py. It is not committed:
feeds change every few weeks, and a stale stop list is worse than none.
"""
from __future__ import annotations

import argparse
import csv
import io
import ssl
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable, Iterator

# Puget Sound's two big operators. Between them they cover the seeded area
# densely enough that the density gate passes across the whole city.
DEFAULT_FEEDS = (
    "https://metro.kingcounty.gov/GTFS/google_transit.zip",
    "https://www.soundtransit.org/GTFS-Rail/40_gtfs.zip",
)

# Stops within ~110 m of each other with the same name are the two directions
# of one street-corner pair. One anchor is enough: the gate cares about how
# many distinct places are nearby, and a pair is one place.
_DEDUPE_DP = 3


def read_stops(data: bytes) -> Iterator[tuple[str, float, float]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        name = next(
            (n for n in archive.namelist() if n.rsplit("/", 1)[-1] == "stops.txt"),
            None,
        )
        if name is None:
            raise ValueError("no stops.txt in feed")
        with archive.open(name) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
            for row in csv.DictReader(text):
                stop = (row.get("stop_name") or "").strip()
                try:
                    lat, lng = float(row["stop_lat"]), float(row["stop_lon"])
                except (KeyError, TypeError, ValueError):
                    continue
                if stop and (lat, lng) != (0.0, 0.0):
                    yield stop, lat, lng


def _ssl_context() -> ssl.SSLContext:
    """Verify against certifi's bundle rather than whatever OpenSSL was built
    to look for.

    A Homebrew Python points at `/opt/homebrew/etc/openssl@3/cert.pem`, which
    often does not exist, and the failure is an opaque
    CERTIFICATE_VERIFY_FAILED rather than "no CA file". certifi ships with the
    HTTP client this project already depends on, so it is always there.
    """
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def fetch(feed: str) -> bytes:
    if "://" not in feed:
        return Path(feed).read_bytes()
    request = urllib.request.Request(feed, headers={"User-Agent": "not-so-mid-point/anchors"})
    with urllib.request.urlopen(request, timeout=120, context=_ssl_context()) as response:  # noqa: S310
        return response.read()


def dedupe(stops: Iterable[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    seen: dict[tuple[str, float, float], tuple[str, float, float]] = {}
    for name, lat, lng in stops:
        key = (name.casefold(), round(lat, _DEDUPE_DP), round(lng, _DEDUPE_DP))
        seen.setdefault(key, (name, lat, lng))
    return sorted(seen.values(), key=lambda s: (s[1], s[2]))


def write(path: Path, stops: list[tuple[str, float, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(("name", "lat", "lng"))
        for name, lat, lng in stops:
            writer.writerow((name, f"{lat:.6f}", f"{lng:.6f}"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--feed", action="append", default=[],
        help="GTFS zip URL or local path; repeatable. Defaults to the Puget Sound feeds.",
    )
    parser.add_argument("--out", default="data/anchors.csv", type=Path)
    args = parser.parse_args(argv)

    collected: list[tuple[str, float, float]] = []
    for feed in args.feed or list(DEFAULT_FEEDS):
        try:
            stops = list(read_stops(fetch(feed)))
        except Exception as exc:  # noqa: BLE001 -- one bad feed is not fatal
            print(f"  ! {feed}: {exc}", file=sys.stderr)
            continue
        print(f"  {len(stops):>6} stops from {feed}")
        collected.extend(stops)

    if not collected:
        print("no stops read; anchor file left alone", file=sys.stderr)
        return 1

    anchors = dedupe(collected)
    write(args.out, anchors)
    print(f"  {len(anchors):>6} anchors written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
