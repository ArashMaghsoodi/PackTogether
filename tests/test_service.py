from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

from packtogether.db import Database
from packtogether.service import Locked, TripService


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
    s = service(); t = trip(s, (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
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
