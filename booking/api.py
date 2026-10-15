import json
import logging
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware

from booking.config import HORIZON_DAYS, MOSCOW, Settings, utc_now
from booking.database import Database
from booking.service import BookingError, BookingService, JsonObject, Principal

logger = logging.getLogger("booking")
STATIC = Path(__file__).parent / "static"
CSRF_HEADER = "booking-assistant"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBooking(StrictModel):
    service_id: str = Field(min_length=1, max_length=64)
    starts_at: datetime


class RescheduleBooking(StrictModel):
    starts_at: datetime


class AdminLogin(StrictModel):
    access_key: str = Field(min_length=20, max_length=256)


def create_app(settings: Settings | None = None, now: Callable[[], datetime] = utc_now) -> FastAPI:
    settings = settings or Settings.from_environment()
    db = Database(settings.db_path)
    service = BookingService(db, now)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db.initialize()
        yield
        # Соединения живут внутри операций и закрываются до этой точки.

    app = FastAPI(
        title="Booking Assistant API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.service = service
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"]
    )

    @app.middleware("http")
    async def request_boundary(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = str(uuid.uuid4())
        start = time.monotonic()
        response: Response
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and request.headers.get("x-requested-with") != CSRF_HEADER
        ):
            response = JSONResponse(
                {
                    "error": {
                        "code": "csrf_rejected",
                        "message": "Запрос должен отправляться из приложения.",
                    }
                },
                status_code=403,
            )
        else:
            response = await call_next(request)
        response.headers.update(
            {
                "X-Request-ID": request_id,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        logger.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "route": getattr(route, "name", "unmatched"),
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - start) * 1000),
                },
                ensure_ascii=False,
            )
        )
        return response

    @app.exception_handler(BookingError)
    async def domain_error(request: Request, error: BookingError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}}, status_code=error.status
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        # Не отражаем тело запроса, ключ входа или произвольный пользовательский текст.
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_input",
                    "message": "Проверьте обязательные поля и формат даты.",
                }
            },
            status_code=422,
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(request: Request, error: sqlite3.OperationalError) -> JSONResponse:
        logger.error(json.dumps({"event": "database_unavailable"}))
        return JSONResponse(
            {
                "error": {
                    "code": "database_unavailable",
                    "message": "База временно недоступна. Повторите запрос.",
                }
            },
            status_code=503,
            headers={"Retry-After": "1"},
        )

    def cookie(response: Response, name: str, token: str, max_age: int) -> None:
        response.set_cookie(
            name,
            token,
            max_age=max_age,
            httponly=True,
            samesite="strict",
            secure=settings.secure_cookies,
            path="/api",
        )

    def request_token(request: Request, name: str) -> str | None:
        authorization = request.headers.get("authorization", "")
        if authorization:
            scheme, _, value = authorization.partition(" ")
            return value if scheme.lower() == "bearer" else None
        return request.cookies.get(name)

    def client(request: Request) -> Principal:
        return service.authenticate(request_token(request, "booking_session"))

    def admin(request: Request) -> Principal:
        principal = service.authenticate(request_token(request, "booking_admin"))
        service.require_admin(principal)
        return principal

    Client = Annotated[Principal, Depends(client)]
    Admin = Annotated[Principal, Depends(admin)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/admin", include_in_schema=False)
    def admin_page() -> FileResponse:
        return FileResponse(STATIC / "admin.html")

    @app.get("/api/health")
    def health() -> JsonObject:
        with db.read() as connection:
            connection.execute("SELECT 1").fetchone()
        return {"status": "ok", "database": "sqlite", "timezone": "Europe/Moscow"}

    @app.get("/api/config")
    def config() -> JsonObject:
        today = now().astimezone(MOSCOW).date()
        return {
            "timezone": "Europe/Moscow",
            "today": today.isoformat(),
            "last_day": (today + timedelta(days=HORIZON_DAYS)).isoformat(),
            "demo": True,
        }

    @app.post("/api/session")
    def session(request: Request, response: Response) -> JsonObject:
        existing = request.cookies.get("booking_session")
        try:
            principal = service.authenticate(existing)
        except BookingError:
            token = service.issue_session()
            cookie(response, "booking_session", token, 30 * 86400)
            principal = service.authenticate(token)
        return {"role": principal.role}

    @app.get("/api/services")
    def services() -> list[JsonObject]:
        return service.services()

    @app.get("/api/slots")
    def slots(day: Annotated[date, Query()]) -> list[JsonObject]:
        return service.slots(day)

    @app.get("/api/bookings")
    def my_bookings(principal: Client) -> list[JsonObject]:
        return service.list_bookings(principal)

    @app.get("/api/bookings/{booking_id}")
    def booking(booking_id: str, principal: Client) -> JsonObject:
        return service.get(principal, booking_id)

    def result_response(result: tuple[JsonObject, bool], created: bool = False) -> JSONResponse:
        payload, replayed = result
        return JSONResponse(
            payload,
            status_code=201 if created and not replayed else 200,
            headers={"Idempotency-Replayed": str(replayed).lower()},
        )

    @app.post("/api/bookings", status_code=201)
    def create(body: CreateBooking, principal: Client, key: Key) -> JSONResponse:
        return result_response(
            service.mutate(principal, key, "create", body.service_id, body.starts_at), created=True
        )

    @app.post("/api/bookings/{booking_id}/cancel")
    def cancel(booking_id: str, principal: Client, key: Key) -> JSONResponse:
        return result_response(service.mutate(principal, key, "cancel", booking_id=booking_id))

    @app.post("/api/bookings/{booking_id}/reschedule")
    def reschedule(
        booking_id: str, body: RescheduleBooking, principal: Client, key: Key
    ) -> JSONResponse:
        return result_response(
            service.mutate(
                principal, key, "reschedule", starts_at=body.starts_at, booking_id=booking_id
            )
        )

    @app.post("/api/admin/login")
    def login(body: AdminLogin, response: Response) -> JsonObject:
        principal = service.authenticate(body.access_key)
        service.require_admin(principal)
        # Браузер получает отдельную короткую сессию; ключ не хранится в localStorage.
        token = service.issue_session("admin", principal.id)
        cookie(response, "booking_admin", token, 86400)
        return {"role": "admin"}

    @app.post("/api/admin/logout")
    def logout(request: Request, response: Response) -> JsonObject:
        service.revoke(request.cookies.get("booking_admin"))
        response.delete_cookie("booking_admin", path="/api")
        return {"ok": True}

    @app.get("/api/admin/bookings")
    def admin_bookings(principal: Admin, day: date | None = None) -> list[JsonObject]:
        return service.list_bookings(principal, day)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
