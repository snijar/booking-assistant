import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest

from booking.config import MOSCOW
from booking.database import Database
from booking.service import BookingError, BookingService, Principal

FIRST = datetime(2026, 10, 16, 10, tzinfo=MOSCOW)
SECOND = FIRST + timedelta(hours=1)


def create(
    service: BookingService, user: Principal, key: str = "create-0001", slot: datetime = FIRST
) -> dict:
    return service.mutate(user, key, "create", "consultation", slot)[0]


def test_booking_is_persisted_with_server_price(service, alice):
    booking = create(service, alice)
    assert booking["status"] == "confirmed"
    assert booking["price_rub"] == 1500
    assert booking["starts_at"] == "2026-10-16T07:00:00Z"
    assert service.get(alice, booking["id"]) == booking
    assert not service.slots(FIRST.date())[0]["available"]


def test_occupied_slot_returns_conflict(service, alice, bob):
    create(service, alice)
    with pytest.raises(BookingError, match="занято") as error:
        create(service, bob)
    assert error.value.code == "slot_taken"


@pytest.mark.parametrize(
    "slot",
    [
        datetime(2026, 10, 14, 10, tzinfo=MOSCOW),
        datetime(2026, 10, 15, 8, tzinfo=MOSCOW),
        datetime(2026, 10, 16, 9, tzinfo=MOSCOW),
        datetime(2026, 10, 16, 18, tzinfo=MOSCOW),
        datetime(2026, 10, 16, 10, 30, tzinfo=MOSCOW),
        datetime(2026, 10, 16, 10, 0, 1, tzinfo=MOSCOW),
        datetime(2026, 10, 16, 10, 0, 0, 1, tzinfo=MOSCOW),
        datetime(2026, 10, 16, 10),
        datetime(2026, 11, 15, 10, tzinfo=MOSCOW),
    ],
)
def test_invalid_times_do_not_create_booking(service, alice, slot):
    with pytest.raises(BookingError):
        create(service, alice, slot=slot)
    assert service.list_bookings(alice) == []


def test_unknown_service(service, alice):
    with pytest.raises(BookingError) as error:
        service.mutate(alice, "unknown-0001", "create", "unknown", FIRST)
    assert error.value.code == "unknown_service"


def test_retry_returns_original_snapshot_even_after_cancel(service, alice):
    result, replayed = service.mutate(alice, "retry-0001", "create", "consultation", FIRST)
    assert not replayed
    service.mutate(alice, "cancel-0001", "cancel", booking_id=result["id"])
    repeated, replayed = service.mutate(alice, "retry-0001", "create", "consultation", FIRST)
    assert replayed and repeated == result
    assert service.get(alice, result["id"])["status"] == "cancelled"


@pytest.mark.parametrize("change", ["payload", "action"])
def test_idempotency_key_cannot_mean_another_request(service, alice, change):
    booking = create(service, alice, "shared-key-001")
    with pytest.raises(BookingError) as error:
        if change == "payload":
            create(service, alice, "shared-key-001", SECOND)
        else:
            service.mutate(alice, "shared-key-001", "cancel", booking_id=booking["id"])
    assert error.value.code == "idempotency_conflict"


def test_keys_are_scoped_to_user_and_normalized_to_utc(service, alice, bob):
    original = create(service, alice)
    result, replayed = service.mutate(
        alice, "create-0001", "create", "consultation", FIRST.astimezone(UTC)
    )
    assert replayed and result == original
    assert create(service, bob, slot=SECOND)["id"] != original["id"]


def test_real_sqlite_concurrency_one_winner(service, clock):
    users = [service.authenticate(service.issue_session()) for _ in range(8)]
    barrier = threading.Barrier(8)

    def attempt(user):
        independent = BookingService(Database(service.db.path), clock)
        barrier.wait(timeout=10)
        try:
            independent.mutate(user, "parallel-0001", "create", "consultation", FIRST)
            return "created"
        except BookingError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, users))
    assert results.count("created") == 1
    assert results.count("slot_taken") == 7
    with service.db.read() as connection:
        assert connection.execute("SELECT count(*) FROM bookings").fetchone()[0] == 1


def test_concurrent_retry_has_one_booking_and_one_original_response(service, alice, clock):
    barrier = threading.Barrier(8)

    def attempt(_):
        independent = BookingService(Database(service.db.path), clock)
        barrier.wait(timeout=10)
        return independent.mutate(alice, "parallel-retry", "create", "consultation", FIRST)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert len({result[0]["id"] for result in results}) == 1
    assert sum(replay for _, replay in results) == 7


def test_cancel_twice_releases_slot(service, alice, bob):
    booking = create(service, alice)
    for key in ("cancel-0001", "cancel-0002"):
        assert (
            service.mutate(alice, key, "cancel", booking_id=booking["id"])[0]["status"]
            == "cancelled"
        )
    assert service.slots(FIRST.date())[0]["available"]
    assert create(service, bob)["status"] == "confirmed"
    with service.db.read() as connection:
        assert (
            connection.execute("SELECT count(*) FROM events WHERE kind='cancelled'").fetchone()[0]
            == 1
        )


