from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from routing.exceptions import (
    GeocodingUnavailableError,
    LocationNotFoundError,
    NoRouteFoundError,
    RoutingProviderError,
    RoutingUnavailableError,
)
from routing.models import Place
from routing.services.geocoding_service import GeoPoint, LocationGeocoder
from routing.services.routing_service import (
    OpenRouteServiceClient,
    OSRMClient,
    get_route,
    get_routing_client,
)
from routing.services.station_service import find_stations_along_route
from routing.tests.utils import create_station, fake_response, route_geometry
from routing.utils.geo import METERS_PER_MILE, decode_polyline, resample_route

START = GeoPoint(30.0, -100.0, "start", "coordinates")
FINISH = GeoPoint(35.0, -100.0, "finish", "coordinates")
ROUTING_HTTP = "routing.services.routing_service.requests.request"
GEOCODING_HTTP = "routing.services.geocoding_service.requests.get"


def ors_payload(distance_miles: float) -> dict:
    return {
        "routes": [
            {
                "summary": {"distance": distance_miles * METERS_PER_MILE, "duration": 3600},
                "geometry": route_geometry(distance_miles),
            }
        ]
    }


class OpenRouteServiceClientTests(TestCase):
    def setUp(self):
        self.client = OpenRouteServiceClient("https://ors.example", "secret", "driving-hgv", timeout=5)

    @mock.patch(ROUTING_HTTP)
    def test_parses_route_and_sends_key(self, http):
        http.return_value = fake_response(200, ors_payload(120))

        route = self.client.get_route(START, FINISH)

        self.assertEqual(route.provider, "openrouteservice")
        self.assertAlmostEqual(route.distance_miles, 120, places=6)
        self.assertEqual(route.coordinates[0], (30.0, -100.0))
        method, url = http.call_args.args
        self.assertEqual((method, url), ("POST", "https://ors.example/v2/directions/driving-hgv"))
        self.assertEqual(http.call_args.kwargs["headers"]["Authorization"], "secret")
        self.assertEqual(http.call_args.kwargs["json"]["coordinates"], [[-100.0, 30.0], [-100.0, 35.0]])

    @mock.patch(ROUTING_HTTP)
    def test_invalid_key(self, http):
        http.return_value = fake_response(403, {"error": "Access to this API has been disallowed"})
        with self.assertRaises(RoutingProviderError):
            self.client.get_route(START, FINISH)

    @mock.patch(ROUTING_HTTP)
    def test_quota_exceeded(self, http):
        http.return_value = fake_response(429, {"error": "Rate limit exceeded"})
        with self.assertRaises(RoutingUnavailableError):
            self.client.get_route(START, FINISH)

    @mock.patch(ROUTING_HTTP)
    def test_unroutable_points(self, http):
        error = {"error": {"code": 2010, "message": "Could not find routable point"}}
        http.return_value = fake_response(404, error)
        with self.assertRaises(NoRouteFoundError):
            self.client.get_route(START, FINISH)

    def test_requires_api_key(self):
        with self.assertRaises(RoutingProviderError):
            OpenRouteServiceClient("https://ors.example", "", "driving-car", timeout=5)


class RoutingClientSelectionTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_default_provider_is_osrm(self):
        self.assertIsInstance(get_routing_client(), OSRMClient)

    @override_settings(
        ROUTING={
            "PROVIDER": "openrouteservice",
            "TIMEOUT_SECONDS": 5,
            "ORS_API_KEY": "k",
            "ORS_BASE_URL": "https://ors.example",
            "ORS_PROFILE": "driving-car",
        }
    )
    def test_openrouteservice_provider(self):
        self.assertIsInstance(get_routing_client(), OpenRouteServiceClient)

    @override_settings(ROUTING={"PROVIDER": "carrier-pigeon", "TIMEOUT_SECONDS": 5})
    def test_unknown_provider(self):
        with self.assertRaises(RoutingProviderError):
            get_routing_client()

    @mock.patch(ROUTING_HTTP)
    def test_route_cache_is_keyed_by_coordinates(self, http):
        http.return_value = fake_response(
            200,
            {
                "code": "Ok",
                "routes": [{"geometry": route_geometry(50), "distance": 50 * METERS_PER_MILE, "duration": 1}],
            },
        )
        client = OSRMClient("https://osrm.example", timeout=5)
        get_route(START, FINISH, client)
        get_route(START, FINISH, client)
        get_route(START, GeoPoint(36.0, -100.0, "other", "coordinates"), client)
        self.assertEqual(http.call_count, 2)


