"""Реальная команда из README: подготовка БД, запись, остановка и перезапуск."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="booking-launch-") as temp:
        env = os.environ.copy()
        env["BOOKING_DB_PATH"] = str(Path(temp) / "launch.sqlite3")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        process = None
        with (Path(temp) / "server.log").open("w", encoding="utf-8") as log:

            def start():
                child = subprocess.Popen(
                    [sys.executable, "-m", "booking", "demo", "--port", str(port)],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=log,
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
                    ),
                )
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise RuntimeError("Команда demo завершилась до готовности сервера")
                    try:
                        if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                            return child
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)
                child.terminate()
                child.wait(timeout=5)
                raise RuntimeError("Команда demo не запустилась за 20 секунд")

            def stop(child):
                if child.poll() is None:
                    child.send_signal(
                        signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
                    )
                    child.wait(timeout=15)
                assert child.returncode == 0, "Остановка demo завершилась с ошибкой"

            try:
                process = start()
                with httpx.Client(
                    base_url=base, headers={"X-Requested-With": "booking-assistant"}
                ) as client:
                    assert client.post("/api/session").status_code == 200
                    day = datetime.now(ZoneInfo("Europe/Moscow")).date() + timedelta(days=1)
                    response = client.post(
                        "/api/bookings",
                        headers={"Idempotency-Key": "launch-check-create-001"},
                        json={"service_id": "consultation", "starts_at": f"{day}T10:00:00+03:00"},
                    )
                    assert response.status_code == 201, response.status_code
                    booking_id = response.json()["id"]
                    stop(process)
                    process = start()
                    readback = client.get("/api/bookings/" + booking_id)
                    assert readback.status_code == 200
                    assert readback.json()["status"] == "confirmed"
                    assert len(client.get("/api/bookings").json()) == 1
                stop(process)
            finally:
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
        # Лог с локальным ключом не выводится и удаляется вместе с временной папкой.
    print(
        json.dumps(
            {
                "status": "passed",
                "entrypoint": "python -m booking demo",
                "checks": ["initialize", "create", "SIGINT_shutdown", "restart", "persistence"],
                "external_services": "not_used",
            }
        )
    )


if __name__ == "__main__":
    main()
