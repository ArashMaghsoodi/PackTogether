import pytest
from concurrent.futures import ThreadPoolExecutor

from packtogether.bot import setup_input
from packtogether.date_utils import format_departure, jalali_to_utc, parse_jalali_date, parse_time, validate_departure_date
from packtogether.db import Database
from packtogether.service import TripService


def service(path=":memory:"):
    return TripService(Database(path))


def test_jalali_date_and_time_convert_in_tehran():
    assert parse_jalali_date("1405.06.17") == (1405, 6, 17)
    assert parse_time("14:30") == (14, 30)
    assert jalali_to_utc("1405.06.17", "14:30").isoformat() == "2026-09-08T11:00:00+00:00"
    assert format_departure("2026-09-08T11:00:00+00:00", "Asia/Tehran") == "۱۷ شهریور ۱۴۰۵، ساعت ۱۴:۳۰"


@pytest.mark.parametrize("value", ["1405.06.17", "1405.6.17", "1405.6.7", "1405.06.7", "1405.5.12"])
def test_flexible_jalali_dates_are_normalized(value):
    assert parse_jalali_date(value) == tuple(map(int, value.split(".")))


def test_date_validation_uses_tehran_today():
    now = jalali_to_utc("1405.06.04", "14:00")
    assert validate_departure_date("1405.6.4", now) == (1405, 6, 4)
    assert validate_departure_date("1405.06.17", now) == (1405, 6, 17)
    with pytest.raises(ValueError, match="تاریخ حرکت"):
        validate_departure_date("1405.05.18", now)


@pytest.mark.parametrize("value, expected", [("14:30", (14, 30)), ("14:3", (14, 3)), ("8:30", (8, 30)), ("8:3", (8, 3))])
def test_flexible_times_are_normalized(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize("value", ["1405.13.01", "1405.02.32", "abc", "2026-08-27"])
def test_invalid_jalali_dates_are_rejected(value):
    with pytest.raises(ValueError):
        parse_jalali_date(value)


@pytest.mark.parametrize("value", ["25:80", "hello", "14", "14.30"])
def test_invalid_times_are_rejected(value):
    with pytest.raises(ValueError):
        parse_time(value)


def test_setup_is_scoped_to_chat_and_initiating_user():
    app_service = service()
    assert app_service.begin_setup(-10, 1)
    assert not app_service.begin_setup(-10, 1)
    assert app_service.setup(-10, 2) is None
    assert setup_input(app_service, -10, 2, "سفر شمال") == ("", None)
    response, _ = setup_input(app_service, -10, 1, "سفر شمال")
    assert "تاریخ حرکت" in response
    assert app_service.setup(-10, 1)["state"] == "date"


def test_owner_can_complete_or_cancel_and_other_user_cannot_cancel():
    app_service = service()
    app_service.begin_setup(-10, 1)
    setup_input(app_service, -10, 1, "سفر شمال")
    setup_input(app_service, -10, 1, "1405.06.17")
    response, trip_id = setup_input(app_service, -10, 1, "14:30")
    assert trip_id is not None
    assert "آماده شد" in response
    assert app_service.setup(-10, 1) is None

    app_service.begin_setup(-20, 1)
    assert app_service.cancel_setup(-20, 2) is False
    assert app_service.setup(-20, 1) is not None
    response, trip_id = setup_input(app_service, -20, 1, "لغو")
    assert trip_id is None
    assert "لغو شد" in response
    assert app_service.setup(-20, 1) is None


def test_setup_lifecycle_in_memory():
    app_service = service()
    app_service.begin_setup(-30, 7)
    setup_input(app_service, -30, 7, "سفر کویر")
    assert app_service.setup(-30, 7)["trip_name"] == "سفر کویر"


def test_past_departure_is_rejected_without_creating_trip():
    app_service = service()
    app_service.begin_setup(-40, 1)
    setup_input(app_service, -40, 1, "سفر گذشته")
    with pytest.raises(ValueError, match="گذشته"):
        setup_input(app_service, -40, 1, "1400.01.01")
    assert app_service.setup(-40, 1) is not None
    assert app_service.active_trip(-40) is None


def test_today_date_reaches_time_stage_then_rejects_past_time():
    app_service = service()
    app_service.begin_setup(-42, 1)
    setup_input(app_service, -42, 1, "سفر امروز")
    today_now = jalali_to_utc("1405.06.04", "14:00")
    response, trip_id = setup_input(app_service, -42, 1, "1405.06.04", today_now)
    assert "ساعت حرکت" in response
    assert trip_id is None
    with pytest.raises(ValueError, match="گذشته"):
        setup_input(app_service, -42, 1, "13:00", today_now)
    assert app_service.setup(-42, 1)["state"] == "time"


def test_past_date_is_rejected_at_date_stage_and_does_not_ask_for_time():
    app_service = service()
    app_service.begin_setup(-41, 1)
    setup_input(app_service, -41, 1, "سفر گذشته")
    with pytest.raises(ValueError, match="تاریخ حرکت"):
        setup_input(app_service, -41, 1, "1405.05.18", jalali_to_utc("1405.06.04", "14:00"))
    assert app_service.setup(-41, 1)["state"] == "date"


def test_past_date_error_is_distinct_from_time_prompt():
    app_service = service()
    app_service.begin_setup(-43, 1)
    setup_input(app_service, -43, 1, "سفر گذشته")
    with pytest.raises(ValueError) as error:
        setup_input(app_service, -43, 1, "1405.05.18")
    assert "تاریخ حرکت نمی‌تواند در گذشته باشد" in str(error.value)
    assert "ساعت حرکت" not in str(error.value)


def test_historical_trip_does_not_block_new_active_trip():
    app_service = service()
    old = app_service.db.insert_returning_id(
        "INSERT INTO trips(chat_id,name,departure_at,status,created_by,created_at) VALUES(?,?,?,?,?,?) RETURNING id",
        (-50, "قدیمی", "2020-01-01T00:00:00+00:00", "locked", 1, "2020-01-01T00:00:00+00:00"),
    )
    new = app_service.create_trip(-50, "جدید", "2099-01-01T00:00:00+00:00", "Asia/Tehran", 2)
    assert new != old
    assert app_service.db.scalar("SELECT COUNT(*) FROM trips WHERE chat_id=?", (-50,)) == 2


def test_new_sqlite_schema_bootstraps_cleanly(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    app_service = service(path)
    trip_id = app_service.create_trip(-60, "کوه", "2099-01-01T00:00:00+00:00", "Asia/Tehran", 1)
    assert trip_id > 0


def test_concurrent_trip_creation_allows_only_one_active_trip():
    app_service = service()
    departure = "2099-01-01T00:00:00+00:00"

    def create(user_id):
        try:
            return app_service.create_trip(-70, f"سفر {user_id}", departure, "Asia/Tehran", user_id)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (1, 2)))
    assert sum(result is not None for result in results) == 1
    assert app_service.db.scalar("SELECT COUNT(*) FROM trips WHERE chat_id=? AND status='packing'", (-70,)) == 1
