from copy import deepcopy
from datetime import date

import pytest

from booking.service import BookingError
from booking.telegram import TelegramAdapter


def update(number, text, user=100):
    return {
        "update_id": number,
        "message": {
            "from": {"id": user, "is_bot": False},
            "chat": {"id": user, "type": "private"},
            "text": text,
        },
    }


def test_telegram_uses_same_booking_logic_and_duplicate_delivery_is_safe(service):
    adapter = TelegramAdapter(service, "test-bot")
    message = update(1, "/book consultation 2026-10-16T10:00:00+03:00")
    first = adapter.handle(message)
    assert "Запись подтверждена" in first and "10:00 МСК" in first
    assert adapter.handle(deepcopy(message)) == first
    with service.db.read() as connection:
        booking_id = connection.execute("SELECT id FROM bookings").fetchone()[0]
        assert connection.execute("SELECT count(*) FROM bookings").fetchone()[0] == 1
    assert not service.slots(date(2026, 10, 16))[0]["available"]
    assert "10:00" in adapter.handle(update(2, "/my"))
    assert "перенесена" in adapter.handle(
        update(3, f"/move {booking_id} 2026-10-16T11:00:00+03:00")
    )
    assert "отменена" in adapter.handle(update(4, f"/cancel {booking_id}"))
    assert "отменена" in adapter.handle(update(5, f"/cancel {booking_id}"))


def test_telegram_update_id_cannot_be_reused_with_different_payload_or_user(service):
    adapter = TelegramAdapter(service, "test-bot")
    adapter.handle(update(1, "/services"))
    for changed in (update(1, "/my"), update(1, "/services", user=200)):
        with pytest.raises(BookingError) as error:
            adapter.handle(changed)
        assert error.value.code == "telegram_update_conflict"


def test_telegram_user_cannot_change_someone_elses_booking(service):
    adapter = TelegramAdapter(service, "test-bot")
    adapter.handle(update(1, "/book consultation 2026-10-16T10:00:00+03:00"))
    with service.db.read() as connection:
        booking_id = connection.execute("SELECT id FROM bookings").fetchone()[0]
    assert "не найдена" in adapter.handle(update(2, f"/cancel {booking_id}", user=200))
    assert "нет записей" in adapter.handle(update(3, "/my", user=200))


def test_telegram_validation_conflict_and_commands(service):
    adapter = TelegramAdapter(service, "test-bot")
    assert "consultation" in adapter.handle(update(1, "/services"))
    assert "+03:00" in adapter.handle(update(2, "/slots 2026-10-16"))
    assert "/book" in adapter.handle(update(3, "/start"))
    assert "Проверьте дату" in adapter.handle(update(4, "/slots broken"))
    adapter.handle(update(5, "/book consultation 2026-10-16T10:00:00+03:00"))
    assert "занято" in adapter.handle(update(6, "/book session 2026-10-16T10:00:00+03:00", 200))
    assert "пояс" in adapter.handle(update(7, "/book consultation 2026-10-16T11:00:00"))


def test_telegram_rejects_group_and_spoofed_chat(service):
    adapter = TelegramAdapter(service, "test-bot")
    message = update(1, "/services")
    message["message"]["chat"]["type"] = "group"
    assert "личный чат" in adapter.handle(message)
    message = update(2, "/services")
    message["message"]["chat"]["id"] = 999
    assert "личный чат" in adapter.handle(message)
    assert "Поддерживаются" in adapter.handle({"update_id": 3})
