from flighttracker.domain.layovers import itinerary_warnings, layovers


def segment(origin, destination, departure, arrival):
    """`departure` / `arrival`: "YYYY-MM-DD HH:MM" in local time."""
    dep_date, dep_time = departure.split()
    arr_date, arr_time = arrival.split()
    return {
        "from": origin,
        "to": destination,
        "departure_date": dep_date,
        "departure_time": dep_time,
        "arrival_date": arr_date,
        "arrival_time": arr_time,
    }


def test_short_connection_and_airport_change():
    trip = [
        segment("ZRH", "LHR", "2026-12-01 10:15", "2026-12-01 11:10"),
        segment("LGW", "BKK", "2026-12-01 13:00", "2026-12-02 06:00"),
    ]
    (stop,) = layovers(trip)
    assert (stop.arrival_airport, stop.departure_airport, stop.minutes) == ("LHR", "LGW", 110)
    # Under three hours is short when the airport changes, an hour otherwise.
    assert stop.warnings == ("airport_change", "short")


def test_overnight_and_long_layover():
    trip = [
        segment("ZRH", "BCN", "2026-11-20 21:20", "2026-11-20 23:15"),
        segment("BCN", "LIS", "2026-11-21 07:15", "2026-11-21 08:30"),
    ]
    assert layovers(trip)[0].warnings == ("overnight", "long")


def test_comfortable_connection_has_no_warning():
    trip = [
        segment("ZRH", "DOH", "2026-12-01 15:10", "2026-12-01 22:50"),
        segment("DOH", "BKK", "2026-12-02 01:40", "2026-12-02 12:30"),
    ]
    # Leaving after midnight local time counts as overnight; 2 h 50 min is neither short nor long.
    assert layovers(trip)[0].warnings == ("overnight",)
    same_day = [trip[0], segment("DOH", "BKK", "2026-12-01 23:55", "2026-12-02 10:00")]
    assert layovers(same_day)[0].warnings == ()
    assert layovers(same_day[:1]) == []


def test_itinerary_warnings_include_the_return_flight():
    outbound = [segment("ZRH", "BKK", "2026-12-01 10:00", "2026-12-02 05:00")]
    back = [
        segment("BKK", "IST", "2026-12-14 08:00", "2026-12-14 14:00"),
        segment("IST", "ZRH", "2026-12-14 14:40", "2026-12-14 16:40"),
    ]
    details = {"segments": outbound, "return": {"segments": back}}
    assert itinerary_warnings(details) == ("short",)
    assert itinerary_warnings(None) == ()
    # Missing times: no length, no warning about it.
    assert layovers([{"to": "IST"}, {"from": "IST"}])[0].warnings == ()
