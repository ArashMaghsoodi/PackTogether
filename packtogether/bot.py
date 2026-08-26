from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .calendar import TEHRAN, jalali_to_utc, validate_departure_date
from .db import Database
from .service import Locked, NotFound, TripError, TripService
from .ui import render_checklist

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
load_dotenv()

def markup(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows])

def cancel_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="setup_cancel")]])

DATE_PROMPT = "📅 تاریخ حرکت را وارد کنید.\n\nمثال:\n1405.06.17"
TIME_PROMPT = "🕐 ساعت حرکت را وارد کنید.\n\nمثال:\n14:30"

async def show(update, service, trip, page=0, main_message=False):
    trip = service.trip_for_chat(trip["chat_id"])
    items, page, pages = service.page(trip["id"], page)
    text, rows = render_checklist(trip, items, page, pages, *service.progress(trip["id"]))
    if update.callback_query and not main_message:
        await update.callback_query.edit_message_text(text, reply_markup=markup(rows))
    elif trip["message_id"]:
        await update.get_bot().edit_message_text(text, chat_id=trip["chat_id"], message_id=trip["message_id"], reply_markup=markup(rows))
    else:
        message = await update.effective_message.reply_text(text, reply_markup=markup(rows))
        service.attach_message(trip["id"], message.message_id)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = context.application.bot_data["service"]
    try:
        await show(update, service, service.trip_for_chat(update.effective_chat.id))
    except NotFound:
        await update.effective_message.reply_text("سلام! برای ساخت چک‌لیست سفر، نام و زمان حرکت را با /newtrip ثبت کنید.")

async def new_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = context.application.bot_data["service"]
    if service.active_trip(update.effective_chat.id):
        await update.effective_message.reply_text("⚠️ این گروه در حال حاضر یک سفر فعال دارد.\n\nلطفاً ابتدا همان سفر را به پایان برسانید یا تا زمان حرکت آن صبر کنید.")
        return
    if not service.begin_setup(update.effective_chat.id, update.effective_user.id):
        await update.effective_message.reply_text("ℹ️ شما در حال ساخت یک سفر هستید. ابتدا همان سفر را کامل یا لغو کنید.")
        return
    await update.effective_message.reply_text("🧳 ساخت سفر جدید\n\nنام سفر را وارد کنید:", reply_markup=cancel_markup())

