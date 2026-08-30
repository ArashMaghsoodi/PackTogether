from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from packtogether.db import Database
from packtogether.service import Locked, NotFound, TripService
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
