from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.request import HTTPXRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

if __package__:
    from .date_utils import TEHRAN, jalali_to_utc, validate_departure_date
    from .db import mirror_from_env, database_from_env
    from .service import ITEM_ADDED, ITEM_CHECKED, ITEM_DELETED, ITEM_UNCHECKED, TRIP_CREATED, TRIP_LOCKED, Locked, NotFound, TripError, TripService
    from .ui import render_activity_line, render_checklist
else:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from packtogether.date_utils import TEHRAN, jalali_to_utc, validate_departure_date
    from packtogether.db import mirror_from_env, database_from_env
    from packtogether.service import ITEM_ADDED, ITEM_CHECKED, ITEM_DELETED, ITEM_UNCHECKED, TRIP_CREATED, TRIP_LOCKED, Locked, NotFound, TripError, TripService
    from packtogether.ui import render_activity_line, render_checklist

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
load_dotenv()

SYNC_JOB_NAME = "mirror-sync"
SYNC_IDLE_SECONDS = 30
SYNC_MAX_UNSYNCED_ACTIONS = 150
SYNC_MAX_DIRTY_AGE_SECONDS = 10 * 60


def markup(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows])


def cancel_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="setup_cancel")]])


DATE_PROMPT = "📅 تاریخ حرکت سفر رو بفرست.\n\nمثال:\n1405.06.17"
TIME_PROMPT = "🕐 ساعت حرکت رو بفرست.\n\nمثال:\n14:30"


def _sync_log_level() -> str:
    return (os.getenv("SYNC_LOG_LEVEL") or "normal").strip().lower()


def _sync_log_verbose() -> bool:
    return (os.getenv("SYNC_LOG_VERBOSE") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _dev_id() -> int | None:
    raw = (os.getenv("DEV_ID") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("DEV_ID is not numeric: %s", raw)
        return None


def _allow_sync_log(kind: str) -> bool:
    level = _sync_log_level()
    if kind == "error":
        return True
    if level == "errors":
        return False
    if kind == "verbose":
        return level == "verbose" or _sync_log_verbose()
    return level in {"normal", "verbose"}


async def _send_sync_log(application: Application, text: str, kind: str = "normal") -> None:
    if not _allow_sync_log(kind):
        return
    dev_id = _dev_id()
    if dev_id is None:
        return
    try:
        await application.bot.send_message(chat_id=dev_id, text=text)
    except Exception:
        log.exception("failed to send sync log to DEV_ID")


def _dirty_age_seconds(state: dict) -> int:
    dirty_since = state.get("dirty_since")
    if not dirty_since:
        return 0
    try:
        ts = datetime.fromisoformat(str(dirty_since)).astimezone(timezone.utc)
    except Exception:
        return 0
    return max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))


def _sync_delay_seconds(state: dict) -> int:
    unsynced = int(state.get("unsynced_actions") or 0)
    if unsynced >= SYNC_MAX_UNSYNCED_ACTIONS:
        return 0
    if _dirty_age_seconds(state) >= SYNC_MAX_DIRTY_AGE_SECONDS:
        return 0
    return SYNC_IDLE_SECONDS


async def schedule_sync(application: Application, reason: str) -> None:
    service = application.bot_data.get("service")
    mirror = application.bot_data.get("mirror")
    if not service or mirror is None:
        return

    state = service.db.get_sync_state()
    if not int(state.get("dirty") or 0):
        return

    for job in application.job_queue.get_jobs_by_name(SYNC_JOB_NAME):
        job.schedule_removal()

    delay = _sync_delay_seconds(state)
    application.job_queue.run_once(run_sync_job, when=delay, name=SYNC_JOB_NAME, data={"reason": reason})
    await _send_sync_log(application, f"🛰️ Sync scheduled ({reason}) in {delay}s | unsynced={int(state.get('unsynced_actions') or 0)}", "verbose")


