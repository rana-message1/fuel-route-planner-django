"""
Resolve free-text US locations to coordinates.

Resolution order (first hit wins):
1. ``"lat,lon"`` input                    -> no lookup at all
2. ``"City, ST"`` / ``"City, State"``     -> local Census ``Place`` table (no network)
3. anything else                          -> Nominatim (OpenStreetMap), cached

Most requests therefore make zero geocoding calls, leaving the single routing
call as the only external request.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from routing.exceptions import GeocodingUnavailableError, LocationNotFoundError
from routing.models import Place
from routing.utils.places import normalize_place_name, parse_location_query

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float
    label: str
    source: str  # "coordinates" | "census" | "nominatim" | "nominatim_cache"

    @property
    def made_external_call(self) -> bool:
        return self.source == "nominatim"


class NominatimClient:
    """Minimal Nominatim client restricted to the USA."""

    def __init__(self) -> None:
        config = settings.GEOCODING
        self.base_url = config["NOMINATIM_BASE_URL"]
        self.timeout = config["TIMEOUT_SECONDS"]
        self.headers = {"User-Agent": config["USER_AGENT"]}

    def search(self, *, query: str | None = None, city: str | None = None, state: str | None = None):
        """Return ``(lat, lon, display_name)`` or ``None`` when nothing matches."""
        params: dict[str, str | int] = {"format": "jsonv2", "limit": 1, "countrycodes": "us"}
        if query:
            params["q"] = query
        else:
            params.update({"city": city or "", "state": state or "", "country": "USA"})
        try:
            response = requests.get(
                f"{self.base_url}/search", params=params, headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()
            results = response.json()
        except requests.Timeout as exc:
            raise GeocodingUnavailableError("The geocoding service timed out.") from exc
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Nominatim request failed: %s", exc)
            raise GeocodingUnavailableError() from exc

        if not results:
            return None
        best = results[0]
        return float(best["lat"]), float(best["lon"]), best.get("display_name", "")


class LocationGeocoder:
    def __init__(self, client: NominatimClient | None = None) -> None:
        self.client = client or NominatimClient()
        self.cache_seconds = settings.GEOCODING["CACHE_SECONDS"]

    def geocode(self, query: str) -> GeoPoint:
        parsed = parse_location_query(query)
        if parsed.is_coordinates:
            return GeoPoint(parsed.latitude, parsed.longitude, query.strip(), "coordinates")

        if parsed.non_us_region:
            raise LocationNotFoundError(
                f"'{parsed.non_us_region}' is not a US state code; only US locations are supported."
            )

        if parsed.city and parsed.state:
            place = Place.objects.filter(
                state=parsed.state, normalized_name=normalize_place_name(parsed.city)
            ).first()
            if place:
                return GeoPoint(place.latitude, place.longitude, f"{place.name}, {place.state}", "census")

        return self._geocode_remote(query)

    def _geocode_remote(self, query: str) -> GeoPoint:
        key = "geocode:" + hashlib.sha256(query.strip().lower().encode()).hexdigest()
        cached = cache.get(key)
        if cached is not None:
            return GeoPoint(*cached, source="nominatim_cache")

        logger.info("Geocoding %r with Nominatim.", query)
        result = self.client.search(query=query)
        if result is None:
            raise LocationNotFoundError(f"Could not find a US location matching '{query}'.")
        cache.set(key, result, self.cache_seconds)
        return GeoPoint(*result, source="nominatim")
