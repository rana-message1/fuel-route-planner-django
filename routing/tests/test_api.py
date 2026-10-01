from unittest import mock

import requests
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from routing.models import Place
from routing.tests.utils import (
    create_station,
    fake_response,
    finish_query,
    osrm_payload,
    start_query,
)

ROUTING_HTTP = "routing.services.routing_service.requests.request"
GEOCODING_HTTP = "routing.services.geocoding_service.requests.get"


class RoutePlanApiTestBase(TestCase):
    url = reverse("route-plan")

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def post_trip(self, distance_miles: float):
        return self.client.post(
            self.url,
            {"start_location": start_query(), "finish_location": finish_query(distance_miles)},
            format="json",
        )


class RequestValidationTests(RoutePlanApiTestBase):
    def test_missing_fields(self):
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("start_location", response.data)
        self.assertIn("finish_location", response.data)

    def test_blank_location(self):
        response = self.client.post(
            self.url, {"start_location": "  ", "finish_location": "Dallas, TX"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("start_location", response.data)

    def test_identical_locations(self):
        response = self.client.post(
            self.url, {"start_location": "Dallas, TX", "finish_location": " dallas,  tx"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("finish_location", response.data)

    def test_overlong_location(self):
        response = self.client.post(
            self.url, {"start_location": "x" * 201, "finish_location": "Dallas, TX"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)


@mock.patch(ROUTING_HTTP)
class RoutePlanSuccessTests(RoutePlanApiTestBase):
    def test_short_route_has_no_fuel_stops(self, routing_http):
        routing_http.return_value = fake_response(200, osrm_payload(300))
        create_station(150, "3.00")

        response = self.post_trip(300)

        self.assertEqual(response.status_code, 200, response.data)
        data = response.json()
        self.assertEqual(data["fuel_stops"], [])
        self.assertEqual(data["total_fuel_cost"], 0.0)
        self.assertEqual(data["distance_miles"], 300.0)
        self.assertEqual(data["estimated_gallons"], 30.0)
        self.assertEqual(data["fuel_summary"]["fuel_remaining_gallons"], 20.0)

    def test_long_route_response_contract(self, routing_http):
        routing_http.return_value = fake_response(200, osrm_payload(1100))
        create_station(250, "3.40", name="EXPENSIVE STOP")
        cheap = create_station(450, "3.00", name="CHEAP STOP", city="Cheapville")
        create_station(800, "3.20", name="MIDDLE STOP")

        response = self.post_trip(1100)

        self.assertEqual(response.status_code, 200, response.data)
        data = response.json()
        self.assertEqual(data["start_location"], start_query())
        self.assertEqual(data["distance_miles"], 1100.0)
        self.assertEqual(data["route"]["provider"], "osrm")
        self.assertTrue(data["route"]["geometry"])
        self.assertEqual(data["meta"]["routing_api_calls"], 1)
        self.assertEqual(data["meta"]["geocoding_api_calls"], 0)
        self.assertEqual(data["vehicle"]["tank_capacity_gallons"], 50.0)

        stops = data["fuel_stops"]
        self.assertEqual([s["station_name"] for s in stops], ["CHEAP STOP", "MIDDLE STOP"])
        first = stops[0]
        self.assertEqual(first["station_id"], cheap.opis_id)
        self.assertEqual(first["city"], "Cheapville")
        self.assertEqual(first["price_per_gallon"], 3.0)
        self.assertAlmostEqual(first["distance_from_start_miles"], 450, delta=1)
        # Arrives with ~5 gal; cheapest fuel within reach, so it fills the tank.
        self.assertAlmostEqual(first["fuel_in_tank_on_arrival_gallons"], 5.0, delta=0.1)
        self.assertAlmostEqual(first["gallons_purchased"], 45.0, delta=0.1)
        self.assertAlmostEqual(first["estimated_cost"], 135.0, delta=0.3)
        # Mile 800 ($3.20): buy only what is needed for the last 300 miles.
        self.assertAlmostEqual(stops[1]["gallons_purchased"], 15.0, delta=0.1)
        total = round(sum(s["estimated_cost"] for s in stops), 2)
        self.assertEqual(data["total_fuel_cost"], total)
        self.assertAlmostEqual(
            data["fuel_summary"]["gallons_purchased"] + 50, data["estimated_gallons"], delta=0.02
        )

    def test_routing_api_called_once_and_route_cached(self, routing_http):
        routing_http.return_value = fake_response(200, osrm_payload(300))

        first = self.post_trip(300)
        second = self.post_trip(300)

        self.assertEqual(routing_http.call_count, 1)
        self.assertEqual(first.json()["meta"]["route_cache_hit"], False)
        self.assertEqual(second.json()["meta"]["route_cache_hit"], True)
        self.assertEqual(second.json()["meta"]["routing_api_calls"], 0)

    def test_osrm_request_parameters(self, routing_http):
        routing_http.return_value = fake_response(200, osrm_payload(300))
        self.post_trip(300)

        method, url = routing_http.call_args.args
        self.assertEqual(method, "GET")
        self.assertIn("/route/v1/driving/-100.000000,30.000000;", url)
        self.assertEqual(routing_http.call_args.kwargs["params"]["overview"], "full")
        self.assertIn("timeout", routing_http.call_args.kwargs)

    def test_no_viable_fuel_station(self, routing_http):
        routing_http.return_value = fake_response(200, osrm_payload(1200))
        create_station(300, "3.00")  # nothing between mile 300 and 1200

        response = self.post_trip(1200)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["detail"].code, "no_viable_fuel_plan")
        self.assertIn("mile 300", str(response.data["detail"]))

    def test_far_off_route_station_is_not_used(self, routing_http):
        routing_http.return_value = fake_response(200, osrm_payload(700))
        create_station(400, "1.00", off_route_degrees=1.0)  # ~60 miles away

        response = self.post_trip(700)

        self.assertEqual(response.status_code, 422)


@mock.patch(ROUTING_HTTP)
class RoutingFailureTests(RoutePlanApiTestBase):
    def test_timeout_returns_504(self, routing_http):
        routing_http.side_effect = requests.Timeout("read timed out")
        response = self.post_trip(300)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.data["detail"].code, "routing_timeout")

    def test_connection_error_returns_503(self, routing_http):
        routing_http.side_effect = requests.ConnectionError("refused")
        self.assertEqual(self.post_trip(300).status_code, 503)

    def test_provider_server_error_returns_503(self, routing_http):
        routing_http.return_value = fake_response(500, {"message": "boom"})
        self.assertEqual(self.post_trip(300).status_code, 503)

    def test_malformed_json_returns_502(self, routing_http):
        routing_http.return_value = fake_response(200, raw=b"<html>not json</html>")
        self.assertEqual(self.post_trip(300).status_code, 502)

    def test_missing_geometry_returns_502(self, routing_http):
        routing_http.return_value = fake_response(200, {"code": "Ok", "routes": [{"distance": 10}]})
        self.assertEqual(self.post_trip(300).status_code, 502)

    def test_no_route_returns_422(self, routing_http):
        routing_http.return_value = fake_response(400, {"code": "NoRoute", "message": "Impossible route"})
        response = self.post_trip(300)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["detail"].code, "no_route_found")

    def test_failed_request_is_not_cached(self, routing_http):
        routing_http.side_effect = [requests.Timeout(), fake_response(200, osrm_payload(300))]
        self.assertEqual(self.post_trip(300).status_code, 504)
        self.assertEqual(self.post_trip(300).status_code, 200)


class GeocodingApiTests(RoutePlanApiTestBase):
    def setUp(self):
        super().setUp()
        Place.objects.create(
            state="TX",
            name="Start Town",
            normalized_name="starttown",
            kind=Place.Kind.PLACE,
            latitude=30.0,
            longitude=-100.0,
        )

    @mock.patch(GEOCODING_HTTP)
    @mock.patch(ROUTING_HTTP)
    def test_city_state_resolved_locally_without_geocoding_call(self, routing_http, geocoding_http):
        routing_http.return_value = fake_response(200, osrm_payload(300))
        response = self.client.post(
            self.url,
            {"start_location": "Start Town, Texas", "finish_location": finish_query(300)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["start_coordinates"]["source"], "census")
        geocoding_http.assert_not_called()

    @mock.patch(GEOCODING_HTTP)
    def test_unknown_location_returns_422(self, geocoding_http):
        geocoding_http.return_value = fake_response(200, [])
        response = self.client.post(
            self.url,
            {"start_location": "Middle of nowhere", "finish_location": "Start Town, TX"},
            format="json",
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["detail"].code, "location_not_found")
        geocoding_http.assert_called_once()

    @mock.patch(GEOCODING_HTTP)
    def test_non_us_region_rejected_without_geocoding_call(self, geocoding_http):
        response = self.client.post(
            self.url, {"start_location": "Toronto, ON", "finish_location": "Start Town, TX"}, format="json"
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("not a US state", str(response.data["detail"]))
        geocoding_http.assert_not_called()

    @mock.patch(GEOCODING_HTTP)
    def test_geocoder_timeout_returns_503(self, geocoding_http):
        geocoding_http.side_effect = requests.Timeout()
        response = self.client.post(
            self.url,
            {"start_location": "Somewhere unusual", "finish_location": "Start Town, TX"},
            format="json",
        )
        self.assertEqual(response.status_code, 503)


class HealthAndMapTests(TestCase):
    def test_health_reports_missing_data(self):
        response = APIClient().get(reverse("health"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "data_not_loaded")

    def test_health_ok_when_data_loaded(self):
        create_station(10, "3.00")
        Place.objects.create(state="TX", name="A", normalized_name="a", kind="place", latitude=1, longitude=1)
        response = APIClient().get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["fuel_stations"], 1)

    def test_map_page_renders(self):
        response = self.client.get(reverse("route-map"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("route-plan"))
