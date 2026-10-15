from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from booking.api import create_app
from booking.config import Settings
from booking.database import Database
from booking.service import BookingService, Principal


@dataclass
class Clock:
    value: datetime = datetime(2026, 10, 15, 6, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def service(db: Database, clock: Clock) -> BookingService:
    return BookingService(db, clock)


@pytest.fixture
def alice(service: BookingService) -> Principal:
    return service.authenticate(service.issue_session())


@pytest.fixture
def bob(service: BookingService) -> Principal:
    return service.authenticate(service.issue_session())


@pytest.fixture
def client(db: Database, clock: Clock) -> Iterator[TestClient]:
    app = create_app(Settings(db.path), clock)
    with TestClient(app, headers={"X-Requested-With": "booking-assistant"}) as http:
        http.post("/api/session")
        yield http
