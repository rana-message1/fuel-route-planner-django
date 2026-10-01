"""Find fuel stations near a route and project them onto it."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q

from routing.models import FuelStation
from routing.utils.geo import RouteCorridor, RoutePoint

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CandidateStation:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price_per_gallon: Decimal
    latitude: float
    longitude: float
    route_mile: float  # position of the nearest route point
    off_route_miles: float  # straight-line distance from that point


_FIELDS = ("opis_id", "name", "address", "city", "state", "retail_price", "latitude", "longitude")


def find_stations_along_route(
    route_points: Sequence[RoutePoint], radius_miles: float
) -> list[CandidateStation]:
    """
    Return stations within ``radius_miles`` of the route, ordered by route mile.

    1. Database: a handful of indexed lat/lon range filters, one per ~100 mile
       chunk of the route, so only stations near the route are loaded.
    2. Python: a spatial hash over 1-mile route samples gives each station's
       distance from the route and the mile at which the route passes it.
    """
    if not route_points:
        return []
    corridor = RouteCorridor(route_points, radius_miles)

    box_filter = Q()
    for min_lat, max_lat, min_lon, max_lon in corridor.bounding_boxes():
        box_filter |= Q(
            latitude__gte=min_lat, latitude__lte=max_lat, longitude__gte=min_lon, longitude__lte=max_lon
        )

    rows = FuelStation.objects.filter(box_filter).values_list(*_FIELDS)
    candidates: list[CandidateStation] = []
    loaded = 0
    for opis_id, name, address, city, state, price, lat, lon in rows.iterator(chunk_size=2000):
        loaded += 1
        match = corridor.nearest(lat, lon)
        if match is None:
            continue
        point, distance = match
        candidates.append(
            CandidateStation(
                opis_id=opis_id,
                name=name,
                address=address,
                city=city,
                state=state,
                price_per_gallon=price,
                latitude=lat,
                longitude=lon,
                route_mile=round(point.mile, 1),
                off_route_miles=round(distance, 1),
            )
        )

    candidates.sort(key=lambda c: (c.route_mile, c.price_per_gallon))
    logger.info(
        "Station lookup: %d loaded from bounding boxes, %d within %.1f miles of the route.",
        loaded,
        len(candidates),
        radius_miles,
    )
    return candidates
