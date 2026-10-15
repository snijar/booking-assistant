import logging
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from booking.api import create_app
from booking.config import Settings
from booking.service import BookingService

PAYLOAD = {"service_id": "consultation", "starts_at": "2026-10-16T10:00:00+03:00"}
KEY = {"Idempotency-Key": "http-create-0001"}


def test_api_complete_lifecycle_and_replay(client):
    assert client.get("/api/services").status_code == 200
    response = client.post("/api/bookings", json=PAYLOAD, headers=KEY)
    assert response.status_code == 201, response.text
    booking_id = response.json()["id"]
    replay = client.post("/api/bookings", json=PAYLOAD, headers=KEY)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()
    assert len(client.get("/api/bookings").json()) == 1
    moved = client.post(
        f"/api/bookings/{booking_id}/reschedule",
        json={"starts_at": "2026-10-16T11:00:00+03:00"},
        headers={"Idempotency-Key": "http-move-0001"},
    )
    assert moved.status_code == 200
    assert moved.json()["starts_at"] == "2026-10-16T08:00:00Z"
    for key in ("http-cancel-001", "http-cancel-002"):
        result = client.post(f"/api/bookings/{booking_id}/cancel", headers={"Idempotency-Key": key})
        assert result.status_code == 200 and result.json()["status"] == "cancelled"


def test_api_forbids_foreign_booking_and_spoofed_identity(client, db, clock):
    booking = client.post("/api/bookings", json=PAYLOAD, headers=KEY).json()
    with TestClient(
        create_app(Settings(db.path), clock), headers={"X-Requested-With": "booking-assistant"}
    ) as other:
        assert other.get("/api/bookings").status_code == 401
        other.post("/api/session")
        assert other.get("/api/bookings").json() == []
        assert other.get(f"/api/bookings/{booking['id']}").status_code == 404
        for action in ("cancel", "reschedule"):
            result = other.post(
                f"/api/bookings/{booking['id']}/{action}",
                headers={"Idempotency-Key": "attack-0001"},
                json={"starts_at": "2026-10-16T11:00:00+03:00"} if action == "reschedule" else None,
            )
            assert result.status_code == 404
        forged = other.post("/api/bookings", json={**PAYLOAD, "telegram_id": 12345}, headers=KEY)
        assert forged.status_code == 422


def test_admin_authentication_and_logout(client, db, clock):
    assert client.get("/api/admin/bookings").status_code == 401
    assert (
        client.get("/api/admin/bookings", headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )
    service = BookingService(db, clock)
    ordinary = service.issue_session()
    assert client.post("/api/admin/login", json={"access_key": ordinary}).status_code == 403
    admin_key = service.issue_session("admin")
    login = client.post("/api/admin/login", json={"access_key": admin_key})
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=strict" in login.headers["set-cookie"]
    assert client.get("/api/admin/bookings").status_code == 200
    client.post("/api/admin/logout")
    assert client.get("/api/admin/bookings").status_code == 401


def test_admin_sees_records_and_filter_uses_moscow_day(client, db, clock):
    client.post("/api/bookings", json=PAYLOAD, headers=KEY)
    key = BookingService(db, clock).issue_session("admin")
    headers = {"Authorization": "Bearer " + key}
    assert len(client.get("/api/admin/bookings?day=2026-10-16", headers=headers).json()) == 1
    assert client.get("/api/admin/bookings?day=2026-10-15", headers=headers).json() == []
    assert client.get("/api/admin/bookings?day=broken", headers=headers).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"service_id": "consultation"},
        {"service_id": "consultation", "starts_at": "bad-date"},
        {"service_id": "consultation", "starts_at": "2026-10-16T10:00:00"},
        {**PAYLOAD, "price_rub": 1},
        {**PAYLOAD, "user_id": "someone-else"},
    ],
)
def test_invalid_body_returns_safe_validation_message(client, body):
    response = client.post("/api/bookings", json=body, headers=KEY)
    assert response.status_code == 422
    assert set(response.json()) == {"error"}


def test_bad_key_and_unknown_service(client):
    assert client.post("/api/bookings", json=PAYLOAD).status_code == 422
    assert (
        client.post(
            "/api/bookings", json=PAYLOAD, headers={"Idempotency-Key": "with spaces"}
        ).status_code
        == 422
    )
    result = client.post("/api/bookings", json={**PAYLOAD, "service_id": "missing"}, headers=KEY)
    assert result.status_code == 422 and result.json()["error"]["code"] == "unknown_service"


def test_csrf_headers_cookies_and_host_protection(client):
    client.headers.pop("X-Requested-With")
    assert client.post("/api/session").status_code == 403
    assert client.post("/api/bookings", json=PAYLOAD, headers=KEY).status_code == 403
    client.headers["X-Requested-With"] = "booking-assistant"
    client.cookies.clear()
    response = client.post("/api/session")
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert "Path=/api" in response.headers["set-cookie"]
    assert client.get("/api/config", headers={"Host": "untrusted.example"}).status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_https_cookie_option(db, clock):
    app = create_app(Settings(db.path, secure_cookies=True), clock)
    with TestClient(
        app, base_url="https://localhost", headers={"X-Requested-With": "booking-assistant"}
    ) as http:
        response = http.post("/api/session")
        assert "Secure" in response.headers["set-cookie"]


def test_static_pages_health_and_openapi(client):
    assert "Запись на удобное время" in client.get("/").text
    assert "Вход администратора" in client.get("/admin").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/health").json()["status"] == "ok"
    assert "/api/bookings" in client.get("/openapi.json").json()["paths"]
    headers = client.get("/").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/bookings").headers["cache-control"] == "no-store"


def test_structured_logs_do_not_contain_keys_or_body(client, caplog):
    caplog.set_level(logging.INFO, logger="booking")
    secret_marker = "private-test-marker-for-log-check"
    client.post("/api/admin/login", json={"access_key": secret_marker})
    assert secret_marker not in caplog.text
    assert '"event": "http_request"' in caplog.text
    assert '"route": "login"' in caplog.text


def test_configuration_follows_moscow_not_host_timezone(db, clock):
    clock.value = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)
    with TestClient(create_app(Settings(db.path), clock)) as http:
        data = http.get("/api/config").json()
        assert data["today"] == "2026-10-16"
        assert data["last_day"] == "2026-11-15"
