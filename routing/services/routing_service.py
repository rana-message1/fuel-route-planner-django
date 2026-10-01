"""
Driving-route lookup.

Exactly one HTTP request is made per (uncached) route: the provider returns
distance, duration and the full route geometry in a single response. Stations
are matched against that geometry locally, so the provider is never asked
about individual fuel stations.

Providers
---------
* ``osrm`` (default) - public OSRM server, no API key needed.
* ``openrouteservice`` - free tier with an API key (``ORS_API_KEY``).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace

import requests
from django.conf import settings
from django.core.cache import cache

from routing.exceptions import (
    NoRouteFoundError,
    RoutingProviderError,
    RoutingTimeoutError,
    RoutingUnavailableError,
)
from routing.services.geocoding_service import GeoPoint
from routing.utils.geo import METERS_PER_MILE, LatLon, decode_polyline

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Route:
    provider: str
    distance_miles: float
    duration_seconds: float
    geometry: str  # Google encoded polyline, precision 5
    coordinates: list[LatLon]
    from_cache: bool = False


class RoutingClient(ABC):
    name: str

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    @abstractmethod
    def get_route(self, start: GeoPoint, finish: GeoPoint) -> Route:
        """Return the driving route from ``start`` to ``finish``."""

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            return requests.request(method, url, timeout=self.timeout, **kwargs)
        except requests.Timeout as exc:
            logger.warning("%s routing request timed out after %ss.", self.name, self.timeout)
            raise RoutingTimeoutError() from exc
        except requests.RequestException as exc:
            logger.warning("%s routing request failed: %s", self.name, exc)
            raise RoutingUnavailableError() from exc

    @staticmethod
    def _json(response: requests.Response) -> dict:
        try:
            payload = response.json()
        except ValueError as exc:
            raise RoutingProviderError("The routing service returned malformed JSON.") from exc
        if not isinstance(payload, dict):
            raise RoutingProviderError()
        return payload

    def _build_route(self, distance_m, duration_s, geometry) -> Route:
        if not isinstance(geometry, str) or distance_m is None:
            raise RoutingProviderError("The routing service response is missing route data.")
        try:
            coordinates = decode_polyline(geometry)
        except ValueError as exc:
            raise RoutingProviderError("The routing service returned an invalid geometry.") from exc
        if not coordinates:
            raise RoutingProviderError("The routing service returned an empty geometry.")
        return Route(
            provider=self.name,
            distance_miles=float(distance_m) / METERS_PER_MILE,
            duration_seconds=float(duration_s or 0),
            geometry=geometry,
            coordinates=coordinates,
        )


class OSRMClient(RoutingClient):
    name = "osrm"

    def __init__(self, base_url: str, timeout: float) -> None:
        super().__init__(timeout)
        self.base_url = base_url

    def get_route(self, start: GeoPoint, finish: GeoPoint) -> Route:
        url = (
            f"{self.base_url}/route/v1/driving/"
            f"{start.longitude:.6f},{start.latitude:.6f};{finish.longitude:.6f},{finish.latitude:.6f}"
        )
        params = {"overview": "full", "geometries": "polyline", "steps": "false", "alternatives": "false"}
        response = self._request("GET", url, params=params)

        if response.status_code == 429:
            raise RoutingUnavailableError("The routing service rate limit was exceeded; retry shortly.")
        if response.status_code >= 500:
            raise RoutingUnavailableError(f"The routing service failed with HTTP {response.status_code}.")
        payload = self._json(response)

        code = payload.get("code")
        if code in {"NoRoute", "NoSegment"}:
            raise NoRouteFoundError()
        if code != "Ok" or not payload.get("routes"):
            logger.warning("OSRM error response: %s", payload)
            raise RoutingProviderError(f"Routing failed: {payload.get('message') or code}.")

        route = payload["routes"][0]
        return self._build_route(route.get("distance"), route.get("duration"), route.get("geometry"))


class OpenRouteServiceClient(RoutingClient):
    name = "openrouteservice"

    # ORS error codes meaning "these points cannot be connected by road".
    _NO_ROUTE_CODES = {2004, 2009, 2010}

    def __init__(self, base_url: str, api_key: str, profile: str, timeout: float) -> None:
        super().__init__(timeout)
        if not api_key:
            raise RoutingProviderError("ORS_API_KEY is not configured.")
        self.base_url = base_url
        self.api_key = api_key
        self.profile = profile

    def get_route(self, start: GeoPoint, finish: GeoPoint) -> Route:
        body = {
            "coordinates": [[start.longitude, start.latitude], [finish.longitude, finish.latitude]],
            "instructions": False,
            "units": "m",
        }
        response = self._request(
            "POST",
            f"{self.base_url}/v2/directions/{self.profile}",
            json=body,
            headers={"Authorization": self.api_key, "Content-Type": "application/json"},
        )

        if response.status_code in {401, 403}:
            raise RoutingProviderError("OpenRouteService rejected the API key.")
        if response.status_code == 429:
            raise RoutingUnavailableError("OpenRouteService quota exceeded; retry later.")
        if response.status_code >= 500:
            raise RoutingUnavailableError(f"OpenRouteService failed with HTTP {response.status_code}.")
        payload = self._json(response)

        if response.status_code != 200:
            error = payload.get("error") or {}
            if isinstance(error, dict) and error.get("code") in self._NO_ROUTE_CODES:
                raise NoRouteFoundError()
            logger.warning("OpenRouteService error response: %s", payload)
            raise RoutingProviderError("OpenRouteService could not compute the route.")

        routes = payload.get("routes") or []
        if not routes:
            raise NoRouteFoundError()
        summary = routes[0].get("summary") or {}
        # ORS omits "distance" for zero-length routes.
        return self._build_route(
            summary.get("distance", 0.0), summary.get("duration"), routes[0].get("geometry")
        )


def get_routing_client() -> RoutingClient:
    config = settings.ROUTING
    provider = config["PROVIDER"]
    if provider == "osrm":
        return OSRMClient(config["OSRM_BASE_URL"], config["TIMEOUT_SECONDS"])
    if provider == "openrouteservice":
        return OpenRouteServiceClient(
            config["ORS_BASE_URL"], config["ORS_API_KEY"], config["ORS_PROFILE"], config["TIMEOUT_SECONDS"]
        )
    raise RoutingProviderError(f"Unknown ROUTING_PROVIDER '{provider}'.")


def get_route(start: GeoPoint, finish: GeoPoint, client: RoutingClient | None = None) -> Route:
    """Fetch a route, re-using a cached one for the same start/finish coordinates."""
    client = client or get_routing_client()
    key = (
        f"route:{client.name}:{start.latitude:.5f},{start.longitude:.5f}:"
        f"{finish.latitude:.5f},{finish.longitude:.5f}"
    )
    cached = cache.get(key)
    if cached is not None:
        return replace(cached, from_cache=True)

    route = client.get_route(start, finish)
    logger.info(
        "Routing API call (%s): %.1f miles, %d geometry points.",
        client.name,
        route.distance_miles,
        len(route.coordinates),
    )
    cache.set(key, route, settings.FUEL_PLANNER["ROUTE_CACHE_SECONDS"])
    return route
