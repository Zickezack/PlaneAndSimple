"""Integration tests against a real PostgreSQL database given by TEST_DATABASE_URL.

The schema is rebuilt from the Alembic migrations (so migrations are tested too) and every
test runs inside a transaction that is rolled back afterwards.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from flighttracker.models import Airport, Country

ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.attributes["connection"] = connection
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture(scope="session")
def engine():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL nicht gesetzt – Integrationstests übersprungen.")
    # The schema gets dropped below – refuse anything that is not clearly a test database.
    if not (make_url(url).database or "").endswith("_test"):
        pytest.exit("TEST_DATABASE_URL muss auf eine Datenbank mit Suffix _test zeigen.")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        command.upgrade(_alembic_config(connection), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def connection(engine):
    with engine.connect() as connection:
        transaction = connection.begin()
        yield connection
        transaction.rollback()


@pytest.fixture
def session_factory(connection) -> sessionmaker[Session]:
    return sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


@pytest.fixture
def db(session_factory) -> Session:
    with session_factory() as session:
        _seed_geo(session)
        # Releases only the savepoint: visible to other sessions on this connection
        # (e.g. web requests), still rolled back with the outer transaction.
        session.commit()
        yield session


def _seed_geo(session: Session) -> None:
    session.add_all([Country(code="CH", name="Switzerland"), Country(code="ES", name="Spain")])
    session.flush()
    session.add_all(
        [
            Airport(
                iata_code=code,
                name=code,
                country_code=country,
                airport_type=kind,
                has_scheduled_service=scheduled,
            )
            for code, country, kind, scheduled in [
                ("ZRH", "CH", "large_airport", True),
                ("GVA", "CH", "large_airport", True),
                ("BRN", "CH", "small_airport", False),
                ("BCN", "ES", "large_airport", True),
                ("MAD", "ES", "large_airport", True),
                ("PMI", "ES", "large_airport", True),
            ]
        ]
    )
    session.flush()
