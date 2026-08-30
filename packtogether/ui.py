from __future__ import annotations

from datetime import datetime, timezone

try:
    from .date_utils import format_departure as format_shamsi_departure
except ImportError:
    from packtogether.date_utils import format_departure as format_shamsi_departure

from .service import PAGE_SIZE

ACTIVITY_LABELS = {
    "item_added": "آیتم «{item}» را اضافه کرد.",
    "item_checked": "آیتم «{item}» را تیک زد.",
    "item_unchecked": "تیک آیتم «{item}» را برداشت.",
    "item_deleted": "آیتم «{item}» را حذف کرد.",
    "trip_locked": "سفر را شروع کرد و چک‌لیست را قفل کرد.",
}

ACTIVITY_EMOJIS = {
    "trip_created": "✨",
    "item_added": "➕",
    "item_checked": "✅",
    "item_unchecked": "↩️",
    "item_deleted": "🗑️",
    "trip_locked": "🔒",
}

_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_number(value: int | str) -> str:
    return str(value).translate(_DIGITS)


def render_activity_line(trip_name: str, activity) -> str:
    actor = activity["actor_name"] or "کاربر"
    emoji = ACTIVITY_EMOJIS.get(activity["action"], "📝")
    desc = (activity.get("desc") or "").strip()

    if desc:
        if activity["action"] == "trip_created":
            return f"{emoji} {desc}"
        return f"{emoji} {trip_name}: {desc}"

    if activity["action"] == "trip_created":
        return f"{emoji} {actor} سفر «{trip_name}» را ساخت."
    sentence = ACTIVITY_LABELS.get(activity["action"], "تغییری ثبت کرد.").format(item=activity["item_name"] or "نامشخص")
    return f"{emoji} {trip_name}: {actor} {sentence}"


def render_checklist(trip, items, page: int, pages: int, packed: int, total: int) -> tuple[str, list[list[tuple[str, str]]]]:
    percent = int((packed * 100) / total) if total else 0
    filled = int(packed * 16 / total) if total else 0
    bar = "█" * filled + "░" * (16 - filled)

    title = f"🔒 {trip['name']}" if trip["status"] == "locked" else f"🧳 {trip['name']}"
    subtitle = "سفر شروع شد و چک‌لیست قفل است." if trip["status"] == "locked" else "بزن بریم برای جمع کردن وسایل!"

    timezone_name = trip.get("timezone") or "Asia/Tehran"

    lines = [
        title,
        subtitle,
        "",
        f"🕐 زمان حرکت: {format_departure(trip['departure_at'], timezone_name)}",
    ]

    if trip["status"] == "packing":
        countdown = format_countdown(trip["departure_at"])
        if countdown:
            lines.append(countdown)

    lines += [
        "",
        f"📦 پیشرفت: {fa_number(packed)} از {fa_number(total)} آیتم ({fa_number(percent)}٪)",
        bar,
        "",
        "📝 لیست وسایل:",
    ]

    keyboard = []
    if not items:
        lines.append("هنوز چیزی اضافه نشده؛ از دکمه پایین شروع کن ✨")

    for item in items:
        has_contributors = bool(item["contributors"])
        mark = "✅" if has_contributors else "⬜️"
        contributors = f" — 👥 {item['contributors']}" if item["contributors"] else ""
        lines.append(f"{mark} {item['name']}{contributors}")
        keyboard.append([(f"{mark} {item['name']}", f"item:{trip['id']}:{item['id']}")])

    if trip["status"] == "packing":
        if pages > 1:
            keyboard.append([
                ("◀ قبلی", f"page:{trip['id']}:{page - 1}"),
                (f"📚 {fa_number(page + 1)} / {fa_number(pages)}", "noop"),
                ("بعدی ▶", f"page:{trip['id']}:{page + 1}"),
            ])
        keyboard += [
            [("➕ افزودن آیتم", f"add:{trip['id']}"), ("🗂 مدیریت آیتم‌ها", f"manage:{trip['id']}")],
            [("🚀 شروع فوری سفر", f"start:{trip['id']}")],
            [("🕓 فعالیت‌ها", f"activity:{trip['id']}")],
        ]
    else:
        keyboard.append([("🕓 تاریخچه فعالیت‌ها", f"activity:{trip['id']}")])

    return "\n".join(lines), keyboard


def format_departure(value: str, timezone_name: str) -> str:
    try:
        return format_shamsi_departure(value, timezone_name)
    except (TypeError, ValueError):
        return "زمان نامشخص"


def format_countdown(value: str) -> str | None:
    try:
        departure = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if departure.tzinfo is None:
        return None
    remaining_minutes = int((departure.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() // 60)
    if remaining_minutes <= 0:
        return None

    days, rem = divmod(remaining_minutes, 24 * 60)
    hours, minutes = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{fa_number(days)} روز")
    if hours:
        parts.append(f"{fa_number(hours)} ساعت")
    if minutes or not parts:
        parts.append(f"{fa_number(minutes)} دقیقه")
    return f"⏳ تا شروع سفر: {' و '.join(parts[:2])}"