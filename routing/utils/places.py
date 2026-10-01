"""US place-name helpers: state lookup, name normalization and query parsing."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# fmt: off
US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
# fmt: on
_STATE_BY_NAME = {name.lower(): code for code, name in US_STATES.items()}

# Census gazetteer names carry a legal/statistical descriptor ("Tomah city",
# "Laurel CDP", "Nashville-Davidson metropolitan government (balance)").
# Descriptors are lower case (except CDP/CCD/UT), so matching is case-sensitive:
# "Carson City" and "Salt Lake City city" both keep the "City" that is part of the name.
_GAZETTEER_DESCRIPTOR = re.compile(
    r"\s+(?:city and borough|consolidated government|unified government|metropolitan government"
    r"|metro government|urban county|charter township|unorganized territory|city|town|village"
    r"|borough|municipality|township|plantation|CDP|CCD|UT|gore|grant|location|purchase"
    r"|precinct|district|comunidad|zona urbana|barrio)$"
)
_BALANCE = re.compile(r"\s*\(balance\)\s*$")
# Consolidated city-county governments: "... unified government", "Lexington-Fayette
# urban county", or simply "Macon-Bibb County".
_CONSOLIDATED = re.compile(r"(?:government|urban county|-[\w .]+ County)$")

# Abbreviations are expanded in the same direction on both sides of a match, so
# "Saint Johns" == "St. Johns" and "S Coffeyville" == "South Coffeyville".
_WORD_ALIASES = {
    "saint": "st",
    "sainte": "ste",
    "mount": "mt",
    "fort": "ft",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
}

_COORDINATES = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_TRAILING_COUNTRY = re.compile(r"(?:,\s*|\s+)(?:usa|u\.s\.a\.|us|united states(?: of america)?)\s*$", re.I)
_TRAILING_ZIP = re.compile(r"\s+\d{5}(?:-\d{4})?\s*$")


def normalize_place_name(name: str) -> str:
    """
    Canonical matching key for a place name.

    "Mc Calla" -> "mccalla", "Cañon City" -> "canoncity", "Saint Johns" -> "stjohns".
    """
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_name.lower())
    return "".join(_WORD_ALIASES.get(word, word) for word in words)


def clean_gazetteer_name(raw_name: str) -> tuple[str, list[str]]:
    """
    Strip the descriptor from a Census gazetteer name.

    Returns the display name plus alias names. Consolidated city-county
    governments ("Macon-Bibb County unified government (balance)") also get
    their leading city as an alias ("Macon"), because that is how people and
    the fuel dataset refer to them.
    """
    name = _BALANCE.sub("", raw_name.strip())
    is_consolidated = bool(_CONSOLIDATED.search(name))
    name = _GAZETTEER_DESCRIPTOR.sub("", name).strip()
    aliases: list[str] = []
    if is_consolidated:
        leading = re.split(r"[-/]", name, maxsplit=1)[0].strip()
        if leading and leading != name:
            aliases.append(leading)
    return name, aliases


def state_code(value: str) -> str | None:
    """Return the two-letter code for a state code or full state name."""
    value = value.strip().rstrip(".")
    if value.upper() in US_STATES:
        return value.upper()
    return _STATE_BY_NAME.get(value.lower())


@dataclass(frozen=True)
class ParsedLocation:
    city: str | None = None
    state: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    # A two-letter region that is not a US state, e.g. "ON" in "Toronto, ON".
    non_us_region: str | None = None

    @property
    def is_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


def parse_location_query(query: str) -> ParsedLocation:
    """
    Parse user input such as ``"New York, NY"``, ``"Austin, Texas 78701, USA"``
    or ``"40.7128,-74.0060"``. Anything else yields an empty ``ParsedLocation``
    and is left to the external geocoder.
    """
    match = _COORDINATES.match(query)
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return ParsedLocation(latitude=lat, longitude=lon)
        return ParsedLocation()

    text = _TRAILING_COUNTRY.sub("", query.strip())
    text = _TRAILING_ZIP.sub("", text)
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) != 2:
        return ParsedLocation()
    city, state_part = parts
    state_part = _TRAILING_ZIP.sub("", state_part)
    code = state_code(state_part)
    if code is None:
        if re.fullmatch(r"[A-Za-z]{2}", state_part):
            return ParsedLocation(non_us_region=state_part.upper())
        return ParsedLocation()
    return ParsedLocation(city=city, state=code)
