import pytest

from booking.__main__ import prepare_demo, reset_demo
from booking.database import Database


def test_reset_refuses_arbitrary_path(db):
    with pytest.raises(ValueError, match="только"):
        reset_demo(db)


def test_reset_requires_demo_marker_and_preserves_services(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = Database(tmp_path / "data" / "demo.sqlite3")
    db.initialize()
    with pytest.raises(ValueError, match="не помечена"):
        reset_demo(db)
    prepare_demo(db)
    from booking.service import BookingService

    BookingService(db).issue_session()
    reset_demo(db)
    with db.read() as connection:
        assert connection.execute("SELECT count(*) FROM users").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM services").fetchone()[0] == 3
