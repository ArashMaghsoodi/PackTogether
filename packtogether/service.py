from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any

from .db import Database, SupabaseApiDatabase, now_iso

PAGE_SIZE = 8


class TripError(Exception):
    pass


class NotFound(TripError):
    pass


class Locked(TripError):
    pass


TRIP_CREATED = "trip_created"
ITEM_ADDED = "item_added"
ITEM_CHECKED = "item_checked"
ITEM_UNCHECKED = "item_unchecked"
ITEM_DELETED = "item_deleted"
TRIP_LOCKED = "trip_locked"


@dataclass(frozen=True)
class Change:
    item_id: int
    claimed: bool
    duplicate_names: tuple[str, ...] = ()


class TripService:
    def __init__(self, db: Database):
        self.db = db
        self._setup_sessions: dict[tuple[int, int], dict[str, str | None]] = {}
        self._setup_lock = RLock()
        self._trip_message_ids: dict[int, int] = {}
        self._message_lock = RLock()

    @staticmethod
    def _to_utc_iso(value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise TripError("زمان حرکت معتبر نیست.")
        return parsed.astimezone(timezone.utc).isoformat()

    def _with_message(self, trip: dict[str, Any] | None) -> dict[str, Any] | None:
        if not trip:
            return None
        record = dict(trip)
        with self._message_lock:
            record["message_id"] = self._trip_message_ids.get(int(record["id"]))
        return record

    @staticmethod
    def _mirror_trip_payload(trip: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(trip["id"]),
            "chat_id": int(trip["chat_id"]),
            "name": str(trip["name"]),
            "departure_at": str(trip["departure_at"]),
            "status": str(trip["status"]),
            "created_by": int(trip["created_by"]),
            "created_at": str(trip["created_at"]),
        }

    @staticmethod
    def _mirror_item_payload(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(item["id"]),
            "trip_id": int(item["trip_id"]),
            "name": str(item["name"]),
            "created_by": int(item["created_by"]),
            "created_at": str(item["created_at"]),
            "status": str(item["status"]),
        }

    @staticmethod
    def _mirror_action_payload(action_row: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "id": int(action_row["id"]),
            "trip_id": int(action_row["trip_id"]),
            "type": str(action_row["type"]),
            "desc": str(action_row["desc"]),
            "actor": str(action_row["actor"]),
            "timestamp": str(action_row["timestamp"]),
            "expires_at": str(action_row["expires_at"]),
        }
        if action_row.get("item_id") is not None:
            payload["item_id"] = int(action_row["item_id"])
        return payload

    def _mark_mutation(self) -> None:
        self.db.mark_dirty_action()

    def mirror_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        trips = [self._mirror_trip_payload(row) for row in self.db.fetchall("SELECT * FROM trips")]
        items = [self._mirror_item_payload(row) for row in self.db.fetchall("SELECT * FROM items")]
        actions = [
            self._mirror_action_payload(row)
            for row in self.db.fetchall('SELECT id, trip_id, item_id, type, "desc", actor, "timestamp", expires_at FROM actions_history')
        ]
        return {"trips": trips, "items": items, "actions": actions}

    def sync_to_mirror(self, mirror: SupabaseApiDatabase) -> dict[str, Any]:
        state_before = self.db.get_sync_state()
        if not int(state_before.get("dirty") or 0):
            return {"synced": False, "reason": "clean", "counts": {"trips": 0, "items": 0, "actions": 0}, "cleared": False}

        marker = state_before.get("last_local_action_at")
        self.db.mark_sync_started()
        snapshot = self.mirror_snapshot()
        counts = mirror.upsert_snapshot(trips=snapshot["trips"], items=snapshot["items"], actions=snapshot["actions"])

        state_after = self.db.get_sync_state()
        if state_after.get("last_local_action_at") == marker:
            self.db.mark_sync_succeeded()
            cleared = True
        else:
            self.db.execute(
                "UPDATE sync_state SET last_sync_completed_at=?, last_sync_status='ok', last_error=NULL WHERE id=1",
                (now_iso(),),
            )
            cleared = False

        return {"synced": True, "reason": "ok", "counts": counts, "cleared": cleared}

    def create_trip(self, chat_id: int, name: str, departure_at: str, timezone_name: str, user_id: int, actor_display_name: str = "کاربر") -> int:
        del timezone_name
        if chat_id >= 0:
            raise TripError("سفر فقط در گروه قابل ساخت است.")
        name = name.strip()
        if not name or len(name) > 80:
            raise TripError("نام سفر باید بین ۱ تا ۸۰ نویسه باشد.")
        departure_iso = self._to_utc_iso(departure_at)
        departure = datetime.fromisoformat(departure_iso)
        if departure <= datetime.now(timezone.utc):
            raise TripError("❌ زمان حرکت نمی‌تواند در گذشته باشد.\n\nلطفاً تاریخ و ساعت آینده‌ای را وارد کنید.")
        with self.db.transaction() as cx:
            self._reconcile_chat(cx, chat_id)
            existing = cx.fetchone("SELECT id FROM trips WHERE chat_id=? AND status='packing'", (chat_id,))
            if existing:
                raise TripError("این گروه از قبل یک سفر فعال دارد.")
            try:
                trip_id = cx.insert_returning_id(
                    "INSERT INTO trips(chat_id,name,departure_at,status,created_by,created_at) VALUES(?,?,?,?,?,?) RETURNING id",
                    (chat_id, name, departure_iso, "packing", user_id, now_iso()),
                )
            except Exception as error:
                raise TripError("این گروه در حال حاضر یک سفر فعال دارد.") from error
            self._record_activity(cx, trip_id, TRIP_CREATED, f"{actor_display_name.strip() or 'کاربر'} سفر «{name}» را ساخت.", actor_display_name)
            self._mark_mutation()
            return int(trip_id)

    def _reconcile_chat(self, cx: Database, chat_id: int) -> None:
        for trip in cx.fetchall("SELECT * FROM trips WHERE chat_id=? AND status='packing'", (chat_id,)):
            self._lock_if_due(cx, trip)

    def trip_for_chat(self, chat_id: int) -> Any:
        trip = self.db.fetchone("SELECT * FROM trips WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,))
        if not trip:
            raise NotFound("هنوز چک‌لیستی ساخته نشده است.")
        return self._with_message(trip)

    def active_trip(self, chat_id: int) -> Any:
        with self.db.transaction() as cx:
            self._reconcile_chat(cx, chat_id)
            trip = cx.fetchone("SELECT * FROM trips WHERE chat_id=? AND status='packing' ORDER BY id DESC LIMIT 1", (chat_id,))
        return self._with_message(trip)

    def attach_message(self, trip_id: int, message_id: int) -> None:
        with self._message_lock:
            self._trip_message_ids[trip_id] = message_id

    def _lock_if_due(self, cx: Database, trip: dict[str, Any]) -> bool:
        if trip["status"] != "packing":
            return False
        departure = datetime.fromisoformat(str(trip["departure_at"]))
        if departure.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            return self._lock_trip(cx, trip, int(trip["created_by"]), "کاربر")
        return False

    def _lock_trip(self, cx: Database, trip: dict[str, Any], user_id: int, actor_display_name: str) -> bool:
        if trip["status"] != "packing":
            return False
        updated = cx.execute("UPDATE trips SET status='locked' WHERE id=? AND status='packing'", (int(trip["id"]),))
        if updated <= 0:
            return False
        self._record_activity(cx, int(trip["id"]), TRIP_LOCKED, f"{actor_display_name.strip() or 'کاربر'} سفر را شروع کرد و چک‌لیست را قفل کرد.", actor_display_name, user_id=user_id)
        self._mark_mutation()
        return True

    def start_trip(self, trip_id: int, user_id: int, actor_display_name: str = "کاربر") -> None:
        with self.db.transaction() as cx:
            trip = cx.fetchone("SELECT * FROM trips WHERE id=?", (trip_id,))
            if not trip:
                raise NotFound("سفر پیدا نشد.")
            if trip["status"] == "locked":
                raise Locked()
            self._lock_trip(cx, trip, user_id, actor_display_name)

    def refresh_lock(self, trip_id: int) -> bool:
        with self.db.transaction() as cx:
            trip = cx.fetchone("SELECT * FROM trips WHERE id=?", (trip_id,))
            if not trip:
                raise NotFound("سفر پیدا نشد.")
            return self._lock_if_due(cx, trip)

    def due_trips(self) -> list[Any]:
        now = datetime.now(timezone.utc)
        due = []
        for trip in self.db.fetchall("SELECT * FROM trips WHERE status='packing'"):
            departure = datetime.fromisoformat(str(trip["departure_at"])).astimezone(timezone.utc)
            if departure <= now:
                due.append(self._with_message(trip))
        return due

    def setup(self, chat_id: int, user_id: int) -> Any:
        with self._setup_lock:
            session = self._setup_sessions.get((chat_id, user_id))
            return dict(session) if session else None

    def begin_setup(self, chat_id: int, user_id: int) -> bool:
        with self._setup_lock:
            key = (chat_id, user_id)
            if key in self._setup_sessions:
                return False
            self._setup_sessions[key] = {"state": "name", "trip_name": None, "departure_date": None}
            return True

    def update_setup(self, chat_id: int, user_id: int, state: str, *, trip_name: str | None = None, departure_date: str | None = None) -> None:
        with self._setup_lock:
            key = (chat_id, user_id)
            if key not in self._setup_sessions:
                return
            self._setup_sessions[key]["state"] = state
            if trip_name is not None:
                self._setup_sessions[key]["trip_name"] = trip_name
            if departure_date is not None:
                self._setup_sessions[key]["departure_date"] = departure_date

    def cancel_setup(self, chat_id: int, user_id: int) -> bool:
        with self._setup_lock:
            return self._setup_sessions.pop((chat_id, user_id), None) is not None

    def complete_setup(self, chat_id: int, user_id: int, departure_at: str, actor_display_name: str = "کاربر") -> int:
        setup = self.setup(chat_id, user_id)
        if not setup or not setup.get("trip_name"):
            raise NotFound("جلسه ساخت سفر پیدا نشد.")
        trip_id = self.create_trip(chat_id, str(setup["trip_name"]), departure_at, "Asia/Tehran", user_id, actor_display_name)
        self.cancel_setup(chat_id, user_id)
        return trip_id

    def add_item(self, trip_id: int, name: str, user_id: int, actor_display_name: str = "کاربر") -> int:
        item_ids = self.add_items(trip_id, [name], user_id, actor_display_name)
        return item_ids[0]

    def add_items(self, trip_id: int, names: list[str], user_id: int, actor_display_name: str = "کاربر") -> list[int]:
        names = [name.strip() for name in names if name.strip()]
        if not names:
            raise TripError("لطفاً حداقل یک مورد وارد کنید.")
        with self.db.transaction() as cx:
            trip = cx.fetchone("SELECT * FROM trips WHERE id=?", (trip_id,))
            if not trip:
                raise NotFound("سفر پیدا نشد.")
            if self._lock_if_due(cx, trip) or trip["status"] == "locked":
                raise Locked()
            item_ids: list[int] = []
            for name in names:
                item_id = cx.insert_returning_id(
                    "INSERT INTO items(trip_id,name,created_by,created_at,status) VALUES(?,?,?,?,?) RETURNING id",
                    (trip_id, name, user_id, now_iso(), "unchecked"),
                )
                self._record_activity(cx, trip_id, ITEM_ADDED, f"{actor_display_name.strip() or 'کاربر'} آیتم «{name}» را اضافه کرد.", actor_display_name, item_id=item_id)
                item_ids.append(int(item_id))
            self._mark_mutation()
            return item_ids

    @staticmethod
    def _apply_contribution_events(events: list[dict[str, Any]]) -> tuple[str, ...]:
        contributors: list[str] = []
        for event in events:
            action = str(event.get("type", ""))
            actor = str(event.get("actor") or "کاربر").strip() or "کاربر"
            if action == ITEM_CHECKED:
                if actor not in contributors:
                    contributors.append(actor)
            elif action == ITEM_UNCHECKED:
                if actor in contributors:
                    contributors.remove(actor)
        return tuple(contributors)

    def _contributors_for_item(self, cx: Database, item_id: int) -> tuple[str, ...]:
        events = cx.fetchall("SELECT type, actor FROM actions_history WHERE item_id=? ORDER BY id", (item_id,))
        return self._apply_contribution_events(events)

    def toggle_contribution(self, trip_id: int, item_id: int, user_id: int, display_name: str, actor_display_name: str | None = None) -> Change:
        del user_id
        with self.db.transaction() as cx:
            trip = cx.fetchone("SELECT * FROM trips WHERE id=?", (trip_id,))
            if not trip:
                raise NotFound("سفر پیدا نشد.")
            if self._lock_if_due(cx, trip) or trip["status"] == "locked":
                raise Locked()
            item = cx.fetchone("SELECT * FROM items WHERE id=? AND trip_id=? AND status <> 'deleted'", (item_id, trip_id))
            if not item:
                raise NotFound("این مورد دیگر وجود ندارد.")
            actor = (display_name.strip() or "هم‌گروهی")
            current_contributors = list(self._contributors_for_item(cx, item_id))
            if actor in current_contributors:
                current_contributors.remove(actor)
                claimed = False
                action = ITEM_UNCHECKED
                duplicate_names: tuple[str, ...] = ()
            else:
                duplicate_names = tuple(current_contributors)
                current_contributors.append(actor)
                claimed = True
                action = ITEM_CHECKED
            next_status = "checked" if current_contributors else "unchecked"
            cx.execute("UPDATE items SET status=? WHERE id=?", (next_status, item_id))
            actor = (actor_display_name or display_name).strip() or "هم‌گروهی"
            desc = f"{actor.strip() or 'کاربر'} آیتم «{item['name']}» را {'تیک زد' if claimed else 'از حالت تیک خارج کرد'}."
            self._record_activity(cx, trip_id, action, desc, actor, item_id=item_id)
            self._mark_mutation()
            return Change(item_id, claimed, duplicate_names)

    def delete_item(self, trip_id: int, item_id: int, user_id: int, actor_display_name: str = "کاربر") -> None:
        self.delete_items(trip_id, [item_id], user_id, actor_display_name)

    def delete_items(self, trip_id: int, item_ids: list[int], user_id: int, actor_display_name: str = "کاربر") -> int:
        del user_id
        if not item_ids:
            raise TripError("لطفاً حداقل یک مورد برای حذف انتخاب کنید.")
        with self.db.transaction() as cx:
            trip = cx.fetchone("SELECT * FROM trips WHERE id=?", (trip_id,))
            if not trip:
                raise NotFound("سفر پیدا نشد.")
            if self._lock_if_due(cx, trip) or trip["status"] == "locked":
                raise Locked()
            unique_ids = list(dict.fromkeys(item_ids))
            placeholders = ",".join("?" for _ in unique_ids)
            items = cx.fetchall(
                f"SELECT * FROM items WHERE trip_id=? AND status <> 'deleted' AND id IN ({placeholders})",
                (trip_id, *unique_ids),
            )
            items_by_id = {int(item["id"]): item for item in items}
            if len(items_by_id) != len(unique_ids):
                raise NotFound("یکی از موارد دیگر وجود ندارد.")
            for item_id in unique_ids:
                cx.execute("UPDATE items SET status='deleted' WHERE id=?", (item_id,))
                self._record_activity(
                    cx,
                    trip_id,
                    ITEM_DELETED,
                    f"{actor_display_name.strip() or 'کاربر'} آیتم «{items_by_id[item_id]['name']}» را حذف کرد.",
                    actor_display_name,
                    item_id=item_id,
                )
            self._mark_mutation()
            return len(unique_ids)

    @staticmethod
    def _record_activity(
        cx: Database,
        trip_id: int,
        action: str,
        desc: str,
        actor_name: str,
        item_id: int | None = None,
        *,
        user_id: int = 0,
    ) -> None:
        del user_id
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()
        cx.execute(
            'INSERT INTO actions_history(trip_id,item_id,type,"desc",actor,"timestamp",expires_at) VALUES(?,?,?,?,?,?,?)',
            (trip_id, item_id, action, desc, actor_name.strip() or "کاربر", now_iso(), expires_at),
        )

    def page(self, trip_id: int, page: int = 0) -> tuple[list[Any], int, int]:
        self.refresh_lock(trip_id)
        total = int(self.db.scalar("SELECT COUNT(*) FROM items WHERE trip_id=? AND status <> 'deleted'", (trip_id,)) or 0)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(page, 0), pages - 1)
        items = self.db.fetchall(
            "SELECT id, trip_id, name, created_by, created_at, status FROM items WHERE trip_id=? AND status <> 'deleted' ORDER BY id LIMIT ? OFFSET ?",
            (trip_id, PAGE_SIZE, page * PAGE_SIZE),
        )
        for item in items:
            contributors = self._contributors_for_item(self.db, int(item["id"]))
            item["contributors"] = "، ".join(contributors)
        return items, page, pages

    def progress(self, trip_id: int) -> tuple[int, int]:
        row = self.db.fetchone(
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE status='checked') AS claimed FROM items WHERE trip_id=? AND status <> 'deleted'",
            (trip_id,),
        )
        if not row:
            return 0, 0
        return int(row["claimed"]), int(row["total"])

    def activities(self, trip_id: int, limit: int = 10) -> list[Any]:
        rows = self.db.fetchall(
            'SELECT id, trip_id, item_id, type, "desc", actor, "timestamp" FROM actions_history WHERE trip_id=? ORDER BY id DESC LIMIT ?',
            (trip_id, limit),
        )
        item_ids = [int(row["item_id"]) for row in rows if row.get("item_id") is not None]
        item_names_by_id: dict[int, str] = {}
        if item_ids:
            unique_ids = sorted(set(item_ids))
            placeholders = ",".join("?" for _ in unique_ids)
            item_rows = self.db.fetchall(
                f"SELECT id, name FROM items WHERE id IN ({placeholders})",
                tuple(unique_ids),
            )
            item_names_by_id = {int(item["id"]): str(item["name"]) for item in item_rows}
        for row in rows:
            row["action"] = row.pop("type")
            row["actor_name"] = row.pop("actor")
            row["created_at"] = row.pop("timestamp")
            item_id = row.get("item_id")
            row["item_name"] = item_names_by_id.get(int(item_id)) if item_id is not None else None
        return rows
