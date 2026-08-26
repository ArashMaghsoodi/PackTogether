from __future__ import annotations

try:
    from .date_utils import format_departure as format_shamsi_departure
except ImportError:
    from packtogether.date_utils import format_departure as format_shamsi_departure

ACTIVITY_LABELS = {
    "item_added": "آیتم «{item}» را اضافه کرد.",
    "item_checked": "آیتم «{item}» را تیک زد.",
    "item_unchecked": "تیک آیتم «{item}» را برداشت.",
    "item_deleted": "آیتم «{item}» را حذف کرد.",
    "trip_locked": "سفر را شروع کرد و چک‌لیست را قفل کرد.",
}

from .service import PAGE_SIZE

_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

def fa_number(value: int | str) -> str:
    return str(value).translate(_DIGITS)


def render_activity_line(trip_name: str, activity) -> str:
    actor = activity["actor_name"] or "کاربر"
    if activity["action"] == "trip_created":
        return f"{actor} سفر «{trip_name}» را ساخت."
    sentence = ACTIVITY_LABELS.get(activity["action"], "تغییری ثبت کرد.").format(item=activity["item_name"] or "نامشخص")
    return f"{trip_name}: {actor} {sentence}"

def render_checklist(trip, items, page: int, pages: int, packed: int, total: int) -> tuple[str, list[list[tuple[str, str]]]]:
    percent = int(packed * 16 / total) if total else 0
    bar = "█" * percent + "░" * (16 - percent)
    title = "🔒 " + trip["name"] + " — سفر شروع شد" if trip["status"] == "locked" else "🧳 " + trip["name"]
    lines = [title, "", f"{fa_number(packed)} / {fa_number(total)} مورد آماده است", bar, ""]
    keyboard = []
    for item in items:
        contributors = f" ({item['contributors']})" if item["contributors"] else ""
        mark = "✓" if item["contributors"] else " "
        lines.append(f"[{mark}] {item['name']}{contributors}")
        keyboard.append([(f"[{mark}] {item['name']}", f"item:{trip['id']}:{item['id']}")])
    if trip["status"] == "packing":
        lines += ["", f"🕐 زمان حرکت: {format_departure(trip['departure_at'], trip['timezone'])}"]
        if pages > 1:
            keyboard.append([( "◀ قبلی", f"page:{trip['id']}:{page-1}"), (f"{fa_number(page+1)} / {fa_number(pages)}", "noop"), ("بعدی ▶", f"page:{trip['id']}:{page+1}")])
        keyboard += [[("➕ افزودن مورد", f"add:{trip['id']}"), ("🗑️ حذف آیتم", f"manage:{trip['id']}")], [("📋 فعالیت‌ها", f"activity:{trip['id']}")]]
    else:
        keyboard.append([("📋 تاریخچه فعالیت‌ها", f"activity:{trip['id']}")])
    return "\n".join(lines), keyboard

def format_departure(value: str, timezone_name: str) -> str:
    try:
        return format_shamsi_departure(value, timezone_name)
    except (TypeError, ValueError):
        return "زمان نامشخص"
