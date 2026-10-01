"""Shared test helpers: synthetic routes, stations and fake HTTP responses."""

from __future__ import annotations

import json
import math
from decimal import Decimal

import requests

from routing.models import FuelStation
from routing.utils.geo import EARTH_RADIUS_MILES, METERS_PER_MILE, encode_polyline

# The synthetic route runs due north along longitude -100 from latitude 30,
# so "mile m" is simply latitude 30 + m / MILES_PER_DEGREE.
ROUTE_LON = -100.0
ROUTE_START_LAT = 30.0
MILES_PER_DEGREE = 2 * math.pi * EARTH_RADIUS_MILES / 360


def lat_at_mile(mile: float) -> float:
    return ROUTE_START_LAT + mile / MILES_PER_DEGREE


def route_geometry(distance_miles: float, step_miles: float = 5.0) -> str:
    steps = max(1, math.ceil(distance_miles / step_miles))
    points = [(lat_at_mile(distance_miles * i / steps), ROUTE_LON) for i in range(steps + 1)]
    return encode_polyline(points)


def start_query() -> str:
    return f"{ROUTE_START_LAT},{ROUTE_LON}"


def finish_query(distance_miles: float) -> str:
    return f"{lat_at_mile(distance_miles):.6f},{ROUTE_LON}"


def osrm_payload(distance_miles: float) -> dict:
    return {
        "code": "Ok",
        "routes": [
            {
                "geometry": route_geometry(distance_miles),
                "distance": distance_miles * METERS_PER_MILE,
                "duration": distance_miles / 60 * 3600,
            }
        ],
    }


def fake_response(status_code: int = 200, payload=None, raw: bytes | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = raw if raw is not None else json.dumps(payload).encode()
    response.headers["Content-Type"] = "application/json"
    return response


_next_id = 1000


def create_station(mile: float, price: str, off_route_degrees: float = 0.0, **fields) -> FuelStation:
    """Create a station at ``mile`` along the synthetic route."""
    global _next_id
    _next_id += 1
    defaults = {
        "opis_id": _next_id,
        "name": f"TEST STOP {_next_id}",
        "address": "I-00, EXIT 1",
        "city": "Testville",
        "state": "TX",
        "retail_price": Decimal(price),
        "latitude": lat_at_mile(mile),
        "longitude": ROUTE_LON + off_route_degrees,
        "geocode_source": FuelStation.GeocodeSource.CENSUS_PLACE,
    }
    defaults.update(fields)
    return FuelStation.objects.create(**defaults)
