from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="route-map", permanent=False)),
    path("admin/", admin.site.urls),
    path("", include("routing.urls")),
]
