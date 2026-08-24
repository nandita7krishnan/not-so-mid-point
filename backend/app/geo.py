"""Small spherical-geometry helpers. No external deps."""
from __future__ import annotations

import math

from .state import LatLng

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(a: LatLng, b: LatLng) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = p2 - p1
    dl = math.radians(b.lng - a.lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def interpolate(a: LatLng, b: LatLng, t: float) -> LatLng:
    """Linear interpolation. Fine at city scale, where the great circle is
    indistinguishable from a straight line in lat/lng space."""
    return LatLng(lat=a.lat + (b.lat - a.lat) * t, lng=a.lng + (b.lng - a.lng) * t)


def distance_to_segment_m(p: LatLng, a: LatLng, b: LatLng) -> float:
    """Perpendicular distance from `p` to segment `a`-`b`, via a local
    equirectangular projection centred on the segment."""
    lat0 = math.radians((a.lat + b.lat) / 2)
    kx = math.cos(lat0) * math.pi / 180 * EARTH_RADIUS_M
    ky = math.pi / 180 * EARTH_RADIUS_M

    ax, ay = a.lng * kx, a.lat * ky
    bx, by = b.lng * kx, b.lat * ky
    px, py = p.lng * kx, p.lat * ky

    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def offset(point: LatLng, north_m: float, east_m: float) -> LatLng:
    dlat = north_m / EARTH_RADIUS_M * 180 / math.pi
    dlng = east_m / (EARTH_RADIUS_M * math.cos(math.radians(point.lat))) * 180 / math.pi
    return LatLng(lat=point.lat + dlat, lng=point.lng + dlng)


def bounding_rectangle(center: LatLng, radius_m: float) -> tuple[LatLng, LatLng]:
    """South-west and north-east corners of a box enclosing the circle.

    Places' text search accepts a rectangle for `locationRestriction` (a circle
    is only allowed as a soft `locationBias`), so a circular search area has to
    be squared off before it can be enforced."""
    dlat = radius_m / EARTH_RADIUS_M * 180 / math.pi
    dlng = radius_m / (EARTH_RADIUS_M * math.cos(math.radians(center.lat))) * 180 / math.pi
    return (
        LatLng(lat=center.lat - dlat, lng=center.lng - dlng),
        LatLng(lat=center.lat + dlat, lng=center.lng + dlng),
    )
