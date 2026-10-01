"""
Django settings for the fuel route planner.

All deployment-specific values come from environment variables (optionally
loaded from a local ``.env`` file). See ``.env.example`` for the full list.
"""

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a number, got {value!r}") from exc


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG", default=False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false.")
    SECRET_KEY = "insecure-development-key-do-not-use-in-production"

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "routing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Full route geometries are large (~125 KB coast to coast); gzip trims about a third.
    "django.middleware.gzip.GZipMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# Enable when served over HTTPS (behind a TLS-terminating proxy).
if env_bool("DJANGO_SECURE_HTTPS", default=False):
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(env_float("DJANGO_HSTS_SECONDS", 3600))

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuel-route-planner",
        # Cached routes hold a decoded polyline; keep the entry count bounded.
        "OPTIONS": {"MAX_ENTRIES": 500},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    # Public, read-only style API: no sessions, so no CSRF requirement for POSTs.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    # Throttling protects the free upstream routing service from abuse.
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": os.environ.get("API_THROTTLE_RATE", "60/minute")},
    "UNAUTHENTICATED_USER": None,
}

# --------------------------------------------------------------------------
# Fuel route planner configuration
# --------------------------------------------------------------------------
FUEL_PLANNER = {
    # Vehicle model from the assignment: 500 mile range at 10 MPG => 50 gallon tank.
    "MAX_RANGE_MILES": env_float("VEHICLE_MAX_RANGE_MILES", 500.0),
    "MILES_PER_GALLON": env_float("VEHICLE_MPG", 10.0),
    # Stations whose (city-level) coordinates are within this distance of the
    # route polyline are treated as reachable from the route.
    "STATION_SEARCH_RADIUS_MILES": env_float("STATION_SEARCH_RADIUS_MILES", 10.0),
    # A fuel stop is only kept if it saves more than this many dollars compared
    # with the best plan without it. 0 = pure minimum cost (may include stops
    # that buy a gallon to save a few cents).
    "MIN_SAVINGS_PER_STOP_USD": os.environ.get("MIN_SAVINGS_PER_STOP_USD", "1.00"),
    # Spacing used when resampling the route polyline for station matching.
    "ROUTE_SAMPLE_INTERVAL_MILES": env_float("ROUTE_SAMPLE_INTERVAL_MILES", 1.0),
    "ROUTE_CACHE_SECONDS": int(env_float("ROUTE_CACHE_SECONDS", 86400)),
    "FUEL_PRICES_FILE": BASE_DIR / "data" / "fuel-prices-for-be-assessment.xlsx",
    "STATION_GEOCODE_OVERRIDES_FILE": BASE_DIR / "data" / "station_geocode_overrides.csv",
    "CENSUS_PLACES_FILE": BASE_DIR / "data" / "census" / "2024_Gaz_place_national.zip",
    "CENSUS_COUSUBS_FILE": BASE_DIR / "data" / "census" / "2024_Gaz_cousubs_national.zip",
}

ROUTING = {
    # "osrm" (no API key, default) or "openrouteservice" (free API key).
    "PROVIDER": os.environ.get("ROUTING_PROVIDER", "osrm").strip().lower(),
    "TIMEOUT_SECONDS": env_float("ROUTING_TIMEOUT_SECONDS", 20.0),
    "OSRM_BASE_URL": os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/"),
    "ORS_BASE_URL": os.environ.get("ORS_BASE_URL", "https://api.openrouteservice.org").rstrip("/"),
    "ORS_API_KEY": os.environ.get("ORS_API_KEY", ""),
    "ORS_PROFILE": os.environ.get("ORS_PROFILE", "driving-car"),
}

GEOCODING = {
    # Only used when a location cannot be resolved from the local Census table.
    "NOMINATIM_BASE_URL": os.environ.get("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org").rstrip(
        "/"
    ),
    # Nominatim's usage policy requires an identifying User-Agent.
    "USER_AGENT": os.environ.get("GEOCODER_USER_AGENT", "fuel-route-planner/1.0 (Django coding assessment)"),
    "TIMEOUT_SECONDS": env_float("GEOCODING_TIMEOUT_SECONDS", 10.0),
    "CACHE_SECONDS": int(env_float("GEOCODING_CACHE_SECONDS", 7 * 86400)),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "routing": {
            "handlers": ["console"],
            "level": os.environ.get("APP_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}

# Error responses are expected in the test suite; keep its output readable.
if len(sys.argv) > 1 and sys.argv[1] == "test":
    LOGGING["root"]["level"] = "CRITICAL"
    LOGGING["loggers"]["routing"]["level"] = "CRITICAL"
    LOGGING["loggers"]["django.request"] = {"level": "CRITICAL"}
