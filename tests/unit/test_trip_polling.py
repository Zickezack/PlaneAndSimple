from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from flighttracker.domain.trips import LayoverRule
from flighttracker.services.trip_polling import connect_quotes


def quote(origin, destination, day, departure, arrival, price="100", currency="CHF"):
    return SimpleNamespace(
        origin=origin,
        destination=destination,
        currency=currency,
        departure_date=date.fromisoformat(day),
        price=Decimal(price),
        details={"departure_time": departure, "arrival_date": day, "arrival_time": arrival},
    )


def test_one_path_per_last_flight_and_no_cap():
    # 1200 first-leg flights all connect to the same two second-leg flights.
    firsts = [quote("ZRH", "LIS", "2027-05-10", "06:00", "08:00", str(i)) for i in range(1200)]
    seconds = [
        quote("LIS", "OPO", "2027-05-12", "08:00", "09:00"),
        quote("LIS", "OPO", "2027-05-13", "08:00", "09:00"),
    ]
    connected, paths = connect_quotes(
        [(f,) for f in firsts],
        seconds,
        LayoverRule(2, 3),
        final_leg=False,
        ends_on=date(2027, 5, 20),
    )
    assert connected == seconds
    assert [
        path[-1] for path in paths
    ] == seconds  # 2 states instead of 2400 (formerly cut at 1000)
