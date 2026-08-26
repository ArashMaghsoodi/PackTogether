from __future__ import annotations

from datetime import datetime

from .service import PAGE_SIZE

_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

def fa_number(value: int | str) -> str:
    return str(value).translate(_DIGITS)

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
        keyboard += [[("➕ افزودن مورد", f"add:{trip['id']}"), ("🗑️ مدیریت", f"manage:{trip['id']}")], [("📋 فعالیت‌ها", f"activity:{trip['id']}")]]
    else:
        keyboard.append([("📋 فعالیت‌ها", f"activity:{trip['id']}")])
    return "\n".join(lines), keyboard

def format_departure(value: str, timezone_name: str) -> str:
    try:
        date = datetime.fromisoformat(value)
        return f"{fa_number(date.strftime('%Y/%m/%d'))}، ساعت {fa_number(date.strftime('%H:%M'))} ({timezone_name})"
    except ValueError:
        return "زمان نامشخص"
