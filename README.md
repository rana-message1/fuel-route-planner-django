# Fuel Route Planner API

A Django REST API that takes two US locations and returns:

- the driving route (distance, duration, encoded geometry),
- the **cost-optimal fuel stops** along it (where to stop and **how many gallons** to buy at each),
- the **total fuel cost** of the trip,

for a truck with a **500-mile range** at **10 MPG** (a 50-gallon tank), using fuel prices from the supplied OPIS spreadsheet.

A single POST normally makes **one** external API call (the routing request). Repeated routes make **zero**.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/route-plan/ \
  -H "Content-Type: application/json" \
  -d '{"start_location": "New York, NY", "finish_location": "Los Angeles, CA"}'
```

A small map page at `http://127.0.0.1:8000/map/` calls the same API and draws the route and stops.

---

## Contents

1. [Tech stack](#tech-stack)
2. [The fuel-price dataset](#the-fuel-price-dataset)
3. [Architecture](#architecture)
4. [Routing provider](#routing-provider)
5. [Setup](#setup)
6. [API reference](#api-reference)
7. [Fuel-stop optimisation](#fuel-stop-optimisation)
8. [Performance](#performance)
9. [External API call count](#external-api-call-count)
10. [Assumptions and limitations](#assumptions-and-limitations)
11. [Testing](#testing)

---

## Tech stack

| | |
|---|---|
| Language | Python 3.12+ (developed on 3.13) |
| Framework | Django 6.1, Django REST Framework 3.18 |
| Database | SQLite (6.6k stations and 48k places; swap to PostgreSQL by changing `DATABASES`) |
| Routing | OSRM public server (default, no key), or OpenRouteService (free API key) |
| Geocoding | Local US Census Gazetteer table; Nominatim as a cached fallback |
| Other deps | `requests`, `openpyxl` (reads the .xlsx), `python-dotenv` |

No GIS extensions, NumPy or pandas. The spatial work is small and done in plain Python.

---

## The fuel-price dataset

The assessment file ships as `data/fuel-prices-for-be-assessment.xlsx`; an identical `.csv` export sits next to it. I inspected it before designing the model:

| Column | Example | Notes |
|---|---|---|
| `OPIS Truckstop ID` | `7` | Stored as a float in the .xlsx (`7.0`). **Not unique**: see below |
| `Truckstop Name` | `WOODSHED OF BIG CABIN` | |
| `Address` | `I-44, EXIT 283 & US-69` | Highway-exit descriptions, **not street addresses** |
| `City` | `Big Cabin` | 1,256 rows padded with trailing spaces |
| `State` | `OK` | 57 codes, **including 9 Canadian provinces/territories** |
| `Rack ID` | `307` | Wholesale rack; not used for routing |
| `Retail Price` | `3.00733333` | USD/gal, up to 8 decimals, range $2.687 to $6.399 |

Key facts:

- **8,151 rows** and **6,738 distinct OPIS IDs**. 678 IDs appear more than once (up to 6 times), often with different prices, and 227 with different name spellings (`PILOT TRAVEL CENTER #1243` / `PILOT #1243`).
- **620 rows are Canadian** (AB, ON, BC, MB, SK, YT, QC, NB, NS). They are skipped because routes are US-only.
- **No latitude/longitude**, and the addresses cannot be geocoded precisely ("I-80, EXIT 284").
- After cleaning: **6,626 US stations** in 3,808 distinct city/state pairs.

### How stations get coordinates (one-time, offline)

Geocoding 6,626 stations through a public API at request time is out of the question, and at setup it would take hours under Nominatim's 1 request/second limit. Instead, each station is placed at its **city's coordinates**, matched against the public-domain **US Census Gazetteer**:

1. `data/census/2024_Gaz_place_national.zip`: 32k incorporated places and CDPs.
2. `data/census/2024_Gaz_cousubs_national.zip`: 36k county subdivisions (townships, towns).
3. `data/station_geocode_overrides.csv`: 135 unincorporated places that are in neither file, such as Jean NV, Breezewood PA, and Eastaboga AL. 130 were geocoded once with Nominatim via `import_fuel_prices --geocode-missing`; 5 were resolved and checked by hand. The file is committed, so setup never calls Nominatim.

Names are normalised so the spreadsheet's spellings match Census names: `Mc Calla` = `McCalla CDP`, `Saint Johns` = `St. Johns`, `Canon City` = `Cañon City`, `S Coffeyville` = `South Coffeyville`. Consolidated city-counties get their city name as an alias, so `Nashville-Davidson metropolitan government (balance)` answers to `Nashville` and `Macon-Bibb County` to `Macon`.

Result: **all 6,626 US stations geocoded**. 6,310 match a Census place, 131 a county subdivision, and 185 the override file.

Duplicate OPIS IDs are collapsed to **one station at the lowest listed price**; `price_observations` records how many rows were merged. Prices are stored as `Decimal` with 4 decimal places.

---

## Architecture

```
.
├── config/                     Django project (settings driven by env vars, urls, wsgi/asgi)
├── routing/                    The app
│   ├── models.py               Place (Census gazetteer), FuelStation
│   ├── serializers.py          Request validation + response schema
│   ├── views.py                RoutePlanView (POST), HealthView, RouteMapView
│   ├── urls.py
│   ├── exceptions.py           Domain errors -> HTTP status codes
│   ├── services/
│   │   ├── geocoding_service.py   "City, ST" -> local table; free text -> Nominatim (cached)
│   │   ├── routing_service.py     OSRM / OpenRouteService clients, one call per route, cached
│   │   ├── station_service.py     Stations within N miles of the route + their route mile
│   │   ├── fuel_optimizer.py      Pure-Python optimiser (no Django imports)
│   │   └── trip_planner.py        Orchestrates the four steps above
│   ├── utils/
│   │   ├── geo.py              haversine, polyline codec, route resampling, spatial hash
│   │   └── places.py           state codes, name normalisation, input parsing
│   ├── management/commands/
│   │   ├── import_places.py        Census gazetteer -> Place
│   │   └── import_fuel_prices.py   Spreadsheet -> FuelStation
│   ├── templates/routing/map.html  Leaflet demo page
│   └── tests/                  79 tests; all external HTTP mocked
├── data/                       Fuel prices (.xlsx/.csv), Census files, geocode overrides
├── docs/                       Postman collection, example response
├── requirements.txt
├── .env.example
└── pyproject.toml              ruff config
```

### Request flow

```
POST /api/v1/route-plan/
  │
  ├─ RoutePlanRequestSerializer        validate input (400 on error)
  ├─ LocationGeocoder.geocode() x2     "City, ST" -> Place table: no network
  ├─ get_route()                       ONE HTTP call to OSRM/ORS (or cache hit)
  ├─ resample_route()                  route -> points every 1 mile, each with its mile marker
  ├─ find_stations_along_route()       1 indexed SQL query + spatial hash -> stations within 10 mi
  ├─ plan_fuel_stops()                 optimal greedy + stop consolidation
  └─ RoutePlanResponseSerializer       JSON response
```

The layers are separated deliberately. `fuel_optimizer.py` has no Django or HTTP code and is unit-tested with plain numbers. The routing clients only turn HTTP into a `Route` dataclass. The view contains no business logic.

---

## Routing provider

**Default: [OSRM](https://project-osrm.org/) public server (`router.project-osrm.org`)**

- **No API key or sign-up**: the project runs right after `git clone`.
- One `GET /route/v1/driving/{start};{finish}?overview=full` returns distance, duration and the full geometry together. That is everything needed, in one call.
- Built on OpenStreetMap data, with fast responses (about 1 s coast to coast).

**Alternative: [OpenRouteService](https://openrouteservice.org/)**, which has a genuinely free tier of 2,000 directions a day with a key. Set `ROUTING_PROVIDER=openrouteservice` and `ORS_API_KEY=...`. It also supports `ORS_PROFILE=driving-hgv` for truck-legal routing. Both providers sit behind the same `RoutingClient` interface and return the same `Route`.

The OSRM demo server is for fair, light use; that suits an assessment. For production, self-host OSRM or use ORS or a commercial provider.

---

## Setup

### 1. Clone and create a virtual environment (Python 3.12+)

```bash
git clone https://github.com/<you>/fuel-route-planner-django.git
cd fuel-route-planner-django

python3.12 -m venv .venv            # or python3.13
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Environment variables

```bash
cp .env.example .env                # Windows: copy .env.example .env
```

The defaults work as-is for local development. The important variables:

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_DEBUG` | `false` (`true` in `.env.example`) | Debug mode |
| `DJANGO_SECRET_KEY` | none; required when debug is off | Django secret |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Allowed hosts |
| `DJANGO_SECURE_HTTPS` | `false` | Secure cookies, HSTS and SSL redirect when served over HTTPS |
| `ROUTING_PROVIDER` | `osrm` | `osrm` or `openrouteservice` |
| `ORS_API_KEY` | empty | Needed only for OpenRouteService |
| `ROUTING_TIMEOUT_SECONDS` | `20` | Timeout for the routing call |
| `ROUTE_CACHE_SECONDS` | `86400` | How long a computed route is reused |
| `GEOCODER_USER_AGENT` | project UA | Required by Nominatim's usage policy |
| `STATION_SEARCH_RADIUS_MILES` | `10` | Max distance from the route for a station |
| `MIN_SAVINGS_PER_STOP_USD` | `1.00` | A stop must save more than this (0 = pure minimum cost) |
| `VEHICLE_MAX_RANGE_MILES` / `VEHICLE_MPG` | `500` / `10` | Vehicle model |
| `API_THROTTLE_RATE` | `60/minute` | Per-IP throttle protecting the routing API |

### 3. Database and data import (one time, about 3 seconds, no network)

```bash
python manage.py migrate
python manage.py import_places        # Census gazetteer -> 47,789 places (~1.5 s)
python manage.py import_fuel_prices   # spreadsheet -> 6,626 US stations (~1 s)
```

Expected `import_fuel_prices` output:

```
Rows read:                    8,151
Rows skipped (invalid):       0
Rows skipped (outside USA):   620
Duplicate rows merged:        905
Stations imported:            6,626
  geocoded via census_county_subdivision   131
  geocoded via census_place                6,310
  geocoded via override                    185
```

Options:

- `python manage.py import_fuel_prices data/fuel-prices-for-be-assessment.csv` imports the CSV instead (.xlsx is the default).
- `python manage.py import_fuel_prices path/to/new-prices.xlsx --geocode-missing` handles a future price file with new cities. Unmatched cities are geocoded with Nominatim once and appended to the overrides file.
- `python manage.py import_places --download` re-downloads the Census files if `data/census/` is missing.

Imports replace the tables atomically, so re-running them is safe.

### 4. Run

```bash
python manage.py runserver
```

- API: `POST http://127.0.0.1:8000/api/v1/route-plan/`
- Health: `GET http://127.0.0.1:8000/api/v1/health/`
- Map demo: `http://127.0.0.1:8000/map/`

---

## API reference

### `POST /api/v1/route-plan/`

**Request**

```json
{
  "start_location": "New York, NY",
  "finish_location": "Los Angeles, CA"
}
```

Accepted location formats:

- `"City, ST"` or `"City, State"`, optionally with a ZIP or `", USA"`. Resolved locally with no network call.
- `"lat,lon"`, e.g. `"40.7128,-74.0060"`.
- Any other free text, e.g. `"Denver Union Station"`. Resolved by Nominatim, restricted to the USA and cached.

**Response `200`** (real output, abridged; full example in [`docs/example_response.json`](docs/example_response.json))

```json
{
  "start_location": "New York, NY",
  "finish_location": "Los Angeles, CA",
  "start_coordinates": {"latitude": 40.662712, "longitude": -73.938677, "resolved_as": "New York, NY", "source": "census"},
  "finish_coordinates": {"latitude": 34.019394, "longitude": -118.410825, "resolved_as": "Los Angeles, CA", "source": "census"},
  "distance_miles": 2810.4,
  "duration_hours": 50.31,
  "estimated_gallons": 281.04,
  "total_fuel_cost": 699.75,
  "fuel_stops": [
    {
      "station_id": 72288,
      "station_name": "S&G #88",
      "address": "I-475 Exit 13 & US-20",
      "city": "Toledo",
      "state": "OH",
      "latitude": 41.664071,
      "longitude": -83.581861,
      "price_per_gallon": 3.009,
      "distance_from_start_miles": 561.9,
      "off_route_miles": 5.8,
      "fuel_in_tank_on_arrival_gallons": 0.0,
      "gallons_purchased": 29.54,
      "estimated_cost": 88.89
    }
  ],
  "fuel_summary": {
    "number_of_stops": 9,
    "starting_fuel_gallons": 50.0,
    "gallons_purchased": 231.04,
    "gallons_consumed": 281.04,
    "fuel_remaining_gallons": 0.0,
    "cost_basis": "total_fuel_cost is the money spent on fuel bought at the stops during the trip. The truck departs with a full tank; that starting fuel is not purchased on the trip."
  },
  "vehicle": {"max_range_miles": 500.0, "miles_per_gallon": 10.0, "tank_capacity_gallons": 50.0},
  "route": {
    "provider": "osrm",
    "geometry_format": "encoded_polyline_precision_5",
    "geometry": "k_ewF~chbMLrF@P?T^hO@LQ?iEUUA...",
    "point_count": 35143
  },
  "meta": {
    "routing_api_calls": 1,
    "geocoding_api_calls": 0,
    "route_cache_hit": false,
    "candidate_stations_near_route": 459,
    "station_search_radius_miles": 10.0,
    "min_savings_per_stop_usd": 1.0,
    "processing_time_ms": 1125
  }
}
```

Field notes:

- `route.geometry` is a standard [Google encoded polyline](https://developers.google.com/maps/documentation/utilities/polylinealgorithm) (precision 5). It decodes with Leaflet, Google Maps, Mapbox, or `polyline.decode()` in Python.
- `estimated_gallons` is the total fuel burned (`distance / 10`). `fuel_summary.gallons_purchased` is what is bought on the way. The difference is the 50-gallon starting tank.
- `total_fuel_cost` equals the sum of the `estimated_cost` values (each rounded to the cent).
- Station coordinates are city-level (see [limitations](#assumptions-and-limitations)), so `off_route_miles` is approximate.

**Errors**

| Status | `detail.code` | When |
|---|---|---|
| 400 | field errors | Missing/blank/over 200 chars, or start == finish |
| 422 | `location_not_found` | Location not found, or not in the USA (`"Toronto, ON"`) |
| 422 | `no_route_found` | No drivable route between the points |
| 422 | `no_viable_fuel_plan` | Some stretch longer than 500 miles has no station (message gives the mile markers) |
| 429 | `throttled` | Per-IP rate limit exceeded |
| 502 | `routing_provider_error` | Provider returned malformed data or rejected the API key |
| 503 | `routing_unavailable` / `geocoding_unavailable` | Provider unreachable, 5xx, or quota exceeded |
| 504 | `routing_timeout` | Provider did not answer within `ROUTING_TIMEOUT_SECONDS` |

Error body: `{"detail": "No fuel station within 500 miles after mile 300.0 (next refuelling opportunity is at mile 1200.0)."}`

### `GET /api/v1/health/`

Returns `200 {"status": "ok", "fuel_stations": 6626, "geocoded_fuel_stations": 6626, "places": 47789}`, or `503` if the imports have not been run.

### Postman

Import [`docs/postman_collection.json`](docs/postman_collection.json). It includes NY → LA (with a saved example response), Chicago → Dallas, a short trip with no stop, coordinates input, a validation error, a non-US location, and the health check. Set the `base_url` variable if you are not using `127.0.0.1:8000`.

---

## Fuel-stop optimisation

### Model

- Tank: 500 mi / 10 MPG = **50 gallons**. The truck **departs with a full tank**.
- Each nearby station is projected onto the route and gets a **mile marker**: the mile of the closest route point.
- Buying any amount at any station is allowed. Burn is linear: 1 gallon per 10 miles.

### Step 1: optimal greedy (the fixed-route "gas station problem")

This is the classic result from Khuller, Malekian and Mestre, *To Fill or Not to Fill: The Gas Station Problem* (2007). At the current station:

1. Look at every station within one full tank (500 miles) ahead.
2. **If one is cheaper than here**, drive to the *nearest* cheaper one, buying here **only the fuel needed to reach it** (possibly nothing).
3. **Otherwise this is the cheapest fuel within reach**: fill the tank, then drive to the cheapest station in the window. Everything in between is more expensive, so it is skipped.

Two modelling tricks keep the code short:

- The **destination** is a station with price 0. It always counts as "cheaper", so the truck buys exactly enough to arrive empty and never overpays for leftover fuel.
- The **start** is a station where nothing can be bought, so the starting tank is used first.

If the window is ever empty (a stretch of more than 500 miles without a station), the optimiser raises an error naming the gap, and the API returns `422`. This greedy is **provably minimum-cost** and runs in O(n·k), where k is the number of stations within 500 miles. That takes about 1 ms for 459 candidates.

### Step 2: stop consolidation ("avoid unnecessary stops")

The pure optimum makes absurd stops on real data. On NY → LA it stopped in Omaha to buy **1.2 gallons**, only to reach a station 12 miles on that was **$0.007/gal** cheaper, and it made several 1–2 gallon top-ups across Nebraska. So a second pass runs:

> Repeatedly try removing each chosen stop, re-run the optimal greedy on the *remaining* stops, and drop the stop whose removal costs least, **if it costs no more than `MIN_SAVINGS_PER_STOP_USD` (default $1.00)**. Stop when every remaining stop saves more than that.

Guarantees: the final plan is the exact optimum for its set of stops. It costs at most $1 more than the global optimum per removed stop, and it never breaks the 500-mile constraint, because infeasible removals are rejected. Set the threshold to `0` for the pure cost optimum.

Measured on real routes (live OSRM, 10-mile corridor):

| Route | Miles | Pure optimum | Default ($1 threshold) | $5 threshold |
|---|---|---|---|---|
| New York → Los Angeles | 2,810 | 15 stops, $698.62 | **9 stops, $699.75** | 6 stops, $705.64 |
| Seattle → Miami | 3,303 | 18 stops, $848.80 | **10 stops, $850.17** | 8 stops, $852.25 |
| Denver → Atlanta | 1,394 | 7 stops, $259.41 | **3 stops, $259.93** | 3 stops, $259.93 |
| Chicago → Dallas | 961 | 4 stops, $133.23 | **2 stops, $133.38** | 2 stops, $133.38 |

The default removes about half the stops for roughly 0.2% extra cost, so cost stays the primary objective.

### Worked example (from the tests)

Stations at mile 450 ($3.10), 900 ($3.00) and 1300 ($3.20), on a 1,400-mile trip:

- Mile 450: arrive with 5 gal. Mile 900 is cheaper and within 500 miles, so **buy 40 gal** (just enough to reach it): $124.00.
- Mile 900: arrive empty. The destination is 500 miles away, so **buy 50 gal**: $150.00.
- Mile 1300 ($3.20) is skipped. **Total: $274.00.**

### What `total_fuel_cost` means

`total_fuel_cost` is **the money spent at fuel stops during the trip**. The truck is assumed to leave with a full, already-paid-for tank. That tank is the only way to satisfy "never exceed 500 miles" when there may be no station at the exact start point. So a trip of 500 miles or less costs $0 and has no stops. To make this explicit, the response reports `starting_fuel_gallons`, `gallons_purchased`, `gallons_consumed` and `fuel_remaining_gallons`, plus a `cost_basis` sentence. Because the plan always arrives empty, `gallons_purchased = gallons_consumed − 50` for any trip longer than 500 miles.

Money uses `Decimal`: gallons are rounded to 0.01, each stop's cost to the cent (half-up), and the total is the sum of the rounded stop costs, so the numbers on screen always add up.

---

## Performance

| Stage | Time (NY → LA, 2,810 mi) | How |
|---|---|---|
| Geocoding | ~1 ms | `"City, ST"` hits the local `Place` table (unique index on `state, normalized_name`) |
| Routing | ~1,000 ms (first time), 0 ms cached | One provider call; routes cached for 24 h |
| Resampling | ~25 ms | 35k polyline vertices → 2.8k evenly spaced points |
| Station lookup | ~20 ms | **One** SQL query + spatial hash |
| Optimisation | ~3–5 ms | O(n·k) greedy + consolidation |
| **Total** | **~1.1 s first request, ~50 ms repeated** | |

Design decisions:

- **The spreadsheet is never read at request time.** It is imported once into indexed tables; imports take about 1 s and need no network.
- **No per-station API calls.** Stations are matched to the route locally against the geometry from the single routing response.
- **Indexed bounding-box query.** The route is split into ~100-mile chunks, and one query ORs a lat/lon range per chunk (composite index `station_lat_lon_idx`). Measured: NY → LA loads 612 stations (29 boxes) instead of 3,420 in one overall bounding box, and Seattle → Miami loads 571 instead of 5,598. `values_list()` avoids building model instances.
- **Spatial hash.** Route points go into grid cells about as wide as the search radius, so each station checks only its 3×3 neighbourhood. Stations far from the route cost O(1).
- **Caching.** Routes are cached by rounded start/finish coordinates and Nominatim results by query, via Django's cache (LocMem by default; Redis in production). Fuel plans are *not* cached, so a new price import applies immediately.
- **gzip** middleware compresses the response. The full coast-to-coast geometry is ~125 KB raw and ~85 KB gzipped.
- **Throttling** (DRF `AnonRateThrottle`) stops one client from exhausting the free routing API.

---

## External API call count

| Scenario | Routing calls | Geocoding calls |
|---|---|---|
| `"City, ST"` or `"lat,lon"` inputs, new route | **1** | **0** |
| Same start/finish again within 24 h | **0** | **0** |
| Free-text input (e.g. a street address) | 1 | 1 per free-text location, cached 7 days |
| Import commands (setup) | 0 | 0 (overrides file is committed) |

Every response reports this in `meta.routing_api_calls` / `meta.geocoding_api_calls`, and the tests assert that the routing HTTP function is called exactly once per new route.

---

## Assumptions and limitations

- **Station coordinates are city-level.** The dataset only has highway-exit addresses, so stations sit at their city's Census centroid. A 10-mile corridor absorbs this; `off_route_miles` is approximate. A big-city station could be placed a few miles from its real location.
- **Detours to reach a station are not added to fuel use.** Stations are treated as being at their route mile.
- **Duplicate OPIS IDs use the lowest listed price.** Rows likely reflect different price feeds for the same site.
- **Prices are a static snapshot** from the supplied file; re-import to update.
- **Starting tank is full and free** (see "What `total_fuel_cost` means").
- **Routing uses a car profile on OSRM.** Truck restrictions (height, weight, hazmat) need ORS `driving-hgv` or a commercial router.
- **Consolidation reduces stops heuristically.** Cost is within the stated bound of optimal; the stop count is small but not guaranteed minimal.
- **US only.** Canadian stations are excluded, and `"City, XX"` input with a non-US region code is rejected. Free text is restricted to the US by Nominatim but can still fuzzy-match an odd place.
- **Production notes.** Use PostgreSQL (PostGIS could replace the spatial hash), Redis for the shared cache and throttling, a self-hosted OSRM or paid provider, and `DJANGO_DEBUG=false` behind gunicorn.

---

## Testing

```bash
python manage.py test
```

79 tests run in about 0.2 s. All routing and geocoding HTTP calls are mocked with `unittest.mock`, so the suite runs offline.

| Area | Covered cases |
|---|---|
| Request validation | missing/blank/too-long fields, identical start and finish, wrong HTTP method |
| Optimiser | no stop for a short route; one stop buying only what's needed; multiple stops skipping an expensive station; 500-mile constraint (a replay simulator checks the tank never runs dry or overflows); cheaper-station choice; fill-up before an expensive stretch; exact Decimal cost rounding; infeasible gaps; partial starting fuel; micro-stop consolidation |
| API end to end | response contract, total = sum of stops, routing called once and then cached, OSRM parameters, no viable station → 422, far-off-route station ignored |
| Routing failures | timeout → 504, connection error → 503, provider 5xx → 503, malformed JSON / missing geometry → 502, no route → 422, failed calls not cached, OpenRouteService auth/quota/no-route |
| Geocoding | local Census lookup makes no HTTP call; Nominatim fallback called once and then cached; not found; service down; non-US region rejected |
| Station lookup | corridor filtering, route-mile projection, off-route distance, a single SQL query |
| Import commands | CSV and XLSX, header mapping, duplicate merge (lowest price), non-US rows skipped, invalid rows skipped, override/place/subdivision priority, idempotent re-import, missing columns |
| Geometry / names | haversine, polyline codec (Google reference vector), resampling, name normalisation, Census name cleaning, input parsing |

Lint and format (optional): `pip install ruff && ruff check . && ruff format --check .`
