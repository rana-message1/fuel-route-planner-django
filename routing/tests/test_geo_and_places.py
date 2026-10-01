from django.test import SimpleTestCase

from routing.tests.utils import ROUTE_LON, lat_at_mile
from routing.utils.geo import (
    RouteCorridor,
    decode_polyline,
    encode_polyline,
    haversine_miles,
    resample_route,
)
from routing.utils.places import (
    clean_gazetteer_name,
    normalize_place_name,
    parse_location_query,
    state_code,
)


class GeoTests(SimpleTestCase):
    def test_haversine_new_york_to_los_angeles(self):
        distance = haversine_miles(40.7128, -74.0060, 34.0522, -118.2437)
        self.assertAlmostEqual(distance, 2445.6, delta=1.0)

    def test_decode_reference_polyline(self):
        # Example from Google's encoded polyline documentation.
        points = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
        self.assertEqual(points, [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)])

    def test_encode_decode_round_trip(self):
        points = [(40.71278, -74.00597), (39.95258, -75.16522), (34.05223, -118.24368)]
        self.assertEqual(decode_polyline(encode_polyline(points)), points)

    def test_decode_rejects_truncated_polyline(self):
        with self.assertRaises(ValueError):
            decode_polyline("_p~iF~ps|U_")

    def test_resample_route_spacing_and_distance_scaling(self):
        coordinates = [(lat_at_mile(0), ROUTE_LON), (lat_at_mile(10), ROUTE_LON)]
        points = resample_route(coordinates, interval_miles=1.0, total_distance_miles=11.0)

        self.assertEqual(len(points), 11)
        self.assertEqual(points[0].mile, 0.0)
        self.assertAlmostEqual(points[-1].mile, 11.0, places=6)  # scaled to road distance
        self.assertAlmostEqual(points[1].mile, 1.1, places=6)

    def test_corridor_finds_nearest_point_within_radius(self):
        coordinates = [(lat_at_mile(0), ROUTE_LON), (lat_at_mile(100), ROUTE_LON)]
        corridor = RouteCorridor(resample_route(coordinates, 1.0), radius_miles=10)

        point, distance = corridor.nearest(lat_at_mile(42), ROUTE_LON + 0.05)  # ~3 miles east
        self.assertAlmostEqual(point.mile, 42, delta=0.6)
        self.assertAlmostEqual(distance, 2.99, delta=0.1)
        self.assertIsNone(corridor.nearest(lat_at_mile(42), ROUTE_LON + 1.0))  # ~60 miles away
        self.assertIsNone(corridor.nearest(lat_at_mile(130), ROUTE_LON))  # past the end

    def test_corridor_bounding_boxes_cover_route(self):
        coordinates = [(lat_at_mile(0), ROUTE_LON), (lat_at_mile(350), ROUTE_LON)]
        corridor = RouteCorridor(resample_route(coordinates, 1.0), radius_miles=10)
        boxes = corridor.bounding_boxes(chunk_miles=100)

        self.assertEqual(len(boxes), 4)
        for mile in (0, 99, 100, 250, 350):
            lat = lat_at_mile(mile)
            self.assertTrue(any(b[0] <= lat <= b[1] and b[2] <= ROUTE_LON <= b[3] for b in boxes))


class PlaceNameTests(SimpleTestCase):
    def test_normalization_matches_spelling_variants(self):
        self.assertEqual(normalize_place_name("Mc Calla"), normalize_place_name("McCalla"))
        self.assertEqual(normalize_place_name("Saint Johns"), normalize_place_name("St. Johns"))
        self.assertEqual(normalize_place_name("Canon City"), normalize_place_name("Cañon City"))
        self.assertEqual(normalize_place_name("S Coffeyville"), normalize_place_name("South Coffeyville"))
        self.assertEqual(normalize_place_name("Fort Worth "), normalize_place_name("Ft. Worth"))

    def test_clean_gazetteer_name(self):
        self.assertEqual(clean_gazetteer_name("Tomah city"), ("Tomah", []))
        self.assertEqual(clean_gazetteer_name("Laurel CDP"), ("Laurel", []))
        self.assertEqual(clean_gazetteer_name("Salt Lake City city"), ("Salt Lake City", []))
        self.assertEqual(clean_gazetteer_name("Carson City"), ("Carson City", []))
        self.assertEqual(clean_gazetteer_name("Indianapolis city (balance)"), ("Indianapolis", []))
        self.assertEqual(
            clean_gazetteer_name("Nashville-Davidson metropolitan government (balance)"),
            ("Nashville-Davidson", ["Nashville"]),
        )
        self.assertEqual(clean_gazetteer_name("Macon-Bibb County"), ("Macon-Bibb County", ["Macon"]))

    def test_state_code(self):
        self.assertEqual(state_code("tx"), "TX")
        self.assertEqual(state_code("New York"), "NY")
        self.assertIsNone(state_code("ON"))  # Ontario is not a US state

    def test_parse_city_state_variants(self):
        for query in (
            "New York, NY",
            "New York, New York",
            " New York ,ny ",
            "New York, NY 10001",
            "New York, NY, USA",
        ):
            parsed = parse_location_query(query)
            self.assertEqual((parsed.city, parsed.state), ("New York", "NY"), query)

    def test_parse_coordinates(self):
        parsed = parse_location_query("40.7128, -74.0060")
        self.assertTrue(parsed.is_coordinates)
        self.assertEqual((parsed.latitude, parsed.longitude), (40.7128, -74.006))
        self.assertFalse(parse_location_query("95.0,-74.0").is_coordinates)

    def test_parse_unstructured_input(self):
        self.assertIsNone(parse_location_query("1600 Pennsylvania Ave NW, Washington, DC").city)
        self.assertEqual(parse_location_query("Toronto, ON").non_us_region, "ON")
