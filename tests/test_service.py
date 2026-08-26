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


def test_claim_unclaim_and_multiple_contributors():
    s = service(); t = trip(s); item = s.add_item(t, "چادر", 1)
    assert s.toggle_contribution(t, item, 1, "علی").claimed
    change = s.toggle_contribution(t, item, 2, "سارا")
    assert change.duplicate_names == ("علی",)
    assert s.progress(t) == (1, 1)
    assert not s.toggle_contribution(t, item, 1, "علی").claimed
    assert s.progress(t) == (1, 1)
    assert not s.toggle_contribution(t, item, 2, "سارا").claimed
    assert s.progress(t) == (0, 1)


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
    t = s.db.connection.execute("INSERT INTO trips(chat_id,name,departure_at,timezone,status,created_by,created_at) VALUES(?,?,?,?,?,?,datetime('now'))", (-100, "سفر شمال", (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), "Asia/Tehran", "packing", 1)).lastrowid
    s.db.connection.commit()
    s.refresh_lock(t)
    assert s.trip_for_chat(-100)["status"] == "locked"
    try:
        s.add_item(t, "چادر", 1)
        assert False
    except Locked:
        pass


def test_concurrent_claims_preserve_both_users():
    db = Database(":memory:"); s = TripService(db); t = trip(s); item = s.add_item(t, "میز", 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda user: s.toggle_contribution(t, item, user, f"کاربر {user}"), (1, 2)))
    assert db.connection.execute("SELECT COUNT(*) FROM contributions WHERE item_id=?", (item,)).fetchone()[0] == 2


def test_activity_records_actor_and_item_snapshots():
    s = service(); t = s.create_trip(-101, "سفر شمال", (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), "Asia/Tehran", 1, "آرش")
    item = s.add_item(t, "قلیان", 2, "حسین")
    s.toggle_contribution(t, item, 3, "علی", "علی")
    s.toggle_contribution(t, item, 3, "علی", "علی")
    s.delete_item(t, item, 4, "رضا")
    events = s.activities(t)
    assert [(event["action"], event["actor_name"], event["item_name"]) for event in events[:4]] == [("item_deleted", "رضا", "قلیان"), ("item_unchecked", "علی", "قلیان"), ("item_checked", "علی", "قلیان"), ("item_added", "حسین", "قلیان")]
    assert events[-1]["actor_name"] == "آرش"
    lines = [render_activity_line("سفر شمال", event) for event in events]
    assert "🗑️ سفر شمال: رضا آیتم «قلیان» را حذف کرد." in lines
    assert "✨ آرش سفر «سفر شمال» را ساخت." in lines
    assert all("مورد را" not in line for line in lines)


def test_batch_add_items_creates_one_item_per_line_and_activity():
    s = service(); t = trip(s)
    item_ids = s.add_items(t, ["  چادر  ", "", "چراغ قوه", "کیسه خواب\n"], 2, "سارا")
    rows = s.db.connection.execute("SELECT name, position FROM items WHERE trip_id=? ORDER BY position", (t,)).fetchall()
    assert item_ids and [row["name"] for row in rows] == ["چادر", "چراغ قوه", "کیسه خواب"]
    events = s.activities(t)
    assert [event["item_name"] for event in events[:3]] == ["کیسه خواب", "چراغ قوه", "چادر"]
    assert all(event["actor_name"] == "سارا" for event in events[:3])


def test_batch_delete_items_records_each_item_and_is_atomic():
    s = service(); t = trip(s)
    item_ids = [s.add_item(t, name, 1) for name in ("چادر", "چراغ", "کوله")]
    assert s.delete_items(t, item_ids[:2], 2, "سارا") == 2
    assert s.progress(t) == (0, 1)
    assert [event["item_name"] for event in s.activities(t)[:2]] == ["چراغ", "چادر"]
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
