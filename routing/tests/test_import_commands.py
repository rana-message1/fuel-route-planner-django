import csv
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase

from routing.models import FuelStation, Place

HEADER = ["OPIS Truckstop ID", "Truckstop Name", "Address", "City", "State", "Rack ID", "Retail Price"]
ROWS = [
    ["7", "WOODSHED OF BIG CABIN", "I-44, EXIT 283 & US-69", "Big Cabin", "OK", "307", "3.00733333"],
    # Duplicate OPIS ID with a different name and a lower price: keep the lowest.
    ["20", "PILOT TRAVEL CENTER #1243", "I-8, EXIT 119 & SR-85", "Gila Bend", "AZ", "930", "3.899"],
    ["20", "PILOT #1243", "I-8, EXIT 119 & SR-85", "Gila Bend", "AZ", "930", "3.799"],
    # City padded with spaces and a spelling that differs from the Census name.
    ["160", "PILOT TRAVEL CENTERS #79", "I-12, EXIT 10", "Mc Calla                   ", "AL", "600", "3.23"],
    # Canadian province: skipped.
    ["629", "FLYING J #850", "TCH-16", "Edmonton", "AB", "80", "4.39948962"],
    # Invalid price: skipped.
    ["999", "BROKEN ROW", "US-1", "Big Cabin", "OK", "1", "n/a"],
    # City unknown to the Census table and present in the overrides file.
    ["4211", "FLYING J #513", "I-15, EXIT 1", "Jean", "NV", "910", "3.73233333"],
    # City unknown everywhere: imported without coordinates.
    ["5000", "MYSTERY STOP", "US-99", "Nowhereville", "TX", "1", "3.10"],
]


class ImportFuelPricesTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for state, name, normalized, lat, lon, kind in [
            ("OK", "Big Cabin", "bigcabin", 36.54, -95.22, Place.Kind.PLACE),
            ("AZ", "Gila Bend", "gilabend", 32.95, -112.72, Place.Kind.PLACE),
            ("AL", "McCalla", "mccalla", 33.33, -87.03, Place.Kind.COUNTY_SUBDIVISION),
        ]:
            Place.objects.create(
                state=state, name=name, normalized_name=normalized, latitude=lat, longitude=lon, kind=kind
            )
        self.overrides = self.dir / "overrides.csv"
        self.overrides.write_text("state,city,latitude,longitude,source\nNV,Jean,35.7789,-115.3239,manual\n")

    def tearDown(self):
        self.tmp.cleanup()

    def write_csv(self, header=HEADER, rows=ROWS) -> Path:
        path = self.dir / "prices.csv"
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    def run_import(self, path: Path) -> str:
        out, err = StringIO(), StringIO()
        call_command(
            "import_fuel_prices", str(path), overrides_file=str(self.overrides), stdout=out, stderr=err
        )
        return out.getvalue()

    def test_imports_csv_with_cleaning_dedup_and_geocoding(self):
        output = self.run_import(self.write_csv())

        self.assertEqual(FuelStation.objects.count(), 5)
        self.assertFalse(FuelStation.objects.filter(state="AB").exists())
        self.assertFalse(FuelStation.objects.filter(opis_id=999).exists())

        pilot = FuelStation.objects.get(opis_id=20)
        self.assertEqual(pilot.retail_price, Decimal("3.7990"))
        self.assertEqual(pilot.name, "PILOT #1243")
        self.assertEqual(pilot.price_observations, 2)

        big_cabin = FuelStation.objects.get(opis_id=7)
        self.assertEqual(big_cabin.retail_price, Decimal("3.0073"))
        self.assertEqual(big_cabin.rack_id, 307)
        self.assertEqual((big_cabin.latitude, big_cabin.geocode_source), (36.54, "census_place"))

        mc_calla = FuelStation.objects.get(opis_id=160)
        self.assertEqual(mc_calla.city, "Mc Calla")
        self.assertEqual(mc_calla.geocode_source, "census_county_subdivision")

        jean = FuelStation.objects.get(opis_id=4211)
        self.assertEqual((jean.latitude, jean.geocode_source), (35.7789, "override"))

        mystery = FuelStation.objects.get(opis_id=5000)
        self.assertIsNone(mystery.latitude)
        self.assertEqual(mystery.geocode_source, "none")

        self.assertIn("Rows skipped (outside USA):   1", output)
        self.assertIn("Duplicate rows merged:        1", output)

    def test_imports_xlsx(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(HEADER)
        # Excel stores the IDs and prices as floats.
        sheet.append([7.0, "WOODSHED OF BIG CABIN", "I-44, EXIT 283", "Big Cabin", "OK", 307.0, 3.00733333])
        sheet.append([20.0, "PILOT #1243", "I-8, EXIT 119", "Gila Bend", "AZ", 930.0, 3.899])
        path = self.dir / "prices.xlsx"
        workbook.save(path)

        self.run_import(path)

        self.assertEqual(FuelStation.objects.get(opis_id=7).retail_price, Decimal("3.0073"))
        self.assertEqual(FuelStation.objects.get(opis_id=20).rack_id, 930)

    def test_reimport_replaces_previous_data(self):
        path = self.write_csv()
        self.run_import(path)
        self.run_import(self.write_csv(rows=ROWS[:1]))
        self.assertEqual(list(FuelStation.objects.values_list("opis_id", flat=True)), [7])

    def test_missing_required_column(self):
        path = self.write_csv(header=HEADER[:-1], rows=[row[:-1] for row in ROWS])
        with self.assertRaisesMessage(CommandError, "retail_price"):
            self.run_import(path)

    def test_unsupported_file_type(self):
        path = self.dir / "prices.json"
        path.write_text("{}")
        with self.assertRaises(CommandError):
            self.run_import(path)

    def test_requires_places(self):
        Place.objects.all().delete()
        with self.assertRaisesMessage(CommandError, "import_places"):
            self.run_import(self.write_csv())


class ImportPlacesTests(TestCase):
    PLACES = (
        # The real files pad the last header column with spaces.
        "USPS\tGEOID\tANSICODE\tNAME\tLSAD\tFUNCSTAT\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI"
        "\tINTPTLAT\tINTPTLONG      \n"
        "OK\t1\t1\tBig Cabin town\t43\tA\t100\t0\t0\t0\t36.54\t-95.22\n"
        "TN\t2\t2\tNashville-Davidson metropolitan government (balance)\t00\tF\t900\t0\t0\t0\t36.17\t-86.78\n"
        "TX\t3\t3\tSpringfield CDP\t57\tS\t10\t0\t0\t0\t30.00\t-97.00\n"
        "TX\t4\t4\tSpringfield city\t25\tA\t500\t0\t0\t0\t31.00\t-98.00\n"
    )
    COUSUBS = (
        "USPS\tGEOID\tANSICODE\tNAME\tFUNCSTAT\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG\n"
        "OK\t5\t5\tBig Cabin CCD\tS\t99999\t0\t0\t0\t36.00\t-95.00\n"
        "AL\t6\t6\tMcCalla CCD\tS\t100\t0\t0\t0\t33.33\t-87.03\n"
    )

    def test_loads_places_with_priorities_and_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            places = Path(tmp) / "places.txt"
            cousubs = Path(tmp) / "cousubs.txt"
            places.write_text(self.PLACES)
            cousubs.write_text(self.COUSUBS)
            call_command(
                "import_places", places_file=str(places), cousubs_file=str(cousubs), stdout=StringIO()
            )

        big_cabin = Place.objects.get(state="OK", normalized_name="bigcabin")
        self.assertEqual((big_cabin.name, big_cabin.kind), ("Big Cabin", "place"))  # place beats CCD
        self.assertEqual(Place.objects.get(state="TN", normalized_name="nashville").latitude, 36.17)
        self.assertEqual(Place.objects.get(state="TX", normalized_name="springfield").latitude, 31.0)
        self.assertEqual(Place.objects.get(state="AL", normalized_name="mccalla").kind, "county_subdivision")

    def test_missing_file_without_download(self):
        with self.assertRaisesMessage(CommandError, "--download"):
            call_command("import_places", places_file="/nonexistent/places.zip", stdout=StringIO())
