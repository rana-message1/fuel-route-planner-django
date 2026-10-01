"""
Load US Census Gazetteer places and county subdivisions into ``Place``.

The gazetteer files are public domain and ship with the repository in
``data/census/``; ``--download`` fetches them from census.gov if missing.
"""

from __future__ import annotations

import csv
import io
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.models import Place
from routing.utils.places import clean_gazetteer_name, normalize_place_name

CENSUS_BASE_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer"


@dataclass(frozen=True)
class _Candidate:
    state: str
    name: str
    normalized_name: str
    kind: str
    latitude: float
    longitude: float
    rank: tuple  # lower is better


def read_gazetteer(path: Path) -> list[dict[str, str]]:
    """Read a tab-separated gazetteer file, plain or zipped."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            member = next((n for n in archive.namelist() if n.endswith(".txt")), None)
            if member is None:
                raise CommandError(f"No .txt file inside {path}.")
            raw = archive.read(member)
    else:
        raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = [column.strip() for column in next(reader)]
    required = {"USPS", "NAME", "ALAND", "INTPTLAT", "INTPTLONG"}
    missing = required - set(header)
    if missing:
        raise CommandError(f"{path.name} is missing columns: {sorted(missing)}")
    return [dict(zip(header, (value.strip() for value in row), strict=False)) for row in reader if row]


class Command(BaseCommand):
    help = "Import US Census Gazetteer places and county subdivisions (used for geocoding)."

    def add_arguments(self, parser):
        config = settings.FUEL_PLANNER
        parser.add_argument("--places-file", type=Path, default=config["CENSUS_PLACES_FILE"])
        parser.add_argument("--cousubs-file", type=Path, default=config["CENSUS_COUSUBS_FILE"])
        parser.add_argument(
            "--download", action="store_true", help="Download missing gazetteer files from census.gov."
        )

    def handle(self, *args, **options):
        started = time.perf_counter()
        sources = [
            (Path(options["places_file"]), Place.Kind.PLACE, 0),
            (Path(options["cousubs_file"]), Place.Kind.COUNTY_SUBDIVISION, 1),
        ]
        best: dict[tuple[str, str], _Candidate] = {}
        for path, kind, kind_rank in sources:
            self._ensure_file(path, options["download"])
            rows = read_gazetteer(path)
            for row in rows:
                for candidate in self._candidates(row, kind, kind_rank):
                    key = (candidate.state, candidate.normalized_name)
                    if key not in best or candidate.rank < best[key].rank:
                        best[key] = candidate
            self.stdout.write(f"Read {len(rows):,} rows from {path.name}")

        places = [
            Place(
                state=c.state,
                name=c.name,
                normalized_name=c.normalized_name,
                kind=c.kind,
                latitude=c.latitude,
                longitude=c.longitude,
            )
            for c in best.values()
        ]
        with transaction.atomic():
            Place.objects.all().delete()
            Place.objects.bulk_create(places, batch_size=5000)

        elapsed = time.perf_counter() - started
        self.stdout.write(self.style.SUCCESS(f"Imported {len(places):,} places in {elapsed:.1f}s."))

    @staticmethod
    def _candidates(row: dict[str, str], kind: str, kind_rank: int):
        try:
            latitude = float(row["INTPTLAT"])
            longitude = float(row["INTPTLONG"])
            land_area = int(row["ALAND"] or 0)
        except ValueError:
            return
        name, aliases = clean_gazetteer_name(row["NAME"])
        # Prefer Census places over county subdivisions, real names over
        # aliases, then the largest land area (the main city of that name).
        for alias_rank, display_name in enumerate([name, *aliases]):
            normalized = normalize_place_name(display_name)
            if not normalized:
                continue
            yield _Candidate(
                state=row["USPS"],
                name=display_name,
                normalized_name=normalized,
                kind=kind,
                latitude=latitude,
                longitude=longitude,
                rank=(kind_rank, alias_rank, -land_area),
            )

    def _ensure_file(self, path: Path, download: bool) -> None:
        if path.exists():
            return
        if not download:
            raise CommandError(f"{path} not found. Re-run with --download to fetch it from census.gov.")
        url = f"{CENSUS_BASE_URL}/{path.name}"
        self.stdout.write(f"Downloading {url} ...")
        try:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f"Download failed: {exc}") from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
