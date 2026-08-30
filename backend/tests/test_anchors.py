"""The anchor lookup, and the density gate that keeps it from naming a house."""
from __future__ import annotations

import os

from app import anchors
from app.geo import haversine_m, offset
from app.state import LatLng

from conftest import CORE, anchor_rows


def write(path, rows):
    lines = ["name,lat,lng"] + [f"{n},{lat},{lng}" for n, lat, lng in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_missing_file_is_not_an_error(tmp_path):
    assert len(anchors.load(tmp_path / "nope.csv")) == 0


def test_a_malformed_row_costs_that_row_not_the_file(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text(
        "name,lat,lng\nGood,47.6,-122.3\nNoCoords,,\n,47.6,-122.3\nBad,x,y\nAlso Good,47.61,-122.31\n",
        encoding="utf-8",
    )
    assert {a.name for a in anchors.load(path).anchors} == {"Good", "Also Good"}


def test_it_returns_the_closest_anchor_where_stops_are_dense(tmp_path):
    loaded = anchors.load(write(tmp_path / "a.csv", anchor_rows()))
    known = LatLng(lat=CORE.lat + 0.002, lng=CORE.lng)
    found = loaded.nearest(offset(known, 40, 40))
    assert found is not None
    assert haversine_m(found.coords, known) < 60


def test_it_names_nothing_where_stops_are_sparse(tmp_path):
    """The gate, which is the whole reason this is safe to log.

    Out in the fringe the nearest stop is kilometres away, so it stands for a
    handful of houses rather than a catchment, and the caller must fall back to
    the coarser neighbourhood name.
    """
    loaded = anchors.load(write(tmp_path / "a.csv", anchor_rows()))
    fringe = LatLng(lat=CORE.lat + 0.35, lng=CORE.lng + 0.49)
    assert loaded.nearest(fringe) is None
    # ... and the sparseness is what refused it, not the absence of anchors.
    assert loaded.nearest(fringe, min_neighbours=1, max_distance_m=20_000) is not None


def test_min_neighbours_counts_stops_around_the_point(tmp_path):
    rows = [("Lone", 47.7, -122.4), ("Pair", 47.7003, -122.4)]
    loaded = anchors.load(write(tmp_path / "a.csv", rows))
    point = LatLng(lat=47.7001, lng=-122.4)
    assert loaded.nearest(point, min_neighbours=3) is None
    assert loaded.nearest(point, min_neighbours=2) is not None


def test_one_corner_cannot_satisfy_the_gate_twice(tmp_path):
    """Real feeds carry a stop per direction, sometimes far enough apart to
    survive de-duplication. Counting rows would let a single corner stand in
    for two of the three places the gate asks for."""
    rows = [
        ("Market St & 22nd Ave", 47.7000, -122.4),
        ("Market St & 22nd Ave", 47.7015, -122.4),  # the other direction
        ("Ballard Ave & 20th Ave", 47.7008, -122.4),
    ]
    loaded = anchors.load(write(tmp_path / "a.csv", rows))
    point = LatLng(lat=47.7008, lng=-122.4)
    assert loaded.nearest(point, min_neighbours=3) is None
    assert loaded.nearest(point, min_neighbours=2) is not None


def test_a_distant_stop_is_refused_even_in_a_dense_feed(tmp_path):
    """Dense enough to pass the gate, too far to describe where you started."""
    rows = [(f"S{i}", 47.70 + i * 0.001, -122.40) for i in range(6)]
    loaded = anchors.load(write(tmp_path / "a.csv", rows))
    point = LatLng(lat=47.7025, lng=-122.40)
    assert loaded.nearest(point, radius_m=1000, max_distance_m=500) is not None
    assert loaded.nearest(point, radius_m=1000, max_distance_m=50) is None


def test_a_rebuilt_file_is_picked_up_without_a_restart(tmp_path):
    """Feeds are refreshed by a script; a cached parse must not outlive one."""
    path = write(tmp_path / "a.csv", [("Old", 47.6, -122.3)])
    assert [a.name for a in anchors.load(path).anchors] == ["Old"]

    write(path, [("New", 47.6, -122.3)])
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))
    assert [a.name for a in anchors.load(path).anchors] == ["New"]
