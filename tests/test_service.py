from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from packtogether.db import Database
from packtogether.service import Locked, NotFound, TripError, TripService
from packtogether.ui import render_activity_line


def service():
    return TripService(Database(":memory:"))


def trip(s, departure=None):
    departure = departure or (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    return s.create_trip(-100, "سفر شمال", departure, "Asia/Tehran", 1)


def test_toggle_check_uncheck_and_progress():
    s = service(); t = trip(s); item = s.add_item(t, "چادر", 1)
    first = s.toggle_contribution(t, item, 1, "علی")
    second = s.toggle_contribution(t, item, 2, "سارا")
    third = s.toggle_contribution(t, item, 1, "علی")
    assert first.claimed
    assert second.duplicate_names == ("علی",)
    assert not third.claimed
    rows, _, _ = s.page(t)
    row = next(item_row for item_row in rows if item_row["id"] == item)
    assert row["contributors"] == "سارا"
    assert s.progress(t) == (1, 1)


def test_delete_and_progress_pagination():
    s = service(); t = trip(s)
    ids = [s.add_item(t, f"مورد {i}", 1) for i in range(9)]
    s.toggle_contribution(t, ids[8], 1, "علی")
    assert len(s.page(t, 0)[0]) == 8
    s.delete_item(t, ids[8], 1)
    assert s.progress(t) == (0, 8)
    assert len(s.page(t, 1)[0]) == 8


def test_lock_is_persistent_and_rejects_changes():
    s = service()
    t = s.db.insert_returning_id(
        "INSERT INTO trips(chat_id,name,departure_at,status,created_by,created_at) VALUES(?,?,?,?,?,?) RETURNING id",
        (-100, "سفر شمال", (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), "packing", 1, datetime.now(timezone.utc).isoformat()),
    )
    s.refresh_lock(t)
    assert s.trip_for_chat(-100)["status"] == "locked"
    with pytest.raises(Locked):
        s.add_item(t, "چادر", 1)


def test_concurrent_claims_do_not_corrupt_item_state():
    db = Database(":memory:"); s = TripService(db); t = trip(s); item = s.add_item(t, "میز", 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda user: s.toggle_contribution(t, item, user, f"کاربر {user}"), (1, 2)))
    row = db.fetchone("SELECT status FROM items WHERE id=?", (item,))
    assert row["status"] in {"checked", "unchecked"}


def test_activity_records_type_actor_and_plain_desc():
    s = service(); t = s.create_trip(-101, "سفر شمال", (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), "Asia/Tehran", 1, "آرش")
    item = s.add_item(t, "قلیان", 2, "حسین")
    s.toggle_contribution(t, item, 3, "علی", "علی")
    s.toggle_contribution(t, item, 3, "علی", "علی")
    s.delete_item(t, item, 4, "رضا")
    events = s.activities(t)
    assert [(event["action"], event["actor_name"]) for event in events[:4]] == [
        ("item_deleted", "رضا"),
        ("item_unchecked", "علی"),
        ("item_checked", "علی"),
        ("item_added", "حسین"),
    ]
    assert events[0]["item_name"] == "قلیان"
    assert "حذف" in events[0]["desc"]
    lines = [render_activity_line("سفر شمال", event) for event in events]
    assert any("✨ آرش سفر «سفر شمال» را ساخت." in line for line in lines)
    assert any("قلیان" in line for line in lines)


def test_batch_add_items_creates_one_item_per_line_and_activity():
    s = service(); t = trip(s)
    item_ids = s.add_items(t, ["  چادر  ", "", "چراغ قوه", "کیسه خواب\n"], 2, "سارا")
    rows = s.db.fetchall("SELECT name FROM items WHERE trip_id=? AND status <> 'deleted' ORDER BY id", (t,))
    assert item_ids and [row["name"] for row in rows] == ["چادر", "چراغ قوه", "کیسه خواب"]
    events = s.activities(t)
    assert [event["action"] for event in events[:3]] == ["item_added", "item_added", "item_added"]
    assert all(event["actor_name"] == "سارا" for event in events[:3])


def test_batch_delete_items_records_each_item_and_is_atomic():
    s = service(); t = trip(s)
    item_ids = [s.add_item(t, name, 1) for name in ("چادر", "چراغ", "کوله")]
    assert s.delete_items(t, item_ids[:2], 2, "سارا") == 2
    assert s.progress(t) == (0, 1)
    assert [event["action"] for event in s.activities(t)[:2]] == ["item_deleted", "item_deleted"]
    with pytest.raises(NotFound):
        s.delete_items(t, [item_ids[2], 999999], 2, "سارا")
    assert s.progress(t) == (0, 1)


