import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

from booking.config import CLOSE_HOUR, HORIZON_DAYS, MOSCOW, OPEN_HOUR, utc_now
from booking.database import Database

JsonObject = dict[str, Any]


class BookingError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


@dataclass(frozen=True)
class Principal:
    id: str
    role: str = "client"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def fingerprint(payload: JsonObject) -> str:
    return token_hash(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


class BookingService:
    def __init__(self, db: Database, now: Callable[[], datetime] = utc_now):
        self.db, self.now = db, now

    def issue_session(self, role: str = "client", user_id: str | None = None) -> str:
        if role not in ("client", "admin"):
            raise ValueError("Неизвестная роль")
        user_id = user_id or str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        expires = int((self.now() + timedelta(days=30 if role == "client" else 1)).timestamp())
        with self.db.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO users VALUES (?, ?)", (user_id, role))
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)", (token_hash(token), user_id, expires)
            )
            connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (int(self.now().timestamp()),)
            )
        return token

    def authenticate(self, token: str | None) -> Principal:
        if not token or len(token) > 256:
            raise BookingError("unauthorized", "Нужна действующая сессия.", 401)
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT users.id, users.role FROM sessions JOIN users ON users.id=sessions.user_id "
                "WHERE token_hash=? AND expires_at>?",
                (token_hash(token), int(self.now().timestamp())),
            ).fetchone()
        if row is None:
            raise BookingError("unauthorized", "Сессия истекла или ключ неверен.", 401)
        return Principal(row["id"], row["role"])

    def revoke(self, token: str | None) -> None:
        if token:
            with self.db.transaction() as connection:
                connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))

    @staticmethod
    def require_admin(principal: Principal) -> None:
        if principal.role != "admin":
            raise BookingError("forbidden", "Нужен доступ администратора.", 403)

    def services(self) -> list[JsonObject]:
        with self.db.read() as connection:
            return [
                dict(row) for row in connection.execute("SELECT * FROM services ORDER BY price_rub")
            ]

    def validate_day(self, day: date) -> None:
        today = self.now().astimezone(MOSCOW).date()
        if not today <= day <= today + timedelta(days=HORIZON_DAYS):
            raise BookingError("invalid_date", "Выберите дату в ближайшие 30 дней.", 422)

    def validate_slot(self, starts_at: datetime) -> int:
        if starts_at.tzinfo is None or starts_at.utcoffset() is None:
            raise BookingError("timezone_required", "Укажите часовой пояс времени записи.", 422)
        local = starts_at.astimezone(MOSCOW)
        self.validate_day(local.date())
        if starts_at <= self.now():
            raise BookingError("past_slot", "На прошедшее время записаться нельзя.", 422)
        if (
            not OPEN_HOUR <= local.hour < CLOSE_HOUR
            or local.minute
            or local.second
            or local.microsecond
        ):
            raise BookingError("invalid_slot", "Доступны часовые слоты с 10:00 до 18:00 МСК.", 422)
        return int(starts_at.timestamp())

    def slots(self, day: date) -> list[JsonObject]:
        self.validate_day(day)
        first = datetime.combine(day, time(OPEN_HOUR), MOSCOW)
        end = datetime.combine(day + timedelta(days=1), time.min, MOSCOW)
        with self.db.read() as connection:
            occupied = {
                row[0]
                for row in connection.execute(
                    "SELECT starts_at FROM bookings WHERE status='confirmed' AND starts_at>=? "
                    "AND starts_at<?",
                    (int(first.timestamp()), int(end.timestamp())),
                )
            }
        result = []
        for hour in range(OPEN_HOUR, CLOSE_HOUR):
            slot = datetime.combine(day, time(hour), MOSCOW)
            if slot <= self.now():
                continue
            epoch = int(slot.timestamp())
            result.append(
                {
                    "starts_at": iso(epoch),
                    "local_time": f"{hour:02}:00",
                    "available": epoch not in occupied,
                }
            )
        return result

    @staticmethod
    def _booking(connection: sqlite3.Connection, booking_id: str) -> JsonObject:
        row = connection.execute(
            "SELECT b.*, s.title AS service_title, s.price_rub, s.duration_minutes "
            "FROM bookings b JOIN services s ON s.id=b.service_id WHERE b.id=?",
            (booking_id,),
        ).fetchone()
        if row is None:
            raise BookingError("not_found", "Запись не найдена.", 404)
        return {
            "id": row["id"],
            "service_id": row["service_id"],
            "service_title": row["service_title"],
            "price_rub": row["price_rub"],
            "duration_minutes": row["duration_minutes"],
            "starts_at": iso(row["starts_at"]),
            "status": row["status"],
            "created_at": iso(row["created_at"]),
            "cancelled_at": iso(row["cancelled_at"]) if row["cancelled_at"] else None,
        }

    @staticmethod
    def _owned(
        connection: sqlite3.Connection, principal: Principal, booking_id: str
    ) -> sqlite3.Row:
        # Одинаковый 404 для чужого и неизвестного ID не раскрывает существование записи.
        row = connection.execute(
            "SELECT * FROM bookings WHERE id=? AND user_id=?", (booking_id, principal.id)
        ).fetchone()
        if row is None:
            raise BookingError("not_found", "Запись не найдена.", 404)
        return cast(sqlite3.Row, row)

    def get(self, principal: Principal, booking_id: str) -> JsonObject:
        with self.db.read() as connection:
            self._owned(connection, principal, booking_id)
            return self._booking(connection, booking_id)

    def list_bookings(self, principal: Principal, day: date | None = None) -> list[JsonObject]:
        with self.db.read() as connection:
            if principal.role == "admin":
                rows = connection.execute(
                    "SELECT id FROM bookings ORDER BY starts_at DESC LIMIT 500"
                )
            else:
                rows = connection.execute(
                    "SELECT id FROM bookings WHERE user_id=? ORDER BY starts_at DESC LIMIT 500",
                    (principal.id,),
                )
            bookings = [self._booking(connection, row[0]) for row in rows.fetchall()]
        if day:
            bookings = [
                b
                for b in bookings
                if datetime.fromisoformat(b["starts_at"]).astimezone(MOSCOW).date() == day
            ]
        return bookings

    def mutate(
        self,
        principal: Principal,
        key: str,
        action: str,
        service_id: str | None = None,
        starts_at: datetime | None = None,
        booking_id: str | None = None,
    ) -> tuple[JsonObject, bool]:
        with self.db.transaction() as connection:
            return self.mutate_in(
                connection, principal, key, action, service_id, starts_at, booking_id
            )

    def mutate_in(
        self,
        connection: sqlite3.Connection,
        principal: Principal,
        key: str,
        action: str,
        service_id: str | None = None,
        starts_at: datetime | None = None,
        booking_id: str | None = None,
    ) -> tuple[JsonObject, bool]:
        if not re.fullmatch(r"[a-zA-Z0-9:_-]{8,128}", key):
            raise BookingError(
                "invalid_key", "Нужен ключ Idempotency-Key длиной 8–128 символов.", 422
            )
        if starts_at and (starts_at.tzinfo is None or starts_at.utcoffset() is None):
            raise BookingError("timezone_required", "Укажите часовой пояс времени записи.", 422)
        digest = fingerprint(
            {
                "action": action,
                "service_id": service_id,
                "booking_id": booking_id,
                "starts_at": starts_at.astimezone(UTC).isoformat() if starts_at else None,
            }
        )
        previous = connection.execute(
            "SELECT * FROM idempotency WHERE user_id=? AND request_key=?", (principal.id, key)
        ).fetchone()
        if previous:
            if previous["fingerprint"] != digest:
                raise BookingError(
                    "idempotency_conflict", "Этот ключ уже использован для другого запроса."
                )
            return json.loads(previous["response"]), True
        now = int(self.now().timestamp())
        try:
            if action == "create":
                if not connection.execute(
                    "SELECT 1 FROM services WHERE id=?", (service_id,)
                ).fetchone():
                    raise BookingError("unknown_service", "Услуга не найдена.", 422)
                if starts_at is None:
                    raise BookingError("missing_slot", "Выберите время записи.", 422)
                epoch = self.validate_slot(starts_at)
                booking_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO bookings VALUES (?, ?, ?, ?, 'confirmed', ?, NULL)",
                    (booking_id, principal.id, service_id, epoch, now),
                )
                event = "created"
            elif action in ("cancel", "reschedule") and booking_id:
                row = self._owned(connection, principal, booking_id)
                if action == "cancel":
                    if row["status"] != "cancelled":
                        if row["starts_at"] <= now:
                            raise BookingError("past_booking", "Время записи уже наступило.")
                        connection.execute(
                            "UPDATE bookings SET status='cancelled', cancelled_at=? WHERE id=?",
                            (now, booking_id),
                        )
                        event = "cancelled"
                    else:
                        event = ""
                else:
                    if row["status"] != "confirmed":
                        raise BookingError(
                            "cancelled_booking", "Отменённую запись перенести нельзя."
                        )
                    if row["starts_at"] <= now:
                        raise BookingError("past_booking", "Время записи уже наступило.")
                    if starts_at is None:
                        raise BookingError("missing_slot", "Выберите новое время.", 422)
                    epoch = self.validate_slot(starts_at)
                    connection.execute(
                        "UPDATE bookings SET starts_at=? WHERE id=?", (epoch, booking_id)
                    )
                    event = "rescheduled" if epoch != row["starts_at"] else ""
            else:
                raise BookingError("invalid_action", "Неизвестное действие.", 422)
        except sqlite3.IntegrityError as error:
            if "bookings.starts_at" in str(error):
                raise BookingError(
                    "slot_taken", "Это время уже занято. Выберите другое."
                ) from error
            raise
        assert booking_id is not None
        result = self._booking(connection, booking_id)
        connection.execute(
            "INSERT INTO idempotency VALUES (?, ?, ?, ?)",
            (principal.id, key, digest, json.dumps(result)),
        )
        if event:
            connection.execute(
                "INSERT INTO events(booking_id, kind, happened_at) VALUES (?, ?, ?)",
                (booking_id, event, now),
            )
        return result, False
