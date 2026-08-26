from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    departure_at TEXT NOT NULL,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'packing' CHECK(status IN ('packing', 'locked')),
    message_id INTEGER,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id),
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS items_trip_position ON items(trip_id, deleted_at, position);
CREATE TABLE IF NOT EXISTS contributions (
    item_id INTEGER NOT NULL REFERENCES items(id),
    user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY(item_id, user_id)
);
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id),
    user_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    item_id INTEGER REFERENCES items(id),
    item_name TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activities_trip_time ON activities(trip_id, id DESC);
CREATE TABLE IF NOT EXISTS setup_sessions (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('name', 'date', 'time')),
    trip_name TEXT,
    departure_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(chat_id, user_id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._transaction_lock = threading.RLock()
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.executescript(SCHEMA)
        self._migrate_trip_constraint()
        self.connection.commit()

    def _migrate_trip_constraint(self) -> None:
        table_sql = self.connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='trips'").fetchone()[0]
        if "chat_id INTEGER NOT NULL UNIQUE" in table_sql:
            self.connection.execute("PRAGMA foreign_keys=OFF")
            self.connection.execute("CREATE TABLE trips_new (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, name TEXT NOT NULL, departure_at TEXT NOT NULL, timezone TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'packing' CHECK(status IN ('packing', 'locked')), message_id INTEGER, created_by INTEGER NOT NULL, created_at TEXT NOT NULL)")
            self.connection.execute("INSERT INTO trips_new SELECT id, chat_id, name, departure_at, timezone, status, message_id, created_by, created_at FROM trips")
            self.connection.execute("DROP TABLE trips")
            self.connection.execute("ALTER TABLE trips_new RENAME TO trips")
            self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS trips_one_active_per_chat ON trips(chat_id) WHERE status='packing'")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._transaction_lock:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection

    def close(self) -> None:
        self.connection.close()
