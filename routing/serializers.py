from rest_framework import serializers


def _money(**kwargs) -> serializers.DecimalField:
    return serializers.DecimalField(max_digits=12, decimal_places=2, coerce_to_string=False, **kwargs)


def _gallons(**kwargs) -> serializers.DecimalField:
    return serializers.DecimalField(max_digits=10, decimal_places=2, coerce_to_string=False, **kwargs)


class RoutePlanRequestSerializer(serializers.Serializer):
    start_location = serializers.CharField(
        max_length=200, help_text='US location, e.g. "New York, NY" or "40.7128,-74.0060".'
    )
    finish_location = serializers.CharField(max_length=200, help_text='US location, e.g. "Los Angeles, CA".')

    def validate(self, attrs):
        start = " ".join(attrs["start_location"].split()).lower()
        finish = " ".join(attrs["finish_location"].split()).lower()
        if start == finish:
            raise serializers.ValidationError(
                {"finish_location": "Finish location must be different from the start location."}
            )
        return attrs


class ResolvedLocationSerializer(serializers.Serializer):
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    resolved_as = serializers.CharField(source="label")
    source = serializers.CharField()


class FuelStopSerializer(serializers.Serializer):
    station_id = serializers.IntegerField(source="station.ref.opis_id")
    station_name = serializers.CharField(source="station.ref.name")
    address = serializers.CharField(source="station.ref.address")
    city = serializers.CharField(source="station.ref.city")
    state = serializers.CharField(source="station.ref.state")
    latitude = serializers.FloatField(source="station.ref.latitude")
    longitude = serializers.FloatField(source="station.ref.longitude")
    price_per_gallon = serializers.DecimalField(
        max_digits=7, decimal_places=4, coerce_to_string=False, source="station.price_per_gallon"
    )
    distance_from_start_miles = serializers.FloatField(source="station.mile")
    off_route_miles = serializers.FloatField(source="station.ref.off_route_miles")
    fuel_in_tank_on_arrival_gallons = _gallons(source="fuel_on_arrival_gallons")
    gallons_purchased = _gallons(source="gallons")
    estimated_cost = _money(source="cost")


class FuelSummarySerializer(serializers.Serializer):
    number_of_stops = serializers.SerializerMethodField()
    starting_fuel_gallons = _gallons()
    gallons_purchased = _gallons()
    gallons_consumed = _gallons()
    fuel_remaining_gallons = _gallons()
    cost_basis = serializers.SerializerMethodField()

    def get_number_of_stops(self, plan) -> int:
        return len(plan.purchases)

    def get_cost_basis(self, plan) -> str:
        return (
            "total_fuel_cost is the money spent on fuel bought at the stops during the trip. "
            "The truck departs with a full tank; that starting fuel is not purchased on the trip."
        )


class VehicleSerializer(serializers.Serializer):
    max_range_miles = serializers.FloatField()
    miles_per_gallon = serializers.FloatField()
    tank_capacity_gallons = serializers.FloatField()


class RouteGeometrySerializer(serializers.Serializer):
    provider = serializers.CharField()
    geometry_format = serializers.SerializerMethodField()
    geometry = serializers.CharField()
    point_count = serializers.SerializerMethodField()

    def get_geometry_format(self, route) -> str:
        return "encoded_polyline_precision_5"

    def get_point_count(self, route) -> int:
        return len(route.coordinates)


class RoutePlanResponseSerializer(serializers.Serializer):
    start_location = serializers.CharField(source="start_query")
    finish_location = serializers.CharField(source="finish_query")
    start_coordinates = ResolvedLocationSerializer(source="start")
    finish_coordinates = ResolvedLocationSerializer(source="finish")
    distance_miles = serializers.SerializerMethodField()
    duration_hours = serializers.SerializerMethodField()
    estimated_gallons = _gallons(source="fuel_plan.gallons_consumed")
    total_fuel_cost = _money(source="fuel_plan.total_cost")
    fuel_stops = FuelStopSerializer(source="fuel_plan.purchases", many=True)
    fuel_summary = FuelSummarySerializer(source="fuel_plan")
    vehicle = VehicleSerializer()
    route = RouteGeometrySerializer()
    meta = serializers.SerializerMethodField()

    def get_distance_miles(self, plan) -> float:
        return round(plan.route.distance_miles, 1)

    def get_duration_hours(self, plan) -> float:
        return round(plan.route.duration_seconds / 3600, 2)

    def get_meta(self, plan) -> dict:
        return {
            "routing_api_calls": plan.routing_api_calls,
            "geocoding_api_calls": plan.geocoding_api_calls,
            "route_cache_hit": plan.route.from_cache,
            "candidate_stations_near_route": plan.candidate_station_count,
            "station_search_radius_miles": plan.search_radius_miles,
            "min_savings_per_stop_usd": float(plan.min_savings_per_stop),
            "processing_time_ms": plan.elapsed_ms,
        }
