from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from flighttracker.models.base import Base


class Country(Base):
    __tablename__ = "countries"

    code: Mapped[str] = mapped_column(String(2), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    continent: Mapped[str | None] = mapped_column(String(2))


class Airport(Base):
    __tablename__ = "airports"

    iata_code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    country_code: Mapped[str] = mapped_column(ForeignKey("countries.code"), index=True)
    airport_type: Mapped[str] = mapped_column(String(30))
    has_scheduled_service: Mapped[bool]
    # Highest yearly passenger count since 2019 (Wikidata); only used to rank airports by size.
    passengers: Mapped[int | None] = mapped_column(BigInteger)
