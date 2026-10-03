from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator


class TripType(StrEnum):
    ONE_WAY = "one_way"
    ROUND_TRIP = "round_trip"


class CabinClass(StrEnum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


MAX_PASSENGERS = 9
DEFAULT_STAY_DAYS = 7
# At most this many stay lengths are checked per departure day (each one costs a request).
MAX_STAY_LENGTHS = 8
DAYS_PER_MONTH_CHOICES = (4, 8, 15, 31)


def stay_lengths(trip_type: "TripType", minimum: int | None, maximum: int | None) -> list[int]:
    """Stay lengths to query: every day from min to max, evenly thinned to MAX_STAY_LENGTHS.

    One-way trips have none; with a single bound only that length is checked.
    """
    if trip_type is TripType.ONE_WAY:
        return []
    low = minimum if minimum is not None else (maximum or DEFAULT_STAY_DAYS)
    high = maximum if maximum is not None else low
    span = high - low
    if span + 1 <= MAX_STAY_LENGTHS:
        return list(range(low, high + 1))
    step = span / (MAX_STAY_LENGTHS - 1)
    return sorted({low + round(i * step) for i in range(MAX_STAY_LENGTHS)})


class SearchFilters(BaseModel):
    """Filters of a Suchabo. Persisted as JSONB in `searches.filters`.

    `max_stops=None` means "any number of stops", `0` means direct flights only.
    Validation messages are English source texts, translated by the web layer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    trip_type: TripType = TripType.ROUND_TRIP
    cabin_classes: frozenset[CabinClass] = Field(
        default=frozenset({CabinClass.ECONOMY}), min_length=1
    )
    max_stops: int | None = Field(default=None, ge=0, le=3)
    months_ahead: int = Field(default=6, ge=1, le=12)
    stay_days_min: int | None = Field(default=None, ge=1, le=365)
    stay_days_max: int | None = Field(default=None, ge=1, le=365)
    adults: int = Field(default=1, ge=1, le=9)
    # Children aged 2–11. Not every provider can price them (see FlightPriceProvider).
    children: int = Field(default=0, ge=0, le=8)
    currency: str = Field(default="CHF", pattern=r"^[A-Z]{3}$")
    # Sampled departure dates per month (providers that query single dates, e.g. Google Flights).
    days_per_month: int = Field(default=4, ge=1, le=31)

    @model_validator(mode="after")
    def _check_stay(self) -> "SearchFilters":
        has_stay = self.stay_days_min is not None or self.stay_days_max is not None
        if self.trip_type is TripType.ONE_WAY and has_stay:
            raise ValueError("A length of stay is only possible for round trips.")
        if (
            self.stay_days_min is not None
            and self.stay_days_max is not None
            and self.stay_days_min > self.stay_days_max
        ):
            raise ValueError("The minimum stay is longer than the maximum stay.")
        if self.adults + self.children > MAX_PASSENGERS:
            raise ValueError("At most 9 passengers in total.")
        return self

    @field_serializer("cabin_classes")
    def _serialize_cabins(self, value: frozenset[CabinClass]) -> list[str]:
        # Sorted so that identical filters always produce identical JSON snapshots.
        return sorted(cabin.value for cabin in value)

    def to_json(self) -> dict:
        return self.model_dump(mode="json")

    @property
    def stay_lengths(self) -> list[int]:
        return stay_lengths(self.trip_type, self.stay_days_min, self.stay_days_max)
