import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK(role IN ('client', 'admin'))
) STRICT;
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    expires_at INTEGER NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS services (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    price_rub INTEGER NOT NULL CHECK(price_rub > 0),
    duration_minutes INTEGER NOT NULL CHECK(duration_minutes = 60)
) STRICT;
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    service_id TEXT NOT NULL REFERENCES services(id),
    starts_at INTEGER NOT NULL CHECK(starts_at % 3600 = 0),
    status TEXT NOT NULL CHECK(status IN ('confirmed', 'cancelled')),
    created_at INTEGER NOT NULL,
    cancelled_at INTEGER,
    CHECK((status = 'confirmed' AND cancelled_at IS NULL) OR
          (status = 'cancelled' AND cancelled_at IS NOT NULL))
) STRICT;
-- Последний рубеж защиты, в том числе для независимых процессов.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking_per_slot
    ON bookings(starts_at) WHERE status = 'confirmed';
CREATE INDEX IF NOT EXISTS bookings_by_user ON bookings(user_id, starts_at);
CREATE TABLE IF NOT EXISTS idempotency (
    user_id TEXT NOT NULL REFERENCES users(id),
    request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    response TEXT NOT NULL,
    PRIMARY KEY(user_id, request_key)
) STRICT;
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    booking_id TEXT NOT NULL REFERENCES bookings(id),
    kind TEXT NOT NULL CHECK(kind IN ('created', 'rescheduled', 'cancelled')),
    happened_at INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    fingerprint TEXT NOT NULL,
    response TEXT NOT NULL
) STRICT;
PRAGMA user_version = 1;
"""

SERVICES = [
    ("consultation", "Консультация", "Знакомство, вопросы и план работы.", 1500, 60),
    ("session", "Индивидуальная сессия", "Один час работы со специалистом.", 2500, 60),
    ("follow-up", "Повторная встреча", "Продолжение работы и следующие шаги.", 2000, 60),
]


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("Неизвестная версия БД. Нужна явная миграция.")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
        with self.transaction() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO services VALUES (?, ?, ?, ?, ?)", SERVICES
            )
            connection.execute(
                "INSERT OR IGNORE INTO meta VALUES ('application', 'booking-assistant')"
            )

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with closing(self.connect()) as connection:
            yield connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