def test_start_trip_locks_immediately_and_records_actor():
    s = service(); t = trip(s)
    s.start_trip(t, 7, "سارا")
    assert s.trip_for_chat(-100)["status"] == "locked"
    event = s.activities(t)[0]
    assert (event["action"], event["actor_name"]) == ("trip_locked", "سارا")
    with pytest.raises(Locked):
        s.add_item(t, "چادر", 1)


def test_trip_status_history_lists_each_trip_with_status_label():
    s = service(); now = datetime.now(timezone.utc)
    t1 = s.db.insert_returning_id(
        "INSERT INTO trips(chat_id,name,departure_at,status,created_by,created_at) VALUES(?,?,?,?,?,?) RETURNING id",
        (-200, "سفر شمال", (now + timedelta(days=1)).isoformat(), "locked", 1, now.isoformat()),
    )
    t2 = s.db.insert_returning_id(
        "INSERT INTO trips(chat_id,name,departure_at,status,created_by,created_at) VALUES(?,?,?,?,?,?) RETURNING id",
        (-200, "سفر تبریز", (now + timedelta(days=2)).isoformat(), "packing", 1, now.isoformat()),
    )
    history = s.trip_status_history(-200)
    assert [line for line, _ in history] == ["سفر تبریز", "سفر شمال"]
    assert [status for _, status in history] == ["هنوز شروع نشده", "شروع و قفل شده"]


def test_group_allows_up_to_three_active_trips():
    s = service(); now = datetime.now(timezone.utc)
    for index in range(3):
        trip_id = s.create_trip(-202, f"سفر {index + 1}", (now + timedelta(days=index + 1)).isoformat(), "Asia/Tehran", 1)
        assert trip_id > 0
    with pytest.raises(TripError):
        s.create_trip(-202, "سفر 4", (now + timedelta(days=10)).isoformat(), "Asia/Tehran", 1)


def test_update_trip_changes_name_and_departure_time():
    s = service(); now = datetime.now(timezone.utc)
    trip_id = s.create_trip(-203, "سفر شمال", (now + timedelta(days=2)).isoformat(), "Asia/Tehran", 1)
    updated = s.update_trip(trip_id, "سفر تبریز", (now + timedelta(days=5)).isoformat())
    assert updated == trip_id
    trip = s.trip_by_id(trip_id)
    assert trip["name"] == "سفر تبریز"
    assert trip["departure_at"] == (now + timedelta(days=5)).isoformat()


def test_delete_trip_does_not_fail_when_recording_activity():
    s = service(); t = trip(s)
    s.delete_trip(t, 1, "آرش")
    with pytest.raises(NotFound):
        s.trip_by_id(t)


def test_update_trip_records_trip_updated_action_type():
    s = service(); now = datetime.now(timezone.utc)
    trip_id = s.create_trip(-300, "سفر قدیم", (now + timedelta(days=2)).isoformat(), "Asia/Tehran", 1, "آرش")
    s.update_trip(trip_id, "سفر جدید", (now + timedelta(days=3)).isoformat(), "مریم")

    rows = s.db.fetchall('SELECT type, "desc" FROM actions_history WHERE trip_id=? ORDER BY id', (trip_id,))
    assert [row["type"] for row in rows] == ["trip_created", "trip_updated"]
    assert "ویرایش" in rows[1]["desc"]


def test_delete_trip_records_trip_deleted_action_type(monkeypatch):
    s = service(); t = trip(s)
    captured = []
    original = TripService._record_activity

    def capture(cx, trip_id, action, desc, actor_name, item_id=None, *, user_id=0):
        captured.append(action)
        return original(cx, trip_id, action, desc, actor_name, item_id=item_id, user_id=user_id)

    monkeypatch.setattr(TripService, "_record_activity", staticmethod(capture))
    s.delete_trip(t, 1, "آرش")
    assert captured[-1] == "trip_deleted"


def test_trips_for_chat_orders_newest_first_within_status_bucket():
    s = service(); now = datetime.now(timezone.utc)
    older = s.create_trip(-400, "قدیمی", (now + timedelta(days=5)).isoformat(), "Asia/Tehran", 1)
    newer = s.create_trip(-400, "جدید", (now + timedelta(days=1)).isoformat(), "Asia/Tehran", 1)
    rows = s.trips_for_chat(-400)
    assert [row["id"] for row in rows[:2]] == [newer, older]
