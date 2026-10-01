from decimal import Decimal
from itertools import pairwise

from django.test import SimpleTestCase

from routing.services.fuel_optimizer import (
    FuelPlan,
    InfeasibleRouteError,
    StationOnRoute,
    VehicleProfile,
    plan_fuel_stops,
)

TRUCK = VehicleProfile(max_range_miles=500, miles_per_gallon=10)


def station(mile: float, price: str, name: str = "") -> StationOnRoute:
    return StationOnRoute(mile=mile, price_per_gallon=Decimal(price), ref=name or f"mile-{mile}")


def replay(plan: FuelPlan, vehicle: VehicleProfile = TRUCK) -> float:
    """
    Drive the plan and assert the tank never runs dry or overflows.
    Returns the fuel left at the destination, in gallons.
    """
    fuel = float(plan.starting_fuel_gallons)
    position = 0.0
    for purchase in plan.purchases:
        fuel -= (purchase.station.mile - position) / vehicle.miles_per_gallon
        assert fuel >= -0.01, f"ran dry before mile {purchase.station.mile}"
        fuel += float(purchase.gallons)
        assert fuel <= vehicle.tank_capacity_gallons + 0.01, "tank overflow"
        position = purchase.station.mile
    fuel -= (plan.total_distance_miles - position) / vehicle.miles_per_gallon
    assert fuel >= -0.01, "ran dry before the destination"
    return fuel


class FuelOptimizerTests(SimpleTestCase):
    def test_short_route_needs_no_fuel_stop(self):
        plan = plan_fuel_stops([station(100, "3.00"), station(200, "2.50")], 300, TRUCK)

        self.assertEqual(plan.purchases, [])
        self.assertEqual(plan.total_cost, Decimal("0.00"))
        self.assertEqual(plan.gallons_consumed, Decimal("30.00"))
        self.assertEqual(plan.fuel_remaining_gallons, Decimal("20.00"))

    def test_route_of_exactly_one_tank_needs_no_stop(self):
        plan = plan_fuel_stops([station(250, "3.00")], 500, TRUCK)
        self.assertEqual(plan.purchases, [])

    def test_one_stop_buys_only_the_fuel_needed(self):
        # Arrive at mile 400 with 10 gal; 300 miles remain -> buy 20 gal, not a full tank.
        plan = plan_fuel_stops([station(400, "3.00")], 700, TRUCK)

        self.assertEqual(len(plan.purchases), 1)
        stop = plan.purchases[0]
        self.assertEqual(stop.fuel_on_arrival_gallons, Decimal("10.00"))
        self.assertEqual(stop.gallons, Decimal("20.00"))
        self.assertEqual(stop.cost, Decimal("60.00"))
        self.assertEqual(plan.total_cost, Decimal("60.00"))
        self.assertAlmostEqual(replay(plan), 0.0, places=2)

    def test_multiple_stops_skip_expensive_station(self):
        stations = [station(450, "3.10"), station(900, "3.00"), station(1300, "3.20")]
        plan = plan_fuel_stops(stations, 1400, TRUCK)

        self.assertEqual([p.station.mile for p in plan.purchases], [450, 900])
        # Mile 450: just enough (40 gal) to reach the cheaper station at 900.
        self.assertEqual(plan.purchases[0].gallons, Decimal("40.00"))
        self.assertEqual(plan.purchases[0].cost, Decimal("124.00"))
        # Mile 900: 50 gal covers the last 500 miles; mile 1300 ($3.20) is skipped.
        self.assertEqual(plan.purchases[1].gallons, Decimal("50.00"))
        self.assertEqual(plan.purchases[1].cost, Decimal("150.00"))
        self.assertEqual(plan.total_cost, Decimal("274.00"))
        self.assertEqual(plan.gallons_purchased, Decimal("90.00"))

    def test_prefers_cheaper_station_when_both_are_reachable(self):
        plan = plan_fuel_stops([station(300, "3.50"), station(400, "3.00")], 800, TRUCK)

        self.assertEqual([p.station.mile for p in plan.purchases], [400])
        self.assertEqual(plan.purchases[0].gallons, Decimal("30.00"))
        self.assertEqual(plan.total_cost, Decimal("90.00"))

    def test_fills_up_at_cheap_station_before_expensive_stretch(self):
        # Mile 300 is the cheapest fuel within reach: fill the tank there and
        # buy only the remainder at the expensive station.
        plan = plan_fuel_stops([station(300, "3.00"), station(400, "3.50")], 900, TRUCK)

        self.assertEqual(
            [(p.station.mile, p.gallons) for p in plan.purchases],
            [(300, Decimal("30.00")), (400, Decimal("10.00"))],
        )
        self.assertEqual(plan.total_cost, Decimal("125.00"))

    def test_never_exceeds_the_500_mile_range(self):
        prices = ["3.40", "2.90", "3.75", "3.10", "3.05", "3.60", "2.85", "3.30", "3.95", "3.15"]
        stations = [station(90 + 180 * i, prices[i % len(prices)]) for i in range(16)]
        plan = plan_fuel_stops(stations, 2900, TRUCK)

        replay(plan)
        stops = [0.0] + [p.station.mile for p in plan.purchases] + [2900.0]
        for leg_start, leg_end in pairwise(stops):
            self.assertLessEqual(leg_end - leg_start, 500)
        self.assertEqual(plan.gallons_purchased + plan.starting_fuel_gallons, Decimal("290.00"))

    def test_costs_use_exact_decimal_rounding(self):
        plan = plan_fuel_stops([station(400, "3.1237")], 723.4, TRUCK)

        stop = plan.purchases[0]
        self.assertEqual(stop.gallons, Decimal("22.34"))  # (723.4 - 400) / 10 - 10 gal on arrival
        self.assertEqual(stop.cost, Decimal("69.78"))  # 22.34 * 3.1237 = 69.783458
        self.assertEqual(plan.total_cost, sum(p.cost for p in plan.purchases))
        self.assertIsInstance(plan.total_cost, Decimal)

    def test_gap_longer_than_range_is_infeasible(self):
        with self.assertRaises(InfeasibleRouteError) as ctx:
            plan_fuel_stops([station(400, "3.00")], 1200, TRUCK)
        self.assertEqual(ctx.exception.gap_start_mile, 400)
        self.assertEqual(ctx.exception.gap_end_mile, 1200)

    def test_long_route_without_stations_is_infeasible(self):
        with self.assertRaises(InfeasibleRouteError):
            plan_fuel_stops([], 750, TRUCK)

    def test_first_station_beyond_starting_range_is_infeasible(self):
        with self.assertRaises(InfeasibleRouteError):
            plan_fuel_stops([station(520, "3.00")], 900, TRUCK)

    def test_stations_outside_route_span_cannot_make_route_feasible(self):
        with self.assertRaises(InfeasibleRouteError):
            plan_fuel_stops([station(-5, "1.00"), station(1500, "1.00")], 900, TRUCK)

    def test_partial_starting_fuel(self):
        plan = plan_fuel_stops([station(100, "3.00")], 300, TRUCK, starting_fuel_gallons=15)
        self.assertEqual(plan.purchases[0].gallons, Decimal("15.00"))
        self.assertEqual(plan.total_cost, Decimal("45.00"))


