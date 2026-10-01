from django.db import models


class Place(models.Model):
    """
    A US city/town/CDP/township with a representative coordinate.

    Loaded once from the public-domain US Census Gazetteer files by
    ``manage.py import_places``. Used to geocode fuel stations (the dataset has
    no coordinates) and to resolve "City, ST" API input without calling an
    external geocoder.
    """

    class Kind(models.TextChoices):
        PLACE = "place", "Census place"
        COUNTY_SUBDIVISION = "county_subdivision", "Census county subdivision"

    state = models.CharField(max_length=2)
    name = models.CharField(max_length=120)
    normalized_name = models.CharField(max_length=120)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    latitude = models.FloatField()
    longitude = models.FloatField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["state", "normalized_name"], name="unique_place_per_state"),
        ]

    def __str__(self) -> str:
        return f"{self.name}, {self.state}"


class FuelStation(models.Model):
    """
    One truck stop from the supplied OPIS fuel-price file.

    The file lists some stations several times (same OPIS ID, different
    prices); the importer collapses them into one row keeping the lowest price.
    """

    class GeocodeSource(models.TextChoices):
        CENSUS_PLACE = "census_place", "Census place centroid"
        CENSUS_COUNTY_SUBDIVISION = "census_county_subdivision", "Census county subdivision centroid"
        OVERRIDE = "override", "Station geocode override file"
        NONE = "none", "Not geocoded"

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=80)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    retail_price = models.DecimalField(max_digits=7, decimal_places=4)
    price_observations = models.PositiveSmallIntegerField(default=1)
    # City-level coordinates: the dataset only has highway-exit style addresses.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    geocode_source = models.CharField(
        max_length=30, choices=GeocodeSource.choices, default=GeocodeSource.NONE
    )
    imported_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            # Serves the bounding-box pre-filter around the route.
            models.Index(fields=["latitude", "longitude"], name="station_lat_lon_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price}"
