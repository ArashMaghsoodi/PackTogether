from datetime import datetime, timedelta, timezone
import sqlite3

from packtogether.db import Database
from packtogether.service import TripService


class MirrorStub:
    def __init__(self):
        self.calls = []

    def upsert_snapshot(self, *, trips, items, actions):
        self.calls.append((trips, items, actions))
        return {"trips": len(trips), "items": len(items), "actions": len(actions)}


def service():
    return TripService(Database(":memory:"))


def trip(s, departure=None):
    departure = departure or (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    return s.create_trip(-100, "سفر شمال", departure, "Asia/Tehran", 1)


def test_local_mutations_mark_db_dirty():
    s = service()
    t = trip(s)
    s.add_item(t, "چادر", 1, "آرش")
    state = s.db.get_sync_state()
    assert int(state["dirty"]) == 1
    assert int(state["unsynced_actions"]) >= 2


def test_sync_to_mirror_clears_dirty_state_when_stable():
    s = service()
    t = trip(s)
    s.add_item(t, "چراغ", 1, "آرش")
    mirror = MirrorStub()

    result = s.sync_to_mirror(mirror)

    assert result["synced"]
    assert result["cleared"] is True
    assert len(mirror.calls) == 1
    state = s.db.get_sync_state()
    assert int(state["dirty"]) == 0
    assert int(state["unsynced_actions"]) == 0


def test_sync_to_mirror_noop_when_clean():
    s = service()
    mirror = MirrorStub()

    result = s.sync_to_mirror(mirror)

    assert result["synced"] is False
    assert result["reason"] == "clean"
    assert mirror.calls == []


def test_sync_to_mirror_excludes_expired_actions():
    s = service()
    t = trip(s)
    s.add_item(t, "چادر", 1, "آرش")
    s.db.execute(
        'UPDATE actions_history SET expires_at=? WHERE trip_id=?',
        ("2000-01-01T00:00:00+00:00", t),
    )
    mirror = MirrorStub()
    result = s.sync_to_mirror(mirror)
    assert result["synced"]
    sent_trips, sent_items, sent_actions = mirror.calls[0]
    assert sent_trips
    assert sent_items
    assert sent_actions == []


def test_legacy_items_position_column_is_migrated(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            departure_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'packing',
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            position INTEGER NOT NULL
        );
        CREATE TABLE actions_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER,
            item_id INTEGER,
            type TEXT NOT NULL,
            "desc" TEXT NOT NULL,
            actor TEXT NOT NULL,
            "timestamp" TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        """
    )
    conn.close()

    db = Database(str(db_path))
    trip_id = db.insert_returning_id(
        "INSERT INTO trips(chat_id,name,departure_at,status,created_by,created_at) VALUES(?,?,?,?,?,?) RETURNING id",
        (-100, "test", datetime.now(timezone.utc).isoformat(), "packing", 1, datetime.now(timezone.utc).isoformat()),
    )
    item_id = db.insert_returning_id(
        "INSERT INTO items(trip_id,name,created_by,created_at,status) VALUES(?,?,?,?,?) RETURNING id",
        (trip_id, "چادر", 1, datetime.now(timezone.utc).isoformat(), "unchecked"),
    )
    assert item_id > 0
    db.close()
