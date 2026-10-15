import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from booking.api import create_app
from booking.config import MOSCOW, Settings
from booking.database import Database
from booking.service import BookingService
from booking.telegram import TelegramAdapter


def prepare_demo(db: Database) -> None:
    db.initialize()
    with db.transaction() as connection:
        connection.execute("INSERT OR IGNORE INTO meta VALUES ('mode', 'demo')")


def reset_demo(db: Database) -> None:
    expected = (Path.cwd() / "data" / "demo.sqlite3").resolve()
    if db.path.resolve() != expected or not db.path.exists():
        raise ValueError("Сброс разрешён только для существующей ./data/demo.sqlite3.")
    with db.transaction() as connection:
        markers = dict(connection.execute("SELECT key,value FROM meta"))
        if markers.get("mode") != "demo" or markers.get("application") != "booking-assistant":
            raise ValueError("Отказ: база не помечена как демо Booking Assistant.")
        for table in ("telegram_updates", "events", "idempotency", "bookings", "sessions", "users"):
            connection.execute(f"DELETE FROM {table}")  # Имена из фиксированного списка.


def simulate(service: BookingService) -> None:
    adapter = TelegramAdapter(service, "local-demo")
    tomorrow = (service.now().astimezone(MOSCOW) + timedelta(days=1)).date()
    message: dict[str, Any] = {
        "from": {"id": 100, "is_bot": False},
        "chat": {"id": 100, "type": "private"},
        "text": "/services",
    }
    update_id = int(datetime.now().timestamp() * 1000)
    for offset, command in enumerate(("/services", f"/slots {tomorrow}", "/my")):
        message["text"] = command
        response = adapter.handle({"update_id": update_id + offset, "message": message})
        print(f"> {command}\n{response}\n")


def poll(service: BookingService, token: str) -> None:
    def api(client: httpx.Client, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        if not result.get("ok"):
            raise RuntimeError("Telegram отклонил запрос.")
        return result

    with httpx.Client(timeout=35) as client:
        bot_id = str(api(client, "getMe", {})["result"]["id"])
        adapter = TelegramAdapter(service, bot_id)
        cursor_key = f"telegram-offset:{bot_id}"
        with service.db.read() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key=?", (cursor_key,)).fetchone()
            offset = int(row[0]) if row else 0
        while True:
            updates = api(
                client,
                "getUpdates",
                {"offset": offset, "timeout": 25, "allowed_updates": ["message"]},
            )["result"]
            for update in updates:
                message = update.get("message", {})
                chat = message.get("chat", {})
                if chat.get("type") == "private" and not message.get("from", {}).get("is_bot"):
                    text = adapter.handle(update)
                    api(client, "sendMessage", {"chat_id": chat["id"], "text": text})
                offset = update["update_id"] + 1
                with service.db.transaction() as connection:
                    connection.execute(
                        "INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) "
                        "DO UPDATE SET value=excluded.value",
                        (cursor_key, str(offset)),
                    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Booking Assistant: локальная демонстрация")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Подготовить демо и запустить на 127.0.0.1")
    demo.add_argument("--port", type=int, default=8000)
    sub.add_parser("init", help="Создать схему БД и демонстрационные услуги")
    sub.add_parser("admin-key", help="Создать случайный ключ администратора на 24 часа")
    reset = sub.add_parser("reset-demo", help="Сбросить только помеченную демонстрационную БД")
    reset.add_argument("--confirm-demo-reset", action="store_true", required=True)
    sub.add_parser("telegram-simulate", help="Проверить адаптер без сети и токена")
    live = sub.add_parser("telegram-poll", help="Реальный polling; требует явного разрешения")
    live.add_argument("--allow-live", action="store_true", required=True)
    args = parser.parse_args()
    settings = Settings.from_environment()
    db = Database(settings.db_path)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # HTTP-клиент не должен журналировать Bot API URL, содержащий токен.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        if args.command == "reset-demo":
            reset_demo(db)
            print("Демонстрационные записи и сессии сброшены. Услуги сохранены.")
            return
        prepare_demo(db)
        service = BookingService(db)
        if args.command == "init":
            print("База подготовлена. Добавлены только демонстрационные услуги.")
        elif args.command in ("demo", "admin-key"):
            # Ключ выводится только локально. В файлы, HTTP-логи и Git он не попадает.
            print(
                "Локальный ключ администратора (24 часа):",
                service.issue_session("admin"),
                flush=True,
            )
            if args.command == "demo":
                print(f"Откройте http://127.0.0.1:{args.port} и /admin", flush=True)
                uvicorn.run(
                    create_app(settings),
                    host="127.0.0.1",
                    port=args.port,
                    access_log=False,
                    log_config=None,
                )
        elif args.command == "telegram-simulate":
            simulate(service)
        elif args.command == "telegram-poll":
            token = os.environ.get("TELEGRAM_BOT_TOKEN")
            if not token:
                parser.error("Нужна переменная TELEGRAM_BOT_TOKEN. Не передавайте ключ аргументом.")
            poll(service, token)
    except KeyboardInterrupt:
        print("Работа завершена.")
    except (httpx.HTTPError, RuntimeError, ValueError) as error:
        # HTTPError может содержать токен в URL, поэтому выводим только тип ошибки.
        print(
            json.dumps({"event": "command_failed", "type": type(error).__name__}), file=sys.stderr
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
