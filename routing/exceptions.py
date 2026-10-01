"""Domain errors, mapped to HTTP status codes through DRF's APIException."""

from rest_framework import status
from rest_framework.exceptions import APIException


class LocationNotFoundError(APIException):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_detail = "The location could not be found in the USA."
    default_code = "location_not_found"


class GeocodingUnavailableError(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "The geocoding service is currently unavailable. Try 'City, ST' input."
    default_code = "geocoding_unavailable"


class NoRouteFoundError(APIException):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_detail = "No drivable route exists between the given locations."
    default_code = "no_route_found"


class RoutingProviderError(APIException):
    """The routing provider answered, but with an error or an unusable payload."""

    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "The routing service returned an invalid response."
    default_code = "routing_provider_error"


class RoutingUnavailableError(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "The routing service is currently unavailable."
    default_code = "routing_unavailable"


class RoutingTimeoutError(APIException):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    default_detail = "The routing service did not respond in time."
    default_code = "routing_timeout"


class NoViableFuelPlanError(APIException):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_detail = "The trip cannot be completed within the vehicle range using known fuel stations."
    default_code = "no_viable_fuel_plan"
