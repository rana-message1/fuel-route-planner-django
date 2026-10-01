"""
Minimum-cost refuelling plan along a fixed route.

Problem
-------
The route is a line from mile 0 to mile D. Stations sit at known mile markers
with known prices. The tank holds ``max_range / mpg`` gallons, fuel burn is
linear in distance, and the truck may buy any amount at a station. Minimise
the money spent on fuel without ever running dry.

Algorithm (greedy, optimal for a fixed route)
---------------------------------------------
This is the fixed-path "gas station problem" (Khuller, Malekian & Mestre,
"To Fill or Not to Fill", 2007). At the current station ``i``:

1. Look ahead one full tank (``max_range`` miles).
2. If some station in that window is *cheaper* than ``i``, drive to the
   *nearest* such station, buying at ``i`` only the fuel needed to get there
   (possibly none). Fuel at ``i`` is only worth buying until a cheaper price is
   reachable.
3. Otherwise ``i`` is the cheapest option within reach: fill the tank, then
   drive to the cheapest station in the window (ties -> the farthest one),
   since every station in between is more expensive and can be skipped.

The destination is modelled as a free (price 0) station, so the truck buys
exactly enough to arrive with an empty tank and no fuel is wasted. The start is
modelled as a station where nothing can be bought, so starting fuel is used
first. Stations where the plan buys nothing are simply driven past and are not
reported as stops.

Each station is visited at most once and the window scan is bounded by the
number of stations within one tank, so the run time is O(n * k) with k the
number of stations per 500 miles.

Stop consolidation
------------------
The pure cost optimum happily makes stops that save fractions of a cent
(buy 1.2 gal, drive 12 miles, buy at a station $0.007 cheaper). A second pass
removes such stops: for every stop it re-runs the greedy using only the
*other* chosen stops and drops the stop whose removal increases the total
least, as long as that increase is at most ``min_savings_per_stop``. It
repeats until every remaining stop saves more than that amount. The result is
the exact optimum for its set of stops and costs at most
``min_savings_per_stop`` more than the global optimum per removed stop. With
``min_savings_per_stop = 0`` only stops that save nothing at all are removed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

CENTS = Decimal("0.01")
_EPSILON = 1e-6  # miles; absorbs float noise in distance comparisons


def to_money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def to_gallons(value: float) -> Decimal:
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class VehicleProfile:
    max_range_miles: float = 500.0
    miles_per_gallon: float = 10.0

    def __post_init__(self) -> None:
        if self.max_range_miles <= 0 or self.miles_per_gallon <= 0:
            raise ValueError("Vehicle range and MPG must be positive.")

    @property
    def tank_capacity_gallons(self) -> float:
        return self.max_range_miles / self.miles_per_gallon


@dataclass(frozen=True)
class StationOnRoute:
    """A station projected onto the route. ``ref`` carries caller data through."""

    mile: float
    price_per_gallon: Decimal
    ref: Any = None


@dataclass(frozen=True)
class FuelPurchase:
    station: StationOnRoute
    fuel_on_arrival_gallons: Decimal
    gallons: Decimal
    cost: Decimal


@dataclass(frozen=True)
class FuelPlan:
    purchases: list[FuelPurchase]
    total_distance_miles: float
    starting_fuel_gallons: Decimal
    gallons_consumed: Decimal
    gallons_purchased: Decimal
    fuel_remaining_gallons: Decimal
    total_cost: Decimal


class InfeasibleRouteError(Exception):
    """A stretch of the route is longer than one tank and has no station in it."""

    def __init__(self, gap_start_mile: float, gap_end_mile: float, max_range_miles: float):
        self.gap_start_mile = gap_start_mile
        self.gap_end_mile = gap_end_mile
        super().__init__(
            f"No fuel station within {max_range_miles:g} miles after mile {gap_start_mile:.1f} "
            f"(next refuelling opportunity is at mile {gap_end_mile:.1f})."
        )


@dataclass(frozen=True)
class _Node:
    mile: float
    price: float  # float for comparisons only; money maths uses Decimal
    station: StationOnRoute | None  # None for the start and the destination


def plan_fuel_stops(
    stations: Sequence[StationOnRoute],
    total_distance_miles: float,
    vehicle: VehicleProfile,
    starting_fuel_gallons: float | None = None,
    min_savings_per_stop: Decimal = Decimal("0"),
) -> FuelPlan:
    """
    Choose where to stop and how many gallons to buy at each stop.

    ``starting_fuel_gallons`` defaults to a full tank. A stop is only kept if
    it saves more than ``min_savings_per_stop`` dollars. Raises
    ``InfeasibleRouteError`` if the route cannot be driven with these stations.
    """
    if total_distance_miles < 0:
        raise ValueError("total_distance_miles must be non-negative.")
    if min_savings_per_stop < 0:
        raise ValueError("min_savings_per_stop must be non-negative.")
    capacity = vehicle.tank_capacity_gallons
    if starting_fuel_gallons is None:
        starting_fuel_gallons = capacity
    if not 0 <= starting_fuel_gallons <= capacity:
        raise ValueError(f"starting_fuel_gallons must be between 0 and {capacity:g}.")

    plan = _cheapest_plan(stations, total_distance_miles, vehicle, starting_fuel_gallons)
    return _consolidate_stops(plan, vehicle, starting_fuel_gallons, min_savings_per_stop)


def _consolidate_stops(
    plan: FuelPlan, vehicle: VehicleProfile, starting_fuel_gallons: float, min_savings_per_stop: Decimal
) -> FuelPlan:
    while plan.purchases:
        chosen = [purchase.station for purchase in plan.purchases]
        best: FuelPlan | None = None
        for skipped in range(len(chosen)):
            remaining = chosen[:skipped] + chosen[skipped + 1 :]
            try:
                alternative = _cheapest_plan(
                    remaining, plan.total_distance_miles, vehicle, starting_fuel_gallons
                )
            except InfeasibleRouteError:
                continue  # this stop is required to stay within range
            increase = alternative.total_cost - plan.total_cost
            if increase <= min_savings_per_stop and (
                best is None or alternative.total_cost < best.total_cost
            ):
                best = alternative
        if best is None:
            return plan
        plan = best
    return plan


def _cheapest_plan(
    stations: Sequence[StationOnRoute],
    total_distance_miles: float,
    vehicle: VehicleProfile,
    starting_fuel_gallons: float,
) -> FuelPlan:
    """The greedy described in the module docstring (minimum fuel cost)."""
    max_range = vehicle.max_range_miles
    mpg = vehicle.miles_per_gallon

    usable = sorted(
        (s for s in stations if 0 <= s.mile <= total_distance_miles),
        key=lambda s: (s.mile, s.price_per_gallon),
    )
    nodes = (
        [_Node(0.0, math.inf, None)]  # start: nothing can be bought here
        + [_Node(s.mile, float(s.price_per_gallon), s) for s in usable]
        + [_Node(total_distance_miles, 0.0, None)]  # destination: "free" fuel
    )
    last = len(nodes) - 1

    purchases: list[FuelPurchase] = []
    fuel_miles = starting_fuel_gallons * mpg  # fuel expressed as remaining range
    i = 0
    while i < last:
        here = nodes[i]
        next_cheaper: int | None = None
        cheapest: int | None = None
        j = i + 1
        while j <= last and nodes[j].mile - here.mile <= max_range + _EPSILON:
            if nodes[j].price < here.price:
                next_cheaper = j
                break
            if cheapest is None or nodes[j].price <= nodes[cheapest].price:
                cheapest = j  # "<=" prefers the farthest among equal prices
            j += 1

        if next_cheaper is None and cheapest is None:
            raise InfeasibleRouteError(here.mile, nodes[i + 1].mile, max_range)

        if next_cheaper is not None:
            target = next_cheaper
            buy_miles = max(0.0, (nodes[target].mile - here.mile) - fuel_miles)
        else:
            target = cheapest
            buy_miles = max(0.0, max_range - fuel_miles)

        if buy_miles > _EPSILON:
            if here.station is None:
                # Only possible at the start with less than a full tank.
                raise InfeasibleRouteError(here.mile, nodes[target].mile, fuel_miles)
            gallons = to_gallons(buy_miles / mpg)
            if gallons > 0:
                purchases.append(
                    FuelPurchase(
                        station=here.station,
                        fuel_on_arrival_gallons=to_gallons(fuel_miles / mpg),
                        gallons=gallons,
                        cost=to_money(gallons * here.station.price_per_gallon),
                    )
                )
            fuel_miles += buy_miles

        fuel_miles = max(0.0, fuel_miles - (nodes[target].mile - here.mile))
        i = target

    gallons_purchased = sum((p.gallons for p in purchases), Decimal("0"))
    return FuelPlan(
        purchases=purchases,
        total_distance_miles=total_distance_miles,
        starting_fuel_gallons=to_gallons(starting_fuel_gallons),
        gallons_consumed=to_gallons(total_distance_miles / mpg),
        gallons_purchased=gallons_purchased,
        fuel_remaining_gallons=to_gallons(fuel_miles / mpg),
        total_cost=sum((p.cost for p in purchases), Decimal("0.00")),
    )
