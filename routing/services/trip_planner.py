"""Orchestrates geocoding -> routing -> station lookup -> fuel optimisation."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from routing.exceptions import NoViableFuelPlanError
from routing.services.fuel_optimizer import (
    FuelPlan,
    InfeasibleRouteError,
    StationOnRoute,
    VehicleProfile,
    plan_fuel_stops,
)
from routing.services.geocoding_service import GeoPoint, LocationGeocoder
from routing.services.routing_service import Route, get_route
from routing.services.station_service import find_stations_along_route
from routing.utils.geo import resample_route

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TripPlan:
    start_query: str
    finish_query: str
    start: GeoPoint
    finish: GeoPoint
    route: Route
    vehicle: VehicleProfile
    fuel_plan: FuelPlan
    candidate_station_count: int
    search_radius_miles: float
    min_savings_per_stop: Decimal
    routing_api_calls: int
    geocoding_api_calls: int
    elapsed_ms: int


def default_vehicle() -> VehicleProfile:
    config = settings.FUEL_PLANNER
    return VehicleProfile(
        max_range_miles=config["MAX_RANGE_MILES"], miles_per_gallon=config["MILES_PER_GALLON"]
    )


def min_savings_per_stop() -> Decimal:
    raw = settings.FUEL_PLANNER["MIN_SAVINGS_PER_STOP_USD"]
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ImproperlyConfigured(f"MIN_SAVINGS_PER_STOP_USD must be a number, got {raw!r}") from exc
    if value < 0:
        raise ImproperlyConfigured("MIN_SAVINGS_PER_STOP_USD must not be negative.")
    return value


def plan_trip(start_query: str, finish_query: str, geocoder: LocationGeocoder | None = None) -> TripPlan:
    started = time.perf_counter()
    config = settings.FUEL_PLANNER
    geocoder = geocoder or LocationGeocoder()
    vehicle = default_vehicle()

    start = geocoder.geocode(start_query)
    finish = geocoder.geocode(finish_query)
    route = get_route(start, finish)

    route_points = resample_route(
        route.coordinates, config["ROUTE_SAMPLE_INTERVAL_MILES"], total_distance_miles=route.distance_miles
    )
    radius = config["STATION_SEARCH_RADIUS_MILES"]
    stop_threshold = min_savings_per_stop()
    candidates = find_stations_along_route(route_points, radius)

    try:
        fuel_plan = plan_fuel_stops(
            [StationOnRoute(c.route_mile, c.price_per_gallon, ref=c) for c in candidates],
            total_distance_miles=route.distance_miles,
            vehicle=vehicle,
            min_savings_per_stop=stop_threshold,
        )
    except InfeasibleRouteError as exc:
        raise NoViableFuelPlanError(str(exc)) from exc

    elapsed_ms = round((time.perf_counter() - started) * 1000)
    logger.info(
        "Planned %s -> %s: %.0f miles, %d stops, $%s (%d ms, route cached=%s).",
        start_query,
        finish_query,
        route.distance_miles,
        len(fuel_plan.purchases),
        fuel_plan.total_cost,
        elapsed_ms,
        route.from_cache,
    )
    return TripPlan(
        start_query=start_query,
        finish_query=finish_query,
        start=start,
        finish=finish,
        route=route,
        vehicle=vehicle,
        fuel_plan=fuel_plan,
        candidate_station_count=len(candidates),
        search_radius_miles=radius,
        min_savings_per_stop=stop_threshold,
        routing_api_calls=0 if route.from_cache else 1,
        geocoding_api_calls=int(start.made_external_call) + int(finish.made_external_call),
        elapsed_ms=elapsed_ms,
    )