async def run_sync_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    application = context.application
    service = application.bot_data.get("service")
    mirror = application.bot_data.get("mirror")
    if not service or mirror is None:
        return

    reason = (context.job.data or {}).get("reason", "unknown")
    await _send_sync_log(application, f"🛰️ Sync started ({reason})", "verbose")
    started = datetime.now(timezone.utc)

    try:
        result = service.sync_to_mirror(mirror)
    except Exception as error:
        service.db.mark_sync_failed(str(error))
        log.exception("mirror sync failed")
        await _send_sync_log(application, f"❌ Sync failed: {error}", "error")
        context.application.job_queue.run_once(run_sync_job, when=60, name=SYNC_JOB_NAME, data={"reason": "retry-after-error"})
        return

    elapsed = int((datetime.now(timezone.utc) - started).total_seconds())
    counts = result.get("counts", {})

    if result.get("synced"):
        await _send_sync_log(
            application,
            f"✅ Sync done in {elapsed}s | trips={counts.get('trips', 0)} items={counts.get('items', 0)} actions={counts.get('actions', 0)} | cleared={result.get('cleared')}",
            "normal",
        )

    state = service.db.get_sync_state()
    if int(state.get("dirty") or 0):
        await schedule_sync(application, "post-sync-still-dirty")


async def touch_local_mutation(context: ContextTypes.DEFAULT_TYPE, reason: str) -> None:
    await schedule_sync(context.application, reason)


async def show(update, service, trip, page=0, main_message=False):
    trip = service.trip_by_id(int(trip["id"]))
    items, page, pages = service.page(trip["id"], page)
    text, rows = render_checklist(trip, items, page, pages, *service.progress(trip["id"]))

    async def send_checklist():
        message = await update.get_bot().send_message(trip["chat_id"], text, reply_markup=markup(rows))
        service.attach_message(trip["id"], message.message_id)

    if update.callback_query and not main_message:
        await update.callback_query.edit_message_text(text, reply_markup=markup(rows))
    elif trip["message_id"]:
        try:
            await update.get_bot().edit_message_text(text, chat_id=trip["chat_id"], message_id=trip["message_id"], reply_markup=markup(rows))
        except BadRequest as error:
            error_text = str(error).lower()
            if "not modified" in error_text:
                return
            if not any(phrase in error_text for phrase in ("message to edit not found", "message can't be edited", "message cannot be edited")):
                raise
            await send_checklist()
    else:
        await send_checklist()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = context.application.bot_data["service"]
    try:
        trips = service.trips_for_chat(update.effective_chat.id)
        if len(trips) > 1:
            await trip_status_command(update, context)
            return
        await show(update, service, trips[0])
    except NotFound:
        await update.effective_message.reply_text("سلام 👋\nبرای ساخت چک‌لیست سفر، دستور /newtrip رو داخل گروه بفرست.")


async def new_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = context.application.bot_data["service"]
    active_count = len(service.active_trips(update.effective_chat.id))
    if active_count >= 3:
        await update.effective_message.reply_text("⚠️ این گروه حداکثر ۳ سفر فعال می‌تواند داشته باشد.\n\nاول یکی از سفرهای فعال را مدیریت کنید یا تا زمان حرکتشان صبر کنید.")
        return
    if not service.begin_setup(update.effective_chat.id, update.effective_user.id):
        await update.effective_message.reply_text("ℹ️ شما همین الان در حال ساخت یک سفر هستید.\nاول همون رو کامل کن یا لغوش کن.")
        return
    await update.effective_message.reply_text("🧳 بزن بریم سفر جدید!\n\nاسم سفر رو وارد کن:", reply_markup=cancel_markup())


