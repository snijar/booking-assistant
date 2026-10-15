import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
SLOT_MINUTES = 60
OPEN_HOUR = 10
CLOSE_HOUR = 18
HORIZON_DAYS = 30


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    secure_cookies: bool = False

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            Path(os.environ.get("BOOKING_DB_PATH", "data/demo.sqlite3")),
            os.environ.get("BOOKING_SECURE_COOKIES", "false").lower() == "true",
        )
