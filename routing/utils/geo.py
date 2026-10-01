"""Small, dependency-free geometry helpers used by the route planner."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344
MILES_PER_DEGREE_LAT = 69.0

LatLon = tuple[float, float]


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(a)))


def decode_polyline(encoded: str, precision: int = 5) -> list[LatLon]:
    """Decode a Google encoded polyline into ``(lat, lon)`` tuples."""
    factor = 10**precision
    coordinates: list[LatLon] = []
    index = lat = lon = 0
    length = len(encoded)

    while index < length:
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                if index >= length:
                    raise ValueError("Malformed polyline: truncated coordinate.")
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        coordinates.append((lat / factor, lon / factor))
    return coordinates


def encode_polyline(points: Iterable[LatLon], precision: int = 5) -> str:
    """Encode ``(lat, lon)`` tuples as a Google encoded polyline."""
    factor = 10**precision
    output: list[str] = []
    prev_lat = prev_lon = 0
    for lat, lon in points:
        lat_i, lon_i = round(lat * factor), round(lon * factor)
        for delta in (lat_i - prev_lat, lon_i - prev_lon):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                output.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            output.append(chr(value + 63))
        prev_lat, prev_lon = lat_i, lon_i
    return "".join(output)


@dataclass(frozen=True, slots=True)
class RoutePoint:
    lat: float
    lon: float
    mile: float  # distance along the route from the start


def resample_route(
    coordinates: Sequence[LatLon],
    interval_miles: float = 1.0,
    total_distance_miles: float | None = None,
) -> list[RoutePoint]:
    """
    Return points spaced ``interval_miles`` apart along the polyline.

    Provider polylines are irregular: dense in cities, sparse (several miles
    between vertices) on straight interstates. Evenly spaced points make the
    "nearest point on the route" lookup accurate to within half an interval.

    If ``total_distance_miles`` (the provider's road distance) is given, the
    mileage of each point is scaled so the final point matches it exactly.
    """
    if interval_miles <= 0:
        raise ValueError("interval_miles must be positive.")
    if not coordinates:
        return []

    cumulative = [0.0]
    for (lat1, lon1), (lat2, lon2) in pairwise(coordinates):
        cumulative.append(cumulative[-1] + haversine_miles(lat1, lon1, lat2, lon2))
    polyline_length = cumulative[-1]

    first_lat, first_lon = coordinates[0]
    if polyline_length == 0:
        return [RoutePoint(first_lat, first_lon, 0.0)]

    scale = (total_distance_miles / polyline_length) if total_distance_miles else 1.0
    points = [RoutePoint(first_lat, first_lon, 0.0)]
    next_mark = interval_miles
    for i in range(1, len(coordinates)):
        seg_start, seg_end = cumulative[i - 1], cumulative[i]
        seg_length = seg_end - seg_start
        if seg_length == 0:
            continue
        (lat1, lon1), (lat2, lon2) = coordinates[i - 1], coordinates[i]
        while next_mark <= seg_end:
            t = (next_mark - seg_start) / seg_length
            points.append(RoutePoint(lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t, next_mark * scale))
            next_mark += interval_miles

    last_lat, last_lon = coordinates[-1]
    if polyline_length * scale - points[-1].mile > 1e-9:
        points.append(RoutePoint(last_lat, last_lon, polyline_length * scale))
    return points


class RouteCorridor:
    """
    Spatial hash of resampled route points for fast "is this station near the
    route, and at which mile?" queries.

    Grid cells are at least ``radius_miles`` wide in both directions, so every
    route point within the radius of a query point lies in the 3x3 block of
    cells around it. Stations far from the route hit empty cells and cost O(1).
    """

    def __init__(self, points: Sequence[RoutePoint], radius_miles: float):
        if radius_miles <= 0:
            raise ValueError("radius_miles must be positive.")
        if not points:
            raise ValueError("A corridor needs at least one route point.")
        self.points = points
        self.radius_miles = radius_miles

        max_abs_lat = min(89.0, max(abs(p.lat) for p in points) + radius_miles / MILES_PER_DEGREE_LAT)
        self.cell_lat = radius_miles / MILES_PER_DEGREE_LAT
        self.cell_lon = radius_miles / (MILES_PER_DEGREE_LAT * math.cos(math.radians(max_abs_lat)))

        self._grid: dict[tuple[int, int], list[RoutePoint]] = {}
        for point in points:
            self._grid.setdefault(self._cell(point.lat, point.lon), []).append(point)

    def _cell(self, lat: float, lon: float) -> tuple[int, int]:
        return math.floor(lat / self.cell_lat), math.floor(lon / self.cell_lon)

    def nearest(self, lat: float, lon: float) -> tuple[RoutePoint, float] | None:
        """Nearest route point within the radius and its distance, or ``None``."""
        row, col = self._cell(lat, lon)
        best: RoutePoint | None = None
        best_distance = self.radius_miles
        for d_row in (-1, 0, 1):
            for d_col in (-1, 0, 1):
                for point in self._grid.get((row + d_row, col + d_col), ()):
                    distance = haversine_miles(lat, lon, point.lat, point.lon)
                    if distance > best_distance:
                        continue
                    # On ties prefer the earlier route point (first pass by the station).
                    if best is None or distance < best_distance or point.mile < best.mile:
                        best, best_distance = point, distance
        return (best, best_distance) if best is not None else None

    def bounding_boxes(self, chunk_miles: float = 100.0) -> list[tuple[float, float, float, float]]:
        """
        Bounding boxes ``(min_lat, max_lat, min_lon, max_lon)`` of consecutive
        route chunks, padded by the search radius.

        Querying the database with several small boxes instead of one box
        around the whole route avoids loading stations from the large empty
        areas a diagonal cross-country route's overall bounding box contains.
        """
        pad_lat, pad_lon = self.cell_lat, self.cell_lon
        boxes: list[tuple[float, float, float, float]] = []
        chunk: list[RoutePoint] = []
        chunk_start = self.points[0].mile

        def flush() -> None:
            lats = [p.lat for p in chunk]
            lons = [p.lon for p in chunk]
            boxes.append((min(lats) - pad_lat, max(lats) + pad_lat, min(lons) - pad_lon, max(lons) + pad_lon))

        for point in self.points:
            chunk.append(point)
            if point.mile - chunk_start >= chunk_miles:
                flush()
                chunk = [point]  # overlap by one point so chunks stay connected
                chunk_start = point.mile
        if len(chunk) > 1 or not boxes:
            flush()
        return boxes
