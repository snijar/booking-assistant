"""Настоящие Chromium + HTTP + временная SQLite. Данные проекта не затрагиваются."""

import argparse
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from booking.api import create_app  # noqa: E402
from booking.config import Settings  # noqa: E402
from booking.database import Database  # noqa: E402
from booking.service import BookingService  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path(tempfile.mkdtemp(prefix="booking-browser-evidence-"))
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    errors = []
    servers = []
    threads = []
    with tempfile.TemporaryDirectory(prefix="booking-browser-data-") as temp:
        settings = Settings(Path(temp) / "browser.sqlite3")
        db = Database(settings.db_path)
        db.initialize()
        admin_key = BookingService(db).issue_session("admin")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        def start_server():
            server = uvicorn.Server(
                uvicorn.Config(
                    create_app(settings),
                    host="127.0.0.1",
                    port=port,
                    log_level="error",
                    access_log=False,
                )
            )
            thread = threading.Thread(target=server.run, daemon=True)
            servers.append(server)
            threads.append(thread)
            thread.start()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                        return server, thread
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            raise RuntimeError("Тестовый сервер не запустился за 20 секунд")

        def stop_server(server, thread):
            server.should_exit = True
            thread.join(timeout=15)
            assert not thread.is_alive(), "Сервер не завершился корректно"

        try:
            server, thread = start_server()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1000},
                    timezone_id="America/New_York",
                    locale="ru-RU",
                )
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: (
                        errors.append(message.text)
                        if message.type == "error" and "401" not in message.text
                        else None
                    ),
                )
                page.goto(base)
                expect(page.get_by_role("heading", name="Запись на удобное время")).to_be_visible()
                expect(page.locator("#slots")).to_have_attribute("aria-busy", "false")
                assert "Booking Assistant" in page.title()
                expect(page.get_by_text("Пока нет записей.", exact=False)).to_be_visible()
                checks.append("page_identity_and_empty_state")
                for width in (390, 768, 1440):
                    page.set_viewport_size({"width": width, "height": 1000})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                        width
                    )
                    page.screenshot(path=str(output / f"booking-{width}.png"), full_page=True)
                    assert page.get_by_role("button", name="Подтвердить запись").is_visible()
                checks.append("responsive_390_768_1440")

                page.get_by_role("button", name="10:00 МСК", exact=True).click()
                page.get_by_role("button", name="Подтвердить запись").click()
                expect(page.locator("#notice")).to_contain_text("Запись подтверждена")
                expect(page.locator(".booking")).to_have_count(1)
                booking_id = page.locator(".booking").get_attribute("data-id")
                assert booking_id
                expect(page.get_by_role("button", name="10:00 МСК — занято")).to_be_disabled()
                assert "10:00" in page.locator(".booking").inner_text()
                page.screenshot(path=str(output / "booking-confirmed.png"), full_page=True)
                checks.append("create_and_moscow_time_from_new_york_browser")

                outsider = browser.new_context()
                other = outsider.new_page()
                other.goto(base)
                expect(other.locator("#bookings")).to_have_attribute("aria-busy", "false")
                expect(other.locator(".booking")).to_have_count(0)
                expect(other.get_by_role("button", name="10:00 МСК — занято")).to_be_disabled()
                foreign = outsider.request.get(base + "/api/bookings/" + booking_id)
                assert foreign.status == 404
                checks.append("separate_sessions_and_foreign_id_404")

                page.get_by_role("button", name="Перенести", exact=True).click()
                expect(page.locator("#move-dialog")).to_be_visible()
                page.locator("#move-dialog").get_by_role(
                    "button", name="11:00 МСК", exact=True
                ).click()
                page.get_by_role("button", name="Сохранить перенос").click()
                expect(page.locator("#notice")).to_contain_text("Запись перенесена")
                expect(page.locator("#move-dialog")).not_to_be_visible()
                expect(page.locator(".booking")).to_contain_text("11:00")
                assert page.locator(".booking").get_attribute("data-id") == booking_id
                expect(page.get_by_role("button", name="10:00 МСК", exact=True)).to_be_enabled()
                checks.append("reschedule_preserves_id_and_releases_old_slot")

                # Два независимых браузерных контекста выбирают один и тот же ещё свободный слот.
                page.get_by_role("button", name="12:00 МСК", exact=True).click()
                other.get_by_role("button", name="12:00 МСК", exact=True).click()
                other.get_by_role("button", name="Подтвердить запись").click()
                expect(other.locator(".booking")).to_have_count(1)
                page.get_by_role("button", name="Подтвердить запись").click()
                expect(page.locator("#notice")).to_contain_text("уже занято")
                expect(page.locator(".booking")).to_have_count(1)
                checks.append("real_conflict_between_two_browser_sessions")

                # Администратор видит запись, а неверный ключ даёт понятную ошибку.
                admin = context.new_page()
                admin.goto(base + "/admin")
                admin.get_by_label("Ключ доступа").fill("invalid-local-test-key-0000")
                admin.get_by_role("button", name="Войти", exact=True).click()
                expect(admin.locator("#notice")).to_contain_text("ключ неверен")
                admin.get_by_label("Ключ доступа").fill(admin_key)
                admin.get_by_role("button", name="Войти", exact=True).click()
                expect(admin.locator("#admin-panel")).to_be_visible()
                expect(admin.locator("#admin-rows tr")).to_have_count(2)
                for width in (390, 768, 1440):
                    admin.set_viewport_size({"width": width, "height": 1000})
                    assert admin.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                        width
                    )
                    admin.screenshot(path=str(output / f"admin-{width}.png"), full_page=True)
                admin.get_by_label("Дата", exact=True).fill("2000-01-01")
                expect(admin.locator("#empty-admin")).to_be_visible()
                admin.get_by_role("button", name="Все даты").click()
                expect(admin.locator("#admin-rows tr")).to_have_count(2)
                checks.append("admin_authentication_filter_and_responsive_table")

                page.get_by_role("button", name="Отменить", exact=True).click()
                page.get_by_role("button", name="Да, отменить", exact=True).click()
                expect(page.locator("#notice")).to_contain_text("Запись отменена")
                expect(page.locator(".booking .status")).to_have_text("Отменена")
                expect(page.get_by_role("button", name="11:00 МСК", exact=True)).to_be_enabled()
                checks.append("cancel_and_release_slot")

                stop_server(server, thread)
                server, thread = start_server()
                page.reload()
                expect(page.locator(".booking .status")).to_have_text("Отменена")
                checks.append("graceful_stop_restart_and_session_persistence")

                admin.get_by_role("button", name="Выйти", exact=True).click()
                expect(admin.locator("#login-panel")).to_be_visible()
                assert context.request.get(base + "/api/admin/bookings").status == 401
                checks.append("admin_logout_revokes_cookie")

                # Ошибка сервера показана как рабочее состояние, а не бесконечная загрузка.
                page.route(
                    "**/api/slots?*",
                    lambda route: route.fulfill(
                        status=503,
                        content_type="application/json",
                        body=json.dumps({"error": {"message": "База временно недоступна."}}),
                    ),
                )
                page.get_by_role("button", name="Обновить", exact=True).click()
                expect(page.locator("#slots")).to_contain_text("База временно недоступна")
                page.unroute("**/api/slots?*")
                page.get_by_role("button", name="Обновить", exact=True).click()
                expect(page.locator("#slots")).to_have_attribute("aria-busy", "false")
                checks.append("rendered_error_and_recovery_with_simulated_503")
                page.locator("#booking-date").focus()
                assert (
                    page.locator("#booking-date").evaluate("e => getComputedStyle(e).outlineStyle")
                    != "none"
                )
                checks.append("keyboard_focus")
                # Проверка ожидаемых HTTP 401 / 503 выполняется отдельно; JS-ошибок быть не должно.
                errors = [e for e in errors if not any(code in e for code in ("503", "401", "409"))]
                assert not errors, errors
                checks.append("no_javascript_errors")
                browser.close()
        finally:
            for server, thread in zip(servers, threads, strict=True):
                if thread.is_alive():
                    stop_server(server, thread)
    result = {
        "status": "passed",
        "checks": checks,
        "javascript_errors": errors,
        "viewports": [390, 768, 1440],
        "screenshots": str(output),
        "database": "isolated real SQLite",
        "external_telegram": "not_run",
    }
    (output / "browser-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
