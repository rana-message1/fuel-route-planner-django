from django.urls import path

from routing.views import HealthView, RouteMapView, RoutePlanView

urlpatterns = [
    path("api/v1/route-plan/", RoutePlanView.as_view(), name="route-plan"),
    path("api/v1/health/", HealthView.as_view(), name="health"),
    # Also accept the API URLs without a trailing slash. APPEND_SLASH cannot
    # redirect a POST (the body would be lost), so clients that drop the slash
    # would otherwise get a 500.
    path("api/v1/route-plan", RoutePlanView.as_view()),
    path("api/v1/health", HealthView.as_view()),
    path("map/", RouteMapView.as_view(), name="route-map"),
]
