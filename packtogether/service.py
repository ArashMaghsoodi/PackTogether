from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .db import Database, now_iso

PAGE_SIZE = 8

class TripError(Exception):
    pass

class NotFound(TripError):
    pass

class Locked(TripError):
    pass

@dataclass(frozen=True)
class Change:
    item_id: int
    claimed: bool
    duplicate_names: tuple[str, ...] = ()

class TripService:
    def __init__(self, db: Database):
        self.db = db

    def create_trip(self, chat_id: int, name: str, departure_at: str, timezone_name: str, user_id: int) -> int:
        name = name.strip()
        if not name or len(name) > 80:
            raise TripError("نام سفر باید بین ۱ تا ۸۰ نویسه باشد.")
        with self.db.transaction() as cx:
            existing = cx.execute("SELECT id FROM trips WHERE chat_id=? AND status='packing'", (chat_id,)).fetchone()
            if existing:
                raise TripError("این گروه از قبل یک سفر فعال دارد.")
            cursor = cx.execute(
                "INSERT INTO trips(chat_id,name,departure_at,timezone,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (chat_id, name, departure_at, timezone_name, user_id, now_iso()),
            )
            trip_id = cursor.lastrowid
            cx.execute("INSERT INTO activities(trip_id,user_id,action,created_at) VALUES(?,?,?,?)", (trip_id, user_id, "trip_created", now_iso()))
            return int(trip_id)

    def trip_for_chat(self, chat_id: int) -> Any:
        trip = self.db.connection.execute("SELECT * FROM trips WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        if not trip:
            raise NotFound("هنوز چک‌لیستی ساخته نشده است.")
        return trip

    def attach_message(self, trip_id: int, message_id: int) -> None:
        self.db.connection.execute("UPDATE trips SET message_id=? WHERE id=?", (message_id, trip_id))
        self.db.connection.commit()

    def _lock_if_due(self, cx, trip) -> bool:
        if trip["status"] == "packing" and datetime.fromisoformat(trip["departure_at"]) <= datetime.now(timezone.utc):
            cx.execute("UPDATE trips SET status='locked' WHERE id=? AND status='packing'", (trip["id"],))
            cx.execute("INSERT INTO activities(trip_id,user_id,action,created_at) VALUES(?,?,?,?)", (trip["id"], trip["created_by"], "trip_locked", now_iso()))
            return True
        return False

    def refresh_lock(self, trip_id: int) -> bool:
        with self.db.transaction() as cx:
            trip = cx.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
            if not trip:
                raise NotFound("سفر پیدا نشد.")
            return self._lock_if_due(cx, trip)

    def due_trips(self) -> list[Any]:
        return list(self.db.connection.execute("SELECT * FROM trips WHERE status='packing' AND departure_at <= ?", (now_iso(),)))

    def add_item(self, trip_id: int, name: str, user_id: int) -> int:
        name = name.strip()
        with self.db.transaction() as cx:
            trip = cx.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
            if not trip: raise NotFound("سفر پیدا نشد.")
            if self._lock_if_due(cx, trip) or trip["status"] == "locked": raise Locked()
            position = cx.execute("SELECT COALESCE(MAX(position), -1)+1 FROM items WHERE trip_id=?", (trip_id,)).fetchone()[0]
            item_id = cx.execute("INSERT INTO items(trip_id,name,position,created_by,created_at) VALUES(?,?,?,?,?)", (trip_id, name, position, user_id, now_iso())).lastrowid
            cx.execute("INSERT INTO activities(trip_id,user_id,action,item_id,item_name,created_at) VALUES(?,?,?,?,?,?)", (trip_id,user_id,"item_added",item_id,name,now_iso()))
            return int(item_id)

    def toggle_contribution(self, trip_id: int, item_id: int, user_id: int, display_name: str) -> Change:
        with self.db.transaction() as cx:
            trip = cx.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
            if not trip: raise NotFound("سفر پیدا نشد.")
            if self._lock_if_due(cx, trip) or trip["status"] == "locked": raise Locked()
            item = cx.execute("SELECT * FROM items WHERE id=? AND trip_id=? AND deleted_at IS NULL", (item_id, trip_id)).fetchone()
            if not item: raise NotFound("این مورد دیگر وجود ندارد.")
            existing = cx.execute("SELECT 1 FROM contributions WHERE item_id=? AND user_id=?", (item_id,user_id)).fetchone()
            if existing:
                cx.execute("DELETE FROM contributions WHERE item_id=? AND user_id=?", (item_id,user_id))
                action = "item_unclaimed"
                duplicate_names = ()
                claimed = False
            else:
                others = tuple(row[0] for row in cx.execute("SELECT display_name FROM contributions WHERE item_id=?", (item_id,)))
                cx.execute("INSERT INTO contributions VALUES(?,?,?,?)", (item_id,user_id,display_name.strip() or "هم‌گروهی",now_iso()))
                action = "item_claimed"
                duplicate_names = others
                claimed = True
            cx.execute("INSERT INTO activities(trip_id,user_id,action,item_id,item_name,created_at) VALUES(?,?,?,?,?,?)", (trip_id,user_id,action,item_id,item["name"],now_iso()))
            return Change(item_id, claimed, duplicate_names)

    def delete_item(self, trip_id: int, item_id: int, user_id: int) -> None:
        with self.db.transaction() as cx:
            trip = cx.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
            if not trip: raise NotFound("سفر پیدا نشد.")
            if self._lock_if_due(cx, trip) or trip["status"] == "locked": raise Locked()
            item = cx.execute("SELECT * FROM items WHERE id=? AND trip_id=? AND deleted_at IS NULL", (item_id, trip_id)).fetchone()
            if not item: raise NotFound("این مورد دیگر وجود ندارد.")
            cx.execute("UPDATE items SET deleted_at=? WHERE id=? AND deleted_at IS NULL", (now_iso(), item_id))
            cx.execute("INSERT INTO activities(trip_id,user_id,action,item_id,item_name,created_at) VALUES(?,?,?,?,?,?)", (trip_id,user_id,"item_deleted",item_id,item["name"],now_iso()))

    def page(self, trip_id: int, page: int = 0) -> tuple[list[Any], int, int]:
        self.refresh_lock(trip_id)
        total = self.db.connection.execute("SELECT COUNT(*) FROM items WHERE trip_id=? AND deleted_at IS NULL", (trip_id,)).fetchone()[0]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(page, 0), pages - 1)
        items = self.db.connection.execute("SELECT i.*, GROUP_CONCAT(c.display_name, '، ') AS contributors FROM items i LEFT JOIN contributions c ON c.item_id=i.id WHERE i.trip_id=? AND i.deleted_at IS NULL GROUP BY i.id ORDER BY i.position LIMIT ? OFFSET ?", (trip_id,PAGE_SIZE,page*PAGE_SIZE)).fetchall()
        return list(items), page, pages

    def progress(self, trip_id: int) -> tuple[int, int]:
        row = self.db.connection.execute("SELECT COUNT(DISTINCT i.id) total, COUNT(DISTINCT CASE WHEN c.item_id IS NOT NULL THEN i.id END) claimed FROM items i LEFT JOIN contributions c ON c.item_id=i.id WHERE i.trip_id=? AND i.deleted_at IS NULL", (trip_id,)).fetchone()
        return int(row["claimed"]), int(row["total"])

    def activities(self, trip_id: int, limit: int = 10) -> list[Any]:
        return list(self.db.connection.execute("SELECT * FROM activities WHERE trip_id=? ORDER BY id DESC LIMIT ?", (trip_id,limit)))
