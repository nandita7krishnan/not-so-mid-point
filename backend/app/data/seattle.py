"""Seed neighbourhood centroids for the Seattle / Eastside transit area.

Node 0 uses these as its candidate sample points instead of an arbitrary grid:
a neighbourhood centroid is somewhere transit actually goes and somewhere a
venue search makes sense, whereas a grid point can land in the middle of Lake
Washington. Coordinates are approximate business-district centroids.

Outside this bounding box the graph falls back to a generated sample grid plus
reverse geocoding (see nodes/search_area.py), so the app is not Seattle-only --
Seattle is just where it has good priors.
"""
from __future__ import annotations

from ..state import LatLng, Neighbourhood

# (name, lat, lng)
_SEED: list[tuple[str, float, float]] = [
    ("Downtown Seattle", 47.6062, -122.3321),
    ("Belltown", 47.6140, -122.3479),
    ("Pioneer Square", 47.6015, -122.3343),
    ("Chinatown-International District", 47.5981, -122.3235),
    ("First Hill", 47.6090, -122.3230),
    ("Capitol Hill", 47.6229, -122.3212),
    ("South Lake Union", 47.6255, -122.3370),
    ("Eastlake", 47.6420, -122.3260),
    ("Lower Queen Anne", 47.6295, -122.3562),
    ("Upper Queen Anne", 47.6375, -122.3570),
    ("Magnolia", 47.6500, -122.3990),
    ("Interbay", 47.6450, -122.3800),
    ("Ballard", 47.6685, -122.3835),
    ("Fremont", 47.6510, -122.3500),
    ("Wallingford", 47.6615, -122.3340),
    ("Phinney Ridge", 47.6680, -122.3550),
    ("Greenwood", 47.6905, -122.3550),
    ("Green Lake", 47.6800, -122.3400),
    ("University District", 47.6612, -122.3134),
    ("Ravenna", 47.6740, -122.3010),
    ("Roosevelt", 47.6760, -122.3170),
    ("Wedgwood", 47.6800, -122.2900),
    ("Northgate", 47.7070, -122.3260),
    ("Lake City", 47.7200, -122.2960),
    ("Shoreline", 47.7560, -122.3410),
    ("Madison Park", 47.6360, -122.2790),
    ("Madrona", 47.6100, -122.2900),
    ("Central District", 47.6060, -122.2990),
    ("Leschi", 47.6000, -122.2870),
    ("Judkins Park", 47.5930, -122.2960),
    ("Beacon Hill", 47.5790, -122.3115),
    ("Mount Baker", 47.5800, -122.2900),
    ("Columbia City", 47.5595, -122.2855),
    ("Hillman City", 47.5490, -122.2870),
    ("Othello", 47.5380, -122.2810),
    ("Rainier Beach", 47.5230, -122.2680),
    ("SoDo", 47.5850, -122.3340),
    ("Georgetown", 47.5470, -122.3200),
    ("West Seattle Junction", 47.5610, -122.3870),
    ("Alki", 47.5810, -122.4090),
    ("Delridge", 47.5510, -122.3620),
    ("White Center", 47.5170, -122.3550),
    ("Burien", 47.4700, -122.3370),
    ("Tukwila", 47.4640, -122.2600),
    ("Renton Downtown", 47.4830, -122.2170),
    ("Mercer Island", 47.5700, -122.2220),
    ("Bellevue Downtown", 47.6150, -122.2000),
    ("Kirkland Downtown", 47.6770, -122.2060),
    ("Redmond Downtown", 47.6740, -122.1215),
    ("Bothell", 47.7600, -122.2050),
]

# Loose bounding box for "we have good seed data here".
BOUNDS = {"min_lat": 47.40, "max_lat": 47.82, "min_lng": -122.46, "max_lng": -122.05}

NEIGHBOURHOODS: list[Neighbourhood] = [
    Neighbourhood(name=name, coords=LatLng(lat=lat, lng=lng)) for name, lat, lng in _SEED
]


def in_seeded_area(point: LatLng) -> bool:
    return (
        BOUNDS["min_lat"] <= point.lat <= BOUNDS["max_lat"]
        and BOUNDS["min_lng"] <= point.lng <= BOUNDS["max_lng"]
    )