def test_reschedule_is_atomic_and_preserves_id(service, alice, bob):
    old = create(service, alice)
    create(service, bob, slot=SECOND)
    with pytest.raises(BookingError) as error:
        service.mutate(
            alice, "move-failed-01", "reschedule", starts_at=SECOND, booking_id=old["id"]
        )
    assert error.value.code == "slot_taken"
    assert service.get(alice, old["id"]) == old
    new = service.mutate(
        alice,
        "move-success-01",
        "reschedule",
        starts_at=SECOND + timedelta(hours=1),
        booking_id=old["id"],
    )[0]
    assert new["id"] == old["id"]
    assert new["starts_at"] != old["starts_at"]
    assert service.slots(FIRST.date())[0]["available"]
    assert create(service, bob, "released-slot-01")["status"] == "confirmed"


def test_cancelled_booking_cannot_be_moved(service, alice):
    booking = create(service, alice)
    service.mutate(alice, "cancel-0001", "cancel", booking_id=booking["id"])
    with pytest.raises(BookingError) as error:
        service.mutate(
            alice, "move-cancelled", "reschedule", starts_at=SECOND, booking_id=booking["id"]
        )
    assert error.value.code == "cancelled_booking"


@pytest.mark.parametrize("action", ["get", "cancel", "reschedule"])
def test_other_user_cannot_access_booking(service, alice, bob, action):
    booking = create(service, alice)
    with pytest.raises(BookingError) as error:
        if action == "get":
            service.get(bob, booking["id"])
        else:
            service.mutate(
                bob,
                "intruder-0001",
                action,
                starts_at=SECOND if action == "reschedule" else None,
                booking_id=booking["id"],
            )
    assert error.value.status == 404
    assert service.list_bookings(bob) == []
    assert service.get(alice, booking["id"]) == booking


def test_database_constraints_hold_without_service_layer(service, alice, bob):
    booking = create(service, alice)
    with pytest.raises(sqlite3.IntegrityError), service.db.transaction() as connection:
        connection.execute(
            "INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, NULL)",
            ("direct-write", bob.id, "consultation", int(FIRST.timestamp()), "confirmed", 1),
        )
    for sql in (
        "UPDATE bookings SET status='impossible' WHERE id=?",
        "UPDATE bookings SET status='cancelled' WHERE id=?",
        "UPDATE bookings SET cancelled_at=123 WHERE id=?",
        "UPDATE bookings SET starts_at=starts_at+60 WHERE id=?",
        "UPDATE bookings SET service_id='missing' WHERE id=?",
    ):
        with pytest.raises(sqlite3.IntegrityError), service.db.transaction() as connection:
            connection.execute(sql, (booking["id"],))


def test_data_survives_service_and_database_restart(service, alice, clock):
    booking = create(service, alice)
    fresh_db = Database(service.db.path)
    fresh_db.initialize()
    restarted = BookingService(fresh_db, clock)
    assert restarted.get(alice, booking["id"]) == booking
    assert not restarted.slots(FIRST.date())[0]["available"]


def test_moscow_day_boundary_and_end_of_day(service, clock):
    clock.value = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)  # 16 октября, 00:30 МСК
    with pytest.raises(BookingError):
        service.slots(date(2026, 10, 15))
    assert service.slots(date(2026, 10, 16))[0]["local_time"] == "10:00"
    clock.value = datetime(2026, 10, 16, 13, 59, tzinfo=UTC)
    assert [s["local_time"] for s in service.slots(date(2026, 10, 16))] == ["17:00"]
    clock.value = datetime(2026, 10, 16, 14, tzinfo=UTC)
    assert service.slots(date(2026, 10, 16)) == []


def test_cannot_modify_booking_after_start(service, alice, clock):
    booking = create(service, alice)
    clock.value = FIRST
    for action in ("cancel", "reschedule"):
        with pytest.raises(BookingError) as error:
            service.mutate(
                alice, "late-" + action, action, starts_at=SECOND, booking_id=booking["id"]
            )
        assert error.value.code == "past_booking"


@pytest.mark.parametrize("key", ["", "short", "with spaces", "кириллица", "x" * 129])
def test_bad_idempotency_key(service, alice, key):
    with pytest.raises(BookingError) as error:
        create(service, alice, key)
    assert error.value.code == "invalid_key"


def test_expired_or_revoked_sessions(service, clock):
    token = service.issue_session()
    assert service.authenticate(token).role == "client"
    service.revoke(token)
    with pytest.raises(BookingError):
        service.authenticate(token)
    token = service.issue_session("admin")
    clock.value += timedelta(days=1)
    with pytest.raises(BookingError):
        service.authenticate(token)
