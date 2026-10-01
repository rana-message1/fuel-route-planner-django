from django.views.generic import TemplateView
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from routing.models import FuelStation, Place
from routing.serializers import RoutePlanRequestSerializer, RoutePlanResponseSerializer
from routing.services.trip_planner import plan_trip


class RoutePlanView(APIView):
    """
    Plan a truck trip between two US locations.

    POST {"start_location": "New York, NY", "finish_location": "Los Angeles, CA"}

    Returns the route geometry, the cost-optimal fuel stops (where and how many
    gallons to buy) and the total fuel cost. Errors use standard codes:
    400 invalid input, 422 unknown location / no route / no viable fuel plan,
    502/503/504 routing provider failures.
    """

    def post(self, request):
        request_serializer = RoutePlanRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        plan = plan_trip(
            request_serializer.validated_data["start_location"],
            request_serializer.validated_data["finish_location"],
        )
        return Response(RoutePlanResponseSerializer(plan).data, status=status.HTTP_200_OK)


class HealthView(APIView):
    """Readiness check: reports whether the reference data has been imported."""

    throttle_classes = []

    def get(self, request):
        stations = FuelStation.objects.count()
        geocoded = FuelStation.objects.filter(latitude__isnull=False).count()
        places = Place.objects.count()
        ready = stations > 0 and places > 0
        return Response(
            {
                "status": "ok" if ready else "data_not_loaded",
                "fuel_stations": stations,
                "geocoded_fuel_stations": geocoded,
                "places": places,
            },
            status=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class RouteMapView(TemplateView):
    """Small Leaflet page that calls the API and draws the route and fuel stops."""

    template_name = "routing/map.html"