class GeocodingServiceTests(TestCase):
    def setUp(self):
        cache.clear()
        Place.objects.create(
            state="NY",
            name="New York",
            normalized_name="newyork",
            kind=Place.Kind.PLACE,
            latitude=40.6943,
            longitude=-73.9249,
        )
        self.geocoder = LocationGeocoder()

    @mock.patch(GEOCODING_HTTP)
    def test_census_lookup_needs_no_network(self, http):
        point = self.geocoder.geocode("New York, NY")
        self.assertEqual((point.latitude, point.source, point.label), (40.6943, "census", "New York, NY"))
        http.assert_not_called()

    @mock.patch(GEOCODING_HTTP)
    def test_nominatim_fallback_is_cached(self, http):
        result = {"lat": "39.7392", "lon": "-104.9903", "display_name": "Denver"}
        http.return_value = fake_response(200, [result])

        first = self.geocoder.geocode("Denver Union Station")
        second = self.geocoder.geocode("denver union station ")

        self.assertEqual((first.latitude, first.longitude, first.source), (39.7392, -104.9903, "nominatim"))
        self.assertEqual(second.source, "nominatim_cache")
        self.assertEqual(http.call_count, 1)
        self.assertEqual(http.call_args.kwargs["params"]["countrycodes"], "us")
        self.assertIn("User-Agent", http.call_args.kwargs["headers"])

    @mock.patch(GEOCODING_HTTP)
    def test_not_found(self, http):
        http.return_value = fake_response(200, [])
        with self.assertRaises(LocationNotFoundError):
            self.geocoder.geocode("Atlantis")

    @mock.patch(GEOCODING_HTTP)
    def test_service_error(self, http):
        http.return_value = fake_response(502, {"error": "bad gateway"})
        with self.assertRaises(GeocodingUnavailableError):
            self.geocoder.geocode("Atlantis")


class StationServiceTests(TestCase):
    def setUp(self):
        coordinates = decode_polyline(route_geometry(600))
        self.points = resample_route(coordinates, 1.0, total_distance_miles=600)

    def test_matches_stations_near_route_in_route_order(self):
        later = create_station(400, "3.10")
        earlier = create_station(120, "3.30", off_route_degrees=0.1)  # ~6 miles off route
        create_station(250, "2.50", off_route_degrees=0.5)  # ~30 miles off route
        create_station(900, "2.50")  # beyond the finish
        create_station(300, "2.50", latitude=None, longitude=None)  # not geocoded

        candidates = find_stations_along_route(self.points, radius_miles=10)

        self.assertEqual([c.opis_id for c in candidates], [earlier.opis_id, later.opis_id])
        self.assertAlmostEqual(candidates[0].route_mile, 120, delta=0.6)
        self.assertAlmostEqual(candidates[0].off_route_miles, 6.0, delta=0.2)
        self.assertAlmostEqual(candidates[1].route_mile, 400, delta=0.6)
        self.assertEqual(candidates[1].off_route_miles, 0.0)

    def test_empty_route(self):
        self.assertEqual(find_stations_along_route([], radius_miles=10), [])

    def test_station_query_is_bounded(self):
        create_station(100, "3.00")
        # One indexed SELECT regardless of how many stations exist.
        with self.assertNumQueries(1):
            find_stations_along_route(self.points, radius_miles=10)
