from django.urls import path

from routing.views import HealthView, RouteMapView, RoutePlanView

urlpatterns = [
    path("api/v1/route-plan/", RoutePlanView.as_view(), name="route-plan"),
    path("api/v1/health/", HealthView.as_view(), name="health"),
    path("map/", RouteMapView.as_view(), name="route-map"),
]
