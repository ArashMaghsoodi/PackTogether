from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import jdatetime

TEHRAN = "Asia/Tehran"
DATE_RE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$")
TIME_RE = re.compile(r"^(\d{1,2}):(\d{1,2})$")
MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")
PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _ascii_digits(value: str) -> str:
    return value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))


def parse_jalali_date(value: str) -> tuple[int, int, int]:
    match = DATE_RE.fullmatch(_ascii_digits(value.strip()))
    if not match:
        raise ValueError("❌ تاریخ واردشده معتبر نیست.\nلطفاً تاریخ را به شکل زیر وارد کنید:\n1405.06.17")
    parts = tuple(map(int, match.groups()))
    try:
        jdatetime.date(*parts)
    except ValueError as error:
        raise ValueError("❌ تاریخ واردشده معتبر نیست.\nلطفاً تاریخ را به شکل زیر وارد کنید:\n1405.06.17") from error
    return parts


def parse_time(value: str) -> tuple[int, int]:
    match = TIME_RE.fullmatch(_ascii_digits(value.strip()))
    if not match:
        raise ValueError("❌ ساعت واردشده معتبر نیست.\nلطفاً ساعت را به شکل زیر وارد کنید:\n14:30")
    hour, minute = map(int, match.groups())
    if hour > 23 or minute > 59:
        raise ValueError("❌ ساعت واردشده معتبر نیست.\nلطفاً ساعت را به شکل زیر وارد کنید:\n14:30")
    return hour, minute


def jalali_today(now: datetime | None = None, timezone_name: str = TEHRAN) -> tuple[int, int, int]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Current time must be timezone-aware")
    local = current.astimezone(ZoneInfo(timezone_name))
    converted = jdatetime.datetime.fromgregorian(datetime=local)
    return converted.year, converted.month, converted.day


def validate_departure_date(date_value: str, now: datetime | None = None, timezone_name: str = TEHRAN) -> tuple[int, int, int]:
    parsed = parse_jalali_date(date_value)
    if parsed < jalali_today(now, timezone_name):
        raise ValueError("❌ تاریخ حرکت نمی‌تواند در گذشته باشد.\n\nلطفاً یک تاریخ آینده یا امروز را وارد کنید.")
    return parsed


def jalali_to_utc(date_value: str, time_value: str, timezone_name: str = TEHRAN) -> datetime:
    year, month, day = parse_jalali_date(date_value)
    hour, minute = parse_time(time_value)
    gregorian = jdatetime.datetime(year, month, day, hour, minute).togregorian()
    local = gregorian.replace(tzinfo=ZoneInfo(timezone_name))
    return local.astimezone(timezone.utc)


def format_departure(value: str, timezone_name: str = TEHRAN) -> str:
    local = datetime.fromisoformat(value).astimezone(ZoneInfo(timezone_name))
    jalali = jdatetime.datetime.fromgregorian(datetime=local)
    return f"{jalali.day} {MONTHS[jalali.month - 1]} {jalali.year}، ساعت {jalali.hour:02d}:{jalali.minute:02d}".translate(PERSIAN_DIGITS)
