"""Presentation of price points: flight summary for tables, tooltips and the price chart."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from markupsafe import Markup

from flighttracker.services.history import (
    CalendarDay,
    FlightKey,
    FlightTrend,
    PricePoint,
    flight_key,
)


def sparkline(values: Sequence, width: int = 84, height: int = 24) -> Markup:
    """Tiny inline SVG of a price series (first to latest); colour comes from CSS classes."""
    if len(values) < 2:
        return Markup("")
    low, high = min(values), max(values)
    span = float(high - low)
    pad = 3
    step = (width - 2 * pad) / (len(values) - 1)

    def y(value) -> float:
        if span == 0:
            return height / 2
        return pad + (height - 2 * pad) * (1 - float(value - low) / span)

    coords = [(pad + i * step, y(v)) for i, v in enumerate(values)]
    points = " ".join(f"{x:.1f},{py:.1f}" for x, py in coords)
    direction = "down" if values[-1] < values[0] else "up" if values[-1] > values[0] else "flat"
    last_x, last_y = coords[-1]
    return Markup(  # noqa: S704 - only numbers and fixed class names are interpolated
        f'<svg class="spark spark-{direction}" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" aria-hidden="true">'
        f'<polyline points="{points}" fill="none" stroke="currentColor" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.2" fill="currentColor"/></svg>'
    )


@dataclass(frozen=True)
class Layover:
    airport: str
    minutes: int | None


@dataclass(frozen=True)
class FlightSummary:
    """What a reader needs to judge one price: which flight, when, how long, how many stops."""

    flights: list[str]
    airlines: list[str]
    departure_time: str | None
    arrival_time: str | None
    # Days between departure and arrival (overnight / long layovers), 0 = same day.
    arrival_day_offset: int
    duration_min: int | None
    layovers: list[Layover]
    url: str | None


def _moment(day: str | None, time: str | None) -> datetime | None:
    if not day or not time:
        return None
    return datetime.fromisoformat(f"{day}T{time}")


def summarize(point: PricePoint) -> FlightSummary | None:
    """None when the provider delivered no flight details (e.g. Travelpayouts, mock)."""
    details = point.details
    if not details:
        return None
    segments = details.get("segments") or []
    layovers = []
    for arriving, leaving in zip(segments, segments[1:], strict=False):
        arrival = _moment(arriving.get("arrival_date"), arriving.get("arrival_time"))
        departure = _moment(leaving.get("departure_date"), leaving.get("departure_time"))
        minutes = (
            int((departure - arrival).total_seconds() // 60) if arrival and departure else None
        )
        layovers.append(Layover(arriving.get("to") or "?", minutes))
    arrival_date = details.get("arrival_date")
    offset = (date.fromisoformat(arrival_date) - point.departure_date).days if arrival_date else 0
    return FlightSummary(
        flights=[s["flight"] for s in segments if s.get("flight")],
        airlines=list(details.get("airlines") or ([point.airline] if point.airline else [])),
        departure_time=details.get("departure_time"),
        arrival_time=details.get("arrival_time"),
        arrival_day_offset=max(offset, 0),
        duration_min=details.get("duration_min"),
        layovers=layovers,
        url=details.get("url"),
    )


FLIGHTRADAR_URL = "https://www.flightradar24.com/data/flights/{code}"


def flightradar_url(flight: str | None) -> str | None:
    """Flightradar24 page of a flight number (e.g. "QR 96" → …/flights/qr96): route history,
    aircraft and punctuality of past flights, for own research."""
    if not flight:
        return None
    code = "".join(flight.split()).lower()
    return FLIGHTRADAR_URL.format(code=code) if code.isalnum() else None


@dataclass(frozen=True)
class SegmentView:
    flight: str | None
    flightradar: str | None
    origin: str | None
    destination: str | None
    departure: str
    arrival: str
    duration: str
    aircraft: str | None
    # Layover after this segment (None for the last one).
    layover: str | None


def segments(details: dict | None) -> list[SegmentView]:
    """Segments of the outbound itinerary for the flight detail page."""
    items = (details or {}).get("segments") or []
    result = []
    for index, segment in enumerate(items):
        layover = None
        if index + 1 < len(items):
            arrival = _moment(segment.get("arrival_date"), segment.get("arrival_time"))
            nxt = items[index + 1]
            departure = _moment(nxt.get("departure_date"), nxt.get("departure_time"))
            if arrival and departure:
                layover = format_duration(int((departure - arrival).total_seconds() // 60))
        result.append(
            SegmentView(
                flight=segment.get("flight"),
                flightradar=flightradar_url(segment.get("flight")),
                origin=segment.get("from"),
                destination=segment.get("to"),
                departure=" ".join(
                    v for v in (segment.get("departure_date"), segment.get("departure_time")) if v
                ),
                arrival=" ".join(
                    v for v in (segment.get("arrival_date"), segment.get("arrival_time")) if v
                ),
                duration=format_duration(segment.get("duration_min")),
                aircraft=segment.get("aircraft"),
                layover=layover,
            )
        )
    return result


def format_duration(minutes: int | None) -> str:
    if minutes is None:
        return "–"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def previous_year_lookup(points: Iterable[PricePoint]) -> Callable[[PricePoint], PricePoint | None]:
    """Same route, cabin, currency and stay length, departing on the same date a year earlier."""
    by_key = {
        (p.origin, p.destination, p.cabin_class, p.currency, p.departure_date, p.stay_days): p
        for p in points
    }

    def lookup(point: PricePoint) -> PricePoint | None:
        try:
            earlier = point.departure_date.replace(year=point.departure_date.year - 1)
        except ValueError:  # 29 February
            return None
        key = (point.origin, point.destination, point.cabin_class, point.currency)
        return by_key.get((*key, earlier, point.stay_days))

    return lookup


def _chart_point(
    point: PricePoint,
    previous_year: PricePoint | None,
    trend: FlightTrend | None = None,
    seen_base: date | None = None,
) -> dict:
    summary = summarize(point)
    return {
        "o": point.origin,
        "d": point.destination,
        "cabin": str(point.cabin_class),
        "date": point.departure_date.isoformat(),
        "price": float(point.price),
        # First observed price and all observed prices (change arrows, sparklines).
        "first": float(trend.first) if trend and trend.change else None,
        "hist": [float(value.price) for value in trend.values] if trend else None,
        # Poll date of each `hist` price, as days after `seenBase` (price-over-time chart).
        "seen": [(value.observed_at.date() - seen_base).days for value in trend.values]
        if trend and seen_base
        else None,
        "changes": [
            {"period": change.period, "amount": float(change.amount)} for change in trend.changes
        ]
        if trend
        else [],
        "ret": point.return_date.isoformat() if point.return_date else None,
        "stay": point.stay_days,
        "stops": point.stops,
        "prev": float(point.previous_price) if point.previous_price is not None else None,
        "py": float(previous_year.price) if previous_year else None,
        "fake": point.fake,
        "flights": summary.flights if summary else [],
        "airline": ", ".join(summary.airlines) if summary else point.airline,
        "dep": summary.departure_time if summary else None,
        "arr": summary.arrival_time if summary else None,
        "arrDays": summary.arrival_day_offset if summary else 0,
        "dur": format_duration(summary.duration_min) if summary and summary.duration_min else None,
        "via": [
            {"airport": lay.airport, "wait": format_duration(lay.minutes)}
            for lay in (summary.layovers if summary else [])
        ],
        "url": summary.url if summary else None,
        "id": point.id,
    }


def chart_data(
    points: Iterable[PricePoint],
    today: date,
    currency: str,
    labels: Mapping[str, str],
    detail_url: str = "",
    trends: Mapping[FlightKey, FlightTrend] | None = None,
    calendar: Iterable[CalendarDay] = (),
) -> dict:
    """JSON for static/js/price-chart.js: upcoming points in the Suchabo's currency.

    `py` is the price for the same route, cabin, stay and date one year earlier (own tracking);
    `first`, `hist` and interval changes come from observations of the same flight.
    """
    all_points = list(points)
    year_before = previous_year_lookup(all_points)
    trends = trends or {}
    upcoming = [p for p in all_points if p.departure_date >= today and p.currency == currency]
    seen_base = min((trend.values[0].observed_at.date() for trend in trends.values()), default=None)
    return {
        "currency": currency,
        "routes": sorted({f"{p.origin}-{p.destination}" for p in all_points}),
        "points": [
            _chart_point(p, year_before(p), trends.get(flight_key(p)), seen_base) for p in upcoming
        ],
        "seenBase": seen_base.isoformat() if seen_base else None,
        # Price calendar (heat map): cheapest price per departure day, without flight details.
        "calendar": [
            {
                "o": day.origin,
                "d": day.destination,
                "cabin": day.cabin_class,
                "date": day.departure_date.isoformat(),
                "stay": day.stay_days,
                "price": float(day.price),
                "fake": day.fake,
            }
            for day in calendar
            if day.departure_date >= today and day.currency == currency
        ],
        "labels": dict(labels),
        # Flight detail page of a point: detail_url + point id.
        "detailUrl": detail_url,
    }