class StopConsolidationTests(SimpleTestCase):
    """Mile 700 is a micro-stop: buy 1.2 gal to reach a station $0.01 cheaper."""

    stations = [station(400, "3.20"), station(700, "3.00"), station(712, "2.99")]

    def test_pure_cost_optimum_keeps_micro_stop(self):
        plan = plan_fuel_stops(self.stations, 1100, TRUCK, min_savings_per_stop=Decimal("0"))

        self.assertEqual([p.station.mile for p in plan.purchases], [400, 700, 712])
        self.assertEqual(plan.purchases[1].gallons, Decimal("1.20"))
        self.assertEqual(plan.total_cost, Decimal("183.61"))

    def test_micro_stop_removed_when_it_saves_less_than_threshold(self):
        plan = plan_fuel_stops(self.stations, 1100, TRUCK, min_savings_per_stop=Decimal("1.00"))

        self.assertEqual([p.station.mile for p in plan.purchases], [400, 712])
        # Removing the stop costs 24 cents more, well under the $1 threshold.
        self.assertEqual(plan.total_cost, Decimal("183.85"))
        replay(plan)

    def test_required_stop_is_never_removed(self):
        plan = plan_fuel_stops([station(400, "3.00")], 700, TRUCK, min_savings_per_stop=Decimal("1000"))
        self.assertEqual(len(plan.purchases), 1)

    def test_vehicle_profile_validation(self):
        with self.assertRaises(ValueError):
            VehicleProfile(max_range_miles=0)
        self.assertEqual(TRUCK.tank_capacity_gallons, 50)