async def private_new_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("⚠️ برای ساخت سفر باید /newtrip رو داخل گروهی بفرستی که PackTogether اونجاست.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("راهنما ✨\nبرای ساخت چک‌لیست، داخل گروه دستور /newtrip رو بفرست.")


async def trip_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = context.application.bot_data["service"]
    try:
        trips = service.trips_for_chat(update.effective_chat.id)
    except NotFound:
        trips = []
    if not trips:
        await update.effective_message.reply_text("هنوز سفری برای این گروه ثبت نشده است.")
        return
    lines = [f"{trip['name']}: {'هنوز شروع نشده' if trip['status'] == 'packing' else 'شروع و قفل شده'}" for trip in trips]
    keyboard = []
    for trip in trips:
        label = f"{'🟢' if trip['status'] == 'packing' else '🔒'} {trip['name']}"
        keyboard.append([(label, f"select:{trip['id']}"), ("🚀", f"start:{trip['id']}"), ("🗑️", f"delete_trip:{trip['id']}"), ("✏️", f"edit_trip:{trip['id']}" )])
    await update.effective_message.reply_text("\n".join(lines), reply_markup=markup(keyboard))


async def setup_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = context.application.bot_data["service"]
    if service.cancel_setup(update.effective_chat.id, update.effective_user.id):
        await update.effective_message.reply_text("❌ ساخت سفر لغو شد. هر زمان خواستی دوباره /newtrip رو بزن.")


def setup_input(service: TripService, chat_id: int, user_id: int, text: str, now=None, actor_display_name: str = "هم‌گروهی") -> tuple[str, int | None]:
    setup = service.setup(chat_id, user_id)
    if not setup:
        return "", None
    text = text.strip()
    if text == "لغو":
        service.cancel_setup(chat_id, user_id)
        return "❌ ساخت سفر لغو شد. هر زمان خواستی دوباره /newtrip رو بزن.", None
    if setup["state"] == "name":
        if not text or len(text) > 80:
            return "❌ نام سفر باید بین ۱ تا ۸۰ نویسه باشد.", None
        service.update_setup(chat_id, user_id, "date", trip_name=text)
        return DATE_PROMPT, None
    if setup["state"] == "date":
        parsed = validate_departure_date(text, now, TEHRAN)
        normalized_date = ".".join(f"{part:02d}" if index else str(part) for index, part in enumerate(parsed))
        service.update_setup(chat_id, user_id, "time", departure_date=normalized_date)
        return TIME_PROMPT, None
    departure = jalali_to_utc(setup["departure_date"], text, TEHRAN)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Current time must be timezone-aware")
    if departure <= current.astimezone(timezone.utc):
        raise ValueError("❌ زمان حرکت نمی‌تواند در گذشته باشد.\n\nلطفاً ساعت آینده‌ای را وارد کنید.")
    trip_id = service.complete_setup(chat_id, user_id, departure.isoformat(), actor_display_name)
    return "✅ سفر آماده شد! وقتشه وسایل رو تیک بزنید 🚀", trip_id


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = context.application.bot_data["service"]
    setup = service.setup(update.effective_chat.id, update.effective_user.id)
    if setup:
        try:
            response, trip_id = setup_input(service, update.effective_chat.id, update.effective_user.id, update.effective_message.text, actor_display_name=update.effective_user.first_name)
            await update.effective_message.reply_text(response, reply_markup=cancel_markup() if not trip_id else None)
            if trip_id:
                await show(update, service, service.trip_by_id(trip_id), main_message=True)
                await touch_local_mutation(context, "setup-complete")
        except (ValueError, TripError, NotFound) as error:
            prompt = DATE_PROMPT if setup["state"] == "date" else TIME_PROMPT if setup["state"] == "time" else None
            message = str(error)
            if prompt:
                message = f"{message}\n\n{prompt}"
            await update.effective_message.reply_text(message, reply_markup=cancel_markup() if prompt else None)
        return
    edit_trip = context.user_data.get("edit_trip")
    if edit_trip:
        try:
            trip = service.trip_by_id(int(edit_trip))
            text = update.effective_message.text.strip()
            if "|" in text:
                name_part, time_part = [part.strip() for part in text.split("|", 1)]
                new_name = name_part or trip["name"]
                if not time_part:
                    service.update_trip(trip["id"], new_name, None, update.effective_user.first_name)
                    context.user_data.pop("edit_trip", None)
                    await show(update, service, trip)
                    return
                if " " in time_part:
                    date_part, time_part = time_part.split(None, 1)
                    departure = jalali_to_utc(date_part, time_part, TEHRAN).isoformat()
                else:
                    departure = jalali_to_utc(time_part, "14:00", TEHRAN).isoformat()
                service.update_trip(trip["id"], new_name, departure, update.effective_user.first_name)
            else:
                service.update_trip(trip["id"], text or None, None, update.effective_user.first_name)
            context.user_data.pop("edit_trip", None)
            await show(update, service, trip)
            await touch_local_mutation(context, "update-trip")
        except (TripError, NotFound, ValueError) as error:
            await update.effective_message.reply_text(str(error))
        return

    add_trip = context.user_data.get("add_item")
    if add_trip:
        try:
            trip = service.trip_by_id(int(add_trip))
            service.add_items(trip["id"], update.effective_message.text.splitlines(), update.effective_user.id, update.effective_user.first_name)
            context.user_data.pop("add_item", None)
            await show(update, service, trip)
            await touch_local_mutation(context, "add-items")
        except (TripError, NotFound) as error:
            await update.effective_message.reply_text(str(error))


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    service = context.application.bot_data["service"]
    try:
        if query.data == "setup_cancel":
            if service.cancel_setup(update.effective_chat.id, update.effective_user.id):
                await query.answer()
                await query.edit_message_text("❌ ساخت سفر لغو شد. هر زمان خواستی دوباره /newtrip رو بزن.")
            else:
                await query.answer("این جلسه دیگه فعال نیست.", show_alert=True)
            return

        kind, trip_id, *rest = query.data.split(":")
        trip = service.trip_by_id(int(trip_id))
        if trip["chat_id"] != update.effective_chat.id:
            raise NotFound("این چک‌لیست متعلق به این گروه نیست.")
        context.user_data["selected_trip"] = trip["id"]

        if kind == "item":
            item_id = int(rest[0])
            if context.user_data.get("manage_trip") == trip["id"]:
                item = service.db.fetchone("SELECT name FROM items WHERE id=? AND trip_id=? AND status <> 'deleted'", (item_id, trip["id"]))
                if not item:
                    raise NotFound("این مورد دیگر وجود ندارد.")
                selected = set(context.user_data.get("delete_items", []))
                if item_id in selected:
                    selected.remove(item_id)
                else:
                    selected.add(item_id)
                context.user_data["delete_items"] = list(selected)
                await query.answer()
                await render_manage(query, service, trip, context)
                return
            change = service.toggle_contribution(trip["id"], item_id, update.effective_user.id, update.effective_user.first_name)
            if change.duplicate_names:
                await query.answer(f"ℹ️ {change.duplicate_names[0]} هم در حال آوردن این مورد است.", show_alert=False)
            else:
                await query.answer()
            await show(update, service, trip)
            await touch_local_mutation(context, "toggle-item")
        elif kind == "add":
            context.user_data["add_item"] = trip["id"]
            context.user_data["selected_trip"] = trip["id"]
            await query.answer()
            await query.message.reply_text(f"➕ در سفر «{trip['name']}» آیتم‌ها رو بفرست.\nهر مورد رو در یک خط جدا بنویس ✍️")
        elif kind == "page":
            await query.answer()
            await show(update, service, trip, int(rest[0]))
        elif kind == "confirm":
            item_ids = context.user_data.get("delete_items", [])
            if not item_ids:
                raise NotFound("هیچ موردی برای حذف انتخاب نشده است.")
            service.delete_items(trip["id"], item_ids, update.effective_user.id, update.effective_user.first_name)
            context.user_data.pop("delete_items", None)
            context.user_data.pop("manage_trip", None)
            await query.answer()
            await query.message.delete()
            await show(update, service, service.trip_for_chat(update.effective_chat.id), main_message=True)
            await touch_local_mutation(context, "delete-items")
        elif kind == "cancel":
            context.user_data.pop("delete_items", None)
            context.user_data.pop("manage_trip", None)
            await query.answer()
            await query.edit_message_text("↩️ حذف لغو شد.")
        elif kind == "start":
            await query.answer()
            await query.edit_message_text(
                "🚀 شروع فوری سفر\n\nبعد از شروع، افزودن، تیک‌زدن و حذف آیتم‌ها ممکن نیست.",
                reply_markup=markup([[("✅ بله، سفر را شروع کن", f"start_confirm:{trip['id']}"), ("↩️ لغو", f"cancel:{trip['id']}")]]),
            )
        elif kind == "start_confirm":
            service.start_trip(trip["id"], update.effective_user.id, update.effective_user.first_name)
            await query.answer()
            await query.message.delete()
            await show(update, service, service.trip_for_chat(update.effective_chat.id), main_message=True)
            await touch_local_mutation(context, "start-trip")
        elif kind == "activity":
            lines = ["🕓 فعالیت‌های اخیر", ""]
            lines.extend(render_activity_line(trip["name"], row) for row in service.activities(trip["id"]))
            await query.answer()
            await query.message.reply_text("\n".join(lines))
        elif kind == "manage":
            context.user_data["manage_trip"] = trip["id"]
            context.user_data["delete_items"] = []
            await query.answer()
            await render_manage(query, service, trip, context)
        elif kind == "select":
            context.user_data["selected_trip"] = trip["id"]
            await query.answer(f"سفر «{trip['name']}» انتخاب شد.", show_alert=False)
            await show(update, service, trip, main_message=True)
        elif kind == "delete_trip":
            await query.answer()
            await query.edit_message_text(
                "🗑️ حذف سفر\n\nاین سفر حذف می‌شود. مطمئن هستی؟",
                reply_markup=markup([[("✅ بله، حذف کن", f"delete_trip_confirm:{trip['id']}"), ("↩️ لغو", f"cancel:{trip['id']}")]]),
            )
        elif kind == "delete_trip_confirm":
            service.delete_trip(trip["id"], update.effective_user.id, update.effective_user.first_name)
            await query.answer("🗑️ سفر حذف شد.")
            await query.message.delete()
            await trip_status_command(update, context)
        elif kind == "edit_trip":
            context.user_data["edit_trip"] = trip["id"]
            await query.answer()
            await query.message.reply_text(
                f"✏️ برای ویرایش سفر «{trip['name']}» نام جدید و/یا تاریخ و ساعت را در یک پیام بفرست.\nمثال: «سفر تبریز | 1405.06.17 14:30»",
            )
    except Locked:
        await query.answer("🔒 این سفر شروع شده و چک‌لیست قفل است.", show_alert=True)
    except (TripError, ValueError) as error:
        await query.answer(str(error), show_alert=True)
    except Exception:
        log.exception("callback failed")


async def render_manage(query, service, trip, context):
    items, _, _ = service.page(trip["id"])
    selected = set(context.user_data.get("delete_items", []))
    rows = [[(f"{'✅' if item['id'] in selected else '⬜️'} {item['name']}", f"item:{trip['id']}:{item['id']}")] for item in items]
    if selected:
        rows.append([("🗑️ حذف موارد انتخاب‌شده", f"confirm:{trip['id']}")])
    rows.append([("↩️ خروج از مدیریت", f"cancel:{trip['id']}")])
    text = "🗂 حالت مدیریت آیتم‌ها\n\nمواردی را که می‌خواهی حذف شوند انتخاب کن."
    if selected:
        text += f"\n\n✅ {len(selected)} مورد انتخاب شده"
    await query.edit_message_text(text, reply_markup=markup(rows))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("update handling failed", exc_info=context.error)


def build_application() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "replace-with-botfather-token":
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Set it in .env or export it before starting the bot.")
    # Prevent inheriting system proxy settings for Telegram polling/session traffic.
    # Misconfigured proxy chains can cause intermittent RemoteProtocolError disconnects.
    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=10.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=5.0,
        httpx_kwargs={"trust_env": False},
    )
    get_updates_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=10.0,
        read_timeout=35.0,
        write_timeout=20.0,
        pool_timeout=5.0,
        httpx_kwargs={"trust_env": False},
    )

    application = (
        Application.builder()
        .token(token)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(set_command_menus)
        .build()
    )
    application.bot_data["service"] = TripService(database_from_env())
    application.bot_data["mirror"] = mirror_from_env()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", trip_status_command, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("newtrip", new_trip, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("newtrip", private_new_trip, filters=filters.ChatType.PRIVATE))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    application.add_error_handler(on_error)

    application.job_queue.run_repeating(lock_due_trips, interval=30, first=5)
    return application


