"""Адаптер Bot API. Извне вызывается только доверенным polling-процессом, не HTTP API."""

import hashlib
from datetime import date, datetime
from typing import Any

from booking.config import MOSCOW
from booking.service import BookingError, BookingService, JsonObject, Principal, fingerprint


class TelegramAdapter:
    def __init__(self, service: BookingService, bot_id: str):
        self.service, self.bot_id = service, bot_id

    def handle(self, update: JsonObject) -> str:
        update_id = update.get("update_id")
        message = update.get("message")
        if not isinstance(update_id, int) or not isinstance(message, dict):
            return "Поддерживаются текстовые сообщения в личном чате."
        sender = message.get("from", {})
        chat = message.get("chat", {})
        text = message.get("text")
        if (
            not isinstance(sender, dict)
            or not isinstance(chat, dict)
            or chat.get("type") != "private"
            or sender.get("id") != chat.get("id")
            or not isinstance(sender.get("id"), int)
            or sender.get("is_bot")
            or not isinstance(text, str)
            or len(text) > 512
        ):
            return "Откройте личный чат с ботом и отправьте текстовую команду."
        user_id = "tg:" + hashlib.sha256(f"{self.bot_id}:{sender['id']}".encode()).hexdigest()
        principal = Principal(user_id)
        # Храним семантическое содержимое, без имён и текста посторонних полей Telegram.
        digest = fingerprint({"bot_id": self.bot_id, "user_id": user_id, "text": text})
        with self.service.db.transaction() as connection:
            previous = connection.execute(
                "SELECT * FROM telegram_updates WHERE update_id=?", (update_id,)
            ).fetchone()
            if previous:
                if previous["user_id"] != user_id or previous["fingerprint"] != digest:
                    raise BookingError("telegram_update_conflict", "Update уже использован иначе.")
                return str(previous["response"])
            connection.execute("INSERT OR IGNORE INTO users VALUES (?, 'client')", (user_id,))
            parts = text.split()
            command = parts[0].split("@")[0] if parts else ""
            key = f"telegram:{self.bot_id}:{update_id}"
            try:
                if command == "/book" and len(parts) == 3:
                    result, _ = self.service.mutate_in(
                        connection,
                        principal,
                        key,
                        "create",
                        parts[1],
                        datetime.fromisoformat(parts[2]),
                    )
                    response = "Запись подтверждена.\n" + self._format(result)
                elif command == "/cancel" and len(parts) == 2:
                    result, _ = self.service.mutate_in(
                        connection, principal, key, "cancel", booking_id=parts[1]
                    )
                    response = "Запись отменена.\n" + self._format(result)
                elif command == "/move" and len(parts) == 3:
                    result, _ = self.service.mutate_in(
                        connection,
                        principal,
                        key,
                        "reschedule",
                        starts_at=datetime.fromisoformat(parts[2]),
                        booking_id=parts[1],
                    )
                    response = "Запись перенесена.\n" + self._format(result)
                elif command == "/services":
                    response = "Услуги по 60 минут:\n" + "\n".join(
                        f"{s['id']}: {s['title']} — {s['price_rub']} ₽"
                        for s in self.service.services()
                    )
                elif command == "/slots" and len(parts) == 2:
                    slots = self.service.slots(date.fromisoformat(parts[1]))
                    response = "Свободное время (МСК):\n" + (
                        "\n".join(
                            datetime.fromisoformat(s["starts_at"]).astimezone(MOSCOW).isoformat()
                            for s in slots
                            if s["available"]
                        )
                        or "Нет свободных слотов."
                    )
                elif command == "/my":
                    bookings = self.service.list_bookings(principal)
                    response = "\n\n".join(map(self._format, bookings)) or "У вас пока нет записей."
                else:
                    response = (
                        "Демо записи. Один специалист, 60 минут, Europe/Moscow.\n"
                        "/services — услуги\n/slots ГГГГ-ММ-ДД — слоты\n"
                        "/book consultation ГГГГ-ММ-ДДT10:00:00+03:00\n/my — ваши записи\n"
                        "/cancel ID — отменить\n/move ID ГГГГ-ММ-ДДT11:00:00+03:00"
                    )
            except BookingError as error:
                response = error.message
            except ValueError:
                response = "Проверьте дату и формат команды. Время — ISO 8601 с часовым поясом."
            connection.execute(
                "INSERT INTO telegram_updates VALUES (?, ?, ?, ?)",
                (update_id, user_id, digest, response),
            )
            return response

    @staticmethod
    def _format(booking: dict[str, Any]) -> str:
        local = datetime.fromisoformat(booking["starts_at"]).astimezone(MOSCOW)
        status = "подтверждена" if booking["status"] == "confirmed" else "отменена"
        return (
            f"{booking['service_title']}, {local:%d.%m.%Y %H:%M} МСК\n"
            f"Статус: {status}\nID: {booking['id']}"
        )