async def private_new_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("⚠️ برای ساخت سفر، دستور /newtrip را داخل گروهی که PackTogether در آن عضو است ارسال کنید.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("برای ساخت چک‌لیست، در گروه دستور /newtrip را بفرستید.")

async def setup_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = context.application.bot_data["service"]
    if service.cancel_setup(update.effective_chat.id, update.effective_user.id):
        await update.effective_message.reply_text("❌ ساخت سفر لغو شد.")

def setup_input(service: TripService, chat_id: int, user_id: int, text: str, now=None) -> tuple[str, int | None]:
    setup = service.setup(chat_id, user_id)
    if not setup:
        return "", None
    text = text.strip()
    if text == "لغو":
        service.cancel_setup(chat_id, user_id)
        return "❌ ساخت سفر لغو شد.", None
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
    from datetime import datetime, timezone
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Current time must be timezone-aware")
    if departure <= current.astimezone(timezone.utc):
        raise ValueError("❌ زمان حرکت نمی‌تواند در گذشته باشد.\n\nلطفاً ساعت آینده‌ای را وارد کنید.")
    trip_id = service.complete_setup(chat_id, user_id, departure.isoformat())
    return "✅ سفر آماده شد!", trip_id

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = context.application.bot_data["service"]
    setup = service.setup(update.effective_chat.id, update.effective_user.id)
    if setup:
        try:
            response, trip_id = setup_input(service, update.effective_chat.id, update.effective_user.id, update.effective_message.text)
            await update.effective_message.reply_text(response, reply_markup=cancel_markup() if not trip_id else None)
            if trip_id:
                await show(update, service, service.trip_for_chat(update.effective_chat.id), main_message=True)
        except (ValueError, TripError, NotFound) as error:
            prompt = DATE_PROMPT if setup["state"] == "date" else TIME_PROMPT if setup["state"] == "time" else None
            message = str(error)
            if prompt:
                message = f"{message}\n\n{prompt}"
            await update.effective_message.reply_text(message, reply_markup=cancel_markup() if prompt else None)
        return
    add_trip = context.user_data.get("add_item")
    if add_trip:
        try:
            service.add_item(add_trip, update.effective_message.text, update.effective_user.id)
            context.user_data.pop("add_item", None); await show(update, service, service.trip_for_chat(update.effective_chat.id))
        except (TripError, NotFound) as error:
            await update.effective_message.reply_text(str(error))

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    service = context.application.bot_data["service"]
    try:
        if query.data == "setup_cancel":
            if service.cancel_setup(update.effective_chat.id, update.effective_user.id):
                await query.answer(); await query.edit_message_text("❌ ساخت سفر لغو شد.")
            else:
                await query.answer("این جلسه دیگر فعال نیست.", show_alert=True)
            return
        kind, trip_id, *rest = query.data.split(":")
        trip = service.trip_for_chat(update.effective_chat.id)
        if int(trip_id) != trip["id"]: raise NotFound("این چک‌لیست متعلق به این گروه نیست.")
        if kind == "item":
            item_id = int(rest[0])
            if context.user_data.get("manage_trip") == trip["id"]:
                item = service.db.connection.execute("SELECT name FROM items WHERE id=? AND trip_id=? AND deleted_at IS NULL", (item_id, trip["id"])).fetchone()
                if not item: raise NotFound("این مورد دیگر وجود ندارد.")
                context.user_data["delete_item"] = item_id
                await query.answer()
                await query.edit_message_text(f"🗑️ حذف مورد\n\n{item['name']}", reply_markup=markup([[('بله، حذف شود', f'confirm:{trip["id"]}:{item_id}'), ('خیر، لغو', f'cancel:{trip["id"]}')]]))
                return
            change = service.toggle_contribution(trip["id"], item_id, update.effective_user.id, update.effective_user.first_name)
            if change.duplicate_names:
                await query.answer(f"ℹ️ {change.duplicate_names[0]} هم در حال آوردن این مورد است.", show_alert=False)
            else:
                await query.answer()
            await show(update, service, trip)
        elif kind == "add":
            context.user_data["add_item"] = trip["id"]; await query.answer(); await query.message.reply_text("موردی که می‌خواهید اضافه کنید را ارسال کنید.")
        elif kind == "page": await query.answer(); await show(update, service, trip, int(rest[0]))
        elif kind == "confirm":
            if context.user_data.get("delete_item") != int(rest[0]): raise NotFound("درخواست حذف منقضی شده است.")
            service.delete_item(trip["id"], int(rest[0]), update.effective_user.id)
            context.user_data.pop("delete_item", None); context.user_data.pop("manage_trip", None)
            await query.answer()
            await query.message.delete()
            await show(update, service, service.trip_for_chat(update.effective_chat.id), main_message=True)
        elif kind == "cancel":
            context.user_data.pop("delete_item", None); context.user_data.pop("manage_trip", None)
            await query.answer()
            await query.edit_message_text("حذف لغو شد.")
        elif kind == "activity":
            lines = ["📋 فعالیت‌های اخیر", ""]
            labels = {"trip_created": "سفر را ساخت.", "item_added": "مورد را اضافه کرد.", "item_claimed": "مورد را برداشت.", "item_unclaimed": "مورد را از لیست خود برداشت.", "item_deleted": "مورد را حذف کرد.", "trip_locked": "سفر شروع شد و چک‌لیست قفل شد."}
            for row in service.activities(trip["id"]): lines.append(f"{row['item_name'] or 'سفر'}: {labels.get(row['action'], 'تغییری ثبت شد.')}")
            await query.answer(); await query.message.reply_text("\n".join(lines))
        elif kind == "manage":
            items, page, pages = service.page(trip["id"])
            context.user_data["manage_trip"] = trip["id"]
            rows = [[(f"{'✓' if item['contributors'] else ' '} {item['name']}", f"item:{trip['id']}:{item['id']}")] for item in items]
            rows.append([("لغو", f"cancel:{trip['id']}")])
            await query.answer(); await query.message.reply_text("🗑️ برای حذف، روی مورد موردنظر بزنید.", reply_markup=markup(rows))
    except Locked: await query.answer("🔒 این سفر شروع شده و چک‌لیست قفل است.", show_alert=True)
    except (TripError, ValueError) as error: await query.answer(str(error), show_alert=True)
    except Exception: log.exception("callback failed")

def build_application() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "replace-with-botfather-token":
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Set it in .env or export it before starting the bot.")
    application = Application.builder().token(token).post_init(set_command_menus).build()
    application.bot_data["service"] = TripService(Database(os.getenv("DATABASE_PATH", "packtogether.sqlite3")))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("newtrip", new_trip, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("newtrip", private_new_trip, filters=filters.ChatType.PRIVATE))
    application.add_handler(CallbackQueryHandler(callback)); application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    application.job_queue.run_repeating(lock_due_trips, interval=30, first=5)
    return application

async def set_command_menus(application: Application) -> None:
    await application.bot.set_my_commands(
        [BotCommand("start", "نمایش چک‌لیست"), BotCommand("help", "راهنما")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await application.bot.set_my_commands(
        [BotCommand("start", "نمایش چک‌لیست"), BotCommand("help", "راهنما"), BotCommand("newtrip", "ساخت سفر جدید")],
        scope=BotCommandScopeAllGroupChats(),
    )

async def lock_due_trips(context: ContextTypes.DEFAULT_TYPE):
    service = context.application.bot_data["service"]
    for trip in service.due_trips():
        try:
            if service.refresh_lock(trip["id"]) and trip["message_id"]:
                locked_trip = service.db.connection.execute("SELECT * FROM trips WHERE id=?", (trip["id"],)).fetchone()
                items, page, pages = service.page(trip["id"])
                text, rows = render_checklist(locked_trip, items, page, pages, *service.progress(trip["id"]))
                await context.bot.edit_message_text(text, chat_id=trip["chat_id"], message_id=trip["message_id"], reply_markup=markup(rows))
        except Exception:
            log.exception("scheduled trip lock failed for %s", trip["id"])

if __name__ == "__main__": build_application().run_polling()