async def set_command_menus(application: Application) -> None:
    await application.bot.set_my_commands(
        [BotCommand("start", "نمایش چک‌لیست"), BotCommand("help", "راهنما")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await application.bot.set_my_commands(
        [BotCommand("start", "نمایش چک‌لیست"), BotCommand("help", "راهنما"), BotCommand("status", "وضعیت سفرها"), BotCommand("newtrip", "ساخت سفر جدید")],
        scope=BotCommandScopeAllGroupChats(),
    )

    service = application.bot_data.get("service")
    mirror = application.bot_data.get("mirror")
    if service and mirror:
        state = service.db.get_sync_state()
        if int(state.get("dirty") or 0):
            await _send_sync_log(application, "🛰️ Startup found pending local changes. Scheduling sync.", "normal")
            await schedule_sync(application, "startup-pending")


async def lock_due_trips(context: ContextTypes.DEFAULT_TYPE):
    service = context.application.bot_data["service"]
    for trip in service.due_trips():
        try:
            if service.refresh_lock(trip["id"]) and trip["message_id"]:
                locked_trip = service.db.fetchone("SELECT * FROM trips WHERE id=?", (trip["id"],))
                items, page, pages = service.page(trip["id"])
                text, rows = render_checklist(locked_trip, items, page, pages, *service.progress(trip["id"]))
                await context.bot.edit_message_text(text, chat_id=trip["chat_id"], message_id=trip["message_id"], reply_markup=markup(rows))
                await touch_local_mutation(context, "auto-lock-due-trip")
        except Exception:
            log.exception("scheduled trip lock failed for %s", trip["id"])


if __name__ == "__main__":
    build_application().run_polling()
