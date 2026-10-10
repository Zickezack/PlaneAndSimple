from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from flighttracker.domain.trips import LayoverRule, connection_fits, find_itineraries


def quote(origin, destination, departure, departure_time, arrival, arrival_time, price):
    return SimpleNamespace(
        origin=origin,
        destination=destination,
        cabin_class="economy",
        currency="CHF",
        departure_date=date.fromisoformat(departure),
        price=Decimal(price),
        details={
            "departure_time": departure_time,
            "arrival_date": arrival,
            "arrival_time": arrival_time,
        },
    )


def test_connection_requires_same_airport_and_layover_window():
    first = quote("ZRH", "IST", "2027-05-10", "08:00", "2027-05-10", "12:00", "100")
    valid = quote("IST", "JFK", "2027-05-11", "00:30", "2027-05-11", "08:00", "200")
    wrong_airport = quote("SAW", "JFK", "2027-05-11", "00:30", "2027-05-11", "08:00", "200")

    assert connection_fits(first, valid, LayoverRule(1, 1))
    assert not connection_fits(first, valid, LayoverRule(2, 3))
    assert not connection_fits(first, wrong_airport, LayoverRule(1, None))


def test_calendar_day_stay_requires_a_later_date_and_forward_time():
    first = quote("ZRH", "IST", "2027-05-10", "08:00", "2027-05-10", "23:50", "100")
    same_day = quote("IST", "JFK", "2027-05-10", "23:59", "2027-05-11", "08:00", "200")
    next_day = quote("IST", "JFK", "2027-05-11", "00:10", "2027-05-11", "08:00", "200")
    before_arrival = quote("IST", "JFK", "2027-05-10", "23:40", "2027-05-11", "08:00", "200")

    assert not connection_fits(first, same_day, LayoverRule(1, 1))
    assert connection_fits(first, next_day, LayoverRule(1, 1))
    assert not connection_fits(first, before_arrival, LayoverRule(1, 1))


def test_find_itineraries_filters_dates_and_sorts_by_total_price():
    first = quote("ZRH", "IST", "2027-05-10", "08:00", "2027-05-10", "12:00", "100")
    cheap_connection = quote("IST", "JFK", "2027-05-10", "14:00", "2027-05-10", "20:00", "200")
    expensive_connection = quote("IST", "JFK", "2027-05-10", "15:00", "2027-05-10", "21:00", "300")
    outside_window = quote("IST", "JFK", "2027-05-11", "14:00", "2027-05-11", "20:00", "50")

    itineraries = find_itineraries(
        [[first], [expensive_connection, outside_window, cheap_connection]],
        [LayoverRule(0, 1)],
        date(2027, 5, 10),
        date(2027, 5, 10),
    )

    assert [option.total_price for option in itineraries] == [Decimal("300"), Decimal("400")]
    assert all(len(option.legs) == 2 for option in itineraries)


def test_missing_times_cannot_claim_a_working_connection():
    first = quote("ZRH", "IST", "2027-05-10", "08:00", "2027-05-10", "12:00", "100")
    missing = quote("IST", "JFK", "2027-05-10", None, "2027-05-10", "20:00", "200")
    assert (
        find_itineraries(
            [[first], [missing]], [LayoverRule()], date(2027, 5, 10), date(2027, 5, 10)
        )
        == []
    )


def test_find_itineraries_returns_the_cheapest_even_beyond_the_limit_in_search_order():
    # Many combinations from expensive first-leg flights must not crowd out the cheap one.
    expensive_firsts = [
        quote("ZRH", "IST", "2027-05-10", f"{6 + i:02d}:00", "2027-05-10", f"{7 + i:02d}:00", "900")
        for i in range(5)
    ]
    cheap_first = quote("ZRH", "IST", "2027-05-10", "05:00", "2027-05-10", "06:00", "50")
    seconds = [
        quote(
            "IST",
            "JFK",
            "2027-05-11",
            f"{8 + i:02d}:00",
            "2027-05-11",
            f"{9 + i:02d}:00",
            str(100 + i),
        )
        for i in range(4)
    ]
    itineraries = find_itineraries(
        [expensive_firsts + [cheap_first], seconds],
        [LayoverRule(1, 1)],
        date(2027, 5, 10),
        date(2027, 5, 11),
        limit=3,
    )
    assert [option.total_price for option in itineraries] == [
        Decimal("150"),
        Decimal("151"),
        Decimal("152"),
    ]


def test_find_itineraries_keeps_the_first_found_of_equal_totals():
    first = quote("ZRH", "IST", "2027-05-10", "08:00", "2027-05-10", "12:00", "100")
    a = quote("IST", "JFK", "2027-05-11", "08:00", "2027-05-11", "12:00", "100")
    b = quote("IST", "JFK", "2027-05-11", "09:00", "2027-05-11", "13:00", "100")
    (only,) = find_itineraries(
        [[first], [a, b]], [LayoverRule(1, 1)], date(2027, 5, 10), date(2027, 5, 11), limit=1
    )
    assert only.legs[1] is a
