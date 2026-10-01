from django.contrib import admin

from routing.models import FuelStation, Place


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ("opis_id", "name", "city", "state", "retail_price", "geocode_source")
    list_filter = ("state", "geocode_source")
    search_fields = ("name", "city", "opis_id")
    ordering = ("state", "city")


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = ("name", "state", "kind", "latitude", "longitude")
    list_filter = ("kind", "state")
    search_fields = ("name",)
