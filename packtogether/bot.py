from __future__ import annotations

import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .db import Database
from .service import Locked, NotFound, TripError, TripService
from .ui import render_checklist

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
load_dotenv()

def markup(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows])

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
    context.user_data["newtrip"] = "name"
    await update.effective_message.reply_text("نام سفر را بفرستید.")

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("newtrip")
    if state == "name":
        context.user_data["trip_name"] = update.effective_message.text.strip(); context.user_data["newtrip"] = "departure"
        await update.effective_message.reply_text("زمان حرکت را با منطقه زمانی بفرستید؛ مثلاً 2026-08-27T08:00:00+03:30.")
        return
    if state == "departure":
        try:
            parsed = datetime.fromisoformat(update.effective_message.text.strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
            if parsed.tzinfo is None:
                raise ValueError("لطفاً منطقه زمانی را هم وارد کنید؛ مثلاً +03:30.")
            departure = parsed.isoformat()
            service = context.application.bot_data["service"]
            trip_id = service.create_trip(update.effective_chat.id, context.user_data["trip_name"], departure, str(parsed.tzinfo), update.effective_user.id)
            context.user_data.pop("newtrip", None)
            await show(update, service, service.trip_for_chat(update.effective_chat.id), main_message=True)
            return
        except (ValueError, TripError) as error:
            await update.effective_message.reply_text(str(error) or "زمان واردشده درست نیست.")
            return
    add_trip = context.user_data.get("add_item")
    if add_trip:
        try:
            service = context.application.bot_data["service"]; service.add_item(add_trip, update.effective_message.text, update.effective_user.id)
            context.user_data.pop("add_item", None); await show(update, service, service.trip_for_chat(update.effective_chat.id))
        except (TripError, NotFound) as error:
            await update.effective_message.reply_text(str(error))

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    service = context.application.bot_data["service"]
    try:
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
            rows.append([("لغو مدیریت", f"cancel:{trip['id']}")])
            await query.answer(); await query.message.reply_text("🗑️ برای حذف، روی مورد موردنظر بزنید.", reply_markup=markup(rows))
    except Locked: await query.answer("🔒 این سفر شروع شده و چک‌لیست قفل است.", show_alert=True)
    except (TripError, ValueError) as error: await query.answer(str(error), show_alert=True)
    except Exception: log.exception("callback failed")

def build_application() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "replace-with-botfather-token":
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Set it in .env or export it before starting the bot.")
    application = Application.builder().token(token).build()
    application.bot_data["service"] = TripService(Database(os.getenv("DATABASE_PATH", "packtogether.sqlite3")))
    application.add_handler(CommandHandler("start", start)); application.add_handler(CommandHandler("newtrip", new_trip))
    application.add_handler(CallbackQueryHandler(callback)); application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    application.job_queue.run_repeating(lock_due_trips, interval=30, first=5)
    return application

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
